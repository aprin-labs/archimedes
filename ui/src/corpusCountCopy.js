// Honest labels for the two /health corpus counts on /architecture.
//
// Live defect (2026-09-01): the page rendered
//   "18,907 of the 18,752 manifest papers are fully ingested and retrievable today"
// by concatenating corpus_db_count and corpus_papers with "of the". A subset
// cannot be larger than its superset. Those fields are different populations:
//
//   corpus_db_count  = COUNT(papers) — metadata records; the Corpus page
//                      reports the same number. The committed JSONL seed is
//                      the floor; intake can grow the table past it.
//   corpus_papers    = count_corpus_papers() published by /health — scalar
//                      COUNT of papers the generation surface would load,
//                      with the Xia Outcome Embargo cutoff mirrored in SQL
//                      (`published != '' AND published < day-after-cutoff`).
//                      Same meaning as before; no longer a full
//                      load_corpus() materialization. Still ≤
//                      corpus_db_count by construction (same table, one
//                      extra WHERE). On the day #1740 deploys the number
//                      may read slightly higher than the old strict-`<`
//                      Python filter (papers exactly 30 days old). The
//                      user-facing "Generation currently loads N" still
//                      means the size of the corpus generation would load.
//
// corpus_papers ≤ corpus_db_count by construction, but they are still
// different populations (embargoed COUNT vs full table), not a
// completeness ratio of a committed manifest — so this module never
// emits "X of the Y" / "fully ingested" / "hydrated" copy.
// Extracted from Architecture.jsx so the live 18,907-vs-18,752
// sentence is unit-testable without a DOM (same shape as paperCopy.js /
// generateQuote.js). See #778 for the broader corpus claim-integrity rule.

/**
 * The live counts that produced the dishonest sentence. Frozen as the
 * regression input — not as a number the UI may quote.
 */
export const LIVE_DEFECT_COUNTS = {
	corpus_papers: 18752,
	corpus_db_count: 18907,
};

export const LIVE_DEFECT_SENTENCE =
	"18,907 of the 18,752 manifest papers are fully ingested and retrievable today";

export const CORPUS_HERO_LABEL = "Paper metadata records";

export const CORPUS_HERO_CAPTION =
	"the Corpus page count — metadata + abstracts, not a knowledge graph";

/**
 * True iff `text` is the banned ingested-of-manifest subset claim.
 *
 * Matches the live sentence and any "N of the M manifest papers are fully
 * ingested" variant (with or without thousands separators). Also matches
 * the ledger's sibling "N of M papers hydrated". Does not match honest
 * copy that names the two populations without a subset preposition.
 */
export function claimsIngestedSubsetOfManifest(text) {
	if (typeof text !== "string" || text.length === 0) return false;
	return (
		/\d[\d,]* of the \d[\d,]* manifest papers/i.test(text) ||
		/\d[\d,]* of(?: the)? \d[\d,]* papers hydrated/i.test(text) ||
		/\bfully ingested and retrievable\b/i.test(text)
	);
}

/**
 * Honesty-note count sentence. Names both live fields as different
 * populations. Never uses "of the" / "fully ingested" / "hydrated".
 */
export function formatCorpusHonestyCounts(health, fmtNum) {
	const records = fmtNum(health.corpus_db_count);
	const generation = fmtNum(health.corpus_papers);
	return (
		`The papers table holds ${records} metadata records — the same count the Corpus page reports. ` +
		`Generation currently loads ${generation}. These are different populations, not a completeness ratio of a committed manifest.`
	);
}

/**
 * Ledger retrieval-row count. Retrieval scores the embargo-filtered
 * generation surface (`corpus_papers`), not the full papers-table count.
 * One population, one label — no denominator.
 */
export function formatCorpusLedgerCounts(health, fmtNum) {
	return `${fmtNum(health.corpus_papers)} embargo-eligible papers scored`;
}
