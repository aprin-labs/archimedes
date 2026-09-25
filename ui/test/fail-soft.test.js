import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// #1356 — an API failure must render as a loud, visible absence, never as an
// empty state or a measured zero. Same shape as public-visuals.test.js /
// a11y.test.js: readFileSync + regex pins on the source, no DOM. Every
// assertion here was confirmed to FAIL against the pre-fix tree (see the PR
// body for the revert-and-rerun transcripts).

const src = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");

const architecture = src("components/Architecture.jsx");
const landing = src("components/Landing.jsx");
const strategies = src("components/Strategies.jsx");
const corpusExplorer = src("components/CorpusExplorer.jsx");
const leaderboard = src("components/Leaderboard.jsx");

// ── Item 2: /api/config/contracts null-vs-loading-vs-zero ────────────────

// The "Live user vaults on Arc" hero tile that #1356's `vaultsUnread` guard
// protected was REMOVED in the 2026-08-30 claim scrub (owner decision:
// on-chain execution is roadmap, so the architecture page states that in one
// place and counts nothing). Its null-vs-loading-vs-zero test is deleted
// rather than kept passing against a surface that no longer renders — a
// guard for a deleted tile guards nothing, and leaving it would have to be
// weakened to keep passing, which is worse. The sibling guards below and in
// Landing.jsx (poolsUnread) still cover the same #1356 principle on the
// surfaces that DO still render a live count.

test("Architecture.jsx treats a degraded leaderboard as failed, not a measured zero", () => {
	// leaderboard?.degraded is a 200 (the provider raised, or the curated
	// cohort came back empty for a reason other than a legitimate filter —
	// #1356), not a rejected fetch, so leaderboardError alone (set only by
	// .catch()) never catches it. Mirrors Landing.jsx's poolsUnread line.
	assert.match(architecture, /failed=\{leaderboardError \|\| leaderboard\?\.degraded\}/);
});

test("Landing.jsx renders the error census state for a null pool read, not the waiting state", () => {
	assert.match(
		landing,
		/const poolsUnread = contracts != null && contracts\.pools == null;/,
	);
	// The error branch's condition must include poolsUnread, so a null-because-
	// unread pool count takes the census-state--error branch rather than
	// falling through to "Waiting for live deployment data…" (totalLive==null
	// is also true in that case, so ordering/inclusion here is load-bearing).
	assert.match(landing, /\{contractsError \|\| poolsUnread \? \(/);
	assert.match(landing, /census-state census-state--error/);
	assert.match(landing, /Waiting for live deployment data…/);
});

test("Landing.jsx's live census states a denominator for the core-contract count", () => {
	assert.match(landing, /CORE_CONTRACT_FIELDS\.length/);
});

// ── Item 3: Library reports a per-feed failure ────────────────────────────

test("Strategies.jsx tracks genRes / gateRes / publishedRes failures separately", () => {
	assert.match(strategies, /const \[genError, setGenError\] = useState\(''\)/);
	assert.match(strategies, /const \[gateError, setGateError\] = useState\(''\)/);
	assert.match(strategies, /const \[publishedError, setPublishedError\] = useState\(''\)/);
	assert.match(strategies, /setGenError\(genRes\.reason\?\.message/);
	assert.match(strategies, /setGateError\(gateRes\.reason\?\.message/);
	assert.match(strategies, /setPublishedError\(publishedRes\.reason\?\.message/);
	// The old comment asserted a false equivalence (empty state == honest
	// fallback) that this issue exists to retract.
	assert.doesNotMatch(strategies, /empty state is the honest fallback/);
});

test("Strategies.jsx's Examples feed treats a fulfilled-but-degraded response as a failure, not a real empty library", () => {
	// /api/strategies/ (the seed/Examples feed) can now return HTTP 200 with
	// `{strategies: [], degraded: true}` when the provider raised — a 2xx is
	// not proof the fetch actually succeeded, so a fulfilled promise alone
	// must not be trusted to mean "the library is genuinely empty" (#1356
	// re-occurring on the very PR meant to close it — a fulfilled seedRes
	// with degraded:true used to fall straight through to setExamples([])
	// with loadError never set, painting the false "No example strategies
	// loaded." instead of the loud loadError banner).
	assert.match(strategies, /if \(seedRes\.value\.degraded\) \{/);
	assert.match(strategies, /setLoadError\(seedRes\.value\.degraded_reason/);
});

test("Strategies.jsx's generated feed treats a fulfilled-but-degraded response as a failure, not a real empty store", () => {
	// /api/strategies/generated (review round 2 — #1356's own Summary bullet 1
	// names this endpoint first) can now return HTTP 200 with
	// `{strategies: [], degraded: true}` when the store raised — a fulfilled
	// promise alone is not proof the fetch actually succeeded, mirroring the
	// seedRes check above. Without this, genError stays '' and the default
	// tab paints the false "No generated strategies yet" empty state.
	assert.match(strategies, /if \(genRes\.value\.degraded\) \{/);
	assert.match(strategies, /setGenError\(genRes\.value\.degraded_reason/);
});

test("Strategies.jsx's Examples panel is gated on !loadError so it never renders alongside the loadError banner", () => {
	// loadError is set from the seed route's own `degraded` flag on a
	// *fulfilled* response (see the seedRes test above). Without this gate a
	// degraded fetch painted the loadError banner at :815 AND the false
	// "No example strategies loaded." empty state simultaneously — the exact
	// claim #1356 was filed against, unlike the Published branch one screen
	// below which already gates on `!publishedError`.
	assert.match(strategies, /\{!loading && !loadError && \(/);
});

test("Strategies.jsx's generated panel has the same loading guard Examples/Published already have", () => {
	// Examples/Published: `{loading && <StrategyListSkeleton />}` then a separate
	// `{!loading && (...)}` block. Generated uses a ternary with the identical
	// loading branch inside the same activeTab block, so this must appear a
	// third time (once per tab).
	//
	// #1645 replaced the `<div className="caption mb-4">Loading…</div>` this
	// used to count with a real skeleton. The INVARIANT is unchanged and is
	// what this test is for — every tab has a loading guard, none paints its
	// empty state while the fetch is still in flight — so the count moved to
	// the new construct rather than the test being deleted.
	const loadingGuardCount = (strategies.match(/<StrategyListSkeleton \/>/g) || []).length;
	assert.equal(loadingGuardCount, 3, "expected one loading guard each for generated, examples, and published");
});

test("Strategies.jsx surfaces per-feed errors near the panels they describe, with a retry", () => {
	assert.match(strategies, /Couldn't load generated strategies: \{genError\}/);
	assert.match(strategies, /Deployability status unavailable: \{gateError\}/);
	assert.match(strategies, /Couldn't load published strategies: \{publishedError\}/);
	// Every per-feed error offers a way back in, not just a dead-end message.
	const retryCount = (strategies.match(/onClick=\{load\}/g) || []).length;
	assert.ok(retryCount >= 3, `expected at least 3 retry buttons wired to load(), found ${retryCount}`);
});

test("Strategies.jsx's tab chips never print a measured zero on a degraded/failed feed (#1356 round 4)", () => {
	// Round 3 made the Examples chip the ONLY number left on the tab once the
	// StrategyTable itself is gated on !loadError — a degraded `/api/strategies/`
	// must not paint "Examples (0)" next to the loadError banner. Same shape
	// Leaderboard.jsx's header total already fixed (`data.degraded ? '—' : ...`);
	// mirrored here for both tab chips.
	assert.match(strategies, /Generated \(\{genError \? '—' : generated\.length\}\)/);
	assert.match(strategies, /Examples \(\{loadError \? '—' : examples\.length\}\)/);
});

test("Strategies.jsx's Library loadError banner offers the same Retry control as the other per-feed banners", () => {
	// Round 4 minor: the PR body claims every degraded surface is "retry-able",
	// but the seed-feed (Library) loadError banner had no button — the only
	// recovery was a full page reload. Make the claim true instead of
	// narrowing it, matching the pattern already used 3x in this file
	// (gateError, genError, publishedError).
	assert.match(strategies, /Couldn't load library: \{loadError\}/);
	const retryCount = (strategies.match(/onClick=\{load\}/g) || []).length;
	assert.ok(retryCount >= 4, `expected at least 4 retry buttons wired to load() (adds the loadError block), found ${retryCount}`);
});

// ── Item 4: Corpus gets error state + retry ───────────────────────────────

test("CorpusExplorer.jsx never swallows an overview failure into null with no error flag", () => {
	assert.doesNotMatch(corpusExplorer, /catch\(\(\) => setOverview\(null\)\)/);
	assert.match(corpusExplorer, /const \[overviewError, setOverviewError\] = useState\(false\)/);
});

test("CorpusExplorer.jsx's overview tab renders an unavailable + Retry line instead of an infinite spinner", () => {
	assert.match(
		corpusExplorer,
		/function OverviewTab\(\{ overview, overviewError, onRetry \}\)/,
	);
	assert.match(corpusExplorer, /if \(overviewError\) \{/);
	assert.match(corpusExplorer, /Overview unavailable/);
	assert.match(corpusExplorer, /onClick=\{onRetry\}/);
	// The permanent-spinner line must still exist as the genuine-loading
	// fallback, but only reached once overviewError has already been ruled out.
	assert.match(corpusExplorer, /Loading overview\.\.\./);
});

test("CorpusExplorer.jsx's catalog tab suppresses the papers-found count on a failed fetch", () => {
	assert.match(corpusExplorer, /const \[catalogError, setCatalogError\] = useState\(false\)/);
	assert.match(corpusExplorer, /setCatalogError\(true\)/);
	assert.match(corpusExplorer, /catalogError \? \(/);
	assert.match(corpusExplorer, /Catalog unavailable/);
});

test("CorpusExplorer.jsx's header stat chips stay visible on an overview failure, on every tab", () => {
	// The header (papers/categories/source chips) renders once, above the
	// per-tab content, so `tab === 'catalog'` (the default) must not hide an
	// overview outage behind a tab the visitor never opens. Must not fall
	// straight through to the bare `overview && (...)` — that silently
	// renders nothing, indistinguishable from "still loading".
	assert.match(corpusExplorer, /overviewError \? \(/);
	assert.match(corpusExplorer, /corpus stats unavailable/);
});

// ── Item 5-6: degraded leaderboard must not fall through to a false claim ─

test("Leaderboard.jsx never renders the filters-empty message while the board is degraded", () => {
	assert.match(leaderboard, /data\?\.degraded/);
	assert.match(leaderboard, /Board data is degraded\./);
	// Both honest-empty branches must be gated on !data.degraded so a
	// degraded board can't fall through to a false "nothing here" claim.
	assert.match(
		leaderboard,
		/!error && data && !data\.degraded && data\.entries\.length === 0 && isOwn/,
	);
	assert.match(
		leaderboard,
		/!error && data && !data\.degraded && data\.entries\.length === 0 && !isOwn/,
	);
});

test("Leaderboard.jsx never prints a measured total while the board is degraded", () => {
	// A total outage still sets `total: 0` on the response (build_leaderboard
	// derives it from the ranked list), so the header count must be gated on
	// `data.degraded` the same way the two honest-empty prose branches above
	// are — otherwise a degraded board reads "0 strategies" next to its own
	// warning banner.
	assert.match(
		leaderboard,
		/\{data\.degraded \? '—' : `\$\{data\.total\} strateg\$\{data\.total === 1 \? 'y' : 'ies'\}`\}/,
	);
});

test("Leaderboard.jsx's degraded banner offers a Retry, matching the PR body's retry-able claim (#1356 round 4)", () => {
	const degradedBannerRetry = /Board data is degraded\.<\/strong>\{' '\}\s*\{data\.degraded_reason[^}]*\}\{' '\}\s*<button type="button"[^>]*onClick=\{load\}/;
	assert.match(leaderboard, degradedBannerRetry);
});

test("Strategies.jsx requests the whole curated library, not the default page", () => {
	// The endpoint defaults to limit=20 of a 34-strategy library, sorted
	// alphabetically — which structurally hid every currently-passing example
	// (all sort past row 20; 2026-08-30 external review: "0 of 20 pass").
	// The library fetch must always carry an explicit limit at the backend's
	// max so the passing strategies are actually visible.
	assert.match(strategies, /apiGet\(['"]\/api\/strategies\/\?limit=100['"]\)/);
});
