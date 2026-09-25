import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	PASSWORD_MAX,
	PASSWORD_MIN,
	passwordRules,
	passwordRulesMet,
	passwordsMatch,
	passwordStrength,
} from "../src/password-rules.js";

// ── The server-mirror drift guard (#1290's core requirement) ────────────
// The UI's rules must equal what auth/auth.js ACTUALLY enforces. Parse the
// server config's literals and compare — if either side changes alone, this
// fails and the UI can never promise what the server rejects.

test("UI password bounds mirror the server's Better Auth config exactly", () => {
	const serverConfig = readFileSync(new URL("../../auth/auth.js", import.meta.url), "utf8");
	const min = serverConfig.match(/minPasswordLength:\s*(\d+)/);
	const max = serverConfig.match(/maxPasswordLength:\s*(\d+)/);
	assert.ok(min && max, "auth/auth.js no longer declares password length bounds — update this guard AND the UI");
	assert.equal(PASSWORD_MIN, Number(min[1]));
	assert.equal(PASSWORD_MAX, Number(max[1]));
});

test("the only listed requirement is length — no invented composition rules", () => {
	// The server enforces length only; a UI that demands uppercase/digits would
	// reject passwords the server accepts (claim-integrity in miniature).
	const rules = passwordRules("x".repeat(PASSWORD_MIN));
	assert.equal(rules.length, 1);
	assert.equal(rules[0].id, "length");
	assert.ok(rules[0].met);
});

// ── Length boundaries ───────────────────────────────────────────────────

test("length rule boundaries: min-1 fails, min passes, max passes, max+1 fails", () => {
	assert.equal(passwordRulesMet("x".repeat(PASSWORD_MIN - 1)), false);
	assert.equal(passwordRulesMet("x".repeat(PASSWORD_MIN)), true);
	assert.equal(passwordRulesMet("x".repeat(PASSWORD_MAX)), true);
	assert.equal(passwordRulesMet("x".repeat(PASSWORD_MAX + 1)), false);
});

// ── Match / mismatch ────────────────────────────────────────────────────

test("match requires identical non-empty passwords", () => {
	assert.equal(passwordsMatch("correct horse battery", "correct horse battery"), true);
	assert.equal(passwordsMatch("correct horse battery", "correct horse batterX"), false);
	assert.equal(passwordsMatch("", ""), false); // empty never counts as matched
});

// ── Strength (guidance only) ────────────────────────────────────────────

test("strength: below the minimum is 'too short' with score 0", () => {
	assert.deepEqual(passwordStrength("short"), { score: 0, label: "too short" });
	assert.deepEqual(passwordStrength(""), { score: 0, label: "empty" });
});

test("strength grows with length and variety but never gates the rules", () => {
	const weak = passwordStrength("aaaaaaaaaaaa"); // 12 chars, one class
	const strong = passwordStrength("Correct-Horse-Battery-42!"); // long + varied
	assert.equal(weak.label, "weak");
	assert.equal(strong.label, "strong");
	assert.ok(weak.score < strong.score);
	// The weak-but-legal password still MEETS the requirements — the meter is
	// guidance; length is the only rule (mirrors the server).
	assert.equal(passwordRulesMet("aaaaaaaaaaaa"), true);
});

test("strength is monotone across the tiers used by the meter", () => {
	const s12 = passwordStrength("aaaaaaaaaaaa").score;
	const s16 = passwordStrength("aaaaaaaaaaaaaaaa").score;
	const s20 = passwordStrength("a".repeat(20)).score;
	assert.ok(s12 <= s16 && s16 <= s20);
});

// ── AuthPage wiring (source assertions, matching this suite's idiom) ────

const authPage = readFileSync(new URL("../src/components/AuthPage.jsx", import.meta.url), "utf8");

test("sign-up form wires the shared rules module, confirm field, and gate", () => {
	assert.match(authPage, /from '\.\.\/password-rules'/);
	assert.match(authPage, /Confirm password/);
	assert.match(authPage, /passwordsMatch\(form\.password, form\.confirm\)/);
	assert.match(authPage, /disabled=\{busy \|\| !canSubmit\}/);
	assert.match(authPage, /minLength=\{PASSWORD_MIN\}/); // no re-hardcoded literal
	assert.match(authPage, /Passwords do not match\./);
	// The gate applies to sign-up only — sign-in must not be blocked by the
	// confirm/rules machinery.
	assert.match(authPage, /const canSubmit = !creating \|\|/);
});

// ── #1323: forgot-password + resend-verification wiring ─────────────────

test("sign-in view offers a Forgot password control wired to the reset request", () => {
	assert.match(authPage, /Forgot password\?/);
	// The control's styling/affordance is pinned in auth-page-copy.test.js;
	// this line keeps the two in step, so a restyle that loses the wiring
	// below fails here rather than only there.
	assert.match(authPage, /className="auth-quiet-link"/);
	assert.match(authPage, /requestPasswordReset/);
	assert.match(authPage, /RESET_REQUESTED_MESSAGE/);
	// No account-enumeration: the same confirmation copy must be shown
	// regardless of whether the address has an account (mirrors the server's
	// identical response — auth/auth.js sendResetPassword). A grep for the
	// constant's name alone can't detect a branch — it stays green even if a
	// future edit does `setSent(result.userExists ? A : RESET_REQUESTED_MESSAGE)`.
	// The actual invariant this call site must hold is that the server's
	// response is never even looked at: requestPasswordReset's return value
	// is discarded outright, and confirmation always follows a bare
	// `setSent(true)` with no conditional between them.
	assert.doesNotMatch(authPage, /=\s*await requestPasswordReset/);
	assert.match(authPage, /await requestPasswordReset\([^)]*\)\s*\n\s*setSent\(true\)/);
});

test("an unverified sign-in (403) offers Resend verification email, wired to the resend endpoint", () => {
	assert.match(authPage, /err\.status === 403/);
	assert.match(authPage, /Resend verification email/);
	assert.match(authPage, /resendVerificationEmail/);
});

test("reset-password mode reads the token from the URL and gates submit on the shared rules", () => {
	assert.match(authPage, /mode === 'reset-password'/);
	assert.match(authPage, /URLSearchParams\(window\.location\.search\)\.get\('token'\)/);
	assert.match(authPage, /disabled=\{busy \|\| !rulesMet \|\| !match\}/);
	// A missing/expired token must not silently render a submittable form.
	assert.match(authPage, /invalid or has expired/);
});
