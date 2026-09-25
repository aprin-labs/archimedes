// Pure decision logic for the /app/insights admin-only gate (owner directive
// 2026-08-20, supersedes #1028 D8) — extracted out of App.jsx/Layout.jsx so
// the decisions the gate actually makes are unit-testable with real inputs
// under bare `node --test`, not merely asserted on source text.
//
// Round-3 review finding: every prior test on this gate was a readFileSync +
// regex match against App.jsx/Layout.jsx's source. None of them executed the
// line that actually consumes a probe result — `setIsInsightsAdmin(admin)`
// at Layout.jsx, `setInsightsAdmin(admin)` at App.jsx — so a mutation
// collapsing either call to `setIsInsightsAdmin(true)` left every one of
// those regexes matching while every signed-in non-admin got the Ops nav
// item and the live dashboard. Routing every consumer of a probe result
// through resolveInsightsAdminState() below, and every render/nav decision
// through isInsightsPageBlocked()/filterInsightsNavItem(), means a mutation
// at any of those three points now has a real behavioral test to fail.
//
// Deliberately zero imports, matching adminProbeCache.js's own "stays
// unit-testable" discipline.

/**
 * Reduces a raw fetchAdminProbe() result to the boolean the gate state
 * (`insightsAdmin` / `isInsightsAdmin`) should hold. This is the ONLY place
 * either caller should read `.admin` off a probe result — inlining
 * `setIsInsightsAdmin(admin)` at the call site is exactly the pattern that
 * silently degrades to `setIsInsightsAdmin(true)` under a typo/mutation with
 * no observable difference until a non-admin wallet loads the page.
 * @param {{admin: boolean, wallet: string|null}} probeResult
 * @returns {boolean}
 */
export function resolveInsightsAdminState(probeResult) {
	return probeResult?.admin === true;
}

/**
 * Should /app/insights render the not-found treatment instead of the
 * dashboard (and should the tab title read as not-found rather than
 * "Insights · Archimedes")? True whenever the gate has not affirmatively
 * resolved `admin === true` — this covers an authoritative denial (`false`)
 * AND an unresolved probe (`null`, still in flight) alike.
 *
 * Round-3 fix: the unresolved case used to render a neutral "Loading…"
 * screen and title the tab "Insights · Archimedes" — both of which a
 * genuinely unknown route never produces on first paint, a real disclosure
 * vector the in-file "does not advertise the page exists" claim did not
 * account for. Treating "unresolved" the same as "denied" here, for both the
 * title and the render branch, closes it: a many-tabs user (or anyone
 * inspecting first paint) can no longer distinguish "probing" from "denied"
 * from "truly unknown route".
 *
 * Returns `false` for any route other than `insights` — nothing to block.
 * @param {string|null} routePage
 * @param {boolean|null} insightsAdmin
 * @returns {boolean}
 */
export function isInsightsPageBlocked(routePage, insightsAdmin) {
	return routePage === "insights" && insightsAdmin !== true;
}

/**
 * Which of the three treatments /app/insights should render right now
 * (#1648 / I-8 B2). Splits the single boolean `isInsightsPageBlocked`
 * collapses into the two cases that genuinely differ:
 *
 *   `"allow"`      — the server answered `admin === true`; render the page.
 *   `"resolving"`  — no answer yet (`null`) AND there is a session in play;
 *                    render a quiet neutral holding state, never a decision.
 *   `"not-found"`  — an authoritative denial (`false`), or no answer yet with
 *                    no session in play; render the not-found treatment.
 *
 * Why the `hasSession` input, rather than simply rendering the holding state
 * for every `null`: the round-3 note on `isInsightsPageBlocked` is about a
 * real vector — a genuinely unknown route never shows a loading state, so a
 * loader on this path is a signal that the path is special. That signal only
 * matters where the observer is *outside* the session boundary. Anonymous
 * callers therefore keep the round-3 behaviour exactly (`null` → not-found,
 * indistinguishable from an unknown route on first paint), which is also the
 * only case nginx's pre-auth gate can be probed alongside.
 *
 * Inside an authenticated session the trade goes the other way, on the owner's
 * call (#1648, I-8 B2): rendering "this page does not exist" to an admin on
 * every entry, for as long as a round trip takes, is a visible NotFound →
 * dashboard flip on a page they are allowed to use. The residual, stated
 * honestly rather than papered over: a signed-in NON-admin's FIRST visit of a
 * session sees a brief neutral holding state where an unknown route would show
 * not-found immediately. That window closes after one answer —
 * insightsAdminMemo.js remembers the denial too, so every later visit in that
 * session renders not-found with no holding state at all — and the tab title
 * still reads "Page not found" throughout (App.jsx keeps titling off
 * isInsightsPageBlocked, whose `null` branch is unchanged).
 *
 * `hasSession` is deliberately "a session may exist" (auth still loading OR a
 * user is present), not "a user is present": on a hard load of /app/insights
 * the auth check has not resolved yet, and treating that instant as anonymous
 * would reintroduce the exact flash this function exists to remove for the one
 * case — a full page load — where it is most visible.
 *
 * @param {string|null} routePage
 * @param {boolean|null} insightsAdmin
 * @param {boolean} hasSession
 * @returns {"allow"|"resolving"|"not-found"}
 */
export function resolveInsightsView(routePage, insightsAdmin, hasSession) {
	if (routePage !== "insights") return "allow";
	if (insightsAdmin === true) return "allow";
	if (insightsAdmin === null && hasSession === true) return "resolving";
	return "not-found";
}

/**
 * Filters the Ops nav group's items down to the ones a visitor with the
 * given admin-gate state should see — i.e. drops the `insights` item unless
 * the probe has affirmatively resolved `true`. Every other item passes
 * through unchanged.
 * @param {Array<{id: string}>} items
 * @param {boolean} isAdmin
 * @returns {Array<{id: string}>}
 */
export function filterInsightsNavItem(items, isAdmin) {
	return items.filter((item) => item.id !== "insights" || isAdmin === true);
}
