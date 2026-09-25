// Claim-integrity guard for the trace anchor-state copy (#714).
//
// #714 retired the v1 `publishTrace` fallbacks from the agent tick. The
// SKIP/error path (`_publish_trace`) now makes no chain call at all and
// persists `arc_tx_hash: None` / `is_verified: False` permanently, by design:
// with no trade there is no tradeId for `commit()` to bind, so no registry
// write is ever attempted for a skip trace.
//
// Portfolio.jsx rendered every `is_verified === false` trace as "anchor
// pending — registry write didn't complete yet — usually transient". For a
// skip that is now false in both halves: nothing is pending, and nothing is
// transient. The agent ticks emit skips continuously while pools are thin, so
// this is the *common* row, not an edge case — the flagship portfolio page
// would promise an anchor that is never coming.
//
// WHAT MOVED, AND WHY THIS FILE CHANGED SHAPE.
// #714 landed the fix as an inline ternary inside Portfolio.jsx, and this file
// pinned it by slicing that ternary out of the source text. The derivation has
// since moved into `src/trace-binding.js` (`anchorState`), because THREE
// surfaces render this claim — Portfolio's activity feed, the Reasoning page,
// and the strategy passport's decisions panel — and two of them had drifted
// into disagreeing about it. The claims below are unchanged and none were
// relaxed; they are now asserted against the derivation every surface shares,
// plus a source pin that Portfolio.jsx still routes through it. A copy rule
// pinned in only one of three renderers is how the drift happened in the first
// place.
//
// Anti-vacuity is preserved in the same form: every predicate is re-run
// against the pre-#714 two-state rule, which it must reject.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { anchorState } from "../src/trace-binding.js";

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

const portfolio = readFileSync(repoFile("src/components/Portfolio.jsx"), "utf8");

//: The exact rule Portfolio.jsx carried BEFORE #714: two states, discriminated
//: on `is_verified` alone, so every unverified trace — skips included — read as
//: "anchor pending". Every assertion that passes against `anchorState` is
//: re-run against this to prove it discriminates.
const PRE_714_ANCHOR_STATE = (trace) =>
	trace.is_verified
		? {
				state: "anchored",
				label: "anchored on Arc",
				icon: "i-lucide-check-circle",
				tone: "verified",
				title: "",
			}
		: {
				state: "anchor_pending",
				label: "anchor pending",
				icon: "i-lucide-clock",
				tone: "pending",
				title:
					"Trace hashed + persisted off-chain; on-chain anchor pending (registry write didn't complete yet — usually transient).",
			};

const SKIP = { decision_type: "skip", is_verified: false, arc_tx_hash: null };

// True when a skip trace is peeled off BEFORE the pending fallback — i.e. the
// pending copy is unreachable for `decision_type === "skip"`.
function skipBranchedBeforePending(derive) {
	return derive(SKIP).label !== "anchor pending";
}

test("a skip trace never falls through to the 'anchor pending' copy", () => {
	assert.ok(
		skipBranchedBeforePending(anchorState),
		"the derivation must peel off decision_type === 'skip' before the pending fallback",
	);
});

test("the skip branch states the honest reason, not a failure or a pending write", () => {
	const a = anchorState(SKIP);
	assert.equal(
		a.label,
		"not anchored (no trade to bind)",
		"the skip state must name the real reason: there is no trade for a commitment to bind",
	);
	// The reason must survive a hover, not just the label: the title is the only
	// place with room to say *why* nothing is anchored.
	assert.match(
		a.title,
		/^No trade was made[^"]*no anchor is attempted or pending\.$/,
		"the skip state's title must say no anchor is attempted or pending",
	);
});

test("the skip branch borrows neither the verified affordance nor the pending one", () => {
	const a = anchorState(SKIP);

	// Not a success: no green check / positive styling for a trace with no anchor.
	assert.notEqual(
		a.icon,
		"i-lucide-check-circle",
		"the skip state must not reuse the anchored-on-Arc check icon",
	);
	assert.notEqual(
		a.tone,
		"verified",
		"the skip state must not reuse the anchored-on-Arc positive styling",
	);
	// `tone` is what Portfolio.jsx and Reasoning.jsx map to `--positive`, so the
	// styling claim is the tone claim — pin the mapping too, or a future
	// renderer could paint a non-verified tone green and this would not notice.
	assert.match(
		portfolio,
		/a\.tone === "verified" \? "text-\[var\(--positive\)\]" : "text-\[var\(--text-3\)\]"/,
		"Portfolio.jsx must reserve the positive colour for the verified tone",
	);

	// Not a failure and not a wait: no clock, and none of the pending state's
	// affirmative claims about a write that is still coming.
	assert.notEqual(
		a.icon,
		"i-lucide-clock",
		"the skip state must not reuse the pending clock icon — nothing is being waited on",
	);
	assert.ok(!a.label.includes("anchor pending"), "the skip state must not carry the pending label");
	assert.ok(
		!/registry write didn't complete|\btransient\b|\bfailed\b/i.test(a.title),
		"the skip state must not imply an incomplete or failed registry write",
	);
});

test("the pending copy survives for the states it is still true of (anti-goal)", () => {
	// #714 did not make "anchor pending" false everywhere. `is_verified` is
	// `reveal_tx is not None` (agent_runner._reveal_trace), so a rebalance that
	// committed but whose reveal has not landed persists unverified with a real
	// commit_tx_hash — genuinely pending, and #1276's reveal reconciliation
	// retries it on a later tick. Widening this fix past `decision_type ===
	// "skip"` would deny an anchor that really is still coming.
	const pending = anchorState({ decision_type: "rebalance", is_verified: false, arc_tx_hash: null });
	assert.equal(
		pending.label,
		"anchor pending",
		"the pending copy must remain for non-skip traces that really are awaiting a write",
	);
	assert.equal(
		pending.icon,
		"i-lucide-clock",
		"the pending clock affordance must remain for the states it still describes",
	);
	assert.equal(
		pending.title,
		"Trace hashed + persisted off-chain; on-chain anchor pending (registry write didn't complete yet — usually transient).",
		"the pending tooltip must survive the move into the shared helper verbatim",
	);
});

test("Portfolio.jsx renders this derivation rather than its own copy of it", () => {
	// The source pin that keeps the behavioural tests above pointed at the
	// flagship page. Without it, the derivation could be correct while
	// Portfolio.jsx quietly kept an inline ternary.
	assert.ok(
		portfolio.includes('import { anchorState } from "../trace-binding"'),
		"Portfolio.jsx must import the shared derivation",
	);
	assert.ok(portfolio.includes("anchorState(t)"), "Portfolio.jsx must call the shared derivation");
	assert.ok(
		!portfolio.includes("{t.is_verified ? ("),
		"Portfolio.jsx still discriminates the anchor state with its own is_verified ternary",
	);
	assert.ok(
		!/title="Trace hashed \+ persisted off-chain/.test(portfolio),
		"the pending tooltip is inline again — the two copies can now disagree",
	);
	// The icon sizing #714 shipped is part of the rendered claim.
	assert.ok(
		portfolio.includes("${a.icon} w-3.5 h-3.5"),
		"Portfolio.jsx must keep the 3.5 icon sizing for the anchor badge",
	);
});

// ── Anti-vacuity ─────────────────────────────────────────────────────────
//
// The pin above is only worth anything if it rejects the code it was written
// against. Run the same predicates over the pre-#714 two-state rule.

test("the pin rejects the pre-#714 rule, where every unverified trace read as pending", () => {
	assert.equal(
		skipBranchedBeforePending(PRE_714_ANCHOR_STATE),
		false,
		"the pre-#714 rule routed skips straight to 'anchor pending' — the pin must reject it",
	);

	const before = PRE_714_ANCHOR_STATE(SKIP);
	assert.notEqual(
		before.label,
		"not anchored (no trade to bind)",
		"the honest skip copy did not exist before #714 — the pin must not match it vacuously",
	);
	assert.doesNotMatch(before.title, /^No trade was made[^"]*no anchor is attempted or pending\.$/);
	assert.equal(before.icon, "i-lucide-clock");
	assert.ok(/registry write didn't complete|\btransient\b/i.test(before.title));
});
