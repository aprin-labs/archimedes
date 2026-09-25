"""The market-data proof script must be able to say NO (#1798).

`scripts/verify_market_data.py` exists to produce the verified-pull record
`docs/claims-ledger.md` lacks for the row "paid analysis runs on licensed
data". A proof script that only ever prints OK is not a proof — it is a
decoration with an exit code. So every test here is paired with the wrong
answer it has to reject: too few bars, the wrong first bar, an empty frame, a
provider that raises (the shape a missing `TIINGO_API_TOKEN` actually takes,
since the provider refuses to fall back to yfinance).

Hermetic by construction: the provider seam is replaced with a stub that
returns a frame from memory, so nothing here reaches Tiingo, Yahoo, Postgres or
the network. Two things are NOT stubbed. The arithmetic of the known window is
checked against the calendar, because a hand-edited window whose expected row
count no longer matches would make every future run's verdict meaningless. And
`TestItSpeaksTheRealSeamApi` runs the script's own calls against the REAL
`market_data_provider` functions with only the vendor registry swapped —
because a stub of `get_provider` accepts whatever the script passes it, so
every other test here stayed green when #1798 made `seam` a required argument
and the operator's run died with a `TypeError` on line one.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_market_data.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_market_data", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_market_data"] = module
    spec.loader.exec_module(module)
    return module


vmd = _load()


def _frame(dates: list[str]) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.DataFrame(
        {
            "Open": [1.0] * len(index),
            "High": [2.0] * len(index),
            "Low": [0.5] * len(index),
            "Close": [1.5] * len(index),
            "Volume": [100] * len(index),
        },
        index=index,
    )


def _known_window_dates() -> list[str]:
    return [str(d.date()) for d in pd.bdate_range(vmd.EXPECTED_FIRST_BAR, vmd.EXPECTED_LAST_BAR)]


class _StubVendor:
    """Stands in for TiingoProvider/YFinanceProvider at the seam."""

    def __init__(self, frame=None, raises: Exception | None = None) -> None:
        self.frame = frame
        self.raises = raises
        self.calls: list[tuple[str, str, str]] = []

    def get_daily_ohlcv(self, ticker: str, start: str, end: str):
        self.calls.append((ticker, start, end))
        if self.raises is not None:
            raise self.raises
        return self.frame


class _StubCachingWrapper(_StubVendor):
    """The production shape: a cache wrapper around an inner vendor."""

    def __init__(self, inner: _StubVendor, frame=None) -> None:
        super().__init__(frame=frame)
        self._inner = inner


def _install(monkeypatch, provider, *, vendor: str = "tiingo") -> list[str]:
    """Stub the seam. Returns the list of seam names the script asked for, so a
    caller can assert it named one rather than taking whatever was handed over.

    The stubs take the arguments the real functions take: `seam` keyword-only
    on `get_provider`, positional on `provider_name`. Keep it that way — a
    permissive `lambda *a, **k` here would re-open exactly the hole
    `TestItSpeaksTheRealSeamApi` exists to close.
    """
    asked: list[str] = []

    def _get_provider(*, seam: str):
        asked.append(seam)
        return provider

    monkeypatch.setattr(vmd.mdp, "get_provider", _get_provider)
    monkeypatch.setattr(vmd.mdp, "provider_name", lambda seam: vendor)
    return asked


def _run(**kwargs) -> tuple[int, str]:
    out = io.StringIO()
    code = vmd.verify(
        symbol=kwargs.pop("symbol", vmd.DEFAULT_SYMBOL),
        start=kwargs.pop("start", vmd.DEFAULT_START),
        end=kwargs.pop("end", vmd.DEFAULT_END),
        out=out,
        **kwargs,
    )
    return code, out.getvalue()


class TestTheKnownWindowIsInternallyConsistent:
    """The expected answer must follow from the window, not from memory."""

    def test_the_expected_row_count_is_the_windows_weekday_count(self) -> None:
        """Demonstrated to reject: moving EXPECTED_LAST_BAR a day earlier while
        leaving EXPECTED_ROWS at 10 fails here, before any run can pass on a
        number nobody re-derived. (Weekdays are checkable; the no-holiday claim
        is not, and is stated in the script's docstring with its dates.)"""
        weekdays = len(pd.bdate_range(vmd.EXPECTED_FIRST_BAR, vmd.EXPECTED_LAST_BAR))
        assert weekdays == vmd.EXPECTED_ROWS

    def test_the_end_bound_is_exclusive_of_the_last_bar(self) -> None:
        """`get_daily_ohlcv` takes `[start, end)`. An END equal to the last bar
        would silently ask for nine bars and expect ten."""
        assert date.fromisoformat(vmd.DEFAULT_END) == date.fromisoformat(vmd.EXPECTED_LAST_BAR) + timedelta(days=1)
        assert vmd.DEFAULT_START == vmd.EXPECTED_FIRST_BAR

    def test_the_window_is_in_the_past(self) -> None:
        """A window running to today would have a different answer every day."""
        assert date.fromisoformat(vmd.DEFAULT_END) < date.today()


class TestItSaysYes:
    def test_the_expected_window_exits_zero_and_names_the_vendor(self, monkeypatch) -> None:
        vendor = _StubVendor(frame=_frame(_known_window_dates()))
        _install(monkeypatch, vendor, vendor="tiingo")

        code, text = _run()

        assert code == 0
        assert "provider     : tiingo" in text
        assert f"rows         : {vmd.EXPECTED_ROWS}" in text
        assert f"first bar    : {vmd.EXPECTED_FIRST_BAR}" in text
        assert f"last bar     : {vmd.EXPECTED_LAST_BAR}" in text
        assert "result       : OK" in text
        # The seam was asked for exactly the documented, end-exclusive window.
        assert vendor.calls == [(vmd.DEFAULT_SYMBOL, vmd.DEFAULT_START, vmd.DEFAULT_END)]


class TestItSaysNo:
    def test_a_short_frame_is_rejected(self, monkeypatch) -> None:
        """The realistic vendor defect: a window silently missing a session."""
        _install(monkeypatch, _StubVendor(frame=_frame(_known_window_dates()[:-1])))

        code, text = _run()

        assert code == 1
        assert f"expected {vmd.EXPECTED_ROWS} rows, got {vmd.EXPECTED_ROWS - 1}" in text

    def test_a_shifted_window_is_rejected(self, monkeypatch) -> None:
        """Right count, wrong days — an off-by-one on the start bound."""
        shifted = [str(d.date()) for d in pd.bdate_range("2026-06-02", periods=vmd.EXPECTED_ROWS)]
        _install(monkeypatch, _StubVendor(frame=_frame(shifted)))

        code, text = _run()

        assert code == 1
        assert f"expected first bar {vmd.EXPECTED_FIRST_BAR}, got 2026-06-02" in text

    def test_an_empty_frame_is_rejected(self, monkeypatch) -> None:
        _install(monkeypatch, _StubVendor(frame=_frame([])))

        code, text = _run()

        assert code == 1
        assert "returned no bars" in text

    def test_a_raising_provider_is_rejected_and_names_the_error(self, monkeypatch) -> None:
        """The shape a missing token actually takes.

        `TiingoAPIKeyMissingError` is raised at provider construction and at
        every HTTP call; the provider does NOT fall back to yfinance. So an
        unwired container fails here loudly, which is the point — the operator
        must not read "no bars" and go looking for a market holiday.
        """
        _install(monkeypatch, _StubVendor(raises=RuntimeError("TIINGO_API_TOKEN is not set")))

        code, text = _run()

        assert code == 1
        assert "FAILED" in text
        assert "TIINGO_API_TOKEN is not set" in text

    def test_a_custom_window_reports_but_returns_no_verdict_on_the_count(self, monkeypatch) -> None:
        """A green tick on a number nobody checked is worse than no tick."""
        _install(monkeypatch, _StubVendor(frame=_frame(["2026-05-04", "2026-05-05"])))

        code, text = _run(start="2026-05-04", end="2026-05-06")

        assert code == 0
        assert "[custom]" in text
        assert "NOT checked" in text


class TestItNamesTheDailySeam:
    """Daily bars are the licensed-data cutover; intraday keeps its own vendor.

    A proof that read the intraday seam would print a vendor nobody flipped and
    be pasted on the issue as evidence for a seam it never touched.
    """

    def test_the_script_asks_the_daily_seam(self, monkeypatch) -> None:
        asked = _install(monkeypatch, _StubVendor(frame=_frame(_known_window_dates())))

        code, text = _run()

        assert code == 0
        assert asked == [vmd.mdp.DAILY_SEAM]
        assert f"seam         : {vmd.mdp.DAILY_SEAM}" in text

    def test_the_module_constant_is_the_daily_seam(self) -> None:
        """Not a literal inside `verify()`: the seam is the first thing a reader
        of this script has to be able to find."""
        assert vmd.SEAM == vmd.mdp.DAILY_SEAM


class TestItSpeaksTheRealSeamApi:
    """The one class here that does NOT stub `get_provider`/`provider_name`.

    Everything else in this file replaces the seam wholesale, so it proves the
    script's arithmetic and never its calling convention. Only the vendor
    registry is swapped below, which leaves `get_provider(seam=...)`,
    `provider_name(seam)` and the real wrapper nesting in the path.
    """

    def _stub_vendor(self, monkeypatch) -> _StubVendor:
        vendor = _StubVendor(frame=_frame(_known_window_dates()))
        monkeypatch.setitem(vmd.mdp._VENDOR_PROVIDERS, "yfinance", lambda: vendor)
        monkeypatch.delenv("MARKET_DATA_DAILY_PROVIDER", raising=False)
        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        return vendor

    def test_the_scripts_own_calls_satisfy_the_real_signatures(self, monkeypatch) -> None:
        """Demonstrated to reject: reverting `verify()` to `mdp.provider_name()`
        or `_provider` to `mdp.get_provider()` fails here with a TypeError,
        which is the failure an operator would otherwise have hit in prod."""
        vendor = self._stub_vendor(monkeypatch)

        code, text = _run(no_cache=True)

        assert code == 0, text
        assert "provider     : yfinance" in text
        assert vendor.calls == [(vmd.DEFAULT_SYMBOL, vmd.DEFAULT_START, vmd.DEFAULT_END)]

    def test_one_unwrap_is_the_cache_not_the_vendor(self, monkeypatch) -> None:
        """Anti-vacuity for the loop in `_provider`. `get_provider` nests two
        deep since #1798, so a single `_inner` hop lands on the cache — and
        `--no-cache` would have read through it while printing 'bypassed'."""
        vendor = self._stub_vendor(monkeypatch)

        routed = vmd.mdp.get_provider(seam=vmd.SEAM)

        assert isinstance(routed, vmd.mdp.SeamRoutedProvider)
        assert isinstance(routed._inner, vmd.mdp.CachingMarketDataProvider)
        assert vmd._provider(no_cache=True) is vendor
        # ...and the default keeps the whole production stack in the path.
        # (`get_provider` builds a fresh object per call, so this is a shape
        # assertion, not an identity one.)
        default = vmd._provider(no_cache=False)
        assert isinstance(default, vmd.mdp.SeamRoutedProvider)
        assert isinstance(default._inner, vmd.mdp.CachingMarketDataProvider)


class TestTheCacheFlag:
    def test_the_default_reads_through_the_production_wrapper(self, monkeypatch) -> None:
        inner = _StubVendor(frame=_frame(_known_window_dates()))
        wrapper = _StubCachingWrapper(inner, frame=_frame(_known_window_dates()))
        _install(monkeypatch, wrapper)

        code, text = _run()

        assert code == 0
        assert wrapper.calls and not inner.calls
        assert "through the production wrapper" in text

    def test_no_cache_reaches_the_inner_vendor(self, monkeypatch) -> None:
        """Proves the flag does what it says rather than only printing that it did."""
        inner = _StubVendor(frame=_frame(_known_window_dates()))
        wrapper = _StubCachingWrapper(inner, frame=_frame(_known_window_dates()))
        _install(monkeypatch, wrapper)

        code, text = _run(no_cache=True)

        assert code == 0
        assert inner.calls and not wrapper.calls
        assert "bypassed" in text

    def test_no_cache_against_an_unwrapped_provider_is_an_error_not_a_silent_pass(self, monkeypatch) -> None:
        """If `get_provider()` stops returning a wrapper, the flag must not
        quietly become a no-op that still prints 'bypassed'."""
        _install(monkeypatch, _StubVendor(frame=_frame(_known_window_dates())))

        code, text = _run(no_cache=True)

        assert code == 1
        assert "no longer returns a cache wrapper" in text


class TestTheCliContract:
    def test_defaults_run_the_known_window(self, monkeypatch) -> None:
        seen: dict[str, object] = {}

        def _fake_verify(**kwargs):
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(vmd, "verify", _fake_verify)
        assert vmd.main([]) == 0
        assert seen == {
            "symbol": vmd.DEFAULT_SYMBOL,
            "start": vmd.DEFAULT_START,
            "end": vmd.DEFAULT_END,
            "no_cache": False,
        }

    def test_the_failure_exit_code_reaches_the_shell(self, monkeypatch) -> None:
        """The record this produces is pasted on an issue; a proof that exits 0
        on a bad pull would be pasted just as confidently."""
        monkeypatch.setattr(vmd, "verify", lambda **_: 1)
        assert vmd.main([]) == 1


def test_the_script_is_executable_as_a_file() -> None:
    """It is run by an operator with `python scripts/verify_market_data.py`."""
    assert SCRIPT.is_file()
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env python3")
    assert "raise SystemExit(main())" in source


@pytest.mark.parametrize("name", ["EXPECTED_ROWS", "EXPECTED_FIRST_BAR", "EXPECTED_LAST_BAR"])
def test_the_expected_answer_is_a_module_constant(name: str) -> None:
    """Not a literal buried in a branch: the known answer has to be findable
    by whoever next has to decide whether the window is still right."""
    assert hasattr(vmd, name)
