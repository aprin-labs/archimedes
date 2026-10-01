"""Per-seam market-data routing (#1798).

#1798's finding: ``MARKET_DATA_PROVIDER`` was ONE global variable read by every
seam, and Tiingo serves daily bars only — so the flip that was supposed to move
backtests onto licensed data would also have pointed the live oracle push, the
paper-marks loop and the Explore history modal at three
``NotImplementedError``s. The fix routes by seam: a ``daily`` vendor
(``MARKET_DATA_DAILY_PROVIDER``) that may be Tiingo, and an ``intraday`` vendor
(``MARKET_DATA_PROVIDER``) that stays on yfinance.

What this module pins, and the mutation that turns each red:

* **The default is unchanged.** Nothing set → both seams yfinance. Mutation:
  default either seam to ``"tiingo"`` → ``TestDefaultsAreUnchanged`` red.
* **The seams are actually separate.** The pre-#1798 global flip
  (``MARKET_DATA_PROVIDER=tiingo``, daily var unset) moves daily bars to Tiingo
  and leaves intraday on yfinance. Mutation: drop the ``_VENDOR_SEAMS``
  capability check in ``provider_name`` → ``TestTheSplitHolds`` red (intraday
  resolves to tiingo again, i.e. the #1798 breakage restored).
* **The daily seam refuses intraday methods.** Mutation: make
  ``SeamRoutedProvider._route`` return ``getattr(self._inner, method)``
  unconditionally → ``TestSeamDispatch`` red.
* **Tiingo refuses intraday before any network call.** Mutation: give
  ``TiingoProvider.get_series`` a body that fetches → ``TestTiingoIsDailyOnly``
  red on the "transport never touched" assertion.
* **Every call site names its seam, and the RIGHT one.** Mutation: drop
  ``seam=`` anywhere → ``TestEveryCallSiteNamesItsSeam`` red (``get_provider``
  is keyword-only on ``seam``, so it is a TypeError at runtime too); change a
  row's seam (``portfolio_backtester``'s ``daily`` → ``intraday``) → the same
  class red. That second mutation used to pass, because the check was a
  substring match over ``inspect.getsource`` and the backtester's DOCSTRING
  quotes its own call — see ``_seams_requested_by``, now ``ast``-based.
* **A vendor flip never leaves a bar stitched from two vendors.** The
  close-only universe sweep lands on the previous vendor's ``(symbol,
  trade_date)`` row; if it overwrote only ``close`` + ``source``, the row would
  read back as a valid Tiingo bar with yfinance OHLV. Mutation: restore that
  partial overwrite in ``_write_cached_series`` →
  ``TestAFlipNeverProducesAMixedVendorBar`` red.

Hermetic: stub providers and an ``httpx.MockTransport`` that fails the test if
it is ever asked for a request. No network, no vendor import; the one
cache-crossing class uses a throwaway SQLite file, never the app's engine.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import UTC, datetime

import httpx
import pandas as pd
import pytest
from archimedes.services.market_data_provider import (
    DAILY_SEAM,
    INTRADAY_SEAM,
    MarketDataProvider,
    MarketDataSeamError,
    SeamRoutedProvider,
    TiingoProvider,
    YFinanceProvider,
    get_provider,
    intraday_is_delayed,
    provider_name,
)


@pytest.fixture(autouse=True)
def _clean_market_data_env(monkeypatch):
    """Neither variable leaks in from the developer's shell or another test."""
    monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
    monkeypatch.delenv("MARKET_DATA_DAILY_PROVIDER", raising=False)


def _seams_requested_by(func) -> list[str]:
    """Every ``seam=`` literal passed to a ``get_provider(...)`` call inside
    ``func``'s own code, parsed with ``ast``.

    **Why parsing and not ``'get_provider(seam="daily")' in getsource(func)``,
    which is what this module shipped with:** ``inspect.getsource`` returns the
    DOCSTRING too, and ``portfolio_backtester._fetch_price_panel``'s docstring
    quotes ``get_provider(seam="daily")`` verbatim to explain itself. That row
    therefore passed on prose — mutating the real call to ``seam="intraday"``
    left this whole module green, which is precisely the wrong-seam defect the
    table exists to catch. Nothing else covered it either: the runtime class
    below checks two other functions, and at runtime the intraday seam happily
    serves ``get_daily_ohlcv``, so the backtester would have run on the
    intraday vendor in silence.

    An ``ast.Call`` node cannot be reached by a docstring, and the fix is
    structural rather than specific to today's one offending docstring — any
    row whose function later quotes its own call stays honest.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    seams: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name != "get_provider":
            continue
        for kw in node.keywords:
            if kw.arg == "seam" and isinstance(kw.value, ast.Constant):
                seams.append(kw.value.value)
    return seams


class _StubProvider(MarketDataProvider):
    """Records what it was asked for. Every method returns a sentinel the test
    can recognize, so "the router delegated" is an assertion about a value that
    could only have come from here."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        self.calls.append("get_daily_close_batch")
        return {k: pd.Series([1.0], name=k) for k in tickers}

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        self.calls.append("get_daily_ohlcv")
        return pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2024-01-02"]))

    def get_intraday_quote(self, ticker: str) -> tuple[float, datetime] | None:
        self.calls.append("get_intraday_quote")
        return (1.0, datetime(2024, 1, 2, tzinfo=UTC))

    def get_intraday_quotes_batch(self, tickers: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        self.calls.append("get_intraday_quotes_batch")
        return {k: (1.0, datetime(2024, 1, 2, tzinfo=UTC)) for k in tickers}

    def get_series(self, ticker: str, period: str, interval: str) -> pd.Series:
        self.calls.append("get_series")
        return pd.Series([1.0], index=pd.to_datetime(["2024-01-02"]))


# ─── Vendor selection, per seam ─────────────────────────────────────────


class TestDefaultsAreUnchanged:
    """The whole point of shipping this dark: with nothing set, both seams
    resolve to yfinance exactly as they did before #1798."""

    def test_both_seams_default_to_yfinance(self):
        assert provider_name(DAILY_SEAM) == "yfinance"
        assert provider_name(INTRADAY_SEAM) == "yfinance"

    def test_get_provider_defaults_to_a_yfinance_vendor_on_both_seams(self):
        for seam in (DAILY_SEAM, INTRADAY_SEAM):
            provider = get_provider(seam=seam)
            assert isinstance(provider, SeamRoutedProvider)
            assert provider.vendor_name == "yfinance"
            assert isinstance(provider._inner._inner, YFinanceProvider)

    def test_a_blank_value_is_not_a_vendor_name(self, monkeypatch):
        """``MARKET_DATA_DAILY_PROVIDER=`` (the shape an unset terraform
        variable or an empty .env line produces) falls through to
        MARKET_DATA_PROVIDER, then to yfinance — it does not select a vendor
        called ''."""
        monkeypatch.setenv("MARKET_DATA_DAILY_PROVIDER", "   ")
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "tiingo")
        assert provider_name(DAILY_SEAM) == "tiingo"

        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        assert provider_name(DAILY_SEAM) == "yfinance"


class TestDailySeamSelection:
    def test_daily_var_selects_the_daily_vendor(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_DAILY_PROVIDER", "tiingo")
        assert provider_name(DAILY_SEAM) == "tiingo"

    def test_daily_var_wins_over_the_legacy_global(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "yfinance")
        monkeypatch.setenv("MARKET_DATA_DAILY_PROVIDER", "tiingo")
        assert provider_name(DAILY_SEAM) == "tiingo"
        assert provider_name(INTRADAY_SEAM) == "yfinance"

    def test_case_and_whitespace_insensitive(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_DAILY_PROVIDER", "  TiinGo ")
        assert provider_name(DAILY_SEAM) == "tiingo"

    def test_unknown_daily_vendor_falls_back_to_yfinance(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("MARKET_DATA_DAILY_PROVIDER", "some_unreleased_vendor")
        with caplog.at_level(logging.WARNING):
            assert provider_name(DAILY_SEAM) == "yfinance"
        assert any("some_unreleased_vendor" in rec.message for rec in caplog.records)


class TestTheSplitHolds:
    """The proof #1798 asked for: the pre-existing global flip must no longer
    be able to break intraday."""

    def test_global_flip_to_tiingo_moves_daily_and_leaves_intraday_alone(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv("MARKET_DATA_PROVIDER", "tiingo")  # daily var deliberately unset
        with caplog.at_level(logging.WARNING):
            assert provider_name(DAILY_SEAM) == "tiingo", "back-compat: the legacy var still moves daily bars"
            assert provider_name(INTRADAY_SEAM) == "yfinance", "#1798: the flip must not reach intraday"
        # Substituted, never silently: the log names the vendor and the seam.
        assert any("tiingo" in rec.message.lower() and "intraday" in rec.message for rec in caplog.records)

    def test_intraday_provider_under_a_global_tiingo_flip_is_a_working_yfinance_one(self, monkeypatch):
        """The failure this replaces: ``get_provider()`` used to build a
        ``TiingoProvider`` here — raising ``TiingoAPIKeyMissingError`` with no
        token, or ``NotImplementedError`` on the first quote with one."""
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "tiingo")
        monkeypatch.delenv("TIINGO_API_TOKEN", raising=False)
        monkeypatch.delenv("TIINGO_API_KEY", raising=False)

        provider = get_provider(seam=INTRADAY_SEAM)

        assert provider.vendor_name == "yfinance"
        assert isinstance(provider._inner._inner, YFinanceProvider)

    def test_the_delayed_intraday_declaration_follows_the_intraday_seam(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "tiingo")
        # yfinance's feed is declared delayed; a tiingo-shaped answer here would
        # mean intraday_is_delayed() had gone looking at the wrong seam.
        assert intraday_is_delayed() is True

    def test_marks_and_pushes_stamp_the_vendor_that_actually_served_them(self, monkeypatch):
        """Provenance stays true under the substitution: ``paper_marks`` and
        ``oracle_updater`` stamp ``provider_name("intraday")``, which is the
        vendor that really answered, not the name in the env var."""
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "tiingo")
        assert provider_name(INTRADAY_SEAM) == get_provider(seam=INTRADAY_SEAM).vendor_name


class TestUnknownSeam:
    def test_an_unknown_seam_name_is_a_loud_error(self):
        with pytest.raises(MarketDataSeamError) as exc:
            provider_name("weekly")
        assert "weekly" in str(exc.value)

    def test_get_provider_rejects_an_unknown_seam(self):
        with pytest.raises(MarketDataSeamError):
            get_provider(seam="weekly")

    def test_seam_is_keyword_only(self):
        with pytest.raises(TypeError):
            get_provider("daily")  # type: ignore[misc]


# ─── Method dispatch ────────────────────────────────────────────────────


class TestSeamDispatch:
    def test_daily_seam_serves_the_two_daily_bar_methods(self):
        stub = _StubProvider()
        routed = SeamRoutedProvider(stub, seam=DAILY_SEAM, vendor_name="tiingo")

        routed.get_daily_close_batch({"sSPY": "SPY"}, period="1y")
        routed.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-05")

        assert stub.calls == ["get_daily_close_batch", "get_daily_ohlcv"]

    @pytest.mark.parametrize(
        "call",
        [
            lambda p: p.get_intraday_quote("SPY"),
            lambda p: p.get_intraday_quotes_batch({"sSPY": "SPY"}),
            lambda p: p.get_series("SPY", "1y", "1h"),
        ],
        ids=["get_intraday_quote", "get_intraday_quotes_batch", "get_series"],
    )
    def test_daily_seam_refuses_intraday_methods_without_touching_the_vendor(self, call):
        stub = _StubProvider()
        routed = SeamRoutedProvider(stub, seam=DAILY_SEAM, vendor_name="tiingo")

        with pytest.raises(MarketDataSeamError) as exc:
            call(routed)

        assert stub.calls == [], "the refusal must happen before the vendor is asked"
        assert "intraday" in str(exc.value), "the error must name the seam that CAN serve it"

    def test_the_refusal_does_not_depend_on_which_vendor_is_configured(self, monkeypatch):
        """Deterministic by design: the daily seam refuses ``get_series`` even
        when its vendor is yfinance, which could serve it. Otherwise the seam's
        shape would change under the operator's flag and a call site could pass
        review, ship, and only fail on the day of the flip."""
        provider = get_provider(seam=DAILY_SEAM)
        assert provider.vendor_name == "yfinance"
        with pytest.raises(MarketDataSeamError):
            provider.get_series("SPY", "1y", "1h")

    def test_intraday_seam_serves_daily_bars_too(self):
        """One run, one vendor (the ADR's rule, unchanged): the oracle snapshot
        reads ^VIX intraday and ^GSPC daily in a single run, so the intraday
        seam must answer both from the same vendor."""
        stub = _StubProvider()
        routed = SeamRoutedProvider(stub, seam=INTRADAY_SEAM, vendor_name="yfinance")

        routed.get_intraday_quote("^VIX")
        routed.get_daily_close_batch({"^GSPC": "^GSPC"}, period="1y")
        routed.get_series("SPY", "1y", "1h")
        routed.get_intraday_quotes_batch({"sSPY": "SPY"})
        routed.get_daily_ohlcv("SPY", "2024-01-02", "2024-01-05")

        assert stub.calls == [
            "get_intraday_quote",
            "get_daily_close_batch",
            "get_series",
            "get_intraday_quotes_batch",
            "get_daily_ohlcv",
        ]

    def test_the_router_returns_the_vendor_s_own_value_unaltered(self):
        stub = _StubProvider()
        routed = SeamRoutedProvider(stub, seam=INTRADAY_SEAM, vendor_name="yfinance")
        assert routed.get_intraday_quote("SPY") == (1.0, datetime(2024, 1, 2, tzinfo=UTC))


# ─── The vendor's own limit, independently ──────────────────────────────


class TestTiingoIsDailyOnly:
    """Belt and braces: even if ``_VENDOR_SEAMS`` were edited to put Tiingo on
    the intraday seam, the adapter itself refuses — and refuses BEFORE it opens
    a socket, so a mis-wired deploy costs an exception, not a vendor bill or a
    hung request."""

    @staticmethod
    def _no_network_client(requests: list[httpx.Request]) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must never run
            requests.append(request)
            raise AssertionError(f"network touched: {request.method} {request.url}")

        return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://api.tiingo.com")

    @pytest.mark.parametrize(
        "call",
        [
            lambda p: p.get_intraday_quote("SPY"),
            lambda p: p.get_intraday_quotes_batch({"sSPY": "SPY"}),
            lambda p: p.get_series("SPY", "1y", "1h"),
        ],
        ids=["get_intraday_quote", "get_intraday_quotes_batch", "get_series"],
    )
    def test_intraday_methods_raise_before_any_request(self, monkeypatch, call):
        monkeypatch.setenv("TIINGO_API_TOKEN", "test-token-not-a-real-one")
        requests: list[httpx.Request] = []
        provider = TiingoProvider(client=self._no_network_client(requests))

        with pytest.raises(NotImplementedError):
            call(provider)

        assert requests == []


# ─── Call sites ─────────────────────────────────────────────────────────


class TestEveryCallSiteNamesItsSeam:
    """#1798's rule: no implicit default at a call site. ``get_provider`` is
    keyword-only on ``seam``, so a dropped argument is a TypeError — but WHICH
    seam a call site asks for is a decision, and this table is where it is
    reviewed. Source-level on purpose: several of these functions need a live
    DB session or an event loop to reach, and the question here is which
    vendor a feature is wired to, which the source answers exactly."""

    @pytest.mark.parametrize(
        ("import_path", "func_name", "seam"),
        [
            # Daily bars — the seam Tiingo may serve.
            ("archimedes.services.strategy_signal_evaluator", "_fetch_price_history", "daily"),
            ("archimedes.services.strategy_signal_evaluator", "_fetch_price_histories", "daily"),
            ("archimedes.services.fusion_market_data", "_fetch_one", "daily"),
            ("archimedes.services.portfolio_backtester", "_fetch_price_panel", "daily"),
            # Live/interactive — stays on yfinance.
            ("archimedes.services.paper_marks", "mark_all", "intraday"),
            ("archimedes.services.asset_market_service", "_fetch_yfinance_series", "intraday"),
        ],
    )
    def test_function_asks_for_its_seam(self, import_path, func_name, seam):
        import importlib

        module = importlib.import_module(import_path)
        seams = _seams_requested_by(getattr(module, func_name))
        assert seams, f"{import_path}.{func_name} makes no get_provider(seam=…) call"
        # A set, so a function is free to ask its seam twice — but asking TWO
        # seams in one function is the vendor mix inside one run the ADR
        # forbids, and belongs red here rather than discovered in a panel.
        assert set(seams) == {seam}, f"{import_path}.{func_name} asks {sorted(set(seams))}, expected [{seam!r}]"

    @pytest.mark.parametrize(
        ("method_name", "seam"),
        [
            ("_fetch_yfinance", "intraday"),
            ("_fetch_yfinance_single", "intraday"),
            ("_fetch_crypto_provider", "intraday"),
            # ^GSPC daily bars, on the INTRADAY seam deliberately: one run,
            # one vendor (fetch_market_snapshot reads ^VIX alongside it).
            ("_fetch_sp500_moving_averages", "intraday"),
        ],
    )
    def test_oracle_updater_is_entirely_on_the_intraday_seam(self, method_name, seam):
        from archimedes.chain.oracle_updater import OracleUpdater

        seams = _seams_requested_by(getattr(OracleUpdater, method_name))
        assert seams, f"OracleUpdater.{method_name} makes no get_provider(seam=…) call"
        assert set(seams) == {seam}, f"OracleUpdater.{method_name} asks {sorted(set(seams))}, expected [{seam!r}]"

    def test_no_backend_call_site_asks_without_a_seam(self):
        """Structural: a ``get_provider(...)`` call anywhere in backend source
        that names no seam would be a call site picking a vendor by accident.

        Parsed with ``ast`` rather than grepped, so the many prose mentions of
        ``get_provider()`` in this package's docstrings cannot make it pass or
        fail for the wrong reason."""
        import ast
        import pathlib

        # The ``archimedes`` package itself — runtime source, not tests (which
        # legitimately call it wrong on purpose, e.g. test_seam_is_keyword_only).
        root = pathlib.Path(inspect.getsourcefile(get_provider)).parents[1]
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name != "get_provider":
                    continue
                if not any(kw.arg == "seam" for kw in node.keywords):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
        assert offenders == []


class TestCallSiteSeamsAtRuntime:
    """Two of the source-level rows above, re-checked by actually calling the
    function with ``get_provider`` monkeypatched — so the table cannot pass on
    a string that no longer runs."""

    def test_fusion_fetch_one_asks_the_daily_seam(self, monkeypatch):
        import archimedes.services.fusion_market_data as fmd
        from archimedes.services import market_data_provider as mdp

        seen: list[str] = []
        stub = _StubProvider()
        monkeypatch.setattr(mdp, "get_provider", lambda *, seam: (seen.append(seam), stub)[1])

        fmd._fetch_one("SPY", "2024-01-02", "2024-01-05")

        assert seen == ["daily"]
        assert stub.calls == ["get_daily_ohlcv"]

    def test_oracle_sp500_moving_averages_asks_the_intraday_seam(self, monkeypatch):
        from archimedes.chain.oracle_updater import OracleUpdater
        from archimedes.services import market_data_provider as mdp

        seen: list[str] = []

        class _Closes(_StubProvider):
            def get_daily_close_batch(self, tickers, period):
                self.calls.append("get_daily_close_batch")
                return {"^GSPC": pd.Series(range(250), dtype=float)}

        stub = _Closes()
        monkeypatch.setattr(mdp, "get_provider", lambda *, seam: (seen.append(seam), stub)[1])

        out = OracleUpdater._fetch_sp500_moving_averages(object())

        assert seen == ["intraday"]
        assert set(out) == {"ma50", "ma200"}


# ─── The flip, end to end, through the cache ────────────────────────────


class _TwoColumnVendor(MarketDataProvider):
    """A vendor whose every number is traceable to it by value.

    ``close``/``open``/``volume`` are given per-instance, so an assertion can
    say WHICH vendor a column came from rather than only that it is populated
    — which is the whole question when the failure mode is one bar stitched
    from two vendors."""

    def __init__(self, *, close: float, open_: float, volume: float) -> None:
        self.close = close
        self.open = open_
        self.volume = volume
        self.close_batch_calls: list[dict[str, str]] = []
        self.ohlcv_calls: list[tuple[str, str, str]] = []

    @staticmethod
    def _index() -> pd.DatetimeIndex:
        return pd.date_range(end=pd.Timestamp.now("UTC").normalize(), periods=10, freq="D")

    def get_daily_close_batch(self, tickers: dict[str, str], period: str) -> dict[str, pd.Series]:
        self.close_batch_calls.append(dict(tickers))
        idx = self._index()
        return {k: pd.Series([self.close] * len(idx), index=idx, name=k) for k in tickers}

    def get_daily_ohlcv(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        self.ohlcv_calls.append((ticker, start, end))
        idx = self._index()
        n = len(idx)
        return pd.DataFrame(
            {
                "Open": [self.open] * n,
                "High": [self.open + 1] * n,
                "Low": [self.open - 1] * n,
                "Close": [self.close] * n,
                "Volume": [self.volume] * n,
            },
            index=idx,
        )

    def get_intraday_quote(self, ticker):  # pragma: no cover - never reached on the daily seam
        raise AssertionError("the daily seam must never ask for an intraday quote")

    def get_intraday_quotes_batch(self, tickers):  # pragma: no cover - same
        raise AssertionError("the daily seam must never ask for intraday quotes")

    def get_series(self, ticker, period, interval):  # pragma: no cover - same
        raise AssertionError("the daily seam must never ask for a series")


class TestAFlipNeverProducesAMixedVendorBar:
    """The write path's half of the vendor seam (#1798).

    ``_read_cached_ohlcv``'s ``source`` filter guards READS: a row stamped
    ``yfinance`` is not served to a Tiingo caller. What that cannot see is a
    row whose ``source`` column says ``tiingo`` because the close-only writer
    stamped it, while ``open/high/low/volume`` are still the yfinance bars
    nobody overwrote. ``asset_daily_bars`` is unique on
    ``(symbol, trade_date)``, so the close-only writer lands ON the old
    vendor's row rather than beside it, and this is the exact sequence prod
    performs on the first tick after the flip: the universe sweep
    (``get_daily_close_batch``) runs before the generation panels
    (``get_daily_ohlcv``) and both are on the daily seam, same tickers.

    Restoring the partial overwrite in ``_write_cached_series`` (assign
    ``close``/``source`` and leave OHLV alone) turns
    ``test_the_daily_seam_refetches_from_the_new_vendor`` red.

    Hermetic: two stub vendors and an isolated SQLite file — no network, no
    real DB, no vendor import.
    """

    @pytest.fixture()
    def flip_bench(self, tmp_path, monkeypatch):
        """Wire ``get_provider`` to two stub vendors and a throwaway SQLite,
        so the test drives the REAL ``get_provider`` → ``SeamRoutedProvider``
        → ``CachingMarketDataProvider`` → vendor stack rather than a
        rehearsal of it."""
        from archimedes.db import Base
        from archimedes.services import market_data_provider as mdp
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(f"sqlite:///{tmp_path / 'seam_flip.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine)
        monkeypatch.setattr(mdp, "_default_session_factory", factory)

        yf = _TwoColumnVendor(close=900.0, open_=900.1, volume=11.0)
        tg = _TwoColumnVendor(close=400.0, open_=400.1, volume=22.0)
        monkeypatch.setattr(mdp, "_VENDOR_PROVIDERS", {"yfinance": lambda: yf, "tiingo": lambda: tg})
        return yf, tg

    @staticmethod
    def _window() -> tuple[str, str]:
        idx = _TwoColumnVendor._index()
        return idx[0].date().isoformat(), idx[-1].date().isoformat()

    def test_the_daily_seam_refetches_from_the_new_vendor(self, flip_bench, monkeypatch):
        yf, tg = flip_bench
        start, end = self._window()

        # 1. Before the flip: a warm, full-OHLCV yfinance cache (what prod has).
        get_provider(seam="daily").get_daily_ohlcv("SPY", start, end)
        assert yf.ohlcv_calls  # cold cache primed from yfinance

        # 2. The flip, then the close-only universe sweep that runs first.
        monkeypatch.setenv("MARKET_DATA_DAILY_PROVIDER", "tiingo")
        assert provider_name(DAILY_SEAM) == "tiingo"
        get_provider(seam="daily").get_daily_close_batch({"sSPY": "SPY"}, period="1mo")
        assert tg.close_batch_calls  # the sweep really went to Tiingo

        # 3. The generation panel's OHLCV read, on the same seam and ticker.
        tg.ohlcv_calls.clear()
        yf.ohlcv_calls.clear()
        panel = get_provider(seam="daily").get_daily_ohlcv("SPY", start, end)

        assert tg.ohlcv_calls, (
            "the OHLCV read was served from cache after a vendor flip — the close-only sweep "
            "left a row stamped source='tiingo' whose OHLV is still yfinance's"
        )
        assert yf.ohlcv_calls == []  # and the old vendor was not re-consulted
        assert panel["Close"].tolist() == [tg.close] * len(panel)
        assert panel["Open"].tolist() == [tg.open] * len(panel)
        assert panel["Volume"].tolist() == [tg.volume] * len(panel)

    def test_no_row_survives_the_sweep_with_a_foreign_vendors_ohlv(self, flip_bench, monkeypatch):
        """The stored row, checked directly: after a cross-vendor close-only
        write the row is an honest partial bar (close from the new vendor,
        OHLV NULL), never the old vendor's OHLV under the new vendor's label.
        ``portfolio_backtester._fetch_price_panel`` consumes ``Volume`` as
        well as ``Close``, so a blended row reaches graded numbers."""
        from archimedes.models.asset_daily_bars import AssetDailyBar
        from archimedes.services import market_data_provider as mdp

        _yf, tg = flip_bench
        start, end = self._window()
        get_provider(seam="daily").get_daily_ohlcv("SPY", start, end)

        monkeypatch.setenv("MARKET_DATA_DAILY_PROVIDER", "tiingo")
        get_provider(seam="daily").get_daily_close_batch({"sSPY": "SPY"}, period="1mo")

        session = mdp._default_session_factory()
        try:
            rows = session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").all()
        finally:
            session.close()

        assert rows
        for row in rows:
            assert row.source == "tiingo"
            assert row.close == tg.close
            assert (row.open, row.high, row.low, row.volume) == (None, None, None, None), (
                f"MIXED-VENDOR BAR: source={row.source!r} close={row.close!r} but "
                f"open={row.open!r} volume={row.volume!r} came from another vendor"
            )

    def test_a_same_vendor_close_write_does_not_clear_the_bar(self, flip_bench):
        """Anti-vacuity: the clearing is scoped to a vendor CHANGE. The daily
        refresh loop re-writing yfinance closes over yfinance bars must leave
        the OHLV alone, or every sweep would evict the OHLCV cache and the
        next panel read would re-fetch the whole universe."""
        from archimedes.models.asset_daily_bars import AssetDailyBar
        from archimedes.services import market_data_provider as mdp

        yf, _tg = flip_bench
        start, end = self._window()
        get_provider(seam="daily").get_daily_ohlcv("SPY", start, end)
        get_provider(seam="daily").get_daily_close_batch({"sSPY": "SPY"}, period="1mo")

        session = mdp._default_session_factory()
        try:
            rows = session.query(AssetDailyBar).filter(AssetDailyBar.symbol == "SPY").all()
        finally:
            session.close()

        assert rows
        assert all(r.source == "yfinance" and r.open == yf.open and r.volume == yf.volume for r in rows)

    def test_the_read_filter_still_rejects_the_other_vendors_row(self, flip_bench, monkeypatch):
        """The read half, confirmed rather than assumed: with a full-OHLCV
        yfinance cache and NO intervening close-only write, a Tiingo caller
        still misses and re-fetches. Both halves are load-bearing — the read
        filter catches an untouched foreign row, the write fix catches a
        re-stamped one."""
        yf, tg = flip_bench
        start, end = self._window()
        get_provider(seam="daily").get_daily_ohlcv("SPY", start, end)

        monkeypatch.setenv("MARKET_DATA_DAILY_PROVIDER", "tiingo")
        yf.ohlcv_calls.clear()
        panel = get_provider(seam="daily").get_daily_ohlcv("SPY", start, end)

        assert tg.ohlcv_calls  # source filter → miss → the new vendor answered
        assert yf.ohlcv_calls == []
        assert panel["Close"].tolist() == [tg.close] * len(panel)
