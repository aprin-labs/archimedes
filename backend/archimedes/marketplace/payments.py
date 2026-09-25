"""x402 micropayment seam over circle-titanoboa-sdk (circlekit).

This module is the ONLY place circlekit is imported. The SDK is
pre-1.0; keeping the import surface here gives API drift a one-file
blast radius.

Flow per charge (all in-process, no HTTP between publisher/subscriber):
  1. middleware.require(price, path)      -> 402 requirements (publisher side)
  2. create_payment_header(signer, reqs)  -> EIP-712 signature with the
     subscriber's ephemeral key (subscriber side, same process)
  3. middleware.settle(header, price)     -> Circle facilitator verifies and
     records the micropayment. Circle batches and settles on-chain later;
     we do NOT run any settlement logic.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import circlekit.server as _circlekit_server
from circlekit import create_gateway_middleware
from circlekit.server import GatewayMiddleware
from circlekit.wallets import CircleWalletSigner
from circlekit.x402 import create_payment_header, get_payment_required

from archimedes.marketplace.config import gateway_chain, payments_halted

logger = logging.getLogger(__name__)

_USDC_DECIMALS = 6

# Minimum authorization validity Circle's Gateway facilitator will VERIFY, in
# seconds. Established empirically against the live testnet facilitator
# (2026-08-20, first real settle on the generation paywall): windows of 3600,
# 21600, 86400, and circlekit's own DEFAULT_MAX_TIMEOUT_SECONDS (345600 = 4d)
# are ALL rejected with `authorization_validity_too_short`; 604800 (7d)
# verifies and settles (tx 831aaaf1-f110-47f7-8faf-c76aa8f841cb). The pre-1.0
# SDK hardcodes its stale default into `require()` with no override parameter,
# so this module — the declared one-file blast radius for circlekit API drift
# (see module docstring) — rebinds the SDK server module's constant at import.
# Every 402 this backend issues then advertises a window clients can actually
# sign against (the UI derives validBefore from the server's maxTimeoutSeconds,
# so a stale server value bricks every honest browser payment). Remove when the
# SDK ships a current default or a require() parameter.
GATEWAY_MIN_AUTH_VALIDITY_SECONDS = 604_800
_circlekit_server.DEFAULT_MAX_TIMEOUT_SECONDS = GATEWAY_MIN_AUTH_VALIDITY_SECONDS

# Per-creator Gateway middleware cache, keyed by lowercase seller address.
_middleware_cache: dict[str, GatewayMiddleware] = {}

# CircleWalletSigner cache, keyed by wallet_id (one signer per wallet).
# Constructing a signer re-inits the Circle client; caching avoids that.
_signer_cache: dict[str, CircleWalletSigner] = {}


def _get_signer(wallet_id: str, wallet_address: str) -> CircleWalletSigner:
    """Return a cached CircleWalletSigner for *wallet_id*."""
    if wallet_id not in _signer_cache:
        _signer_cache[wallet_id] = CircleWalletSigner(
            wallet_id=wallet_id,
            wallet_address=wallet_address,
        )
    return _signer_cache[wallet_id]


def get_gateway_middleware(seller_address: str) -> GatewayMiddleware:
    """Return a GatewayMiddleware for *seller_address*, cached indefinitely.

    Each creator's agent Circle wallet address gets its own middleware
    instance.  The zero address is unconditionally refused.
    """
    key = seller_address.lower()
    if key in _middleware_cache:
        return _middleware_cache[key]

    if not seller_address or int(seller_address, 16) == 0:
        raise RuntimeError(f"refusing to charge into zero address ({seller_address})")

    chain = gateway_chain()
    mw = create_gateway_middleware(
        seller_address=seller_address,
        chain=chain,
        description="Archimedes copy-trading tick charge",
    )
    _middleware_cache[key] = mw
    return mw


def fee_to_price(action_count: int, flat_fee_raw: int) -> str:
    """Convert action_count x flat fee (raw 6-decimal USDC units) to the
    "$X.XXXXXX" price string circlekit expects. Uses Decimal — no floats."""
    if action_count < 0 or flat_fee_raw < 0:
        raise ValueError("action_count and flat_fee_raw must be >= 0")
    total_raw = action_count * flat_fee_raw
    usd = Decimal(total_raw) / (Decimal(10) ** _USDC_DECIMALS)
    return f"${usd:.6f}"


async def charge(
    sub_id: str,
    wallet_id: str,
    wallet_address: str,
    seller_address: str,
    strategy_id: str,
    tick_id: str,
    action_count: int,
    flat_fee_raw: int,
    step: str | None = None,
) -> bool:
    """Charge one subscriber for one tick. Returns True iff the micropayment
    was verified AND settled by Circle's facilitator. Never raises: every
    failure mode is logged and returned as False (the caller's existing
    halt path handles unpaid subscribers).

    *seller_address* is the creator's agent Circle wallet 0x address that
    receives the Gateway settlement (per-creator, not a global singleton).
    *wallet_id* is the subscriber's Circle Developer-Controlled Wallet UUID.
    *wallet_address* is the subscriber's Circle wallet 0x address.
    """
    if payments_halted():
        # #1240 backstop, NOT the policy gate. The tick rail's gate is
        # MarketService._charge_one, which short-circuits before ever calling
        # this function and returns charge_suppressed=True so the tick ledger
        # records charged=False without deferring the subscriber. Reaching
        # HERE while halted therefore means a caller went around that gate —
        # a bug, logged at ERROR so it is visible, and answered in the only
        # safe direction: False, meaning "no charge was made". That is the
        # honest answer for any caller (bool is all this primitive can say),
        # and it fails closed rather than moving USDC.
        logger.error(
            "[%s] payments.charge reached while PAYMENTS_HALT is set — a caller bypassed the "
            "MarketService._charge_one gate. Refusing to charge sub %s.",
            tick_id,
            sub_id,
        )
        return False
    try:
        middleware = get_gateway_middleware(seller_address)
        price = fee_to_price(action_count, flat_fee_raw)

        # Zero-amount tick: nothing to charge, treat as paid.
        if price == "$0.000000":
            return True

        # 1. Publisher side: build 402 requirements. `path` is a logical
        # resource identifier only — no HTTP route exists or is needed.
        suffix = f"/{step}" if step else ""
        path = f"/charge/{strategy_id}/{tick_id}/{sub_id}{suffix}"
        required = middleware.require(price, path)
        x402 = get_payment_required(
            required["headers"].get("PAYMENT-REQUIRED"),
            required["body"],
        )
        requirements = x402.get_gateway_option()
        if requirements is None:
            logger.error("[%s] no gateway payment option in 402 body", tick_id)
            return False

        # 2. Subscriber side (same process): sign via Circle Wallet.
        # Event-loop safety: CircleWalletSigner.sign_typed_data and
        # create_payment_header make blocking HTTPS calls.
        signer = _get_signer(wallet_id, wallet_address)
        # `resource` is REQUIRED by the facilitator's payload schema
        # (paymentPayload.resource.{url,description,mimeType}: Required —
        # verified against the live facilitator 2026-08-20); circlekit
        # defaults it to {} when omitted, which 400s every verify.
        header = await asyncio.to_thread(
            create_payment_header,
            signer=signer,
            requirements=requirements,
            resource=x402.resource,
        )

        # 3. Verify + settle via Circle's facilitator.
        verify_result = await middleware.verify(header, price)
        if not verify_result.is_valid:
            logger.warning(
                "[%s] payment verify failed for sub %s: %s",
                tick_id,
                sub_id,
                getattr(verify_result, "invalid_reason", "unknown"),
            )
            return False

        await middleware.settle(header, price)  # raises ValueError on failure
        logger.info("[%s] charged sub %s %s", tick_id, sub_id, price)
        return True

    except Exception as exc:
        logger.warning("[%s] charge failed for sub %s: %s", tick_id, sub_id, exc)
        return False
