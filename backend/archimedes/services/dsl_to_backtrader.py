"""DSL → backtrader interpreter.

Translates a validated StrategySpec into a backtrader.Strategy subclass
at runtime. No eval/exec/importlib — the strategy is built via type()
with closures over the validated condition trees.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import backtrader as bt

from archimedes.services.strategy_dsl import DSLError, StrategySpec

logger = logging.getLogger(__name__)

# Annualization factor for daily bars — the same 252 the live evaluator and the
# sizing branches below use. One constant so the two interpreters cannot drift.
_TRADING_DAYS = 252

# Lookback (bars) for the SIZING volatility estimate. Frozen at 20 because the
# volatility_target branch has always used 20 and published metrics depend on it.
_SIZING_VOL_LOOKBACK = 20

# Cap on the vol-scaling multiplier for volatility_target / inverse_vol. Without
# it a quiet stretch (realized vol → 0) asks for unbounded leverage.
_VOL_SCALE_CAP = 2.0

# Reference annualized vol for ``inverse_vol`` when the spec does not name one.
# 0.15 ≈ long-run US equity vol; a spec can override with
# ``position_sizing.reference_vol_annual``. PUBLIC because the live evaluator
# must default it to the same number — see ``inverse_vol_weight``.
DEFAULT_INVERSE_VOL_REFERENCE = 0.15


# ── Sizing primitives, shared with the live evaluator ─────────────────
#
# These three functions are the ENTIRE sizing arithmetic for ``equal_weight``
# and ``inverse_vol``. They live at module scope, taking plain floats, so
# ``strategy_signal_evaluator._spec_signal`` computes the live weight by calling
# the very same code the backtest sizes with — the divergence class that produced
# audit findings F1 (momentum) and F5 (RSI) cannot recur here by construction,
# because there is only one implementation.


def slot_weight(universe_slots: int) -> float:
    """Target weight for ONE equal slot of the declared universe.

    See the seam note in ``interpret_spec``'s docstring for what a "slot" is.
    """
    return 1.0 / max(1, int(universe_slots))


def sizing_realized_vol(closes: list[float] | tuple[float, ...]) -> float | None:
    """Annualized trailing vol used by the SIZING branches, or None.

    ``closes`` is a chronological price history ending at the sizing bar. Returns
    None when there are fewer than ``_SIZING_VOL_LOOKBACK + 1`` prices, i.e. not
    enough for the lookback's worth of returns.

    Estimator note (intentional, do not "reconcile"): this is the root-mean-square
    of returns about ZERO over ``_SIZING_VOL_LOOKBACK`` bars — the exact
    expression ``volatility_target`` has always used, kept byte-for-byte
    (including the newest-first summation order) because every volatility_target
    number this repo has published came out of it. The ``realized_vol_N``
    *indicator* above is a sample std (ddof=1) because it has a live-evaluator
    twin it must equal. The two differ by the mean term and the ddof, which is
    immaterial for daily returns; they are separate because one is a sizing knob
    and the other is a graded signal.
    """
    if len(closes) <= _SIZING_VOL_LOOKBACK:
        return None
    recent = [float(closes[-1 - i]) / float(closes[-2 - i]) - 1 for i in range(_SIZING_VOL_LOOKBACK)]
    return (sum(r**2 for r in recent) / len(recent)) ** 0.5 * (_TRADING_DAYS**0.5)


def inverse_vol_weight(
    slot: float,
    realized_vol: float | None,
    reference_vol_annual: float,
) -> float:
    """One slot, scaled by ``reference_vol / realized_vol``. Slot-INVARIANT.

    The cap is applied to the SCALE, before the slot multiply, and that ordering
    is the whole point. Clamping the product (the shipped-and-reverted
    ``min(slot * scale, 1.0)``) makes the result depend on how the universe split
    was expressed: with ``universe_slots=N`` the account-level target
    ``(1/N)·scale`` never reaches 1.0 for N ≥ 2 so the clamp is inert, while with
    ``universe_slots=1`` — the graded sleeve path — it truncates every scale above
    1.0. The same asset on the same prices then gets 2× more exposure through the
    single-feed runner than through the sleeve runner, and ``inverse_vol``
    degenerates into ``volatility_target`` on the sleeve path. Clamping the scale
    makes the per-name exposure identical either way;
    ``test_dsl_sizing_and_indicators.py::TestInverseVolSizing::
    test_slot_invariance_of_the_requested_target`` pins it.

    Consequence, stated rather than clamped away: the returned weight can exceed
    1.0 (up to ``_VOL_SCALE_CAP × slot``). That is a leverage request the
    backtest's cash broker refuses — audibly, via ``notify_order`` — exactly as
    ``volatility_target``'s has always been. It is reachable only for
    ``universe_slots=1`` (or a single-name universe) on an asset calmer than the
    reference. See the DSL spec's Known-limitations list.
    """
    if realized_vol is None or realized_vol <= 0:
        return slot
    return slot * min(reference_vol_annual / realized_vol, _VOL_SCALE_CAP)


# ── Condition evaluation ──────────────────────────────────────────────


def _eval_condition(
    cond: dict[str, Any],
    bar_values: dict[str, float],
) -> bool:
    """Evaluate a condition tree against current bar indicator values."""
    op = next(iter(cond))
    args = cond[op]

    if op == "and":
        return all(_eval_condition(c, bar_values) for c in args)
    if op == "or":
        return any(_eval_condition(c, bar_values) for c in args)
    if op == "not":
        return not _eval_condition(args, bar_values)

    # Comparison operators
    left = args[0]
    right = args[1]

    lv = bar_values.get(left, left) if isinstance(left, str) else left
    rv = bar_values.get(right, right) if isinstance(right, str) else right

    if op == "gt":
        return float(lv) > float(rv)
    if op == "lt":
        return float(lv) < float(rv)
    if op == "gte":
        return float(lv) >= float(rv)
    if op == "lte":
        return float(lv) <= float(rv)

    raise DSLError(f"unknown operator: {op}")


# ── Indicator wiring ──────────────────────────────────────────────────
#
# Bar-offset discipline, applied to every windowed read below and worth stating
# once. ``line[-i]`` means "i bars ago" only while ``i >= 0``; for a negative
# ``i`` the very same expression reads a bar that has not happened yet. A loop
# written over ``range(<a call result>)`` or ``range(<a module constant>, -1, -1)``
# is correct today and *uncheckable* — nothing in the enclosing scope pins the
# sign of the index, so the difference between a trailing window and a look-ahead
# leak is one edit nobody can see. So each windowed read below takes its window
# length as a PARAMETER carrying an explicit precondition guard, and counts
# upward from zero. The guard is the thing that establishes the sign; the offsets
# are then non-positive by construction rather than by the reader's goodwill.


class RealizedVolAnnualized(bt.Indicator):
    """Annualized realized volatility of simple returns over ``period`` bars.

    Deliberately NOT ``bt.indicators.StandardDeviation``. That one is a
    population std (ddof=0); the live evaluator computes ``realized_vol`` as
    ``prices.pct_change().tail(N).std() * sqrt(252)`` and pandas ``.std()`` is a
    SAMPLE std (ddof=1). Shipping the population estimator here would put the
    graded backtest and the live signal a factor of ``sqrt(N/(N-1))`` apart on
    every bar — 5.4% at N=20 — which is exactly the silent-divergence class the
    momentum (F1) and RSI (F5) findings were. This implementation reproduces the
    pandas expression bit-for-bit; ``test_interpreter_parity.py::
    test_realized_vol_parity_per_bar`` pins it per-bar against the live twin.

    ``period`` returns need ``period + 1`` prices, so the minimum period is one
    bar longer than the declared lookback.
    """

    lines = ("realized_vol",)
    params = (("period", _SIZING_VOL_LOOKBACK), ("annualization", _TRADING_DAYS))

    def __init__(self) -> None:
        self.addminperiod(int(self.p.period) + 1)

    def _trailing_returns(self, period: int) -> list[float] | None:
        """Simple returns over the last ``period`` bars, newest first.

        ``returns[i]`` is the return INTO bar ``t - i``, i.e.
        ``price[-i] / price[-i - 1] - 1``. ``None`` when any denominator in the
        window is exactly zero, so the caller decides what an undefined return
        publishes (here: NaN, never a fabricated 0.0).

        The window is a parameter with a guard rather than a read of
        ``self.p.period``, and that is the load-bearing part. ``period >= 1``
        makes ``i`` run over ``0 … period - 1``, so ``-i`` and ``-i - 1`` can
        only address bar ``t`` or earlier — the offsets are non-positive because
        the precondition says so, not because the caller happens to be careful.
        Read ``self.p.period`` inline instead and the sign of the index rests on
        a params tuple defined two hundred lines away.
        """
        if period < 1:
            raise DSLError(f"realized-vol return window must be >= 1 bar, got {period}")
        returns: list[float] = []
        for i in range(period):
            prev = float(self.data[-i - 1])
            if prev == 0.0:
                return None
            returns.append(float(self.data[-i]) / prev - 1.0)
        return returns

    def next(self) -> None:
        n = int(self.p.period)
        if n < 2:
            # A 1-bar sample has no dispersion; pandas .std() on it is NaN, and
            # NaN is the honest answer rather than a fabricated 0.0.
            self.lines.realized_vol[0] = float("nan")
            return
        # n >= 2 here, so the window helper's `period >= 1` precondition holds.
        # It reads newest-first in the same order the loop used to, so the
        # summation order — and with it the last float bit that
        # test_realized_vol_parity_per_bar pins — is unchanged.
        returns = self._trailing_returns(n)
        if returns is None:
            # A zero price in the window leaves at least one return undefined.
            self.lines.realized_vol[0] = float("nan")
            return
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)  # ddof=1
        self.lines.realized_vol[0] = math.sqrt(variance) * math.sqrt(float(self.p.annualization))


# Indicator stems ``_make_indicator`` can actually build. This is the single
# source of truth for "backtestable" — ``strategy_dsl.INDICATOR_NAMES`` (what
# validates) must equal it, and
# ``test_dsl_sizing_and_indicators.py::test_every_validated_indicator_is_interpretable``
# asserts that, so a name can never again be validator-legal but interpreter-fatal.
SUPPORTED_INDICATORS = frozenset({"sma", "ema", "rsi", "momentum", "realized_vol"})


def indicator_min_bars(name: str, period: int) -> int:
    """Bars of history an indicator needs before its value is meaningful.

    ``realized_vol_N`` differences prices, so it needs N+1 bars for N returns;
    every other stem needs exactly its period.
    """
    return period + 1 if name == "realized_vol" else period


def _make_indicator(
    data_line: bt.LineSeries,
    name: str,
    period: int,
) -> Any:
    """Return a backtrader indicator bound to the given data line.

    Must be called from within a Strategy.__init__ so that backtrader's
    auto-discovery wires the indicator's _owner correctly.

    The ``period < 1`` guard below is load-bearing for the look-ahead proof, not
    defensive tidiness. ``momentum`` compiles to ``data_line(-period)``; with
    ``period = -1`` that is ``data_line(1)`` — bar *t+1*, a read of the future.
    ``dsl_lookahead_audit`` proves the interpreter never reads forward by an AST
    pass over this file, and the only way to prove ``-period <= 0`` *locally* is
    for the function to be unable to run with a negative ``period``. Deleting
    this guard therefore does not merely loosen an input check: it makes the
    offset unprovable and the whole structural audit fails closed.
    """
    if period < 1:
        raise DSLError(f"indicator period must be >= 1, got {period!r} for indicator {name!r}")
    if name == "sma":
        return bt.indicators.SimpleMovingAverage(data_line, period=period)
    if name == "ema":
        return bt.indicators.ExponentialMovingAverage(data_line, period=period)
    if name == "rsi":
        return bt.indicators.RSI(data_line, period=period)
    if name == "realized_vol":
        return RealizedVolAnnualized(data_line, period=period)
    if name == "momentum":
        # TRAILING RETURN (centred on 0.0), not a price ratio (centred on 1.0).
        # The distinction decides whether momentum conditions mean anything:
        # every hand-authored momentum condition in this repo is written as
        # {"gt": ["momentum_N", 0]}, and close prices are always positive, so
        # under the ratio convention that entry filter is a tautology — the
        # backtest enters long on a 10% DECLINE (ratio 0.9 > 0). The live
        # signal path (strategy_signal_evaluator._compute_indicator_value),
        # _tsmom_signal, and rank_market all already subtract 1.0; this was
        # the lone ratio-convention outlier, and it is the backtest — i.e.
        # the side whose numbers get published.
        return data_line / data_line(-period) - 1.0
    raise DSLError(f"unsupported indicator: {name}")


# ── Rebalance cadence ─────────────────────────────────────────────────

# Trading-day proxy for each cadence in strategy_dsl.REBALANCE_FREQUENCIES.
# Deliberately module-level rather than a closure method: the LIVE evaluator
# replays this exact gate (strategy_signal_evaluator._replay_position_state,
# divergence audit F3), so cadence has ONE definition and the two interpreters
# of the DSL cannot drift apart on it the way they drifted on momentum (F1) and
# RSI (F5). test_interpreter_parity.py pins the pair per-bar.
_REBALANCE_PERIOD_BARS: dict[str, int] = {"daily": 1, "weekly": 5, "monthly": 21}


def rebalance_period_bars(frequency: str) -> int:
    """Bars between rebalances for a declared cadence.

    Unknown/daily → 1 (every bar). Trading-day proxies, NOT calendar months:
    weekly = 5 bars, monthly = 21 bars.
    """
    return _REBALANCE_PERIOD_BARS.get(frequency, 1)


# ── Strategy factory ──────────────────────────────────────────────────


def interpret_spec(spec: StrategySpec) -> type[bt.Strategy]:
    """Translate a validated StrategySpec into a backtrader.Strategy subclass.

    Returns the class (not an instance). The caller is responsible for
    wiring it into a Cerebro via cerebro.addstrategy(cls).

    ── The single-feed seam (read before touching position sizing) ──
    The interpreted strategy reads exactly ONE instrument: ``self.data``. There
    is no cross-sectional book here, so "equal weight across the universe"
    cannot be expressed as N simultaneous target weights. It is expressed as a
    per-slot weight instead, via the ``universe_slots`` parameter:

      * ``universe_slots`` defaults to ``len(spec.asset_universe)`` — the
        single-feed runner hands the strategy the WHOLE account, so one feed is
        one of N equal slots and ``equal_weight`` targets ``1/N`` of the
        account, leaving the other ``(N-1)/N`` in cash. That is the honest
        reading: the run only ever saw one of the N names, so it must not claim
        the exposure of all N.
      * The sleeve runners (``fusion_evaluator.run_dsl_backtest_portfolio``,
        ``paper_trading._sleeve_dated_returns``) already partition the cash N
        ways and run this same strategy once per ticker. There the equal split
        happens OUTSIDE, so those callers pass ``universe_slots=1`` and each
        sleeve is fully invested in its own share. Applying 1/N on both sides
        would silently size at 1/N².

    The invariant both configurations must satisfy is a PER-NAME one: the share
    of the whole account a given ticker ends up holding is ``slot × scale``
    either way — ``(1/N)·scale`` of the full account on the single-feed path,
    ``scale`` of a ``cash/N`` sleeve on the sleeve path. It is NOT "aggregate
    exposure is 1.0": that is true only for ``equal_weight`` (N names × 1/N),
    and even there only up to the 0.99 exposure buffer. ``inverse_vol``
    aggregates to ``Σ scale_i / N``, which is deliberately not 1.0 — sizing by
    inverse volatility means the book is smaller when the universe is stormier.
    ``inverse_vol_weight`` is where that invariant is enforced (the cap is
    applied to the scale, never to the product).

    ``full_invested_when_in_market`` and ``volatility_target`` ignore
    ``universe_slots`` by definition — "full invested" means all-in on the
    account it was given, and the vol target is an account-level target.
    """
    spec_dict = spec.to_dict()
    entry_cond = spec.entry
    exit_cond = spec.exit
    ps_type = spec.position_sizing.get("type", "full_invested_when_in_market")
    vol_target = spec.position_sizing.get("annual_pct")
    inverse_vol_reference = float(spec.position_sizing.get("reference_vol_annual") or DEFAULT_INVERSE_VOL_REFERENCE)
    default_slots = max(1, len(spec.asset_universe))

    indicator_map: dict[str, tuple[str, int]] = {}
    for ind_name in spec.indicators:
        parts = ind_name.rsplit("_", 1)
        if len(parts) == 2:
            indicator_map[ind_name] = (parts[0], int(parts[1]))

    max_period = max(
        (indicator_min_bars(name, period) for name, period in indicator_map.values()),
        default=0,
    )

    class DSLStrategy(bt.Strategy):
        """Dynamically generated strategy from DSL spec."""

        params = (
            ("dsl_spec", spec_dict),
            ("exposure_fraction", 0.99),
            ("vol_target_annual", vol_target),
            # See the seam note in interpret_spec's docstring. Callers that have
            # ALREADY split the cash per ticker must pass 1.
            ("universe_slots", default_slots),
        )

        def __init__(self) -> None:
            self._indicators: dict[str, Any] = {}
            for alias, (name, period) in indicator_map.items():
                self._indicators[alias] = _make_indicator(self.data.close, name, period)
            self._warmup = max_period
            self._vol_target = self.params.vol_target_annual
            # inverse_vol falls back to the unscaled slot weight before there is
            # enough history for a vol estimate. Warn ONCE per run rather than
            # every entry — a silent fallback is what we are fixing, but a
            # per-bar warning is noise that gets filtered and becomes silence too.
            self._vol_fallback_warned = False
            # Seed the counter so the first post-warmup bar rebalances instead of
            # waiting a full period first. _should_rebalance() increments before
            # testing the modulus, so starting one short of a period boundary makes
            # the first executed bar (counter -> period) the first rebalance. Without
            # this the strategy phase-shifts by up to period-1 bars versus the paper.
            self._rebal_counter = self._rebalance_period() - 1

        def _bar_values(self) -> dict[str, float]:
            vals: dict[str, float] = {
                "close": float(self.data.close[0]),
                "open": float(self.data.open[0]),
                "high": float(self.data.high[0]),
                "low": float(self.data.low[0]),
                "volume": float(self.data.volume[0]),
            }
            for alias, ind in self._indicators.items():
                try:
                    vals[alias] = float(ind[0])
                except (IndexError, TypeError):
                    vals[alias] = float("nan")
            return vals

        @staticmethod
        def _rebalance_period() -> int:
            """Bars between rebalances for the spec's rebalance frequency.

            Delegates to the module-level ``rebalance_period_bars`` so the live
            evaluator's cadence replay reads the SAME table (audit F3).
            """
            return rebalance_period_bars(spec.rebalance_frequency)

        def _should_rebalance(self) -> bool:
            if spec.rebalance_frequency == "daily":
                return True
            period = self._rebalance_period()
            if period <= 1:
                return True
            self._rebal_counter += 1
            return self._rebal_counter % period == 0

        def next(self) -> None:
            if len(self) <= self._warmup:
                return

            if not self._should_rebalance():
                return

            bar_values = self._bar_values()
            in_market = self.position.size > 0

            if not in_market:
                if _eval_condition(entry_cond, bar_values):
                    self._enter_position()
            else:
                if _eval_condition(exit_cond, bar_values):
                    self.close()

        def notify_order(self, order: Any) -> None:
            """Make a refused order audible.

            backtrader delivers Margin/Rejected as a notification and otherwise
            drops the order: no exception, no fill, no trade. The strategy then
            reports a flat bar that is indistinguishable from "the entry
            condition was false", and the equity curve simply shows no exposure
            — a claim ("this strategy was out of the market") the run never
            actually made. Log it loudly instead; the run stays alive because a
            rejected order is a broker-state fact, not a spec error.
            """
            if order.status not in (order.Margin, order.Rejected):
                return
            logger.warning(
                "DSL strategy %s: %s order was %s by the broker (ref=%s, requested size=%s, "
                "last close=%.4f, cash=%.2f, value=%.2f) — the position was NOT taken and the "
                "strategy stays flat this bar.",
                spec.name,
                "BUY" if order.isbuy() else "SELL",
                bt.Order.Status[order.status],
                getattr(order, "ref", "?"),
                getattr(order.created, "size", None),
                float(self.data.close[0]),
                float(self.broker.getcash()),
                float(self.broker.getvalue()),
            )

        def _slot_weight(self) -> float:
            """Target weight for ONE equal slot of the declared universe.

            See the seam note in ``interpret_spec``'s docstring.
            """
            return slot_weight(self.params.universe_slots)

        def _sizing_realized_vol(self, lookback: int = _SIZING_VOL_LOOKBACK) -> float | None:
            """Trailing sizing vol at this bar — delegates to the shared helper.

            The arithmetic lives at module scope (``sizing_realized_vol``) so the
            live evaluator sizes ``inverse_vol`` off the identical estimator
            instead of a second, drifting copy.

            ``lookback`` is a guarded parameter rather than a direct read of
            ``_SIZING_VOL_LOOKBACK`` because the guard is what fixes the sign of
            every bar offset below it. A module constant is a name whose value
            lives elsewhere and can change without this function noticing; the
            guard turns "20, obviously" into a precondition stated where the
            indices are actually formed.
            """
            if lookback < 0:
                raise DSLError(f"sizing vol lookback must be >= 0 bars, got {lookback}")
            if len(self) <= lookback:
                return None
            # Chronological window ending at the current bar, counted UP from
            # the current bar and then reversed. The obvious spelling —
            # ``range(lookback, -1, -1)`` — is the same list, but it hides a sign
            # error in its floor: ``range(n, -3, -1)`` yields -1 and -2, and
            # ``close[1]`` is a bar that has not happened. Counting up, every
            # offset is ``-k`` with ``k >= 0`` from the guard above, so no floor
            # can put an index above zero.
            newest_first = [float(self.data.close[-k]) for k in range(lookback + 1)]
            newest_first.reverse()
            return sizing_realized_vol(newest_first)

        def _order_full_invest(self, price: float) -> None:
            """All of the account's cash (less the exposure buffer) into this feed."""
            cash = float(self.broker.getcash())
            size = int(cash * float(self.params.exposure_fraction) / price)
            if size > 0:
                self.order_target_size(target=size)

        def _enter_position(self) -> None:
            price = float(self.data.close[0])
            if price <= 0:
                return

            if ps_type == "full_invested_when_in_market":
                self._order_full_invest(price)
            elif ps_type == "volatility_target" and self._vol_target:
                # Scale position by target vol / realized vol
                realized_vol = self._sizing_realized_vol()
                if realized_vol is not None and realized_vol > 0:
                    scale = min(self._vol_target / realized_vol, _VOL_SCALE_CAP)
                    cash = float(self.broker.getcash())
                    size = int(cash * float(self.params.exposure_fraction) * scale / price)
                    if size > 0:
                        self.order_target_size(target=size)
                    return
                # Fallback: full invest if not enough data for vol estimate
                self._order_full_invest(price)
            elif ps_type == "equal_weight":
                # One equal slot of the declared universe. With N tickers and a
                # single feed this is 1/N of the account, NOT the whole account
                # — the run only observed one of the N names.
                self.order_target_percent(target=self._slot_weight() * float(self.params.exposure_fraction))
            elif ps_type == "inverse_vol":
                # Equal slot, then scaled by reference vol / realized vol: the
                # calmer the asset relative to the reference, the larger its
                # share. Same scaling machinery (and same cap) as
                # volatility_target, applied to a slot weight instead of the
                # whole account. The cap is applied to the SCALE inside
                # ``inverse_vol_weight`` — clamping the product here instead is
                # the slot-invariance bug that read as a 2x exposure difference
                # between the single-feed and sleeve runners.
                slot = self._slot_weight()
                realized_vol = self._sizing_realized_vol()
                if (realized_vol is None or realized_vol <= 0) and not self._vol_fallback_warned:
                    self._vol_fallback_warned = True
                    logger.warning(
                        "DSL strategy %s: inverse_vol has no usable realized-vol estimate "
                        "(needs > %d bars of non-flat prices) — sizing at the unscaled slot weight %.4f.",
                        spec.name,
                        _SIZING_VOL_LOOKBACK,
                        slot,
                    )
                weight = inverse_vol_weight(slot, realized_vol, inverse_vol_reference)
                self.order_target_percent(target=weight * float(self.params.exposure_fraction))
            else:
                # Unreachable for a validated spec: POSITION_SIZING_TYPES is a
                # closed enum and every member has a branch above. Kept as a
                # loud-ish backstop for an unvalidated spec handed straight to
                # interpret_spec.
                logger.warning(
                    "DSL strategy %s: unhandled position_sizing type %r — falling back to full invest.",
                    spec.name,
                    ps_type,
                )
                self._order_full_invest(price)

    DSLStrategy.__name__ = f"DSL_{spec.name.replace(' ', '_').replace('-', '_')}"
    DSLStrategy.__qualname__ = DSLStrategy.__name__
    return DSLStrategy


def interpret_variant(
    spec: StrategySpec,
    indicator_overrides: dict[str, int],
) -> type[bt.Strategy]:
    """Interpret spec with one variant of its parameter grid applied.

    Deep-copies the spec, overlays period overrides onto the indicator
    list and condition tree, and delegates to ``interpret_spec``.

    Args:
        spec: A validated StrategySpec (may carry parameter_variants).
        indicator_overrides: Mapping from indicator alias (e.g. ``"sma_200"``)
            to the variant period (e.g. ``150``). Keys must already appear in
            ``spec.indicators``.

    Returns:
        A backtrader.Strategy subclass configured with the overridden periods.
    """
    import copy

    # Build a new indicator list with overridden periods.
    new_indicators = list(spec.indicators)
    for alias, new_period in indicator_overrides.items():
        parts = alias.rsplit("_", 1)
        if len(parts) != 2:
            continue
        base_name = parts[0]
        new_alias = f"{base_name}_{new_period}"
        if alias in new_indicators:
            idx = new_indicators.index(alias)
            new_indicators[idx] = new_alias

    # Deep-copy condition trees and replace old alias with new alias.
    new_entry = _rewrite_indicator_aliases(spec.entry, indicator_overrides)
    new_exit = _rewrite_indicator_aliases(spec.exit, indicator_overrides)

    variant_spec = StrategySpec(
        name=f"{spec.name}_v{'_'.join(str(v) for v in indicator_overrides.values())}",
        asset_universe=list(spec.asset_universe),
        rebalance_frequency=spec.rebalance_frequency,
        entry=new_entry,
        exit=new_exit,
        position_sizing=copy.deepcopy(spec.position_sizing),
        source_arxiv_ids=list(spec.source_arxiv_ids),
        indicators=new_indicators,
        parameter_variants=None,
    )

    return interpret_spec(variant_spec)


def _rewrite_indicator_aliases(
    cond: dict[str, Any],
    overrides: dict[str, int],
) -> dict[str, Any]:
    """Deep-copy a condition tree, replacing indicator aliases per overrides."""
    import copy

    cond = copy.deepcopy(cond)
    _rewrite_aliases_in_place(cond, overrides)
    return cond


def _rewrite_aliases_in_place(
    cond: dict[str, Any],
    overrides: dict[str, int],
) -> None:
    """Mutate a condition tree, replacing overridden indicator aliases."""
    op = next(iter(cond))
    args = cond[op]

    if op in ("and", "or"):
        for child in args:
            _rewrite_aliases_in_place(child, overrides)
    elif op == "not":
        _rewrite_aliases_in_place(args, overrides)
    elif op in ("gt", "lt", "gte", "lte"):
        for i, arg in enumerate(args):
            if isinstance(arg, str) and arg in overrides:
                parts = arg.rsplit("_", 1)
                if len(parts) == 2:
                    args[i] = f"{parts[0]}_{overrides[arg]}"
