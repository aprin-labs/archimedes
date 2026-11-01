// Claim-integrity guard for /architecture corpus counts.
//
// Live defect: https://archimedes-arc.com/architecture simultaneously claimed
// "18,752 RESEARCH PAPERS IN THE CORPUS MANIFEST / 18,907 ingested and
// retrievable today" and "18,907 of the 18,752 manifest papers are fully
// ingested and retrievable today". A subset cannot be larger than its
// superset. The Corpus page independently reports "18,907 paper metadata
// records".
//
// The two /health fields are different populations, not a completeness
// ratio of a committed manifest:
//   corpus_db_count = COUNT(papers)  (Corpus page)
//   corpus_papers   = count_corpus_papers()  (/health; embargo-mirrored SQL COUNT; ≤ db_count)
//
// Same idiom as oracle-copy.test.js / ipfs-pinning-copy.test.js: the
// formatter is unit-tested against the live numbers, and Architecture.jsx
// is scanned so the old template cannot return. Anti-vacuity: the detector
// must still reject the live sentence, otherwise it is guarding nothing.
//
// Hermetic: no network, no DOM, no `.env`.

import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

import {
	CORPUS_HERO_CAPTION,
	CORPUS_HERO_LABEL,
	LIVE_DEFECT_COUNTS,
	LIVE_DEFECT_SENTENCE,
	claimsIngestedSubsetOfManifest,
	formatCorpusHonestyCounts,
	formatCorpusLedgerCounts,
} from "../src/corpusCountCopy.js";

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

const architecture = readFileSync(repoFile("src/components/Architecture.jsx"), "utf8");
const spec = readFileSync(repoFile("../docs/specs/architecture-page-design.md"), "utf8");

const usFmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : n);

// The pre-fix concatenator — what Architecture.jsx used to do with the two
// /health fields. Kept here so the detector's anti-vacuity case is the
// exact production sentence, not a paraphrase.
function preFixHonestyNote({ corpus_papers, corpus_db_count }, fmtNum) {
	return `${fmtNum(corpus_db_count)} of the ${fmtNum(corpus_papers)} manifest papers are fully ingested and retrievable today`;
}

function preFixLedgerNote({ corpus_papers, corpus_db_count }, fmtNum) {
	return `${fmtNum(corpus_db_count)} of ${fmtNum(corpus_papers)} papers hydrated`;
}

test("the detector rejects the live 18,907-of-the-18,752 sentence", () => {
	assert.equal(claimsIngestedSubsetOfManifest(LIVE_DEFECT_SENTENCE), true);
	assert.equal(
		claimsIngestedSubsetOfManifest(preFixHonestyNote(LIVE_DEFECT_COUNTS, usFmt)),
		true,
		"detector must reject the exact concatenator the live page used",
	);
	assert.equal(
		claimsIngestedSubsetOfManifest(preFixLedgerNote(LIVE_DEFECT_COUNTS, usFmt)),
		true,
		"detector must reject the ledger's 'N of M papers hydrated' sibling",
	);
});

test("the detector is not a match-anything: honest copy is allowed", () => {
	const honest = formatCorpusHonestyCounts(LIVE_DEFECT_COUNTS, usFmt);
	assert.equal(claimsIngestedSubsetOfManifest(honest), false);
	assert.equal(
		claimsIngestedSubsetOfManifest("the Corpus page count — metadata + abstracts"),
		false,
	);
});

test("ingested-of-manifest copy cannot have ingested > manifest (live counts)", () => {
	const honest = formatCorpusHonestyCounts(LIVE_DEFECT_COUNTS, usFmt);
	assert.equal(
		claimsIngestedSubsetOfManifest(honest),
		false,
		"live 18907-vs-18752 must not render as ingested ⊂ manifest",
	);
	assert.doesNotMatch(honest, /\bof the\b/);
	assert.doesNotMatch(honest, /fully ingested/i);
	assert.doesNotMatch(honest, /hydrated/i);
	assert.match(honest, /18,907/);
	assert.match(honest, /18,752/);
	assert.match(honest, /different populations/);
	assert.match(honest, /metadata records/);
});

test("even when the numbers would make a subset look plausible, still no 'of the'", () => {
	// Swapped: 18,752 ingested of 18,907 would look arithmetically fine and
	// still be a false completeness claim — they are different populations.
	const swapped = formatCorpusHonestyCounts(
		{ corpus_papers: 18907, corpus_db_count: 18752 },
		usFmt,
	);
	assert.equal(claimsIngestedSubsetOfManifest(swapped), false);
	assert.doesNotMatch(swapped, /\bof the\b/);
	assert.doesNotMatch(swapped, /fully ingested/i);
});

test("equal counts still do not get subset copy — equality is not ingestion completeness", () => {
	const equal = formatCorpusHonestyCounts(
		{ corpus_papers: 18907, corpus_db_count: 18907 },
		usFmt,
	);
	assert.equal(claimsIngestedSubsetOfManifest(equal), false);
	assert.doesNotMatch(equal, /\bof the\b/);
});

test("the ledger formatter names one population, with no denominator", () => {
	const ledger = formatCorpusLedgerCounts(LIVE_DEFECT_COUNTS, usFmt);
	assert.equal(claimsIngestedSubsetOfManifest(ledger), false);
	assert.equal(ledger, "18,752 embargo-eligible papers scored");
	assert.doesNotMatch(ledger, /\bof\b/);
	assert.doesNotMatch(ledger, /hydrated/i);
});

// ── Architecture.jsx wiring + the old template must be gone ──────────────

const BANNED_SOURCE_PATTERNS = [
	[
		"honesty_of_the_manifest",
		/of the\{\s*" "\s*\}\s*\n?\s*\{fmtNum\(health\.corpus_papers\)\} manifest papers are fully ingested/,
		'{fmtNum(health.corpus_db_count)} of the{" "}{fmtNum(health.corpus_papers)} manifest papers are fully ingested',
	],
	[
		"fully_ingested_and_retrievable",
		/fully ingested\s+and retrievable today/,
		"fully ingested and retrievable today",
	],
	[
		"ledger_papers_hydrated",
		/papers hydrated/,
		"{fmtNum(health.corpus_db_count)} of {fmtNum(health.corpus_papers)} papers hydrated",
	],
	[
		"hero_corpus_papers_as_manifest",
		/label="Research papers in the corpus manifest"/,
		'label="Research papers in the corpus manifest"',
	],
	[
		"hero_value_is_corpus_papers",
		/value=\{health\?\.corpus_papers\}/,
		"value={health?.corpus_papers}",
	],
	[
		"body_n_paper_arxiv_manifest",
		/health\.corpus_papers\)\}-paper arXiv manifest/,
		"{fmtNum(health.corpus_papers)}-paper arXiv manifest",
	],
	[
		"generation_starts_from_db_count",
		/Generation starts from[\s\S]{0,120}corpus_db_count/,
		"Generation starts from a corpus … {fmtNum(health.corpus_db_count)} paper metadata records",
	],
];

test("every banned source pattern still rejects the pre-fix literal", () => {
	for (const [name, pattern, example] of BANNED_SOURCE_PATTERNS) {
		assert.match(
			example,
			pattern,
			`pattern ${name} no longer rejects its canonical example — it is guarding nothing`,
		);
	}
});

test("Architecture.jsx no longer concatenates ingested-of-manifest copy", () => {
	for (const [name, pattern] of BANNED_SOURCE_PATTERNS) {
		assert.doesNotMatch(
			architecture,
			pattern,
			`Architecture.jsx still contains banned corpus subset copy (${name})`,
		);
	}
});

test("splicing the live concatenator back into Architecture.jsx fails the source scan", () => {
	// The unfixed page. Proves the scan is not passing because it never
	// looks: putting the live sentence's JSX back must trip honesty_of_the_manifest.
	const spliced = architecture.replace(
		"formatCorpusHonestyCounts(health, fmtNum)",
		'{fmtNum(health.corpus_db_count)} of the{" "}{fmtNum(health.corpus_papers)} manifest papers are fully ingested',
	);
	assert.notEqual(spliced, architecture, "splice must actually change Architecture.jsx");
	const honesty = BANNED_SOURCE_PATTERNS.find(([name]) => name === "honesty_of_the_manifest")[1];
	assert.match(
		spliced,
		honesty,
		"the source scan must fail against the live concatenator — otherwise it would have passed on the unfixed page",
	);
});

test("Architecture.jsx drives the hero off corpus_db_count and the honest helper", () => {
	assert.match(architecture, /from "\.\.\/corpusCountCopy\.js"/);
	assert.match(architecture, /formatCorpusHonestyCounts/);
	assert.match(architecture, /formatCorpusLedgerCounts/);
	assert.match(architecture, /CORPUS_HERO_LABEL/);
	assert.match(architecture, /CORPUS_HERO_CAPTION/);
	assert.match(architecture, /value=\{health\?\.corpus_db_count\}/);
	assert.match(architecture, new RegExp(`label=\\{CORPUS_HERO_LABEL\\}`));
	assert.doesNotMatch(architecture, /ingested and retrievable today/);
});

test("the design spec no longer teaches '{ingested} of the {manifest}'", () => {
	assert.doesNotMatch(spec, /of the\s*\n?manifest papers are fully ingested/);
	assert.doesNotMatch(spec, /\{ingested\} of the/);
});

test("hero caption is not a second count posing as a subset", () => {
	assert.doesNotMatch(CORPUS_HERO_CAPTION, /\d/);
	assert.doesNotMatch(CORPUS_HERO_CAPTION, /of the/);
	assert.match(CORPUS_HERO_CAPTION, /Corpus page count/);
	assert.equal(CORPUS_HERO_LABEL, "Paper metadata records");
});
