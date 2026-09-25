"""Shared configuration defaults for the marketplace money seam.

Single source of truth for chain-name defaults so that payment charging,
Gateway withdrawal, and settlement all read from the same constant.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

logger = logging.getLogger(__name__)

DEFAULT_GATEWAY_CHAIN = "arcTestnet"


def payments_halted() -> bool:
    """Live, per-call kill switch for real-money charging (issue #1240).

    ``PAYMENTS_DRY_RUN`` is the primary safety switch, but ``MarketService``
    reads it once at construction (``main.py`` boot) and caches it on
    ``self.payments_dry_run`` — flipping it off requires a container
    restart. ``PAYMENTS_HALT`` is read fresh from ``os.environ`` on every
    call (never cached), so an operator can stop real charges by setting
    the env var — sourced from an SSM parameter under the same
    ``/archimedes/prod/*`` prefix ``secrets_service.load_ssm_secrets``
    already sweeps into ``os.environ`` at boot — and cycling the ECS
    service (``aws ecs update-service --force-new-deployment``), which is
    a same-image task restart, not the full build/push/roll deploy
    pipeline.

    Only the literal truthy strings enable it; unset/anything else is
    "not halted" (fails toward the status quo — PAYMENTS_DRY_RUN remains
    the primary, default-on safety net).

    SCOPE — the switch's one rule is "no Circle call may move USDC". Keep
    this inventory current; the tests named beside each entry fail if a
    site loses its gate. Six fund-moving call sites are gated, each in the
    callee so a caller cannot forget:

      * ``MarketService._charge_one`` — subscriber tick fee. Suppresses the
        charge only; the subscriber is NOT deferred and the (non-custodial)
        trade mirror still runs. The switch stops money, not service.
      * ``generation_payment.enforce_generation_payment`` — the metered
        paywall REFUSES with 503 rather than comping the paid product.
      * ``SettlementSweeper.sweep_publisher`` — per-tick Gateway withdraw →
        gatewayMint → depositToPool.
      * ``SettlementSweeper.withdraw_publisher`` — Withdraw button,
        ``PaymentSplitter.withdraw``.
      * ``SettlementSweeper.withdraw_subscriber`` — unsubscribe refund.
      * ``services.revenue_sweep.sweep_revenue`` — platform revenue
        Gateway → DCW tokens (both the hourly loop and the CLI).

    DELIBERATELY NOT GATED, because none of them can move USDC:

      * The ``generation_credits`` ledger (#1441/#1498) — ``claim``,
        ``settle``, ``consume``, ``void``, ``restore_credit_for_job``.
        These are DB rows recording an entitlement that a Circle settle
        already created; every one of them runs strictly after (or instead
        of) the paywall this switch guards. Halting them would be actively
        harmful: ``consume`` and ``restore`` are what return an unspent
        credit to a payer whose generation died, so freezing them would
        withhold something already paid for — the opposite of the switch's
        purpose. A halted request never reaches them anyway; the 503 above
        propagates out of ``_paywall_with_credit``, whose
        ``except BaseException: void(credit_id)`` releases the claim so the
        Idempotency-Key is not locked out for the halt window.
      * ``payment_receipts`` writes and ``GET /api/payments/receipts`` —
        reads and records of money that already moved.
      * ``check_revenue`` and every balance read — an operator mid-incident
        needs the numbers.
      * Vault trade execution (``chain/executor.py``) and trace publication
        — non-custodial; the user's own funds under the user's own vault,
        gated by ``PAPER_TRADING``, a different switch with a different
        question.
      * ``createPool`` and wallet provisioning — registration, no transfer.
    """
    return os.getenv("PAYMENTS_HALT", "false").strip().lower() in ("1", "true", "yes")


def gateway_chain() -> str:
    """The Circle Gateway chain every money-path caller must settle on.

    Centralised because the default being shared was not enough. Three call
    sites read ``GATEWAY_CHAIN`` and applied this default; a fourth
    (``services/revenue_sweep.py``) passed the constant straight through as a
    keyword argument and never consulted the environment at all, so pointing
    the deployment at mainnet moved the paywall and left the revenue sweep on
    testnet (#1495).

    That divergence was invisible: a sweep querying an empty testnet balance
    logs "below threshold — skip", which on day one of mainnet is exactly what
    a working system looks like.

    Reading the environment through one function means a new call site cannot
    reintroduce the split by forgetting a ``getenv``; the only way to obtain the
    chain is to ask for it.
    """
    return os.getenv("GATEWAY_CHAIN", DEFAULT_GATEWAY_CHAIN).strip()


_USDC = Decimal(10) ** 6

# Circle's Gateway withdrawal fee is charged ON TOP of the burn amount, not
# deducted from it. Asking to withdraw the entire available balance therefore
# always fails — observed in prod 2026-08-26 sweeping the revenue DCW:
#
#   POST /v1/transfer -> 400
#   "Insufficient balance for depositor 0xffa7abba...56c1:
#    available 36.000000, required 36.0035"
#
# So a full sweep must hold back at least the fee (0.0035 USDC on that
# withdrawal). We reserve 0.05 — ~14x observed headroom, and still under two
# thousandths of a $36 sweep — and pass the same number as the burn intent's
# ``maxFee``. That second half matters: circlekit's default maxFee is
# 2_010_000 (2.01 USDC), so an unbounded withdrawal authorises Circle to take
# a 5.6% haircut off a $36 sweep without complaint. Bounding it means an
# unexpectedly large fee fails loudly instead of being paid silently.
DEFAULT_GATEWAY_FEE_RESERVE_USDC = "0.05"


def gateway_fee_reserve_raw() -> int:
    """Raw 6-decimal USDC held back from a Gateway withdrawal to cover Circle's
    fee. Override via ``GATEWAY_WITHDRAW_FEE_RESERVE_USDC`` if Circle's fee
    schedule moves; a bad value falls back to the default rather than risking
    a zero reserve (which is the bug this exists to prevent)."""
    raw = os.getenv("GATEWAY_WITHDRAW_FEE_RESERVE_USDC", "").strip()
    if not raw:
        raw = DEFAULT_GATEWAY_FEE_RESERVE_USDC
    try:
        value = Decimal(raw)
    except Exception:
        logger.warning(
            "invalid GATEWAY_WITHDRAW_FEE_RESERVE_USDC=%r — using %s",
            raw,
            DEFAULT_GATEWAY_FEE_RESERVE_USDC,
        )
        value = Decimal(DEFAULT_GATEWAY_FEE_RESERVE_USDC)
    if value <= 0:
        logger.warning(
            "GATEWAY_WITHDRAW_FEE_RESERVE_USDC=%r is not positive — using %s "
            "(a zero reserve makes every full sweep fail)",
            raw,
            DEFAULT_GATEWAY_FEE_RESERVE_USDC,
        )
        value = Decimal(DEFAULT_GATEWAY_FEE_RESERVE_USDC)
    return int(value * _USDC)


def format_usdc(raw: int) -> str:
    """Raw 6-decimal USDC → the fixed-point decimal string circlekit parses."""
    return f"{Decimal(raw) / _USDC:.6f}"


def gateway_sweep_amount(available_raw: int) -> tuple[str, int]:
    """Plan a full sweep of ``available_raw``.

    Returns ``(amount, fee_reserve_raw)`` where ``amount`` is the decimal
    string to pass to ``GatewayClient.withdraw`` and ``fee_reserve_raw`` is
    the ``max_fee`` to pass alongside it. ``amount + fee <= available`` holds
    for any fee at or under the reserve, which is what Circle actually checks.

    Raises ``ValueError`` when the balance cannot cover the reserve — callers
    treat that as "nothing to sweep", never as an amount to clamp to zero.
    """
    reserve = gateway_fee_reserve_raw()
    amount_raw = available_raw - reserve
    if amount_raw <= 0:
        raise ValueError(
            f"available {format_usdc(available_raw)} USDC does not cover the "
            f"{format_usdc(reserve)} withdrawal fee reserve"
        )
    return format_usdc(amount_raw), reserve
