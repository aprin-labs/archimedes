import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const css = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");
const tokens = readFileSync(
	new URL("../public/theme.css", import.meta.url),
	"utf8",
);
const architecture = readFileSync(
	new URL("../src/components/Architecture.jsx", import.meta.url),
	"utf8",
);
const landing = readFileSync(
	new URL("../src/components/Landing.jsx", import.meta.url),
	"utf8",
);
const publicLayout = readFileSync(
	new URL("../src/components/PublicLayout.jsx", import.meta.url),
	"utf8",
);
const securityUrl = new URL("../src/components/Security.jsx", import.meta.url);
const flowDiagram = readFileSync(
	new URL("../src/assets/flow-diagram.svg", import.meta.url),
	"utf8",
);
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const sitemap = readFileSync(
	new URL("../public/sitemap.xml", import.meta.url),
	"utf8",
);
const nginx = readFileSync(
	new URL("../../nginx/nginx.conf", import.meta.url),
	"utf8",
);

test("public shell inherits Fulcro tokens and retains accessible navigation", () => {
	assert.match(publicLayout, /className="public-site"/);
	assert.match(publicLayout, /BrandMark/);
	assert.match(publicLayout, /className="public-announcement"/);
	assert.match(publicLayout, /aria-label="Public navigation"/);
	assert.match(publicLayout, /<ThemeSwitcher \/>/);
	assert.doesNotMatch(
		css,
		/\.public-brand__copy small,\s*\.public-theme-toggle\s*\{\s*display:\s*none;/s,
	);
	assert.match(publicLayout, /href="\/app\/generate"/);
	assert.match(tokens, /--brand-canvas:\s*var\(--canvas\);/);
	assert.match(tokens, /--brand-ink:\s*var\(--ink\);/);
	assert.match(tokens, /--brand-cobalt:\s*var\(--link\);/);
	assert.match(tokens, /--brand-verdigris:\s*var\(--positive\);/);
	assert.match(tokens, /--brand-muted:\s*var\(--muted\);/);
	assert.doesNotMatch(
		css,
		/\.public-site\s*\{[^}]*--public-paper:/s,
		"a legacy shell-local paper token must not shadow the canonical root palette",
	);
	assert.match(css, /\.public-site :focus-visible\s*\{/);
	assert.match(
		css,
		/\.public-skip-link\s*\{[^}]*color:\s*var\(--accent-on\);/s,
	);
	assert.doesNotMatch(css, /gradient\(/i);
	assert.doesNotMatch(architecture, /gradient\(/i);
});

test("public shell owns one shared footer after page content", () => {
	assert.match(
		publicLayout,
		/<div id="public-content" tabIndex="-1">\s*\{children\}\s*<\/div>\s*<PublicFooter \/>/,
	);
	assert.equal((publicLayout.match(/<PublicFooter \/>/g) ?? []).length, 1);
	assert.match(
		publicLayout,
		/function PublicFooter\(\)[\s\S]*<footer className="public-footer">/,
	);
	assert.doesNotMatch(landing, /PublicFooter|className="public-footer"/);
});

test("security page separates verified controls from known limits", () => {
	assert.equal(existsSync(securityUrl), true, "Security.jsx must exist");
	const security = readFileSync(securityUrl, "utf8");
	assert.match(security, /Security is enforced boundaries, not a guarantee/i);
	assert.match(security, /Better Auth/);
	assert.match(security, /five-minute/i);
	// Post-scrub (2026-08-30): the known-limits list must still lead with the
	// two limits that matter most on a page with no execution behind it —
	// that Archimedes does not trade, and that a passed gate is not a
	// judgement guarantee. These replace the old "Agent may mis-rebalance …
	// cannot withdraw" pair, which pinned a custody claim the page no longer
	// makes.
	assert.match(security, /does not trade with\s+capital today/);
	assert.match(security, /Model risk:/);
	assert.match(security, /No independent security audit/i);
	assert.match(security, /Arc public testnet/i);
	assert.match(security, /<main className="security-page">/);
	assert.match(security, /<h1 id="security-title">/);
	assert.match(security, /id="known-limits"/);
});

test("security controls reserve enough width for their display heading", () => {
	assert.match(
		css,
		/\.security-controls__layout\s*\{[^}]*grid-template-columns:\s*minmax\(360px,\s*0\.82fr\)\s*minmax\(0,\s*1\.18fr\);[^}]*gap:\s*clamp\(56px,\s*6vw,\s*80px\);/s,
	);
});

test("landing has complete evidence-led conversion narrative", () => {
	for (const id of [
		"problem",
		"product",
		"capabilities",
		"workflow",
		"use-cases",
		"integrations",
		"security",
		"faq",
	]) {
		assert.match(landing, new RegExp(`id=["']${id}["']`));
	}
	assert.match(landing, /EvidenceLedger/);
	assert.match(landing, /RigorMatrix/);
	assert.match(landing, /AuthorityBoundary/);
	// Post-scrub (2026-08-30): AuthorityBoundary no longer describes an
	// execution-authority split, so it is no longer flag-gated — it renders
	// unconditionally and describes the admission boundary that is live. The
	// WORKFLOW roadmap tail is gone with it, so there is nothing left to
	// slice: every step in WORKFLOW describes a path that runs today.
	assert.match(landing, /^\t\t\t<AuthorityBoundary \/>$/m);
	assert.doesNotMatch(landing, /ROADMAP_SURFACES_ENABLED/);
	assert.doesNotMatch(landing, /WORKFLOW\.slice\(/);
	assert.match(landing, /Deflated Sharpe Ratio/);
	assert.match(landing, /Probability of Backtest Overfitting/);
	assert.match(landing, /Walk-forward out-of-sample/);
	assert.match(landing, /Look-ahead audit/);
	assert.doesNotMatch(landing, /testimonial|trusted by|customer logos/i);
});

test("landing uses a bespoke product theatre instead of register-template motifs", () => {
	assert.match(landing, /className="public-hero__stage"/);
	assert.match(landing, /className="public-proof-strip"/);
	assert.match(landing, /className="public-proof-deck"/);
	assert.match(landing, /className="public-use-case-scenes"/);
	assert.doesNotMatch(landing, /capability-register__index/);
	assert.doesNotMatch(
		landing,
		/Inspection register|Admission register|Product anatomy/,
	);
	assert.match(tokens, /--public-haze:\s*var\(--canvas\);/);
	assert.match(tokens, /--public-stage:\s*var\(--canvas\);/);
	assert.match(
		tokens,
		/@font-face\s*\{[^}]*font-family:\s*"DM Sans";[^}]*dm-sans-latin\.woff2/s,
	);
	assert.match(
		tokens,
		/@font-face\s*\{[^}]*font-family:\s*Inter;[^}]*inter-latin\.woff2/s,
	);
	assert.match(
		css,
		/\.public-landing\s*\{[^}]*overflow:\s*visible;[^}]*background:\s*var\(--canvas\);/s,
	);
	assert.match(
		css,
		/html:has\(\.public-site\),\s*body:has\(\.public-site\)\s*\{[^}]*overflow:\s*visible;/s,
	);
	assert.equal(
		existsSync(new URL("../public/fonts/dm-sans-latin.woff2", import.meta.url)),
		true,
	);
	assert.equal(
		existsSync(new URL("../public/fonts/inter-latin.woff2", import.meta.url)),
		true,
	);
});

test("proof deck shows all four checks at once instead of stacking them", () => {
	// The deck used to be a sticky card stack: every article `position: sticky`
	// at a staggered `top`, 340px tall, so scrolling buried each panel under the
	// next and the four checks could only ever be flipped through, never
	// compared. The four-panel comparison is the differentiator the owner asked
	// to restore, and a stack is the one layout that cannot show it — so the
	// stacking mechanics must stay gone, not merely be overridden further down
	// the sheet where a later rule could quietly reinstate them.
	assert.doesNotMatch(
		css,
		/\.public-proof-deck article[^{]*\{[^}]*position:\s*sticky;/s,
		"the proof deck must not reintroduce sticky stacking — it hides three of the four checks",
	);
	assert.doesNotMatch(
		css,
		/\.public-proof-deck article[^{]*\{[^}]*height:\s*340px;/s,
		"the fixed 340px card height belonged to the sticky stack; a grid panel sizes to its content",
	);
	assert.match(
		css,
		/\.public-proof-deck\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\);/s,
		"the deck must be a two-column grid so all four checks are legible at rest",
	);
	// One column on a phone: two columns of a 26ch headline is unreadable.
	assert.match(
		css,
		/@media \(max-width: 760px\)[^{]*\{[\s\S]*?\.public-proof-deck\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);/s,
	);
	// The board-level card and the verdict rule are full-width rows under the
	// four panels, not a fifth cell in the two-column flow.
	assert.match(
		css,
		/\.public-proof-deck__board\s*\{[^}]*grid-column:\s*1 \/ -1;/s,
	);
	assert.match(
		css,
		/\.public-proof-deck__rule\s*\{[^}]*grid-column:\s*1 \/ -1;/s,
	);
});

test("each rigor panel states its own limit, and the deck names all four verdict states", () => {
	// The differentiator is not that four checks run — it is that each one says
	// what it does NOT prove, in the same card, and that the gate reports four
	// verdicts rather than a pass/fail boolean. Both are load-bearing claims
	// quoted from the code that computes them, so both get pinned here.
	//
	// Every `limit` below is traceable: DSR → rigor_profiles.py's own
	// "'deflated-Sharpe evidence at the 95% one-sided level', not 'statistically
	// proven'" note (level-1 dsr_p_min IS DSR_P_BADGE_MIN, #1794); PBO →
	// compute_pbo's "Known limitations"
	// (a selection-set property, a neighbour can flip it); OOS →
	// compute_oos_sharpe's "a single chronological hold-out, NOT a rolling
	// walk-forward re-estimation ... no purge/embargo gap"; LEAK → the
	// structural audit (#1566: AST offset proof over dsl_to_backtrader + spec
	// walk) covers the DSL decision path, never the data feed's own revisions.
	const criteria = landing.slice(
		landing.indexOf("const RIGOR_CRITERIA = ["),
		landing.indexOf("const BOARD_FDR = {"),
	);
	assert.equal(
		(criteria.match(/\t\tlimit:/g) ?? []).length,
		4,
		"every one of the four rejection checks must carry its own honest limit",
	);
	assert.match(criteria, /95% one-sided level/);
	assert.match(criteria, /selection set, not one strategy/);
	assert.match(criteria, /not a rolling refit/);
	assert.match(criteria, /proven to read only the current bar and earlier/);
	assert.match(criteria, /cannot audit the market data itself/);
	assert.match(landing, /className="public-proof-deck__limit"/);

	// Four states, verbatim from services/live_rigor_gate.py. "pending" and
	// "degenerate" are the two a two-state UI would silently round into a fail
	// or a pass; naming them is the whole point of the strip.
	const states = landing.slice(
		landing.indexOf("const VERDICT_STATES = ["),
		landing.indexOf("export default function Landing"),
	);
	for (const state of ["pass", "fail", "pending", "degenerate"]) {
		assert.match(states, new RegExp(`state: "${state}"`));
	}
	assert.match(landing, /Four verdicts, not two\./);
});

test("board-level FDR is described without hard-coding today's count", () => {
	// The correction is real and served: GET /api/selection-bias/gate returns
	// board_level_fdr {fdr_level, n_tested, n_significant}, recomputed over the
	// exact cohort each response serves (selection_bias_routes.py), at
	// DEFAULT_BOARD_FDR_LEVEL = 0.05, and compute_board_level_fdr's docstring
	// pins it as ADVISORY — deliberately not wired into passes_all at any level.
	// All three of those are asserted below.
	const board = landing.slice(
		landing.indexOf("const BOARD_FDR = {"),
		landing.indexOf("const VERDICT_STATES = ["),
	);
	assert.match(board, /Benjamini–Hochberg/);
	assert.match(board, /α = 0\.05/);
	assert.match(board, /never flips a gate verdict/);
	// How many strategies clear the correction is a LIVE number. This copy is
	// static, so quoting one would ship a claim that goes stale silently — the
	// same defect the census figcaption above refuses by fetching instead of
	// caching. A bare digit anywhere in this block is that mistake.
	assert.doesNotMatch(
		board,
		/\b(?:no|none|zero|one|two|three|[0-9]+)\s+(?:of\s+\d+\s+)?strategies\b/i,
		"do not hard-code how many strategies currently clear board-level FDR — it is served live on /api/selection-bias/gate",
	);
});

test("landing consolidates proof into connected instrument sections", () => {
	assert.match(landing, /className="public-section public-rigor-story"/);
	assert.match(landing, /className="public-proof-deck"/);
	assert.match(landing, /className="public-section public-path"/);
	assert.match(landing, /id="workflow"\s+className="public-path__sequence"/);
	assert.match(landing, /className="public-use-case-scenes"/);
	assert.match(landing, /id="integrations"\s+className="public-rail-stack"/);
	assert.match(landing, /className="authority-boundary__verdict"/);
	assert.match(
		css,
		/\.public-path__sequence ol\s*\{[^}]*grid-template-columns:\s*1fr;/s,
	);
	assert.match(landing, /className="public-shell public-context__layout"/);
	assert.doesNotMatch(
		css,
		/\.public-use-case-scenes[^{}]*\{[^}]*margin-left:/s,
		"use cases must align with the reading grid, not stagger into empty space",
	);
});

test("landing uses an uncropped editor capture with truthful preview labeling", () => {
	const image = readFileSync(
		new URL("../public/product-workspace.png", import.meta.url),
	);
	assert.equal(image.toString("hex", 0, 8), "89504e470d0a1a0a");
	assert.match(landing, /src="\/product-workspace\.png"/);
	assert.ok(landing.includes(`width={${image.readUInt32BE(16)}}`));
	assert.ok(landing.includes(`height={${image.readUInt32BE(20)}}`));
	assert.match(landing, /Example brief · offline preview/);
	assert.match(landing, /no run submitted/i);
	const imageStyle =
		css.match(/\.public-product-frame img\s*\{[^}]*\}/)?.[0] ?? "";
	assert.match(imageStyle, /height:\s*auto/);
	assert.doesNotMatch(imageStyle, /aspect-ratio|object-fit:\s*cover/);
	assert.match(landing, /Generate a strategy/);
	assert.doesNotMatch(landing, /Get started|Try free|Start now/);
	assert.match(landing, /apiGet\("\/api\/config\/contracts"\)/);
	assert.match(landing, /Live census unavailable/);
	assert.match(landing, /poolsUnread/);
});

test("landing and security share the closing CTA and navbar-style action", () => {
	const security = readFileSync(securityUrl, "utf8");
	assert.match(publicLayout, /export function PublicCallToAction\(\)/);
	for (const page of [landing, security]) {
		assert.match(
			page,
			/import \{ PublicCallToAction \} from "\.\/PublicLayout"/,
		);
		assert.equal((page.match(/<PublicCallToAction \/>/g) ?? []).length, 1);
	}
	const sections = [
		...landing.matchAll(/<section className="public-hero"[\s\S]*?<\/section>/g),
		...publicLayout.matchAll(
			/<section className="public-final"[\s\S]*?<\/section>/g,
		),
	];
	assert.equal(sections.length, 2);
	const button = /<a[^>]*href="\/app\/generate"[^>]*>[\s\S]*?<\/a>/;
	const navbar = publicLayout.match(button)?.[0];
	assert.ok(navbar);
	for (const [section] of sections) {
		const cta = section.match(button)?.[0];
		assert.ok(cta, "each entry point must retain the generation route");
		assert.equal(
			cta.replace(/\s+/g, " ").trim(),
			navbar.replace(/\s+/g, " ").trim(),
		);
	}
	assert.doesNotMatch(css, /\.public-final \.public-cta--primary/);
});

test("public actions drop the secondary hero button and requested arrow icons", () => {
	assert.doesNotMatch(landing, /See the product/);
	assert.match(
		landing,
		/<a href="\/architecture">\s*Read system architecture\s*<\/a>/,
	);
	const cta = architecture.slice(
		architecture.indexOf("function CallToAction("),
		architecture.indexOf("export default function Architecture"),
	);
	assert.match(cta, /onClick=\{\(\) => onNavigate\?\.\("generate"\)\}/);
	assert.match(cta, />\s*Generate a strategy\s*<\/button>/);
	assert.doesNotMatch(cta, /[→↗]/);
});

test("public architecture page uses one skip target and main landmark", () => {
	assert.match(publicLayout, /<div id="public-content" tabIndex="-1">/);
	assert.match(architecture, /return \(\s*<main className="page-content">/);
	assert.doesNotMatch(architecture, /id="public-content"/);
});

test("architecture diagram uses account-first identity and current brand semantics", () => {
	assert.match(flowDiagram, /Account owns research and settings/);
	assert.match(flowDiagram, /Wallet proof only for on-chain actions/);
	assert.match(flowDiagram, /Arc public testnet/);
	assert.doesNotMatch(
		flowDiagram,
		/#D4A853|Georgia|SIWE|289 contracts|verified against main/i,
	);
	assert.doesNotMatch(architecture, /sign in with any wallet/i);
});

test("architecture stats keep two mobile columns and four desktop columns", () => {
	assert.match(architecture, /className="architecture-stats"/);
	assert.match(
		css,
		/\.architecture-stats\s*\{[^}]*grid-template-columns:\s*repeat\(2,/s,
	);
	assert.match(
		css,
		/@media \(min-width: 768px\)[^{]*\{[^}]*\.architecture-stats\s*\{[^}]*grid-template-columns:\s*repeat\(4,/s,
	);
});

test("editorial hero gives copy and real product capture separate columns", () => {
	assert.match(
		css,
		/\.public-hero__stage\s*\{[^}]*grid-template-columns:\s*minmax\(0, 0\.95fr\) minmax\(0, 1\.05fr\);[^}]*background:\s*var\(--canvas\);/s,
	);
	assert.match(css, /\.public-hero__copy\s*\{[^}]*text-align:\s*left;/s);
	assert.match(
		css,
		/@media \(max-width: 1100px\)\s*\{[^]*?\.public-hero__stage\s*\{[^}]*grid-template-columns:\s*1fr;/,
	);
	const lede = landing.match(/className="public-hero__lede">([^<]+)<\/p>/)?.[1];
	assert.ok(lede, "hero needs a readable summary");
	assert.ok(
		lede.trim().split(/\s+/).length <= 25,
		"hero summary must fit beside the product",
	);
});

test("wrapped public navigation leaves keyboard focus room at larger text sizes", () => {
	assert.match(
		css,
		/@media \(max-width: 760px\)\s*\{[^{]*html:has\(\.public-site\)\s*\{[^}]*scroll-padding-top:\s*calc\(4rem \+ 64px\);/s,
	);
	assert.match(css, /\.public-nav\s*\{[^}]*flex-wrap:\s*wrap;/s);
});

test("light landing replaces every dark theatre surface", () => {
	assert.match(tokens, /--public-theatre-bg:\s*var\(--surface-2\);/);
	assert.match(tokens, /--public-theatre-text:\s*var\(--ink\);/);
	for (const selector of ["public-path__sequence", "authority-boundary"]) {
		assert.match(
			css,
			new RegExp(
				`\\.${selector}\\s*\\{[^}]*background:\\s*var\\(--public-theatre-bg\\);`,
				"s",
			),
		);
	}
	assert.match(
		css,
		/\.public-proof-deck article\s*\{[^}]*background:\s*var\(--surface-1\);/s,
	);
	assert.match(
		css,
		/\.public-use-case-scenes article\s*\{[^}]*background:\s*transparent;/s,
	);
	assert.match(tokens, /--public-theatre-positive:\s*var\(--positive\);/);
	assert.match(
		css,
		/\.authority-boundary__side--agent \.authority-boundary__owner\s*\{[^}]*color:\s*var\(--public-theatre-positive\);/s,
	);
});

test("dark landing preserves atmospheric contrast and semantic accents", () => {
	assert.match(tokens, /\[data-theme="dark"\]\s*\{[^}]*--canvas:\s*#0d1917;/s);
	assert.match(tokens, /--glass-border:\s*var\(--line\);/);
	// Contrast text stays opaque; lowering alpha can violate its 4.5:1 floor.
	assert.match(tokens, /--accent-on-muted:\s*var\(--on-control\);/);
	assert.match(tokens, /--public-theatre-bg:\s*var\(--surface-2\);/);
	assert.match(
		css,
		/\.authority-boundary\s*\{[^}]*background:\s*var\(--public-theatre-bg\);/s,
	);
	assert.match(
		css,
		/\.public-proof-deck article\s*\{[^}]*color:\s*var\(--text-1\);/s,
	);
});

test("ownership verdict follows the active public theme", () => {
	assert.match(tokens, /--public-theatre-contrast:\s*var\(--surface\);/);
	assert.match(tokens, /--public-theatre-contrast-text:\s*var\(--ink\);/);
	assert.match(tokens, /--public-theatre-contrast-muted:\s*var\(--muted\);/);
	assert.match(
		css,
		/\.authority-boundary__verdict span\s*\{[^}]*color:\s*var\(--public-theatre-contrast-muted\);/s,
	);
});

test("metadata and sitemap describe canonical anonymous public routes", () => {
	assert.doesNotMatch(html, /rel="canonical"/);
	assert.match(app, /document\.querySelector\(['"]link\[rel=[^\]]*canonical/);
	assert.match(app, /architecture:\s*["']\/architecture["']/);
	assert.match(app, /canonicalPaths\[route\.page\]\s*\?\?\s*["']\/["']/);
	assert.match(
		html,
		/property="og:image"\s+content="https:\/\/archimedes-arc\.com\/og-image\.png"/,
	);
	assert.match(html, /name="twitter:card" content="summary_large_image"/);
	assert.match(
		html,
		/"target":\s*"https:\/\/archimedes-arc\.com\/app\/generate"/,
	);
	assert.match(sitemap, /<loc>https:\/\/archimedes-arc\.com\/<\/loc>/);
	assert.match(
		sitemap,
		/<loc>https:\/\/archimedes-arc\.com\/architecture<\/loc>/,
	);
	for (const route of ["explore", "corpus"]) {
		assert.match(
			sitemap,
			new RegExp(`<loc>https://archimedes-arc\\.com/${route}</loc>`),
		);
	}
	// `insights` was in the list above until the admin-gate PR (owner
	// directive 2026-08-20, supersedes #1028 D8) made /app/insights
	// ADMIN-ONLY. It is not an anonymous public route any more, so it must
	// not be advertised — see ui/test/sitemap.test.js for the whole-file
	// guard and its rationale. Asserting its ABSENCE here, rather than just
	// deleting it from the loop, keeps this test honest about the change.
	assert.doesNotMatch(sitemap, /insights/i);
	// `leaderboard` left the loop the same way with #1753 (the owner's
	// call): /app/leaderboard is gated at nginx now, so the URL a
	// crawler would follow answers 302 /sign-in and indexes nothing. Its
	// <loc> ABSENCE is asserted rather than merely un-asserted — the sitemap
	// must not advertise a page an anonymous visitor cannot read. (Unlike
	// insights, the leaderboard's EXISTENCE is not a secret, so this is the
	// <loc>-only form, not sitemap.test.js's whole-file form: the header
	// comment names it and says why it is excluded.)
	assert.doesNotMatch(
		sitemap,
		/<loc>https:\/\/archimedes-arc\.com\/leaderboard<\/loc>/,
	);
	assert.doesNotMatch(
		sitemap,
		/<loc>https:\/\/archimedes-arc\.com\/(marketplace|portfolio|publish|subscriptions)<\/loc>/,
	);
});

test("share cards do not claim generation is recorded on-chain", () => {
	assert.doesNotMatch(html, /records the whole decision on Arc/);
	assert.doesNotMatch(html, /check the reasoning trace on-chain/);
	assert.doesNotMatch(html, /on-chain reasoning provenance/);
	assert.match(html, /Generation is not anchored on-chain/);
});

test("public announcement and shared footer do not say unqualified No real funds", () => {
	assert.doesNotMatch(publicLayout, /No real funds/);
	assert.doesNotMatch(landing, /No real funds/);
	assert.match(publicLayout, /No mainnet money/);
	assert.match(publicLayout, /Generation fee is real testnet\s+USDC/);
});

test("landing FAQ does not name the unmerged paper_agent_trades table as a visitor path", () => {
	assert.doesNotMatch(landing, /paper_agent_trades/);
	assert.match(landing, /paper_daily_returns is the graded track record/);
});

test("architecture page does not quote a curated pass count, including zero", () => {
	assert.doesNotMatch(architecture, /Not one paper-derived/);
	assert.doesNotMatch(architecture, /clears our bar/);
	assert.match(architecture, /will never quote a\s+count/);
	assert.doesNotMatch(
		architecture,
		/admitted only through a statistical rigor gate before anything can run live/,
	);
});

test("security page is a canonical public destination", () => {
	assert.match(app, /import Security from ["']\.\/components\/Security["']/);
	assert.match(app, /security:\s*["']Security · Archimedes["']/);
	assert.match(app, /security:\s*["']\/security["']/);
	assert.match(app, /route\.page === ["']security["'][\s\S]*<Security \/>/);
	assert.match(publicLayout, /href="\/security"[\s\S]*Security/);
	assert.match(landing, /href="\/security"/);
	const productLinks =
		publicLayout.match(
			/<nav aria-label="Product links">[\s\S]*?<\/nav>/,
		)?.[0] ?? "";
	assert.match(productLinks, /href="\/security"/);
	assert.match(sitemap, /<loc>https:\/\/archimedes-arc\.com\/security<\/loc>/);
});

test("production bootstrap uses existing same-origin CSP without inline execution", () => {
	assert.match(html, /<script src="\/theme-init\.js"><\/script>/);
	assert.ok(existsSync(new URL("../public/theme-init.js", import.meta.url)));
	assert.doesNotMatch(html, /<script>\s*[^<]+<\/script>/i);
	assert.ok(nginx.includes("script-src 'self'"));
	// Scope the unsafe-inline ban to the app's DEFAULT CSP entry: the internal
	// ~^/docs location (FastAPI docs UI, main-side, pre-existing) legitimately
	// carries 'unsafe-inline' for swagger assets and is not part of the public
	// app surface this test guards.
	const defaultCsp = nginx
		.split("\n")
		.find((line) => line.includes('default "default-src'));
	assert.ok(defaultCsp, "default CSP map entry must exist in nginx.conf");
	assert.doesNotMatch(defaultCsp, /script-src[^;]*'unsafe-inline'/);
});

test("generated public product and social images exist", () => {
	assert.equal(
		existsSync(new URL("../public/product-workspace.png", import.meta.url)),
		true,
	);
	assert.equal(
		existsSync(new URL("../public/og-image.png", import.meta.url)),
		true,
	);
});
test("landing does not claim the OOS gate rolls its window forward", () => {
	// The rigor gate is a single 70/30 chronological cut; the rolling
	// walk-forward re-estimation exists but never runs on a live path
	// (rigor_evaluator.py emits NOT_RUN for cpcv). Only the false "forward
	// through time" claim is retracted — the card name is repo-wide
	// vocabulary and must survive (see the previous test).
	assert.doesNotMatch(landing, /forward through time/);
	assert.match(landing, /Walk-forward out-of-sample/);
	assert.match(landing, /held-?out/);
});

test("landing does not claim a failed gate is unoverridable, or that a generation run anchors on Arc", () => {
	// Two false claims retracted 2026-08-30, both verified against the code
	// rather than reasoned about, and both pinned here so a rewrite cannot
	// reintroduce them:
	//
	// 1. "A failed gate is not overridable." POST /api/paper/deployments
	//    (api/paper_routes.py:85-125) is the act-on step a visitor can actually
	//    reach, and it checks ownership of the source strategy plus that the
	//    stored spec still validates — nothing else. There is no rigor
	//    precondition on it; StrategyPassport.jsx:381-382 says exactly that in
	//    the code ("no wallet, no rigor precondition, free by design"). The
	//    owner has deployed a gate-failing strategy to paper himself. The
	//    server-side deploy gate that DOES fail closed (vaults_routes
	//    ._deployable_levels) guards a path this surface no longer describes.
	//
	// 2. "…content-hashed and anchored on Arc" / "Anchor its reasoning on Arc
	//    before it reports a verdict." A generation run computes a keccak
	//    provenance hash and persists it on the strategy row — and
	//    generation_pipeline._persist_candidate's own docstring says that
	//    identifier is "mirrored on-chain in v1.5", i.e. not today. The only
	//    code that writes to ReasoningTraceRegistry is the agent rebalance tick
	//    (chain/agent_runner._commit_trace / _reveal_trace), which no
	//    generation run reaches.
	assert.doesNotMatch(landing, /not overridable/i);
	assert.doesNotMatch(landing, /anchored on Arc/i);
	assert.doesNotMatch(landing, /Anchor its reasoning/i);

	// The replacement invariant must still be an invariant, not a softening:
	// the verdict is not the user's to move, even though running a failing idea
	// in simulation is allowed.
	assert.match(landing, /A failing strategy stays a failing strategy\./);
	assert.match(
		landing,
		/Paper-trading one is allowed\. Relabelling one is not/,
	);
	assert.match(
		landing,
		/paper-trade a failing candidate — simulated, no capital/,
	);

	// Anti-vacuity: the exact pre-scrub literals must trip the predicates above,
	// so a future edit that neuters them fails here instead of passing silently.
	for (const [claim, pattern] of [
		["<strong>A failed gate is not overridable.</strong>", /not overridable/i],
		[
			"the gate verdict — content-hashed and anchored on Arc.",
			/anchored on Arc/i,
		],
		[
			"<li>Anchor its reasoning on Arc before it reports a verdict</li>",
			/Anchor its reasoning/i,
		],
	]) {
		assert.match(
			claim,
			pattern,
			`the guard no longer rejects the retracted claim ${JSON.stringify(claim)} — it is guarding nothing`,
		);
	}
});

test("protocols panel describes V_check by the checks it performs", () => {
	// v_check.py does arithmetic on a weights dict (sum == 10000 bps, max
	// concentration, and an optional cost-benefit floor no live caller
	// supplies). It never reads chain state or LLM output, so it cannot be a
	// chain-vs-narrative consistency gate. Hierarchy of Truth's chain-holdings
	// half is the vault-rebalance path, which is not live — the copy must say
	// so rather than describing it as shipped.
	//
	// Anchored to the Hierarchy of Truth entry specifically, not the whole
	// 1200-line file: a bare `assert.match(architecture, /concentration/)`
	// only guards anything today because the word happens to be unique in
	// the file, so a future rewrite of this exact `what:` string back to a
	// chain-vs-narrative claim would still pass as long as any other line
	// anywhere in the file mentions concentration.
	const hot = architecture.slice(
		architecture.indexOf('name: "Hierarchy of Truth"'),
	);
	const what = hot.slice(0, hot.indexOf("},"));
	assert.doesNotMatch(what, /V_check fails any rebalance where they disagree/);
	assert.match(
		what,
		/When vault execution ships/,
		"Hierarchy of Truth must not describe the rebalance loop as a live path",
	);
	assert.match(what, /will outrank the LLM's narrative/);
	assert.match(what, /concentration/);
	assert.match(what, /not live/);
});

test("honesty ledger gives every row an explicit LedgerStatus verdict", () => {
	// A status cell with no <LedgerStatus> verdict reads as an implicit
	// "Live" next to coloured verdicts on neighbouring rows — on the
	// ledger's highest-stakes row (the autonomous rebalance loop), that is
	// exactly backwards when liveness is genuinely unverified.
	const ledger = architecture.slice(
		architecture.indexOf("function HonestyLedger"),
	);
	const tbody = ledger.slice(
		ledger.indexOf("<tbody>"),
		ledger.indexOf("</tbody>"),
	);
	const rows = tbody.split("<tr>").slice(1);
	assert.equal(rows.length, 8);
	for (const row of rows) assert.match(row, /<LedgerStatus/);
	// The rebalance row must not assert a single hardcoded verdict either
	// way — runner liveness changes over time (the runner was relocated off
	// the old detached EC2 box 2026-08-18/19, #1043/#1065, and could go
	// down again later), so the row must be driven by the live
	// /api/agent/status heartbeat (`agentStatus.alive`) and able to render
	// either a "live" or a "pending" verdict depending on what it reports —
	// never a claim asserted independent of that signal.
	const rebalanceRow = rows.find((row) =>
		row.includes("Autonomous rebalance loop"),
	);
	assert.match(rebalanceRow, /agentStatus\.alive/);
	assert.match(rebalanceRow, /tone="live"/);
	assert.match(rebalanceRow, /tone="pending"/);
});

test("honesty ledger's rebalance row does not tie the full commit/trade/reveal mechanism claim to the heartbeat-only 'Live' verdict — PR #1382 round-2 review", () => {
	// The heartbeat (`agentStatus.alive`) is written unconditionally after
	// every tick — including one that failed entirely (agent_runner.py's
	// outer try/except swallows a failed tick() and logs "will retry" — the
	// heartbeat save sits after that, unguarded) — and it is orthogonal to
	// AGENT_DRY_RUN, under which no commit/trade/reveal happens at all
	// (agent_runner.py gates each phase separately on `if not DRY_RUN`).
	// The heartbeat alone cannot back a claim that evaluate/commit/trade/
	// reveal actually ran; the live-verdict clause must say only what the
	// heartbeat proves (the loop is ticking). Scoped to the whole row (not
	// just the live branch): the pending branch is under the exact same
	// AGENT_DRY_RUN=true recommended default (infra/scripts/setup-ssm-
	// secrets.sh), so an unqualified mechanism claim there is the same
	// defect, just reached via a different verdict.
	const ledger = architecture.slice(
		architecture.indexOf("function HonestyLedger"),
	);
	const tbody = ledger.slice(
		ledger.indexOf("<tbody>"),
		ledger.indexOf("</tbody>"),
	);
	const rebalanceRow = tbody
		.split("<tr>")
		.slice(1)
		.find((row) => row.includes("Autonomous rebalance loop"));
	assert.doesNotMatch(
		rebalanceRow,
		/evaluate, commit, trade, reveal/,
		"a branch of the rebalance row claims the full commit/trade/reveal mechanism ran off a signal (heartbeat) that doesn't measure it — same defect class this PR exists to police",
	);
	const liveBranch = rebalanceRow.slice(
		rebalanceRow.indexOf("agentStatus.alive ? ("),
		rebalanceRow.indexOf(") : ("),
	);
	assert.match(liveBranch, /heartbeat/i);
});

// ── Type scale, controls and link affordance ──────────────────────────────
//
// Owner review of the rebrand: "a lot of blank space and small text… make
// things a bit bigger, easier to read, links more obvious, buttons look
// better." The measured cause was a ~10x spread on one screen — a 96px
// section heading over a 9.6px footer, with running prose at 0.74rem — so the
// fix was a scale expressed as tokens rather than ~40 tuned literals. These
// guard the scale and, more importantly, the two mechanisms that let a
// sub-floor size reach the screen in the first place.

const TYPE_FLOOR_REM = 0.8125; // 13px

// App.css carries TWO `.public-site {` blocks — a legacy one and the rebrand
// layer at the end of the sheet. They have identical specificity, so the LAST
// one is the one that renders; a helper that grabbed the first would assert
// against a palette no visitor ever sees.
function publicSiteBlock() {
	const blocks = [...css.matchAll(/\n\.public-site \{([\s\S]*?)\n\}/g)];
	assert.ok(blocks.length, ".public-site block not found");
	return blocks.at(-1)[1];
}

test("the public type scale and control tokens exist, and the scale bottoms out at 13px", () => {
	const block = publicSiteBlock();
	for (const name of [
		"fs-display",
		"fs-title",
		"fs-lede",
		"fs-body",
		"fs-ui",
		"fs-meta",
		"fs-micro",
		"control-h",
		"control-h-lg",
		"control-pad",
		"control-radius",
	]) {
		assert.match(
			block,
			new RegExp(`--${name}:`),
			`the public token set is missing --${name}`,
		);
	}
	// --fs-micro is the floor every other public size is measured against. If
	// it drops, every call site that resolves it drops with it, silently.
	const micro = /--fs-micro:\s*([^;]+);/.exec(block)[1].trim();
	assert.ok(
		micro.endsWith("rem") && Number.parseFloat(micro) >= TYPE_FLOOR_REM,
		`--fs-micro is ${micro}; the public floor is ${TYPE_FLOOR_REM}rem (13px)`,
	);
});

test("no live public selector resolves to a sub-13px font size", () => {
	// The footer rendered at 9.6px for a reason worth guarding: a LEGACY
	// `.public-footer { font-size: 0.6rem }` earlier in the sheet, and a
	// rebrand rule for the same selector that declared no font-size at all —
	// so the legacy literal won and nothing in the rebrand layer looked wrong.
	// Equal specificity means the LAST declaration wins, so "some rule sets a
	// good value" is not a sufficient check; this resolves the winner the way
	// the cascade does and inspects that one.
	const ruleRe = /([^{}]+)\{([^{}]*)\}/g;
	const winner = new Map();
	let m;
	while ((m = ruleRe.exec(css))) {
		const selector = m[1]
			.replace(/\/\*[\s\S]*?\*\//g, "")
			.trim()
			.split("\n")
			.map((s) => s.trim())
			.join(" ");
		if (!/^\.public-[\w-]/.test(selector)) continue;
		const fs = /font-size:\s*([^;]+);/.exec(m[2]);
		if (fs) winner.set(selector, fs[1].trim());
	}
	assert.ok(winner.size > 10, "found no .public-* font-size rules to check");
	const offenders = [];
	for (const [selector, value] of winner) {
		const rem = /^([0-9.]+)rem$/.exec(value);
		if (rem && Number.parseFloat(rem[1]) < TYPE_FLOOR_REM) {
			offenders.push(`${selector} → ${value}`);
		}
	}
	assert.deepEqual(
		offenders,
		[],
		`public selectors resolving under ${TYPE_FLOOR_REM}rem (13px):\n${offenders.join("\n")}`,
	);
});

test("links carry their affordance at rest, not only on hover", () => {
	// A hover-only underline is not an affordance: keyboard and touch users
	// never produce a hover. Public links that are not shaped like buttons
	// underline at rest, in a colour mixed from currentColor so the rule stays
	// correct on the light canvas, the dark canvas and the theatre panels.
	assert.match(
		css,
		/\.public-site\s+a:where\([\s\S]{0,400}?\)\s*\{[^}]*text-decoration:\s*underline;[^}]*text-decoration-color:\s*color-mix\(in srgb, currentColor \d+%, transparent\)/s,
		"public links must underline at rest, derived from currentColor",
	);
	// The header's links opt out of the resting underline (five underlines in a
	// row would fight the wordmark), so they must gain one on FOCUS as well as
	// hover — otherwise a keyboard user gets no affordance at all.
	assert.match(
		css,
		/\.public-nav__link:focus-visible[\s\S]{0,240}?\{[^}]*text-decoration-color:\s*currentColor;/s,
		"header nav links must show their underline on keyboard focus, not just hover",
	);
});

test("methodology panels keep equal weight and fully legible caveats", () => {
	const deck = css.slice(
		css.indexOf(".public-proof-deck article {"),
		css.indexOf(".public-proof-deck__board"),
	);
	assert.ok(deck.length > 0);
	assert.doesNotMatch(
		deck,
		/background:\s*var\(--accent\)/,
		"a check must not look selected or more important just because of its position",
	);
	assert.doesNotMatch(
		deck,
		/opacity:/,
		"method and limit text must use theme ink, not opacity-reduced ink",
	);
	assert.match(deck, /color:\s*var\(--text-2\)/);
	assert.match(
		css,
		/\.public-use-case-scenes article > p\s*\{[^}]*color:\s*var\(--text-2\);/s,
	);
	assert.doesNotMatch(
		css,
		/\.public-use-case-scenes article > p\s*\{[^}]*opacity:/s,
	);
});
