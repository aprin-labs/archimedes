import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { parseFeatures } from "../src/features.js";
import {
	isAnonymousAppPage,

	canNavigateTo,
	pageToPath,
	passportBackLabel,
	passportBackPage,
	postAuthPath,
	resolveRoute,
	safeNextPath,
	visibleNavigation,
} from "../src/routes.js";

// Test-only override restoring the hidden roadmap surfaces (#1266): the
// build-time VITE_ROADMAP_SURFACES flag is off under node, and
// parseFeatures() never emits this key, so app code can't pass it.
const ROADMAP_ON = { quant: true, roadmapSurfaces: true };

test("landing, security, and architecture remain public", () => {
	assert.equal(resolveRoute("/").kind, "public");
	assert.equal(resolveRoute("/architecture").kind, "public");
	const security = resolveRoute("/security");
	assert.equal(security.kind, "public");
	assert.equal(security.page, "security");
	assert.equal(pageToPath("security"), "/security");
	assert.equal(safeNextPath("/security"), "/app");
});

// Better Auth's account-linking guard (auth/auth.js disableImplicitLinking)
// redirects a rejected OAuth sign-in to `/?error=account_not_linked` — the
// bare landing route, which has no sign-in form. Landing must not swallow it:
// resolveRoute bounces it to /sign-in, the actual sign-in surface, instead.
test("landing OAuth error bounces to the sign-in surface instead of being swallowed (#1420)", () => {
	const route = resolveRoute("/", "?error=account_not_linked");
	assert.equal(route.kind, "redirect");
	assert.equal(route.redirect, "/sign-in?error=account_not_linked");
});

test("landing with no error query param stays plain public landing", () => {
	const route = resolveRoute("/", "");
	assert.equal(route.kind, "public");
	assert.equal(route.redirect, null);
});

test("an error query param reaching /sign-in directly is carried onto the route", () => {
	const route = resolveRoute("/sign-in", "?error=account_not_linked");
	assert.equal(route.kind, "auth");
	assert.equal(route.error, "account_not_linked");
});

// Mutation-prove: this assertion is worthless if it also passes with the
// `error` field never wired into the redirect target. Removing the
// `?error=...` suffix from routes.js's redirect template (or dropping the
// `pathname === '/' && query.error` guard entirely) makes this test fail —
// confirmed by hand before this file was committed.
test("the redirect target actually encodes the error value, not just any redirect", () => {
	const route = resolveRoute("/", "?error=some_other_code");
	assert.equal(route.redirect, "/sign-in?error=some_other_code");
});

test("account routes remain public", () => {
	assert.equal(resolveRoute("/sign-in").kind, "auth");
	assert.equal(resolveRoute("/sign-up").kind, "auth");
});

test("reset-password is a public auth route (#1323)", () => {
	const route = resolveRoute("/reset-password");
	assert.equal(route.kind, "auth");
	assert.equal(route.page, "reset-password");
});

test("application routes use /app boundary", () => {
	const route = resolveRoute("/app/library", "?tab=examples");
	assert.equal(route.kind, "app");
	assert.equal(route.page, "library");
	assert.equal(route.tab, "examples");
	assert.equal(
		pageToPath("library", { tab: "examples" }),
		"/app/library?tab=examples",
	);
});

test("deep application routes retain identifiers", () => {
	// vault-detail is a hidden roadmap surface (#1266) — its identifier
	// extraction is pinned under the flag-on override.
	assert.equal(
		resolveRoute("/app/portfolio/vaults/0x123", "", ROADMAP_ON).vaultAddress,
		"0x123",
	);
	assert.equal(resolveRoute("/app/strategy/alpha").strategyId, "alpha");
	assert.equal(
		pageToPath("strategy", { strategyId: "alpha beta" }),
		"/app/strategy/alpha%20beta",
	);
});

test("legacy private path redirects under app boundary", () => {
	assert.deepEqual(resolveRoute("/generate").redirect, "/app/generate");
	assert.deepEqual(
		resolveRoute("/library", "?highlight=alpha").redirect,
		"/app/library?highlight=alpha",
	);
	assert.deepEqual(
		resolveRoute("/portfolio/vaults/0x123").redirect,
		"/app/portfolio/vaults/0x123",
	);
});

test("unknown route is not silently treated as landing", () => {
	assert.equal(resolveRoute("/gone").kind, "not-found");
});

test("post-auth redirect accepts only local app paths", () => {
	assert.equal(
		safeNextPath("/app/portfolio?tab=mine"),
		"/app/portfolio?tab=mine",
	);
	assert.equal(
		postAuthPath("?next=/app/library&highlight=alpha&tab=generated"),
		"/app/library?highlight=alpha&tab=generated",
	);
	assert.equal(
		postAuthPath("?next=https://evil.example/app&tab=generated"),
		"/app",
	);
	assert.equal(safeNextPath("https://evil.example/app"), "/app");
	assert.equal(safeNextPath("//evil.example/app"), "/app");
	assert.equal(safeNextPath("/architecture"), "/app");
});

test("feature payload accepts booleans only", () => {
	assert.deepEqual(parseFeatures({ quant: false }, { quant: true }), {
		quant: false,
	});
	assert.deepEqual(parseFeatures({ quant: "false" }, { quant: true }), {
		quant: true,
	});
});

test("quant navigation and direct route share feature result", () => {
	const nav = [{ id: "library" }, { id: "quant" }];
	// Third arg = user. Passing one exercises the authenticated filter; the
	// anonymous default is pinned separately below.
	assert.deepEqual(visibleNavigation(nav, { quant: false }, { id: "u1" }), [
		{ id: "library" },
	]);
	assert.equal(
		resolveRoute("/app/quant", "", { quant: false }).kind,
		"not-found",
	);
	assert.equal(resolveRoute("/app/quant", "", { quant: true }).page, "quant");
});

test("public shell lazy-loads wallet and protected application code", () => {
	const app = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
	const authenticated = readFileSync(
		new URL("../src/AuthenticatedApp.jsx", import.meta.url),
		"utf8",
	);
	// Quote-style agnostic (rebrand formats with double quotes).
	assert.match(app, /lazy\(\(\) => import\(["']\.\/AuthenticatedApp["']\)\)/);
	assert.doesNotMatch(app, /from '\.\/config'/);
	assert.match(authenticated, /from ["']\.\/config["']/);
});

test("anonymous browse pages resolve as app routes that allow no session (#1753)", () => {
	for (const path of ["/app", "/app/explore", "/app/corpus"]) {
		const route = resolveRoute(path);
		assert.equal(route.kind, "app", path);
		assert.equal(route.anonymousOk, true, path);
	}
});

test("ANON_APP_PAGES is Explore and Corpus — the owner's decision, pinned (#1753)", () => {
	// The owner's call on #1753 narrowed #1194 rev d: signed-out visitors
	// browse Explore and Corpus, and nothing else. The per-path assertions
	// above and below are both one-directional — this is the set itself, so
	// widening it is a deliberate edit to a line that says whose call it is.
	// Its server-side half (the nginx carve-outs) is pinned, and locked to
	// this same set, by backend/tests/test_nginx_anonymous_carve_outs.py.
	for (const page of ["explore", "corpus"]) {
		assert.equal(isAnonymousAppPage(page), true, page);
	}
	for (const page of [
		"leaderboard",
		"strategy",
		"library",
		"paper",
		"generate",
		"account",
		"insights",
		"quant",
		"reasoning",
		"learnings",
		"portfolio",
		"marketplace",
		"publish",
		"subscriptions",
	]) {
		assert.equal(isAnonymousAppPage(page), false, page);
	}
});

test("the leaderboard and the strategy passport are gated (#1753)", () => {
	// Both WERE anonymous under #1194 rev d. The owner gated them, so a
	// signed-out visitor opening either must be routed to sign-in rather than
	// served the page — App.jsx keys that bounce off `anonymousOk`, and nginx
	// answers the cold load with its own @sign_in 302 (next= preserved).
	const board = resolveRoute("/app/leaderboard");
	assert.equal(board.kind, "app");
	assert.equal(board.anonymousOk, false);
	const passport = resolveRoute("/app/strategy/alpha");
	assert.equal(passport.kind, "app");
	assert.equal(passport.page, "strategy");
	assert.equal(passport.strategyId, "alpha");
	assert.equal(passport.anonymousOk, false);
});

test("a gated deep link survives the round trip through sign-in (#1753)", () => {
	// Gating the passport is only honest if the share link still lands where
	// it pointed after the visitor signs in. nginx's @sign_in emits
	// `302 /sign-in?next=$uri&$args` and App.jsx's redirect effect builds the
	// same shape client-side; AuthPage feeds that query to postAuthPath.
	assert.equal(
		postAuthPath("?next=%2Fapp%2Fstrategy%2Falpha"),
		"/app/strategy/alpha",
	);
	assert.equal(
		postAuthPath("?next=%2Fapp%2Fstrategy%2Falpha%3Ftab%3Dbrief"),
		"/app/strategy/alpha?tab=brief",
	);
	// The assertion above encodes `?tab=brief` INSIDE `next`, which is not the
	// shape production emits: `return 302 /sign-in?next=$uri&$args` puts the
	// deep link's args alongside `next` as SIBLING params, and a different
	// branch of postAuthPath reassembles them onto the destination. That branch
	// was reachable by no assertion here — deleting it whole left this test
	// green — so the nginx shape is pinned literally.
	assert.equal(
		postAuthPath("?next=%2Fapp%2Fstrategy%2Fabc123&tab=brief&ref=twitter"),
		"/app/strategy/abc123?tab=brief&ref=twitter",
	);
	assert.equal(postAuthPath("?next=%2Fapp%2Fleaderboard"), "/app/leaderboard");
	// An off-site `next` is still refused — gating a page must not turn the
	// sign-in redirect into an open redirect.
	assert.equal(postAuthPath("?next=https%3A%2F%2Fevil.example%2Fapp"), "/app");
	// ...and a refused `next` takes its sibling args down with it, rather than
	// grafting an attacker's query onto the fallback destination.
	assert.equal(
		postAuthPath("?next=https%3A%2F%2Fevil.example%2Fapp&tab=x"),
		"/app",
	);
});

test("auth-required pages stay auth-required", () => {
	for (const path of [
		"/app/generate",
		"/app/library",
		"/app/paper",
		"/app/account",
	]) {
		const route = resolveRoute(path);
		assert.equal(route.kind, "app", path);
		assert.equal(route.anonymousOk, false, path);
	}
	// The hidden surfaces stay auth-required in flag-on builds too.
	for (const path of ["/app/portfolio", "/app/portfolio/vaults/0x123"]) {
		const route = resolveRoute(path, "", ROADMAP_ON);
		assert.equal(route.kind, "app", path);
		assert.equal(route.anonymousOk, false, path);
	}
});

test("roadmap surfaces are hidden by default: flat routes, deep links, nav (#1266)", () => {
	for (const path of [
		"/app/portfolio",
		"/app/marketplace",
		"/app/publish",
		"/app/subscriptions",
		"/app/learnings",
	]) {
		assert.equal(resolveRoute(path).kind, "not-found", path);
	}
	// The #1194 handoff case: a feature-disabled page must NOT stay reachable
	// via its deep link — deepRoutes shares the same featureEnabled() gate.
	assert.equal(
		resolveRoute("/app/marketplace/strategy/alpha").kind,
		"not-found",
	);
	assert.equal(resolveRoute("/app/portfolio/vaults/0x123").kind, "not-found");
	// Nav: Portfolio drops and the Market group empties even for a signed-in
	// user (Layout skips emptied groups, so no bare "Market" header remains).
	const nav = [
		{ id: "portfolio" },
		{ id: "marketplace" },
		{ id: "publish" },
		{ id: "subscriptions" },
		{ id: "learnings" },
		{ id: "library" },
	];
	assert.deepEqual(visibleNavigation(nav, { quant: true }, { id: "u1" }), [
		{ id: "library" },
	]);
	// And the flag-on build restores all of it unchanged.
	assert.equal(resolveRoute("/app/marketplace", "", ROADMAP_ON).page, "marketplace");
	assert.equal(
		resolveRoute("/app/marketplace/strategy/alpha", "", ROADMAP_ON).strategyId,
		"alpha",
	);
	assert.equal(visibleNavigation(nav, ROADMAP_ON, { id: "u1" }).length, 6);
});

test("Library's in-page Published tab hides with the marketplace surface it leads into (#1324)", () => {
	// #1266's dead-door audit only checked route/nav call sites
	// (onNavigate/setPage/navigateToPage); it couldn't see a tab that
	// renders and fetches inline with no route of its own. Strategies.jsx
	// (the Library page, itself NOT a hidden route) must gate its Published
	// tab the same way routes.js gates a hidden page: import the one flag
	// and use it, not a second bespoke check.
	const strategiesSrc = readFileSync(
		new URL("../src/components/Strategies.jsx", import.meta.url),
		"utf8",
	);
	assert.match(
		strategiesSrc,
		/import \{ ROADMAP_SURFACES_ENABLED \} from '\.\.\/featureFlags\.js'/,
	);
	// 1. The fetch must not fire unconditionally — a hidden tab that still
	// calls the hidden API on every Library load is the exact bug filed.
	assert.match(
		strategiesSrc,
		/ROADMAP_SURFACES_ENABLED \? apiGet\('\/api\/marketplace\/my-published'\) : Promise\.resolve\(\[\]\)/,
	);
	// 2. The tab button must not render unconditionally. Anchored directly to
	// the Published button's own attributes (no `[\s\S]*?` gap) — a lazy gap
	// here previously let an unrelated gated button elsewhere in the file
	// satisfy the assertion while the real Published tab stayed ungated.
	// Demonstrated: inserting `{ROADMAP_SURFACES_ENABLED && (<button ...>Publish
	// to marketplace</button>)}` above the tab row while removing the Published
	// tab's own gate passed the old regex and fails this one.
	assert.match(
		strategiesSrc,
		/\{ROADMAP_SURFACES_ENABLED && \(\s*<button\s+type="button"\s+className=\{`tag \$\{activeTab === 'published'/,
	);
	// 3. The tab panel must not render either — belt-and-suspenders against
	// a stale ?tab=published deep link coercing activeTab directly.
	assert.match(
		strategiesSrc,
		/activeTab === 'published' && ROADMAP_SURFACES_ENABLED && \(/,
	);
	// 4. The activeTab deep-link fallback: with the flag off, a stale
	// ?tab=published link must not leave activeTab pinned to 'published' —
	// every panel (generated/examples/now-gated published) would then
	// evaluate false and Library would render an empty content area.
	assert.match(
		strategiesSrc,
		/defaultTab === 'published' && !ROADMAP_SURFACES_ENABLED\) return 'generated'/,
	);
});

test("the strategy passport's back control never signs out an anonymous visitor (#1370)", () => {
	// 'library' is wallet-gated and not anonymous-OK, so resolving the back
	// button straight to onNavigate('library') tripped App.jsx's
	// anonymous-page redirect and bounced a visitor who was never signed in
	// out to /sign-in. Since #1753 the passport is itself gated, so the
	// user == null branch is a FAIL-SAFE rather than a live flow — a control
	// must not be able to eject a sessionless render whoever produced it.
	// The helper must resolve anonymous visitors to a page
	// isAnonymousAppPage() actually allows.
	assert.equal(isAnonymousAppPage(passportBackPage(null)), true);
	// A signed-in visitor keeps going back to their own Library.
	assert.equal(passportBackPage({ id: "u1" }), "library");
	// The button's own label must track where it actually navigates — a
	// control that says "Back to Library" while landing on Explore is a
	// mislabeled affordance, not a fixed one.
	assert.equal(passportBackLabel(null), "← Back to Explore");
	assert.equal(passportBackLabel({ id: "u1" }), "← Back to Library");
});

test("the strategy passport component actually wires the back-navigation helpers at both back-button call sites (#1370)", () => {
	// The test above only proves routes.js's helpers are correct in
	// isolation — it imports nothing from StrategyPassport.jsx, so reverting
	// the component's two `onClick`/label call sites back to the literal
	// regression (`onNavigate("library")` / hard-coded "← Back to Library")
	// would still leave it fully green. Read the component source directly,
	// precedent: this file's own "Library's in-page Published tab..." test
	// above and backend/tests/test_breadcrumbs.py's source-parsing tests.
	const passportSrc = readFileSync(
		new URL("../src/components/StrategyPassport.jsx", import.meta.url),
		"utf8",
	);
	const wiredNavigate = passportSrc.match(
		/onClick=\{\(\) => onNavigate\(passportBackPage\(user\)\)\}/g,
	);
	assert.equal(
		wiredNavigate?.length,
		2,
		`expected both back buttons to call onNavigate(passportBackPage(user)), found ${wiredNavigate?.length ?? 0}`,
	);
	const wiredLabel = passportSrc.match(/\{passportBackLabel\(user\)\}/g);
	assert.equal(
		wiredLabel?.length,
		2,
		`expected both back buttons to render {passportBackLabel(user)}, found ${wiredLabel?.length ?? 0}`,
	);
	assert.doesNotMatch(
		passportSrc,
		/onNavigate\(\s*["']library["']\s*\)/,
		"a back button still hard-codes onNavigate(\"library\") instead of the anonymous-safe helper",
	);
	assert.doesNotMatch(
		passportSrc,
		/←\s*Back to Library\s*</,
		"a back button still hard-codes the Library label instead of passportBackLabel(user)",
	);
});

test("anonymous nav shows browse + the Generate conversion path, nothing stateful", () => {
	const nav = [
		{ id: "explore" },
		{ id: "corpus" },
		{ id: "leaderboard" },
		{ id: "generate" },
		{ id: "portfolio" },
		{ id: "marketplace" },
		{ id: "account" },
	];
	// 'leaderboard' is filtered out as of #1753: it is no longer browsable,
	// and a nav entry for a page canNavigateTo() refuses is an affordance that
	// only ejects the visitor to /sign-in. 'generate' stays — it is the
	// conversion path, and routing to sign-in is what clicking it MEANS.
	assert.deepEqual(visibleNavigation(nav, { quant: true }, null), [
		{ id: "explore" },
		{ id: "corpus" },
		{ id: "generate" },
	]);
});

test("canNavigateTo gates on ANON_APP_PAGES for a null user, always true when signed in (#1364)", () => {
	// The onboarding tour's card 3 ('library') and card 4 ('reasoning') are
	// app pages an anonymous visitor may not open (routes.js ANON_APP_PAGES).
	// Before this predicate existed, the tour navigated to them unconditionally
	// and App.jsx's anonymous-app-page guard replaced the whole page with
	// /sign-in — this is the pure check that now gates that navigation.
	assert.equal(canNavigateTo("library", null), false);
	assert.equal(canNavigateTo("reasoning", null), false);
	// 'generate' is in ANON_NAV_IDS (visible in the anon nav, so it measures
	// fine on desktop) but NOT in ANON_APP_PAGES — the exact trap the issue's
	// anti-goal warns against gating on an id allowlist instead of this.
	assert.equal(canNavigateTo("generate", null), false);
	assert.equal(canNavigateTo("explore", null), true);
	assert.equal(canNavigateTo("corpus", null), true);
	// 'leaderboard' and 'strategy' left ANON_APP_PAGES with #1753 and must
	// now be refused for an anonymous visitor exactly like 'library'.
	assert.equal(canNavigateTo("leaderboard", null), false);
	assert.equal(canNavigateTo("strategy", null), false);
	// Any page is navigable once a user is present — canNavigateTo does not
	// re-derive ANON_APP_PAGES for the signed-in branch.
	assert.equal(canNavigateTo("library", { id: "u1" }), true);
	assert.equal(canNavigateTo("reasoning", { id: "u1" }), true);
	assert.equal(canNavigateTo("generate", { id: "u1" }), true);
});
