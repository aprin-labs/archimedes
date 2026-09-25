// Corpus catalog search: debounce + cancel (#1665).
//
// The pre-fix component fired one `/api/papers/?search=…` request per
// keystroke, with no debounce and no AbortController:
//
//     const fetchPapers = useCallback(async () => { … await apiGet(…) … },
//                                     [page, search, categoryFilter])
//     useEffect(() => { fetchPapers() }, [fetchPapers])
//
// Two defects in one line. Typing an 8-character word issued 8 requests, each
// costing the backend an unanchored ILIKE scan of the papers table — and
// nothing tied a response to the request that asked for it, so a slow early
// response could land AFTER a fast later one and repaint the list for a prefix
// the user had already finished typing.
//
// The scheduling logic lives in ../src/corpusSearch.js precisely so it can be
// exercised here for real rather than pattern-matched: `ui/` runs under
// `node --test` with no DOM and no JSX transform, so anything left inside the
// .jsx component can only be checked by scanning source text. Both halves are
// covered:
//
//   * behavioural — the real module, driven through React's effect lifecycle
//     (run, cleanup-then-run, …) with mocked timers;
//   * source-text — CorpusExplorer.jsx actually calls that module, so the
//     behavioural tests are not passing over a component that bypasses them.
//
// Every guard is also run against the pre-fix behaviour it exists to reject
// (`naiveSchedule` / `naiveApply`), so a guard that has stopped guarding fails
// loudly instead of silently.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test, { mock } from "node:test";

import {
	SEARCH_DEBOUNCE_MS,
	isSupersededError,
	runCatalogFetch,
	scheduleCatalogFetch,
} from "../src/corpusSearch.js";

const explorer = readFileSync(
	new URL("../src/components/CorpusExplorer.jsx", import.meta.url),
	"utf8",
);
const api = readFileSync(new URL("../src/api.js", import.meta.url), "utf8");

const TERM = "momentum"; // 8 characters — the issue's own example
const TYPING_GAP_MS = 20; // a fast typist, well inside the debounce window

/**
 * Drive `schedule` exactly the way React drives an effect whose dependency
 * changes on every keystroke: run, then cleanup-then-run, then … . The final
 * cleanup is deliberately NOT called — React only runs it on the next change
 * or on unmount, and the surviving request is the one that must fire.
 *
 * Returns the fetches that actually happened and the number of aborts.
 */
function typeInto(schedule, term) {
	let aborts = 0;
	const RealAbortController = globalThis.AbortController;
	class CountingAbortController extends RealAbortController {
		abort(reason) {
			aborts += 1;
			super.abort(reason);
		}
	}
	globalThis.AbortController = CountingAbortController;

	const fetches = [];
	let cleanup = null;
	try {
		for (let i = 1; i <= term.length; i += 1) {
			const soFar = term.slice(0, i);
			if (cleanup) cleanup(); // React's effect cleanup for the previous value
			cleanup = schedule((signal) => fetches.push({ term: soFar, signal }));
			mock.timers.tick(TYPING_GAP_MS);
		}
		mock.timers.tick(SEARCH_DEBOUNCE_MS + 1); // the user stops typing
	} finally {
		globalThis.AbortController = RealAbortController;
	}
	return { fetches, aborts, cleanup };
}

/** The pre-fix scheduler: fire immediately, no controller, nothing to cancel. */
function naiveSchedule(run) {
	run(undefined);
	return () => {};
}

test("eight keystrokes produce exactly one request and seven aborts", () => {
	mock.timers.enable({ apis: ["setTimeout"] });
	try {
		const { fetches, aborts } = typeInto(scheduleCatalogFetch, TERM);

		assert.equal(
			fetches.length,
			1,
			`typing ${TERM.length} characters issued ${fetches.length} requests — the debounce is not collapsing them`,
		);
		assert.equal(
			fetches[0].term,
			TERM,
			"the surviving request must be the LAST prefix typed, not an earlier one",
		);
		assert.equal(
			aborts,
			TERM.length - 1,
			`expected ${TERM.length - 1} aborts (one per superseded keystroke), got ${aborts}`,
		);
		assert.equal(
			fetches[0].signal.aborted,
			false,
			"the request that actually fired must not be carrying an already-aborted signal",
		);
	} finally {
		mock.timers.reset();
	}
});

test("nothing is requested before the debounce window elapses", () => {
	mock.timers.enable({ apis: ["setTimeout"] });
	try {
		const fetches = [];
		scheduleCatalogFetch(() => fetches.push(1));
		mock.timers.tick(SEARCH_DEBOUNCE_MS - 1);
		assert.equal(fetches.length, 0, "a request fired before the 300 ms window closed");
		mock.timers.tick(1);
		assert.equal(fetches.length, 1, "the request never fired after the window closed");
	} finally {
		mock.timers.reset();
	}
});

test("ADVERSARIAL: the pre-fix scheduler reports 8 requests and 0 aborts, and fails the guard", () => {
	// Anti-vacuity. Feed the counting harness the exact behaviour the fix
	// replaced. If this ever produced 1 request the test above would be
	// guarding nothing.
	mock.timers.enable({ apis: ["setTimeout"] });
	try {
		const { fetches, aborts } = typeInto(naiveSchedule, TERM);
		assert.equal(fetches.length, TERM.length, "the un-debounced path should fire once per keystroke");
		assert.equal(aborts, 0, "the un-debounced path has no controller to abort");
		assert.throws(
			() => assert.equal(fetches.length, 1),
			"the 'exactly 1 request' assertion passes against the un-debounced path — it is decorative",
		);
	} finally {
		mock.timers.reset();
	}
});

// ── the ordering bug: a slow early response settling after a fast later one ──

/**
 * Two overlapping requests where the FIRST one is slower — the exact race the
 * pre-fix code lost. R1 ("mom") is scheduled, fires, and is then superseded by
 * R2 ("momentum"); R2's response arrives first, R1's arrives afterwards.
 *
 * `apply` is the strategy under test: the real `runCatalogFetch` (which checks
 * the signal after the await) or `naiveApply` (which does not).
 */
async function raceOutOfOrder(apply) {
	const painted = [];
	const errors = [];

	let resolveSlow;
	const slow = new Promise((resolve) => {
		resolveSlow = resolve;
	});

	const c1 = new AbortController();
	const c2 = new AbortController();

	// R1: "mom" — issued first, will settle last.
	const r1 = apply(c1.signal, {
		fetchPage: () => slow,
		onResult: (d) => painted.push(d.term),
		onError: (e) => errors.push(e),
	});

	// The user keeps typing: R1 is superseded and cancelled, R2 is issued.
	c1.abort();

	// R2: "momentum" — issued second, settles first.
	const r2 = apply(c2.signal, {
		fetchPage: () => Promise.resolve({ term: "momentum" }),
		onResult: (d) => painted.push(d.term),
		onError: (e) => errors.push(e),
	});
	const outcome2 = await r2;

	// ...and only NOW does the slow first response come back.
	resolveSlow({ term: "mom" });
	const outcome1 = await r1;

	return { painted, errors, outcome1, outcome2 };
}

/** The pre-fix applier: whatever settles last wins, regardless of the signal. */
async function naiveApply(_signal, { fetchPage, onResult, onError }) {
	try {
		onResult(await fetchPage());
		return "applied";
	} catch (err) {
		onError(err);
		return "failed";
	}
}

test("a superseded response never repaints the catalog, even if it settles last", async () => {
	const { painted, errors, outcome1, outcome2 } = await raceOutOfOrder(runCatalogFetch);

	assert.deepEqual(
		painted,
		["momentum"],
		`the catalog was painted with ${JSON.stringify(painted)} — a stale response for an abandoned prefix reached the UI`,
	);
	assert.equal(painted.at(-1), "momentum", "the LAST paint must be the term the user actually typed");
	assert.equal(outcome1, "superseded", "the abandoned request must report itself superseded, not applied");
	assert.equal(outcome2, "applied", "the current request must be applied");
	assert.deepEqual(errors, [], "cancelling a request must not raise an error into the UI");
});

test("ADVERSARIAL: without the post-await signal check the stale response wins", async () => {
	// Anti-vacuity for the ordering guard. `abort()` cannot un-resolve a promise
	// that already resolved, so an applier that only relies on fetch rejecting
	// still paints "mom" last. This is the bug, reproduced.
	const { painted } = await raceOutOfOrder(naiveApply);

	assert.deepEqual(
		painted,
		["momentum", "mom"],
		"expected the pre-fix applier to paint the stale response last",
	);
	assert.throws(
		() => assert.deepEqual(painted, ["momentum"]),
		"the ordering assertion passes against the pre-fix applier — it is guarding nothing",
	);
});

test("an AbortError is treated as cancellation, not as an outage", async () => {
	// A real cancelled fetch REJECTS. That rejection must not reach onError —
	// otherwise every keystroke would flash the catalog's error state.
	const errors = [];
	const controller = new AbortController();
	controller.abort();

	const abortError = Object.assign(new Error("The operation was aborted."), { name: "AbortError" });
	const outcome = await runCatalogFetch(controller.signal, {
		fetchPage: () => Promise.reject(abortError),
		onResult: () => assert.fail("a cancelled request must not paint results"),
		onError: (e) => errors.push(e),
	});

	assert.equal(outcome, "superseded");
	assert.deepEqual(errors, [], "an abort was reported to the user as a failed request");

	// And a genuine backend failure must still reach onError — the check above
	// must not have swallowed error handling wholesale.
	const live = new AbortController();
	const outage = new Error("Backend returned 502");
	const failed = await runCatalogFetch(live.signal, {
		fetchPage: () => Promise.reject(outage),
		onResult: () => assert.fail("a failed request must not paint results"),
		onError: (e) => errors.push(e),
	});
	assert.equal(failed, "failed");
	assert.deepEqual(errors, [outage], "a real 502 must still surface as a catalog error");
});

test("isSupersededError distinguishes cancellation from failure", () => {
	const aborted = new AbortController();
	aborted.abort();
	const live = new AbortController();

	assert.equal(isSupersededError(new Error("Backend returned 502"), aborted.signal), true);
	assert.equal(isSupersededError({ name: "AbortError" }, undefined), true);
	assert.equal(isSupersededError(new Error("Backend returned 502"), live.signal), false);
	assert.equal(isSupersededError(new Error("boom"), undefined), false);
});

// ── the component and the transport actually use all of the above ──

test("CorpusExplorer.jsx routes its catalog fetch through the debounced scheduler", () => {
	assert.match(
		explorer,
		/import\s*\{[^}]*scheduleCatalogFetch[^}]*\}\s*from\s*['"]\.\.\/corpusSearch['"]/,
		"CorpusExplorer.jsx must import the scheduler these tests exercise",
	);
	assert.match(
		explorer,
		/useEffect\(\(\)\s*=>\s*scheduleCatalogFetch\(fetchPapers\),\s*\[fetchPapers\]\)/,
		"the catalog effect must return scheduleCatalogFetch's cancel function — that return value IS the cleanup React calls",
	);
	assert.match(
		explorer,
		/runCatalogFetch\(signal,/,
		"the fetch must go through runCatalogFetch, which is what discards a superseded response",
	);
	// The pre-fix wiring must be gone, not merely supplemented: an effect body
	// that calls fetchPapers() directly would fire on every keystroke alongside
	// the debounced one.
	assert.doesNotMatch(
		explorer,
		/useEffect\(\(\)\s*=>\s*\{\s*fetchPapers\(\)\s*\},\s*\[fetchPapers\]\)/,
		"the un-debounced effect is still present",
	);
});

test("the AbortSignal reaches fetch, so an abandoned request is actually cancelled", () => {
	// A controller whose signal never reaches `fetch` fixes ordering but not
	// load: the abandoned request still runs the backend scan to completion.
	assert.match(
		explorer,
		/apiGet\(`\/api\/papers\/\?\$\{params\}`,\s*\{\s*signal:\s*s\s*\}\)/,
		"CorpusExplorer.jsx must pass the signal to apiGet",
	);
	assert.match(
		api,
		/export async function apiGet\(path,\s*\{\s*signal\s*\}\s*=\s*\{\}\)/,
		"apiGet must accept a signal",
	);
	const body = api.slice(api.indexOf("export async function apiGet"));
	assert.match(
		body.slice(0, 400),
		/fetch\(`\$\{API_BASE\}\$\{path\}`,\s*\{[^}]*signal,/s,
		"apiGet must hand the signal to fetch — a signal it never forwards cancels nothing",
	);
});

test("the debounce window is 300 ms", () => {
	assert.equal(SEARCH_DEBOUNCE_MS, 300);
});
