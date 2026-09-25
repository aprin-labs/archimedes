// Browser-storage inventory + consent gate (#1647).
//
// ONE SOURCE OF TRUTH. `STORAGE_INVENTORY` below is the only place any
// cookie / localStorage / sessionStorage key is described. Three consumers
// read it and none of them re-states it in prose:
//   1. `canStore()` — the runtime gate every write site in ui/src calls
//      before its own `setItem`;
//   2. `components/StorageDisclosure.jsx` — the policy-page section;
//   3. `components/ConsentBanner.jsx` — the first-visit banner's Customize
//      panel.
// `test/cookie-consent.test.js` enforces the map against the actual source
// in BOTH directions (nothing in the code is missing from the map; nothing
// in the map is absent from the code), so the table cannot drift the way a
// hand-written disclosure does.
//
// CATEGORIES.
//   necessary  — auth, wallet proof, and the OAuth/cross-tab correctness
//                markers those flows depend on. Not offered as a toggle:
//                switching them off would silently break sign-in or
//                wallet-signing, which is worse than not offering the
//                control (#1647 anti-goal 1).
//   functional — preferences and progress markers. Fully rejectable; each
//                entry's `onReject` states the fallback the UI takes.
//   analytics  — the anonymous funnel instrument only.
//   consent    — this module's own record. Exempt by necessity: recording a
//                choice has to precede that choice being enforceable. It is
//                the one key written without asking, and it is disclosed
//                here rather than hidden.
//   legacy     — keys the current code only ever REMOVES. Nothing writes
//                them; they are listed so a reader who finds one left over
//                from an older build knows what it was. `canStore()`
//                returns false for them, so a future write would be
//                suppressed AND caught by the test.
//
// DEFAULT BEFORE A CHOICE IS MADE: optional categories are OFF. An
// undecided visitor is treated exactly like one who pressed Reject, so the
// banner is a real gate rather than a notice shown after the fact.

export const CONSENT_STORAGE_KEY = "archimedes.cookieConsent";

// Bumped only when the OPTIONAL surface changes in a way that invalidates a
// previously recorded choice (a new optional key, or a category split). A
// stored record with a different version is treated as "no choice yet" —
// the banner returns and the optional categories stay off until it is
// answered again.
export const CONSENT_VERSION = 1;

export const NECESSARY = "necessary";
export const FUNCTIONAL = "functional";
export const ANALYTICS = "analytics";
export const CONSENT = "consent";
export const LEGACY = "legacy";

// The only categories a user can switch. Order is the render order.
export const OPTIONAL_CATEGORIES = [FUNCTIONAL, ANALYTICS];

export const CATEGORY_LABELS = {
	[NECESSARY]: "Strictly necessary",
	[FUNCTIONAL]: "Functional",
	[ANALYTICS]: "Analytics",
	[CONSENT]: "Consent record",
	[LEGACY]: "Legacy (cleared, never written)",
};

export const CATEGORY_SUMMARIES = {
	[NECESSARY]:
		"Sign-in, wallet proof, and the markers those flows need to stay correct. These cannot be switched off — without them you cannot sign in or sign a transaction.",
	[FUNCTIONAL]:
		"Preferences and progress markers. Switching these off costs you nothing but convenience; each row below says exactly what happens instead.",
	[ANALYTICS]:
		"One anonymous drop-off instrument. There are no third-party trackers on this site.",
	[CONSENT]:
		"The record of the choice you make here. Written whatever you choose, because a choice that is not stored cannot be honoured on your next visit.",
	[LEGACY]:
		"Written by older builds only. Current code never sets these; it deletes them when it finds them.",
};

// ── The inventory ───────────────────────────────────────────────────────
//
// `name` is the literal key. `prefix: true` means the stored key is `name`
// followed by a per-address/per-vault suffix — `display` shows the shape.
// `source` is the repo-relative file the key literal lives in; the test
// reads that file and fails if the literal is not in it (over-disclosure
// guard). `onReject` is only meaningful for switchable categories.

export const STORAGE_INVENTORY = [
	// ── Cookies (all set server-side, all HttpOnly) ─────────────────────
	{
		name: "better-auth.session_token",
		store: "cookie",
		category: NECESSARY,
		source: "backend/archimedes/api/account_auth.py",
		purpose:
			"Better Auth account session. Issued by the Node auth service on sign-in; 7-day expiry (auth/auth.js session.expiresIn).",
		reveals:
			"An opaque session id. The server maps it to your account row; the page's own JavaScript cannot read it (HttpOnly), and it carries no email or wallet in the value itself.",
		onReject: "Strictly necessary — rejecting it would be signing out.",
	},
	{
		name: "better-auth.state",
		store: "cookie",
		category: NECESSARY,
		source: "docs/account-authentication.md",
		purpose:
			"Double-submit CSRF state for a Google/GitHub sign-in round trip. Library-managed, short-lived (600s), only present while a link/sign-in redirect is in flight.",
		reveals:
			"A random handshake token for one OAuth attempt. Nothing about you.",
		onReject:
			"Strictly necessary — without it a social sign-in cannot be verified as yours.",
	},
	{
		name: "archimedes_session",
		store: "cookie",
		category: NECESSARY,
		source: "backend/archimedes/api/auth_siwe.py",
		purpose:
			"SIWE wallet-proof session issued after you sign the challenge. HttpOnly, Secure, SameSite=strict, 24-hour max-age.",
		reveals:
			"Your wallet address, signed by the server. It is the proof that this browser controls that address.",
		onReject:
			"Strictly necessary — rejecting it would break every wallet-gated action.",
	},
	{
		name: "archimedes_vid",
		store: "cookie",
		category: ANALYTICS,
		source: "backend/archimedes/api/funnel_middleware.py",
		purpose:
			"Anonymous funnel id: 16 random bytes, 180-day max-age, HttpOnly. Exists so drop-off between landing, generating, connecting a wallet and deploying can be counted per browser instead of per request.",
		reveals:
			"A random opaque token — no name, email or address in the value. Honest caveat: it stays anonymous only until you prove a wallet. At SIWE verify the server writes an identity_events row carrying both this id and the wallet (auth_siwe.verify_signature), which links the two from that moment on.",
		onReject:
			"The cookie itself is set by the server on the first response and this page cannot delete it. What rejecting DOES stop is the browser-side reporting: the client stops sending funnel events (App.jsx) and stops writing the archimedes_landed marker.",
	},

	// ── localStorage ────────────────────────────────────────────────────
	{
		name: "archimedes_wallet",
		store: "localStorage",
		category: NECESSARY,
		source: "ui/src/config.js",
		purpose:
			"Which wallet provider you connected and at what address, so a reload reconnects the same wallet rather than dropping you to the picker mid-flow.",
		reveals: "Your wallet address and the provider id you chose.",
		onReject:
			"Strictly necessary — reconnect is part of the signing path (#1647 anti-goal 1).",
	},
	{
		name: "archimedes_wallet_names",
		store: "localStorage",
		category: FUNCTIONAL,
		source: "ui/src/config.js",
		purpose:
			"The display name you gave a wallet at passkey-creation time, keyed by lowercase address.",
		reveals:
			"A label you chose, next to your own address, on your own device.",
		onReject:
			"Not stored. Wallets render as a truncated address (or the backend profile name, if you set one).",
	},
	{
		name: "archimedes.theme",
		store: "localStorage",
		category: FUNCTIONAL,
		source: "ui/src/theme.js",
		purpose: "Light or dark, remembered across reloads.",
		reveals: "The single word 'light' or 'dark'.",
		onReject:
			"Not stored. The toggle still works for the current page; every new load starts on the default dark theme.",
	},
	{
		name: "archimedes_payment_key:",
		display: "archimedes_payment_key:<sca-address>",
		prefix: true,
		store: "localStorage",
		category: NECESSARY,
		source: "ui/src/payment-session.js",
		purpose:
			"The device payment key for a Circle passkey wallet — a locally generated secp256k1 key that signs $2 burn authorizations without a WebAuthn prompt per payment.",
		reveals:
			"A private key, in the clear, bounded to whatever you deposited to it. This is a deliberate, disclosed v1 trade-off documented at the top of payment-session.js — not a preference.",
		onReject:
			"Strictly necessary — it is the payment rail's signing credential (#1647 anti-goal 1).",
	},
	{
		name: "archimedes.onboarding.v1",
		store: "localStorage",
		category: FUNCTIONAL,
		source: "ui/src/components/OnboardingTour.jsx",
		purpose: "That you finished or dismissed the product tour.",
		reveals: "One word: 'completed'.",
		onReject: "Not stored. The tour offers itself again on your next visit.",
	},
	{
		name: "archimedes_circle_credential",
		store: "localStorage",
		category: NECESSARY,
		source: "ui/src/circle-wallet.js",
		purpose:
			"The Circle passkey credential handle used to rehydrate your smart-contract wallet after a reload.",
		reveals:
			"A credential id and public-key material for your passkey wallet. The passkey's private half never leaves the device enclave.",
		onReject:
			"Strictly necessary — without it the passkey wallet cannot be reconnected or used to sign.",
	},
	{
		name: "archimedes_deposit_",
		display: "archimedes_deposit_<vault-address>",
		prefix: true,
		store: "localStorage",
		category: FUNCTIONAL,
		source: "ui/src/components/DepositFlow.jsx",
		purpose:
			"How far a three-step vault deposit got, plus the transaction hashes it produced, so a reload resumes instead of restarting.",
		reveals:
			"A vault address, a step index, and your own on-chain transaction hashes (which are public on Arc anyway).",
		onReject:
			"Not stored. A reload mid-deposit restarts the step list; the on-chain transactions already sent are unaffected.",
	},
	{
		name: "archimedes.welcomeProfileSeen.",
		display: "archimedes.welcomeProfileSeen.<wallet-address>",
		prefix: true,
		store: "localStorage",
		category: FUNCTIONAL,
		source: "ui/src/components/WelcomeProfileModal.jsx",
		purpose:
			"That the optional welcome-profile prompt was answered or skipped for this wallet.",
		reveals: "That a given address has seen one modal. The value is '1'.",
		onReject:
			"Not stored. The (skippable) welcome prompt can appear again for that wallet.",
	},
	{
		name: "archimedes.rigorStrictness",
		store: "localStorage",
		category: FUNCTIONAL,
		source: "ui/src/hooks/useRigorStrictness.js",
		purpose:
			"Your personal deploy-strictness level, 1–5. It never moves the global Archimedes Verified bar, which is always evaluated at the strictest level server-side.",
		reveals:
			"A single digit, 1 to 5 — how strict you asked your own deploy gate to be. Nothing about what you generated or deployed.",
		onReject:
			"Not stored. Every load starts at level 1 — the strictest setting, which is the fail-safe direction.",
	},
	{
		name: CONSENT_STORAGE_KEY,
		store: "localStorage",
		category: CONSENT,
		source: "ui/src/storage-consent.js",
		purpose:
			"The choice you make in the consent banner, plus the version of the disclosure you were shown.",
		reveals:
			"Two booleans and a timestamp. Written whatever you choose — including when you reject everything optional, because otherwise the banner could not remember not to ask again.",
		onReject:
			"Exempt by necessity: consent-recording has to precede consent enforcement.",
	},

	// ── sessionStorage (cleared when the tab closes) ─────────────────────
	{
		name: "archimedes_landed",
		store: "sessionStorage",
		category: ANALYTICS,
		source: "ui/src/App.jsx",
		purpose:
			"A per-tab marker so the anonymous 'landed' funnel event is reported once per session instead of on every route change.",
		reveals: "That this tab already reported one landing. The value is '1'.",
		onReject:
			"Not stored — and the landing event is not reported at all. This is the write the analytics toggle actually controls.",
	},
	{
		name: "archimedes:pending-link",
		store: "sessionStorage",
		category: NECESSARY,
		source: "ui/src/components/AccountSettings.jsx",
		purpose:
			"A one-shot marker naming the provider THIS tab just sent you to link, so a replayed ?linked=… URL cannot fake a success toast.",
		reveals: "A provider name ('google' or 'github') for the length of one redirect.",
		onReject:
			"Strictly necessary — it is an anti-replay check on the account-linking flow.",
	},
	{
		name: "archimedes_circle_credential_tab_seen_id",
		store: "sessionStorage",
		category: NECESSARY,
		source: "ui/src/circle-wallet.js",
		purpose:
			"The passkey credential id this tab last used. localStorage is shared across tabs, so without a tab-scoped stamp this tab would silently rehydrate as a wallet another tab swapped in.",
		reveals: "A credential id, for this tab, until it closes.",
		onReject:
			"Strictly necessary — it is the guard against signing as the wrong wallet.",
	},

	// ── Legacy: current code only removes these ─────────────────────────
	{
		name: "archimedes_circle_username",
		store: "localStorage",
		category: LEGACY,
		source: "ui/src/circle-wallet.js",
		purpose:
			"Older builds kept a per-device passkey username here. Registration now generates a fresh username per wallet and login is discoverable, so nothing writes it; clearCircleSession() deletes it if an old build left one.",
		reveals: "A username string, only if you used a build from before that change.",
		onReject: "Never written by current code.",
	},
	{
		name: "archimedes:currentJobId",
		store: "localStorage",
		category: LEGACY,
		source: "ui/src/components/GenerationStream.jsx",
		purpose:
			"A generation job id kept by an older build's resume path. Current code only removes it when a stream finishes.",
		reveals: "A job id, only if an old build left one.",
		onReject: "Never written by current code.",
	},
];

// ── Lookup ──────────────────────────────────────────────────────────────

// Exact keys first, then the longest matching prefix. Longest-first so a
// future, more specific deposit-prefix entry cannot be shadowed by the
// shorter one it extends.
const EXACT = new Map(
	STORAGE_INVENTORY.filter((e) => !e.prefix).map((e) => [e.name, e]),
);
const PREFIXES = STORAGE_INVENTORY.filter((e) => e.prefix).sort(
	(a, b) => b.name.length - a.name.length,
);

/** The inventory entry describing `key`, or null when the key is undisclosed. */
export function lookupEntry(key) {
	if (typeof key !== "string") return null;
	const exact = EXACT.get(key);
	if (exact) return exact;
	return PREFIXES.find((e) => key.startsWith(e.name)) ?? null;
}

/** Every entry in one category, in inventory order. */
export function entriesInCategory(category) {
	return STORAGE_INVENTORY.filter((e) => e.category === category);
}

/** How a key is spelled for a human (prefix entries show their shape). */
export function displayName(entry) {
	return entry.display ?? entry.name;
}

// ── The recorded choice ─────────────────────────────────────────────────

const listeners = new Set();

function parseStored(raw) {
	if (!raw) return null;
	let parsed;
	try {
		parsed = JSON.parse(raw);
	} catch {
		return null;
	}
	if (!parsed || typeof parsed !== "object") return null;
	// A record written against a different disclosure version is not a
	// choice about THIS surface — treat it as undecided.
	if (parsed.version !== CONSENT_VERSION) return null;
	return {
		version: CONSENT_VERSION,
		[FUNCTIONAL]: parsed[FUNCTIONAL] === true,
		[ANALYTICS]: parsed[ANALYTICS] === true,
		decidedAt: typeof parsed.decidedAt === "string" ? parsed.decidedAt : null,
	};
}

/**
 * The stored choice, or null when the visitor has not answered the banner.
 * Read fresh every call (no cache): a stale copy here would mean writing a
 * key the user just switched off in another tab.
 */
export function readConsent() {
	try {
		return parseStored(localStorage.getItem(CONSENT_STORAGE_KEY));
	} catch {
		// Storage blocked entirely (private mode, extension). Nothing optional
		// can be written anyway, so "undecided" is both true and fail-safe.
		return null;
	}
}

/** True once the visitor has answered the banner. */
export function hasDecided() {
	return readConsent() !== null;
}

/**
 * Is this category allowed to write right now?
 * `necessary` and `consent` are always true; `legacy` always false; the two
 * optional categories follow the stored choice, defaulting to OFF.
 */
export function isCategoryAllowed(category) {
	if (category === NECESSARY || category === CONSENT) return true;
	if (category !== FUNCTIONAL && category !== ANALYTICS) return false;
	return readConsent()?.[category] === true;
}

const warned = new Set();

/**
 * THE GATE. Every `setItem` in ui/src is guarded by this call.
 *
 * Fail-closed on an unknown key on purpose: a storage write that nobody
 * disclosed is exactly the thing this module exists to prevent shipping.
 * test/cookie-consent.test.js turns that runtime no-op into a build-time
 * failure by walking the source for undisclosed keys.
 */
export function canStore(key) {
	const entry = lookupEntry(key);
	if (!entry) {
		if (!warned.has(key)) {
			warned.add(key);
			console.warn(
				`[storage-consent] refusing to store undisclosed key "${key}" — add it to STORAGE_INVENTORY in src/storage-consent.js (#1647).`,
			);
		}
		return false;
	}
	return isCategoryAllowed(entry.category);
}

/**
 * Delete everything currently stored that the present choice does not allow.
 * This is what makes "Reject" retroactive: switching a category off removes
 * the keys it already wrote instead of merely refusing the next write.
 * Legacy keys are swept here too. Necessary and consent keys are never
 * touched.
 */
export function purgeDisallowed() {
	for (const store of ["localStorage", "sessionStorage"]) {
		let api;
		try {
			api = globalThis[store];
			if (!api || typeof api.length !== "number") continue;
		} catch {
			continue;
		}
		const doomed = [];
		try {
			for (let i = 0; i < api.length; i += 1) {
				const key = api.key(i);
				const entry = lookupEntry(key);
				if (!entry || entry.category === NECESSARY || entry.category === CONSENT) {
					continue;
				}
				if (!isCategoryAllowed(entry.category)) doomed.push(key);
			}
			for (const key of doomed) api.removeItem(key);
		} catch {
			// Storage went away mid-sweep; nothing more we can do.
		}
	}
}

/**
 * Record a choice and enforce it immediately. `choice` is
 * `{ functional: boolean, analytics: boolean }`; anything missing is false,
 * so `saveConsent({})` is a full reject.
 */
export function saveConsent(choice = {}) {
	const next = {
		version: CONSENT_VERSION,
		[FUNCTIONAL]: choice[FUNCTIONAL] === true,
		[ANALYTICS]: choice[ANALYTICS] === true,
		decidedAt: new Date().toISOString(),
	};
	try {
		// The consent record is the one key written without asking — see the
		// `consent` category note at the top of this file. canStore() is still
		// called so the write is not a special case in the source: the test
		// that requires every setItem to be gated covers this line too.
		if (canStore(CONSENT_STORAGE_KEY)) {
			localStorage.setItem(CONSENT_STORAGE_KEY, JSON.stringify(next));
		}
	} catch {
		// Storage blocked: the choice holds for this page only. Optional
		// categories stay off on the next load, which is the safe direction.
	}
	purgeDisallowed();
	for (const fn of listeners) fn(readConsent());
	return next;
}

/** Subscribe to consent changes. Returns an unsubscribe function. */
export function subscribeConsent(fn) {
	if (typeof fn !== "function") return () => {};
	listeners.add(fn);
	return () => listeners.delete(fn);
}
