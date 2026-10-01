"""#1818 P4 + P6 — the request path issues no DDL, and its DB reads are off the loop.

The 2026-09-03 outage (``docs/incidents/2026-09-03-paper-advance-ddl-wedge.md``)
had two ends on the serving side, and this file guards both.

**P6 — the serving path ran schema DDL.** Eleven request handlers called
``init_db()`` so their transitional columns would exist before they read:
``api/paper_routes.py`` (5 sites), ``api/selection_bias_routes.py`` (2),
``api/leaderboard_routes.py`` (1), ``api/strategies_routes.py`` (2) and
``services/live_rigor_gate.py`` (1). Every one was reached from an ``async def``
endpoint, so an ordinary page load issued ``ALTER TABLE … ADD COLUMN IF NOT
EXISTS`` — and a *waiting* AccessExclusiveLock request queues every later reader
of that table behind it. #1819 bounded that wait (5 s per statement, 10 s per
call); this removes it. Schema belongs to the migrate task plus the one
boot-time call in ``main.py``.

**P4 — the reads ran on the event loop.** ``GET /api/strategies/generated`` is
``async def`` and called ``session.query(...)`` directly. On 2026-09-03 that
query took 5,648,772 ms, and because it was blocking the loop rather than a
worker thread, ``/health`` stopped answering too — so the ALB saw two dead
targets and 504'd the entire site instead of one slow endpoint. The detector for
"on the loop" and the reason it records rather than raises are documented in
``tests/loop_thread.py``.

Both halves are two-sided on purpose. The AST walker is proven to go RED on the
exact 2026-09-03 shape before it is trusted to say green, and every loop
assertion requires that the reads it is watching actually HAPPENED off the loop
— "no query on the loop" is trivially true of a request that ran no query.

Hermetic: tmp sqlite, no network, no Postgres.

Run:
  /opt/homebrew/Caskroom/mambaforge/base/envs/archimedes/bin/pytest -q \\
      -p no:cacheprovider backend/tests/test_handlers_off_the_loop.py
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

import archimedes.db as db
import httpx
import pytest
from archimedes.api import account_auth, leaderboard_routes, paper_routes, strategies_routes
from fastapi import FastAPI

from tests.db_isolation import redirect_to_tmp_sqlite
from tests.loop_thread import watch_session_query

_BACKEND = pathlib.Path(__file__).resolve().parents[1]
_API_DIR = _BACKEND / "archimedes" / "api"
_MAIN = _BACKEND / "archimedes" / "main.py"

#: Modules that a request can reach. Every ``.py`` under ``archimedes/api`` by
#: construction, plus the one service in #1819's P6 list that is not in that
#: directory — ``live_rigor_gate`` is called from the vaults routes.
_EXTRA_SERVING_PATH = (_BACKEND / "archimedes" / "services" / "live_rigor_gate.py",)

#: The eleven call sites #1819's PR body enumerated, by module. Named here so a
#: rename or a move cannot quietly drop a file out of the scan while the tests
#: below keep passing over whatever is left.
_THE_ELEVEN = {
    "archimedes/api/paper_routes.py": 5,
    "archimedes/api/selection_bias_routes.py": 2,
    "archimedes/api/leaderboard_routes.py": 1,
    "archimedes/api/strategies_routes.py": 2,
    "archimedes/services/live_rigor_gate.py": 1,
}

_ROUTER_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _serving_path_files() -> list[pathlib.Path]:
    return sorted(_API_DIR.rglob("*.py")) + list(_EXTRA_SERVING_PATH)


def _init_db_references(tree: ast.AST) -> list[str]:
    """Executable references to ``init_db`` — imports, bare names, attributes.

    AST, not a substring search, for the reason #1819 gives: the modules must be
    free to EXPLAIN in prose why the call is gone (``main.py``'s comment names
    every one of the eleven) without that prose either satisfying or breaking
    the guard. A docstring is an ``ast.Constant``; none of the three node types
    below can match one.
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            hits += [f"line {node.lineno}: imports init_db" for a in node.names if a.name == "init_db"]
        elif isinstance(node, ast.Import):
            hits += [f"line {node.lineno}: imports init_db" for a in node.names if a.name.endswith("init_db")]
        elif isinstance(node, ast.Name) and node.id == "init_db":
            hits.append(f"line {node.lineno}: references init_db")
        elif isinstance(node, ast.Attribute) and node.attr == "init_db":
            hits.append(f"line {node.lineno}: references .init_db")
    return hits


def _async_routes(tree: ast.Module) -> list[ast.AsyncFunctionDef]:
    """Every ``async def`` carrying an ``@<something>_router.<verb>(...)`` decorator."""
    out = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            call = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(call, ast.Attribute) and call.attr in _ROUTER_VERBS:
                out.append(node)
                break
    return out


def _referenced_names(fn: ast.AST) -> list[tuple[str, int]]:
    """Every function-ish name this body mentions, in source order.

    REFERENCES, not just call targets. ``list_strategies`` does not *call* its
    blocking twin — it hands the function object to ``asyncio.to_thread``, where
    the name is an ``ast.Name`` load with no ``ast.Call`` around it. A walker
    that only followed callees would stop at ``to_thread`` and pronounce the
    whole off-loop route clean, which is precisely the route this guard exists
    to watch. (That is not hypothetical: this walker had that bug, and the D1
    mutation below is what exposed it.)
    """
    out: list[tuple[str, int]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            out.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            out.append((node.attr, node.lineno))
    return out


def _reaches_init_db(tree: ast.Module, route: ast.AsyncFunctionDef) -> list[str]:
    """Walk the module-local reference graph out of ``route`` looking for ``init_db``.

    Intra-module and transitive, because the 2026-09-03 shape was never a call
    in the handler's own body: ``list_strategies`` reached ``init_db`` through
    ``_live_rigor_results_for_strategies``, a module-level helper. A guard that
    only looked inside the ``async def`` would have called that route clean.
    """
    defs: dict[str, ast.AST] = {
        n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    seen: set[str] = {route.name}
    trail: list[str] = []

    def visit(fn: ast.AST, path: list[str]) -> None:
        for name, lineno in _referenced_names(fn):
            if name == "init_db":
                trail.append(" -> ".join([*path, f"init_db (line {lineno})"]))
            elif name in defs and name not in seen:
                seen.add(name)
                visit(defs[name], [*path, name])

    visit(route, [route.name])
    return trail


class TestNoRequestHandlerRunsSchemaDdl:
    """P6 — ``init_db`` is gone from every module a request can reach."""

    def test_the_eleven_sites_modules_are_all_in_the_scan(self):
        """Every module #1819 named is actually covered, and "eleven" is the real count.

        Without this, a rename or a move would drop a file out of ``rglob`` and
        the two parametrised tests below would keep passing, green, over
        whatever was left.
        """
        assert sum(_THE_ELEVEN.values()) == 11, "the table stopped describing the eleven sites"
        scanned = {p.relative_to(_BACKEND).as_posix() for p in _serving_path_files()}
        missing = sorted(set(_THE_ELEVEN) - scanned)
        assert not missing, f"#1819 named these modules and the scan does not cover them: {missing}"

    @pytest.mark.parametrize("path", _serving_path_files(), ids=lambda p: p.name)
    def test_no_serving_path_module_names_init_db(self, path: pathlib.Path):
        hits = _init_db_references(ast.parse(path.read_text()))
        assert not hits, (
            f"{path.relative_to(_BACKEND)} names init_db in executable code (#1818 P6): {hits}. "
            "Schema belongs to the migrate task plus main.py's boot call — a request handler "
            "issuing ALTER TABLE is the 2026-09-03 wedge shape."
        )

    @pytest.mark.parametrize("path", _serving_path_files(), ids=lambda p: p.name)
    def test_no_async_route_reaches_init_db(self, path: pathlib.Path):
        tree = ast.parse(path.read_text())
        for route in _async_routes(tree):
            trails = _reaches_init_db(tree, route)
            assert not trails, f"{path.name}: async route reaches DDL on the request path: {trails}"

    def test_the_walker_goes_red_on_the_2026_09_03_shape(self):
        """The walker is proven to see the defect before it is believed about its absence.

        This is the exact shape ``strategies_routes.list_strategies`` had: the
        handler never mentions ``init_db``; a module-level helper it calls does.
        """
        shape = ast.parse(
            "from archimedes.db import get_session, init_db\n"
            "\n"
            "def _live_rigor_results_for_strategies(ids):\n"
            "    init_db()\n"
            "    with get_session() as s:\n"
            "        return s.query(object).all()\n"
            "\n"
            "@strategies_router.get('/')\n"
            "async def list_strategies(request):\n"
            "    return _live_rigor_results_for_strategies([])\n"
        )
        (route,) = _async_routes(shape)
        assert route.name == "list_strategies"
        assert _reaches_init_db(shape, route) == [
            "list_strategies -> _live_rigor_results_for_strategies -> init_db (line 4)"
        ]
        assert _init_db_references(shape) == ["line 1: imports init_db", "line 4: references init_db"]

    def test_the_walker_follows_a_function_handed_to_to_thread(self):
        """The shape THIS PR creates: the route never calls its own body.

        After P4 a handler is one ``await asyncio.to_thread(_x_sync, …)`` — the
        blocking half is passed as an object, so it appears in the AST as a bare
        ``ast.Name`` with no ``ast.Call`` wrapping it. A walker that followed
        only call targets would stop at ``to_thread``, find nothing, and declare
        every off-loop route free of DDL forever. This pins the edge.
        """
        hopped = ast.parse(
            "import asyncio\n"
            "from archimedes.db import get_session, init_db\n"
            "\n"
            "def _list_strategies_sync(request):\n"
            "    init_db()\n"
            "    with get_session() as s:\n"
            "        return s.query(object).all()\n"
            "\n"
            "@strategies_router.get('/')\n"
            "async def list_strategies(request):\n"
            "    return await asyncio.to_thread(_list_strategies_sync, request)\n"
        )
        (route,) = _async_routes(hopped)
        assert _reaches_init_db(hopped, route) == ["list_strategies -> _list_strategies_sync -> init_db (line 5)"]

    def test_the_walker_is_not_always_red(self):
        """Negative control: the same module with the DDL removed is clean."""
        clean = ast.parse(
            "from archimedes.db import get_session\n"
            "\n"
            "def _live_rigor_results_for_strategies(ids):\n"
            "    with get_session() as s:\n"
            "        return s.query(object).all()\n"
            "\n"
            "@strategies_router.get('/')\n"
            "async def list_strategies(request):\n"
            "    return _live_rigor_results_for_strategies([])\n"
        )
        (route,) = _async_routes(clean)
        assert _reaches_init_db(clean, route) == []
        assert _init_db_references(clean) == []

    def test_boot_still_calls_init_db_exactly_once(self):
        """ "Delete the call everywhere" must not pass this class.

        The web process still has to assert its schema; what changed is WHERE.
        ``main.py`` keeps exactly one module-level call, before any request is
        served, and ``archimedes.db.init_db`` still exists to be called.
        """
        assert callable(db.init_db)
        tree = ast.parse(_MAIN.read_text())
        top_level = [
            n
            for n in tree.body
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "init_db"
        ]
        assert len(top_level) == 1, f"main.py must call init_db() exactly once at boot, found {len(top_level)}"


# ── P4: the reads run on a worker thread ────────────────────────────────────

_USER_ID = "u-offloop"
_SPEC = {
    "name": "off-loop probe",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "daily",
    "entry": {"gt": ["momentum_20", 0]},
    "exit": {"lt": ["momentum_20", -0.99]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497"],
    "look_ahead_safe": True,
}


@pytest.fixture()
def tmp_db(tmp_path):
    """Deliberately NOT autouse — the AST class above needs no database at all.

    Autouse would have made ~100 pure source-reading tests each build and tear
    down a fresh sqlite file with every table on it, for nothing.
    """
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture()
def app(tmp_db, monkeypatch) -> FastAPI:  # noqa: ARG001 — ordering dependency, not a value
    """The three routers under guard, behind the real session middleware.

    A hand-built app rather than ``archimedes.main.app`` so the fixture stays
    hermetic (importing main runs the boot ``init_db()`` and the manifest seed
    against the process-wide DB) — the routers, the middleware and the request
    plumbing are the real ones, which is all this guard measures.
    """
    application = FastAPI()
    application.middleware("http")(account_auth.better_auth_session_middleware)
    application.include_router(strategies_routes.strategies_router)
    application.include_router(paper_routes.paper_router)
    application.include_router(leaderboard_routes.leaderboard_router)
    monkeypatch.setattr(paper_routes, "_spec_for_strategy", lambda *_a, **_k: dict(_SPEC))
    monkeypatch.setattr(paper_routes, "advance_deployment", lambda *_a, **_k: {"appended": 0, "drift": 0})

    async def _session(_request):
        return {
            "user": {"id": _USER_ID, "name": "probe", "email": "probe@example.com", "emailVerified": True},
            "session": {"id": "s-probe", "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    monkeypatch.setattr(account_auth, "_fetch_session", _session)
    return application


def _client(application: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
        headers={"cookie": "better-auth.session_token=opaque", "host": "archimedes-arc.com"},
    )


#: (url, the two halves of the split handler). Both names are passed because on
#: the worker thread only the ``_…_sync`` twin is on the stack, and on the loop
#: thread (the reverted state this guard exists to catch) the coroutine is.
_ROUTES = [
    ("/api/strategies/", ("list_strategies", "_list_strategies_sync")),
    ("/api/strategies/generated", ("list_generated_strategies", "_list_generated_strategies_sync")),
    ("/api/strategies/passports", ("list_strategy_passports", "_list_strategy_passports_sync")),
    ("/api/strategies/passports/probe-1", ("get_strategy_passport", "_get_strategy_passport_sync")),
    ("/api/strategies/probe-1", ("get_strategy", "_get_strategy_sync")),
    ("/api/strategies/probe-1/returns", ("get_strategy_returns", "_get_strategy_returns_sync")),
    ("/api/strategies/probe-1/debate", ("get_strategy_debate", "_get_strategy_debate_sync")),
    ("/api/paper/deployments", ("list_paper_deployments", "_list_paper_deployments_sync")),
    ("/api/paper/deployments/probe-dep", ("get_paper_deployment", "_get_paper_deployment_sync")),
    ("/api/paper/deployments/probe-dep/marks", ("get_paper_deployment_marks", "_get_paper_deployment_marks_sync")),
    ("/api/leaderboard", ("get_leaderboard", "_get_leaderboard_sync")),
    ("/api/leaderboard/live-paper", ("get_live_paper_leaderboard", "_get_live_paper_leaderboard_sync")),
]

#: The write routes on the same two routers. They are NOT off the loop — see the
#: PR body — but P6 applies to them exactly as it does to the reads, so the DDL
#: guard below drives them too.
_WRITE_ROUTES = [
    ("POST", "/api/paper/deployments", {"strategy_id": "probe-1"}),
    ("POST", "/api/paper/deployments/probe-dep/stop", None),
    ("PATCH", "/api/strategies/probe-1", {"name": "renamed"}),
]


class TestTheseHandlersDoNotQueryOnTheLoop:
    """P4 — every listed handler's ``session.query`` runs on a worker thread."""

    @pytest.mark.parametrize(("url", "under"), _ROUTES, ids=[u for u, _ in _ROUTES])
    async def test_the_handler_queries_off_the_loop(self, app, url, under):
        with watch_session_query(under=under) as log:
            async with _client(app) as client:
                response = await client.get(url)

        assert response.status_code in (200, 404), response.text
        assert not log.on_loop, (
            f"{url} ran session.query ON the event loop thread (#1818 P4):\n{log.why()}\n"
            "A blocked query here stops /health answering and the ALB kills the task."
        )
        assert log.off_loop, (
            f"{url} ran NO query at all under {under} — this assertion measured nothing. "
            "Either the handler was renamed or the request never reached the database."
        )

    async def test_the_detector_catches_a_handler_that_did_not_hop(self, tmp_db):  # noqa: ARG002 — DB fixture
        """Negative control: an ``async def`` that queries inline IS caught.

        Without this, every assertion above could be passing because the watcher
        is broken rather than because the handlers are correct.
        """
        from archimedes.models.strategy_store import StrategyRecord

        application = FastAPI()

        @application.get("/on-the-loop")
        async def _handler_that_blocks_the_loop():
            with db.get_session() as session:
                session.query(StrategyRecord).all()
            return {"ok": True}

        with watch_session_query(under=("_handler_that_blocks_the_loop",)) as log:
            async with _client(application) as client:
                assert (await client.get("/on-the-loop")).status_code == 200

        assert log.on_loop, "the detector failed to see a query made directly on the loop"
        assert not log.off_loop


class TestDrivingTheseRoutesRunsNoDdl:
    """P6, behaviourally — none of the fifteen routes above reaches ``init_db`` at runtime.

    The AST class proves the source is clean; this proves the running code is,
    including anything the walker cannot see (a dynamic import, a call through a
    service the walker does not follow across module boundaries). A COUNTER
    rather than a raising stub, deliberately: two of these handlers degrade a DB
    exception into a 200, so a raise would be swallowed and the guard would pass
    on the broken code.
    """

    async def test_no_route_calls_init_db(self, app, monkeypatch):
        calls: list[str] = []
        real = db.init_db
        monkeypatch.setattr(db, "init_db", lambda: calls.append("init_db()"))

        reached: list[str] = []
        async with _client(app) as client:
            for url, _ in _ROUTES:
                got = await client.get(url)
                if got.status_code not in (200, 404):
                    reached.append(f"GET {url} -> {got.status_code}")
            for method, url, body in _WRITE_ROUTES:
                got = await client.request(method, url, json=body)
                if got.status_code not in (200, 201, 404, 422):
                    reached.append(f"{method} {url} -> {got.status_code}")

        assert calls == [], f"a request handler ran schema DDL (#1818 P6): {len(calls)} call(s)"
        assert callable(real)
        # The DDL assertion is vacuous if the requests never reached a handler,
        # so pin that every one of them got past auth and routing. A 404 (unknown
        # probe id) or 422 (rejected body) is fine — the handler ran; 401 or 405
        # would mean this test measured nothing.
        assert reached == [], f"these requests never reached their handler: {reached}"
