import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// Source-regex pins on Generate.jsx's new credit-visibility notice (v8 Lane
// 1.3a). Same idiom as ui/test/generate-quote.test.js's "Wiring" section and
// ui/test/payment-receipts.test.js: readFileSync + regex/substring
// assertions on the rendered source, confirming the fetch is wired to the
// real credentialed helper and the endpoint, the notice is gated on a real
// unspent credit, it is dismissable, and its copy actually describes what
// `_paywall_with_credit` (backend/archimedes/api/generate_routes.py) does.

const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);

// The notice's OWN JSX, sliced out of the 1300-line component: from its
// opening comment marker to the next sibling block. The wording anti-goals
// below are about THIS notice's copy — asserted against the whole file they
// would also fail on an unrelated future "refund" elsewhere in Generate.jsx
// (a false alarm) and would pass for the wrong reason if the notice were
// deleted outright. Same indexOf/slice idiom as
// ui/test/payment-receipts.test.js's settlement-reference pin.
const NOTICE_START = "Paid generation credit notice";
const NOTICE_END = "{/* Submit row */}";
const noticeSlice = (() => {
	const start = generate.indexOf(NOTICE_START);
	assert.ok(
		start !== -1,
		`notice marker ${JSON.stringify(NOTICE_START)} not found`,
	);
	const end = generate.indexOf(NOTICE_END, start);
	assert.ok(
		end !== -1,
		`sibling marker ${JSON.stringify(NOTICE_END)} not found after the notice`,
	);
	return generate.slice(start, end);
})();

test("the sliced notice region is the real notice (guards the slice itself)", () => {
	// Without this, an empty or misaligned slice would make every
	// doesNotMatch below pass vacuously — the classic "guard that guards
	// nothing" failure. Pin that the slice actually contains the copy.
	assert.ok(
		noticeSlice.includes("paid generation credit"),
		"slice must contain the notice copy",
	);
	assert.ok(
		noticeSlice.length > 100,
		`slice suspiciously short (${noticeSlice.length} chars)`,
	);
});

// ── Fetches the owner-scoped credits endpoint via the shared apiGet helper ──

test("fetches GET /api/generate/credits via the shared apiGet helper, not a bare fetch", () => {
	// apiGet (src/api.js) sends credentials:'include', which is what makes the
	// request owner-scoped server-side (require_current_user reads the Better
	// Auth session cookie) — pin the real helper call, not a copy/paste fetch.
	assert.match(generate, /apiGet\(\s*["']\/api\/generate\/credits["']\s*\)/);
});

test("fetches credits on mount, independently of GENERATION_QUOTE_ENABLED", () => {
	// A credit is a real balance regardless of whether the upfront quote card
	// is shown — the fetch must not be nested inside the quote flag's gate.
	const fetchCallIdx = generate.indexOf("fetchCredits();");
	assert.ok(fetchCallIdx !== -1, "fetchCredits() must be called somewhere");
	assert.match(
		generate,
		/useEffect\(\(\) => \{\s*fetchCredits\(\);\s*\}, \[fetchCredits\]\);/,
	);
});

test("re-fetches credits after every successful /start (a credit may have just been spent)", () => {
	// Each of the four post-submitStart() success paths (plain start, the
	// refetch-then-retry path, the held-requirements finish, and the
	// smart-wallet finish) must refresh the ledger, not just the quote.
	const matches = generate.match(/fetchCredits\(\);/g) || [];
	// One for the mount effect's definition site is NOT counted here (that's
	// the useEffect body, matched separately above) — this counts every call
	// SITE, so >= 4 post-submit refreshes + the 1 mount-effect call.
	assert.ok(
		matches.length >= 5,
		`expected >=5 fetchCredits() call sites, found ${matches.length}`,
	);
});

// ── The notice: gated on a real unspent ("available") credit ─────────────

test("derives the notice from an `available` credit, not merely a non-empty list", () => {
	// A `pending`/`consumed`/`void` row must not trigger the notice — only a
	// spendable one does (mirrors take_available_credit's status filter).
	assert.match(
		generate,
		/credits\.find\(\(c\) => c\.status === ["']available["']\)/,
	);
});

test("the notice is gated on the derived unspent credit AND the dismissed flag", () => {
	assert.match(
		generate,
		/\{unspentCredit\s*&&\s*quote\?\.payment_required\s*&&\s*!creditNoticeDismissed\s*&&\s*\(/,
	);
});

test("the notice is ALSO gated on payments actually being on", () => {
	// "no new charge" only says something true when there is a charge to
	// avoid. With GENERATION_PAYMENT_REQUIRED off, quote.payment_required is
	// false and nothing is charged either way — showing the banner there would
	// claim the payer was spared a cost that never existed. The gate must read
	// the live quote, not a constant.
	const gate = noticeSlice.match(
		/\{unspentCredit\s*&&\s*(?:quote\?\.payment_required\s*&&\s*)?!creditNoticeDismissed\s*&&\s*\(/,
	);
	assert.ok(gate, "notice gate expression not found inside the notice slice");
	assert.match(gate[0], /quote\?\.payment_required/);
});

test("the notice is an announced live region (assistive tech hears it appear)", () => {
	// Sibling pattern: the generate-submit-status live region below it.
	assert.match(noticeSlice, /role="status"/);
	assert.match(noticeSlice, /aria-live="polite"/);
});

test("the notice has a working dismiss control (dismissable, per spec)", () => {
	assert.match(generate, /creditNoticeDismissed/);
	assert.match(generate, /setCreditNoticeDismissed\(true\)/);
	assert.match(generate, /useState\(false\)/);
});

// ── Wording matches what _paywall_with_credit actually does ──────────────

test("the notice's copy is the exact honest wording from the spec", () => {
	// _paywall_with_credit (generate_routes.py) checks generation_credits.take_credit()
	// BEFORE the paywall runs and, if an unspent credit exists, spends it
	// instead of charging — this is the literal behavior the banner describes.
	assert.match(
		generate,
		/You have a paid generation credit — this run will use\s+it, no\s+new charge\./,
	);
});

test("does not overclaim: never says the credit is a refund, discount, or free trial", () => {
	// Loose but real anti-goal: this is a paid-and-banked credit being spent,
	// not any of these other financial concepts that would misdescribe it.
	// Scoped to the notice's own JSX (see noticeSlice above) — an unrelated
	// "refund" elsewhere in this 1300-line component is not this notice
	// overclaiming, and must not fail this test.
	assert.doesNotMatch(noticeSlice, /\brefund\b/i);
	assert.doesNotMatch(noticeSlice, /\bdiscount\b/i);
	assert.doesNotMatch(noticeSlice, /\bfree trial\b/i);
});
