"""Backtest result data models.

Carries the selection-bias corrections (Deflated Sharpe Ratio, Probability of
Backtest Overfitting, OOS Sharpe split) that make a paper-grounded strategy
credibly distinguishable from a curve-fit artifact. The fields here are the
contract Önder's `IBacktestEvaluator` populates and the strategy passport
surfaces in the UI.

References:
- Bailey & López de Prado (2014). The Deflated Sharpe Ratio. JPM 40(5).
- Bailey, Borwein, López de Prado, Zhu (2014). The Probability of Backtest
  Overfitting (PBO / CSCV framework).
- McLean & Pontiff (2016). Does Academic Research Destroy Stock Return
  Predictability? JoF 71(1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class BacktestResult:
    """Standardized output of backtesting a strategy.

    Produced by: Önder (backtest evaluation engine)
    Consumed by: Chuan (strategy DB, portfolio agent ranking),
                 Dan (validation gate — compare to paper claims),
                 Daniel (performance charts in UI)

    Selection-bias contract: docs/specs/selection-bias-corrections-spec.md
    """

    strategy_id: str  # FK to Strategy.id

    # ── Core risk-adjusted metrics ──────────────────────────
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float  # As a positive fraction, e.g. 0.15 = 15%
    cagr: float  # Compound annual growth rate as fraction
    calmar_ratio: float  # CAGR / max_drawdown

    # ── Trade statistics ────────────────────────────────────
    win_rate: float  # Fraction of winning trades
    profit_factor: float  # Gross profit / gross loss
    total_trades: int
    avg_holding_period_days: float

    # ── Correlation (diversification signal) ────────────────
    # None means "not measured" — never coerce a missing correlation to 0.0,
    # which asserts "uncorrelated to SPY/BTC", a claim nothing measured (#1242
    # review: backtest_mapper.py already made this distinction on the read
    # side; the dataclass annotation had not caught up).
    correlation_to_spy: float | None  # -1 to 1, or None if not measured
    correlation_to_btc: float | None  # -1 to 1, or None if not measured

    # ── Time series ─────────────────────────────────────────
    equity_curve: list[float] = field(default_factory=list)  # Daily equity values
    monthly_returns: list[float] = field(default_factory=list)

    # ── Period ──────────────────────────────────────────────
    backtest_start: date | None = None
    backtest_end: date | None = None

    # ── Paper comparison (claim vs. our re-run) ─────────────
    paper_claimed_sharpe: float | None = None
    paper_claimed_cagr: float | None = None
    paper_claimed_max_dd: float | None = None  # As a positive fraction

    # ── Selection-bias controls (rigor gate) ────────────────
    # Deflated Sharpe Ratio — Sharpe adjusted for non-normality + multiple testing.
    # Bailey & López de Prado (2014). DSR_p_value is the probability that the
    # true Sharpe is greater than zero given the observed return distribution
    # and the number of trials considered in selection.
    deflated_sharpe_ratio: float | None = None
    dsr_p_value: float | None = None  # 0-1, higher = more confident Sharpe > 0
    num_trials_in_selection: int | None = None  # N for DSR multiple-testing correction

    # Probability of Backtest Overfitting — Bailey/Borwein/López de Prado/Zhu
    # (2014). Computed via Combinatorially Symmetric Cross-Validation (CSCV).
    # Lower is better; PBO > 0.5 means the in-sample-optimal strategy is
    # expected to underperform the median out-of-sample.
    pbo_score: float | None = None  # 0-1

    # Out-of-sample slice held separately from in-sample for honesty.
    out_of_sample_sharpe: float | None = None
    walk_forward_train_fraction: float = 0.70  # Train/test split used

    # Static analysis confirmed no look-ahead in strategy code or data slicing.
    look_ahead_audit_passed: bool = False

    # ── Engine + reproducibility ────────────────────────────
    backtest_engine: str | None = None  # 'backtrader' | 'vectorbt' | 'custom-numpy'
    backtest_code_hash: str | None = None  # SHA-256 of executable backtest code
    transaction_cost_bps: int = 10  # Round-trip cost assumed; spec default
    # Fingerprint of the cost model actually installed on the broker, e.g.
    # "cm1:d10:s5". Three engines feed this table and one gate ranks them
    # together, so two rows are only comparable when this matches. It carries
    # the per-symbol overrides and any engine-specific extra (the portfolio
    # simulator appends "+almgren" for its impact term) that transaction_cost_bps
    # alone cannot express. None on rows written before the cost SSOT landed.
    cost_model_id: str | None = None

    # How look_ahead_audit_passed above was actually determined. The analytics
    # engine's own check reads only cerebro.broker.p.coc/coo, which it never
    # sets, so it is True for every run and says nothing about the strategy's
    # signal logic — see _lookahead_audit_passed's docstring. The real AST audit
    # is rigor_evaluator.look_ahead_audit and does not run on that path. Without
    # this field a reader cannot tell a genuine audit pass from the constant.
    #   "broker_config_only"                — execution-timing check only, never fails
    #   "ast_audit"                         — rigor_evaluator.look_ahead_audit ran on real source
    #   "dsl_structural_audit"              — closed-DSL path, the audit reached a verdict
    #                                          about this strategy (pass or fail): the spec
    #                                          was checked against a surface whose interpreter
    #                                          was AST-audited to read only bar t and earlier,
    #                                          and the broker cheat-on-close/open check ran
    #                                          (services/dsl_lookahead_audit.py)
    #   "dsl_audit_not_run"                 — closed-DSL path, the audit reached NO verdict
    #                                          ("pending"/"degenerate"): the boolean beside this
    #                                          label is False because nothing was proven, NOT
    #                                          because a check found a leak
    #   "self_attested"                     — RETIRED, never written any more. Closed-DSL rows
    #                                          from when the LLM's own look_ahead_safe
    #                                          declaration was the "source". That field is gone
    #                                          from the DSL; a row carrying this value asserts
    #                                          nothing and must not be read as an audit result
    #   "static_rebalance_no_signal_shift"  — portfolio simulator: t-1 held weights
    #                                          earn t returns (mechanically look-ahead-
    #                                          free), but the weight matrix itself is
    #                                          unaudited — not a claim about signal logic
    #   None                                — unknown / pre-dates this field
    look_ahead_audit_source: str | None = None

    @property
    def sharpe_vs_paper(self) -> float | None:
        """Ratio of backtest Sharpe to paper's claimed Sharpe.

        Used by Dan's validation gate: reject if < 0.5
        """
        if self.paper_claimed_sharpe and self.paper_claimed_sharpe > 0:
            return self.sharpe_ratio / self.paper_claimed_sharpe
        return None

    @property
    def cagr_vs_paper(self) -> float | None:
        """Ratio of backtest CAGR to paper's claimed CAGR."""
        if self.paper_claimed_cagr and self.paper_claimed_cagr > 0:
            return self.cagr / self.paper_claimed_cagr
        return None

    @property
    def sharpe_decay_estimate(self) -> float | None:
        """Naive McLean-Pontiff (2016) post-publication decay correction.

        Published cross-sectional predictors lost ~58% of in-sample Sharpe
        post-publication on average. If a paper is published and we have a
        claimed Sharpe, this returns the decayed expectation against which to
        sanity-check our backtest.
        """
        if self.paper_claimed_sharpe is None:
            return None
        return self.paper_claimed_sharpe * 0.42

    # ── Deliberately NOT here: passes_validation / passes_rigor_gate ────────
    #
    # This dataclass used to carry its own gate, with its own thresholds
    # (a raw-Sharpe floor, its own DSR bar, pbo<0.5, oos/is>=0.5,
    # sharpe_vs_paper>=0.5, max_dd<0.5). The curated read path grades through
    # ``live_rigor_gate.verdict_from_returns`` and the strictness ladder in
    # ``rigor_profiles``. So "generated and curated are graded on the same
    # scale" was not true — there were two gates, and which one you got
    # depended on which code path reached you.
    #
    # It was also broken in a way nobody could see: ``backtest_portfolio``
    # leaves ``pbo_score=None`` (PBO is a library-level metric a later
    # scheduler refreshes), and the property short-circuited to False whenever
    # PBO was None. Every generated portfolio strategy failed it
    # unconditionally, so the value was a constant rather than a grade.
    #
    # There is now one gate. Grade a return series with
    # ``live_rigor_gate.verdict_from_returns`` and read ``verdict.passes``.
    # Do not reintroduce a threshold here — a gate on a transport dataclass is
    # invisible to the strictness ladder and drifts away from the real one.
