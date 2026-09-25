import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// #verification-resend-button: on-demand resend + honest status surfacing for
// email verification, with ENFORCEMENT (requireEmailVerification) staying off.
// SES is in sandbox today — the auth sidecar's mailer fail-softs (auth/auth.js)
// so a send can silently never arrive. These pins protect the two things that
// matter: the button only appears for an actually-unverified user, and no
// copy anywhere claims confirmed delivery.

const accountSettings = readFileSync(
	new URL("../src/components/AccountSettings.jsx", import.meta.url),
	"utf8",
);
const authClient = readFileSync(
	new URL("../src/auth-client.js", import.meta.url),
	"utf8",
);
const authJs = readFileSync(
	new URL("../../auth/auth.js", import.meta.url),
	"utf8",
);

test("the resend button is gated on emailVerified === false, not any other condition", () => {
	assert.match(accountSettings, /user\.emailVerified === false/);
	// The gate must sit directly in front of the button markup, not just
	// exist somewhere in the file disconnected from the JSX it should guard.
	assert.match(
		accountSettings,
		/user\.emailVerified === false \? \(\s*<div[\s\S]{0,500}Send verification email/,
	);
});

test("a verified user gets a quiet checkmark and no button", () => {
	// The false-branch of the same ternary (rendered when emailVerified is
	// NOT strictly false) must show the quiet state and must not repeat the
	// button markup.
	const falseBranchMatch = accountSettings.match(
		/\) : \(\s*<span className="caption">Email verified ✓<\/span>\s*\)/,
	);
	assert.ok(falseBranchMatch, "expected an else-branch rendering the quiet verified state");
	assert.doesNotMatch(falseBranchMatch[0], /btn-secondary|Send verification email/);
});

test("clicking resend hits Better Auth's real endpoint via the shared auth-client", () => {
	assert.match(accountSettings, /resendVerificationEmail/);
	assert.match(accountSettings, /from '\.\.\/auth-client'/);
	// The literal path lives in auth-client.js (the shared fetch/client
	// pattern every other auth call in this app uses); pin it there rather
	// than duplicating the string in the component.
	assert.match(authClient, /\/api\/auth\/send-verification-email/);
});

test("success copy is honest: requested, not confirmed delivered", () => {
	// Hard requirement: the mailer fail-softs and SES sandbox restricts
	// recipients today, so the server cannot confirm the mail arrived. No
	// copy anywhere in the component may claim confirmed delivery.
	assert.doesNotMatch(accountSettings, /email (was )?sent successfully/i);
	assert.doesNotMatch(accountSettings, /email has been sent/i);
	assert.doesNotMatch(accountSettings, /check your inbox/i);
	// The honest framing: a request was made, delivery isn't confirmed, and
	// it may take a few minutes.
	assert.match(accountSettings, /requested/i);
	assert.match(accountSettings, /few minutes/i);
	assert.match(accountSettings, /(isn't|is not|not) confirmed/i);
});

test("error state surfaces the server's own error, not a generic message", () => {
	assert.match(accountSettings, /verifyError/);
	assert.match(accountSettings, /role="alert">\{verifyError\}/);
});

test("anti-goal: email verification ENFORCEMENT must stay off", () => {
	// requireEmailVerification must keep reading the env flag, not be
	// hard-coded on — enforcement is explicitly out of scope for this change.
	assert.doesNotMatch(authJs, /requireEmailVerification:\s*true/);
	assert.match(authJs, /requireEmailVerification:\s*emailVerificationEnforced\(env\)/);
});

test("the mailer failure catches stay fail-soft (no throw) and stay loud (console.error)", () => {
	// The shape this pins changed with finding EV-3 (2026-08-31 pre-flip
	// audit). It used to be `try { await mailer.send(...) } catch { console.error }`.
	// The await was the defect: /api/auth/send-verification-email — the
	// endpoint this very button calls — is reachable with NO session, and
	// Better Auth defends it with a 500ms constant-time floor so that
	// "unknown or already-verified" and "known and unverified" look alike.
	// An awaited SES round trip walks straight through that floor (measured
	// 504ms vs 922ms against a 900ms mailer), turning the resend button into
	// an account-existence oracle for anonymous callers. It is now
	// fire-and-forget with its own `.catch`, exactly like sendResetPassword.
	//
	// So: same two properties as before — the failure is caught, and it is
	// logged, not swallowed — plus the third one that made it correct. The
	// behavioural guard for the timing property lives in
	// auth/test/email-flows.test.js ("the anonymous resend path does not leak
	// account existence through response time"); this is the cheap
	// source-level pin that sits next to the button's own tests.
	assert.match(
		authJs,
		/sendVerificationEmail:\s*async[\s\S]{0,120}mailer\.send\(\{[\s\S]{0,450}\}\)\.catch\([\s\S]{0,200}console\.error/,
	);
	assert.doesNotMatch(authJs, /sendVerificationEmail:\s*async[\s\S]{0,200}await mailer\.send/);
	assert.doesNotMatch(authJs, /catch \(error\)[\s\S]{0,50}throw error/);
});
