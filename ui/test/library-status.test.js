// The Library status pill, and the one field it is allowed to read.
//
// #1747: the Generated tab's green "Live ✓" and its `passes_rigor_gate` came
// from the SAME generation-time blob (`row.rigor_verdict`), so the pill's own
// demotion rule — "status says live, the gate says fail" — could never fire on
// that tab. Twenty-one rows read "Live ✓" beside their own passports reading
// "Reference only — gate failed".
//
// Two kinds of assertion here, deliberately:
//   * REAL BEHAVIOUR, by importing ui/src/libraryStatus.js. That module is plain
//     `.js` precisely so this is possible — `.jsx` is not importable under
//     `node --test` (see ui/test/account-management.test.js).
//   * SOURCE TEXT, for the wiring inside Strategies.jsx / StrategyPassport.jsx,
//     which are `.jsx` and can only be asserted this way (same shape as
//     ui/test/rigor-tristate.test.js).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	DEGENERATE_LABEL,
	DEGENERATE_TITLE,
	GATE_FAILED_LABEL,
	NOT_GRADED_LABEL,
	NOT_GRADED_TITLE,
	statusLabel,
	statusTag,
	statusTitle,
} from "../src/libraryStatus.js";

const strategies = readFileSync(
	new URL("../src/components/Strategies.jsx", import.meta.url),
	"utf8",
);
const passport = readFileSync(
	new URL("../src/components/StrategyPassport.jsx", import.meta.url),
	"utf8",
);
const libraryStatus = readFileSync(
	new URL("../src/libraryStatus.js", import.meta.url),
	"utf8",
);
const metricDomain = readFileSync(
	new URL("../src/metricDomain.js", import.meta.url),
	"utf8",
);

// ── (a) The four states, as behaviour ──────────────────────────────────────

test("a graded pass on a live row is the only way to green", () => {
	// MUTATION: return 'tag-positive' from any other arm.
	assert.equal(statusTag("live", true, "pass"), "tag-positive");
	assert.equal(statusLabel("live", true, "pass"), "Live");

	for (const [status, passes, gate] of [
		["live", false, "fail"],
		["live", null, "pending"],
		["live", null, null],
		["live", false, "degenerate"],
	]) {
		assert.notEqual(
			statusTag(status, passes, gate),
			"tag-positive",
			`green must be unreachable for (${status}, ${passes}, ${gate})`,
		);
	}
});

test("a failed gate demotes a live row with the shared label", () => {
	// MUTATION: restore the local `status === 'live' && passesRigor === false`
	// pair inside Strategies.jsx, or change either literal.
	assert.equal(statusTag("live", false, "fail"), "tag-muted");
	assert.equal(statusLabel("live", false, "fail"), GATE_FAILED_LABEL);
	assert.equal(GATE_FAILED_LABEL, "Reference only — gate failed");
});

test("an ungraded row says so, and never says it failed", () => {
	// MUTATION: widen the demotion guard to `passesRigor !== true`. Both asserts
	// redden — and that widening is the #1358 defect in mirror image: it asserts
	// a failure for something no gate ever ran on, contradicting the clock icon
	// the very same row renders.
	for (const [passes, gate] of [
		[null, "pending"],
		[null, null],
		[undefined, undefined],
	]) {
		assert.equal(statusTag("live", passes, gate), "tag-muted");
		assert.equal(statusLabel("live", passes, gate), NOT_GRADED_LABEL);
		assert.notEqual(statusLabel("live", passes, gate), GATE_FAILED_LABEL);
	}
	assert.equal(NOT_GRADED_LABEL, "Not yet graded");
});

test("a degenerate row gets its own words, not pending's and not fail's", () => {
	// MUTATION: fold "degenerate" into either neighbouring arm. "Pending" would
	// claim nothing was evaluated (the returns exist, they are just flat);
	// "fail" would claim the strategy was graded and lost.
	assert.equal(statusTag("live", false, "degenerate"), "tag-muted");
	assert.equal(statusLabel("live", false, "degenerate"), DEGENERATE_LABEL);
	assert.notEqual(DEGENERATE_LABEL, NOT_GRADED_LABEL);
	assert.notEqual(DEGENERATE_LABEL, GATE_FAILED_LABEL);
});

test("a curated row with a real pass keeps its label", () => {
	// CONTROL. MUTATION: reorder the arms so the four-state arms swallow these.
	// The Examples tab serves curated rows with a live verdict and a `validated`
	// / `candidate` status; a row the gate actually passed must read exactly as
	// it did before.
	//
	// Named honestly. This used to be called "the curated tab's existing labels
	// are unchanged", which asserted one tuple and claimed the whole tab —
	// see the next test for the curated labels that DO move.
	assert.equal(statusTag("validated", true, "pass"), "tag-accent");
	assert.equal(statusLabel("validated", true, "pass"), "Validated");
	assert.equal(statusLabel("", true, "pass"), "Candidate");
});

test("curated rows with a pending or degenerate verdict change label, on purpose", () => {
	// NOT a control — this is the intended movement on the Examples tab, pinned
	// so it is a decision rather than a surprise (docs/adr/rigor-verdict-of-record.md,
	// "Two curated pill labels change wording"). A `validated` store status
	// beside a gate that has not graded is the same false-confidence shape as
	// the generated side, so it loses its accent and says what is true.
	// MUTATION: drop the `isUngraded` / degenerate arms from statusLabel — these
	// redden and the row goes back to claiming "Validated" over an ungraded gate.
	assert.equal(statusLabel("validated", null, "pending"), NOT_GRADED_LABEL);
	assert.equal(statusTag("validated", null, "pending"), "tag-muted");
	assert.equal(statusLabel("validated", false, "degenerate"), DEGENERATE_LABEL);
	assert.equal(statusTag("validated", false, "degenerate"), "tag-muted");
	assert.equal(statusLabel("candidate", null, "pending"), NOT_GRADED_LABEL);
	assert.equal(statusLabel("candidate", false, "degenerate"), DEGENERATE_LABEL);
});

// ── (a2) The retired `pending_backtest` invention ──────────────────────────

test("a strategy the gate FAILED never reads as pending, with or without a DSR", () => {
	// THE DEFECT THIS CLOSES. `/api/strategies/generated` serves no
	// `sharpe_ratio` key at all (StrategyRecord.to_dict has none, and the
	// passport overlay adds only the verdict + its four numbers), and
	// `deflated_sharpe_ratio` is Optional on a BacktestResult — so a real
	// `fail` with no DSR was the common case, not a corner. The old
	// coerceGenerated rewrote exactly that row to `pending_backtest` and the
	// pill rendered amber "Pending Backtest": a badge asserting NO GATE HAD RUN
	// on a row a gate ran and failed.
	// MUTATION: restore `status: honestStatus` with the `!hasRealMetrics &&
	// row.status === 'rejected'` rewrite in coerceGenerated, or re-add the
	// `status === "pending_backtest"` arms to statusTag/statusLabel.
	for (const gate of ["fail", "pass", "pending", "degenerate"]) {
		assert.notEqual(
			statusTag("pending_backtest", false, gate),
			"tag-warning",
			`no state may render the retired amber pill (${gate})`,
		);
		assert.notEqual(
			statusLabel("pending_backtest", false, gate),
			"Pending Backtest",
			`no state may render the retired label (${gate})`,
		);
	}
	// The real served shape: store status "rejected", gate says fail, no DSR.
	assert.equal(statusLabel("rejected", false, "fail"), "Rejected");
	assert.equal(statusTag("rejected", false, "fail"), "tag-muted");
	assert.equal(statusTitle("rejected", false, "fail"), undefined);
});

test("the tooltip fires on exactly the rows the pill calls ungraded", () => {
	// The tooltip used to be keyed on `s.status === 'pending_backtest'` while
	// the pill was keyed on the four-state, so the two disagreed in BOTH
	// directions: silent on an ungraded `candidate` row, and firing on a
	// DEGENERATE row to announce that no backtest had run — over returns that
	// exist. One state, one vocabulary.
	// MUTATION: key the `title=` in Strategies.jsx back on
	// `s.status === 'pending_backtest'`, or swap the two arms of statusTitle.
	for (const [passes, gate] of [
		[null, "pending"],
		[null, null],
		[undefined, undefined],
	]) {
		assert.equal(statusLabel("rejected", passes, gate), NOT_GRADED_LABEL);
		assert.equal(statusTitle("rejected", passes, gate), NOT_GRADED_TITLE);
	}
	// Degenerate gets its OWN sentence, never the ungraded one.
	assert.equal(statusLabel("rejected", false, "degenerate"), DEGENERATE_LABEL);
	assert.equal(statusTitle("rejected", false, "degenerate"), DEGENERATE_TITLE);
	assert.notEqual(DEGENERATE_TITLE, NOT_GRADED_TITLE);
	assert.doesNotMatch(
		DEGENERATE_TITLE,
		/pending a backtest run/,
		"a degenerate row HAS a backtest — its tooltip must not say one is pending",
	);
	// Same argument, one row over. Since #1746 PR-B a backtest and a grade are
	// separate events, so an UNGRADED row usually has a backtest too — every
	// curated strategy does, between that PR's deploy and its grading run.
	assert.doesNotMatch(
		NOT_GRADED_TITLE,
		/pending a backtest run/,
		"an ungraded row may already HAVE a backtest and be waiting only on the grading run — the tooltip must not name the backtest as the missing thing",
	);
	assert.match(
		NOT_GRADED_TITLE,
		/grading run/,
		"the ungraded tooltip must name the grading run as a possible cause, not only the backtest",
	);
	// A graded row gets no tooltip at all; its pill already says what it means.
	assert.equal(statusTitle("live", true, "pass"), undefined);
	assert.equal(statusTitle("live", false, "fail"), undefined);
});

test("coerceGenerated invents no status of its own, and the retired one has no readers", () => {
	// MUTATION: put `? 'pending_backtest'` back in coerceGenerated, or restore
	// the `row.status === "pending_backtest"` operand in metricDomain.js.
	const coerce = strategies.slice(
		strategies.indexOf("function coerceGenerated(row)"),
	);
	const body = coerce.slice(0, coerce.indexOf("\n}\n"));
	assert.ok(
		body.includes("status: row.status || 'candidate'"),
		"the store status must travel unchanged",
	);
	assert.ok(
		!/\?\s*'pending_backtest'/.test(body),
		"coerceGenerated must not rewrite a store status to an invented one",
	);
	assert.ok(
		!body.includes("hasRealMetrics"),
		"the number-presence heuristic must be gone — the four-state is the answer",
	);
	// No LIVE reader of the retired status may remain anywhere in ui/src. Only
	// prose explaining why it went is allowed.
	for (const [file, text] of [
		["Strategies.jsx", strategies],
		["libraryStatus.js", libraryStatus],
		["metricDomain.js", metricDomain],
	]) {
		for (const line of text.split("\n")) {
			const code = line.trim();
			if (code.startsWith("//") || code.startsWith("*")) continue;
			assert.ok(
				!code.includes("pending_backtest"),
				`${file} still has a live reader of the retired status: ${code}`,
			);
		}
	}
});

test("the pill's three helpers are called with the same argument list", () => {
	// MUTATION: drop the third argument from any one call, or pass a different
	// expression to statusTitle than to statusTag/statusLabel. The three
	// answers — class, words, tooltip — must be derived from one row's one
	// verdict, on both the desktop row and the mobile card.
	const args = "\\(s\\.status, s\\.passes_rigor_gate, s\\.rigor_gate_status\\)";
	for (const fn of ["statusTag", "statusLabel", "statusTitle"]) {
		const calls = strategies.match(new RegExp(fn + args, "g"));
		assert.equal(
			calls?.length,
			2,
			`${fn} must be called with the full four-state on BOTH the table row and the lib-card`,
		);
	}
	assert.ok(
		strategies.includes(
			"import { statusTag, statusLabel, statusTitle } from '../libraryStatus.js'",
		),
		"all three helpers come from the shared module",
	);
});

// ── (b) The wiring, as source text ─────────────────────────────────────────

test("coerceGenerated no longer reads rigor_verdict for the badge or its numbers", () => {
	// MUTATION: restore any of `verdict?.dsr`, `verdict?.pbo`,
	// `verdict?.oos_sharpe`, `verdict?.dsr_p_value`, or
	// `passes_rigor_gate: verdict ? Boolean(verdict.passing) : null`.
	const coerce = strategies.slice(
		strategies.indexOf("function coerceGenerated(row)"),
	);
	const body = coerce.slice(0, coerce.indexOf("\n}\n"));

	assert.ok(
		!/verdict\?\./.test(body),
		"coerceGenerated must not read any field off the generation-time rigor_verdict blob",
	);
	assert.ok(
		!body.includes("Boolean(verdict.passing)"),
		"the badge must not be coerced from the fusion verdict's `passing`",
	);
	assert.ok(
		body.includes(
			"passes_rigor_gate: typeof row.passes_rigor_gate === 'boolean' ? row.passes_rigor_gate : null",
		),
		"passes_rigor_gate must be taken as a LITERAL boolean, with null preserved as null",
	);
	assert.ok(
		body.includes("rigor_gate_status: row.rigor_gate_status ?? null"),
		"the four-state verdict must be carried through to the pill",
	);
	for (const field of [
		"deflated_sharpe_ratio: row.deflated_sharpe_ratio",
		"pbo_score: row.pbo_score",
		"out_of_sample_sharpe: row.out_of_sample_sharpe",
		"dsr_p_value: row.dsr_p_value",
	]) {
		assert.ok(
			body.includes(field),
			`the rigor numbers must come from the served row, not the fusion blob (missing: ${field})`,
		);
	}
});

test("the pill helpers are imported, not redefined", () => {
	// MUTATION: re-declare `function statusTag(...)` inside Strategies.jsx.
	// The per-call-site argument lists are pinned by "the pill's three helpers
	// are called with the same argument list" above, which also covers BOTH
	// renderings — the desktop table row AND the mobile lib-card. The card has
	// no rigor icon of its own, so its pill is the only verdict signal on a
	// phone; a fix that reached only the table would leave a bare green "Live"
	// there with zero counter-signal.
	assert.ok(
		!/^function statusTag\(/m.test(strategies),
		"Strategies.jsx must not carry its own copy of statusTag",
	);
	assert.ok(
		!/^function statusLabel\(/m.test(strategies),
		"Strategies.jsx must not carry its own copy of statusLabel",
	);
	assert.ok(
		!/^function statusTitle\(/m.test(strategies),
		"Strategies.jsx must not carry its own copy of statusTitle",
	);
	assert.ok(
		!/statusTag\(s\.status, s\.passes_rigor_gate\)/.test(strategies),
		"no two-argument call may remain — it would silently drop the four-state",
	);
});

test("the passport renders the Library's pill, not a private two-argument copy", () => {
	// THE GAP THIS CLOSES. Importing only GATE_FAILED_LABEL — which is all the
	// passport used to do — bought byte-identical WORDS while leaving the two
	// surfaces two different DECISIONS about when to say them. The passport's
	// local pair took `(status, passesRigor)` and never saw `rigor_gate_status`,
	// so once the Library moved to the four-state the passport was the last
	// place the fail-closed boolean could still stand in for the verdict: a
	// live row with a PENDING or DEGENERATE gate has `passes_rigor_gate ===
	// false`, so its pill read "Reference only — gate failed" beside this same
	// header's own chip reading "rigor gate pending". One vocabulary, one
	// module.
	//
	// MUTATION: restore `function statusTag(status, passesRigor)` (or
	// statusLabel) in StrategyPassport.jsx and call it with two arguments —
	// every assert below reddens.
	assert.ok(
		passport.includes(
			'import { statusTag, statusLabel, statusTitle } from "../libraryStatus.js"',
		),
		"StrategyPassport.jsx must take all three pill helpers from the shared module",
	);
	for (const fn of ["statusTag", "statusLabel", "statusTitle"]) {
		assert.ok(
			!new RegExp("^function " + fn + "\\(", "m").test(passport),
			`StrategyPassport.jsx must not carry its own copy of ${fn}`,
		);
	}
	assert.ok(
		!/statusTag\(s\.status, s\.passes_rigor_gate\)/.test(passport),
		"no two-argument call may remain — it would silently drop the four-state",
	);
	assert.ok(
		!/statusLabel\(s\.status, s\.passes_rigor_gate\)/.test(passport),
		"no two-argument call may remain — it would silently drop the four-state",
	);
	// Same discipline as the Library: class, words and tooltip derived from one
	// row's one verdict, so a reviewer sees ONE argument list, not three.
	const args = "\\(s\\.status, s\\.passes_rigor_gate, s\\.rigor_gate_status\\)";
	for (const fn of ["statusTag", "statusLabel", "statusTitle"]) {
		assert.equal(
			passport.match(new RegExp(fn + args, "g"))?.length,
			1,
			`${fn} must be called with the full four-state on the passport pill`,
		);
	}
	// The demotion literal may still appear in PROSE explaining the defect —
	// what must never come back is a live copy of it. (Same comment-aware scan
	// this file already uses for the retired `pending_backtest` status.)
	for (const line of passport.split("\n")) {
		const code = line.trim();
		if (code.startsWith("//") || code.startsWith("*")) continue;
		assert.ok(
			!code.includes("Reference only"),
			`StrategyPassport.jsx has a live copy of the demotion label: ${code}`,
		);
	}
});

test("a live passport row whose gate has not graded says pending, never failed", () => {
	// The exact served shape behind the bug: `rigor_gate_status` is the verdict
	// and `passes_rigor_gate` is false for BOTH ungraded states, so the tuple
	// the old two-argument passport helper saw — ("live", false) — is
	// indistinguishable from a real failure. The four-state tells them apart.
	// MUTATION: drop the third argument at the passport call site (the helper
	// then sees rigorGateStatus === undefined, isUngraded falls through to
	// `passesRigor == null` → false, and the fail arm answers) — these redden.
	assert.equal(statusLabel("live", false, "pending"), NOT_GRADED_LABEL);
	assert.equal(statusTag("live", false, "pending"), "tag-muted");
	assert.equal(statusTitle("live", false, "pending"), NOT_GRADED_TITLE);
	assert.notEqual(statusLabel("live", false, "pending"), GATE_FAILED_LABEL);

	assert.equal(statusLabel("live", false, "degenerate"), DEGENERATE_LABEL);
	assert.equal(statusTitle("live", false, "degenerate"), DEGENERATE_TITLE);
	assert.notEqual(statusLabel("live", false, "degenerate"), GATE_FAILED_LABEL);

	// And the pill still agrees with the rigor chip rendered beside it: the
	// chip's pending/degenerate arms key on the same field the pill now reads.
	assert.ok(
		passport.includes('const rigorPending = s.rigor_gate_status === "pending"'),
	);
	assert.ok(
		passport.includes(
			'const rigorDegenerate = s.rigor_gate_status === "degenerate"',
		),
	);
});

test("the passport's selection-bias comment no longer claims a 404 is expected", () => {
	// The comment said "404 (generated strategy not in the curated cohort) is
	// expected — we fall back to the badge boolean below". That stopped being
	// true when `_generated_strategy_rigor` landed: a generated id now gets a
	// 200 with a live ladder. A comment that describes a seam wrongly is how the
	// seam stays invisible.
	const staleClaim = "is expected — we fall back to the badge boolean below";
	const staleIdx = passport.indexOf(staleClaim);
	if (staleIdx !== -1) {
		// It may appear ONLY as a quotation of what the comment used to say,
		// introduced as such — never as a live claim.
		const quoteIntro = passport.indexOf("The old comment here said");
		assert.ok(
			quoteIntro !== -1 && quoteIntro < staleIdx,
			"the stale claim may only appear quoted as history, never asserted",
		);
		assert.ok(passport.includes("stopped being true"));
	}
	assert.ok(
		passport.includes("the DEPLOY ladder"),
		"the comment must say what the call actually is",
	);
	assert.ok(
		passport.includes("badge is the verdict, the ladder is the deploy check"),
		"the comment must name the seam between the stored badge and the live ladder",
	);
});
