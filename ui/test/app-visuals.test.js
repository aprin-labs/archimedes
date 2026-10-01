import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const css = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");
const authPage = readFileSync(
	new URL("../src/components/AuthPage.jsx", import.meta.url),
	"utf8",
);
const authenticatedApp = readFileSync(
	new URL("../src/AuthenticatedApp.jsx", import.meta.url),
	"utf8",
);
const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);
const layout = readFileSync(
	new URL("../src/components/Layout.jsx", import.meta.url),
	"utf8",
);
const onboarding = readFileSync(
	new URL("../src/components/OnboardingTour.jsx", import.meta.url),
	"utf8",
);
const theme = readFileSync(new URL("../src/theme.js", import.meta.url), "utf8");
// PROOF_STAGES itself moved out of Layout.jsx into proofStages.js (#1354) so
// the roadmap-copy guard can call getProofStages() under plain node — see
// that file and ui/test/roadmap-copy.test.js for the 3-vs-5-stage guard
// (getProofStages(false)/(true)/() pins the flag-derived stage count; this
// file only pins that Layout.jsx wires to it, see below).
const passport = readFileSync(
	new URL("../src/components/StrategyPassport.jsx", import.meta.url),
	"utf8",
);
const portfolio = readFileSync(
	new URL("../src/components/Portfolio.jsx", import.meta.url),
	"utf8",
);
const insights = readFileSync(
	new URL("../src/components/Insights.jsx", import.meta.url),
	"utf8",
);

test("authenticated shell has isolated operational tokens and journey rail", () => {
	assert.match(layout, /shell app-site/);
	assert.match(layout, /BrandMark/);
	assert.match(layout, /className="app-skip-link"/);
	assert.match(layout, /id="app-content"/);
	assert.match(layout, /<nav aria-label="Main">/);
	assert.match(layout, /event\.key === "Escape"/);
	assert.match(layout, /closeButtonRef/);
	// Pin the wiring, not just the identifier: Layout.jsx must actually call
	// the flag-derived getProofStages() (proofStages.js), not a hardcoded
	// array — CORE_PROOF_STAGES/ROADMAP_PROOF_STAGES both declare their
	// labels unconditionally as source literals, so a plain
	// `assert.match(proofStages, /Vault/)` (etc.) against that file's text
	// would pass regardless of whether the flag-derived split is wired
	// correctly; the 3-vs-5-stage behaviour itself is exercised by
	// getProofStages(false)/(true)/() in roadmap-copy.test.js.
	assert.match(layout, /import \{ getProofStages \} from ["']\.\.\/proofStages\.js["'];/);
	assert.match(layout, /const PROOF_STAGES = getProofStages\(\);/);
	assert.match(layout, /className="app-proof-rail"/);
	assert.match(layout, /aria-current=\{isCurrent \? "step" : undefined\}/);
	assert.match(
		layout,
		/const proofStage\s*=\s*\(page === ["']generate["'] \? journeyStage : null\) \?\?\s*CORE_PAGE_STAGE\[page\]/,
	);
	assert.match(layout, /\{proofStage && \(/);
	assert.match(authenticatedApp, /const \[journeyStage, setJourneyStage\]/);
	assert.match(authenticatedApp, /onStageChange=\{setJourneyStage\}/);
	assert.match(
		generate,
		/onStageChange\?\.\(drillInJobId \? ["']debate["'] : ["']brief["']\)/,
	);
	assert.match(css, /\.app-site\s*\{[^}]*--app-canvas:\s*#081218;/s);
	assert.match(
		css,
		/:root\[data-theme="light"\] \.app-site\s*\{[^}]*--app-canvas:\s*#f2f5f1;/s,
	);
	assert.match(css, /\.app-site :focus-visible\s*\{/);
	assert.match(css, /\.app-site\s*\{[^}]*--text-4:\s*#71867e;/s);
	assert.match(
		css,
		/:root\[data-theme="light"\] \.app-site\s*\{[^}]*--text-4:\s*#60746b;/s,
	);
});

test("authenticated routes load behind route-level suspense boundaries", () => {
	assert.match(
		authenticatedApp,
		/lazy\(\(\) => import\(["']\.\/components\/Explore["']\)\)/,
	);
	assert.match(
		authenticatedApp,
		/<Suspense fallback=\{<AppRouteFallback \/>\}>/,
	);
});

test("onboarding uses proof-frame identity and verified product language", () => {
	assert.match(onboarding, /Research-grounded strategy generation/);
	assert.match(onboarding, /selection-bias rigor/);
	assert.doesNotMatch(onboarding, /Λ|bleeding-edge/i);
});

test("getStoredTheme stays off window.matchMedia — dark is the product default (#1357)", () => {
	// getStoredTheme runs as the lazy useState initializer on the render path
	// of every /app page. An unguarded window.matchMedia there reintroduces
	// the #1357 failure class (an uncaught throw unmounts the React root),
	// and it contradicts theme.test.js's pinned behavior: any stored value
	// other than 'light' — including nothing — resolves to 'dark'. If a
	// system-preference first theme is ever wanted, it needs a guarded,
	// test-reconciled design of its own; this guard rejects the shortcut.
	assert.doesNotMatch(theme, /matchMedia/);
	assert.match(theme, /stored === ["']light["'] \? ["']light["'] : ["']dark["']/);
});

test("social auth controls do not wait for provider discovery", () => {
	assert.match(authPage, /Continue with Google/);
	assert.match(authPage, /Continue with GitHub/);
	assert.doesNotMatch(authPage, /getProviders|providers\.(?:google|github)/);
	// The brand marks render from the same static markup, so they must not
	// reintroduce a discovery/fetch dependency either — they are inline SVG,
	// not a remote logo asset. Their fidelity (Google's four colours,
	// GitHub's currentColor, no distortion, clear space) is pinned in
	// auth-page-copy.test.js.
	assert.match(authPage, /<GoogleMark \/>/);
	assert.match(authPage, /<GitHubMark \/>/);
});

test("Generate uses a mobile-first brief-first workbench with context rail", () => {
	assert.match(generate, /className="generate-page"/);
	assert.match(generate, /className="app-page-heading generate-page__heading"/);
	assert.match(generate, /className="generate-workbench"/);
	assert.match(generate, /className="card generate-brief"/);
	assert.match(generate, /className="generate-context-rail"/);
	assert.match(generate, /className="generate-register"/);

	// #1642 inverted the layout. The BASE rule — the one with no media query
	// around it — is now the phone layout: one column. Pinning the base as
	// single-column is what makes "mobile-first" a mechanical property rather
	// than a claim in a comment; a desktop grid restored here would fail.
	assert.match(css, /\.generate-workbench\s*\{[^}]*grid-template-columns:\s*1fr;/s);

	// The two-column brief+rail grid and the sticky rail are the enhancement,
	// and they live behind min-width — never behind a max-width collapse.
	const desktopTier = css.match(
		/@media \(min-width: 900px\)\s*\{([\s\S]*?)\n\}/,
	);
	assert.ok(desktopTier, "no min-width:900px tier found");
	assert.match(
		desktopTier[1],
		/\.generate-workbench\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1\.35fr\) minmax\(280px,\s*0\.65fr\);/s,
	);
	assert.match(desktopTier[1], /\.generate-context-rail\s*\{[^}]*position:\s*sticky;/s);

	// The retired always-visible examples list and its styles are gone.
	assert.doesNotMatch(css, /\.generate-example\s*[,{]/);
});

test("Strategy Passport separates evidence from user authority", () => {
	assert.match(passport, /className="passport-page"/);
	assert.match(passport, /className="app-page-heading passport-heading/);
	assert.match(passport, /className="passport-workspace"/);
	assert.match(passport, /className="passport-authority"/);
	assert.match(passport, /className="passport-evidence"/);
	assert.match(passport, /passport-deploy/);
	assert.match(
		css,
		/\.passport-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\) minmax\(280px,\s*340px\);/s,
	);
	assert.match(css, /\.passport-authority\s*\{[^}]*position:\s*sticky;/s);
	assert.match(css, /\.passport-rigor\s*\{[^}]*--text-4:\s*#566a61;/s);
	// #1646 rehomed the evidence column's source-paper cards onto one table
	// and added the DSL panel, so the class pins above no longer describe the
	// whole evidence column. The pins for the new markup live in
	// passport-dsl.test.js beside the behaviour tests for the code block —
	// this case keeps owning the page's SKELETON (workspace / authority /
	// evidence split), which is unchanged.
	assert.match(passport, /className="passport-sources passport-dense fade-up/);
	assert.match(css, /\.passport-dense \.passport-panel\s*\{[^}]*padding:\s*16px 18px;/s);
});

test("Portfolio uses ledger metrics and split audit workspace", () => {
	assert.match(portfolio, /className="portfolio-page"/);
	assert.match(portfolio, /className="app-page-heading portfolio-heading/);
	assert.match(portfolio, /className="portfolio-ledger"/);
	assert.match(portfolio, /className="portfolio-workspace"/);
	assert.match(portfolio, /className="portfolio-positions"/);
	assert.match(portfolio, /className="portfolio-activity"/);
	assert.match(
		css,
		/\.portfolio-workspace\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1\.25fr\) minmax\(300px,\s*0\.75fr\);/s,
	);
});

test("app semantics reserve cobalt for action and verdigris for verification", () => {
	assert.match(css, /--app-action:\s*var\(--brand-cobalt\);/);
	assert.match(css, /--app-verify:\s*var\(--brand-verdigris\);/);
	assert.match(
		css,
		/\.app-site \.btn-primary\s*\{[^}]*background:\s*var\(--app-action\);/s,
	);
	assert.match(
		css,
		/\.app-site \.tag-positive\s*\{[^}]*color:\s*var\(--app-verify\);/s,
	);
});

test("app motion and core workspaces respect narrow or reduced-motion contexts", () => {
	assert.match(
		css,
		/@media \(prefers-reduced-motion: reduce\)[^{]*\{[\s\S]*?\.app-site \.fade-up,[\s\S]*?animation:\s*none !important;/s,
	);
	// Passport and Portfolio still collapse at max-width: 900px. Generate no
	// longer appears in this tier — #1642 made single-column its base state,
	// so there is nothing left here for it to un-collapse (see the
	// mobile-first test above, which pins the base + the min-width tier).
	assert.match(
		css,
		/@media \(max-width: 900px\)[^{]*\{[\s\S]*?\.passport-workspace,[\s\S]*?\.portfolio-workspace\s*\{[^}]*grid-template-columns:\s*1fr;/s,
	);
	// Bounded to each max-width block's own body: an unbounded `[\s\S]*?`
	// would happily span from any earlier media query to the base
	// `.generate-workbench` rule and "find" a violation that is not there.
	// Top-level blocks in this file close with a `}` in column 0.
	for (const block of css.matchAll(/@media \(max-width: \d+px\)\s*\{([\s\S]*?)\n\}/g)) {
		assert.doesNotMatch(
			block[1],
			/\.generate-workbench\s*[,{]/,
			"Generate's workbench must not be laid out from a max-width tier (#1642)",
		);
	}
});

const generationStream = readFileSync(
	new URL("../src/components/GenerationStream.jsx", import.meta.url),
	"utf8",
);

const generationCopy = readFileSync(
	new URL("../src/generation-copy.js", import.meta.url),
	"utf8",
);

test("generation stream claims papers only from real per-candidate citations (task #54)", () => {
	// The event copy moved out of GenerationStream.jsx into src/generation-copy.js
	// (a plain module so the copy is runnable in tests); the claim it may make is
	// unchanged, so the pin follows it.
	//
	// The old candidates_selected line rendered a papers COUNT sliced from the
	// curated library (a constant from the wrong population) — it must not return.
	assert.doesNotMatch(generationCopy, /candidates;.*papers/);
	assert.doesNotMatch(generationStream, /candidates;.*papers/);
	// The honest claim: candidate_drafted's own provenance-checked citations,
	// omitted when absent.
	assert.match(generationCopy, /candidate_drafted:[\s\S]{0,400}source_arxiv_ids/);
	assert.match(generationCopy, /from \$\{plural\(n, "paper", "papers"\)\}/);
});

const leaderboard = readFileSync(
	new URL("../src/components/Leaderboard.jsx", import.meta.url),
	"utf8",
);

test("leaderboard caveat banner is own-scope-gated (#1306; the refresh-residual claim is retired by #1365)", () => {
	// The broad unconditional banner ("known to be incorrect") is retired for
	// the curated view, whose freshness was verified against prod.
	assert.doesNotMatch(leaderboard, /known to be incorrect/);
	// The remaining banner renders ONLY under isOwn — the gate expression must
	// immediately precede the banner's status div. Since the Lane 3.4 board
	// split the gate also carries `isResearch`: the caveat is about
	// backtest-era numbers being fixed at generation time, which is a claim
	// about the RESEARCH board only — repeating it over the live paper board,
	// whose numbers come from the forward ledger, would be false. Pinning both
	// halves keeps the banner from drifting onto the wrong surface.
	assert.match(leaderboard, /\{isResearch && isOwn && \(\s*<div\s*\n?\s*role="status"/);
	// The one remaining residual: pre-correction rows are fixed at generation
	// time (never refreshed) — see the dedicated test below (#1365).
	assert.match(leaderboard, /before the August engine corrections/);
	// The interpreter-divergence clause is RETIRED (F2/F3 landed via #1320,
	// parity-pinned, verified on the redeployed runner) — its claim would now
	// be false, so it must not reappear in the rendered banner text. (The
	// comment block documenting the retirement legitimately mentions the
	// fixes, so pin the banner's own load-bearing phrases, not the comment.)
	assert.doesNotMatch(leaderboard, /live execution currently interprets/);
	assert.doesNotMatch(leaderboard, /awaiting\s*\n?\s*live-trading sign-off/);
});

test("leaderboard renders every field it sorts by, and no constant forward column (#1365)", () => {
	assert.doesNotMatch(leaderboard, /refresh on their next/);
	assert.match(leaderboard, /fixed at generation time/);
	const block = leaderboard.match(/const SORT_OPTIONS = \[([\s\S]*?)\]/)[1];
	for (const [, id] of block.matchAll(/id: '([a-z_]+)'/g)) {
		// A RENDER site is a value handed to a formatter — a bare e.<id> also
		// matches null-CHECKS inside a render gate, which is exactly the defect
		// this test exists to reject (a field sorted but never shown). Since
		// #1651 the formatter is <MetricValue metric="…" value={e.<id>} />
		// rather than the file's own fmt()/fmtPct(); both shapes count as a
		// render, neither of them matches a bare null-check.
		assert.match(
			leaderboard,
			new RegExp(`(?:fmt(?:Pct)?\\(\\s*e\\.${id}\\b|value=\\{e\\.${id}\\})`),
			`sort option ${id} has no rendered value`,
		);
	}
	assert.match(leaderboard, /value=\{e\.out_of_sample_sharpe\}/);
	assert.doesNotMatch(leaderboard, /SB pending/);
	assert.doesNotMatch(leaderboard, /P&L pending/);
});

test("Insights headline claim matches the write path — no bot-exclusion claim, per-stage split shown instead (#1366 AC3)", () => {
	// record_funnel writes the landed HLL for every beacon regardless of the
	// classifier's verdict (metrics_routes.py:278 runs before is_agent is even
	// read at :285) — the funnel's landed count does NOT drop crawlers/bots.
	// The old copy claimed otherwise; it must not come back.
	assert.doesNotMatch(insights, /crawlers and bots drop out/);
	// The fix: render the by_agent_type per-stage split the API already
	// returns (issue #788) instead of describing a filter the code doesn't
	// perform.
	assert.match(insights, /by_agent_type/);
});

test("Insights country names use Intl.DisplayNames, not a hand-maintained map, and the epoch date is real (#1366 AC4)", () => {
	assert.doesNotMatch(insights, /const COUNTRY_NAMES/);
	assert.match(insights, /Intl\.DisplayNames/);
	// ZZ must stay an explicit case — Intl renders it "Unknown Region", which
	// would contradict the "unknown / not provided" copy this page uses for ZZ.
	assert.match(insights, /ZZ.*Unknown \/ not provided/);
	assert.doesNotMatch(insights, /deployed today/);
	assert.match(insights, /epoch_started_at/);
});

test("Insights drops the first-person operator copy (#1366 AC5)", () => {
	assert.doesNotMatch(insights, /Live conversion instruments for our/);
});
