// Library → Examples must not present curated references as failures.
//
// The owner's screenshot of Examples (34): every hand-curated card carried
// "Reference only — gate failed" AND a red `blocked` pill, beside metrics
// nobody re-ran for that card. Two of those three are this file's subject
// (the status pill is a separate change):
//
//   1. `blocked` is a DEPLOYABILITY verdict — DeployabilityChip grades a row
//      against `/api/selection-bias/gate`, the same verdict the vault deploy
//      gate reads (`api/vaults_routes.py::_deployable_levels`). Vaults are out
//      of the MVP cut (#1266), so with ROADMAP_SURFACES_ENABLED off the pill
//      grades references against a capability the shipped build does not have.
//   2. the numbers beside them are the values the strategy record ships with;
//      for the curated library those trace to the backtest-fixture snapshot
//      (#1187), which the API already reports per row as
//      `display_metrics_source` and the UI simply dropped.
//
// Idiom: source-text pins plus one real import, like corpus-kg-tab-gate.test.js
// and library-loading.test.js (ui/package.json runs plain `node --test`; .jsx
// is not importable). Every pattern below is shown rejecting its own pre-fix
// input, so a pattern that silently stops matching anything fails loudly
// instead of guarding nothing.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { METRICS_SOURCE_NOTES, metricsSourceNote } from "../src/metricsSource.js";

const strategies = readFileSync(
	new URL("../src/components/Strategies.jsx", import.meta.url),
	"utf8",
);

// ── 1. The deployability chip is gated on the roadmap flag ───────────────

// The gate must be the chip's FIRST statement: every branch after it (blocked,
// not deployable, needs level N, deployable, checking…) is an answer to a vault
// question, so none of them may be reached with the flag off.
const CHIP_GATE =
	/function DeployabilityChip\(\{ deploy, level, gatePending \}\) \{(?:[^\S\n]*\/\/[^\n]*\n|[^\S\n]*\n)*[^\S\n]*if \(!ROADMAP_SURFACES_ENABLED\) return null/;

// The chip exactly as it stood before this change — the input the guard exists
// to reject. If CHIP_GATE ever matches this, it has stopped testing anything.
const PRE_FIX_CHIP = `function DeployabilityChip({ deploy, level, gatePending }) {
  // #1645: the Library now renders rows as soon as the strategy list resolves,
  // without waiting for the (much slower) /api/selection-bias/gate call.
  if (!deploy) {
    if (!gatePending) return null
`;

test("DeployabilityChip renders nothing while the roadmap surfaces are off", () => {
	assert.match(strategies, CHIP_GATE);
	assert.doesNotMatch(
		PRE_FIX_CHIP,
		CHIP_GATE,
		"the guard must reject the pre-fix chip — an ungated chip is the defect",
	);
});

test("the flag this hangs on is genuinely off in a default build", async () => {
	// import.meta.env is undefined under `node --test`, which is the unset
	// case: this is what the shipped bundle does unless VITE_ROADMAP_SURFACES
	// is exactly "true". Without this, the pattern above could be satisfied by
	// a flag that is on everywhere.
	const { ROADMAP_SURFACES_ENABLED } = await import("../src/featureFlags.js");
	assert.equal(ROADMAP_SURFACES_ENABLED, false);
});

test("the blocked branch is gated, not deleted — it returns with the flag on", () => {
	// Anti-goal: this change must not become "delete the failing pill". With
	// VITE_ROADMAP_SURFACES=true the chip is whole, blocked branch included.
	const gateIdx = strategies.indexOf("if (!ROADMAP_SURFACES_ENABLED) return null");
	const blockedIdx = strategies.indexOf("if (deploy.blocked_by_floor) {");
	assert.notEqual(gateIdx, -1, "no roadmap gate found in DeployabilityChip");
	assert.notEqual(blockedIdx, -1, "the blocked_by_floor branch must survive");
	assert.ok(
		gateIdx < blockedIdx,
		"the roadmap gate must precede the blocked branch, not replace it",
	);
});

// ── 2. …and the hidden annotation costs nothing to fetch ─────────────────

const GATED_FETCH =
	/const gatePromise = ROADMAP_SURFACES_ENABLED \? apiGet\('\/api\/selection-bias\/gate'\) : Promise\.resolve\(\{ strategies: \[\] \}\)/;
const PRE_FIX_FETCH = "const gatePromise = apiGet('/api/selection-bias/gate')";

test("the deployability gate is not fetched when nothing renders it", () => {
	// Same rule #1324 set for the hidden Published tab: gating the render while
	// still hitting the hidden API on every load is not a fix. This route is
	// the slowest request on the page (it recomputes the whole cohort gate).
	assert.match(strategies, GATED_FETCH);
	assert.doesNotMatch(
		PRE_FIX_FETCH,
		GATED_FETCH,
		"the guard must reject the pre-fix unconditional fetch",
	);
});

// ── 3. The Examples intro states the new identity ────────────────────────

function examplesIntro() {
	const start = strategies.indexOf("{activeTab === 'examples' && (");
	assert.notEqual(start, -1, "could not find the Examples tab branch");
	const end = strategies.indexOf("{loading && <StrategyListSkeleton />}", start);
	assert.notEqual(end, -1, "could not bound the Examples intro");
	// JSX comments stripped: these assertions are about the copy a READER sees.
	// A `{/* ... */}` block explaining the change must not be able to satisfy a
	// claim the rendered paragraph does not make (nor to trip the retired-claim
	// check by quoting the sentence it retired).
	return strategies.slice(start, end).replace(/\{\/\*[\s\S]*?\*\/\}/g, "");
}

// The exact copy this replaced. Every REQUIRED assertion below must fail
// against it, and the one RETIRED phrase must be found in it — otherwise these
// patterns are pinning something the old copy already said.
const PRE_FIX_INTRO = `<strong>Example strategies</strong> — hand-curated single-paper implementations
from published research. <em>Not</em> outputs of the fusion engine. Included
so you can read a strategy card, understand the metrics, and see what a
rigor-gate verdict looks like. They're also the candidate pool the curated-library
path of Generate picks and weights from.`;

const REQUIRED_INTRO_CLAIMS = [
	// what they are
	/hand-curated reference implementations/,
	// not graded until a backtest is actually run for them
	/graded only\s*\n?\s*once a backtest has been run for it/,
	/an absent verdict is not a failure/,
	// where the verdict of record lives instead
	/verdict of record/,
	// where the numbers come from
	/marked <strong>fixture<\/strong>/,
];

// The sentence that invited the reading this whole change exists to retract:
// it told the reader the tab was a place to see verdicts, so the red pills
// looked like the point rather than like a bug.
const RETIRED_INTRO_CLAIM = /see what a\s*\n?\s*rigor-gate verdict looks like/;

test("the Examples intro says what the examples are and are not", () => {
	const intro = examplesIntro();
	for (const claim of REQUIRED_INTRO_CLAIMS) {
		assert.match(intro, claim);
		assert.doesNotMatch(
			PRE_FIX_INTRO,
			claim,
			`the old copy already satisfied ${claim} — that assertion guards nothing`,
		);
	}
	assert.doesNotMatch(intro, RETIRED_INTRO_CLAIM);
	assert.match(
		PRE_FIX_INTRO,
		RETIRED_INTRO_CLAIM,
		"the retired-claim pattern no longer matches the copy it was written against",
	);
});

test("the intro does not call the examples fusion output or a scoreboard", () => {
	const intro = examplesIntro();
	assert.match(intro, /<em>not<\/em> outputs of the\s*\n?\s*fusion engine/);
	assert.match(intro, /<strong>not a scoreboard<\/strong>/);
});

// ── 4. The fixture mark rides next to the numbers ────────────────────────

test("both the table row and the card mark where their numbers came from", () => {
	const marks =
		strategies.match(/<MetricsSourceTag source=\{s\.display_metrics_source\} \/>/g) ||
		[];
	assert.equal(
		marks.length,
		2,
		"expected exactly one mark per rendered row — the table row and the card",
	);
	// The card's mark sits inside the Sharpe stat, i.e. beside a number, not
	// off in the badge row where it would read as a status.
	assert.match(
		strategies,
		/Sharpe<\/div><div className="mono"><MetricValue metric="sharpe_ratio"[\s\S]{0,200}<MetricsSourceTag source=\{s\.display_metrics_source\} \/>/,
	);
});

// ── 5. …and the mark never overstates what it knows ──────────────────────

test("only non-measured sources are marked", () => {
	assert.equal(metricsSourceNote("strategy_record").label, "fixture");
	assert.equal(metricsSourceNote("stub_placeholder").label, "placeholder");
	// The load-bearing omission: a persisted backtest row IS a run. Marking it
	// "fixture" would be a fresh false claim, worse than the silence this
	// change fixes.
	assert.equal(metricsSourceNote("persisted_backtest"), null);
	// No data, an older payload, or a generated row (coerceGenerated does not
	// carry the field) makes no claim in either direction.
	for (const absent of ["unavailable", "", undefined, null, 42]) {
		assert.equal(metricsSourceNote(absent), null);
	}
	// The lookup is own-property only: an API payload naming an inherited
	// Object.prototype member must not conjure a mark out of the prototype.
	for (const inherited of ["toString", "constructor", "__proto__"]) {
		assert.equal(metricsSourceNote(inherited), null);
	}
});

test("no mark claims the number was measured or verified", () => {
	for (const source of ["strategy_record", "stub_placeholder"]) {
		const { title } = metricsSourceNote(source);
		assert.doesNotMatch(title, /\bmeasured\b/i);
		assert.doesNotMatch(title, /\bverified\b/i);
	}
});

// ── 6. The mark's vocabulary is the BACKEND's, and stays pinned to it ─────
//
// METRICS_SOURCE_NOTES' keys are bare string literals of what
// `display_metrics_source` returns. Renaming one there and in its own
// backend/tests/test_metrics_provenance.py — the normal way such a rename
// lands — leaves that file at 9 passed and, without this test, the whole UI
// suite green, while every "fixture" mark silently stops rendering AND the
// intro's promise that "the row is marked fixture" becomes false: fail-open
// on the exact honesty signal this change exists to add. Same idiom as
// ui/test/security-claims.test.js and account-deletion.test.js: read the
// backend source from the UI suite.
//
// The function moved to `services/curated_metrics.py` with #1746 / PR-B, which
// made the display chain a WRITE-side resolution (the passport sync stores the
// answer; every read surface serves the row). The four labels it returns are
// unchanged, which is exactly what this pins.
//
// That move also turned the branches from bare string literals into named
// constants, and the first version of this guard extracted the CONSTANT NAMES
// and lowercased them. Those coincide with the wire values only because each
// name happens to match its own value: changing `SOURCE_STRATEGY_RECORD` to
// `"fixture_snapshot"` left this guard extracting the identical set and still
// green, while every "fixture" mark silently stopped rendering — verbatim the
// failure this test exists to catch. So the names are RESOLVED to their values
// before the comparison, and a wire-value rename reddens the UI suite again.
const curatedMetrics = readFileSync(
	new URL(
		"../../backend/archimedes/services/curated_metrics.py",
		import.meta.url,
	),
	"utf8",
);

/** `SOURCE_* = "..."` module constants, as a name -> WIRE VALUE map. */
function sourceConstants() {
	const constants = new Map(
		[...curatedMetrics.matchAll(/^(SOURCE_[A-Z_]+)\s*=\s*"([^"]+)"/gm)].map(
			(m) => [m[1], m[2]],
		),
	);
	assert.ok(
		constants.size >= 4,
		`curated_metrics.py declares ${constants.size} SOURCE_* constants; the four chain links must each have one`,
	);
	return constants;
}

function displayMetricsSourceStates() {
	const constants = sourceConstants();
	const start = curatedMetrics.indexOf("def display_metrics_source(");
	assert.notEqual(
		start,
		-1,
		"display_metrics_source not found — the mark's source is gone",
	);
	const end = curatedMetrics.indexOf("\ndef ", start + 1);
	const body = curatedMetrics.slice(start, end === -1 ? undefined : end);
	const returned = [...body.matchAll(/return (SOURCE_[A-Z_]+)/g)].map(
		(m) => m[1],
	);
	assert.ok(
		returned.length >= 4,
		`display_metrics_source returns ${returned.length} branches; expected one per chain link`,
	);
	return new Set(
		returned.map((name) => {
			const value = constants.get(name);
			assert.ok(
				value !== undefined,
				`display_metrics_source returns ${name}, which curated_metrics.py never assigns a string literal — the wire value cannot be checked`,
			);
			return value;
		}),
	);
}

test("every marked source is a value the backend actually returns", () => {
	const returned = displayMetricsSourceStates();
	// Anti-vacuity: the backend's own four states must be found, or this is
	// comparing against an empty set and pinning nothing.
	for (const state of [
		"strategy_record",
		"persisted_backtest",
		"stub_placeholder",
		"unavailable",
	]) {
		assert.ok(
			returned.has(state),
			`display_metrics_source no longer returns "${state}"`,
		);
	}
	for (const key of Object.keys(METRICS_SOURCE_NOTES)) {
		assert.ok(
			returned.has(key),
			`the UI marks "${key}" but no backend branch returns it — the mark is dead`,
		);
	}
});
