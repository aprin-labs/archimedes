// Pure, framework-free helpers for the Generate page's upfront cost-quote +
// x402 paywall step (docs/specs/generation-quote-contract.md — RATIFIED,
// pinned by backend/tests/test_generate_payment_gate.py in #1296;
// feature-flagged via GENERATION_QUOTE_ENABLED in featureFlags.js).
//
// Kept out of Generate.jsx so the state-transition logic — what a 402/409
// from /start means, which linked wallet a payment binds to, how to parse
// the PAYMENT-REQUIRED header and build the EIP-712 payload/wire header —
// is unit testable without a DOM or a wallet. This repo's `ui/test` has no
// jsdom / testing-library; see ui/test/routes.test.js for the established
// pattern of testing pure logic directly rather than rendered markup.
//
// Real x402 signing is wired (Dan's 2026-08-19 directive: payment
// enforcement at $2.00/generation is live on testnet). The parser below is
// built against a REAL 402 response — ui/test/fixtures/payment-required-402.json,
// produced by calling backend/archimedes/marketplace/payments.py's
// get_gateway_middleware(...).require(...) directly and decoding the
// PAYMENT-REQUIRED header it returns — not against prose. The effectful
// half (reading the Gateway balance, running approve+deposit, calling
// walletClient.signTypedData) lives in ../x402.js.

/** Lifecycle of the GET /api/generate/quote fetch. */
export const QUOTE_STATUS = {
	IDLE: "idle",
	LOADING: "loading",
	READY: "ready",
	ERROR: "error",
};

/**
 * What the payment step shows once /start responds 402 or 409 (paywall
 * gate active). WALLET_LINK_REQUIRED is the 409 precondition; DRY_RUN and
 * LIVE_UNAVAILABLE are the two possible 402s, distinguished by the quote's
 * `dry_run` flag — the backend's PAYMENTS_DRY_RUN mirror (#1296).
 */
export const PAYMENT_STATUS = {
	NONE: "none",
	WALLET_LINK_REQUIRED: "wallet-link-required",
	// PAYMENTS_DRY_RUN on the backend: any non-empty Payment-Signature header
	// is accepted WITHOUT verify/settle. Real functionality (the job actually
	// starts), honestly labeled — never dressed up as a real settlement.
	DRY_RUN: "dry-run",
	// Live paywall, but this build doesn't sign real x402 payments yet —
	// render the honest preview rather than a pay button that doesn't work.
	LIVE_UNAVAILABLE: "live-unavailable",
};

/**
 * Shape a raw GET /api/generate/quote response into what the quote card
 * needs to render. Returns null for a missing/falsy quote (nothing to
 * show yet). Ratified shape (#1296): payment_required, pricing_model,
 * price (a "$X.XXXXXX" decimal string, not a float), asset, chain,
 * recipient, dry_run, how. There is no quote_id and no expires_at — the
 * PROPOSED contract this replaced had both; the ratified one has neither
 * (the price is flat and re-quoted fresh on every /start attempt, so
 * there's nothing to echo back or time out).
 */
export function deriveQuoteView(quote) {
	if (!quote) return null;
	return {
		paymentRequired: Boolean(quote.payment_required),
		pricingModel: quote.pricing_model ?? null,
		price: quote.price,
		asset: quote.asset,
		chain: quote.chain,
		recipient: quote.recipient ?? null,
		dryRun: Boolean(quote.dry_run),
		how: quote.how ?? "",
	};
}

/**
 * True only for a genuine HTTP 402 as thrown by api.js's apiPost /
 * apiPostWithMeta (they set `err.status` from the response). Guards
 * against treating a generic network failure or a 5xx as "payment
 * required".
 */
export function isPaywallError(err) {
	return Boolean(err) && err.status === 402;
}

/**
 * True only for the wallet-link-required 409 — checked via the error
 * body's `detail.reason` (api.js attaches the parsed FastAPI error body as
 * `err.detail`), not just the status code: a bare 409 status alone isn't
 * enough — some other future 409 on this endpoint must not be mistaken
 * for this specific precondition.
 */
export function isWalletLinkRequiredError(err) {
	return Boolean(err) && err.status === 409 && err.detail?.reason === "wallet_link_required";
}

/**
 * Classify a /start error into a payment-step state. Anything that isn't
 * the wallet-link 409 or a genuine 402 fails closed to NONE — callers
 * fall through to the generic error banner for those, never a
 * payment-specific one.
 */
export function derivePaymentState(err, quoteDryRun) {
	if (isWalletLinkRequiredError(err)) return PAYMENT_STATUS.WALLET_LINK_REQUIRED;
	if (isPaywallError(err)) return quoteDryRun ? PAYMENT_STATUS.DRY_RUN : PAYMENT_STATUS.LIVE_UNAVAILABLE;
	return PAYMENT_STATUS.NONE;
}

/**
 * The human-readable message the backend attached to a 402/409 error body
 * (`detail.message` — #1296's shape). Falls back to a generic string so a
 * malformed or absent detail never crashes the banner; never fabricates
 * backend wording otherwise — the faucet caveat on the 409 in particular
 * must render verbatim, not be re-paraphrased.
 */
export function paymentErrorMessage(err, fallback) {
	return err?.detail?.message || fallback || "Payment step failed.";
}

/**
 * The human-readable message the backend attached to a /start failure that
 * is NOT a payment-gate response (402/409 — those route through
 * paymentErrorMessage instead: quota 429s, the burst-limit 429, and auth
 * 401s land here). `err.detail` (api.js's apiPostWithMeta attaches the
 * parsed body's `detail` field, see #1296) arrives in one of two shapes on
 * this endpoint:
 *   - a dict `{message, reason, ...}` — generation_quota.py's daily-cap and
 *     quota-unavailable 429/503s.
 *   - a plain string — the slowapi burst-limit 429 and the 401's
 *     "Authentication required", both FastAPI/slowapi convention.
 * Either shape renders VERBATIM — same rule paymentErrorMessage's docstring
 * states for the faucet caveat, never re-worded. When detail is absent or
 * malformed, falls back to `fallback` (default "Failed to start
 * generation") — appending the HTTP status when `err.status` is known, so
 * the fallback is itself a short, honest sentence naming the status, never
 * the bare `Backend returned <status>` echo api.js's Error.message carries.
 * This never crashes and never regresses to the status-echo issue #1363
 * fixed.
 */
export function startErrorMessage(err, fallback) {
	const detail = err?.detail;
	if (typeof detail === "string" && detail.trim()) return detail;
	if (detail && typeof detail === "object" && typeof detail.message === "string" && detail.message.trim()) {
		return detail.message;
	}
	const base = fallback || "Failed to start generation";
	return err?.status ? `${base} (HTTP ${err.status})` : base;
}

/**
 * The Depth control's offered values — how many papers the pipeline
 * RETRIEVES and shows the model, not how many it must cite.
 *
 * Every value must sit inside the pipeline's actually enforced range,
 * MIN_PAPERS..FUSION_MAX_PAPERS in
 * backend/archimedes/agents/strategy_fusion.py (2..30 since #1636) — never a
 * superset. Issue #1363: the UI used to offer 8 and 10 when the ceiling was
 * 6, both silently clamped; the drift guard for the backend half of this
 * contract lives in backend/tests/test_generate_schemas_depth_drift.py.
 *
 * Issue #1636 dropped 2 and 3 from the picker. They are still ACCEPTED by the
 * API (MIN_PAPERS is 2 and stays 2 — a thin corpus must degrade to a narrow
 * strategy, not to a failed generation), but offering them as a *choice*
 * asked the user to hand the synthesizer two abstracts and then reads the
 * resulting two-paper citation list as evidence depth. The picker now starts
 * at the number we actually ask the model to fuse (FUSE_TARGET_MIN = 5).
 */
export const DEPTH_OPTIONS = [5, 8, 12, 20, 30];

/**
 * The default Depth, matching strategy_fusion.DEFAULT_MAX_PAPERS and
 * GenerateBrief.max_papers server-side (8). Deliberately above
 * FUSE_TARGET_MIN (5): showing the model exactly as many papers as we ask it
 * to cite leaves it no room to reject one honestly.
 */
export const DEFAULT_DEPTH = 8;

/**
 * The account's PRIMARY linked wallet from a GET /api/wallets response
 * (list of LinkedWalletResponse) — or the first entry if none is flagged
 * primary. Null for an empty/missing list.
 */
export function primaryLinkedWallet(linkedWallets) {
	if (!Array.isArray(linkedWallets) || linkedWallets.length === 0) return null;
	return linkedWallets.find((w) => w?.is_primary) ?? linkedWallets[0];
}

/**
 * Detect a payer-binding mismatch worth flagging: the wallet currently
 * active in the injected provider (config.js's getAddress()) is NOT any
 * of the account's linked wallets. Returns null when there's nothing to
 * flag — no active address, no linked wallets yet (the 409 CTA already
 * covers that case), or the active address IS one of the linked wallets
 * (a payment from it resolves to itself server-side, #1296's
 * get_linked_wallet_address — no mismatch).
 *
 * This is a transparency check, not the enforcement gate: the backend is
 * the actual authority (payer_mismatch, #1296) — this only lets the UI
 * warn BEFORE a signing attempt would fail, using the LINKED wallet's
 * account rather than blindly trusting whatever's active in the provider.
 */
export function describePayerMismatch(activeAddress, linkedWallets) {
	const active = (activeAddress || "").trim().toLowerCase();
	const wallets = Array.isArray(linkedWallets) ? linkedWallets : [];
	if (!active || wallets.length === 0) return null;
	const activeIsLinked = wallets.some((w) => (w?.address || "").toLowerCase() === active);
	if (activeIsLinked) return null;
	const primary = primaryLinkedWallet(wallets);
	return { active: activeAddress, linked: primary.address };
}

/**
 * Base64-JSON, matching circlekit's wire encoding for both the
 * PAYMENT-REQUIRED and Payment-Signature headers (see
 * backend/tests/test_generate_payment_gate.py's `_payment_header`, and
 * circlekit.x402.PaymentPayload.to_header) — portable across the browser
 * (btoa) and the node:test runner (Buffer).
 */
function toBase64(str) {
	if (typeof btoa === "function") return btoa(str);
	// globalThis.Buffer (not a bare `Buffer` reference) so this line is
	// statically valid under the browser-only eslint globals this repo lints
	// with — the branch itself only ever runs under node:test, where
	// globalThis.Buffer is the real Node Buffer.
	return globalThis.Buffer.from(str, "utf-8").toString("base64");
}

/** The atob complement of toBase64 — same browser/node:test portability. */
function fromBase64(str) {
	if (typeof atob === "function") return atob(str);
	return globalThis.Buffer.from(str, "base64").toString("utf-8");
}

// ─── Real x402 payment: PAYMENT-REQUIRED parsing + EIP-712 payload building ───
//
// The name circlekit's Gateway-batched payment option carries in every
// requirement's `extra.name` — pinned by the fixture
// (ui/test/fixtures/payment-required-402.json) and by circlekit's own
// CIRCLE_BATCHING_NAME constant.
const GATEWAY_EXTRA_NAME = "GatewayWalletBatched";

/**
 * Decode the PAYMENT-REQUIRED header's base64-JSON payload. Returns null on
 * a missing/malformed header — never throws — so callers render an honest
 * failure state instead of crashing. Shape (pinned by the fixture):
 * {x402Version, resource: {url, description, mimeType}, accepts: [...]}.
 */
export function decodePaymentRequiredHeader(headerValue) {
	if (!headerValue) return null;
	try {
		return JSON.parse(fromBase64(headerValue));
	} catch {
		return null;
	}
}

/**
 * Pick the first `accepts` entry this UI knows how to pay: Circle's Gateway
 * batched EIP-712 scheme (`extra.name === "GatewayWalletBatched"`). Returns
 * null when `accepts` is missing/empty or nothing matches — the caller must
 * render an honest "no supported payment method" state, never fabricate one.
 */
export function selectGatewayRequirement(parsedHeader) {
	const accepts = Array.isArray(parsedHeader?.accepts) ? parsedHeader.accepts : [];
	return accepts.find((a) => a?.extra?.name === GATEWAY_EXTRA_NAME) ?? null;
}

/**
 * Decode + select in one step — what Generate.jsx calls once a 402's error
 * carries the PAYMENT-REQUIRED header value (see extractPaymentRequiredHeader
 * below). Never throws: `error` is a short machine reason string when
 * nothing payable was found, and `requirements` is null in that case;
 * `resource`/`x402Version` are still surfaced when the header parsed but had
 * no matching option, since they don't depend on which option was chosen.
 */
export function derivePaymentRequirements(headerValue) {
	const parsed = decodePaymentRequiredHeader(headerValue);
	if (!parsed) {
		return { requirements: null, resource: null, x402Version: null, error: "header_missing_or_malformed" };
	}
	const requirements = selectGatewayRequirement(parsed);
	if (!requirements) {
		return {
			requirements: null,
			resource: parsed.resource ?? null,
			x402Version: parsed.x402Version ?? null,
			error: "no_gateway_option",
		};
	}
	return { requirements, resource: parsed.resource ?? null, x402Version: parsed.x402Version ?? null, error: null };
}

/**
 * Chain id from a requirement's `network` field ("eip155:5042002" ->
 * 5042002, the CAIP-2 shape circlekit uses). Null for anything that doesn't
 * match — never guesses.
 */
export function requirementChainId(requirements) {
	const network = requirements?.network;
	const match = typeof network === "string" ? /^eip155:(\d+)$/.exec(network) : null;
	return match ? Number(match[1]) : null;
}

/**
 * Build the EIP-712 TransferWithAuthorization pieces for signing (domain,
 * types, primaryType, message — the exact shape viem's
 * walletClient.signTypedData wants) PLUS the string-cast `authorization`
 * object the wire header carries. Two representations because the wire
 * payload requires strings (verified against circlekit's
 * BatchEvmScheme.create_payment_payload) while EIP-712 signing requires
 * bigints for uint256 fields — signing the string would produce the wrong
 * hash.
 *
 * `nowSec` and `nonceHex` are caller-supplied (not generated here) so this
 * stays pure and deterministic under test; ../x402.js supplies real ones
 * (Math.floor(Date.now()/1000), a random 32-byte hex nonce) at the call
 * site. The domain is the GATEWAY's EIP-712 domain (`extra.name`/
 * `extra.version`), never the USDC token's — signing against the token's
 * domain is the #1 way a naive implementation of this flow produces a hash
 * the Gateway contract rejects.
 */
// Margin ADDED to the server-advertised maxTimeoutSeconds when computing
// validBefore. The facilitator enforces its validity minimum against ITS OWN
// clock at verification time with essentially zero grace — probed live
// 2026-08-21 against the testnet facilitator with byte-identical browser
// payloads: a client clock a mere 30 SECONDS behind real time fails
// `authorization_validity_too_short`; correct clock passes; with this
// 24-hour margin even a clock 10 minutes behind passes (and the facilitator
// accepts the longer window). Signing exactly the advertised minimum from
// the CLIENT clock therefore bricks payments for every consumer device
// whose clock runs behind — Dan's iPad, field report 2026-08-21. The
// authorization is single-nonce and for the exact amount/recipient, so the
// longer window adds no spend exposure.
export const VALIDITY_SKEW_MARGIN_SECONDS = 86_400;

export function buildTransferAuthorizationTypedData(requirements, { from, nowSec, nonceHex }) {
	const chainId = requirementChainId(requirements);
	const validAfter = nowSec - 600; // 10 min clock-skew buffer, matches circlekit
	const validBefore =
		nowSec + Number(requirements?.maxTimeoutSeconds ?? 0) + VALIDITY_SKEW_MARGIN_SECONDS;
	const to = requirements?.payTo;
	const value = String(requirements?.amount ?? "0");
	return {
		domain: {
			name: requirements?.extra?.name,
			version: requirements?.extra?.version,
			chainId,
			verifyingContract: requirements?.extra?.verifyingContract,
		},
		types: {
			TransferWithAuthorization: [
				{ name: "from", type: "address" },
				{ name: "to", type: "address" },
				{ name: "value", type: "uint256" },
				{ name: "validAfter", type: "uint256" },
				{ name: "validBefore", type: "uint256" },
				{ name: "nonce", type: "bytes32" },
			],
		},
		primaryType: "TransferWithAuthorization",
		// bigint fields for signing — never the string wire values (wrong hash).
		message: {
			from,
			to,
			value: BigInt(value),
			validAfter: BigInt(validAfter),
			validBefore: BigInt(validBefore),
			nonce: nonceHex,
		},
		// String-cast fields for the Payment-Signature wire payload (all
		// values as strings, nonce as 0x-hex) — matches circlekit's
		// BatchEvmScheme.create_payment_payload exactly.
		authorization: {
			from,
			to,
			value,
			validAfter: String(validAfter),
			validBefore: String(validBefore),
			nonce: nonceHex,
		},
	};
}

/**
 * Build the Payment-Signature header value: base64-JSON of
 * {x402Version, payload: {authorization, signature}, resource, accepted}.
 * Matches circlekit's PaymentPayload.to_header exactly (pinned by the
 * header-builder test against the fixture) — `resource` and `accepted` echo
 * what the 402 carried; `accepted` re-shapes `requirements` under the wire's
 * field names (`payTo`, `maxTimeoutSeconds`).
 */
export function buildPaymentSignatureHeader({ x402Version, resource, requirements, authorization, signature }) {
	const accepted = {
		scheme: requirements?.scheme,
		network: requirements?.network,
		asset: requirements?.asset,
		amount: requirements?.amount,
		payTo: requirements?.payTo,
		maxTimeoutSeconds: requirements?.maxTimeoutSeconds,
		extra: requirements?.extra,
	};
	const header = {
		x402Version,
		payload: { authorization, signature },
		resource: resource ?? {},
		accepted,
	};
	return toBase64(JSON.stringify(header));
}

/**
 * The PAYMENT-REQUIRED header (base64-JSON payment requirements, #1296/x402)
 * from a 402 error's attached response headers, if present. Mirrors
 * extractReceipt's Headers-or-plain-object, case-insensitive lookup — api.js's
 * apiPostWithMeta attaches the raw `Headers` as `err.headers` on a non-2xx
 * response specifically so this is readable.
 */
export function extractPaymentRequiredHeader(headers) {
	if (!headers) return null;
	if (typeof headers.get === "function") {
		return headers.get("PAYMENT-REQUIRED") || headers.get("payment-required") || null;
	}
	const key = Object.keys(headers).find((k) => k.toLowerCase() === "payment-required");
	return key ? headers[key] : null;
}

/**
 * The settlement receipt (PAYMENT-RESPONSE header, #1296) from a
 * successful /start response, if present — surfaced subtly, never
 * fabricated. Accepts either a Fetch `Headers` object or a plain object
 * (tests use the latter); header names are matched case-insensitively
 * either way, since a plain object isn't guaranteed to normalize casing
 * the way `Headers` does.
 */
export function extractReceipt(headers) {
	if (!headers) return null;
	if (typeof headers.get === "function") {
		return headers.get("PAYMENT-RESPONSE") || headers.get("payment-response") || null;
	}
	const key = Object.keys(headers).find((k) => k.toLowerCase() === "payment-response");
	return key ? headers[key] : null;
}
