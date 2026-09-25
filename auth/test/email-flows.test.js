// Email-verification and password-reset flows: token lifetimes, expiry,
// single-use, and the two ways the flows leak information.
//
// Why a second file rather than more cases in auth.test.js: that file covers
// "does the happy path work and is the mail sent" for both flows already
// (`signup sends a verification email even while enforcement is off`,
// `EMAIL_VERIFICATION_ENFORCED=true refuses unverified sign-in...`, `password
// reset dispatches reset mail...`). Nothing there exercises what happens to a
// token that should be REFUSED — expired, replayed — which is the half that
// carries the security claim, nor the two findings below. Same conventions as
// auth.test.js: real Better Auth endpoints against an in-memory sqlite
// adapter, faking only the mailer boundary.
//
// Written for the pre-flip audit of EMAIL_VERIFICATION_ENFORCED
// (`infra/ecs.tf`, currently "false"). Runbook for the live half:
// docs/runbooks/email-verification-validation.md.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'

import { createEmailVerificationToken } from 'better-auth/api'
import { getMigrations } from 'better-auth/db/migration'

import { createAuth } from '../auth.js'

const SECRET = 'test-only-secret-with-at-least-thirty-two-characters'

const env = {
  NODE_ENV: 'test',
  BETTER_AUTH_SECRET: SECRET,
  BETTER_AUTH_URL: 'http://localhost:3000',
  BETTER_AUTH_TRUSTED_ORIGINS: 'http://localhost:3000',
}

const PASSWORD = 'correct horse battery staple'

function capturingMailer() {
  const sent = []
  return { sent, kind: 'test', sender: 'no-reply@test', send: async message => { sent.push(message) } }
}

async function harness(overrides = {}, mailer = capturingMailer()) {
  const database = new DatabaseSync(':memory:')
  const auth = createAuth({ database, env: { ...env, ...overrides }, mailer })
  await (await getMigrations(auth.options)).runMigrations()
  return { auth, database, mailer }
}

async function signUp(auth, email, name = 'U') {
  const response = await auth.api.signUpEmail({ body: { email, password: PASSWORD, name }, asResponse: true })
  assert.equal(response.status, 200, `sign-up for ${email} failed: ${response.status}`)
  return response
}

function urlIn(text) {
  const url = text.match(/https?:\/\/\S+/)?.[0]
  assert.ok(url, `message carried no url: ${text}`)
  return url
}

function verificationTokenFrom(mailer) {
  const mail = mailer.sent.find(m => m.subject === 'Verify your Archimedes account')
  assert.ok(mail, `no verification mail among: ${JSON.stringify(mailer.sent.map(m => m.subject))}`)
  const token = new URL(urlIn(mail.text)).searchParams.get('token')
  assert.ok(token, 'verification url carried no token')
  return token
}

async function resetTokenFor(auth, mailer, email) {
  const before = mailer.sent.length
  const request = await auth.api.requestPasswordReset({
    body: { email, redirectTo: 'http://localhost:3000/reset-password' },
    asResponse: true,
  })
  assert.equal(request.status, 200)
  const mail = mailer.sent.slice(before).find(m => m.subject === 'Reset your Archimedes password')
  assert.ok(mail, `no reset mail among: ${JSON.stringify(mailer.sent.map(m => m.subject))}`)
  const url = urlIn(mail.text)
  const token = new URL(url).searchParams.get('token') ?? url.match(/\/reset-password\/([^/?]+)/)?.[1]
  assert.ok(token, `reset url carried no token: ${url}`)
  return token
}

// ── Token lifetimes are pinned in auth.js, not inherited from the library ──
//
// Same defect class as the session.freshAge finding in auth.test.js: a
// security-relevant duration that lives in a library default is not auditable
// from this repo, and a version bump can move it with nothing failing. Both
// tests assert the pinned literal AND that it reaches the wire, because a
// config value the library ignores would satisfy the first half alone.

test('the verification link\'s lifetime is pinned in auth.js and is what the mailed token actually carries', async () => {
  const { auth, mailer } = await harness()
  await signUp(auth, 'ttl-verify@example.com')

  // The literal, so a future edit has to change this number on purpose.
  assert.equal(auth.options.emailVerification.expiresIn, 60 * 60)

  // ...and the same number on the wire. createEmailVerificationToken signs a
  // JWT (email-verification.mjs:13), so `exp` is readable without the secret:
  // the middle segment is base64url JSON.
  const token = verificationTokenFrom(mailer)
  const payload = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'))
  const lifetimeSeconds = payload.exp - payload.iat
  assert.equal(lifetimeSeconds, 60 * 60, `mailed verification token lives ${lifetimeSeconds}s, not the pinned 3600s`)
})

test('the reset link\'s lifetime is pinned in auth.js and is what the stored row actually carries', async () => {
  const { auth, database, mailer } = await harness()
  await signUp(auth, 'ttl-reset@example.com')

  assert.equal(auth.options.emailAndPassword.resetPasswordTokenExpiresIn, 60 * 60)

  await resetTokenFor(auth, mailer, 'ttl-reset@example.com')
  const rows = database.prepare('SELECT createdAt, expiresAt FROM auth_verifications').all()
  assert.equal(rows.length, 1, `expected exactly one reset row, saw ${rows.length}`)
  // Rounded to the second: createdAt and expiresAt are stamped microseconds
  // apart, so the raw difference is 3599.99x, not 3600.
  const lifetimeSeconds = Math.round((Date.parse(rows[0].expiresAt) - Date.parse(rows[0].createdAt)) / 1000)
  assert.equal(lifetimeSeconds, 60 * 60, `stored reset row lives ~${lifetimeSeconds}s, not the pinned 3600s`)
})

// ── Expiry is actually enforced (the guard, shown rejecting) ──────────────

test('an EXPIRED verification token is refused, and leaves the account unverified', async () => {
  const { auth } = await harness({ EMAIL_VERIFICATION_ENFORCED: 'true' })
  const email = 'expired-verify@example.com'
  await signUp(auth, email)

  // Minted with the library's OWN token factory (the exact function
  // sendVerificationEmailFn calls, email-verification.mjs:28) so this is a
  // structurally valid, correctly-signed token whose ONLY defect is that it
  // expired 60 seconds ago. A hand-mangled string would prove nothing about
  // expiry — it would be refused for being malformed.
  const expired = await createEmailVerificationToken(SECRET, email, undefined, -60)
  const refused = await auth.api.verifyEmail({ query: { token: expired }, asResponse: true })
  assert.equal(refused.status, 401)
  assert.equal((await refused.json()).code, 'TOKEN_EXPIRED')

  // The refusal was real, not cosmetic: enforcement still locks the account out.
  const login = await auth.api.signInEmail({ body: { email, password: PASSWORD }, asResponse: true })
  assert.equal(login.status, 403)
})

test('an EXPIRED reset token is refused, and the old password still works', async () => {
  const { auth, database, mailer } = await harness()
  const email = 'expired-reset@example.com'
  await signUp(auth, email)
  const token = await resetTokenFor(auth, mailer, email)

  // Age the real stored row rather than mocking the clock — same technique
  // auth.test.js's "/link-social now requires the same session freshness"
  // uses on a session's createdAt. The row's identifier is stored HASHED
  // (verification.storeIdentifier: 'hashed' in auth.js), so it is updated by
  // id; there is only one verification row at this point.
  const past = new Date(Date.now() - 60_000).toISOString()
  const rows = database.prepare('SELECT id FROM auth_verifications').all()
  assert.equal(rows.length, 1)
  database.prepare('UPDATE auth_verifications SET expiresAt = ? WHERE id = ?').run(past, rows[0].id)

  const refused = await auth.api.resetPassword({
    body: { token, newPassword: 'attacker chosen password' },
    asResponse: true,
  })
  assert.equal(refused.status, 400)
  assert.equal((await refused.json()).code, 'INVALID_TOKEN')

  // Nothing changed: the old password still signs in and the attacker's does not.
  assert.equal((await auth.api.signInEmail({ body: { email, password: PASSWORD }, asResponse: true })).status, 200)
  assert.equal(
    (await auth.api.signInEmail({ body: { email, password: 'attacker chosen password' }, asResponse: true })).status,
    401,
  )
})

// ── Single use ────────────────────────────────────────────────────────────

test('a reset token is single-use: the second attempt is refused and cannot set a password', async () => {
  const { auth, mailer } = await harness()
  const email = 'single-use-reset@example.com'
  await signUp(auth, email)
  const token = await resetTokenFor(auth, mailer, email)

  const first = await auth.api.resetPassword({ body: { token, newPassword: 'first new password here' }, asResponse: true })
  assert.equal(first.status, 200)

  // Replay the SAME token with a DIFFERENT password. Asserting only the
  // status would not distinguish "refused" from "accepted but idempotent";
  // the second password must not become a working credential.
  const replay = await auth.api.resetPassword({ body: { token, newPassword: 'replayed second password' }, asResponse: true })
  assert.equal(replay.status, 400)
  assert.equal((await replay.json()).code, 'INVALID_TOKEN')

  assert.equal(
    (await auth.api.signInEmail({ body: { email, password: 'replayed second password' }, asResponse: true })).status,
    401,
  )
  assert.equal(
    (await auth.api.signInEmail({ body: { email, password: 'first new password here' }, asResponse: true })).status,
    200,
  )
})

// autoSignInAfterVerification is ON in auth.js, which makes the verification
// URL a bearer sign-in credential for its lifetime, not merely a flag-setter.
// That is a deliberate UX choice, but it is only defensible because the window
// closes on FIRST use — pinned here in both directions so neither half can
// regress silently. (Unlike a reset token, a verification token is a stateless
// JWT with no stored row, so nothing can revoke it before its TTL; what bounds
// it is exactly this early-return, email-verification.mjs:285-291.)
test('the verification link mints a session for whoever opens it FIRST, and only that once', async () => {
  const { auth, mailer } = await harness({ EMAIL_VERIFICATION_ENFORCED: 'true' })
  const email = 'bearer-link@example.com'
  await signUp(auth, email)
  const token = verificationTokenFrom(mailer)

  // No cookie, no prior session — the anonymous holder of the URL.
  const first = await auth.api.verifyEmail({ query: { token }, asResponse: true })
  assert.equal(first.status, 200)
  const setCookie = first.headers.get('set-cookie')
  assert.ok(setCookie, 'first open of the verification link set no session cookie')
  const session = await auth.api.getSession({ headers: new Headers({ cookie: setCookie.split(';', 1)[0] }) })
  assert.equal(session?.user?.email, email)
  assert.equal(session.user.emailVerified, true)

  // Replay: the account is already verified, so the library early-returns
  // before the auto-sign-in block and no second session is minted.
  const replay = await auth.api.verifyEmail({ query: { token }, asResponse: true })
  assert.equal(replay.status, 200)
  assert.equal(replay.headers.get('set-cookie'), null, 'a replayed verification link minted a second session')
})

// ── FINDING EV-4: a completed reset does not imply a verified address ──────
//
// Documented, not asserted-as-desirable. Completing a reset proves control of
// the mailbox — the same proof the verification link carries — but Better
// Auth's resetPassword (password.mjs:150-174) never touches emailVerified, and
// auth.js configures no `onPasswordReset` hook that would. The consequence
// only becomes visible the day EMAIL_VERIFICATION_ENFORCED flips: a user who
// successfully recovers their password is STILL refused at sign-in, with an
// error about verification they have arguably just satisfied. Pinned here so
// the flip runbook's expected-outcome table is checkable, and so a later
// decision to close this is a deliberate change with a failing test to update.
test('FINDING: completing a password reset does NOT mark the address verified, so enforcement still refuses the sign-in', async () => {
  const { auth, mailer } = await harness({ EMAIL_VERIFICATION_ENFORCED: 'true' })
  const email = 'reset-then-locked-out@example.com'
  await signUp(auth, email)

  const token = await resetTokenFor(auth, mailer, email)
  const reset = await auth.api.resetPassword({ body: { token, newPassword: 'a brand new password' }, asResponse: true })
  assert.equal(reset.status, 200)

  const login = await auth.api.signInEmail({ body: { email, password: 'a brand new password' }, asResponse: true })
  assert.equal(login.status, 403, 'a reset now verifies the address — good, but this test documents the opposite')
  assert.equal((await login.json()).code, 'EMAIL_NOT_VERIFIED')
})

// ── The anonymous resend path ─────────────────────────────────────────────
//
// POST /api/auth/send-verification-email is reachable with NO session
// (email-verification.mjs:95-117) and is what the UI's "Resend verification
// email" control calls — a control that only carries weight once enforcement
// is on. Better Auth defends it with a 500ms constant-time FLOOR so that
// "unknown or already-verified" (fast local JWT sign) and "known and
// unverified" (real send) are indistinguishable. A floor only hides what it is
// larger than: with auth.js awaiting mailer.send() inside sendVerificationEmail
// — which is what it did before this file existed — a real SES round trip
// pushed the known-and-unverified case straight through it. Measured against
// this same 900ms mailer at that revision: unknown 504ms, known-unverified
// 922ms. Fire-and-forget (see the comment on sendVerificationEmail in auth.js)
// is what puts both shapes back on the floor.

test('the anonymous resend path does not leak account existence through response time', async () => {
  const SLOW_MS = 900
  const mailer = { kind: 'test', sender: 'x', send: async () => { await new Promise(resolve => setTimeout(resolve, SLOW_MS)) } }
  const { auth } = await harness({}, mailer)
  const known = 'timing-resend-known@example.com'
  await signUp(auth, known)

  async function timed(email) {
    const start = performance.now()
    const response = await auth.api.sendVerificationEmail({ body: { email }, asResponse: true })
    assert.equal(response.status, 200)
    return performance.now() - start
  }

  const unknownMs = await timed('timing-resend-nobody@example.com')
  const knownMs = await timed(known)

  // Both must sit on the library's own 500ms floor. Budget generously
  // against scheduler jitter while staying far under SLOW_MS, so an awaited
  // send (which adds the full 900ms to the known case only) cannot pass.
  assert.ok(
    Math.abs(knownMs - unknownMs) < SLOW_MS / 3,
    `resend timing distinguishes a real unverified account (${Math.round(knownMs)}ms) from an unknown address `
    + `(${Math.round(unknownMs)}ms) against a ${SLOW_MS}ms mailer — the send is being awaited again`,
  )
})

test('the anonymous resend path sends nothing for an unknown address, and answers identically', async () => {
  const { auth, mailer } = await harness()
  const known = 'resend-known@example.com'
  await signUp(auth, known)
  const afterSignup = mailer.sent.length

  const unknown = await auth.api.sendVerificationEmail({ body: { email: 'resend-nobody@example.com' }, asResponse: true })
  const unknownBody = await unknown.json()
  assert.equal(mailer.sent.length, afterSignup, 'an unknown address triggered a send')

  const real = await auth.api.sendVerificationEmail({ body: { email: known }, asResponse: true })
  const realBody = await real.json()
  assert.equal(mailer.sent.length, afterSignup + 1)

  assert.equal(unknown.status, real.status)
  assert.deepEqual(unknownBody, realBody)
})

// ── What the enforcement flag does and does not gate (source-pinned) ───────

test('requireEmailVerification gates the email/password sign-in and nothing else that issues a session (source-pinned)', async () => {
  const src = readFileSync(new URL('../node_modules/better-auth/dist/api/routes/sign-in.mjs', import.meta.url), 'utf8')
  // The one gate. If a bump moves or removes it, the flip runbook's
  // "who gets locked out" section is wrong and this fails loudly.
  assert.match(src, /options\?\.emailAndPassword\?\.requireEmailVerification && !user\.user\.emailVerified/)
  assert.equal((src.match(/requireEmailVerification/g) ?? []).length, 1)

  // The social path in the same file reads it nowhere, which is why an
  // OAuth-only account is unaffected by the flip.
  const callback = readFileSync(new URL('../node_modules/better-auth/dist/api/routes/callback.mjs', import.meta.url), 'utf8')
  assert.equal(callback.includes('requireEmailVerification'), false)
})

test('a refused sign-in does not silently re-send verification mail — the user must ask for it', async () => {
  const { auth, mailer } = await harness({ EMAIL_VERIFICATION_ENFORCED: 'true' })
  const email = 'no-auto-resend@example.com'
  await signUp(auth, email)
  const afterSignup = mailer.sent.length

  // sendOnSignIn is deliberately not configured (sign-in.mjs:314 gates the
  // auto-resend on it), so a refused sign-in is inert. Ten refusals must not
  // become ten messages to an address that may not want them — the
  // bounce/complaint rate that decides whether SES production access
  // survives is measured on exactly this kind of traffic.
  assert.equal(auth.options.emailVerification.sendOnSignIn, undefined)
  for (let i = 0; i < 3; i += 1) {
    const refused = await auth.api.signInEmail({ body: { email, password: PASSWORD }, asResponse: true })
    assert.equal(refused.status, 403)
  }
  assert.equal(mailer.sent.length, afterSignup, 'a refused sign-in re-sent verification mail on its own')
})

// ── EV-1 (#1691): the production rate limiter's client-IP resolution ───────
//
// Better Auth keys every rate-limit bucket on `${ip}|${path}`
// (@better-auth/core/dist/utils/ip.mjs:225 createRateLimitKey) and resolves
// that ip from the headers named in `advanced.ipAddress.ipAddressHeaders`,
// trusting a value only when it is SINGLE-valued (ip.mjs:189 `if
// (forwardedIps.length !== 1) return null`) — every other token in a forwarded
// chain is client-supplied and therefore spoofable, so refusing is correct.
//
// The finding: the default header list is ['x-forwarded-for'], and in
// production that header is never single-valued — CloudFront sets the viewer,
// the ALB appends the edge it saw, nginx appends `$proxy_add_x_forwarded_for`.
// No ip resolved, so `no-trusted-ip` was substituted and the entire internet
// shared one bucket per path: three password-reset requests from anywhere
// exhausted /request-password-reset for everybody.
//
// The fix (option A in #1691): nginx SETS `X-Client-IP: $remote_addr` — its
// realip-resolved address, bound to the trusted ALB CIDR — and auth.js points
// ipAddressHeaders at that one header. The tests below are a matched set:
//   1. control      — buckets separate per client when X-Client-IP resolves,
//                     WITH the multi-hop X-Forwarded-For production sends.
//   2. adversarial  — a caller rotating X-Forwarded-For, and forging its own
//                     X-Client-IP as an XFF token, buys no extra bucket.
//   3. adversarial  — nginx OVERWRITES a client-supplied X-Client-IP, so a
//                     spoof from a non-edge source never reaches this process
//                     (source-pinned against nginx/nginx.conf).
//   4. fail-safe    — no X-Client-IP at all (a request that skipped nginx)
//                     falls back to the shared bucket, i.e. over-limits rather
//                     than handing the caller a key it controls.
//   5. mechanism    — the library-level reads the four above rest on.
// Test 1 is the mutation guard: revert either half of the fix and it fails.
//
// NOTE ON FIDELITY: these run with the process's own NODE_ENV unset, so
// @better-auth/core's isTest()/isDevelopment() are both false
// (env-impl.mjs:33-36) and getIp takes the same branch it takes in the
// container. Rate limiting itself is switched on by the NODE_ENV passed to
// createAuth (`rateLimit.enabled = production` in auth.js), which is why these
// pass NODE_ENV: 'production' in the env object.

// The header shape nginx produces behind CloudFront + ALB: viewer, CloudFront
// edge, nginx's own peer. Never single-valued, which is the whole finding.
const MULTI_HOP_XFF = clientToken => `${clientToken}, 70.132.1.2, 10.0.3.4`

async function signUpOverHttp(auth, email, headers) {
  const response = await auth.handler(new Request('http://localhost:3000/api/auth/sign-up/email', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      origin: 'http://localhost:3000',
      ...headers,
    },
    body: JSON.stringify({ email, password: PASSWORD, name: 'U' }),
  }))
  return response.status
}

test('rate-limit buckets separate per client behind the multi-hop header production sends', async () => {
  const { auth } = await harness({ NODE_ENV: 'production' })
  // Both clients carry the three-hop X-Forwarded-For the live chain produces —
  // the header that used to defeat resolution entirely. What separates them is
  // the single-valued X-Client-IP nginx sets from $remote_addr.
  //
  // MUTATION GUARD (#1691, CLAUDE.md "a guard must be shown to reject
  // something"): drop `ipAddress.ipAddressHeaders` from auth.js's advanced
  // block — or point it back at 'x-forwarded-for' — and client B's FIRST
  // request 429s on client A's spending, failing this test on the assertion
  // below. Removing nginx's `proxy_set_header X-Client-IP` has the same effect
  // in production and is pinned separately by the nginx test further down.
  const a = { 'x-client-ip': '203.0.113.7', 'x-forwarded-for': MULTI_HOP_XFF('203.0.113.7') }
  const b = { 'x-client-ip': '198.51.100.9', 'x-forwarded-for': MULTI_HOP_XFF('198.51.100.9') }

  // '/sign-up/email' is 3 per 600s in auth.js's customRules.
  for (let i = 1; i <= 3; i += 1) {
    assert.equal(await signUpOverHttp(auth, `edge-a${i}@example.com`, a), 200)
  }
  assert.equal(await signUpOverHttp(auth, 'edge-a4@example.com', a), 429, 'client A was not limited')

  // An entirely unrelated client, first request of its life. Under the finding
  // this was a 429; it is what the per-rate-key claim in auth.js's rateLimit
  // comment and docs/account-authentication.md actually asserts.
  assert.equal(
    await signUpOverHttp(auth, 'edge-b1@example.com', b),
    200,
    'client B was rate-limited by client A — the client IP is not resolving; EV-1 has regressed',
  )
})

test('SECURITY: forging X-Forwarded-For buys no extra bucket — the key follows the nginx-set header', async () => {
  const { auth } = await harness({ NODE_ENV: 'production' })
  // One abuser at one address, rotating every token it controls: a fresh
  // leftmost XFF hop per request, and — belt and braces — its own X-Client-IP
  // planted in the chain, which is precisely the value an attacker would forge
  // if the leftmost token were trusted. nginx's X-Client-IP is the only thing
  // read, and it does not move.
  const spoof = i => ({
    'x-client-ip': '203.0.113.7',
    'x-forwarded-for': `10.0.0.${i}, 198.51.100.${i}, 70.132.1.2, 10.0.3.4`,
  })

  for (let i = 1; i <= 3; i += 1) {
    assert.equal(await signUpOverHttp(auth, `spoof-a${i}@example.com`, spoof(i)), 200)
  }
  assert.equal(
    await signUpOverHttp(auth, 'spoof-a4@example.com', spoof(4)),
    429,
    'rotating X-Forwarded-For minted a fresh bucket — a spoofable token is being trusted (#1691 anti-goal)',
  )
})

test('SECURITY: nginx SETS X-Client-IP, so a spoof from a non-edge source never reaches this process', async () => {
  // The auth container trusts x-client-ip because nothing outside the edge can
  // write it. That property lives in nginx.conf, so it is pinned there —
  // source-pinned the same way the better-auth reads above are, and the same
  // way auth.test.js pins ../../.env.example.
  const conf = readFileSync(new URL('../../nginx/nginx.conf', import.meta.url), 'utf8')

  // `proxy_set_header` REPLACES the header; `$proxy_add_x_forwarded_for`
  // (used one line below for X-Forwarded-For) is what APPENDS a client value.
  // X-Client-IP must never be built that way.
  assert.match(conf, /^\s*proxy_set_header X-Client-IP \$remote_addr;$/m)
  assert.equal(/X-Client-IP\s+\$proxy_add_x_forwarded_for/.test(conf), false)
  assert.equal((conf.match(/proxy_set_header X-Client-IP/g) ?? []).length, 1)

  // $remote_addr is only ever influenced by X-Forwarded-For when the socket
  // peer is inside the ALB CIDR: `real_ip_header` is bound by set_real_ip_from,
  // and that CIDR is the narrowed one from AUDIT I7 (not the RFC1918 ranges,
  // which would have let the box itself spoof). A request from any other
  // source contributes nothing to the value it is keyed on.
  assert.match(conf, /^\s*set_real_ip_from 10\.0\.0\.0\/16;$/m)
  assert.match(conf, /^\s*real_ip_header X-Forwarded-For;$/m)
  assert.equal(/set_real_ip_from (?!10\.0\.0\.0\/16)/.test(conf), false)
})

test('fail-safe: a request that never passed through nginx gets the shared bucket, not a key it controls', async () => {
  const { getIp } = await import('@better-auth/core/utils/ip')
  const options = { advanced: { ipAddress: { ipAddressHeaders: ['x-client-ip'] } } }
  const request = headers => new Request('https://archimedes-arc.com/api/auth/request-password-reset', { headers })

  // No x-client-ip (only the spoofable chain): null, which the limiter turns
  // into the shared `no-trusted-ip|<path>` bucket. Over-limiting, never open.
  assert.equal(getIp(request({ 'x-forwarded-for': '203.0.113.7' }), options), null)
  assert.equal(getIp(request({}), options), null)
  // And x-forwarded-for is not consulted as a fallback: a single-valued XFF is
  // exactly what a direct-to-container caller would forge.
  assert.equal(getIp(request({ 'x-forwarded-for': '203.0.113.7', 'x-client-ip': '198.51.100.9' }), options), '198.51.100.9')
})

test('EV-1 mechanism: ipAddressHeaders is what makes the key resolve, and it is pinned in auth.js', async () => {
  const { getIp } = await import('@better-auth/core/utils/ip')
  const request = headers => new Request('https://archimedes-arc.com/api/auth/sign-in/email', { headers })

  // The finding, unchanged and still true of the DEFAULT header list: a
  // multi-hop forwarded header resolves to no client IP.
  assert.equal(getIp(request({ 'x-forwarded-for': '203.0.113.7' }), {}), '203.0.113.7')
  assert.equal(getIp(request({ 'x-forwarded-for': '203.0.113.7, 70.132.1.2' }), {}), null)
  assert.equal(getIp(request({ 'x-forwarded-for': MULTI_HOP_XFF('203.0.113.7') }), {}), null)

  // The fix: a single-valued header nginx controls, named explicitly.
  const { auth } = await harness({ NODE_ENV: 'production' })
  assert.deepEqual(auth.options.advanced.ipAddress.ipAddressHeaders, ['x-client-ip'])
  assert.equal(auth.options.advanced.ipAddress.disableIpTracking, undefined)
  assert.equal(auth.options.advanced.ipAddress.trustedProxies, undefined)
  assert.equal(getIp(request({ 'x-client-ip': '203.0.113.7', 'x-forwarded-for': MULTI_HOP_XFF('203.0.113.7') }), auth.options), '203.0.113.7')

  // The mail endpoints the EMAIL_VERIFICATION_ENFORCED flip puts under load
  // are pinned explicitly rather than inherited (#1691 scope item 5); the
  // values match better-auth/dist/api/rate-limiter/index.mjs:378-382.
  assert.deepEqual(auth.options.rateLimit.customRules['/request-password-reset'], { window: 60, max: 3 })
  assert.deepEqual(auth.options.rateLimit.customRules['/send-verification-email'], { window: 60, max: 3 })
})
