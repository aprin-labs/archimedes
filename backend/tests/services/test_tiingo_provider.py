"""Hermetic tests for ``TiingoProvider`` (#1218 Part 1 — yfinance replacement).

Mocks httpx at the transport boundary (``httpx.MockTransport`` + client
injection) with realistic canned Tiingo JSON per endpoint family — no
network, no real API key required for any test except the explicit
missing-key cases, which assert the loud-failure contract itself.

Covers (per the issue spec): happy path per family (equity/crypto/FX),
symbol routing, unsupported-symbol loud failure, missing-key loud failure,
adjustment-semantics parity (fixture deliberately distinguishes adjusted
from raw), empty-response handling (loud, not empty-frame-as-success), and
output-shape parity with ``YFinanceProvider``'s contract.

Mutation-check evidence (both the unsupported-symbol guard and the
adjustment-field mapping) is recorded in the PR body, not here — per this
repo's convention (see ``test_generation_market_data_seam.py``'s header).
"""

from __future__ import annotations

import httpx
import pandas as pd
import pytest
from archimedes.services.market_data_provider import (
    TiingoAPIKeyMissingError,
    TiingoEmptyResponseError,
    TiingoProvider,
    TiingoProviderError,
    TiingoUnsupportedSymbolError,
    _classify_tiingo_ticker,
    get_provider,
)

# ─── Canned Tiingo fixtures ──────────────────────────────────────────────
#
# Equity adjClose/adjOpen/... are deliberately DIFFERENT from close/open/...
# (as if a split/dividend occurred between the raw print and today) so the
# adjustment-semantics test can distinguish "mapped from adjClose" from
# "mapped from close" — a mapping bug (adjClose -> close) changes the
# asserted values, it doesn't just leave them coincidentally equal.

EQUITY_FIXTURE = [
    {
        "date": "2024-01-02T00:00:00.000Z",
        "close": 472.65,
        "high": 473.67,
        "low": 470.49,
        "open": 472.16,
        "volume": 123456,
        "adjClose": 468.20,
        "adjHigh": 469.21,
        "adjLow": 466.05,
        "adjOpen": 467.72,
        "adjVolume": 123400,
        "divCash": 0.0,
        "splitFactor": 1.0,
    },
    {
        "date": "2024-01-03T00:00:00.000Z",
        "close": 468.79,
        "high": 470.16,
        "low": 467.53,
        "open": 468.35,
        "volume": 234567,
        "adjClose": 464.37,
        "adjHigh": 465.72,
        "adjLow": 463.11,
        "adjOpen": 463.93,
        "adjVolume": 234500,
        "divCash": 0.0,
        "splitFactor": 1.0,
    },
]

CRYPTO_FIXTURE = [
    {
        "ticker": "btcusd",
        "baseCurrency": "btc",
        "quoteCurrency": "usd",
        "priceData": [
            {
                "date": "2024-01-02T00:00:00+00:00",
                "open": 44200.5,
                "high": 45200.75,
                "low": 44000.0,
                "close": 45000.25,
                "volume": 12345.6,
                "volumeNotional": 555555555.5,
                "tradesDone": 10000,
            },
            {
                "date": "2024-01-03T00:00:00+00:00",
                "open": 45000.25,
                "high": 46000.0,
                "low": 44500.0,
                "close": 45700.1,
                "volume": 15000.2,
                "volumeNotional": 600000000.0,
                "tradesDone": 12000,
            },
        ],
    }
]

FX_FIXTURE = [
    {
        "ticker": "eurusd",
        "date": "2024-01-02T00:00:00+00:00",
        "open": 1.1050,
        "high": 1.1090,
        "low": 1.1020,
        "close": 1.1075,
    },
    {
        "ticker": "eurusd",
        "date": "2024-01-03T00:00:00+00:00",
        "open": 1.1075,
        "high": 1.1100,
        "low": 1.1040,
        "close": 1.1060,
    },
]


def _mock_client(routes: dict[str, object], *, capture: list[httpx.Request] | None = None) -> httpx.Client:
    """A hermetic httpx.Client wired to a MockTransport — the "transport
    boundary" mock this repo's testing conventions call for. ``routes`` maps
    an EXACT request path (``request.url.path``) to a JSON body returned
    with HTTP 200; anything unmatched is a 404 (so a routing bug shows up as
    a loud HTTP error, not a silently-wrong endpoint)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        if request.url.path in routes:
            return httpx.Response(200, json=routes[request.url.path], request=request)
        return httpx.Response(404, json={"detail": f"no mock route for {request.url.path}"}, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.tiingo.com")


@pytest.fixture(autouse=True)
def _tiingo_key(monkeypatch):
    """Every test gets a key by default; the missing-key tests explicitly
    delete it after construction (see TestMissingApiKey)."""
    monkeypatch.setenv("TIINGO_API_KEY", "test-key-do-not-log-me")


@pytest.fixture
def session_factory(tmp_path):
    """An isolated SQLite engine/session factory with ``asset_daily_bars``
    created — same construction as ``test_market_data_provider.py``'s
    fixture of the same name, independent of the app's module-level engine.
    Used only by the cold/warm cache round-trip in ``TestShapeParity``."""
    from archimedes.db import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'tiingo_market_data.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


# ─── Symbol classification (ticker-shape heuristic) ─────────────────────


class TestTickerClassification:
    def test_equity_ticker(self):
        assert _classify_tiingo_ticker("SPY") == "equity"

    def test_crypto_ticker(self):
        assert _classify_tiingo_ticker("BTC-USD") == "crypto"

    def test_fx_ticker(self):
        assert _classify_tiingo_ticker("EURUSD=X") == "fx"

    def test_index_ticker_is_unsupported(self):
        with pytest.raises(TiingoUnsupportedSymbolError, match=r"\^GSPC"):
            _classify_tiingo_ticker("^GSPC")

    def test_future_ticker_is_unsupported(self):
        with pytest.raises(TiingoUnsupportedSymbolError, match="GC=F"):
            _classify_tiingo_ticker("GC=F")

    @pytest.mark.parametrize("ticker", ["CL=F", "^N225", "^VIX", "SI=F", "HG=F", "PA=F", "PL=F"])
    def test_unsupported_shape_examples(self, ticker):
        """The task's illustrative unsupported set (CL=F, ^N225) plus the
        5 real metal_spot SSOT tickers — all must fail loud."""
        with pytest.raises(TiingoUnsupportedSymbolError):
            _classify_tiingo_ticker(ticker)


# ─── Happy path per endpoint family ──────────────────────────────────────


class TestHappyPathPerFamily:
    def test_equity_daily_ohlcv(self):
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        provider = TiingoProvider(client=client)

        result = provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")

        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert len(result) == 2
        assert isinstance(result.index, pd.DatetimeIndex)

    def test_crypto_daily_ohlcv(self):
        client = _mock_client({"/tiingo/crypto/prices": CRYPTO_FIXTURE})
        provider = TiingoProvider(client=client)

        result = provider.get_daily_ohlcv("BTC-USD", "2024-01-02", "2024-01-03")

        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert result["Close"].tolist() == [45000.25, 45700.1]
        assert result["Volume"].tolist() == [12345.6, 15000.2]

    def test_fx_daily_ohlcv(self):
        client = _mock_client({"/tiingo/fx/eurusd/prices": FX_FIXTURE})
        provider = TiingoProvider(client=client)

        result = provider.get_daily_ohlcv("EURUSD=X", "2024-01-02", "2024-01-03")

        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert result["Close"].tolist() == [1.1075, 1.1060]
        # FX has no vendor volume on either side (yfinance's EURUSD=X is also
        # all-zero) — documented deviation, not a bug.
        assert result["Volume"].tolist() == [0.0, 0.0]


# ─── Symbol routing: correct path + ticker translation per family ──────


class TestSymbolRouting:
    def test_equity_ticker_passed_through_unchanged(self):
        captured: list[httpx.Request] = []
        client = _mock_client({"/tiingo/daily/AGG/prices": EQUITY_FIXTURE}, capture=captured)
        TiingoProvider(client=client).get_daily_ohlcv("AGG", "2024-01-02", "2024-01-03")
        assert captured[0].url.path == "/tiingo/daily/AGG/prices"

    def test_crypto_ticker_translated_to_tiingo_shape(self):
        """BTC-USD (yfinance shape) -> tickers=btcusd (Tiingo shape) —
        hyphen stripped, lowercased."""
        captured: list[httpx.Request] = []
        client = _mock_client({"/tiingo/crypto/prices": CRYPTO_FIXTURE}, capture=captured)
        TiingoProvider(client=client).get_daily_ohlcv("BTC-USD", "2024-01-02", "2024-01-03")
        assert captured[0].url.path == "/tiingo/crypto/prices"
        assert dict(captured[0].url.params)["tickers"] == "btcusd"

    def test_fx_ticker_translated_to_tiingo_shape(self):
        """EURUSD=X (yfinance shape) -> /tiingo/fx/eurusd/prices (Tiingo
        shape) — =X suffix stripped, lowercased."""
        captured: list[httpx.Request] = []
        client = _mock_client({"/tiingo/fx/eurusd/prices": FX_FIXTURE}, capture=captured)
        TiingoProvider(client=client).get_daily_ohlcv("EURUSD=X", "2024-01-02", "2024-01-03")
        assert captured[0].url.path == "/tiingo/fx/eurusd/prices"

    def test_authorization_via_header_not_query_param(self):
        """The API key must never appear in the URL (query-string params
        routinely end up in access logs / debug traces) — only in the
        Authorization header."""
        captured: list[httpx.Request] = []
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE}, capture=captured)
        TiingoProvider(client=client).get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert captured[0].headers.get("authorization") == "Token test-key-do-not-log-me"
        assert "test-key-do-not-log-me" not in str(captured[0].url)


# ─── Unsupported-symbol loud failure ──────────────────────────────────────


class TestUnsupportedSymbolLoudFailure:
    def test_get_daily_ohlcv_raises_for_index_ticker(self):
        client = _mock_client({})  # no route registered — a real HTTP call would 404
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoUnsupportedSymbolError, match=r"\^GSPC"):
            provider.get_daily_ohlcv("^GSPC", "2024-01-02", "2024-01-03")

    def test_get_daily_ohlcv_raises_for_futures_ticker(self):
        client = _mock_client({})
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoUnsupportedSymbolError, match="GC=F"):
            provider.get_daily_ohlcv("GC=F", "2024-01-02", "2024-01-03")

    def test_unsupported_symbol_never_hits_the_network(self):
        """The guard must fire BEFORE any HTTP call — an unsupported ticker
        that somehow reached the network would 404, not raise
        TiingoUnsupportedSymbolError, which would defeat the point of a
        typed, nameable failure."""
        captured: list[httpx.Request] = []
        client = _mock_client({}, capture=captured)
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoUnsupportedSymbolError):
            provider.get_daily_ohlcv("CL=F", "2024-01-02", "2024-01-03")
        assert captured == []

    def test_batch_skips_unsupported_symbol_but_keeps_others_and_logs_loud(self, caplog):
        """get_daily_close_batch's contract (inherited from the ABC, matched
        by YFinanceProvider) is per-item skip, not whole-batch failure — but
        the skip must be LOUD in the log (full symbol named) and must never
        silently substitute yfinance data (it doesn't call yfinance at all)."""
        import logging

        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        provider = TiingoProvider(client=client)
        with caplog.at_level(logging.ERROR):
            result = provider.get_daily_close_batch({"sXAU": "GC=F", "sSPY": "SPY"}, period="1mo")
        assert "sXAU" not in result
        assert "sSPY" in result
        assert any("GC=F" in rec.message for rec in caplog.records)


# ─── Missing API key: loud failure, never in the message ────────────────


class TestMissingApiKey:
    def test_construction_without_key_raises(self, monkeypatch):
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        with pytest.raises(TiingoAPIKeyMissingError):
            TiingoProvider()

    def test_key_removed_after_construction_still_fails_at_call_time(self, monkeypatch):
        """The key is read fresh on every call, never cached on the
        instance — deleting it after construction must still fail the next
        call (proves 'read at call time', not just at construction)."""
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        provider = TiingoProvider(client=client)  # key present here
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        with pytest.raises(TiingoAPIKeyMissingError):
            provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")

    def test_missing_key_error_never_contains_a_key_value(self, monkeypatch):
        monkeypatch.setenv("TIINGO_API_KEY", "super-secret-value-12345")
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        provider = TiingoProvider(client=client)
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        with pytest.raises(TiingoAPIKeyMissingError) as excinfo:
            provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert "super-secret-value-12345" not in str(excinfo.value)

    def test_batch_propagates_missing_key_loudly_not_as_empty_result(self, monkeypatch):
        """A missing key must NOT be swallowed by the batch's per-ticker
        skip-and-log path (that would silently degrade
        MARKET_DATA_PROVIDER=tiingo-with-no-key into 'every symbol empty'
        instead of a loud, diagnosable failure)."""
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        provider = TiingoProvider(client=client)
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        with pytest.raises(TiingoAPIKeyMissingError):
            provider.get_daily_close_batch({"sSPY": "SPY"}, period="1mo")


# ─── Adjustment-semantics parity (the load-bearing correctness test) ────


class TestAdjustmentSemantics:
    def test_equity_close_is_mapped_from_adjclose_not_close(self):
        """Matches yfinance's auto_adjust=True contract (see
        market_data_provider.py:173/275 and
        archimedes_analytics_engine/market_data.py:63): the returned Close
        must be Tiingo's adjClose, NEVER the raw close. If the mapping were
        flipped (adjClose -> close), these values would be
        [472.65, 468.79] instead — a real, silent split/dividend
        discontinuity re-introduced into every backtest."""
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        provider = TiingoProvider(client=client)

        result = provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")

        assert result["Close"].tolist() == [468.20, 464.37]
        assert result["Open"].tolist() == [467.72, 463.93]
        assert result["High"].tolist() == [469.21, 465.72]
        assert result["Low"].tolist() == [466.05, 463.11]
        assert result["Volume"].tolist() == [123400.0, 234500.0]
        # The raw (unadjusted) values must NOT appear anywhere in the output.
        assert 472.65 not in result["Close"].tolist()
        assert 468.79 not in result["Close"].tolist()

    def test_crypto_has_no_adjustment_distinction(self):
        """Crypto carries only one field set (no corporate actions apply);
        Close must equal the raw `close` field directly."""
        client = _mock_client({"/tiingo/crypto/prices": CRYPTO_FIXTURE})
        provider = TiingoProvider(client=client)
        result = provider.get_daily_ohlcv("BTC-USD", "2024-01-02", "2024-01-03")
        assert result["Close"].tolist() == [45000.25, 45700.1]

    def test_fx_has_no_adjustment_distinction(self):
        client = _mock_client({"/tiingo/fx/eurusd/prices": FX_FIXTURE})
        provider = TiingoProvider(client=client)
        result = provider.get_daily_ohlcv("EURUSD=X", "2024-01-02", "2024-01-03")
        assert result["Close"].tolist() == [1.1075, 1.1060]


# ─── Empty-response handling: loud, not empty-frame-as-success ─────────


class TestEmptyResponseIsLoud:
    def test_equity_empty_list_raises(self):
        """Also the "never returns an empty DataFrame" regression guard:
        ``pytest.raises`` fails the test if the call returns instead of
        raising, so a regression to an empty-but-truthy-shaped sentinel is
        caught right here. (A separate try/except-shaped test asserting the
        same thing was removed as a duplicate — it exercised the identical
        route, call, and exception type.)"""
        client = _mock_client({"/tiingo/daily/NOPE/prices": []})
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoEmptyResponseError, match="NOPE"):
            provider.get_daily_ohlcv("NOPE", "2024-01-02", "2024-01-03")

    def test_crypto_empty_pricedata_raises(self):
        client = _mock_client(
            {
                "/tiingo/crypto/prices": [
                    {"ticker": "nopeusd", "baseCurrency": "nope", "quoteCurrency": "usd", "priceData": []}
                ]
            }
        )
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoEmptyResponseError):
            provider.get_daily_ohlcv("NOPE-USD", "2024-01-02", "2024-01-03")

    def test_crypto_empty_top_level_list_raises(self):
        client = _mock_client({"/tiingo/crypto/prices": []})
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoEmptyResponseError):
            provider.get_daily_ohlcv("NOPE-USD", "2024-01-02", "2024-01-03")

    def test_fx_empty_list_raises(self):
        client = _mock_client({"/tiingo/fx/nopeusd/prices": []})
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoEmptyResponseError):
            provider.get_daily_ohlcv("NOPEUSD=X", "2024-01-02", "2024-01-03")

    def test_batch_skips_empty_response_symbol_and_logs(self, caplog):
        import logging

        client = _mock_client({"/tiingo/daily/NOPE/prices": [], "/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        provider = TiingoProvider(client=client)
        with caplog.at_level(logging.ERROR):
            result = provider.get_daily_close_batch({"sNope": "NOPE", "sSPY": "SPY"}, period="1mo")
        assert "sNope" not in result
        assert "sSPY" in result


# ─── HTTP error surface ──────────────────────────────────────────────────


class TestHttpErrorSurface:
    def test_http_500_raises_tiingo_provider_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "server error"}, request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.tiingo.com")
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoProviderError, match="500"):
            provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")

    def test_network_error_raises_tiingo_provider_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.tiingo.com")
        provider = TiingoProvider(client=client)
        with pytest.raises(TiingoProviderError):
            provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")


# ─── Output-shape parity with YFinanceProvider's contract ───────────────


def _assert_ohlcv_shape_contract(frame: pd.DataFrame) -> None:
    """The shape ``TiingoProvider.get_daily_ohlcv`` output must satisfy.

    **Scope of this helper, stated precisely because an earlier revision
    overclaimed it.** This is a LOCAL contract for this file — it is NOT
    shared with, imported by, or applied to ``YFinanceProvider`` anywhere.
    ``test_market_data_provider.py``'s ``_ohlcv_frame`` is a different
    fixture that is deliberately not reused here: it builds a **tz-AWARE**
    (UTC) index (``pd.date_range(end=pd.Timestamp.now("UTC")...)``), so it
    would FAIL the tz-naive assertion below. Reusing it would mean either a
    false parity claim or silently weakening the contract.

    The tz-naive requirement is not arbitrary and not a guess about what
    yfinance returns over the network (which these hermetic tests cannot
    observe). It comes from production code on ``main``:
    ``_read_cached_ohlcv`` rebuilds a warm-cache frame as
    ``pd.to_datetime([r.trade_date ...])`` over ``date`` objects — always
    tz-naive. So a tz-aware vendor frame would make the SAME
    ``CachingMarketDataProvider.get_daily_ohlcv(...)`` call return a
    tz-aware index cold and a tz-naive one warm. That invariant is pinned
    directly by ``test_cold_and_warm_cache_frames_agree_on_shape`` below,
    against the real cache, not against a hand-built reference frame.
    """
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert isinstance(frame.index, pd.DatetimeIndex)
    assert frame.index.tz is None
    for col in frame.columns:
        assert frame[col].dtype == "float64", f"{col} dtype is {frame[col].dtype}, expected float64"


class TestShapeParity:
    """``TiingoProvider``'s own frames satisfy the ``get_daily_ohlcv`` shape
    contract for all three endpoint families, and — the part that is
    genuinely cross-component rather than self-referential — a Tiingo frame
    round-trips through the real ``CachingMarketDataProvider`` without
    changing shape.

    Deliberately NOT claimed here: byte-compatibility with
    ``YFinanceProvider``'s live output. That would need a real yfinance
    response, which these hermetic tests do not have; mocking ``yf.download``
    would only assert the tz of our own fixture back to us. See the
    cutover-follow-up list in the PR body — a recorded-response parity check
    against both vendors is the honest way to close that gap, and it is not
    in this PR."""

    def test_tiingo_equity_output_satisfies_the_contract(self):
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        result = TiingoProvider(client=client).get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        _assert_ohlcv_shape_contract(result)

    def test_tiingo_crypto_output_satisfies_the_contract(self):
        client = _mock_client({"/tiingo/crypto/prices": CRYPTO_FIXTURE})
        result = TiingoProvider(client=client).get_daily_ohlcv("BTC-USD", "2024-01-02", "2024-01-03")
        _assert_ohlcv_shape_contract(result)

    def test_tiingo_fx_output_satisfies_the_contract(self):
        client = _mock_client({"/tiingo/fx/eurusd/prices": FX_FIXTURE})
        result = TiingoProvider(client=client).get_daily_ohlcv("EURUSD=X", "2024-01-02", "2024-01-03")
        _assert_ohlcv_shape_contract(result)

    def test_get_daily_close_batch_returns_named_float_series(self):
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        result = TiingoProvider(client=client).get_daily_close_batch({"sSPY": "SPY"}, period="1mo")
        assert set(result) == {"sSPY"}
        series = result["sSPY"]
        assert series.name == "sSPY"
        assert series.dtype == "float64"
        assert isinstance(series.index, pd.DatetimeIndex)

    def test_cold_and_warm_cache_frames_agree_on_shape(self, session_factory):
        """The real cross-component check: the SAME
        ``CachingMarketDataProvider.get_daily_ohlcv`` call must return the
        same-shaped frame on a cold cache (Tiingo vendor fetch) and on a warm
        one (``_read_cached_ohlcv`` rebuilding it from ``asset_daily_bars``).

        This is where the tz-naive requirement in
        ``_assert_ohlcv_shape_contract`` actually comes from: the warm path
        is production code on ``main`` and builds its index from ``date``
        objects, so it is unconditionally tz-naive. A tz-aware Tiingo frame
        would pass every Tiingo-only test above and still make this call
        return two differently-typed indexes depending on cache state —
        exactly the kind of state-dependent shape drift a downstream
        backtrader feed would hit intermittently.
        """
        from archimedes.services.market_data_provider import CachingMarketDataProvider

        seen: list[httpx.Request] = []
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE}, capture=seen)
        provider = CachingMarketDataProvider(
            TiingoProvider(client=client), source_name="tiingo", session_factory=session_factory
        )

        cold = provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert len(seen) == 1, "cold call must reach the vendor"
        warm = provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert len(seen) == 1, "warm call must be served by asset_daily_bars, not a second vendor fetch"

        _assert_ohlcv_shape_contract(cold)
        _assert_ohlcv_shape_contract(warm)
        assert list(cold.columns) == list(warm.columns)
        assert cold.index.tz == warm.index.tz
        # ...and the adjusted values survived the asset_daily_bars round-trip
        # intact (same adjClose-derived numbers TestAdjustmentSemantics pins).
        assert warm["Close"].tolist() == cold["Close"].tolist() == [468.20, 464.37]
        assert warm.index.tolist() == cold.index.tolist()


# ─── Known divergences from the ABC contract, pinned rather than described ──


class TestKnownContractDivergences:
    """Behaviours where ``TiingoProvider`` does NOT yet match the contract
    ``MarketDataProvider`` documents. Pinned as tests so they are greppable
    and so closing one is a visible red-to-green diff, rather than living
    only as prose in a PR body that nobody re-reads. Each is on the PR's
    cutover-follow-up list and must be closed before
    ``MARKET_DATA_PROVIDER=tiingo`` is flipped in prod."""

    def test_end_date_is_inclusive_unlike_the_abc_s_half_open_range(self):
        """DIVERGENCE (follow-up): the ABC documents ``get_daily_ohlcv`` over
        ``[start, end)`` — half-open, end EXCLUSIVE — because that is what
        ``yf.download(start=, end=)`` does. Tiingo's ``endDate`` query
        parameter is INCLUSIVE, and ``_fetch_equity_rows`` passes ``end``
        straight through, so Tiingo returns one extra trailing bar for the
        same arguments.

        Concretely: for ``end="2024-01-03"`` the yfinance path yields only
        the 2024-01-02 bar; Tiingo yields 2024-01-02 AND 2024-01-03. Left
        as-is in this PR (the flag is off by default and no call site is on
        Tiingo yet); the fix is to send ``endDate = end - 1 day``, which
        needs its own test for the ``end=""`` and month/year-boundary cases.
        """
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        result = TiingoProvider(client=client).get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")

        assert result.index[-1] == pd.Timestamp("2024-01-03"), (
            "if this now fails, endDate was made exclusive — good; update the "
            "ABC-parity note and drop this test from the follow-up list"
        )

    def test_crypto_shape_error_is_reported_as_an_empty_response(self):
        """DIVERGENCE (follow-up): equity and FX raise
        ``TiingoProviderError('Unexpected ... response shape')`` when Tiingo
        returns a non-list body, but ``_fetch_crypto_rows`` folds that case
        into ``return []``, which surfaces downstream as
        ``TiingoEmptyResponseError`` — "zero rows for BTC-USD" instead of
        "Tiingo changed its crypto response shape". Same loudness, wrong
        cause: an operator reading the log would go looking for a delisting
        rather than a vendor API change."""
        client = _mock_client({"/tiingo/crypto/prices": {"detail": "not a list"}})
        provider = TiingoProvider(client=client)

        with pytest.raises(TiingoEmptyResponseError):  # today; should be TiingoProviderError
            provider.get_daily_ohlcv("BTC-USD", "2024-01-02", "2024-01-03")


# ─── Out-of-scope ABC methods: loud NotImplementedError, not silent wrong data ──


class TestOutOfScopeMethodsFailLoud:
    def test_get_intraday_quote_raises_not_implemented(self):
        client = _mock_client({})
        provider = TiingoProvider(client=client)
        with pytest.raises(NotImplementedError):
            provider.get_intraday_quote("SPY")

    def test_get_intraday_quotes_batch_raises_not_implemented(self):
        client = _mock_client({})
        provider = TiingoProvider(client=client)
        with pytest.raises(NotImplementedError):
            provider.get_intraday_quotes_batch({"sSPY": "SPY"})

    def test_get_series_raises_not_implemented(self):
        client = _mock_client({})
        provider = TiingoProvider(client=client)
        with pytest.raises(NotImplementedError):
            provider.get_series("SPY", "1y", "1d")


# ─── Provider-selection wiring (MARKET_DATA_PROVIDER=tiingo) ────────────


class TestProviderSelectionWiring:
    def test_market_data_provider_tiingo_constructs_caching_tiingo_provider(self, monkeypatch):
        from archimedes.services.market_data_provider import CachingMarketDataProvider

        monkeypatch.setenv("MARKET_DATA_PROVIDER", "tiingo")
        # Since #1798 this variable selects the DAILY seam's vendor when
        # MARKET_DATA_DAILY_PROVIDER is unset (back-compat), and never the
        # intraday seam's — see test_market_data_seams.py.
        monkeypatch.delenv("MARKET_DATA_DAILY_PROVIDER", raising=False)
        provider = get_provider(seam="daily")
        assert isinstance(provider._inner, CachingMarketDataProvider)
        assert isinstance(provider._inner._inner, TiingoProvider)
        assert provider._inner._source_name == "tiingo"

    def test_market_data_provider_tiingo_without_key_fails_loud(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "tiingo")
        monkeypatch.delenv("MARKET_DATA_DAILY_PROVIDER", raising=False)
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        with pytest.raises(TiingoAPIKeyMissingError):
            get_provider(seam="daily")


# ─── Free-tier politeness: pacing + honest rate-limit surfacing (#1218) ──


class TestRequestPacing:
    """The pacer is tested against an INJECTED clock/sleep, never real time.

    Two reasons this is the right boundary rather than a shortcut: a
    wall-clock test of a 1.1 s floor costs 1.1 s of suite time per assertion
    and is flaky under load, and — more importantly — asserting on the
    *value handed to sleep()* is a stronger claim than observing that some
    time passed. ``_tiingo_min_request_interval_s`` defaults to 0 under
    ``TESTING`` so the rest of the hermetic suite never sleeps; these tests
    set the interval explicitly, so nothing here depends on that default.
    """

    @staticmethod
    def _pacer_with_fake_time():
        """Returns ``(pacer, slept)`` over a clock that only advances when
        the pacer itself sleeps — so elapsed time is exactly what the pacer
        asked for, with no wall-clock contribution."""
        now = {"t": 1000.0}
        slept: list[float] = []

        def clock():
            return now["t"]

        def sleep(seconds):
            slept.append(seconds)
            now["t"] += seconds

        from archimedes.services.market_data_provider import _RequestPacer

        return _RequestPacer(clock=clock, sleep=sleep), slept

    def test_first_request_is_never_delayed(self):
        pacer, slept = self._pacer_with_fake_time()
        assert pacer.wait(1.1) == 0.0
        assert slept == [], "a cold pacer must not delay the first request"

    def test_second_immediate_request_waits_the_full_interval(self):
        pacer, slept = self._pacer_with_fake_time()
        pacer.wait(1.1)
        waited = pacer.wait(1.1)
        assert waited == pytest.approx(1.1)
        assert slept == [pytest.approx(1.1)]

    def test_a_request_after_the_interval_has_elapsed_is_not_delayed(self):
        """Pacing is a floor, not a fixed tax: work that already took longer
        than the interval must not be charged again for it."""
        now = {"t": 0.0}
        slept: list[float] = []
        from archimedes.services.market_data_provider import _RequestPacer

        pacer = _RequestPacer(clock=lambda: now["t"], sleep=lambda s: slept.append(s))
        pacer.wait(1.1)
        now["t"] += 5.0  # caller spent 5s doing something else
        assert pacer.wait(1.1) == 0.0
        assert slept == []

    def test_zero_interval_disables_pacing_entirely(self):
        pacer, slept = self._pacer_with_fake_time()
        pacer.wait(0.0)
        pacer.wait(0.0)
        pacer.wait(0.0)
        assert slept == []

    def test_interval_env_override_is_honoured(self, monkeypatch):
        from archimedes.services.market_data_provider import _tiingo_min_request_interval_s

        monkeypatch.setenv("TIINGO_MIN_REQUEST_INTERVAL_S", "2.5")
        assert _tiingo_min_request_interval_s() == 2.5

    @pytest.mark.parametrize("bad", ["abc", "-1", ""])
    def test_unparseable_or_negative_interval_falls_back_to_the_default(self, monkeypatch, bad):
        from archimedes.services.market_data_provider import _tiingo_min_request_interval_s

        monkeypatch.setenv("TIINGO_MIN_REQUEST_INTERVAL_S", bad)
        # TESTING is set by conftest, so the default here is 0.0 — the point
        # of the assertion is that a bad value never becomes the interval.
        assert _tiingo_min_request_interval_s() == 0.0

    def test_pacing_is_applied_at_the_real_http_boundary(self, monkeypatch):
        """End-to-end: a real ``TiingoProvider`` fetch goes through the pacer,
        so the politeness floor covers every endpoint family and both public
        methods rather than only the code path a unit test picked."""
        monkeypatch.setenv("TIINGO_MIN_REQUEST_INTERVAL_S", "1.1")
        pacer, slept = self._pacer_with_fake_time()
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE})
        provider = TiingoProvider(client=client, pacer=pacer)

        provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert slept == [], "first request is free"
        provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert slept == [pytest.approx(1.1)], "the second request must be paced"

    def test_batch_paces_between_symbols(self, monkeypatch):
        """The #1218 cost driver is a multi-symbol sweep; that is exactly the
        loop that must not fire N requests back to back."""
        monkeypatch.setenv("TIINGO_MIN_REQUEST_INTERVAL_S", "1.1")
        pacer, slept = self._pacer_with_fake_time()
        client = _mock_client(
            {
                "/tiingo/daily/SPY/prices": EQUITY_FIXTURE,
                "/tiingo/daily/QQQ/prices": EQUITY_FIXTURE,
                "/tiingo/daily/IWM/prices": EQUITY_FIXTURE,
            }
        )
        provider = TiingoProvider(client=client, pacer=pacer)
        provider.get_daily_close_batch({"sSPY": "SPY", "sQQQ": "QQQ", "sIWM": "IWM"}, period="1mo")
        assert slept == [pytest.approx(1.1), pytest.approx(1.1)], "3 symbols = 2 inter-request waits"


class TestRateLimitSurfacing:
    """HTTP 429 is surfaced as its own error type and propagates out of a
    batch instead of being laundered into a per-symbol skip."""

    @staticmethod
    def _rate_limited_client(headers: dict | None = None, capture: list | None = None) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            if capture is not None:
                capture.append(request)
            return httpx.Response(429, json={"detail": "rate limit"}, headers=headers or {}, request=request)

        return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.tiingo.com")

    def test_429_raises_the_dedicated_rate_limit_error(self):
        from archimedes.services.market_data_provider import TiingoRateLimitError

        provider = TiingoProvider(client=self._rate_limited_client())
        with pytest.raises(TiingoRateLimitError) as exc:
            provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert "429" in str(exc.value)
        assert exc.value.retry_after_s is None, "no header sent — must not invent a number"

    def test_retry_after_header_is_surfaced_verbatim(self):
        from archimedes.services.market_data_provider import TiingoRateLimitError

        provider = TiingoProvider(client=self._rate_limited_client({"Retry-After": "60"}))
        with pytest.raises(TiingoRateLimitError) as exc:
            provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert exc.value.retry_after_s == 60.0
        assert "60" in str(exc.value)

    def test_unparseable_retry_after_is_none_not_a_guess(self):
        """An HTTP-date Retry-After (RFC 9110 permits it) is a shape we do
        not parse. ``None`` is the honest answer; a fabricated number wearing
        the vendor's name is not."""
        from archimedes.services.market_data_provider import TiingoRateLimitError

        provider = TiingoProvider(client=self._rate_limited_client({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
        with pytest.raises(TiingoRateLimitError) as exc:
            provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert exc.value.retry_after_s is None

    def test_rate_limit_is_still_a_tiingo_provider_error_for_existing_callers(self):
        """Subclassing keeps every existing ``except TiingoProviderError``
        call site working — this widens the taxonomy, it does not break it."""
        from archimedes.services.market_data_provider import TiingoRateLimitError

        assert issubclass(TiingoRateLimitError, TiingoProviderError)

    def test_batch_propagates_the_rate_limit_instead_of_skipping_the_symbol(self):
        """THE REGRESSION GUARD.

        Before this change, 429 raised a bare ``TiingoProviderError``, which
        ``get_daily_close_batch``'s ``except TiingoProviderError: continue``
        swallowed per symbol. A universe sweep that hit its quota on symbol 1
        would therefore log N "skipping" lines, fire N requests at a vendor
        that had already said stop, and hand the caller a plausible-looking
        EMPTY dict — indistinguishable from "none of these symbols have
        data". This asserts the opposite: it raises, and it stops.
        """
        from archimedes.services.market_data_provider import TiingoRateLimitError

        seen: list[httpx.Request] = []
        provider = TiingoProvider(client=self._rate_limited_client(capture=seen))
        with pytest.raises(TiingoRateLimitError):
            provider.get_daily_close_batch({"sSPY": "SPY", "sQQQ": "QQQ", "sIWM": "IWM"}, period="1mo")
        assert len(seen) == 1, "must stop at the first 429, not keep hammering the remaining symbols"

    def test_a_non_429_http_error_is_still_skipped_per_symbol(self):
        """The batch's per-symbol tolerance must survive: only account-wide
        conditions escalate. A 500 on one ticker still leaves the others."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/tiingo/daily/QQQ/prices":
                return httpx.Response(500, json={"detail": "boom"}, request=request)
            return httpx.Response(200, json=EQUITY_FIXTURE, request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.tiingo.com")
        result = TiingoProvider(client=client).get_daily_close_batch(
            {"sSPY": "SPY", "sQQQ": "QQQ", "sIWM": "IWM"}, period="1mo"
        )
        assert set(result) == {"sSPY", "sIWM"}
        assert len(calls) == 3, "a per-symbol failure must not abort the sweep"


# ─── Cache source pinning: no mixed-vendor panel (#1218) ────────────────


class _StubProvider:
    """A minimal non-Tiingo vendor used to prime the cache as 'yfinance'.

    Not a ``TiingoProvider`` and not a mock of one: the point is to get rows
    into ``asset_daily_bars`` stamped with a DIFFERENT ``source``, through
    the real ``CachingMarketDataProvider`` write path, exactly as a
    production system running on yfinance would already have done.
    """

    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame
        self.calls = 0

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        self.calls += 1
        return self._frame.copy()

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        self.calls += 1
        return {key: self._frame["Close"].rename(key) for key in tickers}

    def get_intraday_quote(self, ticker):  # pragma: no cover - unused here
        raise NotImplementedError

    def get_intraday_quotes_batch(self, tickers):  # pragma: no cover - unused here
        raise NotImplementedError

    def get_series(self, ticker, period, interval):  # pragma: no cover - unused here
        raise NotImplementedError


#: Deliberately NOT equal to any adjusted value in EQUITY_FIXTURE, so a
#: mixed-source read is visible in the numbers rather than only in a count.
_YFINANCE_LIKE_FRAME = pd.DataFrame(
    {
        "Open": [1.0, 2.0],
        "High": [1.5, 2.5],
        "Low": [0.5, 1.5],
        "Close": [111.11, 222.22],
        "Volume": [10.0, 20.0],
    },
    index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
)

#: ``get_daily_close_batch`` resolves ``period`` to "rows newer than ~N days
#: ago" AND requires the cache to reach back to the window's start, so the
#: close-only cache path can only be exercised with recent dates spanning the
#: whole window. Static 2024 fixtures miss that cache for a coverage reason,
#: not a source one — which is exactly how the first draft of
#: ``test_close_batch_is_source_pinned_too`` ended up vacuous.
_RECENT_DAYS = 40


def _recent_dates() -> pd.DatetimeIndex:
    end = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    return pd.date_range(end=end, periods=_RECENT_DAYS, freq="D")


#: Distinct from the yfinance-like closes below, so a mixed-source read shows
#: up in the VALUES, not only in a request count.
_TIINGO_RECENT_CLOSES = [400.0 + i for i in range(_RECENT_DAYS)]
_YFINANCE_RECENT_CLOSES = [900.0 + i for i in range(_RECENT_DAYS)]


def _recent_frame() -> pd.DataFrame:
    """A yfinance-shaped frame over ``_recent_dates()``."""
    idx = _recent_dates()
    return pd.DataFrame(
        {
            "Open": _YFINANCE_RECENT_CLOSES,
            "High": [c + 1 for c in _YFINANCE_RECENT_CLOSES],
            "Low": [c - 1 for c in _YFINANCE_RECENT_CLOSES],
            "Close": _YFINANCE_RECENT_CLOSES,
            "Volume": [1000.0] * _RECENT_DAYS,
        },
        index=idx,
    )


def _recent_equity_fixture() -> list[dict]:
    """Tiingo equity rows over the same window, with different closes."""
    return [
        {
            "date": d.strftime("%Y-%m-%dT00:00:00.000Z"),
            "close": c + 5,
            "high": c + 6,
            "low": c + 4,
            "open": c + 5,
            "volume": 999,
            "adjClose": c,
            "adjHigh": c + 1,
            "adjLow": c - 1,
            "adjOpen": c,
            "adjVolume": 1000,
        }
        for d, c in zip(_recent_dates(), _TIINGO_RECENT_CLOSES, strict=True)
    ]


class TestCacheSourcePinning:
    """A warm cache written by one vendor must never be served to another.

    ``asset_daily_bars`` has always recorded a ``source`` per row, but the
    reads matched on ``symbol`` alone. That made a provider flip on a system
    with a populated cache — i.e. production — serve yfinance bars for warm
    symbols and Tiingo bars for cold ones inside a single backtest panel,
    silently. These tests pin the fix.
    """

    def test_a_yfinance_warm_cache_is_a_miss_for_the_tiingo_provider(self, session_factory):
        from archimedes.services.market_data_provider import CachingMarketDataProvider

        # 1. A system already running on yfinance primes the cache.
        stub = _StubProvider(_YFINANCE_LIKE_FRAME)
        yf_side = CachingMarketDataProvider(stub, source_name="yfinance", session_factory=session_factory)
        primed = yf_side.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert primed["Close"].tolist() == [111.11, 222.22]
        assert stub.calls == 1
        yf_side.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert stub.calls == 1, "sanity: the yfinance cache IS warm for this symbol/range"

        # 2. MARKET_DATA_PROVIDER flips to tiingo. Same symbol, same range.
        seen: list[httpx.Request] = []
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE}, capture=seen)
        tiingo_side = CachingMarketDataProvider(
            TiingoProvider(client=client), source_name="tiingo", session_factory=session_factory
        )
        after_flip = tiingo_side.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")

        # 3. It must have gone to Tiingo, and returned TIINGO's numbers.
        assert len(seen) == 1, "a foreign-source cache row must not satisfy this read"
        assert after_flip["Close"].tolist() == [468.20, 464.37], "served yfinance bars under a tiingo provider"
        assert stub.calls == 1, "the yfinance vendor must not be consulted either"

    def test_close_batch_is_source_pinned_too(self, session_factory):
        """The close-only read path shares the defect and the fix — the
        universe sweep is the highest-volume consumer of this cache.

        Dates here are RECENT and span the whole ``period`` window on
        purpose. A first draft of this test reused the static 2024-01-02/03
        fixtures and passed with the source filter removed — ``period="1mo"``
        makes ``_read_cached_series`` ask for rows newer than ~31 days ago,
        so two-year-old rows missed the cache for a reason that had nothing
        to do with ``source``. It was a vacuous guard. The revert
        demonstration in the PR body is run against THIS version.
        """
        from archimedes.services.market_data_provider import CachingMarketDataProvider

        # 1. Prime as yfinance, covering the full 1mo window.
        stub = _StubProvider(_recent_frame())
        yf_side = CachingMarketDataProvider(stub, source_name="yfinance", session_factory=session_factory)
        yf_side.get_daily_close_batch({"SPY": "SPY"}, period="1mo")
        assert stub.calls == 1
        yf_side.get_daily_close_batch({"SPY": "SPY"}, period="1mo")
        assert stub.calls == 1, "sanity: the yfinance close cache IS warm for this symbol/period"

        # 2. Flip to tiingo. Same symbol, same period.
        seen: list[httpx.Request] = []
        client = _mock_client({"/tiingo/daily/SPY/prices": _recent_equity_fixture()}, capture=seen)
        tiingo_side = CachingMarketDataProvider(
            TiingoProvider(client=client), source_name="tiingo", session_factory=session_factory
        )
        result = tiingo_side.get_daily_close_batch({"SPY": "SPY"}, period="1mo")

        assert len(seen) == 1, "a foreign-source cache row must not satisfy the batch read either"
        assert result["SPY"].tolist() == _TIINGO_RECENT_CLOSES, "served yfinance closes under a tiingo provider"
        assert stub.calls == 1, "the yfinance vendor must not be consulted either"

    def test_close_batch_same_source_still_hits_the_cache(self, session_factory):
        """Anti-vacuity for the batch path, matching the OHLCV one below."""
        from archimedes.services.market_data_provider import CachingMarketDataProvider

        seen: list[httpx.Request] = []
        client = _mock_client({"/tiingo/daily/SPY/prices": _recent_equity_fixture()}, capture=seen)
        provider = CachingMarketDataProvider(
            TiingoProvider(client=client), source_name="tiingo", session_factory=session_factory
        )
        provider.get_daily_close_batch({"SPY": "SPY"}, period="1mo")
        provider.get_daily_close_batch({"SPY": "SPY"}, period="1mo")
        assert len(seen) == 1, "same-source warm batch read must still be served from asset_daily_bars"

    def test_same_source_still_hits_the_cache(self, session_factory):
        """Anti-vacuity: the guard must reject a FOREIGN source, not defeat
        caching altogether. Without this, 'always miss' would pass the two
        tests above while silently removing the cache's whole purpose."""
        from archimedes.services.market_data_provider import CachingMarketDataProvider

        seen: list[httpx.Request] = []
        client = _mock_client({"/tiingo/daily/SPY/prices": EQUITY_FIXTURE}, capture=seen)
        provider = CachingMarketDataProvider(
            TiingoProvider(client=client), source_name="tiingo", session_factory=session_factory
        )
        provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        provider.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-03")
        assert len(seen) == 1, "same-source warm read must still be served from asset_daily_bars"


# ─── Credential env-var naming (TIINGO_API_TOKEN canonical) ─────────────


class TestCredentialEnvVarNaming:
    def test_canonical_token_var_is_accepted(self, monkeypatch):
        from archimedes.services.market_data_provider import _tiingo_api_key

        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        monkeypatch.setenv("TIINGO_API_TOKEN", "canonical-token")
        assert _tiingo_api_key() == "canonical-token"

    def test_legacy_key_var_still_works(self, monkeypatch):
        """Back-compat: the name already merged on ``main`` and already in
        developers' .env files keeps working."""
        from archimedes.services.market_data_provider import _tiingo_api_key

        monkeypatch.delenv("TIINGO_API_TOKEN", raising=False)
        monkeypatch.setenv("TIINGO_API_KEY", "legacy-key")
        assert _tiingo_api_key() == "legacy-key"

    def test_canonical_wins_when_both_are_set(self, monkeypatch):
        from archimedes.services.market_data_provider import _tiingo_api_key

        monkeypatch.setenv("TIINGO_API_TOKEN", "canonical-token")
        monkeypatch.setenv("TIINGO_API_KEY", "legacy-key")
        assert _tiingo_api_key() == "canonical-token"

    def test_legacy_var_logs_a_rename_hint(self, monkeypatch, caplog):
        from archimedes.services.market_data_provider import _tiingo_api_key

        monkeypatch.delenv("TIINGO_API_TOKEN", raising=False)
        monkeypatch.setenv("TIINGO_API_KEY", "legacy-key")
        with caplog.at_level("WARNING"):
            _tiingo_api_key()
        assert "TIINGO_API_TOKEN" in caplog.text
        assert "legacy-key" not in caplog.text, "a rename hint must never log the credential"

    def test_neither_var_set_fails_loud_and_names_the_canonical_var(self, monkeypatch):
        from archimedes.services.market_data_provider import _tiingo_api_key

        monkeypatch.delenv("TIINGO_API_TOKEN", raising=False)
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        with pytest.raises(TiingoAPIKeyMissingError) as exc:
            _tiingo_api_key()
        assert "TIINGO_API_TOKEN" in str(exc.value)

    def test_flag_forced_on_with_no_token_refuses_rather_than_falling_back(self, monkeypatch):
        """The #1218 fail-safe, stated as the owner framed it: with the flag
        forced ON and no credential, the engine must refuse LOUDLY — never
        silently serve the yfinance path under a 'tiingo' label."""
        from archimedes.services.market_data_provider import YFinanceProvider

        monkeypatch.setenv("MARKET_DATA_PROVIDER", "tiingo")
        monkeypatch.delenv("MARKET_DATA_DAILY_PROVIDER", raising=False)
        monkeypatch.delenv("TIINGO_API_TOKEN", raising=False)
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)
        with pytest.raises(TiingoAPIKeyMissingError):
            provider = get_provider(seam="daily")
            assert not isinstance(provider._inner._inner, YFinanceProvider), "silent yfinance fallback"
