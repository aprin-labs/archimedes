import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

import { resolveRoute, pageToPath } from "../src/routes.js";

// Guards for the /privacy and /terms pages.
//
// Since 2026-11-01 (#1432) these are short notices for the open-source,
// local-first project, not the hosted service's policy and terms. The guards
// pin three things:
//
//   1. ANONYMOUS REACHABILITY. A policy page that bounces an anonymous visitor
//      to sign-in is no policy page. If these routes ever stop resolving public
//      (moved under /app, gated, feature-flagged), this file fails.
//   2. THE KEY FACTS, tied to the code that makes them true where a fact is a
//      default the code can change: the services contacted with nothing
//      configured, and the CLI/MCP default address.
//   3. THE ABSENCE OF THE HOSTED-SERVICE CLAIMS: the privacy@ address,
//      payments, the 18-or-older rule, account suspension, the owner's
//      promises about the service, and the draft banner. They described a
//      service that no longer exists, so their return would be a false claim.
//
// Same file-read + regex shape as public-visuals.test.js / a11y.test.js: no
// DOM, no renderer.

const src = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");
const repo = (p) => readFileSync(new URL(`../../${p}`, import.meta.url), "utf8");

const privacy = src("components/Privacy.jsx");
const terms = src("components/Terms.jsx");
const publicLayout = src("components/PublicLayout.jsx");
const layout = src("components/Layout.jsx");
const landing = src("components/Landing.jsx");
const app = src("App.jsx");
const consentBanner = src("components/ConsentBanner.jsx");
const consentChoices = src("components/ConsentChoices.jsx");
const authPage = src("components/AuthPage.jsx");
const storageDisclosure = src("components/StorageDisclosure.jsx");

// The component body only: everything from the default export on. Each page's
// header comment names stale claims in order to say they are gone, and some
// guards must not be satisfied (or tripped) by that comment.
const rendered = (source) => source.slice(source.indexOf("export default function"));

// What a reader is shown of a JSX fragment, near enough for these guards: JSX
// comments dropped, the whitespace JSX drops beside a tag dropped, {" "}
// spacers made spaces, block tags made spaces and inline tags removed, the
// entities these pages use decoded, and whitespace collapsed. So a sentence commented out with {/* */} is gone,
// and a phrase split by <strong>, an entity or a line wrap reads as one phrase.
// Not a renderer: it is the text the promise pins and the removal guards read.
const ENTITIES = {
	"&rsquo;": "’",
	"&lsquo;": "‘",
	"&ldquo;": "“",
	"&rdquo;": "”",
	"&nbsp;": " ",
	"&amp;": "&",
	"&mdash;": "—",
	"&ndash;": "–",
	"&hellip;": "…",
};
// Any "<" or ">" left after the tag strip is dropped too, so a tag split
// across another ("<<b>p>") cannot survive as markup (CodeQL
// js/incomplete-multi-character-sanitization). The output is only ever
// matched against, never rendered.
const visible = (fragment) =>
	fragment
		.replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
		.replace(/>\s*\n\s*/g, ">")
		.replace(/\s*\n\s*</g, "<")
		.replace(/\{"\s*"\}/g, " ")
		.replace(/\{'\s*'\}/g, " ")
		.replace(/<\/?(?:p|li|ul|ol|h[1-6]|section|header|div)\b[^>]*>/g, " ")
		.replace(/<[^>]*>/g, "")
		.replace(/[<>]/g, "")
		.replace(/&[a-z]+;/g, (entity) => ENTITIES[entity] ?? entity)
		.replace(/\s+/g, " ");
// The rendered part of a page, as text.
const shown = (source) => visible(rendered(source));
// A removal guard: the text must be absent from the source AND from what the
// reader is shown, so re-adding it split by markup or a spacer still fails.
function assertGone(fragment, pattern, message) {
	assert.doesNotMatch(fragment, pattern, message);
	assert.doesNotMatch(visible(fragment), pattern, message);
}
// Whitespace-tolerant, word-for-word match of a sentence in shown text.
const phrase = (s) =>
	new RegExp(s.split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("\\s+"));

const PAGES = [
	["Privacy", privacy],
	["Terms", terms],
];

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
// route these through it. If that happens with the flag off, the policy page
// is a 404.
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

test("App.jsx renders both policy pages in the public shell, titled as they read", () => {
	assert.match(app, /import Privacy from "\.\/components\/Privacy"/);
	assert.match(app, /import Terms from "\.\/components\/Terms"/);
	// The rebrand replaced the publicPages map with an if-chain; same render.
	assert.match(app, /route\.page === "privacy"\) content = <Privacy \/>/);
	assert.match(app, /route\.page === "terms"\) content = <Terms \/>/);
	// The tab title follows the page heading, not the hosted-era names.
	assert.match(app, /privacy: "Privacy · Archimedes"/);
	assert.match(app, /terms: "License and terms · Archimedes"/);
	assertGone(app, /Privacy Policy · Archimedes|Terms of Service · Archimedes/);
});

// ── The date, and no draft banner ───────────────────────────────────────

// The draft banner and the "[pending owner approval]" date were the honest
// label on an unreviewed policy for a hosted service. Both pages are now
// short notices dated 2026-11-01; a returning banner or placeholder would
// relabel them as something they are not.
test("both pages carry the 2026-11-01 date and no draft banner", () => {
	assert.equal(
		existsSync(new URL("../src/components/PolicyBanner.jsx", import.meta.url)),
		false,
		"PolicyBanner.jsx was removed with the hosted-service policy",
	);
	for (const [name, page] of PAGES) {
		assert.match(shown(page), /Last updated 2026-11-01/, `${name} must carry its date`);
		assertGone(page, /PolicyBanner|policy-banner/, `${name} must not mount the draft banner`);
		assertGone(page, /\[pending owner approval\]|Draft\s+—\s+under\s+review/, `${name} is no longer an undated draft`);
	}
});

// APRIN Labs is a trading name, not a filed company (owner, 2026-08-21). The
// "not a registered company" wording stays; "operated by" would imply both a
// company and a running service.
test("the publisher line does not imply a company or a service", () => {
	for (const [name, page] of PAGES) {
		assert.match(shown(page), /published under the name APRIN Labs, which is not a registered company/, name);
		assertGone(page, /operated (?:by|under)/i, `${name} must not describe an operated service`);
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

// The shared public footer (#1864) said no privacy or terms page was published,
// which was true until these pages shipped. The links replace that line, under
// the names the pages carry; the line must not come back beside them.
test("the public footer links the policy pages instead of saying none exist", () => {
	assertGone(publicLayout, /No privacy or terms page published/i, "the pages are published now");
	assert.match(publicLayout, /href="\/privacy">Privacy<\/a>/);
	assert.match(publicLayout, /href="\/terms">License and terms<\/a>/);
});

// The public footer moved out of Landing.jsx into PublicLayout.jsx so that
// Architecture, Privacy, Terms and not-found carry it too. If it is ever
// copied back, every public page renders two footers and the policy links
// appear twice on the landing page.
test("the public footer lives in the shell, not on the landing page", () => {
	assert.match(publicLayout, /<footer className="public-footer">/);
	assert.doesNotMatch(landing, /<footer/);
});

// The /terms page is titled "License and terms", and the public footer says
// so. The app footer said "Terms" until 2026-11-01; both shells now use the
// page's own name.
test("both footers label /terms as the page is titled", () => {
	for (const [name, shell] of [
		["PublicLayout", publicLayout],
		["Layout", layout],
	]) {
		assert.match(shell, /href="\/terms">License and terms<\/a>/, `${name} must label /terms "License and terms"`);
		assertGone(shell, /href="\/terms">Terms<\/a>/, `${name} still labels /terms with the hosted-era "Terms"`);
	}
	// Anti-vacuity: the removal pattern rejects the label it replaced.
	assert.match('<a href="/terms">Terms</a>', /href="\/terms">Terms<\/a>/);
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
	for (const [name, page] of PAGES) {
		// Bounded to a single opening tag ([^>]* rather than [\s\S]*?), so an
		// unsafe link cannot ride along inside a match that passed on its
		// neighbour's rel.
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
		assertGone(page, /archimedes-arcadia/);
	}
});

// ── Privacy: the key facts ──────────────────────────────────────────────

test("privacy says the software is self-run and reports to no one", () => {
	const text = shown(privacy);
	assert.match(text, /Archimedes is software you run yourself\./);
	assert.match(text, phrase("APRIN Labs runs no hosted version of it and receives nothing from your installation"));
	assert.match(text, phrase("the code sends no telemetry, analytics or error reports to the project or to anyone else"));
	// The facts that make the sentence true. Visitor counting writes to the
	// instance's own Redis, and the browser beacon posts to the instance's API.
	for (const store of ["funnel_store.py", "visitor_insights_store.py", "telemetry_store.py"]) {
		const code = repo(`backend/archimedes/services/${store}`);
		assert.match(code, /import redis\.asyncio as aioredis/, `${store} must count into Redis`);
		assert.match(code, /REDIS_URL = os\.getenv\("REDIS_URL"/, `${store} must count into the instance's Redis`);
	}
	assert.match(app, /fetch\(`\$\{API_BASE\}\/api\/metrics\/funnel\/event`/);
	// Better Auth ships an opt-in telemetry package. auth/auth.js pins it off
	// explicitly, and nothing turns it on.
	const authConfig = repo("auth/auth.js");
	assert.match(authConfig, /telemetry:\s*\{\s*enabled:\s*false\s*\}/, "auth/auth.js must pin Better Auth telemetry off");
	assert.equal((authConfig.match(/telemetry:/g) || []).length, 1, "exactly one telemetry setting, the pinned-off one");
	assert.doesNotMatch(authConfig, /BETTER_AUTH_TELEMETRY/);
	assert.doesNotMatch(repo(".env.example"), /BETTER_AUTH_TELEMETRY/);
	assert.doesNotMatch(repo("docker-compose.yml"), /BETTER_AUTH_TELEMETRY/);
});

test("privacy says where an instance keeps its data and points at the storage inventory", () => {
	const text = shown(privacy);
	assert.match(text, phrase("Your instance keeps its records in the Postgres database and the Redis cache that you run"));
	assert.match(privacy, /<a href="\/security#storage-disclosure">Security page<\/a>/);
	assert.match(storageDisclosure, /id="storage-disclosure"/, "the anchor the page links to must exist");
	assert.match(text, /API keys as salted hashes/);
	assert.match(repo("backend/archimedes/models/api_key.py"), /salt: Mapped\[str\]/);
});

// The services a stock install contacts with nothing configured. Each name on
// the page is tied to the code default that makes it true, so changing the
// default without the page (or the page without the default) fails here.
test("privacy names what a stock install contacts, matching the code defaults", () => {
	const text = shown(privacy);
	const defaults = [
		["Yahoo Finance", "backend/archimedes/services/market_data_provider.py", /if not raw:\s*\n\s*return "yfinance"/],
		["The Arc testnet public RPC", "ui/src/chain-config.js", /DEFAULT_RPC_URL = 'https:\/\/rpc\.testnet\.arc\.network'/],
		["Hugging Face", "backend/archimedes/services/paper_rag.py", /os\.getenv\("FUSION_SEMANTIC_RETRIEVAL", "true"\)/],
		["jsDelivr", "nginx/nginx.conf", /"~\^\/docs" "[^"]*https:\/\/cdn\.jsdelivr\.net/],
	];
	for (const [name, file, pattern] of defaults) {
		assert.match(text, new RegExp(name), `the page must name ${name}`);
		assert.match(repo(file), pattern, `${file} no longer has the default that makes "${name}" true`);
	}
	// The boot warmup is why Yahoo Finance is contacted "when the server starts".
	assert.match(text, /first contacted when the server starts/);
	assert.match(repo("backend/archimedes/services/request_path_warmup.py"), /os\.getenv\("REQUEST_PATH_WARMUP", "true"\)/);
	// Bedrock is the template's choice, used only with AWS credentials.
	assert.match(text, phrase("The example .env file selects Bedrock, which is used only when AWS credentials are available."));
	assert.match(repo(".env.example"), /^LLM_PROVIDER=bedrock_converse$/m);
	assert.match(repo("backend/archimedes/services/llm_backend.py"), /if boto3\.Session\(\)\.get_credentials\(\) is None:/);
	// Email goes to SES only when chosen; the default is the log.
	assert.match(text, phrase("otherwise each email is written to the sign-in service’s log"));
	assert.match(repo("auth/mailer.js"), /\(env\.EMAIL_MAILER \|\| 'console'\) === 'ses'/);
});

test("privacy names every service it contacts only when configured, and says whose terms apply", () => {
	const text = shown(privacy);
	assert.match(text, /handles it under its own terms/);
	for (const name of [
		"LLM_PROVIDER",
		"Amazon Bedrock",
		"Anthropic",
		"OpenAI",
		"Ollama",
		"Tiingo",
		"CoinGecko",
		"Pyth",
		"Circle",
		"developer-controlled wallets",
		"Gateway",
		"passkey wallets",
		"Google and GitHub",
		"SES",
		"SSM",
		"S3",
		"arXiv",
	]) {
		assert.match(text, new RegExp(name), `the configured-services list must name ${name}`);
	}
	assert.match(text, phrase("Anything written to the blockchain is public and permanent."));
	assert.match(text, /PAPER_TRACE_ANCHOR/);
	assert.match(text, phrase("loads no third-party scripts, fonts or analytics"));
	// No CDN font or script in the app shell.
	const indexHtml = readFileSync(new URL("../index.html", import.meta.url), "utf8");
	assert.doesNotMatch(indexHtml, /<script[^>]+src="https?:/);
	assert.doesNotMatch(indexHtml, /<link[^>]+href="https?:\/\/[^"]*(?:fonts|cdn)/);
});

// The CLI and MCP server defaulted to the hosted address until 2026-11-01,
// and the page carried an exception saying so. Both now default to the local
// stack, so the exception is gone. The defaults are pinned here too: the
// sentence may only stay gone while neither client points anywhere but the
// instance you run.
test("privacy names no hosted default for the CLI or the MCP server", () => {
	for (const file of ["cli/src/archimedes_cli/cli.py", "mcp-server/src/archimedes_mcp/credentials.py"]) {
		assert.match(
			repo(file),
			/^DEFAULT_API_URL = "http:\/\/localhost:8080"$/m,
			`${file} must default to the local stack; if it points elsewhere, the page must say so`,
		);
	}
	const exception = /One exception to these lists|still default to archimedes-arc\.com/;
	// Anti-vacuity: the pattern rejects the sentence it replaced.
	assert.match(
		"One exception to these lists: the command-line client and the MCP server still default to archimedes-arc.com.",
		exception,
	);
	const body = rendered(privacy);
	assertGone(body, exception, "the clients no longer default to the hosted address");
	assertGone(body, /archimedes-arc\.com/, "the page body names no hosted address");
});

test("privacy puts the operator of a shared instance in charge of its disclosure", () => {
	assert.match(
		shown(privacy),
		phrase(
			"If you run an instance for other people, telling them what it collects, and which of these services it sends their data to, is your job. This page describes the software, not your deployment.",
		),
	);
});

// No page says what became of the former hosted service or its data. That
// waits for the teardown to actually delete it (owner, 2026-11-01), so no
// sentence here can claim it early.
test("no page claims the former hosted service's data was deleted", () => {
	const closure = /has closed|has been deleted|was deleted|OWNER-CONFIRM/;
	for (const [name, source] of [
		["Privacy", privacy],
		["Terms", terms],
		["AuthPage", authPage],
	]) {
		assertGone(source, closure, `${name} must not claim the hosted service closed or its data was deleted`);
	}
});

// ── Terms: the key facts ────────────────────────────────────────────────

test("terms state the public-domain license and link the repo's LICENSE", () => {
	const text = shown(terms);
	assert.match(text, /License and terms/);
	assert.match(text, phrase("Archimedes is free and unencumbered software released into the public domain under the Unlicense"));
	assert.match(terms, /href="https:\/\/github\.com\/aprin-labs\/archimedes\/blob\/main\/LICENSE"/);
	assert.match(text, phrase("It is provided “as is”, without warranty of any kind."));
	assert.match(text, /The LICENSE file is the full text\./);
	// The page's paraphrase stands on the file it names.
	const license = repo("LICENSE");
	assert.match(license, /^This is free and unencumbered software released into the public domain\./);
	assert.match(license, /THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND/);
	assert.match(license, /unlicense\.org/);
	assert.match(text, phrase("self-hosted fonts (SIL Open Font License)"));
});

test("terms state the research-and-testnet position", () => {
	const text = shown(terms);
	assert.match(text, /Nothing Archimedes produces is financial advice\./);
	assert.match(text, /Backtests and rigor verdicts are research outputs/);
	assert.match(text, phrase("Paper trading is simulated."));
	assert.match(text, phrase("No order is placed, and nothing is bought or sold."));
	assert.match(text, /Arc public testnet, whose test assets have no monetary value/);
});

test("terms put keys, funds, credentials and third-party terms on the operator", () => {
	const text = shown(terms);
	assert.match(text, phrase("You are responsible for every key, wallet, fund and API credential you give an instance or connect to it"));
	assert.match(text, phrase("You are also responsible for following the terms of every service you connect."));
});

// Governing law was dropped with the service: there is no agreement left for
// a choice-of-law clause to govern, and the Unlicense carries none.
test("terms carry no governing-law or forum clause", () => {
	assertGone(rendered(terms), /Governing law|Illinois|courts located/i);
	// The Unlicense names no governing law ("jurisdictions" in LICENSE:8 is
	// about where copyright exists, not a choice of law).
	assert.doesNotMatch(repo("LICENSE"), /governed by|governing law|laws of the State/i);
});

// ── The hosted-service claims stay gone ─────────────────────────────────

test("neither page carries the hosted service's contact, payment, age or suspension clauses", () => {
	for (const [name, page] of PAGES) {
		const body = rendered(page);
		// The contact address went with the AWS teardown; contact is the issue tracker.
		assertGone(page, /privacy@/, `${name}: privacy@ is gone`);
		assertGone(body, /mailto:/, `${name}: no mail contact`);
		// Payments: the price, the paywall, the free allowance.
		assertGone(body, /\$\s?\d|\bUSDC\b|paywall|free generations?|settles? for real|refund/i, `${name}: no payment clauses`);
		// The 18-or-older rule.
		assertGone(body, /\b18\b|under 18|or older|children/i, `${name}: no age rule`);
		// Account suspension and closure.
		assertGone(body, /suspen|terminat|close (?:an|your) account|closing an account/i, `${name}: no suspension clause`);
		// The service-era acceptance and the owner's promises about the service.
		assertGone(body, /Using the site means you accept|rather than editing quietly|We will update this page whenever|We may also stop offering the service/i, `${name}: no service promises`);
		assertGone(body, /Terms of Service|Privacy Policy/, `${name}: the hosted-era page names are gone`);
	}
});

// ── The sign-up screen's notice ─────────────────────────────────────────

// The sign-up screen renders outside both shells, so neither footer's policy
// links reach it; it carries them itself, above the OAuth buttons (which
// create accounts too, from /sign-in as well). The agreement and the
// 18-or-older confirmation went with the hosted service.
test("the sign-in screen links both pages without the hosted-service agreement", () => {
	const notice = /Your\s+account\s+is\s+stored\s+in\s+this\s+instance&rsquo;s\s+own\s+database\./;
	assert.match(authPage, notice);
	assert.match(authPage, /href="\/privacy">Privacy notice<\/a>/);
	assert.match(authPage, /href="\/terms">License and terms<\/a>/);
	assertGone(authPage, /By\s+creating\s+an\s+account\s+you\s+agree/, "there is no service to agree to");
	assertGone(authPage, /18\s+or\s+older/, "the age rule went with the service");
	assertGone(authPage, /Terms of Service|Privacy Policy/);
	// Everything between the cross-link row and the OAuth group renders on
	// both screens, so the notice is not gated on the sign-up mode.
	const aboveOauth = authPage.slice(
		authPage.indexOf('className="auth-alt-action"'),
		authPage.indexOf('className="auth-social-group"'),
	);
	assert.ok(aboveOauth.length > 0, "both markers must exist");
	assert.match(aboveOauth, notice, "the notice must sit above the OAuth buttons");
	assert.doesNotMatch(aboveOauth, /\{\s*creating\s*&&/, "the notice must show on /sign-in as well");
	assert.equal((authPage.match(/Your\s+account\s+is\s+stored/g) || []).length, 1, "one notice, not a second copy");
});

// ── The consent banner (unchanged by the 2026-11-01 rewrite) ────────────

// The banner and the consent status used to say nothing optional is written
// and that switching off deletes it, while archimedes_vid (an analytics entry)
// is set by the server whatever the choice and the purge sweeps only web storage.
test("the consent banner and status do not contradict the server-set visitor cookie", () => {
	assert.doesNotMatch(consentBanner, /nothing\s+optional\s+is\s+written/);
	assert.match(consentBanner, /our\s+server\s+still\s+sets\s+its\s+anonymous\s+visitor\s+cookie\s+\(archimedes_vid\)/);
	assert.doesNotMatch(consentChoices, /has\s+been\s+deleted\s+from\s+this\s+browser\."/);
	assert.match(consentChoices, /The\s+server's\s+archimedes_vid\s+cookie\s+stays/);
	assert.match(consentChoices, /the\s+server\s+sets\s+archimedes_vid\s+either\s+way/);
	// The banner's optional count is browser storage only: archimedes_vid is
	// an analytics entry the server sets whatever the choice.
	assert.match(consentBanner, /entry\.store\s*!==\s*"cookie"/);
	assert.match(consentBanner, /more,\s+in\s+your\s+browser,\s+are\s+optional/);
});
