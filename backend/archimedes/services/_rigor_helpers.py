"""Helper functions for rigor gate calculations: DSR, PBO, Sharpe, Kelly, etc.

Extracted from rigor_evaluator.py to reduce module complexity. Contains:
  - Deflated Sharpe Ratio (DSR) computation
  - Probability of Backtest Overfitting (PBO) via CSCV
  - Walk-forward OOS Sharpe and in-sample Sharpe
  - Kelly Criterion position sizing
  - Sharpe confidence intervals
  - Regime classification and conditional analysis
  - Board-level multiple-testing correction (Benjamini-Hochberg FDR, #1185)

All functions are pure computation: no I/O, no web framework, no on-chain
dependencies. They consume daily return arrays and produce rigor-gate metrics.

Owner: Önder (math lane)
Spec:  docs/specs/selection-bias-corrections-spec.md
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import date, datetime
from itertools import combinations
from typing import Any

import numpy as np
from scipy.stats import kurtosis as sp_kurtosis
from scipy.stats import norm
from scipy.stats import skew as sp_skew

from archimedes.services import rf_series

logger = logging.getLogger(__name__)

_EULER_MASCHERONI = 0.5772156649
_ANNUALIZATION = 252

# The DSR's minimum sample, named because an API boundary has to reject against
# THIS number rather than pick a second one (#1803). Below four bars the skew
# and raw kurtosis the Bailey-LdP (2014) denominator is defined on are not
# estimable, so every DSR entry point below returns ``None`` instead of a
# number. `POST /api/rigor/verify` imports it to refuse a shorter series at the
# schema, with a message that says the DSR could not have run anyway — the
# alternative is a 200 whose every leg is `not_evaluable`.
DSR_MIN_BARS = 4
# GATE-side rf convention: the DSR deflates an EXCESS-return Sharpe, because
# Bailey-LdP (2014) is defined on excess returns. This deliberately differs from
# the passport DISPLAY Sharpe, which uses rf=0 (raw) — see fusion_evaluator.py's
# rf_annual=0.0 call sites. The split is intentional (gate=excess, display=raw),
# not drift; the two metrics answer different questions. (audit 2026-06-13 #8)
#
# #1409: this flat scalar is now the FALLBACK convention only — the primary
# convention is the historical 3-month T-bill series (FRED DGS3MO, see
# rf_series.py), used whenever a caller threads the return series' DATE index
# through the optional `dates` parameter on the four functions below. Callers
# that don't pass `dates` (every pre-#1409 call site) get this EXACT flat
# value, computed on the EXACT pre-#1409 code path — byte-identical behavior,
# not merely an equivalent approximation.
_RF_ANNUAL = 0.05  # 5% annual — flat FALLBACK convention only as of #1409, see the comment block above
_RF_DAILY = _RF_ANNUAL / _ANNUALIZATION


def _resolve_rf_daily_array(dates: Sequence[str | date | datetime] | None, n: int) -> np.ndarray:
    """Per-bar daily risk-free-rate array of length ``n``, aligned to ``dates``.

    ``dates=None`` returns the flat ``_RF_DAILY`` broadcast (the pre-#1409
    fallback, unchanged) — callers that don't have a date index keep working
    exactly as before. When ``dates`` is supplied but its length disagrees
    with ``n`` (a caller bug — the return series and its date index must be
    1:1), this falls back the same way rather than raising, logging loudly so
    the mismatch is visible without breaking the gate. Otherwise delegates to
    ``rf_series.resolve_annual_rf_for_dates`` for the real forward-fill /
    14-day-grace / fallback logic and converts FRED's percent units to a daily
    fraction (rate/100/252, the same annualization this module uses
    everywhere else).
    """
    if dates is None:
        return np.full(n, _RF_DAILY, dtype=float)
    dates_list = list(dates)
    if len(dates_list) != n:
        logger.warning(
            "_rigor_helpers: dates length (%d) != return-series length (%d) — falling back to the "
            "flat %.2f%% risk-free rate for this call (disclosed fallback, rf_convention=%s)",
            len(dates_list),
            n,
            _RF_ANNUAL * 100.0,
            rf_series.RF_CONVENTION_FALLBACK,
        )
        return np.full(n, _RF_DAILY, dtype=float)
    rf_pct, _convention = rf_series.resolve_annual_rf_for_dates(dates_list, flat_annual_pct=_RF_ANNUAL * 100.0)
    return np.asarray(rf_pct, dtype=float) / 100.0 / _ANNUALIZATION


# ─── 1. Deflated Sharpe Ratio ────────────────────────────────────────


def _nw_auto_bandwidth(T: int) -> int:
    """Newey & West (1994) automatic-bandwidth rule for HAC estimation.

    ``L = floor(4 · (T/100)^(2/9))`` — the canonical data-independent plug-in
    (Newey & West 1994, "Automatic Lag Selection in Covariance Matrix
    Estimation", *Rev. Econ. Stud.* 61(4): 631–653; the default in common
    econometric packages). For ``T = 252`` daily bars this yields ``L = 4``.
    Floored at 1; the caller caps it at ``T − 1``.
    """
    return max(1, int(math.floor(4.0 * (T / 100.0) ** (2.0 / 9.0))))


def _sharpe_influence_lrv(
    arr: np.ndarray,
    SR_hat: float,
    mean: float,
    sigma: float,
    hac_lags: int | str,
) -> float | None:
    """Newey–West HAC long-run variance of the Sharpe-ratio influence function.

    The Deflated-Sharpe z-statistic's denominator is the asymptotic variance of
    the per-bar Sharpe estimator. The IID form
    ``V_IID = 1 − γ₃·ŜR + ((γ₄−1)/4)·ŜR²`` (Bailey & López de Prado 2014 eq. 8;
    Mertens 2002) is only valid for serially independent returns: positive
    autocorrelation understates the standard error and inflates significance.

    The influence function of the Sharpe ratio (Lo 2002, "The Statistics of
    Sharpe Ratios", *FAJ* 58(4): 36–52 — delta method on the first two sample
    moments) is

        IF_t = z_t − (ŜR/2)(z_t² − 1),   z_t = (x_t − μ)/σ,

    and its IID variance equals ``V_IID`` exactly. The heteroskedasticity- and
    autocorrelation-consistent (HAC) variance is the Newey & West (1987,
    *Econometrica* 55(3): 703–708) long-run variance with a Bartlett kernel:

        LRV = γ₀ + 2·Σ_{k=1}^{L} (1 − k/(L+1))·γ_k,   γ_k = Cov(IF_t, IF_{t−k}).

    This nests the IID case (``γ_k → 0 ⇒ LRV → γ₀ ≈ V_IID``) and the Bartlett
    weights guarantee ``LRV ≥ 0``. ``hac_lags`` is the maximum lag ``L``, or
    ``"auto"`` for the Newey–West (1994) plug-in bandwidth.

    Returns the long-run variance (the drop-in replacement for ``denom_sq`` in
    the z-statistic), or ``None`` when the series is too short / degenerate.
    """
    T = len(arr)
    if T < DSR_MIN_BARS or sigma <= 0.0:
        return None
    z = (arr - mean) / sigma
    influence = z - 0.5 * SR_hat * (z * z - 1.0)
    influence = influence - influence.mean()  # population mean of IF_t is 0
    g0 = float(influence @ influence) / T  # γ₀ — empirical IF variance (≈ V_IID under IID)
    L = _nw_auto_bandwidth(T) if hac_lags == "auto" else int(hac_lags)
    L = max(0, min(L, T - 1))
    lrv = g0
    for k in range(1, L + 1):
        gamma_k = float(influence[k:] @ influence[:-k]) / T  # lag-k autocovariance of IF
        weight = 1.0 - k / (L + 1.0)  # Bartlett kernel
        lrv += 2.0 * weight * gamma_k
    return max(lrv, 1e-12)  # Bartlett kernel ⇒ PSD; clip guards only FP error


def _sharpe_dsr_inputs(
    daily_returns: list[float] | np.ndarray,
    dates: Sequence[str | date | datetime] | None = None,
) -> tuple[np.ndarray, int, float, float, float, float, float] | None:
    """Validate the return series and extract the DSR's moments exactly once.

    Returns ``(arr, T, SR_hat, sigma, mean, gamma_3, gamma_4)`` — the per-bar
    excess Sharpe plus the skew / raw-kurtosis consumed by ``_dsr_from_stats`` —
    or ``None`` when the series is too short (T < 4) or degenerate (zero range /
    zero volatility). Shared by ``compute_dsr`` and ``compute_dsr_hac_and_iid``
    so the numpy coercion + SciPy moment computation runs once on the rigor
    gate's hot path.

    γ₄ is RAW (Pearson) kurtosis (= 3 for normal), matching Bailey-LdP (2014)
    eq. 8; passing Fisher excess here would shift the denominator by a constant
    (3/4)·ŜR² and bias every DSR.

    #1409: ``dates`` is optional and, when omitted (the default), takes the
    EXACT pre-#1409 code path below (flat ``_RF_DAILY`` subtracted only from
    the mean; sigma/skew/kurtosis computed on the raw series) — byte-identical
    to every caller that hasn't threaded a date index through. When ``dates``
    IS supplied, the whole excess-return series (``arr - rf_daily_per_bar``) is
    computed first and mean/sigma/skew/kurtosis are ALL drawn from it — the
    mathematically correct treatment when rf varies bar-to-bar (Sharpe 1994):
    subtracting a time-varying series, unlike a constant, can move sigma and
    the higher moments too, not just the mean.
    """
    arr = np.asarray(daily_returns, dtype=float)
    T = len(arr)
    if T < DSR_MIN_BARS:
        return None
    # numpy std(ddof=1) can be a tiny non-zero float for identical values due to
    # floating-point cancellation; check range first to catch constant series.
    if float(np.ptp(arr)) == 0.0:
        return None

    if dates is not None:
        rf_daily = _resolve_rf_daily_array(dates, T)
        excess = arr - rf_daily
        sigma = float(excess.std(ddof=1))
        if sigma <= 0.0:
            return None
        mean = float(excess.mean())
        SR_hat = mean / sigma  # already excess, un-annualized
        gamma_3 = float(sp_skew(excess))
        gamma_4 = float(sp_kurtosis(excess, fisher=False))
        # Return the EXCESS series (not raw `arr`) as the first element: the
        # HAC influence-function variance (`_sharpe_influence_lrv`, called by
        # `compute_dsr_hac_and_iid` as `_sharpe_influence_lrv(arr, SR_hat, mean,
        # sigma, ...)`) computes `z = (arr - mean) / sigma` — that must be the
        # SAME series `mean`/`sigma` were drawn from, or the z-scores are
        # incoherent (raw arr against an excess mean/sigma).
        return excess, T, SR_hat, sigma, mean, gamma_3, gamma_4

    sigma = float(arr.std(ddof=1))
    if sigma <= 0.0:
        return None
    mean = float(arr.mean())
    SR_hat = (mean - _RF_DAILY) / sigma  # excess return per bar, un-annualized
    gamma_3 = float(sp_skew(arr))
    gamma_4 = float(sp_kurtosis(arr, fisher=False))  # raw (Pearson) kurtosis
    return arr, T, SR_hat, sigma, mean, gamma_3, gamma_4


def compute_dsr(
    daily_returns: list[float] | np.ndarray,
    num_trials: int,
    average_correlation: float = 0.0,
    hac_lags: int | str | None = None,
    dates: Sequence[str | date | datetime] | None = None,
) -> tuple[float | None, float | None]:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    Corrects the observed Sharpe for non-normality (skew + excess kurtosis)
    and multiple-testing inflation (selection across N candidate strategies).

    Args:
        daily_returns: Per-bar (daily) return series, un-annualized.
        num_trials: Number of strategies in the selection set (N). Pass 1
            if this is the only strategy ever evaluated — no correction
            will be applied. The orchestrator should pass
            len(strategy_library) so the correction is meaningful.
        average_correlation: The average pairwise correlation between the
            trials. Used to compute the effective number of independent
            trials (Bailey-López de Prado variance-of-trials correlation model).
        hac_lags: When not None, replaces the IID variance term with the
            heteroskedasticity- and autocorrelation-consistent (HAC)
            Newey–West (1987) long-run variance of the Sharpe influence
            function (Lo 2002), so the DSR is robust to serially dependent
            returns. Pass an int for a fixed maximum lag L, or "auto" for the
            Newey–West (1994) plug-in bandwidth. None (default) keeps the exact
            IID Bailey-LdP variance, which the HAC form nests.
        dates: Optional per-bar date index (1:1 with ``daily_returns``,
            issue #1409) — when supplied, the DSR is computed on excess
            returns against the historical 3-month T-bill series
            (``rf_series.py``) instead of the flat fallback rate. Omitted
            (the default) keeps the exact pre-#1409 flat-rate behavior —
            existing callers need no change. See ``_sharpe_dsr_inputs`` for
            the excess-series math and ``run_rigor_gate``'s ``rf_convention``
            for how the choice is disclosed.

    Returns:
        (deflated_sharpe_ratio, dsr_p_value)
          - deflated_sharpe_ratio: annualized Sharpe excess over the
            expected best-of-N null. Positive means the strategy clears
            the multiple-testing bar.
          - dsr_p_value: P(true SR > 0 | observed SR, N trials, T bars).
            The badge (level-1/Conservative) gate threshold is
            ``rigor_profiles.DSR_P_BADGE_MIN`` — the one definition of the bar;
            looser levels accept down to the 0.50 always-on floor. See RigorGateResult.passes_at_level / min_passing_level.
        Both are None if data is insufficient (T < 4) or degenerate.
    """
    if num_trials < 1:
        raise ValueError("num_trials must be >= 1")

    inputs = _sharpe_dsr_inputs(daily_returns, dates=dates)
    if inputs is None:
        return None, None
    arr, T, SR_hat, sigma, mean, gamma_3, gamma_4 = inputs

    if math.isnan(average_correlation):
        average_correlation = 0.0
    average_correlation = max(0.0, min(1.0, average_correlation))

    # Serial-correlation-robust variance (Newey–West HAC) when requested. The
    # HAC long-run variance of the Sharpe influence function nests the IID
    # variance term, so hac_lags=None preserves the exact Bailey-LdP behaviour.
    variance_override = None
    if hac_lags is not None:
        variance_override = _sharpe_influence_lrv(arr, SR_hat, mean, sigma, hac_lags)

    return _dsr_from_stats(
        SR_hat, T, gamma_3, gamma_4, num_trials, average_correlation, variance_override=variance_override
    )


def compute_dsr_hac_and_iid(
    daily_returns: list[float] | np.ndarray,
    num_trials: int,
    average_correlation: float = 0.0,
    hac_lags: int | str = "auto",
    dates: Sequence[str | date | datetime] | None = None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """HAC-robust DSR and the IID-SE DSR in a single pass.

    Returns ``(dsr_hac, p_hac, dsr_iid, p_iid)``. The series validation, the
    SciPy moments (skew/kurtosis), and the influence-function long-run variance
    are computed **once** and shared between the two verdicts, so this is roughly
    half the work of two separate ``compute_dsr`` calls — the rigor gate's hot
    path may evaluate many candidate strategies. ``run_rigor_gate`` gates on the
    HAC p-value and surfaces the IID-SE p-value as the advisory delta. All four
    values are ``None`` when the series is too short / degenerate.

    Equivalent (to the bit) to calling ``compute_dsr`` twice — with and without
    ``hac_lags`` — at the same arguments; the only saving is the shared inputs.
    """
    if num_trials < 1:
        raise ValueError("num_trials must be >= 1")

    inputs = _sharpe_dsr_inputs(daily_returns, dates=dates)
    if inputs is None:
        return None, None, None, None
    arr, T, SR_hat, sigma, mean, gamma_3, gamma_4 = inputs

    if math.isnan(average_correlation):
        average_correlation = 0.0
    average_correlation = max(0.0, min(1.0, average_correlation))

    lrv = _sharpe_influence_lrv(arr, SR_hat, mean, sigma, hac_lags) if hac_lags is not None else None
    dsr_hac, p_hac = _dsr_from_stats(
        SR_hat, T, gamma_3, gamma_4, num_trials, average_correlation, variance_override=lrv
    )
    dsr_iid, p_iid = _dsr_from_stats(SR_hat, T, gamma_3, gamma_4, num_trials, average_correlation)
    return dsr_hac, p_hac, dsr_iid, p_iid


def _dsr_from_stats(
    SR_hat: float,
    T: int,
    gamma_3: float,
    gamma_4: float,
    N: int,
    average_correlation: float = 0.0,
    variance_override: float | None = None,
) -> tuple[float | None, float | None]:
    """Core DSR formula — exposed for direct unit-testing against spec cases.

    Args:
        SR_hat: Per-bar (un-annualized) Sharpe ratio.
        T: Number of bars in the return series.
        gamma_3: Skewness of the per-bar return series.
        gamma_4: Raw (Pearson) kurtosis of the per-bar return series — γ₄ = 3
            for normal returns. This matches Bailey-LdP (2014) eq. 8 directly;
            do NOT pass Fisher excess kurtosis here (it would bias the denom).
        N: Number of trials in the selection set.
        average_correlation: Correlation scalar for variance of trials.
        variance_override: When provided, used as the variance term ``denom_sq``
            in place of the IID closed form — carries the Newey–West HAC
            long-run variance for serial-correlation-robust inference. The HAC
            form nests the IID one, so passing the IID value is a no-op.

    Returns:
        (deflated_sharpe_annualized, dsr_p_value) or (None, None).
    """
    if T < DSR_MIN_BARS:
        return None, None

    N = max(1, N)

    # E[max_N]: Bailey-LdP (2014) approximation for the expected maximum of N
    # iid standard-normal random variables (the expected best-of-N null SR).
    if N == 1:
        E_max_N = 0.0
    else:
        # Correlated trials are not independent tests: a parameter sweep whose
        # variants move together offers fewer distinct chances to get lucky, so
        # the expected best-of-N null is lower. Under equicorrelation with
        # average pairwise correlation ρ ≥ 0, the one-factor representation
        #     X_i = √ρ·Z + √(1−ρ)·ε_i      (Z, ε_i iid standard normal)
        # gives every X_i unit variance and pairwise correlation ρ, and since Z
        # is common to all of them it factors straight out of the maximum:
        #     max_i X_i = √ρ·Z + √(1−ρ)·max_i ε_i
        #     E[max_i X_i] = √(1−ρ)·E[max of N iid]
        # so the correlated E[max] is exactly the iid one scaled by √(1−ρ). This
        # is Bailey-LdP's own √(V[{SR_n}]) term specialised to equicorrelation:
        # the cross-sectional variance of N equicorrelated null Sharpes has
        # expectation (1−ρ), which is the same factor under the square root.
        # Because SR_zero below substitutes the closed-form null variance
        # 1/(T−1) for the paper's empirical V[{SR_n}], that shrinkage is not
        # carried implicitly and has to be applied here.
        #
        # ρ=0 → the full independent-trial penalty; ρ=1 → all variants are one
        # test and there is no selection bias to deflate. Negative ρ is clamped
        # away: equicorrelation is only positive-semidefinite for ρ > −1/(N−1),
        # and the identity above needs ρ ≥ 0 for √ρ to be real.
        #
        # #1558: this REPLACES an effective-trial-count form
        # N_eff = N/(1 + (N−1)ρ) fed through the quantile approximation. That is
        # the Kish design effect (effective sample size for a mean), not an
        # expectation-of-maximum, and it under-deflated by 2×–10× across
        # ρ ∈ [0.3, 0.9] — at N=16, ρ=0.7 the null gate admitted noise 28.9% of
        # the time against a nominal 10%. It was also wrong in shape, not just
        # scale: N_eff → 1/ρ as N grows, so deflation saturated and a
        # 10,000-variant sweep drew the same penalty as a 64-variant one.
        rho = max(0.0, min(1.0, average_correlation))
        phi_inv_1 = float(norm.ppf(1.0 - 1.0 / N))
        phi_inv_2 = float(norm.ppf(1.0 - 1.0 / (N * math.e)))
        e_max_full = (1.0 - _EULER_MASCHERONI) * phi_inv_1 + _EULER_MASCHERONI * phi_inv_2
        E_max_N = e_max_full * math.sqrt(1.0 - rho)

    # SR_zero: expected best-of-N under the null, scaled to per-bar variance
    # (under iid normal returns, per-bar SR has variance 1/(T-1))
    SR_zero = math.sqrt(1.0 / (T - 1)) * E_max_N

    # Variance-adjusted z-statistic (eq. 8 in Bailey-LdP 2014). The closed-form
    # denominator is the IID asymptotic variance of the Sharpe estimator, equal
    # to the variance of its influence function (Lo 2002; Mertens 2002). When a
    # HAC long-run variance is supplied it replaces this term, leaving the rest
    # of the statistic unchanged.
    if variance_override is not None:
        denom_sq = variance_override
    else:
        denom_sq = 1.0 - gamma_3 * SR_hat + ((gamma_4 - 1.0) / 4.0) * SR_hat**2
    if denom_sq <= 0.0:
        return None, None

    z = (SR_hat - SR_zero) * math.sqrt(T - 1) / math.sqrt(denom_sq)
    dsr_p_value = float(norm.cdf(z))

    # Annualize the deflated SR for display (Sharpe units, not probability)
    deflated_sharpe_ratio = (SR_hat - SR_zero) * math.sqrt(_ANNUALIZATION)

    return round(deflated_sharpe_ratio, 6), round(dsr_p_value, 6)


# ─── 2. Probability of Backtest Overfitting (CSCV) ──────────────────


def compute_pbo(
    returns_matrix: dict[str, list[float]],
    s_partitions: int = 16,
    dates: Sequence[str | date | datetime] | None = None,
) -> dict[str, float]:
    """Probability of Backtest Overfitting via CSCV (Bailey et al. 2014).

    Computes the fraction of combinatorial IS/OOS splits in which the
    in-sample-optimal strategy underperforms the OOS median. PBO is a
    library-level metric: a single value is computed across all N strategies
    and attached identically to each strategy's BacktestResult from this run.

    Args:
        returns_matrix: {strategy_id: [daily_returns]} — all strategies must
            cover the same date range. Series are truncated to the shortest
            length to align them.
        s_partitions: Number of equal time-partitions S. Must be even ≥ 2.
            Default 16 is the paper's recommended value.
        dates: Optional shared date index (1:1 with each series AFTER the
            shortest-length truncation below — issue #1409). CSCV's per-split
            Sharpe ranking (``_sharpe_per_col``) uses this to resolve the
            historical T-bill rf series for the matrix's window instead of the
            flat fallback rate. Omitted (the default) keeps the exact
            pre-#1409 flat-rate code path.

    Returns:
        {strategy_id: pbo_score} — lower is better. PBO ≥ 0.5 means the
        library is expected to overfit (IS-best underperforms OOS median).
        Returns 0.0 for every strategy if N < 2 or data is insufficient.

    Known limitations (the library here is small — N ≈ 4–6 today):
      - Library-level coupling: PBO is a property of the whole selection set,
        not of one strategy, yet the same value is attached to every member.
        A strategy's PBO verdict therefore depends on which *other* strategies
        happen to be in the library — adding or removing a neighbor can flip
        it. This is inherent to CSCV, not a bug, but it means PBO should be
        read as a library-overfit signal, not a per-strategy score.
      - Coarse OOS rank: ω = rank_oos / N takes only N discrete values, so with
        N = 4 the logit λ is quantized to a handful of levels and PBO is
        granular. The estimate sharpens as the library grows.
      - Trailing-bar truncation: rows_per_block = T // S drops up to S − 1
        trailing bars (≤ 15 at the default S = 16) so every block is equal-length.
        Negligible for multi-year series; worth noting for short ones.
    """
    if len(returns_matrix) < 2:
        return dict.fromkeys(returns_matrix, 0.0)

    sorted_ids = sorted(returns_matrix.keys())
    N = len(sorted_ids)

    lengths = {sid: len(returns_matrix[sid]) for sid in sorted_ids}
    T = min(lengths.values())
    T_max = max(lengths.values())
    if T_max != T:
        # Mirror of analytics-engine pbo.py: truncating to the shortest series
        # silently drops the most recent (most forward-looking) OOS bars from
        # longer series. Surface it so the caller date-aligns rather than
        # getting an optimistic PBO from misaligned data.
        import warnings

        discarded = {sid: lengths[sid] - T for sid in sorted_ids if lengths[sid] > T}
        warnings.warn(
            f"compute_pbo: series length mismatch (min={T}, max={T_max}); "
            f"trailing bars discarded per id: {discarded}. Pass date-aligned "
            f"series to suppress this warning.",
            stacklevel=2,
        )

    # Build (T, N) matrix, columns in sorted_ids order
    R = np.array([returns_matrix[sid][:T] for sid in sorted_ids], dtype=float).T  # shape (T, N)

    S = s_partitions if (s_partitions % 2 == 0 and s_partitions >= 2) else 16
    rows_per_block = T // S
    if rows_per_block < 1:
        return dict.fromkeys(sorted_ids, 0.0)

    # #1409: resolve the per-bar rf array ONCE over the truncated (T,) window,
    # matching R's own truncation to `T` above, then block it identically to R
    # so every IS/OOS split's rf slice lines up with that split's return rows.
    # `dates is None` keeps `rf_daily_full` the flat broadcast (unchanged
    # pre-#1409 behavior); `_sharpe_per_col` only takes the excess-return
    # branch when a per-split rf array is actually passed in below.
    rf_daily_full = _resolve_rf_daily_array(list(dates)[:T] if dates is not None else None, T)

    blocks = [R[i * rows_per_block : (i + 1) * rows_per_block, :] for i in range(S)]
    rf_blocks = [rf_daily_full[i * rows_per_block : (i + 1) * rows_per_block] for i in range(S)]
    half = S // 2
    lambdas: list[float] = []

    for is_indices in combinations(range(S), half):
        oos_indices = [i for i in range(S) if i not in is_indices]

        IS = np.vstack([blocks[i] for i in is_indices])
        OOS = np.vstack([blocks[i] for i in oos_indices])
        rf_is = np.concatenate([rf_blocks[i] for i in is_indices])
        rf_oos = np.concatenate([rf_blocks[i] for i in oos_indices])

        is_sharpes = _sharpe_per_col(IS, rf_daily=rf_is if dates is not None else None)
        oos_sharpes = _sharpe_per_col(OOS, rf_daily=rf_oos if dates is not None else None)

        best_is_idx = int(np.argmax(is_sharpes))

        # Ascending rank (1 = worst, N = best) of IS-best strategy in OOS
        oos_ranks = _ascending_ranks(oos_sharpes)
        rank_oos = float(oos_ranks[best_is_idx])

        omega = rank_oos / N
        omega = float(np.clip(omega, 1e-9, 1.0 - 1e-9))
        lam = math.log(omega / (1.0 - omega))
        lambdas.append(lam)

    if not lambdas:
        return dict.fromkeys(sorted_ids, 0.0)

    pbo = round(sum(1 for lam in lambdas if lam <= 0.0) / len(lambdas), 6)
    return dict.fromkeys(sorted_ids, pbo)


# ─── 3. Walk-forward OOS Sharpe ──────────────────────────────────────


def compute_oos_sharpe(
    daily_returns: list[float],
    train_fraction: float = 0.70,
    dates: Sequence[str | date | datetime] | None = None,
) -> float | None:
    """Annualized Sharpe on the held-out OOS slice (no shuffling).

    Note: uses ddof=1 (sample stddev); analytics-engine uses ddof=0 — the two OOS Sharpe
    numbers differ by sqrt(N/(N-1)) and are not directly comparable.

    Splits the return series chronologically: the first train_fraction of
    bars are in-sample; the remainder are the OOS test set. The OOS Sharpe
    must clear the absolute floor (> 0) and the cliff check (OOS/IS ≥ 0.5)
    in RigorGateResult.passes_all.

    NOTE — this is a single chronological hold-out, NOT a rolling walk-forward
    re-estimation. There is no per-window refit and no purge/embargo gap at the
    train/test boundary, so signal state from lookback indicators (e.g. an
    SMA-200 or TSMOM-252 window) can straddle the split. Rolling Combinatorial
    Purged CV is the principled upgrade and is implemented separately in
    ``compute_cpcv_oos_sharpe`` (wired into ``run_rigor_gate`` once the
    analytics-engine emits a combinatorial OOS matrix; reported as MISSING
    until then).

    Args:
        daily_returns: Full per-bar return series.
        train_fraction: Fraction reserved for in-sample training (0 < f < 1).
        dates: Optional per-bar date index (1:1 with ``daily_returns``, issue
            #1409) — sliced to the same OOS window as the returns and used to
            resolve the historical T-bill rf series for that window. Omitted
            (the default) keeps the exact pre-#1409 flat-rate code path.

    Returns:
        Annualized OOS Sharpe, or None if the OOS slice is shorter than one
        trading month (< 21 bars) or the full series has < 10 bars.
    """
    arr = np.asarray(daily_returns, dtype=float)
    T = len(arr)
    if T < 10:
        return None

    split = int(T * train_fraction)
    oos = arr[split:]

    if len(oos) < 21:  # 1 trading month minimum (5-bar floor was statistically degenerate)
        return None

    if float(np.ptp(oos)) == 0.0:
        return None

    sigma = float(oos.std(ddof=1))
    if sigma <= 0.0:
        return None

    if dates is not None:
        oos_dates = list(dates)[split:]
        rf_daily = _resolve_rf_daily_array(oos_dates, len(oos))
        excess = oos - rf_daily
        sigma_excess = float(excess.std(ddof=1))
        if sigma_excess <= 0.0:
            return None
        return round(float((float(excess.mean()) / sigma_excess) * math.sqrt(_ANNUALIZATION)), 6)

    return round(float(((oos.mean() - _RF_DAILY) / sigma) * math.sqrt(_ANNUALIZATION)), 6)


def compute_in_sample_sharpe(
    daily_returns: list[float],
    train_fraction: float = 0.70,
    dates: Sequence[str | date | datetime] | None = None,
) -> float | None:
    """Annualized Sharpe on the in-sample (training) slice.

    The chronological complement of ``compute_oos_sharpe``: the first
    ``train_fraction`` of bars. Uses the same excess-return convention
    (rf=5%/252, sqrt(252) annualization) and the same length thresholds, so the
    OOS/IS cliff ratio enforced in ``RigorGateResult.passes_all`` (and mirrored
    by the fusion gate) compares like with like. Returns None when the series is
    too short to split meaningfully (mirrors ``compute_oos_sharpe`` so the IS
    Sharpe is available exactly when the OOS Sharpe is).

    Args:
        daily_returns: Full per-bar return series.
        train_fraction: Fraction reserved for in-sample training (0 < f < 1).
        dates: Optional per-bar date index (1:1 with ``daily_returns``, issue
            #1409) — sliced to the same IS window as the returns and used to
            resolve the historical T-bill rf series for that window. Omitted
            (the default) keeps the exact pre-#1409 flat-rate code path.
    """
    arr = np.asarray(daily_returns, dtype=float)
    T = len(arr)
    if T < 10:
        return None
    split = int(T * train_fraction)
    is_arr = arr[:split]
    if len(is_arr) < 21:  # 1 trading month minimum — mirrors compute_oos_sharpe's OOS floor
        return None
    is_dates = list(dates)[:split] if dates is not None else None
    s = _annualized_sharpe_arr(is_arr, dates=is_dates)
    return round(s, 6) if s is not None else None


def _annualized_sharpe_arr(
    arr: np.ndarray,
    dates: Sequence[str | date | datetime] | None = None,
) -> float | None:
    """Annualized Sharpe of a 1-D return array, or None if degenerate.

    #1409: ``dates`` optional, 1:1 with ``arr``. Omitted keeps the exact
    pre-#1409 flat-rate code path. **Two** callers always take that no-dates
    path today (2026-08-21 review fix — the docstring previously named only
    the first, live-irrelevant one):

      - ``compute_cpcv_oos_sharpe`` — out of this issue's scope, and has
        **no production caller today** (CPCV is reported ``NOT_RUN`` until a
        real combinatorial OOS matrix is wired in).
      - ``regime_conditional_sharpe`` (reached via ``regime_robustness_score``,
        which ``run_rigor_gate`` DOES call, and DOES surface on every gate
        result long enough to classify >1 regime — see
        ``RigorGateResult.regime_robustness``) — this one IS live. A grade
        that threads ``dates`` therefore mixes a T-bill-series DSR/OOS/IS
        with flat-5% per-regime Sharpes; see ``docs/rigor-methods.md`` §1a
        for the disclosed-holdout list this is documented alongside (Kelly,
        the optimizer, and now regime-robustness).
    """
    if len(arr) < 2:
        return None
    if float(np.ptp(arr)) == 0.0:
        return None
    sigma = float(arr.std(ddof=1))
    if sigma <= 0.0:
        return None
    if dates is not None:
        rf_daily = _resolve_rf_daily_array(dates, len(arr))
        excess = arr - rf_daily
        sigma_excess = float(excess.std(ddof=1))
        if sigma_excess <= 0.0:
            return None
        return float((float(excess.mean()) / sigma_excess) * math.sqrt(_ANNUALIZATION))
    return float(((arr.mean() - _RF_DAILY) / sigma) * math.sqrt(_ANNUALIZATION))


def compute_average_pairwise_correlation(
    returns: dict[str, list[float]] | np.ndarray | list[list[float]],
) -> float:
    """Average off-diagonal Pearson correlation across a set of return series.

    Feeds the Deflated Sharpe Ratio's correlated-trials correction: the
    expected best-of-N null Sharpe is the independent-trial ``E[max]`` scaled by
    ``sqrt(1 - rho_bar)``, because under equicorrelation the factor common to
    every trial passes straight through the maximum (see ``_dsr_from_stats``
    for the one-factor derivation). Correlated trials offer fewer distinct
    chances to get lucky, so the expected best-of-N null is lower. The caller
    that holds the selection set (the strategy library, or a parameter-variant
    grid) computes this and passes it into ``compute_dsr`` / ``run_rigor_gate``.

    #1558/#1559: this previously fed an effective trial count
    ``N_eff = N / (1 + (N-1)*rho_bar)``. That is the Kish design effect, not an
    expectation-of-maximum, and it under-deflated by 2x-10x; do not reintroduce
    it here or in the callers.

    Args:
        returns: Either ``{id: series}`` or a 2-D array/list (rows = trials,
            cols = time). Series are truncated to the shortest length.

    Returns:
        Mean pairwise correlation clamped to ``[0.0, 1.0]`` — negative
        (diversifying) correlations and NaNs collapse to ``0.0``, a conservative
        "no penalty relief" default. Returns ``0.0`` for < 2 usable series.
    """
    series = list(returns.values()) if isinstance(returns, dict) else list(returns)
    arrs = [np.asarray(s, dtype=float) for s in series]
    arrs = [a for a in arrs if a.size >= 2]
    if len(arrs) < 2:
        return 0.0

    T = min(a.size for a in arrs)
    if T < 2:
        return 0.0

    matrix = np.vstack([a[:T] for a in arrs])
    # Drop constant rows — they produce NaN correlations. Use peak-to-peak
    # (exactly 0 for identical values) rather than variance, which carries a
    # tiny float residual for a constant series.
    matrix = matrix[np.ptp(matrix, axis=1) > 0.0]
    if matrix.shape[0] < 2:
        return 0.0

    corr = np.corrcoef(matrix)
    upper = corr[np.triu_indices(corr.shape[0], k=1)]
    upper = upper[np.isfinite(upper)]
    if upper.size == 0:
        return 0.0

    return max(0.0, min(1.0, float(np.mean(upper))))


def assert_self_contained_cohort_correlation(num_trials: int, average_correlation: float) -> None:
    """Guard against the num_trials / cohort-correlation re-coupling trap
    (num_trials-provenance audit 2026-08-03, V4).

    Three call sites (``selection_bias_routes.evaluate_rigor_gate``,
    ``strategies_routes._live_rigor_results_for_strategies``,
    ``live_rigor_gate.verdicts_for_strategies``) each compute a COHORT-WIDE
    ``average_correlation`` across every strategy in the curated library with
    enough persisted returns, then grade each individual strategy at
    ``num_trials=1`` (Dan's decouple #2 principle, 2026-07-09 / restated
    2026-07-27: a curated strategy's rigor depends only on itself, never on
    its neighbours). At ``num_trials=1`` that cohort-wide correlation is INERT
    by construction — ``_dsr_from_stats``'s ``E[max_N]`` term is unconditionally
    ``0.0`` when ``N == 1``, so ``average_correlation`` cannot move the DSR
    verdict no matter what value it holds.

    That inertness is exactly what makes ``num_trials=1`` + a cohort-wide
    correlation SAFE today — and exactly what would make a future edit that
    reintroduces ``num_trials > 1`` at one of those call sites DANGEROUS: the
    cohort-wide correlation would silently start relaxing the multiple-testing
    penalty using OTHER strategies' correlation structure, re-coupling the
    library the decision retired. Nothing else flags it — the code would just
    silently compute a different, wrong DSR for every strategy in the cohort.

    Call this immediately after computing a cohort-wide ``average_correlation``
    and before it is used, at any call site that holds ``num_trials=1`` by
    contract. Raises ``RuntimeError`` (never a bare ``assert``, which ``-O``
    can strip) so the violation is IMPOSSIBLE to silently pass through, not
    merely unlikely. Does nothing when ``average_correlation == 0.0`` — an
    N>1 caller with its OWN, non-cohort correlation context (a strategy's own
    generation search pool or parameter-variant grid) is legitimate and
    unaffected; only the COMBINATION of num_trials>1 with a nonzero
    correlation reaching this specific guard is disallowed.
    """
    if num_trials != 1 and average_correlation != 0.0:
        raise RuntimeError(
            f"cohort-wide average_correlation={average_correlation!r} combined with "
            f"num_trials={num_trials!r} != 1 — this re-couples a curated/self-contained "
            "strategy's DSR to a cohort's correlation structure, exactly what the "
            "decouple #2 decision (2026-07-09, restated 2026-07-27) forbids outside "
            "Leaderboard/Marketplace. If this call site legitimately needs num_trials>1 "
            "(e.g. a strategy's own generation search pool or parameter-variant grid), "
            "compute average_correlation from THAT search's own candidates — never from "
            "the curated cohort."
        )


# ─── 3b. Combinatorial Purged Cross-Validation OOS Sharpe ────────────


def compute_cpcv_oos_sharpe(
    cv_returns_matrix: np.ndarray | list[list[float]] | None = None,
    n_groups: int = 6,
    test_groups: int = 2,
    test_bounds: list[np.ndarray] | list[list[int]] | None = None,
    cv_splits: list[tuple[int, ...]] | None = None,
) -> dict[str, float] | None:
    """Combinatorial Purged Cross-Validation OOS Sharpe (López de Prado, AFML ch. 12).

    Unlike naive block subsampling on a static returns array, true CPCV requires
    a matrix of out-of-sample predictions from models trained on C(N, k) splits.
    This function assembles C(N-1, k-1) continuous backtest paths from those
    splits, preventing artificial variance and evaluating path-to-path stability.

    Note on embargo: Embargoing (dropping bars after test sets) must be applied
    upstream during the generation of `cv_returns_matrix` to prevent serial
    correlation leakage into the training sets. Path assembly only combines
    the resulting OOS test blocks.

    Args:
        cv_returns_matrix: Shape (S, T) where S = C(n_groups, test_groups).
            If a 1D array of static returns is passed (e.g. from a single backtest),
            this correctly rejects the calculation, as static CPCV is mathematically
            invalid (it generates identical paths).
        n_groups: Number of contiguous partitions (N). Must be >= 2.
        test_groups: Blocks held out per split (k), 1 <= k < N.
        test_bounds: Explicit list of N arrays containing the exact time indices for
            each block to prevent look-ahead bias from misaligned uniform splits.
        cv_splits: Explicit list of S tuples mapping each matrix row to the test
            blocks it held out, preventing lexicographical misalignment.

    Returns:
        Dict with metrics, or None if invalid matrix or static returns provided.
    """
    if cv_returns_matrix is None or len(cv_returns_matrix) == 0:
        return None

    arr = np.asarray(cv_returns_matrix, dtype=float)
    if arr.ndim != 2:
        return None

    S, T = arr.shape
    if n_groups < 2 or not (1 <= test_groups < n_groups):
        return None
    if n_groups * 5 > T:  # need >= ~5 bars per block to form a Sharpe
        return None

    splits = cv_splits if cv_splits is not None else list(combinations(range(n_groups), test_groups))

    if len(splits) != S:
        return None

    if test_bounds is not None:
        bounds = [np.asarray(b, dtype=int) for b in test_bounds]
        if len(bounds) != n_groups:
            return None
    else:
        bounds = np.array_split(np.arange(T), n_groups)

    n_paths = math.comb(n_groups - 1, test_groups - 1)

    paths = np.zeros((n_paths, T))

    for i in range(n_groups):
        splits_with_i = [s_idx for s_idx, combo in enumerate(splits) if i in combo]
        if len(splits_with_i) != n_paths:
            return None  # cv_splits ordering doesn't match expected block count
        paths[:, bounds[i]] = arr[np.ix_(splits_with_i, bounds[i])]

    oos_sharpes: list[float] = []
    for p in range(n_paths):
        s = _annualized_sharpe_arr(paths[p])
        if s is not None and math.isfinite(s):
            oos_sharpes.append(s)

    if not oos_sharpes:
        return None

    mean_oos = float(np.mean(oos_sharpes))
    positive_fraction = float(sum(s > 0.0 for s in oos_sharpes) / len(oos_sharpes))

    return {
        "mean_oos_sharpe": round(mean_oos, 6),
        "std_oos_sharpe": round(float(np.std(oos_sharpes, ddof=1)) if len(oos_sharpes) > 1 else 0.0, 6),
        "positive_fraction": round(positive_fraction, 6),
        "mean_is_sharpe": 0.0,
        "oos_is_ratio": 0.0,
        "n_paths": len(oos_sharpes),
    }


# ─── 4. Kelly Criterion position sizing ─────────────────────────────


def compute_kelly_fraction(
    daily_returns: list[float],
    rf_annual: float = 0.05,
    fractional: float = 0.5,
) -> float | None:
    """Fractional Kelly position size for a single-asset strategy.

    Implements the continuous-time Kelly formula:

        f* = (μ_ann - rf_ann) / σ_ann²

    where μ_ann and σ_ann² are the annualized mean return and variance.
    A fractional Kelly (default 0.5×) is applied to reduce drawdown
    volatility — the theoretical full-Kelly bet is too aggressive for
    practical use and is highly sensitive to estimation error.

    Args:
        daily_returns: Per-bar (daily) return series, un-annualized.
        rf_annual: Annual risk-free rate (default 5%).
        fractional: Kelly multiplier — 0.5 = half-Kelly (recommended),
            1.0 = full Kelly (academic reference only).

    Returns:
        Fractional Kelly weight ∈ (0, 1], clipped to [0, 1] to prevent
        leverage beyond 100%. Returns None if data is insufficient or
        degenerate (< 4 bars, zero variance, negative excess return).
    """
    arr = np.asarray(daily_returns, dtype=float)
    T = len(arr)
    if T < 4:
        return None

    if float(np.ptp(arr)) == 0.0:
        return None

    sigma_daily = float(arr.std(ddof=1))
    if sigma_daily <= 0.0:
        return None

    mu_ann = float(arr.mean()) * _ANNUALIZATION
    sigma_sq_ann = (sigma_daily**2) * _ANNUALIZATION

    excess_return = mu_ann - rf_annual
    if excess_return <= 0.0:
        return 0.0

    f_full = excess_return / sigma_sq_ann
    f_fractional = fractional * f_full

    return round(float(np.clip(f_fractional, 0.0, 1.0)), 6)


# ─── 5. Sharpe Confidence Interval ───────────────────────────────────


def compute_sharpe_ci(
    sharpe_annual: float,
    n_obs_daily: int,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Lo (2002) confidence interval for annualized Sharpe from daily IID returns.

    Returns (lower, upper) at the requested confidence level.
    """
    if n_obs_daily <= 0:
        raise ValueError(f"n_obs_daily must be positive, got {n_obs_daily}")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    sr_daily = sharpe_annual / math.sqrt(_ANNUALIZATION)
    se = math.sqrt((1.0 + 0.5 * sr_daily**2) * _ANNUALIZATION / n_obs_daily)
    z = norm.ppf((1.0 + confidence) / 2.0)
    # `norm.ppf` returns a numpy scalar, so `z * se` (and the returned tuple)
    # is np.float64 unless cast. A raw np.float64 breaks the passport DB insert:
    # psycopg2 renders it as `np.float64(-0.33)` and parses `np` as a schema →
    # `InvalidSchemaName: schema "np" does not exist`, rolling back the passport
    # sync. Cast to plain float to honor the tuple[float, float] contract.
    return (float(sharpe_annual - z * se), float(sharpe_annual + z * se))


# ─── 6. Private helpers ──────────────────────────────────────────────


def _sharpe_per_col(R: np.ndarray, rf_daily: np.ndarray | None = None) -> np.ndarray:
    """Annualized Sharpe for each column (strategy) of a return matrix.

    #1409: ``rf_daily`` is an optional per-ROW rf array (shape ``(R.shape[0],)``)
    broadcast-subtracted from every column before computing mean/sigma — the
    excess-return treatment, consistent with ``_sharpe_dsr_inputs``. ``None``
    (the default) keeps the exact pre-#1409 flat-``_RF_DAILY`` code path.
    """
    if R.shape[0] < 2:
        return np.zeros(R.shape[1])
    if rf_daily is not None:
        R = R - rf_daily[:, None]
        mu = R.mean(axis=0)
        sigma = R.std(axis=0, ddof=1)
        safe_sigma = np.where(sigma > 0, sigma, np.inf)
        return (mu / safe_sigma) * math.sqrt(_ANNUALIZATION)
    mu = R.mean(axis=0)
    sigma = R.std(axis=0, ddof=1)
    safe_sigma = np.where(sigma > 0, sigma, np.inf)
    return ((mu - _RF_DAILY) / safe_sigma) * math.sqrt(_ANNUALIZATION)


def _ascending_ranks(values: np.ndarray) -> np.ndarray:
    """1-based ranks, ascending order (rank N = highest value)."""
    n = len(values)
    order = np.argsort(values)
    ranks = np.empty(n, dtype=float)
    ranks[order] = np.arange(1, n + 1, dtype=float)
    return ranks


# ─── 7. Regime Classification ────────────────────────────────────────


def classify_regimes(
    market_returns: list[float] | np.ndarray,
    vol_window: int = 21,
    n_regimes: int = 2,
) -> np.ndarray:
    """Label each day by volatility regime using rolling realized volatility.

    A strategy's edge may be regime-dependent — strong in calm markets and
    fragile in stressed ones (or vice versa). This labels each bar by the
    *market benchmark's* recent volatility so that ``regime_conditional_*``
    can slice the strategy's own returns by the regime the market was in.

    Method (a deliberately simple vol-quantile proxy):
      1. Compute the rolling standard deviation of ``market_returns`` over a
         trailing ``vol_window``.
      2. **Shift the rolling-vol series by one bar** so the label assigned to
         day *t* uses only volatility realized through day *t − 1*. This avoids
         look-ahead: the regime label for a bar never peeks at that bar's own
         return (which the strategy is being evaluated on for the same day).
      3. Split the (shifted) rolling-vol values into ``n_regimes`` buckets by
         quantile — for ``n_regimes=2``, label 0 = "calm" (rolling-vol at or
         below the median) and label 1 = "stressed" (above the median).
      4. The first ``vol_window`` bars lack a full trailing window (and the
         extra shift consumes one more), so they get label ``-1`` =
         "unclassified" and are excluded from every downstream regime stat.

    This is intentionally a coarse realized-vol regime proxy, NOT a fitted
    regime model. A proper Markov-switching / HMM treatment (Ang & Bekaert
    2002, "International Asset Allocation With Regime Shifts", Review of
    Financial Studies 15(4): 1137-1187 — which motivates conditioning asset
    behavior on latent volatility regimes) is out of scope here; we surface a
    transparent, reproducible vol bucketing instead of an opaque fitted state.

    Args:
        market_returns: Per-bar benchmark return series (the regime driver).
        vol_window: Trailing window (bars) for realized volatility. Default 21
            (~one trading month).
        n_regimes: Number of volatility buckets (>= 1). Labels are
            ``0 .. n_regimes-1`` in ascending volatility order.

    Returns:
        Integer ``np.ndarray`` of length ``len(market_returns)``. Entries are
        ``-1`` for unclassified leading bars, else the regime id in
        ``0 .. n_regimes-1``. Returns an all-``-1`` array (no crash) when the
        series is too short to form a single full window or ``n_regimes < 1``.
    """
    arr = np.asarray(market_returns, dtype=float)
    T = len(arr)
    labels = np.full(T, -1, dtype=int)

    if T == 0 or n_regimes < 1 or vol_window < 1:
        return labels

    # Rolling std (ddof=1) over a trailing window; entries before a full window
    # exist remain NaN. Build via a simple stride-free loop for numpy-only clarity.
    roll_vol = np.full(T, np.nan, dtype=float)
    for i in range(vol_window - 1, T):
        window = arr[i - vol_window + 1 : i + 1]
        if window.size >= 2:
            roll_vol[i] = float(window.std(ddof=1))

    # Shift by one bar so day t's label uses vol realized through t-1 (no look-ahead).
    shifted = np.full(T, np.nan, dtype=float)
    shifted[1:] = roll_vol[:-1]

    valid_mask = np.isfinite(shifted)
    valid_vals = shifted[valid_mask]
    if valid_vals.size == 0:
        return labels

    if n_regimes == 1:
        labels[valid_mask] = 0
        return labels

    # Quantile cut-points partition the valid rolling-vol values into n_regimes
    # buckets of (approximately) equal mass. np.digitize maps each value to a
    # bucket in 0 .. n_regimes-1. A degenerate (constant-vol) series yields
    # identical edges; digitize then collapses everything into a single bucket
    # without crashing.
    quantiles = np.quantile(valid_vals, np.linspace(0.0, 1.0, n_regimes + 1)[1:-1])
    bucket = np.digitize(valid_vals, quantiles, right=False)
    bucket = np.clip(bucket, 0, n_regimes - 1).astype(int)
    labels[valid_mask] = bucket
    return labels


# NOTE: this WAS dead code (unwired from run_rigor_gate; see issue #621) but
# is no longer — `run_rigor_gate` reaches it via `regime_robustness_score`
# (2026-08-21 review fix: the banner below had gone stale and would mislead a
# reader into skipping the one call site of `_annualized_sharpe_arr` that is
# actually live in production; see that function's docstring).
def regime_conditional_sharpe(
    strategy_returns: list[float] | np.ndarray,
    regime_labels: list[int] | np.ndarray,
) -> dict[int, dict]:
    """Per-regime annualized Sharpe (and mean/vol/count) of a strategy.

    Slices the strategy's own daily returns by the market regime each day fell
    in (per ``classify_regimes``) and reports the strategy's annualized Sharpe,
    annualized mean return, annualized vol, and day-count within each regime.

    Args:
        strategy_returns: Per-bar strategy return series.
        regime_labels: Integer regime labels aligned to the same bars (``-1``
            for unclassified). Truncated to the shorter of the two lengths.

    Returns:
        ``{regime_id: {"sharpe", "ann_return", "ann_vol", "n_days"}}`` for each
        regime id present (excluding ``-1``). ``sharpe`` is ``None`` for any
        regime with < 2 days or zero variance. Empty dict if no classified days.
    """
    s = np.asarray(strategy_returns, dtype=float)
    r = np.asarray(regime_labels, dtype=int)
    n = min(len(s), len(r))
    if n == 0:
        return {}
    s = s[:n]
    r = r[:n]

    out: dict[int, dict] = {}
    for regime_id in sorted({int(x) for x in r if int(x) != -1}):
        sub = s[r == regime_id]
        n_days = int(sub.size)
        if n_days < 2 or float(np.ptp(sub)) == 0.0:
            out[regime_id] = {
                "sharpe": None,
                "ann_return": round(float(sub.mean()) * _ANNUALIZATION, 6) if n_days >= 1 else None,
                "ann_vol": 0.0 if n_days >= 1 else None,
                "n_days": n_days,
            }
            continue
        sigma = float(sub.std(ddof=1))
        sharpe = _annualized_sharpe_arr(sub)
        out[regime_id] = {
            "sharpe": round(sharpe, 6) if sharpe is not None else None,
            "ann_return": round(float(sub.mean()) * _ANNUALIZATION, 6),
            "ann_vol": round(sigma * math.sqrt(_ANNUALIZATION), 6),
            "n_days": n_days,
        }
    return out


def regime_robustness_score(
    strategy_returns: list[float] | np.ndarray,
    regime_labels: list[int] | np.ndarray,
) -> dict:
    """Regime-fragility score — does the edge survive across volatility regimes?

    A strategy that earns its Sharpe in only one regime (e.g. only in calm
    markets) is fragile: the moment the market shifts, the edge evaporates.
    This surfaces that fragility *transparently* rather than burying it behind
    a single aggregate Sharpe, consistent with the repo's honest-framing
    principle (paper-claim deltas and per-regime numbers are shown, not hidden).

    Motivation: Ang & Bekaert (2002), "International Asset Allocation With
    Regime Shifts", Review of Financial Studies 15(4): 1137-1187 — asset
    behavior (and therefore a strategy's edge) is regime-dependent, so a single
    full-sample Sharpe can mask regime-specific failure.

    Args:
        strategy_returns: Per-bar strategy return series.
        regime_labels: Integer regime labels aligned to the same bars.

    Returns:
        Dict with keys:
          ``per_regime``        — the ``regime_conditional_sharpe`` dict.
          ``min_regime_sharpe`` — lowest per-regime Sharpe (None if none computable).
          ``max_regime_sharpe`` — highest per-regime Sharpe (None if none computable).
          ``sharpe_dispersion`` — ``max - min`` (None if < 2 computable regimes).
          ``consistency``       — a ``[0, 1]`` score; high = the edge holds across
            regimes. Convention: when ``max > 0`` it is
            ``min(1, max(0, min_sharpe / max_sharpe))`` (so a regime with
            negative or near-zero Sharpe drives consistency toward 0). When
            ``max <= 0`` (the strategy is non-positive in *every* regime) there
            is no regime in which it works, so consistency is ``0.0``.
          ``robust``            — ``bool``: the strict honest gate
            ``min_regime_sharpe > 0`` (positive Sharpe in *every* classified
            regime). ``False`` when no regimes are computable.
    """
    per_regime = regime_conditional_sharpe(strategy_returns, regime_labels)
    sharpes = [v["sharpe"] for v in per_regime.values() if v.get("sharpe") is not None]

    if not sharpes:
        return {
            "per_regime": per_regime,
            "min_regime_sharpe": None,
            "max_regime_sharpe": None,
            "sharpe_dispersion": None,
            "consistency": 0.0,
            "robust": False,
        }

    min_sharpe = float(min(sharpes))
    max_sharpe = float(max(sharpes))
    dispersion = round(max_sharpe - min_sharpe, 6) if len(sharpes) >= 2 else None

    # Non-positive in every regime → no regime in which the edge works, so there
    # is no consistency to credit (0.0).
    consistency = min(1.0, max(0.0, min_sharpe / max_sharpe)) if max_sharpe > 0.0 else 0.0

    return {
        "per_regime": per_regime,
        "min_regime_sharpe": round(min_sharpe, 6),
        "max_regime_sharpe": round(max_sharpe, 6),
        "sharpe_dispersion": dispersion,
        "consistency": round(float(consistency), 6),
        "robust": bool(min_sharpe > 0.0),
    }


# DEAD CODE — unwired from run_rigor_gate; see issue #621
def regime_conditional_dsr(
    strategy_returns: list[float] | np.ndarray,
    regime_labels: list[int] | np.ndarray,
    num_trials: int = 1,
) -> dict[int, dict]:
    """Per-regime Deflated Sharpe Ratio — does the DSR evidence hold within each regime?

    Runs the existing ``compute_dsr`` on the strategy's return subset *within*
    each market regime, so the deflated-Sharpe evidence can be inspected regime
    by regime rather than only on the blended full sample. Reuses ``compute_dsr``
    directly, so the same minimum-length guard (T < 4 → ``None``) applies per
    regime subset — a regime with too few bars reports ``None`` DSR honestly
    instead of fabricating significance from a handful of points.

    Args:
        strategy_returns: Per-bar strategy return series.
        regime_labels: Integer regime labels aligned to the same bars.
        num_trials: Number of trials in the selection set, passed through to
            ``compute_dsr`` for the multiple-testing correction (default 1).

    Returns:
        ``{regime_id: {"deflated_sharpe", "dsr_p_value", "n_days"}}`` for each
        regime id present (excluding ``-1``). ``deflated_sharpe`` and
        ``dsr_p_value`` are ``None`` when the regime subset is too short or
        degenerate. Empty dict if no classified days.
    """
    s = np.asarray(strategy_returns, dtype=float)
    r = np.asarray(regime_labels, dtype=int)
    n = min(len(s), len(r))
    if n == 0:
        return {}
    s = s[:n]
    r = r[:n]

    out: dict[int, dict] = {}
    for regime_id in sorted({int(x) for x in r if int(x) != -1}):
        sub = s[r == regime_id]
        dsr, p_val = compute_dsr(sub, num_trials)
        out[regime_id] = {
            "deflated_sharpe": dsr,
            "dsr_p_value": p_val,
            "n_days": int(sub.size),
        }
    return out


# ─── 8. Board-Level Multiple-testing Correction ──────────────────────
#
# Restored (#1185, 2026-08-20) after Tranche-1's dead-code excision (PR #1259 /
# commit c02d5fad) deleted this function on 2026-08-18 for having zero
# non-test callers — accurate at the time, but issue #1185 (open since
# 2026-07-28, three weeks BEFORE that deletion) had already asked for exactly
# this to be wired in. See rigor_evaluator.compute_board_level_fdr for the one
# real call site this issue requires; the pure BH-FDR math below is unchanged
# from the pre-deletion version (restored verbatim from git history).


def benjamini_hochberg_fdr(
    pvalues: list[float],
    fdr_level: float = 0.05,
) -> dict[str, Any]:
    """Benjamini-Hochberg False Discovery Rate correction.

    Controls the expected proportion of false discoveries among the rejected
    hypotheses at level *fdr_level*. BH is uniformly more powerful than
    Bonferroni for independent or positively dependent tests, which makes it
    the preferred correction when evaluating a library of strategies (where
    strategies sharing the same universe tend to have positive return
    correlations).

    Algorithm (Benjamini & Hochberg 1995, Theorem 1):
      1. Sort p-values ascending: p_(1) ≤ p_(2) ≤ … ≤ p_(m).
      2. BH critical value for rank k: q_k = (k / m) × α.
      3. Find the largest k* such that p_(k*) ≤ q_(k*).
      4. Reject all hypotheses with rank ≤ k* (i.e. all p ≤ p_(k*)).
      5. BH-adjusted p-values: p̃_k = min(1, p_k × m / k), enforced to be
         monotone non-decreasing in rank order (step-down adjustment).

    Reference: Benjamini, Y. & Hochberg, Y. (1995). "Controlling the false
    discovery rate: a practical and powerful approach to multiple testing."
    *Journal of the Royal Statistical Society. Series B*, 57(1), 289-300.

    Application to backtesting: Bailey, D.H., Borwein, J., López de Prado, M.,
    & Zhu, Q. (2014). "The Probability of Backtest Overfitting." *Journal of
    Computational Finance*, 20(4), 39-70.

    Args:
        pvalues: List of m p-values (one per strategy / hypothesis). These are
            CLASSICAL p-values — a SMALL value is evidence against the null
            (rejected). Note this is the opposite convention from
            ``RigorGateResult.dsr_p_value`` (P(true SR > 0), where a LARGE
            value is confident evidence): a caller feeding DSR p-values in
            here must convert first (``1 - dsr_p_value``). See
            ``rigor_evaluator.compute_board_level_fdr`` for that conversion.
        fdr_level: Target FDR (α). Default 0.05.

    Returns:
        Dict with keys:
          ``rejected``         — list[bool] of length m in the original input order.
          ``bh_critical_values`` — list[float] of BH thresholds q_k in original order.
          ``n_rejected``       — number of hypotheses rejected.
          ``adjusted_pvalues`` — BH-adjusted p-values in the original input order.
    """
    m = len(pvalues)
    if m == 0:
        return {"rejected": [], "bh_critical_values": [], "n_rejected": 0, "adjusted_pvalues": []}

    arr = np.asarray(pvalues, dtype=float)
    # Sort ascending, track original positions
    sort_order = np.argsort(arr)
    sorted_p = arr[sort_order]

    ranks = np.arange(1, m + 1, dtype=float)
    bh_crit = (ranks / m) * fdr_level

    # Largest k where sorted_p[k-1] <= bh_crit[k-1]
    below_threshold = sorted_p <= bh_crit
    # 0-based index of the last True (largest rejected k); -1 if nothing rejected.
    k_star = int(np.where(below_threshold)[0][-1]) if np.any(below_threshold) else -1

    reject_sorted = np.zeros(m, dtype=bool)
    if k_star >= 0:
        reject_sorted[: k_star + 1] = True

    # BH-adjusted p-values in sorted order: p̃_k = min(1, p_k × m / k)
    # Enforce monotone non-decreasing via cummin from the last rank
    adjusted_sorted = np.minimum(1.0, sorted_p * (m / ranks))
    # Step-down monotonicity: p̃_k ≤ p̃_{k+1} in sorted order (adjust from top)
    for i in range(m - 2, -1, -1):
        adjusted_sorted[i] = min(adjusted_sorted[i], adjusted_sorted[i + 1])

    # Map back to original input order
    inv_order = np.empty(m, dtype=int)
    inv_order[sort_order] = np.arange(m)

    rejected_orig = reject_sorted[inv_order].tolist()
    bh_crit_orig = bh_crit[inv_order].tolist()
    adjusted_orig = adjusted_sorted[inv_order].tolist()

    return {
        "rejected": rejected_orig,
        "bh_critical_values": [round(v, 8) for v in bh_crit_orig],
        "n_rejected": int(np.sum(reject_sorted)),
        "adjusted_pvalues": [round(v, 8) for v in adjusted_orig],
    }
