"""Agent-tick commit-reveal wiring + claim-integrity tests (#714 / T0.3).

Hermetic: the chain client, executor, trace_publisher, provider, and state
store are all mocked at the boundary (mirrors ``test_agent_runner.py``'s runner fixture).

Covers:
  - the reveal phase uses the real ``trace_publisher.reveal()`` (NOT publishTrace) when a
    commit-reveal trace_id exists, and anchors NOTHING when it does not — the v1
    ``publishTrace`` fallback was removed once the #588 redeploy landed (#714);
  - ``temporal_binding_source`` is "chain" ONLY on the real commit-reveal path, and the
    persisted ``temporal_binding_valid`` requires commit < trade <= reveal block ordering;
  - the TraceResponse schema guard can never surface a True binding without a chain source
    (closes AUDIT_2026-06-14 #3 — the "Temporal Binding VERIFIED" badge off a Redis bool).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.models.trace import DecisionType, ReasoningTrace


def _make_trace() -> ReasoningTrace:
    trace = ReasoningTrace(
        id="tick-trace-001",
        vault_address="0x1234567890abcdef1234567890abcdef12345678",
        decision_type=DecisionType.REBALANCE,
        trigger="strategy_signal_drift",
        timestamp=datetime.now(UTC),
        reasoning="Agent tick commit-reveal test",
        confidence=0.9,
    )
    trace.compute_hash()
    return trace


@pytest.fixture()
def runner_env():
    """A StrategyRunner with all chain boundaries mocked and the on-chain path armed."""
    with (
        patch("archimedes.chain.agent_runner.chain_client"),
        patch("archimedes.chain.agent_runner.chain_executor"),
        patch("archimedes.chain.agent_runner.trace_publisher") as mock_tp,
        patch("archimedes.chain.agent_runner.default_provider"),
        patch("archimedes.chain.agent_runner.AgentStateStore"),
        patch("archimedes.chain.agent_runner.DRY_RUN", False),
    ):
        from archimedes.chain.agent_runner import StrategyRunner

        runner = StrategyRunner()
        runner.state = MagicMock()
        runner.state.save_trace = AsyncMock()
        yield runner, mock_tp


def _saved(runner) -> dict:
    return runner.state.save_trace.call_args[0][0]


class TestRevealWiring:
    def test_reveal_uses_commit_reveal_not_publish(self, runner_env):
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.reveal = AsyncMock(return_value=("0xREVEAL", 102))
        mock_tp.publish = AsyncMock(return_value=None)

        asyncio.run(
            runner._reveal_trace(
                _make_trace(),
                trace_id=42,
                tick_id="t1",
                tx_hashes=["0xtrade"],
                commit_tx="0xcommit",
                commit_block=100,
                trade_block=101,
            )
        )

        mock_tp.reveal.assert_called_once()
        mock_tp.publish.assert_not_called()  # the live path is commit-reveal, never publishTrace
        # #1526: hash-only storagePointer — we do not pin.
        assert mock_tp.reveal.await_args.kwargs["storage_pointer"] == ""
        saved = _saved(runner)
        assert saved["ipfs_cid"] is None

    def test_reveal_without_trace_id_anchors_nothing(self, runner_env):
        """#714: no commitment => no anchor at all, never a v1 publishTrace stand-in.

        Before #714 this fell back to ``publish()``. That anchor reveals no
        commitment, so persisting its tx as ``reveal_tx_hash``/``arc_tx_hash``
        reported an unbound anchor as a completed reveal and set ``is_verified``.
        """
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.reveal = AsyncMock(return_value=("0xREVEAL", 102))
        mock_tp.publish = AsyncMock(return_value="0xLEGACY")

        asyncio.run(runner._reveal_trace(_make_trace(), trace_id=None, tick_id="t1", tx_hashes=["0xtrade"]))

        mock_tp.publish.assert_not_called()
        mock_tp.reveal.assert_not_called()
        saved = _saved(runner)
        assert saved["arc_tx_hash"] is None
        assert saved["reveal_tx_hash"] is None
        assert saved["is_verified"] is False


class TestTemporalBindingPersistence:
    def test_source_chain_and_valid_on_real_commit_reveal(self, runner_env):
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.reveal = AsyncMock(return_value=("0xREVEAL", 102))
        mock_tp.publish = AsyncMock(return_value=None)

        asyncio.run(
            runner._reveal_trace(
                _make_trace(),
                trace_id=42,
                tick_id="t1",
                tx_hashes=["0xtrade"],
                commit_tx="0xcommit",
                commit_block=100,
                trade_block=101,
            )
        )

        saved = _saved(runner)
        assert saved["temporal_binding_source"] == "chain"
        # commit(100) < trade(101) <= reveal(102) -> a genuine, verified binding.
        assert saved["temporal_binding_valid"] is True

    def test_source_none_and_not_valid_on_fallback(self, runner_env):
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.reveal = AsyncMock(return_value=("0xREVEAL", 102))
        mock_tp.publish = AsyncMock(return_value="0xPUB")

        asyncio.run(
            runner._reveal_trace(
                _make_trace(),
                trace_id=None,
                tick_id="t1",
                tx_hashes=["0xtrade"],
                commit_tx=None,
                commit_block=99,
                trade_block=101,
            )
        )

        saved = _saved(runner)
        assert saved["temporal_binding_source"] == "none"
        # No real commit-reveal trace_id -> binding cannot be asserted, even though
        # a fallback commit_block exists (this is the exact masquerade #714 closes).
        assert not saved["temporal_binding_valid"]


class TestHashBindingIntegrity:
    """#903: the reveal phase must submit byte-identical canonical content.

    Pre-fix, ``_reveal_trace`` overwrote ``portfolio_after`` (a hashed field) with
    settlement tx hashes, so the revealed ``canonical_json()`` no longer matched the
    committed keccak256 and the contract reverted "Hash mismatch" on every rebalance.
    """

    def test_reveal_does_not_mutate_hashed_fields(self, runner_env):
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.reveal = AsyncMock(return_value=("0xREVEAL", 102))
        mock_tp.publish = AsyncMock(return_value=None)

        trace = _make_trace()
        trace.portfolio_after = {"intended": True, "target_weights": {"sSPY": 0.6}}
        committed_hash = trace.compute_hash()
        committed_bytes = trace.canonical_json()

        asyncio.run(
            runner._reveal_trace(
                trace,
                trace_id=42,
                tick_id="t1",
                tx_hashes=["0xtrade"],
                commit_tx="0xcommit",
                commit_block=100,
                trade_block=101,
            )
        )

        # The exact committed bytes are what got revealed — no hashed field moved.
        assert trace.portfolio_after == {"intended": True, "target_weights": {"sSPY": 0.6}}
        assert trace.canonical_json() == committed_bytes
        assert trace.compute_hash() == committed_hash
        revealed_trace = mock_tp.reveal.call_args.args[1]
        assert revealed_trace.canonical_json() == committed_bytes

    def test_settlement_tx_hashes_persisted_outside_hashed_set(self, runner_env):
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.reveal = AsyncMock(return_value=("0xREVEAL", 102))

        trace = _make_trace()
        trace.consulted_paper_hashes = ["1706.03762:abc123"]
        trace.compute_hash()

        asyncio.run(
            runner._reveal_trace(
                trace,
                trace_id=42,
                tick_id="t1",
                tx_hashes=["0xtrade1", "0xtrade2"],
                commit_tx="0xcommit",
                commit_block=100,
                trade_block=101,
            )
        )

        saved = _saved(runner)
        assert saved["settlement_tx_hashes"] == ["0xtrade1", "0xtrade2"]
        # Hashed fields round-trip so /traces/{id}/canonical can rebuild the
        # exact committed bytes for external verification.
        assert saved["consulted_paper_hashes"] == ["1706.03762:abc123"]
        assert saved["portfolio_after"] == trace.portfolio_after


class TestRevealTimingWindow:
    """#903: reveal must not fire before claimedExecutionTime.

    The contract requires reveal-block timestamp >= claimedExecutionTime; trades
    settle in seconds on Arc, so an immediate intra-tick reveal always reverted
    "Reveal before claimed execution". The reveal now waits the window out.
    """

    def _run_reveal(self, runner, mock_tp, claimed_execution_time):
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.reveal = AsyncMock(return_value=("0xREVEAL", 102))
        with patch("archimedes.chain.agent_runner.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            asyncio.run(
                runner._reveal_trace(
                    _make_trace(),
                    trace_id=42,
                    tick_id="t1",
                    tx_hashes=["0xtrade"],
                    commit_tx="0xcommit",
                    commit_block=100,
                    trade_block=101,
                    claimed_execution_time=claimed_execution_time,
                )
            )
        return mock_sleep, mock_tp.reveal

    def test_reveal_waits_out_remaining_claimed_window(self, runner_env):
        runner, mock_tp = runner_env
        claimed = int(datetime.now(UTC).timestamp()) + 30
        mock_sleep, mock_reveal = self._run_reveal(runner, mock_tp, claimed)

        mock_sleep.assert_awaited_once()
        waited = mock_sleep.await_args.args[0]
        # Remaining window (~30s) plus the skew buffer, minus test overhead.
        assert 25 <= waited <= 30 + runner._REVEAL_SKEW_BUFFER_S
        mock_reveal.assert_awaited_once()

    def test_reveal_does_not_wait_when_window_already_passed(self, runner_env):
        runner, mock_tp = runner_env
        claimed = int(datetime.now(UTC).timestamp()) - 120
        mock_sleep, mock_reveal = self._run_reveal(runner, mock_tp, claimed)

        mock_sleep.assert_not_awaited()
        mock_reveal.assert_awaited_once()

    def test_reveal_does_not_wait_without_claimed_time(self, runner_env):
        runner, mock_tp = runner_env
        mock_sleep, mock_reveal = self._run_reveal(runner, mock_tp, None)

        mock_sleep.assert_not_awaited()
        mock_reveal.assert_awaited_once()


class TestTraceResponseClaimGuard:
    """The schema is the last line of defense against a stale Redis True binding."""

    @staticmethod
    def _resp(**kw):
        from archimedes.api.schemas import TraceResponse

        base = {
            "id": "t",
            "vault_address": "0xv",
            "decision_type": "rebalance",
            "trigger": "x",
            "timestamp": "2026-06-29T00:00:00Z",
            "reasoning": "r",
            "confidence": 0.5,
            "trace_hash": "ab",
        }
        base.update(kw)
        return TraceResponse(**base)

    def test_true_binding_coerced_to_none_without_chain_source(self):
        r = self._resp(temporal_binding_valid=True, temporal_binding_source="none")
        assert r.temporal_binding_valid is None  # cannot claim a binding off a non-chain source

    def test_true_binding_preserved_with_chain_source(self):
        r = self._resp(temporal_binding_valid=True, temporal_binding_source="chain")
        assert r.temporal_binding_valid is True

    def test_is_verified_left_honest(self):
        # A publishTrace anchor is a genuine on-chain hash confirmation: is_verified stays
        # honest even when the stronger temporal binding is absent.
        r = self._resp(is_verified=True, temporal_binding_source="none")
        assert r.is_verified is True


class TestRevealFailureAfterTradeExecuted:
    """G9 (audit 2026-08-18): trade + commit succeed, then the reveal FAILS.

    The mirror of the guarded commit-fails mode (which skips the trade). Here
    the money already moved on-chain, so the only acceptable behaviors are
    loud honesty and zero fabricated verification. Adjudicated against
    current code: a loud ERROR fires and the trace persists as UNVERIFIED
    (``is_verified`` False, reveal fields None, temporal binding invalid) with
    the dangling commit preserved. What does NOT exist is a reconciliation
    path that retries the reveal on a later tick — tracked as a follow-up
    issue; these tests pin the never-fabricate contract that any future
    reconciliation must keep.
    """

    def test_reveal_failure_is_loud_and_persists_an_honest_unverified_trace(self, runner_env, caplog):
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("execution reverted: reveal window"))
        mock_tp.publish = AsyncMock(return_value=None)

        with caplog.at_level("ERROR"):
            asyncio.run(
                runner._reveal_trace(
                    _make_trace(),
                    trace_id=42,
                    tick_id="t9",
                    tx_hashes=["0xtrade"],
                    commit_tx="0xcommit",
                    commit_block=100,
                    trade_block=101,
                )
            )  # must NOT raise — a failed reveal can never kill the tick

        assert "REVEAL publish FAILED" in caplog.text  # the loud, alertable signal
        saved = _saved(runner)
        assert saved["trade_tx_hash"] == "0xtrade"  # the executed trade stays visible…
        assert saved["is_verified"] is False  # …but is never claimed verified
        assert saved["reveal_tx_hash"] is None
        assert saved["reveal_block_number"] is None
        assert saved["temporal_binding_valid"] is False
        # The dangling commitment is preserved — the raw material any future
        # reconciliation pass needs to retry the reveal.
        assert saved["commit_tx_hash"] == "0xcommit"
        assert saved["commit_block_number"] == 100

    def test_pre_v15_registry_keeps_the_same_honest_contract(self, runner_env, caplog):
        """A registry with no commit/reveal degrades identically — loud, unverified.

        #714 removed the v1 ``publishTrace`` fallback that used to run here, so the
        degraded state is now a visible absence: an ERROR naming the reason and a
        trace persisted unanchored. The executed trade stays visible either way.
        """
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=False)
        mock_tp.reveal = AsyncMock(return_value=("0xNEVER", 0))
        mock_tp.publish = AsyncMock(return_value="0xLEGACY")

        with caplog.at_level("ERROR"):
            asyncio.run(runner._reveal_trace(_make_trace(), trace_id=None, tick_id="t9b", tx_hashes=["0xtrade"]))

        assert "REVEAL SKIPPED" in caplog.text  # the loud, alertable signal
        mock_tp.publish.assert_not_called()
        saved = _saved(runner)
        assert saved["is_verified"] is False
        assert saved["arc_tx_hash"] is None
        assert saved["trade_tx_hash"] == "0xtrade"
        assert saved["temporal_binding_valid"] is False


# ── #714: the legacy publishTrace call sites are gone from agent_runner ──


def _commit_args(**over):
    """Minimal real-shaped args for ``_commit_trace``.

    Only the fields the method actually reads are set, and they are real values
    (not MagicMocks) because the trace it builds must survive ``canonical_json()``.
    """
    consensus = MagicMock()
    consensus.label.value = "aligned"
    consensus.flat_pct = 0.0
    portfolio = MagicMock()
    portfolio.total_value_usdc = 1000.0
    portfolio.holdings = []
    args = {
        "vault_address": "0x1234567890abcdef1234567890abcdef12345678",
        "trades": [],
        "all_signals": [],
        "market_regime": "bull",
        "consensus": consensus,
        "tick_id": "t714",
        "reasoning": "commit wiring guard",
        "portfolio": portfolio,
        "targets": [],
        "trade_id": b"\x11" * 32,
    }
    args.update(over)
    return args


class TestCommitPhaseUsesTheModernPath:
    """#714: ``_commit_trace`` anchors via ``commit()`` and never via ``publish()``.

    The representative site for the three legacy ``trace_publisher.publish()`` calls
    removed from ``agent_runner.py``. Mocked at the publisher boundary (the repo
    idiom — see the ``runner_env`` fixture), so this pins the call actually made to
    the chain layer rather than any internal helper.
    """

    def test_commit_invokes_commit_never_publish(self, runner_env):
        # CHARACTERIZATION, not a regression guard (CLAUDE.md § "Before you approve a
        # merge" rule 3): this passes against the pre-#714 tree too — the old fallback
        # only ran when supports_commit_reveal() was False, which this test sets True.
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.commit = AsyncMock(return_value=(42, "0xCOMMIT", 100, False))
        mock_tp.publish = AsyncMock(return_value="0xLEGACY")

        trace, trace_id, commit_tx, commit_block, claimed, reverted = asyncio.run(
            runner._commit_trace(**_commit_args())
        )

        mock_tp.commit.assert_awaited_once()
        mock_tp.publish.assert_not_called()  # the retired v1 anchor
        # The tradeId binding reaches the contract call unchanged.
        assert mock_tp.commit.await_args.args[2] == b"\x11" * 32
        # ...and the hash committed is the trace's own canonical hash (untouched here).
        assert mock_tp.commit.await_args.args[0] is trace
        assert (trace_id, commit_tx, commit_block, reverted) == (42, "0xCOMMIT", 100, False)
        assert claimed is not None

    def test_pre_v15_registry_anchors_nothing_and_says_so(self, runner_env, caplog):
        """No commit/reveal on the deployed ABI => a loud absence, not a v1 anchor.

        The removed fallback returned the publishTrace tx as ``commit_tx``, which
        then persisted as ``commit_tx_hash`` despite binding no trade.
        """
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=False)
        mock_tp.commit = AsyncMock(return_value=(42, "0xCOMMIT", 100, False))
        mock_tp.publish = AsyncMock(return_value="0xLEGACY")

        with caplog.at_level("ERROR"):
            result = asyncio.run(runner._commit_trace(**_commit_args()))

        mock_tp.publish.assert_not_called()
        mock_tp.commit.assert_not_called()
        assert "COMMIT SKIPPED" in caplog.text
        # (trace, trace_id, commit_tx, commit_block, claimed, reverted) — no anchor.
        assert result[1:] == (None, None, None, None, False)

    def test_commit_failure_never_escapes_to_the_tick_loop(self, runner_env, caplog):
        """Error-handling semantics preserved: a publisher raise is logged, not raised."""
        # CHARACTERIZATION, not a regression guard (CLAUDE.md § "Before you approve a
        # merge" rule 3): this passes against the pre-#714 tree too — the raise happens
        # inside commit(), so the except wrapper short-circuits before either tree could
        # reach the fallback. It documents the preserved contract, it does not pin it.
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.commit = AsyncMock(side_effect=RuntimeError("rpc down"))
        mock_tp.publish = AsyncMock(return_value="0xLEGACY")

        with caplog.at_level("ERROR"):
            result = asyncio.run(runner._commit_trace(**_commit_args()))

        assert "COMMIT FAILED" in caplog.text
        mock_tp.publish.assert_not_called()  # no silent legacy retry
        assert result[1:] == (None, None, None, None, False)


class TestNoTradeDecisionsAreNeverAnchored:
    """#714 site 3: ``_publish_trace`` is the SKIP/error path and touches no chain write.

    Every call site passes an empty trade list, so there is no tradeId to bind and
    nothing for ``Vault.executeTrade()`` to consume — the trace is recorded off-chain,
    honestly unverified, rather than anchored via the retired v1 path.
    """

    def test_skip_trace_touches_no_publisher_write_and_is_unverified(self, runner_env):
        runner, mock_tp = runner_env
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.publish = AsyncMock(return_value="0xLEGACY")
        mock_tp.commit = AsyncMock(return_value=(42, "0xCOMMIT", 100, False))

        args = _commit_args()
        asyncio.run(
            runner._publish_trace(
                args["vault_address"],
                DecisionType.SKIP,
                "aligned",
                args["portfolio"],
                [],
                [],
                args["market_regime"],
                args["consensus"],
                "t714b",
                "no drift",
            )
        )

        mock_tp.publish.assert_not_called()
        mock_tp.commit.assert_not_called()
        saved = _saved(runner)
        assert saved["arc_tx_hash"] is None
        assert saved["is_verified"] is False
        assert saved["decision_type"] == "skip"


class TestLegacyPublishCallSitesStayGone:
    """The #714 anti-goal gate, enforced as a test rather than a one-off grep.

    Acceptance criterion from the issue: ``grep -n "trace_publisher.publish("
    backend/archimedes/chain/agent_runner.py`` returns 0 matches. Reading the
    source keeps that durable — a future edit that reintroduces the retired v1
    anchor on the tick path fails here instead of silently shipping.
    """

    def test_agent_runner_never_calls_the_v1_publish_anchor(self):
        from pathlib import Path

        from archimedes.chain import agent_runner

        source = Path(agent_runner.__file__).read_text(encoding="utf-8")
        offenders = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(source.splitlines(), start=1)
            if "trace_publisher.publish(" in line
        ]
        assert offenders == [], (
            "agent_runner.py must anchor via commit()/reveal() only (#714); "
            f"found legacy publishTrace call site(s): {offenders}"
        )


class TestPinPathStaysGone:
    """#1526: reveal is hash-only. A future edit that reimports the pin client
    or calls a pin function on the tick path fails here instead of silently
    shipping a dead pin we would then claim.
    """

    def test_agent_runner_does_not_import_or_call_a_pin_client(self):
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "archimedes" / "chain" / "agent_runner.py"
        text = source.read_text(encoding="utf-8")
        needles = (
            "provenance_publisher",
            "pinata_client",
            "pin_public_provenance",
            "pin_json",
        )
        offenders = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(text.splitlines(), start=1)
            if any(needle in line for needle in needles)
        ]
        assert offenders == [], f"agent_runner.py must reveal hash-only (#1526); found pin-path residue: {offenders}"
