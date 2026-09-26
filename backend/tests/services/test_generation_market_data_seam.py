"""Hermetic guard tests: the #1218 generation-path market-data seam fix.

``fusion_market_data._fetch_one`` and ``portfolio_backtester._fetch_price_panel``
used to import ``archimedes_analytics_engine.data.fetch_ohlcv`` directly —
bypassing the #1282 cached provider seam
(``archimedes.services.market_data_provider.get_provider()``) that the other
backend call sites (``strategy_signal_evaluator``, ``oracle_updater``,
``asset_market_service``) already used. This file proves both GENERATION-path
call sites now route through that same seam: ``MARKET_DATA_PROVIDER``-swappable
and ``asset_daily_bars``-cache-backed, with no second direct-fetch path left.

Cold-cache-equivalence tests here are the anti-goal check: with the provider
mocked at its boundary, a cold cache must return the vendor's frame UNCHANGED
— no reshape/reindex/dtype coercion — so graded backtest numbers cannot
silently shift when this seam was wired in.

Mutation-check evidence (revert the routing fix -> the guard fails; restore
-> it passes) is recorded in the PR body, not here.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _ohlcv_frame(n: int = 30, start: str = "2024-01-02", start_price: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range(start, periods=n)
    close = pd.Series([start_price + i for i in range(n)], index=idx)
    return pd.DataFrame(
        {
            "Open": close.to_numpy() - 0.5,
            "High": close.to_numpy() + 1.0,
            "Low": close.to_numpy() - 1.0,
            "Close": close.to_numpy(),
            "Volume": [1_000_000.0] * n,
        },
        index=idx,
    )


@pytest.fixture()
def session_factory(tmp_path):
    """An isolated SQLite engine/session factory with asset_daily_bars
    created — independent of the app's module-level engine (same pattern as
    test_market_data_provider.py's fixture of the same name)."""
    from archimedes.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'generation_seam.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


# ─── Routing guard: both call sites must ask get_provider() for data ────
#
# The stubs below take ``*, seam`` because since #1798 ``get_provider`` is
# keyword-only on ``seam``; a call site that dropped it would TypeError here.
# WHICH seam each of these asks for is pinned in test_market_data_seams.py.


class TestFusionMarketDataRoutesThroughSeam:
    def test_fetch_one_calls_get_provider_get_daily_ohlcv(self, monkeypatch):
        """Guard: _fetch_one must fetch via
        market_data_provider.get_provider().get_daily_ohlcv, not a direct
        vendor call. Mutation check: reverting _fetch_one to
        ``from archimedes_analytics_engine.data import fetch_ohlcv`` makes
        this fail — the mock below is never invoked, and the real fetch has
        no network in this hermetic run."""
        import archimedes.services.fusion_market_data as fmd
        import archimedes.services.market_data_provider as mdp

        frame = _ohlcv_frame()
        fake_provider = MagicMock()
        fake_provider.get_daily_ohlcv.return_value = frame
        monkeypatch.setattr(mdp, "get_provider", lambda *, seam: fake_provider)

        result = fmd._fetch_one("SPY", "2024-01-02", "2024-02-10")

        fake_provider.get_daily_ohlcv.assert_called_once_with("SPY", "2024-01-02", "2024-02-10")
        pd.testing.assert_frame_equal(result, frame)


class TestPortfolioBacktesterRoutesThroughSeam:
    def test_fetch_price_panel_calls_get_provider_get_daily_ohlcv(self, monkeypatch):
        """Guard: _fetch_price_panel must fetch every symbol via
        market_data_provider.get_provider().get_daily_ohlcv. Mutation check:
        reverting to a direct archimedes_analytics_engine.data.fetch_ohlcv
        import makes this fail the same way as the fusion guard above."""
        import archimedes.services.market_data_provider as mdp
        from archimedes.services.portfolio_backtester import _fetch_price_panel

        frame = _ohlcv_frame(n=300)
        fake_provider = MagicMock()
        fake_provider.get_daily_ohlcv.return_value = frame
        monkeypatch.setattr(mdp, "get_provider", lambda *, seam: fake_provider)

        panel, volumes = _fetch_price_panel(["SPY", "TLT"], "2024-01-02", "2025-01-02")

        assert fake_provider.get_daily_ohlcv.call_count == 2
        fake_provider.get_daily_ohlcv.assert_any_call("SPY", "2024-01-02", "2025-01-02")
        fake_provider.get_daily_ohlcv.assert_any_call("TLT", "2024-01-02", "2025-01-02")
        assert list(panel.columns) == ["SPY", "TLT"]
        assert list(volumes.columns) == ["SPY", "TLT"]


# ─── No second direct-fetch path remains ────────────────────────────────


class TestNoSecondDirectFetchPathRemains:
    """Source-level guard, scoped to the specific fetch function (not the
    whole module, which legitimately still names ``archimedes_analytics_engine``
    in prose/other functions — e.g. ``_ensure_analytics_import``, which
    ``market_data_provider.YFinanceProvider`` now uses as the ONE place
    backend touches that package directly, itself reached only through
    ``get_provider()``). Mutation check: re-adding
    ``from archimedes_analytics_engine.data import fetch_ohlcv`` inside
    either function below makes the matching test fail."""

    def test_fusion_market_data_fetch_one_has_no_direct_analytics_engine_import(self):
        import archimedes.services.fusion_market_data as fmd

        source = inspect.getsource(fmd._fetch_one)
        assert "archimedes_analytics_engine" not in source
        assert "import fetch_ohlcv" not in source

    def test_portfolio_backtester_fetch_price_panel_has_no_direct_analytics_engine_import(self):
        from archimedes.services.portfolio_backtester import _fetch_price_panel

        source = inspect.getsource(_fetch_price_panel)
        assert "archimedes_analytics_engine" not in source
        assert "import fetch_ohlcv" not in source


# ─── Cache cold/warm behavior through the actual generation-path funcs ──


class TestCacheColdWarmThroughGenerationPath:
    """End-to-end through the real CachingMarketDataProvider (fake inner
    vendor, isolated SQLite session) rather than a mocked get_provider() —
    proves the whole seam (routing + cache) behaves correctly from the
    call sites' own entry points, not just that they call the right method."""

    def test_fusion_fetch_one_cold_then_warm(self, monkeypatch, session_factory):
        import archimedes.services.fusion_market_data as fmd
        import archimedes.services.market_data_provider as mdp

        frame = _ohlcv_frame(40)
        vendor = MagicMock()
        vendor.get_daily_ohlcv.return_value = frame
        cached_provider = mdp.CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        monkeypatch.setattr(mdp, "get_provider", lambda *, seam: cached_provider)

        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()

        cold = fmd._fetch_one("SPY", start, end)
        # Anti-goal: cache-cold output equals the vendor's raw frame exactly.
        pd.testing.assert_frame_equal(cold, frame)
        assert vendor.get_daily_ohlcv.call_count == 1

        warm = fmd._fetch_one("SPY", start, end)
        assert vendor.get_daily_ohlcv.call_count == 1  # second call served from the cache
        assert len(warm) == len(frame)
        assert list(warm.columns) == ["Open", "High", "Low", "Close", "Volume"]

    def test_portfolio_backtester_fetch_price_panel_cold_then_warm(self, monkeypatch, session_factory):
        import archimedes.services.market_data_provider as mdp
        from archimedes.services.portfolio_backtester import _fetch_price_panel

        frame = _ohlcv_frame(300)
        vendor = MagicMock()
        vendor.get_daily_ohlcv.return_value = frame
        cached_provider = mdp.CachingMarketDataProvider(vendor, source_name="yfinance", session_factory=session_factory)
        monkeypatch.setattr(mdp, "get_provider", lambda *, seam: cached_provider)

        start = frame.index[0].date().isoformat()
        end = frame.index[-1].date().isoformat()

        panel_cold, volumes_cold = _fetch_price_panel(["SPY"], start, end)
        assert vendor.get_daily_ohlcv.call_count == 1
        pd.testing.assert_series_equal(panel_cold["SPY"], frame["Close"], check_names=False, check_freq=False)
        pd.testing.assert_series_equal(volumes_cold["SPY"], frame["Volume"], check_names=False, check_freq=False)

        panel_warm, volumes_warm = _fetch_price_panel(["SPY"], start, end)
        assert vendor.get_daily_ohlcv.call_count == 1  # warm — vendor not hit again
        # Values + calendar dates must match; the warm path's index round-trips
        # through a SQLite DATE column and can legitimately land on a
        # different datetime64 time-unit (e.g. [us] vs [s]) than the cold
        # path's native pandas index — not a correctness issue, so compare
        # values and calendar dates rather than raw index dtype.
        assert (panel_warm["SPY"].to_numpy() == panel_cold["SPY"].to_numpy()).all()
        assert (volumes_warm["SPY"].to_numpy() == volumes_cold["SPY"].to_numpy()).all()
        assert list(panel_warm.index.date) == list(panel_cold.index.date)
