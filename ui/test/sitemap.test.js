import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Guard: the admin-only /app/insights page must never appear in the public
// sitemap (round 4 review finding). Insights.jsx is gated server-side
// (GET /api/metrics/private/whoami) and, on the frontend, a non-admin visitor
// gets the EXACT SAME NotFound treatment an unknown route gets
// (ui/src/App.jsx / ui/src/components/NotFound.jsx) — the whole design goal
// is that the page "does not exist" for anyone who isn't an admin. A sitemap
// entry defeats that on its own: it hands every crawler, and anyone who
// simply reads /sitemap.xml, the one URL the NotFound treatment exists to
// keep undiscovered. The entry survived the admin-gate PR itself (rounds
// 1-3) because nothing checked the sitemap — this is that check.
//
// ui/scripts/check-sitemap.mjs (which landed on `main` after this PR's
// branch point and arrived here with the `main` merge) is the COMPLEMENT of
// this file, not a replacement for it, and the two are deliberately kept
// separate: that script enforces one direction only — every route routes.js
// marks public must BE in the sitemap. This file enforces the other — the
// admin-gated page must NOT be, anywhere in the served bytes. Neither
// implies the other, and the script's own header documents the reverse
// direction as an intentional gap.

const sitemapPath = new URL("../public/sitemap.xml", import.meta.url);
const sitemapSrc = readFileSync(sitemapPath, "utf8");

// The whole FILE is the property, not just the <loc> URLs: sitemap.xml is
// publicly served byte-for-byte, so an XML comment naming the page, its
// path, or its gate mechanism discloses exactly what the NotFound treatment
// exists to hide — to any human who reads /sitemap.xml, which is precisely
// the audience a crawler-only check ignores. (An earlier revision kept an
// explanatory comment in the sitemap and scoped this test to <loc> values
// to accommodate it; that inverted the priority. The rationale lives HERE,
// in a file that is never served.)
const locs = [...sitemapSrc.matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1].trim());

// Parse every <loc> into its structural parts (origin + pathname) with the
// URL parser rather than comparing URL text. CodeQL flagged the earlier
// `locs.includes("https://archimedes-arc.com/")` form under
// js/incomplete-url-substring-sanitization (alerts 13/14): the rule cannot
// tell `Array.prototype.includes` (exact element equality — what was
// actually meant) from `String.prototype.includes` (an unanchored substring
// test, where "https://archimedes-arc.com/" can sit anywhere in a longer
// hostile URL such as https://evil.example/?u=https://archimedes-arc.com/).
// Parsing removes the ambiguity for the reader and the analyzer at once, and
// is strictly stronger than either: an entry like
// `https://evil.example/#https://archimedes-arc.com/explore` parses to
// origin "https://evil.example", so it can never satisfy an origin+pathname
// match no matter what its text contains.
const parsedLocs = locs.map((loc) => {
	let url;
	try {
		url = new URL(loc);
	} catch {
		throw new Error(`sitemap <loc> is not a parseable absolute URL: ${loc}`);
	}
	return { origin: url.origin, pathname: url.pathname };
});

function hasCanonicalPath(pathname) {
	return parsedLocs.some(
		(u) => u.origin === "https://archimedes-arc.com" && u.pathname === pathname,
	);
}

test("sitemap.xml never advertises the admin-only page — anywhere in the file", () => {
	assert.doesNotMatch(
		sitemapSrc,
		/insights/i,
		"the publicly served sitemap must not name the admin-only page, in a URL, comment, or otherwise",
	);
});

test("sitemap <loc> URLs contain no gated route", () => {
	assert.ok(locs.length > 0, "sitemap parsed to zero <loc> entries — parser or file is broken");
	for (const { pathname } of parsedLocs) {
		assert.doesNotMatch(pathname, /insights/i, `unexpected gated URL in sitemap: ${pathname}`);
	}
});

test("sitemap.xml still lists real public URLs (anti-vacuity)", () => {
	// A guard that also passes against an accidentally emptied sitemap (e.g.
	// a bad merge truncating the file to a bare <urlset/>) proves nothing.
	// Pin real, expected entries so the check above can't be vacuously true.
	assert.ok(locs.length >= 4, `expected several <loc> entries, got ${locs.length}`);
	assert.ok(hasCanonicalPath("/"), "sitemap lost its canonical home <loc>");
	assert.ok(hasCanonicalPath("/explore"), "sitemap lost its canonical /explore <loc>");
});

test("every sitemap <loc> is on the canonical origin", () => {
	// The origin half of the parse above, asserted rather than merely
	// available: a <loc> pointing at any other host is either a bad merge or
	// an injected entry, and would make the anti-vacuity check above
	// meaningless if it were allowed to satisfy it.
	for (const { origin } of parsedLocs) {
		assert.equal(origin, "https://archimedes-arc.com", `off-origin <loc> in sitemap: ${origin}`);
	}
});
