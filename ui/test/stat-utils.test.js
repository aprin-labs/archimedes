import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	changeWindowLabel,
	groupChangeWindowLabel,
	median,
} from "../src/statUtils.js";

// ── #1322: group "24h change" headline must be robust to a single outlier ──
// A prior arithmetic-mean implementation let one bad tick (or one genuinely
// large single mover) drag a whole group's headline by tens of points. The
// median is robust to a single outlier of any magnitude — this is the
// property under test, not just "median math is correct".

test("median: odd-length array returns the middle value", () => {
	assert.equal(median([3, 1, 2]), 2);
});

test("median: even-length array averages the two middle values", () => {
	assert.equal(median([1, 2, 3, 4]), 2.5);
});

test("median: empty array returns null, not NaN or 0", () => {
	assert.equal(median([]), null);
});

test("median: single value returns that value", () => {
	assert.equal(median([42]), 42);
});

test("median: does not mutate its input array", () => {
	const input = [5, 3, 1, 4, 2];
	const copy = [...input];
	median(input);
	assert.deepEqual(input, copy);
});

test("median is robust to one outlier of any magnitude (the actual #1322 bug)", () => {
	// The reported live defect: sJUP's corrupted +1483.08% sat among a crypto
	// group whose real median 24h move was ~6.70%, but an arithmetic mean
	// reported "+29.82% avg 24h" — the outlier moved the headline by ~23 points.
	const realistic = [2.1, -0.8, 1.4, 3.2, -1.1, 0.6, 6.7, 4.4, -2.3, 0.2];
	const withOutlier = [...realistic, 1483.08];

	const meanOf = (nums) => nums.reduce((a, b) => a + b, 0) / nums.length;

	const medianBefore = median(realistic);
	const medianAfter = median(withOutlier);
	const meanBefore = meanOf(realistic);
	const meanAfter = meanOf(withOutlier);

	// The median barely moves for one outlier among 11 values...
	assert.ok(
		Math.abs(medianAfter - medianBefore) < 1,
		`median moved by ${medianAfter - medianBefore}`,
	);
	// ...while the mean is dragged by well over 100 points — proving *why*
	// the median was the correct fix, not merely that it computes correctly.
	assert.ok(
		meanAfter - meanBefore > 100,
		`mean only moved by ${meanAfter - meanBefore}`,
	);
});

// ── Wiring: both group-headline call sites use the shared median, and the
// arithmetic-mean pattern the issue names is fully gone from both ──────────

const explorePage = readFileSync(
	new URL("../src/components/Explore.jsx", import.meta.url),
	"utf8",
);
const groupModal = readFileSync(
	new URL("../src/components/AssetGroupModal.jsx", import.meta.url),
	"utf8",
);

// The import pins allow sibling named imports — #1378 added
// changeWindowLabel / groupChangeWindowLabel alongside median. The property
// under test is that `median` comes from the shared module rather than being
// re-implemented locally, which a `[^}]*\bmedian\b[^}]*` pin still enforces;
// pinning the exact one-name import list guarded spelling, not sourcing.
test("Explore.jsx group headline imports and calls the shared median, not a local mean", () => {
	assert.match(
		explorePage,
		/import \{[^}]*\bmedian\b[^}]*\} from ['"]\.\.\/statUtils['"]/,
	);
	assert.match(explorePage, /median\(vals\)/);
	assert.doesNotMatch(
		explorePage,
		/reduce\(\(a, b\) => a \+ b, 0\) \/ vals\.length/,
	);
});

test("AssetGroupModal.jsx aggregate stat imports and calls the shared median, not a local mean", () => {
	assert.match(
		groupModal,
		/import \{[^}]*\bmedian\b[^}]*\} from '\.\.\/statUtils'/,
	);
	assert.match(groupModal, /median\(vals\)/);
	assert.doesNotMatch(
		groupModal,
		/reduce\(\(a, b\) => a \+ b, 0\) \/ vals\.length/,
	);
});

test("the group headline labels no longer claim 'avg' when the value is a median", () => {
	assert.doesNotMatch(explorePage, />\s*avg 24h\s*</);
	assert.doesNotMatch(groupModal, /Avg 24h change/);
	// #1378 made the window dynamic, so the pin follows the template literal
	// rather than the old hardcoded "24h". "median" is still the word under
	// test — that is what #1322 was about.
	assert.match(explorePage, /`median \$\{medianWindow\}`/);
	assert.match(groupModal, /`Median \$\{medianWindow\} change`/);
});

// ── #1378: the window label must never fall back to the "24h" claim ─────────
//
// `change_24h_pct` is a one-bar change. One bar is 24 hours only on a 24/7
// feed; a Friday-to-Monday equity pair spans 72. The backend now measures the
// real window; these guard the display-layer fallbacks, which are where the
// old false claim actually lived.

test("changeWindowLabel: uses the backend's measured window when present", () => {
	assert.equal(changeWindowLabel({ change_window_label: "3d" }), "3d");
	assert.equal(changeWindowLabel({ change_window_label: "24h" }), "24h");
});

test("changeWindowLabel: an unknown window never falls back to '24h'", () => {
	// The load-bearing assertion. Defaulting to "24h" here would reinstate the
	// exact claim #1378 exists to remove, on precisely the rows where we have
	// least reason to make it.
	assert.equal(changeWindowLabel({ change_window_label: null }), "prev close");
	assert.equal(changeWindowLabel({}), "prev close");
	assert.equal(changeWindowLabel(undefined), "prev close");
});

test("groupChangeWindowLabel: returns the shared window when members agree", () => {
	const members = [
		{ change_24h_pct: 1.0, change_window_label: "3d" },
		{ change_24h_pct: -2.0, change_window_label: "3d" },
	];
	assert.equal(groupChangeWindowLabel(members), "3d");
});

test("groupChangeWindowLabel: returns null when members disagree", () => {
	// A group spanning a holiday genuinely holds both windows. There is no
	// single true label for that, so the caller must render a generic one
	// rather than picking a member's window and implying it covers the group.
	const members = [
		{ change_24h_pct: 1.0, change_window_label: "24h" },
		{ change_24h_pct: -2.0, change_window_label: "2d" },
	];
	assert.equal(groupChangeWindowLabel(members), null);
});

test("groupChangeWindowLabel: one unknown window poisons the group label", () => {
	const members = [
		{ change_24h_pct: 1.0, change_window_label: "24h" },
		{ change_24h_pct: -2.0, change_window_label: null },
	];
	assert.equal(groupChangeWindowLabel(members), null);
});

test("groupChangeWindowLabel: ignores members that contributed no value", () => {
	// Matches the median's own filter — a member with a null change is not in
	// the aggregate, so its window must not constrain the aggregate's label.
	const members = [
		{ change_24h_pct: 1.0, change_window_label: "3d" },
		{ change_24h_pct: null, change_window_label: "24h" },
	];
	assert.equal(groupChangeWindowLabel(members), "3d");
});

test("groupChangeWindowLabel: empty and absent inputs return null, not a guess", () => {
	assert.equal(groupChangeWindowLabel([]), null);
	assert.equal(groupChangeWindowLabel(undefined), null);
});
