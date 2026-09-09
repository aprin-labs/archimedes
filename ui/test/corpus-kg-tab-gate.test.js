// The Topic Clusters tab ships hidden until its data exists (#1406).
//
// CorpusExplorer.jsx always rendered a `knowledge-graph` tab over
// kg_entities/kg_relations, which are 0 rows until #1090 produces a KB
// pipeline artifact and #1092 backfills Postgres. #1392 made the tab's own
// zero-state honest, but a visitor still had to click an empty capability to
// find that out.
//
// Two things are pinned here, and they fail for different reasons:
//
//   1. the flag is OFF unless the env var is exactly "true" — a typo or a
//      truthy-looking "1" must not ship the tab;
//   2. TABS is BUILT from the flag rather than listing the id literally, which
//      is the shape that lets Vite fold the array at build time.
//
// Same idiom as oracle-copy.test.js: a raw source-text scan with anti-vacuity
// coverage — every pattern must reject its own canonical pre-fix example, so a
// pattern that stops matching anything fails loudly instead of guarding nothing.

import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

const explorer = readFileSync(
	repoFile("src/components/CorpusExplorer.jsx"),
	"utf8",
);
const flags = readFileSync(repoFile("src/featureFlags.js"), "utf8");
const envExample = readFileSync(repoFile(".env.example"), "utf8");

// The exact line this change removed. If the guard below ever stops matching
// it, the guard is broken rather than satisfied.
const PRE_FIX_TABS =
	"const TABS = ['catalog', 'overview', 'graph', 'knowledge-graph']";

const UNGATED_TABS =
	/const TABS = \[[^\]]*['"]knowledge-graph['"][^\]]*\]\s*;?\s*$/m;

test('the flag is off unless the env var is exactly "true"', async () => {
	// import.meta.env is undefined under node --test, which is the unset case.
	const { KNOWLEDGE_GRAPH_TAB_ENABLED } = await import(
		"../src/featureFlags.js"
	);
	assert.equal(
		KNOWLEDGE_GRAPH_TAB_ENABLED,
		false,
		"the tab must default to hidden — its backing tables are empty until #1090/#1092",
	);
	assert.match(
		flags,
		/VITE_KNOWLEDGE_GRAPH_TAB === "true"/,
		'strict === "true" only: a truthy-looking "1" or "yes" must not ship an empty capability',
	);
});

test("TABS is derived from the flag, not a bare literal", () => {
	assert.ok(
		!UNGATED_TABS.test(explorer),
		"CorpusExplorer.jsx lists 'knowledge-graph' in TABS unconditionally — the tab ships regardless of the flag",
	);
	assert.match(
		explorer,
		/KNOWLEDGE_GRAPH_TAB_ENABLED\s*\?\s*\[['"]knowledge-graph['"]\]\s*:\s*\[\]/,
		"TABS must spread the id in from the flag so Vite can fold the array away at build time",
	);
});

test("the guard rejects the exact line this change removed", () => {
	// Anti-vacuity: prove UNGATED_TABS still matches the real pre-fix source.
	assert.ok(
		UNGATED_TABS.test(PRE_FIX_TABS),
		"the ungated-TABS pattern no longer matches the code it exists to catch — it is guarding nothing",
	);
});

test("the flag is documented and separate from the roadmap umbrella", () => {
	assert.match(envExample, /VITE_KNOWLEDGE_GRAPH_TAB=false/);
	// ROADMAP_SURFACES means "out of scope for the MVP". This tab is in scope
	// and merely has no data, so folding it in there would mean previewing
	// vaults also reveals an empty Topic Clusters tab.
	assert.ok(
		!/ROADMAP_PAGES = new Set\(\[[^\]]*knowledge-graph/s.test(flags),
		"'knowledge-graph' must not be a ROADMAP_PAGES entry: featureEnabled() gates routed pages and would no-op on a tab id",
	);
});

test("the honest zero-state stays for anyone who previews the tab", () => {
	// #1392's /health-branched copy is explicitly out of scope for #1406 — it
	// remains the fallback when the flag is on.
	const kg = readFileSync(repoFile("src/components/CorpusKG.jsx"), "utf8");
	assert.match(
		kg,
		/corpus_kg_built/,
		"CorpusKG.jsx must still branch on the live /health signal (#1392)",
	);
});
