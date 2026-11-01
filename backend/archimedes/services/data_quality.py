"""Data-quality / fetch-verification harness for the backtest universe (#772).

Before a ticker is admitted to ``GLOBAL_ASSETS`` — the universe over which the
Tier-1 rigor gate's per-strategy Deflated Sharpe Ratio (Bailey & López de Prado
2014) and the library-level Probability of Backtest Overfitting (CSCV; Bailey,
Borwein, López de Prado & Zhu 2014) are computed — its price history must be
*clean*. Both statistics assume a gap-free, survivorship-aware return series;
violate that and the realized Sharpe is silently inflated and the gate is
poisoned. yfinance's long tail (the #759 expansion to 200–300 instruments) is
exactly where this bites: delisted names vanish, histories start late, and the
fetch layer pads/truncates partial series.

This harness verifies four properties per instrument over the backtest window:

  1. **fetchable**     — a non-empty series comes back at all;
  2. **sufficient history** — it starts near ``start`` and covers ≥ a minimum
     fraction of the window's trading days (no late-IPO / short-history bias);
  3. **gap-free**      — within [first, last] the fraction of missing trading
     days is below a tolerance (holidays are ~3.5% of business days, so the
     default 10% tolerance admits real calendars but rejects true gaps);
  4. **survivorship-clean** — the series still trades near ``end`` (a series
     that stops early is a likely delisting → survivorship bias).

A ticker is admitted only if all four hold. We never pad or truncate to force a
pass — bad data is flagged and excluded (#772 anti-goal).

The single yfinance call is isolated in ``_download_close`` so tests mock exactly
one boundary (or inject ``_downloader=``) and never touch the network.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd
from pandas.tseries.offsets import BusinessDay

logger = logging.getLogger(__name__)

# Defaults — deliberately conservative for a rigor-gate precondition.
DEFAULT_EDGE_TOLERANCE_DAYS = 7  # business days of slack at each window edge
DEFAULT_MAX_GAP_RATIO = 0.10  # > 10% missing trading days within span → gappy
DEFAULT_MIN_WINDOW_COVERAGE = 0.90  # obs / expected trading days over the window

Downloader = Callable[[str, pd.Timestamp, pd.Timestamp], "pd.Series"]


@dataclass(frozen=True)
class InstrumentVerdict:
    """Per-instrument data-quality verdict over a backtest window."""

    ticker: str
    ok: bool
    fetchable: bool
    n_obs: int
    expected_obs: int
    coverage_ratio: float  # n_obs / expected trading days over [start, end]
    gap_count: int  # missing trading days within [first_obs, last_obs]
    gap_ratio: float  # gap_count / trading days in [first_obs, last_obs]
    first_obs: str | None
    last_obs: str | None
    sufficient_history: bool
    survivorship_ok: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class UniverseReport:
    """Aggregate verdict over a set of tickers."""

    admitted: tuple[str, ...]
    rejected: dict[str, tuple[str, ...]]  # ticker -> reasons
    verdicts: dict[str, InstrumentVerdict] = field(default_factory=dict)


def _download_close(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """The sole yfinance boundary: daily adjusted-close over [start, end].

    Returns an empty Series on any failure/empty response (treated as
    unfetchable). Isolated so tests mock here and never hit the network.
    """
    try:
        import yfinance as yf

        data = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if data is None or data.empty:
            return pd.Series(dtype=float)
        close = data["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        return close.dropna()
    except Exception as e:  # pragma: no cover - network/library failure path
        logger.warning("data_quality: download failed for %s: %s", ticker, e)
        return pd.Series(dtype=float)


def verify_instrument(
    ticker: str,
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    edge_tolerance_days: int = DEFAULT_EDGE_TOLERANCE_DAYS,
    max_gap_ratio: float = DEFAULT_MAX_GAP_RATIO,
    min_window_coverage: float = DEFAULT_MIN_WINDOW_COVERAGE,
    _downloader: Downloader | None = None,
) -> InstrumentVerdict:
    """Verify one instrument's price history over [start, end].

    Returns an :class:`InstrumentVerdict`; ``ok`` is True only when the series is
    fetchable, has sufficient history, is gap-free within tolerance, and is
    survivorship-clean. ``reasons`` lists every failed check (empty when ``ok``).
    """
    download = _downloader or _download_close
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    expected = max(1, len(pd.bdate_range(start_ts, end_ts)))
    tol = BusinessDay(edge_tolerance_days)

    series = download(ticker, start_ts, end_ts)
    if series is None or len(series) == 0:
        return InstrumentVerdict(
            ticker=ticker,
            ok=False,
            fetchable=False,
            n_obs=0,
            expected_obs=expected,
            coverage_ratio=0.0,
            gap_count=expected,
            gap_ratio=1.0,
            first_obs=None,
            last_obs=None,
            sufficient_history=False,
            survivorship_ok=False,
            reasons=("unfetchable: empty series returned",),
        )

    series = series.dropna().sort_index()
    n = len(series)
    first, last = pd.Timestamp(series.index[0]), pd.Timestamp(series.index[-1])

    # Reference calendar, inferred from the DATA: continuously-traded markets
    # (crypto "-USD" pairs — ~71/340 of GLOBAL_ASSETS) print on weekends, so a
    # Mon-Fri bdate_range under-counts their expected observations and clips
    # gap_count to zero until ~28% of the history is missing — silently
    # defeating the 10% gap check for exactly those assets. Any material
    # weekend presence (≥5% of observations; true 7-day markets sit near 28%,
    # exchange-listed assets at 0%) switches the reference to calendar days.
    weekend_frac = float((series.index.day_of_week >= 5).mean()) if n else 0.0
    continuous = weekend_frac >= 0.05
    _cal = pd.date_range if continuous else pd.bdate_range
    expected = max(1, len(_cal(start_ts, end_ts)))
    coverage = n / expected

    # Internal gaps: missing trading days strictly within the observed span.
    span_days = max(1, len(_cal(first, last)))
    gap_count = max(0, span_days - n)
    gap_ratio = gap_count / span_days

    starts_on_time = first <= start_ts + tol
    sufficient_history = starts_on_time and coverage >= min_window_coverage
    survivorship_ok = last >= end_ts - tol
    gap_free = gap_ratio <= max_gap_ratio

    reasons: list[str] = []
    if not sufficient_history:
        reasons.append(
            f"insufficient history: coverage {coverage:.2f} (< {min_window_coverage}), "
            f"first obs {first.date()} vs window start {start_ts.date()}"
        )
    if not survivorship_ok:
        reasons.append(f"survivorship: last obs {last.date()} well before window end {end_ts.date()} (likely delisted)")
    if not gap_free:
        reasons.append(f"gappy: {gap_count} missing trading days ({gap_ratio:.1%} > {max_gap_ratio:.0%})")

    ok = sufficient_history and survivorship_ok and gap_free
    return InstrumentVerdict(
        ticker=ticker,
        ok=ok,
        fetchable=True,
        n_obs=n,
        expected_obs=expected,
        coverage_ratio=round(coverage, 4),
        gap_count=gap_count,
        gap_ratio=round(gap_ratio, 4),
        first_obs=str(first.date()),
        last_obs=str(last.date()),
        sufficient_history=sufficient_history,
        survivorship_ok=survivorship_ok,
        reasons=tuple(reasons),
    )


def verify_universe(
    tickers: list[str],
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
    *,
    edge_tolerance_days: int = DEFAULT_EDGE_TOLERANCE_DAYS,
    max_gap_ratio: float = DEFAULT_MAX_GAP_RATIO,
    min_window_coverage: float = DEFAULT_MIN_WINDOW_COVERAGE,
    _downloader: Downloader | None = None,
) -> UniverseReport:
    """Verify a set of tickers; aggregate into admitted / rejected.

    Only tickers whose :func:`verify_instrument` verdict is ``ok`` are admitted;
    the rest are rejected with their reasons. This is the precondition gate for
    the #759 universe expansion — admit clean data only, never lower the bar.
    """
    verdicts: dict[str, InstrumentVerdict] = {}
    for t in tickers:
        verdicts[t] = verify_instrument(
            t,
            start,
            end,
            edge_tolerance_days=edge_tolerance_days,
            max_gap_ratio=max_gap_ratio,
            min_window_coverage=min_window_coverage,
            _downloader=_downloader,
        )
    admitted = tuple(t for t, v in verdicts.items() if v.ok)
    rejected = {t: v.reasons for t, v in verdicts.items() if not v.ok}
    return UniverseReport(admitted=admitted, rejected=rejected, verdicts=verdicts)
