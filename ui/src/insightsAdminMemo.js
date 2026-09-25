// Per-session memo of the LAST SERVER ANSWER the admin gate received, keyed
// on the identity that answer was given for (#1648 / I-8 B2).
//
// Why this exists, and why it is not the same thing as adminProbeCache.js:
// that module caches an in-flight/just-resolved probe PROMISE for 30 s so two
// mount points share one request. It says nothing about what to RENDER while a
// fresh probe is in flight after that window closes — and App.jsx's gate reset
// `insightsAdmin` to `null` on every entry to /app/insights and on every
// `wallet-changed` event, so an admin re-entering the page (or simply
// refocusing the tab, which makes an injected provider re-announce the SAME
// account) watched the page flip to the not-found treatment and back. This
// memo is what lets the second and later visits render the answer the server
// already gave, instead of re-deciding from scratch.
//
// The honesty constraint (owner directive, #1648): this is a memo of a real
// server answer, NEVER an optimistic assumption. Two properties enforce that:
//
//   1. It is keyed on the identity the answer was resolved under — account id
//      AND connected wallet address. `require_platform_admin` can give a
//      genuinely different answer per wallet on the same account, so a memo
//      recorded under wallet A must not be readable after a swap to wallet B.
//      A key miss returns `null` ("unknown"), never a stale `true`.
//   2. Anonymous callers get a `null` key (see adminIdentityKey) and are
//      therefore never memoized and never able to read a memo — a signed-out
//      browser cannot inherit the previous session's determination.
//
// Both `true` and `false` are memoized. Remembering the denial matters as much
// as remembering the grant: it is what lets a non-admin's second visit render
// the not-found treatment instantly, exactly like a genuinely unknown route,
// with no resolving state in between.
//
// Deliberately zero imports, matching adminProbeCache.js / insightsGate.js's
// "stays unit-testable under a bare `node --test`" discipline.

let memoKey = null;
let memoAdmin = null;

/**
 * Canonical form of a connected-wallet address for identity purposes: lowercase
 * or `null`. Exported (rather than inlined at the two App.jsx call sites) so
 * the property that actually stops the tab-focus re-probe is testable: an
 * injected provider re-announcing the SAME account — possibly checksummed on
 * one announcement and lowercase on another — must map to the SAME value, so
 * React's setState bail-out fires and the probe effect does not re-run.
 * @param {string|null|undefined} address
 * @returns {string|null}
 */
export function normalizeWalletAddress(address) {
	return typeof address === "string" && address ? address.toLowerCase() : null;
}

/**
 * Builds the identity key an admin answer is recorded under. Returns `null`
 * for an anonymous caller — a null key is never written and never matches on
 * read, so signed-out browsers are excluded from the memo by construction
 * rather than by a cleanup call somebody has to remember to make.
 * @param {string|null|undefined} userId — the authenticated account id.
 * @param {string|null|undefined} walletAddress — the currently connected
 *   wallet, lowercased here so a checksummed/lowercase announcement of the
 *   same account is the same key (an `accountsChanged` re-announcement on tab
 *   focus must not read as a different identity).
 * @returns {string|null}
 */
export function adminIdentityKey(userId, walletAddress) {
	if (!userId) return null;
	return `${userId}|${(walletAddress ?? "").toLowerCase()}`;
}

/**
 * Records a server answer against the identity it was given for. A `null`
 * identity (anonymous) is dropped.
 * @param {string|null} identity
 * @param {boolean} admin
 */
export function rememberInsightsAdmin(identity, admin) {
	if (!identity) return;
	memoKey = identity;
	memoAdmin = admin === true;
}

/**
 * Reads back the memoized answer for `identity`, or `null` ("no answer for
 * this identity — the gate must resolve it from the server") when the key
 * does not match what was last recorded, or the caller is anonymous.
 * @param {string|null} identity
 * @returns {boolean|null}
 */
export function readInsightsAdmin(identity) {
	if (!identity || identity !== memoKey) return null;
	return memoAdmin;
}

/** Test/sign-out hook: forgets the memoized answer entirely. */
export function _resetInsightsAdminMemo() {
	memoKey = null;
	memoAdmin = null;
}
