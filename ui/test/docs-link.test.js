// Guard: the documentation is reachable from the product surfaces, at the one
// place it still lives.
//
// Until 2026-11-01 the docs were a mkdocs site served from our own S3 +
// CloudFront at docs.archimedes-arc.com (#1634). That site is being shut down
// with the hosted service, so the two public surfaces that carry navigation, the shared
// footer's "Resources" nav and the public header nav (both in PublicLayout.jsx,
// see policy-pages.test.js and public-visuals.test.js), now link to the
// repository's docs/ folder on GitHub.
//
// Four things are asserted:
//
//   1. The link EXISTS on both surfaces and points at the repo's docs/ folder,
//      which must exist in this checkout (a link to a folder we deleted would
//      look fine in review and 404 for every reader).
//   2. It opens off-site safely, like every other external link there.
//   3. NO surface names a docs host that is gone or going: docs.archimedes-arc.com
//      (being shut down with the hosted service), `.app` (decommissioned; it caused the Circle passkey rpId
//      bug) or GitHub Pages' `a-apin.github.io` (what #1634 moved off).
//   4. The UI does not link to mkdocs.yml's `site_url`. Until 2026-11-01 this
//      test bound the UI's docs host TO that site_url; the binding is inverted
//      rather than deleted, because the config still names that host and a
//      link copied from it will be dead once the shutdown completes.
//
// Hermetic: raw source-text reads (readFileSync), no JSX parsing, no network,
// no DB, no `.env`. Run with `node --test test/docs-link.test.js` from `ui/`
// (this repo's UI suite is node:test — there is no vitest dependency).
//
// Anti-vacuity, because a source-text scan is exactly the kind of guard that
// silently stops guarding: each nav section is extracted by marker, and every
// extraction is checked to still contain a known sibling link. A rename that
// breaks the extraction fails loudly instead of shrinking the scan to nothing.

import { existsSync, readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

const DOCS_URL = "https://github.com/aprin-labs/archimedes/tree/main/docs";

//: Hostnames that must never appear as the docs link. `docs.archimedes-arc.com`
//: is the docs site being shut down with the hosted service (2026-11-01);
//: `github.io` is the GitHub Pages path #1634 replaced; `.app` is the
//: decommissioned domain.
const FORBIDDEN_HOSTS = [
	"docs.archimedes-arc.com",
	"a-apin.github.io",
	"docs.archimedes-arc.app",
];

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

function read(rel) {
	assert.ok(
		existsSync(repoFile(rel)),
		`${rel} does not exist — this guard's scan would silently cover nothing.`,
	);
	return readFileSync(repoFile(rel), "utf8");
}

//: Slice out one <nav> by its aria-label, up to the first closing tag. Used
//: instead of "does the file contain the URL anywhere" so that a link parked
//: in an unrelated corner of the file cannot satisfy the guard.
function navSection(source, ariaLabel, file) {
	const open = source.indexOf(`aria-label="${ariaLabel}"`);
	assert.notEqual(
		open,
		-1,
		`${file} has no nav with aria-label="${ariaLabel}" — the extraction this guard depends on is broken.`,
	);
	const close = source.indexOf("</nav>", open);
	assert.notEqual(close, -1, `${file}: nav "${ariaLabel}" is never closed.`);
	return source.slice(open, close);
}

//: [label, file, aria-label, a sibling link that must survive any refactor]
const SURFACES = [
	[
		"shared public footer",
		"src/components/PublicLayout.jsx",
		"Resource links",
		"/.well-known/agent.json",
	],
	[
		"public header",
		"src/components/PublicLayout.jsx",
		"Public navigation",
		"/architecture",
	],
];

for (const [label, file, ariaLabel, sibling] of SURFACES) {
	test(`${label} links to the docs site`, () => {
		const section = navSection(read(file), ariaLabel, file);
		assert.ok(
			section.includes(`href="${DOCS_URL}"`),
			`${file}'s "${ariaLabel}" nav has no href="${DOCS_URL}". The docs are in the repo but unreachable from this surface.`,
		);
	});

	test(`${label} docs link opens off-site safely`, () => {
		const section = navSection(read(file), ariaLabel, file);
		const anchor = section.slice(section.indexOf(`href="${DOCS_URL}"`));
		const tagEnd = anchor.indexOf(">");
		const attrs = anchor.slice(0, tagEnd);
		assert.match(
			attrs,
			/target="_blank"/,
			`${file}: the docs link is a different origin; it should open in a new tab like the other off-site links here.`,
		);
		assert.match(
			attrs,
			/rel="noreferrer"/,
			`${file}: the docs link needs rel="noreferrer", same as every other external link on this surface.`,
		);
	});

	// Anti-vacuity: if this sibling disappears the extraction above is no
	// longer looking at the nav this guard was written for, and every
	// assertion in this file would be checking the wrong text.
	test(`${label} nav still contains its known sibling link`, () => {
		const section = navSection(read(file), ariaLabel, file);
		assert.ok(
			section.includes(sibling),
			`${file}'s "${ariaLabel}" nav no longer contains ${sibling} — re-point this guard at the nav that replaced it.`,
		);
	});

	test(`${label} names no non-canonical docs host`, () => {
		const source = read(file);
		for (const host of FORBIDDEN_HOSTS) {
			assert.ok(
				!source.includes(host),
				`${file} references ${host}, a docs host that is gone or going. Link the repo's docs/ folder (${DOCS_URL}).`,
			);
		}
	});
}

test("the docs link points at a docs/ folder this repo actually has", () => {
	const docsDir = new URL("../../docs/", import.meta.url);
	assert.ok(existsSync(docsDir), "the repo has no docs/ folder; the Docs links would 404");
	assert.ok(
		existsSync(new URL("doc-index.md", docsDir)),
		"docs/doc-index.md (the register README.md points at) is missing",
	);
	assert.match(DOCS_URL, /^https:\/\/github\.com\/aprin-labs\/archimedes\/tree\/main\/docs$/);
});

test("the UI does not link to mkdocs.yml's site_url (that site is being shut down)", () => {
	const mkdocs = readFileSync(
		new URL("../../mkdocs.yml", import.meta.url),
		"utf8",
	);
	const match = mkdocs.match(/^site_url:\s*(\S+)\s*$/m);
	assert.ok(
		match,
		"mkdocs.yml has no site_url; re-point this guard at whatever names the docs host now.",
	);
	const siteHost = new URL(match[1]).host;
	assert.notEqual(new URL(DOCS_URL).host, siteHost);
	const source = read("src/components/PublicLayout.jsx");
	assert.ok(
		!source.includes(siteHost),
		`PublicLayout.jsx links to ${siteHost}, mkdocs.yml's site_url. That site is being shut down with the hosted service (2026-11-01); link ${DOCS_URL}.`,
	);
});
