"""Chain executor — executes on-chain vault operations.

Implements IChainExecutor from archimedes/interfaces/chain.py.
Handles reading portfolio state, executing rebalance trades, and vault creation.
"""

from __future__ import annotations

import contextlib
import logging
import os

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3.contract import AsyncContract

from archimedes.chain.circle_signer import circle_signer
from archimedes.chain.client import chain_client
from archimedes.chain.contracts import ContractLoader, get_contract_loader
from archimedes.models.portfolio import (
    Portfolio,
    PortfolioHolding,
    TradeDirection,
    TradeOrder,
)

logger = logging.getLogger(__name__)

# Minimum USDC-equivalent reserve a pool must hold for the executor to submit
# a swap through it. Prevents doomed trades on completely empty pools.
# Set to $5 for testnet (wallet balance is limited by faucet rate).
# Production would raise this to $1000+.
MIN_HEALTHY_LIQUIDITY_USDC = 5.0

# The #1138 fee-cap constants live in archimedes.chain.constants (a module
# with no import side effects) so schemas and tooling can use them without
# instantiating the chain_executor singleton below.

# Solidity types of Vault.rebalance()'s params, in order — MUST match
# Vault.sol's `rebalance(address[] tokensIn, uint256[] amountsIn,
# address[] tokensOut, uint256[] amountsOut)` exactly, since
# `compute_trade_id` reproduces the contract's own
# `keccak256(abi.encode(tokensIn, amountsIn, tokensOut, amountsOut))` (Vault.sol).
_REBALANCE_ABI_TYPES = ["address[]", "uint256[]", "address[]", "uint256[]"]


def compute_trade_id(
    tokens_in: list[str],
    amounts_in: list[int],
    tokens_out: list[str],
    amounts_out: list[int],
) -> bytes:
    """Compute the tradeId Vault.rebalance() will derive from these exact arrays.

    Mirrors Vault.sol's ``bytes32 tradeId = keccak256(abi.encode(tokensIn, amountsIn,
    tokensOut, amountsOut))`` byte-for-byte (same types, same order). The arrays
    passed here MUST be the identical ones later submitted to
    ``ChainExecutor.execute_trades(..., trade_arrays=...)`` — any divergence (a
    reordered leg, a re-read oracle price) produces a different tradeId and the
    on-chain recompute won't find a matching commitment (#588).

    Returns the raw 32-byte hash (not hex-encoded).
    """
    encoded = abi_encode(_REBALANCE_ABI_TYPES, [tokens_in, amounts_in, tokens_out, amounts_out])
    return keccak(encoded)


class InsufficientLiquidityError(RuntimeError):
    """Raised when an AMM pool's USDC reserve is below MIN_HEALTHY_LIQUIDITY_USDC."""


class OraclePriceUnavailableError(RuntimeError):
    """Raised when a synth's oracle price can't be read for a SELL sizing (#923).

    The old behavior fell back to a 1:1 ($1) price, which on a high-priced synth
    (e.g. sSPY ≈ $600) meant ordering ~price× too many units — bleeding the vault.
    Failing the rebalance is the safe choice: no trade beats a grossly mis-sized one.
    """


class TradeRevertedError(RuntimeError):
    """Raised when a submitted rebalance transaction reverts on-chain (status 0).

    Without this, a reverted rebalance is logged as "sent" and returned as a
    success hash — the agent then records a reasoning trace as if the trade
    executed when it actually failed.
    """


class VaultCreationRevertedError(RuntimeError):
    """Raised when a submitted createVault transaction reverts on-chain (status 0).

    Without this, a reverted createVault is logged as "sent" and the Circle
    path falls through to all_vaults[-1] — returning a pre-existing vault's
    address as the result of a creation call that never happened.
    """


class ChainExecutor:
    """Executes on-chain vault operations: read portfolio, rebalance, create vault."""

    def __init__(self, loader: ContractLoader | None = None):
        self.loader = loader or get_contract_loader()

    async def read_portfolio(self, vault_address: str) -> Portfolio:
        """Read a vault's current holdings from on-chain state.

        Falls back to off-chain NAV computation if totalAssets() reverts
        (e.g. stale oracle prices on testnet).
        """
        vault = self.loader.vault(vault_address)

        # Read holdings from vault contract
        holdings_data = await vault.functions.getHoldings().call()
        token_addresses = holdings_data[0]
        amounts = holdings_data[1]

        # Read total assets — fall back to off-chain NAV if stale price
        total_assets = await self._safe_total_assets(vault, token_addresses, amounts)

        # Value every holding first, then weight against the sum of holding
        # values. Weighting against totalAssets() breaks when the on-chain NAV
        # excludes tokens (e.g. an unwired oracle values a synth at 0 in the
        # contract): USDC alone reads 100% and weights can't sum to 100 (#1080).
        valued: list[tuple[str, str, int, int, int, bool]] = []
        for i in range(len(token_addresses)):
            token_addr = token_addresses[i]
            amount = amounts[i]

            if amount == 0:
                continue

            # Get token symbol from contract
            symbol = await self._get_token_symbol(token_addr)
            decimals = await self._get_token_decimals(token_addr)

            # Calculate USDC value (was_priced=False → value is 0 by construction)
            value_usdc, was_priced = await self._token_to_usdc(token_addr, amount, decimals, total_assets > 0)
            valued.append((symbol, token_addr, amount, decimals, value_usdc, was_priced))

        portfolio_value = sum(value for *_, value, _ in valued)
        denominator = portfolio_value if portfolio_value > 0 else total_assets

        holdings = [
            PortfolioHolding(
                symbol=symbol,
                token_address=token_addr,
                amount=amount / 10**decimals if decimals else float(amount),
                value_usdc=value_usdc / 1e6,  # USDC has 6 decimals
                weight=value_usdc / denominator if denominator > 0 else 0.0,
                priced=was_priced,
            )
            for symbol, token_addr, amount, decimals, value_usdc, was_priced in valued
        ]

        return Portfolio(
            vault_address=vault_address,
            holdings=holdings,
            # Same NAV the weights were computed against (review follow-up):
            # weights use `denominator` (sum of priced holdings, falling back
            # to totalAssets), while trade sizing multiplies drift by
            # total_value_usdc — if this stayed on raw totalAssets(), the
            # #1080 scenario (on-chain NAV excluding an unwired-oracle synth)
            # would size trades against a NAV inconsistent with the weights
            # being diffed. One denominator for both surfaces.
            total_value_usdc=denominator / 1e6,
        )

    async def _validate_trade_liquidity(self, trades: list[TradeOrder]) -> None:
        """Pre-flight: verify AMM pools have sufficient liquidity for proposed trades.

        Reads reserve0/reserve1 from each pool, identifies the USDC-side reserve,
        and raises InsufficientLiquidityError if it's below the threshold.
        """
        usdc_addr = chain_client.to_checksum(chain_client.settings.usdc_address)
        router = self.loader.amm_router

        for trade in trades:
            token_addr = chain_client.to_checksum(trade.token_address)
            # A USDC-denominated leg (e.g. the cash / TREASURY allocation) needs
            # no swap — it is already in the settlement asset. getPool(USDC, USDC)
            # has no pool and would otherwise raise InsufficientLiquidityError,
            # aborting the whole rebalance before any fundable leg is reached
            # (issue #399). Skip it.
            if token_addr == usdc_addr:
                continue
            try:
                pool_addr = await router.functions.getPool(usdc_addr, token_addr).call()
                if pool_addr == "0x" + "0" * 40:
                    raise InsufficientLiquidityError(f"No AMM pool for {trade.symbol}: getPool returned zero address")

                pool = self.loader.amm_pool(pool_addr)
                r0 = await pool.functions.reserve0().call()
                r1 = await pool.functions.reserve1().call()
                t0 = await pool.functions.token0().call()

                # Identify USDC-side reserve (6 decimals)
                usdc_reserve = r0 / 1e6 if chain_client.to_checksum(t0) == usdc_addr else r1 / 1e6

                if usdc_reserve < MIN_HEALTHY_LIQUIDITY_USDC:
                    raise InsufficientLiquidityError(
                        f"Pool {pool_addr[:10]}… ({trade.symbol}): USDC reserve "
                        f"${usdc_reserve:,.2f} below threshold ${MIN_HEALTHY_LIQUIDITY_USDC:,.0f}"
                    )
                logger.debug(
                    "liquidity OK for %s: pool %s USDC reserve $%.2f",
                    trade.symbol,
                    pool_addr[:10],
                    usdc_reserve,
                )
            except InsufficientLiquidityError:
                raise
            except Exception as exc:
                # Fail closed: a probe error (RPC failure, ABI decode error, etc.)
                # means we could NOT determine whether this pool has healthy
                # liquidity — treat that the same as "confirmed insufficient"
                # rather than letting the trade through unguarded. The message
                # is worded distinctly ("probe failed") so logs/operators can
                # tell a probe failure apart from a confirmed-thin-pool result,
                # even though the caller-visible effect (skip this leg) matches.
                logger.warning(
                    "liquidity probe failed for %s — skipping leg (fail-closed): %s",
                    trade.symbol,
                    exc,
                )
                raise InsufficientLiquidityError(
                    f"Liquidity probe failed for {trade.symbol} (treating as insufficient): {exc}"
                ) from exc

    async def _confirm_receipt(self, tx_hash: str | bytes) -> str:
        """Wait for a tx receipt and raise TradeRevertedError if it reverted.

        Mirrors the receipt-wait already done in create_vault. Returns the
        normalized 0x-prefixed hex hash on success.
        """
        if isinstance(tx_hash, str):
            hexstr = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash
            wait_arg = chain_client.w3.to_bytes(hexstr=hexstr)
            norm = hexstr
        else:
            wait_arg = tx_hash
            norm = tx_hash.hex()
            norm = norm if norm.startswith("0x") else "0x" + norm

        receipt = await chain_client.w3.eth.wait_for_transaction_receipt(wait_arg)
        if receipt.get("status") == 0:
            raise TradeRevertedError(f"Rebalance tx reverted on-chain: {norm}")
        return norm

    async def build_trade_arrays(
        self,
        vault_address: str,  # noqa: ARG002 — kept as the vault-context param callers pass; array build is vault-agnostic
        trades: list[TradeOrder],
    ) -> tuple[list[str], list[int], list[str], list[int]]:
        """Build the (tokensIn, amountsIn, tokensOut, amountsOut) arrays for Vault.rebalance().

        This is the EXACT array construction ``Vault.rebalance()`` hashes into its
        tradeId — ``keccak256(abi.encode(tokensIn, amountsIn, tokensOut, amountsOut))``
        (Vault.sol). Callers that need to bind a commit-reveal tradeId to a trade
        (see ``agent_runner._commit_trace``) MUST call this once and reuse the
        returned arrays for BOTH the tradeId computation and the ``execute_trades()``
        submission (via its ``trade_arrays`` param) — do NOT call this twice for the
        same trade. The SELL-leg sizing reads a live oracle price
        (``_usdc_value_to_token_raw``), so two independent calls could read two
        different prices and produce two DIFFERENT tradeId values; the on-chain
        recompute in ``Vault.rebalance()`` would then miss the commitment and revert
        (#588 / commit-before-trade guard).

        Amounts in TradeOrder are in human-readable USDC units (e.g. 8.0 for $8).
        The vault's rebalance() expects raw token amounts:
          - BUY (USDC → synth): amount in USDC raw (6 decimals)
          - SELL (synth → USDC): amount in synth raw (18 decimals)

        Raises InsufficientLiquidityError if a leg's AMM pool lacks liquidity.
        """
        # Drop USDC-denominated legs (cash / TREASURY allocation): holding USDC
        # is already the settlement asset, so there is no swap to make and no
        # USDC/USDC pool to find (issue #399). Filter once so both the liquidity
        # check and the swap construction below see only real swap legs.
        usdc_addr = chain_client.to_checksum(chain_client.settings.usdc_address)
        trades = [t for t in trades if chain_client.to_checksum(t.token_address) != usdc_addr]

        # Pre-flight liquidity check (raises InsufficientLiquidityError)
        await self._validate_trade_liquidity(trades)

        # Split trades into buys and sells with proper decimal conversion
        tokens_in: list[str] = []
        amounts_in: list[int] = []
        tokens_out: list[str] = []
        amounts_out: list[int] = []

        for trade in trades:
            token_addr = chain_client.to_checksum(trade.token_address)
            if trade.direction == TradeDirection.BUY:
                # BUY: amount is USDC to spend → convert to raw (6 decimals)
                tokens_in.append(token_addr)
                amounts_in.append(int(trade.amount * 1e6))
            else:
                # SELL: amount is USDC value → convert to token raw amount via oracle price.
                # Guard a zero/sub-cent estimated value (#923): estimated_usdc_value is
                # rounded to 2dp upstream, so a sub-cent leg arrives as 0.00 and would
                # size to 0 units. Skip it rather than submit a no-op 0-unit SELL.
                if trade.estimated_usdc_value <= 0:
                    logger.warning(
                        "Skipping SELL leg for %s: estimated_usdc_value=%s (zero/sub-cent) — nothing to sell",
                        token_addr,
                        trade.estimated_usdc_value,
                    )
                    continue
                token_raw = await self._usdc_value_to_token_raw(
                    token_addr,
                    trade.estimated_usdc_value,
                )
                if token_raw <= 0:
                    logger.warning(
                        "Skipping SELL leg for %s: $%.4f priced to 0 raw units",
                        token_addr,
                        trade.estimated_usdc_value,
                    )
                    continue
                tokens_out.append(token_addr)
                amounts_out.append(token_raw)

        return tokens_in, amounts_in, tokens_out, amounts_out

    async def execute_trades(
        self,
        vault_address: str,
        trades: list[TradeOrder],
        trade_arrays: tuple[list[str], list[int], list[str], list[int]] | None = None,
    ) -> list[str]:
        """Execute rebalance trades for a vault.

        Uses Circle dev-controlled wallet if configured, falls back to raw private key.
        Pre-flight: validates AMM pool liquidity before submitting (unless
        ``trade_arrays`` is already provided, in which case liquidity was validated
        when those arrays were built).

        Amounts in TradeOrder are in human-readable USDC units (e.g. 8.0 for $8).
        The vault's rebalance() expects raw token amounts:
          - BUY (USDC → synth): amount in USDC raw (6 decimals)
          - SELL (synth → USDC): amount in synth raw (18 decimals)

        Args:
            trade_arrays: optional precomputed ``(tokensIn, amountsIn, tokensOut,
                amountsOut)``, e.g. from a prior ``build_trade_arrays()`` call used to
                derive a commit-reveal tradeId. When provided, it is submitted AS-IS
                (not recomputed) so the tx matches the tradeId already committed
                on-chain. When omitted, the arrays are built fresh from ``trades``.
        """
        if trade_arrays is not None:
            tokens_in, amounts_in, tokens_out, amounts_out = trade_arrays
        else:
            tokens_in, amounts_in, tokens_out, amounts_out = await self.build_trade_arrays(vault_address, trades)

        # Nothing left to swap once the cash/USDC legs are filtered out (or a
        # rebalance that was cash-only to begin with). Submitting an all-empty
        # rebalance([],[],[],[]) still costs gas and publishes a REBALANCE trace
        # that never moved a position, so skip the tx entirely (issue #925).
        if not tokens_in and not tokens_out:
            logger.info(f"SKIP rebalance for {vault_address}: no non-cash legs after filtering")
            return []

        # Circle path
        if circle_signer.is_configured:
            # Convert address[] and uint256[] to ABI-compatible strings
            checksummed_in = [chain_client.to_checksum(t) for t in tokens_in]
            checksummed_out = [chain_client.to_checksum(t) for t in tokens_out]
            tx_hash = await circle_signer.execute_contract(
                contract_address=vault_address,
                abi_function="rebalance(address[],uint256[],address[],uint256[])",
                abi_params=[checksummed_in, amounts_in, checksummed_out, amounts_out],
            )
            confirmed = await self._confirm_receipt(tx_hash)
            logger.info(f"Rebalance tx via Circle confirmed: {confirmed}")
            return [confirmed]

        # Fallback: raw private key
        account = chain_client.settings.agent_account
        if not account:
            raise RuntimeError("No agent account configured — set CIRCLE_API_KEY or ARC_AGENT_PRIVATE_KEY")

        vault = self.loader.vault(vault_address)
        # Use the pending-block nonce so a quick second rebalance doesn't reuse
        # a nonce that's already in the mempool (reduces nonce-collision drops).
        nonce = await chain_client.w3.eth.get_transaction_count(account.address, "pending")

        tx = await vault.functions.rebalance(tokens_in, amounts_in, tokens_out, amounts_out).build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "chainId": chain_client.settings.chain_id,
                "gas": 2_000_000,
                "gasPrice": await chain_client.w3.eth.gas_price,
            }
        )

        signed = account.sign_transaction(tx)
        tx_hash = await chain_client.w3.eth.send_raw_transaction(signed.raw_transaction)

        # Confirm the receipt and raise on revert — don't return a "success"
        # hash for a transaction that failed on-chain.
        confirmed = await self._confirm_receipt(tx_hash)
        logger.info(f"Rebalance tx confirmed: {confirmed}")
        return [confirmed]

    async def create_vault(
        self,
        name: str,
        symbol: str,
        management_fee_bps: int,
        performance_fee_bps: int,
        agent_assisted: bool,
        owner_wallet: str | None = None,
    ) -> str:
        """Deploy a new vault via VaultFactory, then make it NON-CUSTODIAL.

        Uses Circle dev-controlled wallet if configured, falls back to raw private key.

        Non-custodial ownership (T0.2 — re-lands the intent of reverted PR #646):
            The deployed ``VaultFactory.createVault`` has a 5-arg selector and
            constructs the Vault with ``Ownable(msg.sender)`` — i.e. the BACKEND
            SIGNER becomes the vault's owner AND (implicitly) its rebalance
            authority. That is custodial: a single compromised backend key could
            re-point the NAV/slippage oracle and bleed the vault.

            #646 tried to fix this by adding a 6th ``address _vaultOwner`` arg to
            ``createVault``, but the on-chain bytecode only has the 5-arg selector,
            so every call reverted and #646 was reverted in full by #650.

            We re-land the SAME non-custodial outcome WITHOUT touching the deployed
            selector or the Vault constructor: right after creation (while the
            backend signer is still owner) we call ``setAgent(backendAgent)`` and
            then ``transferOwnership(owner_wallet)``. Both functions already exist
            in the live Vault ABI, so nothing can mismatch the deployed bytecode.
            The result is ``owner == depositing user`` and ``agent == backend
            signer`` — the agent keeps rebalance-only authority and can never touch
            the owner-only setters (oracles, slippage cap, pause). See
            ``_apply_non_custodial_ownership`` for the owner-resolution policy
            (user wallet → governance address → fail-loud; NEVER silently the agent).

        Args:
            owner_wallet: The depositing user's wallet, which becomes the vault's
                Ownable owner. Pass the SIWE-verified caller for user-facing
                creation. ``None`` is only valid for backend bootstrap/auto-create,
                where an explicit governance owner is resolved instead (see helper).
        """
        factory = self.loader.vault_factory

        # Circle path
        if circle_signer.is_configured:
            tx_hash = await circle_signer.execute_contract(
                contract_address=chain_client.settings.vault_factory_address,
                abi_function="createVault(string,string,uint16,uint16,bool)",
                abi_params=[name, symbol, management_fee_bps, performance_fee_bps, agent_assisted],
            )
            logger.info(f"Vault created via Circle, tx: {tx_hash}")

            # Wait for confirmation, then find the new vault address
            # Circle's tx_hash is the on-chain hash — parse receipt for VaultCreated event
            receipt = await chain_client.w3.eth.wait_for_transaction_receipt(
                chain_client.w3.to_bytes(hexstr=tx_hash.removeprefix("0x")) if isinstance(tx_hash, str) else tx_hash
            )
            if receipt.get("status") == 0:
                raise VaultCreationRevertedError(f"createVault tx reverted on-chain: {tx_hash}")
            vault_address = self._parse_vault_created(factory, receipt)
            if not vault_address:
                # Tx succeeded but no VaultCreated event was found — fall back
                # to the most recently created vault, but flag it: this masks
                # an event-decoding/indexing issue, not a revert (that's
                # already handled above).
                logger.warning(
                    "createVault tx %s succeeded but no VaultCreated event found; "
                    "falling back to most recent vault from getVaults()",
                    tx_hash,
                )
                all_vaults = await factory.functions.getVaults().call()
                vault_address = all_vaults[-1]
            logger.info(f"Vault created at {vault_address}")

            # Make the vault non-custodial: owner = user (or governance), agent = backend.
            await self._apply_non_custodial_ownership(vault_address, owner_wallet)
            return vault_address

        # Fallback: raw private key
        account = chain_client.settings.agent_account
        if not account:
            raise RuntimeError("No agent account configured — set CIRCLE_API_KEY or ARC_AGENT_PRIVATE_KEY")

        nonce = await chain_client.w3.eth.get_transaction_count(account.address, "pending")

        tx = await factory.functions.createVault(
            name, symbol, management_fee_bps, performance_fee_bps, agent_assisted
        ).build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "chainId": chain_client.settings.chain_id,
                "gas": 5_000_000,
                "gasPrice": await chain_client.w3.eth.gas_price,
            }
        )

        signed = account.sign_transaction(tx)
        tx_hash = await chain_client.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = await chain_client.w3.eth.wait_for_transaction_receipt(tx_hash)
        vault_address = self._parse_vault_created(factory, receipt)

        if not vault_address:
            raise RuntimeError("Failed to extract vault address from deployment receipt")

        logger.info(f"Vault created at {vault_address} in tx {tx_hash.hex()}")

        # Make the vault non-custodial: owner = user (or governance), agent = backend.
        await self._apply_non_custodial_ownership(vault_address, owner_wallet)
        return vault_address

    def backend_signer_address(self) -> str | None:
        """The EVM address the backend uses to sign vault/registry txs (the agent).

        Circle path: the managed wallet's EVM address, surfaced via WALLET_ADDRESS
        (same env var the bootstrap script already reads for setAgent). Raw-key
        path: the address derived from ARC_AGENT_PRIVATE_KEY. Returns None if it
        can't be determined (Circle configured but WALLET_ADDRESS unset).

        Public (promoted from ``_backend_signer_address`` for #1353): the reveal
        reconciliation signer pre-check in ``agent_runner.py`` needs the same
        "what address does this backend sign with" answer this vault-creation
        path already computed — one source of truth rather than a second,
        possibly-drifting derivation.

        CAUTION (#1403 review): on the Circle path this is WALLET_ADDRESS, an
        env var kept in sync with the wallet's true address BY HAND (see
        .env.example) — signing itself goes through WALLET_ID, a completely
        separate env var (circle_signer.py). A stale WALLET_ADDRESS is
        harmless here (only ever used defensively, for an owner≠agent
        `==` check). A caller that needs the address to actually be correct —
        not just configured — must use ``backend_signer_address_confirmed``
        instead.
        """
        if circle_signer.is_configured:
            addr = os.getenv("WALLET_ADDRESS", "").strip()
            return addr or None
        account = chain_client.settings.agent_account
        return account.address if account else None

    async def backend_signer_address_confirmed(self) -> str | None:
        """Like ``backend_signer_address``, but ONLY when the answer is
        established from what actually signs — never an operator-maintained
        mirror that can silently drift out of sync with it (#1403 review of
        #1353's signer pre-check).

        Raw-key path: the address IS the signer, derived straight from
        ``ARC_AGENT_PRIVATE_KEY`` — always confirmed, no I/O.

        Circle path: signing is keyed on WALLET_ID (circle_signer.py), while
        the EVM address is *also* surfaced through a hand-maintained
        WALLET_ADDRESS env var (.env.example: "WALLET_ID is the Circle wallet
        UUID, WALLET_ADDRESS is its EVM address") that nothing re-derives from
        the wallet. This method ignores that mirror entirely and asks Circle
        what WALLET_ID signs with (``CircleSigner.get_wallet_address`` →
        ``GET /wallets/{WALLET_ID}``), so the answer is bound to the signing
        identifier rather than to an operator's bookkeeping (#1412).

        Returns None when the answer cannot be established — Circle
        unreachable, non-200, no address in the payload, or (raw-key path) no
        configured account. **None means "could not confirm", never
        "confirmed".** The caller acts IRREVERSIBLY on a mismatch (the reveal
        reconciliation pre-check goes terminal), so it must skip the check
        entirely on None rather than treat it as evidence of anything; see
        CLAUDE.md § fail-soft — a plausible substitute here would permanently
        kill a recoverable dangling reveal.

        Async because the Circle answer is a bounded network read
        (``_WALLET_LOOKUP_TIMEOUT``); before #1412 this returned None
        unconditionally on the Circle path, which made the pre-check a
        permanent no-op in production (the runner env file refuses to write
        without Circle credentials, so Circle is the only prod signer).
        """
        if circle_signer.is_configured:
            return await circle_signer.get_wallet_address()
        account = chain_client.settings.agent_account
        return account.address if account else None

    def _resolve_vault_owner(self, owner_wallet: str | None) -> str | None:
        """Decide who should own the vault — and refuse to default to the agent.

        Priority:
          1. ``owner_wallet`` — the depositing user (user-facing creation). Always
             preferred; this is the whole point of non-custodial vaults.
          2. ``ARC_VAULT_GOVERNANCE_ADDRESS`` — an explicit cold/governance key for
             backend bootstrap/auto-create where no user wallet exists. Documented
             in .env.example; intended to be a key DISTINCT from the hot agent
             signer.
          3. ``None`` — neither is available. The caller MUST NOT silently leave the
             vault owned by the agent (that re-creates the custodial drain vector).
             We return None and the helper logs a loud warning + skips the transfer
             rather than mint an agent-owned vault behind the operator's back.
        """
        if owner_wallet and owner_wallet.strip():
            return owner_wallet.strip()
        gov = os.getenv("ARC_VAULT_GOVERNANCE_ADDRESS", "").strip()
        return gov or None

    async def _apply_non_custodial_ownership(self, vault_address: str, owner_wallet: str | None) -> None:
        """Separate the vault's owner from its agent so a compromised agent key
        cannot drain it (T0.2).

        After ``createVault`` the backend signer is the vault's Ownable owner AND
        the de-facto rebalancer. This method, run by that same signer while it is
        still owner:

          1. ``setAgent(backendAgent)`` — pin the rebalance-only authority to the
             backend signer explicitly (it stays the agent after the handoff).
          2. ``transferOwnership(resolvedOwner)`` — hand the owner role (oracles,
             slippage cap, pause, setAgent) to the depositing user, or to an
             explicit governance key for bootstrap. After this, ``owner != agent``.

        Fail-safe: if no non-agent owner can be resolved (no user wallet AND no
        governance address), we DO NOT transfer ownership — but we log a prominent
        warning. Leaving the freshly-created vault owned by the backend signer is
        the status quo (no regression), and refusing to invent an owner avoids
        bricking a vault by transferring it to a wrong address. Operators see the
        warning and set ARC_VAULT_GOVERNANCE_ADDRESS to close the gap.

        On-chain failures here are logged but NOT raised: the vault already exists
        and is usable; a failed ownership handoff is an operational alert, not a
        reason to 500 the create call (and never a reason to leave funds at risk —
        no user funds are in the vault yet at creation time).
        """
        agent_addr = self.backend_signer_address()
        resolved_owner = self._resolve_vault_owner(owner_wallet)

        if not resolved_owner:
            logger.warning(
                "Vault %s created WITHOUT an owner≠agent handoff: no user wallet was "
                "supplied and ARC_VAULT_GOVERNANCE_ADDRESS is unset. The vault is "
                "owned by the backend signer (custodial). Set "
                "ARC_VAULT_GOVERNANCE_ADDRESS (a cold key distinct from the agent) "
                "to make bootstrap/auto-created vaults non-custodial.",
                vault_address,
            )
            return

        if resolved_owner.lower() == (agent_addr or "").lower():
            # Defensive: never hand ownership to the agent address — that is the
            # exact custodial configuration we are removing.
            logger.warning(
                "Refusing to transferOwnership of vault %s to the agent address %s "
                "(owner would equal agent — custodial). Configure a distinct "
                "ARC_VAULT_GOVERNANCE_ADDRESS or pass a user wallet.",
                vault_address,
                resolved_owner,
            )
            return

        try:
            # 1) Pin the agent (rebalance-only authority) to the backend signer.
            if agent_addr:
                await self._send_vault_admin_tx(
                    vault_address,
                    raw_fn_name="setAgent",
                    raw_fn_args=(chain_client.to_checksum(agent_addr),),
                    circle_sig="setAgent(address)",
                    circle_params=[chain_client.to_checksum(agent_addr)],
                )

            # 2) Hand ownership to the user/governance — owner != agent after this.
            await self._send_vault_admin_tx(
                vault_address,
                raw_fn_name="transferOwnership",
                raw_fn_args=(chain_client.to_checksum(resolved_owner),),
                circle_sig="transferOwnership(address)",
                circle_params=[chain_client.to_checksum(resolved_owner)],
            )
            logger.info(
                "Vault %s is now non-custodial: owner=%s, agent=%s",
                vault_address,
                resolved_owner,
                agent_addr,
            )
        except Exception:
            # Non-fatal: the vault exists and holds no funds yet. Surface loudly so
            # an operator can re-run the handoff, but don't fail the create call.
            logger.exception(
                "owner≠agent handoff failed for vault %s (vault created but still "
                "backend-owned — re-run the handoff before users deposit)",
                vault_address,
            )

    async def _send_vault_admin_tx(
        self,
        vault_address: str,
        *,
        raw_fn_name: str,
        raw_fn_args: tuple,
        circle_sig: str,
        circle_params: list,
    ) -> None:
        """Submit a single owner-only admin tx to a vault via whichever signer is
        configured (Circle managed wallet, else raw key), confirming it on-chain.

        Used by the non-custodial ownership handoff for setAgent /
        transferOwnership. Raises (TradeRevertedError) on an on-chain revert so the
        caller can surface a failed handoff.
        """
        if circle_signer.is_configured:
            tx_hash = await circle_signer.execute_contract(
                contract_address=vault_address,
                abi_function=circle_sig,
                abi_params=circle_params,
            )
            await self._confirm_receipt(tx_hash)
            return

        account = chain_client.settings.agent_account
        if not account:
            raise RuntimeError("No agent account configured for vault admin tx")

        vault = self.loader.vault(vault_address)
        nonce = await chain_client.w3.eth.get_transaction_count(account.address, "pending")
        fn = getattr(vault.functions, raw_fn_name)(*raw_fn_args)
        tx = await fn.build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "chainId": chain_client.settings.chain_id,
                "gas": 200_000,
                "gasPrice": await chain_client.w3.eth.gas_price,
            }
        )
        signed = account.sign_transaction(tx)
        tx_hash = await chain_client.w3.eth.send_raw_transaction(signed.raw_transaction)
        await self._confirm_receipt(tx_hash)

    async def get_vault_metrics(self, vault_address: str) -> dict:
        """Read vault metrics from on-chain state."""
        vault = self.loader.vault(vault_address)

        try:
            total_assets = await vault.functions.totalAssets().call()
        except Exception:
            # Stale price fallback — compute from holdings + raw oracle prices
            try:
                holdings_data = await vault.functions.getHoldings().call()
                token_addresses = holdings_data[0]
                amounts = holdings_data[1]
                total_assets = await self._safe_total_assets(vault, token_addresses, amounts)
            except Exception:
                total_assets = 0
        try:
            total_supply = await vault.functions.totalSupply().call()
        except Exception:
            total_supply = 0

        try:
            hwm = await vault.functions.highWaterMark().call()
        except Exception:
            hwm = 0
        try:
            creator = await vault.functions.creator().call()
        except Exception:
            creator = "0x0000000000000000000000000000000000000000"
        try:
            tier = await vault.functions.tier().call()
        except Exception:
            tier = 2
        # Fee reads degrade to None (unknown), NOT 0 — rendering an unreadable
        # vault as "0% fees" on a trust surface is a lie, and the #1138 guard
        # needs to distinguish "free" from "couldn't verify" to fail closed.
        try:
            mgmt_fee_bps = await vault.functions.managementFeeBps().call()
        except Exception:
            mgmt_fee_bps = None
        try:
            perf_fee_bps = await vault.functions.performanceFeeBps().call()
        except Exception:
            perf_fee_bps = None
        try:
            is_agent = await vault.functions.isAgentAssisted().call()
        except Exception:
            is_agent = False
        try:
            paused = await vault.functions.paused().call()
        except Exception:
            paused = False

        share_price = total_assets / total_supply if total_supply > 0 else 1e6  # 1 USDC

        return {
            "vault_address": vault_address,
            "total_aum_usdc": total_assets / 1e6,
            "total_supply": total_supply,
            "share_price_usdc": share_price / 1e6,
            "high_water_mark": hwm,
            "creator": creator,
            "tier": tier,
            "management_fee_bps": mgmt_fee_bps,
            "performance_fee_bps": perf_fee_bps,
            "is_agent_assisted": is_agent,
            "paused": paused,
        }

    async def get_vault_owner(self, vault_address: str) -> str | None:
        """Read the vault's on-chain Ownable owner — the authoritative controller.

        The owner (not the immutable ``creator``, which is the deploy signer for
        vaults minted through ``createVault``) is who currently controls the
        vault after any ``transferOwnership``. Returns the address string, or
        ``None`` if it can't be read so the caller can fail closed rather than
        assume ownership.
        """
        try:
            vault = self.loader.vault(vault_address)
            owner = await vault.functions.owner().call()
            return str(owner)
        except Exception as exc:
            logger.warning("Could not read on-chain owner for vault %s: %s", vault_address, exc)
            return None

    async def get_vault_fee_bps(self, vault_address: str) -> tuple[int, int]:
        """Read (managementFeeBps, performanceFeeBps) from the vault contract.

        Raises on any read failure — this is the read behind the #1138 fee
        guard, and its callers must fail CLOSED (refuse the vault) when the
        fees can't be verified. No silent zero-default here; contrast with
        get_vault_metrics, whose display path degrades to None instead.
        """
        vault = self.loader.vault(vault_address)
        mgmt_fee_bps = await vault.functions.managementFeeBps().call()
        perf_fee_bps = await vault.functions.performanceFeeBps().call()
        return int(mgmt_fee_bps), int(perf_fee_bps)

    async def get_all_vaults(self) -> list[str]:
        """Get all vault addresses from VaultFactory."""
        factory = self.loader.vault_factory
        vaults = await factory.functions.getVaults().call()
        return list(vaults)

    async def get_vault_count(self) -> int:
        factory = self.loader.vault_factory
        return await factory.functions.vaultCount().call()

    async def set_token_oracles(
        self,
        vault_address: str,
        tokens: list[str],
        oracles: list[str],
    ) -> str:
        """Set oracle addresses on a vault for NAV pricing.

        Uses Circle dev-controlled wallet if configured, falls back to raw private key.
        """
        checksummed_tokens = [chain_client.to_checksum(t) for t in tokens]
        checksummed_oracles = [chain_client.to_checksum(o) for o in oracles]

        # Circle path
        if circle_signer.is_configured:
            tx_hash = await circle_signer.execute_contract(
                contract_address=vault_address,
                abi_function="setTokenOracles(address[],address[])",
                abi_params=[checksummed_tokens, checksummed_oracles],
            )
            logger.info(f"setTokenOracles via Circle: {tx_hash} for vault {vault_address}")
            return tx_hash

        # Fallback: raw private key
        account = chain_client.settings.agent_account
        if not account:
            raise RuntimeError("No agent account configured")

        vault = self.loader.vault(vault_address)
        # Use the pending-block nonce so a second raw-key tx submitted in the
        # same tick (e.g. set_target_allocations right after this) doesn't reuse
        # a nonce already sitting in the mempool and silently drop one of them.
        # Matches execute_rebalance / _send_vault_admin_tx in this file.
        nonce = await chain_client.w3.eth.get_transaction_count(account.address, "pending")

        tx = await vault.functions.setTokenOracles(
            checksummed_tokens,
            checksummed_oracles,
        ).build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "chainId": chain_client.settings.chain_id,
                "gas": 500_000,
                "gasPrice": await chain_client.w3.eth.gas_price,
            }
        )

        signed = account.sign_transaction(tx)
        tx_hash = await chain_client.w3.eth.send_raw_transaction(signed.raw_transaction)

        logger.info(f"setTokenOracles tx sent: {tx_hash.hex()} for vault {vault_address}")
        return tx_hash.hex()

    async def set_target_allocations(
        self,
        vault_address: str,
        tokens: list[str],
        weights_bps: list[int],
    ) -> str:
        """Set target allocations on a vault via setTargetAllocations.

        Uses Circle dev-controlled wallet if configured, falls back to raw private key.
        """
        checksummed = [chain_client.to_checksum(t) for t in tokens]

        # Circle path
        if circle_signer.is_configured:
            tx_hash = await circle_signer.execute_contract(
                contract_address=vault_address,
                abi_function="setTargetAllocations(address[],uint256[])",
                abi_params=[checksummed, weights_bps],
            )
            logger.info(f"setTargetAllocations via Circle: {tx_hash} for vault {vault_address}")
            await self._write_price_baseline_if_absent(vault_address, checksummed, weights_bps)
            return tx_hash

        # Fallback: raw private key
        account = chain_client.settings.agent_account
        if not account:
            raise RuntimeError("No agent account configured — set CIRCLE_API_KEY or ARC_AGENT_PRIVATE_KEY")

        vault = self.loader.vault(vault_address)
        # Pending-block nonce (see set_token_oracles): when _process_vault calls
        # this right after set_token_oracles, the first tx is still in the
        # mempool and "latest" would hand back its nonce, dropping one tx.
        nonce = await chain_client.w3.eth.get_transaction_count(account.address, "pending")

        tx = await vault.functions.setTargetAllocations(
            checksummed,
            weights_bps,
        ).build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "chainId": chain_client.settings.chain_id,
                "gas": 500_000,
                "gasPrice": await chain_client.w3.eth.gas_price,
            }
        )

        signed = account.sign_transaction(tx)
        tx_hash = await chain_client.w3.eth.send_raw_transaction(signed.raw_transaction)

        logger.info(f"setTargetAllocations tx sent: {tx_hash.hex()} for vault {vault_address}")
        await self._write_price_baseline_if_absent(vault_address, checksummed, weights_bps)
        return tx_hash.hex()

    async def _write_price_baseline_if_absent(
        self,
        vault_address: str,
        tokens: list[str],
        weights_bps: list[int],
    ) -> None:
        """Persist the oracle-price baseline vault_service._compute_returns reads (#1103).

        THIS is the writer the reader (``vault_service._compute_returns``, key
        ``vault:prices:{vault_address}``) actually reads — PR #1152 wrote a
        DIFFERENT key (``oracle:prices:latest``, global, keyed by symbol,
        holding LATEST prices) that nothing downstream ever consumed, so every
        vault fell through to a fabricated ASSET_EXPECTED_RETURN + MD5-noise
        number. That mismatch is what this function fixes.

        ``setTargetAllocations`` is called every agent tick (see
        ``agent_runner.py`` / ``marketplace/service.py``), not once at vault
        creation, so this write uses Redis ``SET NX`` — it only ever takes
        effect the FIRST time a vault's allocations become known. Overwriting
        the baseline on every subsequent tick would make creation_price drift
        to match current_price and pin computed returns at ~0% forever, which
        is a different bug, not a fix (do not "fix" this by writing LATEST
        prices here).

        Prices are read the SAME way ``_compute_returns`` reads "current"
        prices (raw on-chain oracle value / 1e6) so the two sides of the
        later comparison are apples-to-apples.

        Fail-soft: ANY failure here (Redis unreachable, an oracle revert for
        one token, etc.) is logged and swallowed — the on-chain
        setTargetAllocations transaction has already succeeded and must never
        be undone or blocked by a best-effort cache write. A vault that ends
        up with no baseline just reads back as "returns unavailable"
        downstream, never as a fabricated number — that asymmetry is the
        entire point of #1103.
        """
        try:
            import json as _json

            import redis.asyncio as _aioredis

            from archimedes.services.price_baseline import baseline_key, normalize_token

            usdc = chain_client.settings.usdc_address.lower()
            addr_to_symbol = {addr.lower(): sym for sym, addr in chain_client.settings.synth_addresses.items()}

            redis_url = (
                os.getenv("REDIS_URL")
                or f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}/0"
            )
            r = _aioredis.from_url(redis_url, decode_responses=True)
            try:
                # A baseline is immutable once written (SET NX below) — when one
                # already exists there is nothing to do, so skip the per-token
                # oracle RPC reads entirely instead of re-pricing every tick and
                # discarding the result (#1201 review).
                if await r.exists(baseline_key(vault_address)):
                    return

                # Snapshot keys are ALWAYS the normalized (lowercase) token
                # address — the reader looks tokens up the same way
                # (price_baseline module), so a checksummed caller-side address
                # can never make a priced token read back as "no baseline"
                # (#1201 review).
                #
                # COMPLETE-OR-NOTHING (#1201 review): the reader makes the WHOLE
                # vault unavailable if any allocated token is missing from the
                # snapshot, and SET NX means the first write stands forever — so
                # persisting a partial snapshot after a transient oracle blip
                # would permanently foreclose the vault's returns with no retry.
                # A failed/unknown token therefore aborts the WRITE (leaving no
                # key), and the next tick gets a fresh chance at a complete one.
                #
                # KNOWN LIMITATION (documented, #1201 review): the baseline is
                # one-shot — a vault that later rebalances into a token absent
                # from its first-tick baseline reads back as returns-unavailable
                # permanently (honest, never fabricated, but not self-healing).
                # Extending an existing baseline when NEW tokens enter the
                # allocation set is a deliberate follow-up, not a drive-by edit:
                # the extension price is read at extension time, which is a
                # different epoch than the original baseline.
                # strict=True: both callers build tokens/weights in lockstep today, but a
                # future desync must fail loudly here rather than silently truncate the
                # completeness check's view (#1201 review).
                expected = {normalize_token(t) for t, w in zip(tokens, weights_bps, strict=True) if w > 0}
                prices: dict[str, float] = {}
                for token_lower in expected:
                    try:
                        if token_lower == usdc:
                            prices[token_lower] = 1.0
                            continue
                        symbol = addr_to_symbol.get(token_lower)
                        if symbol is None:
                            # No oracle exists for this token — the baseline can
                            # never be complete; the vault reads back honestly
                            # as unavailable without a useless partial key.
                            logger.warning(
                                "no oracle mapping for token %s on vault %s — baseline not written",
                                token_lower,
                                vault_address,
                            )
                            return
                        oracle = self.loader.oracle_for(symbol)
                        raw = await oracle.functions.price().call()
                        prices[token_lower] = raw / 1e6
                    except Exception:
                        logger.info(
                            "baseline price read failed for token %s on vault %s — "
                            "baseline not written this tick (will retry next tick)",
                            token_lower,
                            vault_address,
                            exc_info=True,
                        )
                        return

                if not prices:
                    return

                wrote = await r.set(baseline_key(vault_address), _json.dumps(prices), nx=True)
                if wrote:
                    logger.info("Wrote price baseline for vault %s (%d tokens)", vault_address, len(prices))
            finally:
                with contextlib.suppress(Exception):
                    await r.aclose()
        except Exception:
            logger.debug("vault price baseline write failed for %s (non-fatal)", vault_address, exc_info=True)

    async def _usdc_value_to_token_raw(
        self,
        token_address: str,
        usdc_value: float,
    ) -> int:
        """Convert a USDC value to raw token amount using oracle price.

        Args:
            token_address: The synth token address.
            usdc_value: Value in USDC units (e.g. 8.0 for $8).

        Returns:
            Raw token amount in the token's native decimals (18 for synths).
        """
        # Check if it's USDC itself
        if token_address.lower() == chain_client.settings.usdc_address.lower():
            return int(usdc_value * 1e6)

        # Look up oracle price for the synth token
        loader = self.loader
        for sym, addr in chain_client.settings.synth_addresses.items():
            if addr and addr.lower() == token_address.lower():
                try:
                    oracle = loader.oracle_for(sym)
                    price_raw = await oracle.functions.price().call()  # 6 decimals
                except Exception as e:
                    # Do NOT fall back to a 1:1 ($1) price (#923): on a high-priced
                    # synth (sSPY ≈ $600) that orders ~price× too many units and
                    # bleeds the vault. Fail the rebalance instead.
                    logger.error("Oracle price lookup failed for %s (%s) — failing rebalance: %s", sym, addr, e)
                    raise OraclePriceUnavailableError(
                        f"Cannot size SELL for {sym}: oracle price read failed ({e})"
                    ) from e
                price_usd = price_raw / 1e6  # e.g. 592.40 for sSPY
                if price_usd <= 0:
                    logger.error(
                        "Oracle returned non-positive price for %s (raw=%s) — failing rebalance", sym, price_raw
                    )
                    raise OraclePriceUnavailableError(
                        f"Cannot size SELL for {sym}: oracle price is non-positive ({price_raw})"
                    )
                token_amount = usdc_value / price_usd
                return int(token_amount * 1e18)  # synths have 18 decimals

        # No oracle wired for this token: refuse to guess a 1:1 price (#923) —
        # a mis-sized SELL is worse than a failed rebalance.
        logger.error("No oracle for %s — failing rebalance (was: 1:1 USDC estimate)", token_address[:10])
        raise OraclePriceUnavailableError(f"No oracle configured for token {token_address} — cannot size SELL")

    # ─── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _parse_vault_created(factory, receipt) -> str | None:
        """Extract vault address from VaultCreated event in receipt."""
        for log in receipt.logs:
            try:
                result = factory.events.VaultCreated().process_log(log)
                return result["args"]["vault"]
            except Exception:
                continue
        return None

    async def _get_token_symbol(self, token_address: str) -> str:
        """Get ERC-20 symbol, with fallback for known tokens."""
        known = {
            chain_client.settings.usdc_address.lower(): "USDC",
        }
        for sym, addr in chain_client.settings.synth_addresses.items():
            if addr:
                known[addr.lower()] = sym

        addr_lower = token_address.lower()
        if addr_lower in known:
            return known[addr_lower]

        try:
            token = self.loader.token(token_address)
            return await token.functions.symbol().call()
        except Exception:
            return token_address[:8]

    async def _get_token_decimals(self, token_address: str) -> int:
        """Get ERC-20 decimals, with fallback for USDC."""
        if token_address.lower() == chain_client.settings.usdc_address.lower():
            return 6
        try:
            token = self.loader.token(token_address)
            return await token.functions.decimals().call()
        except Exception:
            return 18

    async def _token_to_usdc(
        self, token_address: str, amount: int, decimals: int, use_raw_price: bool = False
    ) -> tuple[int, bool]:
        """Estimate USDC value of a token holding → ``(value_usdc, priced)``.

        If use_raw_price is True (stale oracle fallback), uses the raw price()
        getter which doesn't check staleness.

        Returns ``(0, False)`` (with a warning) when no price can be read —
        never the raw base-unit amount: callers treat the result as 6-decimal
        USDC, so an 18-decimal raw amount inflates the value 10**12x (#1080).
        The ``priced`` flag lets callers distinguish "worth 0" from "value
        unknown" — a distinction trade sizing depends on (see PortfolioHolding).
        """
        if token_address.lower() == chain_client.settings.usdc_address.lower():
            return amount, True

        # For synth tokens, use the oracle price
        for sym, addr in chain_client.settings.synth_addresses.items():
            if addr and addr.lower() == token_address.lower():
                if not use_raw_price:
                    try:
                        oracle = self.loader.oracle_for(sym)
                        price = await oracle.functions.getPrice().call()
                        # USDC value = amount (10**decimals base units) * price (6 dec)
                        return (amount * price) // (10**decimals), True
                    except Exception:
                        logger.warning("getPrice() failed for %s — retrying with raw price()", sym)
                try:
                    # Raw price getter (no staleness check)
                    oracle = self.loader.oracle_for(sym)
                    price = await oracle.functions.price().call()
                    return (amount * price) // (10**decimals), True
                except Exception:
                    logger.warning("oracle price unavailable for %s — valuing holding at 0 (unpriced)", sym)
                    return 0, False

        logger.warning("no oracle mapping for token %s — valuing holding at 0 (unpriced)", token_address)
        return 0, False

    async def _safe_total_assets(
        self,
        vault: AsyncContract,
        token_addresses: list,
        amounts: list,
    ) -> int:
        """Read totalAssets() with stale-price fallback.

        If totalAssets() reverts (e.g. StalePrice), compute NAV off-chain
        using the raw price() getter that doesn't check staleness.
        """
        try:
            return await vault.functions.totalAssets().call()
        except Exception:
            # Fallback: compute off-chain using raw prices
            logging.getLogger(__name__).warning("totalAssets() reverted — computing NAV off-chain with raw prices")
            usdc_address = chain_client.settings.usdc_address
            nav = 0
            for i in range(len(token_addresses)):
                amount = amounts[i]
                if amount == 0:
                    continue
                token_addr = token_addresses[i]
                if token_addr.lower() == usdc_address.lower():
                    nav += amount
                else:
                    # Use raw price() (no staleness check)
                    decimals = await self._get_token_decimals(token_addr)
                    value, _priced = await self._token_to_usdc(token_addr, amount, decimals, use_raw_price=True)
                    nav += value
            return nav


# Singleton
chain_executor = ChainExecutor()
