import assert from "node:assert/strict";
import test from "node:test";

// theme.js is a plain .js module (no JSX) — same shape as
// wallet-providers.test.js: import the real module and assert behaviour.
// No DOM, no new deps. node --test runs outside a browser, so `localStorage`
// and `document` don't exist by default; we stub the two calls theme.js
// touches directly on globalThis, per the issue's precedent.
//
// Every assertion here was confirmed to FAIL against the pre-fix
// ui/src/theme.js (`git show c0e6400f:ui/src/theme.js`) — see the PR body
// for the transcript (CLAUDE.md § "A guard must be shown to reject
// something").

let setAttributeCalls = [];
globalThis.document = {
	documentElement: {
		setAttribute: (...args) => setAttributeCalls.push(args),
	},
};

const { getStoredTheme, applyTheme } = await import("../src/theme.js");

test.beforeEach(() => {
	setAttributeCalls = [];
});

// (a) a storage-blocked visitor must not get a blank page.
test("getStoredTheme falls back to dark, without throwing, when localStorage.getItem throws", () => {
	globalThis.localStorage = {
		getItem: () => {
			throw new Error("blocked");
		},
	};
	let result;
	assert.doesNotThrow(() => {
		result = getStoredTheme();
	});
	assert.equal(result, "dark");
});

// (b) the DOM update must survive a blocked persist, and the caller's
// setState (guarded by the caller not throwing) must be reachable.
test("applyTheme does not throw when localStorage.setItem throws, and still applies the DOM attribute", () => {
	globalThis.localStorage = {
		setItem: () => {
			throw new Error("blocked");
		},
	};
	assert.doesNotThrow(() => applyTheme("light"));
	assert.deepEqual(setAttributeCalls, [["data-theme", "light"]]);
});

// (c) the normal (unblocked) path is unchanged — once functional storage has
// actually been consented to. #1647 made `archimedes.theme` a functional-
// category key: applyTheme now asks canStore() first, and an undecided or
// rejecting visitor gets the theme applied to the page WITHOUT it being
// persisted. Seeding the consent record here is what makes this the
// "unblocked path" it claims to be; the suppressed path is covered in
// test/cookie-consent.test.js.
test("the normal path still round-trips 'light' through localStorage", () => {
	const store = {
		"archimedes.cookieConsent": JSON.stringify({
			version: 1,
			functional: true,
			analytics: true,
		}),
	};
	globalThis.localStorage = {
		setItem: (key, value) => {
			store[key] = value;
		},
		getItem: (key) => (key in store ? store[key] : null),
	};
	applyTheme("light");
	assert.equal(store["archimedes.theme"], "light");
	assert.deepEqual(setAttributeCalls, [["data-theme", "light"]]);
	assert.equal(getStoredTheme(), "light");
});

test("getStoredTheme defaults to dark for any stored value other than 'light'", () => {
	globalThis.localStorage = { getItem: () => "sepia" };
	assert.equal(getStoredTheme(), "dark");
	globalThis.localStorage = { getItem: () => null };
	assert.equal(getStoredTheme(), "dark");
});
