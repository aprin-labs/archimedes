"""Oracle crypto source cascade + named push exclusions (#1710).

Target: backend/archimedes/chain/oracle_updater.py

Two defects this pins, both reported by #1710's runner log:

1. **The crypto leg was off the vendor seam entirely.** It called CoinGecko
   directly with no fallback, so `docs/adr/market-data-sourcing.md`'s
   "reversible by build — one env var selects the vendor" property did not
   hold for the prices this runner actually pushes on-chain.
2. **A starved symbol vanished silently.** A symbol no source could price was
   simply absent from `fetch_prices()`'s result. `push_prices_on_chain` never
   saw it, so it logged no rejection (that path only fires for a price that
   EXISTS and fails a gate) and nothing else logged an exclusion either — the
   on-chain oracle aged past MAX_STALENESS with no log line naming the symbol,
   which is precisely why the ALARM had no grep-able cause.

Hermetic: the aiohttp CoinGecko boundary and the market-data provider seam are
both mocked. No network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.chain.oracle_updater import (
    CRYPTO_MAP,
    CRYPTO_VENDOR_TICKERS,
    OracleUpdater,
    _crypto_source_order,
)


@pytest.fixture
def updater() -> OracleUpdater:
    return OracleUpdater()


def _coingecko_session(*, status: int = 200, payload: dict | None = None, raises: Exception | None = None):
    """A mocked `aiohttp.ClientSession` context manager for the CoinGecko leg."""
    if raises is not None:
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(side_effect=raises)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        return session_cm

    resp = MagicMock(status=status)
    resp.json = AsyncMock(return_value=payload or {})
    get_cm = MagicMock()
    get_cm.__aenter__ = AsyncMock(return_value=resp)
    get_cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=get_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm


def _provider(quotes: dict[str, tuple[float, datetime]] | None = None, raises: Exception | None = None):
    """A fake MarketDataProvider for the seam leg."""
    fake = MagicMock()
    if raises is not None:
        fake.get_intraday_quotes_batch = MagicMock(side_effect=raises)
    else:
        fake.get_intraday_quotes_batch = MagicMock(return_value=quotes or {})
    return fake


class TestCryptoSourceOrder:
    def test_default_is_coingecko_primary_with_provider_fallback(self, monkeypatch):
        monkeypatch.delenv("ORACLE_CRYPTO_SOURCE", raising=False)
        assert _crypto_source_order() == ("coingecko", "provider")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("coingecko_only", ("coingecko",)),
            ("provider", ("provider", "coingecko")),
            ("provider_only", ("provider",)),
            ("  PROVIDER  ", ("provider", "coingecko")),  # case/space tolerant
        ],
    )
    def test_modes_parse(self, monkeypatch, value, expected):
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", value)
        assert _crypto_source_order() == expected

    def test_unknown_value_fails_safe_to_default(self, monkeypatch, caplog):
        """A config typo must not crash a funds-adjacent singleton runner."""
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", "tiingo-ish")
        with caplog.at_level("WARNING"):
            assert _crypto_source_order() == ("coingecko", "provider")
        assert "unknown ORACLE_CRYPTO_SOURCE" in caplog.text


class TestCryptoCascade:
    async def test_default_mode_happy_path_is_unchanged_coingecko(self, updater, monkeypatch):
        """Deploying #1710 is a no-op when CoinGecko answers: same price, same
        `source`, and the provider seam is never consulted."""
        monkeypatch.delenv("ORACLE_CRYPTO_SOURCE", raising=False)
        fake_provider = _provider({"sBTC": (99999.0, datetime.now(UTC))})
        with (
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(payload={"bitcoin": {"usd": 64000.0}}),
            ),
            patch("archimedes.services.market_data_provider.get_provider", return_value=fake_provider),
        ):
            results = await updater._fetch_crypto(datetime.now(UTC))

        assert {r.symbol: (r.price_usd, r.source) for r in results} == {"sBTC": (64000.0, "coingecko")}
        fake_provider.get_intraday_quotes_batch.assert_not_called()
        assert updater._source_miss_reasons == {}

    async def test_coingecko_miss_falls_through_to_the_provider_seam(self, updater, monkeypatch):
        """THE FIX. Old behavior: a CoinGecko failure returned [] and the symbol
        was dropped from the push cycle. New: the seam serves it, stamped with
        the TRUE vendor name (never "coingecko")."""
        monkeypatch.delenv("ORACLE_CRYPTO_SOURCE", raising=False)
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
        fake_provider = _provider({"sBTC": (63500.25, datetime.now(UTC))})
        with (
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(raises=RuntimeError("coingecko 429")),
            ),
            patch("archimedes.services.market_data_provider.get_provider", return_value=fake_provider),
        ):
            results = await updater._fetch_crypto(datetime.now(UTC))

        assert [(r.symbol, r.price_usd, r.source) for r in results] == [("sBTC", 63500.25, "yfinance")]
        fake_provider.get_intraday_quotes_batch.assert_called_once_with(CRYPTO_VENDOR_TICKERS)
        assert updater._source_miss_reasons == {}

    async def test_coingecko_only_never_touches_the_seam(self, updater, monkeypatch):
        """The escape hatch back to literal pre-#1710 behavior."""
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", "coingecko_only")
        fake_provider = _provider({"sBTC": (1.0, datetime.now(UTC))})
        with (
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(raises=RuntimeError("coingecko down")),
            ),
            patch("archimedes.services.market_data_provider.get_provider", return_value=fake_provider),
        ):
            results = await updater._fetch_crypto(datetime.now(UTC))

        assert results == []
        fake_provider.get_intraday_quotes_batch.assert_not_called()
        assert "sBTC" in updater._source_miss_reasons

    async def test_provider_mode_asks_the_seam_first(self, updater, monkeypatch):
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", "provider")
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
        fake_provider = _provider({"sBTC": (64100.0, datetime.now(UTC))})
        session = _coingecko_session(payload={"bitcoin": {"usd": 1.0}})
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session),
            patch("archimedes.services.market_data_provider.get_provider", return_value=fake_provider),
        ):
            results = await updater._fetch_crypto(datetime.now(UTC))

        assert [(r.symbol, r.price_usd, r.source) for r in results] == [("sBTC", 64100.0, "yfinance")]

    async def test_provider_that_cannot_serve_intraday_is_named_then_falls_back(self, updater, monkeypatch, caplog):
        """A seam vendor whose `get_intraday_quotes_batch` raises
        NotImplementedError must be reported BY NAME, not swallowed, and must
        not prevent CoinGecko from serving.

        Since #1798 this is the belt-and-braces path rather than the Tiingo
        one: the crypto leg asks the INTRADAY seam, `_VENDOR_SEAMS` does not
        list Tiingo there, and `MARKET_DATA_PROVIDER=tiingo` therefore resolves
        this seam to yfinance instead of breaking it (pinned in
        `tests/services/test_market_data_seams.py`). What is still reachable —
        and what this pins — is a vendor that declares the intraday seam and
        then refuses one of its methods."""
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", "provider")
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
        fake_provider = _provider(
            raises=NotImplementedError("TiingoProvider.get_intraday_quotes_batch is out of scope")
        )
        with (
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(payload={"bitcoin": {"usd": 64000.0}}),
            ),
            patch("archimedes.services.market_data_provider.get_provider", return_value=fake_provider),
            caplog.at_level("WARNING"),
        ):
            results = await updater._fetch_crypto(datetime.now(UTC))

        assert [(r.symbol, r.source) for r in results] == [("sBTC", "coingecko")]
        assert "yfinance" in caplog.text  # the seam's vendor, named
        assert "cannot serve intraday quotes" in caplog.text

    async def test_provider_only_with_tiingo_prices_nothing_and_never_fabricates(self, updater, monkeypatch):
        """Licensing-strict mode: a miss stays a miss. No CoinGecko fill-in, no
        invented price — the symbol is simply absent, with a named reason.

        Same #1798 note as the test above: the refusing vendor is now a
        hypothetical intraday-declaring one, not Tiingo, which the seam keeps
        away from this leg entirely."""
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", "provider_only")
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
        fake_provider = _provider(raises=NotImplementedError("daily bars only"))
        session = _coingecko_session(payload={"bitcoin": {"usd": 64000.0}})
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session),
            patch("archimedes.services.market_data_provider.get_provider", return_value=fake_provider),
        ):
            results = await updater._fetch_crypto(datetime.now(UTC))

        assert results == []
        session.__aenter__.assert_not_awaited()
        reason = updater._source_miss_reasons["sBTC"]
        assert "yfinance" in reason
        assert "does not implement intraday quotes" in reason

    async def test_every_source_exhausted_records_a_named_reason(self, updater, monkeypatch):
        monkeypatch.delenv("ORACLE_CRYPTO_SOURCE", raising=False)
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
        with (
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(status=503),
            ),
            patch("archimedes.services.market_data_provider.get_provider", return_value=_provider({})),
        ):
            results = await updater._fetch_crypto(datetime.now(UTC))

        assert results == []
        reason = updater._source_miss_reasons["sBTC"]
        assert "coingecko HTTP 503" in reason
        assert "returned no observation for BTC-USD" in reason
        assert "coingecko→provider" in reason


class TestNamedPushExclusions:
    """`fetch_prices` must name every push-set symbol it produced no price for.

    This is the grep the issue's acceptance criterion depends on ("no
    push-universe symbol whose source errored in the prior 24h").
    """

    @pytest.fixture(autouse=True)
    def _no_admin_pins(self, monkeypatch):
        """Admin price overrides are applied on top of every mode and would
        otherwise supply a symbol this test expects to be starved."""
        monkeypatch.delenv("ADMIN_PRICES_JSON", raising=False)

    async def test_starved_symbol_is_named_and_not_fabricated(self, updater, monkeypatch, caplog):
        monkeypatch.setenv("PRICE_SOURCE", "yfinance")
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", "coingecko_only")
        with (
            patch.object(updater, "_fetch_yfinance", return_value=[]),
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(raises=RuntimeError("coingecko down")),
            ),
            caplog.at_level("WARNING"),
        ):
            prices = await updater.fetch_prices()

        # Nothing invented to fill the gap.
        assert prices == []
        # Every push-set symbol is named as EXCLUDED, with a reason.
        for symbol in {"sSPY"} | set(CRYPTO_MAP):
            assert f"oracle push exclusion: {symbol} EXCLUDED" in caplog.text
        assert "No price fabricated or substituted" in caplog.text
        assert "coingecko down" in caplog.text

    async def test_no_exclusion_line_when_every_symbol_priced(self, updater, monkeypatch, caplog):
        monkeypatch.setenv("PRICE_SOURCE", "yfinance")
        monkeypatch.delenv("ORACLE_CRYPTO_SOURCE", raising=False)
        now = datetime.now(UTC)
        from archimedes.models.asset import AssetPrice

        with (
            patch.object(
                updater,
                "_fetch_yfinance",
                return_value=[AssetPrice(symbol="sSPY", price_usd=500.0, timestamp=now, source="yfinance")],
            ),
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(payload={"bitcoin": {"usd": 64000.0}}),
            ),
            caplog.at_level("WARNING"),
        ):
            prices = await updater.fetch_prices()

        assert {p.symbol for p in prices} == {"sSPY", "sBTC"}
        assert "oracle push exclusion" not in caplog.text

    async def test_reasons_do_not_leak_across_cycles(self, updater, monkeypatch, caplog):
        """A symbol that failed LAST cycle and succeeds this one must not carry
        a stale reason — the bookkeeping is per-cycle."""
        monkeypatch.setenv("PRICE_SOURCE", "yfinance")
        monkeypatch.setenv("ORACLE_CRYPTO_SOURCE", "coingecko_only")
        with (
            patch.object(updater, "_fetch_yfinance", return_value=[]),
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(raises=RuntimeError("transient")),
            ),
        ):
            await updater.fetch_prices()
        assert "sBTC" in updater._source_miss_reasons

        now = datetime.now(UTC)
        from archimedes.models.asset import AssetPrice

        with (
            patch.object(
                updater,
                "_fetch_yfinance",
                return_value=[AssetPrice(symbol="sSPY", price_usd=500.0, timestamp=now, source="yfinance")],
            ),
            patch(
                "archimedes.chain.oracle_updater.aiohttp.ClientSession",
                return_value=_coingecko_session(payload={"bitcoin": {"usd": 64000.0}}),
            ),
        ):
            await updater.fetch_prices()
        assert updater._source_miss_reasons == {}


class TestVendorTickerMapParity:
    def test_every_crypto_push_symbol_has_a_vendor_ticker(self):
        """A CRYPTO_MAP entry with no CRYPTO_VENDOR_TICKERS row can never reach
        the seam — the provider leg would report "no vendor ticker mapped" for
        it forever. Adding a crypto synth to the push set must add both."""
        assert set(CRYPTO_MAP) <= set(CRYPTO_VENDOR_TICKERS)

    def test_vendor_tickers_use_the_ssot_crypto_shape(self):
        """`TiingoProvider._classify_tiingo_ticker` routes to the crypto
        endpoint family by the `<BASE>-USD` shape, and that is also the shape
        `synthetic_universe.json` stores. A ticker in any other shape would be
        silently classified as an EQUITY and fetched from the wrong endpoint."""
        from archimedes.services.market_data_provider import _classify_tiingo_ticker

        for symbol, ticker in CRYPTO_VENDOR_TICKERS.items():
            assert _classify_tiingo_ticker(ticker) == "crypto", f"{symbol} → {ticker}"
