"""Every endpoint that reports a verdict for one strategy id reports the SAME one.

Issue #1746, reproduced 3x in the 2026-09-01 agent-mcp dogfood:

    GET /api/strategies/1f9cfe96…            → pass  / passes_rigor_gate: true / Sharpe 0.406
    GET /api/strategies/passports/1f9cfe96…  → candidate / false               / Sharpe null

``1f9cfe96d5048fec9cd22cd888a7d797`` is a CURATED strategy —
``analytics-engine/strategies/harvey_2018_volatility_targeting.py``. The two
endpoints read two different things:

* the detail route took the curated branch, ran a LIVE cohort rigor gate per
  request, promoted the file's ``STATUS = "candidate"`` to ``validated`` off that
  live pass, and resolved its Sharpe through ``real_* → persisted backtest →
  stub`` (harvey has no fixture row, so link 2: ``0.406``);
* ``/passports/{id}`` was a pure read of the ``strategy_passports`` row, whose
  ``passes_rigor_gate`` was a hardcoded ``False`` placeholder and whose
  ``sharpe_ratio`` had been filled from link 1 alone — ``NULL``.

A constant-``False`` column agrees with every failing and pending strategy and
MUST disagree with any the live gate passes, which is why only the flagship pass
case showed it.

PR-A (#1792) made the passport row the verdict of record for GENERATED
strategies. This is PR-B: the curated grade is produced by an operator-run job
(``services.curated_grading``, called from ``scripts/run_backtests.py`` and
``scripts/grade_curated.py``), stored on the same row, and every surface reads
it. ``docs/adr/rigor-verdict-of-record.md``.

Every test here names the mutation that reddens it. Hermetic: tmp sqlite, real
curated files on disk, no network.

Run:
  /opt/homebrew/Caskroom/mambaforge/base/envs/archimedes/bin/pytest -q \\
      -p no:cacheprovider backend/tests/test_curated_verdict_parity.py
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

# The id from the issue, in full. Pinned as a literal AND re-derived from the
# strategy file below, so the constant cannot rot into a meaningless hex string.
REPRO_ID = "1f9cfe96d5048fec9cd22cd888a7d797"
REPRO_STEM = "harvey_2018_volatility_targeting"

# The Sharpe the issue reported on the detail route, from the persisted backtest.
REPRO_SHARPE = 0.406

# A clean strategy snippet that passes the AST look-ahead audit.
_CLEAN_CODE = "def init(self):\n    self.sma = 0\n"


def _passing_series(seed: int = 0, n: int = 500) -> list[float]:
    """A return series engineered to clear the real gate."""
    return np.random.default_rng(seed).normal(0.0015, 0.004, n).tolist()


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """A per-test SQLite file, with the engine and sessionmaker rebound.

    Setting ``DATABASE_URL`` alone would not isolate anything: ``archimedes.db``
    resolves it once, at import time.
    """
    # `from archimedes import db`, not `import archimedes.db as db`: the rest of
    # this file reaches the same module with `from archimedes.db import ...`, and
    # mixing the two import forms for one module is what the code-quality gate
    # flags. Same module object either way — the binding is what monkeypatch
    # needs, not the syntax.
    from archimedes import db
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'curated_parity.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()
    yield


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _insert_backtest(session, strategy_id: str, returns: list[float], sharpe: float) -> None:
    """Persist a real ``backtest_results`` row with a daily-returns artifact."""
    from archimedes.models.backtest import BacktestResult
    from archimedes.services.backtest_repository import insert_backtest_if_missing

    equity = [1.0]
    for r in returns:
        equity.append(equity[-1] * (1.0 + r))
    result = BacktestResult(
        strategy_id=strategy_id,
        sharpe_ratio=sharpe,
        sortino_ratio=0.51,
        max_drawdown=0.23,
        cagr=0.08,
        calmar_ratio=0.35,
        win_rate=0.54,
        profit_factor=1.1,
        total_trades=41,
        avg_holding_period_days=7.0,
        correlation_to_spy=0.62,
        correlation_to_btc=0.0,
        equity_curve=equity,
        backtest_engine="backtrader",
    )
    artifact_json = json.dumps({"results": [{"metrics": {"daily_returns": returns}}]})
    insert_backtest_if_missing(
        session,
        strategy_id=strategy_id,
        content_hash=hashlib.sha256((strategy_id + artifact_json).encode()).hexdigest(),
        result=result,
        artifact_json=artifact_json,
        source_pipeline="test",
    )


def _seed_the_reproduction(monkeypatch, *, grade: bool):
    """Put the issue's exact state in the DB, optionally graded.

    A persisted backtest for the reproduction id (Sharpe 0.406, a return series
    the real gate passes) plus one more graded strategy so the cohort has two
    members. Returns the provider.
    """
    from archimedes.api._route_helpers import strategy_provider
    from archimedes.db import get_session
    from archimedes.services import curated_grading

    # Resolve ids from a throwaway provider BEFORE the cached one is built, so
    # the singleton's boot-time backtest memo sees the rows we insert.
    from archimedes.services.strategy_provider import default_provider

    monkeypatch.setattr(curated_grading, "_load_strategy_code_safe", lambda s: _CLEAN_CODE)

    strategy_provider.cache_clear()
    scratch = default_provider()
    library = scratch.list_strategies()
    cohort_partner = next(s for s in library if s.id != REPRO_ID)

    with get_session() as session:
        _insert_backtest(session, REPRO_ID, _passing_series(0), REPRO_SHARPE)
        _insert_backtest(session, cohort_partner.id, _passing_series(1), 0.9)
        session.commit()

    # Rebuild the cached provider so its backtest map carries the rows above,
    # and so its passport sync writes the resolved display metrics.
    strategy_provider.cache_clear()
    provider = strategy_provider()

    if grade:
        with get_session() as session:
            curated_grading.grade_curated_library(session, provider=provider)
            session.commit()
    return provider


# ═══════════════════════════════════════════════════════════════════════════
# The id in the issue
# ═══════════════════════════════════════════════════════════════════════════


def test_the_reproduction_id_is_the_harvey_strategy():
    """``1f9cfe96…`` is ``harvey_2018_volatility_targeting.py``, re-derived.

    The issue truncates the id; this pins the whole thing to the file it names,
    by running the real id function over the real file. If the strategy's paper
    metadata or methodology text changes, its id changes and this test says so
    rather than letting the parity tests below quietly start asserting nothing.
    """
    from archimedes.services.strategy_provider import _hash_file, _read_module_constants, _strategy_id

    path = _repo_root() / "analytics-engine" / "strategies" / f"{REPRO_STEM}.py"
    assert path.exists(), f"the reproduction's strategy file is gone: {path}"
    metadata = _read_module_constants(path)
    assert _strategy_id(metadata, _hash_file(path)) == REPRO_ID
    assert str(metadata.get("STATUS")).lower() == "candidate", (
        "the reproduction is a CANDIDATE whose live gate passed — that mismatch "
        "between the file's status and the served one is half of what #1746 reported"
    )


# ═══════════════════════════════════════════════════════════════════════════
# The parity guard
# ═══════════════════════════════════════════════════════════════════════════

#: Keys both payloads publish that are NOT compared field-for-field, each with a
#: reason. Anything else the two share must be equal — so a new shared field is
#: covered automatically rather than needing this list extended.
_PARITY_EXEMPT = {
    # `status` is the PERSISTED lifecycle column on the passport (the `?status=`
    # filter queries it) and the SERVED card status on the detail route. Both are
    # published side by side and both are asserted below, under names that say
    # which is which — see `served_status`.
    "status",
}


@pytest.mark.asyncio
async def test_the_two_gate_endpoints_agree_on_the_reproduction_id(monkeypatch):
    """THE #1746 GUARD. ``GET /api/strategies/{id}`` vs ``/passports/{id}``.

    Compares EVERY key the two payloads share — id, methodology, universe, the
    four-state verdict, the boolean, and the metrics both publish
    (``sharpe_ratio``, ``sortino_ratio``, ``max_drawdown``) — not a hand-listed
    subset, so a field added to one payload cannot quietly escape the check.

    RED on the pre-fix behaviour, on this exact id: the detail route computed a
    live ``pass`` with Sharpe 0.406 while the passport row read ``false`` /
    ``null``.

    MUTATION 1: restore the live computation in ``_to_strategy_response``
    (``verdict, rigor_result = _live_verdict_and_result_for_one(s)``) — the
    booleans and the four-state come apart again.
    MUTATION 2: revert ``_sync_to_unified_table`` to ingesting the bare
    ``strategy`` instead of ``with_display_metrics(strategy, bt)`` — the passport
    row's Sharpe goes back to ``null`` while the detail route serves 0.406.
    """
    from archimedes.main import app

    _seed_the_reproduction(monkeypatch, grade=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail_resp = await client.get(f"/api/strategies/{REPRO_ID}")
        passport_resp = await client.get(f"/api/strategies/passports/{REPRO_ID}")

    assert detail_resp.status_code == 200, detail_resp.text
    assert passport_resp.status_code == 200, passport_resp.text
    detail = detail_resp.json()
    passport = passport_resp.json()

    # The reproduction only bites on a PASS — a constant-False column agrees with
    # every fail and every pending. If this is not a pass, the guard is vacuous.
    assert detail["rigor_gate_status"] == "pass", (
        f"fixture guard: the reproduction must reproduce the PASS case; got {detail['rigor_gate_status']}"
    )
    assert detail["sharpe_ratio"] == pytest.approx(REPRO_SHARPE)

    shared = (set(detail) & set(passport)) - _PARITY_EXEMPT
    assert {"rigor_gate_status", "passes_rigor_gate", "sharpe_ratio"} <= shared, (
        f"the parity guard lost one of the three fields the issue names; shared keys were {sorted(shared)}"
    )
    disagreements = {k: (detail[k], passport[k]) for k in sorted(shared) if detail[k] != passport[k]}
    assert not disagreements, (
        f"GET /api/strategies/{REPRO_ID} and GET /api/strategies/passports/{REPRO_ID} "
        f"disagree on {list(disagreements)}: {disagreements}"
    )

    # `status`: two names, one derivation, published together.
    assert passport["status"] == "candidate", "the passport publishes the PERSISTED lifecycle column"
    assert passport["served_status"] == "validated", "…and the card status the same stored verdict derives"
    assert detail["status"] == passport["served_status"]

    # Provenance proves the verdict came from a real gate run, not a placeholder.
    assert passport["graded_at"] is not None
    assert passport["gate_version"], "a stored verdict must name the gate that produced it"
    assert passport["cohort_n"] == 2, "graded against the two strategies with persisted returns"


@pytest.mark.asyncio
async def test_every_surface_that_reports_the_verdict_reports_the_same_one(monkeypatch):
    """Library list, detail, leaderboard and the passport, for one id.

    MUTATION: put the live cohort gate back into ``leaderboard_routes.
    _curated_cohort_responses``. Its numbers drift from the stored ones — the
    same two-answers-for-one-id shape, one surface over.
    """
    from archimedes.main import app

    _seed_the_reproduction(monkeypatch, grade=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/api/strategies/?limit=100")
        detail = await client.get(f"/api/strategies/{REPRO_ID}")
        board = await client.get("/api/leaderboard?limit=100")
        passport = await client.get(f"/api/strategies/passports/{REPRO_ID}")

    for resp in (listed, detail, board, passport):
        assert resp.status_code == 200, resp.text

    row = next(s for s in listed.json()["strategies"] if s["id"] == REPRO_ID)
    entry = next(e for e in board.json()["entries"] if e["id"] == REPRO_ID)
    body = detail.json()
    stored = passport.json()

    assert row["rigor_gate_status"] == body["rigor_gate_status"] == stored["rigor_gate_status"] == "pass"
    assert row["passes_rigor_gate"] == body["passes_rigor_gate"] == stored["passes_rigor_gate"] is True
    assert (
        row["sharpe_ratio"]
        == body["sharpe_ratio"]
        == stored["sharpe_ratio"]
        == entry["sharpe_ratio"]
        == pytest.approx(REPRO_SHARPE)
    )
    for key in ("deflated_sharpe_ratio", "dsr_p_value", "pbo_score", "out_of_sample_sharpe"):
        assert row[key] == body[key] == entry[key], f"{key} differs between the list, the detail route and the board"


@pytest.mark.asyncio
async def test_the_verify_surface_agrees_with_the_grade_it_was_computed_from(monkeypatch):
    """The one read surface that still runs the gate must not contradict the badge.

    ``GET /api/selection-bias/gate/{id}`` — the Strategy Passport's "verify"
    trace and the vault deploy ladder — is the seam this PR deliberately leaves
    open (``docs/adr/rigor-verdict-of-record.md``: admission is arguably the one
    place a fresh recompute is safer). Leaving it open makes the *vintage* free
    to differ; it does not make the COMPUTATION free to differ. ``grade_cohort``
    is that route's cohort path lifted onto the write side, so over the same
    data, at the same moment, the two must produce the same verdict and the same
    four numbers. If they do not, the badge and the deploy answer disagree for a
    reason that is not vintage — which is #1746 again, one surface over.

    The look-ahead audit input is pinned on BOTH sides: ``_load_strategy_code``
    resolves against ``os.getcwd()``, so under pytest the route reads no source
    and fails that leg closed while the grading job (whose loader the fixture
    patches) passes it. Patching both is what makes this compare the two
    computations rather than the two working directories.

    MUTATION: change ``num_trials`` in ``curated_grading.grade_cohort`` from 1 to
    ``len(valid_returns)``. The stored DSR moves and the trace's does not.
    """
    from archimedes.api import selection_bias_routes as sbr
    from archimedes.main import app

    monkeypatch.setattr(sbr, "_load_strategy_code", lambda _path: _CLEAN_CODE)
    _seed_the_reproduction(monkeypatch, grade=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/api/strategies/{REPRO_ID}")
        trace = await client.get(f"/api/selection-bias/gate/{REPRO_ID}")

    assert detail.status_code == 200, detail.text
    assert trace.status_code == 200, trace.text
    body = detail.json()
    computed = trace.json()

    assert body["rigor_gate_status"] == "pass", (
        f"fixture guard: the reproduction must reproduce the PASS case; got {body['rigor_gate_status']}"
    )
    assert computed["passes_all"] is True, (
        "the verify/deploy surface recomputed a FAIL for an id every badge surface serves as "
        f"`pass`: {computed['gate_details']}"
    )
    for served, recomputed in (
        ("deflated_sharpe_ratio", "deflated_sharpe"),
        ("dsr_p_value", "dsr_p_value"),
        ("pbo_score", "pbo_score"),
        ("out_of_sample_sharpe", "oos_sharpe"),
    ):
        assert body[served] == pytest.approx(computed[recomputed]), (
            f"the stored {served} ({body[served]}) and the recomputed {recomputed} "
            f"({computed[recomputed]}) came from two different computations over the same data"
        )


@pytest.mark.asyncio
async def test_two_reads_of_the_same_id_cannot_drift(monkeypatch):
    """The issue's secondary finding: "Sharpe drifted between reads 37s apart".

    The cause was a per-process provider memo with no TTL against two ECS tasks —
    a live recompute presented as a persisted number. A stored answer cannot
    drift between two readers of one row, and the detail route now reads that
    row.

    MUTATION: serve ``metrics = resolve_display_metrics(s, bt)`` unconditionally
    in ``_to_strategy_response`` (drop the ``stored is not None`` branch). The
    number goes back to being resolved per process, and the guard below stops
    being a guarantee — this test still passes in one process, which is why the
    assertion that carries the weight is the one on the SOURCE, not the two
    reads.
    """
    from archimedes.api.strategies_routes import _to_strategy_response
    from archimedes.db import get_session
    from archimedes.main import app
    from archimedes.services.passport_loader import get_passport

    provider = _seed_the_reproduction(monkeypatch, grade=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(f"/api/strategies/{REPRO_ID}")
        second = await client.get(f"/api/strategies/{REPRO_ID}")
    assert first.json()["sharpe_ratio"] == second.json()["sharpe_ratio"]

    # The claim under the guard: the served number IS the stored one. A reader in
    # another process, with another boot vintage of the provider memo, reads the
    # same row and therefore serves the same number.
    with get_session() as session:
        record = get_passport(session, REPRO_ID)
        served = _to_strategy_response(provider.get_strategy(REPRO_ID), record)
    assert served.sharpe_ratio == record.sharpe_ratio == pytest.approx(REPRO_SHARPE)
    assert served.rigor_gate_status == record.rigor_gate_status
    assert served.passes_rigor_gate == record.passes_rigor_gate


# ═══════════════════════════════════════════════════════════════════════════
# No recompute on read
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_the_detail_route_reads_pending_until_the_grading_job_runs(monkeypatch):
    """Persisted returns the gate WOULD pass, and no grade: the answer is
    ``pending`` on both endpoints.

    This is the direct assertion that the read path does not grade. It is also
    the honest state: nothing has graded this strategy, and ``pending`` says so.

    MUTATION: restore ``_live_verdict_and_result_for_one`` in
    ``_to_strategy_response``. The detail route answers ``pass`` while the
    passport still answers ``pending`` — #1746, exactly.
    """
    from archimedes.main import app

    _seed_the_reproduction(monkeypatch, grade=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/api/strategies/{REPRO_ID}")
        passport = await client.get(f"/api/strategies/passports/{REPRO_ID}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["rigor_gate_status"] == "pending"
    assert detail.json()["passes_rigor_gate"] is False
    assert detail.json()["status"] == "candidate", "no pass, so no promotion"
    assert passport.json()["rigor_gate_status"] == "pending"

    # …and the display metrics are still served, from the row the sync wrote.
    # An ungraded strategy has no verdict; it does have a backtest.
    assert detail.json()["sharpe_ratio"] == pytest.approx(REPRO_SHARPE)
    assert passport.json()["sharpe_ratio"] == pytest.approx(REPRO_SHARPE)


@pytest.mark.asyncio
async def test_a_legacy_row_with_fixture_gate_numbers_serves_none(monkeypatch):
    """A curated row carrying the #1187 fixture's DSR with no ``graded_at`` must
    serve no numbers at all.

    Every curated passport in production is such a row until the grading job
    runs: the boot-time sync used to copy the fixture snapshot's
    ``deflated_sharpe_ratio`` / ``dsr_p_value`` / ``pbo_score`` /
    ``out_of_sample_sharpe`` into columns that name a gate. Serving those beside
    a ``pending`` badge would re-commit #1187 in the act of fixing #1746.

    MUTATION: drop the ``graded`` guard in ``_to_strategy_response``
    (``stored.deflated_sharpe_ratio if graded else None`` → the bare read). The
    fixture value reaches the wire and this reddens.
    """
    from archimedes.db import get_session
    from archimedes.main import app
    from sqlalchemy import text

    _seed_the_reproduction(monkeypatch, grade=False)

    # Exactly what the pre-PR-B sync left behind: fixture numbers, no grade.
    with get_session() as session:
        session.execute(
            text(
                "UPDATE strategy_passports SET deflated_sharpe_ratio = 0.283312, dsr_p_value = 0.611531, "
                "pbo_score = 0.373116, out_of_sample_sharpe = 0.930283 WHERE id = :i"
            ),
            {"i": REPRO_ID},
        )
        session.commit()
        assert (
            session.execute(text("SELECT graded_at FROM strategy_passports WHERE id = :i"), {"i": REPRO_ID}).scalar()
            is None
        ), "fixture guard: this row must be ungraded"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/api/strategies/{REPRO_ID}")

    body = detail.json()
    assert body["rigor_gate_status"] == "pending"
    for key in ("deflated_sharpe_ratio", "dsr_p_value", "pbo_score", "out_of_sample_sharpe"):
        assert body[key] is None, f"{key} leaked a fixture number from an ungraded row: {body[key]}"
    assert body["metrics_source"] == "unavailable"


# ═══════════════════════════════════════════════════════════════════════════
# The grading job itself
# ═══════════════════════════════════════════════════════════════════════════


def test_the_grading_job_stores_what_the_real_gate_computed(monkeypatch):
    """The stored verdict and the four numbers come from one real gate run.

    MUTATION: drop the four numeric arguments from the ``RigorVerdictWrite`` in
    ``grade_curated_library``. The badge stays but the numbers become NULL — a
    verdict with nothing to show for it.
    """
    from archimedes.db import get_session
    from archimedes.services.curated_grading import grade_cohort
    from archimedes.services.passport_loader import get_passport

    provider = _seed_the_reproduction(monkeypatch, grade=True)

    expected = grade_cohort(list(provider.list_strategies())).results[REPRO_ID]
    with get_session() as session:
        row = get_passport(session, REPRO_ID)

    assert row.rigor_gate_status == "pass"
    assert row.passes_rigor_gate is True
    assert row.deflated_sharpe_ratio == pytest.approx(expected.deflated_sharpe)
    assert row.dsr_p_value == pytest.approx(expected.dsr_p_value)
    assert row.out_of_sample_sharpe == pytest.approx(expected.oos_sharpe)
    assert row.gate_version and row.graded_at is not None


def test_a_strategy_with_no_persisted_returns_is_graded_pending(monkeypatch):
    """The pairs family's real state: no persisted row, so no verdict.

    ``run_backtests`` refuses to persist their artifact (realized-vol
    plausibility), so they have no returns, so the gate cannot run. ``pending``
    is the honest answer and re-running the job does not change it.

    MUTATION: make ``verdict_from_result(None)`` return ``failed()``. Thirty-odd
    curated strategies start claiming they were graded and lost.
    """
    from archimedes.db import get_session
    from archimedes.services.passport_loader import get_passport

    provider = _seed_the_reproduction(monkeypatch, grade=True)

    ungraded = [s for s in provider.list_strategies() if s.id != REPRO_ID]
    with get_session() as session:
        rows = {s.id: get_passport(session, s.id) for s in ungraded}

    pending = [r for r in rows.values() if r is not None and r.rigor_gate_status == "pending"]
    assert pending, "expected curated strategies with no persisted returns"
    for row in pending:
        assert row.passes_rigor_gate is False
        assert row.graded_at is None, "an ungraded row must not carry a grading timestamp"
        assert row.gate_version is None
        assert row.cohort_n is None, "graded against nothing — never a guessed cohort of 1"
        assert row.deflated_sharpe_ratio is None


# ═══════════════════════════════════════════════════════════════════════════
# The PUBLISHED description of these routes
# ═══════════════════════════════════════════════════════════════════════════
#
# FastAPI publishes each route function's docstring verbatim as the OpenAPI
# `description`. That is an agent-facing surface — the MCP contract and
# /docs both read it — so a docstring describing the deleted read-time gate is
# not a stale comment, it is a false published claim about the exact route this
# issue is about. `list_strategies`' docstring said the badge and the four rigor
# numbers "come from a SINGLE live-gate run via _live_rigor_results_for_strategies"
# and that "the served status reflects the live verdict", after this PR deleted
# that function and made the route a pure read.

#: Route functions whose docstring IS the contract for the verdict of record.
_VERDICT_ROUTES = (
    "/api/strategies/",
    "/api/strategies/{strategy_id}",
    "/api/strategies/passports",
    "/api/strategies/passports/{strategy_id}",
)

#: Read-time gate helpers this PR deleted. No published description may name one.
_DELETED_READ_TIME_HELPERS = (
    "_live_rigor_results_for_strategies",
    "_live_rigor_result_for_one",
    "_live_verdict_and_result_for_one",
    "_live_verdict_for_one",
    "_library_cohort_including",
    "_verdict_from_result",
)


def _published_descriptions() -> dict[str, str]:
    from archimedes.main import app

    spec = app.openapi()
    out: dict[str, str] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if isinstance(op, dict) and op.get("description"):
                out[f"{method.upper()} {path}"] = op["description"]
    return out


def test_no_published_route_description_names_a_deleted_gate_helper():
    """The OpenAPI spec cannot cite a function that no longer exists.

    MUTATION: put ``_live_rigor_results_for_strategies`` back into the
    ``list_strategies`` docstring. This reddens naming the route and the helper.
    """
    described = _published_descriptions()
    assert len(described) > 50, f"the spec rendered almost nothing — {len(described)} described operations"
    offenders = [
        f"{op} names {name}" for op, text in described.items() for name in _DELETED_READ_TIME_HELPERS if name in text
    ]
    assert not offenders, (
        f"these published OpenAPI descriptions cite read-time gate helpers this PR deleted: {offenders}. "
        "The description is the API contract an agent reads — docs/adr/rigor-verdict-of-record.md."
    )


@pytest.mark.parametrize("path", _VERDICT_ROUTES)
def test_the_published_description_says_the_verdict_is_read_not_computed(path):
    """Each verdict-bearing route publishes "stored", and never claims a live run.

    The word "live" is banned outright here rather than a specific phrase: these
    routes run no gate at all now, so any published sentence about a live verdict
    is false whatever its wording, and a tombstone about the retired behaviour
    belongs in a code comment (which FastAPI does not publish), not in the
    contract.

    MUTATION: restore either half of the old text — "come from a SINGLE live-gate
    run" or "the served status reflects the live verdict". Both redden.
    """
    from archimedes.main import app

    description = app.openapi()["paths"][path]["get"]["description"]
    # Anti-vacuity: an empty or missing description would pass every check below.
    assert len(description) > 200, f"{path} publishes almost no description: {description!r}"

    lowered = description.lower()
    assert "live" not in lowered, (
        f"the published description of GET {path} still says 'live'. This route reads the "
        "stored verdict of record and runs no gate; see docs/adr/rigor-verdict-of-record.md."
    )
    assert "stored" in lowered, (
        f"the published description of GET {path} does not say the verdict is STORED — the one "
        "thing an agent comparing it with another endpoint needs to know."
    )


# ═══════════════════════════════════════════════════════════════════════════
# served_status — the GENERATED half of the same claim
# ═══════════════════════════════════════════════════════════════════════════
#
# The ADR and the PR both claim it for every id: "detail.status ==
# passport.served_status, curated or generated". The curated half is asserted
# above. The generated half rests on `_passport_payload` passing
# `promote=(generation_method == "curated")`, an ASYMMETRIC flag — a generated
# strategy's status is written by the pipeline that produced it and
# `_passport_to_strategy_response` serves `record.status` verbatim, so promoting
# it would invent a "validated" the detail route never says. Nothing pinned that
# direction: mutating the flag to `promote=True` left 94 tests green.

_GENERATED_ID = "gen-candidate-with-a-stored-pass"


def _seed_a_published_generated_pass(session, sid: str = _GENERATED_ID) -> None:
    """A PUBLISHED generated row: ``status='candidate'``, stored verdict ``pass``.

    The state that separates the two possible ``promote`` answers. Written
    through the single writer (``ingest_passport(rigor_verdict=…)``) rather than
    by setting columns, so the row is one the production path can actually
    produce.
    """
    from archimedes.models.strategy_passport_record import StrategyPassportRecord
    from archimedes.models.strategy_store import StrategyRecord
    from archimedes.services.passport_loader import RigorVerdictWrite, ingest_passport

    session.add(
        StrategyRecord(
            id=sid,
            content_hash=("0x" + sid.replace("-", "")).ljust(66, "0"),
            generation_method="fusion",
            source_papers="[]",
            strategy_name="Generated candidate",
            thesis="test thesis",
            asset_universe='["SPY"]',
            risk_profile="moderate",
            status="candidate",
            is_example=False,
            is_published=True,
        )
    )
    record = StrategyPassportRecord(
        id=sid,
        generation_method="fusion",
        methodology_summary="generated methodology",
        asset_universe='["SPY"]',
        status="candidate",
    )
    session.add(record)
    session.flush()
    ingest_passport(
        session,
        record.to_strategy_passport(),
        generation_method="fusion",
        force_update=True,
        rigor_verdict=RigorVerdictWrite(
            status="pass",
            cohort_n=1,
            deflated_sharpe_ratio=1.1,
            dsr_p_value=0.01,
            pbo_score=0.2,
            out_of_sample_sharpe=0.9,
        ),
    )
    session.commit()


@pytest.mark.asyncio
async def test_a_generated_candidate_that_passed_is_not_promoted_on_either_endpoint():
    """``detail.status == passport.served_status`` for a GENERATED id too — and
    for a generated row that value is the persisted ``candidate``, not a
    promotion.

    MUTATION: in ``_passport_payload``, replace
    ``promote=(record.generation_method or "").lower() == "curated"`` with
    ``promote=True``. The passport then publishes ``served_status: 'validated'``
    while the detail route still serves ``status: 'candidate'`` — the same
    two-answers-for-one-id shape #1746 reported, on the other half of the
    library.
    """
    from archimedes.db import get_session
    from archimedes.main import app

    with get_session() as session:
        _seed_a_published_generated_pass(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/api/strategies/{_GENERATED_ID}")
        passport = await client.get(f"/api/strategies/passports/{_GENERATED_ID}")

    assert detail.status_code == 200, detail.text
    assert passport.status_code == 200, passport.text
    body, stored = detail.json(), passport.json()

    # Fixture guard: only a `candidate` + `pass` row can tell the two `promote`
    # answers apart. Anything else and this test asserts nothing.
    assert stored["status"] == "candidate"
    assert stored["rigor_gate_status"] == "pass", "the promotion input must be a PASS or this is vacuous"

    assert body["status"] == stored["served_status"], (
        "the detail route and the passport disagree on the served status of a generated id"
    )
    assert stored["served_status"] == "candidate", (
        "a generated strategy's status is written by the pipeline that produced it — "
        "promoting it here would invent a 'validated' the detail route never serves"
    )
    # …and the verdict itself still travels, so this is not a row with nothing on it.
    assert body["rigor_gate_status"] == stored["rigor_gate_status"] == "pass"
    assert body["passes_rigor_gate"] is stored["passes_rigor_gate"] is True


# ═══════════════════════════════════════════════════════════════════════════
# A run that cannot read its data must not DESTROY the verdicts of record
# ═══════════════════════════════════════════════════════════════════════════
#
# `grade_cohort` swallows a failed returns read and a failed cohort-context
# compute. When both degraded to an empty result, `grade_curated_library` looped
# every strategy, got `result=None`, and wrote `RigorVerdictWrite(status=
# "pending")` — which forces `graded_at`, `gate_version`, `cohort_n` and the
# four gate numbers to NULL. One unreachable DB therefore turned every stored
# `pass` into "never graded", on all 34 curated rows, and reported
# `{'graded': 0, 'pending': 34, 'errors': {}}` — a success. Nothing exited
# non-zero, and the runbook told the operator that `graded_at: null` was not a
# reason to re-run.


def _break_the_returns_read(monkeypatch) -> None:
    """Make the one read `grade_cohort` depends on fail, as an outage would."""
    from archimedes.services import backtest_repository

    def _boom(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(backtest_repository, "get_all_daily_returns", _boom)


def test_a_grading_run_that_cannot_read_the_returns_writes_nothing(monkeypatch):
    """The stored verdicts survive an outage, and the run says it failed.

    MUTATION: drop the ``if not cohort.ok`` abort from ``grade_curated_library``
    (or return a plain ``CohortGrade()`` from either ``except`` branch in
    ``grade_cohort``). The reproduction's stored ``pass`` becomes ``pending``
    with ``graded_at`` NULL, and the summary reports it as a clean run.
    """
    from archimedes.db import get_session
    from archimedes.services import curated_grading
    from archimedes.services.passport_loader import get_passport

    provider = _seed_the_reproduction(monkeypatch, grade=True)

    with get_session() as session:
        before = get_passport(session, REPRO_ID)
        assert before.rigor_gate_status == "pass", "fixture guard: there must be a real verdict to destroy"
        graded_at_before = before.graded_at
        dsr_before = before.deflated_sharpe_ratio
        assert graded_at_before is not None and dsr_before is not None

    _break_the_returns_read(monkeypatch)
    with get_session() as session:
        summary = curated_grading.grade_curated_library(session, provider=provider)
        session.commit()

    # The stored verdict is untouched. Asserted FIRST, because it is the claim
    # that matters: the summary being empty is a symptom, the destroyed verdict
    # of record is the defect.
    with get_session() as session:
        after = get_passport(session, REPRO_ID)
    assert after.rigor_gate_status == "pass", (
        f"a grading run that could not read the returns overwrote a real stored verdict: "
        f"'pass' -> {after.rigor_gate_status!r}, graded_at {graded_at_before} -> {after.graded_at}"
    )
    assert after.graded_at == graded_at_before, "the run wiped graded_at on a verdict it never recomputed"
    assert after.deflated_sharpe_ratio == pytest.approx(dsr_before)
    assert summary.graded == 0 and summary.pending == 0, f"the aborted run wrote rows: {summary.as_dict()}"

    # …and the run reported the failure loudly enough for both operator scripts.
    assert summary.errors, "an aborted run must carry an error; an empty `errors` reads as success"
    assert "error" in summary.as_dict(), (
        "a run that wrote nothing must publish the `error` key run_backtests surfaces "
        "in its `graded` field and grade_curated exits non-zero on"
    )
    assert "connection refused" in summary.as_dict()["error"]


def test_the_standalone_job_exits_non_zero_when_it_wrote_nothing(monkeypatch):
    """``scripts/grade_curated`` must fail the ECS task / the operator's ``&&``.

    MUTATION: delete the ``sys.exit(1)`` branch from ``grade_curated.main``.
    The deploy step then reports success over a run that graded nothing —
    which, before this PR, is what a run that could not read the DB produced.
    """
    from archimedes.scripts import grade_curated as script
    from archimedes.services.curated_grading import CuratedGradeSummary

    aborted = CuratedGradeSummary(errors={"cohort_unavailable": "persisted returns unreadable"})
    monkeypatch.setattr(script, "grade_curated", lambda: aborted.as_dict())

    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 1


def test_a_run_that_reached_the_data_and_found_nothing_still_grades_pending(monkeypatch):
    """The other side of the distinction: an EMPTY read is not a FAILED read.

    A library whose strategies simply have no persisted returns must still be
    written as ``pending`` — that is the honest state, and it is what makes the
    pairs family's permanent ``pending`` a real stored verdict rather than an
    absence. The abort above must not swallow it.
    """
    from archimedes.db import get_session
    from archimedes.services import backtest_repository, curated_grading
    from archimedes.services.passport_loader import get_passport

    provider = _seed_the_reproduction(monkeypatch, grade=False)
    monkeypatch.setattr(backtest_repository, "get_all_daily_returns", lambda *_a, **_k: {})

    with get_session() as session:
        summary = curated_grading.grade_curated_library(session, provider=provider)
        session.commit()

    assert summary.pending > 0 and summary.graded == 0
    assert not summary.errors, f"a successful read with nothing gradeable is not an error: {summary.as_dict()}"
    assert "error" not in summary.as_dict()
    with get_session() as session:
        assert get_passport(session, REPRO_ID).rigor_gate_status == "pending"


# ═══════════════════════════════════════════════════════════════════════════
# The display-metric PROVENANCE travels with the number it describes
# ═══════════════════════════════════════════════════════════════════════════
#
# Storing the resolved chain on the row is what fixed the Sharpe half of #1746 —
# and it also pushed the last link, `stub_placeholder` (a constant hand-declared
# in a strategy file), onto `/passports/{id}`, which before PR-B carried link 1
# only. `to_dict()` published the numbers and no label, so on that agent-facing
# payload a hand-declared stub read exactly like a measured backtest, while the
# detail route labelled the very same number. The label is now written by the
# same call that writes the numbers and READ by both surfaces, which also
# removes the second half: it used to be derived per process from the provider's
# boot-time backtest memo, so a task whose memo predated a grading run could
# label a real persisted-backtest number "stub_placeholder".


@pytest.mark.asyncio
@pytest.mark.parametrize("graded", [True, False], ids=["after-the-grading-job", "sync-only"])
async def test_both_endpoints_name_the_same_link_for_the_same_number(monkeypatch, graded):
    """``display_metrics_source`` agrees, is stored, and is not a constant.

    Run on BOTH writers, because both resolve the chain and either one alone
    would satisfy a single-case guard. ``sync-only`` is the production state
    between a deploy and the first ``grade_curated`` run — the passport sync has
    written the numbers, nothing has graded — so the label has to be there
    already or that whole window publishes unlabelled numbers.

    MUTATION: drop ``display_metrics_source=display_metrics_source(strategy, bt)``
    from ``strategy_provider._sync_to_unified_table``. The column stays NULL, the
    passport publishes ``null`` while the detail route derives
    ``"persisted_backtest"``, and both cases redden — the grading job's own
    write cannot rescue them, because ``ingest_passport`` leaves the column alone
    when the label is ``None``. (The grading job's write has its own guard
    below: the sync runs first with the same inputs, so removing it alone leaves
    a correct row unless the stored label is already out of date.)
    """
    from archimedes.db import get_session
    from archimedes.main import app
    from archimedes.services.passport_loader import get_passport

    provider = _seed_the_reproduction(monkeypatch, grade=graded)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = await client.get(f"/api/strategies/{REPRO_ID}")
        passport = await client.get(f"/api/strategies/passports/{REPRO_ID}")

    body, stored = detail.json(), passport.json()
    assert body["display_metrics_source"] == stored["display_metrics_source"] == "persisted_backtest", (
        "the two endpoints must name the same link of the display chain for the same "
        f"stored number; got {body['display_metrics_source']!r} vs {stored['display_metrics_source']!r}"
    )
    assert body["sharpe_ratio"] == stored["sharpe_ratio"] == pytest.approx(REPRO_SHARPE)

    # It is READ from the row, not re-derived — the whole point.
    with get_session() as session:
        assert get_passport(session, REPRO_ID).display_metrics_source == "persisted_backtest"

    # Anti-vacuity: not a constant. `_seed_the_reproduction` gives exactly two
    # strategies a persisted backtest; every other library row has nothing behind
    # its numbers and says so, so the stored labels must differ across the table.
    with get_session() as session:
        labels = {s.id: get_passport(session, s.id).display_metrics_source for s in provider.list_strategies()}
    assert set(labels.values()) == {"persisted_backtest", "unavailable"}, (
        f"the stored label is not varying with the row it describes: {sorted(set(labels.values()))}"
    )
    assert sum(v == "persisted_backtest" for v in labels.values()) == 2, (
        f"only the two seeded backtests may be labelled persisted_backtest: {labels}"
    )


def test_a_hardcoded_stub_is_labelled_on_the_passport_payload():
    """THE defect in one assertion: a ``stub_*`` number reaches
    ``/passports/{id}`` and does NOT read as a measurement.

    ``stub_placeholder`` is the last link of the display chain — a ``BACKTEST_*``
    constant declared in the strategy module. Before this PR the passport payload
    could not carry one; now it can, so it has to say so.

    MUTATION: drop ``"display_metrics_source": self.display_metrics_source`` from
    ``StrategyPassportRecord.to_dict()``. The stub is published as a bare number
    again and this reddens.
    """
    from dataclasses import replace

    from archimedes.db import get_session
    from archimedes.services.curated_metrics import display_metrics_source, with_display_metrics
    from archimedes.services.passport_loader import get_passport, ingest_passport
    from archimedes.services.strategy_provider import default_provider

    base = next(iter(default_provider().list_strategies()))
    # A strategy whose ONLY number is a hand-declared constant: no fixture
    # snapshot (`real_*` all None) and no persisted backtest row (`bt=None`).
    stubbed = replace(
        base,
        id="stub-only-strategy",
        # The content hash is derived from the passport's content, and the row's
        # is UNIQUE — a bare id change would collide with `base`'s own row.
        methodology_summary="a stub-only strategy, for the provenance guard",
        real_sharpe=None,
        stub_sharpe=1.87,
    )
    assert display_metrics_source(stubbed, None) == "stub_placeholder", "fixture guard: this must BE the stub case"

    with get_session() as session:
        ingest_passport(
            session,
            with_display_metrics(stubbed, None),
            generation_method="curated",
            force_update=True,
            display_metrics_source=display_metrics_source(stubbed, None),
        )
        session.commit()
        payload = get_passport(session, "stub-only-strategy").to_dict()

    assert payload["sharpe_ratio"] == pytest.approx(1.87), "fixture guard: the stub must reach the payload"
    assert payload.get("display_metrics_source") == "stub_placeholder", (
        "the passport payload publishes a hand-declared BACKTEST_* constant with no "
        "provenance — indistinguishable from a measured Sharpe on an agent-facing route"
    )


def test_the_grading_job_rewrites_the_label_with_the_numbers(monkeypatch):
    """The label and the numbers move in ONE call, inside the grading job too.

    ``grade_curated_library`` re-resolves the display chain and rewrites the
    number columns through ``with_display_metrics``. If it wrote the numbers
    without the label, a row whose stored label is out of date keeps it beside
    numbers it no longer describes — e.g. ``"unavailable"`` next to a real
    persisted-backtest Sharpe.

    The passport sync normally runs first with the same inputs, so the job's
    write is invisible on a fresh row; this starts from a STALE label to make it
    observable, which is also the case that matters.

    MUTATION: drop ``display_metrics_source=display_metrics_source(s, bt)`` from
    the ``ingest_passport`` call in ``grade_curated_library``.
    """
    from archimedes.db import get_session
    from archimedes.services import curated_grading
    from archimedes.services.passport_loader import get_passport
    from sqlalchemy import text

    provider = _seed_the_reproduction(monkeypatch, grade=False)

    with get_session() as session:
        assert get_passport(session, REPRO_ID).display_metrics_source == "persisted_backtest", (
            "fixture guard: the sync must have labelled this row before we stale it"
        )
        session.execute(
            text("UPDATE strategy_passports SET display_metrics_source = 'unavailable' WHERE id = :i"),
            {"i": REPRO_ID},
        )
        session.commit()

    with get_session() as session:
        curated_grading.grade_curated_library(session, provider=provider)
        session.commit()
        row = get_passport(session, REPRO_ID)

    assert row.sharpe_ratio == pytest.approx(REPRO_SHARPE), "fixture guard: the job rewrote the number"
    assert row.display_metrics_source == "persisted_backtest", (
        "the grading job rewrote the display numbers and left a stale provenance label "
        "beside them — the label has to travel with the number it describes"
    )
