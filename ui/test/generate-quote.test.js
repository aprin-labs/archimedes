import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	DEFAULT_DEPTH,
	DEPTH_OPTIONS,
	PAYMENT_STATUS,
	buildPaymentSignatureHeader,
	buildTransferAuthorizationTypedData,
	decodePaymentRequiredHeader,
	deriveQuoteView,
	derivePaymentRequirements,
	derivePaymentState,
	describePayerMismatch,
	extractPaymentRequiredHeader,
	extractReceipt,
	isPaywallError,
	isWalletLinkRequiredError,
	paymentErrorMessage,
	primaryLinkedWallet,
	requirementChainId,
	selectGatewayRequirement,
	startErrorMessage,
} from "../src/generateQuote.js";

// The REAL 402 requirements — decoded from an actual PAYMENT-REQUIRED header
// produced by calling backend/archimedes/marketplace/payments.py's
// get_gateway_middleware(...).require("2.00", "/api/generate/start")
// directly (circlekit's create_gateway_middleware underneath), not
// hand-authored. This is the drift guard: the parser below is built against
// this fixture, not against prose. See the fixture file's own _comment for
// the exact regeneration recipe.
const fixture = JSON.parse(
	readFileSync(new URL("./fixtures/payment-required-402.json", import.meta.url), "utf8"),
);
const fixtureRequirement = fixture.accepts[0];

/** Re-encode (a subset of) the fixture as a PAYMENT-REQUIRED header value —
 * base64-JSON, same encoding generateQuote.js's toBase64/fromBase64 use.
 * Excludes the fixture file's own `_comment` field (not part of the wire
 * shape) so decode-and-compare tests don't have to special-case it. */
function encodeFixtureHeader({ x402Version = fixture.x402Version, resource = fixture.resource, accepts = fixture.accepts } = {}) {
	return globalThis.Buffer.from(JSON.stringify({ x402Version, resource, accepts }), "utf-8").toString("base64");
}

// ── deriveQuoteView: shapes the ratified GET /api/generate/quote response
// (#1296) — payment_required, pricing_model, price, asset, chain,
// recipient, dry_run, how. NO quote_id, NO expires_at, NO breakdown: the
// PROPOSED contract this replaced had all three; the ratified shape has
// none of them. ─────────────────────────────────────────────────────────

test("quote renders: deriveQuoteView shapes the ratified fields", () => {
	const view = deriveQuoteView({
		payment_required: true,
		pricing_model: "flat_v1",
		price: "$0.150000",
		asset: "USDC",
		chain: "eip155:5042002",
		recipient: "0xRecipient",
		dry_run: true,
		how: "POST /api/generate/start without a Payment-Signature header returns 402...",
	});
	assert.equal(view.paymentRequired, true);
	assert.equal(view.pricingModel, "flat_v1");
	assert.equal(view.price, "$0.150000");
	assert.equal(view.asset, "USDC");
	assert.equal(view.chain, "eip155:5042002");
	assert.equal(view.recipient, "0xRecipient");
	assert.equal(view.dryRun, true);
	assert.match(view.how, /Payment-Signature/);
});

test("quote renders: null quote and a null recipient degrade cleanly, not a crash", () => {
	assert.equal(deriveQuoteView(null), null);
	const view = deriveQuoteView({
		payment_required: false,
		pricing_model: "flat_v1",
		price: "$0.150000",
		asset: "USDC",
		chain: "eip155:5042002",
		recipient: null,
		dry_run: true,
		how: "...",
	});
	assert.equal(view.recipient, null);
	assert.equal(view.paymentRequired, false);
});

test("quote renders: the ratified shape carries no quote_id, expires_at, or breakdown", () => {
	// The PROPOSED contract's dead fields must not leak through even if a
	// stale/misbehaving backend sent them — deriveQuoteView only reads the
	// ratified field names.
	const view = deriveQuoteView({
		payment_required: true,
		pricing_model: "flat_v1",
		price: "$0.150000",
		asset: "USDC",
		chain: "eip155:5042002",
		recipient: null,
		dry_run: false,
		how: "...",
		quote_id: "qt_shouldnt_survive",
		expires_at: "2026-08-19T18:05:00Z",
		breakdown: [{ label: "should not survive" }],
	});
	assert.equal("quoteId" in view, false);
	assert.equal("expiresAt" in view, false);
	assert.equal("breakdown" in view, false);
});

// ── isPaywallError / isWalletLinkRequiredError: the two gate guards ───────

test("402 state: isPaywallError recognizes only a genuine 402", () => {
	assert.equal(isPaywallError({ status: 402 }), true);
	assert.equal(isPaywallError({ status: 500 }), false);
	assert.equal(isPaywallError({ status: 409 }), false);
	assert.equal(isPaywallError(null), false);
	assert.equal(isPaywallError(undefined), false);
	assert.equal(isPaywallError({}), false);
});

test("409 state: isWalletLinkRequiredError recognizes only the wallet_link_required reason, not any 409", () => {
	assert.equal(
		isWalletLinkRequiredError({ status: 409, detail: { reason: "wallet_link_required" } }),
		true,
	);
	// A 409 for some other reason must NOT be mistaken for this precondition.
	assert.equal(
		isWalletLinkRequiredError({ status: 409, detail: { reason: "some_other_conflict" } }),
		false,
	);
	assert.equal(isWalletLinkRequiredError({ status: 409 }), false);
	assert.equal(isWalletLinkRequiredError({ status: 402, detail: { reason: "wallet_link_required" } }), false);
	assert.equal(isWalletLinkRequiredError(null), false);
});

// ── derivePaymentState: routes an error + the quote's dry_run flag into a
// PAYMENT_STATUS ────────────────────────────────────────────────────────

test("derivePaymentState: 409 wallet_link_required routes to WALLET_LINK_REQUIRED regardless of dry_run", () => {
	const err = { status: 409, detail: { reason: "wallet_link_required" } };
	assert.equal(derivePaymentState(err, true), PAYMENT_STATUS.WALLET_LINK_REQUIRED);
	assert.equal(derivePaymentState(err, false), PAYMENT_STATUS.WALLET_LINK_REQUIRED);
});

test("derivePaymentState: a 402 routes to DRY_RUN or LIVE_UNAVAILABLE by the quote's dry_run flag", () => {
	const err = { status: 402, detail: { reason: "payment_required" } };
	assert.equal(derivePaymentState(err, true), PAYMENT_STATUS.DRY_RUN);
	assert.equal(derivePaymentState(err, false), PAYMENT_STATUS.LIVE_UNAVAILABLE);
});

test("derivePaymentState: anything else fails closed to NONE, never a payment-specific state", () => {
	assert.equal(derivePaymentState({ status: 500 }, true), PAYMENT_STATUS.NONE);
	assert.equal(derivePaymentState({ status: 404 }, true), PAYMENT_STATUS.NONE);
	assert.equal(derivePaymentState(null, true), PAYMENT_STATUS.NONE);
});

// ── paymentErrorMessage: renders the backend's message verbatim ───────────

test("paymentErrorMessage: renders detail.message verbatim, falls back only when absent", () => {
	assert.equal(
		paymentErrorMessage({ detail: { message: "fund it with testnet USDC (the faucet currently requires a human)" } }),
		"fund it with testnet USDC (the faucet currently requires a human)",
	);
	assert.equal(paymentErrorMessage({}, "fallback"), "fallback");
	assert.equal(paymentErrorMessage(null), "Payment step failed.");
});

// ── startErrorMessage: the four written backend error messages issue #1363
// found discarded — both detail SHAPES (dict and plain string) must render
// verbatim, never the bare "Backend returned <status>" echo. ─────────────

test("startErrorMessage: dict-shape detail (daily-cap 429, generation_quota.py) renders detail.message verbatim", () => {
	const err = {
		status: 429,
		detail: {
			message:
				"You've reached today's generation limit (10/day for this account). The allowance resets daily — or reach out if you need more.",
			reason: "generation_daily_cap",
			scope: "user",
			cap: 10,
		},
	};
	assert.equal(
		startErrorMessage(err),
		"You've reached today's generation limit (10/day for this account). The allowance resets daily — or reach out if you need more.",
	);
	assert.doesNotMatch(startErrorMessage(err), /^Backend returned /);
});

test("startErrorMessage: dict-shape detail (quota-unavailable 503, generation_quota.py) renders detail.message verbatim", () => {
	const err = {
		status: 503,
		detail: {
			message:
				"Generation is temporarily unavailable — the usage-limit service could not be reached. Nothing was counted against your allowance.",
			reason: "generation_quota_unavailable",
		},
	};
	assert.equal(
		startErrorMessage(err),
		"Generation is temporarily unavailable — the usage-limit service could not be reached. Nothing was counted against your allowance.",
	);
	assert.doesNotMatch(startErrorMessage(err), /^Backend returned /);
});

test("startErrorMessage: plain-string detail (burst-limit 429, slowapi convention) renders verbatim", () => {
	const err = { status: 429, detail: "Rate limit exceeded. Please slow down and try again later." };
	assert.equal(startErrorMessage(err), "Rate limit exceeded. Please slow down and try again later.");
	assert.doesNotMatch(startErrorMessage(err), /^Backend returned /);
});

test("startErrorMessage: plain-string detail (401, account_auth.py) renders verbatim", () => {
	const err = { status: 401, detail: "Authentication required" };
	assert.equal(startErrorMessage(err), "Authentication required");
	assert.doesNotMatch(startErrorMessage(err), /^Backend returned /);
});

test("startErrorMessage: falls back honestly, never crashes, on a missing/malformed detail", () => {
	// No `err.status` (e.g. a network TypeError) → the bare fallback, nothing to name.
	assert.equal(startErrorMessage(null, "fallback text"), "fallback text");
	assert.equal(startErrorMessage({}), "Failed to start generation");
	// A detail object with no usable `message` string must not crash or leak
	// [object Object] — falls back same as an absent detail.
	assert.equal(startErrorMessage({ detail: { reason: "something" } }, "fallback"), "fallback");
});

test("startErrorMessage: fallback NAMES the HTTP status when known, never the bare status-echo issue #1363 fixed", () => {
	// This is the property the function's own JSDoc claims: a detail-less
	// failure (nginx 502 HTML body, a bare 500) must still be more diagnostic
	// than the old `e.message` echo, not strictly less. A build that just
	// returns the caller's literal fallback (dropping the status) fails this.
	assert.equal(startErrorMessage({ status: 500 }, "fallback text"), "fallback text (HTTP 500)");
	const msg = startErrorMessage({ status: 502 }, "Failed to start generation");
	assert.match(msg, /502/);
	assert.doesNotMatch(msg, /^Backend returned /);
});

// ── DEPTH_OPTIONS: must equal the pipeline's actually enforced range,
// never a superset the pipeline silently clamps. ──────────────────────────

test("DEPTH_OPTIONS: inside the enforced [MIN_PAPERS, FUSION_MAX_PAPERS] range, and no 2 or 3", () => {
	// #1636 raised FUSION_MAX_PAPERS 6 → 30 and dropped 2 and 3 from the
	// picker. The #1363 contract is unchanged in substance: never offer a
	// value the pipeline would silently clamp.
	assert.deepEqual(DEPTH_OPTIONS, [5, 8, 12, 20, 30]);
	for (const n of DEPTH_OPTIONS) {
		assert.ok(n >= 2 && n <= 30, `${n} is outside the enforced [2, 30] range`);
	}
	// The cheapest single fix in #1636: the two values that hand the
	// synthesizer barely more than the hard floor and then let the resulting
	// citation count read as evidence depth.
	assert.ok(!DEPTH_OPTIONS.includes(2) && !DEPTH_OPTIONS.includes(3));
	// The default is offered, and sits ABOVE the fuse target (5) so the model
	// is never asked to cite every paper it was shown.
	assert.ok(DEPTH_OPTIONS.includes(DEFAULT_DEPTH));
	assert.ok(DEFAULT_DEPTH > 5);
});

// ── primaryLinkedWallet / describePayerMismatch: payer binding ────────────

test("primaryLinkedWallet: picks the wallet flagged primary, or the first if none is", () => {
	assert.equal(primaryLinkedWallet([]), null);
	assert.equal(primaryLinkedWallet(null), null);
	const wallets = [
		{ address: "0xAAA", is_primary: false },
		{ address: "0xBBB", is_primary: true },
	];
	assert.equal(primaryLinkedWallet(wallets).address, "0xBBB");
	assert.equal(
		primaryLinkedWallet([{ address: "0xCCC", is_primary: false }]).address,
		"0xCCC",
	);
});

test("describePayerMismatch: null when the active wallet IS one of the linked wallets (case-insensitive, either side mixed-case)", () => {
	const wallets = [{ address: "0xabcdef0000000000000000000000000000001", is_primary: true }];
	// The ACTIVE address (not just the linked one) is mixed-case here — both
	// sides must be normalized, not just the linked-wallet side.
	assert.equal(describePayerMismatch("0xAbCdEf0000000000000000000000000000001", wallets), null);
});

test("describePayerMismatch: null when there's nothing to compare (no active address, or no linked wallets)", () => {
	assert.equal(describePayerMismatch(null, [{ address: "0xAAA", is_primary: true }]), null);
	assert.equal(describePayerMismatch("0xAAA", []), null);
	assert.equal(describePayerMismatch("0xAAA", null), null);
});

test("describePayerMismatch: flags the primary linked wallet when the active address isn't any linked one", () => {
	const wallets = [
		{ address: "0xAAA", is_primary: false },
		{ address: "0xBBB", is_primary: true },
	];
	const result = describePayerMismatch("0xCCC", wallets);
	assert.deepEqual(result, { active: "0xCCC", linked: "0xBBB" });
});

// ── Real x402: parsing the PAYMENT-REQUIRED header against the fixture ────
//
// The fixture (ui/test/fixtures/payment-required-402.json) came from a REAL
// call to backend/archimedes/marketplace/payments.py's get_gateway_middleware
// (circlekit underneath) — this is the drift guard the task calls for: the
// parser is built against the actual wire shape, not prose.

test("decodePaymentRequiredHeader: round-trips the fixture through base64", () => {
	const header = encodeFixtureHeader();
	const decoded = decodePaymentRequiredHeader(header);
	assert.equal(decoded.x402Version, 2);
	assert.deepEqual(decoded.resource, fixture.resource);
	assert.deepEqual(decoded.accepts, fixture.accepts);
});

test("decodePaymentRequiredHeader: null on a missing/malformed header, never a throw", () => {
	assert.equal(decodePaymentRequiredHeader(null), null);
	assert.equal(decodePaymentRequiredHeader(""), null);
	assert.equal(decodePaymentRequiredHeader("not-valid-base64-json!!!"), null);
	// Valid base64 that isn't valid JSON underneath.
	assert.equal(decodePaymentRequiredHeader(globalThis.Buffer.from("not json", "utf-8").toString("base64")), null);
});

test("selectGatewayRequirement: picks the fixture's GatewayWalletBatched entry", () => {
	const parsed = decodePaymentRequiredHeader(encodeFixtureHeader());
	const requirement = selectGatewayRequirement(parsed);
	assert.deepEqual(requirement, fixtureRequirement);
});

test("selectGatewayRequirement: null when accepts is missing/empty, or nothing matches", () => {
	assert.equal(selectGatewayRequirement({}), null);
	assert.equal(selectGatewayRequirement({ accepts: [] }), null);
	assert.equal(
		selectGatewayRequirement({ accepts: [{ extra: { name: "SomeOtherScheme" } }] }),
		null,
	);
});

test("derivePaymentRequirements: happy path against the fixture — requirements, resource, x402Version, no error", () => {
	const result = derivePaymentRequirements(encodeFixtureHeader());
	assert.deepEqual(result.requirements, fixtureRequirement);
	assert.deepEqual(result.resource, fixture.resource);
	assert.equal(result.x402Version, 2);
	assert.equal(result.error, null);
});

test("derivePaymentRequirements: header_missing_or_malformed on a missing/malformed header", () => {
	const result = derivePaymentRequirements(null);
	assert.equal(result.requirements, null);
	assert.equal(result.resource, null);
	assert.equal(result.error, "header_missing_or_malformed");
});

test("derivePaymentRequirements: no_gateway_option when the header parses but has no matching accepts entry", () => {
	const header = encodeFixtureHeader({ accepts: [{ scheme: "exact", extra: { name: "SomeOtherScheme" } }] });
	const result = derivePaymentRequirements(header);
	assert.equal(result.requirements, null);
	assert.equal(result.error, "no_gateway_option");
	// resource/x402Version still surfaced — they don't depend on which option matched.
	assert.deepEqual(result.resource, fixture.resource);
	assert.equal(result.x402Version, 2);
});

test("requirementChainId: parses the fixture's eip155 network, null on anything else", () => {
	assert.equal(requirementChainId(fixtureRequirement), 5042002);
	assert.equal(requirementChainId({ network: "not-eip155" }), null);
	assert.equal(requirementChainId({}), null);
	assert.equal(requirementChainId(null), null);
});

// ── buildTransferAuthorizationTypedData: the EIP-712 payload, against the
// fixture's real requirement (GatewayWalletBatched domain, not USDC's) ────

test("buildTransferAuthorizationTypedData: domain is the GATEWAY's EIP-712 domain, from the fixture", () => {
	const { domain } = buildTransferAuthorizationTypedData(fixtureRequirement, {
		from: "0xPayerAddress",
		nowSec: 1_000_000,
		nonceHex: "0xaa".padEnd(66, "0"),
	});
	assert.deepEqual(domain, {
		name: "GatewayWalletBatched",
		version: "1",
		chainId: 5042002,
		verifyingContract: "0x0077777d7EBA4688BDeF3E311b846F25870A19B9",
	});
});

test("buildTransferAuthorizationTypedData: message uses bigints for uint256 fields (signing requires them, strings sign the wrong hash)", () => {
	const nonceHex = "0xaa".padEnd(66, "0");
	const { message } = buildTransferAuthorizationTypedData(fixtureRequirement, {
		from: "0xPayerAddress",
		nowSec: 1_000_000,
		nonceHex,
	});
	assert.equal(message.value, 2_000_000n);
	assert.equal(typeof message.value, "bigint");
	assert.equal(message.validAfter, 1_000_000n - 600n);
	// maxTimeoutSeconds + the 24h skew margin — the facilitator measures
	// remaining validity against ITS clock with zero grace (probed live
	// 2026-08-21: a client clock 30s behind fails without the margin).
	assert.equal(message.validBefore, 1_000_000n + 345_600n + 86_400n);
	assert.equal(message.nonce, nonceHex);
	assert.equal(message.to, fixtureRequirement.payTo);
});

test("buildTransferAuthorizationTypedData: authorization (the wire payload) uses STRINGS, nonce as 0x-hex", () => {
	const nonceHex = "0xaa".padEnd(66, "0");
	const { authorization } = buildTransferAuthorizationTypedData(fixtureRequirement, {
		from: "0xPayerAddress",
		nowSec: 1_000_000,
		nonceHex,
	});
	assert.equal(authorization.from, "0xPayerAddress");
	assert.equal(authorization.to, fixtureRequirement.payTo);
	assert.equal(authorization.value, "2000000");
	assert.equal(authorization.validAfter, String(1_000_000 - 600));
	assert.equal(authorization.validBefore, String(1_000_000 + 345_600 + 86_400));
	assert.equal(authorization.nonce, nonceHex);
	for (const key of ["from", "to", "value", "validAfter", "validBefore", "nonce"]) {
		assert.equal(typeof authorization[key], "string", `authorization.${key} must be a string`);
	}
	assert.match(authorization.nonce, /^0x[0-9a-f]{64}$/);
});

test("buildTransferAuthorizationTypedData: types shape is exactly TransferWithAuthorization", () => {
	const { types, primaryType } = buildTransferAuthorizationTypedData(fixtureRequirement, {
		from: "0xPayerAddress",
		nowSec: 1_000_000,
		nonceHex: "0xaa".padEnd(66, "0"),
	});
	assert.equal(primaryType, "TransferWithAuthorization");
	assert.deepEqual(types.TransferWithAuthorization.map((f) => f.name), [
		"from",
		"to",
		"value",
		"validAfter",
		"validBefore",
		"nonce",
	]);
});

// ── buildPaymentSignatureHeader: the outbound Payment-Signature header ────

test("buildPaymentSignatureHeader: builds a header from the fixture + a dummy signature; decoding it round-trips the shape", () => {
	const nonceHex = "0xaa".padEnd(66, "0");
	const { authorization } = buildTransferAuthorizationTypedData(fixtureRequirement, {
		from: "0xPayerAddress",
		nowSec: 1_000_000,
		nonceHex,
	});
	const header = buildPaymentSignatureHeader({
		x402Version: fixture.x402Version,
		resource: fixture.resource,
		requirements: fixtureRequirement,
		authorization,
		signature: "0xDEADBEEF",
	});
	const decoded = JSON.parse(globalThis.Buffer.from(header, "base64").toString("utf8"));

	assert.deepEqual(Object.keys(decoded).sort(), ["accepted", "payload", "resource", "x402Version"]);
	assert.equal(decoded.x402Version, 2);
	assert.deepEqual(decoded.resource, fixture.resource);
	assert.deepEqual(decoded.payload, { authorization, signature: "0xDEADBEEF" });
	// `accepted` echoes the requirements under the wire's field names.
	assert.equal(decoded.accepted.scheme, "exact");
	assert.equal(decoded.accepted.network, "eip155:5042002");
	assert.equal(decoded.accepted.asset, fixtureRequirement.asset);
	assert.equal(decoded.accepted.amount, "2000000");
	assert.equal(decoded.accepted.payTo, fixtureRequirement.payTo);
	assert.equal(decoded.accepted.maxTimeoutSeconds, 345600);
	assert.deepEqual(decoded.accepted.extra, fixtureRequirement.extra);
	// Authorization values are strings, nonce is 0x-hex 32 bytes.
	for (const key of ["from", "to", "value", "validAfter", "validBefore", "nonce"]) {
		assert.equal(typeof decoded.payload.authorization[key], "string");
	}
	assert.match(decoded.payload.authorization.nonce, /^0x[0-9a-f]{64}$/);
});

// ── extractPaymentRequiredHeader: the PAYMENT-REQUIRED counterpart of
// extractReceipt, reading api.js's err.headers ─────────────────────────────

test("extractPaymentRequiredHeader: reads PAYMENT-REQUIRED from a Headers-like object and a plain object, case-insensitively", () => {
	const fakeHeaders = { get: (name) => (name.toLowerCase() === "payment-required" ? "abc123" : null) };
	assert.equal(extractPaymentRequiredHeader(fakeHeaders), "abc123");
	assert.equal(extractPaymentRequiredHeader({ "payment-required": "def456" }), "def456");
	assert.equal(extractPaymentRequiredHeader({ "PAYMENT-REQUIRED": "ghi789" }), "ghi789");
});

test("extractPaymentRequiredHeader: null when absent, never a crash on a missing/empty headers object", () => {
	assert.equal(extractPaymentRequiredHeader(null), null);
	assert.equal(extractPaymentRequiredHeader({}), null);
	const fakeHeaders = { get: () => null };
	assert.equal(extractPaymentRequiredHeader(fakeHeaders), null);
});

// ── extractReceipt: the PAYMENT-RESPONSE settlement receipt ───────────────

test("extractReceipt: reads PAYMENT-RESPONSE from a Headers-like object and a plain object, case-insensitively", () => {
	const fakeHeaders = { get: (name) => (name.toLowerCase() === "payment-response" ? "receipt-123" : null) };
	assert.equal(extractReceipt(fakeHeaders), "receipt-123");
	assert.equal(extractReceipt({ "payment-response": "receipt-456" }), "receipt-456");
	assert.equal(extractReceipt({ "PAYMENT-RESPONSE": "receipt-789" }), "receipt-789");
});

test("extractReceipt: null when absent, never a crash on a missing/empty headers object", () => {
	assert.equal(extractReceipt(null), null);
	assert.equal(extractReceipt({}), null);
	const fakeHeaders = { get: () => null };
	assert.equal(extractReceipt(fakeHeaders), null);
});

// ── Wiring: Generate.jsx actually uses the flag + helpers, not a fork of the
// logic re-implemented inline, and the dead PROPOSED-contract concepts
// (quote_id, expiry, attachQuoteId) are actually gone. Static-source
// checks, matching the pattern already established in
// ui/test/app-visuals.test.js for this file. ──────────────────────────────

const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);

test("Generate.jsx gates the quote card and payment step behind GENERATION_QUOTE_ENABLED", () => {
	assert.match(generate, /from ["']\.\.\/featureFlags["']/);
	assert.match(generate, /GENERATION_QUOTE_ENABLED\s*&&/);
	assert.match(generate, /Paper trading after generation costs nothing/);
});

test("Generate.jsx submits /start through apiPostWithMeta, not the plain apiPost / a fork of the ratified body", () => {
	assert.match(generate, /apiPostWithMeta\(\s*["']\/api\/generate\/start["']/);
	// The PROPOSED contract's quote_id/attachQuoteId must be fully gone from
	// the code (not merely from a doc comment explaining that it's dead) —
	// the ratified /start body carries no payment field at all.
	assert.doesNotMatch(generate, /attachQuoteId/);
	assert.doesNotMatch(generate, /quote_id\s*[:.]/);
});

test("Generate.jsx routes 409/402 through derivePaymentState into the ratified payment-step states", () => {
	assert.match(generate, /derivePaymentState\(/);
	assert.match(generate, /PAYMENT_STATUS\.WALLET_LINK_REQUIRED/);
	assert.match(generate, /PAYMENT_STATUS\.DRY_RUN/);
	assert.match(generate, /PAYMENT_STATUS\.LIVE_UNAVAILABLE/);
	assert.match(generate, /open-wallet-modal/);
});

test("Generate.jsx wires the real x402 payment flow: parsing, signing, and the receipt surface", () => {
	assert.match(generate, /from ["']\.\.\/x402["']/);
	assert.match(generate, /derivePaymentRequirements\(/);
	assert.match(generate, /extractPaymentRequiredHeader\(/);
	assert.match(generate, /signGatewayPayment\(/);
	assert.match(generate, /depositToGateway\(/);
	assert.match(generate, /getGatewayBalance\(/);
	assert.match(generate, /walletSupportsPayment\(/);
	assert.match(generate, /extractReceipt\(/);
});

test("Generate.jsx: the test-mode-only 'continue' button is GONE — replaced by the real pay flow", () => {
	// Anti-goal check (repo convention: verify a forbidden pattern is
	// actually absent, don't just trust the diff). The old flow's function
	// names, state, and button copy must not survive anywhere in the file.
	assert.doesNotMatch(generate, /Continue in test mode/);
	assert.doesNotMatch(generate, /continueInTestMode/);
	assert.doesNotMatch(generate, /continuingTestMode/);
	assert.doesNotMatch(generate, /buildDryRunPaymentHeader/);
	assert.doesNotMatch(generate, /resolveDryRunPayer/);
	assert.doesNotMatch(generate, /testModeNotice/);
	assert.doesNotMatch(generate, /Payments aren't enabled in this build yet/);
});

test("Generate.jsx: the no-receipt success copy is honest — never claims settlement", () => {
	assert.match(generate, /noSettlementNotice/);
	assert.match(generate, /accepted without settlement/i);
	assert.match(generate, /no funds moved/i);
	// Must not describe this state as paid/settled/charged — that's the
	// receipt branch below it, a DIFFERENT state.
	assert.doesNotMatch(generate, /Payment accepted unverified — no real charge/);
});

test("Generate.jsx: one-click payment — deposit folds into the single pay flow (2026-08-21 UX fix)", () => {
	// The separate "Approve & deposit" button is retired: ONE control runs
	// the whole flow, narrating each step. (The click-time refetch moved to
	// the not-yet-funded branch only — the funded tap signs with held
	// requirements so WebKit's activation survives; see
	// webauthn-activation.test.js.)
	assert.doesNotMatch(generate, /Approve & deposit/);
	assert.match(generate, /handlePayAndGenerate/);
	assert.match(generate, /const fresh = held \?\? \(await refreshPaymentRequirements\(\)\)/);
	assert.match(generate, /one-time\s+deposit covers many generations/);
});

test("Generate.jsx: the no-wallet state offers the connect flow (#1298 supersedes the old unsupported-wallet dead end)", () => {
	// The #1427-era "isn't supported for payments yet" message is RETIRED:
	// passkey wallets now sign via their smart account, and the no-connection
	// state must offer the connect modal (see wallet-matrix.test.js).
	assert.doesNotMatch(generate, /isn't\s+supported for payments yet/);
	assert.match(generate, /walletSupportsPayment\(\)/);
});

// ── Issue #1363 wiring pins ─────────────────────────────────────────────

test("Generate.jsx renders the non-payment /start error through startErrorMessage, not the bare status echo", () => {
	assert.match(generate, /startErrorMessage\(/);
	assert.doesNotMatch(generate, /e\.message \|\| "Failed to start generation"/);
});

test("Generate.jsx offers DEPTH_OPTIONS, not a hardcoded superset the pipeline silently clamps", () => {
	assert.match(generate, /DEPTH_OPTIONS\.map/);
});

test("Generate.jsx's depth default is DEFAULT_DEPTH, not a second copy of the number", () => {
	// #1636: the default lived in three places (generate_schemas.py said 5,
	// strategies_routes.py said 4, Generate.jsx said 5), so the same brief
	// retrieved a different amount of evidence per entry point. The UI half
	// now reads the shared constant rather than restating it — including in
	// the "active" badge comparison, which was its own literal 5.
	assert.match(generate, /useState\(DEFAULT_DEPTH\)/);
	assert.match(generate, /depth !== DEFAULT_DEPTH/);
	assert.doesNotMatch(generate, /useState\(5\)/);
});

// ── featureFlags.js: GENERATION_QUOTE_ENABLED now defaults ON ─────────────
// (payment enforcement going live on testnet, Dan's 2026-08-19 directive) —
// still explicitly disable-able via VITE_GENERATION_QUOTE_ENABLED=false.

test("featureFlags.js: GENERATION_QUOTE_ENABLED defaults to enabled (only an explicit \"false\" turns it off)", () => {
	const featureFlagsSrc = readFileSync(new URL("../src/featureFlags.js", import.meta.url), "utf8");
	assert.match(featureFlagsSrc, /VITE_GENERATION_QUOTE_ENABLED\s*!==\s*["']false["']/);
	// The old off-by-default comparison must be gone, not just superseded.
	assert.doesNotMatch(featureFlagsSrc, /VITE_GENERATION_QUOTE_ENABLED\s*===\s*["']true["']/);
});
