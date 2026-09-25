import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	_resetAdminProbeCache,
	ADMIN_PROBE_TTL_MS,
	getCachedAdminProbe,
} from "../src/adminProbeCache.js";
import {
	filterInsightsNavItem,
	isInsightsPageBlocked,
	resolveInsightsAdminState,
	resolveInsightsView,
} from "../src/insightsGate.js";
import {
	_resetInsightsAdminMemo,
	adminIdentityKey,
	normalizeWalletAddress,
	readInsightsAdmin,
	rememberInsightsAdmin,
} from "../src/insightsAdminMemo.js";
import { NAV } from "../src/navConfig.js";

// ── getCachedAdminProbe: shared TTL cache backing fetchAdminProbe()
// (src/adminProbe.js) — same shape as healthCache.js's getCachedHealth
// (#1333) / agentStatusCache.js's getCachedAgentStatus (#1382 review).
// Owner directive 2026-08-20: /app/insights is admin-only, gated by
// GET /api/metrics/private/whoami; this cache is what lets Layout.jsx's Ops
// nav item and App.jsx's page gate share one probe per navigation.

test("getCachedAdminProbe: a second call within the TTL window reuses the first call's promise instead of probing again", async () => {
	_resetAdminProbeCache();
	let calls = 0;
	const fetcher = async () => {
		calls += 1;
		return { admin: true, wallet: "0xadmin" };
	};
	const p1 = getCachedAdminProbe(fetcher, 1_000);
	const p2 = getCachedAdminProbe(fetcher, 1_000 + ADMIN_PROBE_TTL_MS - 1);
	assert.equal(p1, p2);
	await p1;
	assert.equal(calls, 1);
});

test("getCachedAdminProbe: a call at/after the TTL window probes again", async () => {
	_resetAdminProbeCache();
	let calls = 0;
	const fetcher = async () => {
		calls += 1;
		return { admin: false, wallet: null };
	};
	await getCachedAdminProbe(fetcher, 1_000);
	await getCachedAdminProbe(fetcher, 1_000 + ADMIN_PROBE_TTL_MS);
	assert.equal(calls, 2);
});

test("getCachedAdminProbe: a rejected fetch is not cached — the very next call retries rather than reusing the rejection", async () => {
	_resetAdminProbeCache();
	let calls = 0;
	const failingFetcher = async () => {
		calls += 1;
		throw new Error("network down");
	};
	const okFetcher = async () => {
		calls += 1;
		return { admin: true, wallet: "0xadmin" };
	};
	await assert.rejects(getCachedAdminProbe(failingFetcher, 1_000));
	assert.equal(calls, 1);
	const result = await getCachedAdminProbe(okFetcher, 1_001);
	assert.equal(calls, 2);
	assert.deepEqual(result, { admin: true, wallet: "0xadmin" });
});

test("_resetAdminProbeCache: forces the next call to probe again even within the TTL window", async () => {
	_resetAdminProbeCache();
	let calls = 0;
	const fetcher = async () => {
		calls += 1;
		return { admin: false, wallet: null };
	};
	await getCachedAdminProbe(fetcher, 1_000);
	_resetAdminProbeCache();
	await getCachedAdminProbe(fetcher, 1_001); // well within TTL of the first call
	assert.equal(calls, 2);
});

// ── insightsGate.js: the actual decisions App.jsx/Layout.jsx make from a
// probe result, exercised with REAL inputs rather than asserted on source
// text (round 3 review finding: every gate assertion below this point used
// to be a readFileSync + regex match, none of which executed the line that
// actually consumes a probe result — a mutation collapsing
// `setIsInsightsAdmin(admin)` to `setIsInsightsAdmin(true)` left every one
// of those regexes passing while every signed-in non-admin got the Ops nav
// item and the live dashboard). App.jsx/Layout.jsx now route every such
// decision through these three functions, so a mutation to any of them has
// a real behavioral test to fail. ─────────────────────────────────────────

test("resolveInsightsAdminState: only a literal admin:true probe result resolves to true", () => {
	assert.equal(resolveInsightsAdminState({ admin: true, wallet: "0xadmin" }), true);
	assert.equal(resolveInsightsAdminState({ admin: false, wallet: null }), false);
	// Truthy-but-not-`true` values (a mutation returning the wallet string,
	// or a stray "true" string from a bad deserialize) must NOT pass —
	// `=== true` is the whole point of this function existing.
	assert.equal(resolveInsightsAdminState({ admin: "true", wallet: null }), false);
	assert.equal(resolveInsightsAdminState({ admin: 1, wallet: null }), false);
	assert.equal(resolveInsightsAdminState(undefined), false);
});

test("isInsightsPageBlocked: blocks a denied (false) AND an unresolved (null) probe alike — only admin===true renders", () => {
	assert.equal(isInsightsPageBlocked("insights", true), false);
	assert.equal(isInsightsPageBlocked("insights", false), true);
	assert.equal(isInsightsPageBlocked("insights", null), true);
});

test("isInsightsPageBlocked: never blocks a route other than insights, regardless of admin state", () => {
	assert.equal(isInsightsPageBlocked("portfolio", false), false);
	assert.equal(isInsightsPageBlocked("portfolio", null), false);
	assert.equal(isInsightsPageBlocked(null, false), false);
});

test("filterInsightsNavItem: drops the insights item unless isAdmin===true; every other item passes through", () => {
	const items = [
		{ id: "insights", label: "Insights" },
		{ id: "account", label: "Account" },
	];
	assert.deepEqual(
		filterInsightsNavItem(items, true).map((i) => i.id),
		["insights", "account"],
	);
	assert.deepEqual(
		filterInsightsNavItem(items, false).map((i) => i.id),
		["account"],
	);
	// Mutation-check target: a truthy-but-not-strictly-true isAdmin (e.g. a
	// stray non-empty string) must not let the item through either.
	assert.deepEqual(
		filterInsightsNavItem(items, "yes").map((i) => i.id),
		["account"],
	);
});

// Round 4 review finding: the test above proves filterInsightsNavItem() works
// against SOME array shaped like Layout's "Ops" group — it does NOT prove it
// works against the group Layout actually renders. If a future edit renamed
// the group, changed the insights item's id, or reordered "Ops" so it were no
// longer last, the hand-built array above would keep passing while the real
// nav silently drifted out of the gate's reach. Importing NAV itself (now
// extracted to ../src/navConfig.js for exactly this reason) closes that gap.
test("filterInsightsNavItem: against Layout's REAL nav data, the Ops group's insights item is gated and account is not", () => {
	const ops = NAV.find((group) => group.group === "Ops");
	assert.ok(ops, "expected an 'Ops' group in the real NAV data");
	assert.deepEqual(
		filterInsightsNavItem(ops.items, true).map((i) => i.id),
		["insights", "account"],
	);
	assert.deepEqual(
		filterInsightsNavItem(ops.items, false).map((i) => i.id),
		["account"],
	);
	// Every OTHER group's items must pass through filterInsightsNavItem
	// completely unaffected — it must only ever touch the insights id,
	// regardless of which group it happens to live in today.
	for (const group of NAV) {
		if (group.group === "Ops") continue;
		assert.deepEqual(
			filterInsightsNavItem(group.items, false).map((i) => i.id),
			group.items.map((i) => i.id),
			`group ${group.group ?? "(home)"} must be untouched by the insights gate`,
		);
	}
});

// ── adminProbe.js: classifies apiGet's thrown Error shape (err.status set
// on every non-2xx HTTP response, per api.js) into an authoritative
// {admin:false} vs. a genuine network failure that must NOT be cached. ────

const adminProbeSrc = readFileSync(
	new URL("../src/adminProbe.js", import.meta.url),
	"utf8",
);

test("adminProbe.js: an authoritative 401/403 (err.status set) resolves to {admin:false}, not a rethrow", () => {
	assert.match(adminProbeSrc, /typeof err\.status === "number"/);
	assert.match(adminProbeSrc, /return \{ admin: false, wallet: null \}/);
});

test("adminProbe.js: a genuine network/parse failure (no err.status) is rethrown so adminProbeCache does not cache it", () => {
	// Mutation-check (CLAUDE.md § "Before you approve a merge", rule 4): if
	// this rethrow were replaced with another `return {admin:false,...}`,
	// EVERY failure (including a transient outage) would get cached as a
	// definitive "not admin" for the full TTL window, hiding a real admin's
	// nav item / page after a blip until the cache naturally expires.
	// Verified by temporarily changing the `throw err` line to
	// `return { admin: false, wallet: null }` and re-running this file
	// alone, which reproduced exactly the failure this assertion exists to
	// catch (the regex below stopped matching).
	assert.match(adminProbeSrc, /\n\t\tthrow err;\n/);
});

test("adminProbe.js: fetchAdminProbe never rejects — a non-admin/anonymous caller and a network failure both resolve to {admin:false}", () => {
	assert.match(
		adminProbeSrc,
		/getCachedAdminProbe\(_fetchWhoami\)\.catch\(\(\) => \(\{ admin: false, wallet: null \}\)\)/,
	);
});

test("adminProbe.js probes the private whoami endpoint, not the public metrics surface", () => {
	assert.match(adminProbeSrc, /apiGet\("\/api\/metrics\/private\/whoami"\)/);
});

// ── Wiring: App.jsx's page gate + Layout.jsx's nav gate both go through the
// shared fetchAdminProbe(), never a bespoke/duplicated check. ─────────────

const app = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const layout = readFileSync(
	new URL("../src/components/Layout.jsx", import.meta.url),
	"utf8",
);
const notFoundSrc = readFileSync(
	new URL("../src/components/NotFound.jsx", import.meta.url),
	"utf8",
);
const insights = readFileSync(
	new URL("../src/components/Insights.jsx", import.meta.url),
	"utf8",
);

test("App.jsx: the insights page gate calls the shared fetchAdminProbe(), routing its result through resolveInsightsAdminState()", () => {
	// Quote-style agnostic throughout this block: the calm-precision rebrand
	// reformats App.jsx to double quotes + semicolons. The guarded property is
	// WHICH module the gate goes through, not how the import is quoted.
	assert.match(app, /from ["']\.\/adminProbe\.js["']/);
	assert.match(app, /from ["']\.\/insightsGate\.js["']/);
	assert.match(
		app,
		/fetchAdminProbe\(\)\.then\(\(result\) => \{\n\s+if \(cancelled\) return;\n\s+const admin = resolveInsightsAdminState\(result\);\n\s+rememberInsightsAdmin\(identity, admin\);\n\s+setInsightsAdmin\(admin\);/,
		"the probe result must still be routed through resolveInsightsAdminState() — and the value written to the #1648 memo must be that SAME resolved boolean, not the raw probe body",
	);
	assert.doesNotMatch(
		app,
		/apiGet\(['"]\/api\/metrics\/private\/whoami['"]\)/,
		"App.jsx must not call the whoami endpoint directly — it has to go through the shared cache",
	);
});

test("App.jsx: a denied insights probe renders the SAME NotFound component as a true 404 — not a second, divergent 'access denied' block", () => {
	assert.match(app, /import NotFound from ["']\.\/components\/NotFound["']/);
	// Both branches return the identical <NotFound user={user} /> call —
	// counting occurrences (rather than matching each branch separately)
	// is what actually proves they're the same component, not two markup
	// blocks that happen to render similarly today and can drift apart.
	const matches = app.match(/return <NotFound user=\{user\} \/>/g) || [];
	assert.equal(
		matches.length,
		2,
		"expected exactly 2 call sites: the true not-found branch and the denied-insights branch",
	);
});

test("App.jsx: an anonymous visitor hitting /app/insights is never redirected to /sign-in (that would advertise the page exists)", () => {
	assert.match(
		app,
		/route\.anonymousOk \|\| route\.page === ["']insights["'] \|\| authLoading \|\| user\)\s*\n?\s*return/,
	);
});

test("Layout.jsx: the Ops nav item is filtered on the shared admin probe (via filterInsightsNavItem), defaulting closed", () => {
	assert.match(layout, /from "\.\.\/adminProbe\.js"/);
	assert.match(layout, /from "\.\.\/insightsGate\.js"/);
	assert.match(layout, /const \[isInsightsAdmin, setIsInsightsAdmin\] = useState\(false\)/);
	assert.match(
		layout,
		/filterInsightsNavItem\(\s*\n?\s*visibleNavigation\(group\.items, features, user\),\s*\n?\s*isInsightsAdmin,?\s*\n?\s*\)/,
	);
	assert.match(
		layout,
		/fetchAdminProbe\(\)\.then\(\(result\) => \{\n\s+if \(!cancelled\) setIsInsightsAdmin\(resolveInsightsAdminState\(result\)\)/,
	);
});

test("Layout.jsx: an anonymous visitor (no user) never even attempts the admin probe", () => {
	assert.match(layout, /if \(!userId\) \{\n\t\t\tsetIsInsightsAdmin\(false\);\n\t\t\treturn;\n\t\t\}/);
});

// ── Round 2: a wallet swap must re-run BOTH admin probes, not just clear the
// shared cache. AuthenticatedApp's wallet-changed handler already resets the
// cache; that alone only helps a FUTURE caller — App.jsx's page gate and
// Layout.jsx's nav gate each need their own trigger to actually become that
// caller again. ──────────────────────────────────────────────────────────

test("App.jsx: the insights admin probe re-runs on a wallet swap, not just on navigation (round 2)", () => {
	assert.match(
		app,
		/window\.addEventListener\(["']wallet-changed["'], onWalletChanged\)/,
		"App.jsx must listen for wallet-changed directly — it has no other way to learn the connected wallet changed",
	);
	assert.match(
		app,
		/\}, \[route\.page, walletAddr, userId\]\)/,
		"the insights-probe effect must depend on the wallet-change signal, not just route.page",
	);
	// #1648 / I-8 B2: the signal must be the address VALUE, not a counter.
	// A counter changes on EVERY wallet-changed event — including the
	// `accountsChanged` an injected provider re-fires with the same account on
	// tab focus — which is what made the page blank under an admin who was
	// already using it. A value lets React bail out on a no-op announcement.
	assert.doesNotMatch(
		app,
		/walletChangeSeq/,
		"the probe must not re-run on a monotonic counter bumped by every wallet-changed event (#1648)",
	);
	assert.match(
		app,
		/setWalletAddr\(normalizeWalletAddress\(event\?\.detail\?\.address\)\)/,
		"the wallet-changed handler must store the normalized address value",
	);
});

test("Layout.jsx: the Ops nav admin probe re-runs on a wallet swap, keyed on walletAddr (round 2)", () => {
	assert.match(
		layout,
		/\}, \[userId, walletAddr\]\);/,
		"the admin-probe effect must depend on walletAddr — require_platform_admin checks THIS wallet, not just the account",
	);
});

test("NotFound.jsx exists and is the single source of the 'does not exist' treatment", () => {
	assert.match(notFoundSrc, /Page not found/);
	assert.match(notFoundSrc, /export default function NotFound/);
});

test("Insights.jsx documents the admin-only gate and the #1028 D8 supersession", () => {
	assert.match(insights, /ADMIN-ONLY/);
	assert.match(insights, /SUPERSEDES issue #1028 D8/);
});

test("Insights.jsx loads the new admin-only engagement endpoint alongside the public aggregates", () => {
	assert.match(insights, /apiGet\('\/api\/metrics\/private\/engagement'\)/);
	// Public aggregate endpoints stay untouched — the gate moved on the page
	// and the router, not on these.
	assert.match(insights, /apiGet\('\/api\/metrics'\)/);
	assert.match(insights, /apiGet\('\/api\/metrics\/funnel'\)/);
	assert.match(insights, /apiGet\('\/api\/metrics\/visitors'\)/);
});

test("Insights.jsx never claims a settled payment volume outside the dry-run note (claims-must-be-true)", () => {
	assert.match(insights, /engagement\.payments\?\.settled_volume_usd == null \? '—'/);
});

// Round 4 review finding: generation_costs rows only exist for jobs that
// persisted >=1 strategy row (agents/generation_pipeline.py's
// _persist_generation_cost) — a job that consumed tokens but errored, was
// cancelled, or failed the rigor gate first leaves no row at all. "Total LLM
// tokens" implied an all-time platform total; it must be relabelled and
// carry the coverage caveat rather than present partial instrumentation as
// a universe total (claims-must-be-true).
test("Insights.jsx does not label the token tile as an all-time/total figure, and renders its coverage caveat", () => {
	assert.doesNotMatch(insights, /Total LLM tokens/);
	assert.match(insights, /LLM tokens \(measured jobs\)/);
	assert.match(insights, /engagement\.generation_costs\?\.note/);
});

// ── #1648 / I-8 B2: the NotFound flash ──────────────────────────────────────
//
// The gate's security posture was right and its rendering was wrong. App.jsx
// reset `insightsAdmin` to `null` unconditionally on every entry to
// /app/insights and on every `wallet-changed` event, and `null` rendered the
// not-found treatment — so an admin watched the page they are allowed to use
// flip NotFound → dashboard on every navigation, and blank again whenever an
// injected provider re-announced the same account on tab focus.
//
// The fix must not become an optimistic admin assumption. What follows pins
// both halves: an unknown state renders a holding state rather than a
// decision, and the only thing that can render the dashboard early is an
// answer the server actually gave, for this exact identity, this session.

test("resolveInsightsView: an UNRESOLVED probe inside a session renders the holding state — never the not-found treatment (#1648)", () => {
	assert.equal(resolveInsightsView("insights", null, true), "resolving");
	// Adversarial companion — revert resolveInsightsView to the old
	// `insightsAdmin !== true` collapse and this pair is what fails: the
	// unresolved case would return "not-found" here, which is the flash.
	assert.notEqual(resolveInsightsView("insights", null, true), "not-found");
});

test("resolveInsightsView: an AUTHORITATIVE denial still renders not-found, holding state or not (#1648 must not open the page)", () => {
	assert.equal(resolveInsightsView("insights", false, true), "not-found");
	assert.equal(resolveInsightsView("insights", false, false), "not-found");
	// The only input that renders the page is a literal true.
	assert.equal(resolveInsightsView("insights", true, true), "allow");
	for (const bogus of [undefined, 0, 1, "true", "admin", {}, []]) {
		assert.equal(
			resolveInsightsView("insights", bogus, true),
			"not-found",
			`a non-boolean gate state (${JSON.stringify(bogus)}) must never render the page`,
		);
	}
});

test("resolveInsightsView: an ANONYMOUS caller keeps the round-3 behaviour exactly — unresolved is indistinguishable from unknown-route (#1648 anti-goal)", () => {
	// The disclosure argument round 3 made is about someone OUTSIDE the
	// session boundary: a genuinely unknown route never shows a loading state,
	// so a loader here would mark the path as special. With no session in
	// play, unresolved must still render not-found on first paint.
	assert.equal(resolveInsightsView("insights", null, false), "not-found");
	assert.equal(resolveInsightsView("insights", null, undefined), "not-found");
});

test("resolveInsightsView: never blocks or stalls a route other than insights", () => {
	for (const state of [null, true, false]) {
		assert.equal(resolveInsightsView("library", state, true), "allow");
		assert.equal(resolveInsightsView(null, state, false), "allow");
	}
});

test("App.jsx: the insights branch dispatches on resolveInsightsView, and its holding arm renders neither NotFound nor the dashboard", () => {
	assert.match(app, /from ["']\.\/insightsAdminMemo\.js["']/);
	assert.match(
		app,
		/const view = resolveInsightsView\(\s*\n?\s*route\.page,\s*\n?\s*insightsAdmin,\s*\n?\s*authLoading \|\| Boolean\(user\),?\s*\n?\s*\);/,
		"the session input must be 'auth is still loading OR a user is present' — treating the pre-resolution instant as anonymous reintroduces the flash on a hard load",
	);
	assert.match(app, /if \(view === "not-found"\) return <NotFound user=\{user\} \/>;/);
	assert.match(app, /if \(view === "resolving"\) \{/);
	// The holding arm must not name the page it is holding for.
	assert.doesNotMatch(app, /Loading Insights/i);
	// Still exactly two NotFound call sites: the true 404 and the denial.
	// A third would mean the denial treatment had been forked again.
	const notFoundCalls = app.match(/return <NotFound user=\{user\} \/>/g) || [];
	assert.equal(notFoundCalls.length, 2);
});

test("App.jsx: the tab title still treats an unresolved probe as not-found (only the RENDER branch changed)", () => {
	// I-8 anti-goal: isInsightsPageBlocked's contract is unchanged, and the
	// title effect still uses it — so even during the holding state the tab
	// never reads "Insights · Archimedes" to a visitor the server has not
	// cleared.
	assert.match(app, /isInsightsPageBlocked\(route\.page, insightsAdmin\)/);
	assert.equal(isInsightsPageBlocked("insights", null), true);
	assert.equal(isInsightsPageBlocked("insights", false), true);
	assert.equal(isInsightsPageBlocked("insights", true), false);
});

// ── insightsAdminMemo: the last SERVER ANSWER, keyed on the identity it was
// given for. This is what makes the second and later visits flash-free, and
// what keeps that from being a guess. ───────────────────────────────────────

test("insightsAdminMemo: a remembered grant is readable only under the identity it was recorded for", () => {
	_resetInsightsAdminMemo();
	const walletA = adminIdentityKey("user-1", "0xAAA");
	rememberInsightsAdmin(walletA, true);
	assert.equal(readInsightsAdmin(walletA), true);
	// Same account, DIFFERENT wallet — require_platform_admin can answer
	// differently per wallet, so this must be a miss, not a stale true.
	assert.equal(readInsightsAdmin(adminIdentityKey("user-1", "0xBBB")), null);
	// Different account entirely — a miss.
	assert.equal(readInsightsAdmin(adminIdentityKey("user-2", "0xAAA")), null);
	// Anonymous — a miss, and cannot inherit the signed-in determination.
	assert.equal(readInsightsAdmin(adminIdentityKey(null, "0xAAA")), null);
});

test("insightsAdminMemo: an anonymous answer is never recorded — and cannot evict a signed-in one", () => {
	_resetInsightsAdminMemo();
	rememberInsightsAdmin(adminIdentityKey(null, "0xAAA"), true);
	assert.equal(readInsightsAdmin(adminIdentityKey(null, "0xAAA")), null);
	assert.equal(readInsightsAdmin(adminIdentityKey("user-1", "0xAAA")), null);
	// Dropping the anonymous write is what keeps it from CLOBBERING a real
	// one: without the guard, `memoKey` would be overwritten with null and the
	// grant below would be evicted — which is how a "flash-free" page starts
	// flashing again the moment a probe runs before auth resolves.
	const signedIn = adminIdentityKey("user-1", "0xAAA");
	rememberInsightsAdmin(signedIn, true);
	rememberInsightsAdmin(adminIdentityKey(null, "0xAAA"), false);
	assert.equal(readInsightsAdmin(signedIn), true);
});

test("insightsAdminMemo: only a literal true is remembered as a grant — a truthy non-boolean is recorded as a denial", () => {
	_resetInsightsAdminMemo();
	const id = adminIdentityKey("user-1", "0xAAA");
	rememberInsightsAdmin(id, "yes");
	assert.equal(readInsightsAdmin(id), false);
});

test("insightsAdminMemo: a DENIAL is memoized too — a non-admin's later visits render not-found with no holding state at all", () => {
	_resetInsightsAdminMemo();
	const id = adminIdentityKey("user-9", "0xNOTADMIN");
	rememberInsightsAdmin(id, false);
	assert.equal(readInsightsAdmin(id), false);
	assert.equal(
		resolveInsightsView("insights", readInsightsAdmin(id), true),
		"not-found",
		"a remembered denial must render the not-found treatment immediately, exactly like an unknown route",
	);
});

test("insightsAdminMemo: _resetInsightsAdminMemo forgets the grant (sign-out path)", () => {
	_resetInsightsAdminMemo();
	const id = adminIdentityKey("user-1", "0xAAA");
	rememberInsightsAdmin(id, true);
	_resetInsightsAdminMemo();
	assert.equal(readInsightsAdmin(id), null);
});

test("AuthContext clears the insights-admin memo on sign-out, alongside the probe cache", () => {
	const authContext = readFileSync(
		new URL("../src/AuthContext.jsx", import.meta.url),
		"utf8",
	);
	assert.match(authContext, /_resetInsightsAdminMemo\(\)/);
});

// ── The tab-focus case, end to end over the real modules ────────────────────

test("normalizeWalletAddress: a checksummed and a lowercase announcement of the SAME account are the same value (so React bails out and the probe does not re-run)", () => {
	const checksummed = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01";
	assert.equal(
		normalizeWalletAddress(checksummed),
		normalizeWalletAddress(checksummed.toLowerCase()),
	);
	assert.equal(normalizeWalletAddress(null), null);
	assert.equal(normalizeWalletAddress(undefined), null);
	assert.equal(normalizeWalletAddress(""), null);
	// A genuinely different account is a genuinely different value — the
	// bail-out must not swallow a real swap.
	assert.notEqual(
		normalizeWalletAddress(checksummed),
		normalizeWalletAddress("0x1111111111111111111111111111111111111111"),
	);
});

test("#1648 end to end: a tab-focus accountsChanged carrying the same account cannot blank a page the admin is already on", () => {
	_resetInsightsAdminMemo();
	const userId = "user-admin";
	const announced = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01";

	// First visit: the server answers admin.
	const firstIdentity = adminIdentityKey(
		userId,
		normalizeWalletAddress(announced),
	);
	rememberInsightsAdmin(firstIdentity, resolveInsightsAdminState({ admin: true }));
	assert.equal(resolveInsightsView("insights", true, true), "allow");

	// Tab focus: the injected provider re-announces the SAME account, this
	// time lowercased. Even if the effect were to re-run, the seed is the
	// memoized answer — not null — so the page never renders NotFound.
	const refocusIdentity = adminIdentityKey(
		userId,
		normalizeWalletAddress(announced.toLowerCase()),
	);
	assert.equal(refocusIdentity, firstIdentity);
	const seed = readInsightsAdmin(refocusIdentity);
	assert.equal(seed, true);
	assert.equal(resolveInsightsView("insights", seed, true), "allow");

	// A REAL swap to an unseen wallet is a memo miss — it must resolve from
	// the server (holding state), never carry the previous wallet's grant.
	const swapped = adminIdentityKey(userId, normalizeWalletAddress("0xBEEF"));
	const swappedSeed = readInsightsAdmin(swapped);
	assert.equal(swappedSeed, null);
	assert.equal(resolveInsightsView("insights", swappedSeed, true), "resolving");
	assert.notEqual(resolveInsightsView("insights", swappedSeed, true), "allow");
});
