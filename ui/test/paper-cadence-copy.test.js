import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	PAPER_INTRADAY_CADENCE,
	PAPER_SETTLE_CADENCE,
	newestMark,
	noMarksNote,
	paperCadenceCopy,
} from "../src/paperCopy.js";

// #1802 PR 0. The Paper Trading intro told every reader, unconditionally, that
// the live value "re-prices the strategy's asset basket every 15 minutes".
// The marks job exists in the backend (services/paper_marks.py,
// backend/archimedes/scripts/run_paper_marks.py) but nothing under infra/
// schedules it, so in
// production no marks are written — the sentence was a promise about a job
// that does not run. The DAILY settle is the graded truth
// (paper_trading.py's advance loop, PAPER_ADVANCE_INTERVAL_HOURS default 24),
// so that sentence is unconditional and the intraday one must be earned by a
// mark actually present in the payload.

const FRESH = { portfolio_value: 1.0042, ts: "2026-08-30T14:45:00Z", is_delayed: false };
const NOW = Date.parse("2026-08-30T14:50:00Z");
// Two cadence intervals after the mark — marksStalenessNote's existing rule,
// reused rather than re-invented.
const LATER = Date.parse("2026-08-30T15:45:00Z");

// ── paperCadenceCopy: three states ──────────────────────────────────────────

test("no mark: the daily-settle sentence ONLY — no cadence is claimed", () => {
	for (const absent of [null, undefined]) {
		const copy = paperCadenceCopy(absent, NOW);
		assert.deepEqual(copy.sentences, [PAPER_SETTLE_CADENCE]);
		assert.equal(copy.intraday, false);
		assert.equal(copy.staleness, null);
		// The load-bearing negative: with no marks job deployed, this is the
		// state every production reader is in today.
		assert.doesNotMatch(copy.sentences.join(" "), /15 minutes/);
	}
});

test("a mark with no usable value or timestamp is no mark at all", () => {
	// Same gate markLabel already uses: half a claim is worse than none, and it
	// certainly cannot earn a cadence sentence.
	for (const broken of [{ ...FRESH, portfolio_value: null }, { ...FRESH, ts: "not-a-date" }]) {
		assert.equal(paperCadenceCopy(broken, NOW).intraday, false);
	}
});

test("fresh mark: the intraday sentence appears, after the settle sentence", () => {
	const copy = paperCadenceCopy(FRESH, NOW);
	assert.deepEqual(copy.sentences, [PAPER_SETTLE_CADENCE, PAPER_INTRADAY_CADENCE]);
	assert.equal(copy.intraday, true);
	assert.equal(copy.staleness, null);
	assert.match(copy.sentences.join(" "), /every 15 minutes/);
});

test("stale mark: the staleness note, and NOT the cadence sentence", () => {
	// A mark that stopped arriving is the exact case where "every 15 minutes"
	// is false — so the page states the observed age instead.
	const copy = paperCadenceCopy(FRESH, LATER);
	assert.deepEqual(copy.sentences, [PAPER_SETTLE_CADENCE]);
	assert.equal(copy.intraday, false);
	assert.match(copy.staleness, /Last marked Sun 14:45 UTC/);
	assert.doesNotMatch(copy.sentences.join(" "), /15 minutes/);
});

test("the staleness threshold is marksStalenessNote's, not a new number", () => {
	// One missed tick is a hiccup and stays fresh; two is the state worth
	// naming. Reusing the same intervalMinutes keeps the intro and the per-card
	// line from disagreeing about the same mark.
	const oneTick = Date.parse("2026-08-30T15:05:00Z"); // 20 min < 2 x 15
	assert.equal(paperCadenceCopy(FRESH, oneTick).intraday, true);
	// The same 60-minute-old mark is fresh again at a 45-minute cadence.
	assert.equal(paperCadenceCopy(FRESH, LATER, 45).intraday, true); // 60 min < 2 x 45
});

test("the settle sentence states what actually runs, and never a 15-minute cadence", () => {
	assert.match(PAPER_SETTLE_CADENCE, /once per trading day/i);
	assert.match(PAPER_SETTLE_CADENCE, /unsettled/i);
	assert.doesNotMatch(PAPER_SETTLE_CADENCE, /15 minutes/);
});

test("the settle sentence names what the series is today, not a cancelled cutover", () => {
	// #1822 retracted "the track record that carries to mainnet" from every
	// paper-trading copy surface: the cutover was cancelled by owner call (#1240)
	// and no date is scheduled, so the promise is about an unscheduled event.
	// This constant is one of those surfaces — it is the sentence PaperTrading.jsx
	// used to hold inline — so it must say what the settled series IS.
	//
	// ui/test/no-mainnet-track-record.test.js enforces the same property across
	// all of ui/src by scanning source text. It is pinned again here, on the
	// exported VALUE, because that scan reads the file and this one reads the
	// string: a rewrap of the `' + '` concatenation, or a move of the sentence to
	// another module, changes what the scanner sees and changes nothing here.
	assert.match(PAPER_SETTLE_CADENCE, /paper track record on Arc testnet/);
	assert.match(PAPER_SETTLE_CADENCE, /no real funds/);
	assert.doesNotMatch(PAPER_SETTLE_CADENCE, /mainnet/i);
});

// ── newestMark: the input the gate is computed from ─────────────────────────

test("newestMark: nothing to show when no deployment carries a mark", () => {
	assert.equal(newestMark(null), null);
	assert.equal(newestMark([], {}), null);
	assert.equal(newestMark([{ deployment_id: "d1" }], {}), null);
	assert.equal(newestMark([{ deployment_id: "d1", latest_mark: null }], { d1: [] }), null);
});

test("newestMark: falls back to the summary's latest_mark, and prefers the polled list", () => {
	// Mirrors LiveValue's own precedence, so the intro cannot claim a cadence
	// the cards below it are not showing.
	const summaryOnly = [{ deployment_id: "d1", latest_mark: FRESH }];
	assert.equal(newestMark(summaryOnly, {}), FRESH);
	const polled = { portfolio_value: 1.01, ts: "2026-08-30T15:00:00Z" };
	assert.equal(newestMark(summaryOnly, { d1: [FRESH, polled] }), polled);
});

test("newestMark ignores a deployment whose marks fetch failed", () => {
	// LiveValue checks `error` BEFORE it checks for marks, so a failed fetch
	// renders "Live value unavailable" with no number at all. If newestMark
	// still fell back to the summary's latest_mark, a total marks outage would
	// leave the intro as the only line on the page still claiming a cadence.
	const deps = [{ deployment_id: "d1", latest_mark: FRESH }];
	assert.equal(newestMark(deps, {}, { d1: "Live value unavailable — the marks feed did not respond." }), null);
	assert.equal(paperCadenceCopy(newestMark(deps, {}, { d1: "x" }), NOW).intraday, false);
	// Absent an error it is unchanged — the skip is scoped to the failure.
	assert.equal(newestMark(deps, {}, {}), FRESH);
});

test("noMarksNote does not promise a tick no deployed job produces", () => {
	// The same claim the intro dropped survived on every card: with no marks
	// job scheduled, this string renders under EVERY active deployment.
	assert.doesNotMatch(noMarksNote("active"), /15-minute|15 minutes|next tick/);
	assert.doesNotMatch(noMarksNote("stopped"), /15-minute|15 minutes/);
});

test("newestMark: picks the newest across deployments and skips unusable timestamps", () => {
	const older = { portfolio_value: 1.0, ts: "2026-08-30T13:00:00Z" };
	const broken = { portfolio_value: 1.0, ts: "not-a-date" };
	const deps = [
		{ deployment_id: "d1", latest_mark: older },
		{ deployment_id: "d2", latest_mark: FRESH },
		{ deployment_id: "d3", latest_mark: broken },
	];
	assert.equal(newestMark(deps, {}), FRESH);
});

// ── Wiring: the unconditional claim is gone from the page ───────────────────

const paperTrading = readFileSync(new URL("../src/components/PaperTrading.jsx", import.meta.url), "utf8");

test("PaperTrading.jsx no longer states the 15-minute cadence as page prose", () => {
	// The mutation this guard exists for: putting the sentence back into the
	// intro paragraph, where it is asserted to every reader regardless of
	// whether a single mark has ever been written.
	assert.doesNotMatch(paperTrading, /every 15 minutes/);
	assert.doesNotMatch(paperTrading, /re-prices the strategy/);
});

test("PaperTrading.jsx derives the intro cadence from the marks in the payload", () => {
	assert.match(paperTrading, /paperCadenceCopy\(newestMark\(deployments, marks, marksErrors\)\)/);
	assert.match(paperTrading, /cadence\.sentences\.map\(/);
	assert.match(paperTrading, /cadence\.staleness/);
});
