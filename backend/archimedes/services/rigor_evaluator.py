"""Selection-bias corrections: DSR, PBO, walk-forward OOS Sharpe, look-ahead audit.

Implements the four-primitive admission gate that separates Tier-1 (Archimedes
Verified) strategies from curve-fit noise:

  1. Deflated Sharpe Ratio — Bailey & López de Prado (2014)
  2. Probability of Backtest Overfitting via CSCV — Bailey et al. (2014)
  3. Out-of-sample Sharpe — single chronological hold-out today (see
     compute_oos_sharpe); rolling Combinatorial Purged CV (compute_cpcv_oos_sharpe)
     is the principled upgrade, wired once a combinatorial OOS matrix exists
  4. Look-ahead static audit (AST-based)

Pure computation and orchestration: no I/O, no web framework, no on-chain
dependencies. Arithmetic helpers extracted to _rigor_helpers module for clarity.

Owner: Önder (math lane)
Spec:  docs/specs/selection-bias-corrections-spec.md
"""

from __future__ import annotations

import ast
import logging
import math

import numpy as np
from sqlalchemy.orm import Session

from archimedes.services import rf_series
from archimedes.services._rigor_helpers import (
    _ANNUALIZATION,
    _RF_ANNUAL,
    _RF_DAILY,
    DSR_MIN_BARS,  # noqa: F401 - re-exported for rigor_verify_routes' input floor (#1803)
    _resolve_rf_daily_array,  # used by run_rigor_gate's in-sample-Sharpe fallback + compute_library_pbo (#1409)
    assert_self_contained_cohort_correlation,  # noqa: F401 - re-exported for the 3 cohort call sites (V4 guard)
    benjamini_hochberg_fdr,  # used by compute_board_level_fdr below (#1185)
    classify_regimes,  # used by run_rigor_gate (regime-robustness) + re-exported for test_rigor_regime
    compute_average_pairwise_correlation,  # noqa: F401 - re-exported for fusion_evaluator/selection_bias_routes
    compute_cpcv_oos_sharpe,
    compute_dsr,  # noqa: F401 - re-exported for portfolio_backtester / stockbench adapter
    compute_dsr_hac_and_iid,
    compute_in_sample_sharpe,  # noqa: F401 - re-exported for fusion_evaluator
    compute_oos_sharpe,
    compute_pbo,  # used by compute_library_pbo below + re-exported (fusion/generation/test_pbo_parity)
    compute_sharpe_ci,  # noqa: F401 - re-exported for strategy_provider
    regime_conditional_dsr,  # noqa: F401 - re-exported for test_rigor_regime
    regime_conditional_sharpe,  # noqa: F401 - re-exported for test_rigor_regime
    regime_robustness_score,  # used by run_rigor_gate (regime-robustness) + re-exported for test_rigor_regime
)
from archimedes.services.dsl_lookahead_audit import DEGENERATE as _LA_DEGENERATE
from archimedes.services.dsl_lookahead_audit import PENDING as _LA_PENDING
from archimedes.services.return_diagnostics import diagnose
from archimedes.services.rigor_profiles import (
    CPCV_MIN_POSITIVE_FRACTION,
    DEFAULT_LEVEL,
    DSR_P_FLOOR,
    OOS_ABS_FLOOR,
    STRICTNESS_LEVELS,
    RigorProfile,
    get_profile,
)

logger = logging.getLogger(__name__)


# ─── Look-ahead static audit (AST-based) ──────────────────────────────

_LOOK_AHEAD_FUNCTIONS = {
    "future",
    "forecast",
    "predict",
    "peek",
    "lookahead",
    "look_ahead",
}

# backtrader's Lines/LineBuffer objects (self.data.<line>, self.datas[i].<line>,
# a loop variable bound to a self.datas element, or a custom bt.Indicator stored
# on self) use NEGATIVE subscripts by convention to mean "N bars ago" — the
# opposite of a plain Python list/np.ndarray, where a negative index means "from
# the end" and would silently read into the future on a forward-chronological
# array (#868). These are the standard OHLCV/line attribute names backtrader
# feeds expose; any subscript chain ending in one of these is bars-ago access
# regardless of the root variable's name (covers `self.data.close[-i]` AND a
# loop-var alias like `for d in self.datas: ... d.close[-i]`).
_BACKTRADER_LINE_NAMES = {"close", "open", "high", "low", "volume", "openinterest", "datetime"}

# pandas accessor attributes that index by POSITION/label on a DataFrame/Series —
# `df.iloc[-1]` is last-row (future-leaking) access, structurally identical to
# `self.highest[-1]` (self-rooted) at the AST level. Checked first so a
# self-rooted pandas accessor (e.g. `self.df.iloc[-1]`) is never whitelisted
# just because its root happens to be `self`.
_PANDAS_ACCESSOR_ATTRS = {"iloc", "loc", "at", "iat"}


def _is_backtrader_line_access(value_node: ast.expr) -> bool:
    """True when ``value_node`` (a Subscript's ``.value``) is backtrader
    bars-ago-indexed line access rather than a plain list/array/pandas object.

    A bare local variable subscript (``spread[-1]``, ``scored[-n:]``) is never
    whitelisted — only a dotted attribute chain is a candidate, since every
    backtrader line access in this codebase is reached via attribute access
    (``self.data.close``, ``self.highest``, ``d.close`` for a ``self.datas``
    loop variable) while plain return/price arrays are always bare names.
    Whitelists when either:
      - the chain's ultimate root is ``self`` (covers ``self.data.close[-i]``,
        ``self.datas[1].close[-i]``, and a custom indicator like
        ``self.highest[-1]``), or
      - the chain ends in a standard backtrader OHLCV/line name (covers a
        ``self.datas`` loop-variable alias, e.g. ``d.close[-i]``, whose root is
        not literally ``self``).
    A pandas positional/label accessor (``.iloc``/``.loc``/``.at``/``.iat``)
    anywhere in the chain always disqualifies the whitelist, even under a
    ``self``-rooted chain, since that access pattern reads the DataFrame's
    last row (future data), not a backtrader line.
    """
    if not isinstance(value_node, ast.Attribute):
        return False

    attrs: list[str] = []
    node: ast.expr = value_node
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        if isinstance(node, ast.Attribute):
            attrs.append(node.attr)
            node = node.value
        else:
            node = node.value
    root = node.id if isinstance(node, ast.Name) else None

    if any(a in _PANDAS_ACCESSOR_ATTRS for a in attrs):
        return False
    if root == "self":
        return True
    # attrs[0] is the innermost-collected == outermost/subscripted attribute
    # (e.g. for `d.close`, attrs == ["close"]; for `self.data.close`,
    # attrs == ["close", "data"]) — either way the subscripted attribute is
    # attrs[0].
    return bool(attrs) and attrs[0] in _BACKTRADER_LINE_NAMES


def _is_datas_feed_selection(value_node: ast.expr) -> bool:
    """True when ``value_node`` (a Subscript's ``.value``) is exactly
    ``self.datas`` — i.e. the subscript is ``self.datas[N]``, backtrader's
    convention for selecting which data feed to address in a multi-asset
    strategy (``self.datas[1]`` = the second asset in a pairs trade), not a
    time offset (#881).

    This is deliberately narrower than ``_is_backtrader_line_access``: it
    exempts ONLY a subscript whose subscripted expression is the attribute
    chain ``self.datas`` (root ``self``, single attribute ``datas``). It does
    NOT exempt an arbitrary positive index on any other self-rooted chain —
    e.g. ``self.highest[1]`` or ``self.datas[1].close[1]`` still fall through
    to the general positive-index warning, since those are not feed-selection
    and a positive offset there is bar 0 = now / N>0 = a future bar, exactly
    the violation this audit exists to catch.
    """
    return (
        isinstance(value_node, ast.Attribute)
        and value_node.attr == "datas"
        and isinstance(value_node.value, ast.Name)
        and value_node.value.id == "self"
    )


def look_ahead_audit(strategy_code: str) -> tuple[bool, list[str]]:
    """Static-analysis check for look-ahead bias in strategy code.

    Parses the strategy source with AST and checks for:
    1. Forward data access patterns (e.g., self.data.close[+N])
    2. Calls to functions with look-ahead-suggestive names
    3. Direct indexing into data feeds beyond current bar
    4. Negative shifts (e.g., pandas df.shift(-1)) which leak future data.

    Negative-index subscripts on a backtrader line-buffer object
    (``self.data.close[-i]``, ``self.datas[k].close[-i]``, a ``self.datas``
    loop-variable alias, or a ``self``-rooted custom indicator) are backtrader's
    standard, correct "N bars ago" convention — NOT a look-ahead violation — and
    are excluded from the warning (see ``_is_backtrader_line_access``, #868).
    A negative index on anything else (a plain list/np.ndarray local variable,
    or a pandas ``.iloc``/``.loc``/``.at``/``.iat`` accessor) is still flagged,
    since on those objects ``[-1]`` means "last element", which is future data
    on a forward-chronological series. A positive constant index is suspicious
    on ANY object, backtrader included (bar 0 is "now"; any positive offset is
    a future bar) — with one narrow exemption: ``self.datas[N]`` is
    backtrader's convention for selecting which data feed to address in a
    multi-asset strategy (e.g. ``self.datas[1]`` = the second asset in a pairs
    trade), not a time offset, so it is excluded from the warning (see
    ``_is_datas_feed_selection``, #881). Any other positive-constant subscript
    — including a positive index on ``self.datas[N].close`` or any other
    self-rooted line/attribute — still flags exactly as before.

    Args:
        strategy_code: Python source code of the strategy.

    Returns:
        (passed, warnings) — passed=True if no look-ahead detected.
    """
    warnings: list[str] = []

    try:
        tree = ast.parse(strategy_code)
    except SyntaxError as e:
        return False, [f"Cannot parse strategy code: {e}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = _get_func_name(node.func)
            if func_name and func_name.lower() in _LOOK_AHEAD_FUNCTIONS:
                warnings.append(f"Line {node.lineno}: call to '{func_name}' may indicate look-ahead bias")

            # Check for pandas negative shifts, e.g., shift(-1)
            if func_name == "shift":

                def _is_negative(val_node: ast.AST) -> bool | int | float:
                    if isinstance(val_node, ast.UnaryOp) and isinstance(val_node.op, ast.USub):
                        if isinstance(val_node.operand, ast.Constant) and isinstance(
                            val_node.operand.value, (int, float)
                        ):
                            return val_node.operand.value
                    elif (
                        isinstance(val_node, ast.Constant)
                        and isinstance(val_node.value, (int, float))
                        and val_node.value < 0
                    ):
                        return abs(val_node.value)
                    return False

                # The first positional argument is 'periods'
                if len(node.args) > 0:
                    val = _is_negative(node.args[0])
                    if val is not False:
                        warnings.append(f"Line {node.lineno}: negative shift(-{val}) references future data")
                # Alternatively, check keyword arguments for 'periods'
                for kw in node.keywords:
                    if kw.arg == "periods":
                        val = _is_negative(kw.value)
                        if val is not False:
                            warnings.append(f"Line {node.lineno}: negative shift(-{val}) references future data")

        if isinstance(node, ast.Subscript):
            slice_val = node.slice
            if isinstance(slice_val, ast.UnaryOp) and isinstance(slice_val.op, ast.USub):
                # Negative indices are backtrader's standard, correct "N bars ago"
                # convention on a Lines/LineBuffer object (self.data.close[-i],
                # self.highest[-1], a self.datas loop-variable alias) — NOT a
                # look-ahead violation, so skip the warning there (#868). Still
                # flag a negative index on anything else (a plain list/np.ndarray
                # local variable, or a pandas .iloc/.loc/.at/.iat accessor), where
                # [-1] means "last element" and would silently read future data on
                # a forward-chronological series.
                if not _is_backtrader_line_access(node.value):
                    warnings.append(
                        f"Line {node.lineno}: negative index on a non-backtrader object — "
                        f"verify this is not pandas/list last-row (future) access."
                    )
            # self.datas[N] is backtrader's convention for selecting which data
            # feed to address in a multi-asset strategy (e.g. self.datas[1] =
            # the second asset in a pairs trade) — it is feed selection, not a
            # time offset, so it is exempted from this warning (#881). Any
            # other positive-constant subscript (including
            # self.datas[N].close[1], or a positive index on a plain
            # array/self-rooted line) still flags exactly as before.
            elif (
                isinstance(slice_val, ast.Constant)
                and isinstance(slice_val.value, int)
                and slice_val.value > 0
                and not _is_datas_feed_selection(node.value)
            ):
                warnings.append(
                    f"Line {node.lineno}: positive data index [{slice_val.value}] may reference future bars"
                )

    return len(warnings) == 0, warnings


def _get_func_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# ─── Library-level PBO (criterion-4 input, #546) ──────────────────────


def align_returns_store(
    returns_store: dict[str, dict[str, list]],
) -> dict[str, list[float]]:
    """Inner-join dated return series on ISO dates → an aligned PBO matrix.

    Mirrors ``analytics-engine/scripts/compute_library_pbo.py::build_aligned_matrix``
    so the backend gate and the offline diagnostic compute the library PBO from
    the *same* alignment. The library's strategies trade different calendars
    (^N225 vs SPY vs joined pair windows); CSCV requires that row *i* of the
    returns matrix means the same trading day for every strategy, so we keep
    only the dates present in *every* series before handing off to
    ``compute_pbo`` (which itself only truncates to the shortest length).

    Args:
        returns_store: ``{strategy_stem: {"dates": [...], "daily_returns": [...]}}``
            — one entry per library strategy, each carrying a 1:1 dated series
            (the shape of ``analytics-engine/strategies/daily_returns/<stem>.json``).
            The caller loads it; this module stays I/O-free.

    Returns:
        ``{strategy_stem: returns_on_joint_dates}`` — date-aligned, ready for
        ``compute_pbo``. Returns ``{}`` when fewer than two usable series are
        supplied or the dated intersection is empty (the caller fail-closes on
        the empty matrix — see ``compute_library_pbo``).
    """
    _joint, matrix = align_returns_store_with_dates(returns_store)
    return matrix


def align_returns_store_with_dates(
    returns_store: dict[str, dict[str, list]],
) -> tuple[list[str], dict[str, list[float]]]:
    """``align_returns_store``, also returning the joint ISO-date axis (#1409).

    Same inner-join alignment; ``align_returns_store`` is now a thin wrapper
    around this (kept for backward compatibility with its existing callers,
    which don't need the date axis). ``compute_library_pbo`` uses the date
    axis this returns to thread ``dates`` into ``compute_pbo`` so the library
    PBO's per-split Sharpe ranking CAN use the historical T-bill rf series
    instead of the flat fallback — only when its caller opts in via
    ``use_tbill_series=True`` (#1409 round-4 review fix; default ``False``,
    the not-yet-wired convention every other call site in this module uses).

    Returns:
        ``(joint_dates_sorted, {strategy_stem: returns_on_joint_dates})`` —
        ``joint_dates_sorted[i]`` is the ISO date of row ``i`` in every
        series in the returned matrix. ``([], {})`` under the same
        fewer-than-two-series / empty-intersection conditions
        ``align_returns_store`` returns ``{}`` for.
    """
    series = {stem: rec for stem, rec in returns_store.items() if rec.get("dates") and rec.get("daily_returns")}
    if len(series) < 2:
        return [], {}

    date_sets = [set(rec["dates"]) for rec in series.values()]
    joint = sorted(set.intersection(*date_sets))
    if not joint:
        return [], {}

    matrix: dict[str, list[float]] = {}
    for stem, rec in series.items():
        # strict=True (matching the offline build_aligned_matrix) fails loud on a
        # malformed record whose dates/daily_returns lengths disagree, rather than
        # silently truncating and risking a misaligned row / KeyError on join.
        by_date = dict(zip(rec["dates"], rec["daily_returns"], strict=True))
        matrix[stem] = [float(by_date[d]) for d in joint]
    return joint, matrix


def compute_library_pbo(
    returns_store: dict[str, dict[str, list]],
    s_partitions: int = 16,
    use_tbill_series: bool = False,
) -> float | None:
    """Single library-level CSCV PBO over the whole selection set (#546).

    This is the criterion-4 input for ``run_rigor_gate``: PBO is a property of
    the *selection set*, not of an individual strategy (Bailey et al. 2014), so
    the principled gate input is one library-wide value, not a per-cohort score.
    Date-aligns the store via ``align_returns_store`` then runs the same
    parity-tested ``compute_pbo`` the cohort path uses.

    **Refresh policy (gate-affecting — documented in docs/specs/library-pbo.md).**
    CSCV PBO is a property of the selection set: adding, removing, or re-running
    any library strategy changes it. The caller MUST recompute (call this
    function afresh) whenever the selection set changes — i.e. on every library
    add, removal, or daily-returns-store regeneration — and MUST NOT freeze the
    value into a per-strategy fixture. The store is add-only + idempotent
    (``gen_daily_returns_store.py``), so "selection set changed" reduces to "a
    new ``daily_returns/<stem>.json`` exists"; recomputing on store growth is
    the cadence.

    Args:
        returns_store: ``{strategy_stem: {"dates": [...], "daily_returns": [...]}}``.
        s_partitions: Number of equal time-partitions S (even ≥ 2; default 16,
            the paper's recommended value).
        use_tbill_series: Opt-in (#1409 round-4 review fix), default ``False``.
            When ``True``, threads the joint date axis into ``compute_pbo`` so
            CSCV's per-split Sharpe ranking uses the historical T-bill series
            for this window instead of the flat fallback. Defaulting to
            ``False`` matches every other production caller in this module
            (``run_rigor_gate``'s ``dates`` parameter, also opt-in) — the "wired
            but not yet fed" pattern this file already uses for
            ``cv_returns_matrix``/CPCV. Earlier drafts of this PR threaded
            dates unconditionally here, making this the one live production
            number the PR's rf-series change actually moved without an
            explicit flip-the-switch decision or a before/after value in the
            re-grade delta table; gating it behind this flag (default off)
            restores "every live grade is byte-identical to what production
            has always produced" as literally true with no exceptions, until a
            product/quant decision explicitly turns it on (see
            ``docs/rigor-methods.md`` §1a).

    Returns:
        The single library PBO ∈ [0, 1], or ``None`` when it cannot be computed
        (fewer than two aligned series, empty date intersection, a joint window
        shorter than ``s_partitions``, or a degenerate ``compute_pbo`` result).
        ``None`` is the fail-closed signal: the gate treats a missing/non-finite
        library PBO as criterion-4 FAIL rather than silently passing. A
        non-finite value (should never arise from the rounded ``compute_pbo``
        output, but guarded for safety) also returns ``None``.
    """
    joint_dates, matrix = align_returns_store_with_dates(returns_store)
    if len(matrix) < 2:
        return None

    # Fail closed on a too-short joint window. compute_pbo returns an all-0.0
    # sentinel (a spurious criterion-4 PASS, PBO < 0.5) when rows_per_block =
    # T // s_partitions < 1 — i.e. fewer joint dates than partitions. That 0.0
    # is non-finite-clean but meaningless, so guard it here: an under-length
    # window is non-computable, which must FAIL criterion 4, not pass it.
    shortest = min(len(r) for r in matrix.values())
    if shortest // s_partitions < 1:
        return None

    # #1409: thread the joint date axis through so CSCV's per-split Sharpe
    # ranking uses the historical T-bill rf series for this window instead of
    # the flat fallback — ONLY when the caller opts in (see `use_tbill_series`
    # above). `align_returns_store_with_dates` returns one date per row
    # already common to every series, so it lines up with `matrix` as-is.
    dates_for_pbo = joint_dates if use_tbill_series else None
    scores = compute_pbo(matrix, s_partitions=s_partitions, dates=dates_for_pbo)
    if not scores:
        return None

    # compute_pbo attaches the same library-level value to every member; take any.
    pbo = next(iter(scores.values()))
    if pbo is None or not math.isfinite(pbo):
        return None
    return float(pbo)


def compute_library_pbo_rf_convention(
    returns_store: dict[str, dict[str, list]],
    use_tbill_series: bool = False,
) -> str:
    """The rf_convention ``compute_library_pbo`` actually used for
    ``returns_store``'s joint-date-aligned window (#1409 review fix).

    Mirrors ``compute_library_pbo``'s own ``use_tbill_series`` opt-in (default
    ``False``) exactly, so this always resolves the SAME convention that
    function's ``dates_for_pbo`` argument actually used — never a second,
    independent guess. With the flag left at its default, this returns
    ``RF_CONVENTION_FALLBACK`` unconditionally, matching
    ``compute_library_pbo``'s own not-yet-wired default (byte-identical to
    every pre-#1409 caller); ``_library_pbo_payload`` (``selection_bias_routes.py``)
    can therefore disclose ``LibraryPbo.rf_convention`` honestly either way.

    Returns ``RF_CONVENTION_FALLBACK`` when fewer than two series survive
    date-alignment (no joint date axis to resolve against at all) — matching
    ``compute_library_pbo``'s own < 2-series -> ``None`` fail-closed shape;
    there is no PBO value to have a convention for.
    """
    if not use_tbill_series:
        return rf_series.RF_CONVENTION_FALLBACK
    joint_dates, matrix = align_returns_store_with_dates(returns_store)
    if len(matrix) < 2 or not joint_dates:
        return rf_series.RF_CONVENTION_FALLBACK
    _, convention = rf_series.resolve_annual_rf_for_dates(joint_dates, flat_annual_pct=_RF_ANNUAL * 100.0)
    return convention


# Default board-level FDR α (#1185 code review, 2026-08-20). A module constant
# instead of two independent `= 0.05` defaults (one here, one on
# selection_bias_routes.BoardLevelFdr.fdr_level): the route reports
# BoardLevelFdr.fdr_level to the caller but was never passing it into either
# default, so the two literals could silently diverge if one were edited
# without the other. selection_bias_routes imports this constant and passes it
# explicitly to both compute_board_level_fdr() and BoardLevelFdr(...), so the
# reported α is always sourced from the value the correction actually used.
DEFAULT_BOARD_FDR_LEVEL = 0.05

# Floor for the DSR->classical p-value conversion below (#1185 code review,
# 2026-08-20). compute_dsr's dsr_p_value saturates at exactly 1.0 for a long,
# low-vol, confidently-positive series (rounded via `round(dsr_p_value, 6)` in
# _dsr_from_stats) — converting that via `1.0 - 1.0` yields classical_p == 0.0,
# an impossible p-value. Flooring at this resolution reports "as significant as
# the DSR rounding can actually distinguish" instead of a false certainty.
_CLASSICAL_P_FLOOR = 1e-6


def compute_board_level_fdr(
    dsr_p_values: dict[str, float | None],
    fdr_level: float = DEFAULT_BOARD_FDR_LEVEL,
) -> dict[str, dict[str, float | bool]]:
    """Board-level Benjamini-Hochberg FDR correction across a leaderboard cohort (#1185).

    **The scope decision this issue asked for, stated explicitly.** A user who
    browses the leaderboard and deploys the top-ranked strategy has, statistically,
    run an N-way search over the board — distinct from, and layered on top of, the
    PER-STRATEGY ``num_trials`` convention (#1075: ``num_trials = 1`` on the curated
    path, unaffected by this function). This function performs the board-level
    correction the strictness ladder / PBO / DSR disclosures do NOT: it actually
    computes a BH-FDR-adjusted significance figure across the cohort's simultaneous
    "this strategy's true Sharpe is positive" claims.

    **Chosen scope: ADVISORY / annotation, NOT an admission-gate criterion.** The
    output is surfaced (leaderboard/gate response field) but is deliberately NOT
    wired into ``RigorGateResult.passes_all`` / ``blocked_by_floor`` at any
    strictness level. Rationale, matching the precedent already set for this
    module's other cohort-shaped advisories (IID diagnostics #621, regime
    robustness — see ``run_rigor_gate``'s own comments): (1) promoting a NEW
    statistic to a hard gate is an admission-policy call for the rigor lane
    (Dan/Önder), not a default an issue implementation should make unilaterally;
    (2) gating on it would make a strategy's deploy status move as *unrelated*
    strategies are added to or removed from the board — the exact "library-coupled
    verdict" problem #1075's ADR (docs/adr/num-trials-self-containment.md) already
    rejected for num_trials, so silently reintroducing that coupling as a gate
    here would cut against a decision this codebase already made deliberately;
    (3) the existing quant sign-off gap on #1075 (recorded Accepted-pending-
    quant-sign-off) means a second unreviewed statistical gate change stacked on
    top is exactly the kind of prose claim CLAUDE.md's guard-adversarial-pass rule
    warns against ("a PR description asserting a property the code does not
    enforce") — annotate-only makes no such claim. Promoting this to a gate is a
    legitimate follow-up; it needs its own explicit product/quant decision, not an
    implicit one buried in this issue.

    **Direction of the p-value.** ``benjamini_hochberg_fdr`` expects CLASSICAL
    p-values (small = significant / reject the null). ``RigorGateResult.dsr_p_value``
    is P(true SR > 0) — the OPPOSITE convention (large = confident). This function
    converts via ``classical_p = 1 - dsr_p_value`` before calling BH-FDR, then
    reports back in the ``dsr_p_value`` convention (``board_fdr_confidence = 1 -
    adjusted_p``) so the two numbers read the same way side by side.

    **Saturation floor.** ``dsr_p_value`` can round to exactly 1.0 (a long,
    confidently-positive series — see ``compute_dsr``'s own rounding), which
    would convert to ``classical_p == 0.0``: an impossible p-value asserting
    zero-probability certainty. ``classical_p`` is floored at
    ``_CLASSICAL_P_FLOOR`` (1e-6) before BH-FDR runs, so the saturated case
    reports "significant to the resolution the DSR rounding can distinguish"
    rather than a false exact certainty — ``board_fdr_adjusted_p`` and
    ``board_fdr_confidence`` are bounded away from 0.0 / 1.0 accordingly.

    Args:
        dsr_p_values: ``{strategy_id: dsr_p_value | None}`` for the current
            leaderboard cohort — one entry per strategy, in any order. Entries
            with ``None`` or non-finite values (no backtest data, degenerate
            series) are excluded from the correction (they cannot be assigned a
            p-value) and are ABSENT from the returned dict — the caller's own
            per-strategy MISSING handling covers them, same convention as every
            other rigor-gate field.
        fdr_level: Target board-level FDR (α). Default ``DEFAULT_BOARD_FDR_LEVEL``
            (0.05), matching ``benjamini_hochberg_fdr``'s own default and the
            conventional BH significance level. NOT derived from the DSR
            badge's ``dsr_p_min`` (``rigor_profiles.DSR_P_BADGE_MIN``): the two
            are deliberately different axes — a per-strategy admission bar vs. a
            board-level multiple-testing rate. Since #1794 the badge's implied
            one-sided α and this BH level happen to coincide numerically; that
            is a coincidence, not a derivation, and moving one must not move the
            other.

    Returns:
        ``{strategy_id: {"board_fdr_significant": bool, "board_fdr_adjusted_p":
        float, "board_fdr_confidence": float}}`` for every strategy with a finite
        input p-value. Empty dict when fewer than 1 strategy qualifies.
    """
    ids = [sid for sid, p in dsr_p_values.items() if p is not None and math.isfinite(p)]
    if not ids:
        return {}

    classical_pvalues = [max(_CLASSICAL_P_FLOOR, 1.0 - dsr_p_values[sid]) for sid in ids]
    bh = benjamini_hochberg_fdr(classical_pvalues, fdr_level=fdr_level)

    out: dict[str, dict[str, float | bool]] = {}
    for i, sid in enumerate(ids):
        adjusted_p = bh["adjusted_pvalues"][i]
        out[sid] = {
            "board_fdr_significant": bool(bh["rejected"][i]),
            "board_fdr_adjusted_p": adjusted_p,
            "board_fdr_confidence": round(1.0 - adjusted_p, 8),
        }
    return out


def load_daily_returns_store(
    session: Session | None = None,
) -> tuple[dict[str, dict[str, list]], str | None]:
    """Load the daily-returns store into the shape ``compute_library_pbo`` wants.

    #774: reads the ``strategy_daily_returns`` DB table (one row per
    (stem, date) observation) instead of scanning
    ``analytics-engine/strategies/daily_returns/*.json`` — the same shape
    ``align_returns_store`` / ``compute_library_pbo`` consume, unchanged.
    ``date`` round-trips to the same ``YYYY-MM-DD`` string the JSON records
    used (``date.isoformat()``), so callers see byte-identical output.

    Also returns the data vintage: the MAX ``data_vintage`` string seen across
    every row, so the caller can surface "as of <vintage>" provenance — the
    same semantics as before (each stem's rows share one vintage; the overall
    value is the max across stems).

    **Never raises.** A DB read failure (unreachable DB, table not yet
    migrated) degrades gracefully to ``({}, None)`` — this mirrors the
    fail-closed contract of ``compute_library_pbo``, same as the pre-#774 JSON
    path degrading gracefully when the analytics-engine tree was absent.

    Args:
        session: An existing DB session to query on (tests pass an isolated
            one). When ``None``, opens and closes its own session via
            ``archimedes.db.get_session``.

    Returns:
        ``(store, data_vintage)`` where ``store`` is
        ``{stem: {"dates": [...], "daily_returns": [...]}}`` (empty when the
        table has no rows or the read fails) and ``data_vintage`` is the max
        vintage string across rows (``None`` when no row carried one).
    """
    from archimedes.models.daily_returns_store import StrategyDailyReturn

    def _query(sess: Session) -> tuple[dict[str, dict[str, list]], str | None]:
        rows = sess.query(StrategyDailyReturn).order_by(StrategyDailyReturn.stem, StrategyDailyReturn.date).all()
        store: dict[str, dict[str, list]] = {}
        vintages: list[str] = []
        for row in rows:
            entry = store.setdefault(row.stem, {"dates": [], "daily_returns": []})
            entry["dates"].append(row.date.isoformat())
            entry["daily_returns"].append(row.daily_return)
            if row.data_vintage:
                vintages.append(row.data_vintage)
        data_vintage = max(vintages) if vintages else None
        return store, data_vintage

    try:
        if session is not None:
            return _query(session)
        from archimedes.db import get_session

        with get_session() as sess:
            return _query(sess)
    except Exception:
        logger.warning("load_daily_returns_store: DB read failed, degrading to empty store", exc_info=True)
        return {}, None


# ─── 6. Rigor Gate — composite check ─────────────────────────────────

# #819: minimum library N for PBO/CSCV to gate criterion 4. Below this, the
# CSCV OOS relative rank omega = rank/N is too coarsely quantized (few
# distinct logit values, and the comparison set itself dominates the result)
# for PBO to carry real statistical power, and it becomes library-coupled —
# adding or removing one neighbor can flip a member's verdict. 10 is where a
# leave-one-out instability simulation's flip rate settles at its floor (~10%,
# matching the exact 1/N argument for how much one removal can move an N-member
# comparison set) and stays there through N=16 tested. See
# docs/specs/selection-bias-corrections-spec.md § 2 addendum for the full
# derivation + simulation numbers. PBO is still computed and surfaced below
# this floor — it just stops gating (see RigorGateResult).
MIN_LIBRARY_N_FOR_PBO_GATING = 10


def is_zero_variance_series(daily_returns: list[float] | np.ndarray) -> bool:
    """True iff ``daily_returns`` is a non-empty, mathematically constant series.

    Issue #1184: a zero-variance persisted return series (every bar identical,
    most plausibly all-zero) is broken data or a zero-trade backtest — NOT a
    strategy that is merely "pending more data." ``compute_dsr`` guards on
    exactly this over the FULL series (``_rigor_helpers.py``,
    ``float(np.ptp(arr)) == 0.0``) and correctly returns ``None`` for it — a
    constant series has no well-defined DSR. This helper reuses the IDENTICAL
    full-series test, so ``is_degenerate`` and ``compute_dsr``'s ``None`` agree
    by construction on the "whole series is flat" case.

    NOTE — this is ONE-DIRECTIONAL, not an equivalence, and does not by itself
    cover ``compute_oos_sharpe``: that helper's ptp guard runs on the
    chronological OOS *slice*, not the full series (see
    ``is_oos_zero_variance_series`` below), so a series that goes flat only
    inside the OOS window makes ``compute_oos_sharpe`` return ``None`` while
    this function alone would still say ``False``. ``run_rigor_gate`` ORs both
    predicates into ``is_degenerate`` so the two agree by construction on
    either kind of flatness — read that combination, not this function alone,
    as the "DSR/OOS agree with is_degenerate" guarantee.
    """
    arr = np.asarray(daily_returns, dtype=float)
    if len(arr) == 0:
        return False
    return bool(np.ptp(arr) == 0.0)


def is_oos_zero_variance_series(
    daily_returns: list[float] | np.ndarray,
    train_fraction: float = 0.70,
) -> bool:
    """True iff the chronological OOS slice ``compute_oos_sharpe`` grades is a
    non-empty, mathematically constant series — even when the FULL series is
    not (``is_zero_variance_series`` alone misses this case).

    Issue #1184 follow-up: the PR's original claim that ``is_zero_variance_series``
    "agrees by construction" with ``compute_oos_sharpe`` was false for a series
    that goes flat only inside the OOS window (e.g. a strategy that stops
    placing trades partway through the backtest — exactly the "zero-trade
    backtest" cause #1184 names). That case made ``compute_oos_sharpe`` return
    ``None`` while ``is_zero_variance_series`` returned ``False``, so it still
    fell through to the undifferentiated "fail" + "MISSING" this issue exists
    to eliminate.

    Mirrors ``compute_oos_sharpe``'s own split/length guards exactly (same
    ``train_fraction``, same ``T < 10`` / ``len(oos) < 21`` thresholds) so the
    two agree by construction on both where the OOS window starts and how
    short is "too short to test" — not just on the zero-variance check itself.
    """
    arr = np.asarray(daily_returns, dtype=float)
    T = len(arr)
    if T < 10:
        return False
    split = int(T * train_fraction)
    oos = arr[split:]
    if len(oos) < 21:  # mirrors compute_oos_sharpe's 1-trading-month floor
        return False
    return bool(np.ptp(oos) == 0.0)


class RigorGateResult:
    """Result of running all four selection-bias checks on a strategy."""

    def __init__(
        self,
        strategy_id: str,
        deflated_sharpe: float | None = None,
        dsr_p_value: float | None = None,
        dsr_p_value_iid: float | None = None,
        dsr_se_method: str = "iid",
        num_trials: int = 1,
        pbo_score: float | None = None,
        oos_sharpe: float | None = None,
        look_ahead_passed: bool = False,
        in_sample_sharpe: float | None = None,
        paper_claimed_sharpe: float | None = None,
        cpcv_mean_oos_sharpe: float | None = None,
        cpcv_positive_fraction: float | None = None,
        pbo_source: str = "cohort",
        iid_assumption_violated: bool | None = None,
        iid_diagnostics: dict | None = None,
        regime_robustness: dict | None = None,
        pbo_library_size: int | None = None,
        profile: RigorProfile | None = None,
        is_degenerate: bool = False,
        is_full_series_degenerate: bool = False,
        is_oos_degenerate: bool = False,
        # Why the look-ahead leg never reached a verdict, when it did not. Set
        # only for a structural not-run (no inspectable source and no audit
        # result supplied, or a DSL audit that could not conclude) — never for a
        # real failure. ``look_ahead_passed`` stays False either way, so this
        # changes nothing about admission: it is fail-closed on the gate and
        # honest on the surface, which are two different questions. See
        # services/dsl_lookahead_audit.py, "Deployability is fail-closed;
        # rendering is honest".
        look_ahead_not_run_reason: str | None = None,
        # The DSL audit's four-state verdict ("pass"/"fail"/"pending"/
        # "degenerate") when a DSL caller supplied one; ``None`` on every other
        # path, which keeps the pre-existing two-state rendering for rows that
        # never had a four-state verdict to begin with. It only selects the
        # WORDING of ``gate_details["look_ahead"]`` — admission reads
        # ``look_ahead_passed`` and nothing else.
        look_ahead_status: str | None = None,
        rf_convention: str = rf_series.RF_CONVENTION_FALLBACK,
    ) -> None:
        self.strategy_id = strategy_id
        # #1184: True when the persisted return series itself is zero-variance —
        # either the FULL series (``is_zero_variance_series``) or just the
        # chronological OOS slice ``compute_oos_sharpe`` grades
        # (``is_oos_zero_variance_series``) — the reason ``deflated_sharpe`` /
        # ``dsr_p_value`` / ``oos_sharpe`` are None here is "the data is broken
        # or the backtest placed zero trades," never "not enough data yet"
        # (that's `pending`, decided upstream before this result exists) nor
        # "graded and statistically weak" (a real `fail`). Consumers must check
        # this BEFORE treating a None DSR as either of those two.
        #
        # This is the OR of ``is_full_series_degenerate`` / ``is_oos_degenerate``
        # below — the single boolean ``tri_state_status`` and
        # ``live_rigor_gate.RigorGateVerdict.from_result`` read, since either
        # kind of flatness earns the same "degenerate" badge category. The two
        # components are carried separately (not just folded into this OR)
        # because they are NOT interchangeable for per-criterion messaging: a
        # full-series-flat input makes ``compute_dsr`` mathematically undefined
        # (it runs over the whole series), but an OOS-only-flat input does not —
        # ``compute_dsr`` still ran over the (non-constant) full series and may
        # have produced a legitimate, even passing, DSR. ``gate_details["dsr"]``
        # must branch on ``is_full_series_degenerate`` specifically, not this OR,
        # or it wrongly claims "not a legitimate DSR" for a DSR that is legitimate
        # (#1184 round-2 finding).
        self.is_degenerate = is_degenerate
        # True iff the FULL persisted series is zero-variance (``is_zero_variance_series``)
        # — the case where ``compute_dsr`` itself is mathematically undefined, since
        # it guards on ``np.ptp`` over the whole series. Use this (not ``is_degenerate``)
        # to decide whether the DSR itself is illegitimate.
        self.is_full_series_degenerate = is_full_series_degenerate
        # True iff only the chronological OOS slice ``compute_oos_sharpe`` grades is
        # zero-variance (``is_oos_zero_variance_series``) — the series can be non-constant
        # overall (a legitimate DSR) while still flat inside the OOS window (e.g. a
        # strategy that stops trading partway through the backtest).
        self.is_oos_degenerate = is_oos_degenerate
        # The strictness profile whose thresholds ``passes_all`` / ``gate_details``
        # report against. Defaults to the strictest level (the badge bar), so an
        # unspecified profile is fail-safe. The strictness-adjustable thresholds
        # (DSR p, PBO ceiling, OOS/IS ratio) come from here; the always-on
        # correctness floors (look-ahead, OOS>0, DSR≥floor, CPCV) do NOT and hold
        # at every level. See rigor_profiles for the ladder + floor rationale.
        self.profile = profile or get_profile(DEFAULT_LEVEL)
        self.deflated_sharpe = deflated_sharpe
        self.dsr_p_value = dsr_p_value
        # The gating dsr_p_value uses a serial-correlation-robust (Newey–West
        # HAC) standard error (#621 follow-up). dsr_p_value_iid is what the IID
        # SE would have produced — surfaced as an advisory delta so the passport
        # shows how much serial dependence moved the verdict; dsr_se_method
        # records which SE backs the gating p-value ("hac" or "iid").
        self.dsr_p_value_iid = dsr_p_value_iid
        self.dsr_se_method = dsr_se_method
        self.num_trials = num_trials
        self.pbo_score = pbo_score
        # Which selection set produced the criterion-4 PBO: "library" when the
        # full-library CSCV PBO was supplied (the principled Bailey et al. input,
        # #546), "cohort" when it fell back to the per-cohort dict score. Surfaced
        # in gate_details so the verdict is honest about its provenance.
        self.pbo_source = pbo_source
        # #819: the N (number of strategies) the PBO cross-section was computed
        # over. None when unknown (e.g. no PBO source at all — the pre-#819
        # MISSING path). Used only to decide whether N clears
        # MIN_LIBRARY_N_FOR_PBO_GATING; never changes the PBO value itself.
        self.pbo_library_size = pbo_library_size
        self.oos_sharpe = oos_sharpe
        self.look_ahead_passed = look_ahead_passed
        self.look_ahead_not_run_reason = look_ahead_not_run_reason
        self.look_ahead_status = look_ahead_status
        self.in_sample_sharpe = in_sample_sharpe
        self.paper_claimed_sharpe = paper_claimed_sharpe
        # Combinatorial Purged CV results (None when the series is too short to
        # partition). When present, the gate additionally requires that the
        # strategy's edge holds OOS across a majority of CPCV paths.
        self.cpcv_mean_oos_sharpe = cpcv_mean_oos_sharpe
        self.cpcv_positive_fraction = cpcv_positive_fraction
        # IID / random-walk return diagnostics (#621). ADVISORY, not a pass/fail
        # criterion: autocorrelated returns are the *signal* for trend/momentum
        # strategies (Faber SMA200, TSMOM), so an IID violation must NOT fail the
        # gate — it would wrongly reject legitimate strategies. We surface it so
        # the diagnostic is honest (computed AND reported, not computed-and-dropped)
        # and the passport can flag "interpret the Sharpe SE with caution here".
        # iid_diagnostics carries the full Ljung-Box / variance-ratio / runs detail.
        self.iid_assumption_violated = iid_assumption_violated
        self.iid_diagnostics = iid_diagnostics
        # Regime-robustness (per-volatility-regime Sharpe consistency). ADVISORY for
        # the same reason: a single-regime edge can be a legitimate, disclosed strategy.
        # Carries per_regime / min_regime_sharpe / consistency / robust (see
        # _rigor_helpers.regime_robustness_score). None when the series is too short.
        self.regime_robustness = regime_robustness
        # #1409: which rf convention produced deflated_sharpe/dsr_p_value/oos_sharpe/
        # in_sample_sharpe above — "excess_tbill_series" (the historical 3-month
        # T-bill, FRED DGS3MO, per-window aligned) or "excess_flat_fallback" (the
        # disclosed flat-5% fallback: no `dates` passed to run_rigor_gate, or the
        # window fell outside the vendored series' coverage). Rides the same
        # payload paths `dsr_convention` already rides — see `gate_details` below.
        self.rf_convention = rf_convention

    @property
    def _pbo_below_power_floor(self) -> bool:
        """True when the PBO cross-section's N is too small for CSCV to carry
        real statistical power (#819) — criterion 4 must not gate in that case."""
        return self.pbo_library_size is not None and self.pbo_library_size < MIN_LIBRARY_N_FOR_PBO_GATING

    @property
    def blocked_by_floor(self) -> bool:
        """True when an always-on correctness floor fails — the strategy can NEVER
        be admitted, at any strictness level.

        The floors are level-independent (see rigor_profiles): look-ahead audit
        PASS, a finite OOS Sharpe strictly above ``OOS_ABS_FLOOR``, a finite DSR
        p-value ≥ ``DSR_P_FLOOR``, and — when a combinatorial matrix was supplied —
        a finite CPCV positive fraction ≥ ``CPCV_MIN_POSITIVE_FRACTION``. A
        strategy tripping any of these is broken/curve-fit, not merely "riskier,"
        so no slider level lets it through. This is distinct from a strategy that
        merely fails the loosest *adjustable* thresholds (statistically too weak),
        which is ``min_passing_level is None and not blocked_by_floor``.
        """
        if not self.look_ahead_passed:
            return True
        if self.oos_sharpe is None or not math.isfinite(self.oos_sharpe) or self.oos_sharpe <= OOS_ABS_FLOOR:
            return True
        if self.dsr_p_value is None or not math.isfinite(self.dsr_p_value) or self.dsr_p_value < DSR_P_FLOOR:
            return True
        return self.cpcv_positive_fraction is not None and (
            not math.isfinite(self.cpcv_positive_fraction) or self.cpcv_positive_fraction < CPCV_MIN_POSITIVE_FRACTION
        )

    def _passes_profile(self, profile: RigorProfile) -> bool:
        """Every criterion clears ``profile``'s adjustable thresholds AND every
        always-on floor holds. Single evaluator behind ``passes_all`` and
        ``passes_at_level`` so the badge and the ladder share one definition.

        NaN-hardening: every IEEE-754 comparison against NaN is False, so a NaN
        metric (not None — None is guarded) would silently skip its fail branch
        and let an under-credentialed strategy pass. ``blocked_by_floor`` already
        rejects NaN dsr_p_value / oos_sharpe; the remaining metric (PBO, sourced
        from the library CSCV PBO #546 or a cohort dict) is guarded below.
        """
        # Correctness floors first — level-independent, never bypassable.
        if self.blocked_by_floor:
            return False
        # ── Adjustable: DSR p-value ──
        if self.dsr_p_value < profile.dsr_p_min:
            return False
        # ── Adjustable: PBO ceiling ── (#819: neutral below the CSCV power floor,
        # where PBO is too underpowered/library-coupled to gate — still surfaced
        # in gate_details as NOT_RUN, just not load-bearing.)
        if not self._pbo_below_power_floor:
            if self.pbo_score is None or not math.isfinite(self.pbo_score):
                return False
            if self.pbo_score >= profile.pbo_max:
                return False
        # ── Adjustable: OOS/IS cliff ratio (the absolute OOS>0 floor is enforced
        # by blocked_by_floor above; this guards against a performance *cliff*). ──
        if (  # noqa: SIM103 — explicit fail-branch kept for gate auditability (negation obscures)
            self.in_sample_sharpe is not None
            and math.isfinite(self.in_sample_sharpe)
            and self.in_sample_sharpe > 0
            and self.oos_sharpe / self.in_sample_sharpe < profile.oos_is_ratio_min
        ):
            return False
        # NOTE: the IID (#621) and regime-robustness diagnostics are deliberately NOT
        # pass/fail criteria at any level. Surfaced via gate_details as advisories.
        return True

    @property
    def passes_all(self) -> bool:
        """Verdict at this result's own ``profile`` (the strictest/badge bar by
        default). Consumers that need a specific strictness call
        ``passes_at_level`` instead."""
        return self._passes_profile(self.profile)

    def passes_at_level(self, level: int) -> bool:
        """Verdict at an arbitrary strictness level (1 = strictest … 5 = loosest)."""
        return self._passes_profile(get_profile(level))

    @property
    def min_passing_level(self) -> int | None:
        """Lowest strictness level (1..5) at which the strategy passes, or ``None``.

        Because thresholds relax monotonically with level, ``passes_at_level`` is
        monotonic in level, so this minimum is well-defined. ``None`` means the
        strategy fails even the loosest level — either ``blocked_by_floor`` (a
        correctness floor) or too statistically weak for the loosest thresholds.
        A user at level L can deploy iff ``min_passing_level is not None and
        min_passing_level <= L`` — the rule the deploy gate enforces server-side.
        """
        for level in STRICTNESS_LEVELS:
            if self._passes_profile(get_profile(level)):
                return level
        return None

    @property
    def tri_state_status(self) -> str:
        """This result's category as one of ``"degenerate" | "pass" | "fail"``.

        Issue #1184: expresses the categorization rule so a zero-variance
        persisted return series (``is_degenerate``) reports its OWN category —
        never "pending" (that status only exists upstream, before a
        ``RigorGateResult`` is even computed, for a strategy with too few
        persisted returns to grade at all) and never collapsed into an
        undifferentiated "fail" that reads as "graded and statistically weak"
        when the truth is "the data itself is broken or the backtest placed
        zero trades."

        NOTE — this property is a convenience/logging accessor (used at the
        gate's own INFO log line below), not a dependency
        ``live_rigor_gate.RigorGateVerdict.from_result`` actually imports: it
        re-derives the identical ``is_degenerate``-first / ``passes_all``-else
        rule directly off ``is_degenerate`` and ``passes_all`` instead, so it
        stays constructible from the ``MagicMock`` result doubles the chat/
        portfolio-agent test suites build (which don't stub this property).
        ``strategies_routes._verdict_from_result`` does NOT re-derive the rule
        — it's a pure delegation to ``from_result`` — so the rule lives in two
        places, not three: this property and ``from_result``. A separate,
        still-hardcoded three-state map lives at
        ``strategies_routes._passport_to_strategy_response`` (the
        generated/fusion passport read path); it derives its status from a
        stored aggregate rather than a ``RigorGateResult`` and can't report
        "degenerate" at all — tracked as a known gap in its own inline
        comment, not covered by this property or ``from_result``. If you
        change the categorization rule, change it here and in
        ``from_result``, and check whether ``_passport_to_strategy_response``
        needs the equivalent fix.
        """
        if self.is_degenerate:
            return "degenerate"
        return "pass" if self.passes_all else "fail"

    @property
    def gate_details(self) -> dict[str, str]:
        details: dict[str, str] = {}

        # #1184: a zero-variance series makes DSR and OOS Sharpe undefined for a
        # data-quality reason (broken data / a zero-trade backtest), not a
        # "hasn't run" or "ran and lost" reason — say so explicitly instead of
        # the generic "MISSING" a short series or an ordinary gate loss would
        # also produce. The remaining criteria (PBO, look-ahead, CPCV, IID,
        # regime-robustness) are computed identically either way below.
        #
        # NOTE (round-2 finding): this branches on ``is_full_series_degenerate``
        # specifically, NOT ``is_degenerate`` (the OR with ``is_oos_degenerate``).
        # ``compute_dsr`` runs over the FULL series, so it is undefined only when
        # the FULL series is flat. An OOS-only-flat series (full series
        # non-constant) leaves ``compute_dsr`` unaffected — it may have produced a
        # legitimate, even passing, DSR — so that case must fall through to the
        # ordinary PASS/FAIL/MISSING branches below, not claim "not a legitimate
        # DSR" for a DSR that is legitimate.
        if self.is_full_series_degenerate:
            details["dsr"] = (
                "DEGENERATE (zero-variance persisted return series — broken data or a zero-trade backtest, "
                "not a legitimate DSR)"
            )
        elif self.dsr_p_value is not None and self.dsr_p_value >= self.profile.dsr_p_min:
            details["dsr"] = f"PASS (p={self.dsr_p_value:.4f})"
        elif self.dsr_p_value is not None:
            details["dsr"] = f"FAIL (p={self.dsr_p_value:.4f}, need ≥ {self.profile.dsr_p_min:.2f})"
        else:
            details["dsr"] = "MISSING"
        # Disclose the Sharpe convention behind the DSR (#547). The backend gate
        # computes excess-return Sharpe; served library fixtures carry their own
        # per-entry "dsr_convention" ("raw" for frozen legacy, "excess" for new).
        details["dsr_convention"] = "excess"
        # #1409: disclose WHICH risk-free rate backed that excess-return convention —
        # the historical 3-month T-bill series (per-window aligned) or the flat-5%
        # fallback (no date index supplied, or the window fell outside the vendored
        # series' coverage, both loudly logged at resolution time — never silent).
        #
        # #1409 round-4 review fix: `self.rf_convention` is set unconditionally by
        # `_resolve_gate_rf` from `dates` alone, independent of whether DSR/OOS/IS
        # actually produced a number from it. When NONE of the three excess-return
        # metrics were computed at all (e.g. too-short/degenerate series), disclosing
        # "excess_flat_fallback" implies a flat-rate COMPUTATION happened here, when
        # in fact no computation of any kind ran — the same distinction
        # `_library_pbo_payload` already draws for the analogous `LibraryPbo` case
        # ("MISSING", not whichever convention the resolution alone would have
        # picked, when there is no PBO value to attribute a convention to). Any ONE
        # of the three succeeding is enough to keep the real convention — that
        # metric genuinely used it, and this must not diverge from what OTHER
        # gate_details entries (e.g. `details["dsr"]`) say about the SAME computation.
        details["rf_convention"] = (
            "MISSING"
            if self.deflated_sharpe is None and self.oos_sharpe is None and self.in_sample_sharpe is None
            else self.rf_convention
        )
        # Disclose the standard-error model behind the DSR (#621 follow-up): the
        # gate uses a Newey–West HAC SE robust to serial dependence. Surface the
        # IID-SE p-value as a delta so the reader sees the size of the correction.
        if self.dsr_se_method == "hac":
            if self.dsr_p_value is not None and self.dsr_p_value_iid is not None:
                details["dsr_se"] = (
                    f"HAC (Newey–West); IID-SE p={self.dsr_p_value_iid:.4f} "
                    f"→ HAC p={self.dsr_p_value:.4f} (Δ={self.dsr_p_value - self.dsr_p_value_iid:+.4f})"
                )
            else:
                details["dsr_se"] = "HAC (Newey–West)"
        else:
            details["dsr_se"] = "IID (classical Bailey-LdP)"

        # Criterion 4 input provenance (#546): "library" = full-library CSCV PBO
        # (the principled Bailey et al. selection-set input), "cohort" = the
        # per-cohort dict score. Disclosed so the verdict states which set fed it.
        # #819: below the CSCV power floor, say so explicitly instead of quietly
        # reporting PASS/FAIL/MISSING on a statistic that isn't load-bearing here —
        # still show the number (advisory) when one was computed.
        if self._pbo_below_power_floor:
            floor_note = (
                f"NOT_RUN (N={self.pbo_library_size} below the CSCV power floor of {MIN_LIBRARY_N_FOR_PBO_GATING})"
            )
            if self.pbo_score is not None and math.isfinite(self.pbo_score):
                details["pbo"] = f"{floor_note}; PBO={self.pbo_score:.4f} advisory only, source={self.pbo_source}"
            else:
                details["pbo"] = floor_note
        elif self.pbo_score is not None and math.isfinite(self.pbo_score) and self.pbo_score < self.profile.pbo_max:
            details["pbo"] = f"PASS (PBO={self.pbo_score:.4f}, source={self.pbo_source})"
        elif self.pbo_score is not None and math.isfinite(self.pbo_score):
            details["pbo"] = (
                f"FAIL (PBO={self.pbo_score:.4f}, need < {self.profile.pbo_max:.2f}, source={self.pbo_source})"
            )
        else:
            details["pbo"] = f"MISSING (source={self.pbo_source})"

        if self.is_degenerate:
            details["oos_sharpe"] = (
                "DEGENERATE (zero-variance persisted return series — broken data or a zero-trade backtest)"
            )
        elif self.oos_sharpe is not None and self.in_sample_sharpe and self.in_sample_sharpe > 0:
            ratio = self.oos_sharpe / self.in_sample_sharpe
            if ratio >= self.profile.oos_is_ratio_min:
                details["oos_sharpe"] = f"PASS (OOS/IS={ratio:.2f})"
            else:
                details["oos_sharpe"] = f"FAIL (OOS/IS={ratio:.2f}, need ≥ {self.profile.oos_is_ratio_min:.2f})"
        elif self.oos_sharpe is not None:
            details["oos_sharpe"] = f"SET (OOS={self.oos_sharpe:.4f}, no IS reference)"
        else:
            details["oos_sharpe"] = "MISSING"

        if self.cpcv_positive_fraction is not None:
            if self.cpcv_positive_fraction >= 0.5:
                details["cpcv"] = (
                    f"PASS (OOS+ {self.cpcv_positive_fraction:.0%} of paths, "
                    f"mean OOS SR={self.cpcv_mean_oos_sharpe:.2f})"
                )
            else:
                details["cpcv"] = f"FAIL (OOS+ only {self.cpcv_positive_fraction:.0%} of paths, need ≥ 50%)"
        else:
            # Honest not-run status (#771): the surface must never advertise CPCV as a
            # method while silently emitting a bare "MISSING". CPCV needs a 2-D (S, T)
            # matrix of per-combinatorial-split OOS returns; it is mathematically invalid
            # on a single static return series, so when no matrix is supplied we say so
            # explicitly rather than implying a method that produced no number.
            details["cpcv"] = (
                "NOT_RUN (no combinatorial OOS matrix supplied; CPCV is invalid on a single static return series)"
            )

        # Four-state, for the same reason `cpcv` and `oos_sharpe` above are: a
        # check that never reached a verdict must not be rendered as one that ran
        # and failed. Admission is unchanged in every branch — `blocked_by_floor`
        # reads `look_ahead_passed`, which is False for all three non-passes — so
        # this is honesty on the surface, not a loosened gate. "FAIL" is reserved
        # for an audit that actually looked and found something; the other two
        # say what really happened, and say that they still block.
        #
        #   pass       → PASS
        #   degenerate → DEGENERATE  (the audit ran, its instrument gave no reading)
        #   pending    → NOT_RUN     (nothing was audited)
        #   fail       → FAIL        (a leak was found)
        #
        # A caller that supplies no `look_ahead_status` (every non-DSL path, and
        # every row written before the four-state verdict existed) keeps the
        # earlier two/three-state wording, driven by `look_ahead_not_run_reason`.
        if self.look_ahead_passed:
            details["look_ahead"] = "PASS"
        elif self.look_ahead_status == _LA_DEGENERATE:
            details["look_ahead"] = (
                f"DEGENERATE ({self.look_ahead_not_run_reason or 'the look-ahead audit could not reach a verdict'})"
                " — blocks admission (fail-closed)"
            )
        elif self.look_ahead_status == _LA_PENDING or self.look_ahead_not_run_reason:
            reason = self.look_ahead_not_run_reason or "the look-ahead audit did not run"
            details["look_ahead"] = f"NOT_RUN ({reason}) — blocks admission (fail-closed)"
        else:
            details["look_ahead"] = "FAIL"

        # IID / random-walk diagnostic (#621) — ADVISORY, never gates pass/fail.
        # The diagnostic no longer rests on an SE it cannot defend: the gate's
        # DSR now uses a Newey–West HAC standard error (see details["dsr_se"]),
        # so a detected autocorrelation is *corrected* in the verdict, not merely
        # flagged. We still surface the diagnostic because it tells the reader
        # WHY the HAC correction did (or did not) move the p-value — and because
        # autocorrelation is the expected, legitimate signal for trend/momentum.
        if self.iid_assumption_violated is None:
            details["iid"] = "MISSING"
        elif self.iid_assumption_violated:
            details["iid"] = (
                "ADVISORY: autocorrelation detected — Sharpe SE corrected via HAC (Newey–West); "
                "expected for trend/momentum, where the autocorrelation is the edge"
            )
        else:
            details["iid"] = "ADVISORY: returns consistent with IID / random-walk (HAC ≈ classical SE)"

        # Regime-robustness — ADVISORY, never gates pass/fail. Shows whether the edge
        # survives across volatility regimes or is fragile (earned in only one).
        rr = self.regime_robustness
        if not rr or rr.get("min_regime_sharpe") is None:
            details["regime_robustness"] = "MISSING"
        elif rr.get("robust"):
            details["regime_robustness"] = (
                f"ADVISORY: edge holds across regimes (min regime SR={rr['min_regime_sharpe']:.2f}, "
                f"consistency={rr.get('consistency', 0.0):.2f})"
            )
        else:
            details["regime_robustness"] = (
                f"ADVISORY: regime-fragile (min regime SR={rr['min_regime_sharpe']:.2f} ≤ 0 in ≥1 regime, "
                f"consistency={rr.get('consistency', 0.0):.2f})"
            )

        return details


def _resolve_gate_rf(dates: list | None, n: int) -> tuple[list | None, str]:
    """Resolve the ONE rf convention ``run_rigor_gate`` discloses, matching
    EXACTLY what DSR/OOS/IS below will use — by construction, not by a second,
    independent call after the fact.

    **2026-08-20 review fix.** The disclosed marker was previously derived
    from ``dates`` alone (never validated against ``n``, the return series'
    own length) via a call that ran in parallel with — not shared with — the
    DSR/OOS/IS resolutions below, each of which resolves its OWN slice
    (``dates`` in full for DSR, ``dates[split:]`` for OOS, ``dates[:split]``
    for IS). Two ways that could disagree, both reproduced:
      - A ``dates`` list whose length didn't match ``daily_returns`` made the
        marker compute (from the raw, mismatched ``dates``) while every metric
        silently fell back to flat via ``_resolve_rf_daily_array``'s own
        length guard — marker said series, math ran flat.
      - A window whose tail ran past the vendored series' 14-day forward-fill
        grace window made the FULL-list marker resolution fail (-> disclosed
        FALLBACK) while the IS (or OOS) slice, not containing the stale tail,
        resolved fine on its own -> that metric silently used the T-bill
        series while the marker claimed flat. Self-triggers as the vendored
        CSV ages, no code change needed to hit it.
    Both are exactly the "silent partial substitution" issue #1409 design
    item 4 forbids.

    Returns ``(resolved_dates, rf_convention)``:
      - ``dates is None`` or ``len(dates) != n`` (the same caller-bug guard
        ``_resolve_rf_daily_array`` applies per-call) -> ``(None,
        RF_CONVENTION_FALLBACK)``.
      - Every date in ``dates`` resolves against the vendored series -> the
        WHOLE list, unchanged, plus ``RF_CONVENTION_SERIES``.
      - ANY date is unresolvable (before the series' first published date, or
        beyond the 14-day grace window past its last one, or the vendored
        file itself is unavailable) -> ``(None, RF_CONVENTION_FALLBACK)``.

    The caller MUST thread ``resolved_dates`` — never the original ``dates``
    — into every downstream DSR/OOS/IS call. That is safe, not merely
    convenient, for two reasons:
      1. When this returns FALLBACK, ``resolved_dates`` is ``None``, so every
         downstream call takes the exact flat ``dates=None`` code path — the
         SAME byte-identical arithmetic the marker discloses, regardless of
         which sub-slice a caller further down happens to take.
      2. When this returns SERIES, every date in the FULL window resolved.
         Resolvability is per-date (exact match or forward-fill against the
         vendored series' own coverage/grace bounds), never list-order- or
         list-membership-dependent — so any bar-aligned SUB-SLICE of a fully
         resolvable list (the OOS tail, the IS head) is guaranteed to resolve
         too. A downstream call slicing ``resolved_dates`` can therefore never
         land on FALLBACK when this function reported SERIES.
    Either way, the whole grade agrees with the disclosed marker BY
    CONSTRUCTION — one resolution decision governs every metric, not four
    independent ones that happen to agree.
    """
    if dates is None:
        return None, rf_series.RF_CONVENTION_FALLBACK
    dates_list = list(dates)
    if len(dates_list) != n:
        logger.warning(
            "run_rigor_gate: dates length (%d) != return-series length (%d) — "
            "whole grade falls back to the flat risk-free rate (disclosed "
            "fallback, rf_convention=%s)",
            len(dates_list),
            n,
            rf_series.RF_CONVENTION_FALLBACK,
        )
        return None, rf_series.RF_CONVENTION_FALLBACK
    _, convention = rf_series.resolve_annual_rf_for_dates(dates_list, flat_annual_pct=_RF_ANNUAL * 100.0)
    if convention == rf_series.RF_CONVENTION_FALLBACK:
        return None, rf_series.RF_CONVENTION_FALLBACK
    return dates_list, rf_series.RF_CONVENTION_SERIES


def run_rigor_gate(
    strategy_id: str,
    daily_returns: list[float],
    num_trials: int = 1,
    pbo_scores: dict[str, float] | None = None,
    strategy_code: str | None = None,
    in_sample_sharpe: float | None = None,
    paper_claimed_sharpe: float | None = None,
    look_ahead_audit_passed: bool | None = None,
    look_ahead_status: str | None = None,
    look_ahead_not_run_reason: str | None = None,
    average_correlation: float = 0.0,
    cv_returns_matrix: np.ndarray | list[list[float]] | None = None,
    library_pbo: float | None = None,
    pbo_library_size: int | None = None,
    strictness_level: int = DEFAULT_LEVEL,
    dates: list | None = None,
) -> RigorGateResult:
    """Run all four selection-bias checks on a strategy.

    Main entry point called by the orchestrator and API routes.

    Args:
        dates: Optional per-bar date index, 1:1 with ``daily_returns`` (issue
            #1409). When supplied, DSR/OOS-Sharpe/in-sample-Sharpe are graded
            against the historical 3-month T-bill series (FRED DGS3MO,
            per-window aligned — see ``rf_series.py``) instead of the flat 5%
            fallback; the choice is disclosed on the result as
            ``rf_convention`` / ``gate_details["rf_convention"]``, riding the
            same payload paths ``dsr_convention`` already rides. Omitted (the
            default) keeps every pre-#1409 caller's exact behavior — the flat
            rate, loudly disclosed as ``rf_convention="excess_flat_fallback"``.
        strictness_level: The strictness level (1 = Conservative/strictest =
            the Archimedes Verified badge bar … 5 = Speculative/loosest) whose
            thresholds ``result.passes_all`` / ``gate_details`` report against.
            Defaults to the strictest level, so an unspecified strictness is
            fail-safe (the badge). The returned :class:`RigorGateResult` also
            exposes ``passes_at_level`` / ``min_passing_level`` for querying the
            whole ladder from a single computation — the metrics themselves are
            strictness-independent, only the thresholds differ.
        library_pbo: The single full-library CSCV PBO (Bailey et al. 2014) for
            the *current selection set* — the principled criterion-4 input
            (#546). PBO is a property of the whole library, not of one strategy,
            so when this is supplied it is used for criterion 4 in preference to
            the per-cohort ``pbo_scores`` lookup. Compute it from the
            daily-returns store via ``compute_library_pbo`` and recompute on
            every selection-set change (see that function's refresh-policy note).
            Fail-closed: ``None`` (or a non-finite value) makes criterion 4 FAIL
            rather than silently pass — exactly as a missing cohort score does.
            When ``None``, the gate falls back to ``pbo_scores.get(strategy_id)``
            and the verdict is labelled ``source=cohort``.
        look_ahead_audit_passed: An externally computed look-ahead verdict, for
            callers whose "code" is a closed DSL spec with no inspectable source
            for the AST audit. ONLY this boolean decides admission.
        look_ahead_status: The four-state verdict behind that boolean
            (``"pass"``/``"fail"``/``"pending"``/``"degenerate"``, from
            ``services/dsl_lookahead_audit.py``), supplied so
            ``gate_details["look_ahead"]`` can say which kind of non-pass it was.
            Rendering only — it moves nothing in the gate. ``None`` keeps the
            earlier wording, which is right for every non-DSL caller.
        average_correlation: Mean pairwise correlation among the ``num_trials``
            trials in the selection set, used by the DSR effective-N correction.
            The caller holds the library/variant returns and computes it via
            ``compute_average_pairwise_correlation``; ``0.0`` (the default)
            applies no relief — conservative for an unknown correlation.
        cv_returns_matrix: A 2-D ``(S, T)`` matrix of per-split out-of-sample
            returns (rows = ``C(n_groups, test_groups)`` combinatorial splits)
            for the CPCV path-stability check. CPCV is mathematically invalid on
            a single static 1-D series, so when no combinatorial paths are
            supplied this stays ``None`` and the CPCV gate is honestly reported
            as ``MISSING`` rather than silently passing.
        pbo_library_size: The N the PBO cross-section was computed over, used to
            decide whether criterion 4 gates at all (#819 — CSCV is underpowered
            below ``MIN_LIBRARY_N_FOR_PBO_GATING``). Defaults to ``len(pbo_scores)``
            when not given explicitly and ``pbo_scores`` is the PBO source (the
            cohort dict already has one entry per strategy in the cross-section,
            so its length IS that N) — the common case needs no caller change.
            When ``library_pbo`` is supplied instead, ``pbo_scores``'s length is
            unrelated to the library-PBO cross-section's size, so pass this
            explicitly if that path should ever gate on the floor too.
    """
    if num_trials == 1:
        # Debug-level breadcrumb (#902; demoted from WARNING in the #1075
        # review): num_trials=1 zeroes E[max SR] and collapses DSR to a plain
        # "Sharpe > 0" test. Pre-decouple that was anomalous and deserved a
        # loud line; post-decouple-#2 it is the DOCUMENTED DEFAULT for every
        # curated serving call (list/detail/gate), so at WARNING it would spam
        # the hot path with non-actionable noise. Kept at debug as the tell
        # for the one real misuse: a caller with an N-candidate pool or
        # variant grid that forgot to pass its own count.
        logger.debug(
            "Rigor gate [%s]: num_trials=1 — DSR runs UNDEFLATED (no multiple-testing correction). "
            "Correct for a self-contained strategy; if this strategy has its own real selection "
            "set (an N-candidate generation pool or parameter-variant grid), pass that count "
            "explicitly instead — never the curated library's size.",
            strategy_id,
        )

    # 1. DSR — serial-correlation-robust standard error (#621 follow-up).
    #    The Deflated Sharpe z-statistic's IID variance term understates sampling
    #    uncertainty when returns are autocorrelated (Lo 2002), inflating
    #    significance — exactly the premise the IID diagnostic detects. The gate
    #    therefore uses the Newey–West (1987) HAC long-run variance of the Sharpe
    #    influence function, which NESTS the IID form (a no-op on serially
    #    independent returns) and avoids the pre-test distortion of conditionally
    #    swapping SEs only when the IID flag fires (Leeb & Pötscher 2005). The
    #    effective-N correction (average_correlation) still relaxes the
    #    multiple-testing penalty when the trials themselves are correlated.
    #    The HAC-robust verdict (gating) and the IID-SE verdict (advisory) are
    #    computed in a single pass — one numpy coercion, one SciPy moment
    #    computation, one influence-function LRV — so this is ~half the work of
    #    two compute_dsr calls on a path that may evaluate many candidates. The
    #    IID-SE p-value is surfaced as a delta (never gates) so the passport
    #    shows how much the serial-dependence correction moved the verdict.
    # #1409 (2026-08-20 review fix): resolve the ONE rf convention this WHOLE
    # GRADE uses, once, before DSR runs — not a second, independent call after
    # the fact. `resolved_dates` (never the caller's raw `dates`) is threaded
    # into DSR/OOS/IS below, so the disclosed marker and the arithmetic every
    # metric actually performed can never disagree. See `_resolve_gate_rf`'s
    # docstring for why re-slicing `resolved_dates` downstream is safe.
    resolved_dates, rf_convention = _resolve_gate_rf(dates, len(daily_returns))

    deflated_sharpe, dsr_p_value, _, dsr_p_value_iid = compute_dsr_hac_and_iid(
        daily_returns, num_trials, average_correlation, hac_lags="auto", dates=resolved_dates
    )

    # #1184: is the None DSR (and, symmetrically, the None OOS Sharpe computed
    # below) an UNDEFINED-MATH artifact of a zero-variance series, or one of the
    # gate's ordinary reasons for a None (too short, gate genuinely lost)? Two
    # predicates, ORed, because compute_dsr and compute_oos_sharpe guard on ptp
    # over DIFFERENT slices (full series vs. the chronological OOS tail) — a
    # series can be flat only inside the OOS window while non-constant overall
    # (e.g. a strategy that stops trading partway through). Each predicate
    # mirrors its corresponding helper's own guard, so this agrees with BOTH
    # by construction rather than re-deriving either condition.
    #
    # The two predicates are also carried onto the result SEPARATELY (not just
    # via their OR): ``gate_details["dsr"]`` must know whether the FULL series
    # was flat (DSR undefined) specifically, since an OOS-only-flat series
    # leaves the full-series DSR computation — and therefore its legitimacy —
    # untouched (#1184 round-2 finding).
    is_full_series_degenerate = is_zero_variance_series(daily_returns)
    is_oos_degenerate = is_oos_zero_variance_series(daily_returns)
    is_degenerate = is_full_series_degenerate or is_oos_degenerate

    # 2. PBO (criterion 4 in the gate ordering) — prefer the full-library CSCV
    #    PBO when supplied (#546). PBO is a property of the selection set, not of
    #    one strategy (Bailey et al. 2014), so the library-wide value is the
    #    principled input; the per-cohort dict is the fallback when no library
    #    PBO is passed. Fail-closed semantics live in RigorGateResult.passes_all:
    #    a None/NaN pbo_score fails criterion 4 regardless of source.
    if library_pbo is not None:
        pbo_score = library_pbo
        pbo_source = "library"
        # pbo_scores's length isn't the library-PBO cross-section's N, so don't
        # auto-derive here — only an explicit pbo_library_size arg applies (#819).
        effective_pbo_library_size = pbo_library_size
    else:
        pbo_score = pbo_scores.get(strategy_id) if pbo_scores else None
        pbo_source = "cohort"
        effective_pbo_library_size = (
            pbo_library_size if pbo_library_size is not None else (len(pbo_scores) if pbo_scores else None)
        )

    # 3. Walk-forward OOS Sharpe (single holdout) + Combinatorial Purged CV.
    #    CPCV runs only when a real 2-D combinatorial OOS matrix is supplied.
    oos_sharpe = compute_oos_sharpe(daily_returns, dates=resolved_dates)
    cpcv = compute_cpcv_oos_sharpe(cv_returns_matrix)

    # IID / random-walk diagnostics (#621) — computed AND surfaced (previously
    # computed-and-dropped). ADVISORY only: it never gates pass/fail because a
    # trend/momentum strategy's autocorrelation is its edge, so an IID violation
    # must not reject it. See RigorGateResult.passes_all / gate_details["iid"].
    iid = diagnose(daily_returns)

    # Regime-robustness diagnostic — does the edge survive across volatility regimes,
    # or is it earned only in calm/only-in-one regime (fragile)? Uses vol-based regime
    # labels derived from the series itself (classify_regimes). ADVISORY like IID — it is
    # surfaced, never gates pass/fail: a single-regime edge can still be a legitimate,
    # honestly-disclosed strategy, and promoting this to a hard gate is an admission-policy
    # call for the rigor lane (Dan/Önder). Computed only when the series is long enough to
    # label more than one regime; never allowed to break the gate.
    regime_robustness: dict | None = None
    if len(daily_returns) >= 63:  # ~3 vol windows (vol_window=21) — enough to label >1 regime
        try:
            regime_labels = classify_regimes(daily_returns)
            regime_robustness = regime_robustness_score(daily_returns, regime_labels)
        except Exception as exc:  # an advisory diagnostic must never break the gate
            logger.debug("Regime-robustness diagnostic skipped [%s]: %s", strategy_id, exc)

    # 4. Look-ahead audit
    la_not_run_reason = look_ahead_not_run_reason
    if strategy_code is not None:
        la_passed, la_warnings = look_ahead_audit(strategy_code)
        if la_warnings:
            for w in la_warnings:
                logger.info("Look-ahead audit [%s]: %s", strategy_id, w)
    else:
        la_passed = False
        if look_ahead_audit_passed is None and la_not_run_reason is None:
            # Nothing to audit and nobody supplied a verdict: the leg did not run.
            # It still blocks admission (la_passed stays False) — but the detail
            # line must say "not run", not "FAIL". A check that never looked did
            # not catch anything.
            la_not_run_reason = (
                "no strategy source to audit and no external look-ahead verdict supplied "
                "(the AST audit needs inspectable code)"
            )

    if look_ahead_audit_passed is not None:
        la_passed = look_ahead_audit_passed
        if la_passed:
            la_not_run_reason = None

    # The four-state verdict is a RENDERING input only — `la_passed` above is the
    # single thing admission reads. A caller that claims a pass cannot also claim
    # the leg never ran, so a `pass` clears the not-run wording either way.
    la_status = look_ahead_status
    if la_passed:
        la_status = None

    # Derive in-sample Sharpe from IS slice (first 70%) only — not the full series.
    # Using the full series blends IS+OOS and makes the OOS/IS ratio trivially easy to pass.
    if in_sample_sharpe is None and len(daily_returns) >= 4:
        arr = np.asarray(daily_returns, dtype=float)
        split = int(len(arr) * 0.70)
        is_arr = arr[:split]
        if len(is_arr) >= 2:
            sigma_is = float(is_arr.std(ddof=1))
            if sigma_is > 0:
                if resolved_dates is not None:
                    # #1409: same excess-series treatment _rigor_helpers uses — mean
                    # AND sigma both drawn from the excess series, not just the mean.
                    # `resolved_dates` (not the caller's raw `dates`) — see
                    # `_resolve_gate_rf` for why this slice can never disagree
                    # with the disclosed `rf_convention` above.
                    is_dates = list(resolved_dates)[:split]
                    rf_daily_is = _resolve_rf_daily_array(is_dates, len(is_arr))
                    excess_is = is_arr - rf_daily_is
                    sigma_excess_is = float(excess_is.std(ddof=1))
                    if sigma_excess_is > 0:
                        in_sample_sharpe = (float(excess_is.mean()) / sigma_excess_is) * math.sqrt(_ANNUALIZATION)
                else:
                    in_sample_sharpe = ((float(is_arr.mean()) - _RF_DAILY) / sigma_is) * math.sqrt(_ANNUALIZATION)

    result = RigorGateResult(
        strategy_id=strategy_id,
        deflated_sharpe=deflated_sharpe,
        dsr_p_value=dsr_p_value,
        dsr_p_value_iid=dsr_p_value_iid,
        dsr_se_method="hac",
        num_trials=num_trials,
        pbo_score=pbo_score,
        oos_sharpe=oos_sharpe,
        look_ahead_passed=la_passed,
        look_ahead_not_run_reason=la_not_run_reason,
        look_ahead_status=la_status,
        in_sample_sharpe=in_sample_sharpe,
        paper_claimed_sharpe=paper_claimed_sharpe,
        cpcv_mean_oos_sharpe=cpcv["mean_oos_sharpe"] if cpcv else None,
        cpcv_positive_fraction=cpcv["positive_fraction"] if cpcv else None,
        pbo_source=pbo_source,
        iid_assumption_violated=iid.get("iid_assumption_violated"),
        iid_diagnostics=iid,
        regime_robustness=regime_robustness,
        pbo_library_size=effective_pbo_library_size,
        is_degenerate=is_degenerate,
        is_full_series_degenerate=is_full_series_degenerate,
        is_oos_degenerate=is_oos_degenerate,
        profile=get_profile(strictness_level),
        rf_convention=rf_convention,
    )

    logger.info(
        "Rigor gate [%s]: %s (DSR p=%s, PBO=%s [%s], OOS=%s, CPCV+=%s, LA=%s, IID_violated=%s, "
        "regime_robust=%s [advisories], rf_convention=%s)",
        strategy_id,
        result.tri_state_status.upper(),
        dsr_p_value,
        pbo_score,
        pbo_source,
        oos_sharpe,
        cpcv["positive_fraction"] if cpcv else None,
        la_passed,
        iid.get("iid_assumption_violated"),
        regime_robustness.get("robust") if regime_robustness else None,
        rf_convention,
    )

    return result
