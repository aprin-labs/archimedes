// #1358: the API has served an honest tri/four-state rigor_gate_status
// ("pass"|"fail"|"pending"|"degenerate") since #1184 — but no file under
// ui/src/ read it, so a strategy on which zero statistics were computed
// (pending) rendered as though it had FAILED the rigor gate, on the Library
// table, the deployability chip, and the passport. These are source-text
// assertions (same shape as ui/test/app-visuals.test.js) — no jsdom/vitest,
// per CLAUDE.md's ui/ testing convention.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const strategies = readFileSync(
	new URL("../src/components/Strategies.jsx", import.meta.url),
	"utf8",
);
const passport = readFileSync(
	new URL("../src/components/StrategyPassport.jsx", import.meta.url),
	"utf8",
);
const helper = readFileSync(
	new URL("../src/rigorGateStatus.js", import.meta.url),
	"utf8",
);

// ── (a) Strategies.jsx: rigor_gate_status === "pending" checked BEFORE the
//        passes_rigor_gate === false "X" (does-not-pass) branch ──────────

test("Strategies.jsx checks rigor_gate_status pending before the does-not-pass X branch", () => {
	const pendingCheckIdx = strategies.indexOf(
		"s.rigor_gate_status === 'pending'",
	);
	const xBranchIdx = strategies.indexOf("s.passes_rigor_gate === false");
	assert.notEqual(pendingCheckIdx, -1, "no rigor_gate_status pending check found");
	assert.notEqual(xBranchIdx, -1, "no passes_rigor_gate === false branch found");
	assert.ok(
		pendingCheckIdx < xBranchIdx,
		"rigor_gate_status pending must be tested before the passes_rigor_gate === false X branch",
	);
	// The rendered icon block itself: every UNEVALUABLE arm (unknown state,
	// degenerate, pending) must precede both passes_rigor_gate badges, so the
	// false-badge X is only reachable once all three have been ruled out.
	assert.match(
		strategies,
		/\{unknownRigor \? \(\s*<span[\s\S]{0,400}\) : isDegenerate \? \(\s*<span[\s\S]{0,400}\) : isPending \? \(\s*<span[\s\S]{0,400}\) : s\.passes_rigor_gate === true \? \(\s*<span[\s\S]{0,300}\) : s\.passes_rigor_gate === false && \(/,
	);
	// The pending icon must not borrow the "does not pass" wording.
	assert.doesNotMatch(
		strategies,
		/isPending[\s\S]{0,200}Does not pass rigor gate/,
	);
});

// ── (b) DeployabilityChip: pending branch renders neither tag-negative nor
//        the "even at the most permissive level" title ──────────────────

function extractFunction(src, name) {
	const start = src.indexOf(`function ${name}`);
	assert.notEqual(start, -1, `${name} not found in source`);
	// Scoped to the next top-level `function`/`const` declaration after it —
	// wide enough to contain the whole small chip component, narrow enough
	// not to accidentally match unrelated code further down the file.
	const next = src.indexOf("\nconst STATUS_ORDER", start);
	assert.notEqual(next, -1, "could not bound DeployabilityChip's extent");
	return src.slice(start, next);
}

test("DeployabilityChip has a pending branch distinct from the genuine not-deployable branch", () => {
	const chip = extractFunction(strategies, "DeployabilityChip");
	assert.match(chip, /if \(deploy\.pending\) \{/);

	// The pending branch (up to its closing brace) must not render
	// tag-negative and must not carry the "even at the most permissive
	// level" wording that the genuine (never-scored vs. really-fails-every-
	// level) branch below it uses.
	const pendingBranchMatch = chip.match(
		/if \(deploy\.pending\) \{([\s\S]*?)\n\s*\}/,
	);
	assert.ok(pendingBranchMatch, "could not isolate the pending branch body");
	const pendingBranch = pendingBranchMatch[1];
	assert.doesNotMatch(pendingBranch, /tag-negative/);
	assert.doesNotMatch(
		pendingBranch,
		/even at the most permissive level/,
	);

	// The pre-existing "genuinely fails every level" branch is untouched —
	// still renders its own distinct wording, just no longer reachable for a
	// merely-never-scored strategy now that `pending` is checked first.
	assert.match(chip, /even at the most permissive level/);
});

// ── (b2) DeployabilityChip: the DEGENERATE branch ─────────────────────────
//
// A zero-variance persisted series leaves dsr_p_value / oos_sharpe at None,
// which trips RigorGateResult.blocked_by_floor — so this row reaches the chip
// as `blocked_by_floor: true, min_passing_level: null, pending: false` and,
// before this fix, rendered "Fails an always-on correctness floor". That is an
// assertion about a measurement that never happened. Verified against the real
// evaluator, not assumed:
//   run_rigor_gate('x', [0.0]*300, num_trials=1)
//     -> is_degenerate=True, blocked_by_floor=True, min_passing_level=None

test("DeployabilityChip's degenerate branch precedes blocked_by_floor and claims no measurement", () => {
	const chip = extractFunction(strategies, "DeployabilityChip");

	const degenerateIdx = chip.indexOf("if (deploy.degenerate) {");
	const floorIdx = chip.indexOf("if (deploy.blocked_by_floor) {");
	assert.notEqual(degenerateIdx, -1, "no deploy.degenerate branch found");
	assert.notEqual(floorIdx, -1, "no deploy.blocked_by_floor branch found");
	assert.ok(
		degenerateIdx < floorIdx,
		"degenerate must be tested BEFORE blocked_by_floor — a zero-variance " +
			"series trips the floor mechanically, so the floor branch would " +
			"otherwise claim a correctness failure nothing measured",
	);

	const branchMatch = chip.match(
		/if \(deploy\.degenerate\) \{([\s\S]*?)\n\s*\}/,
	);
	assert.ok(branchMatch, "could not isolate the degenerate branch body");
	const branch = branchMatch[1];

	// Neutral, like pending — never the red "this failed" treatment.
	assert.doesNotMatch(branch, /tag-negative/);
	// Must claim neither of the two graded verdicts.
	assert.doesNotMatch(branch, /even at the most permissive level/);
	assert.doesNotMatch(branch, /always-on correctness floor/);
	// And must NOT borrow pending's sentence: a degenerate row HAS persisted
	// returns (they are flat), so "no backtest data" is a fresh false claim.
	assert.doesNotMatch(branch, /no backtest data/);
	// It must still say what IS true, in the same words
	// agents/portfolio_agent.py `_format_strategies` already uses for this
	// state. (The citation was chat_service.py:395 until the vault-chat
	// surface was deleted; the wording itself is unchanged and still lives in
	// the status map that survived.)
	assert.match(branch, /zero-variance/);
	assert.match(branch, /not a real evaluation/);
});

// ── (a2) Strategies.jsx row icon: degenerate is neutral and distinct ───────

test("Strategies.jsx renders degenerate neutrally, before the X, without pending's wording", () => {
	const degenerateIdx = strategies.indexOf(
		"s.rigor_gate_status === 'degenerate'",
	);
	const xBranchIdx = strategies.indexOf("s.passes_rigor_gate === false");
	assert.notEqual(degenerateIdx, -1, "no rigor_gate_status degenerate check");
	assert.ok(
		degenerateIdx < xBranchIdx,
		"degenerate must be tested before the does-not-pass X branch",
	);

	// Isolate the rendered degenerate arm of the icon ternary.
	const armMatch = strategies.match(
		/\) : isDegenerate \? \(\s*(<span[\s\S]*?\/>)\s*\) : isPending/,
	);
	assert.ok(armMatch, "could not isolate the isDegenerate icon arm");
	const arm = armMatch[1];

	// Neutral colour token, same as pending — never --negative.
	assert.match(arm, /text-\[var\(--text-4\)\]/);
	assert.doesNotMatch(arm, /--negative/);
	// Neither of the two graded claims, and not pending's sentence either.
	assert.doesNotMatch(arm, /Does not pass rigor gate/);
	assert.doesNotMatch(arm, /no backtest data/);
	assert.match(arm, /zero-variance/);
	// Accessible name must exist and must not read as a failure.
	assert.match(arm, /aria-label="Rigor gate could not evaluate/);
});

// ── (c) StrategyPassport.jsx: a third, non-tag-positive state for pending ──

test("StrategyPassport renders a third pending state for the rigor badge, not tag-positive", () => {
	assert.match(passport, /const rigorPending = s\.rigor_gate_status === "pending"/);
	// The badge content ternary: passingRigor branch first (unchanged,
	// tag-positive), THEN a distinct rigorPending branch, THEN the genuine
	// "not Verified" fail case — three arms, not two.
	assert.match(
		passport,
		/passingRigor \? \(\s*<>[\s\S]{0,150}Verified[\s\S]{0,80}<\/>\s*\) : rigorPending \? \(\s*"pending — not yet evaluated"\s*\) : \(\s*"not Verified"\s*\)/,
	);
	// The wrapping span's className is keyed ONLY on passingRigor (unchanged)
	// — pending must never earn tag-positive by taking a shortcut through
	// that className expression.
	assert.match(
		passport,
		/className=\{`tag inline-flex items-center gap-1 \$\{passingRigor \? "tag-positive" : "tag-muted"\}`\}/,
	);
});

// ── (c2) StrategyPassport.jsx: the DEGENERATE state, same neutral bucket ──

test("StrategyPassport renders degenerate neutrally and never as a graded verdict", () => {
	assert.match(
		passport,
		/const rigorDegenerate = s\.rigor_gate_status === "degenerate"/,
	);

	// The Verified-badge ternary: the two unevaluable arms come FIRST, so a
	// stale passes_rigor_gate boolean can never carry an unevaluable row into
	// either "Verified" or "not Verified".
	assert.match(
		passport,
		/\{unknownRigor \? \(\s*UNKNOWN_RIGOR_LABEL\s*\) : rigorDegenerate \? \(\s*"unevaluable — zero-variance return series"\s*\) : passingRigor \? \(/,
	);

	// The header tag: degenerate before pending, and it must not reuse
	// pending's label — "pending" would claim the gate has yet to run, when in
	// fact it ran and found nothing gradeable.
	const headerDegIdx = passport.indexOf("rigor gate unevaluable");
	const headerPendIdx = passport.indexOf("rigor gate pending");
	assert.notEqual(headerDegIdx, -1, "no degenerate header tag found");
	assert.ok(
		headerDegIdx < headerPendIdx,
		"degenerate header tag must precede the pending one",
	);
	assert.doesNotMatch(
		passport,
		/rigorDegenerate \?[\s\S]{0,200}tag-positive/,
		"degenerate must never reach tag-positive",
	);
});

test("StrategyPassport suppresses the correctness-floor claim for a degenerate row", () => {
	// blocked_by_floor is true for every degenerate row (None dsr_p_value /
	// oos_sharpe trip the floor), so all five blockedByFloor consumers would
	// otherwise assert a floor failure nothing measured.
	assert.match(
		passport,
		/const blockedByFloor = gate\s*\?\s*gate\.blocked_by_floor === true && !gateDegenerate/,
	);
	// Suppressing must not go silent — an honest sentence replaces it.
	assert.match(passport, /\{gateDegenerate && \(/);
	assert.match(passport, /it was never\s+gradeable/);
	// And it must not quietly loosen deployment: `deployable` still gates on
	// minLevel, which is null for a degenerate row.
	assert.match(
		passport,
		/const deployable = minLevel != null && minLevel <= level && !blockedByFloor;/,
	);
});

// ── (e) Exhaustiveness default: BOTH surfaces, one shared answer ───────────

test("both surfaces route unknown rigor_gate_status through the same shared helper", () => {
	// assert.ok(regex.test(...)) rather than assert.match(...): these haystacks
	// are whole 40KB+ components, and a failed assert.match dumps the entire
	// file into the report, burying the one line that explains the failure.
	for (const [name, src] of [
		["Strategies.jsx", strategies],
		["StrategyPassport.jsx", passport],
	]) {
		// Quote style differs between the two files (Strategies.jsx is single-
		// quoted, StrategyPassport.jsx double-) — the point is the shared module,
		// not the quoting.
		assert.ok(
			/from ['"]\.\.\/rigorGateStatus\.js['"]/.test(src),
			`${name} must import the shared rigor-status helper, not re-derive the state list`,
		);
		assert.ok(
			src.includes("isUnknownRigorGateStatus("),
			`${name} has no exhaustiveness check`,
		);
		assert.ok(
			src.includes("warnUnknownRigorGateStatus("),
			`${name} does not warn on an unknown state`,
		);
		assert.ok(
			src.includes("UNKNOWN_RIGOR_LABEL"),
			`${name} does not render the shared em-dash fallback`,
		);
	}
});

test("the shared helper defines the four states, spares null, and is dev-only", () => {
	// The four states the API actually serves (schemas.py / tri_state_status).
	for (const state of ["pass", "fail", "pending", "degenerate"]) {
		assert.match(helper, new RegExp(`"${state}"`), `missing state ${state}`);
	}
	// The em-dash — this codebase's "nothing measured this" mark (#1326).
	assert.match(helper, /export const UNKNOWN_RIGOR_LABEL = "—"/);
	// null/undefined must NOT be treated as unknown: coerceGenerated rows have
	// never carried the field, and warning on them would fire on every
	// generated row and bury the case this guard exists for.
	assert.match(
		helper,
		/return status != null && !RIGOR_GATE_STATES\.includes\(status\)/,
	);
	// Production builds stay silent.
	assert.match(helper, /if \(import\.meta\.env\?\.PROD === true\) return;/);
});

// ── (d) StrategyPassport.jsx: four-branch num_trials_in_selection ternary,
//        the null/unspecified-scope branch renders neither sentence ──────

test("StrategyPassport num_trials_in_selection ternary has a null/unspecified branch that asserts nothing", () => {
	// The two-branch shape from before #1358 must be gone: an ungraded
	// strategy (num_trials_in_selection == null) used to fall into the
	// `else` arm and unconditionally assert "num_trials = 1" — a statistic
	// nothing computed for it.
	assert.match(
		passport,
		/s\.num_trials_in_selection == null \|\|\s*s\.num_trials_scope === "unspecified" \? \(/,
	);

	// Isolate that first branch's rendered content and confirm it contains
	// NEITHER the num_trials=1 sentence NOR the multi-candidate-correction
	// sentence — the whole point of a dedicated branch.
	const ternaryStart = passport.indexOf(
		's.num_trials_in_selection == null ||',
	);
	assert.notEqual(ternaryStart, -1);
	const firstBranchMatch = passport
		.slice(ternaryStart)
		.match(/\? \(\s*<>([\s\S]*?)<\/>\s*\) : /);
	assert.ok(firstBranchMatch, "could not isolate the null/unspecified branch body");
	const firstBranch = firstBranchMatch[1];
	assert.doesNotMatch(firstBranch, /num_trials = 1/);
	assert.doesNotMatch(firstBranch, /corrects the realized Sharpe/);

	// #1358 round-2 review: this branch is reached on a batch/DB-failure
	// pending case too (schemas.py's num_trials_scope == "unspecified" covers
	// both), so it must not assert the data fact "no persisted backtest
	// returns" — that claim is false on the DB-failure path, where stored
	// DSR/PBO/OOS numbers render right next to this sentence.
	assert.doesNotMatch(firstBranch, /no persisted backtest returns/);

	// It must still be a FOUR-armed ternary overall (null/unspecified →
	// generated_untracked_default → N>1 real correction → N=1
	// self-contained), not collapsed back to two or three.
	assert.match(
		passport,
		/s\.num_trials_scope === "generated_untracked_default" \? \(/,
	);
	assert.match(
		passport,
		/s\.num_trials_in_selection > 1 \? \(\s*<>[\s\S]{0,50}corrects the realized Sharpe/,
	);
	assert.match(
		passport,
		/graded on its own Sharpe \(num_trials = 1/,
	);
});

test("StrategyPassport's generated_untracked_default branch is distinct from both the ungraded and self-contained sentences", () => {
	// A row that DID get graded (issue A2(d)) but whose generation pipeline
	// never proved it tracks its own selection-pool size (backend/archimedes/
	// api/selection_bias_routes.py's _SCOPE_GENERATED_UNTRACKED_DEFAULT) must
	// neither claim "not graded yet" (it was) nor silently reuse the
	// true-self-contained N=1 sentence (it isn't self-contained — it's
	// forced/untrusted).
	const branchStart = passport.indexOf(
		's.num_trials_scope === "generated_untracked_default"',
	);
	assert.notEqual(branchStart, -1, "generated_untracked_default branch not found");
	const branchMatch = passport
		.slice(branchStart)
		.match(/\? \(\s*<>([\s\S]*?)<\/>\s*\) : /);
	assert.ok(branchMatch, "could not isolate the generated_untracked_default branch body");
	const branch = branchMatch[1];
	assert.doesNotMatch(branch, /not been graded/);
	assert.doesNotMatch(branch, /corrects the realized Sharpe/);
	// Must still surface the honest num_trials=1 fact, just with its own
	// "why" (untracked pool, not true self-containment).
	assert.match(branch, /num_trials = 1/);
	assert.match(branch, /did not record its own\s+selection-pool size/);
});

// ── A1: rigor_gate_status is actually read (mirrors the issue's grep -c) ──

test("rigor_gate_status is read on both surfaces (#1358 A1)", () => {
	assert.ok((strategies.match(/rigor_gate_status/g) || []).length >= 1);
	assert.ok((passport.match(/rigor_gate_status/g) || []).length >= 1);
});
