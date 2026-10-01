// The passport's source-papers header (#1769, follow-up to #1739).
//
// The header read "Fused from 5 papers" for a strategy whose five paper refs
// carried ZERO attributed mechanisms — #1739's dual-write landed the column but
// the model emitted no usable `paper_mechanisms`, so every `contribution` cell
// is an em-dash. "Fused from N" turns a citation count into a claim about
// fusion DEPTH: it tells a reader that five papers were synthesized into the
// methodology, when what is on record is that five papers were cited and no
// attribution tying any of them to an element of the spec was ever recorded.
//
// The count that means something is how many cited papers name a mechanism this
// strategy trades. That number can be 0, and when it is, the header has to say
// so — this is #1636's honest-shortfall rule: a shortfall is labelled, never
// hidden and never gated on.
//
// A plain .js module, not JSX, so `node --test` executes it for real (the same
// split strategySpec.js takes — see the note at the top of
// ui/test/passport-dsl.test.js).

/** How many of `papers` carry a non-empty `contribution`. */
function attributedRows(papers) {
	let n = 0;
	for (const p of papers) if (String(p?.contribution ?? "").trim()) n++;
	return n;
}

/** The header for the source-papers panel, or `null` when there are no papers.
 *
 * `distinctMechanismPapers` is the generation pipeline's own count of the same
 * thing (`FusionProposal.distinct_mechanism_papers` — cited ids that survived
 * the server-side spec_elements filter). It is not on the strategy payload
 * today; the argument is honoured so that the header uses it the day it is,
 * without a second change to this logic.
 *
 * When both are available the SMALLER wins. They are two counts of one fact and
 * the failure that matters is overclaiming: the header must never assert more
 * attribution than the table underneath it can show, and must never assert more
 * than the pipeline recorded. Taking the minimum is the only combination that
 * is safe in both directions.
 *
 * `surface` names WHICH panel is about to print the note, because the two
 * panels show different things underneath it (#1796). The passport prints a
 * table with a Contribution column, so its note can point at where the split is
 * visible; the Library's detail panel prints titles and arXiv links and no
 * attribution at all, so the same sentence there would describe a table the
 * reader cannot see. The counts are surface-independent; only the note moves.
 * `surface` is REQUIRED and an unrecognised one THROWS. Neither is pedantry: a
 * default would mean a fourth panel that forgets the argument silently prints
 * the passport's sentence over something else — which is the defect this
 * argument exists to prevent, arriving by omission instead of by typo. The
 * surface is always a literal in the source, never user input, so the throw
 * fires on the first render in dev and test and can never reach a reader.
 */
export function paperAttributionHeader(papers, distinctMechanismPapers, surface) {
	const note = SURFACE_NOTES[surface];
	if (!note) {
		throw new TypeError(
			`paperAttributionHeader: unknown surface ${JSON.stringify(surface)} ` +
				`(expected one of ${Object.keys(SURFACE_NOTES).join(", ")})`,
		);
	}

	const rows = Array.isArray(papers) ? papers : [];
	const cited = rows.length;
	if (cited === 0) return null;

	let attributed = attributedRows(rows);
	if (Number.isInteger(distinctMechanismPapers) && distinctMechanismPapers >= 0) {
		attributed = Math.min(attributed, distinctMechanismPapers);
	}

	const heading =
		`${cited} ${cited === 1 ? "paper" : "papers"} cited · ` +
		`${attributed} ${attributed === 1 ? "names" : "name"} a mechanism this strategy trades`;

	return { cited, attributed, heading, note: note(cited, attributed) };
}

/** The "N papers have no recorded attribution" clause, shared by both surfaces.
 *
 * Phrased as a statement about THE RECORD, never as a completed negative about
 * the strategy — see the long note under `passportNote`. */
function unattributedClause(cited, attributed) {
	const left = cited - attributed;
	return `The remaining ${left} ${
		left === 1 ? "paper has" : "papers have"
	} no recorded mechanism attribution`;
}

/** The sub-line under the heading on the PASSPORT's source-papers table.
 *
 * Never empty: silence after a "0" reads as a rendering bug, and the zero case
 * is the one a reader most needs explained.
 *
 * Every branch below is phrased as a statement about THE RECORD, never as a
 * completed negative about the strategy. "None of these papers was attributed
 * to the spec" asserts that an attribution step ran and came back empty; for a
 * curated row there is no spec at all, and for every generated row written
 * before #1739's `contribution` writer the column was simply never filled. The
 * true statement in both cases is that nothing is recorded — which is also what
 * the passport's own blank-cell footnote says four lines further down
 * ("Blank cells are unrecorded, not zero"). Two sentences in one panel must not
 * disagree about which of those two facts is on the page. */
function passportNote(cited, attributed) {
	if (attributed === 0) {
		return (
			"No per-paper mechanism attribution is recorded for these references — " +
			"the table below cites them; it does not tie any of them to an element this strategy trades."
		);
	}
	if (attributed < cited) {
		return `${unattributedClause(cited, attributed)} — cited, not tied to an element this strategy trades.`;
	}
	return cited === 1
		? "The cited paper is tied to a named element of the spec."
		: "Every cited paper is tied to a named element of the spec.";
}

/** The sub-line under the heading in the LIBRARY's expanded detail panel.
 *
 * Same counts, same record-not-verdict framing — a different second half,
 * because a different thing is underneath it. The passport's sentence says "the
 * table below cites them", and points, by implication, at a Contribution column
 * the reader can scan. The Library panel renders one italic title and an arXiv
 * link per reference and NOTHING else: no contribution cell, no per-paper
 * split, nothing that distinguishes an attributed paper from an unattributed
 * one. Reusing the passport's wording here would describe a table that is not
 * on the page — the same class of defect as the header this PR retired, one
 * surface over.
 *
 * So each branch says what is true of THIS panel: the list identifies the
 * references and shows no attribution either way, and the place the split is
 * actually rendered is the passport's Contribution column. That last clause
 * names where the data IS, not a button on this panel — the Library's detail
 * block carries an "Open Passport" button only when its mount passes
 * `onOpenPassport`, and PublishPage's StrategyTable does not. */
function libraryNote(cited, attributed) {
	if (attributed === 0) {
		return (
			"No per-paper mechanism attribution is recorded for these references — " +
			"the list below cites them; nothing on this panel ties any of them to an element this strategy trades."
		);
	}
	if (attributed < cited) {
		return (
			`${unattributedClause(cited, attributed)} — cited, not tied to an element this strategy trades. ` +
			"The list below shows no attribution either way; the per-paper split is in the passport's Contribution column."
		);
	}
	return (
		(cited === 1
			? "The cited paper is tied to a named element of the spec. "
			: "Every cited paper is tied to a named element of the spec. ") +
		"The list below shows no attribution either way; what each paper contributed is in the passport's Contribution column."
	);
}

/** The surfaces that may print a note, and the note each one prints.
 *
 * Adding a fourth place that shows this header means adding a row here and
 * writing the sentence that is true where it renders — which is the point: the
 * copy stays in the helper, next to the counts it describes, instead of being
 * retyped into a component that cannot see what the other components say. */
const SURFACE_NOTES = {
	passport: passportNote,
	library: libraryNote,
};
