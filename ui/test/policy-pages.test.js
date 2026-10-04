import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { DELETION_RETAINED } from "../src/account-deletion.js";
import { resolveRoute, pageToPath } from "../src/routes.js";

// Guards for the /privacy and /terms policy pages.
//
// Two of these carry weight beyond ordinary regression cover:
//
//   1. ANONYMOUS REACHABILITY. The privacy URL is submitted to Google's OAuth
//      consent screen, and Google fetches it with no session. If these routes
//      ever stop resolving public — moved under /app, gated, feature-flagged —
//      the consent screen's verification breaks and the only symptom is an
//      email from Google weeks later. Hence a test, not a comment.
//   2. THE DRAFT BANNER. Both pages are unreviewed drafts. The banner and the
//      "[pending owner approval]" date are the honest label on that, and only
//      the owner removes them. A test is the thing that stops a tidy-up pass
//      from quietly deleting an inconvenient disclaimer.
//
// Same file-read + regex shape as public-visuals.test.js / a11y.test.js: no
// DOM, no renderer. Every assertion below was mutation-checked against the
// tree — see the PR body for the list of edits that make each one fail.

const src = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");

const privacy = src("components/Privacy.jsx");
const terms = src("components/Terms.jsx");
const banner = src("components/PolicyBanner.jsx");
const publicLayout = src("components/PublicLayout.jsx");
const layout = src("components/Layout.jsx");
const landing = src("components/Landing.jsx");
const app = src("App.jsx");
const consentBanner = src("components/ConsentBanner.jsx");
const consentChoices = src("components/ConsentChoices.jsx");
const authPage = src("components/AuthPage.jsx");

// The component body only: everything from the default export on. Each page's
// header comment names stale claims in order to say they are gone, and some
// guards must not be satisfied (or tripped) by that comment.
const rendered = (source) => source.slice(source.indexOf("export default function"));

// ── Routing: anonymous, public, and not feature-gated ───────────────────

test("/privacy and /terms resolve as public routes", () => {
	for (const [path, page] of [
		["/privacy", "privacy"],
		["/terms", "terms"],
	]) {
		const route = resolveRoute(path);
		assert.equal(route.kind, "public", `${path} must be a public route`);
		assert.equal(route.page, page);
		assert.equal(route.redirect, null);
	}
});

// The bounce-to-sign-in effect in App.jsx keys off `route.kind !== 'app'`, so
// "public" IS the anonymous guarantee — but only while these pages stay out of
// the app shell. This pins both halves: the routes are public, and the effect
// still gates on kind === 'app'.
test("an anonymous visitor is never bounced off the policy pages", () => {
	for (const path of ["/privacy", "/terms"]) {
		assert.notEqual(
			resolveRoute(path).kind,
			"app",
			`${path} must not be an /app route — those bounce anonymous visitors to /sign-in`,
		);
	}
	// Main's guard gained the insights carve-out (#1437) and the rebrand's
	// quote style; the pinned invariant is unchanged — only kind === "app"
	// routes ever bounce.
	assert.match(app, /if \(route\.kind !== "app" \|\| route\.anonymousOk \|\| route\.page === "insights" \|\| authLoading \|\| user\)/);
});

// featureEnabled() is applied to app routes only, but a future edit could
// route these through it. If that happens with the flag off, Google fetches a
// 404 where the privacy policy should be.
test("policy routes survive a features payload that disables everything", () => {
	const nothingEnabled = { quant: false, roadmapSurfaces: false };
	assert.equal(resolveRoute("/privacy", "", nothingEnabled).kind, "public");
	assert.equal(resolveRoute("/terms", "", nothingEnabled).kind, "public");
});

test("pageToPath round-trips both policy pages", () => {
	assert.equal(pageToPath("privacy"), "/privacy");
	assert.equal(pageToPath("terms"), "/terms");
	assert.equal(resolveRoute(pageToPath("privacy")).page, "privacy");
	assert.equal(resolveRoute(pageToPath("terms")).page, "terms");
});

test("App.jsx renders both policy pages in the public shell", () => {
	assert.match(app, /import Privacy from "\.\/components\/Privacy"/);
	assert.match(app, /import Terms from "\.\/components\/Terms"/);
	// The rebrand replaced the publicPages map with an if-chain; same render.
	assert.match(app, /route\.page === "privacy"\) content = <Privacy \/>/);
	assert.match(app, /route\.page === "terms"\) content = <Terms \/>/);
});

// ── The draft banner and the un-dated date ──────────────────────────────

test("both policy pages carry the draft banner", () => {
	assert.match(privacy, /import PolicyBanner from "\.\/PolicyBanner"/);
	assert.match(terms, /import PolicyBanner from "\.\/PolicyBanner"/);
	assert.match(privacy, /<PolicyBanner \/>/);
	assert.match(terms, /<PolicyBanner \/>/);
});

test("the banner says it is an unreviewed draft and not legal advice", () => {
	assert.match(banner, /Draft — under review/);
	assert.match(banner, /not been reviewed by a lawyer/);
	assert.match(banner, /not legal advice/);
	assert.match(banner, /className="policy-banner"/);
});

// The date is a claim that a human signed off on a particular day. Until the
// owner has, the honest value is the placeholder — so pin the placeholder and
// pin the absence of a real date, since only the second half catches someone
// "helpfully" filling one in.
test("policy pages stay undated until the owner approves them", () => {
	for (const [name, page] of [
		["Privacy", privacy],
		["Terms", terms],
	]) {
		assert.match(page, /Last updated: \[pending owner approval\]/, `${name} must keep the placeholder date`);
		assert.doesNotMatch(
			page,
			/Last updated:\s*(?!\[pending owner approval\])\S/,
			`${name} must not carry a real last-updated date while it is an unreviewed draft`,
		);
	}
});

// ── Footer links from BOTH shells ───────────────────────────────────────

test("both shells link to both policy pages", () => {
	for (const [name, shell] of [
		["PublicLayout", publicLayout],
		["Layout", layout],
	]) {
		assert.match(shell, /href="\/privacy"/, `${name} must link to /privacy`);
		assert.match(shell, /href="\/terms"/, `${name} must link to /terms`);
		assert.match(shell, /aria-label="Policies"/, `${name}'s policy nav needs an accessible name`);
	}
});

// The public footer moved out of Landing.jsx into PublicLayout.jsx so that
// Architecture, Privacy, Terms and not-found carry it too. If it is ever
// copied back, every public page renders two footers and the policy links
// appear twice on the landing page.
test("the public footer lives in the shell, not on the landing page", () => {
	assert.match(publicLayout, /<footer className="public-footer">/);
	assert.doesNotMatch(landing, /<footer/);
});

test("the app shell footer sits below main, inside the sidebar-offset column", () => {
	assert.match(layout, /<\/main>\s*(?:\{\/\*[\s\S]*?\*\/\})?\s*<footer className="app-footer">/);
});

// ── No dead links ───────────────────────────────────────────────────────

const INTERNAL_HREF = /href="(\/[^"]*)"/g;

test("every internal link on the policy pages and in both footers resolves", () => {
	const sources = [
		["Privacy.jsx", privacy],
		["Terms.jsx", terms],
		["PublicLayout.jsx", publicLayout],
		["Layout.jsx", layout],
	];
	let checked = 0;
	for (const [name, source] of sources) {
		for (const [, href] of source.matchAll(INTERNAL_HREF)) {
			// Query/template hrefs (e.g. Layout's `/sign-in?next=...`) are built at
			// runtime; take the path half, which is what resolveRoute needs.
			// Hash anchors (e.g. the header's /#product) resolve on the page the
			// path half names — strip both suffixes before asking resolveRoute.
			const path = href.split("?")[0].split("#")[0] || "/";
			if (path.includes("${")) continue;
			// Static files (llms.txt, .well-known/agent.json) are nginx-served,
			// not SPA routes — resolveRoute cannot vouch for them.
			if (/\.[a-z]+$/i.test(path) || path.startsWith("/.well-known")) continue;
			const route = resolveRoute(path);
			assert.notEqual(route.kind, "not-found", `${name}: dead internal link ${href}`);
			checked += 1;
		}
	}
	// Guards the guard: a regex that stops matching would make the loop above
	// vacuously pass. Four is the floor — /privacy and /terms from each shell.
	assert.ok(checked >= 4, `expected to check at least 4 internal links, checked ${checked}`);
});

test("external links on the policy pages are safe and point at the real repo", () => {
	for (const [name, page] of [
		["Privacy", privacy],
		["Terms", terms],
	]) {
		// Bounded to a single opening tag ([^>]* rather than [\s\S]*?). The lazy
		// cross-tag form let a non-https anchor — the new mailto: contact links
		// — be swallowed into the following https match, so an unsafe link could
		// have ridden along inside a match that passed on its neighbour's rel.
		let external = 0;
		for (const [match] of page.matchAll(/<a\b[^>]*href="(https?:[^"]*)"[^>]*>/g)) {
			assert.match(match, /rel="noopener noreferrer"/, `${name}: external link missing rel=noopener`);
			external += 1;
		}
		// Guards the guard: a regex that stopped matching would pass vacuously.
		assert.ok(external >= 1, `${name}: expected at least one external link to check`);
		// aprin-labs/archimedes is the canonical repo (README.md, CLAUDE.md). The old
		// pre-rename name still redirects, so a stale link would not 404 — which
		// is exactly why it needs pinning rather than eyeballing.
		assert.match(page, /https:\/\/github\.com\/aprin-labs\/archimedes\/issues/);
		assert.doesNotMatch(page, /archimedes-arcadia/);
	}
});

// ── Substance: the claims the pages exist to make ───────────────────────

// These pin the disclosures that are least comfortable and therefore most
// likely to be softened in an edit — the ones a reader is actively looking
// for. Wording can change; the disclosure cannot silently disappear.
test("the privacy policy discloses the things it would be convenient to omit", () => {
	assert.match(privacy, /archimedes_vid/, "the 180-day visitor cookie must be named");
	assert.match(privacy, /180 days/);
	// TRUE again since #1908: nginx trusts CloudFront's origin-facing ranges,
	// so the X-Client-IP Better Auth stores on auth_sessions is the viewer's.
	// Before #1908 it was the CloudFront edge and this sentence was false.
	assert.match(privacy, /IP address is stored on each sign-in/i, "session IP storage must be disclosed");
	assert.match(privacy, /public and permanent/i, "the on-chain permanence caveat must be present");
	assert.match(privacy, /pseudonymous, not anonymous/i);
	assert.match(privacy, /do not sell your data/i);
	// The vid cookie is set by funnel_middleware on the first response with no
	// consent check; the banner only gates the browser's own landing beacon.
	assert.match(
		sectionBody(privacy, "Counting visitors"),
		/sets it\s+whether or not you allow analytics/,
		"the visitor cookie must be disclosed as set regardless of the consent choice",
	);
	// The ALB access log (S3, 30 days) keeps full URLs; nginx's redaction does
	// not reach it, and a verify-email token is an unencrypted JWT of the address.
	assert.match(
		sectionBody(privacy, "IP addresses and logs"),
		/verification token contains your email\s+address/,
		"the load balancer's unredacted auth links must be disclosed",
	);
});

// #1367 shipped self-service deletion (Account Settings -> Delete account,
// auth/auth.js deleteUser.enabled). The draft said there was "no
// delete-my-account button" and that deletion was manual; a guard used to pin
// that sentence. It now pins the opposite, in both directions.
test("the privacy policy describes the self-service deletion that exists", () => {
	const deletion = sectionBody(privacy, "How long we keep things, and how to get them deleted");
	assert.match(deletion, /You can delete your account yourself/, "self-service deletion must be stated");
	assert.doesNotMatch(privacy, /no delete-my-account button/i, "false since #1367: the button exists");
	assert.doesNotMatch(privacy, /Deletion today is manual/i, "false since #1367: the database does it in-request");
	assert.match(deletion, /no\s+recovery window/, "the irreversibility must be stated before anyone clicks");
});

// Fonts, twice over. The draft first called Google Fonts "the only
// third-party request the site makes from your browser" (false: the Circle SDK
// and the Arc RPC are browser calls too), then "linked, but blocked" (stale:
// the rebrand deleted the preconnects and the css2 link from ui/index.html, so
// this site requests nothing from Google at all). What IS true is that the
// docs site, docs.archimedes-arc.com (mkdocs-material), loads Google Fonts and
// calls api.github.com from the visitor's browser, with no CSP. It is linked
// from PublicLayout's header and footer only: the /app shell's footer links
// just /privacy and /terms, and the auth pages render outside PublicLayout, so
// "linked from every page" was false. The guard pins the true text for both
// sites and forbids the stale ones.
test("the third-party disclosure matches what each site actually loads", () => {
	const path = sectionBody(privacy, "Who else is in the path");
	assert.match(path, /serves its typefaces from our own domain/, "this site's self-hosted fonts must be stated");
	assert.doesNotMatch(path, /linked, but blocked/i, "stale: ui/index.html no longer links the font CDN at all");
	assert.match(path, /docs\.archimedes-arc\.com/, "the docs site's third parties must be disclosed");
	assert.match(path, /linked from the header and footer of our public pages/, "where the docs site is linked");
	assert.doesNotMatch(path, /linked from every page/, "false: only PublicLayout links the docs site");
	assert.match(path, /loads its typefaces from Google Fonts/, "the docs site really does load Google Fonts");
	assert.match(path, /GitHub&rsquo;s API/, "the docs site's in-browser GitHub API call must be disclosed");
	assert.match(path, /Arc testnet RPC endpoint/i, "the browser's own RPC calls must be disclosed");
	assert.match(path, /Circle/, "the browser's Circle SDK calls must be disclosed");
	assert.doesNotMatch(
		privacy,
		/only third-party request/i,
		"false: Circle's SDK and the Arc RPC are both called from the browser",
	);
});

// Generation is charged for real (see the terms guard above), so the record
// that charge leaves is collected personal data and belongs on this page.
// Grounded in backend/archimedes/models/payment_receipt.py.
test("the privacy policy discloses the payment records the paywall creates", () => {
	const receipts = sectionBody(privacy, "Free generations and payments");
	assert.match(receipts, /payment_receipts/, "the table must be named, as archimedes_vid is");
	assert.match(receipts, /wallet address that paid/i);
	assert.match(receipts, /settlement reference/i);
	assert.match(receipts, /not an on-chain transaction hash/i, "settlement_ref is a Circle ref, not a tx hash");
	// generation_credits (#1441) is the second record a charge creates, and it
	// holds an idempotency key the payer's own client supplied. Both tables get
	// named; a page that discloses the receipt but not the ledger deciding what
	// you are owed has disclosed the comfortable half.
	assert.match(receipts, /generation_credits/, "the credit ledger must be named too");
	assert.match(receipts, /idempotency key/i, "the client-supplied key stored on the credit must be disclosed");
	// #1643: the first three runs on a verified account are free, and each one
	// writes a free_generation_grants row (no FK to auth_users).
	assert.match(receipts, /free_generation_grants/, "the free-generation ledger must be named");
	// #1908 (#1910): the receipt is written at the settle, before the enqueue.
	// But _persist_payment_receipt swallows a failed write (generate_routes.py),
	// so "every payment that settles is written" was false; the sentence now
	// names its exception and the exception must stay on the page.
	assert.match(
		receipts,
		/row\s+at that moment \(the one exception is described below\)/,
		"the receipt is written when the payment settles, with the exception flagged",
	);
	assert.match(receipts, /a\s+payment can settle without a receipt row/, "the swallowed-write exception must be stated");
	assert.doesNotMatch(receipts, /Every payment that settles is written/, "false: a failed receipt write is swallowed");
	// The grant row carries user_id (no FK), which is why it is personal data.
	assert.match(
		receipts,
		/<code>free_generation_grants<\/code> with your account id/,
		"the free-generation record must say it carries the account id",
	);
});

// payment_receipts.user_id and generation_credits.user_id carry no FK to
// auth_users (migration 85ca5310b7a1 leaves them out on purpose), and
// ui/src/account-deletion.js lists both under DELETION_RETAINED. The draft
// said removing them was "part of the deletion process" and listed them as
// erased; both sentences were false. The receipts list renders only inside
// Portfolio, a roadmap-hidden page, so "read your receipts back in the app"
// was false too.
test("the privacy policy says payment records survive account deletion", () => {
	const receipts = sectionBody(privacy, "Free generations and payments");
	assert.match(receipts, /deleting your account does not\s+remove them/, "receipts and credits outlive the account");
	assert.doesNotMatch(privacy, /removing them is part of the deletion process/i, "false: no FK, nothing removes them");
	assert.doesNotMatch(privacy, /read your own receipts back in the app/i, "false: the receipts list is roadmap-hidden");
	const deletion = sectionBody(privacy, "How long we keep things, and how to get them deleted");
	const erased = deletion.match(/<strong>Erased:<\/strong>([\s\S]*?)<\/li>/);
	assert.ok(erased, "the Erased bullet must exist");
	assert.doesNotMatch(erased[1], /receipt|credit/i, "receipts and credits are not erased by account deletion");
	assert.match(
		deletion,
		/Not touched by account deletion:<\/strong>\s+your payment\s+receipts,\s+your credit\s+ledger/,
		"the retained records must be named where the deletion is described",
	);
});

// #1429 makes user_profiles CASCADE on account deletion: the row holding the
// Fernet-encrypted contact email is ERASED, not detached. The draft said the
// database "detaches your strategies and profile from you rather than erase
// them", which describes the pre-#1429 SET NULL for a row that is about to
// stop behaving that way. The page now splits erase-vs-detach the way the
// policy actually does.
test("the deletion section splits what is erased from what is detached", () => {
	const deletion = sectionBody(privacy, "How long we keep things, and how to get them deleted");
	assert.match(deletion, /profile row/i, "the encrypted-email row's fate must be stated explicitly");
	assert.match(deletion, /detached from you rather than destroyed/i, "the SET NULL tables must be described");
	assert.match(deletion, /payment receipts/i, "receipts must be named where deletion is described");
	assert.doesNotMatch(
		deletion,
		/detach your strategies and profile/i,
		"the profile row is erased, not detached (#1429 cascade policy)",
	);
	// SET NULL clears owner_user_id only: strategy_store keeps brief_intent and
	// owner_wallet, so "detached" must not read as "anonymised".
	assert.match(deletion, /still contain your brief text/, "what a detached row still holds must be stated");
	// Aurora BackupRetentionPeriod=7 (infra/aurora.tf); deleted rows live on there.
	assert.match(deletion, /automated database backups for up to 7\s+days/, "the backup window must be disclosed");
});

// #1460 stood up privacy@archimedes-arc.com (SES receipt rule -> SNS -> the
// owner's inbox). Before it existed, a public issue tracker was the only
// honest answer; it is no longer an acceptable sole route for a request that
// requires naming your account. The tracker stays as the open alternative.
test("both pages route account and privacy requests to the private mailbox", () => {
	for (const [name, page] of [
		["Privacy", privacy],
		["Terms", terms],
	]) {
		assert.match(page, /mailto:privacy@archimedes-arc\.com/, `${name} must offer the private mailbox`);
		assert.match(
			page,
			/github\.com\/aprin-labs\/archimedes\/issues/,
			`${name} must keep the tracker as an alternative, not drop it`,
		);
	}
	assert.match(
		sectionBody(privacy, "Contact"),
		/do not post personal details/i,
		"the tracker must still be labelled public",
	);
	// How the mail is DELIVERED is part of the disclosure: the receipt rule's
	// SNS action has one email subscriber, a personal Gmail inbox
	// (infra/ses_inbound.tf), so Google holds what is sent, and an SNS action
	// bounces any message over 150 KB. "A private mailbox we read" described
	// neither.
	assert.match(sectionBody(privacy, "Contact"), /personal Gmail inbox/, "the Gmail relay must be disclosed");
	assert.match(sectionBody(privacy, "Contact"), /150&nbsp;KB/, "the attachment-size bounce must be disclosed");
	assert.match(sectionBody(terms, "Contact"), /personal Gmail inbox/, "the terms must not imply a dedicated mailbox");
	assert.doesNotMatch(terms, /a private mailbox we read/i, "stale: the address is a relay to a personal inbox");
});

test("the terms state the testnet and no-advice position", () => {
	assert.match(terms, /testnet/i);
	assert.match(terms, /not investment advice/i);
	assert.match(terms, /Do not connect a wallet holding assets you care about/i);
	assert.match(terms, /as is/i);
	assert.match(terms, /Limitation of liability/i);
});

// The payment position is SPLIT, and the page must carry both halves.
//
// This test replaces one that pinned the opposite claim. The draft said
// "settlement is switched off in production. Nothing is charged, nothing is
// collected, and no balance is moved" — and a guard held that sentence in
// place. It was false: infra/ecs.tf pins GENERATION_PAYMENT_REQUIRED="true",
// GENERATION_PAYMENTS_DRY_RUN="false" and GENERATION_PRICE_USD="2.00" in the
// live task definition, so services/generation_payment.py runs its real
// verify+settle path through Circle's facilitator and test USDC actually
// moves from the payer to the platform wallet. Only the MARKETPLACE rail is
// still dry (PAYMENTS_DRY_RUN="true").
//
// A terms page telling people they are not being charged while they are being
// charged is the most expensive sentence on either page, so both directions
// are pinned: the charge must be disclosed, AND the marketplace half must not
// be dropped in the correction (which would over-claim the other way).
test("the terms disclose the real generation charge without over-claiming the marketplace rail", () => {
	assert.match(terms, /\$2\.00 in testnet USDC/, "the generation price must appear on the page");
	// \s+ throughout: these read the raw JSX source, where prose wraps mid-phrase.
	assert.match(rendered(terms), /settles\s+for real/i, "the paywall must be described as really settling");
	assert.doesNotMatch(
		terms,
		/settlement is\s+switched off in production/i,
		"blanket 'settlement is off in production' is false — the generation rail settles",
	);
	assert.doesNotMatch(terms, /Nothing is charged/i, "false: each generation is charged $2.00");

	// The other half: marketplace settlement really is off, and saying so is
	// not optional once the page starts talking about payments that work.
	assert.match(rendered(terms), /marketplace/i, "the marketplace rail must still be named");
	assert.match(terms, /nothing settles there/i, "the marketplace rail must still be disclosed as dry");

	// The price belongs where a reader looks for what things cost, not only in
	// the payments section.
	const limits = sectionBody(terms, "Limits and fair use");
	assert.match(limits, /\$2\.00 in testnet USDC/, "the fair-use section must name the price too");
});

// Three facts the first draft had wrong, each pinned against the live task
// definition's values in infra/ecs.tf:
//   - FREE_GENERATIONS_PER_ACCOUNT=3 (#1643): "each generation costs $2.00"
//     stopped being true for verified accounts on 2026-09-01;
//   - the money comes from a Circle GATEWAY balance funded by an approve +
//     deposit (ui/src/x402.js), not straight from "your linked wallet", and a
//     passkey's device payment key is capped at $50 per deposit
//     (ui/src/payment-deposit-cap.js);
//   - the daily caps are 100 per account and 200 per IP
//     (GENERATION_DAILY_CAP_PER_USER/_PER_IP); 10 and 20 are only the code
//     fallbacks in services/generation_quota.py.
// The allowance is three per account, lifetime, spendable only while the email
// is verified (services/free_generations.py). An unverified account pays for
// its first runs, so "your first three generations are free" was imprecise.
test("the terms state the free allowance, the Gateway flow and the live caps", () => {
	assert.match(
		terms,
		/Each account gets three free generations once its email address is\s+verified/,
		"the free allowance must be stated per account",
	);
	assert.doesNotMatch(terms, /first three generations are free/, "imprecise: unverified accounts pay for their first runs");
	assert.match(
		sectionBody(terms, "Limits and fair use"),
		/Beyond the free allowance/,
		"the price sentence must not read as unconditional",
	);
	assert.match(terms, /Circle&rsquo;s Gateway contract/, "the deposit step must be disclosed");
	assert.match(rendered(terms), /capped at \$50/, "the device-payment-key deposit cap must be disclosed");
	assert.doesNotMatch(terms, /leaves your wallet and arrives in ours/i, "false: settlement moves Gateway balances");
	assert.doesNotMatch(terms, /charged to\s+your linked wallet/i, "false: the charge comes from the Gateway balance");
	const limits = sectionBody(terms, "Limits and fair use");
	// The caps count POST /start requests (enforce_generation_quota runs first,
	// on every one), not generations: the UI's unpaid 402 fetch counts too.
	assert.match(limits, /one hundred requests to start a generation per day/, "the live per-account cap");
	assert.match(limits, /each IP\s+address two hundred/, "the live per-IP cap");
	assert.match(limits, /a paid\s+generation normally uses two/, "what one paid generation costs against the cap");
	assert.doesNotMatch(limits, /one hundred\s+generations per account per day/, "imprecise: the cap counts requests");
	assert.doesNotMatch(terms, /ten\s+generations per account per day/i, "stale: 10/20 are only code fallbacks");
	// services/client_ip.py keys IPv6 on the /64, not on whatever network a
	// caller holds; a delegated /56 is 256 keys.
	assert.match(limits, /each \/64 block/, "the IPv6 key is the /64");
	assert.doesNotMatch(terms, /an IPv6 network counts as one address/, "imprecise: a larger IPv6 network is many keys");
});

// Paper trading is a database replay (services/paper_trading.py) and the live
// agent runner is in dry-run, so the draft's "executing a paper trade genuinely
// writes to a public chain" was false; the user's only own on-chain writes are
// the Gateway approve + deposit. IPFS pinning never ran in production
// (docs/adr/ipfs-pinning-not-live.md), and per-vault chat was deleted. All of
// these are absences, so each is pinned on the RENDERED text (the source
// comments name them in order to say they are gone; see rendered() above).

test("neither page describes features that are not running", () => {
	assert.doesNotMatch(terms, /paper trade genuinely\s+writes/i, "false: paper trading writes nothing to a chain");
	assert.match(terms, /Paper\s+trading, by contrast, is simulated/, "the paper-trading position must be stated");
	for (const [name, page] of [
		["Privacy", privacy],
		["Terms", terms],
	]) {
		assert.doesNotMatch(rendered(page), /pinned to IPFS|pinning a\s+provenance record|IPFS\s+records/i, `${name}: no IPFS pinning runs`);
	}
	assert.match(privacy, /do\s+not pin anything to IPFS/, "the absence of pinning must be stated, not just omitted");
	assert.doesNotMatch(rendered(privacy), /chat you have with the agent/i, "per-vault chat was deleted");
});

// #1908 (#1911): auth/auth.js dropIdToken nulls idToken on every account write,
// and alembic 7d2f9a4c1e60 cleared the stored ones. The draft's "Those tokens
// are encrypted" covered only access/refresh; the Google ID token sat in clear.
// #1908 (#1912): auth/session-sweep.js deletes expired auth_sessions hourly;
// before it, expired rows (with their IP and user-agent) were never deleted.
test("the privacy policy states the token and session facts #1908 made true", () => {
	assert.match(privacy, /We do not store the ID token/, "the dropped ID token must be stated");
	assert.doesNotMatch(privacy, /tokens it issued\.\s+Those tokens are encrypted/, "stale: the ID token was not encrypted");
	assert.match(privacy, /job that runs every\s+hour\s+deletes\s+expired session records/, "the session sweep must be stated");
	assert.doesNotMatch(privacy, /Sessions last seven days/, "stale: sessions roll, and expired rows used to persist");
});

// Once the page says you are really charged, what happens when the thing you
// paid for does not arrive stops being an implementation detail. The shipped
// answer is a credit, decided in docs/adr/generation-payment-credit-not-refund.md
// and implemented in services/generation_credits.py — explicitly NOT a refund,
// because settlement runs one way through Circle and the outbound path is
// blocked on the custody migration (#975). Saying "refund" here would be a
// promise the code cannot keep.
test("the terms describe repayment as a credit and never promise a refund", () => {
	assert.match(terms, /repaid\s+as\s+a\s+credit,\s+not\s+as\s+a\s+refund/i, "the credit position must be stated");
	assert.match(terms, /Credits\s+do\s+not\s+expire/i);
	// Scoped to PROMISES. The page legitimately uses the words "refund" and
	// "money-back guarantee" to deny them, so a bare /refund/ would forbid the
	// honest sentence along with the dishonest one.
	assert.doesNotMatch(
		terms,
		/(we will refund|we can refund|refund your|a full refund|refunded to you)/i,
		"no refund promise — the outbound transfer path does not exist today",
	);
});

// Governing law was the owner's call, and he made it (2026-08-21): Illinois.
// The guard flipped WITH the decision rather than being deleted by it. It used
// to pin the [OWNER TO SPECIFY] marker so nobody could guess a jurisdiction;
// it now pins the answer so nobody can quietly drift off it.
//
// Scoped to the section body, NOT to the whole file: the file-wide form of
// this assertion passed a mutation that replaced the governing-law marker with
// an invented "Delaware law governs", because a second [OWNER TO SPECIFY]
// elsewhere on the page (the liability cap) kept the file-wide match green.
// A guard satisfied by an unrelated line guards nothing.
function sectionBody(source, heading) {
	const re = new RegExp(`<h2>${heading}</h2>([\\s\\S]*?)</section>`);
	const m = source.match(re);
	assert.ok(m, `section not found: ${heading}`);
	return m[1];
}

test("governing law names Illinois, and no other jurisdiction", () => {
	const governing = sectionBody(terms, "Governing law");
	assert.match(governing, /laws of the State of Illinois/, "the owner specified Illinois");
	assert.match(
		governing,
		/courts located in Illinois/,
		"venue must be named too — governing law alone leaves where-you-sue open",
	);
	assert.doesNotMatch(
		governing,
		/\b(Delaware|England|Wales|Singapore|Switzerland|New York|California|Texas|Massachusetts)\b/i,
		"no jurisdiction other than Illinois may appear in this section",
	);
});

// Both owner-decision markers are resolved: governing law -> Illinois, and the
// liability cap -> none (the exclusions stand on their own). Neither page may
// carry the marker again — a page shipped with an unresolved decision printed
// on it is a page nobody finished.
test("no unresolved owner-decision markers remain on either page", () => {
	for (const [name, page] of [
		["Privacy", privacy],
		["Terms", terms],
	]) {
		assert.doesNotMatch(page, /\[OWNER TO SPECIFY\]/, `${name} still carries an unresolved owner decision`);
	}
});

// APRIN Labs is a trading name, not a filed company (owner, 2026-08-21).
// "operated by APRIN Labs" reads as though a legal entity exists to stand
// behind these terms. It doesn't — and asserting one on the two pages whose
// entire purpose is claims that are true is exactly the failure CLAUDE.md's
// first rule exists to prevent.
test("the operator line does not imply a company that does not exist", () => {
	for (const [name, page] of [
		["Privacy", privacy],
		["Terms", terms],
	]) {
		assert.match(page, /operated under the name APRIN&nbsp;Labs/, `${name} must name the operator`);
		assert.match(page, /not a registered company/, `${name} must not leave incorporation implied`);
		assert.doesNotMatch(page, /operated by APRIN/, `${name} must not imply APRIN Labs is a company`);
	}
});

// ── The second 2026-10-01 audit pass ────────────────────────────────────
//
// Each guard below pins a sentence the auditor found false or imprecise
// against main 3591cd79 plus the live stack, in both directions: the true
// text must be present and the old text must not come back.

// How a payment actually moves (ui/src/components/Generate.jsx pay paths,
// ui/src/x402.js depositToGateway, ui/src/payment-session.js):
//   - a deposit happens whenever the Gateway balance is short or unreadable
//     (`bal == null || bal < need`), not only on the first payment;
//   - a passkey deposit is CREDITED to the device key's Gateway balance; the
//     funds never sit in the browser, only the signing key does, and losing
//     the browser's storage strands what is left;
//   - on the passkey path nobody signs: the device key signs without a prompt;
//   - unlink_wallet refuses (409) once the key backs a strategy, because the
//     paid run stamps owner_wallet with it (wallet_routes._wallet_has_owned_data).
test("both pages describe the deposit, the device key and unlinking as they work", () => {
	for (const [name, page] of [
		["Privacy", privacy],
		["Terms", terms],
	]) {
		const body = rendered(page);
		assert.match(body, /Whenever your Gateway balance is too low for a payment/, `${name}: when a deposit happens`);
		assert.doesNotMatch(body, /Your first payment includes a deposit/, `${name}: a deposit is not tied to the first payment`);
		assert.match(body, /credited to a\s+device payment key/, `${name}: the deposit is credited, not sent to the browser`);
		assert.doesNotMatch(body, /deposit goes instead\s+to a device/, `${name}: the funds never go to the browser`);
		assert.match(body, /can no longer be\s+spent from the site/, `${name}: cleared storage strands the remainder`);
		assert.match(body, /device payment key\s+signs it without a prompt/, `${name}: the passkey path signs without the user`);
		assert.doesNotMatch(body, /For each generation you sign a payment authorisation/, `${name}: a credit or a device key means no user signature`);
		assert.match(body, /it can no longer be\s+unlinked/, `${name}: the 409 on unlink must be disclosed`);
		assert.doesNotMatch(body, /you can unlink it in Account Settings|where\s+you can remove it/, `${name}: false once the key backs a strategy`);
		assert.match(body, /when you go back from\s+the\s+run&rsquo;s live view/, `${name}: where the settlement receipt appears`);
	}
	assert.doesNotMatch(terms, /site shows a settlement reference when a payment completes/, "the stream view replaces the page on success");
	// generation_payment.py verifies and settles against the price on the paid
	// request, not against the quote the user saw; only the signed amount holds.
	const limits = sectionBody(terms, "Limits and fair use");
	assert.match(limits, /never charged more\s+than the amount your payment authorisation names/, "the true price guarantee");
	assert.doesNotMatch(limits, /quote you are\s+shown before you pay is the price that applies/, "false in the price-change window");
});

test("the privacy policy states the second audit's infrastructure facts", () => {
	// better-auth's rate limiter counts EVERY auth request in auth_rate_limits
	// (onRequestRateLimit runs on each one, keyed ip|path), get-session on page
	// load included; deleteExpiredRows keeps rows 60s past their last request.
	const logs = sectionBody(privacy, "IP addresses and logs");
	assert.match(logs, /counts every request it receives against your IP\s+address/, "the sign-in service's per-request IP rows");
	assert.match(logs, /a minute or more after its last request/, "when those rows are deleted");
	// The managed rules (IP reputation included) sit only on the regional ACL
	// at the ALB, which sees the CloudFront peer, not the visitor.
	assert.match(logs, /a second firewall at our load balancer/, "the two firewalls must be told apart");
	assert.doesNotMatch(logs, /including an IP-reputation list/, "the reputation list never sees a CDN visitor's IP");
	assert.match(logs, /and to diagnose problems/, "the logs are operational, not security-only");
	assert.doesNotMatch(logs, /All of this exists to keep accounts secure/, "the purpose list was narrower than the uses");

	// The vid cookie is minted by the FastAPI middleware, so it arrives on the
	// first /api/ response (App.jsx fetches /api/features on every load).
	const visitors = sectionBody(privacy, "Counting visitors");
	assert.match(visitors, /The first time a page calls our server&rsquo;s API/, "when the vid cookie is set");
	assert.doesNotMatch(visitors, /On its first response/, "the HTML response sets no cookie");
	// #1927 put POST /api/vaults/create behind the server roadmap gate, so the
	// vault_deployed step cannot fire; the server-counted list names only the
	// steps that can (wallet_gate_shown, generation_started,
	// free_generation_used in generate_routes.py).
	assert.doesNotMatch(visitors, /deploying\s+a\s+vault/, "vault_deployed cannot fire with the roadmap gate off");
	assert.match(
		visitors,
		/being asked\s+to connect a wallet, and starting a generation \(and whether it was a\s+free one\)/,
		"the server-counted steps that can fire on the live site",
	);

	// GitHub's default scopes are read:user and user:email (@better-auth/core).
	const oauth = sectionBody(privacy, "If you sign in with Google or GitHub");
	assert.match(oauth, /from GitHub, read access to your\s+profile and your email addresses/, "GitHub's real scopes");
	assert.doesNotMatch(oauth, /contacts or anything else/, "read:user and user:email are more than name and avatar");

	// persist_proposal runs after the winner is saved and backtested, so a run
	// cancelled, crashed or timed out before then keeps no candidates.
	const made = sectionBody(privacy, "What you make on Archimedes");
	assert.match(made, /gets as far as saving its strategy and\s+backtesting it/, "when candidates are kept");
	assert.doesNotMatch(made, /gets as far as producing candidates/, "false: runs with candidates can still end before the write");

	// Inverted by the final pass. This guard used to REQUIRE the disclosure that
	// "our API still accepts a vault-deployment request", because
	// POST /api/vaults/create was mounted unconditionally. #1927 put it behind
	// require_roadmap_surfaces (404 unless FEATURE_ROADMAP_SURFACES is "true";
	// unset in prod), so the sentence is gone. The two halves are tied: the
	// absence is pinned only while the gate is still on the route, so removing
	// the gate fails here and forces the disclosure back. Audit round 2: the
	// absence is checked on the whole rendered text of both pages, not one
	// section, so the removed paragraph cannot come back somewhere else.
	for (const page of [rendered(privacy), rendered(terms)]) {
		assert.doesNotMatch(page, /still\s+accepts\s+a\s+vault-deployment\s+request/, "the route is gated off since #1927");
		assert.doesNotMatch(page, /One\s+exception:/, "no API exception to the blockchain section remains");
		assert.doesNotMatch(page, /deploying\s+a\s+vault/i, "vault_deployed cannot fire with the roadmap gate off");
	}
	const vaultsRoutes = readFileSync(new URL("../../backend/archimedes/api/vaults_routes.py", import.meta.url), "utf8");
	assert.match(
		vaultsRoutes,
		/@vaults_router\.post\(\s+"\/create",[^@]*?dependencies=\[Depends\(require_roadmap_surfaces\)\],\s+\)/,
		"POST /api/vaults/create must stay behind the roadmap gate while the page omits it",
	);
	const flags = readFileSync(new URL("../../backend/archimedes/feature_flags.py", import.meta.url), "utf8");
	assert.match(
		flags,
		/environ\.get\("FEATURE_ROADMAP_SURFACES", ""\)\.strip\(\)\.lower\(\) == "true"/,
		"the gate must stay off unless explicitly set to true",
	);
});

// Inverted by the final pass. This guard used to require the retention and
// deletion lists to name the historical per-vault chat rows (chat_messages,
// keyed by wallet, no FK to auth_users). Alembic 5cdd4f098a74 (#1926) deletes
// them and ran in prod on 2026-10-03 (0 rows found; a re-count printed
// chat_messages_rows=0), and nothing on main writes the table. The page must
// no longer describe chat rows it does not hold.
test("the retention and deletion lists no longer name per-vault chat rows", () => {
	const body = rendered(privacy);
	assert.doesNotMatch(body, /\bchat\b/i, "#1926 deleted the chat rows; the page must not list them");
	assert.doesNotMatch(body, /chat_messages/, "no chat table is described on the page");
	const deletion = sectionBody(privacy, "How long we keep things, and how to get them deleted");
	const untouched = deletion.match(/<strong>Not touched by account deletion:<\/strong>([\s\S]*?)<\/li>/);
	assert.ok(untouched, "the Not touched by account deletion bullet must exist");
	assert.doesNotMatch(untouched[1], /chat|messages/i, "no chat rows are left for account deletion to miss");
	assert.match(untouched[1], /free-generation record and the\s+wallet ledger/, "the FK-less records stay named");
});

// The final pass, on the owner's commitments: what account deletion leaves is
// erased or scrubbed on request, a copy is sent on request, both by hand after
// proof of ownership. Pinned in both directions so the old "you can ask" wording does
// not come back and the commitment cannot be quietly softened.
test("the privacy policy commits to erasure and a copy on request", () => {
	const deletion = sectionBody(privacy, "How long we keep things, and how to get them deleted");
	// Audit round 2: erasure and a copy are separate requests; asking for a
	// copy must not read as asking for the account to be deleted.
	assert.match(
		deletion,
		/On\s+request,\s+we\s+will\s+erase\s+or\s+scrub\s+what\s+account\s+deletion\s+leaves,\s+send\s+you\s+a\s+copy,\s+or\s+both\./,
		"the commitment must be stated",
	);
	assert.match(deletion, /If\s+you\s+ask\s+us\s+to\s+erase\s+your\s+data,\s+we\s+will\s+delete\s+your\s+account/, "deletion only on an erasure request");
	assert.match(deletion, /If\s+you\s+ask\s+for\s+a\s+copy,\s+we\s+will\s+send\s+you\s+a\s+copy/, "a copy is its own request");
	assert.doesNotMatch(deletion, /We\s+will\s+also\s+send\s+you\s+a\s+copy/, "the bundled reading is gone");
	assert.doesNotMatch(deletion, /erase\s+the\s+rest/, "the body narrows it: detached rows are scrubbed, not erased");
	assert.match(
		deletion,
		/erase\s+your\s+payment\s+receipts,\s+credit\s+ledger\s+\(which\s+gives\s+up\s+any\s+unspent\s+credit\),\s+free-generation\s+record,\s+the\s+reasoning\s+records\s+of\s+your\s+paper-trading\s+decisions\s+and\s+your\s+wallet&rsquo;s\s+entries\s+in\s+the\s+wallet\s+ledger/,
		"what is erased on request",
	);
	assert.match(
		deletion,
		/remove\s+your\s+brief\s+text,\s+any\s+strategy\s+name\s+you\s+typed,\s+and\s+your\s+wallet\s+address\s+from\s+the\s+records\s+that\s+were\s+detached\s+from\s+you/,
		"what is scrubbed from detached records",
	);
	// Proof, and where the copy goes: a wallet signature must not stand in for
	// the account, and a deleted account can only be found through a wallet.
	assert.match(deletion, /by\s+answering\s+a\s+message\s+we\s+send\s+to\s+its\s+email\s+address/, "how the account is proven");
	assert.match(
		deletion,
		/While\s+the\s+account\s+exists,\s+a\s+wallet\s+signature\s+covers\s+only\s+that\s+wallet&rsquo;s\s+records/,
		"a wallet proves only its own records while the account exists",
	);
	assert.match(deletion, /we\s+never\s+ask\s+for\s+identity\s+documents/);
	assert.match(deletion, /we\s+will\s+delete\s+your\s+account\s+if\s+it\s+still\s+exists/, "a live account is deleted first");
	assert.match(deletion, /Write\s+before\s+you\s+delete\s+your\s+account\s+yourself/, "the order the request needs");
	assert.match(deletion, /send you a copy of the data we hold about you/);
	assert.match(deletion, /handled by hand/, "no self-service export exists, and the page must say so");
	assert.match(deletion, /show that the\s+account or wallet is yours/, "proof of ownership before acting");
	assert.doesNotMatch(deletion, /If you want any of the detached or untouched records removed/, "superseded by the commitment");
	assert.match(sectionBody(privacy, "Contact"), /requests to delete or get a copy of your data/);
});

// #1926 nulled the vid on the 20 SIWE-window identity_events rows in prod and
// count_privacy_leftovers.py then printed 0 for every row with a vid; the only
// vid writer (auth_siwe) is mounted only under TESTING. The page states no
// past exception, and "New entries" would understate it.
test("the privacy policy states no visitor-id link, past or present", () => {
	const body = rendered(privacy);
	assert.match(body, /Entries in the ledger do not include the\s+anonymous visitor id/);
	assert.match(body, /We do not link the visitor id\s+to your account or your wallet\./);
	assert.doesNotMatch(body, /summer\s+2026/i, "the past exception was purged by #1926");
	assert.doesNotMatch(body, /New\s+entries\s+do\s+not\s+include/, "no entries include it, not only new ones");
	assert.doesNotMatch(body, /one\s+past\s+exception/);
});

// E3: the EC2 server's EBS snapshot was deleted on 2026-10-02 (0 EBS snapshots
// and 0 manual Aurora snapshots, verified 2026-10-03), so the 7-day automated
// backups are the only copies and the page must not describe another.
test("the privacy policy names no snapshot that no longer exists", () => {
	assert.doesNotMatch(rendered(privacy), /snapshot/i, "no disk or database snapshot remains");
	assert.doesNotMatch(rendered(terms), /snapshot/i);
	// A paraphrase of the removed sentence ("the decommissioned server's disk
	// image") would pass the word check above.
	assert.doesNotMatch(rendered(privacy), /decommissioned/i, "the EC2 server's disk image is gone");
	assert.doesNotMatch(rendered(terms), /decommissioned/i);
	const deletion = sectionBody(privacy, "How long we keep things, and how to get them deleted");
	assert.match(deletion, /automated database backups for up to 7\s+days/, "the backup window stays disclosed");
});

// Audit round 1 (2026-10-03): disclosures the page left out, each pinned
// whitespace-tolerant so a re-wrap cannot hide a removal.
test("the privacy policy discloses what the first audit round found missing", () => {
	// #1804: the SES feedback drain stamps auth_users.emailBouncedAt /
	// emailBounceKind, and auth/auth.js bounceRefusal refuses on it.
	assert.match(
		sectionBody(privacy, "Emails we send"),
		/Our\s+own\s+database\s+also\s+records\s+on\s+your\s+account\s+that\s+your\s+address\s+bounced/,
		"the bounce/complaint stamp on the account row",
	);
	// nginx main_redacted logs "$log_http_referer".
	assert.match(sectionBody(privacy, "IP addresses and logs"), /the\s+page\s+that\s+linked\s+to\s+it\s+\(the\s+referrer\)/);
	// mkdocs-material 9.7.7 loads mermaid from unpkg on pages with diagrams.
	assert.match(sectionBody(privacy, "Who else is in the path"), /loads\s+a\s+script\s+from\s+unpkg\.com/);
	// ui/src/x402.js: the passkey path batches approve + depositFor(token, paymentKey, amount).
	const chain = sectionBody(privacy, "What goes on a public blockchain");
	assert.match(chain, /deposit\s+names\s+this\s+browser&rsquo;s\s+device\s+payment\s+key/);
	assert.match(chain, /deploys\s+the\s+wallet&rsquo;s\s+smart-contract\s+account/);
	// Paper decision traces live in Redis (redis_state.save_trace, no TTL) and
	// the auth_users cascade does not reach them.
	const deletion = sectionBody(privacy, "How long we keep things, and how to get them deleted");
	const erased = deletion.match(/<strong>Erased:<\/strong>([\s\S]*?)<\/li>/);
	assert.match(erased[1], /everything\s+recorded\s+under\s+them\s+in\s+our\s+database/, "erasure is scoped to the database");
	const untouched = deletion.match(/<strong>Not touched by account deletion:<\/strong>([\s\S]*?)<\/li>/);
	assert.match(untouched[1], /the\s+reasoning\s+record\s+of\s+each\s+paper-trading\s+decision,\s+which\s+we\s+keep\s+in\s+our\s+cache/);
	assert.match(deletion, /They\s+still\s+contain\s+your\s+brief\s+text,\s+any\s+strategy\s+name\s+you\s+typed/);
	// privacy@ mail, replies and any copy sent stay in a personal Gmail inbox.
	assert.match(deletion, /messages\s+you\s+send\s+to\s+our\s+privacy\s+address\s+and\s+our\s+replies/);
	// The pre-deletion dialog lists the same not-touched database records.
	const retained = DELETION_RETAINED.map((row) => row.table);
	for (const table of ["payment_receipts", "generation_credits", "free_generation_grants", "identity_events"]) {
		assert.ok(retained.includes(table), `Account Settings must list ${table} as not removed, as the Privacy page does`);
	}
});

// The banner and the consent status used to say nothing optional is written
// and that switching off deletes it, while archimedes_vid (an analytics entry)
// is set by the server whatever the choice and the purge sweeps only web storage.
test("the consent banner and status do not contradict the server-set visitor cookie", () => {
	assert.doesNotMatch(consentBanner, /nothing\s+optional\s+is\s+written/);
	assert.match(consentBanner, /our\s+server\s+still\s+sets\s+its\s+anonymous\s+visitor\s+cookie\s+\(archimedes_vid\)/);
	assert.doesNotMatch(consentChoices, /has\s+been\s+deleted\s+from\s+this\s+browser\."/);
	assert.match(consentChoices, /The\s+server's\s+archimedes_vid\s+cookie\s+stays/);
	assert.match(consentChoices, /the\s+server\s+sets\s+archimedes_vid\s+either\s+way/);
});

// Audit round 2 (2026-10-03): sentences the second audit round found false by
// omission or contradicted by another sentence, each pinned
// whitespace-tolerant so a re-wrap cannot hide a removal.
test("the policy pages carry the second audit round's corrections", () => {
	// Replies to privacy@ (the ownership check, the copy, the answers) go from
	// the operator's Gmail, not the sign-in service's mailer, so "only" and the
	// closed lists had to be scoped.
	const emails = sectionBody(privacy, "Emails we send");
	assert.match(emails, /Our\s+sign-in\s+service\s+emails\s+you\s+only\s+about\s+your\s+account:/);
	assert.doesNotMatch(emails, /We\s+email\s+you\s+only\s+about\s+your\s+account/, "false: replies to privacy@ are email too");
	assert.match(emails, /our\s+answers\s+are\s+sent\s+from\s+the\s+operator&rsquo;s\s+Gmail\s+inbox/);
	assert.match(sectionBody(privacy, "What we do not do"), /or\s+used\s+to\s+write\s+to\s+us\./);
	// The claims ledger's profile-visibility and visitor-marker rows cite these.
	assert.match(sectionBody(privacy, "What we collect when you create an account"), /only\s+you\s+can\s+read\s+the\s+profile\s+back/);
	assert.match(
		sectionBody(privacy, "Counting visitors"),
		/marker\s+holding\s+your\s+visitor\s+id,\s+which\s+expires\s+180\s+days\s+after\s+it\s+is\s+written/,
	);
	const deletion = sectionBody(privacy, "How long we keep things, and how to get them deleted");
	assert.match(deletion, /Visitor-id\s+markers,\s+180\s+days\s+after\s+they\s+are\s+written/);
	// After deletion there is no account email, and a passkey wallet cannot
	// sign a message we send; it is proven by linking.
	assert.match(deletion, /we\s+can\s+send\s+a\s+copy\s+only\s+of\s+that\s+wallet&rsquo;s\s+records/);
	assert.match(deletion, /for\s+a\s+Circle\s+passkey\s+wallet,\s+by\s+linking\s+it\s+to\s+an\s+account\s+here/);
	// The limits list reads as complete, so it names the Gmail copy and what
	// third parties keep.
	const limits = deletion.slice(deletion.indexOf("Limits, stated up front"));
	assert.match(
		limits,
		/Our\s+correspondence\s+with\s+you,\s+including\s+any\s+copy\s+of\s+your\s+data\s+we\s+send,\s+stays\s+in\s+the\s+operator&rsquo;s\s+Gmail\s+inbox/,
	);
	assert.match(limits, /We\s+cannot\s+erase\s+what\s+Circle,\s+Google\s+or\s+GitHub\s+keep\s+about\s+you/);

	// Terms: the hand-back covers what release_entitlements_if_undelivered
	// covers (it runs in the run's own finally, which a process death skips),
	// and a credit is looked up by account id only.
	const body = rendered(terms);
	assert.match(body, /fails,\s+times\s+out,\s+is\s+cancelled,\s+or\s+never\s+starts/);
	assert.doesNotMatch(body, /fails,\s+crashes,\s+or\s+never\s+starts/, "a hard process death skips the release");
	assert.match(
		body,
		/If\s+our\s+server\s+itself\s+goes\s+down\s+after\s+a\s+run\s+is\s+accepted,\s+neither\s+is\s+handed\s+back\s+automatically\./,
	);
	assert.match(
		body,
		/deleting\s+the\s+account,\s+or\s+asking\s+us\s+to\s+erase\s+your\s+records,\s+gives\s+up\s+any\s+credit\s+you\s+have\s+not\s+spent/,
	);
	// Terms sends readers to self-service deletion; it must carry the order
	// the on-request route needs.
	const closing = sectionBody(terms, "Suspension and termination");
	assert.match(closing, /how\s+to\s+ask\s+us\s+to\s+erase\s+or\s+scrub\s+the\s+rest\s+or\s+send\s+you\s+a\s+copy/);
	assert.match(closing, /Ask\s+before\s+you\s+delete\s+the\s+account\s+yourself/);

	// The sign-up screen renders outside both shells, so neither footer's
	// policy links reach it; it carries them itself, above the OAuth buttons.
	assert.match(authPage, /By\s+creating\s+an\s+account\s+you\s+agree\s+to\s+the/);
	assert.match(authPage, /href="\/terms">Terms of Service<\/a>/);
	assert.match(authPage, /confirm\s+you\s+are\s+18\s+or\s+older/);
	assert.match(authPage, /href="\/privacy">Privacy Policy<\/a>/);
	assert.ok(
		authPage.indexOf("By creating an account") > -1 &&
			authPage.indexOf("By creating an account") < authPage.indexOf('className="auth-social-group"'),
		"the notice must sit above the OAuth buttons, which create accounts too",
	);

	// The banner's optional count is browser storage only: archimedes_vid is
	// an analytics entry the server sets whatever the choice.
	assert.match(consentBanner, /entry\.store\s*!==\s*"cookie"/);
	assert.match(consentBanner, /more,\s+in\s+your\s+browser,\s+are\s+optional/);
});

// The owner's seven commitments (#1432), kept word for word. Matched
// whitespace-tolerant against the rendered part of each page, because JSX
// prose re-wraps; any other edit, a softened or dropped word included, fails.
const OWNER_PROMISES = [
	[privacy, "The site does not currently offer vault deployment, on-chain publication of reasoning traces or marketplace publishing, and we do not pin anything to IPFS; if that changes, this page will say what gets published before the feature is switched on."],
	[privacy, "Archimedes is not intended for anyone under 18, and we do not knowingly collect information from children."],
	[privacy, "We will update this page whenever what the software does changes."],
	[privacy, "If a change materially affects what we collect or who receives it, we will say so on this page rather than editing quietly."],
	[terms, "You must be 18 or older."],
	[terms, "We may close an account that breaks these terms, abuses the service, or puts it or its users at risk — immediately where the risk is immediate, and otherwise with notice where we reasonably can. Closing an account deletes it as the Privacy Policy describes. We may also stop offering the service entirely."],
	[terms, "If a change materially affects your rights or what we do, we will say so plainly on this page rather than editing quietly. Continuing to use the service after a change means you accept the updated terms."],
];
const wordForWord = (s) =>
	new RegExp(`(?<![\\w-])${s.split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+")}(?![\\w-])`);

test("the owner's seven promises stay word for word", () => {
	for (const [page, sentence] of OWNER_PROMISES) {
		assert.match(rendered(page), wordForWord(sentence), sentence.slice(0, 60));
	}
});
