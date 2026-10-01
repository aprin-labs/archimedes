// Claim-integrity guard for the paper-trading track-record copy (#1807).
//
// Until this change, four surfaces told the reader that the settled paper
// ledger is "the track record that carries to mainnet":
//
//   PaperTrading.jsx    "This is the track record that carries to mainnet."
//   StrategyPassport.jsx "building the track record that carries to mainnet."
//   Leaderboard.jsx     "Build your track record now; it carries to mainnet."
//   paperCopy.js        the same sentence, in the comment the other three quote.
//
// The Arc mainnet cutover was CANCELLED by owner call on 2026-08-30
// (issue #1240): Archimedes stays a testnet product until legal/regulatory
// review and sustained traction justify charging real money, and no date is
// named. So the sentence is a promise about an event that is not scheduled —
// false on every surface that carried it, not merely optimistic.
//
// Same idiom as oracle-copy.test.js and roadmap-copy.test.js: a raw source-text
// scan (readFileSync, no JSX parsing) with anti-vacuity coverage — every
// pattern must still match the exact literal this change removed, so a guard
// that stops matching anything fails loudly instead of silently guarding
// nothing.
//
// Why the ban is the WORD and not just the sentence. Scrubbing one phrasing
// buys nothing: the next writer reaches for "ahead of mainnet", "when we go
// live on mainnet", "mainnet-ready". These four files are the paper-trading
// copy surfaces and none of them has any legitimate reason to name mainnet
// while the cutover is cancelled — unlike chain-config.js, Landing.jsx,
// PublicLayout.jsx, Security.jsx and Architecture.jsx, which say "NO mainnet
// money" and are the honest negations we want to keep. So the word is banned
// on these four, with an OWNER-DATED escape hatch: a line carrying
//
//     mainnet-claim-exemption: owner=<name> date=<YYYY-MM-DD> issue=#<n>
//
// is skipped. The marker must be well-formed to count — writing the bare word
// "exemption" in a comment does not silence the guard (proved below), so the
// escape hatch costs an owner, a date and an issue rather than a shrug.
//
// ── Two holes review found in the first cut of this file, and what closed them
//
// 1. THE POSITIVE ASSERTION MATCHED SOURCE, NOT COPY. "the reader is told what
//    the ledger IS" was satisfiable by a line nobody renders: delete the
//    sentence from the <p> and write `// NOTE: the ledger is a paper track
//    record on Arc testnet` above a constant, and the guard went green over a
//    page that says nothing. It also matched RAW text, so the same sentence
//    wrapped across a `'…' + \n '…'` concatenation — the exact form #1805
//    moves this copy into — did not match at all, making the guard's verdict a
//    function of where the author happened to break the line. `readerText`
//    below fixes both: comments out, concatenation flattened, wrapping
//    collapsed. Both properties are pinned by their own fixtures, so a
//    normaliser that quietly stops normalising fails here.
//
// 2. THE ui/src SWEEP LOOKED FOR ONE LITERAL, IN TWO EXTENSIONS. The promise
//    "the sentence cannot migrate to a fifth file" held only for the exact
//    words "carries to mainnet" in a .js/.jsx file: `// your paper ledger is
//    the record that moves to mainnet at cutover.` in Portfolio.jsx was the
//    same promise, rephrased, and passed. The sweep now also flags any line
//    that pairs the WORD mainnet with the ledger vocabulary, over every text
//    extension under ui/src — and, because proving that fix on the #1805 merge
//    showed a line-by-line scan cannot see `'…is the track ' + \n 'record that
//    carries to mainnet…'`, a second sentence-wide pass over the same
//    normalised copy `readerText` produces.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";
import test from "node:test";

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

//: The paper-trading copy surfaces. Every one of these carried the retracted
//: sentence, and none of them has another reason to name mainnet today.
const PAPER_SURFACES = [
	"src/components/PaperTrading.jsx",
	"src/components/StrategyPassport.jsx",
	"src/components/Leaderboard.jsx",
	"src/paperCopy.js",
];

//: The exact literals this change removed, file by file. Used only as
//: anti-vacuity fixtures — if `mainnetMentions` stops flagging these, the
//: scanner is broken and every assertion below is worthless.
const RETRACTED_LITERALS = [
	["PaperTrading.jsx", "This is the\n          track record that carries to mainnet."],
	["StrategyPassport.jsx", "building the track record that carries to mainnet."],
	["Leaderboard.jsx", "Build your track record now;\n                  it carries to mainnet.</>"],
	["paperCopy.js", "// the track record that carries to mainnet; a mark is an unsettled decoration"],
	["a rephrasing the sentence-scrub would have missed", "Deploy now and your ledger is ready ahead of mainnet."],
];

const EXEMPTION_RE = /mainnet-claim-exemption:\s*owner=\S+\s+date=\d{4}-\d{2}-\d{2}\s+issue=#\d+/;

/** Lines that name mainnet without a well-formed owner-dated exemption. */
function mainnetMentions(source) {
	return source
		.split("\n")
		.map((text, i) => ({ line: i + 1, text }))
		.filter(({ text }) => /mainnet/i.test(text) && !EXEMPTION_RE.test(text));
}

//: Comments out. Block comments go whole; a `//` counts as a comment only when
//: it starts a line or follows whitespace, which is what keeps a URL's `://`
//: (and `https://` inside JSX text) from truncating a line of real copy.
//: Deliberately used ONLY by the positive assertion — `mainnetMentions` keeps
//: reading raw source, because a comment promising a mainnet cutover is still
//: a false claim living in the repo and must stay bannable.
function stripComments(source) {
	return source.replace(/\/\*[\s\S]*?\*\//g, " ").replace(/(^|\s)\/\/.*$/gm, "$1");
}

/**
 * The text a reader actually gets, from a source file.
 *
 * Comments stripped, `'…' + '…'` concatenation flattened, and every run of
 * whitespace collapsed to one space, so a phrase check asks "does the copy say
 * this" instead of "did the author wrap the line where I expected". #1805 moves
 * this sentence into a multi-line `export const PAPER_SETTLE_CADENCE`, which
 * splits the phrase across a concatenation boundary; without the flattening
 * step, whether that branch passes this guard is decided by its formatter.
 */
function readerText(source) {
	return stripComments(source)
		.replace(/(['"`])\s*\+\s*(['"`])/g, "")
		.replace(/\s+/g, " ");
}

//: Which files under ui/src the repo-wide sweeps read. The first cut took
//: .js/.jsx only, so the same copy in a .ts, .tsx or .json — ui/src already
//: holds a .json and four .css — would have been invisible to both sweeps.
const SCANNED_EXTENSIONS = /\.(js|jsx|ts|tsx|mjs|cjs|json|css|scss|svg|html)$/;

//: The vocabulary that makes a mainnet mention a claim about the paper record
//: rather than an honest negation. "No mainnet money" (PublicLayout.jsx) and
//: the chain-config cutover comments name mainnet and say nothing about a
//: track record; the sentence #1807 retracted cannot be written without one of
//: these words.
const LEDGER_WORDS = /\b(?:track record|ledger)\b/i;

/** ui/src, as [relative path, source] pairs, over every text extension. */
function uiSourceFiles() {
	const srcRoot = fileURLToPath(repoFile("src"));
	const files = [];
	const walk = (dir) => {
		for (const entry of readdirSync(dir)) {
			const full = path.join(dir, entry);
			if (statSync(full).isDirectory()) walk(full);
			else if (SCANNED_EXTENSIONS.test(entry)) files.push(full);
		}
	};
	walk(srcRoot);
	return files.map((full) => [path.relative(srcRoot, full), readFileSync(full, "utf8")]);
}

/** Lines pairing the word mainnet with the ledger vocabulary, unexempted. */
function ledgerMainnetPairs(source) {
	return source
		.split("\n")
		.map((text, i) => ({ line: i + 1, text }))
		.filter(({ text }) => /\bmainnet\b/i.test(text) && LEDGER_WORDS.test(text) && !EXEMPTION_RE.test(text));
}

//: The same pairing, one sentence wide, over `readerText` rather than raw lines.
//: The line scan above cannot see a claim the author wrapped: on the #1805 merge
//: the constant reads `'…is the track ' + \n 'record that carries to mainnet…'`,
//: and neither line holds both halves. Sentence-bounded (`[^.!?]`) so this stays a
//: claim-shaped match and not "this file says mainnet somewhere and ledger
//: somewhere". The window stops at `<>{}` as well as at `.!?`, so a tag or a JSX
//: expression between the two halves ends the sentence — that is what keeps the
//: honesty table in Architecture.jsx (`<td>Mainnet, real funds</td>` two rows
//: below a `<LedgerStatus>`) from reading as one claim. `\b` on both halves is
//: what keeps the component name `LedgerStatus` from counting as the word
//: "ledger". Comments are gone by the time `readerText` is done, which is both
//: why exemptions need no special case here (an exempted comment is not copy) and
//: why this scan is about the rendered page only — the raw line scan is what
//: covers comments.
const SENTENCE_PAIR_RE =
	/\bmainnet\b[^.!?<>{}]{0,140}\b(?:track record|ledger)\b|\b(?:track record|ledger)\b[^.!?<>{}]{0,140}\bmainnet\b/i;

/** The claim a line-by-line scan cannot see, or null. */
function wrappedLedgerMainnetClaim(source) {
	const match = SENTENCE_PAIR_RE.exec(readerText(source));
	return match ? match[0] : null;
}

test("mainnetMentions flags every literal this change removed", () => {
	for (const [where, literal] of RETRACTED_LITERALS) {
		assert.ok(
			mainnetMentions(literal).length > 0,
			`the scanner no longer flags the ${where} literal ${JSON.stringify(literal)} — it is guarding nothing`,
		);
	}
});

test("an owner-dated exemption is honoured, and a hand-waved one is not", () => {
	const exempt = "// mainnet-claim-exemption: owner=dbrowneup date=2026-09-03 issue=#1807 — mainnet named on purpose";
	assert.deepEqual(mainnetMentions(exempt), [], "a well-formed exemption must silence the line");

	for (const sloppy of [
		"// mainnet-claim-exemption: this one is fine, trust me — mainnet",
		"// exemption granted: mainnet",
		"// mainnet-claim-exemption: owner=dbrowneup issue=#1807 — mainnet",
		"// mainnet-claim-exemption: owner=dbrowneup date=soon issue=#1807 — mainnet",
	]) {
		assert.equal(
			mainnetMentions(sloppy).length,
			1,
			`a marker missing an owner, a date or an issue must NOT silence the line: ${sloppy}`,
		);
	}
});

test("readerText reads what ships, not what the source happens to contain", () => {
	// Fixture 1 is the mutation that found the hole: the visible sentence gone,
	// a comment left in its place. Green here would mean the "no hole is left"
	// assertion below can be satisfied by a line nobody renders.
	const commentOnly = '// NOTE: the ledger is a paper track record on Arc testnet — no real funds.\nconst MARKS_POLL_MS = 900_000';
	assert.ok(
		!/paper track record on Arc testnet/.test(readerText(commentOnly)),
		"a claim that lives only in a comment must not count as copy the reader was shown",
	);
	assert.ok(
		/paper track record on Arc testnet/.test(commentOnly),
		"fixture is vacuous: the raw text must contain the phrase, or stripping proves nothing",
	);

	// Fixture 2 is #1805's shape: one sentence, two string literals, a line
	// break between them. Both wraps below say the same thing to a reader and
	// must say the same thing to this guard.
	for (const wrapped of [
		"export const PAPER_SETTLE_CADENCE =\n\t'…that settled series is a paper ' +\n\t'track record on Arc testnet, with no real funds.'",
		"export const PAPER_SETTLE_CADENCE =\n\t'…that settled series is ' +\n\t'a paper track record on Arc testnet, with no real funds.'",
	]) {
		assert.ok(
			/paper track record on Arc testnet/.test(readerText(wrapped)),
			`a sentence wrapped across a concatenation must still read as one sentence: ${wrapped}`,
		);
	}

	// And flattening must not invent the phrase where the copy does not say it.
	assert.ok(
		!/paper track record on Arc testnet/.test(
			readerText("export const X =\n\t'that settled series is the track ' +\n\t'record that carries to mainnet.'"),
		),
		"the normaliser must not manufacture a match the copy never made",
	);
});

test("ledgerMainnetPairs flags a rephrasing that never says \"carries to mainnet\"", () => {
	// The literal that passed the first cut of the ui/src sweep, in a fifth file.
	const rephrased = "// Onboarding blurb: your paper ledger is the record that moves to mainnet at cutover.";
	assert.equal(ledgerMainnetPairs(rephrased).length, 1, "the pair scan is guarding nothing");
	assert.deepEqual(
		ledgerMainnetPairs('<span>No mainnet money</span>'),
		[],
		"the honest negations name mainnet without claiming anything about the record — they must stay green",
	);
	assert.deepEqual(
		ledgerMainnetPairs(`${rephrased} mainnet-claim-exemption: owner=dbrowneup date=2026-09-03 issue=#1807`),
		[],
		"the owner-dated escape hatch applies here too",
	);

	// The wrap that defeats a line-by-line scan: #1805's own constant, as it
	// merges. Neither line holds both halves of the claim; the sentence does.
	const wrapped =
		"export const PAPER_SETTLE_CADENCE =\n\t'…that settled series is the track ' +\n\t'record that carries to mainnet.'";
	assert.deepEqual(ledgerMainnetPairs(wrapped), [], "fixture is vacuous: no single line may hold both halves");
	assert.ok(
		wrappedLedgerMainnetClaim(wrapped),
		"a claim split across a line wrap must still be caught — the line scan provably cannot see this one",
	);
	assert.equal(
		wrappedLedgerMainnetClaim("<span>No mainnet money</span>\n<p>Your paper ledger is append-only.</p>"),
		null,
		"two unrelated sentences in one file are not a claim — this scan is sentence-bounded on purpose",
	);
});

test("no paper-trading surface names mainnet (#1807, cutover cancelled by #1240)", () => {
	for (const rel of PAPER_SURFACES) {
		const found = mainnetMentions(readFileSync(repoFile(rel), "utf8"));
		assert.deepEqual(
			found,
			[],
			`ui/${rel} names mainnet on: ` +
				found.map(({ line, text }) => `${line}: ${text.trim()}`).join(" | ") +
				". The Arc mainnet cutover is cancelled (#1240) — say what is true today (a paper " +
				"track record on Arc testnet, no real funds), or add an owner-dated " +
				"`mainnet-claim-exemption: owner=<name> date=<YYYY-MM-DD> issue=#<n>` on the line.",
		);
	}
});

test('no file under ui/src says the ledger "carries to mainnet"', () => {
	// The narrow phrase, repo-wide across the UI, so the sentence cannot simply
	// migrate to a fifth file that is not on the list above. No exemption here
	// on purpose: this is the retracted sentence itself, and there is no
	// version of the product in which it is true.
	const offenders = uiSourceFiles()
		.filter(([, source]) => /carries to mainnet/i.test(source) || /carries to mainnet/i.test(readerText(source)))
		.map(([rel]) => rel);
	assert.deepEqual(offenders, [], `these ui/src files still say "carries to mainnet": ${offenders.join(", ")}`);
});

test("no file under ui/src pairs mainnet with the paper ledger", () => {
	// The rephrasing the narrow phrase misses: "the record that moves to
	// mainnet", "your ledger carries over at cutover". Any line naming mainnet
	// AND the record is a claim about the cancelled cutover unless an owner
	// says otherwise, on that line, with a date and an issue.
	const offenders = [];
	for (const [rel, source] of uiSourceFiles()) {
		for (const { line, text } of ledgerMainnetPairs(source)) {
			offenders.push(`${rel}:${line}: ${text.trim()}`);
		}
		//: Second pass, sentence-wide over the rendered copy, because the scan
		//: above is line-by-line and #1805's constant splits this exact claim
		//: across a `' + \n '` boundary. No line number to give here — the claim
		//: does not live on one.
		const wrapped = wrappedLedgerMainnetClaim(source);
		if (wrapped && !offenders.some((o) => o.startsWith(`${rel}:`))) {
			offenders.push(`${rel} (wrapped across lines): ${wrapped.trim()}`);
		}
	}
	assert.deepEqual(
		offenders,
		[],
		"these ui/src lines pair mainnet with the paper record, which the cancelled cutover (#1240) " +
			`makes a promise about an unscheduled event: ${offenders.join(" | ")}. Say what is true today, or ` +
			"add an owner-dated `mainnet-claim-exemption: owner=<name> date=<YYYY-MM-DD> issue=#<n>` on the line.",
	);
});

test("the paper-trading copy says what is true instead: Arc testnet, no real funds", () => {
	// A retraction that leaves a hole is not a fix — the reader still has to be
	// told what the ledger IS. Checked over PaperTrading.jsx + paperCopy.js
	// together because the intro copy legitimately lives in either one (#1805
	// moves it into a pinned constant in paperCopy.js), and through `readerText`
	// so that neither a comment nobody renders nor a line wrap can decide it.
	//: `assert.ok` rather than `assert.match` on purpose — a failing `assert.match`
	//: prints the entire file it was handed, which buries the one sentence the
	//: reader needs to see.
	const intro = readerText(
		readFileSync(repoFile("src/components/PaperTrading.jsx"), "utf8") +
			"\n" +
			readFileSync(repoFile("src/paperCopy.js"), "utf8"),
	);
	const passport = readerText(readFileSync(repoFile("src/components/StrategyPassport.jsx"), "utf8"));

	for (const [where, source] of [
		["PaperTrading.jsx + paperCopy.js", intro],
		["StrategyPassport.jsx", passport],
	]) {
		for (const phrase of [/paper track record on Arc testnet/, /no real funds/]) {
			assert.ok(
				phrase.test(source),
				`${where} no longer says ${phrase} — the mainnet claim was scrubbed and nothing true ` +
					"replaced it. A retraction that leaves a hole still leaves the reader guessing what " +
					"the paper ledger is. (Checked on rendered copy: comments and line wrapping are " +
					"normalised away, so a note in a comment does not count.)",
			);
		}
	}
});
