// metricDomain.js — the mathematical domain of every metric the Library, the
// Passport and the Leaderboard render, and the only formatter allowed to turn
// one of those numbers into display text (#1651).
//
// ── Why this file exists ────────────────────────────────────────────────────
//
// A real library row (`avellaneda_lee_2010_pca_statarb`, audited in
// docs/audits/2026-07-09-curated-consolidation.md §2.4) persisted
// `max_drawdown = 1.303`, and every surface rendered it verbatim as
// "−130.3%". A drawdown is a fraction of a PEAK: an account cannot lose more
// than the whole peak without its equity going negative, so −130.3% is not a
// bad result, it is an arithmetically impossible one, and a reader who sees it
// learns that our numbers are wrong rather than that the strategy is bad.
//
// The COMPUTATION that produced it is already fixed upstream, at the engine's
// data boundary: `normalize_ohlcv` now refuses non-positive price bars (CL=F
// settled at −$37.63 on 2020-04-20, and a long position marked at a negative
// price drags portfolio value through zero), and
// analytics-engine/tests/test_drawdown_invariant.py pins
// `max_drawdown_pct < 100 <=> the reported equity curve stayed positive`.
//
// What that fix does NOT do is change history. The persisted row still holds
// 1.303 and — per the append-only ledger rule — must keep holding it. So the
// display layer needs its own discipline, and this is it:
//
//   1. Every metric these surfaces render has a declared domain, in the units
//      the API serves it in. The declaration is the enumeration the tests
//      quantify over; a metric with no entry here is a metric nobody bounded.
//   2. A value outside its domain is rendered AT THE BOUND and carries a
//      visible annotation naming the value that was actually reported. It is
//      never silently clamped and never silently dropped — a clamp the reader
//      cannot see is the same lie in the other direction.
//   3. A value that is absent gets a REASON, drawn from the four-state
//      pass/fail/pending/degenerate vocabulary the rigor gate already speaks
//      (rigorGateStatus.js). A bare em-dash with no tooltip is indistinguishable
//      from a rendering bug.
//
// Nothing here recomputes, corrects or writes back a stored number. `reported`
// travels with every clamped result precisely so the display can keep saying
// what the ledger says while refusing to draw it as if it were possible.

import {
	UNKNOWN_RIGOR_TITLE,
	isUnknownRigorGateStatus,
} from "./rigorGateStatus.js";

/** The glyph for a value nothing measured.
 *
 * Deliberately equal to `generationCost.NOT_MEASURED` and
 * `rigorGateStatus.UNKNOWN_RIGOR_LABEL` — this codebase has exactly one mark
 * for "no number here" (#1326) and three modules agreeing by accident is one
 * refactor away from two of them disagreeing. `metric-domain.test.js` asserts
 * the three are the same character.
 */
export const ABSENT_LABEL = "—";

/** How a metric's absence is explained, in the rigor gate's own vocabulary. */
export const ABSENCE_STATES = Object.freeze([
	"pending",
	"degenerate",
	"not_measured",
	"unknown",
]);

/** What `formatMetric` can conclude about a value. */
export const METRIC_STATES = Object.freeze([
	// A number inside its declared domain, rendered as-is.
	"measured",
	// A number OUTSIDE its declared domain: rendered at the bound, annotated
	// with what was actually reported.
	"clamped",
	// Present but not a finite number (NaN, ±Infinity, a string, an object).
	// There is no bound to clamp such a value to, so the honest render is the
	// absence glyph plus a note saying the API sent something unreadable.
	"unreadable",
	...ABSENCE_STATES,
]);

// ── The enumeration ─────────────────────────────────────────────────────────
//
// `min`/`max` are stated in the units the API serves, NOT in the units the cell
// displays: `max_drawdown` arrives as a POSITIVE fraction of the peak (0.15 =
// a 15% drawdown) and the surfaces prepend the minus sign themselves, which is
// exactly how a served 1.303 became a displayed "−130.3%".
//
// `why` is the one-sentence justification for the bound and is shown to the
// reader when a clamp fires. A bound with no `why` is a threshold somebody
// picked; a bound with one is a fact about the quantity.
export const METRIC_DOMAINS = Object.freeze({
	max_drawdown: Object.freeze({
		label: "Max drawdown",
		min: 0,
		max: 1,
		format: "negpct",
		why: "A drawdown is a fraction of the peak equity it is measured from, so it runs from 0% (no fall) to 100% (the whole peak). Above 100% means the equity curve went negative — a ruined account, not a survivable loss.",
	}),
	cagr: Object.freeze({
		label: "CAGR",
		min: -1,
		max: Number.POSITIVE_INFINITY,
		format: "pct",
		why: "A compound growth rate cannot be below −100% per year: that is losing the entire stake, and there is nothing left to lose twice.",
	}),
	win_rate: Object.freeze({
		label: "Win rate",
		min: 0,
		max: 1,
		format: "pct",
		why: "A share of trades that won, so between none of them and all of them.",
	}),
	pbo_score: Object.freeze({
		label: "PBO",
		min: 0,
		max: 1,
		format: "ratio",
		why: "The probability of backtest overfitting is a probability.",
	}),
	dsr_p_value: Object.freeze({
		label: "DSR confidence",
		min: 0,
		max: 1,
		format: "ratio",
		why: "Despite the legacy `p_value` name this is a confidence — the probability the Sharpe survives deflation — and probabilities live in [0, 1].",
	}),
	correlation_to_spy: Object.freeze({
		label: "ρ to SPY",
		min: -1,
		max: 1,
		format: "ratio",
		why: "A Pearson correlation coefficient is bounded by ±1 (Cauchy–Schwarz).",
	}),
	correlation_to_btc: Object.freeze({
		label: "ρ to BTC",
		min: -1,
		max: 1,
		format: "ratio",
		why: "A Pearson correlation coefficient is bounded by ±1 (Cauchy–Schwarz).",
	}),
	cumulative_return: Object.freeze({
		label: "Realised return",
		min: -1,
		max: Number.POSITIVE_INFINITY,
		format: "signedpct",
		why: "A cumulative simple return compounded from an append-only ledger cannot be below −100%: that is the whole stake gone.",
	}),
	board_fdr_adjusted_p: Object.freeze({
		label: "BH-adjusted p",
		min: 0,
		max: 1,
		format: "ratio",
		why: "A Benjamini–Hochberg adjusted p-value is a probability.",
	}),
	conviction_score: Object.freeze({
		label: "Conviction",
		min: 0,
		max: 100,
		format: "score",
		why: "The leaderboard's conviction score is defined on a 0–100 scale.",
	}),
	total_trades: Object.freeze({
		label: "Trades",
		min: 0,
		max: Number.POSITIVE_INFINITY,
		format: "count",
		why: "A count of trades cannot be negative.",
	}),
	// ── Unbounded by construction ───────────────────────────────────────────
	// A Sharpe-family ratio has no bound this layer can justify: the real one,
	// |SR_annualised| <= sqrt(252 * (n - 1)), needs the sample size `n`, and no
	// Library, Passport or Leaderboard payload carries it (see the issue's
	// "Sharpe on <4 bars"). Rather than invent a threshold, these declare the
	// only thing that IS certain — the value has to be a finite number — and
	// `formatMetric` enforces that for every entry alike. Stating the absence
	// of a bound explicitly is the point: it keeps the enumeration exhaustive,
	// so `metric-domain.test.js` can require every rendered metric to appear
	// here without the sharpe family quietly falling through an `undefined`.
	sharpe_ratio: Object.freeze({
		label: "Sharpe",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "A Sharpe ratio has no bound that can be checked without the sample size, which this payload does not carry; only finiteness is enforced.",
	}),
	sortino_ratio: Object.freeze({
		label: "Sortino",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "A Sortino ratio has no bound that can be checked without the sample size, which this payload does not carry; only finiteness is enforced.",
	}),
	calmar_ratio: Object.freeze({
		label: "Calmar",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "Calmar is CAGR over max drawdown; both signs are reachable and it is unbounded as the drawdown shrinks.",
	}),
	deflated_sharpe_ratio: Object.freeze({
		label: "DSR",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "A deflated Sharpe is a Sharpe; only finiteness is enforced.",
	}),
	out_of_sample_sharpe: Object.freeze({
		label: "OOS Sharpe",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "An out-of-sample Sharpe is a Sharpe; only finiteness is enforced.",
	}),
	paper_claimed_sharpe: Object.freeze({
		label: "Paper-claimed Sharpe",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "A number transcribed from a paper, not measured here; only finiteness is enforced.",
	}),
	kelly_fraction: Object.freeze({
		label: "Kelly fraction",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "A Kelly fraction is a stored sizing figure that can legitimately be negative (a short) or above 1 (levered); nothing at this layer justifies a tighter bound than finiteness.",
	}),
	sharpe_ci_lower: Object.freeze({
		label: "Sharpe CI lower",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "A confidence bound on a Sharpe is a Sharpe; only finiteness is enforced.",
	}),
	sharpe_ci_upper: Object.freeze({
		label: "Sharpe CI upper",
		min: Number.NEGATIVE_INFINITY,
		max: Number.POSITIVE_INFINITY,
		format: "ratio",
		why: "A confidence bound on a Sharpe is a Sharpe; only finiteness is enforced.",
	}),
});

/** Default decimal places per format. A caller may override per cell. */
const DEFAULT_DIGITS = Object.freeze({
	negpct: 1,
	pct: 1,
	signedpct: 2,
	ratio: 2,
	score: 1,
	count: 0,
});

// ── Absence, with a reason ──────────────────────────────────────────────────

export const PENDING_METRIC_TITLE =
	"PENDING — no backtest has run on this strategy yet, so nothing has measured this metric. Not a zero and not a failure.";

export const DEGENERATE_METRIC_TITLE =
	"DEGENERATE — the persisted return series is zero-variance (broken data or a zero-trade backtest), not a real evaluation, so this metric is undefined for it.";

export const NOT_MEASURED_METRIC_TITLE =
	"Not measured — no value was recorded for this metric on this row.";

/**
 * Why a metric cell is empty, in the rigor gate's four-state vocabulary.
 *
 * The branch ORDER mirrors Strategies.jsx's rigor badge exactly (unknown →
 * degenerate → pending → the rest) and for the same reason it was fixed there
 * in #1358: every UNEVALUABLE state has to be ruled out before a row is
 * described in terms that imply it was evaluated. "degenerate" deliberately
 * does not borrow "pending"'s sentence — a degenerate row HAS persisted
 * returns, they are just flat, so "no backtest has run" would be a fresh lie.
 *
 * @param {object|null|undefined} row — the strategy / leaderboard row
 * @returns {{state: string, title: string}}
 */
export function absenceReason(row) {
	if (row == null || typeof row !== "object") {
		return { state: "not_measured", title: NOT_MEASURED_METRIC_TITLE };
	}
	const status = row.rigor_gate_status;
	if (isUnknownRigorGateStatus(status)) {
		return { state: "unknown", title: UNKNOWN_RIGOR_TITLE };
	}
	if (status === "degenerate") {
		return { state: "degenerate", title: DEGENERATE_METRIC_TITLE };
	}
	// `is_backtest_placeholder` is the pre-backtest hypothesis flag the
	// generated-strategies feed sets — `coerceGenerated` hardcodes it `true` on
	// every generated row; a PRESENT-but-null `passes_rigor_gate` is the same
	// "no verdict yet" case for rows that never carried rigor_gate_status at all
	// (coerceGenerated carries the served four-state through, and falls back to
	// null when the API sent none).
	//
	// A `row.status === "pending_backtest"` operand used to sit here too. That
	// status was a client-side invention of coerceGenerated's, retired with the
	// verdict of record (docs/adr/rigor-verdict-of-record.md): no producer emits
	// it any more, and a branch keyed on a string nothing sends is a claim a
	// future reader would trust. The rows it used to catch are caught by
	// `is_backtest_placeholder` above, which is set on every one of them.
	//
	// The `in` check is load-bearing, not defensive tidiness. A live-paper row
	// carries neither field, so a bare `row.passes_rigor_gate == null` would be
	// true for it and every empty cell on the forward board would claim "no
	// backtest has run" — a sentence about the wrong kind of evidence entirely,
	// on the one board whose numbers are deliberately NOT backtests.
	if (
		status === "pending" ||
		row.is_backtest_placeholder === true ||
		("passes_rigor_gate" in row && row.passes_rigor_gate == null)
	) {
		return { state: "pending", title: PENDING_METRIC_TITLE };
	}
	return { state: "not_measured", title: NOT_MEASURED_METRIC_TITLE };
}

// ── Formatting ──────────────────────────────────────────────────────────────

function formatValue(format, value, digits) {
	if (format === "count") return Number(value).toLocaleString("en-US");
	if (format === "negpct") {
		// The served field is a positive fraction and the cell shows the LOSS, so
		// the sign flip happens HERE rather than in each caller — the duplicated
		// `−${fmtPct(v)}` across four components is what let two of them also
		// apply Math.abs(), silently turning a contract-violating negative
		// drawdown into an ordinary-looking loss. Flipping properly means a
		// negative served value renders with a visible "+", which is exactly
		// what a drawdown must never be, so the annotation has something to
		// point at instead of a plausible lie.
		const shown = -(value * 100);
		const rounded = Number(shown.toFixed(digits));
		if (rounded === 0) return `${(0).toFixed(digits)}%`;
		const magnitude = Math.abs(shown).toFixed(digits);
		return rounded < 0 ? `−${magnitude}%` : `+${magnitude}%`;
	}
	if (format === "pct") return `${(value * 100).toFixed(digits)}%`;
	if (format === "signedpct") {
		// Always signed, so a forward return never reads as a bare magnitude.
		const pct = value * 100;
		return `${pct >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(digits)}%`;
	}
	return Number(value).toFixed(digits);
}

/** Human-readable domain, in display units, for the clamp annotation. */
export function domainLabel(key, digits) {
	const domain = METRIC_DOMAINS[key];
	if (!domain) return null;
	const dp = digits ?? DEFAULT_DIGITS[domain.format] ?? 2;
	const lo = Number.isFinite(domain.min)
		? formatValue(domain.format, domain.min, dp)
		: "−∞";
	const hi = Number.isFinite(domain.max)
		? formatValue(domain.format, domain.max, dp)
		: "+∞";
	// `negpct` flips the order — a larger stored drawdown displays as a MORE
	// negative percentage — so the interval is printed in display order.
	return domain.format === "negpct" ? `[${hi}, ${lo}]` : `[${lo}, ${hi}]`;
}

/**
 * Dev-time alarm for a metric rendered through this module with no declared
 * domain. Silent in a production build (same pattern as
 * `warnUnknownRigorGateStatus`) because the user-visible behaviour is already
 * correct — the value is finiteness-checked and rendered — while the thing a
 * developer must not miss is that the enumeration the tests quantify over has
 * a hole in it.
 */
export function warnUndeclaredMetric(key, surface) {
	if (import.meta.env?.PROD === true) return;
	console.warn(
		`[metrics] ${surface || "unknown surface"}: no domain declared for metric ` +
			`${JSON.stringify(key)} in metricDomain.js. It is rendered with a ` +
			"finiteness check only, and no test bounds it.",
	);
}

/**
 * Turn one served metric value into display text that cannot be impossible.
 *
 * @param {string} key — a key of METRIC_DOMAINS
 * @param {number|null|undefined} value — as the API serves it
 * @param {object} [options]
 * @param {object} [options.row] — the row, used only to explain an absence
 * @param {string} [options.format] — override the display format
 * @param {number} [options.digits] — override the decimal places
 * @param {string} [options.surface] — for the dev warning only
 * @returns {{state: string, label: string, note: string|null, title: string|null,
 *            impossible: boolean, reported: number|null}}
 *   `label` is always safe to render. `note` is the VISIBLE annotation and is
 *   non-null exactly when the displayed number is not the reported one — the
 *   caller is not free to drop it, which is why `MetricValue.jsx` exists and
 *   every surface goes through it.
 */
export function formatMetric(key, value, options = {}) {
	const { row = null, format, digits, surface = "" } = options;
	const domain = METRIC_DOMAINS[key];
	if (!domain) warnUndeclaredMetric(key, surface);
	const fmt = format ?? domain?.format ?? "ratio";
	const dp = digits ?? DEFAULT_DIGITS[fmt] ?? 2;

	if (value == null) {
		const absence = absenceReason(row);
		return {
			state: absence.state,
			label: ABSENT_LABEL,
			note: null,
			title: absence.title,
			impossible: false,
			reported: null,
		};
	}

	if (typeof value !== "number" || !Number.isFinite(value)) {
		// No bound to clamp to: NaN is not "too big" and a string is not a
		// number at all. The absence glyph is the only honest label, and the
		// note says why it is there so it cannot be read as "not measured".
		return {
			state: "unreadable",
			label: ABSENT_LABEL,
			note: "unreadable value",
			title:
				`${domain?.label ?? key}: the API returned ${JSON.stringify(value)}, ` +
				"which is not a finite number. Shown as unmeasured rather than drawn as a value.",
			impossible: true,
			reported: null,
		};
	}

	const min = domain?.min ?? Number.NEGATIVE_INFINITY;
	const max = domain?.max ?? Number.POSITIVE_INFINITY;
	if (value < min || value > max) {
		const bound = value < min ? min : max;
		const reportedText = formatValue(fmt, value, dp);
		return {
			state: "clamped",
			label: formatValue(fmt, bound, dp),
			note: `reported ${reportedText} — outside the possible range`,
			title:
				`${domain?.label ?? key} was recorded as ${reportedText}, outside the ` +
				`possible range ${domainLabel(key, dp) ?? "for this metric"}. ` +
				`${domain?.why ?? ""} ` +
				"Displayed at the bound; the recorded value is unchanged.",
			impossible: true,
			reported: value,
		};
	}

	return {
		state: "measured",
		label: formatValue(fmt, value, dp),
		note: null,
		title: null,
		impossible: false,
		reported: value,
	};
}
