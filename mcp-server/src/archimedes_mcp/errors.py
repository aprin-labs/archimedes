"""Failures, as results.

Every tool returns a JSON object whose first field is ``ok``. A failure is
``{"ok": false, "error": ..., "message": ..., "remedy": ...}`` — a *result*, not a
protocol exception. Two reasons, and the second is the important one:

- An MCP protocol error collapses to a string. A ``402`` is not a string: it carries the
  live quote, the price, the chain, and the recipient, and an agent needs all of it to
  decide whether to pay. Returning it structured is the only way it survives the trip.
- Returning it means it cannot be silently dropped by an error path that logs and moves
  on. The paywall, the wallet gate and the quota are the product's real answers; the
  calling agent is the thing that has to act on them, so they are handed to it intact.

``error`` strings are an API, on the same rule ``cli/src/archimedes_cli/exits.py`` states
for exit codes: someone will branch on them, so a new condition gets a new code and an
existing code is never redefined. The mapping below is the ``docs/agent-quickstart.md``
error table, one row at a time, including the two places where one status has two meanings
(``402`` payment vs. entitlement, ``429`` daily cap vs. queue vs. rate limit).

Nothing here logs, and nothing here interpolates a credential — the only inputs are a
status code and a response body the server sent us.
"""

from __future__ import annotations

from typing import Any

import httpx


def failure(error: str, message: str, remedy: str, **extra: Any) -> dict[str, Any]:
    """A failure result. ``ok`` first so a reader that stops early still sees it."""
    return {"ok": False, "error": error, "message": message, "remedy": remedy, **extra}


def ok(payload: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """A success result carrying the API's own body verbatim underneath ``ok: true``.

    ``ok`` is written last as well as first: no route serves a field called ``ok`` today,
    but merging an upstream body straight over the flag would mean a future one could
    turn a success into ``ok: false`` — a client's success/failure signal must not be
    settable by the payload it is describing.
    """
    result: dict[str, Any] = {"ok": True}
    result.update(payload or {})
    result.update(extra)
    result["ok"] = True
    return result


def _detail(response: httpx.Response) -> Any:
    """The API's ``detail`` — a string, an object, or a list, all three of which occur.

    FastAPI errors raised by the app use a string or an object; *request validation*
    failures use a list. The quickstart's error table calls that out because a caller that
    assumes one shape crashes on the other.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] if response.text else None
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body


def _reason(detail: Any) -> str | None:
    return detail.get("reason") if isinstance(detail, dict) else None


def _message(detail: Any, fallback: str) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        msg = detail.get("message")
        if isinstance(msg, str):
            return msg
    return fallback


# One line of "what to change" per input-refusal code from
# `POST /api/rigor/verify` (#1803). The server's own sentence always leads and
# is never replaced by these; it says what was rejected, this says what to do.
_INPUT_REJECTED_REMEDY = {
    "invalid_date": "Write every date as strict YYYY-MM-DD. Epoch seconds and YYYYMMDD are refused, not guessed at.",
    "duplicate_date": "One row per trading day. Duplicates are refused, not merged — decide which bar is real.",
    "unsorted_dates": (
        "Sort the rows by date, oldest first, and resend. The walk-forward split is POSITIONAL, so row "
        "order is the time order it grades; the server will not re-sort for you because that would "
        "return a verdict on a series you did not send."
    ),
    "non_finite": "Drop the NaN/Infinity bars. A non-finite return cannot be graded, only refused.",
    "out_of_range": (
        "Returns are simple decimals, not percentages: +1.3% is 0.013, not 1.3. |r| > 1.0 in a single "
        "day is refused because it silently inflates the Sharpe the verdict rests on."
    ),
    "window_too_short": (
        "Send at least 250 daily bars — one trading year, the minimum evaluation window. Under it the "
        "endpoint returns a refusal instead of a verdict: a short series is not graded and flagged, it "
        "is not graded at all, because a `passes` field with a caveat beside it gets reported as a pass."
    ),
    # The pre-window spelling, kept because ARCHIMEDES_API_URL can point at an
    # older host: its `too_short` should still render a remedy, not the fallback.
    "too_short": "Send a longer series — this host's floor predates the 250-bar evaluation window.",
    "too_many_rows": "Split the series or aggregate to a coarser frequency; the cap is ~10 years of daily bars.",
    "trials_out_of_range": "trials is 1..10000 — the number of variants you actually tried.",
}

_UNKNOWN_INPUT_REJECTION_REMEDY = (
    "Fix the field named in `detail.loc` and retry. The server refuses a malformed series rather than repairing it."
)


def input_rejected_remedy(reason: str) -> str:
    """What to change, per input-rejection code (#1803).

    Shared with :mod:`tools`, which refuses some of these locally: one code must
    come back with one remedy whether the row was caught before the request or by
    the server. An unrecognised code — a newer server's — still gets an answer.
    """
    return _INPUT_REJECTED_REMEDY.get(reason, _UNKNOWN_INPUT_REJECTION_REMEDY)


def from_response(response: httpx.Response, *, credential_kind: str | None) -> dict[str, Any]:
    """Map a non-2xx API response onto a structured, actionable failure result."""
    status = response.status_code
    detail = _detail(response)
    reason = _reason(detail)

    if status == 401:
        if credential_kind == "api_key":
            remedy = (
                "The API key was rejected. It may be revoked, may belong to another "
                "account, or — most likely today — the scoped-API-key lane "
                "(dbrowneup/1653-scoped-api-keys, D3 on #1653) may not be deployed on this "
                "host yet, in which case no bearer key works there. Mint a new key at "
                "POST /api/account/keys with a browser session, or unset ARCHIMEDES_API_KEY "
                "and run `archimedes login` to fall back to the session cookie."
            )
        elif credential_kind == "session_cookie":
            remedy = "The cached session expired or was revoked. Run `archimedes login` again."
        else:
            remedy = "No credential was presented. Set ARCHIMEDES_API_KEY or run `archimedes login`."
        return failure(
            "unauthenticated",
            _message(detail, "Authentication required."),
            remedy,
            http_status=401,
            detail=detail,
        )

    if status == 402:
        if reason == "payment_required" or (isinstance(detail, dict) and "quote" in detail):
            quote = detail.get("quote") if isinstance(detail, dict) else None
            return failure(
                "payment_required",
                _message(detail, "This host charges for generation and no payment was presented."),
                "Sign the x402 requirements in the PAYMENT-REQUIRED header with your linked wallet "
                "and retry POST /api/generate/start yourself, carrying both Payment-Signature and a "
                "stable Idempotency-Key. This server holds no wallet key and will not sign for you. "
                "Do NOT blind-retry: a fresh signature is a fresh real charge. If a previous paid run "
                "never delivered, its credit pays for the next attempt automatically.",
                http_status=402,
                quote=quote,
                payment_requirements=response.headers.get("PAYMENT-REQUIRED"),
                detail=detail,
            )
        return failure(
            "entitlement_required",
            _message(detail, "The requested model requires an entitlement."),
            "Omit `model` to use the server's free default, or name an allowlisted free model. "
            "The request is refused rather than silently downgraded, so nothing ran and nothing "
            "was charged.",
            http_status=402,
            detail=detail,
        )

    if status == 409:
        if reason == "wallet_link_required":
            return failure(
                "wallet_link_required",
                _message(detail, "Payment is required and this account has no linked wallet."),
                "Link a wallet with POST /api/wallets/challenge then POST /api/wallets/verify "
                "(provider: 'headless' is the one an API caller can use). This server does not "
                "link wallets — it holds no key to sign the EIP-4361 challenge with. Note the "
                "wallet must also hold testnet USDC, and the Circle faucet currently needs a "
                "human, so linking an empty wallet only moves you to the 402.",
                http_status=409,
                detail=detail,
            )
        if reason == "idempotency_key_already_used":
            return failure(
                "idempotency_key_already_used",
                _message(detail, "That Idempotency-Key already paid for a generation."),
                "Do not re-sign. That run exists — find it with archimedes_generate_status. Use a "
                "fresh key only for a genuinely new run.",
                http_status=409,
                detail=detail,
            )
        return failure("conflict", _message(detail, "Conflict."), "Read `detail`.", http_status=409, detail=detail)

    if status == 429:
        if reason == "generation_daily_cap":
            scope = detail.get("scope") if isinstance(detail, dict) else None
            return failure(
                "daily_cap_reached",
                _message(detail, "Daily generation cap reached."),
                "Wait for the daily reset. Call archimedes_usage before archimedes_generate_start to "
                "see this coming. No payment was taken — the cap is enforced before the paywall.",
                http_status=429,
                scope=scope,
                detail=detail,
            )
        if reason == "generation_queue_full":
            return failure(
                "queue_full",
                _message(detail, "The generation queue is full."),
                "Retry in a few minutes. No payment was taken — admission control runs before the paywall.",
                http_status=429,
                detail=detail,
            )
        return failure(
            "rate_limited",
            _message(detail, "Rate limit exceeded."),
            "Back off and retry. This is requests-per-minute, a different limit from the daily cap; "
            "the X-RateLimit-* headers carry the window.",
            http_status=429,
            detail=detail,
            rate_limit_reset=response.headers.get("X-RateLimit-Reset"),
        )

    if status == 422:
        # `POST /api/rigor/verify` attaches a stable reason code to an input
        # refusal (#1803). It is promoted to `error` so an agent branches on the
        # specific cause rather than on the generic "the body was bad", and the
        # server's own sentence is the message.
        if isinstance(detail, dict) and detail.get("error") == "input_rejected" and isinstance(reason, str):
            return failure(
                reason,
                _message(detail, "The API rejected the input."),
                input_rejected_remedy(reason),
                http_status=422,
                detail=detail,
            )
        return failure(
            "invalid_request",
            _message(detail, "The API rejected the request body."),
            "Read `detail`. On a validation list each entry's `loc` names the exact field. Common "
            "causes: max_papers outside [2, 6], n_candidates outside [1, 5], an unknown "
            "risk_appetite.",
            http_status=422,
            detail=detail,
        )

    if status == 404:
        return failure(
            "not_found",
            _message(detail, "Not found."),
            "Missing and not-yours are deliberately the same answer — existence is private — so a "
            "404 is not proof the id is wrong. Confirm the id came from a call made with this same "
            "credential.",
            http_status=404,
            detail=detail,
        )

    if status == 503:
        return failure(
            "service_unavailable",
            _message(detail, "The service refused, temporarily."),
            "Not caller-fixable. Retry later. Payment configuration in particular fails closed "
            "rather than letting a request through free.",
            http_status=503,
            detail=detail,
        )

    return failure(
        "http_error",
        _message(detail, f"HTTP {status}."),
        "Unmapped status. Read `http_status` and `detail`.",
        http_status=status,
        detail=detail,
    )


def from_transport_error(exc: httpx.HTTPError, api_url: str) -> dict[str, Any]:
    """A request that never reached the API. Deliberately its own code.

    A network failure is not a verdict about anything — the same distinction
    ``exits.py`` draws between exit 1 and every other non-zero code. Collapsing it into a
    gate or paywall answer would let an agent report a timeout as a product decision.
    The exception's ``str`` is a URL and a socket error, never a credential: credentials
    ride in headers/cookies, which httpx does not render into ``HTTPError`` messages.
    """
    return failure(
        "network_error",
        f"Could not reach {api_url}: {exc}",
        "Check the host is up and ARCHIMEDES_API_URL is right. Nothing ran; nothing was charged.",
        api_url=api_url,
    )


__all__ = ["failure", "from_response", "from_transport_error", "ok"]
