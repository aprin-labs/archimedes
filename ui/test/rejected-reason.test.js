import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	barName,
	checkLine,
	failedChecks,
	hasRejectionDetail,
	isUnattributed,
	notComputedChecks,
	passedChecks,
	recordedReason,
	rejectedSectionSummary,
	showsRejectionReasons,
} from "../src/rejectionReasons.js";

// The owner's screenshot: the Library's "Rejected (1) — did not pass the rigor
// gate" card showed "—" for Sharpe / CAGR / Max DD, a "Gen tokens" count, and
// one shared paragraph asserting that "most rejections at this stage are
// 'return series too short' … A longer backtest window typically unlocks them."
// Nothing measured that sentence, and the card carried no per-strategy reason at
// all — so for a candidate rejected for a different reason the page stated
// something false about it.
//
// Behaviour lives in ../src/rejectionReasons.js (a plain module, importable
// here — .jsx is not), so these are real assertions on the rendered strings,
// not only regex pins on source. The source pins at the bottom cover the two
// wiring facts a pure module cannot: that the component is mounted on the card,
// and that the deleted paragraph did not come back.

const src = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");
const strategies = src("components/Strategies.jsx");

// A row rejected on the numbers — the gate graded it.
const graded = {
	id: "graded",
	passes_rigor_gate: false,
	rigor_reasons: {
		bar: "Archimedes Verified",
		bar_level: 1,
		passing: false,
		recorded_reason: null,
		reason_code: null,
		min_returns_for_gate: 10,
		unattributed: false,
		checks: [
			{ key: "dsr", label: "DSR confidence", status: "fail", detail: "0.08 < 0.90 required", value: 0.08, threshold: 0.9 },
			{ key: "pbo", label: "PBO (overfitting probability)", status: "fail", detail: "0.62 ≥ 0.50 — at or above the overfitting ceiling", value: 0.62, threshold: 0.5 },
			{ key: "oos_sharpe", label: "Out-of-sample Sharpe", status: "pass", detail: "0.41 > 0.00 required", value: 0.41, threshold: 0 },
			{ key: "look_ahead", label: "Look-ahead audit", status: "pass", detail: "no forward-looking data access found", value: null, threshold: null },
		],
	},
};

// A row the gate never graded — the branch the deleted paragraph claimed was
// "most" of them.
const tooShort = {
	id: "short",
	passes_rigor_gate: false,
	rigor_reasons: {
		bar: "Archimedes Verified",
		bar_level: 1,
		passing: false,
		recorded_reason: "return series too short for rigor evaluation",
		reason_code: "short_return_series",
		min_returns_for_gate: 10,
		unattributed: false,
		checks: [
			{ key: "dsr", label: "DSR confidence", status: "not_computed", detail: "no deflated-Sharpe p-value on record", value: null, threshold: 0.9 },
			{ key: "look_ahead", label: "Look-ahead audit", status: "not_computed", detail: "not run — see the reason on record", value: null, threshold: null },
		],
	},
};

// ── The card states THIS strategy's reason ───────────────────────────────

test("a graded rejection renders the checks that failed, with the gate's thresholds", () => {
	const failed = failedChecks(graded).map(checkLine);
	assert.equal(failed.length, 2);
	assert.match(failed[0], /DSR confidence — 0\.08 < 0\.90 required/);
	assert.match(failed[1], /PBO .* 0\.62 ≥ 0\.50/);
	// Passed checks are shown as passed — the block is not a list of grievances.
	assert.deepEqual(
		passedChecks(graded).map((c) => c.label),
		["Out-of-sample Sharpe", "Look-ahead audit"],
	);
	assert.equal(recordedReason(graded), null);
	assert.equal(barName(graded), "Archimedes Verified");
});

test("a row the gate never graded surfaces its own recorded reason, and claims no failures", () => {
	assert.equal(recordedReason(tooShort), "return series too short for rigor evaluation");
	// A check that never ran must not be rendered as one that ran and failed.
	assert.deepEqual(failedChecks(tooShort), []);
	assert.equal(notComputedChecks(tooShort).length, 2);
});

test("two rows rejected for different reasons never render the same text", () => {
	// THE defect: one paragraph cannot be true of both of these rows.
	const gradedText = [recordedReason(graded), ...failedChecks(graded).map(checkLine)].join(" ");
	const shortText = [recordedReason(tooShort), ...failedChecks(tooShort).map(checkLine)].join(" ");
	assert.notEqual(gradedText, shortText);
	assert.doesNotMatch(gradedText, /return series too short/);
	assert.doesNotMatch(shortText, /0\.08 < 0\.90/);
});

// ── Fail-closed: no field, no claim ──────────────────────────────────────

test("a row without the rigor_reasons field renders no block at all", () => {
	// The drop-the-field mutation. Every list is empty and the block's own gate
	// is false, so the card shows nothing rather than an empty heading or a
	// reason it cannot support.
	for (const base of [{}, null, undefined, { rigor_reasons: null }, { rigor_reasons: { checks: "nope" } }]) {
		const row = base == null ? base : { ...base, passes_rigor_gate: false };
		assert.equal(hasRejectionDetail(row), false);
		assert.equal(showsRejectionReasons(row), false);
		assert.deepEqual(failedChecks(row), []);
		assert.deepEqual(notComputedChecks(row), []);
		assert.deepEqual(passedChecks(row), []);
		assert.equal(recordedReason(row), null);
		assert.equal(barName(row), null);
	}
	assert.equal(hasRejectionDetail(graded), true);
	assert.equal(showsRejectionReasons(graded), true);
});

test("a row still awaiting a verdict is never handed one", () => {
	// passes_rigor_gate === null is "not scored yet". Listing the checks it has
	// not been graded against would read as a verdict it has not received.
	assert.equal(showsRejectionReasons({ ...graded, passes_rigor_gate: null }), false);
	assert.equal(showsRejectionReasons({ ...graded, passes_rigor_gate: undefined }), false);
	assert.equal(showsRejectionReasons({ ...graded, passes_rigor_gate: true }), false);
});

test("a rejection nothing on record explains says so instead of naming a culprit", () => {
	const unattributed = {
		rigor_reasons: { bar: "Archimedes Verified", passing: false, recorded_reason: null, reason_code: null, unattributed: true, checks: [{ key: "dsr", label: "DSR confidence", status: "pass", detail: "0.92 ≥ 0.90 required", value: 0.92, threshold: 0.9 }] },
	};
	assert.equal(isUnattributed(unattributed), true);
	assert.deepEqual(failedChecks(unattributed), []);
	assert.equal(isUnattributed(graded), false);
});

// ── The section sentence is shown only when it is true ───────────────────

test("the short-series sentence appears only for rows that actually carry that reason", () => {
	// The mutation the old paragraph WAS: asserting the short-series reason over
	// a population that does not have it.
	assert.equal(rejectedSectionSummary([graded]), null);
	assert.equal(rejectedSectionSummary([graded, graded]), null);
	assert.equal(rejectedSectionSummary([]), null);
	assert.equal(rejectedSectionSummary(null), null);
});

test("the short-series sentence counts the rows it is true of", () => {
	const all = rejectedSectionSummary([tooShort, tooShort]);
	assert.match(all, /All 2 candidates below/);
	assert.match(all, /at least 10 daily returns/); // the number comes from the payload

	const one = rejectedSectionSummary([tooShort]);
	assert.match(one, /The candidate below/);

	const mixed = rejectedSectionSummary([tooShort, graded, graded]);
	assert.match(mixed, /1 of these 3 candidates/);
	assert.match(mixed, /The rest failed other checks/);
	assert.doesNotMatch(mixed, /All 3/);
});

test("no minimum-observation count in the payload means no sentence", () => {
	// The count is quoted from the backend, never typed into the frontend, so a
	// payload without it must not produce a number.
	const noCount = { rigor_reasons: { ...tooShort.rigor_reasons, min_returns_for_gate: null } };
	assert.equal(rejectedSectionSummary([noCount]), null);
});

// ── Wiring: the block is mounted, and the old paragraph is gone ──────────

test("Strategies.jsx mounts the reasons block on the card and in the expanded row", () => {
	assert.match(strategies, /function RejectionReasons\(\{ s \}\) \{/);
	// The mobile card (the surface in the owner's screenshot) renders it
	// un-collapsed, right below the "—" stat tiles.
	assert.match(strategies, /<RejectionReasons s=\{s\} \/>\n\s*\{open && \(/);
	// The shared detail panel renders it for the desktop table row, and the card
	// suppresses that copy so it is not shown twice.
	assert.match(strategies, /\{!hideRejectionReasons && <RejectionReasons s=\{s\} \/>\}/);
	assert.match(strategies, /hideRejectionReasons\n/);
});

test("coerceGenerated carries the per-row reasons through to the card", () => {
	assert.match(strategies, /rigor_reasons: row\.rigor_reasons \?\? null,/);
});

test("the population-wide rejection prose is gone from Strategies.jsx", () => {
	// The exact sentences from the owner's screenshot. Each asserted something
	// about every rejected strategy that nothing had measured.
	assert.doesNotMatch(strategies, /Most rejections at this stage are/);
	assert.doesNotMatch(strategies, /A longer backtest window typically unlocks them/);
	assert.doesNotMatch(strategies, /most are <code>"return series too short"<\/code>/);
	// And the replacement is the measured, conditional one.
	assert.match(strategies, /\{rejectedSectionSummary\(rejected\) && \(/);
});
