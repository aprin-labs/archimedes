import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	ACCOUNT_USAGE_ENDPOINT,
	LOCK_EMAIL_UNVERIFIED,
	VERIFICATION_REQUESTED_MESSAGE,
	deriveFreeGenerationView,
	deriveWalletGateView,
} from "../src/freeGenerations.js";

// Free-generation allowance banner (#1643).
//
// Two halves, deliberately:
//   1. REAL unit tests of ../src/freeGenerations.js — the module that decides
//      whether and what to show. Every honesty rule this feature makes on the
//      frontend is executable here, not asserted by regex.
//   2. A small set of source pins on the component + its single Generate.jsx
//      mount point (the ui/test/generation-credits.test.js idiom), because
//      `node --test` has no DOM and the wiring cannot be executed.

const banner = readFileSync(
	new URL("../src/components/FreeGenerationBanner.jsx", import.meta.url),
	"utf8",
);
const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);

const usage = (overrides = {}) => ({
	date: "2026-08-31",
	user_id: "u1",
	user: { used: 0, cap: 10, unlimited: false, remaining: 10, error: null },
	ip: { used: 0, cap: 20, unlimited: false, remaining: 20, error: null },
	quote: { payment_required: true },
	free_generations_allowance: 3,
	free_generations_remaining: 3,
	free_generations_error: null,
	free_generations_locked_reason: null,
	...overrides,
});

// ── What the banner shows when the backend sent a real number ──────────────

test("a fresh account shows 3 of 3 left and says no wallet is needed yet", () => {
	const view = deriveFreeGenerationView(usage());
	assert.equal(view.remaining, 3);
	assert.equal(view.allowance, 3);
	assert.equal(view.exhausted, false);
	assert.equal(view.chipLabel, "3 free generations left");
	assert.match(view.message, /3 of 3 free generations left/);
	assert.match(view.message, /No wallet needed/i);
});

test("the last remaining generation is singular, not '1 free generations'", () => {
	const view = deriveFreeGenerationView(usage({ free_generations_remaining: 1 }));
	assert.equal(view.chipLabel, "1 free generation left");
	assert.match(view.message, /1 of 3 free generation left/);
});

test("an exhausted allowance switches to the wallet-gate message", () => {
	const view = deriveFreeGenerationView(usage({ free_generations_remaining: 0 }));
	assert.equal(view.exhausted, true);
	assert.equal(view.chipLabel, "Free generations used");
	assert.match(view.message, /used all 3 free generations/);
	assert.match(view.message, /Link a wallet/);
});

test("a non-default allowance is rendered as configured, never hard-coded to 3", () => {
	const view = deriveFreeGenerationView(
		usage({ free_generations_allowance: 5, free_generations_remaining: 4 }),
	);
	assert.equal(view.allowance, 5);
	assert.equal(view.chipLabel, "4 free generations left");
});

// ── The locked state: the allowance unlocks on a verified email (owner D1) ─
//
// The 2026-08-31 owner decision (recorded on #1653). The banner's job here is
// the carrot: silence would let a fresh account conclude the free tier is a
// fiction and bounce off the wallet gate, when the unlock is an inbox it
// already owns.

test("an unverified account is shown the carrot — what to do, and what it unlocks", () => {
	const view = deriveFreeGenerationView(
		usage({ free_generations_locked_reason: LOCK_EMAIL_UNVERIFIED }),
	);
	assert.equal(view.state, "locked");
	assert.equal(view.locked, true);
	assert.equal(view.lockedReason, "email_unverified");
	assert.equal(view.exhausted, false);
	assert.equal(view.remaining, 3);
	assert.equal(view.chipLabel, "3 free generations locked");
	assert.match(view.message, /Verify your email to unlock 3 free generations/);
	assert.match(view.message, /no wallet and no payment needed/i);
	// The carrot must not read as the exhausted state's wallet gate.
	assert.doesNotMatch(view.message, /Link a wallet/i);
});

test("the carrot counts what is actually left, not the headline allowance", () => {
	// Reachable: slots spent while verified cannot come back if verification
	// is later lost. Promising 3 when 1 is left would be a claim the gate
	// refuses on the second run.
	const view = deriveFreeGenerationView(
		usage({ free_generations_remaining: 1, free_generations_locked_reason: LOCK_EMAIL_UNVERIFIED }),
	);
	assert.equal(view.chipLabel, "1 free generation locked");
	assert.match(view.message, /unlock 1 free generation on this account/);
});

test("a locked account with nothing left is told to link a wallet, NOT to verify", () => {
	// Verifying unlocks nothing once the ledger is spent — an account that
	// spent its three before this gate shipped is exhausted AND unverified.
	// Offering the inbox here would be a dead end dressed as a way forward.
	const view = deriveFreeGenerationView(
		usage({ free_generations_remaining: 0, free_generations_locked_reason: LOCK_EMAIL_UNVERIFIED }),
	);
	assert.equal(view.state, "exhausted");
	assert.equal(view.exhausted, true);
	assert.match(view.message, /Link a wallet/);
	assert.doesNotMatch(view.message, /Verify your email/i);
});

test("a LOCKED account with an unreadable ledger still renders NOTHING", () => {
	// The two facts must not be conflated. We do not know how many slots
	// verification would unlock, and a carrot we cannot size is a promise we
	// cannot keep — so this is silence, exactly as an unlocked unreadable
	// ledger is.
	assert.equal(
		deriveFreeGenerationView(
			usage({
				free_generations_remaining: null,
				free_generations_error: "free_generation_backend_unavailable",
				free_generations_locked_reason: LOCK_EMAIL_UNVERIFIED,
			}),
		),
		null,
	);
});

test("a lock reason this build does not know renders nothing, not the email carrot", () => {
	// A newer backend could add a second reason and deploy ahead of the UI.
	// The count is still not spendable, so it must not be shown as available —
	// and "verify your email" would be an instruction that fixes nothing.
	for (const reason of ["region_blocked", "account_suspended", "", 7, {}]) {
		const view = deriveFreeGenerationView(usage({ free_generations_locked_reason: reason }));
		if (reason === "" || typeof reason !== "string") {
			// Absent/malformed is "not locked" — the pre-D1 payload shape.
			assert.equal(view.locked, false, `reason=${String(reason)} must read as unlocked`);
		} else {
			assert.equal(view, null, `reason=${String(reason)} must render nothing`);
		}
	}
});

test("a pre-D1 backend that omits the field entirely still renders the available state", () => {
	const { free_generations_locked_reason, ...rest } = usage();
	void free_generations_locked_reason;
	const view = deriveFreeGenerationView(rest);
	assert.equal(view.state, "available");
	assert.equal(view.locked, false);
	assert.equal(view.lockedReason, null);
	assert.equal(view.chipLabel, "3 free generations left");
});

// ── The honesty rule: a number is shown only when the backend sent one ─────
//
// These are the adversarial cases. Each input is one a naive implementation
// renders as a confident, wrong number; every one of them must render nothing.

test("free_generations_remaining: null renders NOTHING — not 0, not the allowance", () => {
	// The backend's honest "the ledger could not be read". Rendering 0 would
	// tell a brand-new account it is locked out; rendering 3 would promise
	// free runs the gate may refuse.
	const view = deriveFreeGenerationView(
		usage({ free_generations_remaining: null, free_generations_error: "free_generation_backend_unavailable" }),
	);
	assert.equal(view, null);
});

test("a response missing the free_generations_* fields entirely renders nothing", () => {
	// An older backend, or a partial/proxied response.
	const { free_generations_allowance, free_generations_remaining, ...rest } = usage();
	void free_generations_allowance;
	void free_generations_remaining;
	assert.equal(deriveFreeGenerationView(rest), null);
});

test("allowance 0 (the free path switched off) renders nothing, not '0 left'", () => {
	// FREE_GENERATIONS_PER_ACCOUNT=0 means the policy is not running at all; a
	// "0 free generations left" chip would imply it is running and exhausted.
	assert.equal(
		deriveFreeGenerationView(usage({ free_generations_allowance: 0, free_generations_remaining: 0 })),
		null,
	);
});

test("no response at all (signed out / failed fetch) renders nothing", () => {
	assert.equal(deriveFreeGenerationView(null), null);
	assert.equal(deriveFreeGenerationView(undefined), null);
	assert.equal(deriveFreeGenerationView("not an object"), null);
});

test("a malformed count renders nothing rather than a nonsense chip", () => {
	for (const bad of [-1, 1.5, "3", Number.NaN, {}]) {
		assert.equal(
			deriveFreeGenerationView(usage({ free_generations_remaining: bad })),
			null,
			`remaining=${String(bad)} must render nothing`,
		);
	}
});

test("remaining above the allowance is clamped, never shown as '9 of 3'", () => {
	const view = deriveFreeGenerationView(
		usage({ free_generations_allowance: 3, free_generations_remaining: 9 }),
	);
	assert.equal(view.remaining, 3);
	assert.equal(view.chipLabel, "3 free generations left");
});

// ── Wiring pins (no DOM available under `node --test`) ─────────────────────

test("the component renders the state the pure module decided, and decides none itself", () => {
	// data-state carries available|locked|exhausted onto the DOM so styling and
	// any future DOM test read the same decision this file unit-tests, rather
	// than re-deriving the rule from the payload.
	assert.match(banner, /data-state=\{view\.state\}/);
	assert.doesNotMatch(banner, /email_unverified|free_generations_locked_reason/);
});

test("the component reads the count from the backend, via the credentialed helper", () => {
	// apiGet (src/api.js) sends credentials:'include' — that cookie is what
	// makes /api/account/usage answer for THIS account. A bare fetch would
	// silently 401 for every signed-in user.
	assert.equal(ACCOUNT_USAGE_ENDPOINT, "/api/account/usage");
	assert.match(banner, /import \{ apiGet \} from "\.\.\/api"/);
	assert.match(banner, /apiGet\(ACCOUNT_USAGE_ENDPOINT\)/);
});

test("the component never counts generations itself — it only renders the backend's number", () => {
	// The gate is server-side; a client-side tally would drift from it and
	// would be trivially resettable by reloading the page.
	assert.doesNotMatch(banner, /localStorage|sessionStorage/);
	assert.doesNotMatch(banner, /\b(?:count|remaining)\s*(?:\+\+|--|\+=|-=)/);
	assert.match(banner, /deriveFreeGenerationView\(usage\)/);
});

test("a failed request sets no view — the banner never invents a fallback count", () => {
	const catchIdx = banner.indexOf(".catch(");
	assert.ok(catchIdx !== -1, "the fetch must have a catch handler");
	const catchBlock = banner.slice(catchIdx, catchIdx + 400);
	assert.match(catchBlock, /setView\(null\)/);
	assert.doesNotMatch(catchBlock, /setView\(\{/);
});

test("Generate.jsx mounts the banner exactly once, and imports it as its own component", () => {
	// The #1642 redesign is rewriting Generate.jsx concurrently, so this
	// feature's footprint there is one import + one element, and this test is
	// what keeps it that way.
	assert.match(generate, /import FreeGenerationBanner from "\.\/FreeGenerationBanner"/);
	const mounts = generate.match(/<FreeGenerationBanner\b[^>]*\/>/g) || [];
	assert.equal(mounts.length, 1, `expected exactly one mount, found ${mounts.length}`);
	assert.match(mounts[0], /email=\{user\?\.email\}/);
});

test("AuthenticatedApp threads the signed-in user into Generate so resend has an address", () => {
	const authenticatedApp = readFileSync(
		new URL("../src/AuthenticatedApp.jsx", import.meta.url),
		"utf8",
	);
	const genMount = authenticatedApp.match(/<Generate\b[\s\S]*?\/>/);
	assert.ok(genMount, "Generate mount missing from AuthenticatedApp");
	assert.match(genMount[0], /user=\{user\}/);
});

test("Insights.jsx labels the two new funnel stages instead of showing raw keys", () => {
	const insights = readFileSync(
		new URL("../src/components/Insights.jsx", import.meta.url),
		"utf8",
	);
	assert.match(insights, /free_generation_used:\s*'[^']+'/);
	assert.match(insights, /wallet_gate_shown:\s*'[^']+'/);
	// And in the backend's STAGES order — the funnel's step_conversion is
	// computed against the immediately preceding stage, so a label map that
	// implied a different journey would mislabel a real number.
	const order = ["generation_started", "free_generation_used", "wallet_gate_shown", "wallet_connected"];
	const positions = order.map((k) => insights.indexOf(`${k}:`));
	assert.deepEqual(
		positions,
		[...positions].sort((a, b) => a - b),
		"CORE_FUNNEL_LABELS keys must follow the backend STAGES order",
	);
});

// ── 409 wallet-gate panel: human copy, not the agent/curl paragraph (#1658 leftover)

const AGENT_UNLOCK_409 = {
	status: 409,
	detail: {
		reason: "wallet_link_required",
		free_generations_locked_reason: LOCK_EMAIL_UNVERIFIED,
		message:
			"Two ways to generate. (1) Verify your email to unlock 3 free generations on this account — no wallet and no payment needed. We emailed a verification link when you signed up; POST /api/auth/send-verification-email re-sends it. (2) Or link a wallet now (POST /api/wallets/challenge → /api/wallets/verify), fund it with testnet USDC (the faucet currently requires a human), and pay per run. See GET /api/generate/quote for the price.",
	},
};

const AGENT_EXHAUSTED_409 = {
	status: 409,
	detail: {
		reason: "wallet_link_required",
		free_generations_locked_reason: null,
		message:
			"Your free generations are used up. Generation now requires a linked, funded wallet. Link a wallet to your account (POST /api/wallets/challenge → /api/wallets/verify), fund it with testnet USDC (the faucet currently requires a human), then retry. See GET /api/generate/quote for the price.",
	},
};

test("a 409 with email_unverified tells a human to verify, and never dumps POST /api/ paths", () => {
	const view = deriveWalletGateView(AGENT_UNLOCK_409);
	assert.equal(view.kind, "email_unverified");
	assert.equal(view.offerResend, true);
	assert.match(view.title, /Verify your email/i);
	assert.match(view.message, /no wallet and no payment needed/i);
	assert.doesNotMatch(view.title + view.message, /POST \/api\//);
	assert.doesNotMatch(view.message, /wallets\/challenge/);
	assert.doesNotMatch(view.message, /send-verification-email/);
});

test("a 409 with no lock (exhausted free tier) tells a human to link a wallet, not to verify", () => {
	const view = deriveWalletGateView(AGENT_EXHAUSTED_409);
	assert.equal(view.kind, "exhausted");
	assert.equal(view.offerResend, false);
	assert.match(view.title, /Wallet link required/i);
	assert.match(view.message, /Link a wallet/i);
	assert.doesNotMatch(view.message, /Verify your email/i);
	assert.doesNotMatch(view.title + view.message, /POST \/api\//);
});

test("an unknown lock reason still offers the wallet path, never a fabricated verify instruction", () => {
	const view = deriveWalletGateView({
		status: 409,
		detail: {
			reason: "wallet_link_required",
			free_generations_locked_reason: "region_blocked",
			message: "POST /api/wallets/challenge",
		},
	});
	assert.equal(view.kind, "exhausted");
	assert.equal(view.offerResend, false);
	assert.doesNotMatch(view.message, /Verify your email/i);
	assert.doesNotMatch(view.message, /POST \/api\//);
});

test("deriveWalletGateView returns null for anything that is not the wallet-link 409", () => {
	assert.equal(deriveWalletGateView(null), null);
	assert.equal(deriveWalletGateView({ status: 402, detail: { reason: "payment_required" } }), null);
	assert.equal(deriveWalletGateView({ status: 409, detail: { reason: "some_other_conflict" } }), null);
	assert.equal(deriveWalletGateView({ status: 409 }), null);
});

test("Generate.jsx paints the 409 panel from deriveWalletGateView, not paymentErrorMessage", () => {
	assert.match(generate, /deriveWalletGateView\(/);
	assert.match(generate, /walletGateView\?\.title/);
	assert.match(generate, /walletGateView\?\.message/);
	assert.match(generate, /walletGateView\?\.offerResend/);
	// The 409 branch used to interpolate paymentMessage, which is the agent
	// paragraph. That interpolation must not survive in the wallet-gate panel.
	const gateStart = generate.indexOf("PAYMENT_STATUS.WALLET_LINK_REQUIRED ?");
	assert.ok(gateStart !== -1, "409 panel branch missing");
	const gateEnd = generate.indexOf("PAYMENT_STATUS.DRY_RUN", gateStart);
	const gateSlice = generate.slice(gateStart, gateEnd);
	assert.doesNotMatch(gateSlice, /\{paymentMessage\}/);
	assert.doesNotMatch(gateSlice, /paymentErrorMessage/);
	assert.doesNotMatch(gateSlice, /POST \/api\//);
});

test("the locked banner mounts a resend control, gated on the locked state", () => {
	assert.match(banner, /import ResendVerificationControl from "\.\/ResendVerificationControl"/);
	assert.match(banner, /view\.state === ["']locked["'] && <ResendVerificationControl email=\{email\} \/>/);
});

test("resend success copy is honest — requested, not confirmed delivered — and matches Account Settings", () => {
	const accountSettings = readFileSync(
		new URL("../src/components/AccountSettings.jsx", import.meta.url),
		"utf8",
	);
	const settingsMsg = accountSettings.match(
		/const VERIFICATION_REQUESTED_MESSAGE = "([^"]+)"/,
	);
	assert.ok(settingsMsg, "AccountSettings honesty constant missing");
	assert.equal(VERIFICATION_REQUESTED_MESSAGE, settingsMsg[1]);
	assert.match(VERIFICATION_REQUESTED_MESSAGE, /requested/i);
	assert.match(VERIFICATION_REQUESTED_MESSAGE, /(isn't|is not|not) confirmed/i);
	assert.doesNotMatch(VERIFICATION_REQUESTED_MESSAGE, /email (was )?sent successfully/i);
});

test("ResendVerificationControl POSTs via the shared auth-client and renders nothing without an email", () => {
	const control = readFileSync(
		new URL("../src/components/ResendVerificationControl.jsx", import.meta.url),
		"utf8",
	);
	assert.match(control, /resendVerificationEmail/);
	assert.match(control, /from ["']\.\.\/auth-client["']/);
	assert.match(control, /if \(!email\) return null/);
	assert.match(control, /VERIFICATION_REQUESTED_MESSAGE/);
	assert.doesNotMatch(control, /email (was )?sent successfully/i);
	assert.doesNotMatch(control, /email has been sent/i);
});
