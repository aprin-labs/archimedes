// Admin-gate probe — the frontend half of the server-truth gate on
// /app/insights (owner directive 2026-08-20, supersedes issue #1028 D8
// "public Insights page"). GET /api/metrics/private/whoami is the ONLY
// thing that decides "is this visitor an admin" — this module never guesses
// from local state (a linked-wallet address, a signed-in user id) because
// none of those imply PLATFORM_ADMIN_WALLETS membership, which only the
// server can check.
//
// Two independent mount points share this probe within one TTL window
// (Layout.jsx's Ops nav item, App.jsx's /app/insights page gate — NOT
// Insights.jsx, which never imports this module: by the time Insights.jsx
// mounts, App.jsx has already resolved `admin === true`) via
// ./adminProbeCache.js — see that file for why.
//
// Scope of "does not advertise the page exists" (round 2 correction;
// extended 2026-08-30 for the nginx vector). That property holds for three
// specific vectors:
//   1. The denial screen — identical NotFound, never an "admin access
//      required" message.
//   2. The in-app anonymous redirect — a client-side navigation onto
//      /app/insights renders NotFound rather than bouncing to
//      /sign-in?next=/app/insights.
//   3. The pre-auth edge response. `nginx/nginx.conf`'s `location =
//      /app/insights` sits behind the same `auth_request /_auth_session`
//      boundary as the catch-all `^~ /app`, so an anonymous GET for this
//      path returns the identical 302 nginx returns for an anonymous GET of
//      any UNKNOWN /app path. An earlier revision served the shell here
//      ungated, which made `/app/insights -> 200` vs `/app/library -> 302` a
//      readable existence oracle before any client JS ran; that carve-out is
//      gone. This note claimed the property before the config delivered it —
//      keep the two in lockstep (guarded by
//      backend/tests/test_local_setup_contract.py::
//      test_nginx_gates_insights_exactly_like_every_other_app_path).
// It does NOT hide this endpoint's existence
// from someone reading network traffic or the shipped bundle: Layout.jsx
// calls this probe for every signed-in user on every page (so a non-admin's
// devtools Network tab shows a 403 on `/api/metrics/private/whoami` on
// first render of any app page), and `/app/insights` plus the `Insights`
// component ship in the same statically-imported main chunk as every other
// app page (ui/src/routes.js, ui/src/AuthenticatedApp.jsx) rather than
// behind a lazy import gated on a successful probe. Closing those two would
// need probing only once the Ops nav is opened (or only on the insights
// route) and `lazy(() => import('./components/Insights'))` — not done here;
// what ships today is "consistent denial UX", not "the page cannot be
// discovered by an attentive visitor."
import { apiGet } from "./api";
import { getCachedAdminProbe, _resetAdminProbeCache } from "./adminProbeCache.js";

async function _fetchWhoami() {
	try {
		const body = await apiGet("/api/metrics/private/whoami");
		return { admin: body?.admin === true, wallet: body?.wallet ?? null };
	} catch (err) {
		// An AUTHORITATIVE answer from the server (401 anonymous / 403
		// verified-non-admin) resolves to a real, cacheable "not admin" —
		// apiGet sets `.status` on every non-2xx HTTP response, so this
		// branch only fires once a response was actually received.
		if (err && typeof err.status === "number") {
			return { admin: false, wallet: null };
		}
		// A genuine network/parse failure (no `.status`) is NOT an answer —
		// rethrow so adminProbeCache does not cache it, and the next probe
		// gets a fresh attempt rather than being stuck "not admin" for the
		// rest of the TTL window on a transient blip.
		throw err;
	}
}

/**
 * Resolves to `{admin: boolean, wallet: string | null}`. Never rejects for
 * an anonymous/non-admin caller — that resolves to `{admin: false, wallet:
 * null}`, same as a genuine network failure the caller should also treat as
 * "not admin" (fail closed, matching the not-found treatment this gates).
 */
export function fetchAdminProbe() {
	return getCachedAdminProbe(_fetchWhoami).catch(() => ({ admin: false, wallet: null }));
}

export { _resetAdminProbeCache };
