"""Intraday mark-to-market on open paper positions (design §§2–4, v1).

**What this is.** Every ``PAPER_MARKS_INTERVAL_MINUTES`` (default 15), for each
ACTIVE deployment, fetch the current price of each symbol in its universe,
apply it to the position set the DAILY replay last established, and write one
row saying "at 14:45 UTC this portfolio was worth index 1.0347". The user sees
a number that moves during the day.

**What this deliberately is NOT.** It re-prices; it never re-decides. This
module does not call ``run_dsl_backtest``, does not evaluate an entry/exit
condition, and never reads ``rebalance_frequency``. That restraint is the
whole safety argument, and it is an anti-goal, not an omission:

  - rebalance cadence is counted in BARS, not calendar time
    (``dsl_to_backtrader._REBALANCE_PERIOD_BARS`` — daily 1, weekly 5, monthly
    21), so feeding a 15-minute bar into the graded engine turns "weekly" into
    75 minutes and "monthly" into 5¼ hours, silently, with nothing raised;
  - indicator warmup is counted in bars too, so ``sma_200`` becomes a
    week-and-a-half average on equities and a two-day one on crypto;
  - this repo has already lived the failure — divergence audit F3, recorded at
    ``strategy_signal_evaluator.py:1095``: a fast tick loop re-decided a
    monthly-cadence spec on every one of ~288 ticks a day.

Re-pricing a position more often is a display change and is honest — it is
what a brokerage statement does between trades. Re-deciding it more often is a
DIFFERENT STRATEGY from the one the rigor gate graded, and would need
re-grading. That is v2, behind an ADR.

**The disclosed v1 limitation: a mark cannot see cash.** What is re-priced is
the strategy's ASSET BASKET — the sleeve weights the last daily settle
established — not its live position. ``replay_spec`` returns dated portfolio
returns, not a per-sleeve invested/flat vector, so this module has no way to
know that a strategy's exit condition fired and it is sitting in cash. It
marks it as if invested. A settled ``+0.00%`` day can therefore carry a
``+10.00%`` mark when the underlying rose 10% —
``test_a_cash_sleeve_is_still_marked_as_if_invested`` pins that exact fixture
so the gap cannot silently regress, and so a fix that closes it is forced to
update the pin rather than slip past it.

The error is bounded and self-correcting: it never touches
``paper_daily_returns``, and the next daily advance re-anchors the cache to
the ledger, so a mark can be wrong for at most one session and cannot
accumulate. That is what makes DISCLOSURE (the API doc, the route docstring,
and the UI at the point of render) the honest v1 answer rather than a guess at
the position. Closing it needs a per-sleeve position vector out of the graded
engine: the marks-v2 follow-up.

**Marks are not the track record.** ``paper_daily_returns`` remains
append-only-by-law and remains the recorded paper track record (Arc testnet,
no real funds). ``paper_marks`` is a decoration with a TTL and is safe to
delete wholesale — which is what makes ``rollup_and_prune``'s third tier
(``DELETE``) safe.

**On the lease, if a runner takes one.** ``RunnerLeaseGuard`` exists because
``oracle_runner`` and ``agent_runner`` are FUNDS-ADJACENT singletons where a
duplicate is a double-signed transaction. **Marks are not funds-adjacent.** No
money moves, and ``uq_paper_marks_dep_ts_gran`` makes a duplicate insert a
no-op. A lease is still worth taking, but for the honest reason: it stops two
copies from burning double the vendor quota during a deploy overlap. Copying
the funds-adjacent language across would be a false safety claim, and a false
safety claim is a defect even when the mechanism is identical.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func

from archimedes.models.paper_store import (
    DEFAULT_HOURLY_RETENTION_DAYS,
    DEFAULT_MARKS_INTERVAL_MINUTES,
    DEFAULT_MAX_ROWS_PER_DEPLOYMENT,
    DEFAULT_MAX_STALENESS_MINUTES,
    DEFAULT_RAW_RETENTION_DAYS,
    GRANULARITY_HOURLY,
    GRANULARITY_RAW,
    STATUS_ACTIVE,
    PaperDeployment,
    PaperMark,
)

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Parse an integer env var, failing SAFE to ``default`` (mirrors
    ``market_data_provider._int_env`` — same fail-safe convention: a config
    typo must not take down a long-lived loop)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("invalid %s=%r (not an integer) — falling back to %d", name, raw, default)
        return default


def interval_minutes() -> int:
    """The marks cadence. 15 by default: 26 marks across a US equity session,
    96 across a crypto day — enough points to draw a moving line, and a
    retention profile that stays bounded (§3.2). A knob rather than a constant
    because the tradeoff is a product call; the retention arithmetic moves
    with it."""
    return _int_env("PAPER_MARKS_INTERVAL_MINUTES", DEFAULT_MARKS_INTERVAL_MINUTES)


def max_staleness_minutes() -> int:
    """How old a bar may be and still contribute to a mark (default 60).

    **This knob is not monotonic on a mixed universe, and that is a known v1
    wart, not a bug to discover in production.** On a single-asset deployment,
    raising it does the obvious thing: more bars qualify, so marks keep being
    written for longer after the vendor goes quiet. On a MIXED equity+crypto
    deployment it does the opposite — a frozen equity leg that is still inside
    the window pins ``ts = min(fresh_bar_times)`` to its last bar, and every
    subsequent tick dedupes against the row already written at that timestamp.
    Raising the cap widens that window and therefore SUPPRESSES MORE crypto
    marks; lowering it lets the equity leg drop out sooner and the crypto
    cadence resume.

    See the long comment in ``mark_deployment`` for why the stamp and the
    dedupe key are the same value in v1 and what splitting them would cost.
    """
    return _int_env("PAPER_MARKS_MAX_STALENESS_MINUTES", DEFAULT_MAX_STALENESS_MINUTES)


def raw_retention_days() -> int:
    return _int_env("PAPER_MARKS_RAW_RETENTION_DAYS", DEFAULT_RAW_RETENTION_DAYS)


def hourly_retention_days() -> int:
    return _int_env("PAPER_MARKS_HOURLY_RETENTION_DAYS", DEFAULT_HOURLY_RETENTION_DAYS)


def max_rows_per_deployment() -> int:
    return _int_env("PAPER_MARKS_MAX_ROWS_PER_DEPLOYMENT", DEFAULT_MAX_ROWS_PER_DEPLOYMENT)


def _aware(ts: datetime) -> datetime:
    """A tz-aware UTC view of a stored timestamp.

    SQLite drops the offset on a ``DateTime(timezone=True)`` column and hands
    back a naive value, so every comparison against ``datetime.now(UTC)``
    would raise there while working on Postgres. Normalizing on read keeps the
    staleness and retention arithmetic identical in both — which is the point
    of a hermetic SQLite test.
    """
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


# ─── Read side ─────────────────────────────────────────────────────────


def mark_to_dict(row: PaperMark) -> dict:
    """One mark, as the API renders it.

    ``ts`` is the UPSTREAM observation time, and it is named as such all the
    way to the client: the whole point of storing it is that a consumer can
    say "as of 14:45" and be right. ``prices`` carries only the legs that were
    actually fresh enough to contribute — a caller can therefore tell a
    fully-marked portfolio from a partially-frozen one instead of being handed
    an opaque number.
    """
    return {
        "ts": _aware(row.ts).isoformat(),
        "portfolio_value": row.portfolio_value,
        "source": row.source,
        "is_delayed": row.is_delayed,
        "granularity": row.granularity,
        "prices": json.loads(row.prices_json),
    }


def latest_mark(session, deployment_id: str) -> PaperMark | None:
    return (
        session.query(PaperMark).filter(PaperMark.deployment_id == deployment_id).order_by(PaperMark.ts.desc()).first()
    )


def list_marks(session, deployment_id: str, *, limit: int = 500) -> list[PaperMark]:
    """Marks for one deployment, OLDEST FIRST, capped at ``limit``.

    The cap is applied newest-first and then reversed, so a client that asks
    for fewer rows than exist gets the most RECENT window rather than the
    oldest — the opposite would hand back ancient history and no live value,
    which is the one thing this endpoint exists to provide.
    """
    rows = (
        session.query(PaperMark)
        .filter(PaperMark.deployment_id == deployment_id)
        .order_by(PaperMark.ts.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


# ─── The marks loop ────────────────────────────────────────────────────


def _load_cache(dep: PaperDeployment) -> dict | None:
    """The position set the daily advance stamped, or None. Malformed JSON is
    treated as absent (and logged) rather than raised: a corrupt cache costs
    this deployment its live value, never anyone else's mark and never a
    ledger row."""
    if not dep.position_cache_json:
        return None
    try:
        cache = json.loads(dep.position_cache_json)
    except (TypeError, ValueError):
        logger.warning("paper marks: deployment %s has an unreadable position cache — skipping", dep.id)
        return None
    if not isinstance(cache, dict) or not cache.get("weights") or not cache.get("ref_prices"):
        return None
    return cache


def mark_deployment(
    session,
    dep: PaperDeployment,
    *,
    quotes: dict[str, tuple[float, datetime]],
    tickers: dict[str, str],
    source: str,
    is_delayed: bool,
    now: datetime,
) -> PaperMark | None:
    """Value one deployment's cached position set and insert one mark, or not.

    Returns the inserted row, or ``None`` when no row was written. Every
    ``None`` path is a deliberate refusal, not a swallowed error:

      - **no position cache** — nothing to price;
      - **every leg stale or missing** — §2.4 rule 4. A missing mark is an
        honest gap; a duplicated stale mark is a fabricated flat line, so this
        writes NOTHING rather than repeating the last value;
      - **this exact bar is already marked** — the vendor has not printed a
        new bar since the last tick. Re-writing it under a fresh wall-clock
        would be the same fabrication in a different disguise;
      - **the row cap is already exceeded** — §3.2 guard 1.

    A leg that is stale but not *all* legs are (an equity outside the session
    beside a 24/7 crypto pair) is not fatal: v1 marks what it can. The stale
    leg contributes its weight at a ratio of exactly 1.0 — which is the true
    statement that a closed market's price has not moved — and is ABSENT from
    ``prices_json``, so "which symbols were actually fresh" stays recoverable
    rather than hidden behind one opaque number.
    """
    cache = _load_cache(dep)
    if cache is None:
        return None

    weights: dict[str, float] = cache["weights"]
    ref_prices: dict[str, float] = cache["ref_prices"]
    equity_index = float(cache.get("equity_index", 1.0))
    cutoff = now - timedelta(minutes=max_staleness_minutes())

    multiplier = 0.0
    observed: dict[str, float] = {}
    fresh_bar_times: list[datetime] = []
    for sym, weight in weights.items():
        ref = ref_prices.get(sym)
        ticker = tickers.get(sym)
        quote = quotes.get(ticker) if ticker else None
        if not ref or ref <= 0 or quote is None:
            multiplier += weight  # unpriceable leg: no move claimed
            continue
        price, bar_ts = quote
        bar_ts = _aware(bar_ts)
        if bar_ts < cutoff:
            multiplier += weight  # stale leg: frozen at the settled close
            continue
        multiplier += weight * (price / ref)
        observed[ticker] = price
        fresh_bar_times.append(bar_ts)

    if not fresh_bar_times:
        logger.info(
            "paper marks: deployment %s has no leg fresher than %d min — writing NO row "
            "(a missing mark is an honest gap; a repeated stale one is a fabricated flat line)",
            dep.id,
            max_staleness_minutes(),
        )
        return None

    # The mark is only as current as the OLDEST price inside it. Taking the
    # newest would let one live leg lend its timestamp to the whole portfolio —
    # a stale price wearing a fresh label, which is the defect this whole
    # module's timestamp discipline exists to prevent. Pinned by
    # `test_ts_is_the_oldest_fresh_leg_not_the_newest` (a min→max mutation
    # fails it).
    #
    # ── The known cost, stated rather than hidden (v1 accepted tradeoff) ──
    # This one value is BOTH the honesty stamp and the dedupe key: the row is
    # deduped on (deployment_id, ts, granularity) just below, and `ts` is what
    # is stored. On a MIXED universe those two jobs disagree.
    #
    # Concretely, at a 60-minute staleness cap: for the hour after an equity
    # close the equity leg's 19:45 bar is still "fresh", so it pins `ts` to
    # 19:45 on every tick while the crypto leg keeps printing new bars. Tick 1
    # writes the 19:45 row; ticks 2-4 compute the same `ts`, find that row, and
    # write NOTHING — roughly an hour of crypto marks is lost at each equity
    # close. Once the equity bar ages past the cap it drops out of
    # `fresh_bar_times` entirely and the crypto cadence resumes.
    #
    # That also makes the staleness knob NON-MONOTONIC on a mixed universe:
    # RAISING PAPER_MARKS_MAX_STALENESS_MINUTES produces FEWER marks, because it
    # widens the window in which a frozen equity leg pins the stamp. See
    # `max_staleness_minutes` and `test_a_mixed_universe_loses_marks_while_a_
    # frozen_leg_pins_the_stamp`, which pins the cadence as it actually is.
    #
    # Not fixed in v1, deliberately. Splitting the dedupe key from the stamp
    # (dedupe on max(fresh_bar_times), store min) cannot work against the
    # current schema: `uq_paper_marks_dep_ts_gran` is on the STORED ts, so two
    # ticks that agree on min and differ on max would collide on insert. A real
    # split needs a SECOND timestamp column and a migration — not a small
    # change, and not one to make while its only beneficiary (mixed
    # equity+crypto universes) is a minority of deployments. The alternative,
    # stamping max, is rejected outright: it buys cadence by letting the
    # portfolio claim to be as current as its freshest leg, which is the lie.
    # A missing mark is an honest gap; a mislabelled one is not. Tracked as
    # marks-v2 alongside the position vector.
    ts = min(fresh_bar_times)

    existing = (
        session.query(PaperMark)
        .filter(
            PaperMark.deployment_id == dep.id,
            PaperMark.ts == ts,
            PaperMark.granularity == GRANULARITY_RAW,
        )
        .first()
    )
    if existing is not None:
        logger.debug("paper marks: deployment %s already marked at %s — no new bar, no new row", dep.id, ts)
        return None

    count = session.query(func.count(PaperMark.id)).filter(PaperMark.deployment_id == dep.id).scalar() or 0
    cap = max_rows_per_deployment()
    if count >= cap:
        logger.error(
            "paper marks: deployment %s already holds %d rows (cap %d) — REFUSING the insert. "
            "Steady state under the retention policy is ~2,664 rows; this is a runaway loop, "
            "not growth.",
            dep.id,
            count,
            cap,
        )
        return None

    row = PaperMark(
        deployment_id=dep.id,
        ts=ts,
        prices_json=json.dumps(observed, sort_keys=True),
        portfolio_value=equity_index * multiplier,
        source=source,
        is_delayed=is_delayed,
        granularity=GRANULARITY_RAW,
    )
    session.add(row)
    session.flush()
    return row


def mark_all(session, *, now: datetime | None = None, provider=None) -> dict:
    """One tick: mark every ACTIVE deployment off ONE batched vendor call.

    ``status == STATUS_ACTIVE`` is the same filter ``advance_all`` uses, and
    it is a guard, not an optimization (§3.2 guard 3): a stopped deployment's
    track record is frozen by design, so its marks stop accumulating the
    moment the user hits Stop.

    The vendor-call count is driven by TICK CADENCE, not by deployment count
    or universe breadth, because the whole union of tickers goes out in one
    ``get_intraday_quotes_batch``. That is the honest cost argument for
    starting here: 26 calls/day for an equity session, 96 for crypto 24/7,
    against the ~1,440/day the oracle runner already makes.

    Seam: ``intraday`` (#1798) — live quotes, and the ``source`` stamped on
    every mark is that seam's vendor, so a daily-bar vendor flip leaves both
    the marks and their provenance untouched.
    """
    from archimedes.services import fusion_market_data
    from archimedes.services.market_data_provider import get_provider, intraday_is_delayed, provider_name

    now = now or datetime.now(UTC)
    deps = session.query(PaperDeployment).filter(PaperDeployment.status == STATUS_ACTIVE).all()

    per_deployment_tickers: dict[str, dict[str, str]] = {}
    union: dict[str, str] = {}
    markable: list[PaperDeployment] = []
    for dep in deps:
        cache = _load_cache(dep)
        if cache is None:
            continue
        try:
            spec = json.loads(dep.spec_json)
            resolved = fusion_market_data.resolve_universe(list(spec.get("asset_universe") or []))
        except Exception:
            logger.warning("paper marks: could not resolve universe for %s — skipping", dep.id, exc_info=True)
            continue
        # Only the legs the cache actually holds weights for; the universe
        # SSOT (resolve_universe) stays the boundary — marks never price an
        # arbitrary ticker.
        tickers = {sym: t for sym, t in resolved.items() if sym in cache["weights"]}
        if not tickers:
            continue
        per_deployment_tickers[dep.id] = tickers
        union.update({t: t for t in tickers.values()})
        markable.append(dep)

    if not union:
        return {"deployments": len(deps), "marked": 0, "skipped": len(deps), "tickers": 0}

    quotes = (provider or get_provider(seam="intraday")).get_intraday_quotes_batch(union)
    source = provider_name("intraday")
    is_delayed = intraday_is_delayed()

    marked = 0
    for dep in markable:
        try:
            if mark_deployment(
                session,
                dep,
                quotes=quotes,
                tickers=per_deployment_tickers[dep.id],
                source=source,
                is_delayed=is_delayed,
                now=now,
            ):
                marked += 1
        except Exception:
            # Same isolation contract as advance_all: one bad deployment must
            # not stall everyone else's marks.
            logger.exception("paper marks: marking crashed for %s", dep.id)
    return {
        "deployments": len(deps),
        "marked": marked,
        "skipped": len(deps) - marked,
        "tickers": len(union),
    }


# ─── Retention: roll up, prune, and say so out loud ────────────────────


def rollup_and_prune(session, *, now: datetime | None = None) -> dict:
    """Three tiers, and the third one is ``DELETE`` (§3.2).

      0–7 days     every raw 15-minute mark
      7–90 days    one mark per hour (the LAST mark in the hour)
      > 90 days    nothing — the rows are deleted

    Beyond 90 days there is nothing worth aggregating to: the daily close is
    already stored, authoritatively and permanently, in
    ``paper_daily_returns``. Rolling marks up to daily would duplicate the
    ledger with a less-trustworthy copy — a second source of truth for a fact
    the ledger already owns, which is worse than no copy at all.

    The rollup rewrites the surviving row IN PLACE as ``granularity='hourly'``
    rather than writing to a second table, so there is one table and one query
    shape; a re-run finds nothing left in ``'raw'`` for those hours and is a
    no-op, which the unique constraint also enforces independently.

    Every cycle LOGS its counts at INFO (§3.2 guard 2), including the
    post-prune table total, so the number that mattered for
    ``backtest_results`` — how big is this getting — is visible in CloudWatch
    without anyone running a query.
    """
    now = now or datetime.now(UTC)
    raw_cutoff = now - timedelta(days=raw_retention_days())
    hourly_cutoff = now - timedelta(days=hourly_retention_days())

    expired = session.query(PaperMark).filter(PaperMark.ts < hourly_cutoff).delete(synchronize_session=False)

    stale_raw = (
        session.query(PaperMark)
        .filter(PaperMark.granularity == GRANULARITY_RAW, PaperMark.ts < raw_cutoff)
        .order_by(PaperMark.ts.asc())
        .all()
    )
    buckets: dict[tuple[str, datetime], list[PaperMark]] = defaultdict(list)
    for row in stale_raw:
        buckets[(row.deployment_id, _aware(row.ts).replace(minute=0, second=0, microsecond=0))].append(row)

    promoted = 0
    rolled_up = 0
    for rows in buckets.values():
        keeper = max(rows, key=lambda r: _aware(r.ts))
        for row in rows:
            if row is keeper:
                continue
            session.delete(row)
            rolled_up += 1
        keeper.granularity = GRANULARITY_HOURLY
        promoted += 1
    session.flush()

    total = session.query(func.count(PaperMark.id)).scalar() or 0
    logger.info(
        "paper marks prune: %d expired past %dd deleted, %d raw rows rolled up into %d hourly rows "
        "(raw retention %dd); paper_marks now holds %d rows",
        expired,
        hourly_retention_days(),
        rolled_up,
        promoted,
        raw_retention_days(),
        total,
    )
    return {
        "deleted_expired": expired,
        "deleted_rolled_up": rolled_up,
        "promoted_hourly": promoted,
        "total_rows": total,
    }
