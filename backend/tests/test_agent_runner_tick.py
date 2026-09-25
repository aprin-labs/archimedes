"""StrategyRunner tick-pipeline coverage (#738 Tier-A + behavior c).

Target: backend/archimedes/chain/agent_runner.py
Complements test_agent_runner.py (deterministic _compute_trades / regime /
dedup) by exercising the *full tick pipeline* and the on-chain commit/reveal +
vault-discovery surface:

  behavior c — one tick (mocked chain/redis/oracle) flows
  strategies → signals → regime classification → position-scale (portfolio
  constructor) → target allocations → per-vault processing.

Plus: _get_managed_vaults, _discover_new_vaults, _commit_trace / _reveal_trace
(DRY_RUN so no on-chain), and the run() loop body.

Hermetic: every boundary (strategy provider/evaluator, oracle, regime detector,
portfolio constructor, chain executor, trace publisher, Redis AgentStateStore)
is mocked. No network, no Arc RPC, no Circle, no Redis.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from archimedes.models.portfolio import Portfolio, PortfolioHolding, TargetAllocation
from archimedes.models.regime import ConsensusLabel, EnsembleConsensus, Regime, RegimeClassification
from archimedes.services.strategy_signal_evaluator import AssetSignal, Signal, StrategySignals

# ── Builders ──────────────────────────────────────────────────


def _signals(asset: str = "sSPY", weight: float = 0.6, sig: Signal = Signal.LONG) -> StrategySignals:
    return StrategySignals(
        strategy_id="faber_001",
        strategy_name="Faber SMA200",
        paper_title="A Quantitative Approach to Tactical Asset Allocation",
        signals=[
            AssetSignal(
                strategy_id="faber_001",
                strategy_name="Faber SMA200",
                asset=asset,
                signal=sig,
                weight=weight,
                reason="Price above SMA200",
            )
        ],
        paper_arxiv_id="0001",
    )


def _allocs(**weights: float) -> list[TargetAllocation]:
    return [TargetAllocation(symbol=s, token_address="", weight=w, strategy_ids=[]) for s, w in weights.items()]


def _portfolio(total: float = 1000.0) -> Portfolio:
    return Portfolio(
        vault_address="0xVault",
        total_value_usdc=total,
        holdings=[PortfolioHolding(symbol="USDC", token_address="0xusdc", amount=total, weight=1.0, value_usdc=total)],
        risk_profile="moderate",
    )


def _regime() -> RegimeClassification:
    cls = MagicMock(spec=RegimeClassification)
    cls.regime = Regime.RISK_ON
    cls.confidence = 0.9
    cls.regime_changed = False
    cls.signals = MagicMock(vix_level=13.0)
    return cls


def _consensus() -> EnsembleConsensus:
    return EnsembleConsensus(flat_pct=0.1, signal_count=3, label=ConsensusLabel.RISK_ON)


@pytest.fixture
def runner_env(monkeypatch):
    """A StrategyRunner with every chain/redis/oracle boundary mocked.

    Yields (runner, mocks-dict). DRY_RUN is forced on so the commit/reveal path
    builds + hashes traces without touching the chain.
    """
    monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", True)
    monkeypatch.setattr("archimedes.chain.agent_runner.EXPLICIT_VAULTS", "")
    with (
        patch("archimedes.chain.agent_runner.chain_client") as mock_client,
        patch("archimedes.chain.agent_runner.chain_executor") as mock_executor,
        patch("archimedes.chain.agent_runner.trace_publisher") as mock_publisher,
        patch("archimedes.chain.agent_runner.default_provider") as mock_provider,
        patch("archimedes.chain.agent_runner.AgentStateStore") as mock_state_cls,
        patch("archimedes.chain.agent_runner.strategy_evaluator") as mock_eval,
    ):
        # Fee-cap guard (issue #1138 follow-up) default: compliant (0, 0) fees so
        # existing live-mode (DRY_RUN=False) tests that don't care about fees
        # keep reaching Phase 2 TRADE unmodified. TestFeeCapGuardBlocksTradeOnHostileVault
        # overrides this per-case to exercise the guard itself.
        mock_executor.get_vault_fee_bps = AsyncMock(return_value=(0, 0))
        mock_client.settings = MagicMock(
            synth_addresses={"sSPY": "0xsspy", "sGOLD": "0xsgold"},
            usdc_address="0xusdc",
            oracle_addresses={"sSPY": "0xoraclespy"},
        )
        mock_client.to_checksum = lambda a: a
        # Strategy provider returns a couple of strategies.
        strat = MagicMock(paper_title="Faber SMA200", id="faber_001")
        mock_provider.return_value.list_strategies.return_value = [strat]

        # Evaluator: signals + aggregate weights.
        mock_eval.evaluate_strategies.return_value = [_signals()]
        mock_eval.aggregate_signals.return_value = {"sSPY": 0.6, "USDC": 0.4}

        # Redis state store: all awaitables no-op.
        state = mock_state_cls.return_value
        state.save_regime = AsyncMock()
        state.save_ensemble_consensus = AsyncMock()
        state.save_heartbeat = AsyncMock()
        state.save_trace = AsyncMock()
        state.get_last_trace = AsyncMock(return_value=None)
        state.save_last_rebalance = AsyncMock()
        # #1276: tick step 0 scans for dangling commitments to reconcile. Empty
        # store => the pass is a no-op, so live-mode (DRY_RUN=False) cases below
        # exercise exactly the same commit/trade/reveal flow they always did.
        state.list_recent_traces = AsyncMock(return_value=[])

        from archimedes.chain.agent_runner import StrategyRunner

        runner = StrategyRunner()
        # Position-scaler (portfolio constructor) returns scaled allocations.
        runner.portfolio_constructor = MagicMock()
        runner.portfolio_constructor.construct.return_value = _allocs(sSPY=0.6, USDC=0.4)
        # Regime detector + oracle snapshot.
        runner.oracle = MagicMock()
        runner.oracle.fetch_market_snapshot = AsyncMock(return_value=MagicMock(has_regime_signals=True))
        runner.regime_detector = MagicMock()
        runner.regime_detector.classify.return_value = _regime()

        yield (
            runner,
            {
                "client": mock_client,
                "executor": mock_executor,
                "publisher": mock_publisher,
                "eval": mock_eval,
                "state": state,
            },
        )


# ── behavior c: full tick pipeline ────────────────────────────


class TestTickPipeline:
    async def test_tick_flows_signals_regime_scale_allocations(self, runner_env):
        runner, m = runner_env
        # One managed vault with an empty (USDC-only) portfolio.
        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        # No metadata → legacy/global-consensus path.
        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        # Pipeline stages all fired:
        m["eval"].evaluate_strategies.assert_called_once()
        m["eval"].aggregate_signals.assert_called()  # global aggregate
        runner.regime_detector.classify.assert_called_once()  # regime classified
        # Position-scaler (portfolio constructor) consumed regime + consensus.
        runner.portfolio_constructor.construct.assert_called()
        ckw = runner.portfolio_constructor.construct.call_args.kwargs
        assert ckw["regime"] is not None
        assert isinstance(ckw["ensemble_consensus"], EnsembleConsensus)
        assert ckw["base_weights"] == {"sSPY": 0.6, "USDC": 0.4}
        # Regime + consensus persisted to Redis.
        m["state"].save_regime.assert_awaited()
        m["state"].save_ensemble_consensus.assert_awaited()
        m["state"].save_heartbeat.assert_awaited()

    async def test_tick_no_strategies_returns_early(self, runner_env):
        runner, m = runner_env
        runner.provider.list_strategies = MagicMock(return_value=[])
        await runner.tick()
        # Bailed before evaluating signals.
        m["eval"].evaluate_strategies.assert_not_called()

    async def test_tick_no_vaults_returns_after_regime(self, runner_env):
        runner, m = runner_env
        m["executor"].get_all_vaults = AsyncMock(return_value=[])
        await runner.tick()
        # Regime still classified, but no vault processed → no portfolio reads.
        runner.regime_detector.classify.assert_called_once()
        m["executor"].read_portfolio.assert_not_called()

    async def test_tick_scoped_vault_filters_signals(self, runner_env):
        runner, m = runner_env
        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        # Vault scoped to the strategy that produced signals → scoped path.
        runner._get_vault_strategy_ids = MagicMock(return_value=["faber_001"])
        await runner.tick()
        # aggregate_signals called for the per-vault scope (≥2 total calls).
        assert m["eval"].aggregate_signals.call_count >= 2


# ── generated-strategy rebalance (rebalancer decouple, Part A #1 of
# docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md) ───


def _gen_signals(strategy_id: str = "gen_001", asset: str = "sSPY", weight: float = 0.7) -> StrategySignals:
    """Signals a generated strategy's DSL spec would produce — distinct
    strategy_id from ``_signals()``'s curated ``faber_001`` so tests can tell
    the two evaluate_strategies() calls (curated vs. generated) apart."""
    return StrategySignals(
        strategy_id=strategy_id,
        strategy_name="Generated DSL Strategy",
        paper_title="Generated DSL Strategy",
        signals=[
            AssetSignal(
                strategy_id=strategy_id,
                strategy_name="Generated DSL Strategy",
                asset=asset,
                signal=Signal.LONG,
                weight=weight,
                reason="dsl entry condition met",
            )
        ],
        paper_arxiv_id="",
    )


class TestGeneratedStrategyRebalance:
    """A vault bound to a GENERATED strategy_id must now be rebalanced using
    its own persisted DSL spec (strategy_store.strategy_spec) — previously
    ``all_signals`` was curated-only, so a generated-strategy vault's scoped
    signals were always empty and the vault was silently skipped.

    DB fixture follows the proven rebind pattern from
    test_selection_bias_generated_gate.py: db.engine/SessionLocal are
    module-level globals created once at import, so monkeypatch.setenv alone
    doesn't repoint them — both must be rebound to a fresh per-test sqlite.
    """

    @pytest.fixture(autouse=True)
    def _use_tmp_db(self, tmp_path, monkeypatch):
        import archimedes.db as db
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        url = f"sqlite:///{tmp_path / 'gen_rebalance.db'}"
        monkeypatch.setenv("DATABASE_URL", url)
        eng = create_engine(url, connect_args={"check_same_thread": False})
        monkeypatch.setattr(db, "engine", eng)
        monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
        db.init_db()
        yield

    @staticmethod
    def _seed_strategy(strategy_id: str, *, raw_spec: str | None) -> None:
        """Insert a StrategyRecord directly (bypassing upsert_strategy) so a
        test can seed a deliberately-corrupt ``strategy_spec`` column value —
        ``raw_spec`` is stored VERBATIM (already-serialized text or None),
        not JSON-encoded here."""
        import archimedes.db as db
        from archimedes.models.strategy_store import StrategyRecord

        with db.get_session() as session:
            session.add(
                StrategyRecord(
                    id=strategy_id,
                    content_hash=("0x" + strategy_id).ljust(66, "0"),
                    generation_method="debate",
                    source_papers="[]",
                    strategy_name="Generated DSL Strategy",
                    thesis="test thesis",
                    asset_universe='["SPY"]',
                    risk_profile="moderate",
                    status="live",
                    strategy_spec=raw_spec,
                )
            )
            session.commit()

    async def test_generated_strategy_vault_now_reaches_process_vault(self, runner_env):
        """The core fix: a vault scoped to a generated strategy_id with a
        VALID persisted spec now yields scoped signals and is processed —
        not silently skipped."""
        import json

        from archimedes.services.strategy_dsl import FABER_2007_SPEC

        runner, m = runner_env
        self._seed_strategy("gen_001", raw_spec=json.dumps(FABER_2007_SPEC))

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xGenVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        runner._get_vault_strategy_ids = MagicMock(return_value=["gen_001"])

        def _eval_side_effect(strategies, _synth_assets, *_a, **_kw):
            ids = {getattr(s, "id", None) for s in strategies}
            return [_gen_signals()] if "gen_001" in ids else [_signals()]

        m["eval"].evaluate_strategies.side_effect = _eval_side_effect

        process_vault_spy = AsyncMock(wraps=runner._process_vault)
        runner._process_vault = process_vault_spy

        await runner.tick()

        process_vault_spy.assert_awaited_once()
        vault_addr, targets, scoped_signals = process_vault_spy.await_args.args[:3]
        assert vault_addr == "0xGenVault"
        assert any(t.weight > 0 for t in targets), "generated-strategy vault got no non-zero targets"
        assert any(ss.strategy_id == "gen_001" for ss in scoped_signals)

    async def test_legacy_vault_signals_stay_curated_only_alongside_generated(self, runner_env):
        """Regression (#1076 review): step 5b must NOT mutate ``all_signals``
        in place — the legacy (no-VaultMetadata) path hands that exact list to
        ``_process_vault``, where it feeds ``_build_reasoning`` and every
        ``_publish_trace`` call. A tick managing BOTH a legacy vault and a
        generated-strategy vault must give the legacy vault curated-only
        signals; otherwise another user's generated-strategy signals leak into
        the legacy vault's published reasoning trace."""
        import json

        from archimedes.services.strategy_dsl import FABER_2007_SPEC

        runner, m = runner_env
        self._seed_strategy("gen_001", raw_spec=json.dumps(FABER_2007_SPEC))

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xLegacyVault", "0xGenVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        runner._get_vault_strategy_ids = MagicMock(
            side_effect=lambda addr: None if addr == "0xLegacyVault" else ["gen_001"]
        )

        def _eval_side_effect(strategies, _synth_assets, *_a, **_kw):
            ids = {getattr(s, "id", None) for s in strategies}
            return [_gen_signals()] if "gen_001" in ids else [_signals()]

        m["eval"].evaluate_strategies.side_effect = _eval_side_effect

        process_vault_spy = AsyncMock(wraps=runner._process_vault)
        runner._process_vault = process_vault_spy

        await runner.tick()

        assert process_vault_spy.await_count == 2
        signals_by_vault = {c.args[0]: c.args[2] for c in process_vault_spy.await_args_list}
        assert all(ss.strategy_id != "gen_001" for ss in signals_by_vault["0xLegacyVault"]), (
            "generated-strategy signals leaked into the legacy vault's signal list"
        )
        assert any(ss.strategy_id == "gen_001" for ss in signals_by_vault["0xGenVault"])

    async def test_generated_strategy_without_spec_is_still_skipped(self, runner_env):
        """Control: a generated strategy_id bound to a vault but persisted
        WITHOUT a spec (legacy row, or pre-this-feature) is still skipped —
        the fix only unblocks ids that actually carry a persisted spec."""
        runner, m = runner_env
        self._seed_strategy("gen_002", raw_spec=None)

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xGenVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        runner._get_vault_strategy_ids = MagicMock(return_value=["gen_002"])

        process_vault_spy = AsyncMock(wraps=runner._process_vault)
        runner._process_vault = process_vault_spy

        await runner.tick()

        # No spec to evaluate → no scoped signals → vault skipped, same as
        # the pre-fix behavior for every generated-strategy vault.
        process_vault_spy.assert_not_awaited()
        m["executor"].read_portfolio.assert_not_called()

    async def test_corrupt_spec_json_is_skipped_per_record_curated_vault_unaffected(self, runner_env):
        """Fail-safe: a generated strategy_id whose persisted strategy_spec
        is NOT valid JSON (corrupt row) must not raise out of tick() — it's
        logged and skipped — and a second, legacy (unscoped) vault in the
        SAME tick still processes normally."""
        runner, m = runner_env
        self._seed_strategy("gen_003", raw_spec="{not valid json at all")

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xGenVault", "0xLegacyVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        # gen vault scoped to the corrupt id; legacy vault has no metadata.
        runner._get_vault_strategy_ids = MagicMock(
            side_effect=lambda addr: ["gen_003"] if addr == "0xGenVault" else None
        )

        process_vault_spy = AsyncMock(wraps=runner._process_vault)
        runner._process_vault = process_vault_spy

        await runner.tick()  # must not raise

        # The corrupt-spec vault never got scoped signals — skipped.
        processed_addrs = {c.args[0] for c in process_vault_spy.await_args_list}
        assert "0xGenVault" not in processed_addrs
        # The legacy vault (global-consensus fallback) still processed.
        assert "0xLegacyVault" in processed_addrs

    async def test_evaluator_exception_on_generated_call_does_not_break_tick(self, runner_env):
        """Fail-safe (belt-and-suspenders): even if the evaluator itself
        raises while grading a generated strategy (not just a bad row), the
        whole generated-signal load+eval is wrapped — tick() completes and
        curated/legacy vaults still process."""
        import json

        from archimedes.services.strategy_dsl import FABER_2007_SPEC

        runner, m = runner_env
        self._seed_strategy("gen_004", raw_spec=json.dumps(FABER_2007_SPEC))

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xGenVault", "0xLegacyVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        runner._get_vault_strategy_ids = MagicMock(
            side_effect=lambda addr: ["gen_004"] if addr == "0xGenVault" else None
        )

        def _eval_side_effect(strategies, _synth_assets, *_a, **_kw):
            ids = {getattr(s, "id", None) for s in strategies}
            if "gen_004" in ids:
                raise RuntimeError("boom — simulated evaluator failure")
            return [_signals()]

        m["eval"].evaluate_strategies.side_effect = _eval_side_effect

        process_vault_spy = AsyncMock(wraps=runner._process_vault)
        runner._process_vault = process_vault_spy

        await runner.tick()  # must not raise

        processed_addrs = {c.args[0] for c in process_vault_spy.await_args_list}
        assert "0xGenVault" not in processed_addrs
        assert "0xLegacyVault" in processed_addrs


# ── vault discovery ───────────────────────────────────────────


class TestVaultDiscovery:
    async def test_get_managed_vaults_from_factory(self, runner_env):
        runner, m = runner_env
        m["executor"].get_all_vaults = AsyncMock(return_value=["0xA", "0xB"])
        vaults = await runner._get_managed_vaults()
        assert set(vaults) == {"0xA", "0xB"}
        assert runner._known_vaults == {"0xA", "0xB"}

    async def test_get_managed_vaults_factory_error_returns_empty(self, runner_env):
        runner, m = runner_env
        m["executor"].get_all_vaults = AsyncMock(side_effect=ConnectionError("RPC down"))
        assert await runner._get_managed_vaults() == []

    async def test_discover_new_vaults_returns_only_unseen(self, runner_env):
        runner, m = runner_env
        runner._known_vaults = {"0xA"}
        m["executor"].get_all_vaults = AsyncMock(return_value=["0xA", "0xB"])
        new = await runner._discover_new_vaults()
        assert new == ["0xB"]
        assert "0xB" in runner._known_vaults

    async def test_discover_new_vaults_none_when_all_known(self, runner_env):
        runner, m = runner_env
        runner._known_vaults = {"0xA", "0xB"}
        m["executor"].get_all_vaults = AsyncMock(return_value=["0xA", "0xB"])
        assert await runner._discover_new_vaults() == []

    async def test_discover_new_vaults_factory_error_returns_empty(self, runner_env):
        runner, m = runner_env
        m["executor"].get_all_vaults = AsyncMock(side_effect=RuntimeError("boom"))
        assert await runner._discover_new_vaults() == []


# ── commit / reveal (DRY_RUN) ─────────────────────────────────


class TestCommitRevealDryRun:
    @staticmethod
    def _trade():
        from archimedes.models.portfolio import TradeDirection, TradeOrder

        return TradeOrder(
            symbol="sSPY",
            token_address="0xsspy",
            direction=TradeDirection.BUY,
            amount=100.0,
            estimated_usdc_value=100.0,
        )

    async def test_commit_trace_dry_run_builds_and_hashes(self, runner_env):
        runner, _ = runner_env
        trace, trace_id, commit_tx, commit_block, claimed_time, reverted = await runner._commit_trace(
            "0xVault",
            [self._trade()],
            [_signals()],
            "risk_on",
            _consensus(),
            "tick-1",
            "reasoning text",
            _portfolio(),
            _allocs(sSPY=0.6, USDC=0.4),
            None,  # trade_id — DRY_RUN, so no real commit is made and it's unused
        )
        # DRY_RUN → no on-chain ids, but the canonical trace IS built + hashed.
        assert trace_id is None and commit_tx is None and commit_block is None and claimed_time is None
        assert reverted is False
        assert trace.trace_hash and len(trace.trace_hash.removeprefix("0x")) == 64
        assert trace.decision_type.value == "rebalance"
        # portfolio_after is hashed, so it carries the pre-trade INTENDED targets (#903).
        assert trace.portfolio_after["intended"] is True
        assert trace.portfolio_after["target_weights"] == {"sSPY": 0.6, "USDC": 0.4}

    async def test_reveal_trace_dry_run_persists_off_chain(self, runner_env):
        runner, m = runner_env
        # Build a trace via commit, then reveal it (DRY_RUN → no chain).
        trace, trace_id, *_ = await runner._commit_trace(
            "0xVault",
            [self._trade()],
            [_signals()],
            "risk_on",
            _consensus(),
            "t",
            "r",
            _portfolio(),
            _allocs(sSPY=0.6, USDC=0.4),
            None,  # trade_id — DRY_RUN, so no real commit is made and it's unused
        )
        await runner._reveal_trace(trace, trace_id, "tick-1", tx_hashes=[])
        # Off-chain persist happened with temporal_binding_source = "none" (dry-run).
        m["state"].save_trace.assert_awaited()
        saved = m["state"].save_trace.await_args.args[0]
        assert saved["temporal_binding_source"] == "none"
        assert saved["is_verified"] is False


# ── _publish_trace (legacy SKIP path) ─────────────────────────


class TestPublishTrace:
    async def test_empty_vault_publishes_skip_trace(self, runner_env):
        runner, m = runner_env
        # Empty portfolio → _process_vault publishes an "empty_vault" SKIP trace.
        m["executor"].read_portfolio = AsyncMock(
            return_value=Portfolio(vault_address="0xVault", total_value_usdc=0.0, holdings=[])
        )
        spy = AsyncMock(wraps=runner._publish_trace)
        runner._publish_trace = spy
        await runner._process_vault("0xVault", _allocs(sSPY=0.6), [_signals()], "risk_on", _consensus(), "tick-1")
        spy.assert_awaited_once()
        # The trigger names the empty-vault decision.
        assert spy.await_args.args[2] == "empty_vault"
        # The trace was hashed + persisted off-chain (DRY_RUN → no anchor).
        m["state"].save_trace.assert_awaited()
        saved = m["state"].save_trace.await_args.args[0]
        assert saved["decision_type"] == "skip"
        assert saved["is_verified"] is False
        assert len(saved["trace_hash"].removeprefix("0x")) == 64


# ── commit-reveal guard: failed commit must block the trade ───
# (PR #1045 Copilot review, comment 2 — FUNDS-CRITICAL: if the registry
# supports commit-reveal but the commit itself fails, Phase 2 must not
# submit the rebalance — post-#588-redeploy that reverts on-chain with
# "no matching commitment", wasting gas and leaving an unrevealed
# commitment attempt.)


class TestCommitRevealGuardBlocksTradeOnFailedCommit:
    async def test_failed_commit_skips_trade_when_commit_reveal_supported(self, runner_env, monkeypatch):
        runner, m = runner_env
        # Exercise the LIVE (non-DRY_RUN) path — the guard is gated on the same
        # `not DRY_RUN` condition the commit path uses.
        monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", False)

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        # Phase 0 SIZE succeeds with valid on-chain arrays...
        m["executor"].build_trade_arrays = AsyncMock(return_value=(["0x" + "11" * 20], [600_000000], [], []))
        # ...but Phase 2 must never be reached: submitting now would revert
        # on-chain against a missing commitment (#588 commit-before-trade guard).
        m["executor"].execute_trades = AsyncMock(side_effect=AssertionError("execute_trades must not be called"))

        # Registry supports commit-reveal, but the commit itself fails.
        m["publisher"].supports_commit_reveal = MagicMock(return_value=True)
        m["publisher"].commit = AsyncMock(return_value=(None, None, None, False))
        # The SKIP trace this guard publishes still anchors via the legacy publishTrace path.
        m["publisher"].publish = AsyncMock(return_value=None)

        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        m["executor"].execute_trades.assert_not_called()
        m["state"].save_trace.assert_awaited()
        saved = m["state"].save_trace.await_args.args[0]
        assert saved["decision_type"] == "skip"
        assert saved["trigger"] == "commit_failed"

    async def test_confirmed_revert_skips_trade_even_with_a_real_tx_hash(self, runner_env, monkeypatch):
        """#1095 review (dbrowneup): a CONFIRMED revert still returns a real
        commit_tx (kept for the diagnostic trail), so the guard must not treat
        commit_tx-is-truthy as "commit landed" — it must also check
        commit_reverted. Without this, Phase 2 would submit a trade against a
        commitment that never landed, reverting on-chain with "no matching
        commitment"."""
        runner, m = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", False)

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        m["executor"].build_trade_arrays = AsyncMock(return_value=(["0x" + "11" * 20], [600_000000], [], []))
        m["executor"].execute_trades = AsyncMock(side_effect=AssertionError("execute_trades must not be called"))

        # Registry supports commit-reveal; the commit tx was sent and mined,
        # but reverted on-chain — a real tx hash, trace_id=None, reverted=True.
        m["publisher"].supports_commit_reveal = MagicMock(return_value=True)
        m["publisher"].commit = AsyncMock(return_value=(None, "0xREVERTED", 100, True))
        m["publisher"].publish = AsyncMock(return_value=None)

        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        m["executor"].execute_trades.assert_not_called()
        m["state"].save_trace.assert_awaited()
        saved = m["state"].save_trace.await_args.args[0]
        assert saved["decision_type"] == "skip"
        assert saved["trigger"] == "commit_failed"

    async def test_confirmed_revert_logs_reverted_not_anchored(self, runner_env, monkeypatch, caplog):
        """#1095 review: the tick log must not claim "COMMIT anchored" for a
        commit whose tx CONFIRMED as reverted — a real tx hash that anchored
        nothing is the exact falsehood the commit_reverted plumbing kills."""
        import logging as _logging

        runner, m = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", False)
        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        m["executor"].build_trade_arrays = AsyncMock(return_value=(["0x" + "11" * 20], [600_000000], [], []))
        m["executor"].execute_trades = AsyncMock(side_effect=AssertionError("execute_trades must not be called"))
        m["publisher"].supports_commit_reveal = MagicMock(return_value=True)
        m["publisher"].commit = AsyncMock(return_value=(None, "0xREVERTED", 100, True))
        m["publisher"].publish = AsyncMock(return_value=None)
        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        with caplog.at_level(_logging.INFO, logger="archimedes.chain.agent_runner"):
            await runner.tick()

        assert "COMMIT anchored" not in caplog.text
        assert "COMMIT reverted on-chain" in caplog.text

    async def test_dry_run_unaffected_by_guard(self, runner_env):
        """DRY_RUN never reaches a real commit (trade_id/commit_tx are None by
        design) — the guard must not block the DRY_RUN simulate path."""
        runner, m = runner_env  # runner_env fixture forces DRY_RUN=True
        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        m["publisher"].supports_commit_reveal = MagicMock(return_value=True)
        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        # DRY_RUN skips the SIZE phase entirely (no trade_arrays built) and
        # proceeds through the normal "DRY RUN — skipping on-chain execution"
        # branch — the new guard must not introduce a SKIP where none existed.
        m["executor"].build_trade_arrays.assert_not_called()
        m["state"].save_trace.assert_awaited()
        saved = m["state"].save_trace.await_args.args[0]
        assert saved["decision_type"] == "rebalance"

    async def test_legacy_registry_without_commit_reveal_still_trades(self, runner_env, monkeypatch):
        """If the registry does NOT support commit-reveal (legacy publishTrace-only
        anchor), the old proceed-to-trade behavior must remain — the guard only
        blocks when commit-reveal IS supported and the commit failed. Here even
        the legacy publishTrace anchor itself fails (commit_tx ends up None, same
        as the failed-commit case above) to prove the guard's gate is really on
        `supports_commit_reveal()`, not merely on `commit_tx is None`."""
        runner, m = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", False)

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        m["executor"].build_trade_arrays = AsyncMock(return_value=(["0x" + "11" * 20], [600_000000], [], []))
        m["executor"].execute_trades = AsyncMock(return_value=["0xtradetx"])

        # Pre-#588 registry: no commit()/reveal() exposed at all — and the
        # publishTrace fallback anchor also fails, so commit_tx is None too.
        m["publisher"].supports_commit_reveal = MagicMock(return_value=False)
        m["publisher"].publish = AsyncMock(return_value=None)

        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        # Trade proceeds exactly as before commit-reveal existed — the guard
        # does not fire because supports_commit_reveal() is False.
        m["executor"].execute_trades.assert_awaited_once()


class TestFeeCapGuardBlocksTradeOnHostileVault:
    """Issue #1138 follow-up (Dan's PR #1139 review): the marketplace-only
    fee guard never reached the agent's own rebalance() call — the actual
    state-changing path that triggers Vault.sol's _accrueFees(). These cover
    the gap: over-cap and unreadable fees must both refuse Phase 2 TRADE,
    fail-closed, while an at-cap vault must still trade normally."""

    async def test_over_cap_fees_skip_trade(self, runner_env, monkeypatch):
        runner, m = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", False)

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        m["executor"].build_trade_arrays = AsyncMock(return_value=(["0x" + "11" * 20], [600_000000], [], []))
        m["executor"].execute_trades = AsyncMock(side_effect=AssertionError("execute_trades must not be called"))
        from archimedes.chain.constants import MAX_PERFORMANCE_FEE_BPS

        m["executor"].get_vault_fee_bps = AsyncMock(return_value=(0, MAX_PERFORMANCE_FEE_BPS + 1))

        m["publisher"].supports_commit_reveal = MagicMock(return_value=False)
        m["publisher"].publish = AsyncMock(return_value=None)
        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        m["executor"].execute_trades.assert_not_called()
        m["state"].save_trace.assert_awaited()
        saved = m["state"].save_trace.await_args.args[0]
        assert saved["decision_type"] == "skip"
        assert saved["trigger"] == "fee_cap_exceeded"

    async def test_unreadable_fees_skip_trade_fail_closed(self, runner_env, monkeypatch):
        """Unreadable fees refuse the same as over-cap — never silently proceed."""
        runner, m = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", False)

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        m["executor"].build_trade_arrays = AsyncMock(return_value=(["0x" + "11" * 20], [600_000000], [], []))
        m["executor"].execute_trades = AsyncMock(side_effect=AssertionError("execute_trades must not be called"))
        m["executor"].get_vault_fee_bps = AsyncMock(side_effect=RuntimeError("rpc down"))

        m["publisher"].supports_commit_reveal = MagicMock(return_value=False)
        m["publisher"].publish = AsyncMock(return_value=None)
        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        m["executor"].execute_trades.assert_not_called()
        saved = m["state"].save_trace.await_args.args[0]
        assert saved["decision_type"] == "skip"
        assert saved["trigger"] == "fee_cap_exceeded"
        assert "rpc down" in saved["reasoning"]

    async def test_at_cap_fees_trade_proceeds(self, runner_env, monkeypatch):
        """A vault at exactly the caps is allowed — the contract allows it too."""
        runner, m = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", False)
        from archimedes.chain.constants import MAX_MANAGEMENT_FEE_BPS, MAX_PERFORMANCE_FEE_BPS

        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        m["executor"].build_trade_arrays = AsyncMock(return_value=(["0x" + "11" * 20], [600_000000], [], []))
        m["executor"].execute_trades = AsyncMock(return_value=["0xtradetx"])
        m["executor"].get_vault_fee_bps = AsyncMock(return_value=(MAX_MANAGEMENT_FEE_BPS, MAX_PERFORMANCE_FEE_BPS))

        m["publisher"].supports_commit_reveal = MagicMock(return_value=False)
        m["publisher"].publish = AsyncMock(return_value=None)
        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        m["executor"].execute_trades.assert_awaited_once()

    async def test_dry_run_unaffected_by_fee_guard(self, runner_env):
        """DRY_RUN never submits a real trade — the fee-cap read must not run
        (and must not block the simulate path) under DRY_RUN."""
        runner, m = runner_env  # runner_env fixture forces DRY_RUN=True
        m["executor"].get_all_vaults = AsyncMock(return_value=["0xVault"])
        m["executor"].read_portfolio = AsyncMock(return_value=_portfolio())
        m["executor"].set_token_oracles = AsyncMock()
        m["executor"].set_target_allocations = AsyncMock()
        m["executor"].get_vault_fee_bps = AsyncMock(side_effect=AssertionError("must not be read under DRY_RUN"))
        runner._get_vault_strategy_ids = MagicMock(return_value=None)

        await runner.tick()

        m["executor"].get_vault_fee_bps.assert_not_called()
        saved = m["state"].save_trace.await_args.args[0]
        assert saved["decision_type"] == "rebalance"


# ── run() loop body ───────────────────────────────────────────


def _fake_lease(*, is_valid: bool = True) -> MagicMock:
    """A RunnerLeaseGuard stub that is always already-acquired and valid (#1043)."""
    lease = MagicMock()
    lease.acquire_forever = AsyncMock(return_value=None)
    lease.start_renewal = MagicMock()
    lease.install_sigterm_release = MagicMock()
    lease.is_valid = is_valid
    return lease


class TestRunLoop:
    async def test_run_executes_one_tick_then_sleeps(self, runner_env, monkeypatch):
        runner, m = runner_env
        m["client"].is_connected = AsyncMock(return_value=True)

        class _Stop(Exception):
            pass

        # Patch the module symbols run() uses, plus a sleep that breaks the loop.
        with (
            patch("archimedes.chain.agent_runner.StrategyRunner", return_value=runner),
            patch("archimedes.chain.agent_runner.chain_client", m["client"]),
            patch("archimedes.chain.agent_runner.RunnerLeaseGuard", return_value=_fake_lease()),
            patch("archimedes.chain.agent_runner.asyncio.sleep", AsyncMock(side_effect=_Stop)),
        ):
            runner.tick = AsyncMock()
            runner.state.save_heartbeat = AsyncMock()
            from archimedes.chain.agent_runner import run

            with pytest.raises(_Stop):
                await run()
            runner.tick.assert_awaited_once()

    async def test_run_wires_lease_onto_runner_before_ticking(self, runner_env, monkeypatch):
        """run() blocks on acquire_forever() and wires the lease BEFORE the loop starts (#1043)."""
        runner, m = runner_env
        m["client"].is_connected = AsyncMock(return_value=True)

        class _Stop(Exception):
            pass

        lease = _fake_lease()
        with (
            patch("archimedes.chain.agent_runner.StrategyRunner", return_value=runner),
            patch("archimedes.chain.agent_runner.chain_client", m["client"]),
            patch("archimedes.chain.agent_runner.RunnerLeaseGuard", return_value=lease),
            patch("archimedes.chain.agent_runner.asyncio.sleep", AsyncMock(side_effect=_Stop)),
        ):
            runner.tick = AsyncMock()
            runner.state.save_heartbeat = AsyncMock()
            from archimedes.chain.agent_runner import run

            with pytest.raises(_Stop):
                await run()

        lease.acquire_forever.assert_awaited_once()
        lease.start_renewal.assert_called_once()
        lease.install_sigterm_release.assert_called_once()
        assert runner.lease is lease
        runner.tick.assert_awaited_once()


# ── Cadence gate upstream of the drift threshold (divergence audit F3) ────


def _band_price_series() -> pd.Series:
    """20-bar-down / 20-bar-up sawtooth. Deterministic; RSI(14) sweeps 0–83 so
    an RSI 30/70 band spec genuinely flips both ways."""
    vals = [100.0]
    for i in range(1, 320):
        step = 0.010 if (i // 20) % 2 else -0.010
        zig = 0.002 if (i * 7919) % 7 < 3 else -0.001
        vals.append(round(vals[-1] * (1.0 + step + zig), 10))
    return pd.Series(vals, name="sSPY")


def _band_spec(rebalance_frequency: str) -> dict:
    return {
        "name": f"RSI band ({rebalance_frequency})",
        "asset_universe": ["SPY"],
        "rebalance_frequency": rebalance_frequency,
        "entry": {"lt": ["rsi_14", 30]},
        "exit": {"gt": ["rsi_14", 70]},
        "position_sizing": {"type": "full_invested_when_in_market"},
        "source_arxiv_ids": ["0000.0000"],
        "look_ahead_safe": True,
    }


def _spec_strategy(spec: dict):
    from archimedes.models.paper_ref import PaperRef
    from archimedes.models.strategy import Strategy

    return Strategy(
        id="band_001",
        papers=[PaperRef(title=spec["name"])],
        asset_universe=["SPY"],
        strategy_spec=spec,
    )


def _portfolio_at(weights: dict[str, float], total: float = 1000.0) -> Portfolio:
    return Portfolio(
        vault_address="0xVault",
        total_value_usdc=total,
        holdings=[
            PortfolioHolding(
                symbol=sym,
                token_address=f"0x{sym.lower()}",
                amount=w * total,
                weight=w,
                value_usdc=w * total,
            )
            for sym, w in weights.items()
        ],
        risk_profile="moderate",
    )


class TestCadenceGateRunsBeforeDriftThreshold:
    """F3, at the runner boundary: the per-strategy cadence gate decides WHEN a
    strategy may change its target; _DRIFT_THRESHOLD only decides whether an
    already-cadence-gated change is worth trading. Same vault, same bar, same
    drift gate — only the spec's declared ``rebalance_frequency`` differs.

    Hermetic: a real StrategySignalEvaluator over an injected in-memory price
    Series (no yfinance), and _compute_trades called directly (no chain).
    """

    @staticmethod
    def _targets(evaluator, spec: dict, prices, upto: int) -> list[TargetAllocation]:
        signals = evaluator.evaluate_strategies(
            [_spec_strategy(spec)],
            ["sSPY"],
            price_histories={"sSPY": prices.iloc[: upto + 1]},
        )
        weights = evaluator.aggregate_signals(signals, usdc_floor=0.20)
        return [
            TargetAllocation(symbol=s, token_address=f"0x{s.lower()}", weight=w, strategy_ids=["band_001"])
            for s, w in weights.items()
        ]

    def test_monthly_spec_holds_target_where_a_daily_spec_would_trade(self, runner_env):
        from archimedes.chain.agent_runner import _DRIFT_THRESHOLD
        from archimedes.services.strategy_signal_evaluator import StrategySignalEvaluator

        runner, _ = runner_env
        evaluator = StrategySignalEvaluator()
        prices = _band_price_series()
        monthly, daily = _band_spec("monthly"), _band_spec("daily")
        warmup = 14  # max indicator period → first bar the FSM can act on

        def weight_of(targets, sym):
            return next((t.weight for t in targets if t.symbol == sym), 0.0)

        # An OFF-cadence bar on which the daily version of the same spec wants a
        # different sSPY weight than the monthly version is holding.
        bar = next(
            (
                t
                for t in range(warmup + 1, 200)
                if (t - warmup) % 21 != 0
                and abs(
                    weight_of(self._targets(evaluator, daily, prices, t), "sSPY")
                    - weight_of(self._targets(evaluator, monthly, prices, t), "sSPY")
                )
                > _DRIFT_THRESHOLD
            ),
            None,
        )
        assert bar is not None, (
            "no off-cadence bar where the daily and monthly versions of one spec disagree — "
            "either the fixture stopped exercising cadence, or the live path is ignoring "
            "rebalance_frequency again (audit F3)"
        )

        # The vault sits exactly on the monthly strategy's target from the
        # previous bar.
        prev_targets = self._targets(evaluator, monthly, prices, bar - 1)
        portfolio = _portfolio_at({t.symbol: t.weight for t in prev_targets})

        # Cadence gate holds the target → nothing for the drift gate to fire on.
        held_trades = runner._compute_trades(portfolio, self._targets(evaluator, monthly, prices, bar))
        assert held_trades == [], f"monthly spec produced trades on off-cadence bar {bar}: {held_trades}"

        # Same vault, same bar, same 15% threshold — the DAILY spec is due, so it
        # moves and the drift gate lets it through. This is the contrast that
        # makes the assertion above mean something rather than "nothing ever
        # trades on this fixture".
        due_trades = runner._compute_trades(portfolio, self._targets(evaluator, daily, prices, bar))
        assert due_trades, f"daily spec produced no trades on bar {bar} — contrast case is vacuous"
        assert any(t.symbol == "sSPY" for t in due_trades)

    def test_repeated_intraday_ticks_never_accumulate_drift(self, runner_env):
        """The literal ~288-ticks-a-day case: re-evaluating the same daily-bar
        window over and over must keep producing the identical target, so the
        vault never drifts into a trade between decision bars."""
        from archimedes.services.strategy_signal_evaluator import StrategySignalEvaluator

        runner, _ = runner_env
        evaluator = StrategySignalEvaluator()
        prices = _band_price_series()
        monthly = _band_spec("monthly")

        targets = self._targets(evaluator, monthly, prices, 150)
        portfolio = _portfolio_at({t.symbol: t.weight for t in targets})
        for _ in range(24):
            assert runner._compute_trades(portfolio, self._targets(evaluator, monthly, prices, 150)) == []
