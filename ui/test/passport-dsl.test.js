// Strategy Passport redesign (#1646): the papers table, the DSL block, and
// the Recently-Generated → Library link.
//
// Two kinds of case here, and the split is deliberate. The rendering helpers
// live in a plain .js module (`src/strategySpec.js`) precisely so they can be
// executed for real under `node --test` — those get behaviour tests. The .jsx
// components cannot be imported without a build step, so their contracts are
// pinned as source-structure assertions, the same convention the rest of this
// suite uses (app-visuals.test.js, brief-on-passport.test.js).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	MAX_SPEC_CHARS,
	formatStrategySpec,
	tokenizeJson,
} from "../src/strategySpec.js";

const passport = readFileSync(
	new URL("../src/components/StrategyPassport.jsx", import.meta.url),
	"utf8",
);
const generationStatus = readFileSync(
	new URL("../src/components/GenerationStatus.jsx", import.meta.url),
	"utf8",
);
const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);
const strategies = readFileSync(
	new URL("../src/components/Strategies.jsx", import.meta.url),
	"utf8",
);
const css = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");

// ── formatStrategySpec ────────────────────────────────────────────────────

test("formatStrategySpec pretty-prints a spec across multiple lines", () => {
	const out = formatStrategySpec({ entry: { rsi: 30 }, universe: ["SPY"] });
	assert.ok(out);
	assert.equal(out.truncated, false);
	assert.deepEqual(JSON.parse(out.text), { entry: { rsi: 30 }, universe: ["SPY"] });
	// Indentation is the whole reason to pretty-print: a one-line blob is no
	// more readable than the prose it exists to replace.
	assert.ok(out.text.includes("\n  "), "nested keys must be indented");
});

test("formatStrategySpec returns null for everything that is not a spec", () => {
	// null/undefined: not served, or withheld from a non-owner.
	assert.equal(formatStrategySpec(null), null);
	assert.equal(formatStrategySpec(undefined), null);
	// An empty object is not a spec. Printing "{}" under a heading that calls
	// it "the rules this strategy runs on" would assert the rules are empty.
	assert.equal(formatStrategySpec({}), null);
	// Wrong types must not reach a <pre> as "42" or "\"hi\"".
	assert.equal(formatStrategySpec(42), null);
	assert.equal(formatStrategySpec("a string"), null);
	assert.equal(formatStrategySpec([1, 2]), null);
});

test("formatStrategySpec caps a pathological spec and says that it capped it", () => {
	// `strategy_spec` is an untyped JSON column — nothing at the DB or schema
	// layer bounds it. Silent truncation would be worse than none: the reader
	// would believe they had audited the whole spec.
	const huge = { assets: Array.from({ length: 4000 }, (_, i) => `TICKER${i}`) };
	const out = formatStrategySpec(huge);
	assert.ok(out);
	assert.equal(out.truncated, true);
	assert.equal(out.text.length, MAX_SPEC_CHARS);
	assert.ok(out.totalChars > MAX_SPEC_CHARS);
});

// ── tokenizeJson ──────────────────────────────────────────────────────────

test("tokenizeJson is lossless — concatenating the tokens rebuilds the input", () => {
	// The guard that matters. A highlighter that drops a character silently
	// changes a document the reader believes they are auditing.
	const text = JSON.stringify(
		{
			asset_universe: ["SPY", "TLT"],
			entry: { all: [{ indicator: "rsi", window: 14, op: "<", value: 30 }] },
			enabled: true,
			stop_loss: null,
			threshold: -1.5e-3,
		},
		null,
		2,
	);
	const tokens = tokenizeJson(text);
	assert.equal(tokens.map((t) => t.text).join(""), text);
});

test("tokenizeJson classifies keys apart from string values", () => {
	// The ordering trap: `"entry"` and `"rsi"` are both quoted strings, and a
	// naive string-first regex colours the key as a value.
	const tokens = tokenizeJson('{\n  "entry": "rsi"\n}');
	const keys = tokens.filter((t) => t.kind === "key").map((t) => t.text);
	const strings = tokens.filter((t) => t.kind === "string").map((t) => t.text);
	assert.deepEqual(keys, ['"entry"']);
	assert.deepEqual(strings, ['"rsi"']);
});

test("tokenizeJson classifies numbers, booleans and null distinctly", () => {
	const tokens = tokenizeJson('{"a": 14, "b": -1.5e-3, "c": true, "d": null}');
	const kinds = (k) => tokens.filter((t) => t.kind === k).map((t) => t.text);
	assert.deepEqual(kinds("number"), ["14", "-1.5e-3"]);
	assert.deepEqual(kinds("boolean"), ["true"]);
	assert.deepEqual(kinds("null"), ["null"]);
});

test("tokenizeJson is stateless across calls", () => {
	// TOKEN_RE is a module-level /g regex, so its lastIndex survives between
	// calls. Two identical calls must produce identical output — a component
	// re-render is exactly this.
	const text = '{"a": 1}';
	assert.deepEqual(tokenizeJson(text), tokenizeJson(text));
});

test("tokenizeJson emits no markup — the code block cannot inject HTML", () => {
	// Spec strings originate from an LLM. The obvious way to colour JSON is to
	// build an HTML string and hand it to dangerouslySetInnerHTML; this module
	// exists to make that impossible, and the component renders real elements.
	const hostile = JSON.stringify({ name: "<img src=x onerror=alert(1)>" });
	const tokens = tokenizeJson(hostile);
	assert.equal(tokens.map((t) => t.text).join(""), hostile);
	for (const t of tokens) assert.equal(typeof t.text, "string");
	assert.doesNotMatch(passport, /dangerouslySetInnerHTML/);
});

// ── The papers table ──────────────────────────────────────────────────────

test("source papers render as one real table, not a stack of cards", () => {
	// The structural check the issue asks for: a real <table> with a header
	// row, not a styled div list. The passport spec has required this since
	// day one ("All N PaperRefs as a table"; "the UI table is the right
	// primitive").
	assert.match(passport, /<table className="passport-papers__table">/);
	assert.match(passport, /<thead>/);
	assert.match(passport, /<th scope="col">Paper<\/th>/);
	assert.match(passport, /<th scope="col">Contribution<\/th>/);
	assert.match(passport, /<caption className="sr-only">/);
	// One table for every paper count. The old evidence column branched into
	// two entirely different layouts — a stacked multi-paper card and a
	// scalar-field single-paper card. Both are gone; a single-paper strategy
	// is now one row of the same table. Pinned on each dead layout's own copy,
	// which nothing else in the file uses.
	assert.doesNotMatch(
		passport,
		/This strategy synthesizes ideas from multiple research papers/,
	);
	assert.doesNotMatch(passport, /<div className="label mb-3">Source paper<\/div>/);
	// `\s+` since #1769: the element gained a third prop and wrapped onto
	// multiple lines. The claim this pins is "ONE table primitive renders the
	// papers", which is about the element, not about it fitting on one line.
	assert.match(passport, /<PapersTable\s+papers=\{papers\}/);
});

test("the papers table bounds its own height and width", () => {
	// Both overflow guards, because they fix different halves of "squished and
	// weird": max-height keeps a 20-paper strategy from burying the backtest
	// panel, overflow keeps a wide table from widening the page.
	assert.match(css, /\.passport-papers__scroll\s*\{[^}]*max-height:\s*19rem;/s);
	assert.match(css, /\.passport-papers__scroll\s*\{[^}]*overflow:\s*auto;/s);
	// The header must survive the body scrolling or the columns lose meaning.
	assert.match(
		css,
		/\.passport-papers__table thead th\s*\{[^}]*position:\s*sticky;/s,
	);
});

test("the papers table is honest about columns nothing has filled in", () => {
	// Authors / venue / year / DOI / citations / contribution are structurally
	// NULL for every generated row today (#1637 owns filling them). The table
	// must not invent "Unknown" or 0, and must tell the reader that a blank is
	// unrecorded rather than measured-as-nothing.
	assert.match(passport, /Blank cells are unrecorded, not zero/);
	// The footnote names only the columns that are empty for EVERY row, so it
	// shrinks by itself as #1637 lands rather than becoming a false claim.
	assert.match(passport, /function blankColumns\(rows\)/);
	assert.match(passport, /per-paper contribution/);
	// No fabricated placeholders anywhere in the paper cells.
	assert.doesNotMatch(passport, /"Unknown author/);
});

test("a strategy with no recorded papers says so instead of printing empty quotes", () => {
	// The old single-paper card rendered `"{s.paper_title}"` — literally a
	// pair of quotation marks around nothing when the title was null.
	assert.doesNotMatch(passport, /"\{s\.paper_title\}"/);
	assert.match(passport, /No source papers are recorded for this strategy\./);
});

// ── The DSL block ─────────────────────────────────────────────────────────

test("the passport renders the generated DSL spec", () => {
	assert.match(passport, /strategy_spec/);
	assert.match(passport, /<StrategySpecPanel spec=\{s\.strategy_spec\} \/>/);
	assert.match(passport, /className="passport-dsl__code"/);
	assert.match(passport, /Generated DSL/);
	// Pretty-printed and tokenized through the tested module, not inline.
	assert.match(passport, /formatStrategySpec\(spec\)/);
	assert.match(passport, /tokenizeJson\(formatted\.text\)/);
});

test("an absent DSL spec is explained, not silently omitted", () => {
	// null means one of three things and the reader cannot tell them apart
	// from an empty panel: no spec exists, the row predates the column, or the
	// server withheld it because the spec is owner-only reasoning.
	assert.match(passport, /No spec to show/);
	assert.match(passport, /visible only to the strategy/);
});

test("the DSL block preserves its own formatting and scrolls rather than widening the page", () => {
	assert.match(css, /\.passport-dsl__code\s*\{[^}]*white-space:\s*pre;/s);
	assert.match(css, /\.passport-dsl__code\s*\{[^}]*overflow:\s*auto;/s);
	assert.match(css, /\.passport-dsl__code\s*\{[^}]*max-height:\s*22rem;/s);
	// It is focusable so a keyboard user can scroll it, which obliges a
	// visible focus ring.
	assert.match(passport, /tabIndex=\{0\}/);
	assert.match(css, /\.passport-dsl__code:focus-visible\s*\{[^}]*outline:/s);
});

test("DSL token colours come from palette tokens, so both themes work", () => {
	// No literal hex in this block: `:root` and `:root[data-theme="light"]`
	// already define each of these with contrast tuned per theme, so there is
	// nothing to override for light mode.
	const block = css.slice(css.indexOf("Strategy Passport redesign — issue #1646"));
	assert.match(block, /\.passport-dsl__t-key\s*\{[^}]*color:\s*var\(--info\);/s);
	assert.match(block, /\.passport-dsl__t-string\s*\{[^}]*color:\s*var\(--positive\);/s);
	assert.doesNotMatch(block, /color:\s*#[0-9a-fA-F]{3,8}\b/);
});

// ── Recently-Generated → Library linkage ──────────────────────────────────

test("a finished generation links to the strategy it produced", () => {
	assert.match(generationStatus, /onNavigate/);
	// The exact call shape Library already uses, character for character up to
	// the argument name, so one grep finds every passport navigation in the
	// app and the two sites cannot drift into two route contracts.
	assert.match(generationStatus, /onNavigate\('strategy', \{ strategyId: j\.best_strategy_id \}\)/);
	assert.match(strategies, /onNavigate\('strategy', \{ strategyId \}\)/);
	// Threaded from the page that owns the router callback.
	assert.match(generate, /<GenerationStatus[\s\S]{0,200}?onNavigate=\{onNavigate\}/);
});

test("the passport link is gated on a strategy actually existing", () => {
	// A job can finish having persisted nothing (every candidate rejected), so
	// gating on `state === 'done'` alone would offer a link to a 404.
	assert.match(generationStatus, /\{j\.best_strategy_id && \(/);
});

test("the stream drill-in survives beside the new link", () => {
	// Explicit anti-goal in #1646: do NOT remove the SSE-replay drill-in. A
	// user may still want to watch a past job's reasoning.
	assert.match(generationStatus, /onDrillIn\?\.\(j\.job_id\)/);
	assert.match(generationStatus, /'resume →' : 'view →'/);
});
