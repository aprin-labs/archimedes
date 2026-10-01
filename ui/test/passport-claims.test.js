// #1769 — the two claims the passport was making that its own data did not
// support.
//
// 1. "FUSED FROM 5 PAPERS" over five paper refs with ZERO attributed
//    mechanisms. #1739 landed the `contribution` column; the model emitted no
//    usable `paper_mechanisms`, so every cell is an em-dash — and the header
//    still told the reader those five papers had been fused into the
//    methodology. A citation count is not fusion depth.
// 2. A Backtest card printing Sharpe 0.70 and a Rigor card printing Deflated
//    Sharpe 0.08 beside a verdict pill, with no sentence anywhere saying they
//    are the same edge measured twice.
//
// The helpers live in plain .js modules so `node --test` executes them for
// real; the .jsx that consumes them is pinned by source-structure assertions,
// the convention the rest of this suite uses (see the note at the top of
// ui/test/passport-dsl.test.js).

import assert from "node:assert/strict";
import {
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { pathToFileURL } from "node:url";

import { paperAttributionHeader } from "../src/paperAttribution.js";
import {
	hasSharpeReconciliation,
	rigorBarClause,
} from "../src/sharpeReconciliation.js";

const passport = readFileSync(
	new URL("../src/components/StrategyPassport.jsx", import.meta.url),
	"utf8",
);

const papers = (n, attributed = 0) =>
	Array.from({ length: n }, (_, i) => ({
		arxiv_id: `2401.0000${i}`,
		title: `Paper ${i}`,
		contribution: i < attributed ? "supplies the 200-day trend filter" : null,
	}));

/** `paperAttributionHeader` on the PASSPORT surface.
 *
 * `surface` is a required argument with no default (#1796): the counts are the
 * same everywhere, the sentence under them is not, and a default would let a
 * fourth panel print the passport's sentence over something the passport does
 * not describe just by forgetting to say where it is. The assertions below all
 * predate that argument and all mean "on the passport", so they say it once
 * here rather than threading `undefined, "passport"` through twenty calls. The
 * requirement itself is pinned by its own test further down.
 */
const header = (rows, distinctMechanismPapers, surface = "passport") =>
	paperAttributionHeader(rows, distinctMechanismPapers, surface);

// ── The paper header ──────────────────────────────────────────────────────

test("the header counts citations and attributions separately", () => {
	// The exact strategy from the issue: 5 refs, 0 contributions.
	const h = header(papers(5, 0));
	assert.equal(
		h.heading,
		"5 papers cited · 0 name a mechanism this strategy trades",
	);
	assert.equal(h.cited, 5);
	assert.equal(h.attributed, 0);
});

test("zero attributions gets its own sentence, not silence", () => {
	// A bare "· 0" with nothing after it reads as a rendering bug. The zero
	// case is the one a reader most needs told, and it is the honest-shortfall
	// rule (#1636): label it, never hide it and never gate on it.
	const h = header(papers(5, 0));
	assert.match(h.note, /No per-paper mechanism attribution is recorded/);
	assert.match(h.note, /does not tie any of them to an element this strategy trades/);
});

test("the zero sentence claims nothing was RECORDED, never that nothing was found", () => {
	// MUTATION: restore "None of the cited papers was attributed to any element
	// of this strategy's spec". That asserts an attribution step ran and came
	// back empty. Curated rows have no spec at all, and every generated row
	// written before #1739's `contribution` writer simply has an empty column —
	// so the completed negative is false on both, and it contradicts the
	// blank-cell footnote ("unrecorded, not zero") in the same panel.
	for (const n of [1, 2, 5]) {
		const note = header(papers(n, 0)).note;
		assert.doesNotMatch(note, /was attributed to/);
		assert.doesNotMatch(note, /None of the cited papers/);
		assert.match(note, /recorded/);
	}
	// The partial branch is the same claim with a smaller subject and must use
	// the same framing.
	const partial = header(papers(3, 2)).note;
	assert.doesNotMatch(partial, /without an attributed mechanism/);
	assert.match(partial, /no recorded mechanism attribution/);
});

test("a partial attribution says how many are left over", () => {
	const h = header(papers(3, 2));
	assert.equal(
		h.heading,
		"3 papers cited · 2 name a mechanism this strategy trades",
	);
	assert.match(h.note, /The remaining 1 paper has no recorded mechanism attribution/);
	assert.match(
		header(papers(5, 2)).note,
		/The remaining 3 papers have no recorded mechanism attribution/,
	);
});

test("the header reads as English at every count", () => {
	// Singular/plural on BOTH halves, including the "1 names" agreement that a
	// naive `${n} name` gets wrong.
	assert.equal(
		header(papers(1, 1)).heading,
		"1 paper cited · 1 names a mechanism this strategy trades",
	);
	assert.equal(
		header(papers(1, 0)).heading,
		"1 paper cited · 0 name a mechanism this strategy trades",
	);
	assert.equal(
		header(papers(2, 2)).heading,
		"2 papers cited · 2 name a mechanism this strategy trades",
	);
	assert.match(
		header(papers(2, 2)).note,
		/Every cited paper is tied to a named element of the spec\./,
	);
});

test("the pipeline's own count can only ever LOWER the claim", () => {
	// `distinct_mechanism_papers` and the rendered contribution cells are two
	// counts of one fact. The failure that matters is overclaiming, so the
	// smaller wins in both directions: the header must never assert more
	// attribution than the table under it can show, nor more than the pipeline
	// recorded.
	assert.equal(header(papers(5, 4), 1).attributed, 1);
	assert.equal(header(papers(5, 1), 4).attributed, 1);
	// Absent / malformed leaves the rendered count alone rather than zeroing it.
	assert.equal(header(papers(5, 3), undefined).attributed, 3);
	assert.equal(header(papers(5, 3), null).attributed, 3);
	assert.equal(header(papers(5, 3), "2").attributed, 3);
});

test("whitespace is not an attribution", () => {
	// `contribution` is model-authored text through a nullable column; a "  "
	// would otherwise count as a named mechanism.
	const rows = papers(2, 0);
	rows[0].contribution = "   ";
	rows[1].contribution = "";
	assert.equal(header(rows).attributed, 0);
});

test("no papers means no header at all", () => {
	assert.equal(header([]), null);
	assert.equal(header(null), null);
	assert.equal(header(undefined), null);
});

test("the passport renders the honest header and not the old one", () => {
	// MUTATION: restore the old header — put
	// `` {rows.length === 1 ? "Source paper" : `Fused from ${rows.length} papers`} ``
	// back in PapersTable — and the first two assertions fail.
	//
	// Matched on the RENDER forms (an interpolated template, and the chip's
	// `{expr} fused papers`) rather than on the bare phrase, because the module
	// comment above `PapersTable` quotes the retired header to explain why it
	// went. A test that forbade the words would forbid saying what was fixed.
	assert.doesNotMatch(passport, /Fused from \$\{/);
	assert.doesNotMatch(passport, /\}\s*fused papers/);
	assert.match(
		passport,
		/paperAttributionHeader\(rows, distinctMechanismPapers, "passport"\)/,
	);
	assert.match(passport, /\{attribution\.heading\}/);
	assert.match(passport, /\{attribution\.note\}/);
	// The sub-line is no longer conditional on a multi-paper row: it used to be
	// "One methodology synthesized from every row below", which is the same
	// overclaim written as a sentence.
	assert.doesNotMatch(
		passport,
		/One methodology synthesized from every row below/,
	);
	// The header chip beside the title carried the identical claim.
	assert.match(passport, /\{s\.papers\.length\} papers cited/);
	// The pipeline's own count is threaded from the payload, so the day the API
	// serves it the header uses it without a second change.
	assert.match(passport, /distinctMechanismPapers=\{s\.distinct_mechanism_papers\}/);
});

// ── The raw-vs-deflated sentence ──────────────────────────────────────────

test("the sentence needs BOTH numbers or it does not render", () => {
	// Its whole subject is the relationship between them; with one in hand
	// there is nothing to reconcile and half a sentence would have to invent
	// the missing side.
	assert.equal(
		hasSharpeReconciliation({ sharpe_ratio: 0.7, deflated_sharpe_ratio: 0.08 }),
		true,
	);
	assert.equal(
		hasSharpeReconciliation({ sharpe_ratio: 0.7, deflated_sharpe_ratio: null }),
		false,
	);
	assert.equal(
		hasSharpeReconciliation({ sharpe_ratio: null, deflated_sharpe_ratio: 0.08 }),
		false,
	);
	assert.equal(hasSharpeReconciliation({}), false);
	assert.equal(hasSharpeReconciliation(null), false);
	// A zero is a measurement, not an absence.
	assert.equal(
		hasSharpeReconciliation({ sharpe_ratio: 0, deflated_sharpe_ratio: 0 }),
		true,
	);
	// NaN/Infinity reach JSON as null, but a coerced row could carry them.
	assert.equal(
		hasSharpeReconciliation({ sharpe_ratio: NaN, deflated_sharpe_ratio: 0.08 }),
		false,
	);
	assert.equal(
		hasSharpeReconciliation({ sharpe_ratio: 0.7, deflated_sharpe_ratio: Infinity }),
		false,
	);
});

test("the bar clause reads the four-state verdict, never the boolean alone", () => {
	// #1358's bug class: `passes_rigor_gate` is false for "pending" and
	// "degenerate" too, so keying the sentence on the boolean would tell a user
	// their ungraded strategy had lost.
	assert.match(rigorBarClause({ rigor_gate_status: "pass" }), /clears the Archimedes Verified bar/);
	assert.match(rigorBarClause({ rigor_gate_status: "fail" }), /sits below the Archimedes Verified bar/);
	assert.match(
		rigorBarClause({ rigor_gate_status: "pending", passes_rigor_gate: false }),
		/has not graded this strategy yet/,
	);
	assert.doesNotMatch(
		rigorBarClause({ rigor_gate_status: "pending", passes_rigor_gate: false }),
		/below/,
	);
	assert.match(
		rigorBarClause({ rigor_gate_status: "degenerate", passes_rigor_gate: false }),
		/nothing to grade/,
	);
	assert.doesNotMatch(
		rigorBarClause({ rigor_gate_status: "degenerate", passes_rigor_gate: false }),
		/below/,
	);
});

test("an unreadable verdict produces no clause at all", () => {
	// The em-dash rule (#1326) applied to prose: for a status this build does
	// not know, no verdict beats a guessed one. The sentence still renders —
	// the raw-vs-deflated half is true regardless — it just stops there.
	assert.equal(rigorBarClause({ rigor_gate_status: "probationary" }), null);
	assert.equal(rigorBarClause({}), null);
	assert.equal(rigorBarClause(null), null);
	// Rows coerced from the generated feed carry no status; the boolean is the
	// documented fallback for exactly those.
	assert.match(rigorBarClause({ passes_rigor_gate: true }), /clears/);
	assert.match(rigorBarClause({ passes_rigor_gate: false }), /sits below/);
});

test("the passport renders the sentence under the backtest block", () => {
	// MUTATION: delete the `hasSharpeReconciliation(s) && (...)` block — the
	// two Sharpes go back to sitting in separate cards with nothing connecting
	// them, and every assertion here fails.
	assert.match(passport, /hasSharpeReconciliation\(s\) && \(/);
	assert.match(passport, /is the raw backtest figure; deflated for selection bias it is/);
	// The four signals named here are the four `run_rigor_gate` actually ORs
	// into `passes_all` (rigor_evaluator.gate_details) and the same four the
	// Rigor card's own explainer lists. "grades the deflated Sharpe" alone
	// would have been a narrower claim than the gate makes.
	assert.match(
		passport,
		/reads that deflation — not the raw\s*\n?\s*Sharpe — alongside PBO, out-of-sample Sharpe and the\s*\n?\s*look-ahead audit/,
	);
	assert.match(passport, /rigorBarClause\(s\)/);
});

test("the sentence introduces no number of its own", () => {
	// The rule for this sentence: every figure in it is one the payload already
	// carries and the page already prints. A hardcoded threshold (the 0.90
	// level-1 `dsr_p_min`, say) would be a fourth number no API field backs —
	// and it would be a claim about a p-value pinned next to two Sharpes, which
	// is the confusion this sentence exists to remove.
	const block = passport.slice(
		passport.indexOf("hasSharpeReconciliation(s) && ("),
		passport.indexOf("{(s.backtest_start || s.backtest_end) && ("),
	);
	assert.ok(block.length > 200, "the reconciliation block was not located");
	assert.doesNotMatch(block, /\d+\.\d+/);
	// Both figures go through the shared renderer like every other metric on
	// this page (#1651) — no local formatting, in-domain or not.
	assert.match(block, /metric="sharpe_ratio"/);
	assert.match(block, /metric="deflated_sharpe_ratio"/);
});

// ── The Library carried the same header, three times (#1796) ──────────────
//
// `ui/src/components/Strategies.jsx` said "Fused from N papers" in three
// places — the desktop row's chip tooltip, the expanded detail panel's
// heading, and the mobile card's chip tooltip. #1783 left the file alone on
// purpose (it belonged to the concurrent stored-verdict PR #1792), so the
// overclaim the passport lost survived one route away from it, on the surface
// most users reach first.

const strategies = readFileSync(
	new URL("../src/components/Strategies.jsx", import.meta.url),
	"utf8",
);

test("the surface is required, and an unknown one throws rather than guessing", () => {
	// MUTATION: give `surface` a `= "passport"` default and the first two
	// assertions go red. The default is what would let a fourth panel print the
	// passport's sentence over a layout the passport does not describe — the
	// defect below, arriving by omission instead of by typo. The counts are
	// surface-independent; the sentence under them is not.
	assert.throws(() => paperAttributionHeader(papers(2, 0)), /unknown surface/);
	assert.throws(
		() => paperAttributionHeader(papers(2, 0), undefined, "libary"),
		/unknown surface/,
	);
	// It throws before the empty-list short-circuit, so a typo'd surface cannot
	// hide behind a strategy that happens to have no papers.
	assert.throws(() => paperAttributionHeader([], undefined, "nope"), /unknown surface/);
	// Both real surfaces are accepted and count identically.
	for (const surface of ["passport", "library"]) {
		const h = paperAttributionHeader(papers(5, 2), undefined, surface);
		assert.equal(h.heading, "5 papers cited · 2 name a mechanism this strategy trades");
	}
});

test("the Library's sub-line describes the Library's panel, not the passport's table", () => {
	// THE DEFECT THIS CLOSES. The first cut reused the passport's note verbatim
	// in the Library detail panel. That sentence says "the table below cites
	// them" and points, by implication, at a Contribution column the reader can
	// scan. The Library panel renders one italic title and an arXiv link per
	// reference and nothing else — no contribution cell, no per-paper split. A
	// sentence describing a table that is not on the page is the same class of
	// claim defect as the header this PR retired, one surface over.
	//
	// MUTATION: point SURFACE_NOTES.library at `passportNote` and every
	// assertion below goes red.
	for (const [cited, attributed] of [
		[5, 0],
		[5, 2],
		[5, 5],
	]) {
		const rows = papers(cited, attributed);
		const lib = paperAttributionHeader(rows, undefined, "library").note;
		const pass = paperAttributionHeader(rows, undefined, "passport").note;
		assert.notEqual(lib, pass);
		// No table and no Contribution column on this panel — so the note may
		// not name one as if it were underneath.
		assert.doesNotMatch(lib, /table below/);
		assert.match(lib, /list below/);
		// The record-not-verdict framing #1783 settled on is surface-independent
		// and survives the rewrite (same rule as the passport branch above).
		assert.doesNotMatch(lib, /was attributed to/);
		assert.doesNotMatch(lib, /None of the cited papers/);
		assert.match(lib, /tied to|recorded/);
	}

	// Zero: what this panel shows is the citation, and nothing else.
	assert.match(
		paperAttributionHeader(papers(5, 0), undefined, "library").note,
		/the list below cites them; nothing on this panel ties any of them to an element this strategy trades/,
	);
	assert.match(
		paperAttributionHeader(papers(5, 0), undefined, "library").note,
		/No per-paper mechanism attribution is recorded/,
	);
	// Partial and full: the split EXISTS, it is just not rendered here — so the
	// note says where it is rather than implying this list shows it.
	for (const [cited, attributed] of [
		[5, 2],
		[5, 5],
	]) {
		const lib = paperAttributionHeader(papers(cited, attributed), undefined, "library").note;
		assert.match(lib, /shows no attribution either way/);
		assert.match(lib, /passport's Contribution column/);
	}
	// The count itself is still spelled out, in the passport's own words.
	assert.match(
		paperAttributionHeader(papers(5, 2), undefined, "library").note,
		/The remaining 3 papers have no recorded mechanism attribution/,
	);
});

/** `src` with WHOLE-LINE comments dropped.
 *
 * The phrase has to stay sayable: three files now explain the retired header
 * by quoting it, and a guard that forbade the words outright would forbid
 * saying what was fixed — the reason the passport assertions above match
 * render forms instead of the bare phrase. Dropping only whole-line comments
 * keeps that escape hatch as narrow as it can be: a trailing `// Fused from …`
 * on a line of JSX still trips the guard. That is the safe direction to be
 * wrong in — a false red is a sentence to rewrite, a false green is the
 * overclaim back on the page.
 */
const withoutComments = (src) =>
	src
		.split("\n")
		.filter((line) => !/^\s*(\/\/|\/\*|\*)/.test(line))
		.join("\n");

/** The retired claim, in any casing and across any run of whitespace.
 *
 * A literal, case-sensitive `/Fused from/` is a guard against ONE spelling of
 * the overclaim, not against the overclaim: `Fused  from` (two spaces, which
 * is what a reformat or a careless retype produces), `fused from` inside a
 * template literal, and a line break between the two words all read the same
 * on screen and all walked straight past it. `\s+` also covers the newline
 * case, since the sweep matches against the whole file rather than line by
 * line.
 */
const FUSION_DEPTH_CLAIM = /fused\s+from/i;

const presentsCountAsFusionDepth = (src) =>
	FUSION_DEPTH_CLAIM.test(withoutComments(src));

/** Every .js/.jsx file under `dir`, recursively. */
function jsSources(dir) {
	const out = [];
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const child = new URL(entry.name + (entry.isDirectory() ? "/" : ""), dir);
		if (entry.isDirectory()) out.push(...jsSources(child));
		else if (/\.jsx?$/.test(entry.name)) out.push(child);
	}
	return out;
}

/** The files under `root` whose rendered source presents a citation count as
 * fusion depth, named relative to `root`. */
function fusionDepthOffenders(root) {
	const offenders = [];
	for (const file of jsSources(root)) {
		if (presentsCountAsFusionDepth(readFileSync(file, "utf8"))) {
			offenders.push(decodeURIComponent(file.pathname.slice(root.pathname.length)));
		}
	}
	return offenders.sort();
}

test("no rendered string in ui/src presents a citation count as fusion depth", () => {
	// MUTATION: put `` title={`Fused from ${s.papers.length} papers`} `` back on
	// either Strategies.jsx chip, or `Fused from {s.papers.length} papers` back
	// in its detail-panel heading, and this goes red naming the file.
	//
	// Swept over the whole tree rather than the two files that had the phrase:
	// this issue IS the fourth-copy failure mode — one wording fixed in one
	// component while three copies of it sat in another — so the guard has to
	// cover the copies nobody has written yet.
	assert.deepEqual(fusionDepthOffenders(new URL("../src/", import.meta.url)), []);
});

test("the sweep catches the copy a literal `Fused from` walks past", () => {
	// MUTATION: narrow FUSION_DEPTH_CLAIM back to /Fused from/ and the first
	// three of these go green — i.e. the guard passes with the overclaim on the
	// page. Every one of them renders as the retired sentence.
	for (const src of [
		"title={`Fused from ${s.papers.length} papers`}",
		"title={`Fused  from ${s.papers.length} papers`}",
		'<div className="label">fused from {n} papers</div>',
		"<div>Fused\n  from {n} papers</div>",
	]) {
		assert.ok(presentsCountAsFusionDepth(src), src);
	}

	// The escape hatch stays exactly as wide as it was: WHOLE-LINE comments,
	// which is how three files explain the retired header by quoting it.
	for (const src of [
		'// It used to read "Fused from N papers" — retired by #1783.',
		' * "Fused from N" turns a citation count into a claim about depth.',
	]) {
		assert.ok(!presentsCountAsFusionDepth(src), src);
	}
	// A TRAILING comment is not an escape hatch. False red: a sentence to
	// rewrite. False green: the overclaim back on the page.
	assert.ok(presentsCountAsFusionDepth("<Chip /> // Fused from N papers"));
});

test("the sweep descends into nested component directories", () => {
	// MUTATION: drop the `if (entry.isDirectory())` recursion in jsSources and
	// this goes red. A copy planted at ui/src/components/nested/FourthCopy.jsx
	// is two levels down; a sweep that only reads the top of the tree reports
	// a clean [] over it, which is the worst possible failure for this guard.
	const root = mkdtempSync(join(tmpdir(), "fusion-sweep-"));
	try {
		mkdirSync(join(root, "components", "nested"), { recursive: true });
		writeFileSync(
			join(root, "components", "nested", "FourthCopy.jsx"),
			"export const C = () => <span>Fused  from {n} papers</span>;\n",
		);
		writeFileSync(join(root, "clean.js"), "export const ok = 1;\n");
		// Not a source file: the sweep must not widen into docs and fixtures,
		// where quoting the retired phrase is the whole point.
		writeFileSync(join(root, "components", "notes.md"), "Fused from 5 papers\n");
		assert.deepEqual(fusionDepthOffenders(pathToFileURL(`${root}/`)), [
			"components/nested/FourthCopy.jsx",
		]);
	} finally {
		rmSync(root, { recursive: true, force: true });
	}
});

test("all three Library sites read #1783's helper", () => {
	assert.match(
		strategies,
		/import \{ paperAttributionHeader \} from '\.\.\/paperAttribution\.js'/,
	);

	// Sites 1 and 3 — the desktop row's chip and the mobile card's — are ONE
	// component mounted twice. Two hand-written copies of a pill is how the
	// tooltip came to disagree with the passport in the first place; the
	// component is what makes a fourth wording impossible to add to one layout
	// and forget in the other.
	assert.match(strategies, /function PapersCitedChip\(/);
	assert.equal((strategies.match(/<PapersCitedChip/g) || []).length, 2);
	assert.match(
		strategies,
		/paperAttributionHeader\(papers, distinctMechanismPapers, 'library'\)/,
	);
	// The chip is a COUNT and says what it counts, in the passport's own words
	// (StrategyPassport.jsx's "{s.papers.length} papers cited", pinned above).
	// The split it cannot fit goes in the tooltip — the attribute the old copy
	// made its claim in.
	assert.match(strategies, /\{attribution\.cited\} papers cited/);
	assert.match(strategies, /title=\{attribution\.heading\}/);
	// Both layouts thread the pipeline's own count, so the day the Library
	// payload carries it the chips lower their claim without a second change.
	assert.equal(
		(strategies.match(/distinctMechanismPapers=\{s\.distinct_mechanism_papers\}/g) || [])
			.length,
		2,
	);

	// Site 2 — the expanded detail panel, one component behind both layouts.
	//
	// Pinned as the RENDERED PAIR, not as a bare `{attribution.heading}`: the
	// chip's `title={attribution.heading}` three hundred lines up satisfies a
	// bare match on its own, so restoring `Fused from {s.papers.length} papers`
	// in this heading left THIS test green and only the sweep red — a test
	// named "all three sites" that could not see one of the three regress.
	// MUTATION: restore that heading, or drop the note line, and this fails.
	assert.match(
		strategies,
		/const attribution = paperAttributionHeader\(s\.papers, s\.distinct_mechanism_papers, 'library'\)/,
	);
	assert.match(
		strategies,
		/<div className="label mb-2">\{attribution\.heading\}<\/div>\s*\n\s*<p className="caption mb-2">\{attribution\.note\}<\/p>/,
	);
	// The note is not optional here either: a heading ending "· 0" with silence
	// under it reads as a rendering bug, and zero is the case a reader most
	// needs told (#1636).
	assert.match(strategies, /\{attribution\.note\}/);
});
