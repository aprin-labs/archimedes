// Claim-integrity guard for Explore's data-sourcing disclosure (#1218).
//
// #1218 established that yfinance is an unlicensed-for-commercial-
// redistribution dependency, and the decision recorded in
// docs/adr/market-data-sourcing.md splits sourcing by surface: Tiingo feeds
// backtesting and paid analysis under commercial terms; yfinance stays on
// the free, ungated Explore viewer. That split is only honest if the page
// SAYS it — a reader looking at a yfinance-priced card has no other way to
// know the paid path runs on different data.
//
// So the disclosure is a claim, and claims get pinned. Same idiom as
// oracle-copy.test.js and roadmap-copy.test.js: a raw source-text scan
// (readFileSync, no JSX parsing) with anti-vacuity coverage — every required
// pattern must match a canonical example of the sentence it is guarding, so
// a pattern that stops matching anything fails loudly instead of silently
// guarding nothing.
//
// What this file does NOT claim: that the disclosure is legally sufficient.
// It pins that four specific statements are present and that a set of
// overclaiming phrasings is absent. Legal sufficiency is the owner's call.

import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

const explore = readFileSync(repoFile("src/components/Explore.jsx"), "utf8");

//: `[name, regex, canonical_example]` — the example is a sentence that SHOULD
//: satisfy the pattern. `test_every_required_pattern_matches_its_canonical_example`
//: runs it, so a typo'd or over-tightened regex is caught here rather than
//: silently passing forever against a file it happens to match.
const REQUIRED_DISCLOSURE = [
	[
		"free_open_source_viewer",
		/free, open-source viewer/,
		"Explore is a free, open-source viewer over yfinance market-data streams.",
	],
	[
		"names_yfinance_as_the_stream",
		/yfinance market-data streams/,
		"Explore is a free, open-source viewer over yfinance market-data streams.",
	],
	[
		"nothing_sold_or_redistributed",
		/[Nn]othing on this page is sold or commercially\s+redistributed/,
		"Nothing on this page is sold or commercially redistributed — it is here to look at.",
	],
	[
		"paid_analysis_is_separately_licensed",
		/[Pp]aid\s+analysis runs on separately licensed data/,
		"Paid analysis runs on separately licensed data, not on this feed.",
	],
];

test("every required disclosure pattern matches its canonical example", () => {
	for (const [name, pattern, example] of REQUIRED_DISCLOSURE) {
		assert.match(
			example,
			pattern,
			`pattern ${name} no longer matches its canonical example ${JSON.stringify(example)} — it is guarding nothing`,
		);
	}
});

test("Explore.jsx carries the four required data-sourcing statements (#1218)", () => {
	for (const [name, pattern] of REQUIRED_DISCLOSURE) {
		assert.match(
			explore,
			pattern,
			`Explore.jsx is missing the '${name}' half of the #1218 data-sourcing disclosure — ` +
				"see docs/adr/market-data-sourcing.md for the wording this pins.",
		);
	}
});

//: Phrasings that would make the disclosure dishonest in the OTHER direction:
//: claiming a licensing posture we do not have, or implying Explore's own feed
//: is the licensed one. Each is paired with a canonical example so the
//: anti-vacuity test below can prove the pattern still bites.
const OVERCLAIMING_PATTERNS = [
	[
		"claims_explore_data_is_licensed",
		/Explore[^.]{0,80}\blicensed (?:market )?data\b/,
		"Explore runs on licensed market data from our vendor.",
	],
	[
		"claims_commercial_redistribution_rights",
		/(?:[Ww]e|[Yy]ou) (?:may|can) (?:commercially )?redistribute/,
		"You may redistribute this data commercially.",
	],
	[
		"claims_a_data_licence_for_yfinance",
		/yfinance[^.]{0,60}\b(?:licen[cs]ed|under licence|under license)\b/,
		"Prices come from yfinance, licensed for our use.",
	],
];

test("every overclaiming pattern matches its canonical example", () => {
	for (const [name, pattern, example] of OVERCLAIMING_PATTERNS) {
		assert.match(
			example,
			pattern,
			`pattern ${name} no longer matches its canonical example ${JSON.stringify(example)} — it is guarding nothing`,
		);
	}
});

test("Explore.jsx never claims a data licence it does not have (#1218)", () => {
	for (const [name, pattern] of OVERCLAIMING_PATTERNS) {
		assert.doesNotMatch(
			explore,
			pattern,
			`Explore.jsx contains an overclaiming data-licensing phrasing (${name}). yfinance is ` +
				"accessed through an unofficial interface and is NOT licensed for commercial " +
				"redistribution — see docs/adr/market-data-sourcing.md.",
		);
	}
});

test("the disclosure sits on the page itself, not only in a code comment", () => {
	// A disclosure that lives in a `//` comment renders to nobody. Strip
	// comments before re-checking the load-bearing sentence.
	const withoutComments = explore
		.replace(/\/\*[\s\S]*?\*\//g, "")
		.replace(/^\s*\/\/.*$/gm, "");
	assert.match(
		withoutComments,
		/free, open-source viewer/,
		"the #1218 disclosure must be rendered copy, not a comment",
	);
	assert.match(
		withoutComments,
		/[Pp]aid\s+analysis runs on separately licensed data/,
		"the #1218 disclosure must be rendered copy, not a comment",
	);
});
