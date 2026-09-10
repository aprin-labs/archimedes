import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

// WCAG 2.2 AA regression guards.
//
// Same shape as app-visuals.test.js / public-visuals.test.js: readFileSync +
// regex pins on the source, no DOM. These pin the specific accessibility
// properties that a later refactor is most likely to delete by accident —
// a token value, a live-region attribute, a real <button> where a click-only
// <span> used to be. Every assertion here was confirmed to FAIL against the
// pre-fix tree.

const src = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");

// ── WCAG contrast helpers ───────────────────────────────────────────────
//
// A regex pinning a literal foreground hex only guards that one side of the
// pair — the background it is measured against can drift (a token edit
// elsewhere in the same block) with the guard still green, because nothing
// ever computes the ratio. These resolve `--token: var(--other-token)`
// indirection inside one CSS block and compute the real WCAG 2.x relative
// luminance / contrast ratio, so the 4.5:1 floor is enforced against
// whatever the current tokens actually are, not a snapshot of them.

function cssBlock(cssText, selector) {
	const re = new RegExp(`${selector}\\s*\\{([\\s\\S]*?)\\n\\}`);
	const m = cssText.match(re);
	assert.ok(m, `CSS block not found: ${selector}`);
	return m[1];
}

function tokenValue(block, name) {
	const re = new RegExp(`--${name}:\\s*([^;]+);`);
	const m = block.match(re);
	assert.ok(m, `token not found in block: --${name}`);
	return m[1].trim();
}

function resolveHex(block, rawValue) {
	const varRef = rawValue.match(/^var\(--([\w-]+)\)$/);
	if (varRef) return resolveHex(block, tokenValue(block, varRef[1]));
	assert.match(rawValue, /^#[0-9a-fA-F]{3,6}$/, `not a hex literal: ${rawValue}`);
	return rawValue;
}

// A regex pinning `scroll-padding-top: 80px` only guards that one side of
// the clearance pair too — the header height / focus-ring geometry it was
// derived from can drift with the guard still green unless the relationship
// is actually computed. These pull a raw px number off a declaration and,
// for a rule nested one level inside another block (e.g. inside a @media
// query), off a block whose own closing brace carries that block's indent
// rather than sitting at column 0.

function numPx(block, prop) {
	const re = new RegExp(`\\b${prop}:\\s*(\\d+(?:\\.\\d+)?)px`);
	const m = block.match(re);
	assert.ok(m, `property not found in block: ${prop}`);
	return Number(m[1]);
}

function nestedCssBlock(blockText, selector) {
	const re = new RegExp(`${selector}\\s*\\{([\\s\\S]*?)\\n\\t\\}`);
	const m = blockText.match(re);
	assert.ok(m, `nested CSS block not found: ${selector}`);
	return m[1];
}

// createPortal(node, document.body) mounts OUTSIDE both shells, so the
// portalled subtree resolves the BASE palette even when a .app-site page
// opened it. For a component that renders inside a shell AND opens a portal
// (WalletConnect's topbar menu, Strategies' rigor modal) only that subtree is
// on the base palette, so a whole-file check would be wrong in both
// directions. Slice from each `createPortal(` to the `document.body` argument
// that closes the call.
function portalRegions(source) {
	const regions = [];
	const re = /createPortal\(/g;
	let m;
	while ((m = re.exec(source))) {
		const end = source.indexOf("document.body", m.index);
		if (end !== -1) regions.push(source.slice(m.index, end));
	}
	return regions;
}

function relativeLuminance(hex) {
	const h = hex.replace("#", "");
	const full = h.length === 3 ? [...h].map((c) => c + c).join("") : h;
	const n = Number.parseInt(full, 16);
	const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => {
		const s = v / 255;
		return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
	});
	return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrastRatio(hexA, hexB) {
	const [lo, hi] = [relativeLuminance(hexA), relativeLuminance(hexB)].sort(
		(a, b) => a - b,
	);
	return (hi + 0.05) / (lo + 0.05);
}

const css = src("App.css");
const criticalCss = readFileSync(new URL("../public/theme.css", import.meta.url), "utf8");

// First matching token wins here: theme-specific overrides, base roles, then
// compatibility aliases. Shells and portals inherit this same palette.
function themeBlock(mode) {
	const light = cssBlock(criticalCss, '\\[data-theme="light"\\]');
	const dark = mode === "dark" ? cssBlock(criticalCss, '\\[data-theme="dark"\\]') : "";
	return `${dark}\n${light}\n${cssBlock(criticalCss, ":root")}`;
}

function checkContrast(block, foreground, backgrounds, minimum) {
	const ink = resolveHex(block, tokenValue(block, foreground));
	for (const name of backgrounds) {
		const surface = resolveHex(block, tokenValue(block, name));
		const ratio = contrastRatio(ink, surface);
		assert.ok(ratio >= minimum, `--${foreground} on --${name}: ${ratio.toFixed(2)}:1 < ${minimum}:1`);
	}
}
const app = src("App.jsx");
const authPage = src("components/AuthPage.jsx");
const corpusExplorer = src("components/CorpusExplorer.jsx");
const corpusKg = src("components/CorpusKG.jsx");
const customSelect = src("components/CustomSelect.jsx");
const explore = src("components/Explore.jsx");
const generate = src("components/Generate.jsx");
const generationStatus = src("components/GenerationStatus.jsx");
const generationStream = src("components/GenerationStream.jsx");
const layout = src("components/Layout.jsx");
const onboardingTour = src("components/OnboardingTour.jsx");
const paperTrading = src("components/PaperTrading.jsx");
const strategies = src("components/Strategies.jsx");
const walletConnect = src("components/WalletConnect.jsx");

// ── 1.4.3 Contrast (Minimum) ──────────────────────────────────────────────
//
// Auth, both shells and body-level portals now share the Fulcro palette.
// Measure actual foreground/background pairs, not superseded color literals.

test("base and public palettes keep muted text above the 4.5:1 floor", () => {
	for (const mode of ["light", "dark"]) {
		for (const ink of ["text-3", "text-4", "accent"]) {
			checkContrast(themeBlock(mode), ink, ["canvas", "sidebar", "surface-1", "surface-2", "surface-3", "selected"], 4.5);
		}
	}
	// Keep the existing auth call sites on the named reading-text role.
	assert.doesNotMatch(authPage, /text-\[var\(--text-4\)\]/);
});

// RigorExplainer is the one file that is entirely portalled without calling
// createPortal itself: it has no call site outside Strategies' portalled
// rigor modal, so everything it draws resolves the base palette. It has to be
// named. Every OTHER portalled subtree is DISCOVERED below rather than listed,
// so a dialog added later is covered without anyone remembering to add it —
// a hand-maintained list would have silently excluded CustomSelect and
// OnboardingTour, which both portal to document.body and were outside the
// first version of this guard.
const ALWAYS_PORTALLED = ["components/RigorExplainer.jsx"];

// Every .jsx under src/ that opens a portal, as repo-relative paths.
function filesCallingCreatePortal() {
	const files = [];
	const walk = (dir) => {
		for (const entry of readdirSync(dir)) {
			const full = join(dir, entry);
			if (statSync(full).isDirectory()) walk(full);
			else if (/\.jsx$/.test(entry)) files.push(full);
		}
	};
	const root = new URL("../src", import.meta.url).pathname;
	walk(root);
	return files
		.filter((f) => readFileSync(f, "utf8").includes("createPortal("))
		.map((f) => f.slice(root.length + 1));
}

// Historically --text-4 was a low-contrast decoration token in portals.
// Retain the established reading-role call sites even though Fulcro also
// makes that legacy alias readable. Check every non-decoration spelling so
// ternaries and fallbacks cannot evade the source guard.
const TEXT_4_AS_DECORATION = [
	// DepositFlow's CONFIRMING spinner track: a 2px ring, not text.
	/border:\s*['"]2px solid var\(--text-4\)['"]/,
];

// A --text-4 mention that is not one of the allowed decoration spellings.
// Comment lines are skipped so a note explaining why the token is avoided
// does not read as a use of it.
function text4Offenders(label, source) {
	const found = [];
	for (const line of source.split("\n")) {
		if (!line.includes("--text-4")) continue;
		const trimmed = line.trim();
		if (/^(\/\/|\/\*|\*)/.test(trimmed)) continue;
		// Strip the allowed decoration spellings and re-check, rather than
		// skipping the whole line when one matches. DepositFlow's spinner is a
		// single long inline-style line, so a line-level skip would let a text
		// colour ride along on it — verified: that input passed a skip-based
		// version of this guard and fails this one.
		let rest = line;
		for (const pattern of TEXT_4_AS_DECORATION) {
			rest = rest.split(pattern).join("");
		}
		if (!rest.includes("--text-4")) continue;
		found.push(`${label} — ${trimmed.slice(0, 100)}`);
	}
	return found;
}

test("portalled dialogs retain readable text roles in both themes", () => {
	// Portals escape shell selectors. Verify their inherited root palette and
	// retain the call-site fixes from #1318; neither half proves the other.
	const base = themeBlock("dark");
	for (const mode of ["light", "dark"]) {
		checkContrast(themeBlock(mode), "text-3", ["surface-1", "surface-2", "surface-3"], 4.5);
	}
	const text3 = resolveHex(base, tokenValue(base, "text-3"));
	const text4 = resolveHex(base, tokenValue(base, "text-4"));

	// --surface-1 is `.modal`'s background and AssetModal's card background;
	// --surface-2 backs the nested blocks inside them; --surface-3 is
	// `.table-container thead th`, which RigorExplainer renders inside the
	// portalled rigor modal.
	for (const surfaceName of ["surface-1", "surface-2", "surface-3"]) {
		const surfaceHex = resolveHex(base, tokenValue(base, surfaceName));
		const ratio = contrastRatio(text3, surfaceHex);
		assert.ok(
			ratio >= 4.5,
			`--text-3 (${text3}) on --${surfaceName} (${surfaceHex}) is ${ratio.toFixed(2)}:1, below the 4.5:1 floor — portalled dialogs resolve these values`,
		);
	}

	const surface1 = resolveHex(base, tokenValue(base, "surface-1"));
	const decorationRatio = contrastRatio(text4, surface1);
	// Fulcro deliberately raises the old decoration alias to readable muted
	// ink. Preserve existing call-site discipline as well as the stronger floor.
	assert.ok(decorationRatio >= 4.5);

	const offenders = [];
	for (const file of ALWAYS_PORTALLED) {
		offenders.push(...text4Offenders(file, src(file)));
	}

	// Region-slice every file that opens a portal. A component can render
	// inside .app-site AND open a portal (WalletConnect's topbar menu,
	// Strategies' rigor modal), so only the portalled subtree is on the base
	// palette — a whole-file check would be wrong in both directions, and
	// Strategies legitimately keeps 9 --text-4 call sites outside its portal.
	const portalFiles = filesCallingCreatePortal();
	assert.ok(
		portalFiles.includes("components/Strategies.jsx") &&
			portalFiles.includes("components/CustomSelect.jsx"),
		`portal discovery found ${portalFiles.length} files but missed a known one: ${portalFiles.join(", ")}`,
	);
	for (const file of portalFiles) {
		const regions = portalRegions(src(file));
		assert.ok(regions.length > 0, `${file}: no createPortal region found`);
		for (const region of regions) {
			offenders.push(...text4Offenders(`${file} (portalled subtree)`, region));
		}
	}
	assert.deepEqual(
		offenders,
		[],
		`these portalled dialogs changed established --text-3 reading roles to legacy --text-4:\n${offenders.join("\n")}`,
	);
});

test("primary fill and readable accent text remain separate roles", () => {
	// Lime primary fill cannot also be readable ink on light surfaces.
	// Legacy --accent and --accent-text both resolve the separate link role.
	const light = themeBlock("light");
	assert.notEqual(resolveHex(light, tokenValue(light, "primary")), resolveHex(light, tokenValue(light, "accent-text")));
	for (const mode of ["light", "dark"]) {
		checkContrast(themeBlock(mode), "on-primary", ["primary", "primary-hover"], 4.5);
	}
	const accentText = resolveHex(light, tokenValue(light, "accent-text"));
	for (const surfaceName of ["surface-1", "surface-2", "surface-3"]) {
		const surfaceHex = resolveHex(light, tokenValue(light, surfaceName));
		const ratio = contrastRatio(accentText, surfaceHex);
		assert.ok(
			ratio >= 4.5,
			`--accent-text (${accentText}) on --${surfaceName} (${surfaceHex}) is ${ratio.toFixed(2)}:1, below the 4.5:1 floor`,
		);
	}
	// Live-status text must retain contrast independently of action colors.
	const positive = resolveHex(light, tokenValue(light, "positive"));
	const surface2 = resolveHex(light, tokenValue(light, "surface-2"));
	const positiveRatio = contrastRatio(positive, surface2);
	assert.ok(
		positiveRatio >= 4.5,
		`--positive (${positive}) on --surface-2 (${surface2}) is ${positiveRatio.toFixed(2)}:1, below the 4.5:1 floor`,
	);
	// No rule targeting the public shell may paint text with the raw fill
	// token. Scope by selector, not a slice of this layered stylesheet.
	const offenders = [];
	const ruleRe = /([^{}]+)\{([^{}]*)\}/g;
	let rule;
	while ((rule = ruleRe.exec(css))) {
		const selector = rule[1]
			.replace(/\/\*[\s\S]*?\*\//g, "")
			.trim()
			.split("\n")
			.map((s) => s.trim())
			.join(" ");
		if (!/^\.public-[\w-]|^\.authority-boundary|^\.security-/.test(selector)) {
			continue;
		}
		if (/^\s*color:\s*var\(--primary\);\s*$/m.test(rule[2])) {
			offenders.push(selector);
		}
	}
	assert.deepEqual(
		offenders,
		[],
		`these public rules paint text with --primary (a fill token) instead of --accent-text:\n${offenders.join("\n")}`,
	);
});

test("chart axis labels clear 4.5:1 on every card they are drawn on", () => {
	// Axis ticks are real 8-10px <text>. Neither shell overrides these tokens
	// and the asset modal portals outside both, so the BASE values are the
	// ones that must hold. 0.42 / 0.5 gave 3.88:1 and 3.74:1.
	for (const mode of ["light", "dark"]) {
		checkContrast(themeBlock(mode), "chart-label", ["canvas", "surface-1", "surface-2", "surface-3"], 4.5);
	}
});

test("existing error paths retain the shared negative role", () => {
	// Keep the established call-site fix. The legacy --negative role now
	// aliases Fulcro --danger rather than falling back to a literal color.
	const files = [];
	const walk = (dir) => {
		for (const entry of readdirSync(dir)) {
			const full = join(dir, entry);
			if (statSync(full).isDirectory()) walk(full);
			else if (/\.jsx?$/.test(entry)) files.push(full);
		}
	};
	walk(new URL("../src", import.meta.url).pathname);
	const offenders = files.filter((f) =>
		readFileSync(f, "utf8").includes("--danger"),
	);
	assert.deepEqual(offenders, []);
	assert.match(criticalCss, /--negative:\s*var\(--danger\);/);
	for (const mode of ["light", "dark"]) checkContrast(themeBlock(mode), "negative", ["surface-1", "surface-2"], 4.5);
});

// ── 1.4.11 Non-text Contrast / 2.4.7 Focus Visible ────────────────────────

test("form fields carry a 3:1 boundary token in both shells", () => {
	// Fields are filled with the canvas inside surface cards, so the fill gives
	// no cue (1.08:1) and --glass-border was 1.22-1.45:1.
	for (const mode of ["light", "dark"]) {
		checkContrast(themeBlock(mode), "field-border", ["canvas", "sidebar", "surface-1", "surface-2", "surface-3"], 3);
		checkContrast(themeBlock(mode), "focus", ["canvas", "sidebar", "surface-1", "surface-2"], 3);
	}
	assert.match(
		css,
		/^input,\nselect,\ntextarea \{[^}]*border: 1px solid var\(--field-border\);/m,
	);
	assert.match(
		css,
		/\.app-site \.chat-input,\n\.app-site input,\n\.app-site select,\n\.app-site textarea \{[^}]*border-color: var\(--field-border\);/,
	);
});

test("no rule strips the focus indicator from a control", () => {
	// `.app-site input:focus { outline: none }` out-specified
	// `.app-site :focus-visible` and deleted the 3px ring from every text
	// field in the shell; the base input rule did the same on the auth screen,
	// which has no :focus-visible rule of its own to fall back to.
	for (const block of [
		/^input,\nselect,\ntextarea \{[^}]*\}/m,
		/\.app-site \.chat-input:focus,[\s\S]*?\}/,
		/^\.cs-trigger \{[^}]*\}/m,
		/^\.catalog-search:focus,\n\.kg-search:focus \{[^}]*\}/m,
	]) {
		const match = css.match(block);
		assert.ok(match, `rule not found: ${block}`);
		assert.doesNotMatch(match[0], /outline:\s*none/);
	}
	assert.match(css, /\.app-site :focus-visible \{\n\toutline: 3px solid/);
});

// ── 2.4.11 Focus Not Obscured / 1.4.4 Resize Text ─────────────────────────

test("sticky topbar cannot cover a focused control", () => {
	// Pinning the scroll-padding-top literal alone only guards that one side
	// of the clearance pair — the sticky bar's height and the focus ring's
	// outline width / offset can drift out from under it with the guard
	// still green. This resolves both sides out of the live CSS for each
	// shell and asserts scroll-padding-top actually clears
	// height + outline + outline-offset, the same static-parse approach the
	// contrast test above uses.

	// App shell: `.topbar`'s own rule below says 56px, but `.topbar` only
	// ever renders inside `.app-site` (Layout.jsx nests it under
	// `.shell.app-site`), and `.app-site .topbar` — higher specificity, not
	// scoped to any breakpoint — overrides that to 64px. That is the height
	// that actually renders, so it's what has to be measured, not the bare
	// rule's 56px.
	const appTopbarHeight = numPx(cssBlock(css, "\\.app-site \\.topbar"), "height");
	const appFocus = cssBlock(css, "\\.app-site :focus-visible");
	const appRequired =
		appTopbarHeight + numPx(appFocus, "outline") + numPx(appFocus, "outline-offset");
	const baseScrollPad = numPx(cssBlock(css, "html"), "scroll-padding-top");
	assert.ok(
		baseScrollPad >= appRequired,
		`scroll-padding-top (${baseScrollPad}px) does not clear .app-site .topbar: ` +
			`${appTopbarHeight}px height + focus ring needs ${appRequired}px`,
	);

	// Public shell: `.public-header__inner` is taller than the app topbar
	// (72px, 66px on the ≤560px breakpoint) with a wider :focus-visible
	// offset (4px) than `.app-site`'s (3px). `.public-site` is not the
	// scroll container — html is — so the override has to reach html via
	// :has(), not sit on .public-site itself (#1318 residual).
	const publicHeaderHeight = numPx(cssBlock(css, "\\.public-header__inner"), "min-height");
	const media560 = cssBlock(css, "@media \\(max-width: 560px\\)");
	const publicHeaderHeight560 = numPx(
		nestedCssBlock(media560, "\\.public-header__inner"),
		"min-height",
	);
	// `.public-header__inner`'s min-height is not the whole sticky bar: the
	// outer `.public-header` adds its own border-bottom below it, which also
	// occupies rendered height a scrolled-to control can hide under.
	const publicHeaderBorder = numPx(cssBlock(css, "\\.public-header"), "border-bottom");
	const publicFocus = cssBlock(css, "\\.public-site :focus-visible");
	const publicRequired =
		Math.max(publicHeaderHeight, publicHeaderHeight560) +
		publicHeaderBorder +
		numPx(publicFocus, "outline") +
		numPx(publicFocus, "outline-offset");
	const publicScrollPad = numPx(
		cssBlock(css, "html:has\\(\\.public-site\\)"),
		"scroll-padding-top",
	);
	assert.ok(
		publicScrollPad >= publicRequired,
		`html:has(.public-site) scroll-padding-top (${publicScrollPad}px) does not clear ` +
			`.public-header__inner: needs ${publicRequired}px`,
	);
});

test("corpus category labels wrap instead of clipping under zoom", () => {
	const rule = css.match(/^\.bar-label,\n\.year-label \{[^}]*\}/m);
	assert.ok(rule);
	assert.doesNotMatch(rule[0], /width:\s*140px/);
	assert.doesNotMatch(rule[0], /white-space:\s*nowrap/);
	assert.match(rule[0], /flex: 0 1 140px/);
});

// ── 2.4.1 Bypass Blocks / 2.4.2 Page Titled ───────────────────────────────

test("the authenticated shell ships a skip link with a focusable target", () => {
	assert.match(layout, /className="app-skip-link" href="#app-content"/);
	assert.match(layout, /id="app-content"\s+tabIndex=\{-1\}/);
	// It cannot borrow .public-skip-link's colours — --public-paper /
	// --public-abyss are undefined inside .app-site.
	assert.match(css, /\.app-skip-link \{\n\tbackground: var\(--text-1\);/);
	// ...and it must out-stack the sidebar. `.sidebar` is position: fixed,
	// inset: 0 auto 0 0, opaque, z-index: 100, and comes LATER in the DOM than
	// the link, so at the shared block's z-index: 100 the sidebar won the paint
	// order and covered the link's entire focused box (top 10px / left 12px,
	// inside the 232px rail). Verified in a browser against the built CSS:
	// elementFromPoint at all three corners returned DIV.sidebar-brand.
	// `.app-skip-link` is declared twice — once in the shared geometry block
	// alongside `.public-skip-link`, once on its own — at equal specificity, so
	// the EFFECTIVE z-index is the last one declared. Read it that way rather
	// than matching the first rule that mentions the class.
	const zDecls = [
		...css.matchAll(/([^{}]*\.app-skip-link[^{}]*)\{([^}]*)\}/g),
	].flatMap((rule) =>
		[...rule[2].matchAll(/z-index:\s*(\d+);/g)].map((m) => Number(m[1])),
	);
	assert.ok(zDecls.length > 0, ".app-skip-link declares no z-index");
	const effectiveZ = zDecls[zDecls.length - 1];
	const sidebar = css.match(/^\.sidebar \{[^}]*\}/m);
	const sidebarZ = Number(sidebar[0].match(/z-index:\s*(\d+);/)[1]);
	assert.ok(
		effectiveZ > sidebarZ,
		`skip link z-index ${effectiveZ} must beat .sidebar's ${sidebarZ}`,
	);
});

test("a failed deep link does not share the landing page's title", () => {
	// Quote-style agnostic: the rebrand formats with double quotes; the
	// guarded behavior (a dedicated not-found title, keyed off route.kind)
	// is what matters, not the quoting.
	assert.match(app, /["']not-found["']: ["']Page not found · Archimedes["']/);
	// A denied /app/insights admin-gate probe (owner directive 2026-08-20)
	// titles the tab as 'not-found' too — see the next test — so this key
	// computation ORs in that case rather than checking route.kind alone.
	assert.match(
		app,
		/const key = route\.kind === ["']not-found["'] \|\| deniedInsights \? ["']not-found["'] : route\.page/,
	);
});

test("a denied insights admin-gate probe titles the tab as not-found, not 'Insights'", () => {
	// "do not advertise existence" (owner directive 2026-08-20) applies to the
	// tab title too — a many-tabs user must not be able to tell "unknown
	// route" apart from "gated route I'm not allowed on" — or from "gate
	// still resolving" (round 3: isInsightsPageBlocked treats an unresolved
	// probe the same as a denied one, for the title as well as the render).
	assert.match(
		app,
		/const deniedInsights = isInsightsPageBlocked\(route\.page, insightsAdmin\)/,
	);
});

// ── 2.1.1 Keyboard / 4.1.2 Name, Role, Value ──────────────────────────────

test("filter and toggle pills are real buttons with a pressed state", () => {
	// Library tabs: activeTab defaults to 'generated', so click-only spans
	// pinned a keyboard user to that one view forever.
	assert.match(
		strategies,
		/<button\n\s+type="button"\n\s+className=\{`tag \$\{activeTab === 'generated'[\s\S]{0,120}aria-pressed=\{activeTab === 'generated'\}/,
	);
	assert.match(strategies, /aria-pressed=\{activeTab === 'examples'\}/);
	assert.match(strategies, /aria-pressed=\{activeTab === 'published'\}/);
	// Explore asset-class filter.
	assert.match(explore, /aria-pressed=\{filterClass === c\}/);
	// Generate asset universe picker.
	assert.match(generate, /aria-pressed=\{selectedAssets\.includes\(a\)\}/);
	// None of the three may regress to a click-only span.
	for (const [name, source] of [
		["Strategies", strategies],
		["Explore", explore],
		["Generate", generate],
	]) {
		assert.doesNotMatch(
			source,
			/<span[^>]*className=\{`tag[\s\S]{0,200}?onClick=/,
			`${name} still has a click-only tag span`,
		);
	}
	// The pill reset that lets a <button> render like the <span> it replaced.
	assert.match(css, /button:where\(\.tag\) \{/);
});

test("row-level click targets expose a real control", () => {
	// Library: App.css hides the keyboard-accessible card list above 768px, so
	// the desktop table row was the only disclosure and it had no role, no tab
	// stop and no aria-expanded.
	assert.match(strategies, /className="lib-row-toggle"[\s\S]{0,120}aria-expanded=\{open\}/);
	// aria-controls must be conditional: <tr id={detailId}> only exists in the
	// DOM while open, so an unconditional aria-controls pointed at a
	// nonexistent id on every collapsed row (4.1.2, #1318).
	assert.match(strategies, /aria-controls=\{open \? detailId : undefined\}/);
	assert.doesNotMatch(strategies, /aria-controls=\{detailId\}/);
	assert.match(strategies, /className="lib-row-detail" id=\{detailId\}/);
	// Corpus catalog: clicking the row was the only way into a paper.
	assert.match(
		corpusExplorer,
		/className="catalog-title-btn"[\s\S]{0,140}openPaper\(p\.arxiv_id\)/,
	);
	// Recent Generations: role="button" on a <tr> destroys the cells.
	assert.doesNotMatch(generationStatus, /<tr[\s\S]{0,200}role="button"/);
	assert.match(generationStatus, /<caption className="sr-only">Recent generations<\/caption>/);
	assert.match(generationStatus, /<th scope="col"/);
});

test("shared select and its call site expose an accessible name", () => {
	assert.match(customSelect, /aria-label=\{ariaLabel\}/);
	assert.match(customSelect, /aria-controls=\{open \? listboxId : undefined\}/);
	assert.match(customSelect, /aria-activedescendant=\{/);
	assert.match(corpusExplorer, /ariaLabel="Filter papers by category"/);
});

test("sidebar navigation states where the user currently is", () => {
	assert.match(layout, /<nav aria-label="Main">/);
	assert.match(layout, /aria-current=\{isCurrent \? "page" : undefined\}/);
	// aria-label survives only for the collapsed rail, where .nav-label is
	// display:none — on the expanded rail it silently overrode the visible
	// label (2.5.3).
	assert.match(layout, /aria-label=\{sidebarCollapsed \? item\.label : undefined\}/);
});

// ── 1.1.1 Non-text Content / 1.4.1 Use of Color ───────────────────────────

test("informative icons and charts carry a text alternative", () => {
	// UnoCSS icons are a CSS mask on an empty <span>: no role, no name, and
	// `title` on a bare span is not reliably exposed.
	assert.match(strategies, /role="img" aria-label="Passes rigor gate"/);
	assert.match(strategies, /role="img" aria-label="Does not pass rigor gate"/);
	// Strategies.jsx must use --warning, never the raw amber hex: 2.15:1 on a
	// white card in the light theme.
	assert.doesNotMatch(strategies, /#f59e0b/);
	// The knowledge graph is a 500px informative SVG that had no role at all.
	assert.match(
		corpusKg,
		/role="img"\n\s+aria-label=\{`Topic cluster graph: \$\{entities\.length\} entities, \$\{relations\.length\} relations`\}/,
	);
});

test("threshold verdicts do not rely on colour alone", () => {
	// "(43%)" and "(97%)" rendered identically apart from green/red.
	assert.match(strategies, /replicated \? '✓' : '✗'/);
	assert.match(strategies, /below the 50% replication threshold/);
	assert.match(strategies, /above the 0\.50 overfitting threshold/);
	// The Library table's "$1k ->" projection painted every strategy
	// profit-green regardless of sign; fmtUsd never emits a minus, so a losing
	// projection needs its own sr-only text alternative (#1361).
	assert.match(strategies, /below the \{fmtUsd\(principal\)\} starting principal/);
	assert.match(css, /^\.sr-only \{/m);
});

// ── 2.4.3 Focus Order / 2.5.8 Target Size / 2.5.7 Dragging ────────────────

test("portalled dialogs move, trap and restore focus", () => {
	for (const [name, source] of [
		["OnboardingTour", onboardingTour],
		["Strategies", strategies],
		["WalletConnect", walletConnect],
	]) {
		assert.match(
			source,
			/import useDialogFocus from '\.\.\/hooks\/useDialogFocus'/,
			`${name} does not use the shared dialog-focus hook`,
		);
	}
	// The two dialogs that had no dialog semantics at all.
	assert.match(
		walletConnect,
		/role="dialog"\n\s+aria-modal="true"\n\s+aria-labelledby="wallet-modal-title"/,
	);
	assert.match(walletConnect, /<h3 id="wallet-modal-title">Connect Wallet<\/h3>/);
	assert.match(
		strategies,
		/role="dialog"\n\s+aria-modal="true"\n\s+aria-labelledby="rigor-explainer-title"/,
	);
	// Escape had no handler on the rigor explainer.
	assert.match(strategies, /useDialogFocus\(rigorModalOpen, \{ onEscape: closeRigorExplainer \}\)/);
});

test("tour pagination dots meet the 24px minimum target", () => {
	// 8px dots with a 6px gap: the 24px targets centred on adjacent dots
	// overlapped, so the spacing exception did not apply either.
	assert.match(onboardingTour, /className="w-6 h-6 flex items-center justify-center border-none cursor-pointer"/);
	// role="tab" promised arrow-key navigation and tabpanels this component
	// never implemented.
	assert.doesNotMatch(onboardingTour, /role="tablist"/);
	assert.doesNotMatch(onboardingTour, /role="tab"/);
});

test("Generate's primary controls meet the target minimum at phone width", () => {
	// #1642 made Generate mobile-first, which means its two primary controls
	// — submit and "Surprise me" — are thumb targets first and mouse targets
	// second. Same 24px WCAG 2.2 AA floor as the tour dots above.
	//
	// The check reads the PHONE rules specifically: everything in the #1642
	// block before its first `@media` is the base (no-media-query) tier, i.e.
	// what a 375px viewport gets. A min-height added only inside
	// `min-width: 560px` would satisfy a whole-file grep and still leave the
	// phone short, so slicing the base tier out is the point of this test.
	const BANNER = "#1642 — Generate page: mobile-first layout + Surprise Me";
	const blockStart = css.indexOf(BANNER);
	assert.ok(blockStart > 0, "the #1642 Generate block is missing from App.css");
	const phoneBase = css.slice(blockStart).split("@media")[0];

	const MIN_TARGET_PX = 24;
	for (const selector of ["\\.generate-surprise-btn", "\\.generate-brief \\.generate-submit-btn"]) {
		const height = numPx(cssBlock(phoneBase, selector), "min-height");
		assert.ok(
			height >= MIN_TARGET_PX,
			`${selector} is ${height}px at phone width, below the ${MIN_TARGET_PX}px minimum`,
		);
	}

	// A rule guards nothing if the markup does not carry the class.
	assert.match(generate, /className="generate-surprise-btn"/);
	assert.match(generate, /className="btn btn-primary generate-submit-btn"/);
	// Both live inside .generate-brief, which is what makes the two-selector
	// submit rule apply at all.
	assert.match(generate, /className="card generate-brief"/);
});

test("knowledge-graph pan and zoom have single-pointer alternatives", () => {
	// Pan was drag-only and zoom wheel-only; "Reset view" only ever returns to
	// the origin.
	assert.match(corpusKg, /aria-label="Graph view controls"/);
	for (const label of ["Zoom in", "Zoom out", "Pan left", "Pan right", "Pan up", "Pan down"]) {
		assert.match(corpusKg, new RegExp(`aria-label="${label}"`), `missing ${label}`);
	}
});

// ── 3.3.x Labels, Instructions, Error Identification ──────────────────────

test("the generate form's fields are programmatically labelled", () => {
	assert.match(generate, /<label className="label mb-1 block" htmlFor="generate-brief">/);
	// The description widened with the 600-character bound (#1801): the live
	// counter is announced WITH the field, not left visual-only, so a
	// screen-reader user learns why the textarea stopped accepting keystrokes.
	assert.match(
		generate,
		/id="generate-brief"\s+aria-describedby="generate-brief-help generate-brief-count"/,
	);
	assert.match(generate, /htmlFor="generate-strategy-name"/);
	assert.match(generate, /aria-label="Search assets"/);
});

test("the corpus search field has a name that survives typing", () => {
	// The name widened with the author leg (#1451) — it now states the three
	// columns the field actually searches, so match on the stable prefix rather
	// than the exact old string.
	assert.match(corpusExplorer, /aria-label="Search papers[^"]*"/);
	assert.match(corpusExplorer, /aria-describedby="catalog-search-scope"/);
});

test("sign-up states why the confirm field is invalid and the button disabled", () => {
	assert.match(authPage, /id="password-rules"/);
	assert.match(authPage, /id="confirm-mismatch"/);
	assert.match(
		authPage,
		/aria-describedby=\{showMismatch \? 'password-rules confirm-mismatch' : 'password-rules'\}/,
	);
});

test("unlinking a wallet is confirmed before it happens", () => {
	// Single unconfirmed click deleted the account↔wallet binding with no undo.
	assert.match(
		src("components/AccountSettings.jsx"),
		/window\.confirm\(\n\s+`Unlink \$\{label\}\?/,
	);
});

// ── 4.1.3 Status Messages ─────────────────────────────────────────────────

test("generation outcomes are announced, not just painted", () => {
	// One slot carried start failures, the quote-not-ready message, both
	// payment banners and the settlement receipt, with no role or aria-live.
	assert.match(
		generate,
		/className="generate-submit-status"\s+role="status"\s+aria-live="polite"\s+aria-atomic="true"/,
	);
	// The stream is a minutes-long append-only feed.
	assert.match(
		generationStream,
		/role="log"\n\s+aria-live="polite"\n\s+aria-relevant="additions text"\n\s+aria-label="Generation events"/,
	);
	// The live region must hold ONLY the terminal outcome. `events.length`
	// increments on every SSE frame, so while the running-state counter sat
	// inside the region a screen reader re-read the job id and the whole block
	// once per event for the length of the run.
	assert.match(
		generationStream,
		/role="status"\n\s+aria-live=\{terminal === 'error' \? 'assertive' : 'polite'\}/,
	);
	const liveRegion = generationStream.match(
		/<div\n\s+role="status"\n\s+aria-live=\{terminal[\s\S]*?\n {10}<\/div>/,
	);
	assert.ok(liveRegion, "GenerationStream live region not found");
	assert.doesNotMatch(liveRegion[0], /events\.length/);
});

test("stopping a paper deployment says so", () => {
	// On success the row's chip silently flipped and the Stop button unmounted.
	assert.match(paperTrading, /<div role="status" aria-live="polite"/);
	assert.match(paperTrading, /setNotice\(`Paper trading stopped for \$\{label\}\./);
	assert.match(corpusExplorer, /className="catalog-results-info" role="status"/);
});
