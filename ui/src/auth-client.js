// import.meta.env?. (not .env.), so this module loads under plain node in
// ui/test/ too — see featureFlags.js's header comment for the same pattern.
const API_BASE = import.meta.env?.VITE_API_BASE ?? ''

async function authRequest(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...options,
    headers: options.body ? { 'Content-Type': 'application/json', ...options.headers } : options.headers,
  })
  const data = response.status === 204 ? null : await response.json().catch(() => null)
  if (!response.ok) {
    const error = new Error(data?.message || data?.error || 'Authentication failed')
    error.status = response.status
    // Better Auth's own APIError body carries a stable machine `code`
    // alongside the human `message` (e.g. { message: 'Session is not
    // fresh', code: 'SESSION_NOT_FRESH' }) — expose it so callers can branch
    // on the code instead of string-matching the prose, which is fragile
    // and would silently stop matching if the library ever reworded it.
    error.code = data?.code
    throw error
  }
  return data
}

export const getSession = () => authRequest('/api/auth/get-session')
export const getProviders = () => authRequest('/api/auth/providers')

export const signInEmail = (email, password, callbackURL) => authRequest('/api/auth/sign-in/email', {
  method: 'POST',
  body: JSON.stringify({ email, password, callbackURL }),
})

export const signUpEmail = (name, email, password, callbackURL) => authRequest('/api/auth/sign-up/email', {
  method: 'POST',
  body: JSON.stringify({ name, email, password, callbackURL }),
})

export async function signInSocial(provider, callbackURL) {
  const result = await authRequest('/api/auth/sign-in/social', {
    method: 'POST',
    body: JSON.stringify({ provider, callbackURL, disableRedirect: true }),
  })
  if (!result?.url) throw new Error('OAuth provider did not return a redirect')
  window.location.assign(result.url)
}

export const signOut = () => authRequest('/api/auth/sign-out', { method: 'POST' })

// Same user-visible outcome whether or not the address has an account
// (Better Auth's own anti-enumeration behavior — see auth/auth.js
// sendResetPassword) — callers must not branch UI copy on the result.
export const requestPasswordReset = (email, redirectTo) => authRequest('/api/auth/request-password-reset', {
  method: 'POST',
  body: JSON.stringify({ email, redirectTo }),
})

export const resetPassword = (newPassword, token) => authRequest('/api/auth/reset-password', {
  method: 'POST',
  body: JSON.stringify({ newPassword, token }),
})

export const resendVerificationEmail = (email, callbackURL) => authRequest('/api/auth/send-verification-email', {
  method: 'POST',
  body: JSON.stringify({ email, callbackURL }),
})

// #1748 item 2 — what actually happened to the mail the call above requests.
//
// Deliberately takes NO argument. The endpoint reports on the SESSION's own
// address and reads no address from the request; passing one here would imply
// otherwise. That self-only shape is what lets it be specific (`suppressed` vs
// `sent`) without becoming the per-address oracle Better Auth's constant-time
// floor on /send-verification-email exists to prevent — see
// auth/verification-status.js's header.
//
// 401 when signed out and 503 when the auth service has no delivery log wired;
// both surface as a thrown error with `.status`, and the callers render
// nothing rather than inventing a state.
export const getVerificationStatus = () => authRequest('/api/auth/verification-status')

// ── #1420 follow-up: explicit account linking (Account Settings → Connected
// accounts) ────────────────────────────────────────────────────────────
//
// listAccounts/linkSocial/unlinkAccount call Better Auth's own /list-accounts,
// /link-social and /unlink-account endpoints directly — no custom backend
// route. The state/CSRF handshake for the OAuth round trip (the `state`
// param + its double-submit cookie) is entirely library-managed on both the
// initiate call here and the /callback/:id redirect target; nothing here
// hand-rolls any part of it.

export const listAccounts = () => authRequest('/api/auth/list-accounts')

// Redirect-based, same shape as signInSocial: POST for the authorize URL,
// then a full navigation (not an SPA transition) so the provider's consent
// screen is a real top-level page. callbackURL/errorCallbackURL land the
// browser back on Account Settings either way — the callback endpoint is
// what actually decides success/failure (email match, provider trust); this
// function only starts the round trip.
export async function linkSocial(provider, callbackURL, errorCallbackURL) {
  const result = await authRequest('/api/auth/link-social', {
    method: 'POST',
    body: JSON.stringify({ provider, callbackURL, errorCallbackURL, disableRedirect: true }),
  })
  if (!result?.url) throw new Error('OAuth provider did not return a redirect')
  window.location.assign(result.url)
}

// Better Auth's own /unlink-account refuses to remove an account's last
// remaining credential (FAILED_TO_UNLINK_LAST_ACCOUNT, 400) — server-
// enforced regardless of the UI guard in AccountSettings.jsx.
export const unlinkAccount = (providerId, accountId) => authRequest('/api/auth/unlink-account', {
  method: 'POST',
  body: JSON.stringify({ providerId, accountId }),
})

// ── #1367 (D2/D4): the account-management write surface ─────────────────
//
// Account Settings was read-only: no way to change an email or password, no
// way to see or end another session, no way to delete the account. All six
// calls below are Better Auth's OWN endpoints — no custom backend route,
// and nothing here re-implements password hashing, verification tokens, or
// session invalidation. The two that need server config to exist at all
// (/change-email, /delete-user) are switched on in auth/auth.js's `user`
// block; without it they answer 400/404, which is why that config and these
// exports have to land together.

// The address does NOT change when this resolves. Better Auth mails a link
// (to the current address first when it is already verified, otherwise
// straight to the new one) and only switches over once the NEW address is
// proven — see auth/auth.js's sendChangeEmailConfirmation.
//
// Anti-enumeration, same shape as requestPasswordReset above: an address
// that already belongs to another account returns the identical
// `{ status: true }` with no mail sent (better-auth/dist/api/routes/
// update-user.mjs:456-460). Callers must not branch their copy on the
// result.
export const changeEmail = (newEmail, callbackURL) => authRequest('/api/auth/change-email', {
  method: 'POST',
  body: JSON.stringify({ newEmail, callbackURL }),
})

// currentPassword is verified server-side against the credential row; a
// wrong one is a 400 with code INVALID_PASSWORD, and an account with no
// password at all (Google/GitHub-only) is CREDENTIAL_ACCOUNT_NOT_FOUND.
// revokeOtherSessions defaults ON: a password rotation that leaves every
// other signed-in device alive is not the thing a user changing their
// password believes they just did.
export const changePassword = (currentPassword, newPassword, revokeOtherSessions = true) => authRequest('/api/auth/change-password', {
  method: 'POST',
  body: JSON.stringify({ currentPassword, newPassword, revokeOtherSessions }),
})

// Fresh-session gated by the library itself (session.mjs:378
// `use: [freshSessionMiddleware]`), unlike the revoke calls below — so this
// is the one that throws SESSION_NOT_FRESH for most of a 7-day session's
// life, and callers must handle that rather than rendering an empty list.
export const listSessions = () => authRequest('/api/auth/list-sessions')

// `token` comes from listSessions() — it is the only handle Better Auth
// accepts. A token belonging to another account (or to nothing) is a 404
// with code SESSION_NOT_FOUND, from the ownership guard in auth/auth.js's
// hooks.before; the library alone would answer 200 for both without
// revoking anything, so a caller MUST NOT treat "no throw" as proof of a
// revocation unless that guard is in place.
export const revokeSession = (token) => authRequest('/api/auth/revoke-session', {
  method: 'POST',
  body: JSON.stringify({ token }),
})

export const revokeOtherSessions = () => authRequest('/api/auth/revoke-other-sessions', { method: 'POST' })

// Irreversible. `password` re-authenticates an account that has one; omit it
// for a Google/GitHub-only account, where Better Auth falls back to
// requiring a session younger than freshAge (SESSION_EXPIRED otherwise).
// What actually gets erased is the database's decision, not this call's —
// see account-deletion.js.
export const deleteAccount = (password) => authRequest('/api/auth/delete-user', {
  method: 'POST',
  body: JSON.stringify(password ? { password } : {}),
})
