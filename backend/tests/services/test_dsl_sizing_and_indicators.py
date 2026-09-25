"""Behavioural guards for the DSL's enum surface — sizing, indicators, rejections.

Three defects, all of the same shape: the DSL *advertised* something the
interpreter did not do, and said nothing about it.

  1. ``position_sizing.type`` accepted ``equal_weight`` and ``inverse_vol`` and
     then fell through an ``else`` into the same full-invest path as
     ``full_invested_when_in_market``. A spec asking for 1/N exposure got 99%.
  2. ``realized_vol`` was in ``INDICATOR_NAMES`` (so ``validate_strategy_spec``
     accepted it) but ``interpret_spec`` raised ``DSLError`` on it — legal to
     write, fatal to run.
  3. A margin-rejected order was dropped by the broker with no fill, no
     exception and no log line, so the equity curve showed a flat bar that
     looked exactly like "the entry condition was false".

Every test here runs the real interpreter through a real ``bt.Cerebro`` on a
constructed price path — no mocks of the thing under test. Hermetic: no network,
no DB, no ``.env``.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
import typing

import backtrader as bt
import numpy as np
import pandas as pd
import pytest
from archimedes.services.dsl_to_backtrader import (
    SUPPORTED_INDICATORS,
    interpret_spec,
)
from archimedes.services.strategy_dsl import (
    INDICATOR_NAMES,
    POSITION_SIZING_KEYS,
    POSITION_SIZING_TYPES,
    DSLError,
    validate_strategy_spec,
)

_LOGGER_NAME = "archimedes.services.dsl_to_backtrader"
_CASH = 100_000.0

# Entry that is always true / exit that is never true, so a sizing test observes
# the SIZE decision and nothing else.
_ALWAYS = {"gt": ["close", 0]}
_NEVER = {"lt": ["close", 0]}


class _PositionTrace(bt.Analyzer):
    """Per-bar (size, close, portfolio value) — the raw material for exposure."""

    def start(self) -> None:
        self.rows: list[tuple[float, float, float]] = []

    def next(self) -> None:
        self.rows.append(
            (
                float(self.strategy.position.size),
                float(self.strategy.data.close[0]),
                float(self.strategy.broker.getvalue()),
            )
        )

    def get_analysis(self) -> dict:
        return {
            "rows": self.rows,
            "sizes": [r[0] for r in self.rows],
        }


def _frame(closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    """OHLCV frame from a close path (open defaults to close — no gap)."""
    opens = list(closes) if opens is None else opens
    n = len(closes)
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) * 1.0001 for o, c in zip(opens, closes, strict=True)],
            "low": [min(o, c) * 0.9999 for o, c in zip(opens, closes, strict=True)],
            "close": closes,
            "volume": [1_000_000] * n,
        },
        index=pd.date_range("2020-01-01", periods=n, freq="D"),
    )


def _run(spec_dict: dict, frame: pd.DataFrame, *, cash: float = _CASH, **strategy_kwargs):
    """Interpret + run one spec over one frame. Returns (strategy, cerebro)."""
    cls = interpret_spec(validate_strategy_spec(spec_dict))
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.addstrategy(cls, **strategy_kwargs)
    cerebro.addanalyzer(_PositionTrace, _name="positions")
    cerebro.broker.setcash(cash)
    strat = cerebro.run()[0]
    return strat, cerebro


def _targets_of_class(strategy_cls, frame: pd.DataFrame, *, cash: float = _CASH, **strategy_kwargs) -> list[float]:
    """Every ``order_target_percent`` target an interpreted class asks for."""
    recorded: list[float] = []

    class _Spy(strategy_cls):  # type: ignore[misc,valid-type]
        def order_target_percent(self, data=None, target=0.0, **kwargs):
            recorded.append(float(target))
            return super().order_target_percent(data=data, target=target, **kwargs)

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=frame))
    cerebro.addstrategy(_Spy, **strategy_kwargs)
    cerebro.broker.setcash(cash)
    cerebro.run()
    return recorded


def _requested_targets(spec_dict: dict, frame: pd.DataFrame, *, cash: float = _CASH, **strategy_kwargs) -> list[float]:
    """Every ``order_target_percent`` target the strategy ASKS the broker for.

    Measuring the request rather than the fill is load-bearing for the
    slot-invariance assertion. A target above 1.0 is a leverage request the cash
    broker refuses outright, so a fill-based comparison would read 0.0 on exactly
    the inputs where the clamp bug lived and the guard would prove nothing.
    """
    return _targets_of_class(interpret_spec(validate_strategy_spec(spec_dict)), frame, cash=cash, **strategy_kwargs)


def _entry_exposure(strat) -> float:
    """Fraction of portfolio value in the instrument on the FIRST bar it held it.

    Measuring at entry, not at the end of the run, is deliberate. Sizing happens
    once per entry and the share count is then frozen while the price drifts, so
    a last-bar reading of a volatile path mostly measures the drift. An earlier
    draft of this file "passed" the inverse-vol comparison for exactly that
    reason — the calm and volatile runs had taken the SAME 50% slot and only the
    price paths differed. Returns 0.0 if the strategy never held anything.
    """
    for size, close, value in strat.analyzers.positions.get_analysis()["rows"]:
        if size != 0 and value > 0:
            return size * close / value
    return 0.0


def _sizing_spec(ps: dict, universe: list[str], *, warmup_bars: int | None = None) -> dict:
    """A spec whose only interesting decision is the position size.

    ``warmup_bars`` inserts an always-true condition on ``sma_<warmup_bars>``
    (close prices are positive, so ``sma_N > 0`` is a tautology) purely to delay
    the first entry past a chosen bar. The vol-scaling branches need >20 bars of
    history before they can scale at all; without the delay they enter on bar 1
    and take the unscaled fallback, which would make a vol test measure nothing.
    """
    entry = _ALWAYS if warmup_bars is None else {"gt": [f"sma_{warmup_bars}", 0]}
    return {
        "name": f"sizing-{ps['type']}",
        "asset_universe": universe,
        "rebalance_frequency": "daily",
        "entry": entry,
        "exit": _NEVER,
        "position_sizing": ps,
        "source_arxiv_ids": ["0000.0001"],
        "look_ahead_safe": True,
    }


# ── Guard 1: equal_weight is not full-invest ──────────────────────────────────


class TestEqualWeightSizing:
    """``equal_weight`` must size one slot of the universe, not the whole account."""

    UNIVERSE = ["SPY", "QQQ", "IWM", "EFA"]  # N = 4 → one slot = 25%

    @pytest.fixture
    def flat_frame(self):
        # Constant price: exposure arithmetic is exact, so a 0.25-vs-0.99 gap
        # cannot be explained by drift.
        return _frame([100.0] * 40)

    def test_equal_weight_targets_one_slot(self, flat_frame):
        strat, _ = _run(_sizing_spec({"type": "equal_weight"}, self.UNIVERSE), flat_frame)
        exposure = _entry_exposure(strat)
        # 1/4 of the account, less the 0.99 exposure buffer.
        assert exposure == pytest.approx(0.25 * 0.99, abs=0.01), f"equal_weight held {exposure:.4f} of the account"

    def test_equal_weight_differs_from_full_invest(self, flat_frame):
        """The regression itself: the two used to be the same code path."""
        eq, _ = _run(_sizing_spec({"type": "equal_weight"}, self.UNIVERSE), flat_frame)
        full, _ = _run(
            _sizing_spec({"type": "full_invested_when_in_market"}, self.UNIVERSE),
            flat_frame,
        )
        eq_exposure = _entry_exposure(eq)
        full_exposure = _entry_exposure(full)

        assert full_exposure == pytest.approx(0.99, abs=0.01)
        assert eq_exposure < full_exposure / 2, (
            f"equal_weight ({eq_exposure:.4f}) is indistinguishable from full invest "
            f"({full_exposure:.4f}) — the sizing branch is falling through again"
        )

    def test_sleeve_caller_overrides_slots_to_one(self, flat_frame):
        """The documented seam: a caller that already split the cash passes 1.

        ``run_dsl_backtest_portfolio`` capitalizes each sleeve at ``cash/N`` and
        runs this same strategy once per ticker. If the strategy ALSO divided by
        N the sleeve would size at 1/N². ``universe_slots=1`` is how the runner
        says "the split already happened".
        """
        strat, _ = _run(
            _sizing_spec({"type": "equal_weight"}, self.UNIVERSE),
            flat_frame,
            universe_slots=1,
        )
        assert _entry_exposure(strat) == pytest.approx(0.99, abs=0.01)

    def test_single_asset_universe_is_fully_invested(self, flat_frame):
        """N=1 → one slot IS the whole account; no silent haircut."""
        strat, _ = _run(_sizing_spec({"type": "equal_weight"}, ["SPY"]), flat_frame)
        assert _entry_exposure(strat) == pytest.approx(0.99, abs=0.01)


# ── Guard 1b: inverse_vol actually inverts vol ────────────────────────────────


def _vol_path(daily_sigma: float, n: int = 80, seed: int = 7) -> pd.DataFrame:
    """Deterministic price path with a chosen daily return volatility."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0.0, daily_sigma, n)
    closes = list(100.0 * np.cumprod(1.0 + shocks))
    return _frame(closes)


class TestInverseVolSizing:
    """``inverse_vol`` = one slot, scaled by reference vol / realized vol."""

    UNIVERSE = ["SPY", "QQQ"]  # N = 2 → slot = 50%
    # Delay the first entry past the 20-bar sizing lookback so the scale is real.
    WARMUP = 25

    def _spec(self):
        return _sizing_spec(
            {"type": "inverse_vol", "reference_vol_annual": 0.15},
            self.UNIVERSE,
            warmup_bars=self.WARMUP,
        )

    def test_calm_asset_gets_more_than_volatile_asset(self):
        calm, _ = _run(self._spec(), _vol_path(0.002))  # ~3% annualized
        wild, _ = _run(self._spec(), _vol_path(0.04))  # ~63% annualized
        calm_exposure = _entry_exposure(calm)
        wild_exposure = _entry_exposure(wild)

        assert calm_exposure > wild_exposure, (
            f"inverse_vol sized the calm asset at {calm_exposure:.4f} and the volatile one at "
            f"{wild_exposure:.4f} — it is not responding to realized vol"
        )
        # The calm path hits the 2.0x scale cap: 0.5 slot * 2.0 = 1.0, buffered.
        assert calm_exposure == pytest.approx(0.99, abs=0.02)
        # The volatile path is scaled well below its 50% slot.
        assert wild_exposure < 0.25

    def test_inverse_vol_differs_from_full_invest(self):
        frame = _vol_path(0.04)
        inv, _ = _run(self._spec(), frame)
        full, _ = _run(
            _sizing_spec(
                {"type": "full_invested_when_in_market"},
                self.UNIVERSE,
                warmup_bars=self.WARMUP,
            ),
            frame,
        )
        assert _entry_exposure(inv) < _entry_exposure(full) / 2

    def test_reference_vol_must_be_positive_when_present(self):
        bad = _sizing_spec({"type": "inverse_vol", "reference_vol_annual": 0.0}, self.UNIVERSE)
        with pytest.raises(DSLError, match="reference_vol_annual"):
            validate_strategy_spec(bad)

    def test_slot_invariance_of_the_requested_target(self):
        """The same asset must get the same share of the account either way.

        The interpreter reads one feed, and the 1/N universe split is expressed
        in one of two places: INSIDE the strategy (``universe_slots=N``, the
        single-feed runner hands it the whole account) or OUTSIDE it
        (``universe_slots=1``, the sleeve runner hands it ``cash/N``). Those are
        two spellings of one allocation, so the per-name share of the whole
        account — ``target × (account share this run controls)`` — must be equal.

        It was not. The scale cap was applied to the slot-multiplied product
        (``min(slot * scale, 1.0)``), which is inert at ``slots=N`` for N ≥ 2 and
        truncating at ``slots=1``: the calm path below asked for 0.99 of the
        whole account through the single-feed runner and 0.99 of a HALF account
        through the sleeve runner. Same spec, same prices, 2× the exposure.
        """
        frame = _vol_path(0.002)  # ~3% annualized → scale pinned at the 2.0 cap
        n = len(self.UNIVERSE)

        single_feed = _requested_targets(self._spec(), frame)  # slots defaults to N
        sleeve = _requested_targets(self._spec(), frame, universe_slots=1)

        assert single_feed and sleeve, "no sizing order was placed — the test measured nothing"
        # Anti-vacuity: if the calm path never reached a scale above 1.0 the old
        # clamp would not have bitten and this comparison would pass either way.
        assert sleeve[0] > 1.0, f"sleeve requested {sleeve[0]:.4f} — the clamp under test never engages here"
        assert sleeve[0] / n == pytest.approx(single_feed[0], rel=1e-12), (
            f"single-feed run asked for {single_feed[0]:.4f} of the whole account; the sleeve run asked for "
            f"{sleeve[0]:.4f} of a 1/{n} account = {sleeve[0] / n:.4f} — the cap is being applied after the "
            "slot multiply again, so inverse_vol is not slot-invariant"
        )

    def test_the_slot_invariant_target_can_be_unfundable(self, caplog):
        """The honest cost of clamping the scale instead of the product.

        Slot-invariance means the sleeve path may ask for up to
        ``_VOL_SCALE_CAP`` × its own account, which the backtest's cash broker
        cannot fund — the same pre-existing defect ``volatility_target`` has (see
        ``test_volatility_target_can_request_unfundable_leverage``). Truncating it
        back to 1.0 is what made the two runners disagree, so it stays, and stays
        AUDIBLE: rejected, logged, flat. Pinned here so the leverage request is
        never mistaken for "the entry condition was false".
        """
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            strat, _ = _run(self._spec(), _vol_path(0.002), universe_slots=1)

        assert _entry_exposure(strat) == 0.0, "the 2x request became fundable — re-check this pin"
        assert [r for r in caplog.records if r.name == _LOGGER_NAME and "Margin" in r.getMessage()], (
            "an unfundable inverse_vol sleeve went flat for the whole run and said nothing about why"
        )


# ── Guard 2: realized_vol interprets and drives signals ───────────────────────


def _regime_frame(n_calm: int = 60, n_wild: int = 60) -> pd.DataFrame:
    """Calm regime followed by a violent one — realized_vol must cross."""
    rng = np.random.default_rng(11)
    calm = rng.normal(0.0, 0.001, n_calm)
    wild = rng.normal(0.0, 0.05, n_wild)
    closes = list(100.0 * np.cumprod(1.0 + np.concatenate([calm, wild])))
    return _frame(closes)


class TestRealizedVolIndicator:
    """``realized_vol_N`` was validator-legal and interpreter-fatal."""

    SPEC = {
        "name": "calm-regime-only",
        "asset_universe": ["SPY"],
        "rebalance_frequency": "daily",
        # Hold while the last 10 bars were calm; step aside when they were not.
        "entry": {"lt": ["realized_vol_10", 0.30]},
        "exit": {"gt": ["realized_vol_10", 0.30]},
        "position_sizing": {"type": "full_invested_when_in_market"},
        "source_arxiv_ids": ["1704.03022"],
        "look_ahead_safe": True,
    }

    def test_spec_runs_without_a_dsl_error(self):
        """The old defect was fatal at RUN time, so the guard must run the spec.

        ``interpret_spec`` only closes over the spec and returns a class; it
        never touches ``_make_indicator``, which is called from
        ``DSLStrategy.__init__`` when Cerebro instantiates the strategy. An
        earlier version of this test asserted ``issubclass(cls, bt.Strategy)``
        and therefore passed against the very interpreter that raised
        ``DSLError('unsupported indicator: realized_vol')`` on every run — it
        guarded nothing. Feeding the class to a real Cerebro is what reaches the
        line the defect lived on.
        """
        strat, _ = _run(self.SPEC, _regime_frame(n_calm=30, n_wild=30))
        values = [float(strat._indicators["realized_vol_10"][-i]) for i in range(5)]
        assert all(np.isfinite(v) and v >= 0 for v in values), f"realized_vol produced no usable values: {values}"

    def test_indicator_drives_entry_and_exit(self):
        strat, _ = _run(self.SPEC, _regime_frame())
        sizes = strat.analyzers.positions.get_analysis()["sizes"]

        assert any(s > 0 for s in sizes), "never entered — realized_vol produced no actionable signal"
        assert any(s == 0 for s in sizes[1:]), "never exited — the vol regime switch was not seen"
        # The calm regime comes first: exposure must START on and END off.
        assert sizes[-1] == 0, "still holding through the violent regime"

    def test_matches_the_live_evaluator_formula(self):
        """Backtest value == live value on the same series, to float precision.

        The live evaluator computes ``realized_vol`` as
        ``prices.pct_change().tail(N).std() * sqrt(252)``. pandas ``.std()`` is
        ddof=1; backtrader's built-in StandardDeviation is ddof=0. Using the
        built-in would have put the graded backtest and the live signal ~5% apart
        at N=20 — the same silent divergence class as the momentum (F1) and RSI
        (F5) findings.
        """
        from archimedes.services.strategy_signal_evaluator import _compute_indicator_value

        period = 10
        frame = _regime_frame(n_calm=40, n_wild=40)

        captured: list[float] = []

        class _Probe(bt.Strategy):
            def __init__(self) -> None:
                from archimedes.services.dsl_to_backtrader import _make_indicator

                self.rv = _make_indicator(self.data.close, "realized_vol", period)

            def next(self) -> None:
                captured.append(float(self.rv[0]))

        cerebro = bt.Cerebro(stdstats=False)
        cerebro.adddata(bt.feeds.PandasData(dataname=frame))
        cerebro.addstrategy(_Probe)
        cerebro.run()

        expected = _compute_indicator_value("realized_vol", period, frame["close"].reset_index(drop=True))
        assert captured, "indicator produced no values"
        assert captured[-1] == pytest.approx(expected, rel=1e-12)

    def test_warmup_accounts_for_the_differencing_bar(self):
        """N returns need N+1 prices; the first value must not appear early."""
        period = 10
        frame = _frame([100.0 + i for i in range(30)])

        first_valid: list[int] = []

        class _Probe(bt.Strategy):
            def __init__(self) -> None:
                from archimedes.services.dsl_to_backtrader import _make_indicator

                self.rv = _make_indicator(self.data.close, "realized_vol", period)

            def next(self) -> None:
                if not first_valid:
                    first_valid.append(len(self))

        cerebro = bt.Cerebro(stdstats=False)
        cerebro.adddata(bt.feeds.PandasData(dataname=frame))
        cerebro.addstrategy(_Probe)
        cerebro.run()

        assert first_valid == [period + 1]


# ── Guard 3: a refused order is audible ───────────────────────────────────────


class TestRejectedOrdersAreLogged:
    """Margin rejections used to leave no trace at all."""

    SPEC = {
        "name": "margin-canary",
        "asset_universe": ["SPY"],
        "rebalance_frequency": "daily",
        "entry": _ALWAYS,
        "exit": _NEVER,
        "position_sizing": {"type": "full_invested_when_in_market"},
        "source_arxiv_ids": ["0000.0002"],
        "look_ahead_safe": True,
    }

    @staticmethod
    def _gap_up_frame() -> pd.DataFrame:
        """Bar 1 closes at 100; bar 2 OPENS at 100_000.

        backtrader fills a market order at the next bar's open, so the size sized
        off the 100 close costs ~1000x the account at fill time → Margin.
        """
        closes = [100.0, 100_000.0, 100_000.0, 100_000.0]
        opens = [100.0, 100_000.0, 100_000.0, 100_000.0]
        return _frame(closes, opens)

    def test_margin_rejection_logs_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            _run(self.SPEC, self._gap_up_frame())

        rejections = [r for r in caplog.records if r.name == _LOGGER_NAME and "Margin" in r.getMessage()]
        assert rejections, (
            "a margin-rejected order produced no WARNING — the strategy went quietly "
            f"flat. Captured: {[r.getMessage() for r in caplog.records]}"
        )
        message = rejections[0].getMessage()
        assert "margin-canary" in message, "the warning does not name the strategy"
        assert "BUY" in message, "the warning does not say which side was refused"

    def test_clean_run_logs_no_rejection_warning(self, caplog):
        """The guard must not fire on a normal run (else it means nothing)."""
        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            _run(self.SPEC, _frame([100.0] * 20))

        assert not [r for r in caplog.records if r.name == _LOGGER_NAME and "Margin" in r.getMessage()]


# ── Guard 4: an unknown position_sizing key is refused, not ignored ───────────


class TestPositionSizingRejectsUnknownKeys:
    """A closed enum inside an open dict is not a closed vocabulary.

    ``{"type": "inverse_vol", "reference_vol": 0.30}`` — the plausible
    misspelling of ``reference_vol_annual`` — used to validate, interpret,
    backtest and publish, sizing at the 0.15 DEFAULT with the author's 0.30
    discarded and no log line anywhere. Same shape as the three defects this file
    already guards: the DSL accepted something and quietly did something else.
    """

    UNIVERSE: typing.ClassVar[list[str]] = ["SPY", "QQQ"]

    @pytest.mark.parametrize(
        ("ps", "bad_key"),
        [
            ({"type": "inverse_vol", "reference_vol": 0.30}, "reference_vol"),
            ({"type": "inverse_vol", "reference_vol_annual": 0.30, "annual_pct": 0.2}, "annual_pct"),
            ({"type": "equal_weight", "weight": 0.25}, "weight"),
            ({"type": "full_invested_when_in_market", "leverage": 2}, "leverage"),
            ({"type": "volatility_target", "annual_pct": 0.15, "cap": 3.0}, "cap"),
        ],
    )
    def test_unknown_key_is_rejected_by_name(self, ps, bad_key):
        with pytest.raises(DSLError, match=bad_key):
            validate_strategy_spec(_sizing_spec(ps, self.UNIVERSE))

    @pytest.mark.parametrize(
        "ps",
        [
            {"type": "full_invested_when_in_market"},
            {"type": "equal_weight"},
            {"type": "inverse_vol"},
            {"type": "inverse_vol", "reference_vol_annual": 0.30},
            {"type": "volatility_target", "annual_pct": 0.15},
        ],
    )
    def test_every_documented_shape_still_validates(self, ps):
        """The other half of a rejection guard: it must accept the legal set.

        Without this, tightening the check into "reject everything" would pass
        the tests above and break every real spec.
        """
        assert validate_strategy_spec(_sizing_spec(ps, self.UNIVERSE)).position_sizing["type"] == ps["type"]

    def test_the_rejected_spelling_would_otherwise_have_been_silently_ignored(self):
        """Names the exact damage: the misspelled key changes NOTHING at runtime.

        ``reference_vol`` is not read by ``interpret_spec``, so a spec carrying it
        sizes off ``DEFAULT_INVERSE_VOL_REFERENCE``. This asserts that the
        interpreter ignores it — which is why the validator has to be the one to
        object, and why "the interpreter would have caught it" is not an argument
        for leaving the dict open.
        """
        from archimedes.services.dsl_to_backtrader import DEFAULT_INVERSE_VOL_REFERENCE

        frame = _vol_path(0.02)
        stated = _sizing_spec(
            {"type": "inverse_vol", "reference_vol_annual": DEFAULT_INVERSE_VOL_REFERENCE},
            self.UNIVERSE,
            warmup_bars=25,
        )
        # The same spec with the correct key swapped for the misspelling. Built
        # via dataclasses.replace because validate_strategy_spec now (correctly)
        # refuses to produce it — which is the point of the guard.
        misspelled = dataclasses.replace(
            validate_strategy_spec(stated),
            position_sizing={"type": "inverse_vol", "reference_vol": 0.30},
        )

        default_targets = _requested_targets(stated, frame)
        # 0.30 is double the default reference; had the key been read, the
        # requested target would have doubled with it.
        misspelled_targets = _targets_of_class(interpret_spec(misspelled), frame)

        assert default_targets, "no order placed — the comparison would be vacuous"
        assert misspelled_targets[0] == pytest.approx(default_targets[0], rel=1e-12), (
            "the misspelled key changed the sizing — rewrite this test, the premise moved"
        )


def test_position_sizing_key_table_covers_every_type():
    """``POSITION_SIZING_KEYS`` and ``POSITION_SIZING_TYPES`` cannot drift.

    A new sizing type without a key row would ``KeyError`` inside the validator;
    a stale row would silently permit keys for a type that no longer exists.
    """
    assert set(POSITION_SIZING_KEYS) == set(POSITION_SIZING_TYPES)
    for sizing_type, keys in POSITION_SIZING_KEYS.items():
        assert "type" in keys, f"{sizing_type} must accept its own discriminator"


# ── The enum surface itself ───────────────────────────────────────────────────


def test_every_validated_indicator_is_interpretable():
    """``INDICATOR_NAMES`` (what validates) == ``SUPPORTED_INDICATORS`` (what runs).

    Any drift between these two reintroduces the ``realized_vol`` defect: a name
    a spec may legally use and the interpreter then dies on.
    """
    assert set(INDICATOR_NAMES) == set(SUPPORTED_INDICATORS)


@pytest.mark.parametrize("ps_type", sorted(POSITION_SIZING_TYPES))
def test_every_sizing_type_takes_a_position(ps_type):
    """No sizing type may be a no-op, and none may be a silent alias of another.

    Each type is run on a path it can actually be funded on: the two vol-scaling
    types are given a ~32%-annualized path so their scale lands below 1.0. See
    ``test_volatility_target_can_request_unfundable_leverage`` for what happens
    when it lands above 1.0.
    """
    ps = {"type": ps_type}
    if ps_type == "volatility_target":
        ps["annual_pct"] = 0.15
    frame = _vol_path(0.02) if ps_type in ("volatility_target", "inverse_vol") else _frame([100.0] * 60)
    strat, _ = _run(_sizing_spec(ps, ["SPY", "QQQ", "IWM", "EFA"], warmup_bars=25), frame)
    assert _entry_exposure(strat) > 0.0, f"{ps_type} never took a position"


def test_volatility_target_can_request_unfundable_leverage(caplog):
    """A pre-existing defect this change makes AUDIBLE rather than fixing.

    ``volatility_target`` multiplies the full-cash size by up to
    ``_VOL_SCALE_CAP`` (2.0). On a calm series that is a 2x leverage request the
    default cash broker cannot fund, so EVERY order is margin-rejected and the
    strategy stays flat for the entire run — previously reporting an all-cash
    equity curve that was indistinguishable from "the signal said stay out".

    This test pins the behaviour and asserts it is now logged. Changing the
    sizing (clamping the scale, or enabling margin) would move published
    volatility_target metrics, so it is deliberately NOT done here.
    """
    spec = _sizing_spec({"type": "volatility_target", "annual_pct": 0.15}, ["SPY"], warmup_bars=25)
    # +0.1/day on ~100 → ~1.5% annualized vol → scale pinned at the 2.0 cap.
    calm_trend = _frame([100.0 + 0.1 * i for i in range(60)])

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        strat, _ = _run(spec, calm_trend)

    assert _entry_exposure(strat) == 0.0, "the 2x request became fundable — re-check this pin"
    assert [r for r in caplog.records if r.name == _LOGGER_NAME and "Margin" in r.getMessage()], (
        "the strategy was flat for the whole run and said nothing about why"
    )


def test_sleeve_runner_does_not_apply_the_equal_split_twice():
    """The seam, asserted on the RUNNER rather than on the strategy parameter.

    ``run_dsl_backtest_portfolio`` capitalizes each of N sleeves at ``cash/N``
    and runs the same strategy once per ticker. An ``equal_weight`` spec must
    therefore produce the SAME portfolio as a ``full_invested_when_in_market``
    spec: each sleeve is fully invested in its own share either way. If the
    runner stops passing ``universe_slots=1``, the strategy divides by N a second
    time, every sleeve sizes at 1/N², and the equal-weight portfolio's return
    collapses to a fraction of the full-invest one.
    """
    from archimedes.services._fusion_helpers import _csv_data_feed
    from archimedes.services.fusion_evaluator import run_dsl_backtest_portfolio

    fixture = pathlib.Path(__file__).parent.parent / "fixtures" / "spy_ohlcv_2004_2026.csv"
    factories = {
        "SPY": lambda: _csv_data_feed(fixture),
        "SPY2": lambda: _csv_data_feed(fixture),
    }
    base = {
        "name": "sleeve-seam",
        "asset_universe": ["SPY", "SPY2"],
        "rebalance_frequency": "monthly",
        "entry": {"gt": ["close", "sma_200"]},
        "exit": {"lt": ["close", "sma_200"]},
        "source_arxiv_ids": ["0706.1497"],
        "look_ahead_safe": True,
    }

    equal = run_dsl_backtest_portfolio(
        validate_strategy_spec({**base, "position_sizing": {"type": "equal_weight"}}),
        factories,
        label="csv:fixture",
    )
    full = run_dsl_backtest_portfolio(
        validate_strategy_spec({**base, "position_sizing": {"type": "full_invested_when_in_market"}}),
        factories,
        label="csv:fixture",
    )

    assert full.equity_curve[-1] > full.equity_curve[0], "fixture never made money — assertion would be vacuous"
    assert equal.equity_curve[-1] == pytest.approx(full.equity_curve[-1], rel=0.01), (
        f"equal_weight sleeves ended at {equal.equity_curve[-1]:.2f} vs full-invest "
        f"{full.equity_curve[-1]:.2f} — the 1/N split is being applied twice"
    )


def test_equal_weight_and_inverse_vol_are_not_aliases_of_full_invest():
    """One assertion covering the whole class of defect this file exists for."""
    universe = ["SPY", "QQQ", "IWM", "EFA"]
    frame = _vol_path(0.02)

    exposures = {}
    for ps in (
        {"type": "full_invested_when_in_market"},
        {"type": "equal_weight"},
        {"type": "inverse_vol", "reference_vol_annual": 0.15},
    ):
        strat, _ = _run(_sizing_spec(ps, universe, warmup_bars=25), frame)
        exposures[ps["type"]] = _entry_exposure(strat)

    full = exposures["full_invested_when_in_market"]
    assert exposures["equal_weight"] != pytest.approx(full, abs=0.05), exposures
    assert exposures["inverse_vol"] != pytest.approx(full, abs=0.05), exposures
