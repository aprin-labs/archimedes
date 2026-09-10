// Claim-integrity guard for Explore's oracle-coverage copy (#1371).
//
// Every deployed PriceOracle has been stale since the T3.2 redeploy, and the
// live push set (oracle_updater's YFINANCE_MAP + CRYPTO_MAP) covers just 2 of
// the 281 deployed oracles (sSPY + sBTC) — the #1371 scope amendment's
// correction to the issue's original "281" ceiling. Explore.jsx's copy used
// to frame the on-chain oracle as the primary source ("Prices come from the
// on-chain PriceOracle when available, with yfinance as the ... fallback"),
// which reads backwards: yfinance is the actual default for 279 of 281
// assets, and there is no pusher at all for those 279, not merely an
// "unavailable" oracle.
//
// Same idiom as roadmap-copy.test.js: a raw source-text scan (readFileSync,
// no JSX parsing) with anti-vacuity coverage — every pattern must reject its
// own canonical example (the literal string this PR removed), so a pattern
// that stops matching anything fails loudly instead of silently guarding
// nothing.

import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

const explore = readFileSync(repoFile("src/components/Explore.jsx"), "utf8");
const assetModal = readFileSync(
	repoFile("src/components/AssetModal.jsx"),
	"utf8",
);

//: `(name, regex, canonical_example)` — the example is the exact pre-#1371
//: literal from Explore.jsx; test_every_pattern_rejects_its_canonical_example
//: runs it so a pattern that stops matching anything is caught immediately.
const ORACLE_PRIMARY_PATTERNS = [
	[
		"prices_come_from_oracle_when_available",
		/Prices come from the on-chain PriceOracle when available/,
		"Prices come from the on-chain PriceOracle when available, with yfinance as the off-chain fallback.",
	],
	[
		"oracle_vs_offchain_fallback_binary",
		/upstream source the\s+price came from \(on-chain oracle vs\. off-chain fallback\)/,
		"the upstream source the price came from (on-chain oracle vs. off-chain fallback).",
	],
];

test("every oracle-primary pattern rejects its canonical example", () => {
	for (const [name, pattern, example] of ORACLE_PRIMARY_PATTERNS) {
		assert.match(
			example,
			pattern,
			`pattern ${name} no longer rejects its canonical example ${JSON.stringify(example)} — it is guarding nothing`,
		);
	}
});

test("Explore.jsx no longer implies oracle-primary pricing (#1371)", () => {
	for (const [name, pattern] of ORACLE_PRIMARY_PATTERNS) {
		assert.doesNotMatch(
			explore,
			pattern,
			`Explore.jsx still contains the oracle-primary phrasing (${name}) — state the actual ` +
				"design instead: N of the deployed oracles are priced from the on-chain PriceOracle " +
				"today, the rest from off-chain market-data feeds (yfinance).",
		);
	}
});

test("Explore.jsx derives its oracle-coverage count from price_source, not a literal", () => {
	assert.match(
		explore,
		/assets\.filter\(\s*\(?a\)?\s*=>\s*a\.price_source === ['"]oracle['"]\s*,?\s*\)\.length/,
		"expected an oracleBackedCount derived from the served assets' price_source " +
			"(never a hard-coded '2 of 281' literal, so the copy tracks reality if the push set changes)",
	);
});

test("Explore.jsx's footer and header copy both cite the derived oracle count", () => {
	assert.match(explore, /oracleBackedCount/);
	assert.match(explore, /oracleCoverageNote/);
});

test("AssetModal.jsx only renders the oracle address when the card is actually oracle-priced (#1371)", () => {
	assert.match(
		assetModal,
		/\{asset\.oracle_address && asset\.price_source === 'oracle' && \(/,
		"oracle_address is a capability marker populated for every deployed oracle regardless of " +
			"which source actually priced the card — gating on price_source === 'oracle' too is " +
			"required so a yfinance-priced card never shows an oracle address",
	);
});

test("AssetModal.jsx no longer renders the oracle address off presence alone", () => {
	// The exact pre-#1371 defect site, literally: rendering solely on
	// oracle_address truthiness with no price_source check.
	assert.doesNotMatch(
		assetModal,
		/\{asset\.oracle_address && \(\s*\n\s*<div>\s*\n\s*<div className="caption"/,
	);
});
