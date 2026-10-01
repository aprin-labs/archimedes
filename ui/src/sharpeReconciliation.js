// One sentence reconciling the two Sharpes a passport shows (#1769).
//
// The Backtest card renders a Sharpe (0.70, say) and the Rigor card renders a
// Deflated Sharpe (0.08) beside a verdict pill. Nothing on the page told a
// first-time reader that these are the SAME edge measured twice — once raw, and
// once after the best-of-N selection null is subtracted — so the honest gap
// between them read as two contradictory results, or as a typo.
//
// Every number in the sentence is one the payload already carries and the page
// already prints; this module contributes no arithmetic and no threshold. The
// numbers themselves are rendered by <MetricValue> at the call site (#1651 —
// no surface may format a metric itself), so what lives here is the prose whose
// TRUTH depends on the row: which side of the bar the strategy is on.
//
// A plain .js module so `node --test` executes it, per the ui/ testing note in
// CLAUDE.md.

import { RIGOR_GATE_STATES } from "./rigorGateStatus.js";

const CLEARS = "and this strategy clears the Archimedes Verified bar";
const BELOW = "and this strategy sits below the Archimedes Verified bar";

/** Whether the reconciliation sentence has both numbers it is about.
 *
 * Both, not either: the sentence's whole subject is the RELATIONSHIP between
 * them. With only one in hand there is nothing to reconcile, and a half
 * sentence would have to invent the missing side.
 */
export function hasSharpeReconciliation(s) {
	return (
		Number.isFinite(s?.sharpe_ratio) && Number.isFinite(s?.deflated_sharpe_ratio)
	);
}

/** The trailing clause naming where this strategy stands relative to the bar,
 * or `null` when the row does not support any such claim.
 *
 * Keyed on `rigor_gate_status` — the four-state verdict the API has served
 * since #1184 — and NOT on `passes_rigor_gate` alone, because that boolean is
 * false for "pending" and "degenerate" too (fail-closed by design) and reading
 * it by itself is exactly how #1358 rendered "never evaluated" as "evaluated
 * and lost". `passes_rigor_gate` is the documented fallback only for rows that
 * carry no status field at all.
 *
 * `null` for a status this build does not recognise: the em-dash rule (#1326)
 * applied to prose — no verdict is better than a guessed one.
 */
export function rigorBarClause(s) {
	const status = s?.rigor_gate_status;
	if (status == null) {
		if (s?.passes_rigor_gate === true) return CLEARS;
		if (s?.passes_rigor_gate === false) return BELOW;
		return null;
	}
	if (!RIGOR_GATE_STATES.includes(status)) return null;
	if (status === "pass") return CLEARS;
	if (status === "fail") return BELOW;
	if (status === "pending") return "and the gate has not graded this strategy yet";
	// "degenerate": there IS a persisted series, it just carries no variance.
	// Never "below the bar" — nothing was measurable to fall short of it.
	return "and the gate had nothing to grade — this strategy's persisted return series carries no variance";
}
