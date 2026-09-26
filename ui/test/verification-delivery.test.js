import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	DELIVERY_STATES,
	RATE_LIMITED_BY_CLIENT,
	VERIFICATION_STATUS_ENDPOINT,
	deriveVerificationDeliveryView,
	shouldShowRequestedFallback,
} from "../src/verificationDelivery.js";

// Verification-mail delivery feedback (#1748 item 2).
//
// THE OBSERVATION: POST /api/auth/send-verification-email returns
// `200 {status:true}` forever — for an address Amazon SES has already dropped
// onto the account suppression list (SES accepts the send, returns a MessageId,
// and bins the message), for an address whose last send threw, for every
// address. The UI had exactly one thing it could honestly say, and it said it
// forever: "requested — delivery isn't confirmed."
//
// Two halves, the same split ui/test/free-generation-banner.test.js makes:
//   1. REAL unit tests of ../src/verificationDelivery.js — the module that
//      decides what a human reads for each state. Every honesty rule this
//      feature makes on the frontend is executable here, not asserted by regex.
//   2. Source-text pins on the two mount points and the shared component, for
//      the wiring a DOM-free suite cannot execute (no jsdom / testing-library /
//      vitest in ui/ — CLAUDE.md anti-goal).

const settings = readFileSync(new URL("../src/components/AccountSettings.jsx", import.meta.url), "utf8");
const control = readFileSync(new URL("../src/components/ResendVerificationControl.jsx", import.meta.url), "utf8");
const panel = readFileSync(new URL("../src/components/VerificationDeliveryStatus.jsx", import.meta.url), "utf8");
const authClient = readFileSync(new URL("../src/auth-client.js", import.meta.url), "utf8");
const deliveryModule = readFileSync(new URL("../src/verificationDelivery.js", import.meta.url), "utf8");
const statusResolver = readFileSync(new URL("../../auth/verification-status.js", import.meta.url), "utf8");

// ── 1. the module that decides ───────────────────────────────────────────

test("the three states the resend control must distinguish produce three distinct messages", () => {
	const sent = deriveVerificationDeliveryView({ state: "sent", sends: 1, checkSpam: false, retryAfterSeconds: 0 });
	const suppressed = deriveVerificationDeliveryView({
		state: "suppressed",
		suppression: { checked: true, suppressed: true, reason: "BOUNCE" },
	});
	const rateLimited = deriveVerificationDeliveryView({ state: "rate_limited", retryAfterSeconds: 42 });

	for (const view of [sent, suppressed, rateLimited]) assert.ok(view, "every one of the three states must render");
	assert.equal(sent.state, DELIVERY_STATES.SENT);
	assert.equal(suppressed.state, DELIVERY_STATES.SUPPRESSED);
	assert.equal(rateLimited.state, DELIVERY_STATES.RATE_LIMITED);
	assert.equal(new Set([sent.message, suppressed.message, rateLimited.message]).size, 3);
});

test("suppressed is the only state that takes the button away, and it says why", () => {
	const view = deriveVerificationDeliveryView({
		state: "suppressed",
		suppression: { checked: true, suppressed: true, reason: "BOUNCE" },
	});
	assert.equal(view.canResend, false, "resending an address SES is dropping cannot work");
	assert.match(view.message, /bounced/i);
	// The two exits a user actually has. "Try again later" is not one of them.
	assert.match(view.message, /different email address/i);
	assert.match(view.message, /contact the team/i);
	assert.doesNotMatch(view.message, /try again/i);

	const complaint = deriveVerificationDeliveryView({
		state: "suppressed",
		suppression: { checked: true, suppressed: true, reason: "COMPLAINT" },
	});
	assert.match(complaint.message, /spam complaint/i);
	assert.equal(complaint.canResend, false);
});

test("rate-limited quotes the wait when the server knows it, and never invents one when it does not", () => {
	const known = deriveVerificationDeliveryView({ state: "rate_limited", retryAfterSeconds: 42 });
	assert.match(known.message, /42 seconds/);
	assert.equal(known.canResend, false);

	// The client-side 429: Better Auth's limiter keys on IP, not address, so
	// this arrives with no number attached.
	const clientSide = deriveVerificationDeliveryView(RATE_LIMITED_BY_CLIENT);
	assert.equal(clientSide.state, DELIVERY_STATES.RATE_LIMITED);
	assert.equal(clientSide.canResend, false);
	assert.doesNotMatch(clientSide.message, /\d+ seconds/);
	assert.match(clientSide.message, /about a minute/i);
});

test("sent claims ACCEPTANCE, never delivery — SES accepts mail it then drops", () => {
	const view = deriveVerificationDeliveryView({ state: "sent", sends: 1, checkSpam: false, retryAfterSeconds: 0 });
	assert.match(view.message, /accepted/i);
	assert.doesNotMatch(view.message, /delivered|has been sent|sent successfully|arrived/i);
	assert.equal(view.canResend, true);
});

test("the spam hint appears only once the server says so, and carries the count it saw", () => {
	const quiet = deriveVerificationDeliveryView({ state: "sent", sends: 1, checkSpam: false, retryAfterSeconds: 0 });
	assert.doesNotMatch(quiet.message, /spam|junk/i);

	const noisy = deriveVerificationDeliveryView({ state: "sent", sends: 3, checkSpam: true, retryAfterSeconds: 0 });
	assert.match(noisy.message, /spam or junk folder/i);
	assert.match(noisy.message, /3 requests/);
});

test("failed is not sent: it names the provider's refusal and says nothing went out", () => {
	const view = deriveVerificationDeliveryView({ state: "failed", lastError: "MessageRejected", retryAfterSeconds: 0 });
	assert.match(view.message, /MessageRejected/);
	assert.match(view.message, /Nothing went out/i);
	assert.doesNotMatch(view.message, /accepted/i);
});

test("an unreadable delivery history and an empty one are different sentences", () => {
	const unreadable = deriveVerificationDeliveryView({ state: "unknown", sends: null, retryAfterSeconds: 0 });
	assert.match(unreadable.message, /cannot read|cannot say/i);

	const empty = deriveVerificationDeliveryView({ state: "unknown", sends: 0, retryAfterSeconds: 0 });
	assert.match(empty.message, /No verification email has been recorded/i);
	assert.notEqual(unreadable.message, empty.message);
	// Neither may imply a send happened.
	for (const view of [unreadable, empty]) assert.doesNotMatch(view.message, /accepted|sent to you|on its way/i);
});

test("nothing is rendered for a verified account, an absent response, or a state this build does not know", () => {
	assert.equal(deriveVerificationDeliveryView({ state: "verified" }), null);
	assert.equal(deriveVerificationDeliveryView(null), null);
	assert.equal(deriveVerificationDeliveryView(undefined), null);
	assert.equal(deriveVerificationDeliveryView("sent"), null);
	assert.equal(deriveVerificationDeliveryView({}), null);
	// A state deployed server-side ahead of this build: silence, not the
	// nearest familiar message, which would be an invented claim about
	// someone's mail. Same rule freeGenerations.js applies to an unknown lock.
	assert.equal(deriveVerificationDeliveryView({ state: "quarantined_by_provider" }), null);
});

test("no state anywhere in this module claims the mail was delivered", () => {
	const states = [
		{ state: "sent", sends: 4, checkSpam: true, retryAfterSeconds: 0 },
		{ state: "suppressed", suppression: { checked: true, suppressed: true, reason: "BOUNCE" } },
		{ state: "rate_limited", retryAfterSeconds: 12 },
		{ state: "failed", lastError: "AccessDeniedException", retryAfterSeconds: 0 },
		{ state: "unknown", sends: 0, retryAfterSeconds: 0 },
		{ state: "unknown", sends: null, retryAfterSeconds: 0 },
		{ state: "bounced", bounce: { at: "2026-09-02T10:00:00.000Z", kind: "bounce" } },
		{ state: "bounced", bounce: { at: "2026-09-02T10:00:00.000Z", kind: "complaint" } },
	];
	for (const status of states) {
		const view = deriveVerificationDeliveryView(status);
		assert.ok(view, `${status.state} must render something`);
		assert.doesNotMatch(view.message, /was delivered|has been delivered|check your inbox|email has been sent/i);
	}
});

// ── #1804: the PUSHED bounce ─────────────────────────────────────────────

test("bounced takes the button away and tells the user to use another address", () => {
	const view = deriveVerificationDeliveryView({
		state: "bounced",
		bounce: { at: "2026-09-02T10:00:00.000Z", kind: "bounce" },
	});
	assert.equal(view.state, DELIVERY_STATES.BOUNCED);
	assert.equal(view.canResend, false, "resending to a mailbox that does not exist cannot work");
	assert.equal(view.tone, "blocked");
	assert.match(view.message, /does not exist/i);
	assert.match(view.message, /different email address/i);
});

test("a COMPLAINT is never described as a broken mailbox — that would be a lie", () => {
	// The person exists and their mail works; they told their provider our
	// mail is spam. Telling them their address is dead is a false claim about
	// their mailbox, and the only honest sentence is that we stopped sending.
	const view = deriveVerificationDeliveryView({
		state: "bounced",
		bounce: { at: "2026-09-02T10:00:00.000Z", kind: "complaint" },
	});
	assert.equal(view.canResend, false);
	assert.match(view.message, /spam/i);
	assert.doesNotMatch(view.message, /bounced|does not exist/i);
});

test("bounced and suppressed are two different sentences, not one recycled", () => {
	// They are different evidence — SES pushed one to us, we asked for the
	// other — and #1790's suppressed copy names the account suppression list,
	// which is a fact the pushed bounce does not establish.
	const bounced = deriveVerificationDeliveryView({
		state: "bounced",
		bounce: { at: "2026-09-02T10:00:00.000Z", kind: "bounce" },
	});
	const suppressed = deriveVerificationDeliveryView({
		state: "suppressed",
		suppression: { checked: true, suppressed: true, reason: "BOUNCE" },
	});
	assert.notEqual(bounced.message, suppressed.message);
	assert.equal(bounced.canResend, false);
	assert.equal(suppressed.canResend, false);
});

test("the resend caption stands down for bounced, like every other recognised state", () => {
	// Two answers to one question is the defect #1790's review push removed.
	// A new state must not reintroduce it.
	assert.equal(
		shouldShowRequestedFallback({ state: "bounced", bounce: { at: "2026-09-02T10:00:00.000Z", kind: "bounce" } }),
		false,
	);
});

// ── 2. wiring the DOM-free suite cannot execute ──────────────────────────

test("both mount points render the SAME component, so the two surfaces cannot drift", () => {
	for (const [name, source] of [["AccountSettings", settings], ["ResendVerificationControl", control]]) {
		assert.match(source, /import VerificationDeliveryStatus from ["']\.[/.]*\/?VerificationDeliveryStatus["']/, name);
		assert.match(source, /<VerificationDeliveryStatus status=\{[a-zA-Z]+\} \/>/, name);
		assert.match(source, /getVerificationStatus/, name);
	}
});

test("the shared component renders the derived message and tags the state it is showing", () => {
	assert.match(panel, /deriveVerificationDeliveryView/);
	assert.match(panel, /if \(!view\) return null/);
	assert.match(panel, /data-delivery-state=\{view\.state\}/);
	assert.match(panel, /\{view\.message\}/);
	// Suppressed/failed are the states a user must act on: announced, not
	// quietly appended to the page.
	assert.match(panel, /role=\{view\.tone === "blocked" \|\| view\.tone === "error" \? "alert" : "status"\}/);
	// No copy is composed at the render layer — every string has one home.
	assert.doesNotMatch(panel, /"[A-Z][a-z]+ [a-z]+ [a-z]+/);
});

test("both mount points disable the button when the derived view says resending cannot help", () => {
	assert.match(settings, /deriveVerificationDeliveryView\(verifyDelivery\)\?\.canResend === false/);
	assert.match(settings, /disabled=\{resendDisabled\}/);
	assert.match(control, /deriveVerificationDeliveryView\(delivery\)\?\.canResend === false/);
	assert.match(control, /disabled=\{status === "sending" \|\| blocked\}/);
});

test("a status the client cannot read renders nothing, never an optimistic default", () => {
	// Both callers catch and set null — the panel then renders nothing at all.
	assert.match(settings, /catch \{\s*(\/\/[^\n]*\n\s*)*setVerifyDelivery\(null\)/);
	assert.match(control, /catch \{\s*(\/\/[^\n]*\n\s*)*setDelivery\(null\)/);
});

test("a 429 from the resend POST is rendered as rate-limited by both mount points", () => {
	// Better Auth's limiter keys on client IP, not address, so this refusal is
	// a fact the server-side status endpoint cannot always see.
	assert.match(settings, /err\.status === 429\) setVerifyDelivery\(RATE_LIMITED_BY_CLIENT\)/);
	assert.match(control, /err\?\.status === 429\) setDelivery\(RATE_LIMITED_BY_CLIENT\)/);
});

test("the status endpoint path has one home, and it is the one the auth service serves", () => {
	assert.match(authClient, /'\/api\/auth\/verification-status'/);
	assert.equal(VERIFICATION_STATUS_ENDPOINT, "/api/auth/verification-status");
	// The client sends no address — the endpoint reports on the session's own,
	// which is what keeps it from being a per-address oracle.
	assert.match(authClient, /getVerificationStatus = \(\) => authRequest\('\/api\/auth\/verification-status'\)/);
});

test("every state this UI renders is one the auth service can actually return", () => {
	// Source-pinned against auth/verification-status.js's own state table: a
	// state named here but never produced there is dead copy, and the reverse
	// is a state that would render nothing.
	const served = new Set(
		[...statusResolver.matchAll(/^\s{2}([A-Z_]+): '([a-z_]+)',$/gm)].map(match => match[2]),
	);
	assert.ok(served.size >= 6, `parsed ${served.size} states from auth/verification-status.js`);
	for (const state of Object.values(DELIVERY_STATES)) {
		assert.ok(served.has(state), `ui names a state the auth service never returns: ${state}`);
	}
	for (const state of served) {
		assert.ok(
			Object.values(DELIVERY_STATES).includes(state),
			`auth returns a state this build renders nothing for: ${state}`,
		);
	}
});

test("the honesty constants live in the pure module, not scattered across components", () => {
	// No component may hard-code a delivery sentence of its own.
	for (const source of [settings, control]) {
		assert.doesNotMatch(source, /suppression list/i);
		assert.doesNotMatch(source, /spam or junk/i);
	}
	assert.match(deliveryModule, /suppression list/i);
	assert.match(deliveryModule, /spam or junk folder/i);
});

test("a suppression lookup that never ran is never hidden behind an optimistic 'accepted'", () => {
	// Day one in production: `terraform apply` has not run, so every
	// GetSuppressedDestination is AccessDeniedException and the server answers
	// `checked: false`. That is not "not suppressed" — an address SES is
	// silently binning would otherwise read as an ordinary accepted send, which
	// is the eternal 200 this feature exists to end.
	const unchecked = {
		checked: false,
		suppressed: false,
		reason: null,
		since: null,
		detail: "suppression lookup failed",
	};
	for (const status of [
		{ state: "sent", sends: 1, checkSpam: false, retryAfterSeconds: 0, suppression: unchecked },
		{ state: "unknown", sends: 0, retryAfterSeconds: 0, suppression: unchecked },
	]) {
		assert.match(deriveVerificationDeliveryView(status).message, /could not check|cannot be ruled out/i);
	}

	const clean = deriveVerificationDeliveryView({
		state: "sent",
		sends: 1,
		checkSpam: false,
		retryAfterSeconds: 0,
		suppression: { checked: true, suppressed: false, reason: null, since: null, detail: null },
	});
	assert.doesNotMatch(clean.message, /could not check/i);
});

// ── 3. one click, one answer (review push, 2026-09-02) ───────────────────
//
// Both surfaces used to mount VERIFICATION_REQUESTED_MESSAGE — "requested —
// delivery isn't confirmed and may take a few minutes" — ABOVE the delivery
// panel on a successful click. That was right when the eternal 200 was the
// only fact available. It is wrong now: the panel can say **suppressed** /
// **failed** / **rate_limited** while the caption above it says delivery
// merely "isn't confirmed", or say an honest **sent** under a second, softer
// "requested". Two competing truths for one click.
//
// The rule, held by ONE shared predicate so the two surfaces cannot drift:
// the caption is the pre-status fallback only.

const STATES_WITH_A_VIEW = [
	["sent", { state: "sent", sends: 1, checkSpam: false, retryAfterSeconds: 0 }],
	["suppressed", { state: "suppressed", suppression: { checked: true, suppressed: true, reason: "BOUNCE" } }],
	["failed", { state: "failed", lastError: "MessageRejected", retryAfterSeconds: 0 }],
	["rate_limited", { state: "rate_limited", retryAfterSeconds: 42 }],
];

test("once the panel has something real to say, the eternal 'requested' caption stands down", () => {
	for (const [name, status] of STATES_WITH_A_VIEW) {
		assert.ok(deriveVerificationDeliveryView(status), `${name} must render a view at all`);
		assert.equal(
			shouldShowRequestedFallback(status),
			false,
			`${name} has a specific answer — the eternal caption would contradict or dilute it`,
		);
	}
});

test("the caption is still there while the status GET has nothing", () => {
	// The three ways the panel renders nothing after a click: the GET is
	// still in flight, it failed closed (401 / 503 / offline → null), or the
	// body named a state this build has never heard of. Something must still
	// acknowledge the click, and "a send was requested" is always true.
	assert.equal(shouldShowRequestedFallback(null), true, "GET still loading / failed closed");
	assert.equal(shouldShowRequestedFallback(undefined), true);
	assert.equal(shouldShowRequestedFallback({ state: "quarantined_by_a_future_build" }), true);
	assert.equal(shouldShowRequestedFallback(RATE_LIMITED_BY_CLIENT), false, "a client 429 IS a specific answer");
});

test("both mount points gate the caption on the SAME predicate, and neither keeps a copy", () => {
	// Mutation this pins: restore either unconditional mount and this goes red.
	assert.match(
		settings,
		/\{verifyStatus === 'sent' && shouldShowRequestedFallback\(verifyDelivery\) && \(/,
		"AccountSettings must gate VERIFICATION_REQUESTED_MESSAGE on the shared predicate",
	);
	assert.match(
		control,
		/\{status === "sent" && shouldShowRequestedFallback\(delivery\) && \(/,
		"ResendVerificationControl must gate VERIFICATION_REQUESTED_MESSAGE on the shared predicate",
	);
	for (const [name, source] of [["AccountSettings", settings], ["ResendVerificationControl", control]]) {
		assert.match(source, /shouldShowRequestedFallback\b/, `${name} must import the shared predicate`);
		// No surface may re-derive the rule for itself.
		assert.doesNotMatch(
			source,
			/deriveVerificationDeliveryView\([a-zA-Z]+\)\s*(===|!==)\s*null/,
			`${name} must not reimplement shouldShowRequestedFallback inline`,
		);
	}
	// And the predicate itself is defined once, in the pure module.
	assert.match(deliveryModule, /export function shouldShowRequestedFallback/);
});
