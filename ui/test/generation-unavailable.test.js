import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	CORPUS_TOO_FEW_PAPERS,
	CORPUS_UNAVAILABLE,
	NO_LLM_BACKEND,
	describeUnavailable,
	normalizeSuggestions,
	waysForward,
} from "../src/data/generationUnavailable.js";

// Guards for the too-few-papers error card.
//
// Owner's screenshot: the run died with "Generation is unavailable right now:
// the corpus yielded <2 papers for this steer — the society cannot fuse."
// rendered as a red line and nothing else — no way forward. These tests pin
// (a) the decisions about what is honest to offer, as pure functions, and
// (b) the card source actually rendering both ways forward.
//
// `.jsx` is not importable under plain `node --test`, so the component itself
// is checked as source text — the established pattern in this suite.

const card = readFileSync(
	new URL("../src/components/GenerationUnavailable.jsx", import.meta.url),
	"utf8",
);
const stream = readFileSync(
	new URL("../src/components/GenerationStream.jsx", import.meta.url),
	"utf8",
);
const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);

// The payload backend/archimedes/agents/corpus_viability.py emits.
const TOO_FEW = {
	code: "GENERATION_UNAVAILABLE",
	reason_code: CORPUS_TOO_FEW_PAPERS,
	message:
		"Generation stopped before synthesis: keyword (lexical) retrieval over the paper corpus matched 1 paper for your brief — “a treasury ladder that beats cash” — and fusing a strategy needs at least 2, so no strategy was drafted or saved.",
	steer: "a treasury ladder that beats cash",
	retrieval: "lexical",
	candidates_found: 1,
	min_papers: 2,
	corpus_size: 8,
	suggestions: [
		{ term: "crypto", kind: "asset_class", papers: 8 },
		{ term: "equities", kind: "asset_class", papers: 8 },
		{ term: "vol", kind: "asset_class", papers: 2 },
	],
};

// ── The numbers come from the payload, never from the UI ─────────────────

test("describeUnavailable reads the measured fields off the event", () => {
	const d = describeUnavailable(TOO_FEW);
	assert.equal(d.reasonCode, CORPUS_TOO_FEW_PAPERS);
	assert.equal(d.steer, "a treasury ladder that beats cash");
	assert.equal(d.candidatesFound, 1);
	assert.equal(d.minPapers, 2);
	assert.equal(d.retrieval, "lexical");
	assert.equal(d.suggestions.length, 3);
});

test("a missing count stays null — the card must not invent one", () => {
	const d = describeUnavailable({ reason_code: CORPUS_TOO_FEW_PAPERS });
	assert.equal(d.candidatesFound, null);
	assert.equal(d.minPapers, null);
	assert.equal(d.retrieval, "", "retrieval is only claimed when the server said so");
	assert.deepEqual(d.suggestions, []);
});

test("normalizeSuggestions drops anything with no corpus evidence behind it", () => {
	const kept = normalizeSuggestions([
		{ term: "crypto", kind: "asset_class", papers: 8 },
		{ term: "vapourware", kind: "asset_class", papers: 0 },
		{ term: "no-count", kind: "mechanism" },
		{ term: "   ", kind: "mechanism", papers: 5 },
		null,
	]);
	assert.deepEqual(
		kept.map((s) => s.term),
		["crypto"],
	);
});

// ── The two ways forward ─────────────────────────────────────────────────

test("a too-few-papers failure offers exactly two ways forward", () => {
	const ways = waysForward(TOO_FEW);
	assert.deepEqual(
		ways.map((w) => w.id),
		["broaden", "surprise"],
	);
});

test("the broaden way carries the corpus-derived suggestions", () => {
	// The mutation this guards: strip `suggestions` out of the error payload
	// (or out of the card) and "broaden the brief" becomes advice with nothing
	// behind it. Then this assertion is red.
	const broaden = waysForward(TOO_FEW).find((w) => w.id === "broaden");
	assert.ok(broaden, "no broaden way was offered");
	assert.deepEqual(
		broaden.suggestions.map((s) => s.term),
		["crypto", "equities", "vol"],
	);
	for (const s of broaden.suggestions) {
		assert.ok(s.papers > 0, `${s.term} offered with no paper count`);
	}
});

test("broaden never promises a mechanism it cannot deliver", () => {
	// `select_candidates` filters candidate membership on asset-class terms
	// only — the brief's free text reaches ranking, never membership. Copy
	// that says "or mechanism" is a promise the retrieval cannot keep, and it
	// costs the user another failed run to find out.
	const broaden = waysForward(TOO_FEW).find((w) => w.id === "broaden");
	assert.doesNotMatch(broaden.detail, /mechanism/i, broaden.detail);
	assert.match(broaden.detail, /asset class/i);
	for (const s of broaden.suggestions) {
		assert.equal(s.kind, "asset_class", `${s.term} is offered on an axis retrieval never reads`);
	}
});

test("with no suggestions, broaden is withheld rather than faked", () => {
	const ways = waysForward({ ...TOO_FEW, suggestions: [] });
	assert.deepEqual(
		ways.map((w) => w.id),
		["surprise"],
	);
});

test("an unavailable corpus or a downed LLM offers neither way", () => {
	// Rewriting the brief cannot fix either; saying otherwise sends the user
	// in a circle.
	for (const reason of [CORPUS_UNAVAILABLE, NO_LLM_BACKEND]) {
		assert.deepEqual(waysForward({ ...TOO_FEW, reason_code: reason }), [], reason);
	}
});

test("only the corpus-shortfall failure gets a card", () => {
	// An unstructured error, a downed LLM, or an unavailable corpus has no
	// move to offer — those keep the stream's one-line message and get no
	// card of non-advice.
	assert.deepEqual(waysForward({ message: "boom" }), []);
	assert.equal(describeUnavailable({ message: "boom" }).reasonCode, "");
	assert.match(
		card,
		/if \(d\.reasonCode !== CORPUS_TOO_FEW_PAPERS\) return null/,
		"the card must render nothing outside the corpus-shortfall case",
	);
});

test("the ways-forward heading counts the ways it actually has", () => {
	assert.match(
		card,
		/ways\.length === 1 \? "One way forward" : "Two ways forward"/,
		'a card showing one way must not be headed "Two ways forward"',
	);
});

// ── The card actually renders both ways ──────────────────────────────────

test("the card renders every way forward, with the suggestion chips", () => {
	assert.match(card, /waysForward\(data\)/);
	assert.match(card, /ways\.map\(/);
	assert.match(
		card,
		/suggestions=\{w\.suggestions\}/,
		"the broaden way must render its corpus-derived chips",
	);
	assert.match(card, /Surprise me\s*<\/button>/, "the second way must be pressable");
	assert.match(card, /onBroaden\(s\.term, steer\)/);
});

test("the card shows the steer and the measured count", () => {
	assert.match(card, /\{d\.steer/);
	assert.match(card, /\{d\.candidatesFound\}/);
	assert.match(card, /\{d\.minPapers/);
});

test("the card never claims semantic retrieval", () => {
	// Prod retrieval is a lowercased substring match; there is no embedding
	// column. "keyword (lexical)" is the only honest description.
	for (const banned of ["semantic", "embedding", "vector search"]) {
		assert.ok(
			!card.toLowerCase().includes(banned),
			`GenerationUnavailable.jsx claims ${banned}`,
		);
	}
	assert.match(card, /keyword/);
});

// ── Wiring ───────────────────────────────────────────────────────────────

test("the stream mounts the card on a structured error and keeps the payload", () => {
	assert.match(stream, /import GenerationUnavailable from '\.\/GenerationUnavailable'/);
	assert.match(stream, /setErrorData\(data \|\| null\)/);
	assert.match(
		stream,
		/terminal === 'error' && errorData\?\.reason_code && \(/,
		"the card is gated on the structured payload",
	);
	assert.match(stream, /<GenerationUnavailable data=\{errorData\} onBroaden=\{onBroaden\} onSurprise=\{onSurprise\} \/>/);
});

test("Generate wires both ways forward back to the brief box", () => {
	assert.match(generate, /onBroaden=\{handleBroaden\}/);
	assert.match(generate, /onSurprise=\{handleSurpriseFromStream\}/);
	// Both moves must leave the stream view, or the "way forward" is a button
	// that changes something the user cannot see.
	assert.match(generate, /const handleBroaden = \(term, steer\) => \{\s*\n\s*setDrillInJobId\(null\);/);
	assert.match(generate, /const handleSurpriseFromStream = \(\) => \{\s*\n\s*setDrillInJobId\(null\);/);
	assert.match(generate, /ref=\{briefRef\}/, "the brief box must be focusable after a way forward");
});
