"""x402 paywall for strategy generation (Dan's directive, 2026-08-19).

**Policy correction, 2026-08-31 (#1643).** This docstring used to state that
generation required a wallet connection and a payment for humans and agents
alike, with no free allowance whatsoever. That is no longer what the code
does, so the claim is deleted rather than softened — a docstring that
contradicts shipped behaviour is the "claims must be true" defect, not a
stylistic one. The owner's product review kept the *account*
requirement absolute (``require_current_user`` gates every generation, and
there is no wallet-only-without-account path) but made the *wallet* optional
for a small lifetime allowance: the first ``FREE_GENERATIONS_PER_ACCOUNT``
(default 3) generations on an account need neither a linked wallet nor a
payment. **Amended the same day (owner decision D1, recorded on #1653): that
allowance unlocks on a VERIFIED email, not on account creation alone** — an
unverified account has the wallet + payment path below and nothing else, which
is precisely the pre-#1643 behaviour. Nothing in THIS module implements that —
the allowance is claimed in
``api/generate_routes.start_generation`` from
``services/free_generations.py``, ahead of the wallet check and this paywall —
and nothing in this module changed: from generation #4 onward every rule below
applies exactly as written.

Generation past the free allowance requires wallet connection + payment — for
humans and agents alike — while paper trading stays free. The 402 response *is* the
quote-approval flow: it carries the full payment requirements in the
``PAYMENT-REQUIRED`` header (and a human-readable quote in the JSON body), the
client signs and retries with a ``Payment-Signature`` header, and the server
verifies + settles via Circle's facilitator (the same
``marketplace.payments``/circlekit machinery the tick-charging rail uses —
this module reuses ``get_gateway_middleware``; circlekit stays imported in one
place).

FLAG-GATED: ``GENERATION_PAYMENT_REQUIRED`` (only the literal ``"true"``
enables — Dan flips it deliberately; see the flip-list, issue #834). While the
flag is off every function here is inert and /api/generate behaves exactly as
before. The account+IP daily quota stays stacked UNDERNEATH the paywall as
anti-abuse — and runs BEFORE it, so a quota-blocked caller is refused with 429
before ever being asked to pay.

``PAYMENTS_DRY_RUN`` semantics (mirrors ``main.py``): in dry-run the paywall
still quotes (402 without a payment header — so the approval UX is exercised
end to end) but accepts a presented payment WITHOUT verify/settle, loudly.
No real value can move while the custody migration (#975) is pending.

``PAYMENTS_HALT`` (#1240 kill switch) is NOT the same shape as dry-run here,
unlike on the marketplace tick-charging rail: this is a metered pay-per-call
API with no pre-existing subscriber relationship, so a halted charge REFUSES
service (503) instead of accepting an unverified header for free — comping
the paid product (and dropping the payer-binding check with it) is not what
an operator flipping a kill switch wants.

Pricing is flat (``GENERATION_PRICE_USD``, default $2.00 testnet USDC — one $20 faucet drip = 10 generations) behind
``quote()`` — the single seam #1217's measured per-generation budget replaces
later without touching the paywall flow.
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

DEFAULT_PRICE_USD = "2.00"

# The logical resource identifier bound into the 402 requirements. One flat
# resource for now — per-job binding is unnecessary while the price is flat
# (replay of a settled payment is prevented by the facilitator consuming the
# signed authorization at settle time, not by path uniqueness).
_RESOURCE_PATH = "/api/generate/start"


def payment_required() -> bool:
    """Only the literal "true" enables — mirrors EMAIL_VERIFICATION_ENFORCED."""
    return os.getenv("GENERATION_PAYMENT_REQUIRED") == "true"


def _payments_dry_run() -> bool:
    """Generation-scoped dry-run switch, split from the global (2026-08-20).

    GENERATION_PAYMENTS_DRY_RUN, when set, governs ONLY this paywall; unset,
    it inherits PAYMENTS_DRY_RUN (main.py's parse, EXACT mirror — the two
    must never disagree on the fallback). Why the split: the recorded scope
    decision (infra/ecs.tf money-switch block) allows real settlement on the
    caller-signed metered-API path while the marketplace DCW flow stays
    blocked on #975 — but one global switch gated both. The split lets the
    generation rail go live without un-drying marketplace sweeps/withdraws.
    """
    raw = os.getenv("GENERATION_PAYMENTS_DRY_RUN")
    if raw is None:
        raw = os.getenv("PAYMENTS_DRY_RUN", "true")
    return raw.lower() in ("1", "true", "yes")


def _payments_halted() -> bool:
    """#1240 kill switch — see marketplace.config.payments_halted's docstring.

    This surface needs the switch MORE than the marketplace tick-charging rail
    does, and for a reason that is easy to state wrong. In prod today
    (infra/ecs.tf money-switch block) the global PAYMENTS_DRY_RUN is "true";
    what makes this path settle real value is the generation-scoped override
    GENERATION_PAYMENTS_DRY_RUN="false" (the 2026-08-20 split, #1428) plus
    GENERATION_PAYMENT_REQUIRED="true". So this is the only rail currently
    moving real money — while the marketplace sweeps and withdraws stay dry
    behind the global, blocked on #975.

    The consequence for the kill switch: _payments_dry_run() above, which is
    what stops value moving everywhere else, is deliberately False here. This
    is therefore the one place where PAYMENTS_HALT is not a redundant second
    belt but the only no-redeploy way to stop a live charge.

    (Do not restate this as "the one surface allowed to run
    PAYMENTS_DRY_RUN=false" — that was true of the original scope decision,
    but the split replaced the mechanism, and the global is true in prod.)
    """
    from archimedes.marketplace.config import payments_halted

    return payments_halted()


def _recipient() -> str:
    return os.getenv("GENERATION_PAYMENT_RECIPIENT", "").strip()


def settles_real_value() -> bool:
    """True only when a presented payment will actually be verified and settled.

    The generation credit ledger (#1441) hangs off this. Under flag-off or
    dry-run no value moves, so there is nothing to owe anybody and no claim
    worth writing — the ledger stays completely inert, which is what keeps this
    change a no-op in production until the payment stack is flipped on.
    """
    return payment_required() and not _payments_dry_run()


def _price() -> str:
    """The circlekit "$X.XXXXXX" price string. Fails SAFE to the default on a
    malformed env value (a typo must not turn the paywall free or absurd)."""
    raw = os.getenv("GENERATION_PRICE_USD", DEFAULT_PRICE_USD).strip() or DEFAULT_PRICE_USD
    try:
        value = Decimal(raw)
        if value < 0:
            raise InvalidOperation
    except InvalidOperation:
        logger.warning("invalid GENERATION_PRICE_USD=%r — using default %s", raw, DEFAULT_PRICE_USD)
        value = Decimal(DEFAULT_PRICE_USD)
    return f"${value:.6f}"


def quote() -> dict:
    """The generation price quote — public, also embedded in every 402 body.

    ``pricing_model: "flat_v1"`` names the current scheme; #1217's measured
    per-generation budget replaces the internals of this function (and bumps
    the model name) without changing the paywall flow around it.
    """
    from archimedes.marketplace.config import gateway_chain

    return {
        "payment_required": payment_required(),
        "pricing_model": "flat_v1",
        "price": _price(),
        "asset": "USDC",
        "chain": gateway_chain(),
        "recipient": _recipient() or None,
        "dry_run": _payments_dry_run(),
        "halted": _payments_halted(),
        "how": (
            "POST /api/generate/start without a Payment-Signature header returns 402 "
            "with these requirements in the PAYMENT-REQUIRED header; sign them "
            "(x402 / Circle Gateway) and retry with Payment-Signature."
        ),
    }


def _quote_402(detail_reason: str, message: str) -> HTTPException:
    """A 402 carrying BOTH the machine quote (PAYMENT-REQUIRED header, built by
    the same circlekit middleware the settle path uses) and a human/agent
    readable body. Raising-site helper so every 402 is shaped identically."""
    from archimedes.marketplace.payments import get_gateway_middleware

    headers = {}
    try:
        required = get_gateway_middleware(_recipient()).require(_price(), _RESOURCE_PATH)
        headers = required.get("headers") or {}
    except Exception:
        # Requirements are computed locally; a failure here is a config bug.
        # Still 402 (honest: payment IS required) with the JSON quote only.
        logger.exception("could not build PAYMENT-REQUIRED header")
    return HTTPException(
        status_code=402,
        detail={"reason": detail_reason, "message": message, "quote": quote()},
        headers=headers,
    )


async def enforce_generation_payment(request: Request, linked_wallet: str):
    """The paywall. Returns a settled ``PaymentInfo`` (or ``None`` when the
    flag is off / dry-run accepted). Raises HTTPException 402/503 otherwise.

    Fail-closed configuration: flag ON with no recipient configured is an
    OUTAGE (503), never a free pass — the flip-list documents that
    ``GENERATION_PAYMENT_RECIPIENT`` must be set before the flag.
    """
    if not payment_required():
        return None

    if not _recipient():
        logger.error("GENERATION_PAYMENT_REQUIRED=true but GENERATION_PAYMENT_RECIPIENT is unset — refusing (503)")
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "payment_config_missing",
                "message": "Generation payments are enabled but not fully configured. Please try again later.",
            },
        )

    header = request.headers.get("Payment-Signature")
    if not header:
        raise _quote_402(
            "payment_required",
            "Generation requires payment. Sign the PAYMENT-REQUIRED requirements with your linked wallet "
            "and retry with a Payment-Signature header.",
        )

    if _payments_dry_run():
        # No real value may move while PAYMENTS_DRY_RUN holds (#975 custody
        # migration pending). Accept the presented payment WITHOUT verify or
        # settle — loudly, so a log reader can never mistake this for revenue.
        logger.warning(
            "PAYMENTS_DRY_RUN — generation payment header accepted UNVERIFIED and UNSETTLED (no value moved)"
        )
        return None

    if _payments_halted():
        # #1240 kill switch — but on THIS surface (unlike the marketplace
        # tick-charging rail) halting must REFUSE service, not give the paid
        # product away. This is a metered pay-per-call API with no pre-existing
        # subscription relationship, so "no real charge" here doesn't mean
        # "let the tick continue as if it had paid" — it means "we cannot take
        # payment right now." Accepting ANY Payment-Signature value unverified
        # (the marketplace rail's own "treated as no-op" shape) would have
        # served the paid product for free AND dropped the payer-binding check
        # above's guarantee for the whole halt window — the honest reading of
        # a kill switch on a paid surface is to refuse, not to comp it.
        logger.warning("PAYMENTS_HALT active — refusing generation payment (service unavailable, not free)")
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "payments_halted",
                "message": "Generation payments are temporarily halted by an operator kill switch. "
                "Please try again later.",
            },
        )

    from circlekit.x402 import decode_payment_header

    from archimedes.marketplace.payments import get_gateway_middleware

    # Bind the payment to the caller's PROVEN wallet before spending a
    # facilitator round-trip: the payer inside the signed authorization must be
    # the wallet this account linked (the wallet-connection precondition and
    # the payment are one identity, not two).
    try:
        payload = decode_payment_header(header)
        payer = str(((payload.get("payload") or {}).get("authorization") or {}).get("from", ""))
    except Exception:
        raise _quote_402("payment_malformed", "The Payment-Signature header could not be decoded.") from None
    if not payer or payer.lower() != linked_wallet.lower():
        raise _quote_402(
            "payer_mismatch",
            "The payment must be signed by your linked wallet "
            f"({linked_wallet[:10]}…). Link that wallet or sign with it and retry.",
        )

    middleware = get_gateway_middleware(_recipient())
    price = _price()
    try:
        verify_result = await middleware.verify(header, price)
    except ValueError as exc:
        raise _quote_402("payment_invalid", f"Payment could not be verified: {exc}") from exc
    if not verify_result.is_valid:
        reason = getattr(verify_result, "invalid_reason", None) or "verification failed"
        raise _quote_402("payment_invalid", f"Payment verification failed: {reason}")

    try:
        payment = await middleware.settle(header, price)
    except ValueError as exc:
        # Verified but not settled — the caller keeps their funds; honest 402.
        raise _quote_402("payment_settle_failed", f"Payment could not be settled: {exc}") from exc

    logger.info(
        "generation payment settled: payer=%s amount=%s tx=%s",
        payment.payer,
        payment.amount,
        payment.transaction,
    )
    return payment
