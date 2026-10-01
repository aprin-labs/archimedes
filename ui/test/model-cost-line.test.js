// Claim + orientation guard for the Generate page's engine card
// (ui/src/components/ModelCostPanel.jsx).
//
// Owner observation (screenshot, Generate page, under "Approve & Generate"):
// the card read
//
//     MODEL & COST
//     Running Amazon Nova Micro · $0.035 in / $0.140 out per 1M tokens
//
// Every word of that is true, but it leads with a billing rate on the one
// surface whose first job is to tell a new user WHAT they are about to run.
// The reworked header leads with the engine ("Strategy engine — debate
// society over the research corpus, served by <model>") and keeps the rate as
// a secondary line, labelled as the provider's published list price rather
// than as the user's bill.
//
// This file pins three properties of that header:
//
//   1. it names the engine BEFORE it quotes any per-token price;
//   2. the price is labelled as the provider's list price;
//   3. it can never claim a model the backend is not running — no model name
//      or model id from the pricing snapshot appears as a literal in the
//      component, and the served model is still read from /health's
//      `llm_model` through the shared fetchHealth cache, matched against the
//      snapshot by `model_id` (the same source the card used before).
//
// Idiom is the repo's source-text one (oracle-copy.test.js,
// roadmap-copy.test.js): `.jsx` is not importable under `node --test`, so the
// component is read as text. The three properties above are each computed by a
// `null`-or-reason helper (`orderingProblem`, `priceLabelProblem`,
// `hardCodedModelProblem`) and each is paired with a mutation test that feeds
// the helper an input it must reject and asserts the exact reason — so a helper
// that stops checking anything fails loudly instead of passing vacuously. The
// two remaining tests (the /health wiring and the drill-in) are flat
// `assert.match` regexes with no branch that can silently stop checking, so
// they carry no paired mutation.

import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

const pricing = JSON.parse(
	readFileSync(
		new URL("../src/data/modelPricing.json", import.meta.url),
		"utf8",
	),
);

const panel = readFileSync(
	new URL("../src/components/ModelCostPanel.jsx", import.meta.url),
	"utf8",
);

// Comments are not copy. Without this, a `{/* ... list price ... */}` note
// next to the markup would satisfy a check about what the user reads (it did,
// on the first run of this file).
function stripComments(text) {
	return text.replace(/\{?\/\*[\s\S]*?\*\/\}?/g, "").replace(/^\s*\/\/.*$/gm, "");
}

// ── The collapsed header: the toggle button's own subtree ──────────────────
// Everything a user sees before they drill in lives between the toggle's
// `aria-expanded={open}` and its `</button>`. Slicing it keeps the ordering
// assertions honest: the drill-in table below quotes prices too, and a
// whole-file index comparison would pass on a header that quoted nothing.
function collapsedHeader(src) {
	const start = src.indexOf("aria-expanded={open}");
	const end = src.indexOf("</button>", start);
	assert.ok(
		start >= 0 && end > start,
		"ModelCostPanel.jsx: could not find the collapsed header (the `aria-expanded={open}` toggle button) — this guard is reading the wrong region",
	);
	return stripComments(src.slice(start, end));
}

const header = collapsedHeader(panel);

// `null` = header is well-ordered; a string = the problem, so the mutation
// tests below can assert the exact rejection reason instead of "some error".
function orderingProblem(text) {
	const engine = text.search(/Strategy engine/);
	const corpus = text.search(/[Dd]ebate society over the research corpus/);
	const price = text.search(/per 1M tokens/);
	if (engine < 0) return "header never names the strategy engine";
	if (corpus < 0) return "header never says what the engine does";
	if (price < 0) return "header quotes no per-token rate at all";
	if (engine > price || corpus > price)
		return "header quotes the per-token price before it names the engine";
	return null;
}

// `null` = the rate is labelled as the provider's list price ahead of the
// numbers; a string = the problem.
function priceLabelProblem(text) {
	// `\b` after "price" so the plural fallback line ("Compare provider list
	// prices across N models") cannot stand in for the real label.
	const label = text.search(/[Pp]rovider list price\b/);
	const price = text.search(/per 1M tokens/);
	if (price < 0) return "header quotes no per-token rate at all";
	if (label < 0) return "the per-token rate is not labelled as a list price";
	if (label > price)
		return "the per-token rate appears before its list-price label";
	return null;
}

// `null` = no snapshot model name or id is a literal in the source; a string
// = the problem. Shared by the property test and the mutation that must turn
// it red — a check inlined only on the clean source cannot be shown to reject
// a hard-coded model.
function hardCodedModelProblem(src) {
	for (const m of pricing.models) {
		if (src.includes(m.name)) return `hard-codes model name ${m.name}`;
		if (src.includes(m.model_id)) return `hard-codes model id ${m.model_id}`;
	}
	return null;
}

// ── 1. The header leads with the engine ───────────────────────────────────

test("collapsed header names the engine before it quotes any price", () => {
	assert.equal(
		orderingProblem(header),
		null,
		"ModelCostPanel.jsx's collapsed header must name the strategy engine (and what it does) before the per-token rate — the owner's screenshot showed the rate leading",
	);
});

test("mutation: a price-led header is rejected", () => {
	// (a) the literal pre-change header from the owner's screenshot.
	const PRE_CHANGE = `<>Running <strong style={{ color: 'var(--text-1)' }}>{active.provider} {active.name}</strong>{' · '}{fmt(active.input)} in / {fmt(active.output)} out per 1M tokens</>`;
	assert.equal(
		orderingProblem(PRE_CHANGE),
		"header never names the strategy engine",
		"the ordering check no longer rejects the exact header this change replaced — it is guarding nothing",
	);

	// (b) the SHIPPED header, mechanically rotated so its price block leads.
	// This is the reorder-only mutation: not one word changes, only the order.
	const pricePos = header.search(/per 1M tokens/);
	const reordered = header.slice(pricePos) + header.slice(0, pricePos);
	assert.equal(
		orderingProblem(reordered),
		"header quotes the per-token price before it names the engine",
		"reordering the shipped header so the price leads does not turn the ordering check red — it is guarding nothing",
	);
});

// ── 2. The rate is labelled as the provider's list price ──────────────────

test("the per-token rate is labelled as the provider's list price", () => {
	assert.equal(
		priceLabelProblem(header),
		null,
		"ModelCostPanel.jsx's collapsed header must label the per-token rate as the provider's list price, not present it as the user's bill",
	);
	assert.match(
		header,
		/Provider list price/,
		"expected the literal 'Provider list price' label on the secondary line",
	);
});

test("mutation: an unlabelled rate is rejected", () => {
	// (a) the pre-change header from the owner's screenshot — a bare rate.
	const PRE_CHANGE = `<>Running <strong>{active.provider} {active.name}</strong>{' · '}{fmt(active.input)} in / {fmt(active.output)} out per 1M tokens</>`;
	assert.equal(
		priceLabelProblem(PRE_CHANGE),
		"the per-token rate is not labelled as a list price",
		"the label check no longer rejects the exact header this change replaced — it is guarding nothing",
	);

	// (b) the SHIPPED header with only its label word swapped for a claim
	// about the user's own bill.
	const UNLABELLED = header.replace(/Provider list price/g, "Your cost");
	assert.equal(
		priceLabelProblem(UNLABELLED),
		"the per-token rate is not labelled as a list price",
		"stripping the list-price label does not turn the label check red — it is guarding nothing",
	);
});

// ── 3. The card can only name the model it is actually being served by ────

test("no model name or id from the pricing snapshot is hard-coded in the card", () => {
	assert.ok(
		pricing.models.length > 1,
		"pricing snapshot is empty — this check would be vacuous",
	);
	assert.equal(
		hardCodedModelProblem(stripComments(panel)),
		null,
		"ModelCostPanel.jsx hard-codes a model name or id from the pricing snapshot. The card must name only the model /health reports as served (or the user's pick), never a literal — a literal keeps claiming that model after the backend moves off it.",
	);
});

test("mutation: a hard-coded served model is rejected", () => {
	const recommended = pricing.models.find((m) => m.recommended);
	assert.ok(recommended, "pricing snapshot has no recommended model to mutate with");
	const HARD_CODED = panel.replace(
		"Debate society over the research corpus",
		`Debate society over the research corpus, served by ${recommended.provider} ${recommended.name}`,
	);
	assert.equal(
		hardCodedModelProblem(stripComments(HARD_CODED)),
		`hard-codes model name ${recommended.name}`,
		`writing ${JSON.stringify(recommended.name)} into the card is not caught by the hard-coded-model check — it is guarding nothing`,
	);
});

test("the served model is still read from /health's llm_model, matched by model_id", () => {
	// The card's claim is only as good as its source. Keep it on the shared
	// health cache (#1333) and on the id-equality lookup, so "served by X"
	// tracks the backend instead of a snapshot guess.
	assert.match(
		panel,
		/fetchHealth\(\)/,
		"ModelCostPanel.jsx must read the served model through the shared fetchHealth() cache",
	);
	assert.match(
		panel,
		/setActiveModel\(d\?\.llm_model \|\| null\)/,
		"the served model must come from /health's llm_model field",
	);
	assert.match(
		panel,
		/rows\.find\(\(m\) => m\.model_id && m\.model_id === activeModel\)/,
		"the header's model row must be the snapshot row whose model_id equals the served model id",
	);
	// The rate quoted in the header is that row's (or the user's pick's) —
	// never a constant.
	assert.match(
		panel,
		/const priced = chosen \|\| active/,
		"the header's quoted rate must come from the chosen or served row",
	);
	assert.doesNotMatch(
		header,
		/\$\d/,
		"the collapsed header must not contain a literal dollar figure — the rate is formatted from the snapshot row",
	);
});

// ── 4. The drill-in the header opens is still there ───────────────────────

test("the header still drills in to the full model/cost table", () => {
	assert.match(
		panel,
		/aria-expanded=\{open\}/,
		"the collapsed header must remain an expand toggle",
	);
	assert.match(
		panel,
		/<table/,
		"the drill-in must still render the full model cost table",
	);
	assert.match(
		panel,
		/\{pricing\.note\}/,
		"the drill-in must still carry the pricing snapshot's own note",
	);
});
