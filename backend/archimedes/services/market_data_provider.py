"""Market-data vendor seam (#775 / #1218), routed per seam (#1798).

#1218 priced yfinance as an unlicensed commercial dependency that scales with
strategies x symbols x re-run cadence, and named the seam it should be
substituted at. This module IS that seam for backend's live/request-path call
sites (analytics-engine's own choke point — ``fetch_ohlcv`` — gets the
equivalent treatment in ``archimedes_analytics_engine.market_data``; the two
are separate modules because analytics-engine is a standalone, DB-less
package. Both default to ``"yfinance"``, so a deploy is a no-op until a flag
flips; since #1798 they no longer read the same var in every case — this
module reads ``MARKET_DATA_DAILY_PROVIDER`` for its daily seam, while
analytics-engine's standalone CLI seam still reads ``MARKET_DATA_PROVIDER``
only and registers no Tiingo adapter, so a daily flip does not reach it).

**Two seams, two vendors, one vendor per run (#1798).** ``get_provider`` takes
a required ``seam=``. ``"daily"`` resolves ``MARKET_DATA_DAILY_PROVIDER``
(falling back to ``MARKET_DATA_PROVIDER``, then yfinance) and serves daily bars
only; ``"intraday"`` resolves ``MARKET_DATA_PROVIDER`` and serves the whole
interface, because a live run reads a quote and a daily context bar together
and both must come from one vendor. The per-seam table lives in
``docs/adr/market-data-sourcing.md``; the routing itself is ``_SEAM_METHODS`` /
``_VENDOR_SEAMS`` / ``SeamRoutedProvider`` below.

Six call sites route through here (grep-verified), each naming its seam:
  - ``strategy_signal_evaluator._fetch_price_history(ies)`` — daily close
    series for backtests/Explore's universe sweep (the #1218 volume driver).
    Seam: ``daily``.
  - ``chain.oracle_updater`` — the live oracle-push equity fetch, the VIX /
    S&P-MA regime reads, and the #775 secondary-source cross-check's
    independent reading. Seam: ``intraday`` for all of them, including the
    ^GSPC daily moving averages: ``fetch_market_snapshot`` reads ^VIX and
    ^GSPC in ONE run, and Tiingo has no index coverage anyway.
  - ``services.asset_market_service._fetch_yfinance_series`` — the Explore
    per-asset history-modal endpoint. Seam: ``intraday`` (arbitrary interval).
  - ``services.paper_marks.mark_all`` — the paper-trading mark loop. Seam:
    ``intraday``.
  - ``services.fusion_market_data._fetch_one`` — the GENERATION path's
    fusion/debate real-data panel (#1218 generation-path seam fix). Seam:
    ``daily``.
  - ``services.portfolio_backtester._fetch_price_panel`` — the GENERATION
    path's portfolio-weights backtester (same fix). Seam: ``daily``.

**#775 resolution, in one line:** the cross-check
(``oracle_updater._cross_check_secondary``) reads its independent secondary
through ``get_provider(seam="intraday")`` and treats
``provider_name("intraday")`` (not a hardcoded ``"yfinance"``) as "same source,
skip". Swap the intraday vendor and the cross-check's secondary source swaps
with it automatically — no separate change needed at the guardrail.

**Caching, scoped intentionally.** ``get_daily_close_batch`` — the DAILY-bar
close-only shape that matches ``asset_daily_bars`` — and ``get_daily_ohlcv``
— the DAILY full-bar (Open/High/Low/Close/Volume) shape the generation path's
backtrader feeds and Close+Volume panel need — are both cache-backed
(``CachingMarketDataProvider``), and both read/write the SAME
``asset_daily_bars`` table (a row primed by one method's writer that lacks a
full bar is treated as a miss by the other, so the two never hand back a
partially-populated frame as if it were complete). ``get_intraday_quote(s)``
(live oracle pushes, VIX, the cross-check's secondary) and ``get_series`` (the
Explore history modal, any interval) pass straight through, uncached, on
purpose: a stale daily close must never masquerade as a live push/guardrail
reading. Background refresh is deliberately NOT built here (#1218 anti-goal:
seam, not migration) — the cache primes itself on the first miss.

**The cache is PER-VENDOR.** Both readers filter ``asset_daily_bars`` on the
``source`` column as well as the symbol, so rows written by one provider are
invisible to another. That filter is also what lets the two seams share one
table: the daily seam's Tiingo rows and the intraday seam's yfinance rows sit
side by side and neither reads the other's. Without it, flipping a provider
on a system with a warm cache serves the OLD vendor's bars for cached symbols
and the NEW vendor's for uncached ones — inside a single backtest panel, with
no error and no log line. See ``_read_cached_ohlcv``'s docstring and
``docs/adr/market-data-sourcing.md``. Cost: a provider flip starts cold.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

logger = logging.getLogger(__name__)

# yfinance period vocabulary -> approximate calendar days, used to derive a
# cache coverage window. Approximate on purpose (trading days < calendar
# days); the +5-day tolerance in _read_cached_series absorbs the slack.
_PERIOD_DAYS: dict[str, int] = {
    "1d": 1,
    "5d": 5,
    "1mo": 31,
    "3mo": 93,
    "6mo": 186,
    "1y": 366,
    "2y": 731,
    "5y": 1827,
    "10y": 3653,
    "ytd": 366,
    "max": 3653 * 4,
}
_DEFAULT_PERIOD_DAYS = 731  # ~2y — matches the evaluator's own default period

# How much back-history the cache must already hold, relative to the
# requested window's start, to count as "covers this request". Trading-day
# gaps (weekends/holidays) plus vendor listing-date differences mean an exact
# match is too strict.
_COVERAGE_TOLERANCE_DAYS = 5

# How long a cached row is trusted before a request re-fetches it live.
# Configurable because "how often is it OK to be a day stale" is a product
# call, not a code constant — default errs toward freshness (daily bars
# genuinely change once a day; the point of the cache is to not re-fetch on
# every request within that window, not to go stale for a week).
DEFAULT_CACHE_TTL_HOURS = 12


def _int_env(name: str, default: int) -> int:
    """Parse an integer env var, failing SAFE to ``default`` (mirrors
    ``chain.oracle_updater._int_env`` — same fail-safe convention)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("invalid %s=%r (not an integer) — falling back to %d", name, raw, default)
        return default


def _bar_ts_to_utc(ts) -> datetime:
    """Normalize a pandas bar-index entry to a tz-aware UTC ``datetime``.

    Extracted from ``YFinanceProvider.get_intraday_quote`` so the single- and
    batch-quote siblings normalize identically — the batch method's widened
    return is only interchangeable with the single one if "UTC" means the
    same thing on both. A naive index (yfinance returns one for some daily
    frames) is LOCALIZED to UTC rather than converted, matching what the
    single-quote path has always done.
    """
    ts = ts.tz_convert("UTC") if ts.tzinfo is not None else ts.tz_localize("UTC")
    return ts.to_pydatetime()


# ─── Provider interface ─────────────────────────────────────────────────


class MarketDataProvider(ABC):
    """Vendor abstraction for market data. Default implementation (below) is
    yfinance, unchanged in behavior from what each call site did before this
    seam existed. A new vendor implements this interface and registers in
    ``_VENDOR_PROVIDERS``, declares the seams it can serve in
    ``_VENDOR_SEAMS``, and is selected per seam (#1798) by
    ``MARKET_DATA_DAILY_PROVIDER`` / ``MARKET_DATA_PROVIDER``."""

    @abstractmethod
    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        """Batch daily close-price series. ``tickers`` maps caller-chosen keys
        (our synth symbols) to vendor tickers; the result is keyed the same
        way. Missing/failed tickers are simply absent from the result."""

    @abstractmethod
    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        """Latest intraday (price, bar_timestamp) for one vendor ticker, or
        None on any failure. Never cached — see module docstring."""

    @abstractmethod
    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        """Latest intraday ``(price, bar_timestamp)`` for many tickers in one
        vendor call. Keyed like ``get_daily_close_batch``. Never cached.

        The bar timestamp is part of the contract, not a nicety — same shape
        ``get_intraday_quote`` already returns for a single ticker, and
        ``bar_timestamp`` is always tz-aware UTC. Two consumers need it and
        neither can be honest without it:

          - ``oracle_updater._validate_for_push`` gates an on-chain push on
            ``now - price.timestamp``. When this method returned price only,
            ``_fetch_yfinance`` had nothing to stamp but the POLL time, so on
            the yfinance leg that gate compared now against now and could
            never reject a stale bar (the Pyth cascade always carried a real
            observation time; this leg did not).
          - the paper-marks loop (``services.paper_marks``) stores the
            UPSTREAM observation time on every mark and writes NO row when
            the newest bar is stale. Both rules are unbuildable on a bare
            float.

        A symbol whose bar time is genuinely old (an equity outside market
        hours) is still returned with its true, old timestamp — deciding what
        counts as too stale belongs to the caller's policy, not to the vendor
        seam.
        """

    @abstractmethod
    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        """Generic close-price series at an arbitrary (period, interval) —
        the Explore history-modal shape. Never cached."""

    @abstractmethod
    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """Full daily OHLCV history for one vendor ticker over ``[start, end)``
        (ISO date strings) — the shape the GENERATION path needs: backtrader
        feeds (``fusion_market_data.feed_factory``) and the portfolio
        simulator's Close+Volume panel (``portfolio_backtester._fetch_price_panel``)
        both consume a full OHLCV frame, not a close-only series, so this is a
        separate method from ``get_daily_close_batch`` rather than a batch
        variant of it. Columns ``Open``/``High``/``Low``/``Close``/``Volume``,
        a ``DatetimeIndex``, normalized (dropna, monotonic — see
        ``archimedes_analytics_engine.data.normalize_ohlcv``). Raises on a
        genuinely unfetchable symbol/range rather than returning an empty
        frame — callers already handle that (fail-closed to synthetic,
        per-symbol skip) and rely on the exception, not a sentinel value."""


class YFinanceProvider(MarketDataProvider):
    """Default provider — today's yfinance behavior, unchanged. Each method
    body is a straight move of the logic that used to live inline at its
    call site (see the docstring in each caller for the #1218 provenance)."""

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        if not tickers:
            return {}
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance not installed — returning empty histories")
            return {}

        yf_tickers = list(tickers.values())
        try:
            data = yf.download(
                tickers=" ".join(yf_tickers),
                period=period,
                interval="1d",
                progress=False,
                auto_adjust=True,
                group_by="ticker",
                threads=True,
            )
        except Exception as exc:
            logger.warning("Batched yfinance fetch failed: %s", exc)
            return {}

        if data is None or len(data) == 0:
            return {}

        result: dict[str, pd.Series] = {}

        if len(yf_tickers) == 1:
            sole_key = next(iter(tickers))
            try:
                close = data["Close"]
                if isinstance(close, pd.DataFrame):
                    close = close.iloc[:, 0]
                close = close.dropna()
                close.name = sole_key
                if not close.empty:
                    result[sole_key] = close
            except Exception as exc:
                logger.warning("Failed to extract Close for %s: %s", sole_key, exc)
            return result

        for key, yf_ticker in tickers.items():
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    if yf_ticker not in data.columns.get_level_values(0):
                        continue
                    close = data[yf_ticker]["Close"]
                else:
                    close = data["Close"]
                if isinstance(close, pd.DataFrame):
                    close = close.iloc[:, 0]
                close = close.dropna()
                if close.empty:
                    continue
                close.name = key
                result[key] = close
            except Exception as exc:
                logger.warning("Failed to extract %s (%s): %s", key, yf_ticker, exc)
        return result

    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        try:
            import yfinance as yf

            data = yf.download(ticker, period="1d", interval="1m", progress=False)
            if not data.empty:
                close = data["Close"]
                if hasattr(close, "columns"):
                    close = close.iloc[:, 0]
                return float(close.iloc[-1]), _bar_ts_to_utc(data.index[-1])
        except Exception as exc:
            logger.warning("Failed to fetch %s: %s", ticker, exc)
        return None

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        """One ``yf.download`` for the whole list; ``(price, bar_ts)`` per key.

        The bar timestamp was always in hand here and thrown away: the frame
        this method already holds is indexed by bar time. It is read
        PER SYMBOL (``col.dropna().index[-1]``), not once off the frame's own
        last index, because a mixed universe's legs do not share a last bar
        — an equity outside the session and a 24/7 crypto pair sit in the same
        frame with the equity column NaN across the tail. Taking the frame's
        last index for both would stamp the equity leg with the crypto leg's
        time, i.e. exactly the "stale price wearing a fresh timestamp" defect
        the widened signature exists to make impossible.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance not installed — returning empty prices")
            return {}

        tickers_str = " ".join(tickers.values())
        data = yf.download(tickers_str, period="1d", interval="1m", progress=False)

        results: dict[str, tuple[float, datetime]] = {}
        for key, yf_ticker in tickers.items():
            try:
                if data.empty:
                    continue
                if len(tickers) == 1:
                    close = data["Close"]
                    if hasattr(close, "columns"):
                        close = close.iloc[:, 0]
                else:
                    close = data["Close"]
                    if yf_ticker not in close.columns:
                        continue
                    close = close[yf_ticker]
                close = close.dropna()
                if close.empty:
                    continue
                results[key] = (float(close.iloc[-1]), _bar_ts_to_utc(close.index[-1]))
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", key, exc)
        return results

    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        try:
            import yfinance as yf

            data = yf.download(
                ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
                threads=False,
            )
        except Exception as exc:
            logger.warning("Failed to fetch series for %s (%s/%s): %s", ticker, period, interval, exc)
            return pd.Series(dtype=float)

        if data is None or len(data) == 0:
            return pd.Series(dtype=float)
        try:
            close = data["Close"]
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            return close.dropna()
        except Exception as exc:
            logger.warning("Failed to extract Close for %s: %s", ticker, exc)
            return pd.Series(dtype=float)

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """Delegates to analytics-engine's ``data.fetch_ohlcv`` — the exact
        fetch/retry/normalize contract ``fusion_market_data`` and
        ``portfolio_backtester`` already depended on directly before this
        seam existed (#1282). Reusing it here (rather than re-implementing
        the fetch against yfinance a second time) is what guarantees the
        cold-cache output is byte-identical to the pre-seam direct-fetch
        path — the two are now the same function call."""
        from archimedes.services.fusion_market_data import _ensure_analytics_import

        _ensure_analytics_import()
        from archimedes_analytics_engine.data import fetch_ohlcv

        return fetch_ohlcv(ticker, start, end)


# ─── Tiingo provider (#1218 Part 1 — yfinance replacement) ─────────────


class TiingoProviderError(RuntimeError):
    """Base class for TiingoProvider failures. Deliberately loud (never
    caught-and-silently-substituted with yfinance) — see CLAUDE.md 'fail-soft
    is wrong for anything a claim depends on'."""


class TiingoAPIKeyMissingError(TiingoProviderError):
    """``TIINGO_API_TOKEN`` is unset/blank. Raised at ``TiingoProvider``
    construction — ``get_provider()`` builds a fresh instance on every call
    (no long-lived singleton in this seam), so this fires on the very next
    call site whose seam resolves to tiingo (``MARKET_DATA_DAILY_PROVIDER``,
    or ``MARKET_DATA_PROVIDER`` on the daily seam when the first is unset):
    the closest thing this seam has to a "startup" check, since there is no
    separate eager app-boot validation of the market-data vendor today — AND
    again at every HTTP call (the key is never cached on the instance;
    re-read from the environment every time, so a rotated value takes effect
    as soon as the process's own environment carries the new one — no
    restart needed on THIS module's account). The message never contains the
    key itself — there is none to include."""


class TiingoUnsupportedSymbolError(TiingoProviderError, ValueError):
    """A ticker's shape identifies it as a commodity-future / index symbol
    (e.g. ``GC=F``, ``CL=F``, ``^N225``, ``^GSPC``) that none of Tiingo's
    three REST endpoint families (equities/ETFs, crypto, FX) can serve.
    Raised loud, naming the symbol and the reason — never silently swapped
    for a yfinance fallback, which would defeat the #1218 migration this
    provider exists to make. ``ValueError`` in the MRO keeps it catchable by
    any existing ``except ValueError`` call site (matches
    ``YFinanceProvider``'s / ``fetch_ohlcv``'s existing
    raise-on-unfetchable-symbol contract)."""


class TiingoRateLimitError(TiingoProviderError):
    """Tiingo answered HTTP 429 — the account's request quota is exhausted.

    A SEPARATE type from the generic ``TiingoProviderError`` because the two
    demand opposite handling and, before this class existed, got the same
    one. ``get_daily_close_batch`` catches ``TiingoProviderError`` and SKIPS
    the offending symbol so a single bad ticker cannot fail a 280-symbol
    universe sweep — correct for "this symbol has no data", and exactly
    wrong for a quota exhaustion, which is not about the symbol at all.
    Laundered through that path, a rate limit hit mid-sweep would (a) drop
    every remaining symbol from the batch while the caller saw a
    successful-looking partial result, and (b) keep firing requests at a
    vendor that has already said stop. Quota state is per-account and
    affects every ticker, so this propagates out of the batch like
    ``TiingoAPIKeyMissingError`` does, and says so in the message.

    ``retry_after_s`` carries the vendor's own ``Retry-After`` header when
    it sent one (``None`` when it did not) — surfaced rather than guessed,
    because our pacing default is a politeness floor we chose, not a
    published quota we can verify from inside this process.
    """

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class TiingoEmptyResponseError(TiingoProviderError, ValueError):
    """Tiingo returned a syntactically valid but empty result (no rows) for a
    ticker/date-range that passed symbol-routing. Raised loud rather than
    handed back as an empty-but-"successful" DataFrame — an empty frame
    masquerading as a hit would look identical to "no data exists" at every
    downstream consumer (backtrader feed, portfolio panel), silently
    truncating whatever depends on it."""


_TIINGO_BASE_URL = "https://api.tiingo.com"
_TIINGO_TIMEOUT_S = 15.0

# Case-insensitive: crypto vendor tickers in this codebase are always
# "<BASE>-USD" (see backend/archimedes/data/synthetic_universe.json's 71
# `crypto` entries, e.g. "BTC-USD", "AAVE-USD") — the same shape yfinance
# uses. Tiingo's crypto endpoint wants the pair concatenated and lowercased
# ("btcusd").
_CRYPTO_TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}-USD$")


def _classify_tiingo_ticker(ticker: str) -> str:
    """Route a vendor-ticker string to one of Tiingo's three REST endpoint
    families by ITS SHAPE — the same shape yfinance's own ticker convention
    already encodes (``=X`` FX suffix, ``-USD`` crypto suffix, bare symbol
    for equities/ETFs), since every call site into this seam still hands
    ``TiingoProvider`` a yfinance-formatted ticker string (the ``tickers``
    dict's VALUES / the ``ticker`` positional arg — see the ABC method
    docstrings), not a synth symbol or an ``asset_class`` label.

    A ``archimedes.universe.SYNTHETIC_UNIVERSE`` lookup was considered
    instead of a shape heuristic, and rejected: several real call sites hand
    this seam tickers that are NOT in the on-chain synthetic universe at all
    — ``chain.oracle_updater``'s ``^GSPC``/``^VIX`` regime-signal tickers,
    and arbitrary Explore/backtest tickers passed straight through by
    ``asset_market_service`` / ``fusion_market_data``. A universe lookup
    would return nothing useful for exactly the tickers most likely to need
    the unsupported-symbol guard (index tickers aren't synths at all).
    The shape heuristic covers every one of the 281 SSOT entries'
    ``yfinance_ticker`` values correctly (verified against
    ``synthetic_universe.json`` while building this provider) plus the
    out-of-universe ``^`` / ``=F`` cases the guard exists for.

    Returns ``"equity"``, ``"crypto"``, or ``"fx"``. Raises
    ``TiingoUnsupportedSymbolError`` for an index (``^``-prefixed) or
    commodity-future/continuous-contract (``=F``-suffixed) ticker — e.g. the
    5 ``metal_spot`` SSOT entries (``GC=F``/``SI=F``/``HG=F``/``PA=F``/
    ``PL=F``) and index regime tickers like ``^GSPC``/``^VIX``/``^N225``/
    ``CL=F``. These have no Tiingo REST equivalent on the endpoint families
    this provider targets.
    """
    t = ticker.strip()
    if t.startswith("^"):
        raise TiingoUnsupportedSymbolError(
            f"{ticker!r} is an index ticker (^-prefixed, e.g. ^GSPC/^VIX/^N225) — "
            "Tiingo's REST API has no index-level endpoint; TiingoProvider cannot "
            "serve this symbol. Never silently falls back to yfinance."
        )
    if t.upper().endswith("=F"):
        raise TiingoUnsupportedSymbolError(
            f"{ticker!r} is a futures/commodity-continuous-contract ticker (=F suffix, "
            "e.g. GC=F/SI=F/CL=F) — Tiingo has no futures endpoint on the "
            "equities/crypto/FX REST families TiingoProvider targets. Never silently "
            "falls back to yfinance."
        )
    if t.upper().endswith("=X"):
        return "fx"
    if _CRYPTO_TICKER_RE.match(t.upper()):
        return "crypto"
    return "equity"


#: Canonical credential env var, first; the name the provider shipped with,
#: second. Tiingo's own docs and its ``Authorization: Token <...>`` header
#: call the credential a *token*, so ``TIINGO_API_TOKEN`` is what the ADR,
#: ``.env.example`` and the prod SSM parameter name all use. ``TIINGO_API_KEY``
#: stays readable because it is the name already merged on ``main`` and
#: already in developers' local ``.env`` files; dropping it outright would
#: turn a working local setup into a ``TiingoAPIKeyMissingError`` with no
#: hint as to why. Order matters: the canonical name WINS when both are set,
#: so the migration direction is unambiguous.
_TIINGO_TOKEN_ENV_VARS: tuple[str, ...] = ("TIINGO_API_TOKEN", "TIINGO_API_KEY")


def _tiingo_api_key() -> str:
    """Read the Tiingo API token fresh from the environment — never cached on
    a ``TiingoProvider`` instance, so a rotated value takes effect on the next
    call as soon as the process's environment carries it. Raises loud
    (``TiingoAPIKeyMissingError``) rather than proceeding with an
    unauthenticated request Tiingo would reject anyway; the message never
    includes the token (there is none to include).

    Reads ``TIINGO_API_TOKEN`` first, then the legacy ``TIINGO_API_KEY`` — see
    ``_TIINGO_TOKEN_ENV_VARS``.
    """
    for name in _TIINGO_TOKEN_ENV_VARS:
        token = os.getenv(name, "").strip()
        if token:
            if name != _TIINGO_TOKEN_ENV_VARS[0]:
                logger.warning(
                    "Tiingo credential read from the legacy %s env var — rename it to %s "
                    "(the canonical name; see .env.example and docs/adr/market-data-sourcing.md)",
                    name,
                    _TIINGO_TOKEN_ENV_VARS[0],
                )
            return token
    raise TiingoAPIKeyMissingError(
        "TIINGO_API_TOKEN is not set (legacy alias TIINGO_API_KEY also empty). Required "
        "whenever a seam resolves to tiingo — MARKET_DATA_DAILY_PROVIDER, or "
        "MARKET_DATA_PROVIDER on the daily seam when that is unset (see .env.example). "
        "Wired into infra/ecs.tf's backend `secrets` and pinned on the deploy clone path "
        "by .github/scripts/ecs_rewrite_task_def.py (#1798), so in prod this means the "
        "value is blank or the process is not the deployed container — see "
        "docs/runbooks/market-data-provider-proof.md."
    )


# ─── Free-tier politeness: request pacing ───────────────────────────────
#
# Tiingo's free tier is metered per hour and per day. This module does NOT
# hardcode those ceilings: they are account- and plan-dependent, they change,
# and a number copied into source here would read as verified when it is not.
# What it does instead is (a) never fire two requests closer together than a
# floor we choose, and (b) surface the vendor's own 429 verbatim as the
# authority on when we have actually crossed a line (TiingoRateLimitError).
#
# The floor is a politeness default, not a quota model. It is applied at the
# single HTTP boundary (``TiingoProvider._request``), so every family
# (equity/crypto/FX) and both public methods pace through one place.
_TIINGO_DEFAULT_MIN_REQUEST_INTERVAL_S = 1.1


def _tiingo_min_request_interval_s() -> float:
    """Seconds to hold between consecutive Tiingo HTTP requests.

    ``TIINGO_MIN_REQUEST_INTERVAL_S`` overrides; ``0`` disables pacing.
    Defaults to 0 under ``TESTING`` so the hermetic suite (which mocks the
    transport and never touches Tiingo) does not sleep — the pacer itself is
    still tested directly, with an injected clock, so switching it off here
    hides nothing. Same ``TESTING`` kill-switch convention as
    ``fusion_market_data.real_data_enabled``.
    """
    default = 0.0 if os.getenv("TESTING") else _TIINGO_DEFAULT_MIN_REQUEST_INTERVAL_S
    raw = os.getenv("TIINGO_MIN_REQUEST_INTERVAL_S")
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning(
            "invalid TIINGO_MIN_REQUEST_INTERVAL_S=%r (not a number) — falling back to %s",
            raw,
            default,
        )
        return default
    if value < 0:
        logger.warning("negative TIINGO_MIN_REQUEST_INTERVAL_S=%r — falling back to %s", raw, default)
        return default
    return value


class _RequestPacer:
    """Thread-safe minimum-interval throttle over a shared clock.

    Deliberately process-wide (one module-level instance below) rather than
    per-``TiingoProvider``: ``get_provider()`` constructs a FRESH provider on
    every call, so per-instance state would reset on each one and pace
    nothing at all. Concurrent callers serialize on the lock across the
    sleep, which is the point — two threads that each waited independently
    would still fire simultaneously.

    ``clock``/``sleep`` are injectable so the pacing behaviour is testable
    without real time passing (mock at the boundary, not the internals).
    """

    def __init__(self, clock=time.monotonic, sleep=time.sleep) -> None:
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request_at: float | None = None

    def wait(self, min_interval_s: float) -> float:
        """Block until at least ``min_interval_s`` has elapsed since the
        previous call. Returns the number of seconds actually slept (0.0 when
        no wait was needed) so callers/tests can observe the pacing."""
        with self._lock:
            now = self._clock()
            slept = 0.0
            if min_interval_s > 0 and self._last_request_at is not None:
                remaining = min_interval_s - (now - self._last_request_at)
                if remaining > 0:
                    self._sleep(remaining)
                    slept = remaining
                    now = self._clock()
            self._last_request_at = now
            return slept


#: Process-wide pacer for the Tiingo HTTP boundary — see ``_RequestPacer``.
_tiingo_pacer = _RequestPacer()


def _parse_retry_after(raw: str | None) -> float | None:
    """Parse a ``Retry-After`` header into seconds, or ``None``.

    Only the delta-seconds form is honoured. RFC 9110 also permits an
    HTTP-date, but returning ``None`` for a shape we did not parse is the
    honest answer — ``TiingoRateLimitError.retry_after_s`` is documented as
    "the vendor's own value when it sent one", and inventing a number from a
    header we failed to read would make it a guess wearing the vendor's
    name. A non-numeric or negative value is likewise ``None``, never 0
    (which would read as "retry immediately").
    """
    if raw is None or not raw.strip():
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _tiingo_rows_to_ohlcv(
    rows: list[dict],
    ticker: str,
    *,
    open_key: str,
    high_key: str,
    low_key: str,
    close_key: str,
    volume_key: str | None,
) -> pd.DataFrame:
    """Shared row->frame mapper for all three Tiingo endpoint families —
    shape contract match with ``YFinanceProvider`` (same columns, a
    tz-naive ``DatetimeIndex``, float64 throughout). ``volume_key=None``
    (Tiingo's FX endpoint carries no volume field — FX is OTC, there is no
    consolidated tape) fills ``Volume`` with 0.0, matching yfinance's own
    FX-ticker behavior (``EURUSD=X`` etc. return an all-zero ``Volume``
    column from yfinance too — forex has no exchange-reported volume for
    either vendor, so this is not a Tiingo-specific gap)."""
    if not rows:
        raise TiingoEmptyResponseError(f"Tiingo returned zero rows for {ticker!r} in the requested range")
    index = pd.DatetimeIndex(pd.to_datetime([r["date"] for r in rows], utc=True)).tz_localize(None)
    volume = [float(r[volume_key]) for r in rows] if volume_key else [0.0] * len(rows)
    frame = pd.DataFrame(
        {
            "Open": [float(r[open_key]) for r in rows],
            "High": [float(r[high_key]) for r in rows],
            "Low": [float(r[low_key]) for r in rows],
            "Close": [float(r[close_key]) for r in rows],
            "Volume": volume,
        },
        index=index,
    )
    return frame.sort_index()


class TiingoProvider(MarketDataProvider):
    """Yfinance-replacement vendor (#1218 Part 1) backed by Tiingo's REST
    API. Scope of THIS PR: ``get_daily_close_batch`` and ``get_daily_ohlcv``
    only — the two methods ``CachingMarketDataProvider`` cache-backs, and the
    #1218 cost driver (the universe sweep + generation-path OHLCV fetches).
    ``get_intraday_quote`` / ``get_intraday_quotes_batch`` / ``get_series``
    are intentionally NOT implemented — see their docstrings below for why.
    Since #1798 that is a *declared* limit, not a landmine: ``_VENDOR_SEAMS``
    lists Tiingo on the ``daily`` seam only, so ``get_provider`` never hands a
    ``TiingoProvider`` to an intraday call site and flipping the daily flag
    cannot take the oracle push or the Explore history modal down. These three
    methods still raise (rather than being omitted) so that a direct
    construction, or a future ``_VENDOR_SEAMS`` edit that outruns the adapter,
    fails loudly before any network call.

    Three REST endpoint families, routed per-ticker by
    ``_classify_tiingo_ticker`` (ticker-SHAPE heuristic — see that function's
    docstring for why a universe lookup was rejected):
      - equities/ETFs → ``GET /tiingo/daily/{ticker}/prices``
      - crypto        → ``GET /tiingo/crypto/prices?tickers=...``
      - FX            → ``GET /tiingo/fx/{ticker}/prices``

    **Adjustment semantics — the load-bearing part (#1218 point 3).** The
    yfinance path this replaces calls ``yf.download(..., auto_adjust=True,
    ...)`` at every equity/ETF call site that feeds these two methods:
    ``YFinanceProvider.get_daily_close_batch`` (this file, line ~173),
    ``YFinanceProvider.get_series`` (this file, line ~275), and — via
    delegation from ``YFinanceProvider.get_daily_ohlcv`` — ``archimedes_analytics_engine
    .market_data.YFinanceProvider.fetch_ohlcv`` (``analytics-engine/src/
    archimedes_analytics_engine/market_data.py:63``). ``auto_adjust=True``
    means yfinance's ``Close``/``Open``/``High``/``Low`` columns are ALREADY
    split/dividend back-adjusted — yfinance folds what would otherwise be a
    separate ``Adj Close`` column into ``Close`` itself under this flag, so
    there is no unadjusted column left in the frame at all. Every graded
    backtest, PBO run, and the #775 cross-check compares against that
    adjusted series today.

    TiingoProvider therefore maps its EQUITY rows from Tiingo's
    ``adjOpen``/``adjHigh``/``adjLow``/``adjClose``/``adjVolume`` fields —
    NEVER the sibling raw ``open``/``high``/``low``/``close``/``volume``
    fields Tiingo also returns in the same payload. Using the raw fields
    would silently reintroduce split/dividend discontinuities into every
    backtest run against this provider (a KO-style 2-for-1 split would show
    up as a ~50% overnight price cliff) with no error and no log — exactly
    the corruption #1218 point 3 warns about. Mutation-checked: flipping
    ``adjClose`` -> ``close`` (etc.) in this mapping makes
    ``TestAdjustmentSemantics`` in ``test_tiingo_provider.py`` fail (before/
    after run recorded in the PR body).

    Crypto and FX have no adjusted/raw distinction to make: Tiingo's crypto
    and FX payloads carry only ONE set of OHLC fields each (no corporate
    actions apply to either asset class), so those two families map their
    single raw field set directly — there is nothing to get wrong there.
    """

    def __init__(self, client: httpx.Client | None = None, pacer: _RequestPacer | None = None) -> None:
        # Presence-check (not value-cache) at construction — see
        # TiingoAPIKeyMissingError's docstring for why this is the closest
        # thing this seam has to a "startup" gate. The key itself is
        # re-read fresh (never reused from here) by every _request() call.
        _tiingo_api_key()
        self._client = client
        # Defaults to the PROCESS-WIDE pacer, not a per-instance one:
        # get_provider() builds a fresh TiingoProvider on every call, so
        # per-instance pacing state would reset each time and pace nothing.
        # Injectable for tests (fake clock, no real sleeping).
        self._pacer = pacer if pacer is not None else _tiingo_pacer

    # ─── HTTP boundary ───────────────────────────────────────────────

    def _request(self, path: str, params: dict[str, str]) -> object:
        key = _tiingo_api_key()  # re-read fresh — never cached on self
        headers = {"Authorization": f"Token {key}"}  # header, never a query param: never lands in a logged URL
        # Politeness floor, applied at the ONE HTTP boundary so every endpoint
        # family and both public methods pace through the same place.
        self._pacer.wait(_tiingo_min_request_interval_s())
        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.Client(base_url=_TIINGO_BASE_URL, timeout=_TIINGO_TIMEOUT_S)
        try:
            response = client.get(path, params=params, headers=headers)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                retry_after = _parse_retry_after(exc.response.headers.get("Retry-After"))
                suffix = f" Vendor asked us to retry after {retry_after:g}s." if retry_after is not None else ""
                raise TiingoRateLimitError(
                    f"Tiingo API rate limit hit (HTTP 429) for {path} — the account's request "
                    f"quota is exhausted, which is an account-wide condition, not a per-symbol "
                    f"data gap.{suffix}",
                    retry_after_s=retry_after,
                ) from exc
            raise TiingoProviderError(f"Tiingo API returned HTTP {status} for {path}") from exc
        except httpx.RequestError as exc:
            raise TiingoProviderError(f"Tiingo API request failed for {path}: {type(exc).__name__}") from exc
        finally:
            if owns_client:
                client.close()

    # ─── Per-family fetch ────────────────────────────────────────────

    def _fetch_equity_rows(self, ticker: str, start: str, end: str) -> list[dict]:
        params: dict[str, str] = {"format": "json", "startDate": start}
        if end:
            params["endDate"] = end
        data = self._request(f"/tiingo/daily/{ticker}/prices", params)
        if not isinstance(data, list):
            raise TiingoProviderError(f"Unexpected Tiingo equity response shape for {ticker!r}")
        return data

    def _fetch_crypto_rows(self, ticker: str, start: str, end: str) -> list[dict]:
        tiingo_ticker = ticker.replace("-", "").lower()
        params: dict[str, str] = {"tickers": tiingo_ticker, "startDate": start, "resampleFreq": "1day"}
        if end:
            params["endDate"] = end
        data = self._request("/tiingo/crypto/prices", params)
        if not isinstance(data, list) or not data:
            return []
        return data[0].get("priceData") or []

    def _fetch_fx_rows(self, ticker: str, start: str, end: str) -> list[dict]:
        tiingo_ticker = ticker[:-2].lower() if ticker.upper().endswith("=X") else ticker.lower()
        params: dict[str, str] = {"startDate": start, "resampleFreq": "1day"}
        if end:
            params["endDate"] = end
        data = self._request(f"/tiingo/fx/{tiingo_ticker}/prices", params)
        if not isinstance(data, list):
            raise TiingoProviderError(f"Unexpected Tiingo FX response shape for {ticker!r}")
        return data

    def _fetch_ohlcv_for_ticker(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """Shared by both public methods below — one routing + adjustment-
        mapping implementation, not two, so the two public methods can never
        drift into inconsistent semantics for the same ticker."""
        asset_class = _classify_tiingo_ticker(ticker)
        if asset_class == "equity":
            rows = self._fetch_equity_rows(ticker, start, end)
            return _tiingo_rows_to_ohlcv(
                rows,
                ticker,
                open_key="adjOpen",
                high_key="adjHigh",
                low_key="adjLow",
                close_key="adjClose",
                volume_key="adjVolume",
            )
        if asset_class == "crypto":
            rows = self._fetch_crypto_rows(ticker, start, end)
            return _tiingo_rows_to_ohlcv(
                rows, ticker, open_key="open", high_key="high", low_key="low", close_key="close", volume_key="volume"
            )
        rows = self._fetch_fx_rows(ticker, start, end)  # asset_class == "fx"
        return _tiingo_rows_to_ohlcv(
            rows, ticker, open_key="open", high_key="high", low_key="low", close_key="close", volume_key=None
        )

    # ─── ABC surface — IN SCOPE for this PR ─────────────────────────

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        """Per the ABC contract, a failed/unsupported/empty ticker is simply
        ABSENT from the result (never raises the whole batch) — matching
        ``YFinanceProvider``'s existing behavior for this method and the
        cache-wrapper's own invariant
        (``test_vendor_miss_leaves_symbol_absent_not_erroring``). An
        unsupported symbol is still LOUD in the log (full symbol + reason,
        at ERROR level): "loud" here means "never silently substitutes
        yfinance data", not "raises out of a 280-symbol universe sweep over
        one bad ticker". ``get_daily_ohlcv`` below is the method whose
        contract is to raise on a single unfetchable ticker; this one's
        contract (inherited from the ABC, unchanged by this PR) is per-item
        skip.

        Two conditions DO propagate out of the batch immediately rather than
        being skipped per-ticker, because neither is a per-symbol data issue
        and both affect every remaining ticker equally:

        - a missing API token — a configuration problem; skipping it
          per-item would silently degrade ``MARKET_DATA_PROVIDER=tiingo``
          with no token into "every symbol empty" instead of a loud,
          startup-shaped failure;
        - an HTTP 429 rate limit (``TiingoRateLimitError``) — an account-wide
          quota exhaustion. Skipping it per-item would drop every remaining
          symbol from a universe sweep while handing the caller a
          successful-looking partial result, AND keep firing requests at a
          vendor that already said stop.
        """
        if not tickers:
            return {}
        start_date = _period_to_start_date(period, datetime.now(UTC))
        start = start_date.isoformat()
        end = datetime.now(UTC).date().isoformat()

        result: dict[str, pd.Series] = {}
        for key, ticker in tickers.items():
            try:
                frame = self._fetch_ohlcv_for_ticker(ticker, start, end)
            except (TiingoAPIKeyMissingError, TiingoRateLimitError):
                # Account-wide conditions, not per-symbol data gaps — loud, no
                # partial batch. MUST stay ABOVE the generic handler below:
                # both subclass TiingoProviderError, so ordering is what makes
                # this a propagate rather than a skip.
                raise
            except TiingoProviderError as exc:
                logger.error("TiingoProvider: skipping %s (%s) in batch — %s", key, ticker, exc)
                continue
            close = frame["Close"].dropna()
            if close.empty:
                continue
            close.name = key
            result[key] = close
        return result

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """Raises on a genuinely unfetchable/unsupported symbol or an empty
        result — matches the ABC's documented contract (and
        ``YFinanceProvider.get_daily_ohlcv``'s delegate,
        ``archimedes_analytics_engine.data.fetch_ohlcv``): callers
        (``fusion_market_data``, ``portfolio_backtester``) rely on the
        exception for their own fail-closed / per-symbol-skip handling, not
        a sentinel empty frame."""
        return self._fetch_ohlcv_for_ticker(ticker, start, end)

    # ─── ABC surface — OUT OF SCOPE for this PR ─────────────────────
    # See the class docstring's "Scope of THIS PR" note. Loud
    # NotImplementedError, never a silent wrong-data fallback: a stubbed
    # intraday quote or arbitrary-interval series would be indistinguishable
    # from a working one at every call site until it produced a visibly
    # wrong number in production (the live oracle push, the VIX/S&P regime
    # reads, or the Explore history modal) — see CLAUDE.md "fail-soft is
    # wrong for anything a claim depends on".

    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        raise NotImplementedError(
            "TiingoProvider.get_intraday_quote is out of scope for #1218 Part 1 (daily "
            "batch + OHLCV only). Tiingo is declared on the 'daily' seam only (#1798), so "
            "chain.oracle_updater's live oracle push and VIX/S&P regime reads run on the "
            "'intraday' seam's vendor (yfinance) whatever MARKET_DATA_DAILY_PROVIDER says; "
            "reaching this line means something bypassed get_provider(seam=...)."
        )

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, float]:
        raise NotImplementedError(
            "TiingoProvider.get_intraday_quotes_batch is out of scope for #1218 Part 1 "
            "(daily batch + OHLCV only). Tiingo is declared on the 'daily' seam only "
            "(#1798), so call sites needing a live intraday batch quote (the oracle push, "
            "the paper-marks loop) are routed to the 'intraday' seam's vendor instead; "
            "reaching this line means something bypassed get_provider(seam=...)."
        )

    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        raise NotImplementedError(
            "TiingoProvider.get_series is out of scope for #1218 Part 1 (daily batch + "
            "OHLCV only). Tiingo is declared on the 'daily' seam only (#1798), so "
            "services.asset_market_service's Explore history modal is routed to the "
            "'intraday' seam's vendor instead; reaching this line means something "
            "bypassed get_provider(seam=...)."
        )


_VENDOR_PROVIDERS: dict[str, type[MarketDataProvider]] = {
    "yfinance": YFinanceProvider,
    "tiingo": TiingoProvider,
}


# ─── Seams (#1798) ──────────────────────────────────────────────────────
#
# One env var could not express the vendor split the ADR actually decided.
# ``MARKET_DATA_PROVIDER=tiingo`` selected Tiingo for EVERY method, and Tiingo
# serves daily bars only — so the single global flip took the live oracle
# push, the paper marks and the Explore history modal down with it. Routing is
# therefore per SEAM: a seam is a family of reads that one feature makes inside
# one run, and each seam resolves its own vendor.
#
#   daily    — daily bars: the strategy signal evaluation behind every
#              marketplace tick (vault AND paper deployments) and the
#              generation-path fusion/backtester panels, whose artifacts the
#              daily-returns series is then derived from. Vendor:
#              MARKET_DATA_DAILY_PROVIDER, falling back to MARKET_DATA_PROVIDER,
#              falling back to yfinance. This is the seam Tiingo can serve.
#   intraday — the live/interactive seam: intraday quotes, arbitrary-interval
#              series, AND any daily context bar the same run needs (the oracle
#              snapshot reads ^VIX intraday and ^GSPC daily in one call).
#              Vendor: MARKET_DATA_PROVIDER, falling back to yfinance.
#
# The ADR's "never mix vendors inside one run" is unchanged and is exactly why
# the intraday seam serves daily methods too: the oracle snapshot is one run,
# so its ^GSPC moving averages come from the same vendor as its ^VIX quote.
# What #1798 adds is that different FEATURES may sit on different vendors —
# see docs/adr/market-data-sourcing.md § "Amendment: per-seam routing" for the
# feature-by-feature table.
DAILY_SEAM = "daily"
INTRADAY_SEAM = "intraday"

# Which ABC methods each seam will serve. The daily seam's refusal does NOT
# depend on which vendor is configured — a daily-seam caller that needs a live
# quote must ask the intraday seam BY NAME, so that reaching across vendors is
# always a visible act in the diff rather than an accident of today's flag
# values.
_SEAM_METHODS: dict[str, frozenset[str]] = {
    DAILY_SEAM: frozenset({"get_daily_close_batch", "get_daily_ohlcv"}),
    INTRADAY_SEAM: frozenset(
        {
            "get_intraday_quote",
            "get_intraday_quotes_batch",
            "get_series",
            "get_daily_close_batch",
            "get_daily_ohlcv",
        }
    ),
}

# Which seams each vendor can actually serve. Declared, not inferred: a vendor
# on the intraday seam must implement the WHOLE interface (that seam's runs mix
# quote and daily-bar reads), which is why Tiingo — daily bars only, three
# NotImplementedError methods — declares the daily seam alone.
_VENDOR_SEAMS: dict[str, frozenset[str]] = {
    "yfinance": frozenset({DAILY_SEAM, INTRADAY_SEAM}),
    "tiingo": frozenset({DAILY_SEAM}),
}

# The env var each seam reads first. The daily seam falls back to
# MARKET_DATA_PROVIDER when its own var is unset, so the pre-#1798 single-var
# configuration keeps its exact meaning and a deploy of this change is a no-op.
_SEAM_ENV_VARS: dict[str, tuple[str, ...]] = {
    DAILY_SEAM: ("MARKET_DATA_DAILY_PROVIDER", "MARKET_DATA_PROVIDER"),
    INTRADAY_SEAM: ("MARKET_DATA_PROVIDER",),
}


class MarketDataSeamError(RuntimeError):
    """A seam was asked for something it does not serve.

    Raised for an unknown seam name and for a method outside the seam's
    declared set (the daily seam asked for an intraday quote, say). Loud on
    purpose: the alternative — quietly reaching for the other seam's vendor —
    is the vendor mix inside one run that the ADR forbids, and it would carry
    no signal at the call site."""


def _resolve_seam(seam: str) -> str:
    if seam not in _SEAM_METHODS:
        raise MarketDataSeamError(
            f"unknown market-data seam {seam!r} — expected one of {sorted(_SEAM_METHODS)}. "
            "Every call site names its seam explicitly (#1798)."
        )
    return seam


def provider_name(seam: str) -> str:
    """The active vendor name FOR ONE SEAM: ``MARKET_DATA_DAILY_PROVIDER`` (or
    ``MARKET_DATA_PROVIDER``) for ``"daily"``, ``MARKET_DATA_PROVIDER`` for
    ``"intraday"``; default ``"yfinance"`` for both.

    ``seam`` is required. Since #1798 there is no single "the active vendor" to
    return, and a function that guessed would hand a caller the wrong vendor's
    name to stamp on a row.

    Two fail-safes, both logged, both landing on ``"yfinance"``:

    * an unrecognized vendor name (a config typo) — same behaviour as before
      #1798, and the same posture as ``price_source.price_source_mode``;
    * a known vendor that does not serve THIS seam (``tiingo`` on the intraday
      seam). This is the substitution #1798 exists for: it is not a silent
      fallback, because the returned name is the vendor that actually serves
      the read, so every provenance stamp derived from it stays true. The ADR's
      no-silent-fallback rule is about the missing-token case, which still
      raises ``TiingoAPIKeyMissingError`` at construction.
    """
    _resolve_seam(seam)
    raw = ""
    for var in _SEAM_ENV_VARS[seam]:
        raw = os.getenv(var, "").strip().lower()
        if raw:
            break
    if not raw:
        return "yfinance"
    if raw not in _VENDOR_PROVIDERS:
        logger.warning("unknown market-data provider %r (seam=%s) — falling back to yfinance", raw, seam)
        return "yfinance"
    if seam not in _VENDOR_SEAMS.get(raw, frozenset()):
        logger.warning(
            "market-data vendor %r cannot serve the %s seam — that seam falls back to yfinance "
            "(see docs/adr/market-data-sourcing.md). The %s seam is unaffected.",
            raw,
            seam,
            DAILY_SEAM if seam == INTRADAY_SEAM else INTRADAY_SEAM,
        )
        return "yfinance"
    return raw


class SeamRoutedProvider(MarketDataProvider):
    """Dispatches each method to the seam's vendor, or refuses.

    A thin wrapper, and deliberately not a smart one: it does not fetch, it
    does not cache and it never reaches for the other seam's vendor. What it
    adds is that ``get_provider(seam="daily").get_series(...)`` raises a
    ``MarketDataSeamError`` naming both seams instead of silently working
    today (daily vendor = yfinance) and raising ``NotImplementedError`` from
    inside a vendor adapter the day the daily flag flips to Tiingo."""

    def __init__(self, inner: MarketDataProvider, seam: str, vendor_name: str) -> None:
        self._inner = inner
        self._seam = _resolve_seam(seam)
        self._vendor_name = vendor_name

    @property
    def seam(self) -> str:
        return self._seam

    @property
    def vendor_name(self) -> str:
        return self._vendor_name

    def _route(self, method: str):
        if method not in _SEAM_METHODS[self._seam]:
            other = INTRADAY_SEAM if self._seam == DAILY_SEAM else DAILY_SEAM
            raise MarketDataSeamError(
                f"{method}() is not served by the {self._seam!r} market-data seam "
                f"(vendor {self._vendor_name!r}, env {_SEAM_ENV_VARS[self._seam][0]}). "
                f"Ask for it explicitly with get_provider(seam={other!r}) — crossing seams is "
                "crossing vendors, so it must be visible at the call site (#1798)."
            )
        return getattr(self._inner, method)

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        return self._route("get_daily_close_batch")(tickers, period)

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        return self._route("get_daily_ohlcv")(ticker, start, end)

    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        return self._route("get_intraday_quote")(ticker)

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        return self._route("get_intraday_quotes_batch")(tickers)

    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        return self._route("get_series")(ticker, period, interval)


# Whether a vendor's INTRADAY feed is a delayed tape rather than a real-time
# one. A property of the vendor contract, declared here once, so a consumer
# that has to label a number for a user ("delayed") reads a stated fact
# instead of guessing from a timestamp at render time.
#
# yfinance: True. Its 1-minute bars come off a consolidated feed with a lag
# Yahoo does not contract to bound — the same property `oracle_updater`'s
# DEFAULT_MAX_UPSTREAM_STALENESS_SECONDS already treats as first-class.
_INTRADAY_DELAYED_BY_PROVIDER: dict[str, bool] = {
    "yfinance": True,
}


def intraday_is_delayed() -> bool:
    """Does the ACTIVE provider's intraday feed carry a delay?

    Fails toward ``True`` for a vendor that has not declared otherwise:
    claiming real-time for a feed nobody verified is the dishonest
    direction, and an unnecessary "delayed" badge costs nothing. A new
    provider that genuinely serves real-time intraday adds itself to
    ``_INTRADAY_DELAYED_BY_PROVIDER`` with ``False`` and a note saying what
    contract backs that claim.
    """
    return _INTRADAY_DELAYED_BY_PROVIDER.get(provider_name(INTRADAY_SEAM), True)


# ─── Postgres read-through cache (asset_daily_bars) ────────────────────


def _period_to_start_date(period: str, now: datetime) -> date:
    days = _PERIOD_DAYS.get(period, _DEFAULT_PERIOD_DAYS)
    return (now - timedelta(days=days)).date()


def _default_session_factory():
    from archimedes.db import get_session

    return get_session()


def _read_cached_series(session, ticker: str, start_date: date, ttl: timedelta, source: str) -> pd.Series | None:
    """Return a cached close-price ``Series`` for ``ticker`` if the cache was
    written by ``source``, covers back to (approximately) ``start_date``, and
    was written within ``ttl``. ``None`` on a source, coverage or freshness
    miss (caller re-fetches).

    See ``_read_cached_ohlcv`` for why the ``source`` filter is load-bearing.
    """
    from archimedes.models.asset_daily_bars import AssetDailyBar

    rows = (
        session.query(AssetDailyBar)
        .filter(
            AssetDailyBar.symbol == ticker,
            AssetDailyBar.source == source,
            AssetDailyBar.trade_date >= start_date,
        )
        .order_by(AssetDailyBar.trade_date)
        .all()
    )
    if not rows:
        return None

    earliest = rows[0].trade_date
    if earliest > start_date + timedelta(days=_COVERAGE_TOLERANCE_DAYS):
        return None  # cache doesn't reach back far enough for this request

    newest_fetch = max(r.fetched_at for r in rows)
    if newest_fetch.tzinfo is None:
        newest_fetch = newest_fetch.replace(tzinfo=UTC)
    if datetime.now(UTC) - newest_fetch > ttl:
        return None  # cache is past its freshness window

    idx = pd.to_datetime([r.trade_date for r in rows])
    return pd.Series([r.close for r in rows], index=idx, name=ticker)


def _write_cached_series(session, ticker: str, series: pd.Series, source: str) -> None:
    """Upsert ``series`` (close prices indexed by date) into ``asset_daily_bars``
    for ``ticker``, dialect-agnostic (works on SQLite in tests and Postgres in
    prod) via select-then-add/update rather than a dialect-specific ON
    CONFLICT clause.

    **Cross-vendor overwrites clear the rest of the bar (#1798).** This writer
    knows only ``close``. The row it upserts is keyed ``(symbol, trade_date)``
    — the table's unique constraint — so it cannot sidestep an existing row by
    adding a second one, and the row it lands on may have been written by a
    *different* vendor. Blindly assigning ``close`` + ``source`` there would
    leave the previous vendor's ``open/high/low/volume`` in place under the new
    vendor's label: a bar whose Close is Tiingo's and whose OHLV is yfinance's,
    stamped ``source='tiingo'``. ``_read_cached_ohlcv``'s ``source`` filter
    cannot catch that — the row now claims to be the vendor being asked for —
    so ``portfolio_backtester._fetch_price_panel`` (which consumes ``Volume``
    as well as ``Close``) would grade a silently blended panel. That is the
    exact failure the seam exists to prevent, reached through the write path
    instead of the read path.

    So on a vendor change we keep the one column we actually know and NULL the
    four we do not. The row becomes the honest partial bar it is, which
    ``_read_cached_ohlcv``'s existing partial-bar guard already treats as a
    miss — the next OHLCV read re-fetches the whole range from the new vendor
    and ``_write_cached_ohlcv`` (which writes every column) fills it back in.
    Cost: one extra vendor round-trip per symbol after a flip, which is the
    same cold-cache price the ``source`` filter already charges on reads.
    """
    from archimedes.models.asset_daily_bars import AssetDailyBar

    now = datetime.now(UTC)
    to_write: list[tuple[date, float]] = []
    for ts, close in series.items():
        try:
            close_f = float(close)
        except (TypeError, ValueError):
            continue
        if math.isnan(close_f):
            continue
        trade_date = ts.date() if hasattr(ts, "date") else ts
        to_write.append((trade_date, close_f))
    if not to_write:
        return

    dates = [d for d, _ in to_write]
    existing = {
        row.trade_date: row
        for row in session.query(AssetDailyBar)
        .filter(AssetDailyBar.symbol == ticker, AssetDailyBar.trade_date.in_(dates))
        .all()
    }
    displaced_vendors: set[str] = set()
    displaced_rows = 0
    for trade_date, close_f in to_write:
        row = existing.get(trade_date)
        if row is not None:
            if row.source != source:
                # A different vendor wrote this row and we only know `close`.
                # Drop the old vendor's OHLV rather than leave a bar stitched
                # from two vendors under one `source` label — see the docstring.
                displaced_vendors.add(row.source)
                displaced_rows += 1
                row.open = None
                row.high = None
                row.low = None
                row.volume = None
            row.close = close_f
            row.source = source
            row.fetched_at = now
        else:
            session.add(
                AssetDailyBar(
                    symbol=ticker,
                    trade_date=trade_date,
                    close=close_f,
                    source=source,
                    fetched_at=now,
                )
            )

    if displaced_vendors:
        # Loud on purpose: this is the visible half of a vendor flip. It says
        # which vendor's bars were demoted to close-only and why the next
        # OHLCV read for this symbol will go back to the network.
        logger.info(
            "market data cache: close-only write by %s cleared OHLV previously written by %s "
            "for %s (%d row(s)); the next OHLCV read re-fetches the full bars",
            source,
            ", ".join(sorted(displaced_vendors)),
            ticker,
            displaced_rows,
        )


def _read_cached_ohlcv(
    session, ticker: str, start_date: date, end_date: date, ttl: timedelta, source: str
) -> pd.DataFrame | None:
    """Return a cached OHLCV ``DataFrame`` for ``ticker`` if the cache was
    written by ``source``, covers back to (approximately) ``start_date``,
    holds through ``end_date``, every row carries a full bar (not a
    close-only row written by ``get_daily_close_batch``'s writer), and was
    written within ``ttl``. ``None`` on any miss (caller re-fetches the whole
    range).

    **The ``source`` filter is the anti-source-mixing guard (#1218).** Rows
    in ``asset_daily_bars`` record which vendor wrote them, and
    ``_write_cached_ohlcv`` has always stamped that column — but the read
    used to ignore it, matching on ``symbol`` alone. On a system whose cache
    is already full of ``source='yfinance'`` rows (i.e. production), flipping
    ``MARKET_DATA_PROVIDER=tiingo`` would therefore serve **yfinance** bars
    for every warm symbol and **Tiingo** bars for every cold one — inside a
    single backtest panel, with no error and no log line. The two vendors do
    not agree bar-for-bar (different adjustment pipelines, different
    corporate-action timing), so that is a silently mixed-source panel
    graded as if it came from one vendor: exactly the failure the seam
    exists to prevent, and one that no amount of correctness in
    ``TiingoProvider`` itself could catch.

    Filtering on ``source`` makes a provider flip a cold cache rather than a
    corrupt one. Cost: the first run after a flip re-fetches every symbol.
    That is the intended price — a cache miss is cheap and visible, a
    mixed-source backtest is neither.
    """
    from archimedes.models.asset_daily_bars import AssetDailyBar

    rows = (
        session.query(AssetDailyBar)
        .filter(
            AssetDailyBar.symbol == ticker,
            AssetDailyBar.source == source,
            AssetDailyBar.trade_date >= start_date,
            AssetDailyBar.trade_date <= end_date,
        )
        .order_by(AssetDailyBar.trade_date)
        .all()
    )
    if not rows:
        return None

    # A row primed by get_daily_close_batch's writer only carries `close` —
    # open/high/low/volume are NULL. Treat that as a miss for THIS method:
    # a partial bar is not a valid OHLCV cache entry, and re-fetching the
    # whole range fills it in properly (via _write_cached_ohlcv below).
    if any(r.open is None or r.high is None or r.low is None or r.volume is None for r in rows):
        return None

    earliest = rows[0].trade_date
    if earliest > start_date + timedelta(days=_COVERAGE_TOLERANCE_DAYS):
        return None  # cache doesn't reach back far enough for this request

    # Forward-coverage is load-bearing, not symmetry for its own sake: both
    # call sites default `end` to "today" on every call, so a cache primed
    # through day D is routinely re-read after the date rolls to D+1 while
    # still inside the TTL. Without this check that read is a "hit" on a
    # silently shorter frame — a backtest window truncation that moves every
    # graded number with no signal. Known tradeoff: a symbol whose vendor
    # data genuinely ends before `end_date` (delisted/halted) now misses and
    # re-fetches every call; correctness over cache hits — recording a
    # vendor-end marker at write time is the fix for that, out of scope here.
    latest = rows[-1].trade_date
    if latest < end_date - timedelta(days=_COVERAGE_TOLERANCE_DAYS):
        return None  # cache doesn't reach forward to the requested end

    newest_fetch = max(r.fetched_at for r in rows)
    if newest_fetch.tzinfo is None:
        newest_fetch = newest_fetch.replace(tzinfo=UTC)
    if datetime.now(UTC) - newest_fetch > ttl:
        return None  # cache is past its freshness window

    idx = pd.to_datetime([r.trade_date for r in rows])
    return pd.DataFrame(
        {
            "Open": [r.open for r in rows],
            "High": [r.high for r in rows],
            "Low": [r.low for r in rows],
            "Close": [r.close for r in rows],
            "Volume": [r.volume for r in rows],
        },
        index=idx,
    )


#: Rows per flush in the OHLCV cache write (#1632 mitigation).
#:
#: A full multi-year OHLCV frame is thousands of rows, and the ORM turns the
#: whole pending set into ONE ``executemany`` at commit — which is the exact
#: frame at the top of #1632's abort traceback (psycopg2 ``do_executemany``).
#: Flushing every N rows bounds that batch. Deliberately a constant and not an
#: env knob: a number nobody can tune is a number nobody has to reason about
#: during an incident, and it should be deleted with the rest of this
#: mitigation rather than inherited as config.
_OHLCV_WRITE_CHUNK_ROWS = 500

#: Serializes the OHLCV cache write+commit across threads in this process.
#:
#: **MITIGATION, NOT A FIX — and the distinction is the point.**
#:
#: *Proven:* the faulthandler traceback posted on #1632 shows a backend
#: container dying with ``Fatal Python error: Aborted`` inside psycopg2's
#: ``do_executemany``, on this module's OHLCV cache-write commit, reached from
#: ``paper_trading.replay_spec_with_decisions`` via ``fetch_real_panel``. That
#: is a C-level abort: no Python ``except`` arm can catch it, which is why the
#: existing fail-soft arms below did not contain it and the container died.
#:
#: *Not proven:* the mechanism. An abort inside libpq is consistent with one
#: connection being used from two threads at once, but we have NOT shown that
#: is what happens here, and this lock does not prove it either. It removes
#: in-process write concurrency as a variable so the fleet stops cycling while
#: the real cause is found. If the aborts continue with this held, the
#: concurrency hypothesis is wrong — which is itself a useful result.
#:
#: Scoped to the write path only: the vendor fetch happens above it, so a
#: network call never holds this lock. Delete both this and the chunking above
#: once #1632 has a proven cause.
_OHLCV_CACHE_WRITE_LOCK = threading.Lock()


def _write_cached_ohlcv(session, ticker: str, df: pd.DataFrame, source: str) -> None:
    """Upsert a full OHLCV frame (indexed by date, columns
    Open/High/Low/Close/Volume — ``fetch_ohlcv``'s output shape) into
    ``asset_daily_bars`` for ``ticker``. Mirrors ``_write_cached_series`` but
    persists the whole bar, not close-only, so the row is a valid cache entry
    for ``get_daily_ohlcv`` as well as ``get_daily_close_batch``.

    Needs no cross-vendor guard of its own (unlike ``_write_cached_series``,
    whose docstring explains the hazard): every column is assigned on every
    update, so landing on another vendor's row REPLACES the whole bar rather
    than blending it. The ``source`` stamp is therefore always true of all
    five values."""
    from archimedes.models.asset_daily_bars import AssetDailyBar

    def _float_or_none(value: object) -> float | None:
        try:
            v = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return None if math.isnan(v) else v

    now = datetime.now(UTC)
    to_write: list[tuple[date, float | None, float | None, float | None, float, float | None]] = []
    for ts, row in df.iterrows():
        close_f = _float_or_none(row.get("Close"))
        if close_f is None:
            continue
        trade_date = ts.date() if hasattr(ts, "date") else ts
        to_write.append(
            (
                trade_date,
                _float_or_none(row.get("Open")),
                _float_or_none(row.get("High")),
                _float_or_none(row.get("Low")),
                close_f,
                _float_or_none(row.get("Volume")),
            )
        )
    if not to_write:
        return

    dates = [d for d, *_ in to_write]
    existing = {
        row.trade_date: row
        for row in session.query(AssetDailyBar)
        .filter(AssetDailyBar.symbol == ticker, AssetDailyBar.trade_date.in_(dates))
        .all()
    }
    pending = 0
    for trade_date, open_f, high_f, low_f, close_f, volume_f in to_write:
        row = existing.get(trade_date)
        if row is not None:
            row.open = open_f
            row.high = high_f
            row.low = low_f
            row.close = close_f
            row.volume = volume_f
            row.source = source
            row.fetched_at = now
        else:
            session.add(
                AssetDailyBar(
                    symbol=ticker,
                    trade_date=trade_date,
                    open=open_f,
                    high=high_f,
                    low=low_f,
                    close=close_f,
                    volume=volume_f,
                    source=source,
                    fetched_at=now,
                )
            )
        pending += 1
        if pending >= _OHLCV_WRITE_CHUNK_ROWS:
            # FLUSH, never commit. The caller owns the transaction, so this
            # changes only the SIZE of the batch psycopg2 executes, not the
            # all-or-nothing semantics: a failure in any chunk — here or at the
            # caller's commit — still rolls the whole cache write back and
            # still lands in the caller's existing IntegrityError /
            # SQLAlchemyError arms unchanged. Committing here instead would
            # leave a half-written range behind on failure, and a partially
            # cached window reads as a coverage hit on the next call.
            session.flush()
            pending = 0
    # The final partial chunk is left to the caller's commit — flushing it here
    # would only duplicate that work.


class CachingMarketDataProvider(MarketDataProvider):
    """Wraps another provider with the Postgres ``asset_daily_bars``
    read-through cache for ``get_daily_close_batch`` only. Every other method
    passes straight through, uncached — see the module docstring for why."""

    def __init__(self, inner: MarketDataProvider, source_name: str, session_factory=None) -> None:
        self._inner = inner
        self._source_name = source_name
        self._session_factory = session_factory or _default_session_factory
        self._ttl = timedelta(hours=_int_env("MARKET_DATA_CACHE_TTL_HOURS", DEFAULT_CACHE_TTL_HOURS))

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        if not tickers:
            return {}
        start_date = _period_to_start_date(period, datetime.now(UTC))

        result: dict[str, pd.Series] = {}
        missing: dict[str, str] = {}
        session = self._session_factory()
        try:
            for key, ticker in tickers.items():
                series = _read_cached_series(session, ticker, start_date, self._ttl, self._source_name)
                if series is not None:
                    result[key] = series
                else:
                    missing[key] = ticker
        finally:
            session.close()

        if not missing:
            return result

        fetched = self._inner.get_daily_close_batch(missing, period)
        # Merge BEFORE priming: the fetch succeeded, and the caller gets that
        # data no matter what happens to the best-effort cache write below.
        result.update(fetched)
        if fetched:
            session = self._session_factory()
            try:
                for key, series in fetched.items():
                    ticker = missing.get(key)
                    if ticker is None or series is None or series.empty:
                        continue
                    _write_cached_series(session, ticker, series, self._source_name)
                session.commit()
            except IntegrityError:
                # Concurrent writers priming the same cold window (two tasks,
                # or the refresh loop overlapping a request sweep): the loser
                # trips uq_asset_daily_bars_symbol_trade_date at commit. The
                # winner's rows are equivalent vendor data, so this is benign —
                # roll back and serve the fetch; the next read is warm.
                session.rollback()
                logger.info("asset_daily_bars prime lost a concurrent-writer race (benign); next read is warm")
            except SQLAlchemyError:
                # Any other cache-write failure is still only a cache failure —
                # loud, but never allowed to discard a successful fetch.
                session.rollback()
                logger.warning("asset_daily_bars cache write failed (fetch still served)", exc_info=True)
            finally:
                session.close()

        return result

    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        return self._inner.get_intraday_quote(ticker)

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        # Pass-through, and it must STAY a pass-through: intraday is uncached
        # by design (module docstring) — a cached quote handed to the on-chain
        # push gate or to a paper mark is a stale reading wearing a fresh
        # label, which is the failure both consumers' staleness rules exist
        # to prevent.
        return self._inner.get_intraday_quotes_batch(tickers)

    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        return self._inner.get_series(ticker, period, interval)

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        """Read-through cache for the generation-path (fusion / portfolio
        backtester) OHLCV fetch — the #1218 volume driver these two call
        sites contribute alongside the universe sweep behind
        ``get_daily_close_batch``. On a coverage/freshness miss, the whole
        ``[start, end)`` range is fetched from the vendor and returned
        UNCHANGED (only a best-effort cache write happens afterward) — a
        fetch failure never gets a chance to corrupt the returned frame, and
        a cold cache produces the exact same object the pre-seam direct call
        would have."""
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end) if end else date.today()

        session = self._session_factory()
        try:
            cached = _read_cached_ohlcv(session, ticker, start_date, end_date, self._ttl, self._source_name)
        finally:
            session.close()
        if cached is not None:
            return cached

        fetched = self._inner.get_daily_ohlcv(ticker, start, end)
        if fetched is None or fetched.empty:
            return fetched

        session = self._session_factory()
        # #1632 mitigation — see _OHLCV_CACHE_WRITE_LOCK for what is proven and
        # what is only hypothesised. The lock is entered AFTER the vendor fetch
        # above, so it never spans a network call; it covers the write, the
        # commit, and the rollback arms, because a rollback is DB work too. It
        # adds no exception handling of its own: every arm below is byte-for-
        # byte the one that was already here, so a write failure fails exactly
        # as it did before.
        with _OHLCV_CACHE_WRITE_LOCK:
            try:
                _write_cached_ohlcv(session, ticker, fetched, self._source_name)
                session.commit()
            except IntegrityError:
                # Same benign concurrent-writer race as get_daily_close_batch's
                # prime — the loser rolls back; the fetch is still served.
                session.rollback()
                logger.info("asset_daily_bars OHLCV prime lost a concurrent-writer race (benign); next read is warm")
            except SQLAlchemyError:
                session.rollback()
                logger.warning("asset_daily_bars OHLCV cache write failed (fetch still served)", exc_info=True)
            finally:
                session.close()

        return fetched


def get_provider(*, seam: str) -> SeamRoutedProvider:
    """The active provider FOR ONE SEAM, cache-wrapped and seam-routed. Call
    sites use this — never ``YFinanceProvider`` (or ``yfinance``) directly — so
    a vendor swap changes every choke point (including the #775 cross-check's
    secondary source) in one place.

    ``seam`` is keyword-only and required: ``"daily"`` for daily bars (the
    marketplace tick's signal evaluation and the generation-path panels, and so
    the daily-returns series derived from those backtests) and ``"intraday"``
    for the live/interactive reads (oracle push, paper marks, the Explore
    history modal, and any daily context bar those same runs need).
    Since #1798 the two can resolve to different vendors, so a call site that
    did not say which one it meant would be picking a vendor by accident.

    Nesting, outermost first: ``SeamRoutedProvider`` (refuses off-seam methods
    before anything is fetched) → ``CachingMarketDataProvider`` (the per-vendor
    ``asset_daily_bars`` cache) → the vendor adapter."""
    name = provider_name(seam)
    vendor = _VENDOR_PROVIDERS[name]()
    cached = CachingMarketDataProvider(vendor, source_name=name)
    return SeamRoutedProvider(cached, seam=seam, vendor_name=name)
