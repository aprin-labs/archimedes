import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	changeEmail,
	changePassword,
	deleteAccount,
	listSessions,
	revokeOtherSessions,
	revokeSession,
} from "../src/auth-client.js";

// ── #1367 (D2/D4): email change, password change, session revocation and
// account deletion ──────────────────────────────────────────────────────
//
// Two halves, same split as auth-client.test.js / account-settings.test.js:
// the six client calls get direct fetch-boundary coverage (mock at the
// boundary — fetch — never at authRequest's internals), and the component
// wiring is proven by source assertion, since .jsx is not importable under
// plain `node --test` and this suite has no DOM.
//
// The server side of all six lives in auth/test/auth.test.js, driving the
// real Better Auth handler: two-step email change, the current-password
// check, the /revoke-session ownership 404, and the delete-user
// re-authentication.

function jsonResponse(status, body) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { "Content-Type": "application/json" },
	});
}

function mockFetch(t, handler) {
	t.mock.method(globalThis, "fetch", handler);
}

// ── auth-client: the fetch boundary ─────────────────────────────────────

test("changeEmail POSTs newEmail + callbackURL to /api/auth/change-email, credentials included", async (t) => {
	let seen;
	mockFetch(t, async (url, options) => {
		seen = { url, method: options.method, credentials: options.credentials, body: JSON.parse(options.body) };
		return jsonResponse(200, { status: true });
	});

	await changeEmail("new@example.com", "https://app.test/app/account");

	assert.equal(seen.url, "/api/auth/change-email");
	assert.equal(seen.method, "POST");
	assert.equal(seen.credentials, "include");
	assert.deepEqual(seen.body, { newEmail: "new@example.com", callbackURL: "https://app.test/app/account" });
});

test("changePassword sends the current password for verification and revokes other sessions by default", async (t) => {
	let seen;
	mockFetch(t, async (url, options) => {
		seen = { url, body: JSON.parse(options.body) };
		return jsonResponse(200, { token: null, user: {} });
	});

	await changePassword("the old one", "a new correct horse battery");

	assert.equal(seen.url, "/api/auth/change-password");
	assert.deepEqual(seen.body, {
		currentPassword: "the old one",
		newPassword: "a new correct horse battery",
		// A password rotation that leaves other devices signed in is not
		// what a user changing their password believes happened.
		revokeOtherSessions: true,
	});
});

test("changePassword surfaces the server's INVALID_PASSWORD code rather than a generic failure", async (t) => {
	mockFetch(t, async () => jsonResponse(400, { message: "Invalid password", code: "INVALID_PASSWORD" }));

	await assert.rejects(() => changePassword("wrong", "a new correct horse battery"), (err) => {
		assert.equal(err.code, "INVALID_PASSWORD");
		assert.equal(err.status, 400);
		return true;
	});
});

test("listSessions GETs /api/auth/list-sessions", async (t) => {
	let seen;
	mockFetch(t, async (url, options) => {
		seen = { url, credentials: options.credentials };
		return jsonResponse(200, [{ id: "s1", token: "t1", createdAt: "2026-08-30T00:00:00Z" }]);
	});

	const rows = await listSessions();

	assert.equal(seen.url, "/api/auth/list-sessions");
	assert.equal(seen.credentials, "include");
	assert.equal(rows[0].token, "t1");
});

test("listSessions exposes SESSION_NOT_FRESH so the page can say so instead of rendering an empty list", async (t) => {
	mockFetch(t, async () => jsonResponse(403, { message: "Session is not fresh", code: "SESSION_NOT_FRESH" }));

	await assert.rejects(() => listSessions(), (err) => {
		assert.equal(err.code, "SESSION_NOT_FRESH");
		return true;
	});
});

test("revokeSession POSTs the token to /api/auth/revoke-session", async (t) => {
	let seen;
	mockFetch(t, async (url, options) => {
		seen = { url, method: options.method, body: JSON.parse(options.body) };
		return jsonResponse(200, { status: true });
	});

	await revokeSession("session-token-1");

	assert.equal(seen.url, "/api/auth/revoke-session");
	assert.equal(seen.method, "POST");
	assert.deepEqual(seen.body, { token: "session-token-1" });
});

// The client half of auth.js's ownership guard: a token that isn't yours
// comes back 404, and this call must THROW rather than resolve — otherwise
// AccountSettings' "Session ended." notice fires for a session that is
// still alive. (The server half, including the mutation transcript, is in
// auth/test/auth.test.js.)
test("revokeSession throws on the ownership 404 instead of resolving into a false success", async (t) => {
	mockFetch(t, async () => jsonResponse(404, { message: "Session not found", code: "SESSION_NOT_FOUND" }));

	await assert.rejects(() => revokeSession("someone-elses-token"), (err) => {
		assert.equal(err.code, "SESSION_NOT_FOUND");
		assert.equal(err.status, 404);
		return true;
	});
});

test("revokeOtherSessions POSTs to /api/auth/revoke-other-sessions with no body", async (t) => {
	let seen;
	mockFetch(t, async (url, options) => {
		seen = { url, method: options.method, body: options.body };
		return jsonResponse(200, { status: true });
	});

	await revokeOtherSessions();

	assert.equal(seen.url, "/api/auth/revoke-other-sessions");
	assert.equal(seen.method, "POST");
	assert.equal(seen.body, undefined);
});

test("deleteAccount sends the password when there is one", async (t) => {
	let seen;
	mockFetch(t, async (url, options) => {
		seen = { url, method: options.method, body: JSON.parse(options.body) };
		return jsonResponse(200, { success: true, message: "User deleted" });
	});

	await deleteAccount("my password");

	assert.equal(seen.url, "/api/auth/delete-user");
	assert.equal(seen.method, "POST");
	assert.deepEqual(seen.body, { password: "my password" });
});

// A Google/GitHub-only account has no credential row, so a password field —
// even an empty one — makes Better Auth answer CREDENTIAL_ACCOUNT_NOT_FOUND
// instead of falling through to its session-freshness check. The omission
// has to be a real omission.
test("deleteAccount omits the password field entirely for a password-less account", async (t) => {
	let seen;
	mockFetch(t, async (url, options) => {
		seen = { body: JSON.parse(options.body) };
		return jsonResponse(200, { success: true, message: "User deleted" });
	});

	await deleteAccount(undefined);

	assert.deepEqual(seen.body, {});
	assert.equal("password" in seen.body, false);
});

// ── AccountSettings.jsx wiring ──────────────────────────────────────────

const accountSettings = readFileSync(new URL("../src/components/AccountSettings.jsx", import.meta.url), "utf8");

test("AccountSettings imports all six calls from the shared auth-client, not a hand-rolled fetch", () => {
	for (const call of ["changeEmail", "changePassword", "deleteAccount", "listSessions", "revokeOtherSessions", "revokeSession"]) {
		assert.match(accountSettings, new RegExp(`\\n  ${call},`), `${call} is not imported from auth-client`);
	}
	assert.doesNotMatch(accountSettings, /fetch\(/);
});

test("the email-change form calls changeEmail and never claims the address changed", () => {
	assert.match(accountSettings, /await changeEmail\(newEmail\.trim\(\), `\$\{window\.location\.origin\}\/app\/account`\)/);
	assert.match(accountSettings, /does not change until/);
	assert.doesNotMatch(accountSettings, /Email (address )?(updated|changed)\./);
});

// Anti-enumeration: Better Auth answers a taken address with the same
// `{status:true}` it answers a free one and sends nothing, so a UI that
// branched its copy on the response would re-open the account-existence
// oracle the server closes. A grep for the constant's name alone cannot see
// a branch — the invariant is that the response is never even read. Same
// assertion shape as password-rules.test.js's requestPasswordReset guard.
test("the email-change notice never branches on the server's response", () => {
	assert.doesNotMatch(accountSettings, /=\s*await changeEmail/);
	assert.match(accountSettings, /await changeEmail\(newEmail\.trim\(\), `\$\{window\.location\.origin\}\/app\/account`\)\n\s*setEmailNotice\(EMAIL_CHANGE_REQUESTED_MESSAGE\)/);
});

test("the password form is gated on the shared password-rules module, not re-invented rules", () => {
	assert.match(accountSettings, /from '\.\.\/password-rules'/);
	assert.match(accountSettings, /passwordRulesMet\(passwordForm\.next\)/);
	assert.match(accountSettings, /passwordsMatch\(passwordForm\.next, passwordForm\.confirm\)/);
	assert.match(accountSettings, /disabled=\{passwordBusy \|\| !passwordReady\}/);
	assert.match(accountSettings, /minLength=\{PASSWORD_MIN\}/); // no re-hardcoded literal
});

// An account with no credential row cannot change a password and cannot be
// given one from the client (/set-password is serverOnly in Better Auth).
// Rendering a form that can only ever 400 would be a promise the code does
// not keep.
test("a password-less account is told so instead of being shown a form that can only fail", () => {
	assert.match(accountSettings, /accountHasPassword\(connectedAccounts\)/);
	assert.match(accountSettings, /so it has no password to change/);
});

test("the sessions section revokes one, revokes the rest, and confirms before either", () => {
	assert.match(accountSettings, /await revokeSession\(session\.token\)/);
	assert.match(accountSettings, /await revokeOtherSessions\(\)/);
	assert.match(accountSettings, /window\.confirm\('End this session\?/);
	assert.match(accountSettings, /window\.confirm\('End every other session\?/);
});

// A stale session cannot READ /list-sessions (freshSessionMiddleware) but
// CAN still revoke (sensitiveSessionMiddleware). Rendering the unreadable
// case as "no other sessions" would be a fabricated all-clear at exactly
// the moment someone is checking whether they have been compromised.
test("a stale session renders the honest re-auth state, not an empty session list", () => {
	assert.match(accountSettings, /\) : sessionsStale \? \(/);
	assert.match(accountSettings, /SESSIONS_STALE_MESSAGE/);
	// ...and the list-is-empty branch is a separate, later branch, so the two
	// states can never collapse into one another.
	assert.match(accountSettings, /\) : sessions\.length === 0 \? \(/);
});

test("the current session is identified from AuthContext's own session, never guessed, and offers no End-session button", () => {
	assert.match(accountSettings, /const \{ user, session: currentSession, signOut \} = useAuth\(\)/);
	assert.match(accountSettings, /isCurrentSession\(session, currentSession\)/);
	assert.match(accountSettings, /\{!current && \(/);
});

test("deletion is gated on the typed phrase AND a confirm AND the server's re-authentication", () => {
	assert.match(accountSettings, /deleteConfirmationMatches\(deletePhrase\)/);
	assert.match(accountSettings, /if \(!deleteReady\) return/);
	assert.match(accountSettings, /window\.confirm\('Delete your account\? This cannot be undone\.'\)/);
	assert.match(accountSettings, /await deleteAccount\(hasPassword \? deletePassword : undefined\)/);
});

// The erased/detached/retained sentences must come from the data module the
// schema-mirror test pins (see account-deletion.test.js) — hand-written
// copy in the JSX would be a claim nothing keeps true.
test("the deletion explanation is rendered from account-deletion.js, not written by hand in the JSX", () => {
	assert.match(accountSettings, /DELETION_ERASED\.map\(\(row\) => <li key=\{row\.table\}>\{row\.label\}<\/li>\)/);
	assert.match(accountSettings, /DELETION_DETACHED\.map\(\(row\) => <li key=\{row\.table\}>\{row\.label\}<\/li>\)/);
	assert.match(accountSettings, /DELETION_RETAINED\.map\(\(row\) => <li key=\{row\.table\}>\{row\.label\}<\/li>\)/);
});

// ── auth/auth.js: the server switches these controls depend on ───────────
//
// Same server-mirror idiom as password-rules.test.js. /change-email and
// /delete-user are opt-in in better-auth@1.6.25 and refuse outright without
// this config, so a UI shipped without it would render controls that always
// error.

const authConfig = readFileSync(new URL("../../auth/auth.js", import.meta.url), "utf8");

test("auth/auth.js enables the two opt-in capabilities this page depends on", () => {
	assert.match(authConfig, /changeEmail: \{\n\s*enabled: true,/);
	assert.match(authConfig, /deleteUser: \{\n\s*enabled: true,/);
});

test("auth/auth.js does not enable the options that would make either control dishonest", () => {
	// updateEmailWithoutVerification would switch an unverified account's
	// address over with no proof the new address exists.
	// The colon is what distinguishes a real setting from the prose above
	// it explaining why the setting is absent; the runtime assertion in
	// auth/test/auth.test.js ("deletion is opt-in...") checks the parsed
	// options object, which no comment can fool.
	assert.doesNotMatch(authConfig, /updateEmailWithoutVerification\s*:/);
	// sendDeleteAccountVerification makes /delete-user ALWAYS mail a link and
	// never delete in-request — a button that silently does nothing for every
	// address SES will not deliver to, while SES is sandboxed.
	assert.doesNotMatch(authConfig, /sendDeleteAccountVerification:/);
});

test("auth/auth.js guards /revoke-session ownership, which is what makes the page's success notice true", () => {
	assert.match(authConfig, /if \(ctx\.path === '\/revoke-session'\)/);
	assert.match(authConfig, /target\?\.session\?\.userId !== session\.user\.id/);
	assert.match(authConfig, /code: 'SESSION_NOT_FOUND'/);
});
