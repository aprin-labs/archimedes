import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { driftTooltip, formatTotalReturn, paperErrorMessage } from "../src/paperCopy.js";

// #1362 — two false statements on /app/paper, both the exact shape CLAUDE.md
// § fail-soft names: a state the system does not have (a drift "freeze"),
// and an absence rendered as a plausible measured number (day-0 "+0.00%").
// Every assertion below was confirmed to FAIL against the pre-fix tree —
// see the mutation-check transcripts in the PR body.

// ── driftTooltip: the DRIFT chip's tooltip must state what actually
// happens (append-only, never rewritten, keeps advancing) and must never
// promise a halt/investigation that no code path enforces, nor leak the
// raw machine timestamp into English prose. ────────────────────────────────

test("driftTooltip: never claims a freeze or a pending investigation", () => {
	const tooltip = driftTooltip("2026-08-20T04:12:33.481207+00:00", "active");
	assert.doesNotMatch(tooltip, /frozen|pending investigation/);
});

test("driftTooltip: states the true, honest consequence — append-only, not rewritten", () => {
	const tooltip = driftTooltip("2026-08-20T04:12:33.481207+00:00", "active");
	assert.match(tooltip, /append-only/);
	assert.match(tooltip, /not rewritten/);
});

test("driftTooltip: never interpolates the raw ISO machine timestamp", () => {
	const tooltip = driftTooltip("2026-08-20T04:12:33.481207+00:00", "active");
	assert.doesNotMatch(tooltip, /T04:12:33/);
	assert.doesNotMatch(tooltip, /\d{4}-\d{2}-\d{2}T/); // no raw ISO date at all
});

test("driftTooltip: a malformed timestamp degrades cleanly, never 'Invalid Date' or a crash", () => {
	assert.doesNotThrow(() => driftTooltip("not-a-real-timestamp", "active"));
	assert.doesNotMatch(driftTooltip("not-a-real-timestamp", "active"), /Invalid Date/);
});

// A STOPPED deployment does not advance — advance_all (paper_trading.py)
// filters on STATUS_ACTIVE, and drift_detected_at is never cleared, so the
// same DRIFT chip can still be showing on a stopped row. "Keeps advancing"
// must be conditioned on status, not asserted unconditionally — that is
// just the false claim this issue exists to remove, on a different clause.
test("driftTooltip: claims the track record keeps advancing only when active", () => {
	assert.match(driftTooltip("2026-08-20T04:12:33.481207+00:00", "active"), /keeps advancing/);
});

test("driftTooltip: a stopped deployment never claims the record keeps advancing", () => {
	const tooltip = driftTooltip("2026-08-20T04:12:33.481207+00:00", "stopped");
	assert.doesNotMatch(tooltip, /keeps advancing/);
	assert.doesNotMatch(tooltip, /frozen|pending investigation/);
	assert.match(tooltip, /append-only/);
});

// The stopped-branch "no further rows" clause must be anchored to the stop
// itself, not left ambiguous against the drift date named earlier in the
// same sentence — rows genuinely can be appended between a drift stamp and
// a later Stop (advance_all keeps appending until STATUS_STOPPED; the drift
// stamp only refreshes when the disagreement recurs). "Since the deployment
// was stopped" is true under both readings; "since <the drift date>" is not.
test("driftTooltip: the stopped-branch 'no further rows' clause is anchored to the stop, not the drift date", () => {
	const tooltip = driftTooltip("2026-08-20T04:12:33.481207+00:00", "stopped");
	assert.match(tooltip, /no rows have been added since the deployment was stopped/i);
});

test("paperErrorMessage: a 404 does not claim causes the API cannot produce (stop is idempotent; there is no delete route)", () => {
	const err = { status: 404, message: "Backend returned 404" };
	assert.doesNotMatch(paperErrorMessage(err), /stopped or removed/i);
});

// ── formatTotalReturn: gate on `days`, never on the value. A day-0 ledger
// (zero rows — the normal state right after deploy) must render as
// unmeasured; a genuinely measured zero at day N must still print. ────────

test("formatTotalReturn: day 0 renders unmeasured, even though the API's total_return is a real 0.0", () => {
	assert.equal(formatTotalReturn(0.0, 0), "—");
});

test("formatTotalReturn: a measured zero at day N still prints — the gate is days, not the value", () => {
	assert.equal(formatTotalReturn(0.0, 3), "+0.00%");
});

test("formatTotalReturn: signed percentages for a genuinely measured return", () => {
	assert.equal(formatTotalReturn(-0.0123, 5), "-1.23%");
	assert.equal(formatTotalReturn(0.0456, 10), "+4.56%");
});

test("formatTotalReturn: null/NaN also degrade to unmeasured regardless of days", () => {
	assert.equal(formatTotalReturn(null, 5), "—");
	assert.equal(formatTotalReturn(NaN, 5), "—");
});

// ── paperErrorMessage: api.js's raw "Backend returned NNN" must never
// reach the role="alert" card. Mirrors StrategyPassport.jsx's
// PaperDeployCard's start() catch block status mapping (:906-919). ─────────

test("paperErrorMessage: a 401 never surfaces the raw 'Backend returned' string, and reads as a sign-in prompt", () => {
	const err = { status: 401, message: "Backend returned 401" };
	assert.doesNotMatch(paperErrorMessage(err), /Backend returned/);
	assert.match(paperErrorMessage(err), /sign in/i);
});

test("paperErrorMessage: a 404 (stop on an already-gone deployment) never surfaces 'Backend returned' either", () => {
	const err = { status: 404, message: "Backend returned 404" };
	assert.doesNotMatch(paperErrorMessage(err), /Backend returned/);
});

test("paperErrorMessage: any other status also never leaks the raw string, and falls to a generic sentence", () => {
	const err = { status: 500, message: "Backend returned 500" };
	assert.doesNotMatch(paperErrorMessage(err), /Backend returned/);
	assert.ok(paperErrorMessage(err).length > 0);
});

test("paperErrorMessage: a status-less error (e.g. a network failure) falls back to its own message, then the caller's fallback", () => {
	assert.equal(paperErrorMessage(new Error("Failed to fetch")), "Failed to fetch");
	assert.equal(paperErrorMessage({}, "Failed to load paper deployments"), "Failed to load paper deployments");
	assert.equal(paperErrorMessage(null, "Failed to load paper deployments"), "Failed to load paper deployments");
});

// ── Wiring: PaperTrading.jsx actually calls these helpers at every site the
// issue named, not a re-implemented fork — and the dead local `pct` is
// gone (the now-unused-import ESLint would otherwise catch). Same
// source-regex pattern ui/test/a11y.test.js and
// ui/test/generate-quote.test.js already use for this file/kind of check. ──

const paperTrading = readFileSync(
	new URL("../src/components/PaperTrading.jsx", import.meta.url),
	"utf8",
);

test("PaperTrading.jsx imports the shared copy/format helpers from ../paperCopy", () => {
	// Matched per-name against the single ../paperCopy import block rather than
	// as one literal line: the list grew when intraday marks landed, and a
	// whole-line regex would have to be rewritten (and could be quietly
	// loosened) every time it grows again. What this guards is unchanged —
	// every copy helper the component renders comes from the tested module,
	// not a local re-implementation.
	const block = paperTrading.match(/import \{([^}]*)\} from ['"]\.\.\/paperCopy['"]/);
	assert.ok(block, "expected a named import block from ../paperCopy");
	const imported = new Set(block[1].split(",").map((s) => s.trim()).filter(Boolean));
	for (const name of [
		"driftTooltip",
		"formatTotalReturn",
		"paperErrorMessage",
		"markLabel",
		"markAnnouncement",
		"marksStalenessNote",
		"noMarksNote",
		"marksUnavailableNote",
		"markBasisNote",
		"MARK_BASIS_DISCLOSURE",
	]) {
		assert.ok(imported.has(name), `expected ${name} to be imported from ../paperCopy`);
	}
});

test("PaperTrading.jsx's DRIFT tooltip calls driftTooltip(driftAt, status), not an inline literal", () => {
	assert.match(paperTrading, /title=\{driftTooltip\(driftAt,\s*status\)\}/);
});

test("PaperTrading.jsx's headline figure calls formatTotalReturn(dep.total_return, dep.days) — days is part of the call", () => {
	assert.match(paperTrading, /formatTotalReturn\(dep\.total_return,\s*dep\.days\)/);
});

test("PaperTrading.jsx routes both catch blocks through paperErrorMessage, never bare e.message", () => {
	assert.doesNotMatch(paperTrading, /setError\(e\.message/);
	const calls = paperTrading.match(/setError\(paperErrorMessage\(e,/g) || [];
	assert.equal(calls.length, 2, "expected paperErrorMessage(e, ...) at both the load() and stop() catch blocks");
});

test("the dead local pct() formatter is gone, not just unused", () => {
	assert.doesNotMatch(paperTrading, /function pct\(/);
});
