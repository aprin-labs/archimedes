import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
	CATEGORY_SUMMARIES,
	STORAGE_INVENTORY,
} from "../src/storage-consent.js";

const EM_DASH = /—|&mdash;|&#8212;|&#x2014;|\\u2014/i;

// Match the existing copy guards: ignore comments, preserve URLs and JSX text.
function copyWithoutComments(source) {
	return source
		.replace(/\/\*[\s\S]*?\*\//g, " ")
		.replace(/(^|\s)\/\/.*$/gm, "$1");
}

for (const component of [
	"Landing",
	"Security",
	"Architecture",
	"PublicLayout",
	"StorageDisclosure",
	"ConsentBanner",
	"ConsentChoices",
	"ThemeSwitcher",
]) {
	test(`${component} public copy contains no em dashes`, () => {
		const source = readFileSync(
			new URL(`../src/components/${component}.jsx`, import.meta.url),
			"utf8",
		);
		assert.doesNotMatch(copyWithoutComments(source), EM_DASH, component);
	});
}

test("missing architecture stats use a compact label and explicit unavailable caption", () => {
	const source = readFileSync(
		new URL("../src/components/Architecture.jsx", import.meta.url),
		"utf8",
	);
	assert.match(source, /\{failed \? \(\s*<span[^>]*>N\/A<\/span>/);
	assert.match(source, /\{failed \? "live value unavailable" : caption\}/);
});

test("shared storage disclosure copy contains no em dashes", () => {
	assert.doesNotMatch(Object.values(CATEGORY_SUMMARIES).join("\n"), EM_DASH);
	for (const entry of STORAGE_INVENTORY) {
		assert.doesNotMatch(
			[entry.purpose, entry.reveals, entry.onReject].join("\n"),
			EM_DASH,
			entry.name,
		);
	}
});

test("copy guard rejects literal and encoded em dashes, but ignores comments", () => {
	for (const source of [
		"<p>Research — not advice.</p>",
		"<p>Research&mdash;not advice.</p>",
		"<p>Research&#8212;not advice.</p>",
		"<p>Research&#x2014;not advice.</p>",
		'const copy = "Research \\u2014 not advice.";',
		"<p>See https://example.test/ — source code.</p>",
	]) {
		assert.match(copyWithoutComments(source), EM_DASH, source);
	}
	assert.doesNotMatch(
		copyWithoutComments(
			"// Context — not rendered.\n<p>Research.{/* Note — not copy. */}</p>",
		),
		EM_DASH,
	);
});
