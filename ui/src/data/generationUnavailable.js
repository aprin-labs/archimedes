// The too-few-papers outcome, as data.
//
// The backend's `error` SSE event (code GENERATION_UNAVAILABLE) carries a
// structured payload — `reason_code`, `steer`, `candidates_found`,
// `min_papers`, `retrieval`, `suggestions[]` — built by
// backend/archimedes/agents/corpus_viability.py. This module turns that payload
// into exactly what the card renders, and decides which ways forward are
// HONEST to offer. React-free on purpose so those decisions are testable
// without a DOM (ui/test/generation-unavailable.test.js).
//
// Claim discipline: candidate papers are found by a lowercased substring match
// over category + title + abstract. That is lexical. Nothing here — and
// nothing in the card — may describe it as semantic or embedding-based.

export const CORPUS_TOO_FEW_PAPERS = "CORPUS_TOO_FEW_PAPERS";
export const CORPUS_UNAVAILABLE = "CORPUS_UNAVAILABLE";
export const NO_LLM_BACKEND = "NO_LLM_BACKEND";

/**
 * Keep only suggestions that carry real corpus evidence.
 *
 * A suggestion without a positive paper count is a term we cannot justify
 * recommending, so it is dropped rather than rendered with a blank or a zero.
 */
export function normalizeSuggestions(raw) {
	if (!Array.isArray(raw)) return [];
	return raw
		.filter(
			(s) =>
				s &&
				typeof s.term === "string" &&
				s.term.trim().length > 0 &&
				Number.isFinite(s.papers) &&
				s.papers > 0,
		)
		.map((s) => ({
			term: s.term.trim(),
			kind: s.kind === "mechanism" ? "mechanism" : "asset_class",
			papers: s.papers,
		}));
}

/**
 * Normalized view of the failure for rendering. Never invents a number:
 * a missing count comes back as null and the card omits that clause.
 */
export function describeUnavailable(data) {
	const d = data || {};
	const suggestions = normalizeSuggestions(d.suggestions);
	return {
		reasonCode: typeof d.reason_code === "string" ? d.reason_code : "",
		message: typeof d.message === "string" ? d.message : "",
		steer: typeof d.steer === "string" ? d.steer.trim() : "",
		retrieval: d.retrieval === "lexical" ? "lexical" : "",
		candidatesFound: Number.isFinite(d.candidates_found)
			? d.candidates_found
			: null,
		minPapers: Number.isFinite(d.min_papers) ? d.min_papers : null,
		suggestions,
	};
}

/**
 * The ways forward we are entitled to offer, in the order they are shown.
 *
 * `broaden` needs suggestions behind it — "name an asset class the corpus
 * covers" with no covered classes to name is advice, not a way forward. Both
 * moves are withheld when the corpus itself is unavailable or the LLM backend
 * is down: rewriting the brief cannot fix either, and pretending otherwise
 * sends the user in a circle.
 */
export function waysForward(data) {
	const d = describeUnavailable(data);
	if (d.reasonCode !== CORPUS_TOO_FEW_PAPERS) return [];
	const ways = [];
	if (d.suggestions.length > 0) {
		ways.push({
			id: "broaden",
			title: "Broaden the brief",
			detail:
				"Name an asset class the corpus actually covers. These come from the corpus itself — the number is how many of its papers each term matches:",
			suggestions: d.suggestions,
		});
	}
	ways.push({
		id: "surprise",
		title: "Try Surprise me",
		detail:
			"Drops an example brief from the bank into the box — a different one each press — to start from a steer someone has already written out in full.",
		suggestions: [],
	});
	return ways;
}
