// The sidebar's real nav data — extracted out of Layout.jsx (round 4 review
// finding) so it can be imported by BOTH the rendering code and its own
// tests, instead of tests re-declaring a hand-built stand-in array. A
// hand-built `[{id: "insights"}, {id: "account"}]` fixture in a test proves
// filterInsightsNavItem() works against SOME array shaped like Layout's — it
// does not prove it works against the actual "Ops" group Layout renders, and
// the two can silently drift (e.g. a future item added to "Ops" with an id
// that happens to collide, or the group renamed) without the test noticing.
// Deliberately plain JS, zero imports, zero JSX — matching
// adminProbeCache.js / insightsGate.js's "stays unit-testable under bare
// `node --test`" discipline.
//
// EVERY entry belongs to a labelled group (#1641). The array used to open
// with an unlabelled section (its `group` key set to `null`) holding a single
// marketing-site link. Layout.jsx renders that section without a
// `.nav-group-label`, so it was a bare, header-less button sitting above the
// labelled bands with no section context — the owner's "looks terrible"
// finding in the 2026-08-31 product review. The marketing site itself is
// untouched by its removal: `/` still maps to the `landing` page (routes.js
// PUBLIC_PATHS) and is still reached from PublicLayout's own nav and by URL —
// only the in-shell link to it was removed, and with it the sole reason this
// file ever had an unlabelled group. Adding one back re-creates the defect;
// the guards in ui/test/nav-groups.test.js reject it.
//
// The four labelled groups split the /app surfaces by where the user is in
// the spine (owner's grouping call, 2026-08-31 — this supersedes the earlier
// DISCOVER/STRATEGY/POSITION split, which banded by wallet-gating instead):
//   STRATEGY — finding and building one: browse the seed strategies
//     (Explore), see the paper substrate they are drawn from (Corpus),
//     generate your own, keep them (Library). That order is the onboarding
//     read; Explore and Corpus came from the dissolved DISCOVER group.
//   POSITION — acting on one and reviewing the result: Paper Trading (the
//     act-on step of the MVP spine — simulated, account-owned, free),
//     Reasoning (the trace behind a verdict), Leaderboard (how the library
//     ranks). Portfolio and Learnings are ROADMAP_PAGES, hidden by default
//     (#1266); Quant Lab defaults off separately via the backend `quant`
//     feature flag. Those three trail the shipped three so the rendered
//     order is the owner's spec whatever the flags say — their VISIBILITY is
//     unchanged and stays owned by routes.js featureEnabled(), not by their
//     position here.
//   MARKET — the strategy marketplace (ROADMAP_PAGES, hidden by default)
//   OPS — insights + account
//
// Grouping is no longer a gating signal. Which items an anonymous visitor
// sees is ANON_NAV_IDS' job (routes.js) and always was; DISCOVER's "open to
// anonymous visitors" label merely happened to agree with it, and reading a
// group name as a permission boundary is the drift this note exists to stop.
//
// Architecture is deliberately NOT a shell nav item (#1370, PR #1400): the
// `architecture` page id resolves to the PUBLIC `/architecture` route
// (routes.js PUBLIC_PATHS, not APP_PATHS), so clicking it from inside the
// shell rendered the page in PublicLayout — no sidebar, no breadcrumbs. It
// stays reachable from PublicLayout's own nav and by direct URL; #1370's
// anti-goal forbids fixing it by minting a second, /app-side route. Two
// tests reject a re-add: nav-groups.test.js by id, and
// backend/tests/test_breadcrumbs.py::test_shell_nav_items_stay_inside_the_shell
// by "every nav id must be an APP_PATHS page".
export const NAV = [
	{
		group: "Strategy",
		items: [
			{ id: "explore", label: "Explore", icon: "i-lucide-compass" },
			{ id: "corpus", label: "Corpus", icon: "i-lucide-library" },
			{ id: "generate", label: "Generate", icon: "i-lucide-sparkles" },
			{ id: "library", label: "Library", icon: "i-lucide-line-chart" },
		],
	},
	{
		group: "Position",
		items: [
			{ id: "paper", label: "Paper Trading", icon: "i-lucide-trending-up" },
			{ id: "reasoning", label: "Reasoning", icon: "i-lucide-brain" },
			// Leaderboard moved out of STRATEGY (#1641, superseding #1077's
			// placement): it ranks strategies by realised outcome, which is a
			// review-the-result surface, not a build-one surface.
			{ id: "leaderboard", label: "Leaderboard", icon: "i-lucide-trophy" },
			// Flag-gated, hidden in the shipped build — see the header note.
			{
				id: "portfolio",
				label: "Portfolio",
				icon: "i-lucide-layout-dashboard",
			},
			{ id: "quant", label: "Quant Lab", icon: "i-lucide-flask-conical" },
			{ id: "learnings", label: "Learnings", icon: "i-lucide-graduation-cap" },
		],
	},
	{
		group: "Market",
		items: [
			{
				id: "marketplace",
				label: "Marketplace",
				icon: "i-lucide-shopping-bag",
			},
			{ id: "publish", label: "Publish", icon: "i-lucide-megaphone" },
			{ id: "subscriptions", label: "Subscriptions", icon: "i-lucide-bell" },
		],
	},
	{
		group: "Ops",
		items: [
			{ id: "insights", label: "Insights", icon: "i-lucide-bar-chart-3" },
			{ id: "account", label: "Account", icon: "i-lucide-user-round-cog" },
		],
	},
];
