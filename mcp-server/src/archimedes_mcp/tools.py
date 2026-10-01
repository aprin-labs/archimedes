"""The nine tool handlers.

Each one is a parameter shuffle around a single :func:`client.request` call. There is no
arithmetic here, no caching, no retry, and no interpretation of a gate verdict — those all
live server-side, and duplicating any of them client-side would create a second answer
that can disagree with the first. Thin means thin.

Handlers never raise. A missing credential, a paywall, a bad argument and a dead socket
all come back as ``{"ok": false, ...}`` from :mod:`errors`, because this process is a
long-lived stdio server and a raised exception is a worse answer than a described one.
"""

from __future__ import annotations

import math
import re
from datetime import date as _Date
from typing import Any

from . import client, contract, errors
from .credentials import Credential, credential_help, resolve_api_url, resolve_credential


def _context() -> tuple[str, Credential | None]:
    """Resolved per call, not per process.

    A long-lived stdio server outlives ``archimedes login`` and outlives a key rotation.
    Reading the environment and the session cache on every call means a refreshed
    credential is picked up without restarting the server, and a *revoked* one stops
    working immediately instead of living on in a captured variable.
    """
    return resolve_api_url(), resolve_credential()


def _call(
    tool_name: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve the credential the tool's contract row says it needs, then call."""
    api_url, credential = _context()
    needs_credential = contract.by_name(tool_name)["auth"] == contract.AUTH_CREDENTIAL
    if needs_credential and credential is None:
        # Refused locally, before any socket: an unauthenticated call to a gated route
        # spends a request to be told something already knowable here.
        return errors.failure("no_credential", f"{tool_name} needs an account credential.", credential_help())
    return client.request(
        method,
        path,
        api_url=api_url,
        credential=credential,
        params={k: v for k, v in (params or {}).items() if v is not None},
        json_body=json_body,
    )


# ── read + verify ────────────────────────────────────────────────────


def archimedes_quote() -> dict[str, Any]:
    return _call("archimedes_quote", "GET", "/api/generate/quote")


def archimedes_usage() -> dict[str, Any]:
    return _call("archimedes_usage", "GET", "/api/account/usage")


# `POST /api/rigor/verify`'s own input bounds (#1803). Mirrored, not imported —
# this package depends on nothing — and the SERVER stays the authority: these
# only save a round trip on an invocation that cannot succeed.
_MIN_RETURN_ROWS = 250  # the minimum evaluation window: one trading year (owner decision, #1803)
_MAX_RETURN_ROWS = 2600  # ~10 years of daily bars
_MAX_TRIALS = 10_000
_MAX_ABS_DAILY_RETURN = 1.0  # |r| <= 1.0, in simple-return units
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _row_rejection(returns: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The per-bar half of the input contract, checked here (#1803 review round 2).

    One of these rows is not merely refusable, it is UNSENDABLE: ``httpx``
    serialises the body with ``json.dumps(..., allow_nan=False)``, so a NaN or
    ±Infinity return raises ``ValueError`` inside :func:`client.request` — and
    this module's contract is that handlers never raise. The remaining rules ride
    along so one bad series comes back in one shape.

    A mirror, never a replacement: the server re-checks every one of these, the
    codes are its codes, and nothing here sorts, deduplicates or coerces
    anything. Row counts stay server-side (they are the gate's own moving
    policy) — they are bounded separately, above, where they already were.

    Anything this cannot classify (a row that is not a mapping, a return that is
    not a number) is left alone for the server to answer on: a client-side guess
    at a shape it does not understand would be a second, disagreeing answer.
    """
    # Per-row first, then duplicates, then order — the order the server's own
    # validators run in, so both sides name the same defect on a series that has
    # more than one.
    dates: list[tuple[int, str]] = []
    for index, row in enumerate(returns, start=1):
        if not isinstance(row, dict):
            continue
        raw_date = row.get("date")
        if isinstance(raw_date, str):
            if not _ISO_DATE_RE.match(raw_date):
                return _row_failure(
                    "invalid_date",
                    f"returns row {index} has date {raw_date!r}, which is not a strict ISO calendar date (YYYY-MM-DD).",
                )
            try:
                _Date.fromisoformat(raw_date)
            except ValueError:
                return _row_failure(
                    "invalid_date",
                    f"returns row {index} has date {raw_date!r}, which is well-formed "
                    "YYYY-MM-DD but is not a real calendar date.",
                )
            dates.append((index, raw_date))

        value = row.get("daily_return")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value):
            return _row_failure(
                "non_finite",
                f"returns row {index} has a non-finite daily_return ({value!r}). It cannot be "
                "graded, and it cannot even be encoded as JSON.",
            )
        if abs(value) > _MAX_ABS_DAILY_RETURN:
            return _row_failure(
                "out_of_range",
                f"returns row {index} has daily_return {value}, outside "
                f"[-{_MAX_ABS_DAILY_RETURN}, {_MAX_ABS_DAILY_RETURN}] (simple return units, "
                "0.01 = +1%).",
            )

    first_seen: dict[str, int] = {}
    for index, iso in dates:
        if iso in first_seen:
            return _row_failure(
                "duplicate_date",
                f"returns row {index} repeats the date {iso}, already used by row "
                f"{first_seen[iso]}. A daily series has one bar per date.",
            )
        first_seen[iso] = index
    # Lexicographic order IS chronological order for strict YYYY-MM-DD, which
    # every date above has already been proved to be.
    for position in range(1, len(dates)):
        (index, iso), (prev_index, prev_iso) = dates[position], dates[position - 1]
        if iso < prev_iso:
            return _row_failure(
                "unsorted_dates",
                f"returns is not in ascending date order: row {index} ({iso}) precedes row {prev_index} ({prev_iso}).",
            )
    return None


def _row_failure(reason: str, message: str) -> dict[str, Any]:
    """A locally-caught input rejection, in the SERVER's own vocabulary — the same
    ``error`` string and the same remedy the 422 path returns for this code."""
    return errors.failure(reason, message, errors.input_rejected_remedy(reason))


def archimedes_rigor_verify(returns: list[dict[str, Any]], trials: int = 1) -> dict[str, Any]:
    """`returns` is `[{"date": "2026-01-02", "daily_return": 0.001}, ...]`; `trials` is 1..10000.

    Dates must be strict `YYYY-MM-DD`, unique and ASCENDING; returns must be finite
    simple decimals with `|r| <= 1.0`; the series is 250..2600 rows — 250 daily bars, one
    trading year, is the MINIMUM EVALUATION WINDOW, and under it there is no verdict at
    all, only a refusal naming `bars_received` and `bars_required`. The server refuses a
    violating body with a 422 whose `detail.reason` is one of `invalid_date`,
    `duplicate_date`, `unsorted_dates`, `non_finite`, `out_of_range`, `window_too_short`,
    `too_many_rows`, `trials_out_of_range` — surfaced here as `error` on the failure
    result. It does NOT sort or deduplicate for you: the walk-forward split is
    positional, so re-ordering server-side would grade a series you did not send.

    A row this tool can already see is wrong comes back with that same `error` before
    any request is spent; a non-finite return in particular cannot even be encoded as
    JSON, so it is refused here rather than raised. The server re-checks all of it.
    """
    if not 1 <= trials <= _MAX_TRIALS:
        return errors.failure(
            "trials_out_of_range",
            f"trials must be between 1 and {_MAX_TRIALS}.",
            "trials is the self-attested number of variants you tried; the DSR is deflated by it. "
            "One attempt is 1, not 0. It is bounded above because an enormous count drives the "
            "deflation to -inf, which turns a FAIL into 'not_evaluable'.",
        )
    if len(returns) < _MIN_RETURN_ROWS:
        return errors.failure(
            "window_too_short",
            f"returns has {len(returns)} rows; the minimum evaluation window is "
            f"{_MIN_RETURN_ROWS} daily bars (one trading year).",
            errors.input_rejected_remedy("window_too_short"),
        )
    if len(returns) > _MAX_RETURN_ROWS:
        return errors.failure(
            "too_many_rows",
            f"returns has {len(returns)} rows; the maximum is {_MAX_RETURN_ROWS}.",
            "That is a payload cap (~10 years of daily bars), not a statistical one. Split the "
            "series or aggregate to a coarser frequency.",
        )
    rejection = _row_rejection(returns)
    if rejection is not None:
        return rejection
    return _call(
        "archimedes_rigor_verify",
        "POST",
        "/api/rigor/verify",
        json_body={"returns": returns, "trials": trials},
    )


def archimedes_strategy(strategy_id: str) -> dict[str, Any]:
    return _call("archimedes_strategy", "GET", f"/api/strategies/{strategy_id}")


def archimedes_passport(strategy_id: str) -> dict[str, Any]:
    return _call("archimedes_passport", "GET", f"/api/strategies/passports/{strategy_id}")


def archimedes_leaderboard(
    scope: str | None = None,
    sort_by: str = "conviction_score",
    order: str = "desc",
    min_rigor: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """`scope` is "own" or "curated"; anonymous callers are served "curated" whatever they ask."""
    return _call(
        "archimedes_leaderboard",
        "GET",
        "/api/leaderboard",
        params={"scope": scope, "sort_by": sort_by, "order": order, "min_rigor": min_rigor, "limit": limit},
    )


def archimedes_corpus_search(
    search: str,
    page: int = 1,
    page_size: int = 20,
    category: str | None = None,
    processed_only: bool = True,
) -> dict[str, Any]:
    """Lexical substring search over title, abstract and authors. No embeddings."""
    return _call(
        "archimedes_corpus_search",
        "GET",
        "/api/papers/",
        params={
            "search": search,
            "page": page,
            "page_size": page_size,
            "category": category,
            "processed_only": processed_only,
        },
    )


# ── the metered write path ───────────────────────────────────────────


def archimedes_generate_start(
    intent: str,
    risk_appetite: str = "moderate",
    max_papers: int = 5,
    n_candidates: int = 1,
    name: str | None = None,
) -> dict[str, Any]:
    """Start a generation. Costs money on a `payment_required: true` host — quote first."""
    brief: dict[str, Any] = {
        "intent": intent,
        "risk_appetite": risk_appetite,
        "max_papers": max_papers,
    }
    if name is not None:
        brief["name"] = name
    # Bounds are NOT re-validated here beyond what the signature expresses. The API owns
    # them (422 names the exact field in `loc`), and a client-side copy is one more thing
    # that can disagree with the server after a bound changes.
    return _call(
        "archimedes_generate_start",
        "POST",
        "/api/generate/start",
        json_body={"brief": brief, "n_candidates": n_candidates},
    )


def archimedes_generate_status(job_id: str) -> dict[str, Any]:
    return _call("archimedes_generate_status", "GET", f"/api/generate/jobs/{job_id}")


HANDLERS = {
    "archimedes_quote": archimedes_quote,
    "archimedes_usage": archimedes_usage,
    "archimedes_rigor_verify": archimedes_rigor_verify,
    "archimedes_generate_start": archimedes_generate_start,
    "archimedes_generate_status": archimedes_generate_status,
    "archimedes_strategy": archimedes_strategy,
    "archimedes_passport": archimedes_passport,
    "archimedes_leaderboard": archimedes_leaderboard,
    "archimedes_corpus_search": archimedes_corpus_search,
}
"""``contract.TOOL_NAMES`` -> handler. Pinned equal to the contract by
``tests/test_contract_sync.py``: a tool declared and not implemented, or implemented and
undeclared, is the drift this whole arrangement exists to prevent."""


__all__ = ["HANDLERS"]
