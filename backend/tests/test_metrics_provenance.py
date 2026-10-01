"""Hermetic tests for metric provenance on the strategy + leaderboard payloads.

No DB, no network. Two properties, both of which exist so a placeholder cannot
read as a measurement:

* ``curated_metrics.display_metrics_source`` names which link of the
  ``s.real_* -> bt.* -> s.stub_*`` fallback chain actually supplied the display
  metrics. (It moved out of ``strategies_routes`` with #1746 / PR-B, which made
  the chain a WRITE-side resolution: the passport sync stores the answer and the
  read surfaces serve it. The link-naming rule is unchanged and still lives in
  exactly one place, which is what these cases pin.)
* ``LeaderboardEntry`` carries ``backtest_engine`` / ``cost_model_id`` /
  ``metrics_source`` through from ``StrategyResponse``, since the board is the
  one surface where rows from different engines sit side by side.
"""

from __future__ import annotations

from dataclasses import dataclass

from archimedes.api.schemas import StrategyResponse
from archimedes.services.curated_metrics import display_metrics_source as _display_metrics_source
from archimedes.services.leaderboard import _entry


@dataclass
class _Strat:
    """Only the two fields _display_metrics_source reads."""

    real_sharpe: float | None = None
    stub_sharpe: float | None = None


@dataclass
class _Bt:
    sharpe_ratio: float | None = None


class TestDisplayMetricsSource:
    """Every branch, and each one returns a distinct name."""

    def test_strategy_record_wins_when_real_sharpe_is_present(self):
        # Deliberately NOT called "measured": for the curated library
        # s.real_* traces to the #1187 fixture snapshot.
        assert _display_metrics_source(_Strat(real_sharpe=1.2), _Bt(sharpe_ratio=0.9)) == "strategy_record"

    def test_persisted_backtest_when_no_real_sharpe_but_a_row_exists(self):
        assert _display_metrics_source(_Strat(stub_sharpe=0.5), _Bt(sharpe_ratio=0.9)) == "persisted_backtest"

    def test_stub_placeholder_when_only_a_stub_remains(self):
        assert _display_metrics_source(_Strat(stub_sharpe=0.5), None) == "stub_placeholder"

    def test_unavailable_when_no_source_at_all(self):
        assert _display_metrics_source(_Strat(), None) == "unavailable"

    def test_a_bt_row_with_no_sharpe_does_not_count_as_a_backtest(self):
        # bt is not None but carries no sharpe — must fall through to the
        # stub rather than claim a persisted backtest supplied the number.
        assert _display_metrics_source(_Strat(stub_sharpe=0.5), _Bt(sharpe_ratio=None)) == "stub_placeholder"

    def test_every_branch_returns_a_distinct_name(self):
        names = {
            _display_metrics_source(_Strat(real_sharpe=1.2), None),
            _display_metrics_source(_Strat(), _Bt(sharpe_ratio=0.9)),
            _display_metrics_source(_Strat(stub_sharpe=0.5), None),
            _display_metrics_source(_Strat(), None),
        }
        assert len(names) == 4, f"branches collapsed onto the same label: {names}"


def _resp(**kw) -> StrategyResponse:
    base = {
        "id": "s1",
        "methodology_summary": "m",
        "asset_universe": ["SPY"],
        "position_sizing": "equal_weight",
        "rebalance_frequency": "monthly",
        "status": "validated",
    }
    base.update(kw)
    return StrategyResponse(**base)


class TestLeaderboardCarriesProvenance:
    def test_entry_carries_engine_cost_model_and_metrics_source(self):
        e = _entry(
            _resp(
                backtest_engine="dsl-fusion",
                cost_model_id="cm-v2",
                metrics_source="live_gate",
            )
        )
        assert e.backtest_engine == "dsl-fusion"
        assert e.cost_model_id == "cm-v2"
        assert e.metrics_source == "live_gate"

    def test_entry_defaults_are_honest_when_the_response_carries_nothing(self):
        e = _entry(_resp())
        assert e.backtest_engine is None
        assert e.cost_model_id is None
        # "unavailable", never a plausible-looking substitute.
        assert e.metrics_source == "unavailable"

    def test_metrics_source_has_no_persisted_backtest_value(self):
        """#1187/#1340 removed the `s.<field> ?? bt.<field>` rigor fallback.

        The absence of a "persisted_backtest" value from the rigor-side
        ``metrics_source`` enum IS the assertion that the fallback stayed
        removed. If it ever comes back, something has to add the value, and
        this test is where that shows up.
        """
        import inspect

        from archimedes.api import strategies_routes as sr

        src = inspect.getsource(sr._to_strategy_response)
        start = src.index("metrics_source=")
        assigned = src[start : src.index("\n", start)]
        assert "persisted_backtest" not in assigned, (
            f"rigor metrics_source gained a persisted_backtest branch: {assigned}"
        )
