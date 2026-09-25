"""Trace publisher — anchors reasoning traces on-chain.

Implements ITracePublisher from archimedes/interfaces/chain.py.
Publishes keccak256 hashes to ReasoningTraceRegistry on Arc.
"""

from __future__ import annotations

import logging

from archimedes.chain.circle_signer import circle_signer
from archimedes.chain.client import chain_client
from archimedes.chain.contracts import ContractLoader, get_contract_loader
from archimedes.models.trace import ReasoningTrace

logger = logging.getLogger(__name__)


def _normalize_tx_hash(value: object) -> str | None:
    """Lower-cased, 0x-stripped hex for tx-hash comparison, or None.

    Tx hashes reach us as ``HexBytes``, ``bytes``, or ``str`` (with or without
    the ``0x`` prefix, in either case) depending on whether they came from a
    receipt, a log entry, or the Circle signer. Comparing the raw values would
    make an equality check fail on representation alone — which, on the #1604
    lookup, would silently degrade an exact match into "no match".
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return value.hex().lower()
    text = str(value).strip().lower()
    return text.removeprefix("0x") or None


class TracePublisher:
    """Publishes reasoning trace hashes to on-chain ReasoningTraceRegistry.

    Two anchoring paths:
      - ``publish`` → ``publishTrace`` (v1 anchor-after-the-fact). No longer used
        by the agent tick — ``agent_runner`` was migrated off it in #714 — but kept
        for the operator-driven ``POST /api/traces/publish`` route and any external
        caller anchoring a trace that has no covered trade.
      - ``commit`` + ``reveal`` (v1.5 temporal binding): the agent commits the
        trace hash BEFORE the trade and reveals the canonical content AFTER it
        settles. The contract recomputes keccak256 on reveal and enforces
        commit block < execution < reveal block — proving "trace existed before
        the trade". commit/reveal require the v1.5 registry; ``supports_commit_reveal``
        detects whether the deployed ABI exposes them (redeploy gated on #588).
    """

    def __init__(self, loader: ContractLoader | None = None):
        self.loader = loader or get_contract_loader()

    def supports_commit_reveal(self) -> bool:
        """True iff the deployed registry ABI exposes commit() + reveal().

        The v1.5 commit-reveal pair lives in the Solidity source but the deployed
        ABI may still be v1 (publishTrace only) until the registry is redeployed
        (#588). Callers use this to fall back to publishTrace gracefully instead
        of throwing an AttributeError mid-tick.
        """
        try:
            fns = self.loader.trace_registry.functions
            return hasattr(fns, "commit") and hasattr(fns, "reveal")
        except Exception:
            return False

    async def publish(self, trace: ReasoningTrace) -> str | None:
        """Publish a reasoning trace hash on-chain.

        Steps:
          1. trace.compute_hash() → keccak256 hex (32 bytes)
          2. Call ReasoningTraceRegistry.publishTrace(vault, hash, metadata)
          3. Return tx hash
        """
        trace_hash = trace.compute_hash()
        if not trace_hash:
            logger.warning("Trace hash is empty — skipping publish")
            return None

        # keccak256 output is exactly 32 bytes
        trace_hash_bytes = bytes.fromhex(trace_hash.removeprefix("0x"))  # 32 bytes

        # Encode metadata
        metadata = self._encode_metadata(trace)

        vault_addr = chain_client.to_checksum(trace.vault_address)
        registry_addr = chain_client.to_checksum(chain_client.settings.reasoning_trace_registry_address)

        # ── Path 1: Circle Developer-Controlled Wallet ──
        if circle_signer.is_configured:
            try:
                # Circle SDK expects hex strings for bytes/bytes32 types
                trace_hash_hex = "0x" + trace_hash if not trace_hash.startswith("0x") else trace_hash
                metadata_hex = "0x" + metadata.hex() if metadata else "0x"
                logger.info(
                    f"Publishing trace via Circle: vault={vault_addr}, "
                    f"hash={trace_hash_hex[:18]}..., metadata_len={len(metadata)}"
                )
                tx_hash = await circle_signer.execute_contract(
                    contract_address=registry_addr,
                    abi_function="publishTrace(address,bytes32,bytes)",
                    abi_params=[vault_addr, trace_hash_hex, metadata_hex],
                )
                logger.info(f"Trace published via Circle: {tx_hash[:16]}...")
                trace.arc_tx_hash = tx_hash
                return tx_hash
            except Exception as e:
                logger.error(f"Circle publish failed, falling back: {e}")
                # Fall through to raw key path

        # ── Path 2: Raw private key ──
        account = chain_client.settings.agent_account
        if not account:
            logger.warning("No agent account configured — skipping trace publish")
            return None

        registry = self.loader.trace_registry
        nonce = await chain_client.w3.eth.get_transaction_count(account.address, "pending")

        try:
            tx = await registry.functions.publishTrace(vault_addr, trace_hash_bytes, metadata).build_transaction(
                {
                    "from": account.address,
                    "nonce": nonce,
                    "chainId": chain_client.settings.chain_id,
                    "gas": 300_000,
                    "gasPrice": await chain_client.w3.eth.gas_price,
                }
            )

            signed = account.sign_transaction(tx)
            tx_hash_bytes = await chain_client.w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash = tx_hash_bytes.hex()

            logger.info(f"Trace published on-chain: {tx_hash[:16]}...")
            trace.arc_tx_hash = tx_hash
            return tx_hash

        except Exception as e:
            logger.error(f"Failed to publish trace on-chain: {e}")
            return None

    # ── Commit-Reveal (v1.5 temporal binding) ─────────────────────────

    async def commit(
        self,
        trace: ReasoningTrace,
        claimed_execution_time: int,
        trade_id: bytes,
        trade_intent_summary: bytes = b"",
    ) -> tuple[int | None, str | None, int | None, bool]:
        """Commit the trace hash on-chain BEFORE the covered trade executes.

        Calls ``ReasoningTraceRegistry.commit(vault, contentHash, claimedExecutionTime,
        tradeId, tradeIntentSummary)``. The committed ``contentHash`` is keccak256 of
        the trace's canonical JSON — the SAME bytes ``reveal`` will later submit, so
        the on-chain hash binding holds. ``tradeId`` binds this commitment to the
        specific rebalance ``Vault.rebalance()`` will later recompute
        (``keccak256(abi.encode(tokensIn, amountsIn, tokensOut, amountsOut))`` —
        see ``archimedes.chain.executor.compute_trade_id``): the caller MUST derive
        it from the EXACT arrays that will be submitted to
        ``ChainExecutor.execute_trades()``, or the on-chain recompute won't find a
        matching commitment and ``rebalance()`` reverts (#588).

        Args:
            trace: the trace being committed (its hash is computed here if absent).
            claimed_execution_time: unix time the trade is claimed to land at; must be
                strictly after the commit block's timestamp (the contract enforces this).
            trade_id: 32-byte trade identifier matching what ``Vault.rebalance()`` will
                recompute for the covered trade. Must be non-empty (the contract rejects
                ``bytes32(0)``).
            trade_intent_summary: ABI/opaque bytes summarizing intended trades (metadata).

        Returns:
            (trace_id, tx_hash, commit_block, reverted) — trace_id is the on-chain id
            needed to reveal; trace_id/tx_hash/commit_block are None when no tx was
            sent at all (missing commit(), send failure) — NOT on a revert.
            ``reverted`` is True only on a CONFIRMED on-chain revert (status=0) — a
            reverted commit still has a real tx_hash (kept for the diagnostic
            trail), so callers gating further on-chain action MUST check
            ``reverted``, not just ``tx_hash is not None`` (#1095 review). Falls
            back to (None, None, None, False) if the deployed registry has no
            commit() (pre-#588 redeploy) or the send itself fails.
        """
        if not self.supports_commit_reveal():
            logger.warning(
                "Registry ABI has no commit() — deployed contract is pre-v1.5 "
                "(redeploy gated on #588). Falling back to publishTrace anchor."
            )
            return None, None, None, False

        if not trade_id or len(trade_id) != 32:
            raise ValueError(f"trade_id must be 32 bytes, got {len(trade_id) if trade_id else 0}")

        if trade_id == b"\x00" * 32:
            raise ValueError("trade_id must be non-zero (bytes32(0) is rejected on-chain)")

        content_hash = trace.trace_hash or trace.compute_hash()
        content_hash_bytes = bytes.fromhex(content_hash.removeprefix("0x"))  # 32 bytes
        vault_addr = chain_client.to_checksum(trace.vault_address)
        registry_addr = chain_client.to_checksum(chain_client.settings.reasoning_trace_registry_address)

        # ── Path 1: Circle wallet ──
        if circle_signer.is_configured:
            try:
                content_hash_hex = "0x" + content_hash.removeprefix("0x")
                trade_id_hex = "0x" + trade_id.hex()
                intent_hex = "0x" + trade_intent_summary.hex() if trade_intent_summary else "0x"
                tx_hash = await circle_signer.execute_contract(
                    contract_address=registry_addr,
                    abi_function="commit(address,bytes32,uint64,bytes32,bytes)",
                    abi_params=[vault_addr, content_hash_hex, str(claimed_execution_time), trade_id_hex, intent_hex],
                )
                logger.info(f"Trace committed via Circle: {tx_hash[:16]}...")
                return await self._finalize_commit(
                    trace, tx_hash, vault_addr, trade_id=trade_id, content_hash=content_hash_bytes
                )
            except Exception as e:
                logger.error(f"Circle commit failed, falling back: {e}")

        # ── Path 2: Raw private key ──
        account = chain_client.settings.agent_account
        if not account:
            logger.warning("No agent account configured — skipping trace commit")
            return None, None, None, False

        registry = self.loader.trace_registry
        try:
            nonce = await chain_client.w3.eth.get_transaction_count(account.address, "pending")
            tx = await registry.functions.commit(
                vault_addr, content_hash_bytes, claimed_execution_time, trade_id, trade_intent_summary
            ).build_transaction(
                {
                    "from": account.address,
                    "nonce": nonce,
                    "chainId": chain_client.settings.chain_id,
                    "gas": 300_000,
                    "gasPrice": await chain_client.w3.eth.gas_price,
                }
            )
            signed = account.sign_transaction(tx)
            tx_hash_bytes = await chain_client.w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash = tx_hash_bytes.hex()
            logger.info(f"Trace committed on-chain: {tx_hash[:16]}...")
            return await self._finalize_commit(
                trace, tx_hash, vault_addr, trade_id=trade_id, content_hash=content_hash_bytes
            )
        except Exception as e:
            logger.error(f"Failed to commit trace on-chain: {e}")
            return None, None, None, False

    async def _finalize_commit(
        self,
        trace: ReasoningTrace,
        tx_hash: str,
        vault_addr: str,
        trade_id: bytes | None = None,
        content_hash: bytes | None = None,
    ) -> tuple[int | None, str | None, int | None, bool]:
        """Resolve the on-chain trace_id + block from a commit tx receipt.

        Decodes the TraceCommitted event in THIS tx's own receipt to read the
        auto-incremented trace_id. Every fallback below is likewise keyed to this
        specific commit — see ``_resolve_commit_trace_id``. **There is deliberately
        no recency fallback.** Until #1604 an undecodable event fell back to
        ``getTracesByVault(vault)[-1]``, "the newest trace id for the vault": with
        two commits for the same vault in flight, that binds decision A's trace to
        commit B's id — a silent provenance mis-attribution that reads as a
        perfectly plausible id downstream. Per
        ``docs/architectural-principles.md`` § fail-soft, a loud ``None`` beats a
        plausible substitute on a surface whose whole product claim is provenance.

        The 4th return value, ``reverted``, is True only on a CONFIRMED revert
        (receipt.status == 0) — callers that gate further on-chain action (e.g.
        agent_runner's Phase 2 trade execution) must check it in addition to
        ``tx_hash``: a reverted commit still returns a real tx_hash for the
        diagnostic trail, so ``tx_hash is not None`` alone doesn't mean the
        commitment landed (#1095 review). A receipt-fetch failure leaves
        ``reverted`` False (unchanged from pre-revert-check behavior) — we
        can't positively confirm a revert, so this deliberately doesn't newly
        block Phase 2 on transient receipt-read flakiness.

        This method is the ONLY revert check, and it's chain-native: it WAITS
        for the receipt via ``chain_client.w3.eth.wait_for_transaction_receipt``,
        independent of how the tx was sent. The raw-key path lands here
        immediately after ``send_raw_transaction`` — a plain
        ``get_transaction_receipt`` there raises ``TransactionNotFound`` before
        the tx mines and would silently skip this check (#1095 review) — while
        Circle's ``_poll_transaction`` has usually mined the tx already, making
        the wait a no-op. Either way, a tx that reverted on-chain (including one
        Circle reports COMPLETE) is caught here.
        """
        trace.commit_tx_hash = tx_hash
        registry = self.loader.trace_registry
        block_num = None
        trace_id = None
        reverted = False
        try:
            receipt = await chain_client.w3.eth.wait_for_transaction_receipt(tx_hash)
            block_num = (
                receipt.get("blockNumber") if isinstance(receipt, dict) else getattr(receipt, "blockNumber", None)
            )
            trace.commit_block_number = block_num
            receipt_status = receipt.get("status") if isinstance(receipt, dict) else getattr(receipt, "status", None)
            if receipt_status == 0:
                logger.error(f"Commit tx {tx_hash} reverted on-chain (status=0)")
                return None, tx_hash, block_num, True
            if receipt_status is None:
                # Unknown is unknown, never success: keep reverted=False (the
                # documented no-new-blocking choice on receipt flakiness) but
                # say so loudly instead of silently defaulting to success.
                logger.warning(f"Commit receipt for {tx_hash} has no status field — revert state unknown")

            logs = receipt.get("logs", []) if isinstance(receipt, dict) else getattr(receipt, "logs", [])
            for log in logs:
                try:
                    decoded = registry.events.TraceCommitted().process_log(log)
                    trace_id = int(decoded["args"]["traceId"])
                    break
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"Cannot read commit receipt: {e}")

        if trace_id is None:
            trace_id = await self._resolve_commit_trace_id(tx_hash, vault_addr, block_num, trade_id, content_hash)

        if trace_id is None:
            logger.error(
                f"Commit tx {tx_hash} left no resolvable trace id "
                f"(vault={vault_addr}, block={block_num}): the TraceCommitted event was "
                "undecodable and neither the single-block log re-read nor "
                "pendingTradeCommitment() could bind this commit. Returning None — "
                "guessing the vault's newest id would mis-attribute a concurrent "
                "commit's trace (#1604). Reveal is skipped; reconcile from the "
                "on-chain commitment."
            )

        trace.commit_block_number = block_num
        return trace_id, tx_hash, block_num, reverted

    async def _resolve_commit_trace_id(
        self,
        tx_hash: str,
        vault_addr: str,
        block_num: int | None,
        trade_id: bytes | None,
        content_hash: bytes | None,
    ) -> int | None:
        """Recover this commit's trace id when its receipt event won't decode.

        Both routes are keyed to the specific commit, never to "whatever is
        newest for this vault" (#1604):

        1. **Single-block ``TraceCommitted`` re-read matched on our own tx hash.**
           The commit block is already known from the receipt, so the log range is
           one block — no provider rejects that for size (same bound as
           ``find_reveal_tx``). The tx hash then picks OUR log out of the block even
           when a sibling commit for the same vault landed in it.
        2. **``pendingTradeCommitment(vault, tradeId)``.** The registry keys its
           outstanding commitment by the caller's own ``tradeId`` and refuses a
           second live commitment for the same key (``"Pending commitment exists"``,
           #589), so this mapping read is exact — and it survives the case where
           route 1 can't run at all (no block number, or an RPC with no log
           filtering). The candidate is verified against ``getCommitment``'s stored
           content hash before it is trusted, so a stale pointer can't slip through.

        Returns None when neither route can bind the commit; the caller turns that
        into a loud ERROR.
        """
        trace_id = await self._trace_id_from_commit_log(tx_hash, block_num, content_hash)
        if trace_id is not None:
            return trace_id
        return await self._trace_id_from_pending_trade(vault_addr, trade_id, content_hash)

    async def _trace_id_from_commit_log(
        self, tx_hash: str, block_num: int | None, content_hash: bytes | None
    ) -> int | None:
        """Route 1: re-read TraceCommitted in the commit block, match on tx hash."""
        want = _normalize_tx_hash(tx_hash)
        if block_num is None or want is None:
            return None

        registry = self.loader.trace_registry
        kwargs: dict = {"from_block": int(block_num), "to_block": int(block_num)}
        if content_hash:
            # contentHash is an indexed topic — narrows the block scan server-side.
            kwargs["argument_filters"] = {"contentHash": content_hash}
        try:
            logs = await registry.events.TraceCommitted().get_logs(**kwargs)
            for entry in logs or []:
                entry_tx = (
                    entry.get("transactionHash") if isinstance(entry, dict) else getattr(entry, "transactionHash", None)
                )
                if _normalize_tx_hash(entry_tx) != want:
                    continue  # a sibling commit sharing this block — not ours
                args = entry.get("args") if isinstance(entry, dict) else getattr(entry, "args", None)
                raw_id = args.get("traceId") if isinstance(args, dict) else getattr(args, "traceId", None)
                if raw_id is None:
                    continue
                return int(raw_id)
        except Exception as e:
            logger.warning(f"TraceCommitted re-read for {tx_hash} @ block {block_num} failed: {e}")
        return None

    async def _trace_id_from_pending_trade(
        self, vault_addr: str, trade_id: bytes | None, content_hash: bytes | None
    ) -> int | None:
        """Route 2: the registry's own (vault, tradeId) -> traceId mapping."""
        if not trade_id:
            return None

        registry = self.loader.trace_registry
        try:
            candidate = int(await registry.functions.pendingTradeCommitment(vault_addr, trade_id).call())
        except Exception as e:
            logger.warning(f"pendingTradeCommitment({vault_addr}, {trade_id.hex()}) unreadable: {e}")
            return None
        if not candidate:
            return None

        if content_hash:
            commitment = await self.get_commitment(candidate)
            if commitment is None:
                logger.warning(f"Cannot verify pending trace id {candidate} — commitment unreadable")
                return None
            stored = commitment.get("content_hash")
            if not isinstance(stored, (bytes, bytearray)) or bytes(stored) != content_hash:
                logger.error(
                    f"pendingTradeCommitment returned trace id {candidate} whose stored content "
                    "hash is not this commit's — refusing to bind (stale or foreign commitment)"
                )
                return None
        return candidate

    async def reveal(
        self,
        trace_id: int,
        trace: ReasoningTrace,
        storage_pointer: str = "",
    ) -> tuple[str | None, int | None]:
        """Reveal the full canonical trace content AFTER the trade settles.

        Calls ``ReasoningTraceRegistry.reveal(traceId, storagePointer, fullTraceContent)``.
        ``fullTraceContent`` MUST be the exact canonical bytes whose keccak256 equals the
        committed hash; we derive them from ``trace.canonical_json()`` (the same source the
        commit hash was computed from). ``storage_pointer`` is an optional off-chain
        locator recorded for convenience; the hash verification is what binds the
        reveal. Live reveals pass the empty string — we do not pin traces
        (``docs/adr/ipfs-pinning-not-live.md``).

        Returns (reveal_tx_hash, reveal_block) — None on failure or pre-v1.5 registry.
        """
        if not self.supports_commit_reveal():
            logger.warning("Registry ABI has no reveal() — skipping reveal (redeploy gated on #588).")
            return None, None
        if trace_id is None:
            logger.warning("No trace_id to reveal (commit likely failed) — skipping reveal")
            return None, None

        full_content = trace.canonical_json().encode("utf-8")
        registry_addr = chain_client.to_checksum(chain_client.settings.reasoning_trace_registry_address)

        # ── Path 1: Circle wallet ──
        if circle_signer.is_configured:
            try:
                content_hex = "0x" + full_content.hex()
                tx_hash = await circle_signer.execute_contract(
                    contract_address=registry_addr,
                    abi_function="reveal(uint256,string,bytes)",
                    abi_params=[str(trace_id), storage_pointer, content_hex],
                )
                logger.info(f"Trace revealed via Circle: {tx_hash[:16]}...")
                return await self._finalize_reveal(trace, tx_hash)
            except Exception as e:
                logger.error(f"Circle reveal failed, falling back: {e}")

        # ── Path 2: Raw private key ──
        account = chain_client.settings.agent_account
        if not account:
            logger.warning("No agent account configured — skipping trace reveal")
            return None, None

        registry = self.loader.trace_registry
        try:
            nonce = await chain_client.w3.eth.get_transaction_count(account.address, "pending")
            tx = await registry.functions.reveal(trace_id, storage_pointer, full_content).build_transaction(
                {
                    "from": account.address,
                    "nonce": nonce,
                    "chainId": chain_client.settings.chain_id,
                    "gas": 500_000,
                    "gasPrice": await chain_client.w3.eth.gas_price,
                }
            )
            signed = account.sign_transaction(tx)
            tx_hash_bytes = await chain_client.w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash = tx_hash_bytes.hex()
            logger.info(f"Trace revealed on-chain: {tx_hash[:16]}...")
            return await self._finalize_reveal(trace, tx_hash)
        except Exception as e:
            logger.error(f"Failed to reveal trace on-chain: {e}")
            return None, None

    async def _finalize_reveal(self, trace: ReasoningTrace, tx_hash: str) -> tuple[str | None, int | None]:
        """Record reveal tx + block on the trace and return them.

        Mirrors ``_finalize_commit``'s revert check (#1095): waits for the
        receipt and, on a CONFIRMED revert (status == 0), returns (None, None)
        so callers never derive ``is_verified`` / temporal-binding claims from a
        reveal that did not happen. The tx hash stays on ``trace.reveal_tx_hash``
        for the diagnostic trail, but ``arc_tx_hash`` — the canonical anchor —
        is only set on success. A receipt with no status field logs a warning
        and does not block (unknown is unknown), matching the commit path's
        documented choice not to newly block on receipt flakiness.
        """
        trace.reveal_tx_hash = tx_hash
        block_num = None
        try:
            receipt = await chain_client.w3.eth.wait_for_transaction_receipt(tx_hash)
            block_num = (
                receipt.get("blockNumber") if isinstance(receipt, dict) else getattr(receipt, "blockNumber", None)
            )
            status = receipt.get("status") if isinstance(receipt, dict) else getattr(receipt, "status", None)
            if status == 0:
                logger.error(f"Reveal tx {tx_hash} reverted on-chain (status=0) — trace NOT revealed")
                trace.reveal_block_number = block_num
                return None, None
            if status is None:
                logger.warning(f"Reveal receipt for {tx_hash} has no status field — revert state unknown")
        except Exception:
            logger.debug("reveal receipt block lookup failed", exc_info=True)
        trace.arc_tx_hash = tx_hash  # reveal is the canonical anchor tx for this trace
        trace.reveal_block_number = block_num
        return tx_hash, block_num

    async def get_commitment(self, trace_id: int) -> dict | None:
        """Read the registry's commitment record for ``trace_id``.

        The authoritative answer to "did that reveal actually land?" — the
        contract sets ``revealBlock`` inside ``reveal()`` itself, AFTER it has
        re-hashed the submitted content and checked it against the committed
        hash. A non-zero ``revealBlock`` is therefore proof that the content
        was revealed AND verified on-chain, independent of whether our own
        off-chain write of the reveal tx survived (#1276 reconciliation).

        Returns None when the commitment cannot be read — either the registry
        has no such id (``getCommitment`` reverts "No commitment" for an
        unknown id, e.g. after a redeploy) or the RPC call failed. The caller
        CANNOT distinguish those two from here, so an unreadable commitment
        must be treated as "unknown, retry later", never as proof of absence.
        """
        if not self.supports_commit_reveal():
            return None

        registry = self.loader.trace_registry
        try:
            result = await registry.functions.getCommitment(int(trace_id)).call()
        except Exception as e:
            logger.warning(f"getCommitment({trace_id}) unreadable: {e}")
            return None

        reveal_block = int(result[6])
        return {
            "content_hash": result[0],
            "committer": result[1],
            "vault": result[2],
            "commit_block": int(result[3]),
            "claimed_execution_time": int(result[4]),
            "revealed": bool(result[5]),
            "reveal_block": reveal_block or None,
            "storage_pointer": result[7],
        }

    async def find_reveal_tx(self, trace_id: int, reveal_block: int | None) -> str | None:
        """Best-effort lookup of the tx hash that revealed ``trace_id``.

        ``get_commitment`` proves a reveal happened and in which block, but not
        which transaction carried it. The ``TraceRevealed`` event does, and it
        indexes ``traceId`` — and because the block is already known the log
        query is a single-block range, which no RPC provider rejects for size.

        Returns None if the log can't be found or read. Callers must then record
        the reveal WITHOUT a tx hash rather than inventing one: the on-chain
        commitment is still honest proof, a fabricated hash never would be.
        """
        if reveal_block is None:
            return None

        registry = self.loader.trace_registry
        try:
            logs = await registry.events.TraceRevealed().get_logs(
                from_block=int(reveal_block),
                to_block=int(reveal_block),
                argument_filters={"traceId": int(trace_id)},
            )
        except Exception as e:
            logger.warning(f"TraceRevealed log lookup for trace {trace_id} @ block {reveal_block} failed: {e}")
            return None

        for entry in logs or []:
            tx_hash = (
                entry.get("transactionHash") if isinstance(entry, dict) else getattr(entry, "transactionHash", None)
            )
            if tx_hash is None:
                continue
            return tx_hash.hex() if isinstance(tx_hash, (bytes, bytearray)) else str(tx_hash)
        return None

    async def verify(self, trace: ReasoningTrace) -> bool:
        """Verify a trace against its on-chain hash."""
        if not trace.trace_hash:
            return False

        registry = self.loader.trace_registry

        try:
            # Get trace by searching through vault traces
            vault_addr = chain_client.to_checksum(trace.vault_address)
            trace_ids = await registry.functions.getTracesByVault(vault_addr).call()

            if not trace_ids:
                return False

            # Check the most recent traces
            for trace_id in reversed(trace_ids):
                stored = await registry.functions.getTraceById(trace_id).call()
                stored_hash = stored[2]  # bytes32 at index 2

                # Compare
                expected = bytes.fromhex(trace.trace_hash.removeprefix("0x"))  # 32 bytes from keccak256
                if stored_hash == expected:
                    return True

            return False

        except Exception as e:
            logger.error(f"Failed to verify trace: {e}")
            return False

    async def get_trace_count(self, vault_address: str) -> int:
        """Get total published traces for a vault."""
        registry = self.loader.trace_registry

        try:
            vault_addr = chain_client.to_checksum(vault_address)
            ids = await registry.functions.getTracesByVault(vault_addr).call()
            return len(ids)
        except Exception:
            return 0

    async def get_total_trace_count(self) -> int:
        """Get total trace count across all vaults."""
        registry = self.loader.trace_registry
        try:
            return await registry.functions.traceCount().call()
        except Exception:
            return 0

    async def get_trace_by_id(self, trace_id: int) -> dict | None:
        """Get trace details by on-chain ID."""
        registry = self.loader.trace_registry
        try:
            result = await registry.functions.getTraceById(trace_id).call()
            return {
                "agent": result[0],
                "vault": result[1],
                "trace_hash": result[2].hex(),
                "timestamp": result[3],
                "metadata": result[4],
            }
        except Exception as e:
            logger.error(f"Failed to get trace {trace_id}: {e}")
            return None

    async def get_traces_by_vault(self, vault_address: str) -> list[int]:
        """Get on-chain trace IDs for a specific vault."""
        registry = self.loader.trace_registry
        try:
            vault_addr = chain_client.to_checksum(vault_address)
            return await registry.functions.getTracesByVault(vault_addr).call()
        except Exception:
            return []

    async def get_trace_by_tx_hash(self, tx_hash: str) -> dict | None:
        """Get trace details from the TracePublished event in a known tx receipt.

        O(1) verification path — single RPC roundtrip — used by /verify when the
        off-chain trace already remembers its `arc_tx_hash`. Avoids the O(N)
        getTracesByVault → getTraceById scan over a vault's full trace history.

        Returns the same shape as `get_trace_by_id` (agent / vault / trace_hash /
        timestamp / metadata=None) or None if the receipt is missing or the
        TracePublished event cannot be decoded.
        """
        if not tx_hash:
            return None

        registry = self.loader.trace_registry
        try:
            receipt = await chain_client.w3.eth.get_transaction_receipt(tx_hash)
        except Exception as e:
            logger.error(f"Failed to fetch receipt for {tx_hash}: {e}")
            return None

        for log in receipt.logs:
            try:
                decoded = registry.events.TracePublished().process_log(log)
            except Exception:
                continue
            args = decoded["args"]
            trace_hash_raw = args["traceHash"]
            trace_hash_hex = (
                trace_hash_raw.hex() if isinstance(trace_hash_raw, (bytes, bytearray)) else str(trace_hash_raw)
            )
            return {
                "agent": args["agent"],
                "vault": args["vault"],
                "trace_hash": trace_hash_hex,
                "timestamp": args["timestamp"],
                "metadata": None,
                "trace_id": args.get("traceId", 0),
            }

        return None

    def _encode_metadata(self, trace: ReasoningTrace) -> bytes:
        """Encode trace metadata as ABI-encoded bytes for on-chain storage."""
        import json

        metadata_dict = {
            "decision_type": trace.decision_type.value,
            "trigger": trace.trigger,
            "confidence": trace.confidence,
        }
        return json.dumps(metadata_dict).encode("utf-8")


# Singleton
trace_publisher = TracePublisher()
