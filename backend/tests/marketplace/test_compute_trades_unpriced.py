"""The marketplace diff loop is the runner's, unpriced-holding guard included (#1719).

``marketplace.service.compute_trades`` was a hand-ported copy of
``StrategyRunner._compute_trades``. The runner grew the #1080 unpriced-holding
skip; the copy did not. A vault holding a synth whose oracle price could not be
read reports that holding at weight 0 BY CONSTRUCTION — the balance is real, the
value is unknown — so the fork read the fake 0 as "we hold none of this" and
sized a full-weight BUY against it on every tick, forever.

These tests pin the fix from both ends: the unpriced case is now skipped, and
the marketplace result is byte-identical to the runner's for the same inputs,
because it IS the runner's — the marketplace function is an adapter, not a
second implementation.
"""

from __future__ import annotations

import archimedes.marketplace.service as mkt
from archimedes.execution.core import DRIFT_THRESHOLD
from archimedes.execution.core import compute_trades as core_compute_trades
from archimedes.models.portfolio import Portfolio, PortfolioHolding, TradeDirection

# Bound through the module object, not `from ... import compute_trades`: one of
# the tests below asserts on the module's ATTRIBUTES (that it no longer declares
# its own ``_DRIFT_THRESHOLD``), so the module has to be in hand anyway, and
# importing it twice two different ways is the smell the code-quality bot
# flagged. `mkt.compute_trades` also keeps every call site visibly labelled with
# which of the two implementations-that-are-now-one it is entering.

USDC_ADDR = "0x" + "11" * 20
TSLA_ADDR = "0x" + "22" * 20
AAPL_ADDR = "0x" + "33" * 20

ADDR_MAP = {"USDC": USDC_ADDR, "sTSLA": TSLA_ADDR, "sAAPL": AAPL_ADDR}

# The tick the fork mishandled: 40% USDC / 60% sTSLA priced fine, and a real
# sAAPL position the oracle could not price. Its `amount` is the raw 6-decimal
# base-unit balance (1000 sAAPL) that #1080 made visible — the number the fork
# was one step away from sizing against once a target asked for sAAPL.
UNPRICED_TARGETS = {"USDC": 0.2, "sTSLA": 0.4, "sAAPL": 0.4}


def _portfolio(*, priced_aapl: bool) -> Portfolio:
    return Portfolio(
        vault_address="0x" + "ab" * 20,
        total_value_usdc=1000.0,
        holdings=[
            PortfolioHolding(symbol="USDC", token_address=USDC_ADDR, amount=400.0, value_usdc=400.0, weight=0.4),
            PortfolioHolding(symbol="sTSLA", token_address=TSLA_ADDR, amount=6.0, value_usdc=600.0, weight=0.6),
            # value_usdc/weight are 0 BECAUSE the price read failed, not because
            # the position is empty — `amount` is 1000 sAAPL in base units.
            PortfolioHolding(
                symbol="sAAPL",
                token_address=AAPL_ADDR,
                amount=1_000_000_000.0,
                value_usdc=0.0,
                weight=0.0,
                priced=priced_aapl,
            ),
        ],
    )


def _by_symbol(trades):
    return {t.symbol: t for t in trades}


class TestUnpricedHoldingGuard:
    """#1080's skip now applies on the marketplace path too."""

    def test_unpriced_holding_is_not_traded(self):
        trades = _by_symbol(mkt.compute_trades(_portfolio(priced_aapl=False), UNPRICED_TARGETS, ADDR_MAP))

        assert "sAAPL" not in trades, (
            "sized a trade against an unpriced holding: its weight is 0 by construction, not truth (#1080)"
        )

    def test_the_priced_legs_of_the_same_tick_still_trade(self):
        """The skip is per-symbol, not a tick-wide bail-out."""
        trades = _by_symbol(mkt.compute_trades(_portfolio(priced_aapl=False), UNPRICED_TARGETS, ADDR_MAP))

        assert set(trades) == {"USDC", "sTSLA"}
        assert trades["USDC"].direction is TradeDirection.SELL
        assert trades["sTSLA"].direction is TradeDirection.SELL
        assert trades["sTSLA"].estimated_usdc_value == 200.0

    def test_the_same_input_priced_is_the_trade_the_fork_used_to_emit(self):
        """Adversarial control: only the ``priced`` flag separates the two runs.

        With ``priced=True`` the guard has nothing to fire on and a 400 USDC BUY
        of sAAPL comes out — that is exactly what the fork emitted for the
        UNPRICED portfolio above, because it never looked at the flag. If this
        BUY ever disappears, the test above has stopped proving anything.
        """
        trades = _by_symbol(mkt.compute_trades(_portfolio(priced_aapl=True), UNPRICED_TARGETS, ADDR_MAP))

        assert trades["sAAPL"].direction is TradeDirection.BUY
        assert trades["sAAPL"].estimated_usdc_value == 400.0

    def test_repeated_ticks_never_accumulate_the_unpriced_buy(self):
        """The #1080 failure mode was unbounded, not one bad trade.

        Nothing in the portfolio changes while the oracle stays down, so the
        fork re-bought the same 400 USDC of sAAPL on every tick.
        """
        for _ in range(5):
            trades = mkt.compute_trades(_portfolio(priced_aapl=False), UNPRICED_TARGETS, ADDR_MAP)
            assert all(t.symbol != "sAAPL" for t in trades)


class TestRunnerParity:
    """Same inputs → same trades, because there is one implementation."""

    def _targets(self, weights):
        from archimedes.models.portfolio import TargetAllocation

        return [TargetAllocation(symbol=s, weight=w, token_address=ADDR_MAP.get(s, "")) for s, w in weights.items()]

    def test_identical_to_the_runner_on_the_unpriced_tick(self):
        portfolio = _portfolio(priced_aapl=False)

        assert sorted(mkt.compute_trades(portfolio, UNPRICED_TARGETS, ADDR_MAP), key=lambda t: t.symbol) == sorted(
            core_compute_trades(portfolio, self._targets(UNPRICED_TARGETS)), key=lambda t: t.symbol
        )

    def test_identical_to_the_runner_on_a_fully_priced_tick(self):
        portfolio = _portfolio(priced_aapl=True)

        assert sorted(mkt.compute_trades(portfolio, UNPRICED_TARGETS, ADDR_MAP), key=lambda t: t.symbol) == sorted(
            core_compute_trades(portfolio, self._targets(UNPRICED_TARGETS)), key=lambda t: t.symbol
        )

    def test_one_drift_threshold_not_two(self):
        """The marketplace no longer keeps its own copy of the constant.

        A drift just under the runner's threshold must produce nothing; just
        over must produce a trade. Both read the runner's value, so the two can
        no longer be edited apart.
        """
        portfolio = _portfolio(priced_aapl=True)
        under = {"USDC": 0.4 - (DRIFT_THRESHOLD - 0.01), "sTSLA": 0.6, "sAAPL": DRIFT_THRESHOLD - 0.01}
        over = {"USDC": 0.4 - (DRIFT_THRESHOLD + 0.01), "sTSLA": 0.6, "sAAPL": DRIFT_THRESHOLD + 0.01}

        assert mkt.compute_trades(portfolio, under, ADDR_MAP) == []
        assert {t.symbol for t in mkt.compute_trades(portfolio, over, ADDR_MAP)} == {"USDC", "sAAPL"}

        assert not hasattr(mkt, "_DRIFT_THRESHOLD"), "marketplace re-declared its own drift threshold"


class TestMarketplaceOnlyAddressGuard:
    """The one behaviour the adapter keeps that the runner does not need."""

    def test_symbol_with_no_resolved_address_is_still_skipped(self):
        """``addr_map`` comes from a per-publisher universe lookup and can miss.

        Dropping the trade after the shared loop is equivalent to the fork's
        in-loop ``continue`` — nothing else in the loop reads the address.
        """
        portfolio = _portfolio(priced_aapl=True)
        partial = {"USDC": USDC_ADDR, "sTSLA": TSLA_ADDR}  # sAAPL unresolved

        trades = mkt.compute_trades(portfolio, UNPRICED_TARGETS, partial)

        assert all(t.symbol != "sAAPL" for t in trades)
        assert {t.symbol for t in trades} == {"USDC", "sTSLA"}

    def test_usdc_is_exempt_from_the_address_guard(self):
        portfolio = _portfolio(priced_aapl=True)

        trades = mkt.compute_trades(portfolio, {"USDC": 0.0, "sTSLA": 1.0}, {"sTSLA": TSLA_ADDR})

        assert {t.symbol for t in trades} == {"USDC", "sTSLA"}
        assert _by_symbol(trades)["USDC"].token_address == ""
