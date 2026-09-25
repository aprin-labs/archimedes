// The four states `rigor_gate_status` has carried on the wire since #1184 —
// backend/archimedes/api/schemas.py, derived from
// rigor_evaluator.RigorGateResult.tri_state_status ("degenerate" first, then
// pass/fail) and from selection_bias_routes' pre-gate "pending" branch.
//
// This list lives in ONE module because the Library table and the Passport are
// two independent renderings of the same verdict, and #1358 is what happens
// when they disagree about it. If the API grows a fifth state, both surfaces
// must start being honest about it on the same commit — not one of them
// silently mapping it onto "failed".
export const RIGOR_GATE_STATES = Object.freeze([
	"pass",
	"fail",
	"pending",
	"degenerate",
]);

// What both surfaces render for a status neither of them knows how to read. An
// em-dash is this codebase's "nothing measured this" mark (#1326), and it is
// the only honest glyph for a verdict we cannot interpret — never a pass,
// never a fail, never a guess.
export const UNKNOWN_RIGOR_LABEL = "—";

export const UNKNOWN_RIGOR_TITLE =
	"Unrecognised rigor-gate status — this build of the UI cannot interpret the verdict the API returned";

/** True only for a PRESENT value outside the four known states.
 *
 * `null`/`undefined` is deliberately NOT unknown. Rows coerced from the
 * generated-strategies feed have never carried this field, and both surfaces
 * already fall back to the `passes_rigor_gate` tri-state for them — a
 * documented shape, not a hole. Warning on it would fire on every generated
 * row and drown the case this exists to catch: the API grew a state this build
 * does not know, and the UI is rendering a confident verdict for it anyway.
 */
export function isUnknownRigorGateStatus(status) {
	return status != null && !RIGOR_GATE_STATES.includes(status);
}

/** Dev-time alarm for the above. Silent in a production build.
 *
 * The failure mode being guarded is a *silent* mis-render, so the alarm has to
 * be something a developer cannot miss and a user never sees. `import.meta.env`
 * is undefined outside a Vite build (`node --test`), hence the optional
 * chaining — same pattern as chain-config.js.
 */
export function warnUnknownRigorGateStatus(status, surface) {
	if (import.meta.env?.PROD === true) return;
	console.warn(
		`[rigor] ${surface}: unrecognised rigor_gate_status ${JSON.stringify(status)} — ` +
			`expected one of ${RIGOR_GATE_STATES.join(", ")}. Rendering "${UNKNOWN_RIGOR_LABEL}" ` +
			"rather than guessing a verdict.",
	);
}
