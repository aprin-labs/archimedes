"""Tests for the streaming Generate pipeline.

Forces the fixture path (no LLM credentials needed) and asserts that the
event sequence matches the spec ordering and that a strategy is persisted
at the end.

Debate-path tests (test_debate_dispatch_*, test_debate_critic_rigor_*,
test_debate_text_only_*) stub the debate engine's proposer and evaluator
seams to run _run_debate_leaderboard hermetically without a live LLM or
real market-data backtest. Pattern mirrors test_debate_engine.py.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from archimedes.agents.generation_pipeline import run_generation
from archimedes.api.generate_schemas import GenerateBrief
from archimedes.services.dsl_lookahead_audit import PASS as LOOK_AHEAD_PASS
from archimedes.services.dsl_lookahead_audit import DslLookAheadAudit


@pytest.fixture(autouse=True)
def force_fixture_path(monkeypatch):
    monkeypatch.setenv("GENERATION_PIPELINE_FIXTURE", "1")


class _FakeStore:
    """In-memory JobStore stand-in. Captures the event sequence."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.status: list[tuple[str, dict | None, str]] = []
        self.current_status: str | None = None

    async def push_event(self, job_id, payload):
        self.events.append(payload)
        return len(self.events)

    async def update_status(self, job_id, status, *, result=None, error=""):
        self.status.append((status, result, error))
        self.current_status = status

    async def update_terminal_status(self, job_id, status, *, result=None, error=""):
        """Mirrors ``JobStore.update_terminal_status`` (#1355): a no-op once
        ``cancelled`` has already been recorded."""
        if self.current_status == "cancelled":
            return False
        await self.update_status(job_id, status, result=result, error=error)
        return True


@pytest.mark.asyncio
async def test_fixture_pipeline_emits_full_event_sequence():
    store = _FakeStore()
    brief = GenerateBrief(intent="13-week treasury alternative", risk_appetite="conservative")

    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_fixture_001", "0xdeadbeef")),
    ):
        await run_generation(job_id="job_fixture_001", brief=brief, n_candidates=1, store=store)

    names = [e["event"] for e in store.events]
    # Spec ordering — terminal `done` must be last
    assert names[0] == "job_queued"
    assert names[1] == "brief_validated"
    assert names[2] == "pipeline_selected"
    assert names[3] == "candidates_selected"
    assert "pipeline_selected" in names
    assert "agent_iteration" in names
    assert "tool_called" in names
    assert "candidate_drafted" in names
    assert "candidate_evaluated" in names
    assert "best_selected" in names
    assert "trace_hashed" in names
    assert "persisted" in names
    assert names[-1] == "done"

    # Dual regime: both bull + bear candidates drafted
    drafted = [e for e in store.events if e["event"] == "candidate_drafted"]
    assert len(drafted) == 2
    drafted_regimes = {e["data"]["regime"] for e in drafted}
    assert drafted_regimes == {"bull", "bear"}


@pytest.mark.asyncio
async def test_pipeline_terminates_with_done_status():
    store = _FakeStore()
    brief = GenerateBrief(intent="balanced macro", risk_appetite="moderate")

    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_test_002", "0xabc")),
    ):
        await run_generation(job_id="job_002", brief=brief, n_candidates=1, store=store)

    # Status sequence: running → done
    statuses = [s[0] for s in store.status]
    assert statuses == ["running", "done"]
    last_result = store.status[-1][1]
    assert last_result is not None
    assert last_result["best_strategy_id"] == "strat_test_002"
    # Default dual_regime=True → 2 candidates (bull + bear)
    assert len(last_result["candidates"]) == 2
    regimes = {c["regime"] for c in last_result["candidates"]}
    assert regimes == {"bull", "bear"}


@pytest.mark.asyncio
async def test_pipeline_writes_identity_ledger_generation_completed_for_owned_job():
    """Issue #1028 (D2/D2a): an IDENTIFIED run (owner_wallet set) ledgers a
    ``generation_completed`` event carrying the winning strategy/candidate ids."""
    from archimedes.db import get_session
    from archimedes.models.identity import IdentityEvent, WalletIdentity

    wallet = "0x" + "aa11" * 10

    def _cleanup():
        session = get_session()
        try:
            session.query(IdentityEvent).filter(IdentityEvent.wallet == wallet).delete(synchronize_session=False)
            session.query(WalletIdentity).filter(WalletIdentity.wallet_address == wallet).delete(
                synchronize_session=False
            )
            session.commit()
        finally:
            session.close()

    _cleanup()
    store = _FakeStore()
    brief = GenerateBrief(intent="balanced macro", risk_appetite="moderate")

    try:
        with patch(
            "archimedes.agents.generation_pipeline._persist_candidate",
            new=AsyncMock(return_value=("strat_owned_001", "0xabc")),
        ):
            await run_generation(job_id="job_owned_001", brief=brief, n_candidates=1, store=store, owner_wallet=wallet)

        session = get_session()
        try:
            events = session.query(IdentityEvent).filter(IdentityEvent.wallet == wallet).all()
            shapes = [(e.event_type, e.actor_class, e.meta) for e in events]
        finally:
            session.close()
    finally:
        _cleanup()

    matching = [s for s in shapes if s[0] == "generation_completed"]
    assert len(matching) == 1
    _, actor_class, meta = matching[0]
    assert actor_class == "human"
    assert meta["best_strategy_id"] == "strat_owned_001"


@pytest.mark.asyncio
async def test_pipeline_does_not_ledger_generation_completed_for_anonymous_job():
    """D2a: owner_wallet=None (anonymous generation) has nothing to anchor a
    row to — must not write to the identity ledger."""
    from archimedes.db import get_session
    from archimedes.models.identity import IdentityEvent

    session = get_session()
    try:
        before = session.query(IdentityEvent).filter(IdentityEvent.event_type == "generation_completed").count()
    finally:
        session.close()

    store = _FakeStore()
    brief = GenerateBrief(intent="balanced macro", risk_appetite="moderate")
    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_anon_001", "0xabc")),
    ):
        await run_generation(job_id="job_anon_001", brief=brief, n_candidates=1, store=store, owner_wallet=None)

    session = get_session()
    try:
        after = session.query(IdentityEvent).filter(IdentityEvent.event_type == "generation_completed").count()
    finally:
        session.close()
    assert after == before


def test_rigor_adapter_computes_dsr_and_oos_sharpe_on_synthetic_series():
    """Wires Önder's rigor_evaluator on a synthetic return series.

    Verifies the verdict shape matches the contract the SSE event +
    frontend RejectedCandidates view expect — all four fields present,
    `passing` is a bool, numeric fields are floats.
    """
    # Synthetic price histories — two assets, trending up with noise.
    import random

    from archimedes.agents.generation_pipeline import (
        _portfolio_return_series,
        _rigor_verdict_for,
    )

    random.seed(42)
    n = 250
    spy = [100.0]
    gld = [100.0]
    for _ in range(n - 1):
        spy.append(spy[-1] * (1 + 0.0005 + random.gauss(0, 0.01)))
        gld.append(gld[-1] * (1 + 0.0002 + random.gauss(0, 0.008)))

    histories = {
        "sSPY": {"close": spy},
        "sGLD": {"close": gld},
    }
    weights = {"sSPY": 0.6, "sGLD": 0.4}

    series = _portfolio_return_series(weights, histories)
    assert len(series) == n - 1

    verdict = _rigor_verdict_for(series, num_trials=3)
    assert set(verdict.keys()) >= {
        "dsr",
        "oos_sharpe",
        "lookahead_audit_passed",
        "passing",
        "pbo",
    }
    assert isinstance(verdict["passing"], bool)
    assert verdict["dsr"] is not None
    assert verdict["oos_sharpe"] is not None


def test_rigor_adapter_handles_empty_series():
    """Short series should produce None metrics + non-passing verdict."""
    from archimedes.agents.generation_pipeline import _rigor_verdict_for

    verdict = _rigor_verdict_for([], num_trials=1)
    assert verdict["dsr"] is None
    assert verdict["oos_sharpe"] is None
    assert verdict["passing"] is False


def test_rigor_adapter_deflates_with_num_trials():
    """Higher num_trials must deflate the DSR (lower deflated Sharpe).

    Regression for the audit finding that the Generate flow called
    `_rigor_verdict_for(..., num_trials=1)`, which collapses the DSR
    expectation-of-max term to 0 and leaves the ratio undeflated. With a real
    library-size trial count the deflated Sharpe must be strictly lower.
    """
    import random

    from archimedes.agents.generation_pipeline import _rigor_verdict_for

    random.seed(7)
    series = [0.001 + random.gauss(0, 0.01) for _ in range(250)]

    v1 = _rigor_verdict_for(series, num_trials=1)
    v_lib = _rigor_verdict_for(series, num_trials=6)
    assert v1["dsr"] is not None and v_lib["dsr"] is not None
    # Deflating for 6 trials instead of 1 lowers the deflated Sharpe.
    assert v_lib["dsr"] < v1["dsr"]


def test_rigor_adapter_lookahead_gates_passing():
    """A failed look-ahead audit must force `passing` False even if DSR/OOS pass.

    Regression for the audit finding that look-ahead was hardcoded True and never
    gated the verdict (the fourth admission primitive was unenforced).
    """
    import random

    from archimedes.agents.generation_pipeline import _rigor_verdict_for

    random.seed(11)
    # Strongly positive drift so DSR/OOS would otherwise pass.
    series = [0.004 + random.gauss(0, 0.006) for _ in range(300)]

    clean = _rigor_verdict_for(series, num_trials=4, lookahead_passed=True)
    leaked = _rigor_verdict_for(series, num_trials=4, lookahead_passed=False)
    assert leaked["lookahead_audit_passed"] is False
    assert leaked["passing"] is False
    # Same series, only the look-ahead flag differs → it is the deciding factor.
    if clean["passing"]:
        assert leaked["passing"] is False


def test_lookahead_for_candidate_ignores_backtrader_negative_index(tmp_path):
    """Curated backtrader strategies use safe close[-N]; that must not fail audit.

    Genuine forward-looking patterns (shift(-1), positive data index) must fail.
    """
    from archimedes.agents.generation_pipeline import _lookahead_for_candidate

    class _Strat:
        def __init__(self, path, title):
            self.strategy_code_path = path
            self.paper_title = title

    safe = tmp_path / "safe_strat.py"
    safe.write_text("def next(self):\n    return self.data.close[-1] - self.data.close[-2]\n")
    leaky = tmp_path / "leaky_strat.py"
    leaky.write_text("import pandas as pd\ndef signal(df):\n    return df['close'].shift(-1)\n")

    assert _lookahead_for_candidate([_Strat(str(safe), "Safe")]) is True
    assert _lookahead_for_candidate([_Strat(str(leaky), "Leaky")]) is False
    # No auditable source → vacuously clean (buy-and-hold by construction).
    assert _lookahead_for_candidate([]) is True


def test_pick_pipeline_always_debate_after_cutover(monkeypatch):
    # T1.1 Phase-3: _pick_pipeline always returns 'debate' regardless of flag
    # state, fusion enabled/disabled, library size, or mode override.
    # The legacy fusion/architect/agent decision tree is deleted; no mocking of
    # fusion_enabled or default_provider is needed or meaningful.
    #
    # Equivalent coverage of the old architect-branch regression now lives in
    # test_debate_engine.py::test_pick_pipeline_is_debate_unconditionally.
    from archimedes.agents import generation_pipeline as gp

    for override in (None, "fusion", "architect", "agent", "debate"):
        name, reason = gp._pick_pipeline(mode_override=override)
        assert name == "debate", f"override {override!r} routed off the society (got {name!r})"
        assert "debate" in reason.lower()


def test_pick_pipeline_reason_is_user_copy_not_internal_jargon():
    """The reason string is USER-FACING: run_generation emits it verbatim on the
    SSE stream (`pipeline_selected`) and GenerationStream.jsx renders it into
    every viewer's event log. Internal planning shorthand ("T1.1 Phase-3
    cutover") shipped to production screens through exactly this pipe
    (found in owner review, 2026-08-30). Guard: no issue-tracker numbering,
    no cutover/phase/sprint/roadmap vocabulary, ever."""
    import re

    from archimedes.agents import generation_pipeline as gp

    jargon = re.compile(r"T\d\.\d|cutover|Phase-|phase-\d|sprint|roadmap|TODO", re.IGNORECASE)
    for override in (None, "fusion", "architect", "agent", "debate"):
        _, reason = gp._pick_pipeline(mode_override=override)
        hit = jargon.search(reason)
        assert hit is None, f"internal jargon {hit.group(0)!r} in user-facing reason: {reason!r}"


@pytest.mark.asyncio
async def test_pipeline_emits_cancellation_event_when_task_cancelled():
    """CancelledError path emits a CANCELLED error event + flips status."""
    store = _FakeStore()
    brief = GenerateBrief(intent="cancel me mid-run", risk_appetite="moderate")

    # Drive the pipeline as a real task so we can cancel it.
    task = asyncio.create_task(
        run_generation(
            job_id="job_cancel_001",
            brief=brief,
            n_candidates=1,
            store=store,
        )
    )
    # Let it kick off (job_queued + brief_validated emit synchronously).
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cancellation handler must have logged the error event + status.
    names = [e["event"] for e in store.events]
    assert "error" in names
    err = next(e for e in store.events if e["event"] == "error")
    assert err["data"]["code"] == "CANCELLED"
    assert err["data"]["recoverable"] is False
    statuses = [s[0] for s in store.status]
    assert statuses[-1] == "cancelled"


@pytest.mark.asyncio
async def test_brief_validation_rejects_invalid_brief(monkeypatch):
    """When the validator says is_valid=false, emit BRIEF_INVALID and stop.

    Forces the live path (so the validator runs) by mocking _llm_available;
    fake the LLM result to flag the brief invalid; assert the pipeline
    emits the error event and never reaches candidates_selected.
    """
    from archimedes.services import generation_pipeline as gp

    monkeypatch.setattr(gp, "_llm_available", lambda: True)

    async def fake_validate(brief):
        return {
            "is_valid": False,
            "reason": "Brief looks like a recipe, not a strategy intent.",
            "hint": "Try mentioning an asset class or risk appetite.",
        }

    monkeypatch.setattr(gp, "_validate_brief", fake_validate)

    store = _FakeStore()
    brief = GenerateBrief(intent="add flour and bake at 350F", risk_appetite="moderate")
    await gp.run_generation(job_id="job_invalid_001", brief=brief, n_candidates=1, store=store)

    names = [e["event"] for e in store.events]
    assert "candidates_selected" not in names  # pipeline halted before agent ran
    err = next((e for e in store.events if e["event"] == "error"), None)
    assert err is not None
    assert err["data"]["code"] == "BRIEF_INVALID"
    assert err["data"]["recoverable"] is True
    statuses = [s[0] for s in store.status]
    assert statuses[-1] == "error"


@pytest.mark.asyncio
async def test_validated_asset_classes_are_folded_into_brief_before_dispatch(monkeypatch):
    """#892: brief_validated's asset_classes_inferred must reach the brief the
    pipeline actually dispatches with — not just the SSE event payload.

    Reproduces the issue's exact bug: the user's brief is classified with
    ["crypto", "treasuries"] (brief_validated correctly reports it), but before
    this fix, brief.asset_classes downstream still held whatever the CLIENT
    originally sent (here: nothing at all) because the validator's inferred
    classes were only ever used for the SSE display event, never folded back
    into the brief object that the debate society / fusion universe steering
    consumes. Forces the live-validator branch (_llm_available=True) but
    routes to the fixture runner (_debate_can_run=False) so the assertion is
    hermetic — no LLM, no corpus.
    """
    from archimedes.services import generation_pipeline as gp

    monkeypatch.setattr(gp, "_llm_available", lambda: True)

    async def fake_validate(brief):
        return {
            "is_valid": True,
            "intent_summary": brief.intent[:140],
            "asset_classes_inferred": ["crypto", "treasuries"],
            "time_horizon_inferred": "unknown",
            "risk_appetite_adjusted": brief.risk_appetite,
        }

    monkeypatch.setattr(gp, "_validate_brief", fake_validate)

    # Force the fixture runner (not the debate society) so this stays
    # hermetic, while still exercising the live-style validation branch above.
    import archimedes.agents.debate_engine as de

    monkeypatch.setattr(de, "_debate_can_run", lambda brief: False)
    monkeypatch.setenv("GENERATION_PIPELINE_FIXTURE", "1")

    seen_asset_classes: list[list[str] | None] = []
    real_runner = gp._run_fixture_candidate

    async def _spy_runner(*, brief, **kwargs):
        seen_asset_classes.append(list(brief.asset_classes or []))
        return await real_runner(brief=brief, **kwargs)

    monkeypatch.setattr(gp, "_run_fixture_candidate", _spy_runner)

    store = _FakeStore()
    brief = GenerateBrief(
        intent="trend-following on BTC and ETH with a defensive rotation into treasuries when volatility spikes",
        risk_appetite="moderate",
        asset_classes=None,  # the client sent nothing explicit — only the LLM classified it
    )
    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_892_001", "0x892")),
    ):
        await gp.run_generation(job_id="job_892_001", brief=brief, n_candidates=1, store=store)

    # The SSE event still reports the validator's classification (unchanged UX).
    bv = next(e for e in store.events if e["event"] == "brief_validated")
    assert set(bv["data"]["asset_classes"]) == {"crypto", "treasuries"}

    # THE BUG: every dispatched candidate must actually see the classified
    # classes on brief.asset_classes, not just the discarded SSE payload.
    assert seen_asset_classes, "fixture runner was never invoked — test setup is wrong"
    for classes in seen_asset_classes:
        assert set(classes) == {"crypto", "treasuries"}, (
            f"brief.asset_classes at dispatch time was {classes!r} — the validator's "
            "asset_classes_inferred was not folded back into the brief"
        )


@pytest.mark.asyncio
async def test_validated_asset_classes_union_with_explicit_user_picks(monkeypatch):
    """#892: an explicit user ticker pick must survive the merge, not be
    replaced by the validator's coarser class inference."""
    from archimedes.services import generation_pipeline as gp

    monkeypatch.setattr(gp, "_llm_available", lambda: True)

    async def fake_validate(brief):
        return {
            "is_valid": True,
            "intent_summary": brief.intent[:140],
            "asset_classes_inferred": ["treasuries"],
            "time_horizon_inferred": "unknown",
            "risk_appetite_adjusted": brief.risk_appetite,
        }

    monkeypatch.setattr(gp, "_validate_brief", fake_validate)

    import archimedes.agents.debate_engine as de

    monkeypatch.setattr(de, "_debate_can_run", lambda brief: False)
    monkeypatch.setenv("GENERATION_PIPELINE_FIXTURE", "1")

    seen_asset_classes: list[list[str] | None] = []
    real_runner = gp._run_fixture_candidate

    async def _spy_runner(*, brief, **kwargs):
        seen_asset_classes.append(list(brief.asset_classes or []))
        return await real_runner(brief=brief, **kwargs)

    monkeypatch.setattr(gp, "_run_fixture_candidate", _spy_runner)

    store = _FakeStore()
    brief = GenerateBrief(
        intent="BTC momentum with a treasury hedge",
        risk_appetite="moderate",
        asset_classes=["BTC"],  # explicit user pick — must not be dropped
    )
    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_892_002", "0x892b")),
    ):
        await gp.run_generation(job_id="job_892_002", brief=brief, n_candidates=1, store=store)

    assert seen_asset_classes
    for classes in seen_asset_classes:
        assert "BTC" in classes, "explicit user pick must survive the validator merge"
        assert "treasuries" in classes, "validator's inferred class must be folded in alongside the user pick"


@pytest.mark.asyncio
async def test_validated_asset_classes_non_list_is_not_exploded_into_characters(monkeypatch):
    """Copilot review comment on PR #1033: `_validate_brief`'s output is
    LLM-generated and not type-enforced, so `asset_classes_inferred` could
    come back as a bare string instead of a list. Iterating a string
    character-by-character would merge garbage single-letter "classes" into
    brief.asset_classes; the non-list case must be treated as one whole class
    name instead.
    """
    from archimedes.services import generation_pipeline as gp

    monkeypatch.setattr(gp, "_llm_available", lambda: True)

    async def fake_validate(brief):
        return {
            "is_valid": True,
            "intent_summary": brief.intent[:140],
            "asset_classes_inferred": "treasuries",  # malformed: string, not list
            "time_horizon_inferred": "unknown",
            "risk_appetite_adjusted": brief.risk_appetite,
        }

    monkeypatch.setattr(gp, "_validate_brief", fake_validate)

    import archimedes.agents.debate_engine as de

    monkeypatch.setattr(de, "_debate_can_run", lambda brief: False)
    monkeypatch.setenv("GENERATION_PIPELINE_FIXTURE", "1")

    seen_asset_classes: list[list[str] | None] = []
    real_runner = gp._run_fixture_candidate

    async def _spy_runner(*, brief, **kwargs):
        seen_asset_classes.append(list(brief.asset_classes or []))
        return await real_runner(brief=brief, **kwargs)

    monkeypatch.setattr(gp, "_run_fixture_candidate", _spy_runner)

    store = _FakeStore()
    brief = GenerateBrief(
        intent="defensive rotation into treasuries",
        risk_appetite="moderate",
        asset_classes=None,
    )
    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_892_003", "0x892c")),
    ):
        await gp.run_generation(job_id="job_892_003", brief=brief, n_candidates=1, store=store)

    assert seen_asset_classes
    for classes in seen_asset_classes:
        assert classes == ["treasuries"], f"non-list validator output was exploded into: {classes!r}"


# ── Dual bull/bear regime tests (Issue #163) ───────────────────────────────


@pytest.mark.asyncio
async def test_dual_regime_always_emits_both_bull_and_bear():
    """dual_regime=True (default) emits both bull and bear candidates."""
    store = _FakeStore()
    brief = GenerateBrief(intent="trend following with low drawdown", risk_appetite="moderate")

    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_dual_001", "0xdual")),
    ):
        await run_generation(job_id="job_dual_001", brief=brief, store=store)

    drafted = [e for e in store.events if e["event"] == "candidate_drafted"]
    assert len(drafted) == 2
    regimes = [e["data"]["regime"] for e in drafted]
    assert "bull" in regimes
    assert "bear" in regimes


@pytest.mark.asyncio
async def test_dual_regime_k1_only_winner_persisted():
    """K=1 persistence (Phase-3 contract): fixture dual-regime run yields 2
    candidates in the result payload (bull+bear regimes intact) but exactly
    ONE _persist_candidate call (the winner) and ONE persist_proposal call
    for the reject (verdict='rejected').

    Replaces test_dual_regime_persists_both_with_correct_regime_tags which
    tested the retired K=2 pattern (both regimes persisted as StrategyRecords).
    """
    import archimedes.services.strategy_memory as sm

    store = _FakeStore()
    brief = GenerateBrief(intent="balanced equities", risk_appetite="conservative")

    persist_calls: list[dict] = []

    async def mock_persist(c, b, **_kw):
        persist_calls.append({"regime": c.regime, "candidate_id": c.candidate_id})
        return (f"strat_{c.regime}", f"0x{c.regime}")

    proposal_calls: list[dict] = []

    def mock_persist_proposal(**kwargs):
        proposal_calls.append(kwargs)

    with (
        patch("archimedes.agents.generation_pipeline._persist_candidate", new=mock_persist),
        patch.object(sm, "persist_proposal", side_effect=mock_persist_proposal),
    ):
        await run_generation(job_id="job_k1_persist", brief=brief, store=store)

    # K=1: exactly ONE _persist_candidate call (the winner only)
    assert len(persist_calls) == 1, f"K=1 must persist exactly 1 winner; got {len(persist_calls)} calls"

    # The rejected candidate lands in strategy_proposals with verdict='rejected'
    rejected = [c for c in proposal_calls if c.get("verdict") == "rejected"]
    assert len(rejected) == 1, f"Expected 1 rejected persist_proposal; got {rejected}"
    assert rejected[0]["regime_tag"] in ("bull", "bear"), "reject must carry a regime_tag"

    # Both candidates still surface in the result payload (stream visibility preserved)
    last_result = store.status[-1][1]
    assert last_result is not None
    candidates = last_result["candidates"]
    assert len(candidates) == 2
    regimes = {c["regime"] for c in candidates}
    assert regimes == {"bull", "bear"}


@pytest.mark.asyncio
async def test_dual_regime_fixture_names_differ():
    """Fixture bull/bear candidates have distinct names and weights."""
    store = _FakeStore()
    brief = GenerateBrief(intent="growth focus", risk_appetite="aggressive")

    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_names_001", "0xnames")),
    ):
        await run_generation(job_id="job_names", brief=brief, store=store)

    drafted = [e for e in store.events if e["event"] == "candidate_drafted"]
    names = [e["data"]["strategy_name"] for e in drafted]
    # Names must be different (bull vs bear)
    assert len(set(names)) == 2
    # One should contain "Bull", the other "Bear"
    combined = " ".join(names)
    assert "Bull" in combined
    assert "Bear" in combined


@pytest.mark.asyncio
async def test_dual_regime_off_produces_single_neutral():
    """dual_regime=False falls back to single neutral candidate."""
    store = _FakeStore()
    brief = GenerateBrief(intent="basic equities", risk_appetite="moderate")

    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_single_001", "0xsingle")),
    ):
        await run_generation(job_id="job_single", brief=brief, store=store, dual_regime=False)

    drafted = [e for e in store.events if e["event"] == "candidate_drafted"]
    assert len(drafted) == 1
    assert drafted[0]["data"]["regime"] == "neutral"


@pytest.mark.asyncio
async def test_dual_regime_pipeline_selected_event_lists_regimes():
    """pipeline_selected event includes the regime plan."""
    store = _FakeStore()
    brief = GenerateBrief(intent="macro hedge", risk_appetite="conservative")

    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_regime_evt", "0xevt")),
    ):
        await run_generation(job_id="job_regime_evt", brief=brief, store=store)

    ps = next(e for e in store.events if e["event"] == "pipeline_selected")
    assert ps["data"]["regimes"] == ["bull", "bear"]


@pytest.mark.asyncio
async def test_pipeline_multi_candidate_picks_best():
    store = _FakeStore()
    brief = GenerateBrief(intent="aggressive crypto", risk_appetite="aggressive")

    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_multi_001", "0xfeed")),
    ):
        await run_generation(job_id="job_multi", brief=brief, n_candidates=3, store=store, dual_regime=False)

    # dual_regime=False + n_candidates=3 → 3 neutral candidates
    drafted = [e for e in store.events if e["event"] == "candidate_drafted"]
    assert len(drafted) == 3
    best = next((e for e in store.events if e["event"] == "best_selected"), None)
    assert best is not None
    assert best["data"]["considered_count"] == 3


@pytest.mark.asyncio
async def test_best_selected_abstains_when_no_candidate_passes_rigor():
    # ABSTAIN semantics (#818): when NO candidate clears the gate, best_selected must
    # honestly carry validated_count=0 and deployable=False — the surfaced best is a
    # *considered* alternative, not a validated, deployable winner. Pins the new fields
    # so the ABSTAIN-vs-deployable distinction can't regress silently.
    store = _FakeStore()
    brief = GenerateBrief(intent="aggressive crypto", risk_appetite="aggressive")

    def _fail_all(cands):
        # Stand in for _patch_pbo: force every candidate to fail the gate.
        for c in cands:
            c.rigor_verdict["passing"] = False

    with (
        patch("archimedes.agents.generation_pipeline._patch_pbo", side_effect=_fail_all),
        patch(
            "archimedes.agents.generation_pipeline._persist_candidate",
            new=AsyncMock(return_value=("strat_abstain_001", "0xfeed")),
        ),
    ):
        await run_generation(job_id="job_abstain", brief=brief, n_candidates=3, store=store, dual_regime=False)

    best = next((e for e in store.events if e["event"] == "best_selected"), None)
    assert best is not None
    assert best["data"]["considered_count"] == 3
    assert best["data"]["validated_count"] == 0
    assert best["data"]["deployable"] is False


# ── Debate dispatch wire — hermetic end-to-end ─────────────────────────────
#
# Re-targets the old "fusion dispatch wire" tests at the debate path. Instead
# of the retired standalone-fusion dispatch, we stub the debate engine's
# proposer (_propose_pool) and evaluator (evaluate_fusion_spec) seams so
# _run_debate_leaderboard runs hermetically through run_generation — no live
# LLM, no real market-data backtest, no network. Pattern follows the
# _CannedFusionBackend + monkeypatch seams in test_debate_engine.py.


# ── Shared helpers for debate dispatch tests ──────────────────────────────────


def _debate_fake_corpus(n: int = 4):
    """A small fixture corpus whose arxiv_ids match the fake proposals' citations.

    Kept small (n=4) since we're mocking _propose_pool anyway and just need
    _critic_prov to pass provenance checks.
    """
    from archimedes.agents.strategy_fusion import CorpusPaper

    return [
        CorpusPaper(
            arxiv_id=f"240{r}.0000{i}",
            title=f"Paper {r}-{i}",
            abstract="Momentum trend-following equities volatility hedge.",
            primary_category="q-fin.PM",
            categories=("q-fin.PM",),
            published="2024-01-01",
        )
        for r in (1, 2)
        for i in range(1, (n // 2) + 1)
    ]


# Conformant DSL spec — passes _dsl_conformance_ok and evaluate_fusion_spec.
_DEBATE_SPEC = {
    "name": "Debate canned SMA",
    "asset_universe": ["SPY"],
    "rebalance_frequency": "monthly",
    "entry": {"gt": ["sma_50", 0]},
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
}

#: A real look-ahead audit in its clean state — see test_debate_engine.
_CLEAN_LOOK_AHEAD = DslLookAheadAudit(
    status=LOOK_AHEAD_PASS,
    interpreter_verified=True,
    broker_cheat_check=True,
)


def _make_fake_proposal(name: str, ids: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        strategy_name=name,
        thesis=f"Thesis for {name}.",
        source_arxiv_ids=ids,
        strategy_spec=dict(_DEBATE_SPEC, name=name),
        fusion_reasoning="canned reasoning",
        novelty_rationale="canned novelty",
        is_actionable=True,
    )


def _fake_eval_result(*, num_trials: int | None = None) -> SimpleNamespace:
    """Canned FusionEvalResult that passes all rigor gates + C-null."""
    rigor = SimpleNamespace(
        dsr=1.5,
        dsr_p_value=0.01,
        pbo_score=0.1,
        oos_sharpe=1.2,
        in_sample_sharpe=1.3,
        # See test_debate_engine._fake_ev: mirrors a verdict that cleared the
        # real structural look-ahead audit, with the fields read off an actual
        # DslLookAheadAudit so the double cannot drift from the real vocabulary.
        look_ahead_clean=_CLEAN_LOOK_AHEAD.passed,
        look_ahead_status=_CLEAN_LOOK_AHEAD.status,
        look_ahead_label=_CLEAN_LOOK_AHEAD.label,
        look_ahead_reason=_CLEAN_LOOK_AHEAD.reason,
        num_trials=num_trials,
        passing=True,
        data_source="synthetic",
        admissible=False,
    )
    bt = SimpleNamespace(
        sharpe_ratio=1.4,
        sortino_ratio=1.6,
        max_drawdown=-0.1,
        cagr=0.2,  # > MIN_COST_BENEFIT (0.05%) → clears C-null
        calmar_ratio=1.1,
        win_rate=0.55,
        total_trades=42,
        equity_curve=None,  # no return series needed for hermetic tests
    )
    return SimpleNamespace(rigor=rigor, backtest=bt, success=True, admissible=False, error=None, spec={})


def _fake_evidence_by_id(proposals):
    """The `{arxiv_id: {"title", "published"}}` map `_propose_pool` returns
    alongside the pool (#1636) — the debate's attribution whitelist.

    A double for `_propose_pool` must stub its FULL surface, not just the half
    the branch under test reads (CLAUDE.md § "a boundary mock must stub the
    shared function's full surface"). Derived from what the fake proposals
    cite, so it is a plausible surface rather than an empty dict that would
    make every debate claim read as unattributed.
    """
    return {
        arxiv_id: {"title": f"Fixture paper {arxiv_id}", "published": "2024-01-01"}
        for prop in proposals
        for arxiv_id in (getattr(prop, "source_arxiv_ids", None) or [])
    }


def _setup_debate_hermetic(monkeypatch, *, gp, de, sf, fe, fake_proposals, fake_corpus):
    """Apply the standard debate-path hermetic stubs.

    Monkeypatches:
    - gp._llm_available → True (live path)
    - de._debate_can_run → True (bypass flag + corpus size check)
    - de._propose_pool → returns (fake_proposals, evidence_by_id) (no LLM call)
    - sf.load_corpus → returns fake_corpus (no DB/disk read)
    - fe.evaluate_fusion_spec → returns _fake_eval_result (no backtest)
    - de._debate_round → no-op (best-effort transcript, never gates)
    """
    monkeypatch.setattr(gp, "_llm_available", lambda: True)
    monkeypatch.setattr(de, "_debate_can_run", lambda brief: True)

    async def _fake_pool(*a, **k):
        return list(fake_proposals), _fake_evidence_by_id(fake_proposals)

    monkeypatch.setattr(de, "_propose_pool", _fake_pool)
    monkeypatch.setattr(sf, "load_corpus", lambda *a, **k: list(fake_corpus))

    def _fake_eval(spec, *, num_trials=None, use_real_data=False, **kw):
        return _fake_eval_result(num_trials=num_trials)

    monkeypatch.setattr(fe, "evaluate_fusion_spec", _fake_eval)

    async def _noop_debate_round(*a, **k):
        return []

    monkeypatch.setattr(de, "_debate_round", _noop_debate_round)


def _permissive_validate(brief):
    """Permissive brief validator (no LLM call)."""

    async def _inner(brief):
        return {
            "is_valid": True,
            "intent_summary": brief.intent[:140],
            "asset_classes_inferred": brief.asset_classes or [],
            "time_horizon_inferred": "unknown",
            "risk_appetite_adjusted": brief.risk_appetite,
        }

    return _inner(brief)


# ── Test 3 — debate dispatch end-to-end persists a debate strategy ────────────


@pytest.mark.asyncio
async def test_debate_dispatch_end_to_end_persists_debate_strategy(tmp_path, monkeypatch):
    """Debate dispatch drives run_generation → a StrategyRecord with
    generation_method='debate' is persisted.

    Re-targets test_fusion_dispatch_end_to_end_persists_fusion_strategy which
    tested the retired standalone-fusion dispatch. Hermetic: tmp SQLite, mocked
    proposer + evaluator — no network, no LLM.

    Asserts: (a) pipeline_selected=debate, (b) candidate_drafted + persisted
    emitted, (c) StrategyRecord.generation_method == 'debate'.
    """
    import archimedes.agents.debate_engine as de
    import archimedes.db as _db
    import archimedes.services.fusion_evaluator as fe
    from archimedes.agents import generation_pipeline as gp
    from archimedes.agents import strategy_fusion as sf
    from archimedes.models.strategy_store import StrategyRecord
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'debate_e2e.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(_db, "engine", test_engine)
    monkeypatch.setattr(_db, "SessionLocal", sessionmaker(bind=test_engine, autocommit=False, autoflush=False))
    from archimedes.models import kg, strategy_passport_record  # noqa: F401

    _db.Base.metadata.create_all(bind=test_engine)

    monkeypatch.delenv("GENERATION_PIPELINE_FIXTURE", raising=False)

    async def _pv(brief):
        return await _permissive_validate(brief)

    monkeypatch.setattr(gp, "_validate_brief", _pv)

    fake_corpus = _debate_fake_corpus(4)
    fake_proposals = [
        _make_fake_proposal("Debate A", ["2401.00001", "2402.00001"]),
        _make_fake_proposal("Debate B", ["2401.00002", "2402.00002"]),
    ]
    _setup_debate_hermetic(
        monkeypatch, gp=gp, de=de, sf=sf, fe=fe, fake_proposals=fake_proposals, fake_corpus=fake_corpus
    )

    store = _FakeStore()
    brief = GenerateBrief(
        intent="equity momentum with a volatility overlay",
        risk_appetite="moderate",
        asset_classes=["equities"],
    )
    await run_generation(job_id="job_debate_e2e", brief=brief, store=store, dual_regime=False, n_candidates=1)

    # pipeline_selected announced debate
    ps = [e for e in store.events if e["event"] == "pipeline_selected"]
    assert ps, "no pipeline_selected event emitted"
    assert ps[0]["data"]["pipeline"] == "debate", (
        f"debate not dispatched; got {ps[0]['data']['pipeline']!r} (reason={ps[0]['data'].get('reason')!r})"
    )

    # Required streaming events present
    names = [e["event"] for e in store.events]
    for required in ("candidate_drafted", "candidate_evaluated", "persisted", "done"):
        assert required in names, f"debate run did not emit {required!r} (events={names})"

    # Persisted as a debate strategy (generation_method='debate')
    with _db.get_session() as session:
        record = session.query(StrategyRecord).filter(StrategyRecord.generation_method == "debate").first()
    assert record is not None, "no debate StrategyRecord was persisted"
    assert record.generation_method == "debate"

    # Rigor verdict shape present and has required fields
    assert record.rigor_verdict is not None, "debate strategy persisted without a rigor verdict"
    verdict = json.loads(record.rigor_verdict)
    for key in ("dsr", "pbo", "oos_sharpe", "passing"):
        assert key in verdict, f"rigor verdict missing {key!r}: {verdict}"
    assert isinstance(verdict["passing"], bool)


# ── Test 3b — user's Generate-page model pick reaches the live LLM call sites
# (issue #1049: selection must thread end-to-end, not just to the API boundary)


@pytest.mark.asyncio
async def test_run_generation_threads_user_model_to_debate_proposer_and_agent(tmp_path, monkeypatch):
    """``run_generation(model=...)`` on the LIVE debate dispatch path must reach:

      (a) the debate proposer (``_propose_pool`` → ``StrategyFusion(model=...)``
          → ``make_llm_backend(model=...)``) — the seam that actually drafts the
          candidate text, and
      (b) the per-job ``PortfolioAgent``'s backend, built via
          ``make_llm_backend(model=...)`` — the seam ``_served_model_for`` reads
          for the ``done`` event's ``served_model`` provenance.

    Route-level tests (test_generate_model_gating.py) stop at the enqueue
    boundary (the pipeline task is mocked out entirely); the debate-engine unit
    test (test_debate_engine.py::test_model_pick_threads_into_served_model)
    covers only the ``StrategyFusion`` layer in isolation. Neither proves the
    full chain from ``run_generation`` — what the job queue actually invokes —
    down to the LLM-backend constructors. This closes that gap: a caller that
    picked an allowlisted free-tier model must get THAT model, not a silent
    fallback to the env default, anywhere along the live path.
    """
    import archimedes.agents.debate_engine as de
    import archimedes.db as _db
    import archimedes.services.fusion_evaluator as fe
    import archimedes.services.llm_backend as llm_backend_mod
    from archimedes.agents import generation_pipeline as gp
    from archimedes.agents import strategy_fusion as sf
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'model_select_e2e.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(_db, "engine", test_engine)
    monkeypatch.setattr(_db, "SessionLocal", sessionmaker(bind=test_engine, autocommit=False, autoflush=False))
    from archimedes.models import kg, strategy_passport_record  # noqa: F401

    _db.Base.metadata.create_all(bind=test_engine)

    monkeypatch.delenv("GENERATION_PIPELINE_FIXTURE", raising=False)

    async def _pv(brief):
        return await _permissive_validate(brief)

    monkeypatch.setattr(gp, "_validate_brief", _pv)

    picked_model = "zai.glm-4.7-flash"  # an allowlisted free-tier id, never the env default

    # Spy on _propose_pool (the debate proposer's entry point) instead of the
    # blanket fake used by _setup_debate_hermetic — capture the `model` it
    # actually receives.
    proposer_calls: list[str | None] = []
    fake_proposals = [
        _make_fake_proposal("Debate A", ["2401.00001", "2402.00001"]),
        _make_fake_proposal("Debate B", ["2401.00002", "2402.00002"]),
    ]

    async def _spy_pool(brief, model, corpus):
        proposer_calls.append(model)
        return list(fake_proposals), _fake_evidence_by_id(fake_proposals)

    monkeypatch.setattr(gp, "_llm_available", lambda: True)
    monkeypatch.setattr(de, "_debate_can_run", lambda brief: True)
    monkeypatch.setattr(de, "_propose_pool", _spy_pool)
    monkeypatch.setattr(sf, "load_corpus", lambda *a, **k: _debate_fake_corpus(4))

    def _fake_eval(spec, *, num_trials=None, use_real_data=False, **kw):
        return _fake_eval_result(num_trials=num_trials)

    monkeypatch.setattr(fe, "evaluate_fusion_spec", _fake_eval)

    async def _noop_debate_round(*a, **k):
        return []

    monkeypatch.setattr(de, "_debate_round", _noop_debate_round)

    # Spy on make_llm_backend (the per-job PortfolioAgent construction reads
    # this module attribute via a local import inside run_generation, so patch
    # it where it's DEFINED — the same seam the live bedrock_converse path uses).
    # Returns a tiny fake backend rather than delegating to the real factory —
    # this test has no AWS credentials to exercise a live BedrockConverseBackend
    # with, and a credential-less real call would correctly degrade to
    # CannedBackend (served_model == "canned-fallback" always, by design — see
    # CannedBackend's docstring), which would make assertion (c) below test
    # nothing. The fake isolates what we actually want to verify here: that the
    # WIRING passes the user's model through, independent of live credentials.
    backend_calls: list[str | None] = []

    class _FakeAgentBackend:
        def __init__(self, model):
            self.model_id = model
            self.served_model = model
            self.available = True

        def complete(self, system, user):  # pragma: no cover — debate runner ignores `agent`
            raise AssertionError("PortfolioAgent.complete should not be called by the debate runner")

    def _spy_make_llm_backend(model=None, **kw):
        backend_calls.append(model)
        return _FakeAgentBackend(model)

    monkeypatch.setattr(llm_backend_mod, "make_llm_backend", _spy_make_llm_backend)

    store = _FakeStore()
    brief = GenerateBrief(
        intent="equity momentum with a volatility overlay",
        risk_appetite="moderate",
        asset_classes=["equities"],
    )
    await run_generation(
        job_id="job_model_select_e2e",
        brief=brief,
        store=store,
        dual_regime=False,
        n_candidates=1,
        model=picked_model,
    )

    # (a) the debate proposer received the user's exact pick, not the env default.
    assert proposer_calls == [picked_model], (
        f"expected _propose_pool called with {picked_model!r}, got {proposer_calls}"
    )

    # (b) the per-job PortfolioAgent's backend was built with the same pick.
    assert picked_model in backend_calls, (
        f"expected make_llm_backend(model={picked_model!r}) among the per-job agent's calls; got {backend_calls}"
    )

    # (c) provenance surfaced on the `done` event reflects the user's pick, not
    # "fixture" and not silently the env default — the whole point of #1049.
    done_events = [e for e in store.events if e["event"] == "done"]
    assert done_events, "no done event emitted"
    assert done_events[-1]["data"]["served_model"] == picked_model, (
        f"done event served_model={done_events[-1]['data']['served_model']!r}, expected {picked_model!r}"
    )


# ── Test 4 — debate C-rigor threads _society_num_trials(library, pool) ────────


@pytest.mark.asyncio
async def test_debate_critic_rigor_num_trials_matches_society_formula(tmp_path, monkeypatch):
    """Debate C-rigor passes num_trials = _society_num_trials(library_size, pool_size)
    to evaluate_fusion_spec — the same formula the live agent path uses.

    Re-targets test_fusion_path_num_trials_matches_society_formula.  pool_size is
    the society's own internal count of conformant proposals (the real selection
    set), not n_candidates. The spy asserts evaluate_fusion_spec receives the right
    value across the full proposal pool.
    """
    import archimedes.agents.debate_engine as de
    import archimedes.db as _db
    import archimedes.services.fusion_evaluator as fe
    from archimedes.agents import generation_pipeline as gp
    from archimedes.agents import strategy_fusion as sf
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'debate_num_trials.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(_db, "engine", test_engine)
    monkeypatch.setattr(_db, "SessionLocal", sessionmaker(bind=test_engine, autocommit=False, autoflush=False))
    from archimedes.models import kg, strategy_passport_record  # noqa: F401

    _db.Base.metadata.create_all(bind=test_engine)

    monkeypatch.delenv("GENERATION_PIPELINE_FIXTURE", raising=False)

    async def _pv(brief):
        return await _permissive_validate(brief)

    monkeypatch.setattr(gp, "_validate_brief", _pv)

    # library_size only sizes the fake provider's list — decouple #2 means it must
    # NOT affect num_trials (which is the strategy's own pool_size).
    library_size = 4

    class _FakeStrategy:
        paper_arxiv_id = "1234.5678"
        paper_title = "Fake Paper"

    class _FakeProvider:
        def list_strategies(self, *a, **k):
            return [_FakeStrategy() for _ in range(library_size)]

    import archimedes.services.strategy_provider as sp

    monkeypatch.setattr(sp, "default_provider", lambda *a, **k: _FakeProvider())

    # The pool has 3 proposals → pool_size = 3 inside _run_debate_leaderboard.
    pool_size = 3
    fake_corpus = _debate_fake_corpus(pool_size * 2)
    fake_proposals = [
        _make_fake_proposal(f"Debate {i}", [f"2401.0000{i}", f"2402.0000{i}"]) for i in range(1, pool_size + 1)
    ]

    # Spy on evaluate_fusion_spec — capture num_trials for every call.
    captured_num_trials: list[int | None] = []

    def _spy_evaluate(spec, *, num_trials=None, use_real_data=False, **kw):
        captured_num_trials.append(num_trials)
        return _fake_eval_result(num_trials=num_trials)

    # Setup hermetic stubs but override evaluate_fusion_spec with the spy.
    monkeypatch.setattr(gp, "_llm_available", lambda: True)
    monkeypatch.setattr(de, "_debate_can_run", lambda brief: True)

    async def _fake_pool(*a, **k):
        return list(fake_proposals), _fake_evidence_by_id(fake_proposals)

    monkeypatch.setattr(de, "_propose_pool", _fake_pool)
    monkeypatch.setattr(sf, "load_corpus", lambda *a, **k: list(fake_corpus))
    monkeypatch.setattr(fe, "evaluate_fusion_spec", _spy_evaluate)

    async def _noop_debate_round(*a, **k):
        return []

    monkeypatch.setattr(de, "_debate_round", _noop_debate_round)

    store = _FakeStore()
    brief = GenerateBrief(
        intent="equity momentum with volatility overlay",
        risk_appetite="moderate",
        asset_classes=["equities"],
    )
    await run_generation(
        job_id="job_debate_num_trials",
        brief=brief,
        store=store,
        dual_regime=False,
        n_candidates=1,
    )

    assert captured_num_trials, "evaluate_fusion_spec was never called — debate C-rigor did not run"
    expected = gp._society_num_trials(pool_size)  # own pool only — NOT library+pool
    assert all(n == expected for n in captured_num_trials), (
        f"debate num_trials {captured_num_trials} != formula {expected} "
        f"(library_size={library_size}, pool_size={pool_size})"
    )

    # candidate_evaluated event carries the verdict; num_trials should match.
    evaluated = [e for e in store.events if e["event"] == "candidate_evaluated"]
    assert evaluated, "no candidate_evaluated event emitted"
    for e in evaluated:
        verdict = e["data"].get("rigor_verdict") or {}
        if verdict.get("num_trials") is not None:
            assert verdict["num_trials"] == expected, (
                f"candidate_evaluated verdict num_trials={verdict['num_trials']} != {expected}"
            )


# ── Test 5 — debate winner with weights={} never causes spurious backtest_failed ─


@pytest.mark.asyncio
async def test_debate_text_only_pool_no_spurious_backtest_failed(tmp_path, monkeypatch):
    """Regression guard (debate-path re-target of #784 live bug).

    A debate winner has weights={} by design (it emits a DSL spec scored by
    C-rigor, not a static weight vector). It must NOT be routed into
    _backtest_and_persist (the static buy-and-hold backtester), which would
    emit the misleading backtest_failed("no weights emitted by agent").

    Protection: generation_method='debate' is in _static_skip. Crucially, the
    test does NOT set GENERATION_PIPELINE_SKIP_BACKTEST — the bug only surfaces
    when _backtest_and_persist is actually reachable.

    Re-targets test_fusion_text_only_does_not_emit_spurious_backtest_failed which
    tested the same _static_skip guard on the retired standalone-fusion dispatch.
    """
    import archimedes.agents.debate_engine as de
    import archimedes.db as _db
    import archimedes.services.fusion_evaluator as fe
    from archimedes.agents import generation_pipeline as gp
    from archimedes.agents import strategy_fusion as sf
    from archimedes.models.strategy_store import StrategyRecord
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'debate_textonly.db'}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(_db, "engine", test_engine)
    monkeypatch.setattr(_db, "SessionLocal", sessionmaker(bind=test_engine, autocommit=False, autoflush=False))
    from archimedes.models import kg, strategy_passport_record  # noqa: F401

    _db.Base.metadata.create_all(bind=test_engine)

    monkeypatch.delenv("GENERATION_PIPELINE_FIXTURE", raising=False)
    # Intentionally NOT setting GENERATION_PIPELINE_SKIP_BACKTEST — the bug only
    # manifests when _backtest_and_persist is reachable (SKIP_BACKTEST masks it).

    async def _pv(brief):
        return await _permissive_validate(brief)

    monkeypatch.setattr(gp, "_validate_brief", _pv)

    fake_corpus = _debate_fake_corpus(4)
    fake_proposals = [
        _make_fake_proposal("Debate Winner", ["2401.00001", "2402.00001"]),
    ]
    _setup_debate_hermetic(
        monkeypatch, gp=gp, de=de, sf=sf, fe=fe, fake_proposals=fake_proposals, fake_corpus=fake_corpus
    )

    store = _FakeStore()
    brief = GenerateBrief(
        intent="equity momentum with volatility overlay",
        risk_appetite="moderate",
        asset_classes=["equities"],
    )
    await run_generation(job_id="job_debate_textonly", brief=brief, store=store, dual_regime=False, n_candidates=1)

    # Debate was dispatched.
    ps = [e for e in store.events if e["event"] == "pipeline_selected"]
    assert ps and ps[0]["data"]["pipeline"] == "debate", f"debate not dispatched: {ps}"

    # Run completed cleanly.
    names = [e["event"] for e in store.events]
    for required in ("candidate_drafted", "persisted", "done"):
        assert required in names, f"debate run did not emit {required!r} (events={names})"

    # THE FIX: no spurious "no weights" backtest_failed for the debate winner.
    backtest_failed = [e for e in store.events if e["event"] == "backtest_failed"]
    spurious = [e for e in backtest_failed if "no weights" in (e["data"].get("error", "") or "")]
    assert not spurious, f"debate winner (weights={{}}) was wrongly routed into the static backtester: {spurious}"

    # Persisted as a debate strategy.
    with _db.get_session() as session:
        record = session.query(StrategyRecord).filter(StrategyRecord.generation_method == "debate").first()
    assert record is not None, "no debate StrategyRecord was persisted"
    assert record.generation_method == "debate"


# ── Test 6 — no-LLM dispatch: fixture path or GENERATION_UNAVAILABLE ──────────


@pytest.mark.asyncio
async def test_no_llm_testing_mode_selects_fixture_pipeline():
    """Under TESTING with no LLM, dispatch selects the 'fixture' pipeline.

    The autouse fixture already sets GENERATION_PIPELINE_FIXTURE=1, which forces
    _llm_available() → False AND sets fixture_mode=True. Asserts pipeline_selected
    says 'fixture' and the run completes cleanly.

    Replaces test_fusion_falls_back_to_agent_when_no_llm (which tested the
    retired agent-fallback relabeling; fixture/GENERATION_UNAVAILABLE is now the
    honest contract).
    """
    store = _FakeStore()
    brief = GenerateBrief(intent="balanced macro", risk_appetite="moderate")
    # autouse fixture forces GENERATION_PIPELINE_FIXTURE=1 → fixture path.
    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_fixture_nollm", "0xfx")),
    ):
        await run_generation(job_id="job_nollm_testing", brief=brief, store=store, dual_regime=False, n_candidates=1)

    ps = next(e for e in store.events if e["event"] == "pipeline_selected")
    assert ps["data"]["pipeline"] == "fixture", (
        f"no-LLM+fixture-env must select 'fixture', got {ps['data']['pipeline']!r}"
    )
    names = [e["event"] for e in store.events]
    assert "candidate_drafted" in names
    assert names[-1] == "done"


@pytest.mark.asyncio
async def test_no_llm_prod_shape_emits_generation_unavailable(monkeypatch):
    """Prod shape (no TESTING, no fixture env, no LLM) → GENERATION_UNAVAILABLE.

    With the retired agent-fallback removed, an LLM-less prod environment emits
    an honest GENERATION_UNAVAILABLE error code and sets status to 'error'.
    Completes the new no-LLM contract (both cases of TASK 1 test 6).
    """
    from archimedes.agents import generation_pipeline as gp

    monkeypatch.delenv("GENERATION_PIPELINE_FIXTURE", raising=False)
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setattr(gp, "_llm_available", lambda: False)

    store = _FakeStore()
    brief = GenerateBrief(intent="balanced macro", risk_appetite="moderate")
    await gp.run_generation(job_id="job_nollm_prod", brief=brief, store=store, dual_regime=False, n_candidates=1)

    err = next((e for e in store.events if e["event"] == "error"), None)
    assert err is not None, "prod no-LLM must emit an error event"
    assert err["data"]["code"] == "GENERATION_UNAVAILABLE", (
        f"expected GENERATION_UNAVAILABLE, got {err['data']['code']!r}"
    )
    statuses = [s[0] for s in store.status]
    assert "error" in statuses


@pytest.mark.asyncio
async def test_papers_claim_lives_on_candidate_drafted_not_candidates_selected():
    """The stream's papers claim must come from each candidate's ACTUAL
    citations, never the depth knob (task #54, claims-must-be-true):

    - ``candidates_selected`` fires BEFORE any retrieval, so it must carry NO
      papers field at all — the removed field sliced the curated library to
      ``max_papers``, a constant count from the wrong population;
    - every ``candidate_drafted`` carries ``source_arxiv_ids`` — the accepted
      proposal's own citations (provenance-checked by the debate critics on
      the live path; the fixture path threads its fixture citations through
      the same emit).
    """
    store = _FakeStore()
    brief = GenerateBrief(intent="13-week treasury alternative", risk_appetite="conservative")

    with patch(
        "archimedes.agents.generation_pipeline._persist_candidate",
        new=AsyncMock(return_value=("strat_fixture_002", "0xdeadbeef")),
    ):
        await run_generation(job_id="job_papers_001", brief=brief, n_candidates=1, store=store)

    selected = [e for e in store.events if e["event"] == "candidates_selected"]
    assert len(selected) == 1
    assert "source_arxiv_ids" not in selected[0]["data"], (
        "candidates_selected must not claim papers — no retrieval has happened yet"
    )

    drafted = [e for e in store.events if e["event"] == "candidate_drafted"]
    assert drafted
    for e in drafted:
        assert "source_arxiv_ids" in e["data"], "each drafted candidate must carry its own citations"
        assert isinstance(e["data"]["source_arxiv_ids"], list)
