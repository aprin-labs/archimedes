"""The leaderboard split — research (backtest) vs live paper (Lane 3.4).

Two surfaces, and the whole point of splitting them is that a number can never
be read off either one without its basis attached. This file pins both halves:

  1. ANTI-FABRICATION (the load-bearing guard): an ACTIVE paper deployment with
     zero ``paper_daily_returns`` rows never becomes an entry — not as a 0.0%
     row, not as a placeholder holding a rank. It is dropped and COUNTED into
     ``withheld_no_ledger``, so the omission is a visible absence.
  2. Rows that DO have ledger data carry real, recomputable numbers:
     cumulative_return is the compounded product of the ledger, days_live is
     the observation count, as_of is the last observation's date, and
     inception_date is the deployment's own deploy date.
  3. Ordering is realised forward return, descending — never conviction, never
     a blend.
  4. Ownership: the forward board is ``owner_user_id``-scoped (#850 — a paper
     track record is private). User A never sees user B's deployment even when
     B's is the better-performing one.
  5. Stopped deployments are excluded (the board claims "live", so it must be).
  6. Anonymous callers get an honest empty board (scope='anonymous'), never a
     401 and never someone else's rows.
  7. The RESEARCH board keeps its conviction math untouched but now states its
     basis: every row carries performance_basis='backtest_research' plus the
     backtest window it was measured over.
  8. No blended score exists: the forward response has no conviction field and
     the research response has no live-paper field.

Hermetic: tmp sqlite via ``redirect_to_tmp_sqlite``; Better Auth's session
fetch is monkeypatched at its boundary; the httpx ASGITransport idiom from
test_leaderboard_single_user_scope.py / test_risk_routes.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from archimedes.api import account_auth
from archimedes.api.leaderboard_schemas import BASIS_BACKTEST, BASIS_LIVE_PAPER
from archimedes.main import app
from archimedes.services.leaderboard import LivePaperLedger, build_live_paper_leaderboard

from tests.db_isolation import redirect_to_tmp_sqlite

LIVE_PAPER_URL = "/api/leaderboard/live-paper"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


# ── fixtures ────────────────────────────────────────────────────────────────


def _deploy(
    deployment_id: str,
    *,
    owner_user_id: str,
    strategy_id: str = "strat-1",
    deployed_at: date = date(2026, 8, 1),
    status: str = "active",
    returns: list[float] | None = None,
    drift: bool = False,
) -> None:
    """One paper deployment plus ``returns`` consecutive ledger observations.

    ``returns=None`` means an EMPTY ledger — a deployment that opened but has
    not produced a single observation yet. That is the shape the anti-
    fabrication guard is about, so it is the default, not a special case.
    """
    import archimedes.db as db
    from archimedes.models.paper_store import PaperDailyReturn, PaperDeployment

    with db.get_session() as session:
        session.add(
            PaperDeployment(
                id=deployment_id,
                strategy_id=strategy_id,
                owner_user_id=owner_user_id,
                owner_wallet=None,
                spec_json='{"name": "Deployed Spec Name"}',
                deployed_at=deployed_at,
                status=status,
                drift_detected_at=datetime.now(UTC) if drift else None,
            )
        )
        for offset, daily in enumerate(returns or []):
            session.add(
                PaperDailyReturn(
                    deployment_id=deployment_id,
                    date=deployed_at + timedelta(days=offset),
                    daily_return=daily,
                    appended_at=datetime(2026, 8, 20, 12, 0, offset, tzinfo=UTC),
                )
            )
        session.commit()


def _named_strategy(strategy_id: str, name: str) -> None:
    import archimedes.db as db
    from archimedes.models.strategy_store import StrategyRecord

    with db.get_session() as session:
        session.add(
            StrategyRecord(
                id=strategy_id,
                content_hash=("0x" + strategy_id).ljust(66, "0"),
                generation_method="fusion",
                source_papers="[]",
                strategy_name=name,
                thesis="t",
                asset_universe="[]",
                risk_profile="moderate",
                status="live",
                is_example=False,
            )
        )
        session.commit()


def _session_for(user_id: str):
    async def fetch(_request):
        return {
            "user": {"id": user_id, "name": user_id, "email": f"{user_id}@example.com", "emailVerified": True},
            "session": {"id": f"s-{user_id}", "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    return fetch


def _sign_in(monkeypatch, user_id: str) -> None:
    monkeypatch.setattr(account_auth, "_fetch_session", _session_for(user_id))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"cookie": "better-auth.session_token=opaque", "host": "test"},
    )


# ── 1. Anti-fabrication: no ledger data ⇒ no row, and the drop is counted ───


@pytest.mark.asyncio
async def test_active_deployment_with_empty_ledger_is_never_rendered(monkeypatch):
    """THE guard. A deployment that is active but has produced zero
    observations has no forward performance, so it must not appear at all —
    and the count of what was withheld must be on the wire."""
    _deploy("dep-empty", owner_user_id="user-a", strategy_id="s-empty", returns=None)

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        resp = await client.get(LIVE_PAPER_URL)

    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == [], "an active deployment with an empty ledger must not produce a row"
    assert body["total"] == 0
    assert body["withheld_no_ledger"] == 1, "the withheld deployment must be counted, not silently dropped"
    assert body["degraded"] is False, "an honestly-empty board is not a degraded board"
    assert body["as_of"] is None


@pytest.mark.asyncio
async def test_empty_ledger_row_is_dropped_even_beside_a_real_one(monkeypatch):
    """The mixed case: the drop must be per-row, not an all-or-nothing bail.
    A real row still renders; the ledger-less one is still withheld."""
    _deploy("dep-real", owner_user_id="user-a", strategy_id="s-real", returns=[0.01, 0.02])
    _deploy("dep-empty", owner_user_id="user-a", strategy_id="s-empty", returns=None)

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    assert [e["deployment_id"] for e in body["entries"]] == ["dep-real"]
    assert body["total"] == 1
    assert body["withheld_no_ledger"] == 1


def test_builder_drops_empty_ledgers_without_any_io():
    """Same rule at the pure layer — the builder is the single place the rule
    lives, so it is asserted directly and not only through the route."""
    board = build_live_paper_leaderboard(
        [
            LivePaperLedger(
                deployment_id="d1",
                strategy_id="s1",
                name="Has data",
                inception=date(2026, 8, 1),
                returns=[(date(2026, 8, 1), 0.01)],
            ),
            LivePaperLedger(
                deployment_id="d2",
                strategy_id="s2",
                name="No data",
                inception=date(2026, 8, 1),
                returns=[],
            ),
        ]
    )
    assert [e.deployment_id for e in board.entries] == ["d1"]
    assert board.withheld_no_ledger == 1
    # No entry may ever carry days_live == 0 — the schema floor plus the drop.
    assert all(e.days_live >= 1 for e in board.entries)


# ── 2. Real rows carry real, recomputable numbers ──────────────────────────


@pytest.mark.asyncio
async def test_live_row_numbers_are_computed_from_the_ledger(monkeypatch):
    _named_strategy("s-real", "Cross-Sectional Momentum")
    _deploy(
        "dep-real",
        owner_user_id="user-a",
        strategy_id="s-real",
        deployed_at=date(2026, 8, 3),
        returns=[0.01, -0.005, 0.02],
    )

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    (row,) = body["entries"]
    expected = 1.01 * 0.995 * 1.02 - 1.0
    assert row["cumulative_return"] == pytest.approx(expected)
    assert row["days_live"] == 3
    assert row["inception_date"] == "2026-08-03"
    assert row["as_of"] == "2026-08-05", "as_of is the LAST observation's date, not today"
    assert row["last_updated"] is not None
    assert row["name"] == "Cross-Sectional Momentum"
    assert row["performance_basis"] == BASIS_LIVE_PAPER
    assert body["performance_basis"] == BASIS_LIVE_PAPER
    assert body["as_of"] == "2026-08-05"
    assert body["methodology"], "the forward board must state how its one number is computed"


@pytest.mark.asyncio
async def test_drift_is_surfaced_on_the_row(monkeypatch):
    """The ledger is append-only; a disagreeing replay stamps the deployment
    instead of rewriting it. A drifted track record must read as drifted."""
    _deploy("dep-drift", owner_user_id="user-a", strategy_id="s-d", returns=[0.01], drift=True)

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    assert body["entries"][0]["drift_detected"] is True


# ── 3. Ranking is realised forward return, descending ──────────────────────


@pytest.mark.asyncio
async def test_rows_sort_by_realised_return_descending(monkeypatch):
    _deploy("dep-low", owner_user_id="user-a", strategy_id="s-low", returns=[-0.05])
    _deploy("dep-high", owner_user_id="user-a", strategy_id="s-high", returns=[0.04])
    _deploy("dep-mid", owner_user_id="user-a", strategy_id="s-mid", returns=[0.01])

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    assert [e["deployment_id"] for e in body["entries"]] == ["dep-high", "dep-mid", "dep-low"]
    assert [e["rank"] for e in body["entries"]] == [1, 2, 3]
    assert body["sort_by"] == "cumulative_return"


# ── 4 & 5. Ownership and liveness are both real filters ────────────────────


@pytest.mark.asyncio
async def test_forward_board_never_leaks_another_users_deployment(monkeypatch):
    """Poison test, same shape as the conviction board's: B's deployment is
    the better performer, so a broken ownership filter would leak it straight
    to rank 1."""
    _deploy("dep-a", owner_user_id="user-a", strategy_id="s-a", returns=[0.01])
    _deploy("dep-b", owner_user_id="user-b", strategy_id="s-b", returns=[0.50])

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    assert [e["deployment_id"] for e in body["entries"]] == ["dep-a"]
    assert body["scope"] == "own"


@pytest.mark.asyncio
async def test_stopped_deployment_is_excluded(monkeypatch):
    """The board says "live paper trading". A stopped deployment is not live,
    ledger rows or not."""
    _deploy("dep-stopped", owner_user_id="user-a", strategy_id="s-s", status="stopped", returns=[0.30])
    _deploy("dep-active", owner_user_id="user-a", strategy_id="s-a", status="active", returns=[0.01])

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    assert [e["deployment_id"] for e in body["entries"]] == ["dep-active"]
    # A stopped deployment is not "withheld for lack of data" either — it is
    # out of cohort entirely, so it must not inflate that count.
    assert body["withheld_no_ledger"] == 0


# ── 6. Anonymous: honest empty board, never a 401, never a leak ────────────


@pytest.mark.asyncio
async def test_anonymous_gets_empty_board_not_an_error_and_not_a_leak():
    _deploy("dep-private", owner_user_id="user-a", strategy_id="s-p", returns=[0.42])

    async with _client() as client:
        client.headers.pop("cookie", None)
        resp = await client.get(LIVE_PAPER_URL)

    assert resp.status_code == 200, "public endpoint — never 401, same contract as the conviction board"
    body = resp.json()
    assert body["entries"] == []
    assert body["scope"] == "anonymous"
    assert body["degraded"] is False


# ── 7. The research board states its basis and its window ──────────────────


@pytest.mark.asyncio
async def test_research_board_rows_carry_backtest_basis_and_window(monkeypatch):
    from archimedes.api import leaderboard_routes
    from archimedes.api.schemas import StrategyResponse

    resp = StrategyResponse(
        id="r-1",
        methodology_summary="m",
        asset_universe=["SPY"],
        position_sizing="equal_weight",
        rebalance_frequency="daily",
        status="live",
        paper_title="A Research Row",
        sharpe_ratio=1.2,
        dsr_p_value=0.9,
        out_of_sample_sharpe=1.1,
        pbo_score=0.1,
        passes_rigor_gate=True,
        backtest_start="2015-01-01",
        backtest_end="2024-12-31",
    )
    monkeypatch.setattr(leaderboard_routes, "_own_cohort_responses", lambda *_a, **_k: ([resp], False, ""))

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get("/api/leaderboard?scope=own")).json()

    (row,) = body["entries"]
    assert row["performance_basis"] == BASIS_BACKTEST
    assert row["backtest_start"] == "2015-01-01"
    assert row["backtest_end"] == "2024-12-31"
    assert body["performance_basis"] == BASIS_BACKTEST
    # Conviction math is untouched: 100 × (0.35·1 + 0.25·0.9 + 0.25·1.0 + 0.15·0.9) = 96.0
    # (OOS 1.1 clamps to 1.0 against OOS_TARGET; overfit-resistance = 1 − PBO.)
    assert row["conviction_score"] == pytest.approx(96.0)


@pytest.mark.asyncio
async def test_research_row_without_a_window_says_so_rather_than_inventing_one(monkeypatch):
    from archimedes.api import leaderboard_routes
    from archimedes.api.schemas import StrategyResponse

    resp = StrategyResponse(
        id="r-2",
        methodology_summary="m",
        asset_universe=["SPY"],
        position_sizing="equal_weight",
        rebalance_frequency="daily",
        status="candidate",
    )
    monkeypatch.setattr(leaderboard_routes, "_own_cohort_responses", lambda *_a, **_k: ([resp], False, ""))

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get("/api/leaderboard?scope=own")).json()

    (row,) = body["entries"]
    assert row["backtest_start"] is None and row["backtest_end"] is None
    assert row["performance_basis"] == BASIS_BACKTEST


# ── 8. The two bases are never blended ─────────────────────────────────────


@pytest.mark.asyncio
async def test_neither_board_carries_the_other_boards_numbers(monkeypatch):
    """Anti-goal pin: no blended score. The forward response must expose no
    conviction/rigor field, and the research response must expose no
    live-paper field — a future 'combined score' cannot land without
    breaking this test."""
    _deploy("dep-a", owner_user_id="user-a", strategy_id="s-a", returns=[0.01])
    _sign_in(monkeypatch, "user-a")

    async with _client() as client:
        forward = (await client.get(LIVE_PAPER_URL)).json()
        research = (await client.get("/api/leaderboard?scope=own")).json()

    # Unchanged by #1764, deliberately: the forward row gained the verdict of
    # record (a dated four-state LABEL) and nothing else. `passes_rigor_gate`
    # stays forbidden — a bare boolean beside a forward return is the field a
    # consumer would blend or sort on — and no backtest METRIC may appear.
    forbidden_on_forward = {"conviction_score", "score_components", "passes_rigor_gate", "deflated_sharpe_ratio"}
    for row in forward["entries"]:
        assert not (forbidden_on_forward & set(row)), f"forward row leaked backtest fields: {set(row)}"
    assert "scoring_engine" not in forward
    for row in forward["entries"]:
        assert not ({"pbo_score", "out_of_sample_sharpe", "sharpe_ratio", "cagr"} & set(row))
        # The verdict that IS carried never travels without its provenance:
        # `graded_at` is what keeps it readable as a statement about the
        # backtest rather than about the ledger beside it.
        assert "rigor_gate_status" in row and "graded_at" in row
    # And nothing ranks on it — the board's one sort is its one forward number.
    assert forward["sort_by"] == "cumulative_return"

    forbidden_on_research = {"cumulative_return", "days_live", "inception_date"}
    for row in research["entries"]:
        assert not (forbidden_on_research & set(row)), f"research row leaked live-paper fields: {set(row)}"


# ── 9. The verdict of record, beside the forward record (#1764) ─────────────
#
# Deploy is AT WILL: a strategy whose gate said `fail`, `pending` or
# `degenerate` can be paper-traded, so a gate-REJECTED strategy can sit on this
# board with a real forward return. Shown bare, that row reads as the board
# endorsing the strategy. So every row carries the STORED verdict — read, never
# recomputed, and never a fabricated pass.


def _graded(strategy_id: str, **kwargs) -> None:
    """A passport row for ``strategy_id``, with only the columns the test names."""
    import archimedes.db as db
    from archimedes.models.strategy_passport_record import StrategyPassportRecord

    with db.get_session() as session:
        session.add(StrategyPassportRecord(id=strategy_id, methodology_summary="probe", **kwargs))
        session.commit()


@pytest.mark.asyncio
async def test_a_gate_failed_row_says_so_beside_its_forward_return(monkeypatch):
    """THE guard for this surface. The row keeps its realised return AND states
    the verdict the strategy was graded with, with the date that verdict was
    recorded."""
    _graded(
        "s-fail",
        rigor_gate_status="fail",
        passes_rigor_gate=False,
        graded_at=datetime(2026, 8, 30, 11, 22, 33),
        gate_version="gate-v1-deadbeefdeadbeef",
    )
    _deploy("dep-fail", owner_user_id="user-a", strategy_id="s-fail", returns=[0.01, 0.02])

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    (row,) = body["entries"]
    assert row["rigor_gate_status"] == "fail"
    assert row["graded_at"] == "2026-08-30T11:22:33"
    assert row["gate_version"] == "gate-v1-deadbeefdeadbeef"
    # Beside it, untouched: the forward number the verdict qualifies.
    assert row["cumulative_return"] == pytest.approx(1.01 * 1.02 - 1.0)


@pytest.mark.asyncio
async def test_a_strategy_with_no_passport_row_reads_pending_never_a_pass(monkeypatch):
    _deploy("dep-unknown", owner_user_id="user-a", strategy_id="s-never-graded", returns=[0.03])

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        (row,) = (await client.get(LIVE_PAPER_URL)).json()["entries"]

    assert row["rigor_gate_status"] == "pending"
    assert row["graded_at"] is None
    assert row["gate_version"] is None


@pytest.mark.asyncio
async def test_the_board_and_the_deployment_payload_state_one_verdict(monkeypatch):
    """Anti-split. ``/api/leaderboard/live-paper`` and ``/api/paper/deployments``
    describe the same deployment on two pages; both READ the same passport row
    through the same ``passport_loader`` derivation, so they cannot disagree.
    A read-time re-grade on either would be the #1746/#1747 split on a new
    surface."""
    import archimedes.db as db
    from archimedes.models.paper_store import PaperDeployment
    from archimedes.services.paper_trading import deployment_summary

    _graded(
        "s-deg",
        rigor_gate_status="degenerate",
        graded_at=datetime(2026, 8, 30, 11, 22, 33),
        gate_version="gate-v1-abc",
    )
    _deploy("dep-deg", owner_user_id="user-a", strategy_id="s-deg", returns=[0.01])

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        (row,) = (await client.get(LIVE_PAPER_URL)).json()["entries"]

    with db.get_session() as session:
        dep = session.query(PaperDeployment).filter_by(id="dep-deg").one()
        card = deployment_summary(session, dep)

    for key in ("rigor_gate_status", "graded_at", "gate_version"):
        assert row[key] == card[key], f"the board and the deployment card disagree on {key}"


@pytest.mark.asyncio
async def test_a_broken_passport_read_degrades_the_verdict_not_the_board(monkeypatch):
    """Per-FIELD degradation. The realised forward returns are this board's
    subject and are already loaded; a passport read that blows up must not take
    them down with it, and must never produce a pass."""
    from archimedes.services import passport_loader

    _graded("s-pass", rigor_gate_status="pass", passes_rigor_gate=True)
    _deploy("dep-pass", owner_user_id="user-a", strategy_id="s-pass", returns=[0.01, 0.01])

    def _boom(*_a, **_k):
        raise RuntimeError("strategy_passports.gate_version does not exist")

    monkeypatch.setattr(passport_loader, "stored_rigor_verdicts", _boom)

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    (row,) = body["entries"]
    assert row["rigor_gate_status"] == "pending", "a broken verdict read must never serve a stale or fabricated pass"
    assert row["cumulative_return"] == pytest.approx(1.01 * 1.01 - 1.0), "the ledger still serves"
    assert body["degraded"] is False, "the VERDICT degraded, not the board — the returns are real"


def test_the_builder_gives_every_row_a_verdict_even_with_none_loaded():
    """The pure layer's half: no arm of the builder emits a row without the
    verdict fields. A ledger with no verdict loaded renders the explicit
    ungraded state, never an omitted field."""
    from archimedes.services.leaderboard import UNGRADED_ENTRY_VERDICT

    board = build_live_paper_leaderboard(
        [
            LivePaperLedger(
                deployment_id="d1",
                strategy_id="s1",
                name="No verdict loaded",
                inception=date(2026, 8, 1),
                returns=[(date(2026, 8, 1), 0.01)],
            ),
            LivePaperLedger(
                deployment_id="d2",
                strategy_id="s2",
                name="Graded",
                inception=date(2026, 8, 1),
                returns=[(date(2026, 8, 1), 0.02)],
                verdict={
                    "passes_rigor_gate": False,
                    "rigor_gate_status": "fail",
                    "graded_at": "2026-08-30T11:22:33",
                    "gate_version": "gate-v1-abc",
                },
            ),
        ]
    )
    ungraded, graded = board.entries[1], board.entries[0]
    assert ungraded.rigor_gate_status == UNGRADED_ENTRY_VERDICT["rigor_gate_status"] == "pending"
    assert ungraded.graded_at is None and ungraded.gate_version is None
    assert graded.rigor_gate_status == "fail"
    assert graded.graded_at == "2026-08-30T11:22:33"
    # The boolean the deployment payload carries is dropped on the way in — a
    # bare pass/fail beside a forward return is the field that would get blended.
    assert not hasattr(graded, "passes_rigor_gate")


def test_the_boards_ungraded_verdict_is_the_paper_surfaces_ungraded_verdict():
    """Anti-drift across the layers. ``leaderboard.UNGRADED_ENTRY_VERDICT`` is a
    copy (the pure layer must not import the ORM), so it is pinned to
    ``passport_loader.UNGRADED_RIGOR_VERDICT`` here — a change to either that
    made them disagree would put two different answers to "has a gate graded
    this?" on the board and the deployment card."""
    from archimedes.services.leaderboard import UNGRADED_ENTRY_VERDICT
    from archimedes.services.passport_loader import UNGRADED_RIGOR_VERDICT

    assert set(UNGRADED_ENTRY_VERDICT) < set(UNGRADED_RIGOR_VERDICT)
    for key, value in UNGRADED_ENTRY_VERDICT.items():
        assert UNGRADED_RIGOR_VERDICT[key] == value, key


@pytest.mark.asyncio
async def test_the_verdict_never_reorders_the_board(monkeypatch):
    """The rank is realised forward return, and only that. A gate-FAILED
    deployment that earned more outranks a gate-PASSED one that earned less —
    the board reports what happened, and the verdict beside each row is what
    lets a reader judge it."""
    _graded("s-pass", rigor_gate_status="pass", passes_rigor_gate=True)
    _graded("s-fail", rigor_gate_status="fail", passes_rigor_gate=False)
    _deploy("dep-pass", owner_user_id="user-a", strategy_id="s-pass", returns=[0.01])
    _deploy("dep-fail", owner_user_id="user-a", strategy_id="s-fail", returns=[0.05])

    _sign_in(monkeypatch, "user-a")
    async with _client() as client:
        body = (await client.get(LIVE_PAPER_URL)).json()

    assert [e["deployment_id"] for e in body["entries"]] == ["dep-fail", "dep-pass"]
    assert [e["rigor_gate_status"] for e in body["entries"]] == ["fail", "pass"]
    assert body["sort_by"] == "cumulative_return"
