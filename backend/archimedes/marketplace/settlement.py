"""Settlement sweep: Gateway balance → agent wallet → PaymentSplitter.depositToPool → withdraw.

Three cadences:
  Stage A — Gateway → wallet (threshold-based; Circle's fee is charged on top
            of the burn amount, so the request holds back a reserve — see
            ``marketplace.config``).
  Stage B — wallet USDC → depositToPool (per tick interval, 1 USDC min).
  Stage C — PaymentSplitter.withdraw(pool_id, amount) — creator/platform payout.

Stage A+B run automatically per tick via ``sweep_publisher``.  Stage C is
triggered on-demand via the Withdraw button (D3) and may also be called from
``sweep_publisher`` to auto-disburse.

None of the stages ever raises out of ``sweep_publisher``; failures are logged
and returned silently so the tick always completes.
"""

import asyncio
import logging
import os
from decimal import Decimal

from circlekit.client import GatewayClient
from circlekit.wallets import CircleTxExecutor, CircleWalletSigner

from archimedes.marketplace.config import (
    format_usdc,
    gateway_chain,
    gateway_sweep_amount,
    payments_halted,
)

logger = logging.getLogger(__name__)

# Config
SWEEP_WITHDRAW_THRESHOLD_USDC = os.getenv(
    "SWEEP_WITHDRAW_THRESHOLD_USDC",
    "10.0",  # enough that a withdrawal is worth its own transaction
)
SWEEP_MIN_DEPOSIT_RAW = int(os.getenv("SWEEP_MIN_DEPOSIT_RAW", "1000000"))  # 1 USDC, raw 6-dec
GATEWAY_CHAIN = gateway_chain()

_THRESHOLD_RAW = int(Decimal(SWEEP_WITHDRAW_THRESHOLD_USDC) * 10**6)

# TOTAL wall-clock budget for one balance read, retries and backoff included, and
# the attempt count it is split across. Deliberately looser than the async client's
# 3s: nothing here sits behind an ALB health check, and a sweep that reads a stale
# balance is worse than a sweep that waits. What matters is that it is BOUNDED —
# this call previously had no timeout of any kind. RPC_BALANCE_TIMEOUT_SECONDS /
# RPC_BALANCE_ATTEMPTS override.
_RPC_TOTAL_BUDGET_SECONDS = float(os.getenv("RPC_BALANCE_TIMEOUT_SECONDS", "10.0"))
_RPC_ATTEMPTS = int(os.getenv("RPC_BALANCE_ATTEMPTS", "3"))


class SettlementSweeper:
    """Two-cadence settlement sweeper for one publisher's agent wallet.

    Caches ``CircleWalletSigner`` and ``CircleTxExecutor`` by ``wallet_id``
    so the Circle SDK client is initialised once per publisher.
    """

    def __init__(self, settings, payments_dry_run: bool = False):
        self._settings = settings
        # Fail-safe: when dry-run is on, every fund-moving method short-circuits so
        # NO caller can move real value. Gating lives HERE (not only at call sites)
        # so a future caller can't forget it — the manual withdraw endpoint (M1')
        # did exactly that, bypassing PAYMENTS_DRY_RUN on a real on-chain path.
        self._payments_dry_run = payments_dry_run
        self._signers: dict[str, CircleWalletSigner] = {}
        self._executors: dict[str, CircleTxExecutor] = {}

    def _get_signer(self, wallet_id: str, wallet_address: str) -> CircleWalletSigner:
        if wallet_id not in self._signers:
            self._signers[wallet_id] = CircleWalletSigner(
                wallet_id=wallet_id,
                wallet_address=wallet_address,
            )
        return self._signers[wallet_id]

    def _get_executor(self, wallet_id: str, wallet_address: str) -> CircleTxExecutor:
        if wallet_id not in self._executors:
            self._executors[wallet_id] = CircleTxExecutor(
                wallet_id=wallet_id,
                wallet_address=wallet_address,
            )
        return self._executors[wallet_id]

    async def sweep_publisher(self, pub) -> None:
        """Run both sweep stages for *pub*.  Each stage is independently
        try/except'd so one failure never blocks the other."""
        if self._payments_dry_run:
            logger.info(
                "[%s] sweep_publisher: PAYMENTS_DRY_RUN — skipping real settlement", getattr(pub, "strategy_id", "?")
            )
            return
        if payments_halted():
            # #1240 kill switch: read fresh every call (never cached), unlike
            # payments_dry_run above. This sweep runs automatically every tick
            # with no human approval step and moves real USDC (Gateway
            # withdraw -> on-chain gatewayMint -> depositToPool) — exactly the
            # "real money moving without a deploy" class of transfer the
            # switch exists to stop. Gating lives HERE (not only at the
            # tick() call site) for the same reason payments_dry_run is
            # gated here: a future caller can't forget it.
            logger.warning(
                "[%s] sweep_publisher: PAYMENTS_HALT active — skipping real settlement",
                getattr(pub, "strategy_id", "?"),
            )
            return
        if not pub.agent_wallet_id or not pub.gateway_seller_address:
            logger.warning(
                "Skipping sweep for %s — missing agent_wallet_id or gateway_seller_address",
                pub.strategy_id,
            )
            return

        await self._stage_a_gateway_to_wallet(pub)
        await self._stage_b_wallet_to_pool(pub)

    # ── Stage A: Gateway balance → agent wallet ──────────────────────────

    async def _stage_a_gateway_to_wallet(self, pub) -> None:
        """Withdraw available Gateway balance to the agent wallet when the
        balance exceeds the configured threshold."""
        try:
            signer = self._get_signer(pub.agent_wallet_id, pub.gateway_seller_address)
            executor = self._get_executor(pub.agent_wallet_id, pub.gateway_seller_address)

            # GatewayClient wraps both signer + tx_executor for the full
            # withdraw flow (burn-intent signing + on-chain gatewayMint).
            client = GatewayClient(
                chain=GATEWAY_CHAIN,
                signer=signer,
                tx_executor=executor,
            )

            balances = await client.get_gateway_balance()
            if balances.available < _THRESHOLD_RAW:
                logger.info(
                    "[%s] Gateway balance %s below threshold %s USDC — skip withdraw",
                    pub.strategy_id,
                    balances.formatted_available,
                    SWEEP_WITHDRAW_THRESHOLD_USDC,
                )
                return

            # NOT the whole balance: Circle charges its withdrawal fee on top
            # of the burn amount, so requesting all of it is always rejected
            # with "available X, required X+fee" (see marketplace.config).
            try:
                amount, fee_reserve_raw = gateway_sweep_amount(balances.available)
            except ValueError as exc:
                logger.info("[%s] Stage A: %s — skip withdraw", pub.strategy_id, exc)
                return

            result = await client.withdraw(amount=amount, max_fee=fee_reserve_raw)
            logger.info(
                "[%s] Stage A: withdrew %s of %s USDC available (%s held for fee) from Gateway → wallet; tx=%s",
                pub.strategy_id,
                amount,
                balances.formatted_available,
                format_usdc(fee_reserve_raw),
                result.mint_tx_hash,
            )
        except Exception:
            logger.exception("[%s] Stage A (Gateway→wallet) failed", pub.strategy_id)

    # ── Stage B: wallet USDC → PaymentSplitter.depositToPool ─────────────

    async def _stage_b_wallet_to_pool(self, pub) -> None:
        """Deposit the agent wallet's on-chain USDC balance into the
        PaymentSplitter pool."""
        try:
            executor = self._get_executor(pub.agent_wallet_id, pub.gateway_seller_address)

            # Read on-chain USDC balance via CircleTxExecutor - we use
            # the synchronous http client's get_balance pattern, but
            # CircleTxExecutor only provides execute_* methods.  Delegate
            # to an ethers-style RPC call via asyncio.to_thread.
            balance = await asyncio.to_thread(
                self._usdc_balance_of,
                pub.gateway_seller_address,
            )

            if balance < SWEEP_MIN_DEPOSIT_RAW:
                logger.info(
                    "[%s] Stage B: wallet USDC balance %d below min deposit %d — skip",
                    pub.strategy_id,
                    balance,
                    SWEEP_MIN_DEPOSIT_RAW,
                )
                return

            splitter = self._settings.payment_splitter_address
            if not splitter:
                logger.warning("[%s] Stage B: no payment_splitter_address configured", pub.strategy_id)
                return

            # Approve PaymentSplitter to spend USDC from the agent wallet.
            approve_tx = await asyncio.to_thread(
                executor.execute_approve,
                GATEWAY_CHAIN,
                pub.gateway_seller_address,
                splitter,
                balance,
            )
            logger.info(
                "[%s] Stage B: approve(%s, %d) tx=%s",
                pub.strategy_id,
                splitter,
                balance,
                approve_tx,
            )

            # depositToPool(pool_id, amount) via generic contract execution.
            deposit_tx = await asyncio.to_thread(
                executor._submit_and_wait,
                splitter,
                "depositToPool(bytes32,uint256)",
                [pub.pool_id, str(balance)],
            )
            logger.info(
                "[%s] Stage B: depositToPool(pool_id, %d) tx=%s",
                pub.strategy_id,
                balance,
                deposit_tx,
            )
        except Exception:
            logger.exception("[%s] Stage B (wallet→pool) failed", pub.strategy_id)

    # ── Stage C: PaymentSplitter.withdraw(pool_id, amount) ──────────────────

    async def withdraw_publisher(self, pub, amount_raw: int) -> str | None:
        """Stage C: PaymentSplitter.withdraw(pool_id, amount) — creator/platform payout.

        Callable regardless of pool.active (D6 §2.5); caller no longer needs to be
        pool.creator/platform (see PaymentSplitter.sol withdraw docstring).
        """
        if self._payments_dry_run:
            logger.info(
                "[%s] withdraw_publisher: PAYMENTS_DRY_RUN — skipping real withdraw of %d raw",
                getattr(pub, "strategy_id", "?"),
                amount_raw,
            )
            return None
        if payments_halted():
            # #1240 kill switch: this is Stage C, reachable directly from the
            # authenticated Withdraw button (marketplace_routes.py) with no
            # dependency on sweep_publisher — gating the sweep alone left this
            # real PaymentSplitter.withdraw call unprotected. Same "gate in the
            # callee, not just the caller" posture as payments_dry_run above.
            logger.warning(
                "[%s] withdraw_publisher: PAYMENTS_HALT active — skipping real withdraw of %d raw",
                getattr(pub, "strategy_id", "?"),
                amount_raw,
            )
            return None
        try:
            executor = self._get_executor(pub.agent_wallet_id, pub.gateway_seller_address)
            splitter = self._settings.payment_splitter_address
            if not splitter:
                logger.warning("[%s] Stage C: no payment_splitter_address configured", pub.strategy_id)
                return None
            tx = await asyncio.to_thread(
                executor._submit_and_wait,
                splitter,
                "withdraw(bytes32,uint256)",
                [pub.pool_id, str(amount_raw)],
            )
            logger.info("[%s] Stage C: withdraw(pool_id, %d) tx=%s", pub.strategy_id, amount_raw, tx)
            return tx
        except Exception:
            logger.exception("[%s] Stage C (withdraw) failed", pub.strategy_id)
            return None

    async def withdraw_subscriber(
        self, *, circle_wallet_id: str, dcw_address: str, to_wallet: str, sub_id: str
    ) -> str | None:
        """Sweep a subscriber's remaining prepaid-fee USDC out of their custodial
        Circle DCW back to their own wallet.

        This is the subscriber's exit from the interim custodial fee model
        (issue #975): on unsubscribe we return whatever prepaid fee balance
        remains in the platform-controlled DCW to the subscriber's SIWE wallet.
        ERC-20 ``transfer`` executed FROM the DCW (its wallet_id + the platform
        entity secret sign it) — the same executor path as the Stage-B deposit.

        Best-effort: returns None (never raises) so the unsubscribe itself
        always completes; a failed sweep leaves the balance recoverable by a
        later retry.
        """
        if self._payments_dry_run:
            logger.info("withdraw_subscriber: PAYMENTS_DRY_RUN — skipping real return for sub %s", sub_id)
            return None
        if payments_halted():
            # #1240 kill switch: this is the unsubscribe-refund transfer,
            # reachable directly from the authenticated unsubscribe endpoint
            # with no dependency on sweep_publisher — same reasoning as
            # withdraw_publisher above.
            logger.warning("withdraw_subscriber: PAYMENTS_HALT active — skipping real return for sub %s", sub_id)
            return None
        if not circle_wallet_id or not dcw_address or not to_wallet:
            return None
        try:
            balance = await asyncio.to_thread(self._usdc_balance_of, dcw_address)
            if balance <= 0:
                logger.info("withdraw_subscriber: sub %s DCW empty — nothing to return", sub_id)
                return None
            executor = self._get_executor(circle_wallet_id, dcw_address)
            tx = await asyncio.to_thread(
                executor._submit_and_wait,
                self._settings.usdc_address,
                "transfer(address,uint256)",
                [to_wallet, str(balance)],
            )
            logger.info(
                "withdraw_subscriber: returned %d raw USDC from DCW %s → %s tx=%s",
                balance,
                dcw_address[:10],
                to_wallet[:10],
                tx,
            )
            return tx
        except Exception:
            logger.exception("withdraw_subscriber failed for sub %s (balance left recoverable)", sub_id)
            return None

    def _usdc_balance_of(self, address: str) -> int:
        """Read on-chain USDC balance via direct RPC call.

        Uses web3 (already a dependency) rather than pulling in another SDK.
        """
        from requests.exceptions import ConnectionError as _ReqConnectionError
        from requests.exceptions import HTTPError as _ReqHTTPError
        from requests.exceptions import Timeout as _ReqTimeout
        from web3 import Web3
        from web3.providers.rpc.utils import ExceptionRetryConfiguration

        from archimedes.chain.client import RPC_BACKOFF_FACTOR, rpc_retry_policy

        # This runs inside asyncio.to_thread, so an unbounded call does not just
        # stall one sweep — it parks a worker from the default executor pool for
        # as long as the RPC stays dark, and enough of them stall every other
        # to_thread caller in the process. Bound it the same way the async client
        # is bounded: a TOTAL budget, split across a small attempt count, because
        # web3 retries eth_call five times by default and would otherwise turn
        # this into 5x whatever timeout we set. requests takes a bare number
        # (aiohttp needs a ClientTimeout — see chain/client.py).
        rpc_url = os.getenv("RPC_URL", "http://localhost:8545")
        per_attempt, attempts = rpc_retry_policy(_RPC_TOTAL_BUDGET_SECONDS, _RPC_ATTEMPTS)
        w3 = Web3(
            Web3.HTTPProvider(
                rpc_url,
                request_kwargs={"timeout": per_attempt},
                exception_retry_configuration=ExceptionRetryConfiguration(
                    errors=(_ReqConnectionError, _ReqHTTPError, _ReqTimeout),
                    retries=attempts,
                    backoff_factor=RPC_BACKOFF_FACTOR,
                ),
            )
        )
        usdc_addr = Web3.to_checksum_address(self._settings.usdc_address)
        addr = Web3.to_checksum_address(address)

        # Minimal ERC-20 ABI for balanceOf
        abi = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function",
            }
        ]
        contract = w3.eth.contract(address=usdc_addr, abi=abi)
        return contract.functions.balanceOf(addr).call()
