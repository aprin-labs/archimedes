import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

import * as theme from "../src/theme.js";
import { saveConsent } from "../src/storage-consent.js";

// Browser boundaries only: exercise the real theme/consent code, including
// first paint with the React bundle absent. No credentials or DOM dependency.
function browser({
	stored = null,
	consent = true,
	systemDark = false,
	blockedRead = false,
	blockedWrite = false,
	blockedMedia = false,
} = {}) {
	const values = new Map();
	if (consent !== null)
		values.set(
			"archimedes.cookieConsent",
			JSON.stringify({ version: 1, functional: consent, analytics: false }),
		);
	if (stored !== null) values.set("archimedes.theme", stored);
	const localStorage = {
		get length() {
			return values.size;
		},
		key: (index) => [...values.keys()][index] ?? null,
		getItem(key) {
			if (blockedRead) throw new Error("blocked");
			return values.get(key) ?? null;
		},
		setItem(key, value) {
			if (blockedWrite) throw new Error("blocked");
			values.set(key, value);
		},
		removeItem: (key) => values.delete(key),
	};
	const root = {
		dataset: {},
		style: {},
		setAttribute(name, value) {
			this.dataset[name === "data-theme" ? "theme" : "appearance"] = value;
		},
	};
	const metadata = {
		content: "",
		setAttribute(name, value) {
			this[name] = value;
		},
	};
	const listeners = new Set();
	const media = {
		matches: systemDark,
		addEventListener: (_event, callback) => listeners.add(callback),
		removeEventListener: (_event, callback) => listeners.delete(callback),
	};
	let mediaReads = 0;
	return {
		values,
		root,
		metadata,
		media,
		listeners,
		get mediaReads() {
			return mediaReads;
		},
		localStorage,
		document: { documentElement: root, querySelector: () => metadata },
		getComputedStyle: () => ({
			getPropertyValue: () =>
				root.dataset.theme === "dark" ? "#0d1917" : "#f2f1e8",
		}),
		window: {
			matchMedia() {
				mediaReads++;
				if (blockedMedia) throw new Error("blocked");
				return media;
			},
		},
	};
}

function mount(options) {
	const env = browser(options);
	for (const key of ["document", "localStorage", "window", "getComputedStyle"])
		globalThis[key] = env[key];
	return env;
}

test("blocked storage still follows System without throwing", () => {
	for (const systemDark of [true, false]) {
		mount({ blockedRead: true, systemDark });
		assert.equal(theme.getStoredTheme(), "system");
		assert.equal(
			theme.renderTheme(theme.getStoredTheme()),
			systemDark ? "dark" : "light",
		);
	}
});

test("invalid or missing choice defaults to System without querying OS in the storage reader", () => {
	for (const stored of [null, "sepia", "", "LIGHT"]) {
		const env = mount({ stored });
		assert.equal(theme.getStoredTheme(), "system");
		assert.equal(env.mediaReads, 0);
	}
});

test("unselected appearance resolves System without saving an override", () => {
	for (const choice of [undefined, null, "sepia", "system"]) {
		for (const systemDark of [true, false]) {
			const env = mount({ systemDark });
			assert.equal(theme.normalizeTheme(choice), "system");
			assert.equal(theme.renderTheme(choice), systemDark ? "dark" : "light");
			assert.equal(env.root.dataset.appearance, "system");
			assert.equal(env.values.has("archimedes.theme"), false);
		}
	}
});

test("stored choices include System, but never bypass functional consent", () => {
	for (const stored of ["light", "dark", "system"]) {
		mount({ stored });
		assert.equal(theme.getStoredTheme(), stored);
		for (const consent of [false, null]) {
			mount({ stored, consent });
			assert.equal(theme.getStoredTheme(), "system");
		}
	}
});

test("applyTheme updates current page even when writing is blocked", () => {
	const env = mount({ blockedWrite: true });
	assert.equal(theme.applyTheme("light"), false);
	assert.equal(env.root.dataset.theme, "light");
	assert.equal(env.values.has("archimedes.theme"), false);
});

test("all choices round-trip only when consent permits", () => {
	for (const choice of ["light", "dark", "system"]) {
		for (const consent of [true, false, null]) {
			const env = mount({ consent });
			assert.equal(theme.applyTheme(choice), consent === true);
			assert.equal(
				env.values.get("archimedes.theme"),
				consent === true ? choice : undefined,
			);
			assert.equal(env.root.dataset.appearance, choice);
		}
	}
});

test("System stores choice, not resolution; native controls and metadata agree", () => {
	const env = mount({ systemDark: false });
	theme.applyTheme("system");
	assert.equal(env.root.dataset.appearance, "system");
	assert.equal(env.root.dataset.theme, "light");
	assert.equal(env.root.style.colorScheme, "light");
	assert.equal(env.metadata.content, "#f2f1e8");
	assert.equal(theme.getStoredTheme(), "system");
});

test("invalid choices and failed media queries resolve dark, never light by coercion", () => {
	for (const choice of ["sepia", undefined, "system"]) {
		const env = mount({ blockedMedia: true });
		theme.applyTheme(choice);
		assert.equal(env.root.dataset.theme, "dark");
		assert.equal(env.root.style.colorScheme, "dark");
		assert.equal(env.metadata.content, "#0d1917");
	}
});

test("OS subscription updates System without persisting its resolution, and cleans up", () => {
	const env = mount();
	assert.equal(typeof theme.watchSystemTheme, "function");
	theme.applyTheme("system");
	const stop = theme.watchSystemTheme(() => theme.renderTheme("system"));
	env.media.matches = true;
	for (const listener of env.listeners) listener({ matches: true });
	assert.equal(env.root.dataset.theme, "dark");
	assert.equal(env.values.get("archimedes.theme"), "system");
	stop();
	assert.equal(env.listeners.size, 0);
	mount({ blockedMedia: true });
	assert.doesNotThrow(() => theme.watchSystemTheme(() => {})());
});

test("revocation clears saved choice, retaining current visit appearance", () => {
	const env = mount();
	theme.applyTheme("light");
	saveConsent({ functional: false });
	assert.equal(env.values.has("archimedes.theme"), false);
	assert.equal(env.root.dataset.theme, "light");
	assert.equal(theme.getStoredTheme(), "system");
});

test("bootstrap and mounted code agree with blocked React, invalid storage and OS failures", () => {
	const scriptPath = new URL("../public/theme-init.js", import.meta.url);
	assert.ok(
		existsSync(scriptPath),
		"same-origin first-paint script must exist",
	);
	const script = readFileSync(scriptPath, "utf8");
	for (const stored of [null, "sepia", "light", "dark", "system"]) {
		for (const consent of [true, false, null]) {
			for (const systemDark of [true, false]) {
				for (const blocked of [false, true]) {
					const options = {
						stored,
						consent,
						systemDark,
						blockedRead: blocked,
						blockedMedia: blocked,
					};
					const firstPaint = browser(options);
					vm.runInNewContext(script, firstPaint);
					const expectedChoice =
						!blocked &&
						consent === true &&
						["light", "dark", "system"].includes(stored)
							? stored
							: "system";
					const expectedTheme =
						expectedChoice === "system"
							? blocked || systemDark
								? "dark"
								: "light"
							: expectedChoice;
					assert.equal(firstPaint.root.dataset.appearance, expectedChoice);
					assert.equal(firstPaint.root.dataset.theme, expectedTheme);
					const mounted = mount(options);
					theme.renderTheme(theme.getStoredTheme());
					assert.deepEqual(
						firstPaint.root.dataset,
						mounted.root.dataset,
						JSON.stringify(options),
					);
					assert.equal(
						firstPaint.root.style.colorScheme,
						mounted.root.style.colorScheme,
					);
					assert.equal(firstPaint.metadata.content, mounted.metadata.content);
					assert.deepEqual(
						firstPaint.values,
						mounted.values,
						"first paint must not write preferences",
					);
				}
			}
		}
	}
});
