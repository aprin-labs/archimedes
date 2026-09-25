import { canStore } from "./storage-consent.js";

const STORAGE_KEY = "archimedes.theme";

// localStorage can throw (Safari ITP in an embedded/third-party context,
// Chrome "block all cookies", an enterprise policy, a privacy extension).
// getStoredTheme runs as the lazy useState initializer on the render path of
// every /app page — including the anonymous-OK front doors (#1357) — so an
// uncaught throw here unmounts the whole React root. Fail safe to the
// default theme instead. Deliberately NO system-preference media-query
// fallback: it would run unguarded on that same render path (the exact #1357
// failure class), and the product default is dark by design — the test
// "defaults to dark for any stored value other than 'light'" pins this.
export function getStoredTheme() {
	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		return stored === "light" ? "light" : "dark";
	} catch {
		return "dark";
	}
}

export function applyTheme(theme) {
	const next = theme === "dark" ? "dark" : "light";
	// Storage failure must not prevent current-page theme updates.
	document.documentElement.setAttribute("data-theme", next);
	try {
		// Functional category (#1647): with functional storage rejected — or
		// simply not yet consented to — the theme applies to this page and is
		// deliberately NOT persisted. getStoredTheme's dark default is the
		// fallback, which is why rejecting costs a reload's memory, not the
		// toggle itself.
		if (canStore(STORAGE_KEY)) localStorage.setItem(STORAGE_KEY, next);
	} catch {
		// Non-fatal: theme will not persist across reloads.
	}
}
