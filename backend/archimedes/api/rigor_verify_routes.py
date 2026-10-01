"""``POST /api/rigor/verify`` — the CLI's ``verify`` command backend (#1305).

Runs the rigor gate's admission checks over a BARE returns series (no
strategy code, no trial matrix) submitted directly by a caller — the CLI's
``archimedes verify RETURNS_CSV``. Reuses the exact same computational
functions the strategy-passport verdict uses
(``archimedes.services.rigor_evaluator`` / ``_rigor_helpers`` via it, and the
SAME threshold constants from ``rigor_profiles``) — no reimplementation, no
new thresholds, per the issue spec.

**rf convention (#1409 review fix, 2026-08-21).** ``ReturnPoint.date`` was
threaded to the request schema from the start, but the DSR/OOS/in-sample
Sharpe calls below discarded it and always graded on the flat 5% fallback
with no `rf_convention` disclosure at all — the exact "silent partial
substitution" issue #1409 design item 4 forbids, and on a route that (unlike
every ``run_rigor_gate`` call site) already HAS real per-bar dates on every
request, no schema change needed. Fixed the same way ``run_rigor_gate``
resolves its ONE convention up front (``_resolve_gate_rf``): the per-bar
dates are resolved ONCE here too, and the resolved (never the raw) dates are
threaded into every downstream call, so the disclosed ``rf_convention`` on
the response can never disagree with the arithmetic that produced it.

**Honesty contract (load-bearing).** The gate is four checks. A single
returns series can only ever support two of them:

  * **DSR** — evaluable. Deflated by the *declared* (self-attested) ``trials``
    count via ``compute_dsr_hac_and_iid`` (the identical function
    ``run_rigor_gate`` calls), gated against ``DSR_P_FLOOR`` — the always-on,
    strictness-independent correctness floor (``rigor_profiles.DSR_P_FLOOR``),
    not a strictness-adjustable per-level threshold, since this endpoint takes
    no strictness parameter.
  * **walk-forward OOS consistency** — evaluable. ``compute_oos_sharpe``
    (single chronological 70/30 holdout, the same function the gate uses)
    gated against ``OOS_ABS_FLOOR`` — the identical always-on floor
    ``RigorGateResult.blocked_by_floor`` enforces.
  * **PBO** — ALWAYS ``not_evaluable``. Probability of backtest overfitting
    (Bailey et al. 2014 CSCV) is a property of a *selection set* — it needs a
    trial matrix of multiple candidate strategies' returns over the same
    window. A bare series has no selection set to measure overfitting
    probability against, so this is reported honestly rather than silently
    passed, defaulted, or graded as a FAIL it was never evaluated for.
  * **look-ahead audit** — ALWAYS ``not_evaluable``. The audit is AST-based
    static analysis of strategy source code; a returns series carries no code.
    Archimedes never executes or uploads strategy code server-side (the
    README's hard boundary) — this endpoint accepts only numbers, on purpose.

**The verdict is CAPPED, and ``passes`` is a quorum, not a majority (#1481).**
Two of the four legs above are structurally ``not_evaluable`` on a bare
series — always, for every request. So this endpoint can never reproduce the
full passport gate, and a caller must not read ``passes`` as "cleared the
rigor gate."

``passes`` is ``True`` iff **every RUNNABLE leg actually ran and passed** —
that is, both DSR and OOS consistency. Computing it over "whichever legs
happened to be evaluable" is the #1481 defect: OOS needs ~70 bars while DSR
needs only 4, so a 4-bar series had exactly one evaluable leg and returned
``passes=True`` on one leg of four. The zero-evaluable case was already
guarded ("vacuous truth is not honesty"); the one-evaluable case was not.

``legs_evaluated`` / ``legs_runnable`` / ``legs_total`` and
``verdict_capped`` are on the response so ``passes`` is qualifiable by a
consumer without re-deriving the leg statuses itself. ``legs_total`` is the
real gate's four; ``legs_runnable`` is the two this transport can support.

**The input contract is strict, and it fails closed (#1803).** This is the one
route that takes a caller's raw numbers and returns a verdict on them, so the
series has to be a series before it is graded: strict ``YYYY-MM-DD`` dates,
unique, strictly ascending, every return finite and within +/-100% for one
bar, at least ``_MIN_WINDOW_BARS`` (250, one trading year — the owner's
minimum evaluation window) and at most 2,600 rows, with ``trials`` bounded
at 10,000. Below the window the answer is a TYPED REFUSAL naming
``bars_received`` and ``bars_required`` — never a verdict, and never a verdict
with a warning attached. Each refusal is a 422 whose ``detail.reason`` is a stable code from
``INPUT_REJECTED_CODES``, surfaced by ``archimedes verify`` and the MCP tool. Nothing is sorted, deduplicated, clipped or coerced on the way in: a
shuffled series is REFUSED rather than repaired, because the walk-forward
split is positional and sorting it server-side would hand back a verdict on a
series the caller never sent. See the block above ``ReturnPoint`` for why each
limit is where it is.

Account-session-gated (Better Auth) + rate-limited ``5/minute`` per the issue
spec, mirroring ``paper_routes.py`` / ``selection_bias_routes.py`` style.
"""

from __future__ import annotations

import math
import re
from datetime import date as _Date
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError

from archimedes.api.account_auth import CurrentUser, require_current_user
from archimedes.api.limiter import limiter
from archimedes.services.rigor_evaluator import (
    DSR_MIN_BARS,  # the passport gate's OWN sample floor (#1803) — never a second number
    _resolve_gate_rf,  # the SAME once-up-front resolution run_rigor_gate uses (#1409 review fix)
    compute_dsr_hac_and_iid,
    compute_in_sample_sharpe,
    compute_oos_sharpe,
)
from archimedes.services.rigor_profiles import DSR_P_FLOOR, OOS_ABS_FLOOR


def _input_rejected_response(exc: RequestValidationError) -> JSONResponse:
    """Render a request-validation failure as ONE object with a reason code.

    Two things FastAPI's default renderer does are wrong for this route (#1803):

    1. **It cannot render this route's worst input at all.** Every entry it
       emits carries ``input`` — the offending value, verbatim. JSON's ``NaN``
       and ``Infinity`` literals parse into Python floats, so a poisoned bar
       reaches the validator, the validator correctly refuses it, and then
       ``JSONResponse`` (``json.dumps(..., allow_nan=False)``) raises while
       rendering the refusal — turning a clean 422 into a 500. Fail-closed has
       to mean the refusal is *deliverable*.
    2. **It echoes the whole payload back.** ``input`` on a ``returns`` error is
       the entire series, so a 122 KB request produced a 122 KB rejection.

    So this router answers with ``{"detail": {"error": "input_rejected",
    "reason": "<code>", ...}}``: the code an agent branches on, the sentence a
    human reads, and the field that failed — and nothing echoed back. Anything
    that is not one of this route's own codes keeps FastAPI's list shape, minus
    ``input``/``ctx``, for the same two reasons.
    """
    errors = exc.errors()
    coded = [entry for entry in errors if entry.get("type") in INPUT_REJECTED_CODES]
    if coded:
        first = coded[0]
        detail: dict[str, object] = {
            "error": "input_rejected",
            "reason": first.get("type"),
            # Pydantic collects every field error in one pass; the first
            # is what to fix first, and the full set is here so a caller
            # is not made to fix them one round trip at a time.
            "reasons": sorted({entry.get("type") for entry in coded}),
            "message": first.get("msg", ""),
            "loc": [str(part) for part in first.get("loc", ())],
        }
        # The window refusal carries its two numbers as FIELDS as well as in the
        # sentence, so a caller can decide "fetch more history" without parsing
        # English (owner decision, #1803). These are the only structured values
        # this renderer emits, and both are integers this route computed — a row
        # COUNT and a constant — never a value echoed back out of the payload,
        # so rule 1 above (a NaN in `input` is unserialisable) still holds.
        ctx = first.get("ctx") or {}
        for key in ("bars_received", "bars_required"):
            value = ctx.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                detail[key] = value
        return JSONResponse(status_code=422, content={"detail": detail})
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "type": entry.get("type"),
                    "loc": [str(part) for part in entry.get("loc", ())],
                    "msg": entry.get("msg"),
                }
                for entry in errors
            ]
        },
    )


class _InputRejectionRoute(APIRoute):
    """Scopes :func:`_input_rejected_response` to this router and nothing else.

    A FastAPI exception handler is app-wide; the 422 shape of every other route
    in the app is not this change's business, and quietly rewriting it would be.
    ``route_class`` is the documented way to keep the override where it belongs.
    """

    def get_route_handler(self):
        original_route_handler = super().get_route_handler()

        async def rejection_aware_handler(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except RequestValidationError as exc:
                return _input_rejected_response(exc)

        return rejection_aware_handler


rigor_verify_router = APIRouter(prefix="/api/rigor", tags=["rigor"], route_class=_InputRejectionRoute)

# Legs that can ever be evaluated against a bare return series. PBO needs a
# selection set and the look-ahead audit needs inspectable source; neither
# exists on this transport, so both are structurally not_evaluable on every
# request (see the module docstring's honesty contract). `passes` is a quorum
# over RUNNABLE legs — never over "whichever legs happened to be evaluable",
# which is what let a 4-bar series pass on one leg of four (#1481).
_RUNNABLE_LEGS: tuple[str, ...] = ("dsr", "oos_consistency")
_STRUCTURALLY_NOT_RUN_LEGS: tuple[str, ...] = ("pbo", "look_ahead")
_LEGS_TOTAL = len(_RUNNABLE_LEGS) + len(_STRUCTURALLY_NOT_RUN_LEGS)

CheckStatus = Literal["pass", "fail", "not_evaluable"]

_PBO_NOT_EVALUABLE_REASON = (
    "PBO (probability of backtest overfitting, Bailey et al. 2014 CSCV) is a property "
    "of a SELECTION SET, not one series — it requires a trial matrix of multiple "
    "candidate strategies' returns over the same window. A single returns series has "
    "no selection set to measure overfitting probability against. "
    "See POST /api/selection-bias/pbo for the trial-matrix form."
)
_LOOK_AHEAD_NOT_EVALUABLE_REASON = (
    "The look-ahead audit is AST-based static analysis of strategy SOURCE CODE; a "
    "bare returns series carries no code to inspect. Archimedes never executes or "
    "uploads strategy code server-side, so this check can only ever run locally "
    "(see `archimedes verify --local`)."
)


# ── Schemas ──────────────────────────────────────────────────


# ── Input rejection: the codes, and why each limit is where it is (#1803) ──
#
# This endpoint is the ONLY place in the product where a caller hands us a raw
# numeric series and asks for a verdict on it. Nothing here is executed,
# persisted, shelled or logged, so the attack surface is not code execution —
# it is the VERDICT. Two of the four gate legs are structurally not_evaluable
# on a bare series, so the two that DO run have to be arithmetically
# unshakeable, and every one of the limits below exists because an unchecked
# input could otherwise move a leg's answer without moving the data:
#
#   * `unsorted_dates`  — the walk-forward split is POSITIONAL
#     (`_rigor_helpers.compute_oos_sharpe` slices `arr[split:]`). Dates were
#     carried on the request but never used for ordering, so a caller could
#     sort their series by return and put their best 30% in the holdout:
#     `oos_consistency: pass` on the same numbers that fail chronologically.
#     We REJECT rather than silently sort. Sorting server-side would repair a
#     shuffled series into a passing one and hand back a verdict on a series
#     the caller never submitted; the caller has to be told their input was
#     not a time series, because "these are my returns, in order" is the
#     claim the OOS leg is grading.
#   * `duplicate_date`  — two bars on one date is not a daily series; it also
#     defeats the ordering check (a repeated date can be inserted anywhere
#     without breaking monotonicity).
#   * `invalid_date`    — strict `YYYY-MM-DD`. `date.fromisoformat` alone
#     accepts `20240102` and ISO week dates (`2024-W01-1`) on py>=3.11, which
#     the rf-series resolution (`rf_series.py`) would then have to guess at.
#   * `non_finite`      — JSON's `NaN`/`Infinity` literals parse into Python
#     floats. The DSR/OOS legs catch them and answer `not_evaluable`, but
#     `in_sample_sharpe` rendered `null`, which reads as "unavailable" rather
#     than "you poisoned the input". Rejected at the boundary instead.
#   * `out_of_range`    — |r| <= 1.0. A simple daily return cannot be below
#     -1.0 (an unlevered position cannot lose more than everything), and
#     +1.0 — a 100% gain in one bar — is the honest ceiling for a DAILY
#     series: anything past it is a decimal-shifted or already-annualized
#     column, not a daily return, and it silently inflates the Sharpe the
#     whole verdict rests on. A genuinely levered series that exceeds it is
#     refused loudly rather than graded wrongly.
#   * `window_too_short` — the MINIMUM EVALUATION WINDOW: 250 daily bars, one
#     trading year (owner decision, #1803). Below it the endpoint returns a
#     TYPED REFUSAL and nothing else — never a verdict, and never a verdict
#     with a warning label on it, because a warning beside a `passes` field
#     is read as a verdict by every consumer that branches on the field. The
#     refusal names `bars_received` and `bars_required`, in the sentence and
#     as fields. Note this floor is a PRODUCT rule, strictly above the gate's
#     arithmetic floors (DSR needs 4 bars, the walk-forward split ~70): at
#     250 both runnable legs can actually run, so a shortness-driven
#     INCOMPLETE is no longer reachable through this route at all.
#   * `too_many_rows`   — the #1749 payload cap, unchanged (see below).
#   * `trials_out_of_range` — `trials` is self-attested and deflates the DSR.
#     It had no upper bound, so `trials=10**18` drove the deflation to -inf
#     and the leg to `not_evaluable`: an unbounded, caller-controlled way to
#     turn a FAIL into "could not be evaluated". 10,000 is far past any real
#     parameter sweep and keeps the deflation finite.
#
# Each is raised as a `PydanticCustomError` whose ``type`` IS the code, which
# `_input_rejected_response` (above) turns into
# ``{"detail": {"error": "input_rejected", "reason": "<code>", "reasons": [...],
# "message": "...", "loc": [...]}}`` — a machine-readable reason next to the
# human sentence, read by the CLI (`archimedes verify`) and the MCP tool.
# Fail-closed throughout: every one of these is a refusal, never a truncation,
# a coercion, a re-sort or a silent accept.
INPUT_REJECTED_CODES: tuple[str, ...] = (
    "invalid_date",
    "duplicate_date",
    "unsorted_dates",
    "non_finite",
    "out_of_range",
    "window_too_short",
    "too_many_rows",
    "trials_out_of_range",
)

# Strict calendar-date form. Deliberately narrower than `date.fromisoformat`.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The honest ceiling on ONE DAILY bar, in simple-return units (0.01 = +1%).
_MAX_ABS_DAILY_RETURN = 1.0

# Self-attested trial count for the DSR deflation. An upper bound is what keeps
# the deflation finite; the value is not a statistical claim.
_MAX_TRIALS = 10_000


class ReturnPoint(BaseModel):
    """One bar: a strict ISO calendar date and its simple daily return.

    ``date`` is a real ``datetime.date`` (not a string), so the chronological
    invariant the OOS split depends on is established by PARSING, in the
    schema, before any arithmetic sees the series.
    """

    date: _Date
    daily_return: float

    @field_validator("date", mode="before")
    @classmethod
    def _strict_iso_date(cls, value: object) -> object:
        """Accept exactly ``YYYY-MM-DD`` (#1803).

        Runs before pydantic's own date coercion, which in lax mode also
        accepts an int/float epoch and (via ``date.fromisoformat`` on
        py>=3.11) ``20240102`` and ``2024-W01-1``. A returns series whose date
        column is a unix timestamp is a different thing from the one this
        endpoint grades, and the rf-series resolution downstream would have to
        guess which.
        """
        if isinstance(value, _Date):
            return value
        if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
            raise PydanticCustomError(
                "invalid_date",
                "date must be a strict ISO calendar date, YYYY-MM-DD (got {got!r}). "
                "Epoch seconds, ISO week dates and YYYYMMDD are refused rather than guessed at.",
                {"got": value},
            )
        try:
            return _Date.fromisoformat(value)
        except ValueError as exc:
            raise PydanticCustomError(
                "invalid_date",
                "date {got!r} is well-formed YYYY-MM-DD but is not a real calendar date.",
                {"got": value},
            ) from exc

    @field_validator("daily_return", mode="after")
    @classmethod
    def _finite_and_in_range(cls, value: float) -> float:
        """Reject NaN/±Infinity and any |r| > 1.0 (#1803)."""
        if not math.isfinite(value):
            raise PydanticCustomError(
                "non_finite",
                "daily_return must be a finite number; got {got}. JSON's NaN/Infinity literals "
                "parse, but they cannot be graded — a non-finite bar is refused rather than "
                "rendered as an 'unavailable' metric.",
                {"got": repr(value)},
            )
        if abs(value) > _MAX_ABS_DAILY_RETURN:
            raise PydanticCustomError(
                "out_of_range",
                "daily_return {got} is outside [-{limit}, {limit}] (simple return units, "
                "0.01 = +1%). A daily bar cannot lose more than 100%, and a >100% single-day "
                "gain means the column is percentages, annualized figures or prices — not daily "
                "returns. Refused rather than graded.",
                {"got": value, "limit": _MAX_ABS_DAILY_RETURN},
            )
        return value


# #1749: the size ceiling on a verify payload belongs to the APPLICATION, at a
# row count we chose, instead of being set implicitly by whatever byte count the
# edge happens to enforce.
#
# Until this cap existed, `returns` had `min_length=1` and no upper bound, so the
# only ceiling anywhere in the path was AWS WAF's `SizeRestrictions_BODY` —
# 8,192 bytes on a REGIONAL web ACL fronting an ALB. Measured (dogfood
# 2026-09-01, agent-cli): 160 rows = 8,076 B -> 200; 165 rows = 8,326 B -> 403.
# That is 50.5 bytes per serialised row including the envelope, so the limit is
# crossed at ~163 rows and one trading year (252 bars, ~12.7 KB extrapolated)
# 403'd at the edge with an `awselb/2.0` HTML body the CLI could only report as
# a generic http_error. See infra/waf.tf for the scoped edge fix.
#
# 2,600 rows is a decade of daily bars (10 x 252 = 2,520, plus headroom for
# leap-year/exchange-calendar variation). A full-cap payload measures ~122 KB of
# JSON with 6-decimal returns and extrapolates to ~131 KB at the 50.5 B/row seen
# in production — well under nginx's implicit 1 MB `client_max_body_size`
# default (nginx/nginx.conf sets none; nginx is the ALB target, infra/ecs.tf),
# which with the WAF exception in place is now the only BYTE ceiling on this
# path. Note this is a ROW cap: FastAPI buffers and json-parses the entire body
# before dependencies or field validation run, so an over-cap — or
# unauthenticated — request is still parsed in full first. It is also small
# enough that the DSR/OOS math stays sub-second.
#
# Fail-closed: over the cap is a 422 with a message that names the limit, the
# count received and the reason — never a truncation, never a silent accept.
_MAX_RETURN_ROWS = 2600

# ── The minimum evaluation window (owner decision, #1803) ───────────────
#
# 250 daily bars: one trading year (252 US sessions, less two days of headroom
# for a holiday-short year or a series that starts mid-week). This is a PRODUCT
# floor, not an arithmetic one, and it sits well above both arithmetic floors —
# the DSR is computable from `DSR_MIN_BARS` (4) bars and the walk-forward split
# from ~70. Those two say when the math CAN run; this says when the answer is
# worth publishing as a verdict.
#
# Below it the endpoint returns a TYPED REFUSAL — 422, `reason:
# window_too_short`, with `bars_received` and `bars_required` — and never a
# verdict. Not a verdict with a warning attached, either: `passes` is a field
# consumers branch on (the CLI exits on it, the MCP tool reports it, CI gates on
# it), and a warning string beside it is read by exactly none of them. A route
# that refuses `20240102` as a date rather than guessing must not hand back a
# graded `passes` on 30 bars with a caveat in prose.
#
# What it buys, concretely: at 250 bars BOTH runnable legs can actually run, so
# the 4..69-bar hole — accepted, DSR-only, `legs_evaluated: 1`, INCOMPLETE — is
# no longer reachable through this route. (`legs_evaluated < legs_runnable` is
# still possible on a DEGENERATE series, e.g. zero variance; that is a property
# of the numbers, not of their count, and it stays honestly reported.)
_MIN_WINDOW_BARS = 250

# The window floor must never sit BELOW the gate's own DSR sample floor — that
# would accept a series whose DSR leg cannot run for want of bars. Written as a
# `max` rather than asserted, so the relationship holds by construction if
# `DSR_MIN_BARS` is ever raised past 250 rather than being a comment that rots.
_MIN_RETURN_ROWS = max(_MIN_WINDOW_BARS, DSR_MIN_BARS)


class RigorVerifyRequest(BaseModel):
    # `min_length`/`max_length` are the declarative contract (they land in the
    # OpenAPI schema and are the backstop if the validators below are ever
    # removed); the mode="before" validator runs first and is what produces the
    # explicit message and the machine-readable reason code, because pydantic's
    # own length errors ("List should have at most 2600 items after
    # validation") do not tell the caller what to do.
    # (Pydantic types its own `min_length` failure `too_short` and its
    # `max_length` failure `too_long`. NEITHER is one of this route's codes —
    # `too_short` was, until the window floor replaced it with
    # `window_too_short`, whose message and `bars_*` fields the generic error
    # cannot produce. So if either validator below is removed the backstop
    # still REFUSES the body, it just degrades to the generic list shape. The
    # validators are the primary; the constraints are the fail-closed floor
    # under them, and they are what lands in the published OpenAPI schema.)
    returns: list[ReturnPoint] = Field(
        min_length=_MIN_RETURN_ROWS,
        max_length=_MAX_RETURN_ROWS,
        description=(
            f"The daily return series, oldest bar first. {_MIN_RETURN_ROWS}..{_MAX_RETURN_ROWS} rows: "
            f"the floor is the minimum evaluation window — one trading year — below which the "
            f"endpoint returns a typed refusal (422, reason `window_too_short`, with "
            f"`bars_received`/`bars_required`) rather than a verdict; the ceiling is a payload cap "
            f"(~10 years of daily bars). Dates must be strict YYYY-MM-DD, unique and strictly "
            f"ascending — the walk-forward split is positional, so an out-of-order series is "
            f"refused, never sorted."
        ),
    )
    trials: int = Field(
        default=1,
        ge=1,
        le=_MAX_TRIALS,
        description=(
            f"Self-attested trial count for the DSR deflation, 1..{_MAX_TRIALS}. Unverifiable by "
            "construction, but bounded: an unbounded count drives the deflation to -inf and turns "
            "a FAIL into 'not_evaluable' (#1803)."
        ),
    )

    @field_validator("returns", mode="before")
    @classmethod
    def _row_count_bounds(cls, value: object) -> object:
        """Reject over-long (#1749) and under-window (#1803) series by row count.

        Runs before any per-row parsing so the cheapest refusal happens first
        and the message names the limit, the count received and the reason.
        """
        if isinstance(value, list):
            if len(value) > _MAX_RETURN_ROWS:
                raise PydanticCustomError(
                    "too_many_rows",
                    "returns has {n} rows; the maximum is {limit} (~10 years of daily bars). "
                    "This is a payload cap, not a statistical one: split the series or aggregate "
                    "to a coarser frequency.",
                    {"n": len(value), "limit": _MAX_RETURN_ROWS},
                )
            if len(value) < _MIN_RETURN_ROWS:
                raise PydanticCustomError(
                    "window_too_short",
                    "returns has {bars_received} rows; the minimum evaluation window is "
                    "{bars_required} daily bars (one trading year). A shorter series is REFUSED, "
                    "not graded: this endpoint returns a verdict or a refusal, never a verdict "
                    "with a warning on it. Send at least {bars_required} bars, or use a longer "
                    "history — nothing is padded, extrapolated or annualized on your behalf.",
                    {"bars_received": len(value), "bars_required": _MIN_RETURN_ROWS},
                )
        return value

    @field_validator("returns", mode="after")
    @classmethod
    def _unique_and_chronological(cls, value: list[ReturnPoint]) -> list[ReturnPoint]:
        """Reject duplicate dates and any non-ascending pair (#1803).

        Rejects rather than sorts, deliberately. ``compute_oos_sharpe`` splits
        POSITIONALLY, so accepting a shuffled series and quietly sorting it
        would grade a different series from the one submitted, while accepting
        it unsorted lets a caller choose which 30% of their bars land in the
        holdout. Refusing is the only answer that leaves the caller's own claim
        ("this is my return series, in time order") intact.
        """
        seen: set[_Date] = set()
        for point in value:
            if point.date in seen:
                raise PydanticCustomError(
                    "duplicate_date",
                    "returns contains more than one row dated {got}. A daily series has one bar "
                    "per date; duplicates are refused rather than deduplicated, summed or "
                    "averaged.",
                    {"got": point.date.isoformat()},
                )
            seen.add(point.date)

        for index in range(1, len(value)):
            if value[index].date < value[index - 1].date:
                raise PydanticCustomError(
                    "unsorted_dates",
                    "returns must be in ascending date order; row {index} ({got}) precedes row "
                    "{prev_index} ({prev}). The walk-forward out-of-sample split is positional, "
                    "so row order IS the time order it grades — a shuffled series is refused "
                    "rather than sorted, because sorting would return a verdict on a series you "
                    "did not send.",
                    {
                        "index": index,
                        "got": value[index].date.isoformat(),
                        "prev_index": index - 1,
                        "prev": value[index - 1].date.isoformat(),
                    },
                )
        return value

    @field_validator("trials", mode="before")
    @classmethod
    def _trials_bounds(cls, value: object) -> object:
        """Bound the self-attested trial count (#1803).

        Runs in ``mode="before"``, on the RAW JSON value, so it has to widen the
        value the same way pydantic's lax mode is about to before it can bound it.
        An earlier version tested only ``isinstance(value, int)``, and every other
        spelling of a huge count slipped past it onto the declarative ``le`` —
        whose error type is ``less_than_equal``, not one of this route's codes, so
        the refusal came back in the GENERIC list shape and the documented
        ``trials_out_of_range`` envelope never reached the caller. Both spellings
        are ordinary: ``json.dumps(10**18)`` emits ``1e+18``, and a form-ish client
        sends ``"10000000000"``.

        A bool is refused outright rather than widened. Pydantic's lax mode reads
        ``True`` as ``1``, so ``trials: true`` used to return 200 with ``trials: 1``
        — a route that refuses ``"20240102"`` as a date rather than guessing at it
        must not guess a boolean into a count.

        Anything that is not a number at all is returned untouched: pydantic's own
        ``int_parsing`` error is the right answer for it, and inventing a range
        refusal for a value that has no range would be a wrong reason code.
        """
        if isinstance(value, bool):
            raise PydanticCustomError(
                "trials_out_of_range",
                "trials must be a whole number between 1 and {limit}; got the boolean {got}. It is "
                "a count of variants tried, and a boolean is not one — refused rather than read as "
                "0 or 1.",
                {"got": value, "limit": _MAX_TRIALS},
            )
        candidate: int | float | None = None
        if isinstance(value, (int, float)):
            candidate = value
        elif isinstance(value, str):
            try:
                candidate = float(value.strip())
            except ValueError:
                candidate = None
        if candidate is None:
            return value
        # `math.isfinite` on an arbitrarily large int would itself raise
        # (OverflowError), so the finiteness test is scoped to floats; a huge int
        # compares exactly and is caught by the range test below.
        non_finite = isinstance(candidate, float) and not math.isfinite(candidate)
        if non_finite or not 1 <= candidate <= _MAX_TRIALS:
            raise PydanticCustomError(
                "trials_out_of_range",
                "trials must be between 1 and {limit}; got {got}. It is self-attested and "
                "unverifiable, but it is not unbounded: a huge count drives the DSR deflation to "
                "-inf, which turns a FAIL into 'not_evaluable'.",
                {"got": value, "limit": _MAX_TRIALS},
            )
        return value


class RigorCheck(BaseModel):
    status: CheckStatus
    reason: str | None = None


class DsrCheckResult(RigorCheck):
    deflated_sharpe: float | None = None
    dsr_p_value: float | None = None


class OosConsistencyResult(RigorCheck):
    oos_sharpe: float | None = None
    in_sample_sharpe: float | None = None


class RigorVerifyResponse(BaseModel):
    passes: bool
    trials: int
    self_attested: bool = True
    n_bars: int
    # #1481: `passes` alone overstated the evidence. These make the scalar
    # qualifiable without the caller re-deriving leg statuses.
    legs_evaluated: int
    legs_runnable: int
    legs_total: int
    legs_not_run: list[str]
    verdict_capped: bool
    dsr: DsrCheckResult
    pbo: RigorCheck
    oos_consistency: OosConsistencyResult
    look_ahead: RigorCheck
    rf_convention: str = Field(
        description=(
            "'excess_tbill_series' when the request's per-bar `date`s all resolved against the "
            "vendored historical 3-month T-bill series (FRED DGS3MO), 'excess_flat_fallback' when "
            "any date fell outside its coverage — rides the same disclosure `run_rigor_gate` uses "
            "(issue #1409). Resolved ONCE, up front, and the SAME resolved dates are threaded into "
            "every check below, so this can never disagree with the DSR/OOS arithmetic that used it."
        )
    )


# ── Check evaluators (thin wrappers over the SAME gate functions) ──────


def _evaluate_dsr(daily_returns: list[float], trials: int, dates: list[str] | None = None) -> DsrCheckResult:
    """DSR, deflated by the declared (self-attested) trial count.

    Calls the identical ``compute_dsr_hac_and_iid`` the passport gate calls
    (Newey-West HAC standard error, ``average_correlation=0.0`` — a bare
    series carries no cohort/variant-pool correlation context to supply, the
    same conservative default ``run_rigor_gate`` falls back to). Gated against
    ``DSR_P_FLOOR`` — the always-on floor, not a strictness-level threshold,
    since this endpoint takes no strictness parameter.

    Args:
        dates: The ALREADY-RESOLVED per-bar date index (issue #1409 review
            fix) — ``None`` when ``_resolve_gate_rf`` determined the request
            falls back to flat, never the caller's raw dates. See the module
            docstring's "rf convention" note for why this must be the
            resolved value, not a second independent call.
    """
    deflated_sharpe, p_value, _dsr_iid, _p_iid = compute_dsr_hac_and_iid(
        daily_returns, trials, average_correlation=0.0, hac_lags="auto", dates=dates
    )
    if deflated_sharpe is None or p_value is None or not math.isfinite(deflated_sharpe) or not math.isfinite(p_value):
        return DsrCheckResult(
            status="not_evaluable",
            reason="return series too short or degenerate for DSR (need >= 4 bars with nonzero variance)",
        )
    passed = p_value >= DSR_P_FLOOR
    reason = (
        f"self-attested trials={trials}: DSR p-value {p_value:.4f} "
        f"{'>=' if passed else '<'} floor {DSR_P_FLOOR:.2f} (Newey-West HAC standard error)"
    )
    return DsrCheckResult(
        status="pass" if passed else "fail",
        reason=reason,
        deflated_sharpe=deflated_sharpe,
        dsr_p_value=p_value,
    )


def _evaluate_oos_consistency(daily_returns: list[float], dates: list[str] | None = None) -> OosConsistencyResult:
    """Walk-forward OOS consistency — the same chronological 70/30 holdout the
    passport gate uses (``compute_oos_sharpe``), gated against
    ``OOS_ABS_FLOOR`` — the identical always-on floor
    ``RigorGateResult.blocked_by_floor`` enforces (a strategy that loses money
    out-of-sample is broken, not merely riskier).

    Args:
        dates: The ALREADY-RESOLVED per-bar date index — see
            ``_evaluate_dsr``'s docstring for why this must be the resolved
            value, not the caller's raw dates.
    """
    oos_sharpe = compute_oos_sharpe(daily_returns, dates=dates)
    if oos_sharpe is None or not math.isfinite(oos_sharpe):
        return OosConsistencyResult(
            status="not_evaluable",
            reason=(
                "insufficient data for a walk-forward OOS split "
                "(need >= 10 bars total and >= 21 OOS bars, or a degenerate slice)"
            ),
        )
    in_sample_sharpe = compute_in_sample_sharpe(daily_returns, dates=dates)
    # #1803: the SAME isfinite guard the OOS and DSR legs carry. Without it a
    # non-finite in-sample Sharpe rendered as `null`, which a reader cannot
    # distinguish from "this metric was not computed" — the leg reports itself
    # not_evaluable instead of publishing a pass/fail beside a poisoned number.
    # Non-finite bars are refused at the schema now, so this is the
    # defence-in-depth layer, not the primary one.
    if in_sample_sharpe is not None and not math.isfinite(in_sample_sharpe):
        return OosConsistencyResult(
            status="not_evaluable",
            reason=(
                "in-sample Sharpe is not finite — the in-sample slice is degenerate. The "
                "out-of-sample Sharpe alone is not the consistency check (the check is the "
                "PAIR), so this leg reports not_evaluable rather than a verdict."
            ),
            oos_sharpe=oos_sharpe,
        )
    passed = oos_sharpe > OOS_ABS_FLOOR
    if passed:
        reason = f"walk-forward OOS Sharpe {oos_sharpe:.4f} > floor {OOS_ABS_FLOOR:.2f} (chronological 70/30 holdout)"
    else:
        reason = (
            f"walk-forward OOS Sharpe {oos_sharpe:.4f} <= floor {OOS_ABS_FLOOR:.2f} "
            "— strategy loses money out-of-sample"
        )
    return OosConsistencyResult(
        status="pass" if passed else "fail",
        reason=reason,
        oos_sharpe=oos_sharpe,
        in_sample_sharpe=in_sample_sharpe,
    )


# ── Endpoint ─────────────────────────────────────────────────


@rigor_verify_router.post("/verify", response_model=RigorVerifyResponse)
@limiter.limit("5/minute")
async def verify_rigor(
    request: Request,  # noqa: ARG001 — slowapi @limiter.limit inspects param name
    response: Response,  # noqa: ARG001 — slowapi injects rate-limit headers into it; omitting it 500s every SUCCESSFUL call (#1182)
    body: RigorVerifyRequest,
    user: CurrentUser = Depends(require_current_user),  # noqa: ARG001 — auth gate only; verdict is per-request
):
    """Grade a bare daily-return series against the two gate legs it can support.

    The verdict is CAPPED: PBO needs a selection set and the look-ahead audit
    needs source code, so both are always `not_evaluable` here and
    `verdict_capped` is always `true`. `passes` is a quorum — true only when
    DSR **and** walk-forward OOS both ran and both passed.

    **Minimum evaluation window: 250 daily bars (one trading year).** A shorter
    series is refused with **422** and
    `{"detail": {"error": "input_rejected", "reason": "window_too_short",
    "bars_received": N, "bars_required": 250, "message": "…"}}` — a typed
    refusal, never a verdict and never a warning-labelled verdict. The rest of
    the input contract is equally strict and repairs nothing: strict
    `YYYY-MM-DD` dates, unique and strictly ascending (the walk-forward split is
    positional, so an out-of-order series is refused rather than sorted), finite
    returns with `abs(r) <= 1.0` in simple-return units, at most 2,600 rows, and
    `1 <= trials <= 10000`. Every refusal carries a stable `detail.reason`:
    `invalid_date`, `duplicate_date`, `unsorted_dates`, `non_finite`,
    `out_of_range`, `window_too_short`, `too_many_rows`, `trials_out_of_range`.

    `trials` is self-attested and unverifiable by construction; it deflates the
    DSR, and the response echoes it with `self_attested: true`.
    """
    daily_returns = [pt.daily_return for pt in body.returns]
    # The PARSED dates, re-serialised to canonical `YYYY-MM-DD` — never the
    # caller's raw text (#1803). The schema has already established that they
    # are real calendar dates, unique and strictly ascending, so this list IS
    # the chronological index that the positional 70/30 split inside
    # `compute_oos_sharpe` is implicitly keyed on: row order and time order are
    # the same thing here by construction, not by hope.
    parsed_dates = [pt.date.isoformat() for pt in body.returns]

    # #1409 review fix: resolve the ONE rf convention this WHOLE response
    # discloses, once, up front — the same `_resolve_gate_rf` `run_rigor_gate`
    # uses (rigor_evaluator.py). `resolved_dates` (never `parsed_dates`) is
    # threaded into every downstream call below, so `rf_convention` and the
    # arithmetic that produced `dsr`/`oos_consistency` can never disagree.
    resolved_dates, rf_convention = _resolve_gate_rf(parsed_dates, len(daily_returns))

    dsr_check = _evaluate_dsr(daily_returns, body.trials, dates=resolved_dates)
    oos_check = _evaluate_oos_consistency(daily_returns, dates=resolved_dates)
    pbo_check = RigorCheck(status="not_evaluable", reason=_PBO_NOT_EVALUABLE_REASON)
    look_ahead_check = RigorCheck(status="not_evaluable", reason=_LOOK_AHEAD_NOT_EVALUABLE_REASON)

    leg_status = {
        "dsr": dsr_check.status,
        "pbo": pbo_check.status,
        "oos_consistency": oos_check.status,
        "look_ahead": look_ahead_check.status,
    }
    runnable_statuses = [leg_status[leg] for leg in _RUNNABLE_LEGS]
    legs_evaluated = sum(1 for st in runnable_statuses if st != "not_evaluable")

    # QUORUM over runnable legs, not "all evaluable legs" (#1481). Every
    # runnable leg must actually have run AND passed. A partially-evaluated
    # series (DSR at 4 bars, OOS still short) is not a pass — it is an
    # incomplete evaluation, and `passes` must not launder it into a verdict.
    passes = legs_evaluated == len(_RUNNABLE_LEGS) and all(st == "pass" for st in runnable_statuses)

    legs_not_run = [leg for leg, st in leg_status.items() if st == "not_evaluable"]

    return RigorVerifyResponse(
        passes=passes,
        trials=body.trials,
        self_attested=True,
        n_bars=len(daily_returns),
        legs_evaluated=legs_evaluated,
        legs_runnable=len(_RUNNABLE_LEGS),
        legs_total=_LEGS_TOTAL,
        legs_not_run=legs_not_run,
        # Always true on this transport: PBO and look-ahead can never run
        # here, so the verdict can never stand in for the passport gate.
        verdict_capped=True,
        dsr=dsr_check,
        pbo=pbo_check,
        oos_consistency=oos_check,
        look_ahead=look_ahead_check,
        rf_convention=rf_convention,
    )
