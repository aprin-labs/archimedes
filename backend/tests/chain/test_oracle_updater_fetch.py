"""OracleUpdater price-fetch + snapshot coverage (#738 Tier-A).

Target: backend/archimedes/chain/oracle_updater.py
Complements test_oracle_updater.py (which covers the sanity-bound / push-refusal
logic) by exercising the *fetch* surface: yfinance equity prices, CoinGecko
crypto prices, the market snapshot (VIX + S&P MAs), the cache, the Circle
public-key fetch, and the no-credentials push early-return.

Hermetic: yfinance is replaced with a fake module via sys.modules; the aiohttp
CoinGecko/Circle boundary is mocked. No network, no Arc RPC, no Circle.

``archimedes.services.market_data_provider`` is imported here at module level
(not just where it's used below) so it's already in ``sys.modules`` before any
test's ``patch.dict(sys.modules, {"yfinance": ...})`` runs. That context
manager restores ``sys.modules`` to its pre-``with`` snapshot on exit — a
module first imported (by the code under test, via a *lazy* import inside the
patched block) is wiped along with it, since it isn't in that snapshot. The
oracle-push / cross-check paths lazily import ``market_data_provider`` inside
``patch.dict(sys.modules, {"yfinance": ...})`` blocks below (#1218 seam); this
import guarantees a stable module identity for ``TestSp500MovingAverages``'s
own ``patch.object(mdp, "get_provider", ...)`` regardless of run order.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.chain.oracle_updater import OracleUpdater
from archimedes.models.asset import AssetPrice
from archimedes.services import market_data_provider as mdp  # see module docstring above


@pytest.fixture
def updater() -> OracleUpdater:
    return OracleUpdater()


_BAR_TS = datetime(2026, 8, 30, 19, 45, tzinfo=UTC)


def _fake_yfinance_multi(prices_by_ticker: dict[str, float], bar_ts: datetime = _BAR_TS):
    """A fake `yfinance` module whose download() returns a multi-ticker frame.

    The real code reads `data["Close"]`, indexes `.columns` per ticker, then
    takes `.dropna()` and reads BOTH `.iloc[-1]` (the price) and `.index[-1]`
    (the bar's upstream observation time). The index is therefore a real
    tz-aware DatetimeIndex, not the default RangeIndex — the bar time is part
    of the batch-quote contract now (intraday design §2 item 0), so a fake
    without one is not a faithful stand-in for the vendor.
    """
    import pandas as pd

    index = pd.DatetimeIndex([pd.Timestamp(bar_ts)])
    close = pd.DataFrame({t: [p] for t, p in prices_by_ticker.items()}, index=index)
    frame = MagicMock()
    frame.empty = False
    frame.__getitem__ = MagicMock(side_effect=lambda k: close if k == "Close" else None)
    fake = MagicMock()
    fake.download = MagicMock(return_value=frame)
    return fake


class TestFetchYfinance:
    def test_parses_multi_ticker_close(self, updater):
        fake_yf = _fake_yfinance_multi({"TSLA": 250.0, "SPY": 500.0})
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            results = updater._fetch_yfinance({"sTSLA": "TSLA", "sSPY": "SPY"}, datetime.now(UTC))
        by_symbol = {r.symbol: r.price_usd for r in results}
        assert by_symbol["sTSLA"] == 250.0
        assert by_symbol["sSPY"] == 500.0

    def test_stamps_the_poll_time_unchanged_by_the_widened_batch_seam(self, updater):
        """ON-CHAIN BEHAVIOR IS UNCHANGED by the paper-marks batch-seam widening.

        `get_intraday_quotes_batch` now returns `(price, bar_ts)` because the
        paper-marks loop cannot be honest without an upstream observation
        time. This leg unpacks that tuple and DISCARDS `bar_ts`: the
        `AssetPrice` still carries the poll time, so `_validate_for_push`'s
        `age_s` — and therefore which prices get pushed on-chain — is bit-for-
        bit what it was before this branch.

        This is a REGRESSION PIN, not an endorsement. Stamping `bar_ts` here
        is a real improvement (it makes the staleness gate able to reject a
        stale bar at all) AND it silently stops every off-hours equity push,
        because a Friday-close bar is hours older than the 900s cap. That
        trade is split out to `dbrowneup/oracle-bar-time-stamp` with its own
        off-hours test. A paper-trading PR must not change what gets written
        on-chain at the weekend, and this test is what makes that visible.

        Demonstrated to reject: changing `timestamp=timestamp` back to
        `timestamp=bar_ts or timestamp` in `_fetch_yfinance` makes this test
        fail (the stamp becomes `_BAR_TS`), while
        `test_parses_multi_ticker_close` above still passes — the prices stay
        right and only the *time* moves, which is exactly why the price test
        alone could never have caught it.
        """
        poll_time = datetime(2026, 8, 30, 23, 59, tzinfo=UTC)
        fake_yf = _fake_yfinance_multi({"SPY": 500.0})
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            results = updater._fetch_yfinance({"sSPY": "SPY"}, poll_time)
        assert results[0].timestamp == poll_time
        assert results[0].timestamp != _BAR_TS, "the bar time is available here and deliberately not used (yet)"

    def test_import_error_returns_empty(self, updater):
        # Force the `import yfinance as yf` inside to raise (no mock object needed —
        # mapping the module to None makes the import statement itself raise).
        with patch.dict(sys.modules, {"yfinance": None}):
            results = updater._fetch_yfinance({"sTSLA": "TSLA"}, datetime.now(UTC))
        assert results == []


class TestFetchPrices:
    async def test_combines_equity_and_crypto(self, updater):
        now = datetime.now(UTC)
        equities = [AssetPrice(symbol="sTSLA", price_usd=250.0, timestamp=now, source="yfinance")]
        crypto = [AssetPrice(symbol="sBTC", price_usd=65000.0, timestamp=now, source="coingecko")]
        with (
            patch.object(updater, "_fetch_yfinance", return_value=equities),
            patch.object(updater, "_fetch_crypto", AsyncMock(return_value=crypto)),
        ):
            prices = await updater.fetch_prices()
        symbols = {p.symbol for p in prices}
        assert {"sTSLA", "sBTC"} <= symbols
        # fetch_prices populates the per-instance cache.
        assert updater.get_cached_price("sTSLA").price_usd == 250.0
        assert updater.get_cached_price("sBTC").price_usd == 65000.0

    def test_get_cached_price_miss_returns_none(self, updater):
        assert updater.get_cached_price("sNOPE") is None


class TestFetchCrypto:
    async def test_parses_coingecko_response(self, updater):
        resp = MagicMock(status=200)
        resp.json = AsyncMock(return_value={"bitcoin": {"usd": 64000.0}})
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=resp)
        get_cm.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=get_cm)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm):
            results = await updater._fetch_crypto(datetime.now(UTC))
        by_symbol = {r.symbol: r.price_usd for r in results}
        assert by_symbol["sBTC"] == 64000.0

    async def test_coingecko_error_is_swallowed(self, updater, monkeypatch):
        """A raising session must not crash the cycle.

        Pinned to ``ORACLE_CRYPTO_SOURCE=coingecko_only`` (#1710) so this keeps
        asserting exactly what it always asserted — the CoinGecko leg alone,
        returning [] on failure. The DEFAULT mode now consults the provider
        seam after a CoinGecko miss, which is a different contract and is
        covered by ``test_oracle_crypto_source.py``; leaving this test on the
        default would silently turn it into a LIVE yfinance fetch (it has no
        provider double), which is both non-hermetic and no longer a test of
        this leg.
        """
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", "coingecko_only")
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("network down"))
        session_cm.__aexit__ = AsyncMock(return_value=False)
        with patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm):
            results = await updater._fetch_crypto(datetime.now(UTC))
        assert results == []
        # Not silent: the symbol carries a named reason for _log_push_exclusions.
        assert "network down" in updater._source_miss_reasons["sBTC"]


class TestFetchMarketSnapshot:
    async def test_assembles_prices_vix_and_mas(self, updater):
        now = datetime.now(UTC)
        equities = [AssetPrice(symbol="sSPY", price_usd=500.0, timestamp=now, source="yfinance")]
        with (
            patch.object(updater, "fetch_prices", AsyncMock(return_value=equities)),
            patch.object(updater, "_fetch_yfinance_single", AsyncMock(return_value=(14.5, now))),
            patch.object(updater, "_fetch_sp500_moving_averages", return_value={"ma50": 4900.0, "ma200": 4800.0}),
        ):
            snap = await updater.fetch_market_snapshot()
        assert snap.vix == 14.5
        assert snap.sp500_ma50 == 4900.0
        assert snap.sp500_ma200 == 4800.0
        assert snap.prices["sSPY"] == 500.0
        # VIX + MAs present → the snapshot reports it carries regime signals.
        assert snap.has_regime_signals is True

    async def test_vix_none_when_yfinance_single_fails(self, updater):
        # _fetch_yfinance_single returns a bare None on failure (not a 2-tuple) —
        # fetch_market_snapshot must unpack that shape safely.
        now = datetime.now(UTC)
        equities = [AssetPrice(symbol="sSPY", price_usd=500.0, timestamp=now, source="yfinance")]
        with (
            patch.object(updater, "fetch_prices", AsyncMock(return_value=equities)),
            patch.object(updater, "_fetch_yfinance_single", AsyncMock(return_value=None)),
            patch.object(updater, "_fetch_sp500_moving_averages", return_value={}),
        ):
            snap = await updater.fetch_market_snapshot()
        assert snap.vix is None


class TestFetchYfinanceSingle:
    async def test_returns_last_close_and_bar_timestamp(self, updater):
        import pandas as pd

        idx = pd.date_range("2026-06-30 00:00", periods=2, freq="min", tz="UTC")
        close = pd.DataFrame({"^VIX": [13.2, 14.0]}, index=idx)
        frame = MagicMock()
        frame.empty = False
        frame.__getitem__ = MagicMock(side_effect=lambda k: close if k == "Close" else None)
        frame.index = idx
        fake_yf = MagicMock()
        fake_yf.download = MagicMock(return_value=frame)
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            result = await updater._fetch_yfinance_single("^VIX")
        assert result is not None
        price, bar_ts = result
        assert price == 14.0
        assert bar_ts == idx[-1].to_pydatetime()
        assert bar_ts.tzinfo is not None

    async def test_naive_bar_timestamp_normalized_to_utc(self, updater):
        import pandas as pd

        idx = pd.date_range("2026-06-30 00:00", periods=2, freq="min")  # tz-naive
        close = pd.DataFrame({"^VIX": [13.2, 14.0]}, index=idx)
        frame = MagicMock()
        frame.empty = False
        frame.__getitem__ = MagicMock(side_effect=lambda k: close if k == "Close" else None)
        frame.index = idx
        fake_yf = MagicMock()
        fake_yf.download = MagicMock(return_value=frame)
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            result = await updater._fetch_yfinance_single("^VIX")
        assert result is not None
        _, bar_ts = result
        assert bar_ts.tzinfo is not None
        assert bar_ts.utcoffset().total_seconds() == 0

    async def test_swallows_error_returns_none(self, updater):
        fake_yf = MagicMock()
        fake_yf.download = MagicMock(side_effect=RuntimeError("yf boom"))
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            assert await updater._fetch_yfinance_single("^VIX") is None


class TestSp500MovingAverages:
    def test_computes_rolling_means(self, updater):
        # #1218 seam: the fetch now goes through get_provider().get_daily_close_batch
        # rather than yf.Ticker(...).history() directly — mock at that boundary
        # (market_data_provider is preloaded at this file's top, see the comment
        # there — patch.object needs its target module identity stable across
        # this file's patch.dict(sys.modules, {"yfinance": ...}) tests).
        import pandas as pd

        close = pd.Series(range(1, 301), index=pd.date_range("2024-01-01", periods=300), name="^GSPC")
        fake_provider = MagicMock()
        fake_provider.get_daily_close_batch = MagicMock(return_value={"^GSPC": close})
        with patch.object(mdp, "get_provider", return_value=fake_provider):
            mas = updater._fetch_sp500_moving_averages()
        # rolling(50)/(200) means over 1..300 → finite numbers, ma200 < ma50.
        assert mas["ma50"] > mas["ma200"] > 0

    def test_empty_series_returns_empty_dict(self, updater):
        import pandas as pd

        fake_provider = MagicMock()
        fake_provider.get_daily_close_batch = MagicMock(return_value={"^GSPC": pd.Series(dtype=float)})
        with patch.object(mdp, "get_provider", return_value=fake_provider):
            assert updater._fetch_sp500_moving_averages() == {}

    def test_empty_history_returns_empty_dict(self, updater):
        import pandas as pd

        ticker = MagicMock()
        ticker.history = MagicMock(return_value=pd.DataFrame())
        fake_yf = MagicMock()
        fake_yf.Ticker = MagicMock(return_value=ticker)
        with patch.dict(sys.modules, {"yfinance": fake_yf}):
            assert updater._fetch_sp500_moving_averages() == {}


class TestGetCirclePublicKey:
    async def test_fetches_and_caches(self, monkeypatch, updater):
        monkeypatch.setenv("CIRCLE_API_KEY", "key")
        upd = OracleUpdater()
        resp = MagicMock(status=200)
        resp.json = AsyncMock(return_value={"data": {"publicKey": "PEM"}})
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=resp)
        get_cm.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=get_cm)
        key = await upd._get_circle_public_key(session)
        assert key == "PEM"
        # Cached → second call doesn't re-fetch.
        session.get.reset_mock()
        assert await upd._get_circle_public_key(session) == "PEM"
        session.get.assert_not_called()

    async def test_non_200_returns_none(self, updater):
        resp = MagicMock(status=500)
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=resp)
        get_cm.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=get_cm)
        assert await updater._get_circle_public_key(session) is None


class TestPushNoCredentials:
    async def test_returns_none_without_creds(self, monkeypatch):
        for var in ("CIRCLE_API_KEY", "CIRCLE_ENTITY_SECRET", "WALLET_ID"):
            monkeypatch.delenv(var, raising=False)
        upd = OracleUpdater()
        price = AssetPrice(symbol="sTSLA", price_usd=100.0, timestamp=datetime.now(UTC), source="yfinance")
        # No creds → early return None, nothing submitted.
        assert await upd.push_prices_on_chain([price]) is None

    async def test_push_aborts_when_public_key_unavailable(self, monkeypatch):
        for var, val in (("CIRCLE_API_KEY", "k"), ("CIRCLE_ENTITY_SECRET", "ab" * 32), ("WALLET_ID", "w")):
            monkeypatch.setenv(var, val)
        upd = OracleUpdater()
        session = MagicMock()
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        price = AssetPrice(symbol="sTSLA", price_usd=100.0, timestamp=datetime.now(UTC), source="yfinance")
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch.object(upd, "_get_circle_public_key", AsyncMock(return_value=None)),
        ):
            assert await upd.push_prices_on_chain([price]) is None


class TestModuleConstants:
    def test_symbol_maps_present(self):
        from archimedes.chain.oracle_updater import CRYPTO_MAP, YFINANCE_MAP

        # sSPY is the one live synth with a yfinance ticker; sBTC is the one crypto.
        assert YFINANCE_MAP["sSPY"] == "SPY"
        assert CRYPTO_MAP["sBTC"] == "bitcoin"

    def test_circle_constants(self):
        from archimedes.chain.oracle_updater import CIRCLE_API_BASE, CIRCLE_BLOCKCHAIN

        assert CIRCLE_BLOCKCHAIN == "ARC-TESTNET"
        assert CIRCLE_API_BASE.startswith("https://")


# Synths dropped from the on-chain universe: sTSLA/sNVDA (single stocks, #725,
# compliance-flagged backtest-only) and sGOLD/sOIL/sNKY (#842 — sGOLD→sGLD/sXAU,
# sOIL/sNKY dropped). None are in universe.ON_CHAIN_SYNTHS, so none must be fetched.
RETIRED_SYNTHS = ("sTSLA", "sNVDA", "sGOLD", "sOIL", "sNKY")


class TestYfinanceMapPrunedToLiveUniverse:
    """Issue #943 — YFINANCE_MAP must not carry retired synths (wasted fetch + misleading log)."""

    def test_retired_synths_absent_from_yfinance_map(self):
        from archimedes.chain.oracle_updater import YFINANCE_MAP

        for symbol in RETIRED_SYNTHS:
            assert symbol not in YFINANCE_MAP, (
                f"{symbol} is retired from the on-chain universe and must not be in YFINANCE_MAP"
            )

    def test_every_synth_key_is_in_the_live_universe(self):
        """Every ``s``-prefixed YFINANCE_MAP key must be a live on-chain synth.

        The ``^``-prefixed keys (^GSPC, ^VIX) are index tickers used for regime
        signals, not synths, so they are exempt — and the fetch loop's leading-"s"
        filter already excludes them from the synth fetch. This is the parity guard
        that stops a future stale entry from creeping back in.
        """
        from archimedes import universe
        from archimedes.chain.oracle_updater import YFINANCE_MAP

        live = set(universe.ON_CHAIN_SYNTHS)
        synth_keys = {k for k in YFINANCE_MAP if k.startswith("s")}
        assert synth_keys, "expected at least one live synth in YFINANCE_MAP"
        stale = synth_keys - live
        assert not stale, f"YFINANCE_MAP carries synths absent from the live universe: {sorted(stale)}"

    async def test_fetch_prices_does_not_fetch_retired_symbols(self, updater):
        """Negative control: the yfinance fetch is handed only live-universe synths.

        ``fetch_prices`` builds ``equity_symbols`` from YFINANCE_MAP and passes it to
        ``_fetch_yfinance``. We spy on that boundary (no network) and assert none of the
        retired symbols — nor their old tickers — reach the fetch. Positive control: the
        one live synth (sSPY) IS passed through, proving the filter isn't just empty.
        """
        captured: dict[str, str] = {}

        def _spy_fetch(symbols, timestamp):
            captured.update(symbols)
            return []

        with (
            patch.object(updater, "_fetch_yfinance", side_effect=_spy_fetch),
            patch.object(updater, "_fetch_crypto", AsyncMock(return_value=[])),
        ):
            await updater.fetch_prices()

        # No retired synth key reaches the fetch...
        for symbol in RETIRED_SYNTHS:
            assert symbol not in captured
        # ...and neither do their old upstream tickers.
        for stale_ticker in ("GC=F", "CL=F", "^N225", "TSLA", "NVDA"):
            assert stale_ticker not in captured.values()
        # Positive control: the live synth is still fetched.
        assert captured.get("sSPY") == "SPY"
