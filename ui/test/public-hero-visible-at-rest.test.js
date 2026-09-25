/**
 * Issue #1750 — the landing hero rendered as a solid black void.
 *
 * The hero's own rules declare no opacity. The only opacity the stylesheet
 * ever stated for that content was `from { opacity: 0 }` in the
 * `public-arrive` entrance, so the headline, lede, CTA row and product
 * screenshot were visible only as a consequence of that animation running.
 * Every state that holds an animation's first frame therefore painted the
 * hero at opacity 0 over `.public-hero__stage`'s near-black `--public-stage`
 * (#0c0c11) — including the one state that never ends, a play-pending
 * animation whose start time never resolves.
 *
 * No fill mode closes that. `backwards`/`both` are *defined* as applying the
 * first frame through the before phase, and `none`/`forwards` leave the
 * zero-delay elements holding active time 0, which is the first frame again.
 * The fix, and the invariant these checks pin, is structural: **the hero's
 * opacity is 1 in every state, because no rule and no keyframe that reaches
 * the hero ever states otherwise.** Pending, before phase, active, finished,
 * idle, cancelled, reduced motion, a stale bundle, a stylesheet that failed
 * to load — all opacity 1.
 *
 * These are source-text checks over ui/src/App.css. They cannot prove what a
 * browser paints; what they prove is that the stylesheet never asks for an
 * invisible hero.
 *
 *   1. `@keyframes public-arrive` contains no `opacity` token at all, and
 *      still animates something (so an emptied keyframe cannot pass it).
 *   2. No rule targeting the hero hides it — opacity below 1, `visibility:
 *      hidden`/`collapse`, `display: none`, or `content-visibility`. Checked
 *      in every context, media queries included, because a rule that hides
 *      the hero at one viewport width is #1750 at that width.
 *   3. Every keyframe named by an animation on a hero rule is opacity-free —
 *      not just `public-arrive`. This is not a passenger of check 1: adding
 *      `animation: some-other-fade` (whose keyframes fade from 0) to
 *      `.public-hero__copy > *` reddens 3 alone.
 *
 * Each check also asserts that it actually looked at something — that the
 * keyframe exists, that the hero selectors still match rules, that the two
 * known animation users are still there. A rename or a deletion makes this
 * file fail loudly rather than pass over an empty set.
 *
 * Not checked, deliberately: reduced motion. The `@media (prefers-reduced-
 * motion: reduce)` block in App.css is now a motion-preference courtesy, not
 * a visibility guard — with opacity gone from the entrance, deleting it could
 * not reintroduce #1750, so asserting on it here would be a passenger.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const raw = readFileSync(new URL("../src/App.css", import.meta.url), "utf8");

// Blank comments out, preserving length and newlines so offsets and line
// numbers stay true — prose about `opacity` must never read as a declaration.
const css = raw.replace(/\/\*[\s\S]*?\*\//g, (match) =>
	match.replace(/[^\n]/g, " "),
);

const NAME = "public-arrive";

/**
 * Selectors that carry the hero's visible content. The `(?![\w-])` boundary
 * keeps `.public-product-frame` from swallowing `.public-product-frame__bar`
 * (whose chrome legitimately hides a span on narrow screens) while still
 * matching `.public-product-frame img` and `.public-hero__copy > :nth-child`.
 */
const HERO_SELECTORS = [
	".public-hero",
	".public-hero__stage",
	".public-hero__copy",
	".public-product-frame",
	// The headline is styled through `.public-hero h1` today and has no rule of
	// its own, so this one is scanned rather than required — it is here so an
	// id-targeted rule cannot slip a hide past the scan later.
	"#public-hero-title",
];

/** Of those, the ones that must still match a rule or the scan is vacuous. */
const REQUIRED_SELECTORS = HERO_SELECTORS.filter(
	(token) => token !== "#public-hero-title",
);

const lineOf = (index) => css.slice(0, index).split("\n").length;

/** Contents of the block whose opening brace is at `openIndex`. */
function blockAt(openIndex) {
	let depth = 0;
	for (let i = openIndex; i < css.length; i += 1) {
		if (css[i] === "{") depth += 1;
		else if (css[i] === "}") {
			depth -= 1;
			if (depth === 0) return css.slice(openIndex + 1, i);
		}
	}
	throw new Error(`src/App.css: unbalanced braces from line ${lineOf(openIndex)}`);
}

/**
 * Every leaf rule in the sheet, with the at-rules it nests inside. App.css is
 * a flat stylesheet (rules and @media/@keyframes blocks, no CSS nesting), so
 * a leaf block's text is its declaration list.
 */
function leafRules() {
	const rules = [];
	const stack = [];
	let buffer = "";
	for (let i = 0; i < css.length; i += 1) {
		const char = css[i];
		if (char === "{") {
			stack.push({ prelude: buffer.replace(/\s+/g, " ").trim(), at: i });
			buffer = "";
		} else if (char === "}") {
			const frame = stack.pop();
			assert.ok(frame, `src/App.css:${lineOf(i)}: unbalanced closing brace`);
			if (buffer.trim()) {
				rules.push({
					selector: frame.prelude,
					context: stack.map((outer) => outer.prelude).join(" / "),
					declarations: buffer,
					line: lineOf(frame.at),
				});
			}
			buffer = "";
		} else buffer += char;
	}
	assert.equal(stack.length, 0, "src/App.css: unbalanced braces");
	return rules;
}

const rules = leafRules();

const keyframes = new Map(
	[...css.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)].map((match) => [
		match[1],
		{
			body: blockAt(match.index + match[0].length - 1),
			line: lineOf(match.index),
		},
	]),
);

const MISSING =
	`src/App.css: @keyframes ${NAME} is gone. If the hero's entrance was ` +
	`retired, delete this guard in the same change; do not leave it passing ` +
	`over an animation that no longer exists. (#1750)`;

const targetsHero = (selector) =>
	HERO_SELECTORS.some((token) =>
		new RegExp(`${token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?![\\w-])`).test(
			selector,
		),
	);

const heroRules = rules.filter((rule) => targetsHero(rule.selector));

/** Declarations that make an element, or its text, unpaintable. */
const HIDING = [
	{
		pattern: /opacity\s*:\s*(-?[\d.]+%?)/g,
		hides: (value) =>
			Number.parseFloat(value) / (value.endsWith("%") ? 100 : 1) < 1,
		why: "the hero must never be less than fully opaque — that is #1750",
	},
	{
		pattern: /visibility\s*:\s*(hidden|collapse)\b/g,
		hides: () => true,
		why: "a hidden hero is #1750 with a different property",
	},
	{
		pattern: /display\s*:\s*(none)\b/g,
		hides: () => true,
		why: "a hero that is not laid out is a hero nobody reads",
	},
	{
		pattern: /content-visibility\s*:\s*(hidden|auto)\b/g,
		hides: () => true,
		why: "a skipped subtree paints nothing, which is #1750 by another route",
	},
];

test(`@keyframes ${NAME} animates transform only — no opacity anywhere in it`, () => {
	const frame = keyframes.get(NAME);
	assert.ok(frame, MISSING);
	assert.doesNotMatch(
		frame.body,
		/\bopacity\b/,
		`src/App.css:${frame.line}: @keyframes ${NAME} mentions \`opacity\`. The ` +
			`hero has no opacity of its own, so any opacity in this entrance is ` +
			`the only opacity the stylesheet states for the headline, lede, CTAs ` +
			`and screenshot — and a pending or stalled animation holds its first ` +
			`frame forever, under every fill mode. Animate \`transform\` only, so ` +
			`a broken entrance leaves the hero 10px low and readable rather than ` +
			`invisible. (#1750)`,
	);
	assert.match(
		frame.body,
		/\btransform\s*:/,
		`src/App.css:${frame.line}: @keyframes ${NAME} no longer animates ` +
			`\`transform\`, so this check would pass over an empty keyframe. If ` +
			`the entrance changed shape, update this guard deliberately. (#1750)`,
	);
});

test("no rule targeting the hero hides it", () => {
	for (const token of REQUIRED_SELECTORS) {
		assert.ok(
			heroRules.some((rule) => rule.selector.includes(token)),
			`src/App.css: no rule matches \`${token}\` any more. This guard scans ` +
				`the hero by selector; a rename leaves it scanning nothing. Update ` +
				`HERO_SELECTORS in the same change. (#1750)`,
		);
	}

	for (const rule of heroRules) {
		for (const { pattern, hides, why } of HIDING) {
			for (const match of rule.declarations.matchAll(pattern)) {
				assert.ok(
					!hides(match[1]),
					`src/App.css:${rule.line}: \`${rule.selector}\`` +
						(rule.context ? ` (inside ${rule.context})` : "") +
						` declares \`${match[0].replace(/\s+/g, " ")}\` — ${why}. The ` +
						`hero sits on a near-black stage; anything that stops it ` +
						`painting is the black void the issue reports. (#1750)`,
				);
			}
		}
	}
});

test("every animation the hero runs comes from an opacity-free keyframe", () => {
	assert.ok(keyframes.has(NAME), MISSING);

	const animated = heroRules.filter((rule) =>
		/animation(?:-name)?\s*:/.test(rule.declarations),
	);
	const arriveUsers = animated.filter((rule) =>
		new RegExp(`\\b${NAME}\\b`).test(rule.declarations),
	);
	const covered = arriveUsers.map((rule) => rule.selector).join(" ");
	assert.match(
		covered,
		/public-hero__copy/,
		`src/App.css: no hero-copy rule runs ${NAME} any more; this guard would ` +
			`have nothing to check. (#1750)`,
	);
	assert.match(
		covered,
		/public-product-frame/,
		`src/App.css: the product frame no longer runs ${NAME}; this guard would ` +
			`have nothing to check. (#1750)`,
	);

	for (const rule of animated) {
		const named = [
			...rule.declarations.matchAll(/animation(?:-name)?\s*:([^;}]*)/g),
		]
			.flatMap((match) => match[1].split(/[\s,]+/))
			.map((token) => token.trim())
			.filter((token) => keyframes.has(token));

		for (const name of new Set(named)) {
			assert.doesNotMatch(
				keyframes.get(name).body,
				/\bopacity\b/,
				`src/App.css:${rule.line}: \`${rule.selector}\` runs ` +
					`@keyframes ${name} (defined at line ${keyframes.get(name).line}), ` +
					`which animates \`opacity\`. Nothing in the hero declares an ` +
					`opacity of its own, so that keyframe's first frame becomes the ` +
					`hero's appearance for as long as the animation is pending — ` +
					`indefinitely, if its start time never resolves. Animate ` +
					`\`transform\` only. (#1750)`,
			);
		}
	}
});
