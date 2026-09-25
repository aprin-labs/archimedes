// Guard: the documentation site is reachable from the product surfaces, at the
// one canonical host (#1634).
//
// The docs site is built by mkdocs and served from our own S3 + CloudFront
// (docs-site/infra/main.tf). A site nobody can navigate to is the same as an
// unpublished one, so the two public surfaces that carry navigation — the
// landing footer's "Resources" nav and the public header nav — must each link
// to it.
//
// Three things are asserted, and each one has failed somewhere in this repo
// before:
//
//   1. The link EXISTS on both surfaces.
//   2. It points at the CANONICAL host. `docs.archimedes-arc.com` is the only
//      docs hostname: `.app` was decommissioned (it caused the Circle passkey
//      rpId bug) and GitHub Pages' `a-apin.github.io` is what #1634 moved off.
//      A link to either would look fine in review and 404 (or worse, resolve
//      to something we no longer control) in production.
//   3. It agrees with `mkdocs.yml`'s `site_url`. The UI and the site config are
//      two places the same hostname is written down; binding them here means a
//      future rename has to change both or turn this red.
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

const DOCS_URL = "https://docs.archimedes-arc.com/";
const DOCS_HOST = "docs.archimedes-arc.com";

//: Hostnames that must never appear as the docs link. `github.io` is the
//: GitHub Pages path #1634 replaced; `.app` is the decommissioned domain.
const FORBIDDEN_HOSTS = ["a-apin.github.io", "docs.archimedes-arc.app"];

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
		"landing footer",
		"src/components/Landing.jsx",
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
			`${file}'s "${ariaLabel}" nav has no href="${DOCS_URL}". The docs site is published (docs-site/infra) but unreachable from this surface.`,
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
				`${file} references ${host}. ${DOCS_HOST} is the only docs hostname (#1634).`,
			);
		}
	});
}

test("the UI's docs host matches mkdocs.yml's site_url", () => {
	const mkdocs = readFileSync(new URL("../../mkdocs.yml", import.meta.url), "utf8");
	const match = mkdocs.match(/^site_url:\s*(\S+)\s*$/m);
	assert.ok(match, "mkdocs.yml has no site_url — the site would be built with relative canonical URLs.");
	assert.equal(
		new URL(match[1]).host,
		DOCS_HOST,
		`mkdocs.yml builds the site for ${match[1]} but the UI links to ${DOCS_URL}. One of the two is wrong.`,
	);
});
