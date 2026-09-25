// Pure TTL-cache primitive backing fetchAdminProbe() (./adminProbe.js).
// Same shape as healthCache.js's getCachedHealth (#1333) / agentStatusCache.js's
// getCachedAgentStatus (#1382 review) — deliberately zero imports so it stays
// unit-testable under a bare `node --test` run.
//
// Why this needs its own shared cache at all: the admin-gate probe
// (GET /api/metrics/private/whoami) is called from TWO independent mount
// points — Layout.jsx (to decide whether the Ops nav item renders) and
// App.jsx (to decide whether /app/insights renders the dashboard or the
// not-found treatment; NOT Insights.jsx — it has no page-gate role, App.jsx
// only ever mounts it once the probe has already resolved `admin === true`)
// — and both can be on-screen from the same navigation. Without sharing one
// cached promise, every /app/insights load fires the probe twice for no
// reason.
//
// A failed fetch is NOT cached — cachedAt/cachedPromise are cleared
// immediately on rejection, so the next call (e.g. App.jsx's own probe
// right after Layout's) gets a fresh attempt instead of being stuck reusing
// a rejected promise for the rest of the TTL window. ./adminProbe.js's
// fetcher turns the AUTHORITATIVE denials (401 anonymous / 403 non-admin)
// into a resolved `{admin: false}` value specifically so THOSE get cached
// here — only genuine network/parse failures reject and skip the cache.

export const ADMIN_PROBE_TTL_MS = 30_000;

let cachedPromise = null;
let cachedAt = 0;

/**
 * @param {() => Promise<{admin: boolean, wallet: string | null}>} fetcher —
 *   performs the actual probe request; injected so this stays a pure,
 *   dependency-free module (production code passes the real bound fetcher
 *   from ./adminProbe.js, tests pass a fake).
 * @param {number} [now] — injectable clock for tests; defaults to `Date.now()`.
 * @returns {Promise<{admin: boolean, wallet: string | null}>} shared across
 *   callers within the TTL window.
 */
export function getCachedAdminProbe(fetcher, now = Date.now()) {
	if (cachedPromise && now - cachedAt < ADMIN_PROBE_TTL_MS) {
		return cachedPromise;
	}
	cachedAt = now;
	cachedPromise = fetcher().catch((err) => {
		cachedAt = 0;
		cachedPromise = null;
		throw err;
	});
	return cachedPromise;
}

// Called on sign-out / wallet change (AuthenticatedApp.jsx's existing
// "wallet-changed" event, and AuthContext on sign-out) so a stale admin
// determination from a previous session/wallet can never outlive it — an
// admin's nav item must not survive into a non-admin's session just because
// it's still inside the TTL window.
export function _resetAdminProbeCache() {
	cachedPromise = null;
	cachedAt = 0;
}
