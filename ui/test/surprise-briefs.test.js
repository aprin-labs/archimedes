import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { SURPRISE_BRIEFS } from "../src/data/surpriseBriefs.js";
import { pickSurpriseBrief } from "../src/data/pickSurpriseBrief.js";
import { SUPPORTED_ASSETS } from "../src/data/assetUniverse.js";

// Guards for the "Surprise me" bank + picker (#1642).
//
// Plain `node --test`, no DOM: both modules are deliberately React-free so
// the two load-bearing properties — never visible before a press, never the
// same entry twice in a row — are testable as pure functions.

const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);

// ── The bank ─────────────────────────────────────────────────────────────

test("the bank is large enough to feel bottomless", () => {
	// The issue's floor is 100. Asserting the floor rather than the exact
	// count so adding an entry does not fail the test that guards the bank.
	assert.ok(
		SURPRISE_BRIEFS.length >= 100,
		`bank has ${SURPRISE_BRIEFS.length} entries, need >= 100`,
	);
});

test("every entry has a unique id and the three fields the UI reads", () => {
	const seen = new Set();
	for (const entry of SURPRISE_BRIEFS) {
		assert.equal(typeof entry.id, "string", `bad id: ${JSON.stringify(entry)}`);
		assert.match(entry.id, /^[a-z0-9]+(-[a-z0-9]+)*$/, `id not kebab-case: ${entry.id}`);
		assert.ok(!seen.has(entry.id), `duplicate id: ${entry.id}`);
		seen.add(entry.id);
		assert.equal(typeof entry.label, "string", `${entry.id}: label must be a string`);
		assert.ok(entry.label.length > 0 && entry.label.length <= 60, `${entry.id}: label length`);
		assert.equal(typeof entry.brief, "string", `${entry.id}: brief must be a string`);
	}
});

test("no two entries carry the same brief text", () => {
	// A duplicated brief makes "a different one each press" a lie for the
	// reader even when the ids differ.
	const seen = new Map();
	for (const entry of SURPRISE_BRIEFS) {
		const prior = seen.get(entry.brief);
		assert.equal(prior, undefined, `${entry.id} duplicates the brief of ${prior}`);
		seen.set(entry.brief, entry.id);
	}
});

test("every brief is substantive and clean enough to submit as-is", () => {
	for (const entry of SURPRISE_BRIEFS) {
		assert.equal(entry.brief, entry.brief.trim(), `${entry.id}: leading/trailing space`);
		assert.ok(entry.brief.length >= 40, `${entry.id}: brief too short (${entry.brief.length})`);
		// The pipeline reads free text; a newline in a one-line brief is a
		// copy-paste artefact, not intent.
		assert.doesNotMatch(entry.brief, /[\n\r\t]/, `${entry.id}: contains a control whitespace char`);
	}
});

// The three dogfood-proven entries carried over from exampleBriefs.js describe
// their universe in prose ("major ETFs") rather than by ticker, and their text
// is not ours to edit — it is what actually cleared the live pipeline in the
// 2026-07-04 bake-off. They are exempt from the names-its-symbols rule below,
// and nothing else is.
const LEGACY_PROSE_UNIVERSE = new Set([
	"momentum-quality-gold-usdc",
	"crypto-trend-treasury-rotation",
	"low-vol-income-preservation",
]);

test("suggestedAssets only ever pre-selects real, supported symbols", () => {
	const supported = new Set(SUPPORTED_ASSETS);
	for (const entry of SURPRISE_BRIEFS) {
		if (entry.suggestedAssets === undefined) continue;
		assert.ok(Array.isArray(entry.suggestedAssets), `${entry.id}: suggestedAssets must be an array`);
		assert.ok(
			entry.suggestedAssets.length >= 1 && entry.suggestedAssets.length <= 5,
			`${entry.id}: keep suggestedAssets to 1-5 symbols`,
		);
		for (const symbol of entry.suggestedAssets) {
			// Generate.jsx filters unsupported symbols out silently
			// (applyExample), so a typo here does not throw — it just
			// quietly pre-selects less than the entry promises.
			assert.ok(supported.has(symbol), `${entry.id}: "${symbol}" is not in the supported universe`);
		}
	}
});

test("suggestedAssets names symbols the brief itself asked for", () => {
	for (const entry of SURPRISE_BRIEFS) {
		if (entry.suggestedAssets === undefined) continue;
		if (LEGACY_PROSE_UNIVERSE.has(entry.id)) continue;
		for (const symbol of entry.suggestedAssets) {
			assert.ok(
				entry.brief.includes(symbol),
				`${entry.id}: pre-selects "${symbol}" but the brief never names it`,
			);
		}
	}
});

// ── The picker ───────────────────────────────────────────────────────────

test("pickSurpriseBrief returns an entry from the bank", () => {
	const ids = new Set(SURPRISE_BRIEFS.map((b) => b.id));
	for (let i = 0; i < 200; i += 1) {
		const pick = pickSurpriseBrief(SURPRISE_BRIEFS);
		assert.ok(pick, "picker returned nothing");
		assert.ok(ids.has(pick.id), `picker invented an id: ${pick.id}`);
		assert.ok(SURPRISE_BRIEFS.includes(pick), "picker returned a copy, not a bank entry");
	}
});

test("two presses in a row never return the same brief", () => {
	// 200 trials over the real bank, threading each pick's id back in exactly
	// as Generate.jsx does with `lastSurpriseId`.
	let previousId = null;
	for (let i = 0; i < 200; i += 1) {
		const pick = pickSurpriseBrief(SURPRISE_BRIEFS, previousId);
		assert.notEqual(pick.id, previousId, `repeat on trial ${i}: ${pick.id}`);
		previousId = pick.id;
	}
});

test("the no-repeat guarantee holds at both ends of the random range", () => {
	// 200 samples of Math.random will essentially never produce exactly 0 or
	// the largest float below 1, so the boundaries are pinned explicitly
	// rather than left to chance.
	const bank = [{ id: "a" }, { id: "b" }, { id: "c" }];
	for (const random of [() => 0, () => 0.999999999999999, () => 1]) {
		for (const previousId of ["a", "b", "c"]) {
			const pick = pickSurpriseBrief(bank, previousId, random);
			assert.ok(pick, "picker returned nothing at a boundary");
			assert.notEqual(pick.id, previousId);
		}
	}
});

test("degenerate banks do not blank the box", () => {
	assert.equal(pickSurpriseBrief([], "a"), null);
	assert.equal(pickSurpriseBrief(null, "a"), null);
	// One entry and it is the one just shown: returning null would clear the
	// textarea, which is worse than a repeat nobody can avoid.
	const single = [{ id: "only" }];
	assert.equal(pickSurpriseBrief(single, "only").id, "only");
	// An unknown previousId (bank edited under a stale id) leaves the whole
	// bank in play instead of returning nothing.
	assert.ok(pickSurpriseBrief(SURPRISE_BRIEFS, "no-such-id"));
});

// ── The wiring ───────────────────────────────────────────────────────────

test("Generate renders no bank text until Surprise me is pressed", () => {
	// The retired always-visible list, in both its markup and its data forms.
	assert.doesNotMatch(generate, /EXAMPLE_BRIEFS/);
	assert.doesNotMatch(generate, /exampleBriefs/);
	assert.doesNotMatch(generate, /SURPRISE_BRIEFS\.map/);
	// The retired heading, with its trailing colon — that colon is what makes
	// this match the rendered text node and not the file's own history note.
	assert.doesNotMatch(generate, /Examples — click to fill:/);
	// The ONLY read of the bank is the picker call inside the press handler.
	const bankReads = generate.match(/SURPRISE_BRIEFS/g) ?? [];
	assert.equal(bankReads.length, 2, "SURPRISE_BRIEFS should appear exactly twice: the import and the pick");
	assert.match(generate, /pickSurpriseBrief\(SURPRISE_BRIEFS, lastSurpriseId\)/);
	// State holds an id and a label — never brief text, which would be a
	// second place an entry could surface before a press.
	assert.match(generate, /const \[lastSurpriseId, setLastSurpriseId\] = useState\(null\)/);
	assert.match(generate, /setSurpriseLabel\(pick\.label\)/);
	assert.doesNotMatch(generate, /setSurprise(?:Brief|Text)\b/);
});

test("the Surprise me button exists and is the only example affordance", () => {
	assert.match(generate, /Surprise me/);
	assert.match(generate, /className="generate-surprise-btn"/);
	assert.match(generate, /onClick=\{handleSurprise\}/);
	// The retired per-example button class must not come back.
	assert.doesNotMatch(generate, /className="generate-example"/);
});

test("the long-form prompting guide lives in docs, not in the component", () => {
	assert.match(generate, /docs\/writing-a-brief\.md/);
	// The one-line hint stays; the tutorial prose does not move in with it.
	assert.match(generate, /Name assets, a mechanism, and a goal\./);
	// The number is a tripwire for "did the tutorial move in here", not a
	// budget: `docs/writing-a-brief.md` is ~6,500 characters, so any cap
	// meaningfully below (current size + 6,500) still fires the moment prose
	// lands. Raised 60,000 -> 62,000 when #1801's brief bound (a maxLength, a
	// live counter and the guidelines link) merged with main: main alone was
	// 59,824 characters — 176 under — and the union crossed the line without
	// a word of tutorial prose moving anywhere. Bump it the same way, with
	// the same arithmetic, the next time two honest additions collide.
	assert.ok(
		generate.length < 62000,
		"Generate.jsx grew past 62K characters — did the tutorial prose land here instead of docs/?",
	);
});

test("the gating banner (#1643) has a mount point and no gating logic here", () => {
	assert.match(generate, /className="generate-gate-slot" data-generate-gate-slot/);
});
