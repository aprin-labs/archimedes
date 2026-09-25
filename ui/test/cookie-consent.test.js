import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

// Storage-consent guards (#1647).
//
// Two halves:
//   1. STRUCTURAL — readFileSync + regex over src/, the same idiom as
//      routes.test.js / a11y.test.js. These are the bidirectional map-vs-code
//      checks: nothing the code writes is missing from the disclosure, and
//      nothing in the disclosure is absent from the code.
//   2. BEHAVIOURAL — import the real modules and prove the gate suppresses a
//      functional write when consent is withheld. `node --test` runs each test
//      FILE in its own process, so stubbing localStorage on globalThis here
//      cannot leak into theme.test.js.
//
// Every assertion in this file was confirmed to FAIL against a deliberately
// broken tree before the PR was opened — transcripts are in the PR body
// (CLAUDE.md § "a guard must be shown to reject something").

// ── source access ───────────────────────────────────────────────────────

const SRC = new URL("../src/", import.meta.url).pathname;
const REPO = new URL("../../", import.meta.url).pathname;

function walk(dir, out = []) {
	for (const entry of readdirSync(dir)) {
		const full = join(dir, entry);
		if (statSync(full).isDirectory()) walk(full, out);
		else if (/\.(js|jsx)$/.test(entry)) out.push(full);
	}
	return out;
}

const SRC_FILES = walk(SRC).map((f) => ({
	path: `ui/src/${f.slice(SRC.length)}`,
	text: readFileSync(f, "utf8"),
}));

const file = (rel) => {
	const found = SRC_FILES.find((f) => f.path === rel);
	assert.ok(found, `expected source file to exist: ${rel}`);
	return found.text;
};

const consentBanner = file("ui/src/components/ConsentBanner.jsx");
const consentChoices = file("ui/src/components/ConsentChoices.jsx");
const disclosure = file("ui/src/components/StorageDisclosure.jsx");
const appJsx = file("ui/src/App.jsx");
const mainJsx = file("ui/src/main.jsx");
const securityJsx = file("ui/src/components/Security.jsx");

const {
	CONSENT_STORAGE_KEY,
	STORAGE_INVENTORY,
	OPTIONAL_CATEGORIES,
	canStore,
	displayName,
	entriesInCategory,
	isCategoryAllowed,
	lookupEntry,
	purgeDisallowed,
	readConsent,
	saveConsent,
} = await import("../src/storage-consent.js");

// ── 1. the raw-write census ─────────────────────────────────────────────

const WRITE_RE = /localStorage\.setItem|sessionStorage\.setItem|document\.cookie/;

// Every (file, line) in src/ that writes browser storage.
const writeSites = [];
for (const { path, text } of SRC_FILES) {
	const lines = text.split("\n");
	lines.forEach((line, i) => {
		if (WRITE_RE.test(line)) writeSites.push({ path, line: i + 1, lines, i });
	});
}

// The count on origin/main at the time this landed, from
//   grep -rn "localStorage.setItem\|document.cookie\|sessionStorage.setItem" \
//     ui/src --include="*.js" --include="*.jsx" | wc -l
// The issue's first acceptance criterion: the branch must MATCH OR EXCEED it.
// It exceeds it by exactly one — storage-consent.js writing its own consent
// record — because this PR wraps the existing 13 writes in place rather than
// funnelling them through a helper (a helper would have DROPPED the count to
// 3 and quietly defeated this very guard).
const MAIN_BASELINE_WRITE_SITES = 13;

test("no storage write was removed or hidden from the census (#1647 AC1)", () => {
	assert.ok(
		writeSites.length >= MAIN_BASELINE_WRITE_SITES,
		`expected >= ${MAIN_BASELINE_WRITE_SITES} storage-write sites in ui/src, found ${writeSites.length}`,
	);
});

test("the app still sets zero cookies from JavaScript", () => {
	// Every cookie in the inventory is server-set and HttpOnly; the disclosure
	// says so. A client-side document.cookie write would make that false.
	const jsCookieWrites = writeSites.filter((s) =>
		/document\.cookie/.test(s.lines[s.i]),
	);
	assert.deepEqual(jsCookieWrites, []);
});

// A write is "gated" when canStore() appears on its own line or within the
// four lines above it. Deliberately a proximity rule rather than real
// dataflow: it is a regex guard, and it is honest about that. What it
// reliably catches is the regression this issue exists to prevent — a NEW
// `localStorage.setItem` pasted in with no consent check anywhere near it.
const GUARD_WINDOW = 4;

test("every storage write in ui/src is gated by canStore() (#1647 AC1)", () => {
	const ungated = writeSites.filter(({ lines, i }) => {
		const window = lines.slice(Math.max(0, i - GUARD_WINDOW), i + 1).join("\n");
		return !/canStore\(/.test(window);
	});
	assert.deepEqual(
		ungated.map((s) => `${s.path}:${s.line}`),
		[],
		"these storage writes have no canStore() guard within 4 lines — add the key to STORAGE_INVENTORY and gate the write (#1647)",
	);
});

// ── 2. bidirectional map ↔ code ─────────────────────────────────────────

// Storage keys in this app are all spelled `archimedes` + one of . _ :
// (`archimedes-<hex>` ids and the bare string 'archimedes' are NOT storage
// keys and are correctly excluded by the separator class).
const KEY_LITERAL_RE = /(['"`])(archimedes[._:][^'"`\n]*)\1/g;

const inventoryNames = new Set(STORAGE_INVENTORY.map((e) => e.name));
const inventoryDisplays = new Set(STORAGE_INVENTORY.map((e) => displayName(e)));

test("direction A: every storage-key literal in ui/src is disclosed in the map", () => {
	const undisclosed = [];
	for (const { path, text } of SRC_FILES) {
		for (const match of text.matchAll(KEY_LITERAL_RE)) {
			const literal = match[2];
			if (inventoryNames.has(literal) || inventoryDisplays.has(literal)) continue;
			undisclosed.push(`${path}: ${literal}`);
		}
	}
	assert.deepEqual(
		undisclosed,
		[],
		"these keys appear in the source but not in STORAGE_INVENTORY — under-disclosure (#1647)",
	);
});

test("direction B: every key in the map really is in the code it names", () => {
	const phantom = [];
	for (const entry of STORAGE_INVENTORY) {
		const source = readFileSync(join(REPO, entry.source), "utf8");
		if (!source.includes(entry.name)) phantom.push(`${entry.name} @ ${entry.source}`);
	}
	assert.deepEqual(
		phantom,
		[],
		"these keys are disclosed but their named source file does not contain them — over-disclosure (#1647)",
	);
});

// The enumeration the issue itself asked for, transcribed here ON PURPOSE:
// this is the spec side of the bidirectional check, so it must be an
// independent copy rather than something derived from the module under test.
// `category` is this PR's classification; the notes record the two places the
// issue's own table needed correcting against a fresh grep.
const SPEC_KEYS = [
	["better-auth.session_token", "cookie", "necessary"],
	["archimedes_session", "cookie", "necessary"],
	["archimedes_vid", "cookie", "analytics"],
	["archimedes_wallet", "localStorage", "necessary"],
	["archimedes_wallet_names", "localStorage", "functional"],
	["archimedes.theme", "localStorage", "functional"],
	["archimedes_payment_key:", "localStorage", "necessary"],
	["archimedes.onboarding.v1", "localStorage", "functional"],
	["archimedes_circle_credential", "localStorage", "necessary"],
	// The issue's table cites circle-wallet.js:50 as a write site. Re-grepped
	// at PR time that line is only the const declaration: USERNAME_STORAGE_KEY
	// has no setItem anywhere, just the removeItem in clearCircleSession. It is
	// disclosed as `legacy` rather than as something the app stores.
	["archimedes_circle_username", "localStorage", "legacy"],
	["archimedes_deposit_", "localStorage", "functional"],
	["archimedes.welcomeProfileSeen.", "localStorage", "functional"],
	["archimedes.rigorStrictness", "localStorage", "functional"],
	["archimedes_landed", "sessionStorage", "analytics"],
	["archimedes:pending-link", "sessionStorage", "necessary"],
	["archimedes_circle_credential_tab_seen_id", "sessionStorage", "necessary"],
];

test("every key the issue enumerated is in the map, with its category", () => {
	for (const [name, store, category] of SPEC_KEYS) {
		const entry = lookupEntry(name);
		assert.ok(entry, `missing from STORAGE_INVENTORY: ${name}`);
		assert.equal(entry.name, name, `${name} resolved to a different entry`);
		assert.equal(entry.store, store, `${name} store`);
		assert.equal(entry.category, category, `${name} category`);
		assert.ok(entry.purpose?.length > 20, `${name} needs a real purpose`);
		assert.ok(entry.reveals?.length > 20, `${name} needs a real 'reveals'`);
		assert.ok(entry.onReject?.length > 10, `${name} needs a stated fallback`);
	}
});

test("the consent record itself is disclosed and exempt, not hidden", () => {
	const entry = lookupEntry(CONSENT_STORAGE_KEY);
	assert.ok(entry, "the consent key must disclose itself");
	assert.equal(entry.category, "consent");
	assert.match(entry.reveals, /whatever you choose/i);
});

// ── 3. the UI actually offers the controls ──────────────────────────────

test("the banner ships Accept, Reject and Customize as real controls (#1647 AC3)", () => {
	// Not just copy: each label must sit on a <button> wired to a handler.
	assert.match(consentBanner, /Accept all\s*<\/button>/);
	assert.match(consentBanner, /Reject optional\s*<\/button>/);
	assert.match(consentBanner, /Customize\s*<\/button>/);
	assert.match(consentBanner, /onClick=\{acceptAll\}/);
	assert.match(consentBanner, /onClick=\{rejectOptional\}/);
	assert.match(consentBanner, /const rejectOptional = \(\) =>\s*decide\(\{\s*functional: false,\s*analytics: false\s*\}\)/);
	// Customize toggles a real panel, referenced by aria-controls.
	assert.match(consentBanner, /aria-expanded=\{customizing\}/);
	assert.match(consentBanner, /aria-controls=\{panelId\}/);
	assert.match(consentBanner, /<ConsentChoices/);
});

test("the banner is mounted app-wide, not just on one layout", () => {
	assert.match(mainJsx, /import ConsentBanner from '\.\/components\/ConsentBanner\.jsx'/);
	assert.match(mainJsx, /<ConsentBanner \/>/);
	// Not a modal: no focus trap, no portal, no page-blocking overlay.
	assert.doesNotMatch(consentBanner, /createPortal/);
	assert.match(consentBanner, /<aside/);
});

test("the customize panel offers exactly the switchable categories", () => {
	assert.match(consentChoices, /OPTIONAL_CATEGORIES\.map/);
	assert.deepEqual(OPTIONAL_CATEGORIES, ["functional", "analytics"]);
	// Anti-goal 1: no control is offered for anything strictly necessary.
	assert.ok(entriesInCategory("necessary").length > 0);
	assert.ok(!OPTIONAL_CATEGORIES.includes("necessary"));
});

test("no third-party consent SDK was introduced (#1647 anti-goal 2)", () => {
	const pkg = JSON.parse(readFileSync(join(REPO, "ui/package.json"), "utf8"));
	const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });
	const suspicious = deps.filter((d) =>
		/cookie|consent|osano|onetrust|cookiebot|klaro|analytics|segment|posthog|mixpanel/i.test(d),
	);
	assert.deepEqual(suspicious, []);
	for (const source of [consentBanner, consentChoices, disclosure]) {
		assert.doesNotMatch(source, /https?:\/\//);
	}
	// The disclosure page claims a third-party consent SDK could not load
	// here. A claim the code does not enforce is the defect CLAUDE.md names,
	// so pin the app-surface CSP that makes it true: script-src carries no
	// remote origin.
	const nginx = readFileSync(join(REPO, "nginx/nginx.conf"), "utf8");
	const appScriptSrc = nginx.match(
		/default\s+"default-src 'self'; script-src ([^;]*);/,
	);
	assert.ok(appScriptSrc, "the app-surface CSP was not found in nginx.conf");
	assert.doesNotMatch(appScriptSrc[1], /https?:\/\//);
});

// ── 4. the policy-page section ──────────────────────────────────────────

test("the policy page names all three session/analytics cookies (#1647 AC7)", () => {
	// The literal acceptance grep:
	//   grep -n "archimedes_vid\|archimedes_session\|better-auth.session_token" \
	//     ui/src/components/StorageDisclosure.jsx
	for (const name of [
		"better-auth.session_token",
		"archimedes_session",
		"archimedes_vid",
	]) {
		assert.ok(disclosure.includes(name), `disclosure must name ${name}`);
		const entry = lookupEntry(name);
		assert.ok(entry && entry.store === "cookie", `${name} must be a cookie entry`);
		// "with their stated purpose" — rendered from the shared inventory, so
		// the purpose can never drift from the gate's own copy.
		assert.ok(entry.purpose.length > 20);
	}
	// …and those names are a coverage assertion resolved against the shared
	// source, not a second hand-written table (anti-goal 3).
	assert.match(disclosure, /REQUIRED_COOKIES/);
	assert.match(disclosure, /lookupEntry\(name\)/);
	assert.match(disclosure, /entriesInCategory\(category\)/);
	assert.doesNotMatch(disclosure, /purpose:/);
});

test("the disclosure renders from the shared inventory and is reachable", () => {
	assert.match(disclosure, /from "\.\.\/storage-consent\.js"/);
	assert.match(disclosure, /id="storage-disclosure"/);
	assert.match(securityJsx, /<StorageDisclosure \/>/);
	assert.match(consentBanner, /href="\/security#storage-disclosure"/);
	// Every category the inventory uses has a rendered section.
	const rendered = disclosure.match(/const SECTION_ORDER = \[([\s\S]*?)\]/)[1];
	for (const category of new Set(STORAGE_INVENTORY.map((e) => e.category))) {
		assert.ok(rendered.includes(`"${category}"`), `no section renders ${category}`);
	}
});

// ── 5. behaviour: the gate actually suppresses (#1647 AC4) ──────────────

function makeStore() {
	const map = new Map();
	return {
		get length() {
			return map.size;
		},
		key: (i) => [...map.keys()][i] ?? null,
		getItem: (k) => (map.has(k) ? map.get(k) : null),
		setItem: (k, v) => map.set(k, String(v)),
		removeItem: (k) => map.delete(k),
		snapshot: () => Object.fromEntries(map),
	};
}

let setAttributeCalls = [];
globalThis.document = {
	documentElement: { setAttribute: (...args) => setAttributeCalls.push(args) },
};

const { applyTheme, getStoredTheme } = await import("../src/theme.js");

test.beforeEach(() => {
	setAttributeCalls = [];
	globalThis.localStorage = makeStore();
	globalThis.sessionStorage = makeStore();
});

test("undecided means OFF: a functional key is not written before any choice", () => {
	assert.equal(readConsent(), null);
	assert.equal(canStore("archimedes.theme"), false);
	applyTheme("light");
	assert.equal(globalThis.localStorage.getItem("archimedes.theme"), null);
	// …and the page still themes itself; only persistence is withheld.
	assert.deepEqual(setAttributeCalls, [["data-theme", "light"]]);
});

test("REJECT suppresses the functional write entirely (the revert-demo case)", () => {
	saveConsent({ functional: false, analytics: false });
	assert.equal(canStore("archimedes.theme"), false);
	applyTheme("light");
	assert.equal(
		globalThis.localStorage.getItem("archimedes.theme"),
		null,
		"theme must NOT be persisted when functional storage is rejected",
	);
	assert.deepEqual(setAttributeCalls, [["data-theme", "light"]]);
	// Only the consent record itself survives in storage.
	assert.deepEqual(Object.keys(globalThis.localStorage.snapshot()), [
		CONSENT_STORAGE_KEY,
	]);
	// The fallback the disclosure promises: default dark on the next load.
	assert.equal(getStoredTheme(), "dark");
});

test("ACCEPT restores the write, so the guard is a gate and not a deletion", () => {
	saveConsent({ functional: true, analytics: false });
	assert.equal(canStore("archimedes.theme"), true);
	applyTheme("light");
	assert.equal(globalThis.localStorage.getItem("archimedes.theme"), "light");
	assert.equal(getStoredTheme(), "light");
});

test("rejecting analytics suppresses the landed marker AND its beacon", () => {
	saveConsent({ functional: true, analytics: false });
	assert.equal(canStore("archimedes_landed"), false);
	// The beacon is behind the same early return, not merely the marker —
	// otherwise the event would fire on every route change instead of once.
	const effect = appJsx.match(
		/const LANDED_KEY = "archimedes_landed";[\s\S]*?\}, \[consent\]\);/,
	);
	assert.ok(effect, "the landed effect was not found in its expected shape");
	assert.match(effect[0], /if \(!canStore\(LANDED_KEY\)\) return;/);
	assert.ok(
		effect[0].indexOf("if (!canStore(LANDED_KEY)) return;") <
			effect[0].indexOf("/api/metrics/funnel/event"),
		"the consent check must precede the beacon",
	);
	saveConsent({ functional: false, analytics: true });
	assert.equal(canStore("archimedes_landed"), true);
});

test("necessary keys keep working when everything optional is rejected", () => {
	saveConsent({ functional: false, analytics: false });
	for (const entry of entriesInCategory("necessary")) {
		const probe = entry.prefix ? `${entry.name}0xabc` : entry.name;
		assert.equal(
			canStore(probe),
			true,
			`${entry.name} is strictly necessary and must never be suppressed`,
		);
	}
	assert.equal(isCategoryAllowed("necessary"), true);
	assert.equal(isCategoryAllowed("consent"), true);
});

test("prefixed keys resolve to their entry, including the per-address ones", () => {
	saveConsent({ functional: true, analytics: true });
	assert.equal(
		lookupEntry("archimedes_payment_key:0xdead").name,
		"archimedes_payment_key:",
	);
	assert.equal(canStore("archimedes_deposit_0xvault"), true);
	assert.equal(
		lookupEntry("archimedes.welcomeProfileSeen.0xabc").category,
		"functional",
	);
});

test("an undisclosed key fails closed (this is AC1's runtime half)", () => {
	saveConsent({ functional: true, analytics: true });
	const warnings = [];
	const realWarn = console.warn;
	console.warn = (msg) => warnings.push(msg);
	try {
		assert.equal(canStore("archimedes.brandNewUndisclosedKey"), false);
	} finally {
		console.warn = realWarn;
	}
	assert.match(warnings.join("\n"), /undisclosed key/);
});

test("legacy keys are never writable", () => {
	saveConsent({ functional: true, analytics: true });
	for (const entry of entriesInCategory("legacy")) {
		assert.equal(canStore(entry.name), false, `${entry.name} must stay unwritable`);
	}
});

test("withdrawing consent deletes what was already stored, and only that", () => {
	saveConsent({ functional: true, analytics: true });
	globalThis.localStorage.setItem("archimedes.theme", "light");
	globalThis.localStorage.setItem("archimedes_wallet", '{"providerId":"x"}');
	globalThis.localStorage.setItem("archimedes_circle_username", "legacy-leftover");
	globalThis.sessionStorage.setItem("archimedes_landed", "1");
	globalThis.sessionStorage.setItem("archimedes:pending-link", "google");

	saveConsent({ functional: false, analytics: false });
	purgeDisallowed();

	assert.equal(globalThis.localStorage.getItem("archimedes.theme"), null);
	assert.equal(globalThis.localStorage.getItem("archimedes_circle_username"), null);
	assert.equal(globalThis.sessionStorage.getItem("archimedes_landed"), null);
	// Necessary keys and the consent record are untouched — a purge that signs
	// the user out would be worse than no purge at all.
	assert.equal(
		globalThis.localStorage.getItem("archimedes_wallet"),
		'{"providerId":"x"}',
	);
	assert.equal(globalThis.sessionStorage.getItem("archimedes:pending-link"), "google");
	assert.ok(globalThis.localStorage.getItem(CONSENT_STORAGE_KEY));
});

test("a blocked localStorage leaves the gate closed rather than throwing", () => {
	globalThis.localStorage = {
		getItem: () => {
			throw new Error("blocked");
		},
		setItem: () => {
			throw new Error("blocked");
		},
	};
	assert.doesNotThrow(() => readConsent());
	assert.equal(readConsent(), null);
	assert.equal(canStore("archimedes.theme"), false);
	assert.doesNotThrow(() => saveConsent({ functional: true, analytics: true }));
});
