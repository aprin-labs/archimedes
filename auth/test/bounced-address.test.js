// An address SES has reported dead is refused, with a reason (#1804).
//
// The fact itself is written by `archimedes.scripts.ses_events` draining the
// SES bounce/complaint queue (infra/ses_events.tf) — never by this service.
// These tests write the two columns directly, which is exactly what the
// consumer does, and then drive the REAL Better Auth endpoints.
//
// What each test is defending:
//
//   * Before this, a signup for a mailbox that does not exist was answered
//     with better-auth's generic duplicate response — 200, a synthetic user,
//     no token, nothing written (sign-up.mjs's
//     `shouldReturnGenericDuplicateResponse`, which `autoSignIn: false` turns
//     on). Someone who typos their address therefore gets a cheerful success,
//     never receives the mail SES silently drops, retries the same typo, and
//     gets the same success forever, with `emailVerified` — and so the free
//     tier — stuck false. So the test that matters is not "signup is refused"
//     but "signup is refused DIFFERENTLY from the generic duplicate answer".
//   * The anonymous resend path must keep answering 200 for everyone. Its
//     uniform response is what stops /send-verification-email being an
//     account-existence oracle (see email-flows.test.js). Refusing there would
//     trade this issue's problem for a worse one.
//   * Nothing a caller sends may set its own bounce state.
//
// Same conventions as auth.test.js: real endpoints, in-memory SQLite, Better
// Auth's own migrations (which create the two additional-field columns from
// auth.js's declaration — so a drift between that declaration and the Alembic
// migration shows up here as a missing column, not in production).

import assert from 'node:assert/strict'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'

import { getMigrations } from 'better-auth/db/migration'

import { BOUNCE_REFUSALS, bounceRefusal, createAuth } from '../auth.js'

const env = {
  NODE_ENV: 'test',
  BETTER_AUTH_SECRET: 'test-only-secret-with-at-least-thirty-two-characters',
  BETTER_AUTH_URL: 'http://localhost:3000',
  BETTER_AUTH_TRUSTED_ORIGINS: 'http://localhost:3000',
}

const PASSWORD = 'correct horse battery staple'

function capturingMailer() {
  const sent = []
  return { sent, kind: 'test', sender: 'no-reply@test', send: async message => { sent.push(message) } }
}

async function harness(overrides = {}) {
  const database = new DatabaseSync(':memory:')
  const mailer = capturingMailer()
  const auth = createAuth({ database, env: { ...env, ...overrides }, mailer })
  await (await getMigrations(auth.options)).runMigrations()
  return { auth, database, mailer }
}

async function signUp(auth, email, name = 'U') {
  const response = await auth.api.signUpEmail({ body: { email, password: PASSWORD, name }, asResponse: true })
  assert.equal(response.status, 200, `sign-up for ${email} failed: ${response.status}`)
  return response
}

function cookieHeader(response) {
  return response.headers.get('set-cookie').split(';', 1)[0]
}

// Driven through `auth.handler`, not `auth.api.*`: an APIError thrown from
// hooks.before propagates out of the direct api caller as a JS exception, and
// only the HTTP handler turns it into the status + {code, message} body a
// browser (and ui/src/auth-client.js) actually sees. Same helper shape as
// auth.test.js's postJson, origin header included for the same reason — Better
// Auth's originCheck middleware would otherwise 403 first and every assertion
// below would pass for the wrong reason.
function postJson(auth, path, requestBody, cookie = '') {
  return auth.handler(new Request(`http://localhost:3000${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', cookie, origin: 'http://localhost:3000' },
    body: JSON.stringify(requestBody),
  }))
}

// Exactly what `archimedes.scripts.ses_events` writes: a timestamp and a kind,
// straight onto the user row, through no API of this service's.
function stamp(database, email, kind, at = '2026-09-02T10:00:00.000Z') {
  const statement = database.prepare(
    'UPDATE auth_users SET emailBouncedAt = ?, emailBounceKind = ? WHERE lower(email) = lower(?)',
  )
  const result = statement.run(at, kind, email)
  assert.equal(Number(result.changes), 1, `no auth_users row for ${email} to stamp`)
}

function bounceState(database, email) {
  return database
    .prepare('SELECT emailBouncedAt, emailBounceKind FROM auth_users WHERE lower(email) = lower(?)')
    .get(email)
}

async function body(response) {
  return JSON.parse(await response.text())
}

// ── the pure resolver ──────────────────────────────────────────────────────

test('bounceRefusal answers null for every address SES has not complained about', () => {
  assert.equal(bounceRefusal(undefined), null)
  assert.equal(bounceRefusal({}), null)
  // The unverified-but-fine case — the one this must never refuse.
  assert.equal(bounceRefusal({ email: 'a@b.com', emailVerified: false, emailBouncedAt: null }), null)
})

test('bounceRefusal distinguishes a dead mailbox from a spam complaint', () => {
  const at = new Date()
  assert.equal(bounceRefusal({ emailBouncedAt: at, emailBounceKind: 'bounce' }).code, 'EMAIL_ADDRESS_BOUNCED')
  assert.equal(bounceRefusal({ emailBouncedAt: at, emailBounceKind: 'complaint' }).code, 'EMAIL_ADDRESS_COMPLAINED')
  assert.notEqual(BOUNCE_REFUSALS.bounce.message, BOUNCE_REFUSALS.complaint.message)
})

test('an unrecognised kind still refuses — the timestamp is the fact', () => {
  // A future SES event type the consumer learns to record must not silently
  // turn the refusal off; the kind only chooses the wording.
  const refusal = bounceRefusal({ emailBouncedAt: new Date(), emailBounceKind: 'something-new' })
  assert.equal(refusal.code, 'EMAIL_ADDRESS_BOUNCED')
})

// ── signup ─────────────────────────────────────────────────────────────────

test('signup for a bounced address is refused with EMAIL_ADDRESS_BOUNCED, not a generic 200', async () => {
  const { auth, database } = await harness()
  const email = 'ghost@example.invalid'
  await signUp(auth, email)

  // Control: without the stamp, this same second signup gets better-auth's
  // generic duplicate answer — 200, a synthetic user, no token, nothing
  // written. Indistinguishable from a real registration, which is the whole
  // problem. This assertion is what makes the next one meaningful.
  const taken = await postJson(auth, '/api/auth/sign-up/email', { email, password: PASSWORD, name: 'U' })
  assert.equal(taken.status, 200)
  const synthetic = await body(taken)
  assert.equal(synthetic.token, null)
  assert.equal(synthetic.user.email, email)

  stamp(database, email, 'bounce')

  const refused = await postJson(auth, '/api/auth/sign-up/email', { email, password: PASSWORD, name: 'U' })
  assert.equal(refused.status, 422)
  const payload = await body(refused)
  assert.equal(payload.code, 'EMAIL_ADDRESS_BOUNCED')
  assert.match(payload.message, /different email address/)
})

test('signup for a complained address says we stopped sending, not that the address is broken', async () => {
  const { auth, database } = await harness()
  const email = 'annoyed@example.com'
  await signUp(auth, email)
  stamp(database, email, 'complaint')

  const refused = await postJson(auth, '/api/auth/sign-up/email', { email, password: PASSWORD, name: 'U' })
  assert.equal(refused.status, 422)
  assert.equal((await body(refused)).code, 'EMAIL_ADDRESS_COMPLAINED')
})

test('an ordinary unverified address signs up and is not refused', async () => {
  const { auth, mailer } = await harness()
  const response = await auth.api.signUpEmail({
    body: { email: 'fresh@example.com', password: PASSWORD, name: 'U' },
    asResponse: true,
  })
  assert.equal(response.status, 200)
  assert.ok(mailer.sent.some(message => message.to === 'fresh@example.com'))
})

test('the refusal is case-insensitive, like every other address comparison here', async () => {
  const { auth, database } = await harness()
  await signUp(auth, 'ghost@example.invalid')
  stamp(database, 'ghost@example.invalid', 'bounce')

  const refused = await postJson(
    auth,
    '/api/auth/sign-up/email',
    { email: 'GHOST@Example.INVALID', password: PASSWORD, name: 'U' },
  )
  assert.equal((await body(refused)).code, 'EMAIL_ADDRESS_BOUNCED')
})

test('a signup body cannot set its own bounce state', async () => {
  // `input: false` on both additional fields. Without it Better Auth writes
  // whatever the request sends, and the refusal becomes caller-controlled.
  const { auth, database } = await harness()
  const email = 'liar@example.com'
  const response = await postJson(auth, '/api/auth/sign-up/email', {
    email,
    password: PASSWORD,
    name: 'U',
    emailBouncedAt: '2020-01-01T00:00:00.000Z',
    emailBounceKind: 'bounce',
  })
  // Better Auth's body schema does not even accept the keys, so the request is
  // rejected outright rather than quietly ignoring them. Either answer would
  // be safe; this one is checked because it is the one that actually happens,
  // and because the row must not exist afterwards either.
  assert.equal(response.status, 400)
  assert.equal(bounceState(database, email), undefined)

  // And the honest signup that follows starts clean — no stamp arrived by any
  // other route.
  await signUp(auth, email)
  const row = bounceState(database, email)
  assert.equal(row.emailBouncedAt, null)
  assert.equal(row.emailBounceKind, null)
})

// ── the self-service resend ────────────────────────────────────────────────

test('a signed-in caller resending to their OWN bounced address is told why', async () => {
  const { auth, database } = await harness()
  const email = 'locked-out@example.invalid'
  await signUp(auth, email)
  const login = await auth.api.signInEmail({ body: { email, password: PASSWORD }, asResponse: true })
  const cookie = cookieHeader(login)
  stamp(database, email, 'bounce')

  const refused = await postJson(
    auth,
    '/api/auth/send-verification-email',
    { email, callbackURL: 'http://localhost:3000/app' },
    cookie,
  )
  assert.equal(refused.status, 422)
  assert.equal((await body(refused)).code, 'EMAIL_ADDRESS_BOUNCED')
})

test('a signed-in caller whose address is fine still gets a resend', async () => {
  const { auth, mailer } = await harness()
  const email = 'patient@example.com'
  await signUp(auth, email)
  const login = await auth.api.signInEmail({ body: { email, password: PASSWORD }, asResponse: true })
  const before = mailer.sent.length

  const response = await postJson(
    auth,
    '/api/auth/send-verification-email',
    { email, callbackURL: 'http://localhost:3000/app' },
    cookieHeader(login),
  )
  assert.equal(response.status, 200)
  assert.ok(mailer.sent.length > before, 'no verification mail was sent for a perfectly good address')
})

test('the ANONYMOUS resend still answers 200 for a bounced address — no existence oracle', async () => {
  // Deliberate, not an oversight. /send-verification-email is reachable with
  // no session and answers identically for unknown / verified / genuine-send;
  // a 4xx here would tell any stranger "this address is registered AND its
  // mail bounces". #1790's session-required verification-status endpoint is
  // the honest reporter for callers who are not signed in.
  const { auth, database } = await harness()
  const email = 'ghost@example.invalid'
  await signUp(auth, email)
  stamp(database, email, 'bounce')

  const anonymous = await postJson(
    auth,
    '/api/auth/send-verification-email',
    { email, callbackURL: 'http://localhost:3000/app' },
  )
  assert.equal(anonymous.status, 200)

  const unknown = await postJson(
    auth,
    '/api/auth/send-verification-email',
    { email: 'nobody@example.invalid', callbackURL: 'http://localhost:3000/app' },
  )
  assert.equal(unknown.status, anonymous.status)
})

test("a session for one address learns nothing about another address's bounce state", async () => {
  const { auth, database } = await harness()
  const mine = 'mine@example.com'
  const theirs = 'theirs@example.invalid'
  await signUp(auth, mine)
  await signUp(auth, theirs)
  stamp(database, theirs, 'bounce')
  const login = await auth.api.signInEmail({ body: { email: mine, password: PASSWORD }, asResponse: true })

  // Holding a session is not permission to learn about someone else's mail.
  // better-auth answers a signed-in caller naming a different address with its
  // own EMAIL_MISMATCH; the hook must fall through to that rather than
  // volunteering the other account's bounce state.
  const response = await postJson(
    auth,
    '/api/auth/send-verification-email',
    { email: theirs, callbackURL: 'http://localhost:3000/app' },
    cookieHeader(login),
  )
  assert.equal(response.status, 400)
  assert.equal((await body(response)).code, 'EMAIL_MISMATCH')
})
