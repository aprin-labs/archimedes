// Corpus catalog search: the box must say what it searches (#1451).
//
// Catalog search is a case-insensitive substring match over three text columns
// — title, abstract and, as of #1451, the serialised author list. It is
// LEXICAL. There are no embeddings behind /api/papers/: `corpus_meta` is 0
// rows and the papers table has no embedding column, so any copy that reads as
// semantic / similarity / "smart" search is a false claim on a public page.
//
// Three things are pinned here:
//
//   1. the input names authors, so the author leg is discoverable rather than
//      an invisible backend behaviour;
//   2. a scope sentence exists, is the input's accessible description, and
//      names all three columns;
//   3. no part of that copy claims semantic retrieval or ranking.
//
// Plus the acceptance criterion #1451 attached to the result count: `total`
// comes from the response, never recomputed from `papers.length` (a page, not
// a total).
//
// Same idiom as corpus-kg-tab-gate.test.js / oracle-copy.test.js: a raw
// source-text scan with anti-vacuity coverage — every pattern is also run
// against the canonical string it exists to reject, so a pattern that has
// stopped matching anything fails loudly instead of guarding nothing.

import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

const explorer = readFileSync(repoFile("src/components/CorpusExplorer.jsx"), "utf8");
const css = readFileSync(repoFile("src/App.css"), "utf8");

// Block comments (including JSX `{/* */}`) are stripped before the
// false-claim scan: the rule is about copy a visitor reads, and the source
// legitimately explains *why* there are no embeddings. `//` comments are left
// alone because stripping them would also eat `https://` inside a string.
const explorerCopy = explorer.replace(/\/\*[\s\S]*?\*\//g, "");

// The exact pre-fix markup. Every guard below is checked against it, so a
// guard can never pass by having stopped matching the real source.
const PRE_FIX_INPUT = `<input
          type="text" placeholder="Search papers..." value={search}
          aria-label="Search papers"
          onChange={e => { setSearch(e.target.value); setPage(1) }}
          className="catalog-search"
        />`;

const NAMES_AUTHORS = /placeholder="[^"]*authors[^"]*"/i;
const LABEL_NAMES_AUTHORS = /aria-label="[^"]*author[^"]*"/i;
const DESCRIBED_BY = /aria-describedby="catalog-search-scope"/;
const SCOPE_SENTENCE = /id="catalog-search-scope"[\s\S]{0,400}?<\/p>/;

// Words that would turn an honest substring match into a claim the corpus
// cannot back. "no ranking" is allowed — a negation is not a claim.
const SEMANTIC_CLAIM = /\b(semantic|embedding|embeddings|vector search|similarity search|relevance[- ]ranked|smart search|AI[- ]powered)\b/i;

test("the search box names authors", () => {
	assert.match(
		explorer,
		NAMES_AUTHORS,
		"the catalog placeholder must name authors — the author leg is otherwise invisible to the user",
	);
	assert.match(
		explorer,
		LABEL_NAMES_AUTHORS,
		"the accessible name must match what the field actually searches (3.3.2)",
	);
});

test("a scope sentence exists and is the input's accessible description", () => {
	assert.match(explorer, DESCRIBED_BY, "the input must point at the scope sentence via aria-describedby");

	const scope = explorer.match(SCOPE_SENTENCE);
	assert.ok(scope, "no #catalog-search-scope element found in CorpusExplorer.jsx");
	const copy = scope[0];

	for (const column of ["title", "abstract", "author"]) {
		assert.ok(
			copy.toLowerCase().includes(column),
			`the scope sentence must name the ${column} column — it exists so the user can judge a result count`,
		);
	}
	assert.match(copy, /substring/i, "say what the match actually is: a substring match, not a ranked search");
	assert.ok(
		/3 characters/.test(copy),
		"the author leg's minimum length is user-visible behaviour (a 2-letter name silently misses) and must be stated",
	);
});

test("no copy on the catalog claims semantic retrieval", () => {
	// Retrieval is lexical. corpus_meta is 0 rows; there is no embedding column
	// on `papers`. Anything below would be a false claim, not a stretch.
	const offenders = (explorerCopy.match(SEMANTIC_CLAIM) || []).join(", ");
	assert.equal(offenders, "", `CorpusExplorer.jsx claims semantic retrieval over a lexical index: ${offenders}`);
});

test("the result count is the response total, never papers.length", () => {
	// #1451 acceptance criterion 2: `papers` is one page, `total` is the corpus
	// match count. Recomputing from the array would silently cap the number at
	// page_size and read as "20 papers found" for a 900-hit query.
	assert.match(
		explorer,
		/\{total\.toLocaleString\(\)\} papers found/,
		"the count must render the response's `total` verbatim",
	);
	assert.ok(
		!/papers\.length\}?\s*papers found/.test(explorer),
		"the count is recomputed from the current page instead of the response total",
	);
	assert.match(
		explorer,
		/setTotalPapers\(data\.total \|\| 0\)/,
		"`total` must come off the API response, not be derived client-side",
	);
});

test("the guards reject the exact pre-fix markup they exist to catch", () => {
	// Anti-vacuity. If any of these start passing against the old source, the
	// corresponding guard above is decorative.
	assert.ok(
		!NAMES_AUTHORS.test(PRE_FIX_INPUT),
		'the placeholder guard matches "Search papers..." — it is guarding nothing',
	);
	assert.ok(
		!LABEL_NAMES_AUTHORS.test(PRE_FIX_INPUT),
		'the aria-label guard matches the old "Search papers" label — it is guarding nothing',
	);
	assert.ok(!DESCRIBED_BY.test(PRE_FIX_INPUT), "the aria-describedby guard matches markup that had no description");
	assert.ok(
		!SCOPE_SENTENCE.test(PRE_FIX_INPUT),
		"the scope-sentence guard matches source with no scope sentence in it",
	);
	// And the semantic-claim pattern must still catch a real false claim.
	assert.match(
		"Semantic search over the paper corpus",
		SEMANTIC_CLAIM,
		"the semantic-claim pattern no longer matches a false claim — it would let one ship",
	);
});

test("the scope sentence is styled, not left unstyled or display:none", () => {
	assert.match(css, /\.catalog-search-scope \{/, "the scope sentence needs a rule in App.css");
	const rule = css.match(/\.catalog-search-scope \{[^}]*\}/)[0];
	assert.ok(
		!/display:\s*none/.test(rule),
		"the scope sentence is the input's accessible description — hiding it removes the description too",
	);
});
