"""Interpreter parity: the backtest and the live evaluator, same spec, same numbers.

THE ROOT CAUSE THIS EXISTS FOR. The 2026-08-03 divergence audit confirmed 21
ways the two interpreters of one strategy language disagreed — momentum was a
ratio in the backtest and a return live (F1, a published-number tautology),
RSI was Wilder in the backtest and Cutler live (F5), and so on. Each was a
symptom of the same structural fact: two implementations, zero tests holding
them together. This file is the tether. Every indicator the DSL exposes is
computed BOTH ways over the same series, per-bar, and must agree — so the
next convention drift fails CI instead of shipping into published numbers.

SCOPE, stated exactly (the honest part):
  * PINNED EQUAL, per-bar: sma, rsi (post-F5 Wilder), momentum (post-F1
    trailing return), flat-state daily entry decisions, the STATEFUL position
    path (post-F2 — the live evaluator replays the spec's FSM instead of
    recomputing "entry AND NOT exit", so a band strategy holds through the dead
    zone on both sides), and REBALANCE CADENCE (post-F3 — the live replay runs
    the same cadence gate the backtest does, so a monthly spec re-decides on the
    same bars in both interpreters). Each indicator's whole-series form is also
    pinned against its per-prefix scalar form, because the position replay reads
    the series and a live tick reads the scalar.
  * PINNED CONVERGENT: ema — backtrader seeds with an SMA, the live evaluator
    seeds ewm from the first value (audit F10). The seeds differ; the decay
    factor (1 - 2/(N+1))^k drives them together, so parity is asserted after
    a burn-in with a tight tolerance. If either side's seeding changes, the
    burn-in bound breaks here first.
  * PINNED EQUAL (was PINNED ASYMMETRIC, audit F6): realized_vol used to
    compute live and RAISE in the backtest. The backtest now implements it with
    the same ddof=1 estimator, so the asymmetry pin was retired and the
    indicator promoted into the parity block above.
  * PINNED WITH A ONE-BAR EXECUTION OFFSET: the stateful/cadence cases compare
    DECISIONS, not fills. backtrader submits the order on the decision bar and
    the broker fills it at the next bar's open, so the backtest's observable
    position at bar t+1 is the state its FSM decided at bar t. That offset is an
    execution convention, not an interpreter divergence, and the helpers below
    state it explicitly rather than hiding it in a fudge factor.
  * PINNED EQUAL: equal_weight and inverse_vol SIZING. Both sides call the same
    module-level functions (dsl_to_backtrader.slot_weight / sizing_realized_vol
    / inverse_vol_weight) over the same price window, so the weight is equal by
    construction rather than by coincidence — the live path used to return a
    flat 1.0 for both while the backtest sized them, an N-fold exposure gap on
    the same spec. ONE input class still diverges and is pinned BY NAME below
    (test_single_name_inverse_vol_divergence_is_pinned_by_name): a slot of 1.0
    (single-name universe, or the sleeve runners' universe_slots=1) on an asset
    calmer than the reference asks for up to 2.0x; the backtest broker
    margin-rejects it and goes flat, the live twin reports a clamped 1.0 and
    says so in its reason. Unreachable for two or more names. The comparison is
    made AT THE ENTRY BAR on purpose: the backtest sizes once at entry and
    freezes the share count, the live side recomputes each decision bar, so for
    a vol-scaled type the two agree there and drift afterwards. That is the
    unification plan's open D4 (sizing TIMING) question, which inverse_vol now
    inherits from volatility_target and which this file does not claim to close.
    equal_weight has no such seam — 1/N does not move.
  * DELIBERATELY OUT: volatility_target SIZING parity — the backtest sizes off a
    20-bar RMS of returns capped at 2.0x, the live path off a 22-bar sample
    stdev capped at 1.0x. Both sides now agree on WHEN a vol-targeted strategy
    is in the market (the cases below cover that); how much they buy once in is
    a separate, still-open divergence and is not asserted here. It is the LAST
    unpinned sizing divergence, and it is deliberately not fixed in the same
    change that pinned the other two: moving that estimator moves published
    volatility_target numbers.
  * DELIBERATELY OUT: window-age position membership — the live replay derives
    position from the visible rolling window, so an entry older than the
    window's left edge is invisible (backtest long, live flat). Unreachable
    until a strategy approaches the fetch window's age; guarded LOUDLY by the
    created_at-vs-window warning in evaluate_strategies (pinned in
    test_live_position_fsm.py::TestWindowAgeGuard), and the fetch must be
    re-anchored at strategy inception before it ever becomes reachable.

Mechanics: the backtrader side runs a probe strategy that records each
indicator's value at every bar; the live side recomputes on the price prefix
up to that bar — exactly the history a live tick sees.
"""

from __future__ import annotations

import math

import backtrader as bt
import pandas as pd
import pytest
from archimedes.services.dsl_to_backtrader import _eval_condition, _make_indicator, interpret_spec
from archimedes.services.strategy_dsl import INDICATOR_NAMES, validate_strategy_spec
from archimedes.services.strategy_signal_evaluator import (
    Signal,
    _compute_indicator_series,
    _compute_indicator_value,
    _spec_signal,
)

# Deterministic two-regime series: a drift-up half then a drift-down half,
# with bar-level zig-zag inside each. Two regimes are load-bearing, not
# flavour — the decision-parity test's anti-vacuity check requires the entry
# condition to genuinely flip both ways (a single-drift fixture kept
# momentum_20 > 0 on every bar and the check rejected it). No randomness:
# seed drift is how parity tests go flaky.
_N_BARS = 260
_SERIES = [100.0]
for _i in range(1, _N_BARS):
    zig = 0.004 if (_i * 7919) % 13 < 6 else -0.003
    drift = 0.0012 if _i < _N_BARS // 2 else -0.0015
    _SERIES.append(round(_SERIES[-1] * (1.0 + zig + drift), 10))


def _bt_indicator_series(name: str, period: int) -> list[float]:
    """Per-bar values of the BACKTEST implementation of one indicator."""
    idx = pd.bdate_range("2024-01-02", periods=_N_BARS)
    close = pd.Series(_SERIES, index=idx)
    df = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 0.0}, index=idx)

    captured: list[float] = []

    class Probe(bt.Strategy):
        def __init__(self) -> None:
            self.ind = _make_indicator(self.data.close, name, period)

        def next(self) -> None:
            try:
                captured.append(float(self.ind[0]))
            except (IndexError, TypeError):
                captured.append(float("nan"))

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(Probe)
    cerebro.run()
    # backtrader only starts calling next() once the indicator's minimum
    # period is filled, so `captured` is END-aligned: captured[i] is bar
    # (N - len + i). Left-pad with NaN to restore per-bar index alignment
    # with the live series — off-by-warmup misalignment here would compare
    # different bars and make every "parity" assertion meaningless.
    return [float("nan")] * (_N_BARS - len(captured)) + captured


def _live_indicator_series(name: str, period: int) -> list[float]:
    """Per-bar values of the LIVE implementation — recomputed on each price
    prefix, exactly the history a live tick sees."""
    out: list[float] = []
    for t in range(_N_BARS):
        prefix = pd.Series(_SERIES[: t + 1])
        try:
            out.append(float(_compute_indicator_value(name, period, prefix)))
        except Exception:
            out.append(float("nan"))
    return out


def _compare(name: str, period: int, *, burn_in: int, rel: float) -> None:
    bt_vals = _bt_indicator_series(name, period)
    live_vals = _live_indicator_series(name, period)
    assert len(bt_vals) == len(live_vals) == _N_BARS
    compared = 0
    for t in range(burn_in, _N_BARS):
        b, lv = bt_vals[t], live_vals[t]
        if math.isnan(b) or math.isnan(lv):
            continue
        assert lv == pytest.approx(b, rel=rel), (
            f"{name}_{period} diverges at bar {t}: backtest={b!r} live={lv!r} — "
            "the two interpreters of one spec language have stopped agreeing; "
            "see the divergence-audit scope note in this file's docstring."
        )
        compared += 1
    # Anti-vacuity: a parity loop that compared nothing proves nothing.
    assert compared >= (_N_BARS - burn_in) * 0.8, f"only {compared} bars compared for {name}_{period}"


# ── Pinned EQUAL ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("period", [10, 50, 200])
def test_sma_parity_per_bar(period):
    _compare("sma", period, burn_in=period + 1, rel=1e-9)


@pytest.mark.parametrize("period", [3, 14])
def test_rsi_parity_per_bar(period):
    """Post-F5: both sides are Wilder SMMA. Pre-F5 this failed by whole points."""
    _compare("rsi", period, burn_in=period + 2, rel=1e-9)


@pytest.mark.parametrize("period", [5, 20, 60])
def test_momentum_parity_per_bar(period):
    """Post-F1: both sides are the trailing return. Pre-F1 the backtest was a
    price ratio offset by exactly 1.0 — mutation-verified: reverting the -1.0
    in dsl_to_backtrader fails every bar here."""
    _compare("momentum", period, burn_in=period + 1, rel=1e-9)


@pytest.mark.parametrize("period", [10, 20])
def test_realized_vol_parity_per_bar(period):
    """Post-F6: the backtest now HAS a realized_vol, and it equals the live one.

    Promoted here from the pinned-ASYMMETRIC section below, which existed only
    to catch this arrival. The estimator had to be written by hand
    (``RealizedVolAnnualized``) rather than taken from
    ``bt.indicators.StandardDeviation``: the live side is
    ``pct_change().tail(N).std()``, pandas ``.std()`` is ddof=1, and
    backtrader's built-in is ddof=0 — a sqrt(N/(N-1)) gap on every bar, 5.4% at
    N=20. Mutation check: switching the ddof to N fails every bar here."""
    _compare("realized_vol", period, burn_in=period + 2, rel=1e-9)


def test_flat_state_daily_entry_decision_parity():
    """When flat, on daily cadence, both interpreters enter iff entry(bar) —
    the one slice of decision logic that is convention-identical today (the
    stateful exit/cadence halves are audit F2/F3, held for Dan). Evaluated
    through the SHARED _eval_condition on both sides' own indicator values, so
    a divergent indicator value flips a decision here, not just a number."""
    period = 20
    entry = {"gt": [f"momentum_{period}", 0]}
    bt_vals = _bt_indicator_series("momentum", period)
    live_vals = _live_indicator_series("momentum", period)
    decisions = 0
    entered_bt = entered_live = 0
    for t in range(period + 1, _N_BARS):
        if math.isnan(bt_vals[t]) or math.isnan(live_vals[t]):
            continue
        d_bt = _eval_condition(entry, {f"momentum_{period}": bt_vals[t]})
        d_live = _eval_condition(entry, {f"momentum_{period}": live_vals[t]})
        assert d_bt == d_live, f"entry decision diverges at bar {t}"
        decisions += 1
        entered_bt += d_bt
        entered_live += d_live
    assert decisions > 100
    # Anti-vacuity: the series must actually exercise BOTH decision branches.
    assert 0 < entered_bt < decisions, "series never flips the entry decision — test asserts nothing"


@pytest.mark.parametrize(
    ("name", "period"),
    [("sma", 50), ("ema", 12), ("rsi", 14), ("momentum", 20), ("realized_vol", 20)],
)
def test_indicator_series_matches_per_prefix_value(name, period):
    """The whole-series indicator form equals the per-prefix scalar form, bar
    for bar. Load-bearing, not bookkeeping: the position replay (F2/F3) reads
    ``_compute_indicator_series`` while a plain live tick reads
    ``_compute_indicator_value``, so a drift between the two would put the
    replayed position and the value the same tick reports on different numbers —
    the exact failure mode this whole file exists to prevent, reintroduced
    inside one module."""
    series = _compute_indicator_series(name, period, pd.Series(_SERIES))
    assert len(series) == _N_BARS
    compared = 0
    for t in range(period + 2, _N_BARS):
        vec = float(series.iloc[t])
        scalar = float(_compute_indicator_value(name, period, pd.Series(_SERIES[: t + 1])))
        if math.isnan(vec) and math.isnan(scalar):
            continue
        assert vec == pytest.approx(scalar, rel=1e-12), (
            f"{name}_{period} series/scalar disagree at bar {t}: series={vec!r} scalar={scalar!r}"
        )
        compared += 1
    assert compared >= (_N_BARS - period - 2) * 0.8, f"only {compared} bars compared for {name}_{period}"


# ── Pinned EQUAL: stateful position path (F2) + cadence (F3) ────────────────
#
# A second deterministic series, purpose-built for the band case. `_SERIES`
# above keeps RSI(14) inside a narrow 65–83 corridor, which never crosses a
# 30/70 band and would make every band assertion below vacuous. This one is a
# 20-bar-down / 20-bar-up sawtooth with a bar-level zig, so RSI(14) sweeps the
# full 0–83 range: ~67 bars below 30, ~70 above 70, and ~169 bars in the DEAD
# ZONE between them where entry and exit are BOTH false — which is precisely
# where the pre-F2 stateless live rule and the backtest FSM disagreed.
_BAND_N_BARS = 320
_BAND_SERIES = [100.0]
for _i in range(1, _BAND_N_BARS):
    _leg = (_i // 20) % 2  # 0 = 20-bar down leg, 1 = 20-bar up leg
    _step = 0.010 if _leg else -0.010
    _zig = 0.002 if (_i * 7919) % 7 < 3 else -0.001
    _BAND_SERIES.append(round(_BAND_SERIES[-1] * (1.0 + _step + _zig), 10))

_RSI_PERIOD = 14
_ENTRY = {"lt": [f"rsi_{_RSI_PERIOD}", 30]}
_EXIT = {"gt": [f"rsi_{_RSI_PERIOD}", 70]}


def _band_spec(rebalance_frequency: str) -> dict:
    """RSI 30/70 band spec — the canonical F2 case: long below 30, out above 70,
    HOLD in between."""
    return {
        "name": f"RSI band ({rebalance_frequency})",
        "asset_universe": ["SPY"],
        "rebalance_frequency": rebalance_frequency,
        "entry": dict(_ENTRY),
        "exit": dict(_EXIT),
        "position_sizing": {"type": "full_invested_when_in_market"},
        "source_arxiv_ids": ["0000.0000"],
        "look_ahead_safe": True,
    }


def _bt_position_path(spec_dict: dict) -> dict[int, bool]:
    """Per-bar in-market state of the REAL backtest FSM.

    Runs the generated ``DSLStrategy`` unmodified and records
    ``self.position.size > 0`` at the top of each ``next()`` — the state the FSM
    reads BEFORE deciding on that bar. backtrader submits the order on the
    decision bar and the broker fills it at the NEXT bar's open, so the state
    the FSM decided at bar t is what shows up at bar t+1; callers compare
    ``path[t + 1]`` against the live decision at bar t.

    ``exposure_fraction`` is dialled down from the 0.99 default on purpose: at
    99% notional an entry order sized on bar t's close can be MARGIN-REJECTED
    when bar t+1's open gaps up, and the FSM then silently stays flat. That is
    an execution-layer artifact of this fixture's 1%/bar sawtooth, not an
    interpreter divergence, and letting it into the comparison would have this
    test failing for a reason it does not claim to be about.
    """
    idx = pd.bdate_range("2024-01-02", periods=_BAND_N_BARS)
    close = pd.Series(_BAND_SERIES, index=idx)
    df = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 0.0}, index=idx)

    captured: dict[int, bool] = {}
    strategy_cls = interpret_spec(validate_strategy_spec(spec_dict))

    class Probe(strategy_cls):
        def next(self) -> None:
            captured[len(self) - 1] = self.position.size > 0
            super().next()

    cerebro = bt.Cerebro()
    cerebro.broker.setcash(1_000_000.0)
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(Probe, exposure_fraction=0.5)
    cerebro.run()
    return captured


def _live_position_path(spec_dict: dict) -> dict[int, bool]:
    """Per-bar in-market decision of the LIVE evaluator — ``_spec_signal`` on
    each growing price prefix, exactly the history a live tick sees."""
    close = pd.Series(_BAND_SERIES)
    out: dict[int, bool] = {}
    for t in range(_RSI_PERIOD, _BAND_N_BARS):
        sig = _spec_signal("parity", "sSPY", close.iloc[: t + 1], dict(spec_dict))
        out[t] = sig.signal is not Signal.FLAT
    return out


def _stateless_in_market(t: int) -> bool:
    """The PRE-F2 live rule: ``entry(bar) and not exit(bar)``, no memory."""
    rsi = _compute_indicator_value("rsi", _RSI_PERIOD, pd.Series(_BAND_SERIES[: t + 1]))
    bar = {f"rsi_{_RSI_PERIOD}": rsi}
    return _eval_condition(_ENTRY, bar) and not _eval_condition(_EXIT, bar)


def test_stateful_dead_zone_position_parity():
    """F2: an RSI 30/70 band strategy holds its position through the dead zone
    in BOTH interpreters, per bar.

    Entry is only tested while flat and exit only while in position, so the
    ~169 bars where 30 <= rsi <= 70 are a HOLD — the backtest stays long there
    and, post-fix, so does the live evaluator. The anti-vacuity block at the end
    is also the regression demonstration: it asserts the dead zone is actually
    exercised AND that the pre-F2 stateless rule (`entry and not exit`) gives a
    different answer on those bars, which is what made this strategy long in the
    backtest and in cash live on the same bar."""
    spec = _band_spec("daily")
    bt_path = _bt_position_path(spec)
    live_path = _live_position_path(spec)

    compared = 0
    live_long = 0
    for t in range(_RSI_PERIOD, _BAND_N_BARS - 1):
        expected = bt_path.get(t + 1)  # +1: broker fills the decision at the next bar
        if expected is None:
            continue
        assert live_path[t] == expected, (
            f"position state diverges at bar {t}: backtest={expected} live={live_path[t]} — "
            "the live evaluator and the backtest FSM disagree about whether this strategy "
            "holds its position; see the divergence-audit scope note in this file's docstring."
        )
        compared += 1
        live_long += live_path[t]

    assert compared > 250, f"only {compared} bars compared — fixture stopped exercising the FSM"
    assert 0 < live_long < compared, "series never flips the position — test asserts nothing"

    # Anti-vacuity + regression demonstration: bars where BOTH conditions are
    # false and the FSM is long. These are exactly the bars the stateless rule
    # got wrong, so this count must be non-trivial or the test proves nothing.
    dead_zone_holds = 0
    for t in range(_RSI_PERIOD, _BAND_N_BARS - 1):
        if bt_path.get(t + 1) and live_path[t] and not _stateless_in_market(t):
            dead_zone_holds += 1
    assert dead_zone_holds > 50, (
        f"only {dead_zone_holds} dead-zone hold bars — the fixture no longer separates the "
        "position FSM from the pre-F2 stateless rule, so this test would pass on the old code"
    )


def test_monthly_cadence_decision_parity():
    """F3: a monthly spec re-decides on the SAME bars in both interpreters, and
    holds its position in between.

    Cadence is a 21-trading-bar modulus anchored on the first post-warm-up bar
    (``DSLStrategy._rebalance_period`` / ``_rebal_counter = period - 1``), and
    the gate runs BEFORE both branches — an off-cadence bar is not even
    exit-tested. The last block is the regression demonstration: the identical
    spec on daily cadence must change state on bars the monthly one holds
    through, so a live path that ignored ``rebalance_frequency`` (as it did
    pre-F3) cannot pass this."""
    spec = _band_spec("monthly")
    bt_path = _bt_position_path(spec)
    live_path = _live_position_path(spec)

    compared = 0
    for t in range(_RSI_PERIOD, _BAND_N_BARS - 1):
        expected = bt_path.get(t + 1)
        if expected is None:
            continue
        assert live_path[t] == expected, (
            f"monthly-cadence position diverges at bar {t}: backtest={expected} live={live_path[t]}"
        )
        compared += 1
    assert compared > 250, f"only {compared} bars compared — fixture stopped exercising the FSM"

    # The live side changes state ONLY on cadence-eligible bars, and there are
    # several of them (a single flip would not distinguish cadence from luck).
    changes = [t for t in range(_RSI_PERIOD + 1, _BAND_N_BARS) if live_path[t] != live_path[t - 1]]
    assert len(changes) >= 3, f"only {len(changes)} state changes — cadence spacing is not exercised"
    for t in changes:
        assert (t - _RSI_PERIOD) % 21 == 0, (
            f"live position changed at bar {t}, which is not a monthly cadence boundary "
            f"(offset {(t - _RSI_PERIOD) % 21} from the 21-bar grid)"
        )

    # Anti-vacuity: the cadence gate must actually suppress decisions the same
    # spec would take on daily cadence.
    daily_path = _live_position_path(_band_spec("daily"))
    suppressed = sum(1 for t in range(_RSI_PERIOD, _BAND_N_BARS) if daily_path[t] != live_path[t])
    assert suppressed > 20, (
        f"monthly and daily cadence differ on only {suppressed} bars — the fixture no longer "
        "distinguishes a cadence-gated live path from one that ignores rebalance_frequency"
    )


# ── Pinned CONVERGENT (F10) ─────────────────────────────────────────────────


@pytest.mark.parametrize("period", [12, 26])
def test_ema_converges_after_seed_burn_in(period):
    """Backtrader seeds EMA with an SMA; the live path seeds from the first
    value (audit F10, documented-benign). The gap decays by (1-2/(N+1))^k, so
    after 6 periods of burn-in the two must agree tightly. A seeding change on
    EITHER side moves the convergence rate and breaks this bound."""
    _compare("ema", period, burn_in=period * 6, rel=1e-4)


# ── No asymmetric indicators remain (F6 closed) ─────────────────────────────


# ── Pinned EQUAL: equal_weight / inverse_vol SIZING ─────────────────────────

_SIZING_WARMUP = 25


def _sizing_spec(ps: dict, universe: list[str]) -> dict:
    """A spec whose only interesting decision is the size.

    ``sma_25 > 0`` is a tautology on positive closes; it exists to hold the first
    entry back past the 20-bar sizing lookback so the vol scale is real rather
    than the unscaled fallback. The exit is never true, so the position is
    entered once and held — the run has exactly one sizing decision to compare.
    """
    return {
        "name": f"sizing-{ps['type']}",
        "asset_universe": universe,
        "rebalance_frequency": "daily",
        "entry": {"gt": [f"sma_{_SIZING_WARMUP}", 0]},
        "exit": {"lt": ["close", 0]},
        "position_sizing": ps,
        "source_arxiv_ids": ["0000.0000"],
        "look_ahead_safe": True,
    }


def _bt_entry_weight(spec_dict: dict) -> tuple[int, float]:
    """(entry bar index, fraction of the account the BACKTEST asked for).

    Captures the ``order_target_percent`` REQUEST rather than the resulting
    position, and divides out the 0.99 exposure buffer — the buffer is an
    execution allowance, not part of the spec's sizing, and the live evaluator
    has no equivalent. Reading the request is also what keeps the single-name
    inverse_vol case measurable: that one asks for more than 1.0 and the cash
    broker refuses it, so a fill-based reading would be 0.0.
    """
    idx = pd.bdate_range("2024-01-02", periods=_N_BARS)
    close = pd.Series(_SERIES, index=idx)
    df = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close, "Volume": 0.0}, index=idx)

    strategy_cls = interpret_spec(validate_strategy_spec(spec_dict))
    recorded: list[tuple[int, float]] = []

    class Probe(strategy_cls):
        def order_target_percent(self, data=None, target=0.0, **kwargs):
            recorded.append((len(self) - 1, float(target)))
            return super().order_target_percent(data=data, target=target, **kwargs)

    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(Probe)
    cerebro.broker.setcash(100_000.0)
    cerebro.run()

    assert recorded, "the backtest placed no sizing order — nothing to compare"
    bar, target = recorded[0]
    return bar, target / 0.99


def _live_weight_at(spec_dict: dict, bar: int) -> float:
    """The live evaluator's weight for a window ENDING at ``bar``.

    Daily cadence makes the last bar the decision bar, so both sides size off
    the same 21 closes.
    """
    signal = _spec_signal("parity", "SPY", pd.Series(_SERIES[: bar + 1]), spec_dict)
    assert signal.signal is not Signal.FLAT, f"live evaluator was flat at bar {bar}: {signal.reason}"
    return signal.weight


@pytest.mark.parametrize("universe", [["SPY", "QQQ"], ["SPY", "QQQ", "IWM", "EFA"]])
def test_equal_weight_sizing_parity(universe):
    """Both sides must put 1/N of the account into one name, for the same N.

    Until 2026-08-30 the live evaluator returned a flat 1.0 here while the
    backtest sized 1/N — a 4× exposure gap on a four-name universe, on the same
    spec, with nothing anywhere saying so. Mutation check: restoring the
    ``weight=1.0`` return in ``_spec_signal`` fails this at every N > 1.
    """
    spec = _sizing_spec({"type": "equal_weight"}, universe)
    bar, bt_weight = _bt_entry_weight(spec)
    assert bt_weight == pytest.approx(1.0 / len(universe), rel=1e-9)
    assert _live_weight_at(spec, bar) == pytest.approx(bt_weight, abs=1e-4)


@pytest.mark.parametrize("reference", [0.03, 0.05])
def test_inverse_vol_sizing_parity(reference):
    """Slot × capped scale, computed by the SAME estimator on both sides.

    ``reference`` is deliberately below this fixture's realized vol so the scale
    lands under the 2.0 cap — at the cap both sides would agree even if their
    vol estimates differed, and the test would prove nothing about the
    estimator. The live path calls ``dsl_to_backtrader.sizing_realized_vol``
    rather than the 22-bar ddof=1 estimator the live ``volatility_target``
    branch uses; that is the point of the shared module-level function.
    """
    universe = ["SPY", "QQQ"]
    spec = _sizing_spec({"type": "inverse_vol", "reference_vol_annual": reference}, universe)
    bar, bt_weight = _bt_entry_weight(spec)
    # Anti-vacuity: a capped scale would make this a comparison of two 1.0s.
    assert 0.0 < bt_weight < 1.0 / len(universe) * 2.0, f"scale hit the cap ({bt_weight:.4f}) — pick a lower reference"
    assert _live_weight_at(spec, bar) == pytest.approx(bt_weight, abs=1e-4)


def test_single_name_inverse_vol_divergence_is_pinned_by_name():
    """KNOWN, NARROW DIVERGENCE — asserted rather than described.

    ``inverse_vol`` clamps its scale, not the slot-multiplied product, so the
    per-name weight is slot-invariant (see ``dsl_to_backtrader.
    inverse_vol_weight``). The cost is that a slot of 1.0 — a single-name
    universe, or the sleeve runners' ``universe_slots=1`` — can ask for up to
    2.0× on an asset calmer than the reference. The two sides then part ways:

      * the BACKTEST asks for it, the cash broker margin-rejects it (audibly),
        and the strategy holds nothing for the whole run;
      * the LIVE evaluator has no leverage either, so it reports the clamped
        1.0 with the request named in its ``reason``.

    That is the entire remaining sizing divergence for these two types, and it
    is unreachable for any universe of two or more names (slot ≤ 0.5, cap 2.0 →
    product ≤ 1.0). Listed in the DSL spec's Known limitations.
    """
    spec = _sizing_spec({"type": "inverse_vol", "reference_vol_annual": 0.60}, ["SPY"])
    bar, bt_weight = _bt_entry_weight(spec)
    assert bt_weight > 1.0, f"the single-name case did not request leverage ({bt_weight:.4f}) — premise moved"

    signal = _spec_signal("parity", "SPY", pd.Series(_SERIES[: bar + 1]), spec)
    assert signal.weight == 1.0
    assert "clamped" in signal.reason and "no leverage" in signal.reason, signal.reason


def test_no_indicator_is_validator_legal_but_interpreter_fatal():
    """Every name a spec may legally write must be buildable by BOTH sides.

    This replaces the old realized_vol asymmetry pin (audit F6). That pin
    asserted a one-sided indicator existed; this one asserts none does. It is
    the generalized version — a new name added to the grammar without a
    backtest implementation fails here rather than at C-rigor time."""
    for name in sorted(INDICATOR_NAMES):
        period = 20
        live = _compute_indicator_value(name, period, pd.Series(_SERIES[:80]))
        assert isinstance(live, float) and not math.isnan(live), f"{name} has no live value"
        assert _bt_indicator_series(name, period), f"{name} has no backtest implementation"
