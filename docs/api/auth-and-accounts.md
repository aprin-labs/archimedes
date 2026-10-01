# Account Authentication API

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-01

`/api/auth/*` — canonical account identity: email/password and (when configured) Google/GitHub
OAuth sign-up, sign-in, session lookup, and email verification.

**This is the Better Auth Node sidecar (`auth/`), not a FastAPI router.** `auth/auth.js`
configures Better Auth `1.6.25` on Node 22; `auth/server.js` runs it as a standalone HTTP
service (`archimedes-auth`) beside nginx and FastAPI in the same deploy. nginx owns public
namespace routing and proxies the entire `/api/auth/` prefix straight to this sidecar
(`nginx/nginx.conf`: `location /api/auth/ { proxy_pass http://auth_service/api/auth/; }`) —
it never passes through FastAPI. FastAPI is a *consumer* of this service, not its host:
`require_current_user` (`backend/archimedes/api/account_auth.py`) authenticates every
private FastAPI route by forwarding the caller's `cookie` header to this sidecar's own
`GET /api/auth/get-session`, over the internal ECS loopback
(`BETTER_AUTH_INTERNAL_URL`, default `http://127.0.0.1:3000`), and trusting only a live,
non-expired result. FastAPI never parses or signs the session cookie itself.

**Auth model.** A successful `sign-in/email` (or a completed OAuth callback) sets the
`better-auth.session_token` cookie: `HttpOnly`, `Secure` in production, `SameSite=Lax`,
7-day `expiresIn` with a 1-day `updateAge`. There is no bearer-token flow for browser
clients — every route below is either `anonymous` or reads that one cookie; a curl cookie
jar (`-c`/`-b`) round-trips it exactly like a browser would.

**Do not confuse this with the legacy SIWE router.** FastAPI separately defines routes
literally named `/api/auth/nonce`, `/api/auth/verify`, `/api/auth/logout`, and
`/api/auth/session` in `archimedes.api.auth_siwe` — a wallet-signature login path that
predates Better Auth. That router is mounted **only when the `TESTING` env var is
truthy** (`main.py`, "Legacy SIWE router remains test-only so signature-verification
regression tests exercise its reusable proof helpers without exposing wallet login
live") — it 404s in every real environment, including production, where nginx's
`/api/auth/` block already claims the whole prefix for this sidecar. The two surfaces
share a URL prefix only under `TESTING`; they never collide live. **This document covers
the Better Auth sidecar only** — the SIWE test-only routes belong to a different
capability area and are not documented here.

---

### POST /api/auth/sign-up/email
Create a new email/password account. | **Auth**: anonymous | **Flags**: production-only
rate limit 3 signups / 10 min (`auth/auth.js` `rateLimit.customRules['/sign-up/email']`;
Better Auth's `rateLimit.enabled` is `NODE_ENV==='production'` only), plus nginx's own
`/api/auth/` `limit_req` zone

Request: JSON body `{name: str, email: str, password: str (12-128 chars), image?: str,
callbackURL?: str, rememberMe?: bool}`.
Response: `{token: str|null, user: {id, email, name, image, emailVerified, ...}}`, HTTP
200 — no `set-cookie` (registration alone does not start a session; `autoSignIn: false`).
Errors:
- `400` — email already registered, or password outside the 12–128 char bound.
- `429` — signup rate limit exceeded (production only).

A verification email is sent on **every** signup regardless of enforcement (SES in
deployed environments, a console mailer that logs the link locally) — see
`docs/account-authentication.md`. Sign-in refusal for an unverified account is a separate,
env-gated behavior (see `POST /api/auth/sign-in/email` below).

```bash
curl -sS -c /tmp/session.jar -X POST http://localhost:8080/api/auth/sign-up/email \
  -H 'Content-Type: application/json' \
  -d '{"name":"Dan","email":"dan@example.com","password":"correct horse battery staple"}'
```

### POST /api/auth/sign-in/email
Authenticate with email + password and start a session. | **Auth**: anonymous | **Flags**:
production-only rate limit 10 / minute (`rateLimit.customRules['/sign-in/email']`);
refuses unverified accounts only when `EMAIL_VERIFICATION_ENFORCED=true` (currently
`false` in production — the AWS account is in the SES sandbox, per
`docs/account-authentication.md`)

Request: JSON body `{email: str, password: str, callbackURL?: str, rememberMe?: bool=true}`.
Response: `{redirect: false, token: str, url: str|null, user: {...}}`, HTTP 200; sets the
`better-auth.session_token` cookie.
Errors:
- `401` — wrong credentials or unknown email (a password hash is computed either way, so
  the response doesn't leak which side was wrong).
- `403` — `EMAIL_NOT_VERIFIED`, only when `EMAIL_VERIFICATION_ENFORCED=true` and the
  account hasn't clicked its verification link yet.
- `429` — sign-in rate limit exceeded (production only).

```bash
curl -sS -c /tmp/session.jar -X POST http://localhost:8080/api/auth/sign-in/email \
  -H 'Content-Type: application/json' \
  -d '{"email":"dan@example.com","password":"correct horse battery staple"}'
```

### POST /api/auth/sign-out
Revoke the current session and clear its cookie. | **Auth**: anonymous (a request with no
session cookie still returns 200 — there is nothing to revoke)

Request: none (cookie only).
Response: `{success: true}`, HTTP 200 — the DB-backed `auth_sessions` row is deleted and the
cookie cleared.
Errors: none raised.

```bash
curl -sS -b /tmp/session.jar -X POST http://localhost:8080/api/auth/sign-out
```

### GET /api/auth/get-session
Read the caller's current session, if any. | **Auth**: anonymous (returns `null`, not
401, when unauthenticated) | **Flags**: this is the exact endpoint FastAPI's
`require_current_user` calls internally to authenticate every private FastAPI route — the
single source of truth for "is this caller logged in" across the whole app

Request: none (cookie only).
Response: `{session: {id, token, expiresAt, ...}, user: {id, name, email, emailVerified,
image, ...}}` on a live session; bare `null` (still HTTP 200) otherwise.
Errors: none raised — `null` is the "not authenticated" signal, not an error.

```bash
curl -sS -b /tmp/session.jar http://localhost:8080/api/auth/get-session
```

### GET /api/auth/verify-email
Verify an emailed verification token, marking the account's email verified. | **Auth**:
anonymous (the token itself is the proof) | **Flags**: runs regardless of
`EMAIL_VERIFICATION_ENFORCED` — that flag only gates whether `sign-in/email` refuses an
unverified account, not whether this endpoint works

Request: query params `token: str` (required — the token from the emailed link),
`callbackURL?: str`.
Response: `{user: {...}, status: true}`, HTTP 200 — or, when `callbackURL` was supplied, an
HTTP redirect to it (`?error=<code>` appended on failure instead of a JSON error body).
Errors:
- `401` `TOKEN_EXPIRED` — link expired.
- `401` `INVALID_TOKEN` — malformed or tampered token.
- `401` `USER_NOT_FOUND` — token's email no longer matches an account.

```bash
curl -sS "http://localhost:8080/api/auth/verify-email?token=<token-from-emailed-link>"
```

### GET /api/auth/verification-status
What actually happened to this account's verification mail. | **Auth**: session required
(reports on `session.user.email` and reads **no** address from the request) | **Flags**:
the suppression half is live only with `EMAIL_MAILER=ses`; under the console mailer it
answers `suppression.checked: false`, never "clean"

Added by [#1748](https://github.com/aprin-labs/archimedes/issues/1748) item 2, because
`POST /api/auth/send-verification-email` answers `200 {status: true}` forever — including
for an address Amazon SES has already dropped onto the account suppression list, where SES
accepts the send, returns a MessageId, and bins the message.

**Why a separate GET rather than a richer response on the resend POST.** That POST is
reachable with no session and Better Auth defends it with a 500ms constant-time floor so
"unknown or already-verified" and "known and unverified" are indistinguishable to an
anonymous caller (`auth/auth.js` `sendVerificationEmail` is fire-and-forget for the same
reason — the measured leak was 504ms vs 922ms). Teaching that response to say `suppressed`
vs `sent` would hand any anonymous caller a per-address oracle. This endpoint requires a
live session and answers only about that session's own address, so it can be specific.

Request: none (cookie only). Any query string is ignored.
Response, HTTP 200:
`{state, email, bounce: {at, kind}, sends, sendsInWindow, lastSentAt, lastError,
retryAfterSeconds, checkSpam, suppression: {checked, suppressed, reason, since, detail},
resendWindowSeconds, resendWindowMax}`.

`state` is one of, in precedence order:
- `bounced` — SES **pushed** us a permanent bounce or a spam complaint for this address, and
  the consumer recorded it on the user row ([#1804](https://github.com/aprin-labs/archimedes/issues/1804)).
  `bounce.at` is the ISO timestamp, `bounce.kind` is `bounce` (the mailbox does not exist) or
  `complaint` (a human told their provider our mail is spam). Resending cannot work, and the
  same address is refused at signup and at the signed-in resend with `EMAIL_ADDRESS_BOUNCED` /
  `EMAIL_ADDRESS_COMPLAINED`. It outranks `suppressed` because it is the same conclusion from a
  stronger source, and because it still answers when the suppression lookup cannot run.
- `suppressed` — SESv2 `GetSuppressedDestination` says this address is on the account
  suppression list (`suppression.reason` is `BOUNCE` or `COMPLAINT`). Resending cannot work.
- `failed` — the most recent send threw; `lastError` carries the AWS SDK error **name**
  (never its message). Nothing went out.
- `rate_limited` — the recorded sends fill the resend window; `retryAfterSeconds` says how
  long until the limiter would accept another.
- `sent` — the mailer **accepted** the last send. Acceptance, not delivery. `checkSpam`
  turns on after two recorded sends.
- `unknown` — no send on record. `sends: 0` means "none was ever made"; `sends: null` means
  "the delivery log could not be read" — deliberately different values.
- `verified` — the account is already verified; there is nothing pending to describe.

Errors:
- `401` — no live session.
- `503` — the service has no delivery log wired (fail-closed; it never invents a `sent`).

Rows come from `auth_email_deliveries` (migration `d4b1f7c8e206`), written one per send by
`auth/mailer.js`; the decision logic is `auth/verification-status.js` and is unit-tested in
`auth/test/delivery-feedback.test.js`. `bounce` comes from `auth_users.emailBouncedAt` /
`emailBounceKind` (migration `e6b2a19c4d70`), written only by
`python -m archimedes.scripts.ses_events drain` — see
[`docs/runbooks/ses-bounce-signal.md`](../runbooks/ses-bounce-signal.md).

"The last attempt" is the row with the highest `seq` — a `BIGINT GENERATED BY DEFAULT AS
IDENTITY` the **database** assigns, not a timestamp. `created_at` is millisecond-resolution,
back-to-back sends routinely share one, and the auth service can run more than one task, so
no key a task computes for itself can order another task's writes. `created_at` is what the
24h window and `retryAfterSeconds` are measured from.

```bash
curl -sS -b /tmp/session.jar http://localhost:8080/api/auth/verification-status
```

### GET /api/auth/providers
Which login methods are enabled right now — drives which buttons the sign-in/sign-up UI
renders. | **Auth**: anonymous | **Flags**: served directly by `auth/server.js`, not by a
generic Better Auth route

Request: none.
Response: `{emailPassword: true, google: bool, github: bool, passkey: false}`.
`google`/`github` are `true` only when *both* halves of that provider's credential pair
(`GOOGLE_CLIENT_ID`+`GOOGLE_CLIENT_SECRET`, or the GitHub equivalent) are set;
`passkey` is hardcoded `false` — not wired.
Errors: none.

```bash
curl -sS http://localhost:8080/api/auth/providers
```

### POST /api/auth/sign-in/social
Start an OAuth sign-in with Google or GitHub. | **Auth**: anonymous | **Flags**: only
usable for a provider `GET /api/auth/providers` reports `true` for — check there first

Request: JSON body `{provider: "google"|"github", callbackURL?: str, disableRedirect?: bool}`.
Response: `{url: str, redirect: bool}` — the provider's OAuth authorize URL; the browser is
redirected there directly unless `disableRedirect: true`.
Errors: `404` `PROVIDER_NOT_FOUND` — `provider` isn't configured (missing client ID/secret
pair).

```bash
curl -sS -X POST http://localhost:8080/api/auth/sign-in/social \
  -H 'Content-Type: application/json' \
  -d '{"provider":"google","callbackURL":"https://archimedes-arc.com/app"}'
```

### GET /api/auth/callback/{provider}
OAuth redirect target — completes the provider handshake and sets the session cookie. |
**Auth**: anonymous (the OAuth `state`/`code` round-trip is the proof) | **Flags**: only
reachable for a configured provider; callback URLs are `https://<domain>/api/auth/callback/google`
and `.../github` (`docs/account-authentication.md`)

Request: path `provider: "google"|"github"`; query params set by the OAuth provider
(`code`, `state`, ...) — never called directly by a client.
Response: HTTP redirect to `callbackURL` (or the app default) with the session cookie set
on success.
Errors: redirects to the callback URL with `?error=<code>` appended on failure, rather than
returning a JSON error body.

```bash
# Not called directly — the browser lands here via redirect from Google/GitHub
# after POST /api/auth/sign-in/social. Shown for completeness only.
```

---

See also: `docs/account-authentication.md` (topology, login configuration, migration and
rollback) and `docs/security/auth-model.md` (trust boundaries across the whole app).
