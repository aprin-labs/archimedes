import assert from "node:assert/strict";
import test from "node:test";

import { NAV } from "../src/navConfig.js";
import { pageToPath, resolveRoute, visibleNavigation } from "../src/routes.js";

// ── #1641: the sidebar's group structure, asserted against the REAL NAV
// array rather than a grep. The issue's acceptance criteria are greps over
// navConfig.js source (`grep -n "group: null"`, `grep -n "'architecture'"`);
// those pass or fail on quote style and comment prose, which is not what any
// of them actually mean. This file asserts the same properties structurally,
// against the parsed array Layout.jsx renders — following the precedent the
// admin-probe suite set when it stopped hand-building a stand-in "Ops" group
// and imported NAV itself.
//
// Every assertion below is a regression guard for a specific defect the owner
// named in the 2026-08-31 product review, or for an anti-goal of #1641. None
// of them assert cosmetics for their own sake.

const groupLabels = () => NAV.map((g) => g.group);
const idsIn = (label) => {
	const group = NAV.find((g) => g.group === label);
	assert.ok(group, `expected a "${label}" group in NAV`);
	return group.items.map((i) => i.id);
};

test("#1641: every NAV section is labelled — no header-less top item", () => {
	// Layout.jsx renders `.nav-group-label` only `{group.group && ...}`, so a
	// falsy group label produces a bare button floating above the labelled
	// bands with no section context. That was the "looks terrible" entry.
	const unlabelled = groupLabels().filter((label) => !label);
	assert.deepEqual(
		unlabelled,
		[],
		"a NAV section has no group label — Layout.jsx renders it as a header-less band (#1641)",
	);
});

test("#1641: the marketing-site link is gone from the sidebar, but the marketing site is still a page", () => {
	const allIds = NAV.flatMap((g) => g.items.map((i) => i.id));
	assert.ok(
		!allIds.includes("landing"),
		"the marketing-site nav entry is back in NAV — it is the header-less item #1641 removed",
	);
	// Anti-goal guard: #1641 removed the sidebar's LINK to the marketing
	// site, not the marketing site. Both halves of the route are checked —
	// `/` still resolves to the public landing page, and the page still
	// resolves back to `/` — because deleting either one alone would leave
	// the other looking intact.
	assert.equal(
		pageToPath("landing"),
		"/",
		"the landing page's route disappeared — #1641 removed only the in-shell link to it",
	);
	const root = resolveRoute("/");
	assert.equal(root.kind, "public");
	assert.equal(
		root.page,
		"landing",
		"`/` no longer resolves to the landing page — the marketing site was an #1641 anti-goal",
	);
	// ...and 'landing' is no longer an anonymous NAV id, because it is no
	// longer a NAV id at all. visibleNavigation is the consumer of that set,
	// so ask it rather than reaching into the private Set.
	assert.deepEqual(
		visibleNavigation([{ id: "landing" }], { quant: true }, null),
		[],
		"'landing' is still in ANON_NAV_IDS — dead config now that no nav item carries that id (#1641)",
	);
});

test("#1641 / #1370: Architecture is never a shell nav item", () => {
	// Regression guard, not new work: `pageToPath('architecture')` resolves to
	// the PUBLIC /architecture route, so a shell nav item pointing at it
	// unmounts the whole authenticated shell (#1370 item 4, fixed by #1400).
	// backend/tests/test_breadcrumbs.py enforces the general rule ("every nav
	// id is an APP_PATHS page"); this names the specific offender, so the
	// failure message says what went wrong instead of just which id is bad.
	const allIds = NAV.flatMap((g) => g.items.map((i) => i.id));
	assert.ok(
		!allIds.includes("architecture"),
		"Architecture is back in the shell nav — clicking it unmounts the sidebar (#1370 item 4)",
	);
	// ...and it is still reachable, publicly. "Not in the shell nav" must not
	// drift into "gone": #1370's anti-goal forbade fixing the unmount by
	// minting an /app-side route, which means the public one is the only one.
	const arch = resolveRoute("/architecture");
	assert.equal(
		arch.kind,
		"public",
		"/architecture is no longer a public route — it is public-only by design (#1370/#1400)",
	);
	assert.equal(arch.page, "architecture");
	assert.equal(pageToPath("architecture"), "/architecture");
});

test("#1641: STRATEGY is the find-and-build band, in onboarding order", () => {
	assert.deepEqual(idsIn("Strategy"), [
		"explore",
		"corpus",
		"generate",
		"library",
	]);
	assert.ok(
		!groupLabels().includes("Discover"),
		"the 'Discover' group is back — #1641 dissolved it into 'Strategy'",
	);
});

test("#1641: POSITION is the act-and-review band; paper, reasoning, leaderboard in that order", () => {
	const position = idsIn("Position");
	// Order is asserted over the three shipped items only. portfolio, quant
	// and learnings are flag-gated (ROADMAP_PAGES / the backend `quant` flag)
	// and #1641 leaves their gating alone, so pinning their exact slots would
	// make this test about something it is not policing.
	const spine = position.filter((id) =>
		["paper", "reasoning", "leaderboard"].includes(id),
	);
	assert.deepEqual(spine, ["paper", "reasoning", "leaderboard"]);
});

test("#1641: no page id appears in two sidebar groups", () => {
	// The regroup moved four ids between groups by hand; a copy-paste that
	// left one behind renders the same nav button under two headers.
	const allIds = NAV.flatMap((g) => g.items.map((i) => i.id));
	const dupes = allIds.filter((id, i) => allIds.indexOf(id) !== i);
	assert.deepEqual(dupes, [], `page ids listed under more than one group: ${dupes}`);
});
