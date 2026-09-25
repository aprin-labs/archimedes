// Claim-integrity + affordance guards for the sign-in / sign-up screen
// (AuthPage.jsx), from the owner's review of the calm-precision rebrand.
//
// Three things shipped wrong on that screen and each has a guard here:
//
//  1. COPY. Under "ACCOUNT BEFORE WALLET / Research stays linked to you." the
//     page asserted "Your account owns briefs, strategies, and settings.
//     Wallet control is separate and only needed for on-chain actions." The
//     second half is false: a linked wallet also gates PII profile reads
//     (user_routes._extract_linked_wallet) and legacy-data reclaim
//     (wallet_routes.claim_legacy_wallet_data), neither of which is an
//     on-chain action. The replacement is the owner's three statements, each
//     checkable against the live path — see the ACCOUNT_BOUNDARY_PROOFS
//     comment in AuthPage.jsx for the citation per statement.
//
//  2. AFFORDANCES. "Forgot password?" was an unstyled <button> carrying
//     `caption text-[var(--accent)] text-left`, and the create-account link
//     was a caption-sized aside below the OAuth block. Both are now
//     design-system controls, and the auth screen — which renders outside
//     both .app-site and .public-site and so inherits neither's focus rule —
//     has a :focus-visible indicator of its own.
//
//  3. BRAND MARKS. The social buttons were text-only. They now carry inline
//     SVGs: Google's four-colour G unmodified, GitHub's Invertocat in
//     currentColor (the only monochrome treatments its guidelines allow).
//
// Idiom matches app-visuals.test.js / a11y.test.js: readFileSync + regex pins
// on the source, no DOM. Anti-vacuity, mirroring signin-claims.test.js: the
// removed-claim patterns are checked against their own canonical examples, so
// a pattern that stops matching anything fails loudly instead of guarding
// nothing.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const authPage = readFileSync(
	new URL("../src/components/AuthPage.jsx", import.meta.url),
	"utf8",
);
const css = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");

// ── 1. Copy ─────────────────────────────────────────────────────────────

// Kept verbatim by the owner; the statements below hang off this header, so
// if it moves the guard should be re-read rather than silently pass.
test("the account-boundary header is kept", () => {
	assert.match(authPage, /Account before wallet/);
	assert.match(authPage, /Research stays linked to you\./);
});

const TRUE_STATEMENTS = [
	"Email and password work without a wallet.",
	"Wallet linking requires signature proof.",
	"Generation settles real testnet USDC on Arc public testnet.",
];

// Slice the RENDERED const's array literal — a match in a code comment must not
// satisfy this pin (the validator proved the whole-file check passed with the
// array emptied). The map() guard below proves the array actually reaches JSX.
const proofsStart = authPage.indexOf("const ACCOUNT_BOUNDARY_PROOFS = [");
const proofsEnd = authPage.indexOf("\n]", proofsStart);
const proofsArray = authPage.slice(proofsStart, proofsEnd);

test("the three verified statements are present, verbatim", () => {
	assert.ok(proofsStart > -1 && proofsEnd > proofsStart, "ACCOUNT_BOUNDARY_PROOFS array literal not found");
	assert.ok(/ACCOUNT_BOUNDARY_PROOFS\.map\(/.test(authPage), "ACCOUNT_BOUNDARY_PROOFS is never rendered via map()");
	for (const statement of TRUE_STATEMENTS) {
		assert.ok(
			proofsArray.includes(statement),
			`AuthPage.jsx no longer states "${statement}" — these three are the owner's exact wording and each is backed by a cited live path; do not paraphrase`,
		);
	}
});

// Each entry: a phrase-shape from the removed claim, plus the canonical
// example it must keep matching.
const REMOVED_CLAIMS = [
	{
		pattern: /only needed for on-chain actions/i,
		canonical:
			"Wallet control is separate and only needed for on-chain actions.",
	},
	{
		pattern: /account owns briefs/i,
		canonical: "Your account owns briefs, strategies, and settings.",
	},
	{
		pattern: /Wallet control is separate/i,
		canonical: "Wallet control is separate and only needed for on-chain actions.",
	},
];

test("removed-claim patterns match their own canonical examples (guard is not vacuous)", () => {
	for (const { pattern, canonical } of REMOVED_CLAIMS) {
		assert.match(
			canonical,
			pattern,
			`pattern ${pattern} no longer matches its own canonical example — it is guarding nothing`,
		);
	}
});

test("the false no-real-funds claim never returns", () => {
	// Vacuity: the pattern still matches the overclaim it is meant to reject.
	const overclaim = "Arc public testnet uses no real funds.";
	assert.match(overclaim, /no real funds/i);
	assert.doesNotMatch(
		authPage,
		/no real funds/i,
		'AuthPage.jsx overclaims: generate is paid (GET /api/generate/quote, prod dry_run: false, $2 USDC on arcTestnet). Testnet USDC is not "no real funds."',
	);
});

test("the false wallet-boundary claim never returns", () => {
	for (const { pattern } of REMOVED_CLAIMS) {
		assert.doesNotMatch(
			authPage,
			pattern,
			`AuthPage.jsx reasserts a claim the live path does not back (${pattern}) — a linked wallet also gates PII profile reads and legacy-data reclaim, so "only ... on-chain actions" is false`,
		);
	}
});

// ── 2. Affordances ──────────────────────────────────────────────────────

test("Forgot password is a design-system control, not ad-hoc utility classes", () => {
	assert.match(authPage, /Forgot password\?/);
	assert.match(authPage, /className="auth-quiet-link"/);
	// The exact class soup it replaced. Pinning the shape (not just the new
	// class) is what stops a revert from passing: both could otherwise
	// coexist with the guard green.
	assert.doesNotMatch(
		authPage,
		/className="caption text-\[var\(--accent\)\] text-left"/,
	);
	// Quiet BUT VISIBLE: a link affordance and a real target size, not colour
	// alone (1.4.1 — colour is never the sole indicator).
	assert.match(css, /\.auth-quiet-link\s*\{[^}]*text-decoration:\s*underline;/s);
	assert.match(css, /\.auth-quiet-link\s*\{[^}]*min-height:\s*44px;/s);
});

test("the create-account cross-link sits with the submit action and reads as a link", () => {
	assert.match(authPage, /className="auth-alt-action"/);
	assert.match(authPage, /className="auth-alt-action__link"/);
	assert.match(authPage, /\{creating \? 'Sign in' : 'Create account'\}/);
	// Obvious, not buried: the cross-link must be emitted BEFORE the OAuth
	// group in source order, i.e. directly under the submit button.
	assert.ok(
		authPage.indexOf('className="auth-alt-action"') <
			authPage.indexOf('className="auth-social-group"'),
		"the create-account cross-link moved back below the OAuth block — the owner asked for it near the submit button",
	);
	assert.match(css, /\.auth-alt-action__link\s*\{[^}]*text-decoration:\s*underline;/s);
	assert.match(css, /\.auth-alt-action\s*\{[^}]*border:\s*1px solid var\(--glass-border\);/s);
});

test("the auth screen defines its own visible focus indicator", () => {
	// It renders outside both .app-site and .public-site, so neither shell's
	// :focus-visible rule reaches it — without this block the only indicator
	// is the UA default, which a stray `outline` shorthand has erased here
	// before (see the NOTE by the input block in App.css).
	assert.match(
		css,
		/\.auth-site :focus-visible\s*\{[^}]*outline:\s*3px solid var\(--accent\);/s,
	);
	assert.match(css, /\.auth-site :focus-visible\s*\{[^}]*outline-offset:/s);
	// Both new controls are natively focusable elements, so the indicator
	// actually reaches them: a <button type="button"> and an <a href>.
	assert.match(
		authPage,
		/<button\s+type="button"\s+className="auth-quiet-link"/,
	);
	assert.match(
		authPage,
		/<a\s+className="auth-alt-action__link"\s+href=/,
	);
});

// ── 3. Brand marks ──────────────────────────────────────────────────────

test("social buttons carry inline brand marks, not text alone", () => {
	assert.match(authPage, /Continue with Google/);
	assert.match(authPage, /Continue with GitHub/);
	assert.match(authPage, /<GoogleMark \/>/);
	assert.match(authPage, /<GitHubMark \/>/);
	// Inline, not a remote asset or an icon font: the CSP-constrained build
	// ships no external logo host and the icon-font classes elsewhere in this
	// tree (i-lucide-*) carry no brand artwork.
	assert.doesNotMatch(authPage, /<img[^>]+(google|github)/i);
});

test("the Google mark is the official four-colour G, undistorted", () => {
	// All four brand hexes, unrecoloured. Google's guidelines forbid altering
	// the artwork's colours; dropping one would silently produce a
	// three-quarter G that still renders.
	for (const hex of ["#EA4335", "#4285F4", "#FBBC05", "#34A853"]) {
		assert.ok(
			authPage.includes(`fill="${hex}"`),
			`the Google mark is missing brand colour ${hex} — the artwork must be reproduced unmodified`,
		);
	}
	// Square viewBox + equal width/height = no distortion, in the markup and
	// again in CSS (a percentage width would stretch it inside the flex row).
	assert.match(authPage, /viewBox="0 0 48 48"/);
	assert.match(css, /\.auth-social__icon\s*\{[^}]*width:\s*18px;[^}]*height:\s*18px;/s);
});

test("the GitHub mark is the Invertocat in currentColor, undistorted", () => {
	assert.match(authPage, /viewBox="0 0 16 16"/);
	// currentColor resolves to --text-1: near-black on light, near-white on
	// dark — the two monochrome treatments GitHub's logo guidelines permit.
	// A hard-coded fill would break one of the two themes.
	assert.match(
		authPage,
		/viewBox="0 0 16 16"\s*\n\s*fill="currentColor"/,
		"the GitHub mark must inherit currentColor so it stays solid in both themes",
	);
});

test("brand marks are decorative — the button text is the accessible name", () => {
	// Two SVGs, both hidden from AT and both out of the tab order. A labelled
	// icon inside an already-labelled button double-announces the provider.
	const hidden = authPage.match(/aria-hidden="true"\s*\n\s*focusable="false"/g);
	assert.equal(
		hidden?.length,
		2,
		"both brand marks must carry aria-hidden + focusable=false — the button's own text names the provider",
	);
});

test("brand buttons keep clear space and a 44px target", () => {
	// Google's and GitHub's usage rules both require breathing room around the
	// mark; 12px of gap plus 20px of inline padding is well beyond half the
	// 18px mark's height. 44px is the 2.5.8 target-size floor.
	assert.match(css, /\.auth-social\s*\{[^}]*min-height:\s*44px;/s);
	assert.match(css, /\.auth-social\s*\{[^}]*gap:\s*12px;/s);
	assert.match(css, /\.auth-social\s*\{[^}]*padding:\s*12px 20px;/s);
});
