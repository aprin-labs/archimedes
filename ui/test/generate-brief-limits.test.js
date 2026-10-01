import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { SURPRISE_BRIEFS } from "../src/data/surpriseBriefs.js";

// The brief's length bound, on the browser side (#1801).
//
// The bound that MATTERS is the server's: `GenerateBrief.intent`'s
// `max_length` in backend/archimedes/api/generate_schemas.py, plus
// `shape.too_long` in backend/archimedes/services/brief_screen.py. An agent
// or a curl client never runs this file's `maxLength`. What the browser cap
// buys is that a paste does not silently turn into a 422 whose cause the user
// cannot see — so its only real requirement is that it equals the server's.
//
// Cross-language constant: this test reads the Python source and the JSX and
// asserts the two numbers match, which is what stops them drifting.

const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);
const schemas = readFileSync(
	new URL(
		"../../backend/archimedes/api/generate_schemas.py",
		import.meta.url,
	),
	"utf8",
);

const uiMax = Number(/const BRIEF_MAX_LEN = (\d+);/.exec(generate)?.[1]);
const serverMax = Number(/^INTENT_MAX_LEN = (\d+)$/m.exec(schemas)?.[1]);

test("the UI declares a brief length cap", () => {
	assert.ok(Number.isInteger(uiMax), "Generate.jsx must declare BRIEF_MAX_LEN");
});

test("the UI cap equals the server's max_length", () => {
	assert.ok(
		Number.isInteger(serverMax),
		"generate_schemas.py must declare INTENT_MAX_LEN",
	);
	assert.equal(
		uiMax,
		serverMax,
		"Generate.jsx BRIEF_MAX_LEN and generate_schemas.INTENT_MAX_LEN have drifted",
	);
});

test("the textarea actually carries the cap", () => {
	// Asserting on the attribute, not just the constant: a constant nothing
	// reads is decoration.
	assert.match(generate, /maxLength=\{BRIEF_MAX_LEN\}/);
});

test("the brief has a live character counter", () => {
	// Without a counter a hard cap is a textarea that silently stops
	// accepting keystrokes — the worst version of a limit.
	assert.match(generate, /\{intent\.length\}\/\{BRIEF_MAX_LEN\}/);
	assert.match(generate, /id="generate-brief-count"/);
	assert.match(
		generate,
		/aria-describedby="generate-brief-help generate-brief-count"/,
		"the counter must be announced with the field, not left visual-only",
	);
});

test("the page links the guidelines page", () => {
	assert.match(generate, /docs\/brief-guidelines\.md/);
	// The tutorial link (#1642) must survive alongside it.
	assert.match(generate, /docs\/writing-a-brief\.md/);
});

test("every Surprise Me entry fits inside the cap", () => {
	// The bank fills the textarea directly. An entry over the cap would be
	// truncated on insert — a shipped brief the product itself cannot send.
	const over = SURPRISE_BRIEFS.filter((b) => b.brief.length > uiMax);
	assert.deepEqual(
		over.map((b) => b.id),
		[],
		`Surprise Me entries longer than ${uiMax} characters`,
	);
});
