"""Commit-reveal path tests for TracePublisher (#714 / T0.3).

Hermetic: no testnet, no Circle SDK, no real chain calls — circle_signer and the
chain client / contract loader are mocked at the boundary, mirroring the precedent in
``test_trace_publisher.py`` (per CLAUDE.md §"Mock at boundaries, not internals").

Covers the real ``commit()`` / ``reveal()`` ABI calls the live agent path now uses,
the trace_id parse from the ``TraceCommitted`` event, the ``claimedExecutionTime``
argument, and the graceful fallback when the deployed registry is pre-v1.5 (#588).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.models.trace import DecisionType, ReasoningTrace


def _make_trace(**overrides) -> ReasoningTrace:
    defaults = {
        "id": "test-trace-cr-001",
        "vault_address": "0x1234567890abcdef1234567890abcdef12345678",
        "decision_type": DecisionType.REBALANCE,
        "trigger": "strategy_signal_drift",
        "timestamp": datetime.now(UTC),
        "reasoning": "Commit-reveal unit test trace",
        "confidence": 0.85,
    }
    defaults.update(overrides)
    return ReasoningTrace(**defaults)


@pytest.fixture()
def supported_loader():
    """A loader whose registry ABI exposes commit()/reveal() (v1.5).

    A bare MagicMock auto-creates the ``commit``/``reveal`` attributes, so
    ``supports_commit_reveal()`` (which does ``hasattr(functions, "commit")``) is True.
    The TraceCommitted event decode is stubbed to yield a deterministic trace_id.
    """
    loader = MagicMock()
    loader.trace_registry = MagicMock()
    loader.trace_registry.events.TraceCommitted.return_value.process_log.return_value = {"args": {"traceId": 42}}
    return loader


@pytest.fixture()
def unsupported_loader():
    """A loader whose registry has NO commit()/reveal() (pre-v1.5, #588 pending)."""
    loader = MagicMock()
    loader.trace_registry = MagicMock()
    loader.trace_registry.functions = MagicMock(spec=[])  # hasattr(..., "commit") -> False
    return loader


def _patch_chain(mock_client):
    mock_client.to_checksum = lambda x: x
    mock_client.settings = MagicMock(reasoning_trace_registry_address="0xregistry", chain_id=5042002)
    # The finalizers WAIT for the receipt (#1095) — a bare get_transaction_receipt
    # raises TransactionNotFound on the raw-key path's immediate read.
    mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(
        return_value=MagicMock(blockNumber=100, status=1, logs=[MagicMock()])
    )


class TestCommit:
    def test_commit_circle_path_calls_correct_abi(self, supported_loader):
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xCOMMIT")
            _patch_chain(mock_client)

            from archimedes.chain.trace_publisher import TracePublisher

            publisher = TracePublisher(loader=supported_loader)
            trace = _make_trace()
            trace.compute_hash()
            claimed = 2_000_000_000
            trade_id = b"\x11" * 32

            trace_id, tx, block, reverted = asyncio.run(publisher.commit(trace, claimed, trade_id, b"\x01"))

            assert (trace_id, tx, block, reverted) == (42, "0xCOMMIT", 100, False)
            _, kwargs = mock_signer.execute_contract.call_args
            assert kwargs["abi_function"] == "commit(address,bytes32,uint64,bytes32,bytes)"
            # vault, contentHash, claimedExecutionTime (as str), tradeId, intent
            assert kwargs["abi_params"][0] == trace.vault_address
            assert kwargs["abi_params"][2] == str(claimed)
            assert kwargs["abi_params"][3] == "0x" + trade_id.hex()

    def test_commit_parses_trace_id_from_event(self, supported_loader):
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xCOMMIT")
            _patch_chain(mock_client)
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            trace_id, _, _, _ = asyncio.run(
                TracePublisher(loader=supported_loader).commit(trace, 2_000_000_000, b"\x22" * 32)
            )
            assert trace_id == 42  # decoded from TraceCommitted, not the getTracesByVault fallback

    def test_commit_returns_none_when_registry_pre_v1_5(self, unsupported_loader):
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            _patch_chain(mock_client)
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            result = asyncio.run(TracePublisher(loader=unsupported_loader).commit(trace, 2_000_000_000, b"\x33" * 32))
            assert result == (None, None, None, False)


class TestFinalizeCommitRevertHandling:
    """#714 follow-up: a reverted commit must never surface a stale trace_id.

    A confirmed revert (``status == 0`` — the #1047-class failure mode: the client
    builds a call shape the deployed bytecode doesn't expose) must short-circuit
    straight to ``trace_id=None``, before any id-recovery route runs at all.

    (#1604 removed the ``getTracesByVault()[-1]`` recency fallback these tests
    originally guarded against; ``getTracesByVault`` must now never be reached from
    the commit path under ANY receipt outcome. The assertions below keep watching
    it, so a reintroduction fails here as well as in ``TestCommitIdIsCommitSpecific``.)
    """

    def test_reverted_commit_does_not_fall_back_to_stale_trace_id(self, supported_loader, caplog):
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xREVERTED")
            mock_client.to_checksum = lambda x: x
            mock_client.settings = MagicMock(reasoning_trace_registry_address="0xregistry", chain_id=5042002)
            mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(
                return_value=MagicMock(blockNumber=100, status=0, logs=[])
            )
            # If the buggy fallback fired, it would return this id — belonging to
            # some earlier, unrelated commit for the same vault.
            supported_loader.trace_registry.functions.getTracesByVault.return_value.call = AsyncMock(return_value=[999])
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            with caplog.at_level(logging.WARNING, logger="archimedes.chain.trace_publisher"):
                trace_id, tx, block, reverted = asyncio.run(
                    TracePublisher(loader=supported_loader).commit(trace, 2_000_000_000, b"\x44" * 32)
                )

            assert trace_id is None  # NOT 999 — the confirmed revert must not be masked
            assert tx == "0xREVERTED"  # tx hash still recorded for the diagnostic trail
            assert block == 100
            # A confirmed revert MUST surface as reverted=True: a consumer (e.g.
            # agent_runner's Phase 2 guard) that only checks tx is not None would
            # wrongly treat this reverted-but-truthy tx as a landed commit (#1095
            # review — see agent_runner._commit_trace / commit_reverted).
            assert reverted is True
            supported_loader.trace_registry.functions.getTracesByVault.assert_not_called()
            # The revert must be loud: the commit path logs an INFO success line
            # before the receipt comes back, so without this warning the log
            # stream would claim the commit succeeded.
            assert "reverted on-chain (status=0)" in caplog.text

    def test_successful_commit_with_undecodable_event_never_guesses_newest_id(self, supported_loader, caplog):
        """#1604: a successful receipt whose event won't decode, with every
        commit-specific route unavailable, resolves to a loud None — NOT to the
        vault's newest trace id.

        Pre-#1604 this returned 9 (``getTracesByVault()[-1]``), which is only
        correct by coincidence: it is whichever commit for this vault happens to
        be newest at read time, not this one.
        """
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xOK")
            mock_client.to_checksum = lambda x: x
            mock_client.settings = MagicMock(reasoning_trace_registry_address="0xregistry", chain_id=5042002)
            mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(
                return_value=MagicMock(blockNumber=101, status=1, logs=[])  # no logs -> event decode finds nothing
            )
            supported_loader.trace_registry.functions.getTracesByVault.return_value.call = AsyncMock(
                return_value=[7, 8, 9]
            )
            # Both commit-specific routes come up empty: no matching log in the
            # block, and no outstanding pending commitment.
            supported_loader.trace_registry.events.TraceCommitted.return_value.get_logs = AsyncMock(return_value=[])
            supported_loader.trace_registry.functions.pendingTradeCommitment.return_value.call = AsyncMock(
                return_value=0
            )
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            with caplog.at_level(logging.ERROR, logger="archimedes.chain.trace_publisher"):
                trace_id, tx, block, reverted = asyncio.run(
                    TracePublisher(loader=supported_loader).commit(trace, 2_000_000_000, b"\x55" * 32)
                )

            assert trace_id is None  # NOT 9
            assert tx == "0xOK"
            assert block == 101
            assert reverted is False
            assert "no resolvable trace id" in caplog.text
            supported_loader.trace_registry.functions.getTracesByVault.assert_not_called()


class TestCommitIdIsCommitSpecific:
    """#1604 — the id a commit resolves to must be THIS commit's, never "newest".

    Önder's 2026-08-20 review note (kept out of #1095's scope, forked out of
    #714's close-out): when the receipt's ``TraceCommitted`` event can't be
    decoded, ``_finalize_commit`` used to fall back to
    ``getTracesByVault(vault)[-1]``. With two commits for the same vault in
    flight and the FIRST one's event undecodable, that binds decision A's trace
    to commit B's id — silent provenance mis-attribution, which is strictly worse
    than a loud None on the one surface whose product claim IS provenance.

    The scenario below is the mis-attribution, made hermetic: two commits land in
    the same block for the same vault, A's receipt event is undecodable, and B is
    the newest id for the vault.
    """

    VAULT = "0x1234567890abcdef1234567890abcdef12345678"
    BLOCK = 500
    TX_A = "0xaaa1"
    TX_B = "0xbbb2"
    TRACE_A = 101
    TRACE_B = 102
    TRADE_A = b"\xa1" * 32
    TRADE_B = b"\xb2" * 32

    def _racing_loader(self, hash_a: bytes, hash_b: bytes, *, logs=None):
        """Registry double for the two-commits-in-one-block race.

        ``getTracesByVault`` returns ``[A, B]`` — so the removed recency fallback
        would answer ``B`` (102) for BOTH commits. The block-``BLOCK`` log re-read
        returns both commits' ``TraceCommitted`` entries, deliberately WITHOUT
        honouring ``argument_filters``: that models an RPC that ignores server-side
        topic filtering, and forces the disambiguation to come from the tx-hash
        match rather than from the filter.
        """
        loader = MagicMock()
        loader.trace_registry = MagicMock()
        # A's receipt event is undecodable — the defect's precondition.
        loader.trace_registry.events.TraceCommitted.return_value.process_log.side_effect = ValueError("undecodable")
        entries = (
            logs
            if logs is not None
            else [
                # bytes tx hash (HexBytes shape, as web3 returns it) …
                {"transactionHash": bytes.fromhex("bbb2"), "args": {"traceId": self.TRACE_B, "contentHash": hash_b}},
                # … and a str one, mixed-case with 0x — both must normalize.
                {"transactionHash": "0xAAA1", "args": {"traceId": self.TRACE_A, "contentHash": hash_a}},
            ]
        )
        loader.trace_registry.events.TraceCommitted.return_value.get_logs = AsyncMock(return_value=entries)
        loader.trace_registry.functions.getTracesByVault.return_value.call = AsyncMock(
            return_value=[self.TRACE_A, self.TRACE_B]
        )
        loader.trace_registry.functions.pendingTradeCommitment.return_value.call = AsyncMock(return_value=0)
        return loader

    def _run_commit_a(self, loader, trace):
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value=self.TX_A)
            mock_client.to_checksum = lambda x: x
            mock_client.settings = MagicMock(reasoning_trace_registry_address="0xregistry", chain_id=5042002)
            mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(
                return_value=MagicMock(blockNumber=self.BLOCK, status=1, logs=[MagicMock()])
            )
            from archimedes.chain.trace_publisher import TracePublisher

            return asyncio.run(TracePublisher(loader=loader).commit(trace, 2_000_000_000, self.TRADE_A))

    def test_concurrent_same_vault_commits_do_not_cross_attribute(self):
        """The headline guard: decision A must never receive commit B's id."""
        trace_a = _make_trace(id="decision-A", vault_address=self.VAULT)
        hash_a = bytes.fromhex(trace_a.compute_hash().removeprefix("0x"))
        hash_b = bytes.fromhex("cd" * 32)  # some other decision's content hash
        loader = self._racing_loader(hash_a, hash_b)

        trace_id, tx, block, reverted = self._run_commit_a(loader, trace_a)

        assert trace_id == self.TRACE_A, "decision A must bind to ITS OWN commit's trace id"
        assert trace_id != self.TRACE_B, "the newest-for-vault id belongs to commit B, not to decision A"
        assert (tx, block, reverted) == (self.TX_A, self.BLOCK, False)
        # The re-read is bounded to the single known commit block (the
        # find_reveal_tx precedent) — never an open-ended range.
        _, kwargs = loader.trace_registry.events.TraceCommitted.return_value.get_logs.call_args
        assert kwargs["from_block"] == self.BLOCK
        assert kwargs["to_block"] == self.BLOCK
        assert kwargs["argument_filters"] == {"contentHash": hash_a}
        # And the recency lookup is not merely out-ranked, it is gone.
        loader.trace_registry.functions.getTracesByVault.assert_not_called()

    def test_pending_trade_commitment_binds_when_log_reread_unavailable(self):
        """Route 2: no usable logs, but the caller's own tradeId still binds exactly.

        ``getTracesByVault`` would still answer B here; ``pendingTradeCommitment``
        answers A, because the registry keys the outstanding commitment by the
        tradeId the caller passed in (#589 forbids a second live commitment for
        the same key).
        """
        trace_a = _make_trace(id="decision-A", vault_address=self.VAULT)
        hash_a = bytes.fromhex(trace_a.compute_hash().removeprefix("0x"))
        loader = self._racing_loader(hash_a, b"", logs=[])  # log re-read finds nothing
        loader.trace_registry.functions.pendingTradeCommitment.return_value.call = AsyncMock(return_value=self.TRACE_A)
        loader.trace_registry.functions.getCommitment.return_value.call = AsyncMock(
            return_value=[hash_a, "0xagent", self.VAULT, self.BLOCK, 2_000_000_000, False, 0, ""]
        )

        trace_id, _, _, _ = self._run_commit_a(loader, trace_a)

        assert trace_id == self.TRACE_A
        args, _ = loader.trace_registry.functions.pendingTradeCommitment.call_args
        assert args == (self.VAULT, self.TRADE_A), "must be keyed by THIS commit's tradeId"
        loader.trace_registry.functions.getTracesByVault.assert_not_called()

    def test_pending_pointer_with_foreign_content_hash_is_refused(self, caplog):
        """Adversarial: a stale/foreign pending pointer must NOT be bound.

        Feeds route 2 a candidate whose on-chain commitment carries someone
        else's content hash — exactly the shape the removed fallback would have
        swallowed. The verified route rejects it and returns a loud None.
        """
        trace_a = _make_trace(id="decision-A", vault_address=self.VAULT)
        hash_a = bytes.fromhex(trace_a.compute_hash().removeprefix("0x"))
        foreign_hash = bytes.fromhex("ef" * 32)
        loader = self._racing_loader(hash_a, b"", logs=[])
        loader.trace_registry.functions.pendingTradeCommitment.return_value.call = AsyncMock(return_value=self.TRACE_B)
        loader.trace_registry.functions.getCommitment.return_value.call = AsyncMock(
            return_value=[foreign_hash, "0xagent", self.VAULT, self.BLOCK, 2_000_000_000, False, 0, ""]
        )

        with caplog.at_level(logging.ERROR, logger="archimedes.chain.trace_publisher"):
            trace_id, _, _, _ = self._run_commit_a(loader, trace_a)

        assert trace_id is None, "a commitment whose content hash isn't ours must never be bound"
        assert "refusing to bind" in caplog.text
        assert "no resolvable trace id" in caplog.text

    def test_log_entry_for_a_different_tx_in_the_same_block_is_skipped(self):
        """Only B's log is present in A's commit block — resolve to None, not B."""
        trace_a = _make_trace(id="decision-A", vault_address=self.VAULT)
        trace_a.compute_hash()
        loader = self._racing_loader(
            b"",
            b"",
            logs=[{"transactionHash": "0xBBB2", "args": {"traceId": self.TRACE_B, "contentHash": b""}}],
        )

        trace_id, _, _, _ = self._run_commit_a(loader, trace_a)

        assert trace_id is None
        assert trace_id != self.TRACE_B

    def test_unreadable_receipt_block_does_not_reopen_the_recency_guess(self, caplog):
        """No block number => route 1 can't run; route 2 empty => loud None."""
        trace_a = _make_trace(id="decision-A", vault_address=self.VAULT)
        trace_a.compute_hash()
        loader = self._racing_loader(b"", b"")
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value=self.TX_A)
            mock_client.to_checksum = lambda x: x
            mock_client.settings = MagicMock(reasoning_trace_registry_address="0xregistry", chain_id=5042002)
            mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(side_effect=Exception("RPC timeout"))
            from archimedes.chain.trace_publisher import TracePublisher

            with caplog.at_level(logging.ERROR, logger="archimedes.chain.trace_publisher"):
                trace_id, tx, block, reverted = asyncio.run(
                    TracePublisher(loader=loader).commit(trace_a, 2_000_000_000, self.TRADE_A)
                )

        assert (trace_id, tx, block, reverted) == (None, self.TX_A, None, False)
        assert "no resolvable trace id" in caplog.text
        loader.trace_registry.events.TraceCommitted.return_value.get_logs.assert_not_called()
        loader.trace_registry.functions.getTracesByVault.assert_not_called()


class TestReveal:
    def test_reveal_circle_path_calls_correct_abi(self, supported_loader):
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xREVEAL")
            _patch_chain(mock_client)
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            pointer = ""  # live path is hash-only (#1526); the ABI still forwards whatever we pass

            reveal_tx, block = asyncio.run(
                TracePublisher(loader=supported_loader).reveal(42, trace, storage_pointer=pointer)
            )

            assert (reveal_tx, block) == ("0xREVEAL", 100)
            _, kwargs = mock_signer.execute_contract.call_args
            assert kwargs["abi_function"] == "reveal(uint256,string,bytes)"
            assert kwargs["abi_params"][0] == "42"
            assert kwargs["abi_params"][1] == pointer

    def test_reveal_returns_none_without_trace_id(self, supported_loader):
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            mock_signer.execute_contract = AsyncMock(return_value="0xREVEAL")
            _patch_chain(mock_client)
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            assert asyncio.run(TracePublisher(loader=supported_loader).reveal(None, trace)) == (None, None)

    def test_reveal_returns_none_when_registry_pre_v1_5(self, unsupported_loader):
        with (
            patch("archimedes.chain.trace_publisher.circle_signer") as mock_signer,
            patch("archimedes.chain.trace_publisher.chain_client") as mock_client,
        ):
            mock_signer.is_configured = True
            _patch_chain(mock_client)
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            assert asyncio.run(TracePublisher(loader=unsupported_loader).reveal(42, trace)) == (None, None)


class TestFinalizeReceiptTiming:
    """#1095 review: the finalizers must WAIT for the receipt (the raw-key path
    finalizes immediately after send, where a plain get_transaction_receipt
    raises TransactionNotFound and silently skips the revert check), and an
    unknown receipt status must never default to success."""

    def test_finalize_commit_catches_revert_when_receipt_not_yet_readable(self, supported_loader, caplog):
        with patch("archimedes.chain.trace_publisher.chain_client") as mock_client:
            mock_client.to_checksum = lambda x: x
            # The immediate read raises (tx not yet mined) — the pre-#1095 code
            # swallowed this and fell back to the stale newest-id lookup.
            mock_client.w3.eth.get_transaction_receipt = AsyncMock(side_effect=Exception("TransactionNotFound"))
            mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(
                return_value=MagicMock(blockNumber=77, status=0, logs=[])
            )
            supported_loader.trace_registry.functions.getTracesByVault.return_value.call = AsyncMock(
                return_value=[999]  # the stale id the buggy fallback would return
            )
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            with caplog.at_level(logging.ERROR, logger="archimedes.chain.trace_publisher"):
                trace_id, tx, block, reverted = asyncio.run(
                    TracePublisher(loader=supported_loader)._finalize_commit(trace, "0xRAWSEND", "0xvault")
                )

            assert reverted is True  # the revert is CAUGHT, not skipped
            assert trace_id is None  # never the stale 999
            assert (tx, block) == ("0xRAWSEND", 77)
            supported_loader.trace_registry.functions.getTracesByVault.assert_not_called()
            assert "reverted on-chain (status=0)" in caplog.text

    def test_finalize_commit_unknown_status_warns_instead_of_assuming_success(self, supported_loader, caplog):
        with patch("archimedes.chain.trace_publisher.chain_client") as mock_client:
            mock_client.to_checksum = lambda x: x
            # A receipt with NO status field: unknown must be logged as unknown —
            # the pre-#1095 code defaulted it to 1 (success) silently.
            mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(
                return_value=SimpleNamespace(blockNumber=88, logs=[])
            )
            supported_loader.trace_registry.functions.getTracesByVault.return_value.call = AsyncMock(return_value=[5])
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            with caplog.at_level(logging.WARNING, logger="archimedes.chain.trace_publisher"):
                trace_id, tx, block, reverted = asyncio.run(
                    TracePublisher(loader=supported_loader)._finalize_commit(trace, "0xNOSTATUS", "0xvault")
                )

            assert reverted is False  # documented choice: unknown does not newly block
            assert "revert state unknown" in caplog.text

    def test_finalize_reveal_reverted_returns_none_and_withholds_anchor(self, supported_loader, caplog):
        with patch("archimedes.chain.trace_publisher.chain_client") as mock_client:
            mock_client.to_checksum = lambda x: x
            mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(
                return_value=MagicMock(blockNumber=200, status=0, logs=[])
            )
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            with caplog.at_level(logging.ERROR, logger="archimedes.chain.trace_publisher"):
                reveal_tx, block = asyncio.run(
                    TracePublisher(loader=supported_loader)._finalize_reveal(trace, "0xREVREV")
                )

            # A reverted reveal must never drive is_verified / temporal binding:
            assert (reveal_tx, block) == (None, None)
            assert trace.reveal_tx_hash == "0xREVREV"  # diagnostic trail kept
            assert trace.arc_tx_hash != "0xREVREV"  # canonical anchor withheld
            assert "trace NOT revealed" in caplog.text

    def test_finalize_reveal_success_sets_the_canonical_anchor(self, supported_loader):
        with patch("archimedes.chain.trace_publisher.chain_client") as mock_client:
            mock_client.to_checksum = lambda x: x
            mock_client.w3.eth.wait_for_transaction_receipt = AsyncMock(
                return_value=MagicMock(blockNumber=201, status=1, logs=[])
            )
            from archimedes.chain.trace_publisher import TracePublisher

            trace = _make_trace()
            trace.compute_hash()
            reveal_tx, block = asyncio.run(TracePublisher(loader=supported_loader)._finalize_reveal(trace, "0xGOOD"))

            assert (reveal_tx, block) == ("0xGOOD", 201)
            assert trace.arc_tx_hash == "0xGOOD"
            assert trace.reveal_block_number == 201
