"""Reveal reconciliation for dangling commitments (#1276, audit G9).

The repair pass for the failure mode ``TestRevealFailureAfterTradeExecuted``
(#1275) pins: trade + commit land, the reveal does not, and the trace persists
honestly as unverified with a dangling commit. Pre-#1276 no later tick ever came
back for it, so that executed trade's reasoning could never become
on-chain-verifiable.

Hermetic, boundary-mocked exactly like ``test_agent_runner_commit_reveal.py`` /
``test_agent_runner_tick.py``: chain client, executor, trace_publisher,
provider and the Redis state store are all mocked. No network, no RPC, no Redis.

The dangling records under test are NOT hand-written dicts — they are produced by
running the real ``_reveal_trace`` failure path and capturing what it persisted,
so the scan is proven against the actual #1275 output shape rather than against a
test author's idea of it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.chain.agent_runner import _needs_reveal_reconciliation
from archimedes.models.trace import DecisionType, ReasoningTrace


def _make_trace(trace_id: str = "recon-trace-001") -> ReasoningTrace:
    trace = ReasoningTrace(
        id=trace_id,
        vault_address="0x1234567890abcdef1234567890abcdef12345678",
        decision_type=DecisionType.REBALANCE,
        trigger="strategy_signal_drift",
        timestamp=datetime.now(UTC),
        market_context={"regime": "risk_on"},
        portfolio_before={"vault": "0x12345678", "aum_usdc": 1000.0},
        portfolio_after={"intended": True, "target_weights": {"sSPY": 0.6}},
        reasoning="Reveal reconciliation test",
        confidence=0.9,
        trades_executed=[{"symbol": "sSPY", "direction": "buy", "amount": 100.0}],
        strategies_referenced=["faber_001"],
        consulted_paper_hashes=["0001:abc123"],
    )
    trace.compute_hash()
    return trace


@pytest.fixture()
def runner_env():
    """A StrategyRunner with every chain boundary mocked and the live path armed."""
    with (
        patch("archimedes.chain.agent_runner.chain_client"),
        patch("archimedes.chain.agent_runner.chain_executor") as mock_executor,
        patch("archimedes.chain.agent_runner.trace_publisher") as mock_tp,
        patch("archimedes.chain.agent_runner.default_provider"),
        patch("archimedes.chain.agent_runner.AgentStateStore"),
        patch("archimedes.chain.agent_runner.DRY_RUN", False),
    ):
        from archimedes.chain.agent_runner import StrategyRunner

        runner = StrategyRunner()
        runner.state = MagicMock()
        runner.state.save_trace = AsyncMock()
        runner.state.list_recent_traces = AsyncMock(return_value=[])
        # Durable-index reads (#1353) — default to "nothing indexed yet" /
        # "no first-seen marker" so pre-existing scan-only tests are
        # unaffected; individual tests override to exercise the new paths.
        runner.state.list_dangling_reveal_traces = AsyncMock(return_value=[])
        runner.state.get_reveal_reconcile_first_seen = AsyncMock(return_value=None)
        runner.state.seed_reveal_reconcile_first_seen = AsyncMock()
        # Durable-terminal writes/reads (#1403 review) — default to "closes
        # cleanly, nothing already terminal" so pre-existing tests are
        # unaffected; individual tests override to exercise the new paths.
        runner.state.mark_reveal_reconcile_terminal = AsyncMock()
        runner.state.list_reveal_reconcile_terminal_hashes = AsyncMock(return_value=set())
        mock_tp.supports_commit_reveal = MagicMock(return_value=True)
        mock_tp.get_commitment = AsyncMock(return_value=None)
        mock_tp.find_reveal_tx = AsyncMock(return_value=None)
        mock_tp.publish = AsyncMock(return_value=None)
        # Signer pre-check (#1353) default: "can't be determined" — the check
        # is skipped rather than guessed, matching every existing test's
        # commitment fixtures (none carry a "committer" key). AsyncMock since
        # #1412: on the Circle path confirming the signer is a bounded network
        # read of GET /wallets/{WALLET_ID}, so the executor method is async.
        mock_executor.backend_signer_address_confirmed = AsyncMock(return_value=None)
        yield runner, mock_tp


def _dangling_record(runner, mock_tp, *, trace_id: str = "recon-trace-001") -> dict:
    """The REAL persisted output of the #1275 reveal-failure path.

    Runs ``_reveal_trace`` with a reveal that raises (RPC outage / revert) and
    returns the dict it saved: executed trade, dangling commit, no reveal, no
    fabricated verification.
    """
    mock_tp.reveal = AsyncMock(side_effect=RuntimeError("execution reverted: rpc down"))
    asyncio.run(
        runner._reveal_trace(
            _make_trace(trace_id),
            trace_id=42,
            tick_id="t-orig",
            tx_hashes=["0xtrade"],
            commit_tx="0xcommit",
            commit_block=100,
            trade_block=101,
            claimed_execution_time=int(datetime.now(UTC).timestamp()) - 300,
        )
    )
    record = runner.state.save_trace.call_args[0][0]
    runner.state.save_trace.reset_mock()
    # Sanity: this really is the honest-degradation shape #1275 pins.
    assert record["is_verified"] is False
    assert record["reveal_tx_hash"] is None
    assert record["trade_tx_hash"] == "0xtrade"
    assert record["commit_tx_hash"] == "0xcommit"
    return record


def _run_pass(runner, records: list[dict], tick_id: str = "t-recon") -> None:
    runner.state.list_recent_traces = AsyncMock(return_value=records)
    asyncio.run(runner._reconcile_dangling_reveals(tick_id))


def _saved(runner) -> dict:
    return runner.state.save_trace.call_args[0][0]


# ── the scan filter ───────────────────────────────────────────


class TestScanPicksUpExactlyTheDanglingShape:
    """commit_tx_hash NOT NULL and reveal_tx_hash NULL and trade_tx_hash NOT NULL."""

    def test_matches_the_real_1275_failure_record(self, runner_env):
        runner, mock_tp = runner_env
        assert _needs_reveal_reconciliation(_dangling_record(runner, mock_tp)) is True

    @pytest.mark.parametrize(
        ("label", "mutate"),
        [
            # HOUSE-RULE guard demos: each of these MUST NOT match. They are the
            # near-misses the filter exists to exclude, constructed by breaking
            # exactly one leg of the real dangling record.
            ("unrevealed_but_no_trade", lambda r: r.update(trade_tx_hash=None)),
            ("already_revealed", lambda r: r.update(reveal_tx_hash="0xREVEALED")),
            ("no_commit_anchored", lambda r: r.update(commit_tx_hash=None)),
            ("already_terminal", lambda r: r.update(reveal_reconcile_state="terminal")),
            ("already_reconciled", lambda r: r.update(reveal_reconcile_state="reconciled")),
            ("already_reconciled_from_chain", lambda r: r.update(reveal_reconcile_state="reconciled_from_chain")),
        ],
    )
    def test_near_misses_are_excluded(self, runner_env, label, mutate):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mutate(record)
        assert _needs_reveal_reconciliation(record) is False, label

    def test_pending_state_is_still_eligible(self, runner_env):
        """ "pending" is the retried-and-failed state — it must stay in the scan."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        record["reveal_reconcile_state"] = "pending"
        record["reveal_reconcile_attempts"] = 1
        assert _needs_reveal_reconciliation(record) is True

    def test_pass_ignores_non_matching_records_end_to_end(self, runner_env):
        """The guard demo, run through the real pass: a store full of near-misses
        must produce ZERO reveal attempts and ZERO writes."""
        runner, mock_tp = runner_env
        base = _dangling_record(runner, mock_tp)

        no_trade = dict(base, id="no-trade", trade_tx_hash=None)
        revealed = dict(base, id="revealed", reveal_tx_hash="0xREVEALED", is_verified=True)
        no_commit = dict(base, id="no-commit", commit_tx_hash=None)
        terminal = dict(base, id="terminal", reveal_reconcile_state="terminal")

        mock_tp.reveal = AsyncMock(return_value=("0xNEVER", 999))
        _run_pass(runner, [no_trade, revealed, no_commit, terminal])

        mock_tp.reveal.assert_not_awaited()
        mock_tp.get_commitment.assert_not_awaited()
        runner.state.save_trace.assert_not_awaited()


# ── the retry ─────────────────────────────────────────────────


class TestSuccessfulRetry:
    def test_backfills_reveal_and_verification_honestly(self, runner_env):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)

        # Commitment exists on-chain, is NOT yet revealed, window already open.
        mock_tp.get_commitment = AsyncMock(
            return_value={
                "revealed": False,
                "reveal_block": None,
                "claimed_execution_time": int(datetime.now(UTC).timestamp()) - 300,
                "storage_pointer": "",
            }
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        _run_pass(runner, [record])

        # The retry submitted the re-derived canonical bytes for the SAME on-chain id.
        mock_tp.reveal.assert_awaited_once()
        assert mock_tp.reveal.await_args.args[0] == 42
        revealed_trace = mock_tp.reveal.await_args.args[1]
        assert revealed_trace.compute_hash() == record["trace_hash"]

        saved = _saved(runner)
        assert saved["reveal_tx_hash"] == "0xRETRYREVEAL"
        assert saved["reveal_block_number"] == 150
        assert saved["is_verified"] is True
        assert saved["arc_tx_hash"] == "0xRETRYREVEAL"
        # commit(100) < trade(101) <= reveal(150), chain source → a genuine binding.
        assert saved["temporal_binding_valid"] is True
        assert saved["reveal_reconcile_state"] == "reconciled"
        # …and it is closed to any further scan.
        assert _needs_reveal_reconciliation(saved) is False

    def test_binding_stays_false_when_block_ordering_does_not_hold(self, runner_env):
        """A successful retry never asserts a binding it cannot prove: a reveal
        block that predates the trade block is not a valid ordering."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 100))  # < trade_block 101

        _run_pass(runner, [record])

        saved = _saved(runner)
        assert saved["is_verified"] is True  # the reveal really did land…
        assert saved["temporal_binding_valid"] is False  # …but the ordering is not provable

    def test_reuses_the_pinned_cid_as_storage_pointer(self, runner_env):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        record["ipfs_cid"] = "bafyREUSED"
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        _run_pass(runner, [record])

        assert mock_tp.reveal.await_args.kwargs["storage_pointer"] == "bafyREUSED"


class TestFailingRetry:
    def test_increments_the_counter_and_stays_unverified(self, runner_env):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("execution reverted: rpc down again"))

        _run_pass(runner, [record])

        saved = _saved(runner)
        assert saved["reveal_reconcile_attempts"] == 1
        assert saved["reveal_reconcile_state"] == "pending"
        assert "rpc down again" in saved["reveal_reconcile_last_error"]
        # The #1275 honest-degradation contract is untouched by a failed retry.
        assert saved["is_verified"] is False
        assert saved["reveal_tx_hash"] is None
        assert saved["reveal_block_number"] is None
        assert saved["temporal_binding_valid"] is False
        assert saved["commit_tx_hash"] == "0xcommit"
        assert saved["trade_tx_hash"] == "0xtrade"
        # Still eligible — a bounded retry, not a give-up.
        assert _needs_reveal_reconciliation(saved) is True

    def test_reveal_returning_no_tx_counts_as_a_failure(self, runner_env):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(return_value=(None, None))  # publisher swallowed its own error

        _run_pass(runner, [record])

        saved = _saved(runner)
        assert saved["reveal_reconcile_attempts"] == 1
        assert saved["is_verified"] is False

    def test_unreadable_commitment_is_retryable_not_terminal(self, runner_env):
        """Unreadable ≠ absent: an RPC failure must not be read as proof that
        the commitment does not exist."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(return_value=None)
        mock_tp.reveal = AsyncMock(return_value=("0xNEVER", 1))

        _run_pass(runner, [record])

        mock_tp.reveal.assert_not_awaited()  # never transact against an unknown commitment
        saved = _saved(runner)
        assert saved["reveal_reconcile_attempts"] == 1
        assert saved["reveal_reconcile_state"] == "pending"

    def test_window_not_yet_open_consumes_no_attempt(self, runner_env):
        """reveal() reverts before claimedExecutionTime — a wait, not a failure."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={
                "revealed": False,
                "reveal_block": None,
                "claimed_execution_time": int(datetime.now(UTC).timestamp()) + 300,
                "storage_pointer": "",
            }
        )
        mock_tp.reveal = AsyncMock(return_value=("0xNEVER", 1))

        _run_pass(runner, [record])

        mock_tp.reveal.assert_not_awaited()
        runner.state.save_trace.assert_not_awaited()  # nothing changed, nothing burned
        assert _needs_reveal_reconciliation(record) is True


# ── the bound ─────────────────────────────────────────────────


class TestTerminalCap:
    """The failure mode this pass must not become: an infinite silent retry."""

    def test_nth_failure_goes_terminal_loudly_and_is_never_retried(self, runner_env, caplog, monkeypatch):
        runner, mock_tp = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_ATTEMPTS", 3)
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("execution reverted: still broken"))

        # Attempts 1 and 2: pending, retried each tick.
        for expected in (1, 2):
            _run_pass(runner, [record])
            record = _saved(runner)
            assert record["reveal_reconcile_attempts"] == expected
            assert record["reveal_reconcile_state"] == "pending"
            runner.state.save_trace.reset_mock()

        # Attempt 3 (the cap): TERMINAL, loudly.
        with caplog.at_level("ERROR"):
            _run_pass(runner, [record])
        record = _saved(runner)

        assert "REVEAL RECONCILIATION TERMINAL" in caplog.text
        assert record["id"] in caplog.text  # the ERROR names the trace…
        assert "still broken" in caplog.text  # …and why it gave up
        assert record["reveal_reconcile_state"] == "terminal"
        assert record["reveal_reconcile_attempts"] == 3
        assert record["reveal_reconcile_terminal_reason"]

        # Terminal is a STATE, not a deletion — #1275's honest record survives.
        assert record["is_verified"] is False
        assert record["reveal_tx_hash"] is None
        assert record["trade_tx_hash"] == "0xtrade"
        assert record["commit_tx_hash"] == "0xcommit"
        assert record["temporal_binding_valid"] is False

        # HOUSE-RULE guard demo: feed the terminal record straight back in. It
        # must be picked up by NOTHING — no scan match, no chain read, no
        # transaction, no further write.
        assert _needs_reveal_reconciliation(record) is False
        mock_tp.reveal.reset_mock()
        mock_tp.get_commitment.reset_mock()
        runner.state.save_trace.reset_mock()
        _run_pass(runner, [record])
        mock_tp.reveal.assert_not_awaited()
        mock_tp.get_commitment.assert_not_awaited()
        runner.state.save_trace.assert_not_awaited()

    # 0 is the registry's empty sentinel (traces are 1-indexed) and a
    # non-numeric id is unusable — all three mean "no commitment to reveal".
    @pytest.mark.parametrize("bad_id", [None, 0, "not-an-id"])
    def test_missing_onchain_id_is_terminal_immediately(self, runner_env, caplog, bad_id):
        """A publishTrace-fallback anchor (pre-v1.5, #588) has no commitment to
        reveal — it is unrevealable by construction, so it must surface as a
        countable terminal state instead of being retried forever."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        record["onchain_trace_id"] = bad_id
        mock_tp.reveal = AsyncMock(return_value=("0xNEVER", 1))

        with caplog.at_level("ERROR"):
            _run_pass(runner, [record])

        mock_tp.reveal.assert_not_awaited()
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "terminal"
        assert "no on-chain commitment id" in saved["reveal_reconcile_terminal_reason"]
        assert "REVEAL RECONCILIATION TERMINAL" in caplog.text
        assert saved["is_verified"] is False

    def test_canonical_drift_is_terminal_immediately(self, runner_env, caplog):
        """If the persisted bytes no longer re-derive to the committed hash, the
        contract's keccak256 check can never pass — retrying only burns gas."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        record["reasoning"] = "mutated after the commit"  # a HASHED field
        mock_tp.reveal = AsyncMock(return_value=("0xNEVER", 1))

        with caplog.at_level("ERROR"):
            _run_pass(runner, [record])

        mock_tp.reveal.assert_not_awaited()
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "terminal"
        assert "Hash mismatch" in saved["reveal_reconcile_terminal_reason"]
        assert "REVEAL RECONCILIATION TERMINAL" in caplog.text


# ── restart safety ────────────────────────────────────────────


class TestAlreadyRevealedOnChain:
    """The reveal landed; the process died before the DB write (#1276 c.5)."""

    def test_backfills_from_chain_without_a_new_transaction(self, runner_env, caplog):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={
                "revealed": True,
                "reveal_block": 150,
                "claimed_execution_time": 0,
                "storage_pointer": "bafyONCHAIN",
            }
        )
        mock_tp.find_reveal_tx = AsyncMock(return_value="0xONCHAINREVEAL")
        mock_tp.reveal = AsyncMock(return_value=("0xSHOULDNOTHAPPEN", 999))

        with caplog.at_level("WARNING"):
            _run_pass(runner, [record])

        mock_tp.reveal.assert_not_awaited()  # no second reveal — that would revert
        saved = _saved(runner)
        assert saved["reveal_tx_hash"] == "0xONCHAINREVEAL"
        assert saved["reveal_block_number"] == 150
        assert saved["is_verified"] is True
        assert saved["temporal_binding_valid"] is True
        assert saved["ipfs_cid"] == "bafyONCHAIN"
        assert saved["reveal_reconcile_state"] == "reconciled_from_chain"
        assert saved["reveal_source"] == "chain_commitment"
        assert "RECONCILED FROM CHAIN" in caplog.text
        assert _needs_reveal_reconciliation(saved) is False

    def test_no_recoverable_tx_hash_is_recorded_honestly_not_invented(self, runner_env):
        """When the TraceRevealed log can't be served, the on-chain commitment is
        still proof — but the tx hash is left None rather than fabricated."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": True, "reveal_block": 150, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.find_reveal_tx = AsyncMock(return_value=None)
        mock_tp.reveal = AsyncMock(return_value=("0xSHOULDNOTHAPPEN", 999))

        _run_pass(runner, [record])

        saved = _saved(runner)
        assert saved["reveal_tx_hash"] is None
        assert saved["reveal_block_number"] == 150
        assert saved["is_verified"] is True
        assert saved["reveal_source"] == "chain_commitment"

    def test_already_revealed_revert_routes_to_the_chain_backfill(self, runner_env):
        """The race: the commitment reads unrevealed, then our own in-flight tx
        (or another attempt) lands first and reveal() reverts "Already revealed".
        That is a success, not a retry."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            side_effect=[
                {"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""},
                {"revealed": True, "reveal_block": 151, "claimed_execution_time": 0, "storage_pointer": ""},
            ]
        )
        mock_tp.find_reveal_tx = AsyncMock(return_value="0xRACEREVEAL")
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("execution reverted: Already revealed"))

        _run_pass(runner, [record])

        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "reconciled_from_chain"
        assert saved["reveal_tx_hash"] == "0xRACEREVEAL"
        assert saved["reveal_block_number"] == 151
        assert saved["is_verified"] is True
        assert saved.get("reveal_reconcile_attempts", 0) == 0  # not counted as a failure


# ── the gates ─────────────────────────────────────────────────


class TestDryRunAndLeaseGates:
    def test_dry_run_does_not_transact_or_even_scan(self, runner_env, monkeypatch):
        """AGENT_DRY_RUN is honoured exactly as ``_reveal_trace`` honours it —
        reconciliation must never become a dry-run bypass to the chain."""
        runner, mock_tp = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.DRY_RUN", True)
        record = _dangling_record(runner, mock_tp)
        mock_tp.reveal = AsyncMock(return_value=("0xNEVER", 1))

        _run_pass(runner, [record])

        mock_tp.reveal.assert_not_awaited()
        mock_tp.get_commitment.assert_not_awaited()
        runner.state.save_trace.assert_not_awaited()
        runner.state.list_recent_traces.assert_not_awaited()

    def test_lease_not_held_skips_the_pass(self, runner_env, caplog):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        runner.lease = MagicMock(is_valid=False)
        mock_tp.reveal = AsyncMock(return_value=("0xNEVER", 1))

        with caplog.at_level("ERROR"):
            _run_pass(runner, [record])

        assert "LEASE NOT HELD" in caplog.text
        mock_tp.reveal.assert_not_awaited()
        runner.state.save_trace.assert_not_awaited()

    def test_scan_failure_is_loud_and_never_kills_the_tick(self, runner_env, caplog):
        runner, _ = runner_env
        runner.state.list_recent_traces = AsyncMock(side_effect=RuntimeError("redis down"))

        with caplog.at_level("ERROR"):
            asyncio.run(runner._reconcile_dangling_reveals("t-recon"))

        assert "Reveal reconciliation scan FAILED" in caplog.text

    def test_per_tick_batch_is_bounded(self, runner_env, monkeypatch):
        runner, mock_tp = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_PER_TICK", 2)
        # Five genuinely distinct dangling records (the id is a HASHED field, so
        # they must be built through the real path, not copied and relabelled).
        records = [_dangling_record(runner, mock_tp, trace_id=f"dangling-{i}") for i in range(5)]
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        _run_pass(runner, records)

        assert mock_tp.reveal.await_count == 2


class TestTickIntegration:
    """The pass runs at tick start, before any new commit work."""

    def test_tick_runs_reconciliation_first(self, runner_env):
        runner, _ = runner_env
        order: list[str] = []
        runner._reconcile_dangling_reveals = AsyncMock(side_effect=lambda tick_id: order.append("reconcile"))
        runner.provider.list_strategies = MagicMock(side_effect=lambda: order.append("strategies") or [])

        asyncio.run(runner.tick())

        assert order == ["reconcile", "strategies"]

    def test_reconciliation_failure_does_not_abort_the_tick(self, runner_env, caplog):
        """Repairing yesterday's trace must never stop today's rebalance."""
        runner, _ = runner_env
        runner._reconcile_dangling_reveals = AsyncMock(side_effect=RuntimeError("boom"))
        reached = MagicMock(return_value=[])
        runner.provider.list_strategies = reached

        with caplog.at_level("ERROR"):
            asyncio.run(runner.tick())  # must not raise

        assert "Reveal reconciliation pass FAILED" in caplog.text
        reached.assert_called_once()  # the tick carried on past it
        assert "Strategy tick failed" not in caplog.text


# ── signer pre-check (#1353) ─────────────────────────────────


class TestSignerPreCheck:
    """Cheap terminal short-circuit on a rotated committer (#1353).

    ``reveal()`` requires ``msg.sender == committer``; after a key rotation
    the OLD retry loop burned all REVEAL_RECONCILE_MAX_ATTEMPTS on 'Not
    committer' reverts before going terminal. This checks it BEFORE
    transacting so a confirmed mismatch costs zero attempts and zero gas.
    """

    def test_signer_mismatch_goes_terminal_without_consuming_an_attempt(self, runner_env):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={
                "revealed": False,
                "reveal_block": None,
                "claimed_execution_time": 0,
                "storage_pointer": "",
                "committer": "0xOLDKEY000000000000000000000000000000000",
            }
        )
        mock_tp.reveal = AsyncMock(return_value=("0xSHOULDNOTHAPPEN", 999))

        with patch("archimedes.chain.agent_runner.chain_executor") as mock_executor:
            mock_executor.backend_signer_address_confirmed = AsyncMock(
                return_value="0xNEWKEY000000000000000000000000000000000"
            )
            _run_pass(runner, [record])

        mock_tp.reveal.assert_not_awaited()  # zero gas burned on a doomed revert
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "terminal"
        assert "signer mismatch" in saved["reveal_reconcile_terminal_reason"]
        assert "Not committer" in saved["reveal_reconcile_terminal_reason"]
        assert saved.get("reveal_reconcile_attempts", 0) == 0  # NOT counted as a failed attempt
        assert _needs_reveal_reconciliation(saved) is False

    def test_matching_signer_proceeds_normally_case_insensitively(self, runner_env):
        """GUARD-DOESN'T-OVERFIRE demo: a checksum-cased committer that
        matches the configured (differently-cased) address must NOT be
        flagged — the comparison is case-insensitive."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={
                "revealed": False,
                "reveal_block": None,
                "claimed_execution_time": 0,
                "storage_pointer": "",
                "committer": "0xAAAABBBBCCCCDDDDEEEEFFFF000000000000AAAA",
            }
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        with patch("archimedes.chain.agent_runner.chain_executor") as mock_executor:
            mock_executor.backend_signer_address_confirmed = AsyncMock(
                return_value="0xaaaabbbbccccddddeeeeffff000000000000aaaa"
            )
            _run_pass(runner, [record])

        mock_tp.reveal.assert_awaited_once()
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "reconciled"

    def test_undeterminable_signer_skips_the_check_rather_than_guessing(self, runner_env):
        """backend_signer_address_confirmed() returning None (Circle path, or
        raw-key path with no account configured) must fall through to the
        normal path — never terminal on a guess."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={
                "revealed": False,
                "reveal_block": None,
                "claimed_execution_time": 0,
                "storage_pointer": "",
                "committer": "0xSOMECOMMITTER00000000000000000000000000",
            }
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        with patch("archimedes.chain.agent_runner.chain_executor") as mock_executor:
            mock_executor.backend_signer_address_confirmed = AsyncMock(return_value=None)
            _run_pass(runner, [record])

        mock_tp.reveal.assert_awaited_once()
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "reconciled"

    def test_circle_path_key_rotation_goes_terminal_via_the_real_executor(self, runner_env, monkeypatch):
        """#1412 acceptance: a GENUINE key rotation on the Circle path — the
        only signer production uses — now short-circuits to terminal.

        This wires the REAL ``ChainExecutor.backend_signer_address_confirmed``
        into the runner and mocks only the Circle boundary underneath it, so
        it exercises the actual prod path (executor → circle_signer →
        ``GET /wallets/{WALLET_ID}``) rather than restating the runner's own
        `if`. Before #1412 the executor returned None unconditionally here and
        this pre-check was a permanent no-op in prod: the runner fell through
        and burned every REVEAL_RECONCILE_MAX_ATTEMPTS on 'Not committer'
        reverts.

        WALLET_ADDRESS is deliberately set to the OLD committer: if the
        confirmed path ever regressed to reading that operator-maintained
        mirror instead of asking Circle, it would read as a match and this
        test would stop terminaling."""
        from archimedes.chain.executor import ChainExecutor

        monkeypatch.setenv("WALLET_ADDRESS", "0xOLDKEY000000000000000000000000000000000")
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        # The commitment was signed by the OLD Circle wallet key.
        mock_tp.get_commitment = AsyncMock(
            return_value={
                "revealed": False,
                "reveal_block": None,
                "claimed_execution_time": 0,
                "storage_pointer": "",
                "committer": "0xOLDKEY000000000000000000000000000000000",
            }
        )
        mock_tp.reveal = AsyncMock(return_value=("0xSHOULDNOTHAPPEN", 999))

        real_executor = ChainExecutor(loader=MagicMock())
        with (
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
            patch("archimedes.chain.agent_runner.chain_executor") as mock_executor,
        ):
            mock_signer.is_configured = True  # the prod signer path
            # Circle now reports a DIFFERENT wallet address than the committer.
            mock_signer.get_wallet_address = AsyncMock(return_value="0xNEWKEY000000000000000000000000000000000")
            mock_executor.backend_signer_address_confirmed = real_executor.backend_signer_address_confirmed
            _run_pass(runner, [record])

        mock_tp.reveal.assert_not_awaited()  # zero gas burned on a doomed revert
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "terminal"
        assert "signer mismatch" in saved["reveal_reconcile_terminal_reason"]
        # The mismatching address is logged, not swallowed — the operator needs
        # to see which key the backend is signing with now.
        assert "0xNEWKEY000000000000000000000000000000000" in saved["reveal_reconcile_terminal_reason"]
        assert saved.get("reveal_reconcile_attempts", 0) == 0

    def test_circle_path_unconfirmable_still_never_terminals(self, runner_env):
        """Anti-goal guard (#1412, and the standing #1403 review finding): when
        the Circle lookup itself fails, ``backend_signer_address_confirmed``
        returns None and the pre-check must NOT fire. None means "could not
        confirm", never "confirmed" — terminal here is irreversible, so an
        unreachable Circle must leave the reveal on the ordinary retry path.

        Same real-executor wiring as above; only the Circle answer changes."""
        from archimedes.chain.executor import ChainExecutor

        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={
                "revealed": False,
                "reveal_block": None,
                "claimed_execution_time": 0,
                "storage_pointer": "",
                "committer": "0xREALSIGNER00000000000000000000000000000",
            }
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        real_executor = ChainExecutor(loader=MagicMock())
        with (
            patch("archimedes.chain.executor.circle_signer") as mock_signer,
            patch("archimedes.chain.agent_runner.chain_executor") as mock_executor,
        ):
            mock_signer.is_configured = True
            mock_signer.get_wallet_address = AsyncMock(return_value=None)  # Circle unreachable / non-200
            mock_executor.backend_signer_address_confirmed = real_executor.backend_signer_address_confirmed
            _run_pass(runner, [record])

        mock_tp.reveal.assert_awaited_once()  # the retry actually ran — not short-circuited
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "reconciled"

    def test_missing_committer_field_skips_the_check(self, runner_env):
        """A commitment payload without a 'committer' key (e.g. an older
        TracePublisher shape) must not crash or false-positive."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        with patch("archimedes.chain.agent_runner.chain_executor") as mock_executor:
            mock_executor.backend_signer_address_confirmed = AsyncMock(
                return_value="0xANYADDRESS0000000000000000000000000000"
            )
            _run_pass(runner, [record])

        mock_tp.reveal.assert_awaited_once()


# ── durable index (#1353) ─────────────────────────────────────


class TestDurableIndexUnion:
    """The durable index makes the dangling scan exact regardless of trace
    volume — a record the bounded 200-newest scan would miss is still found
    via ``list_dangling_reveal_traces`` (closes #1276 known-limit #2)."""

    def test_record_absent_from_the_bounded_scan_is_still_found_via_the_index(self, runner_env):
        """GUARD DEMO: the bounded scan returns NOTHING (as if the record had
        aged out of the newest-200 window) but the durable index still has
        it — proving the index, not a wider window, is what closes the gap."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        runner.state.list_recent_traces = AsyncMock(return_value=[])  # aged out of the window
        runner.state.list_dangling_reveal_traces = AsyncMock(return_value=[record])  # still indexed
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        asyncio.run(runner._reconcile_dangling_reveals("t-recon"))

        mock_tp.reveal.assert_awaited_once()
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "reconciled"

    def test_duplicate_between_index_and_scan_is_processed_exactly_once(self, runner_env):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        runner.state.list_recent_traces = AsyncMock(return_value=[record])
        runner.state.list_dangling_reveal_traces = AsyncMock(return_value=[record])
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        asyncio.run(runner._reconcile_dangling_reveals("t-recon"))

        mock_tp.reveal.assert_awaited_once()  # not twice

    def test_index_read_failure_degrades_to_scan_only_without_aborting(self, runner_env, caplog):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        runner.state.list_recent_traces = AsyncMock(return_value=[record])
        runner.state.list_dangling_reveal_traces = AsyncMock(side_effect=RuntimeError("redis down"))
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(return_value=("0xRETRYREVEAL", 150))

        with caplog.at_level("ERROR"):
            asyncio.run(runner._reconcile_dangling_reveals("t-recon"))

        assert "Reveal reconciliation index read FAILED" in caplog.text
        mock_tp.reveal.assert_awaited_once()  # the scan-sourced record still got processed


# ── compound-failure max-age guard (#1353) ────────────────────


class TestMaxAgeCompoundFailureGuard:
    """The attempt cap trusts ``reveal_reconcile_attempts``, a field inside
    the SAME save_trace call a broken Redis write path might keep failing.
    ``first_seen_at`` is written independently (HSETNX, set once) and bounds
    age regardless of whether that counter's own persistence ever works."""

    def test_stale_first_seen_terminals_even_with_the_attempt_cap_effectively_disabled(self, runner_env, monkeypatch):
        runner, mock_tp = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_ATTEMPTS", 1000)
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_AGE_SECONDS", 3600)
        record = _dangling_record(runner, mock_tp)
        stale = datetime.now(UTC) - timedelta(hours=2)
        runner.state.get_reveal_reconcile_first_seen = AsyncMock(return_value=stale)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("still broken"))

        _run_pass(runner, [record])

        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "terminal"
        assert "max age exceeded" in saved["reveal_reconcile_terminal_reason"]
        assert "compound-failure guard" in saved["reveal_reconcile_terminal_reason"]
        # The counter never got anywhere near its (effectively disabled) cap —
        # proof this bound is independent of the attempt counter, not a
        # restatement of it.
        assert saved["reveal_reconcile_attempts"] == 1

    def test_fresh_first_seen_does_not_terminal_within_max_age(self, runner_env, monkeypatch):
        """GUARD-DOESN'T-OVERFIRE demo: a record dangling for only seconds
        must stay pending, not jump straight to terminal."""
        runner, mock_tp = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_ATTEMPTS", 1000)
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_AGE_SECONDS", 3600)
        record = _dangling_record(runner, mock_tp)
        runner.state.get_reveal_reconcile_first_seen = AsyncMock(return_value=datetime.now(UTC))
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("still broken"))

        _run_pass(runner, [record])

        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "pending"

    def test_missing_first_seen_skips_the_age_guard_entirely(self, runner_env, monkeypatch):
        """No first-seen marker (e.g. a record written before this index
        existed) → the age guard is silently skipped THIS cycle, falling back
        to the attempt-cap alone. MAX_AGE set to 1s here — it would fire
        instantly if the guard didn't correctly no-op on a missing marker."""
        runner, mock_tp = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_ATTEMPTS", 1000)
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_AGE_SECONDS", 1)
        record = _dangling_record(runner, mock_tp)
        runner.state.get_reveal_reconcile_first_seen = AsyncMock(return_value=None)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("still broken"))

        _run_pass(runner, [record])

        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "pending"

    def test_missing_first_seen_gets_seeded_independently_of_the_blob_save(self, runner_env, monkeypatch):
        """#1403 round-2 review, item 4: a migration-era record with no
        first-seen marker must acquire one on its first reconciliation pass
        — via a small, independent write, not gated on ``save_trace``
        succeeding — so the compound failure (broken blob write path AND a
        pre-existing dangling record) doesn't leave the age guard permanently
        unbounded for it."""
        runner, mock_tp = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_ATTEMPTS", 1000)
        record = _dangling_record(runner, mock_tp)
        runner.state.get_reveal_reconcile_first_seen = AsyncMock(return_value=None)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("still broken"))

        _run_pass(runner, [record])

        runner.state.seed_reveal_reconcile_first_seen.assert_awaited_once_with(record["trace_hash"])

    def test_seed_failure_is_loud_and_does_not_block_the_normal_attempt_cap(self, runner_env, caplog):
        """A failed seed write (e.g. Redis down) must not abort reconciliation
        — the normal attempt-cap path still has to work off the in-memory
        record."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        runner.state.get_reveal_reconcile_first_seen = AsyncMock(return_value=None)
        runner.state.seed_reveal_reconcile_first_seen = AsyncMock(side_effect=RuntimeError("redis down"))
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("still broken"))

        with caplog.at_level("WARNING"):
            _run_pass(runner, [record])

        assert "first-seen SEED FAILED" in caplog.text
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "pending"
        assert saved["reveal_reconcile_attempts"] == 1

    def test_first_seen_read_failure_is_loud_and_does_not_block_the_normal_attempt_cap(self, runner_env, caplog):
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        runner.state.get_reveal_reconcile_first_seen = AsyncMock(side_effect=RuntimeError("redis down"))
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("still broken"))

        with caplog.at_level("WARNING"):
            _run_pass(runner, [record])

        assert "first-seen read FAILED" in caplog.text
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "pending"  # normal attempt-cap path still worked
        assert saved["reveal_reconcile_attempts"] == 1


# ── terminal survives a broken blob write (#1403 review) ──────


class TestTerminalIndexSurvivesABrokenBlobWrite:
    """PR #1403 review finding: the max-age breaker's own doc claims it closes
    a compound failure of 'broken Redis writes AND a broken reveal' — but
    ``_reconcile_terminal`` only ever wrote the terminal STATE inside the
    trace's JSON blob via ``save_trace``. If THAT specific write kept
    failing, the terminal verdict was recomputed correctly every tick but
    never stuck in Redis, so the next tick's scan still saw "pending" and
    retried the actual on-chain ``reveal()`` call unbounded — identical to
    having no breaker at all. ``mark_reveal_reconcile_terminal`` closes the
    durable index with its own small, independent writes so the verdict
    survives even when the blob save keeps failing."""

    def test_terminal_index_write_survives_a_broken_blob_save_and_stops_the_next_ticks_retry(
        self, runner_env, monkeypatch
    ):
        runner, mock_tp = runner_env
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_ATTEMPTS", 1000)
        monkeypatch.setattr("archimedes.chain.agent_runner.REVEAL_RECONCILE_MAX_AGE_SECONDS", 3600)
        record = _dangling_record(runner, mock_tp)
        trace_hash = record["trace_hash"]
        # What Redis actually holds throughout: the blob save below never
        # lands, so this stays "pending" forever — captured BEFORE the pass
        # mutates `record` in place.
        stale_snapshot = dict(record)
        stale = datetime.now(UTC) - timedelta(hours=2)
        runner.state.get_reveal_reconcile_first_seen = AsyncMock(return_value=stale)
        mock_tp.get_commitment = AsyncMock(
            return_value={"revealed": False, "reveal_block": None, "claimed_execution_time": 0, "storage_pointer": ""}
        )
        mock_tp.reveal = AsyncMock(side_effect=RuntimeError("still broken"))
        # The broken write path: the blob save fails EVERY time, every tick.
        runner.state.save_trace = AsyncMock(side_effect=RuntimeError("redis SET broken"))

        _run_pass(runner, [record])  # tick N — age guard fires, goes terminal

        # The durable index write ran, and succeeded, DESPITE the blob save
        # failing right after it in the same call.
        runner.state.mark_reveal_reconcile_terminal.assert_awaited_once_with(trace_hash)
        runner.state.save_trace.assert_awaited_once()  # attempted — and it did fail (swallowed, not fatal)
        assert record["reveal_reconcile_state"] == "terminal"  # computed correctly in memory

        # Tick N+1: Redis hands back the SAME stale ("pending") blob via both
        # the index and the scan backstop — save_trace never landed it — but
        # the durable terminal set (populated by mark_reveal_reconcile_terminal
        # above, independent of that failure) now has this trace_hash.
        mock_tp.reveal.reset_mock()
        runner.state.list_dangling_reveal_traces = AsyncMock(return_value=[])
        runner.state.list_recent_traces = AsyncMock(return_value=[stale_snapshot])
        runner.state.list_reveal_reconcile_terminal_hashes = AsyncMock(return_value={trace_hash})

        asyncio.run(runner._reconcile_dangling_reveals("t-recon-2"))

        # The compound failure no longer retries unbounded: the durable
        # terminal cross-check filtered the stale-blob candidate out before
        # reveal() could ever be attempted again.
        mock_tp.reveal.assert_not_awaited()

    def test_terminal_index_write_failure_is_loud_and_does_not_block_the_blob_save_attempt(self, runner_env, caplog):
        """GUARD-DOESN'T-BLOCK demo: if the durable-index write itself fails
        (e.g. that specific Redis op errors), the code must still attempt the
        blob save — one broken write must not additionally suppress the
        other, independent write."""
        runner, mock_tp = runner_env
        record = _dangling_record(runner, mock_tp)
        runner.state.mark_reveal_reconcile_terminal = AsyncMock(side_effect=RuntimeError("sadd broken"))
        mock_tp.get_commitment = AsyncMock(return_value=None)  # onchain unreadable → terminal path unrelated
        # Force straight to terminal via the "no on-chain commitment id" path.
        record["onchain_trace_id"] = 0

        with caplog.at_level("ERROR"):
            _run_pass(runner, [record])

        assert "Durable terminal-index write FAILED" in caplog.text
        saved = _saved(runner)
        assert saved["reveal_reconcile_state"] == "terminal"  # blob save still ran and captured it
