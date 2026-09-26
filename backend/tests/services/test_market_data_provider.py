"""Hermetic tests for the #1218 / #775 market-data vendor seam.

Covers: provider selection (default + unknown-value fail-safe), the
``asset_daily_bars`` Postgres cache's read-through/miss/freshness/coverage
behavior, and that the intraday methods (live oracle pushes, the #775
cross-check's secondary reading) are never routed through that cache.

Hermetic: an isolated, fresh SQLite engine (not the module-level
``archimedes.db`` singleton) is injected via ``session_factory`` — no real DB,
no network. The vendor boundary (``MarketDataProvider``) is a hand-rolled
fake, not real yfinance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from archimedes.services.market_data_provider import (
    CachingMarketDataProvider,
    MarketDataProvider,
    YFinanceProvider,
    get_provider,
    provider_name,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ─── Provider selection ────────────────────────────────────────────────


class TestProviderSelection:
    """Selection on the ``intraday`` seam — ``MARKET_DATA_PROVIDER``, the
    variable that existed before #1798 split the seams. The ``daily`` seam's
    own variable and the routing between the two are covered in
    ``test_market_data_seams.py``."""

    def test_default_is_yfinance(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        assert provider_name("intraday") == "yfinance"

    def test_explicit_yfinance(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
        assert provider_name("intraday") == "yfinance"

    def test_unknown_value_falls_back_to_yfinance(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("MARKET_DATA_PROVIDER", "some_unreleased_vendor")
        with caplog.at_level(logging.WARNING):
            assert provider_name("intraday") == "yfinance"
        assert any("some_unreleased_vendor" in rec.message for rec in caplog.records)

    def test_case_and_whitespace_insensitive(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "  YFinance  ")
        assert provider_name("intraday") == "yfinance"

    def test_get_provider_wraps_vendor_in_caching_provider(self):
        # Nesting since #1798: SeamRoutedProvider → CachingMarketDataProvider
        # → vendor. The cache layer is still there, one level in.
        provider = get_provider(seam="intraday")
        assert isinstance(provider._inner, CachingMarketDataProvider)
        # The inner vendor is the default (yfinance) implementation.
        assert isinstance(provider._inner._inner, YFinanceProvider)


# ─── Cache read-through / miss ──────────────────────────────────────────


@pytest.fixture()
def session_factory(tmp_path):
    """An isolated SQLite engine/session factory with asset_daily_bars
    created — independent of the app's module-level engine."""
    from archimedes.db import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'market_data.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


class _FakeVendor(MarketDataProvider):
    """A hand-rolled fake vendor — records every call it receives so tests
    can assert whether the cache actually skipped it."""

    def __init__(self, series_by_key: dict[str, pd.Series]) -> None:
        self.series_by_key = series_by_key
        self.daily_batch_calls: list[dict[str, str]] = []

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        self.daily_batch_calls.append(dict(tickers))
        return {k: self.series_by_key[k] for k in tickers if k in self.series_by_key}

    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        raise AssertionError("get_intraday_quote must never be called by the caching wrapper's own logic")

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        raise AssertionError("get_intraday_quotes_batch must never be called by the caching wrapper's own logic")

    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        raise AssertionError("get_series must never be called by the caching wrapper's own logic")

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        raise AssertionError("get_daily_ohlcv must never be called by TestDailyCloseBatchCache")


def _series(n_days: int = 30, start_price: float = 100.0) -> pd.Series:
    idx = pd.date_range(end=pd.Timestamp.now("UTC").normalize(), periods=n_days, freq="D")
    return pd.Series([start_price + i for i in range(n_days)], index=idx, name="X")


def _ohlcv_frame(n_days: int = 30, start_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp.now("UTC").normalize(), periods=n_days, freq="D")
    close = pd.Series([start_price + i for i in range(n_days)], index=idx)
    return pd.DataFrame(
        {
            "Open": close.to_numpy() - 0.5,
            "High": close.to_numpy() + 1.0,
            "Low": close.to_numpy() - 1.0,
            "Close": close.to_numpy(),
            "Volume": [1_000_000.0] * n_days,
        },
        index=idx,
    )


class _FakeOhlcvVendor(MarketDataProvider):
    """A hand-rolled fake vendor for ``get_daily_ohlcv`` — records every call
    it receives so tests can assert whether the cache actually skipped it."""

    def __init__(self, frames_by_ticker: dict[str, pd.DataFrame]) -> None:
        self.frames_by_ticker = frames_by_ticker
        self.ohlcv_calls: list[tuple[str, str, str]] = []

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        raise AssertionError("not exercised in TestDailyOhlcvCache")

    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        raise AssertionError("not exercised in TestDailyOhlcvCache")

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        raise AssertionError("not exercised in TestDailyOhlcvCache")

    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        raise AssertionError("not exercised in TestDailyOhlcvCache")

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        self.ohlcv_calls.append((ticker, start, end))
        if ticker not in self.frames_by_ticker:
            raise ValueError(f"no data for {ticker}")
        return self.frames_by_ticker[ticker]


class TestDailyCloseBatchCache:
    def test_cold_cache_is_a_miss_and_primes(self, session_factory):
        vendor = _FakeVendor({"sSPY": _series(30)})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)

        result = provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo")

        assert vendor.daily_batch_calls == [{"sSPY": "SPY"}]  # vendor WAS hit (cold cache)
        assert "sSPY" in result
        assert len(result["sSPY"]) == 30

        # Verify it actually wrote through to the DB.
        from archimedes.models.asset_daily_bars import AssetDailyBar

        session = session_factory()
        try:
            rows = session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").all()
            assert len(rows) == 30
            assert all(r.source == "yfinance" for r in rows)
        finally:
            session.close()

    def test_warm_cache_within_ttl_skips_vendor(self, session_factory):
        vendor = _FakeVendor({"sSPY": _series(30)})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)

        provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo")  # primes the cache
        vendor.daily_batch_calls.clear()

        result = provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo")

        assert vendor.daily_batch_calls == []  # cache hit — vendor NOT called
        assert len(result["sSPY"]) == 30

    def test_stale_cache_past_ttl_refetches(self, session_factory):
        vendor = _FakeVendor({"sSPY": _series(30)})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        provider._ttl = timedelta(hours=1)
        provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo")

        # Backdate every row's fetched_at past the TTL.
        from archimedes.models.asset_daily_bars import AssetDailyBar

        session = session_factory()
        try:
            for row in session.query(AssetDailyBar).all():
                row.fetched_at = datetime.now(UTC) - timedelta(hours=2)
            session.commit()
        finally:
            session.close()

        vendor.daily_batch_calls.clear()
        provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo")
        assert vendor.daily_batch_calls == [{"sSPY": "SPY"}]  # stale → refetched

    def test_insufficient_back_coverage_refetches(self, session_factory):
        """A cache primed for a SHORT period ("1mo") does not satisfy a later
        request for a LONGER period ("2y") over the same symbol — the earliest
        cached row doesn't reach back far enough."""
        vendor = _FakeVendor({"sSPY": _series(30)})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo")  # only 30 days cached

        vendor.series_by_key["sSPY"] = _series(731)
        vendor.daily_batch_calls.clear()
        result = provider.get_daily_close_batch({"sSPY": "SPY"}, period="2y")

        assert vendor.daily_batch_calls == [{"sSPY": "SPY"}]  # coverage gap → refetched
        assert len(result["sSPY"]) == 731

    def test_partial_miss_only_fetches_missing_symbols(self, session_factory):
        vendor = _FakeVendor({"sSPY": _series(30), "sAGG": _series(30)})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo")  # primes sSPY only
        vendor.daily_batch_calls.clear()

        result = provider.get_daily_close_batch({"sSPY": "SPY", "sAGG": "AGG"}, period="1mo")

        assert vendor.daily_batch_calls == [{"sAGG": "AGG"}]  # only the miss goes to the vendor
        assert set(result) == {"sSPY", "sAGG"}

    def test_empty_tickers_is_a_noop(self, session_factory):
        vendor = _FakeVendor({})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        assert provider.get_daily_close_batch({}, period="1mo") == {}
        assert vendor.daily_batch_calls == []

    def test_vendor_miss_leaves_symbol_absent_not_erroring(self, session_factory):
        """A vendor that returns nothing for a requested ticker (delisted,
        typo) must not raise — the caller (e.g. _fetch_price_histories)
        expects a silently-omitted key."""
        vendor = _FakeVendor({})  # returns nothing for anything
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        result = provider.get_daily_close_batch({"sNOPE": "NOPE"}, period="1mo")
        assert result == {}


# ─── Intraday / generic passthrough — never cache-backed ───────────────


class TestUncachedPassthrough:
    def test_intraday_quote_never_touches_the_cache(self, session_factory):
        vendor = _FakeVendorPassthroughSpy()
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        assert provider.get_intraday_quote("SPY") == (123.0, vendor.ts)
        assert vendor.quote_calls == ["SPY"]

    def test_intraday_quotes_batch_never_touches_the_cache(self, session_factory):
        vendor = _FakeVendorPassthroughSpy()
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        assert provider.get_intraday_quotes_batch({"sSPY": "SPY"}) == {"sSPY": (123.0, vendor.ts)}
        assert vendor.batch_calls == [{"sSPY": "SPY"}]

    def test_get_series_never_touches_the_cache(self, session_factory):
        vendor = _FakeVendorPassthroughSpy()
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        out = provider.get_series("SPY", "1y", "1wk")
        assert list(out) == [1.0, 2.0]
        assert vendor.series_calls == [("SPY", "1y", "1wk")]


class _FakeVendorPassthroughSpy(MarketDataProvider):
    def __init__(self) -> None:
        self.ts = datetime(2026, 8, 19, tzinfo=UTC)
        self.quote_calls: list[str] = []
        self.batch_calls: list[dict[str, str]] = []
        self.series_calls: list[tuple[str, str, str]] = []

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        raise AssertionError("not exercised in this test class")

    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        self.quote_calls.append(ticker)
        return (123.0, self.ts)

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        self.batch_calls.append(dict(tickers))
        return dict.fromkeys(tickers, (123.0, self.ts))

    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        self.series_calls.append((ticker, period, interval))
        return pd.Series([1.0, 2.0])

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        raise AssertionError("not exercised in TestUncachedPassthrough")


# ─── Daily OHLCV cache (generation-path seam: fusion_market_data /
#     portfolio_backtester, #1218 follow-up) ────────────────────────────


class TestDailyOhlcvCache:
    def test_cold_cache_is_a_miss_and_primes(self, session_factory):
        frame = _ohlcv_frame(30)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()

        result = provider.get_daily_ohlcv("SPY", start, end)

        assert vendor.ohlcv_calls == [("SPY", start, end)]  # vendor WAS hit (cold cache)
        # Anti-goal: cache-cold output must be byte-identical to the vendor's
        # raw frame — the cache layer must never reshape/reindex/coerce it.
        pd.testing.assert_frame_equal(result, frame)

        from archimedes.models.asset_daily_bars import AssetDailyBar

        session = session_factory()
        try:
            rows = session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").all()
            assert len(rows) == 30
            assert all(r.source == "yfinance" for r in rows)
            assert all(r.open is not None and r.high is not None and r.low is not None for r in rows)
            assert all(r.volume is not None for r in rows)
        finally:
            session.close()

    def test_warm_cache_within_ttl_skips_vendor(self, session_factory):
        frame = _ohlcv_frame(30)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()

        provider.get_daily_ohlcv("SPY", start, end)  # primes the cache
        vendor.ohlcv_calls.clear()

        result = provider.get_daily_ohlcv("SPY", start, end)

        assert vendor.ohlcv_calls == []  # cache hit — vendor NOT called
        assert len(result) == 30
        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]

    def test_stale_cache_past_ttl_refetches(self, session_factory):
        frame = _ohlcv_frame(30)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        provider._ttl = timedelta(hours=1)
        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()
        provider.get_daily_ohlcv("SPY", start, end)

        from archimedes.models.asset_daily_bars import AssetDailyBar

        session = session_factory()
        try:
            for row in session.query(AssetDailyBar).all():
                row.fetched_at = datetime.now(UTC) - timedelta(hours=2)
            session.commit()
        finally:
            session.close()

        vendor.ohlcv_calls.clear()
        provider.get_daily_ohlcv("SPY", start, end)
        assert vendor.ohlcv_calls == [("SPY", start, end)]  # stale → refetched

    def test_insufficient_back_coverage_refetches(self, session_factory):
        """A cache primed for a SHORT window does not satisfy a later request
        reaching further back — the earliest cached row doesn't cover it."""
        short_frame = _ohlcv_frame(30)
        vendor = _FakeOhlcvVendor({"SPY": short_frame})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        short_start = short_frame.index[0].date().isoformat()
        end = short_frame.index[-1].date().isoformat()
        provider.get_daily_ohlcv("SPY", short_start, end)  # only 30 days cached

        long_frame = _ohlcv_frame(365)
        vendor.frames_by_ticker["SPY"] = long_frame
        long_start = long_frame.index[0].date().isoformat()
        vendor.ohlcv_calls.clear()

        result = provider.get_daily_ohlcv("SPY", long_start, end)

        assert vendor.ohlcv_calls == [("SPY", long_start, end)]  # coverage gap → refetched
        assert len(result) == 365

    def test_insufficient_forward_coverage_refetches(self, session_factory):
        """A warm, TTL-fresh cache primed through an earlier end day must NOT
        satisfy a later request whose end has advanced past it. This is the
        routine case, not an edge: both call sites default ``end`` to "today"
        on every call, so any date rollover inside the TTL window lands here —
        and serving the old frame silently truncates the backtest window
        (moving every graded number computed over it) with no signal."""
        old_frame = _ohlcv_frame(30)
        old_frame.index = old_frame.index - pd.Timedelta(days=10)
        vendor = _FakeOhlcvVendor({"SPY": old_frame})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        start = old_frame.index[0].date().isoformat()
        old_end = old_frame.index[-1].date().isoformat()
        provider.get_daily_ohlcv("SPY", start, old_end)  # primes through old_end only

        new_frame = _ohlcv_frame(40)  # same first day, ends today (10 days later)
        vendor.frames_by_ticker["SPY"] = new_frame
        new_end = new_frame.index[-1].date().isoformat()
        vendor.ohlcv_calls.clear()

        result = provider.get_daily_ohlcv("SPY", start, new_end)

        assert vendor.ohlcv_calls == [("SPY", start, new_end)]  # end advanced past cache → refetched
        assert len(result) == 40

    def test_close_only_cached_row_is_not_a_valid_ohlcv_hit(self, session_factory):
        """A row primed by ``get_daily_close_batch``'s writer only carries
        ``close`` — open/high/low/volume are NULL. ``get_daily_ohlcv`` must
        treat that as a miss (not hand back a frame with NaN OHLC columns)
        and re-fetch + heal the row with the full bar."""
        from archimedes.services.market_data_provider import _write_cached_series

        close_series = _series(30)
        session = session_factory()
        try:
            _write_cached_series(session, "SPY", close_series, "yfinance")
            session.commit()
        finally:
            session.close()

        frame = _ohlcv_frame(30)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()

        result = provider.get_daily_ohlcv("SPY", start, end)

        assert vendor.ohlcv_calls == [("SPY", start, end)]  # close-only row treated as a miss
        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]

        from archimedes.models.asset_daily_bars import AssetDailyBar

        session = session_factory()
        try:
            rows = session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").all()
            assert all(r.open is not None for r in rows)  # row healed with the full bar
        finally:
            session.close()

    def test_cross_vendor_close_write_clears_the_old_vendors_ohlv(self, session_factory):
        """A close-only write by a DIFFERENT vendor must not leave the previous
        vendor's open/high/low/volume in place under the new vendor's
        ``source`` label (#1798).

        ``asset_daily_bars`` is unique on ``(symbol, trade_date)``, so the
        close-only writer lands ON the warm row rather than beside it. Assigning
        just ``close`` + ``source`` there produces a bar whose Close is one
        vendor's and whose OHLV is another's — and ``_read_cached_ohlcv``'s
        ``source`` filter cannot catch it, because the row now claims to BE the
        vendor being asked for. Clearing OHLV makes it the partial bar it
        actually is, which the existing partial-bar guard treats as a miss."""
        from archimedes.models.asset_daily_bars import AssetDailyBar
        from archimedes.services.market_data_provider import _write_cached_ohlcv, _write_cached_series

        frame = _ohlcv_frame(30)
        session = session_factory()
        try:
            _write_cached_ohlcv(session, "SPY", frame, "yfinance")
            session.commit()
        finally:
            session.close()

        other_closes = pd.Series(frame["Close"].to_numpy() + 500.0, index=frame.index, name="SPY")
        session = session_factory()
        try:
            _write_cached_series(session, "SPY", other_closes, "tiingo")
            session.commit()
        finally:
            session.close()

        session = session_factory()
        try:
            rows = session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").all()
        finally:
            session.close()

        assert len(rows) == 30  # updated in place, not duplicated (unique constraint)
        for row in rows:
            assert row.source == "tiingo"
            assert row.close in set(other_closes.to_numpy())
            assert (row.open, row.high, row.low, row.volume) == (None, None, None, None)

        # And the consequence that matters: the OHLCV read is now a MISS, so
        # the new vendor is asked for the full bars instead of the blend.
        tiingo_frame = _ohlcv_frame(30, start_price=600.0)
        vendor = _FakeOhlcvVendor({"SPY": tiingo_frame})
        provider = CachingMarketDataProvider(vendor, source_name="tiingo", session_factory=session_factory)
        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()

        result = provider.get_daily_ohlcv("SPY", start, end)

        assert vendor.ohlcv_calls == [("SPY", start, end)]
        assert result["Open"].tolist() == tiingo_frame["Open"].tolist()

    def test_same_vendor_close_write_leaves_the_bar_intact(self, session_factory):
        """Anti-vacuity for the guard above: the clearing is scoped to a vendor
        CHANGE. The routine same-vendor close refresh must NOT evict OHLV, or
        every universe sweep would cold-start the OHLCV cache."""
        from archimedes.models.asset_daily_bars import AssetDailyBar
        from archimedes.services.market_data_provider import _write_cached_ohlcv, _write_cached_series

        frame = _ohlcv_frame(30)
        session = session_factory()
        try:
            _write_cached_ohlcv(session, "SPY", frame, "yfinance")
            _write_cached_series(session, "SPY", frame["Close"], "yfinance")
            session.commit()
            rows = session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").all()
            assert len(rows) == 30
            assert all(r.open is not None and r.volume is not None for r in rows)
        finally:
            session.close()

    def test_vendor_error_propagates_uncaught(self, session_factory):
        """get_daily_ohlcv's contract is to RAISE on a genuinely unfetchable
        symbol (matching fetch_ohlcv's contract) — the caching wrapper must
        not swallow that into an empty/None result, since callers
        (fusion_market_data, portfolio_backtester) rely on the exception for
        their own fail-closed / per-symbol-skip handling."""
        vendor = _FakeOhlcvVendor({})  # every ticker raises ValueError
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        with pytest.raises(ValueError, match="no data for NOPE"):
            provider.get_daily_ohlcv("NOPE", "2024-01-01", "2024-02-01")


class TestYFinanceProviderOhlcvBoundary:
    def test_get_daily_ohlcv_delegates_to_analytics_engine_fetch_ohlcv(self):
        """YFinanceProvider.get_daily_ohlcv must be a pure passthrough to
        ``archimedes_analytics_engine.data.fetch_ohlcv`` — the exact
        function ``fusion_market_data``/``portfolio_backtester`` imported
        and called directly before this seam existed (#1282). Delegating
        (rather than re-implementing the yfinance fetch a second time) is
        what guarantees cold-cache output is unchanged from the pre-seam
        direct-fetch path."""
        import sys

        frame = pd.DataFrame({"Open": [1.0], "High": [1.1], "Low": [0.9], "Close": [1.0], "Volume": [100.0]})
        fake_data_module = MagicMock()
        fake_data_module.fetch_ohlcv.return_value = frame

        with patch.dict(
            sys.modules,
            {"archimedes_analytics_engine": MagicMock(), "archimedes_analytics_engine.data": fake_data_module},
        ):
            provider = YFinanceProvider()
            result = provider.get_daily_ohlcv("SPY", "2020-01-01", "2020-02-01")

        fake_data_module.fetch_ohlcv.assert_called_once_with("SPY", "2020-01-01", "2020-02-01")
        assert result is frame  # identity, not a copy — nothing reshapes it in between


# ─── YFinanceProvider — the default vendor's own boundary ──────────────


class TestYFinanceProviderBoundary:
    def test_get_daily_close_batch_import_error_returns_empty(self):
        import sys

        provider = YFinanceProvider()
        with patch.dict(sys.modules, {"yfinance": None}):
            assert provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo") == {}

    def test_get_intraday_quotes_batch_import_error_returns_empty(self):
        import sys

        provider = YFinanceProvider()
        with patch.dict(sys.modules, {"yfinance": None}):
            assert provider.get_intraday_quotes_batch({"sSPY": "SPY"}) == {}


class TestConcurrentPrimeRace:
    def test_losing_a_concurrent_write_race_still_returns_the_fetched_data(self, session_factory):
        """Two writers prime a cold cache; the loser's commit hits
        ``uq_asset_daily_bars_symbol_trade_date``. The fetch SUCCEEDED — the
        caller must still get its data, nothing may raise, and the winner's
        rows stand (equivalent vendor data; next read is warm).

        Deterministic re-creation of the select→commit race window: the WRITE
        session's ``commit`` is intercepted to first land the same
        (symbol, trade_date) rows through a rival session — exactly what a
        second Fargate task or the refresh loop does — before the real commit
        flushes this session's now-conflicting INSERTs."""
        from archimedes.models.asset_daily_bars import AssetDailyBar
        from archimedes.services.market_data_provider import _write_cached_series

        series = _series()
        vendor = _FakeVendor({"X": series})

        sessions_made: list = []
        state = {"raced": False}

        def racing_factory():
            session = session_factory()
            sessions_made.append(session)
            if len(sessions_made) == 2 and not state["raced"]:
                real_commit = session.commit

                def commit_with_rival():
                    if not state["raced"]:
                        state["raced"] = True
                        rival = session_factory()
                        _write_cached_series(rival, "X", series, "rival")
                        rival.commit()
                        rival.close()
                    real_commit()

                session.commit = commit_with_rival
            return session

        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=racing_factory)
        out = provider.get_daily_close_batch({"X": "X"}, period="1mo")

        assert state["raced"] is True, "the rival writer never ran — the race was not exercised"
        assert "X" in out and len(out["X"]) == len(series)  # fetched data survives the collision

        check = session_factory()
        try:
            rows = check.query(AssetDailyBar).filter_by(symbol="X").all()
        finally:
            check.close()
        assert len(rows) == len(series)  # winner's rows stand; no duplicates


# ─── The widened batch-quote contract (intraday design §2 item 0) ──────
#
# ``get_intraday_quotes_batch`` used to return ``dict[str, float]``. Two
# consumers cannot be honest on a bare float: the on-chain push staleness gate
# (``oracle_updater._validate_for_push``, which reads ``AssetPrice.timestamp``)
# and the paper-marks loop (which stores the upstream observation time and
# refuses to write a row from a stale bar). The signature is now
# ``dict[str, tuple[float, datetime]]`` — the same shape the single-ticker
# sibling has always returned.


class _FakeYFModule:
    """Stand-in for the ``yfinance`` module: ``download`` returns a caller-
    supplied frame. Installed via ``patch.dict(sys.modules, ...)`` so the
    provider's own lazy ``import yfinance as yf`` picks it up — no network."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls: list[str] = []

    def download(self, tickers, **kwargs):
        self.calls.append(tickers)
        return self.frame


def _intraday_index(n: int, *, end: str = "2026-08-30 20:00", tz: str | None = "UTC") -> pd.DatetimeIndex:
    return pd.date_range(end=pd.Timestamp(end, tz=tz), periods=n, freq="15min")


def _multi_close_frame(cols: dict[str, list[float]], index: pd.DatetimeIndex) -> pd.DataFrame:
    """A multi-ticker yfinance frame: ``data["Close"]`` is a DataFrame whose
    columns are vendor tickers (the shape ``yf.download`` returns for >1
    ticker)."""
    return pd.DataFrame({("Close", t): v for t, v in cols.items()}, index=index)


class TestIntradayBatchCarriesBarTimestamps:
    def test_batch_returns_price_and_utc_bar_timestamp_per_key(self):
        import sys

        idx = _intraday_index(3)
        frame = _multi_close_frame({"SPY": [510.0, 511.0, 512.5], "QQQ": [430.0, 431.0, 432.5]}, idx)
        fake = _FakeYFModule(frame)
        provider = YFinanceProvider()

        with patch.dict(sys.modules, {"yfinance": fake}):
            out = provider.get_intraday_quotes_batch({"sSPY": "SPY", "sQQQ": "QQQ"})

        assert set(out) == {"sSPY", "sQQQ"}
        for key, expected_price in (("sSPY", 512.5), ("sQQQ", 432.5)):
            price, bar_ts = out[key]
            assert price == pytest.approx(expected_price)
            assert isinstance(bar_ts, datetime)
            assert bar_ts.tzinfo is not None, "a bar timestamp with no tz is unusable to a staleness gate"
            assert bar_ts == idx[-1].to_pydatetime()

    def test_a_frozen_leg_keeps_its_own_older_bar_time_not_the_live_legs(self):
        """THE adversarial case for this widening, and the reason the bar time
        is read per symbol rather than once off the frame index.

        A mixed universe outside US market hours: the crypto leg keeps
        printing 15-minute bars while the equity leg's column is NaN across
        the tail. Both legs live in ONE frame, so ``data.index[-1]`` is the
        CRYPTO leg's time. Stamping that on the equity price is precisely
        "a stale price wearing a fresh timestamp" — the defect §2.4 rule 1
        exists to prevent, and it would sail past every staleness gate
        downstream.

        Demonstrated to reject: reverting the per-symbol
        ``close.dropna().index[-1]`` to the frame-level ``data.index[-1]``
        makes this test fail (both legs report the crypto bar time) while
        every other test in this class still passes.
        """
        import sys

        idx = _intraday_index(4)
        frame = _multi_close_frame(
            {
                "SPY": [510.0, 512.5, float("nan"), float("nan")],  # session closed two bars ago
                "BTC-USD": [61000.0, 61100.0, 61200.0, 61250.0],  # 24/7, still printing
            },
            idx,
        )
        fake = _FakeYFModule(frame)
        provider = YFinanceProvider()

        with patch.dict(sys.modules, {"yfinance": fake}):
            out = provider.get_intraday_quotes_batch({"sSPY": "SPY", "sBTC": "BTC-USD"})

        spy_price, spy_ts = out["sSPY"]
        btc_price, btc_ts = out["sBTC"]
        assert spy_price == pytest.approx(512.5)  # the last REAL equity print
        assert btc_price == pytest.approx(61250.0)
        assert btc_ts == idx[-1].to_pydatetime()
        assert spy_ts == idx[-3].to_pydatetime()
        assert spy_ts < btc_ts, "the frozen leg must not inherit the live leg's bar time"

    def test_single_ticker_path_also_carries_its_bar_time(self):
        import sys

        idx = _intraday_index(3)
        frame = pd.DataFrame({"Close": [510.0, 511.0, 512.5]}, index=idx)
        fake = _FakeYFModule(frame)
        provider = YFinanceProvider()

        with patch.dict(sys.modules, {"yfinance": fake}):
            out = provider.get_intraday_quotes_batch({"sSPY": "SPY"})

        assert out["sSPY"][0] == pytest.approx(512.5)
        assert out["sSPY"][1] == idx[-1].to_pydatetime()

    def test_a_naive_bar_index_is_localized_to_utc_not_left_naive(self):
        import sys

        idx = _intraday_index(3, tz=None)
        frame = pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=idx)
        provider = YFinanceProvider()

        with patch.dict(sys.modules, {"yfinance": _FakeYFModule(frame)}):
            _price, bar_ts = provider.get_intraday_quotes_batch({"sX": "X"})["sX"]

        assert bar_ts.tzinfo is not None
        assert bar_ts == idx[-1].tz_localize("UTC").to_pydatetime()

    def test_an_all_nan_column_is_omitted_rather_than_reported_with_a_wrong_time(self):
        """A symbol the vendor returned nothing usable for is ABSENT from the
        result (the long-standing contract), never present with a fabricated
        price or a borrowed timestamp."""
        import sys

        idx = _intraday_index(3)
        frame = _multi_close_frame(
            {"SPY": [510.0, 511.0, 512.5], "DEAD": [float("nan")] * 3},
            idx,
        )
        provider = YFinanceProvider()

        with patch.dict(sys.modules, {"yfinance": _FakeYFModule(frame)}):
            out = provider.get_intraday_quotes_batch({"sSPY": "SPY", "sDEAD": "DEAD"})

        assert set(out) == {"sSPY"}


class TestIntradayDelayedDeclaration:
    def test_yfinance_declares_its_intraday_feed_delayed(self, monkeypatch):
        from archimedes.services.market_data_provider import intraday_is_delayed

        monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
        assert intraday_is_delayed() is True

    def test_an_undeclared_provider_fails_toward_delayed(self, monkeypatch):
        """Fail-honest: an unknown vendor is assumed DELAYED. Claiming
        real-time for a feed nobody verified is the dishonest direction."""
        from archimedes.services import market_data_provider as mdp

        monkeypatch.setattr(mdp, "provider_name", lambda _seam: "some-unlisted-vendor")
        assert mdp.intraday_is_delayed() is True


# ─── #1632: the OHLCV cache-write mitigation ───────────────────────────


class TestOhlcvWriteChunking:
    """The OHLCV cache write must not hand psycopg2 one unbounded batch.

    What is PROVEN: the faulthandler traceback on #1632 shows a container
    dying with ``Fatal Python error: Aborted`` inside psycopg2's
    ``do_executemany``, on this exact commit, reached from the paper replay.
    What is NOT proven is the mechanism — see ``_OHLCV_CACHE_WRITE_LOCK``'s
    comment. These tests pin the mitigation's OBSERVABLE properties (batch
    size is bounded, the transaction is still all-or-nothing, failures fall
    through unchanged), which are true regardless of which hypothesis holds.
    """

    def test_writes_are_flushed_in_bounded_batches(self, session_factory, monkeypatch):
        """With the bound set to 10 and 25 rows to write, the writer must
        flush twice mid-loop (at 10 and 20) and leave the final partial batch
        of 5 to the caller's commit.

        MUTATION CHECK: with the chunking removed, ``flush`` is called zero
        times mid-loop and this fails on ``len(batch_sizes) == 2``.
        """
        from archimedes.services import market_data_provider as mdp

        monkeypatch.setattr(mdp, "_OHLCV_WRITE_CHUNK_ROWS", 10)

        frame = _ohlcv_frame(25)
        vendor = _FakeOhlcvVendor({"SPY": frame})

        batch_sizes: list[int] = []

        def spying_factory():
            session = session_factory()
            real_flush = session.flush

            def flush_with_spy(*args, **kwargs):
                # Size of the batch about to become one executemany. SQLAlchemy
                # also autoflushes on every query (including the writer's own
                # `existing` lookup), and those carry no pending work — count
                # only flushes that actually have rows to send.
                size = len(session.new) + len(session.dirty)
                if size:
                    batch_sizes.append(size)
                return real_flush(*args, **kwargs)

            session.flush = flush_with_spy
            return session

        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=spying_factory)
        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()

        result = provider.get_daily_ohlcv("SPY", start, end)

        # Two mid-loop chunks of 10, then commit flushes the remaining 5.
        assert batch_sizes == [10, 10, 5], f"expected batches of 10/10/5 at a bound of 10, saw {batch_sizes}"
        # THE load-bearing assertion, and the mutation-sensitive one: no single
        # batch may exceed the bound. Remove the chunking and this reads [25].
        assert all(size <= 10 for size in batch_sizes), f"a batch exceeded the bound: {batch_sizes}"
        # The data still lands in full — chunking changes batch size, not content.
        from archimedes.models.asset_daily_bars import AssetDailyBar

        session = session_factory()
        try:
            assert session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").count() == 25
        finally:
            session.close()
        # Anti-goal: the returned frame is still the vendor's, untouched.
        pd.testing.assert_frame_equal(result, frame)

    def test_a_frame_under_the_bound_needs_no_mid_loop_flush(self, session_factory, monkeypatch):
        """No behaviour change for the common case — the overwhelming majority
        of frames are one batch, exactly as before."""
        from archimedes.services import market_data_provider as mdp

        monkeypatch.setattr(mdp, "_OHLCV_WRITE_CHUNK_ROWS", 500)

        frame = _ohlcv_frame(30)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        flushes: list[int] = []

        def spying_factory():
            session = session_factory()
            real_flush = session.flush

            def flush_with_spy(*args, **kwargs):
                # Same autoflush filter as the test above.
                size = len(session.new) + len(session.dirty)
                if size:
                    flushes.append(size)
                return real_flush(*args, **kwargs)

            session.flush = flush_with_spy
            return session

        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=spying_factory)
        provider.get_daily_ohlcv("SPY", frame.index[0].date().isoformat(), frame.index[-1].date().isoformat())

        # Exactly one batch, carrying every row — i.e. commit's own flush and
        # nothing else. Byte-for-byte the pre-#1632 write shape.
        assert flushes == [30], f"a 30-row frame under a 500-row bound must be one batch, saw {flushes}"


class TestOhlcvWriteSerializationGuard:
    """The module-level lock around write+commit (#1632 mitigation).

    Honest framing, repeated here because a test name can be read as a claim:
    this lock is NOT known to fix the abort. It removes in-process write
    concurrency as a variable. These tests assert only that it is actually
    held over the write path and — the part that matters more — that it can
    never be left held.
    """

    def test_the_lock_is_held_across_the_write_and_commit(self, session_factory):
        """MUTATION CHECK: delete the ``with`` statement and ``locked()`` reads
        False here."""
        from archimedes.services import market_data_provider as mdp

        frame = _ohlcv_frame(5)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        observed: list[bool] = []

        real_write = mdp._write_cached_ohlcv

        def write_observing_lock(session, ticker, df, source):
            observed.append(mdp._OHLCV_CACHE_WRITE_LOCK.locked())
            return real_write(session, ticker, df, source)

        with patch.object(mdp, "_write_cached_ohlcv", write_observing_lock):
            provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
            provider.get_daily_ohlcv("SPY", frame.index[0].date().isoformat(), frame.index[-1].date().isoformat())

        assert observed == [True], "the cache write did not run under the serialization lock"

    def test_the_lock_is_released_after_a_failed_write(self, session_factory):
        """The one way this mitigation could be WORSE than the bug.

        A lock left held by an error path would wedge every subsequent OHLCV
        fetch in the process — a deadlocked fleet instead of a cycling one. So:
        force the write to fail, then prove the lock is free and the next call
        still works.
        """
        from archimedes.services import market_data_provider as mdp
        from sqlalchemy.exc import SQLAlchemyError

        frame = _ohlcv_frame(5)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()

        def boom(session, ticker, df, source):
            raise SQLAlchemyError("simulated cache-write failure")

        with patch.object(mdp, "_write_cached_ohlcv", boom):
            result = provider.get_daily_ohlcv("SPY", start, end)

        assert not mdp._OHLCV_CACHE_WRITE_LOCK.locked(), "the lock was left held after a failed write"
        # The fetch still succeeded and was served — unchanged fail-soft contract.
        pd.testing.assert_frame_equal(result, frame)
        # And the path is not wedged: a second call goes straight through.
        vendor.ohlcv_calls.clear()
        again = provider.get_daily_ohlcv("SPY", start, end)
        pd.testing.assert_frame_equal(again, frame)


class TestOhlcvWriteFailureFallthroughUnchanged:
    """Anti-goal enforcement: the mitigation must not change which exceptions
    are caught, nor which are allowed to propagate."""

    def test_an_integrity_error_mid_chunk_still_serves_the_fetch(self, session_factory, monkeypatch):
        """A chunked flush can now raise where only ``commit`` used to. It must
        land in the SAME ``except IntegrityError`` arm, roll back, and serve
        the fetched frame."""
        from archimedes.models.asset_daily_bars import AssetDailyBar
        from archimedes.services import market_data_provider as mdp
        from sqlalchemy.exc import IntegrityError

        monkeypatch.setattr(mdp, "_OHLCV_WRITE_CHUNK_ROWS", 10)

        frame = _ohlcv_frame(25)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        rolled_back: list[int] = []

        def failing_factory():
            session = session_factory()
            real_flush = session.flush
            real_rollback = session.rollback

            def flush_that_fails(*args, **kwargs):
                # Fail only on a REAL chunk flush — one carrying pending rows.
                # SQLAlchemy autoflushes on every query, including the cache
                # READ that happens before (and outside) the guarded write; a
                # stub that failed there would be testing the wrong seam.
                if len(session.new) + len(session.dirty):
                    raise IntegrityError("INSERT ...", {}, Exception("uq_asset_daily_bars_symbol_trade_date"))
                return real_flush(*args, **kwargs)

            def rollback_spy(*args, **kwargs):
                rolled_back.append(1)
                return real_rollback(*args, **kwargs)

            session.flush = flush_that_fails
            session.rollback = rollback_spy
            return session

        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=failing_factory)
        result = provider.get_daily_ohlcv("SPY", frame.index[0].date().isoformat(), frame.index[-1].date().isoformat())

        pd.testing.assert_frame_equal(result, frame)  # fetch still served
        assert rolled_back, "the mid-chunk IntegrityError did not reach the rollback arm"
        # All-or-nothing preserved: no partially-cached window was committed.
        session = session_factory()
        try:
            assert session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").count() == 0
        finally:
            session.close()

    def test_a_non_sqlalchemy_error_still_propagates(self, session_factory):
        """ANTI-GOAL: 'do not swallow new exception classes silently.'

        The lock is a ``with``, not an ``except``. A ``RuntimeError`` from the
        write path escaped before this change and must still escape — if the
        mitigation had widened the arms to ``except Exception``, this test is
        what catches it.
        """
        from archimedes.services import market_data_provider as mdp

        frame = _ohlcv_frame(5)
        vendor = _FakeOhlcvVendor({"SPY": frame})
        provider = CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)

        def boom(session, ticker, df, source):
            raise RuntimeError("not a DB error — must not be caught here")

        with patch.object(mdp, "_write_cached_ohlcv", boom), pytest.raises(RuntimeError):
            provider.get_daily_ohlcv("SPY", frame.index[0].date().isoformat(), frame.index[-1].date().isoformat())

        # ...and even on the propagating path the lock is not left held.
        assert not mdp._OHLCV_CACHE_WRITE_LOCK.locked()
