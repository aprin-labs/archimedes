"""Market-data vendor seam (#1218) — analytics-engine's choke point.

#1218 priced yfinance as an unlicensed commercial dependency that scales with
strategies x symbols x re-run cadence, and named ``data.fetch_ohlcv`` as (one
of) the seam(s) to substitute a vendor at. This module IS that seam:
``data.fetch_ohlcv`` becomes a thin façade over ``get_provider().fetch_ohlcv``
— identical signature, identical default behavior (yfinance), vendor-swappable
via the ``MARKET_DATA_PROVIDER`` env var (this package's only market-data
variable — see the #1798 note below).

analytics-engine is a standalone, DB-less package (no sqlalchemy/psycopg
dependency — see ``pyproject.toml``) that runs independently of ``backend``'s
Postgres, so this provider has no cache layer, unlike
``backend.archimedes.services.market_data_provider``'s
``asset_daily_bars``-backed one. Both modules default to the same vendor name
(``"yfinance"``), and a vendor implementation is registered separately in each
(there is no shared package to put one implementation in; ``backend`` already
depends on THIS package for ``fetch_ohlcv``, which is how backend's
``YFinanceProvider.get_daily_ohlcv`` reuses this fetch/retry/normalize path).

**This seam still reads ``MARKET_DATA_PROVIDER`` only, and registers no Tiingo
adapter (#1798).** Backend's daily-bar seam now resolves
``MARKET_DATA_DAILY_PROVIDER`` first, so flipping THAT variable does not reach
this module — and it must not, because ``_PROVIDERS`` here has one entry: a
"tiingo" value would log the unknown-vendor warning and serve yfinance, which
is exactly the "ran on licensed data" lie the ADR's no-silent-fallback rule
exists to prevent. What flipping backend's daily flag actually does is stop
routing daily OHLCV through here at all (backend selects ``TiingoProvider``
instead of ``YFinanceProvider``, and only the latter delegates to this
module), so the two never disagree about which vendor served a bar. The
standalone CLI path (``cli.py`` → ``data.fetch_ohlcv``) stays on yfinance
until a Tiingo adapter is registered here too.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod

import pandas as pd
import yfinance as yf

from archimedes_analytics_engine.data import _MAX_RETRIES, _RETRY_DELAY_S, normalize_ohlcv

logger = logging.getLogger(__name__)


class MarketDataProvider(ABC):
    """Vendor abstraction for OHLCV history. Default implementation (below) is
    yfinance, unchanged in behavior from ``data.fetch_ohlcv`` before this seam
    existed."""

    @abstractmethod
    def fetch_ohlcv(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Return a normalized OHLCV frame for ``symbol`` over [start, end).
        Raises on a genuinely unfetchable symbol/range (see
        ``YFinanceProvider`` for the exact retry/error contract this
        preserves)."""


class YFinanceProvider(MarketDataProvider):
    """Default provider — the exact retry/normalize logic that used to live
    directly in ``data.fetch_ohlcv``, moved here unchanged."""

    def fetch_ohlcv(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                # auto_adjust=True applies yfinance's built-in split/dividend back-adjustment so
                # corporate actions (e.g. KO's Aug-2012 2-for-1 split) don't show up as spurious
                # overnight price discontinuities that corrupt pairs-trading signals and PBO.
                data = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
            except Exception as exc:  # transient network/yfinance error — retry with backoff
                last_exc = exc
                if attempt == _MAX_RETRIES:
                    break
                logger.warning("fetch_ohlcv(%s) attempt %d failed: %s — retrying", symbol, attempt, exc)
                time.sleep(_RETRY_DELAY_S * attempt)
                continue

            if data.empty:
                if attempt == _MAX_RETRIES:
                    raise ValueError(f"No data returned for symbol={symbol} in range {start}..{end}")
                logger.warning("fetch_ohlcv(%s) returned empty — retrying (attempt %d)", symbol, attempt)
                time.sleep(_RETRY_DELAY_S * attempt)
                continue

            result = normalize_ohlcv(data, symbol=symbol)
            if len(result) == 0:
                raise ValueError(f"All rows dropped after normalization for {symbol} — check date range")
            return result

        raise RuntimeError(f"yfinance download failed for {symbol} after {_MAX_RETRIES} attempts") from last_exc


_PROVIDERS: dict[str, type[MarketDataProvider]] = {
    "yfinance": YFinanceProvider,
}


def provider_name() -> str:
    """The active vendor name: ``MARKET_DATA_PROVIDER`` env, default
    ``"yfinance"``. An unrecognized value fails SAFE to the default (logged)
    rather than crashing a backtest run over a config typo."""
    raw = os.getenv("MARKET_DATA_PROVIDER", "yfinance").strip().lower()
    if raw not in _PROVIDERS:
        logger.warning("unknown MARKET_DATA_PROVIDER=%r — falling back to yfinance", raw)
        return "yfinance"
    return raw


def get_provider() -> MarketDataProvider:
    """The active provider. Call sites use this — never ``YFinanceProvider``
    directly — so a vendor swap via ``MARKET_DATA_PROVIDER`` changes every
    choke point that goes through it."""
    return _PROVIDERS[provider_name()]()
