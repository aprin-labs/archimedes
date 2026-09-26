"""Tests for the GENERATED-strategy rigor gate (companion to test_selection_bias_routes.py).

``GET /api/selection-bias/gate/{strategy_id}`` previously 404d for every
GENERATED strategy (fusion/architect, persisted to ``strategy_store`` — not the
filesystem-backed curated ``LocalStrategyProvider`` the route otherwise grades).
That left the Strategy Passport's Deploy button + Rigor Strictness slider dead
for every generated strategy. These tests cover the fix
(``selection_bias_routes._generated_strategy_rigor``):

  1. A generated strategy with a real persisted backtest grades on ITS OWN
     context (never merged into the curated cohort) and returns 200 with a
     non-null ``min_passing_level``.
  2. Ownership (#850 pattern): an unpublished, non-owned strategy 404s for a
     non-owner — existence never leaks via a different response shape.
  3. "Pending Backtest": a strategy that exists but has no (or too few)
     persisted daily returns returns 200 with a MISSING-shaped result, not a
     404 and not a crash.
  4. An unknown id is still a plain 404 (no regression on the pre-existing
     curated-cohort-only behavior).
  5. The companion look-ahead-threading fix in
     ``live_rigor_gate.verdict_from_returns``: passing
     ``look_ahead_audit_passed=True`` can flip a verdict that would otherwise
     be blocked by the always-on look-ahead floor.

DB fixture follows the proven rebind pattern from test_strategy_ownership.py /
test_live_gate_returns.py: db.engine/SessionLocal are module-level globals
created once at import, so ``monkeypatch.setenv`` alone does not repoint them —
both must be rebound to a fresh per-test sqlite engine.
"""

from __future__ import annotations

import json
import time

import archimedes.db as db
import numpy as np
import pytest
from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from archimedes.services.rigor_profiles import DSR_P_BADGE_MIN
from httpx import ASGITransport, AsyncClient

_W_OWNER = "0xAbC1230000000000000000000000000000000A"  # mixed case on purpose
_W_OTHER = "0x0000000000000000000000000000000000000B"


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Point the DB at a FRESH temp sqlite (rebinds db.engine + db.SessionLocal).

    db.engine/SessionLocal are created once at import, so setenv alone doesn't
    re-point them; rebind both to a per-test engine, then init_db() registers
    every table (incl. the passport/backtest side-effect imports).
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'generated_gate.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()
    yield


def _siwe_cookies(wallet: str) -> dict[str, str]:
    """Valid signed SIWE session cookie for `wallet` (same helper shape as
    test_user_routes.py / test_strategy_ownership.py — a real signed session,
    not header spoofing)."""
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


def _mk_strategy(
    sid: str,
    *,
    owner: str | None = None,
    published: bool = False,
    example: bool = False,
    name: str = "Generated Test Strategy",
):
    from archimedes.models.strategy_store import StrategyRecord

    with db.get_session() as session:
        row = StrategyRecord(
            id=sid,
            content_hash=("0x" + sid).ljust(66, "0"),
            generation_method="fusion",
            source_papers="[]",
            strategy_name=name,
            thesis="test thesis",
            asset_universe="[]",
            risk_profile="moderate",
            status="candidate",
            is_example=example,
            owner_wallet=owner.lower() if owner else None,
            is_published=published,
        )
        session.add(row)
        session.commit()


def _strong_returns(seed: int = 42, n: int = 300) -> list[float]:
    """A realistic, comfortably-passing daily-return series (deterministic)."""
    return np.random.default_rng(seed).normal(0.0018, 0.006, n).tolist()


def _mk_backtest(
    sid: str,
    *,
    returns: list[float] | None,
    num_trials: int | None = 1,
    pbo: float | None = 0.05,
    look_ahead: bool = True,
    backtest_engine: str | None = None,
):
    """Persist a BacktestResultRecord whose artifact_json carries daily_returns
    (mirrors how the real pipeline / analytics-engine writes them — see
    backend/archimedes/services/backtest_repository.get_daily_returns).

    ``backtest_engine`` defaults to None (matches every pre-existing caller in
    this file — legacy/untagged row shape) — tests exercising the num_trials
    PROVENANCE discriminator (V3) pass it explicitly.
    """
    from archimedes.models.backtest_store import BacktestResultRecord

    artifact_json = None
    if returns is not None:
        artifact_json = json.dumps({"results": [{"metrics": {"daily_returns": returns}}]})

    with db.get_session() as session:
        session.add(
            BacktestResultRecord(
                strategy_id=sid,
                content_hash=f"bt_{sid}",
                artifact_json=artifact_json,
                num_trials_in_selection=num_trials,
                pbo_score=pbo,
                look_ahead_audit_passed=look_ahead,
                source_pipeline="test",
                backtest_engine=backtest_engine,
            )
        )
        session.commit()


# ── 1. Found + graded: real persisted backtest → 200, non-null min_passing_level ──


async def test_generated_strategy_found_and_graded_not_404():
    sid = "gen0000000000001"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_backtest(sid, returns=_strong_returns())

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/selection-bias/gate/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["strategy_id"] == sid
    assert body["min_passing_level"] is not None
    # Real numbers, not the MISSING/pending shape.
    assert body["gate_details"]["dsr"] != "MISSING (no backtest data)"
    assert body["dsr_p_value"] is not None
    # A real, non-flat series is not degenerate — the inverse half of the guard
    # below, so a mutation hard-coding degenerate=True on this path is caught.
    assert body["degenerate"] is False


async def test_generated_strategy_zero_variance_series_reports_degenerate():
    """#1358 round-3: the generated single-strategy path must carry the same
    ``degenerate`` discriminator the cohort path does.

    A flat persisted series leaves dsr_p_value and oos_sharpe at None, which
    trips ``blocked_by_floor`` — so without this field the passport receives a
    row indistinguishable from a fully-graded correctness failure, and says
    "fails an always-on correctness floor" about a series no floor measured.
    ``pending`` cannot carry it: the series is long enough to be scored, it just
    has no variance to score.
    """
    sid = "gen0000000000009"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_backtest(sid, returns=[0.0] * 300)

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/selection-bias/gate/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["degenerate"] is True, (
        "a zero-variance persisted series must be reported as degenerate on the generated path too"
    )
    assert body["pending"] is False, (
        "degenerate is not pending — this row has 300 persisted returns, they are merely flat"
    )
    # Documents WHY the field is needed: the floor trips mechanically here.
    assert body["blocked_by_floor"] is True
    assert body["min_passing_level"] is None


async def test_generated_strategy_published_visible_to_anonymous_caller():
    sid = "gen0000000000002"
    _mk_strategy(sid, owner=_W_OWNER, published=True)
    _mk_backtest(sid, returns=_strong_returns())

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/selection-bias/gate/{sid}")  # no cookie

    assert resp.status_code == 200
    assert resp.json()["min_passing_level"] is not None


# ── 2. Ownership: unpublished + non-owner (and anonymous) → 404, existence hidden ──


async def test_generated_strategy_ownership_hides_existence():
    sid = "gen0000000000003"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_backtest(sid, returns=_strong_returns())

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anon = await client.get(f"/api/selection-bias/gate/{sid}")
        other = await client.get(f"/api/selection-bias/gate/{sid}", cookies=_siwe_cookies(_W_OTHER))
        owner = await client.get(f"/api/selection-bias/gate/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert anon.status_code == 404
    assert other.status_code == 404  # 404, not 403 — existence stays hidden
    assert owner.status_code == 200


# ── 3. Pending: strategy exists but no (or too little) backtest data ─────────


async def test_generated_strategy_no_backtest_row_is_missing_shaped_not_404():
    sid = "gen0000000000004"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    # No BacktestResultRecord at all.

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/selection-bias/gate/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["min_passing_level"] is None
    assert body["blocked_by_floor"] is False
    assert body["passes_all"] is False
    assert body["gate_details"]["dsr"] == "MISSING (no backtest data)"
    assert body["gate_details"]["look_ahead"] == "MISSING (no code)"


async def test_generated_strategy_too_few_returns_is_missing_shaped_not_404():
    sid = "gen0000000000005"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_backtest(sid, returns=[0.001] * 5)  # < 10 observations

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/selection-bias/gate/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["min_passing_level"] is None
    assert body["gate_details"]["pbo"] == "MISSING (no backtest data)"


# ── 4. Unknown id is still a plain 404 (no regression) ────────────────────────


async def test_unknown_id_still_404():
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/selection-bias/gate/totally-unknown-id-xyz")

    assert resp.status_code == 404


# ── 5. Look-ahead threading: verdict_from_returns(look_ahead_audit_passed=True) ──


def test_look_ahead_override_flips_blocked_verdict_to_pass():
    """Without the override, strategy_code=None forces look_ahead_passed=False
    unconditionally, tripping the always-on floor (blocked_by_floor=True) no
    matter how strong the DSR/PBO/OOS numbers are. Passing the pipeline's own
    closed-DSL self-attestation through fixes that — the companion bug fix in
    live_rigor_gate.verdict_from_returns (backend/archimedes/agents/
    generation_pipeline.py:1436 wires this from the already-computed
    result.look_ahead_audit_passed)."""
    from archimedes.services.live_rigor_gate import verdict_from_returns
    from archimedes.services.rigor_evaluator import compute_pbo

    a = np.random.default_rng(0).normal(0.0015, 0.004, 500).tolist()
    b = np.random.default_rng(1).normal(0.0015, 0.004, 500).tolist()
    pbo = compute_pbo({"a": a, "b": b})

    without = verdict_from_returns("a", a, num_trials=2, pbo_scores=pbo)
    assert without.status == "fail"
    assert without.passes is False
    assert without.blocked_by_floor is True

    with_override = verdict_from_returns("a", a, num_trials=2, pbo_scores=pbo, look_ahead_audit_passed=True)
    assert with_override.status == "pass"
    assert with_override.passes is True
    assert with_override.blocked_by_floor is False


# ── 6. num_trials PROVENANCE discriminator (V3 / W3, num_trials-provenance audit 2026-08-03) ──
#
# `_generated_strategy_rigor` used to do a bare read of the persisted
# `num_trials_in_selection` column — ANY populated value was trusted,
# regardless of which pipeline wrote it. These tests prove the replacement
# (`_num_trials_for_generated_row`) discriminates on `backtest_engine`
# PROVENANCE instead, using a return series (seed=3, mu=0.0006, sigma=0.008,
# n=300) deliberately chosen so num_trials=1 vs num_trials=25 flips the
# DSR-gate verdict (PASS at N=1, FAIL at N=25) — an end-to-end, observable
# consequence, not just an internal label.


def _flip_sensitive_returns() -> list[float]:
    """PASSES the DSR gate at num_trials=1 (p≈0.964) and FAILS it at
    num_trials=25 (p≈0.377) — verified directly against
    rigor_evaluator.run_rigor_gate with these exact parameters. The series
    straddles the badge bar at both ends by a wide margin, so it stayed
    flip-sensitive across #1794's move of the bar; the assertions below compare
    against ``DSR_P_BADGE_MIN`` rather than a literal so they cannot go stale
    the next time it moves."""
    return np.random.default_rng(3).normal(0.0006, 0.008, 300).tolist()


def test_num_trials_for_generated_row_discriminates_on_provenance():
    """Unit-level: the discriminator trusts a stored value ONLY for engines
    whose live writer tracks its own selection-pool size; everything else
    (unknown/missing engine, or a search-tracked engine with no real value
    stored) is forced to num_trials=1 with an explicit, honest scope label —
    never a bare 'is the column populated' check."""
    from archimedes.api.selection_bias_routes import (
        _SCOPE_GENERATED_SEARCH_POOL,
        _SCOPE_GENERATED_UNTRACKED_DEFAULT,
        _num_trials_for_generated_row,
    )

    # Trusted: a search-tracking engine with a real stored pool size.
    assert _num_trials_for_generated_row("dsl-fusion", 35) == (35, _SCOPE_GENERATED_SEARCH_POOL)
    assert _num_trials_for_generated_row("portfolio-simulator-v1", 12) == (12, _SCOPE_GENERATED_SEARCH_POOL)

    # Adversarial: an UNKNOWN/missing engine tag with a large stored value that
    # LOOKS like a real search pool (exactly the shape a stale/borrowed
    # library-size write would produce, e.g. the V1 bug) must NOT be trusted —
    # this is the case the bare-read code got wrong.
    assert _num_trials_for_generated_row(None, 25) == (1, _SCOPE_GENERATED_UNTRACKED_DEFAULT)
    assert _num_trials_for_generated_row("backtrader", 25) == (1, _SCOPE_GENERATED_UNTRACKED_DEFAULT)
    assert _num_trials_for_generated_row("some-future-engine", 999) == (1, _SCOPE_GENERATED_UNTRACKED_DEFAULT)

    # A search-tracked engine with no real value stored (falsy) still forces 1.
    assert _num_trials_for_generated_row("dsl-fusion", None) == (1, _SCOPE_GENERATED_UNTRACKED_DEFAULT)
    assert _num_trials_for_generated_row("dsl-fusion", 0) == (1, _SCOPE_GENERATED_UNTRACKED_DEFAULT)

    # NEGATIVES. This is the case a truthiness test cannot catch and the two
    # above do not discriminate: 0 and None are already falsy, so they pass
    # against a buggy ``and stored_num_trials`` implementation too. -5 is
    # TRUTHY, so only a ``> 0`` check rejects it.
    #
    # Why it matters beyond hygiene: num_trials is a COUNT of candidates
    # evaluated, and it feeds the DSR multiple-testing deflation. A negative
    # would not merely be ignored -- it would make the correction weaker than
    # applying no correction at all, so a corrupt row would grade MORE
    # favourably than an honest one. Fail closed to 1.
    assert _num_trials_for_generated_row("dsl-fusion", -5) == (1, _SCOPE_GENERATED_UNTRACKED_DEFAULT)
    assert _num_trials_for_generated_row("portfolio-simulator-v1", -1) == (1, _SCOPE_GENERATED_UNTRACKED_DEFAULT)


async def test_dsl_fusion_engine_trusts_stored_search_pool_end_to_end():
    """A row tagged 'dsl-fusion' with a stored num_trials is graded at THAT
    N (trusted, real search-pool size) — verdict FAILS because num_trials=25
    applies the full multiple-testing penalty to this return series."""
    sid = "gen0000000000006"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_backtest(sid, returns=_flip_sensitive_returns(), num_trials=25, backtest_engine="dsl-fusion")

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/selection-bias/gate/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["num_trials_scope"] == "generated_search_pool"
    assert body["dsr_p_value"] < DSR_P_BADGE_MIN
    assert body["passes_all"] is False


async def test_untracked_engine_num_trials_forced_to_one_not_bare_column_read():
    """THE adversarial case (V3): a row with NO recognized search-tracking
    engine (backtest_engine=None — the untagged/legacy shape) but a stored
    num_trials_in_selection=25 that LOOKS like a real cohort/search size.

    Before the fix, `_generated_strategy_rigor` did a bare read of the
    column: ``latest.num_trials_in_selection if latest and
    latest.num_trials_in_selection else 1`` — this would have trusted 25 and
    FAILED the gate (dsr_p≈0.377, far under the badge bar) on a return series that is
    genuinely fine at its true (self-contained) trial count. The fix
    discriminates on provenance: an untracked engine cannot prove it ran a
    real 25-candidate search, so num_trials is forced to 1 and the gate
    PASSES (dsr_p≈0.964, over the badge bar) — the honest verdict for this strategy.
    """
    sid = "gen0000000000007"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_backtest(sid, returns=_flip_sensitive_returns(), num_trials=25, backtest_engine=None)

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/selection-bias/gate/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["num_trials_scope"] == "generated_untracked_default"
    assert body["dsr_p_value"] >= DSR_P_BADGE_MIN, (
        f"num_trials was not forced to 1 — the bare-column-read bug is back "
        f"(dsr_p_value={body['dsr_p_value']} suggests the untrusted stored "
        f"num_trials=25 was used instead)"
    )
    assert body["passes_all"] is True


# ── 7. The SAME num_trials provenance reaches GET /api/strategies/{id} (#1358 A4) ──
#
# selection-bias/gate/{id} is not the only surface the Strategy Passport reads
# num_trials from — StrategyPassport.jsx ternaries on `num_trials_in_selection`
# from `GET /api/strategies/{id}` (StrategyResponse), a field that did not exist
# on the schema at all before #1358 (grep -n num_trials backend/archimedes/api/
# schemas.py had no match). These tests prove the two surfaces agree — the
# StrategyResponse fields are sourced from the SAME `_num_trials_for_generated_row`
# discriminator, not a re-derived or bare column read.


def _mk_passport_record(sid: str, *, sharpe_ratio: float | None = None):
    """A minimal StrategyPassportRecord row so GET /api/strategies/{id} resolves
    through `_passport_to_strategy_response` (needs BOTH a strategy_store row —
    `_mk_strategy`, for the visibility gate — and a strategy_passports row, for
    the served content; `get_passport` reads only the latter)."""
    from archimedes.models.strategy_passport_record import PassportPaperRef, StrategyPassportRecord

    with db.get_session() as session:
        session.add(
            StrategyPassportRecord(
                id=sid,
                generation_method="fusion",
                methodology_summary="test methodology",
                asset_universe="[]",
                position_sizing="equal_weight",
                rebalance_frequency="weekly",
                status="candidate",
                regime_tag="regime_neutral",
                passes_rigor_gate=False,
                sharpe_ratio=sharpe_ratio,
            )
        )
        session.add(PassportPaperRef(passport_id=sid, arxiv_id="2301.00001", title="A Paper", authors="[]"))
        session.commit()


async def test_strategy_response_num_trials_matches_gate_for_search_tracked_engine():
    sid = "gen0000000000008"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_passport_record(sid, sharpe_ratio=0.9)
    _mk_backtest(sid, returns=_flip_sensitive_returns(), num_trials=25, backtest_engine="dsl-fusion")

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/strategies/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["num_trials_in_selection"] == 25
    assert body["num_trials_scope"] == "generated_search_pool"


async def test_strategy_response_num_trials_forced_to_one_for_untracked_engine():
    sid = "gen0000000000009"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_passport_record(sid, sharpe_ratio=0.9)
    _mk_backtest(sid, returns=_flip_sensitive_returns(), num_trials=25, backtest_engine=None)

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/strategies/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["num_trials_in_selection"] == 1
    assert body["num_trials_scope"] == "generated_untracked_default"


async def test_strategy_response_num_trials_unspecified_without_backtest():
    """#1358: a generated strategy with no persisted backtest at all must not
    claim ANY num_trials — never a silently-assumed 1. This is the exact
    passport defect the issue reports: StrategyPassport.jsx's ternary on
    `num_trials_in_selection` had nothing to read (the field did not exist),
    so its `else` branch always won and unconditionally asserted num_trials=1
    for every strategy, graded or not."""
    sid = "gen0000000000010"
    _mk_strategy(sid, owner=_W_OWNER, published=False)
    _mk_passport_record(sid, sharpe_ratio=None)
    # No BacktestResultRecord at all.

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/strategies/{sid}", cookies=_siwe_cookies(_W_OWNER))

    assert resp.status_code == 200
    body = resp.json()
    assert body["num_trials_in_selection"] is None
    assert body["num_trials_scope"] == "unspecified"
    assert body["rigor_gate_status"] == "pending"
