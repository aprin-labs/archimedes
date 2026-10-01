"""The curated card's display metrics — resolved ONCE, on the write side.

A curated strategy's headline numbers (Sharpe, CAGR, max drawdown, …) have
three possible sources, in this order:

1. ``real_*`` — the migrated ``strategy_backtest_fixtures`` snapshot (#863).
2. the latest persisted ``backtest_results`` row for the strategy.
3. ``stub_*`` — the ``BACKTEST_*`` constants declared in the strategy file.

That chain used to live on the READ side, inside
``strategies_routes._to_strategy_response``, and it is half of what #1746 is:
``GET /api/strategies/{id}`` served link 2 (0.406 for
``harvey_2018_volatility_targeting``) while ``GET /api/strategies/passports/{id}``
served ``strategy_passports.sharpe_ratio``, which the passport sync had filled
from link 1 alone — ``NULL`` for a strategy with no fixture row. Two endpoints,
one strategy id, two answers to "what is its Sharpe".

The chain now runs in exactly one place: ``LocalStrategyProvider._sync_to_unified_table``
resolves it and writes the ANSWER onto the passport row, and every read surface
serves that row. Same precedence, same numbers as before — what changes is that
they are decided once, by a writer, instead of re-decided per request per process.

**Why that also fixes the drift.** The provider memoises its backtest map at
boot (``LocalStrategyProvider._backtests``) with no TTL, and prod runs two ECS
tasks, so the read-side chain could resolve to two different vintages depending
on which task answered — the "Sharpe drifted between reads 37s apart" half of
#1746. A stored answer cannot drift between two readers of the same row.

**What this module deliberately does NOT decide:** whether a fixture snapshot
*should* outrank a real persisted backtest. It preserves today's precedence
exactly (fixture first), because changing which number the product shows is an
owner call (#1746 diagnosis, open decision (a)), not a side effect of fixing an
endpoint disagreement.

Owner: Dan Browne. Decision of record: ``docs/adr/rigor-verdict-of-record.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Provenance labels for ``StrategyResponse.display_metrics_source`` — one per
# link of the chain above, plus "nothing supplied a number".
SOURCE_STRATEGY_RECORD = "strategy_record"
SOURCE_PERSISTED_BACKTEST = "persisted_backtest"
SOURCE_STUB_PLACEHOLDER = "stub_placeholder"
SOURCE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DisplayMetrics:
    """One backtest's descriptive statistics, from ONE link of the chain.

    ``source`` names which link supplied them. It is keyed on Sharpe as the
    representative field because the whole block is taken from a single link —
    mixing links would make the row describe no backtest at all.
    """

    sharpe_ratio: float | None
    sortino_ratio: float | None
    cagr: float | None
    max_drawdown: float | None
    win_rate: float | None
    calmar_ratio: float | None
    correlation_to_spy: float | None
    total_trades: int | None
    backtest_start: str | None
    backtest_end: str | None
    source: str


def resolve_display_metrics(s, bt) -> DisplayMetrics:
    """Resolve the curated display metrics for ``s`` given its persisted backtest.

    ``s`` is the provider's ``Strategy`` (file metadata + fixture row); ``bt`` is
    the ``BacktestResult`` hydrated from the latest ``backtest_results`` row, or
    ``None``.

    Field-for-field identical to the expression this replaces in
    ``_to_strategy_response`` — including the deliberate asymmetries: only
    Sharpe / CAGR / max-drawdown / win-rate / Calmar / correlation fall through
    to a ``stub_*`` value, while Sortino and total-trades stop at the persisted
    backtest (the strategy files declare no stub for those two).
    """
    return DisplayMetrics(
        sharpe_ratio=s.real_sharpe if s.real_sharpe is not None else (bt.sharpe_ratio if bt else s.stub_sharpe),
        sortino_ratio=s.real_sortino if s.real_sortino is not None else (bt.sortino_ratio if bt else None),
        cagr=s.real_cagr if s.real_cagr is not None else (bt.cagr if bt else s.stub_cagr),
        max_drawdown=s.real_max_dd if s.real_max_dd is not None else (bt.max_drawdown if bt else s.stub_max_dd),
        win_rate=s.real_win_rate if s.real_win_rate is not None else (bt.win_rate if bt else s.stub_win_rate),
        calmar_ratio=s.real_calmar if s.real_calmar is not None else (bt.calmar_ratio if bt else s.stub_calmar),
        correlation_to_spy=(
            s.real_corr_spy if s.real_corr_spy is not None else (bt.correlation_to_spy if bt else s.stub_corr_spy)
        ),
        total_trades=s.real_total_trades if s.real_total_trades is not None else (bt.total_trades if bt else None),
        backtest_start=(
            s.real_backtest_start
            if s.real_backtest_start
            else (bt.backtest_start.isoformat() if bt and bt.backtest_start else None)
        ),
        backtest_end=(
            s.real_backtest_end
            if s.real_backtest_end
            else (bt.backtest_end.isoformat() if bt and bt.backtest_end else None)
        ),
        source=display_metrics_source(s, bt),
    )


def display_metrics_source(s, bt) -> str:
    """Which link of the chain supplied the numbers above.

    Unlike the rigor fields (whose fallback #1187/#1340 removed outright) these
    are descriptive stats, so the chain stays — but an un-named chain means a
    stub renders identically to a real backtest.
    """
    if s.real_sharpe is not None:
        # NOT "measured": for the curated library this traces to the #1187
        # fixture snapshot. It is what the strategy record stores, no more.
        return SOURCE_STRATEGY_RECORD
    if bt is not None and bt.sharpe_ratio is not None:
        return SOURCE_PERSISTED_BACKTEST
    if s.stub_sharpe is not None:
        return SOURCE_STUB_PLACEHOLDER
    return SOURCE_UNAVAILABLE


def with_display_metrics(s, bt):
    """A copy of ``s`` whose ``real_*`` fields carry the RESOLVED metrics.

    This is what the passport sync ingests, so ``strategy_passports`` stores the
    number the product actually shows rather than only the first link of the
    chain. A copy, never a mutation: the provider's in-memory ``Strategy`` keeps
    its own ``real_*``/``stub_*`` split, which is what
    :func:`display_metrics_source` reads to name the link.
    """
    from dataclasses import replace

    m = resolve_display_metrics(s, bt)
    return replace(
        s,
        real_sharpe=m.sharpe_ratio,
        real_sortino=m.sortino_ratio,
        real_cagr=m.cagr,
        real_max_dd=m.max_drawdown,
        real_win_rate=m.win_rate,
        real_calmar=m.calmar_ratio,
        real_corr_spy=m.correlation_to_spy,
        real_total_trades=m.total_trades,
        real_backtest_start=m.backtest_start,
        real_backtest_end=m.backtest_end,
    )
