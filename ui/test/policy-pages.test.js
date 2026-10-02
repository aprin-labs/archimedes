import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
// docs site linked from every page, docs.archimedes-arc.com (mkdocs-material),
// loads Google Fonts and calls api.github.com from the visitor's browser, with
// no CSP. The guard pins the true text for both sites and forbids the stale one.
test("the third-party disclosure matches what each site actually loads", () => {
	const path = sectionBody(privacy, "Who else is in the path");
	assert.match(path, /serves its typefaces from our own domain/, "this site's self-hosted fonts must be stated");
	assert.doesNotMatch(path, /linked, but blocked/i, "stale: ui/index.html no longer links the font CDN at all");
	assert.match(path, /docs\.archimedes-arc\.com/, "the docs site's third parties must be disclosed");
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
	assert.match(receipts, /written, at that moment/, "the receipt is written when the payment settles");
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
		/Not touched:<\/strong>\s+your payment receipts,\s+your credit\s+ledger/,
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
	assert.match(terms, /settles\s+for real/i, "the paywall must be described as really settling");
	assert.doesNotMatch(
		terms,
		/settlement is\s+switched off in production/i,
		"blanket 'settlement is off in production' is false — the generation rail settles",
	);
	assert.doesNotMatch(terms, /Nothing is charged/i, "false: each generation is charged $2.00");

	// The other half: marketplace settlement really is off, and saying so is
	// not optional once the page starts talking about payments that work.
	assert.match(terms, /marketplace/i, "the marketplace rail must still be named");
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
test("the terms state the free allowance, the Gateway flow and the live caps", () => {
	assert.match(terms, /first three generations are free/, "the free allowance must be stated");
	assert.match(
		sectionBody(terms, "Limits and fair use"),
		/After your three free generations/,
		"the price sentence must not read as unconditional",
	);
	assert.match(terms, /Circle&rsquo;s Gateway contract/, "the deposit step must be disclosed");
	assert.match(rendered(terms), /capped at \$50/, "the device-payment-key deposit cap must be disclosed");
	assert.doesNotMatch(terms, /leaves your wallet and arrives in ours/i, "false: settlement moves Gateway balances");
	assert.doesNotMatch(terms, /charged to\s+your linked wallet/i, "false: the charge comes from the Gateway balance");
	const limits = sectionBody(terms, "Limits and fair use");
	assert.match(limits, /one hundred\s+generations per account per day/, "the live per-account cap");
	assert.match(limits, /two hundred per IP address per day/, "the live per-IP cap");
	assert.doesNotMatch(terms, /ten\s+generations per account per day/i, "stale: 10/20 are only code fallbacks");
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
