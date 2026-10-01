import { betterAuth } from 'better-auth'
import { APIError, createAuthMiddleware, getSessionFromCtx } from 'better-auth/api'
import pg from 'pg'

import { DELIVERY_KINDS } from './delivery-log.js'
import { createMailer } from './mailer.js'
import { RESEND_WINDOW_MAX, RESEND_WINDOW_SECONDS } from './verification-status.js'

const { Pool } = pg

function csv(value) {
  return String(value ?? '')
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
}

// Verification mail is SENT on every signup regardless of this flag (so
// verified_at data accrues from day one); the flag controls whether an
// unverified account is REFUSED sign-in. Explicit opt-in ("true") because
// SES starts in sandbox — enforcement flips on via env once production
// sending access is granted, with no code change.
// The literal shipped in .env.example. Public by design, refused in production
// by createAuth() — keep these two in lockstep; auth/test/auth.test.js pins it.
export const PLACEHOLDER_SECRET = 'insecure-local-dev-placeholder-change-me-before-any-deploy'

export function emailVerificationEnforced(env = process.env) {
  return env.EMAIL_VERIFICATION_ENFORCED === 'true'
}

export function enabledProviders(env = process.env) {
  return [
    env.GOOGLE_CLIENT_ID && env.GOOGLE_CLIENT_SECRET ? 'google' : null,
    env.GITHUB_CLIENT_ID && env.GITHUB_CLIENT_SECRET ? 'github' : null,
  ].filter(Boolean)
}

function socialProviders(env) {
  return Object.fromEntries(enabledProviders(env).map(provider => [provider, {
    clientId: env[`${provider.toUpperCase()}_CLIENT_ID`],
    clientSecret: env[`${provider.toUpperCase()}_CLIENT_SECRET`],
  }]))
}

const CONNECTED_ACCOUNT_LABELS = { credential: 'Email & password', google: 'Google', github: 'GitHub' }

// Round-2 review finding (minor): linking or unlinking a sign-in credential
// used to be completely silent to the account owner — no email, nothing —
// despite being exactly the kind of change an account-takeover attempt would
// make (see the freshAge/hooks.before comment above: this is the other half
// of closing that off, an out-of-band signal alongside the in-band guard).
// Wired via databaseHooks.account.{create,delete}.after below.
//
// MUST NOT throw. Unlike sendResetPassword/sendVerificationEmail above
// (both hand-rolled fire-and-forget, each with its own .catch),
// better-auth's databaseHooks create.after/delete.after are awaited by the
// library itself as part of the write (node_modules/@better-auth/core/dist/
// context/transaction.mjs: `for (const hook of pendingHooks) await hook();`
// followed by `if (hasError) throw error;`) — an uncaught throw here would
// fail the /link-social or /unlink-account request that triggered it, over
// a notification email that is not the actual security control. Every
// awaited step is inside the try/catch for exactly that reason.
//
// Round-4 review finding (minor): this had TWO distinct failure modes and
// only one of them logged anything. Mode 1 (mailer.send() throws — SES
// sandboxed/down/etc.) hit the catch below and logged a line. Mode 2 (the
// recipient can't be resolved at all — `internalAdapter.findUserById`
// returns nothing, or `endpointContext.context` itself is missing/malformed)
// hit the `if (!user?.email) return` below and logged NOTHING — a second,
// silent way for this account-takeover signal to just not go out, with no
// operator-visible trace. Both modes now share one greppable marker
// (`ACCOUNT_CHANGE_NOTIFY_FAILED`) so either is findable in logs / a
// CloudWatch metric filter the same way. Neither mode throws: the
// notification stays fail-open for the user-facing link/unlink flow (a mail
// outage, or a data anomaly here, must not block the actual security
// control — the link/unlink itself, already enforced server-side) — it just
// never again fails *silently*. Exported for direct unit testing (see
// auth/test/auth.test.js): both branches are hard to reach through the real
// HTTP handler on demand, since `internalAdapter` always resolves a
// just-committed row in practice.
export async function notifyAccountChange(mailer, endpointContext, account, action) {
  try {
    // #1367 (D4): deleting the whole account deletes its auth_accounts rows
    // too (better-auth/dist/db/internal-adapter.mjs:148-161), which fires
    // this same delete.after hook. Two things go wrong if it is allowed to
    // run: the account owner gets "Email & password was unlinked from your
    // Archimedes account... sign in and review Account Settings" as the
    // last word about an account they just deleted and can no longer sign
    // in to, and — because the after-hooks are queued and drained only once
    // the whole request's write scope resolves (see the transaction.mjs
    // note below), by then the auth_users row is gone as well, so the
    // recipient lookup below fails and raises ACCOUNT_CHANGE_NOTIFY_FAILED
    // on a completely expected event. An alarm marker that fires on the
    // normal path stops being an alarm. Deletion is not a link/unlink, so
    // it is filtered here at the source rather than papered over by
    // loosening the failure branch (which round-4 review made loud on
    // purpose). Verified by
    // auth/test/auth.test.js's "deleting an account does not email the
    // owner about 'unlinked' sign-in methods, and does not trip the
    // notify-failure alarm".
    if (endpointContext?.path === '/delete-user' || endpointContext?.path === '/delete-user/callback') return
    // Round-3 review finding (blocker): this used to special-case only
    // `providerId === 'credential'` — the row EMAIL/PASSWORD signup
    // creates — on the theory that signup already gets its own "verify
    // your email" mail. But `databaseHooks.account.create.after` (this
    // function) fires from OAuth SIGNUP too: when a "Continue with
    // Google/GitHub" click is for an email that owns no account at all,
    // link-account.mjs's handleOAuthUserInfo takes its `else` branch
    // (`dbUser` undefined -> `isRegister = true`) and calls
    // `internalAdapter.createOAuthUser`, which writes exactly the same
    // kind of account row a link does — provider 'google'/'github', not
    // 'credential'. The old check let that through, so a brand-new OAuth
    // signup got "A sign-in method was added... remove it and reset your
    // password" as its first email, which is impossible to act on: it is
    // the account's ONLY sign-in method (canUnlink() below refuses to even
    // render an Unlink control for it) and there is no password to reset.
    //
    // The test that actually distinguishes "this is registration" from
    // "this is a link onto an existing account" is not the new row's
    // provider — it's whether the user has any OTHER account row. A genuine
    // link (the account-takeover-relevant event this mail exists for)
    // always lands with at least one pre-existing account on the user;
    // either signup shape (credential or OAuth) always lands with exactly
    // one. databaseHooks' create.after is queued to run only after the
    // triggering write has committed (@better-auth/core/dist/context/
    // transaction.mjs: `for (const hook of pendingHooks) await hook()` runs
    // after `als.run(...)` resolves), so the just-created row is already
    // visible to this query and counts toward the total.
    if (action === 'added') {
      const accounts = await endpointContext?.context?.internalAdapter?.findAccounts(account.userId)
      // ?? 1: if the adapter is unavailable for some reason, fail toward
      // NOT sending rather than toward a false alert.
      if ((accounts?.length ?? 1) <= 1) return
    }
    const user = await endpointContext?.context?.internalAdapter?.findUserById(account.userId)
    if (!user?.email) {
      // Failure mode 2 — see the header comment above. Fail-open (return,
      // don't throw) but loud: this is the branch that used to be silent.
      console.error('ACCOUNT_CHANGE_NOTIFY_FAILED: could not resolve account owner email', { userId: account.userId, action })
      return
    }
    const label = CONNECTED_ACCOUNT_LABELS[account.providerId] || account.providerId
    const verb = action === 'added' ? 'added to' : 'removed from'
    await mailer.send({
      kind: DELIVERY_KINDS.ACCOUNT_CHANGE,
      userId: account.userId,
      to: user.email,
      subject: `A sign-in method was ${verb} your Archimedes account`,
      text:
        `${label} was just ${action === 'added' ? 'linked as a sign-in method on' : 'unlinked as a sign-in method from'} `
        + 'your Archimedes account.\n\n'
        + "If this wasn't you, sign in and review Account Settings → Connected accounts immediately"
        + (action === 'added' ? ', then remove it and reset your password.' : '.'),
    })
  } catch (error) {
    // Failure mode 1 — see the header comment above. Fail-open (swallow, the
    // request that triggered this must still succeed) but loud.
    console.error('ACCOUNT_CHANGE_NOTIFY_FAILED: mailer.send threw', { action, error: error instanceof Error ? error.name : 'UnknownError' })
  }
}

// #1367 (D2): the "confirm from your CURRENT address" half of the two-step
// email change. Better Auth calls this only when the account's existing
// address is already verified (update-user.mjs `canSendConfirmation` =
// sendVerificationEmail configured && session.user.emailVerified &&
// this option); the link it hands us carries a `change-email-confirmation`
// token, and clicking it makes email-verification.mjs mint a SECOND,
// `change-email-verification` token and mail it to the NEW address. The
// address only actually switches over when that second link is opened
// (email-verification.mjs:213-240 `updateUserByEmail({ email: updateTo,
// emailVerified: true })`) — so the new address is always proven before
// switchover, and the old address always gets a chance to notice.
//
// Fire-and-forget, NOT awaited — the same anti-enumeration reasoning as
// sendResetPassword below, and load-bearing for the same reason. Better
// Auth deliberately returns an identical `{ status: true }` when the
// requested address already belongs to someone else, WITHOUT calling any
// mail callback (update-user.mjs:456-460). If a mailer failure could
// surface as a 500, "address is taken" (200) and "address is free but SES
// is sandboxed" (500) would become distinguishable by status code, handing
// any signed-in visitor an account-existence oracle. Swallowing the error
// keeps both shapes identical; the greppable marker keeps the failure
// visible to operators. The user-facing copy in AccountSettings.jsx never
// claims delivery for exactly this reason.
export function sendChangeEmailConfirmation(mailer, { user, newEmail, url }) {
  mailer.send({
    kind: DELIVERY_KINDS.CHANGE_EMAIL,
    userId: user.id,
    to: user.email,
    subject: 'Confirm your Archimedes email change',
    text:
      `A change of this account's email address to ${newEmail} was requested.\n\n`
      + `${url}\n\n`
      + 'Opening this link sends a verification message to the new address. '
      + 'Your email address does not change until that second link is opened.\n\n'
      + "If this wasn't you, do not open the link — sign in and change your password.",
  }).catch(error => {
    console.error('CHANGE_EMAIL_CONFIRM_SEND_FAILED', { error: error instanceof Error ? error.name : 'UnknownError' })
  })
}

// ── #1804: an address SES has told us is dead is refused, with a reason ─────
//
// `auth_users.emailBouncedAt` / `emailBounceKind` are written by exactly one
// thing — `archimedes.scripts.ses_events` draining the SES bounce/complaint
// feedback queue (infra/ses_events.tf). Nothing in this service ever writes
// them (`input: false` on both additional fields below), it only reads them.
//
// WHY A TYPED CODE AND NOT JUST A MESSAGE. Before this, an address whose
// mailbox does not exist was answered `USER_ALREADY_EXISTS` at signup — true,
// and useless: it tells the person to sign in to an account they can never
// verify, because every verification mail SES accepts for that address is
// dropped inside AWS. The two states need different words, so they need
// different codes; the UI keys off `error.code`, and a code is also the thing
// a support conversation can quote.
//
// The kinds are distinguished for the same reason. A permanent bounce is a
// mailbox that does not exist — "use a different address" is actionable. A
// complaint is a real person who told their provider our mail is spam, and
// telling them their address is broken would be a lie; the honest answer is
// that we stopped sending.
export const BOUNCE_REFUSALS = {
  bounce: {
    code: 'EMAIL_ADDRESS_BOUNCED',
    message:
      'Mail to this address bounced — the mailbox does not exist, so we cannot send a verification link to it. '
      + 'Please use a different email address.',
  },
  complaint: {
    code: 'EMAIL_ADDRESS_COMPLAINED',
    message:
      'This address reported our mail as spam, so we no longer send to it. '
      + 'Please use a different email address, or contact support if this was a mistake.',
  },
}

// Exported for direct unit testing, and so the "is this address refused"
// question has exactly one answer in this file. Returns null when the address
// is fine — which is every address SES has never complained about, including
// every address that simply has not been verified yet.
//
// An unrecognised kind falls back to the bounce wording rather than to
// "allowed": the timestamp is the fact, the kind is only how we phrase it, and
// a future SES event type the consumer learns to record must not silently turn
// the refusal off here.
export function bounceRefusal(user) {
  if (!user?.emailBouncedAt) return null
  return BOUNCE_REFUSALS[user.emailBounceKind] ?? BOUNCE_REFUSALS.bounce
}

// The service's Postgres pool, extracted from createAuth (#1748 item 2) so
// server.js can hand the SAME connection to both Better Auth and the email
// delivery log — one pool, not two, and no second place that has to get the
// sslmode translation below right. Body unchanged from what lived inline in
// createAuth, comment included.
//
// node-postgres does NOT honor libpq's `sslmode` query parameter — it must
// be translated into an explicit `ssl` config or the connection goes out in
// plaintext and TLS-enforcing servers (Aurora) reject it. `sslmode=require`
// in libpq means "encrypt, do not verify the CA", so rejectUnauthorized:false
// is the faithful translation, not a shortcut. verify-ca/verify-full would
// need the RDS CA bundle shipped in the image (follow-up: #1284's image work).
// pg's connection-string parser gives an in-URL `sslmode` precedence over the
// constructor's `ssl` object — with sslmode=require left in the string, full
// certificate verification still ran and failed on Aurora with
// UNABLE_TO_GET_ISSUER_CERT_LOCALLY (no RDS CA in the image). So: STRIP the
// parameter from the string and pass the ssl config explicitly. Proven
// against live Aurora in-container before this commit: SELECT succeeds.
// verify-full + the RDS CA bundle remains the #1284 follow-up.
export function createPool(env = process.env) {
  const rawUrl = env.DATABASE_URL || ''
  const wantsTls = /[?&]sslmode=(require|prefer|verify-ca|verify-full)/.test(rawUrl)
  const connectionString = rawUrl.replace(/[?&]sslmode=[^&]+/, '')
  return new Pool({
    connectionString,
    ...(wantsTls ? { ssl: { rejectUnauthorized: false } } : {}),
  })
}

export function createAuth({ database, env = process.env, mailer = createMailer(env) } = {}) {
  const production = env.NODE_ENV === 'production'
  const baseURL = env.BETTER_AUTH_URL || 'http://localhost:5173'
  const secret = env.BETTER_AUTH_SECRET

  if (!secret || secret.length < 32) {
    throw new Error('BETTER_AUTH_SECRET must contain at least 32 characters')
  }

  // .env.example ships PLACEHOLDER_SECRET so that `cp .env.example .env` gives a
  // working LOCAL stack — without it every docker compose command, including
  // `down`, dies during interpolation on `${BETTER_AUTH_SECRET:?}`. The value is
  // public by construction, so it must never boot a deployed environment: it
  // signs session cookies, and anyone reading the repo could forge them.
  if (production && secret === PLACEHOLDER_SECRET) {
    throw new Error(
      'BETTER_AUTH_SECRET is still the public .env.example placeholder. '
      + 'Set a real secret (production reads SSM /archimedes/prod/BETTER_AUTH_SECRET). '
      + 'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"',
    )
  }

  const db = database ?? createPool(env)

  return betterAuth({
    appName: 'Archimedes',
    baseURL,
    secret,
    database: db,
    trustedOrigins: csv(env.BETTER_AUTH_TRUSTED_ORIGINS || baseURL),
    emailAndPassword: {
      enabled: true,
      autoSignIn: false,
      minPasswordLength: 12,
      maxPasswordLength: 128,
      revokeSessionsOnPasswordReset: true,
      requireEmailVerification: emailVerificationEnforced(env),
      // Pinned for the same reason as emailVerification.expiresIn below and
      // session.freshAge further down: left unset the number lives in the
      // library, not here (better-auth/dist/api/routes/password.mjs:73
      // `getDate(ctx.context.options.emailAndPassword.resetPasswordTokenExpiresIn
      // || 3600 * 1, "sec")`), so nobody reading auth.js can tell how long a
      // reset link stays live. 3600 is exactly today's effective value —
      // this pins behaviour, it does not change it.
      //
      // Unlike a verification token (a stateless JWT — see expiresIn below)
      // a reset token is a real `auth_verifications` row consumed on first
      // use (password.mjs:157 `consumeVerificationValue`), so it is
      // single-use AND revocable, and `verification.storeIdentifier:
      // 'hashed'` further down means the stored identifier is not a usable
      // token even to someone holding a database dump. Both properties are
      // covered by auth/test/email-flows.test.js.
      resetPasswordTokenExpiresIn: 60 * 60,
      sendResetPassword: async ({ user, url }) => {
        // Fire-and-forget ON PURPOSE — do not `await` the send. Better Auth
        // already returns an identical response body/status for a known vs.
        // unknown email (requestPasswordReset in
        // better-auth/dist/api/routes/password.mjs: an unknown address
        // never reaches this callback at all, it takes a dummy-lookup
        // early-return instead), which is the anti-enumeration design. But
        // without `advanced.backgroundTasks.handler` configured, Better
        // Auth's own dispatcher (`runInBackgroundOrAwait` in
        // better-auth/dist/context/create-context.mjs) does `await promise`
        // on whatever this callback returns — so an awaited mailer.send()
        // here would make a known-address request measurably slower than an
        // unknown-address one (the real SES round trip vs. an immediate
        // early return), reopening the same enumeration channel through
        // response TIMING instead of status code. Not awaiting the send
        // makes this callback's own duration independent of mailer latency
        // regardless. The `.catch` below is what keeps a mailer failure
        // fail-soft (same reasoning as sendVerificationEmail below, and
        // load-bearing here for the same anti-enumeration reason: a 500
        // that only known accounts could trigger would leak account
        // existence via status code) — loud single line, logged
        // asynchronously after the response has already gone out.
        mailer.send({
          kind: DELIVERY_KINDS.RESET,
          userId: user.id,
          to: user.email,
          subject: 'Reset your Archimedes password',
          text:
            'A password reset was requested for your Archimedes account:\n\n'
            + `${url}\n\n`
            + 'If you did not request this, ignore this message — your password will not change.',
        }).catch(error => {
          console.error('reset password email send failed:', error instanceof Error ? error.name : 'UnknownError')
        })
      },
    },
    emailVerification: {
      sendOnSignUp: true,
      autoSignInAfterVerification: true,
      // Pinned, not the library's inherited default — same reasoning as
      // session.freshAge below (round-2 review, blocker: a security-relevant
      // duration nobody chose is not auditable from the code). Left unset,
      // the value comes from a DEFAULT PARAMETER buried in the library
      // (node_modules/better-auth/dist/api/routes/email-verification.mjs:13
      // `async function createEmailVerificationToken(secret, email, updateTo,
      // expiresIn = 3600, extraPayload)`), so a reader of auth.js cannot see
      // how long a verification link stays live and a library bump could
      // change it silently. 3600 is exactly today's effective value: this
      // pins behaviour, it does not change it. It matters more than a
      // typical TTL because of autoSignInAfterVerification above — see
      // auth/test/email-flows.test.js, which drives the real endpoint and
      // shows an ANONYMOUS holder of the URL getting a live session on the
      // first open. That makes the link a one-time bearer sign-in
      // credential, and this number is its whole lifetime.
      expiresIn: 60 * 60,
      // Fire-and-forget, NOT awaited — the same anti-enumeration reasoning
      // as sendResetPassword above, and load-bearing here for a reason that
      // is specific to this callback's OTHER caller.
      //
      // On the signup path an awaited send is harmless (the caller already
      // knows the address it just registered). The problem is
      // /send-verification-email, which is reachable with NO session at all
      // (better-auth/dist/api/routes/email-verification.mjs:95-117) and is
      // exactly the endpoint the "Resend verification email" control calls
      // — a control that only becomes load-bearing the day
      // EMAIL_VERIFICATION_ENFORCED flips on. Better Auth defends that
      // endpoint with a 500ms constant-time FLOOR, deliberately: an address
      // that is unknown-or-already-verified takes the fast local
      // JWT-signing branch, an address that is known-and-unverified takes
      // the real send, and the floor is meant to hide the difference. A
      // floor only hides a difference it is larger than. With the send
      // awaited here, a real SES round trip pushes the known-and-unverified
      // case straight through the floor and the endpoint becomes an
      // account-existence AND verification-state oracle for any anonymous
      // caller. Measured against a 900ms mailer before this change:
      // unknown 504ms vs known-unverified 922ms. Not awaiting the send
      // makes this callback's own duration independent of mailer latency,
      // so both shapes land on the floor together. The regression guard is
      // "the anonymous resend path does not leak..." in
      // auth/test/email-flows.test.js; reverting to `await mailer.send(...)`
      // makes it fail.
      //
      // The .catch is what keeps a mailer failure fail-soft while SES is in
      // sandbox: an undeliverable verification mail must not 500 the signup.
      // Loud single line; requireEmailVerification is what actually gates
      // sign-in.
      sendVerificationEmail: async ({ user, url }) => {
        mailer.send({
          kind: DELIVERY_KINDS.VERIFICATION,
          userId: user.id,
          to: user.email,
          subject: 'Verify your Archimedes account',
          text:
            'Verify your email address to activate your Archimedes account:\n\n'
            + `${url}\n\n`
            + 'If you did not create this account, ignore this message.',
        }).catch(error => {
          console.error('verification email send failed:', error instanceof Error ? error.name : 'UnknownError')
        })
      },
    },
    socialProviders: socialProviders(env),
    // #1367 (D2 + D4). Both of these are opt-in in better-auth@1.6.25 —
    // /change-email and /delete-user refuse outright without them
    // (update-user.mjs:288 and :434) — so this block is what makes Account
    // Settings' email-change and delete-account controls real rather than
    // decorative.
    user: {
      modelName: 'auth_users',
      // #1804. Declared here so Better Auth's own schema and the Alembic
      // migration (backend/migrations/versions/e6b2a19c4d70_…) produce the
      // SAME two columns — `emailBouncedAt` / `emailBounceKind`, camelCase for
      // the same reason `emailVerified` is. Better Auth derives its column
      // names from these keys, so renaming one half in isolation makes this
      // service query a column that does not exist.
      //
      // `input: false` is the security-relevant half: without it Better Auth
      // adds both fields to the /sign-up/email body schema and writes whatever
      // the request sends, so a caller could stamp — or, far worse, could keep
      // its own address UNstamped through a later update. Nothing in this
      // service writes them; `archimedes.scripts.ses_events` does, from what
      // AWS reported. Pinned by auth/test/bounced-address.test.js's "a signup
      // body cannot set its own bounce state".
      //
      // `required: false` keeps both columns nullable, which is what NULL has
      // to mean here: "SES has never told us anything bad about this address".
      additionalFields: {
        emailBouncedAt: { type: 'date', required: false, input: false },
        emailBounceKind: { type: 'string', required: false, input: false },
      },
      changeEmail: {
        enabled: true,
        // Two-step for an already-verified address: confirm from the old
        // one, then prove the new one. See sendChangeEmailConfirmation
        // above for the exact library path. An account whose address is
        // NOT yet verified skips straight to the second step (a link to
        // the new address) — there is no proven old address to confirm
        // from, so requiring one would just be theatre.
        //
        // updateEmailWithoutVerification is deliberately NOT set: with it
        // on, an unverified account's address would switch over the moment
        // the form is submitted, with no proof the new address exists or
        // belongs to the person typing it.
        sendChangeEmailConfirmation: async ({ user, newEmail, url }) =>
          sendChangeEmailConfirmation(mailer, { user, newEmail, url }),
      },
      deleteUser: {
        enabled: true,
        // NO sendDeleteAccountVerification, on purpose. With it set, every
        // /delete-user call takes the "mail a delete link" branch
        // (update-user.mjs:311-327) and NEVER deletes in-request — which,
        // while SES is still sandboxed (see sendVerificationEmail's
        // fail-soft comment above), would ship a Delete-account button that
        // silently does nothing for any address SES will not deliver to.
        // Instead the endpoint's own re-authentication applies: a password
        // account must submit its current password (:293-300, verified
        // against the credential row), and an account with no password —
        // Google/GitHub-only — falls to the session-freshness check
        // (:328-332, the same freshAge pinned in session below), so a
        // stale or hijacked session cannot delete an account on its own.
        //
        // The erasure itself is the DATABASE's, not this service's:
        // internalAdapter.deleteUser ends in a bare `DELETE FROM
        // auth_users WHERE id = ?` (better-auth/dist/db/internal-adapter.
        // mjs:148-161), which is exactly the statement migration
        // 85ca5310b7a1's per-table ON DELETE actions and its
        // trg_auth_users_purge_unclaimed_owned_rows trigger are written to
        // fire on — see backend/tests/test_account_deletion_cascade.py,
        // which drives that same bare statement. Nothing about what gets
        // erased vs detached is implemented here, so it cannot drift from
        // what the Python backend's own deletes would do.
      },
    },
    session: {
      modelName: 'auth_sessions',
      expiresIn: 60 * 60 * 24 * 7,
      updateAge: 60 * 60 * 24,
      cookieCache: { enabled: false },
      // Explicit, not the library's inherited default (round-2 review
      // finding, blocker). Left unset, better-auth defaults freshAge to 24h
      // regardless of expiresIn (node_modules/better-auth/dist/context/
      // create-context.mjs:148: `freshAge: options.session?.freshAge ===
      // void 0 ? 3600 * 24 : options.session.freshAge`) — a 7-day session
      // silently carrying a 1-day "fresh" window nobody chose. Pinned here
      // so it is a deliberate value, tracked by
      // auth/test/auth.test.js's "freshAge is pinned, not an inherited
      // default" test. This ONE value now governs both the library's own
      // freshSessionMiddleware (/unlink-account, /list-sessions, ...) and
      // the hand-rolled twin on /link-social below (hooks.before) — see
      // that comment for why both must move together.
      freshAge: 60 * 60 * 24,
    },
    account: {
      modelName: 'auth_accounts',
      encryptOAuthTokens: true,
      // #1420 follow-up (account linking). Semantics below verified against
      // the INSTALLED better-auth@1.6.25 source
      // (node_modules/better-auth/dist/oauth2/link-account.mjs +
      // node_modules/better-auth/dist/api/routes/{account,callback}.mjs) —
      // not the changelog, not memory. Two independent link paths exist and
      // this config affects them differently:
      //
      // 1. IMPLICIT auto-link (plain "Continue with Google/GitHub" on the
      //    sign-in screen, no prior session — link-account.mjs
      //    handleOAuthUserInfo) stays OFF (disableImplicitLinking: true,
      //    same as before this feature). Gate, verbatim:
      //      (!isTrustedProvider && !userInfo.emailVerified)
      //      || (requireLocalEmailVerified && !dbUser.user.emailVerified)
      //      || accountLinking.enabled === false
      //      || accountLinking.disableImplicitLinking === true
      //    The last clause alone makes the whole OR always true, so this
      //    path unconditionally refuses (account_not_linked) regardless of
      //    provider trust or either side's emailVerified.
      //
      //    Round-2 review finding (major): an earlier revision of this PR
      //    flipped this to false and added trustedProviders: ['google',
      //    'github'] to enable it. That was reverted here before merge.
      //
      //    Round-3 review finding (major) — CORRECTION to round-2's own
      //    reasoning above: trustedProviders is NOT consulted in exactly
      //    one place. `grep -rn trustedProviders node_modules/better-auth/
      //    dist/` finds three call sites:
      //      - oauth2/link-account.mjs:21 (this implicit path)
      //      - api/routes/callback.mjs:98 — the EXPLICIT `if (link)` branch
      //        in path 2 below, i.e. the actual code the "Link Google/
      //        GitHub" buttons drive
      //      - api/routes/account.mjs:176 — the explicit `idToken` link
      //        branch (unused by this UI today, same endpoint)
      //    At BOTH explicit-path call sites trustedProviders is the same
      //    switch as here: it turns OFF the requirement that the
      //    PROVIDER's own emailVerified claim be true. Today that
      //    requirement holds on the explicit path (guard (a) in path 2
      //    below) only because trustedProviders is absent — the guard
      //    `!trustedProviders.includes(id) && !userInfo.emailVerified`
      //    reduces to plain `!userInfo.emailVerified` against an empty
      //    list. Reintroducing trustedProviders: ['google', 'github'] to
      //    arm implicit auto-link would ALSO silently drop that
      //    requirement from the explicit link flow that actually ships —
      //    not an isolated change to the implicit path alone. See
      //    auth/test/auth.test.js's source-pinned tests on both
      //    link-account.mjs and callback.mjs for the exact lines this
      //    depends on. Enabling implicit auto-link is a real security
      //    decision — the moment EMAIL_VERIFICATION_ENFORCED flips on
      //    (emailVerificationEnforced above), dbUser.user.emailVerified
      //    stops being the guard that was blocking it in practice, and an
      //    anonymous "Continue with GitHub/Google" click could silently
      //    attach to an existing user's account with only the provider's
      //    own emailVerified claim standing between an attacker and a
      //    takeover. It is not reintroduced here without that decision
      //    being made and reviewed on its own, with
      //    EMAIL_VERIFICATION_ENFORCED actually on — and with the
      //    explicit-path consequence above accounted for, not just the
      //    implicit one.
      //
      // 2. EXPLICIT link (signed-in user clicks "Link Google/GitHub" in
      //    Account Settings — api/routes/account.mjs linkSocialAccount +
      //    api/routes/callback.mjs's `if (link)` branch). This path does NOT
      //    consult disableImplicitLinking, and does NOT check the base
      //    account's emailVerified at all — proof of account ownership
      //    comes from the live session state binds to at call time, not
      //    from any verification flag — so it works today regardless of the
      //    operator's own emailVerified value. It DOES still enforce: (a)
      //    the provider's own emailVerified claim — TODAY, because
      //    trustedProviders is absent (see the round-3 correction above;
      //    this is literally the same `trustedProviders` list as the
      //    implicit path, read at callback.mjs:98, not a separate
      //    guarantee) and (b) allowDifferentEmails — the provider's OAuth
      //    email must equal the signed-in user's account email, or the
      //    callback redirects with ?error=email_doesn't_match. Kept false
      //    here (unchanged): the owner's Google/GitHub email is expected to
      //    match their password account's email, and this is not weakened
      //    to let them differ.
      //
      // allowUnlinkingAll stays false: /unlink-account (api/routes/
      // account.mjs) throws FAILED_TO_UNLINK_LAST_ACCOUNT when it is the
      // account's only remaining credential. ui/src/components/
      // AccountSettings.jsx additionally disables the Unlink control in that
      // state — belt and suspenders, not a substitute for this flag staying
      // false.
      accountLinking: {
        enabled: true,
        disableImplicitLinking: true,
        allowDifferentEmails: false,
        allowUnlinkingAll: false,
      },
    },
    // Round-2 review finding (blocker): /unlink-account is gated behind
    // better-auth's OWN freshSessionMiddleware (node_modules/better-auth/
    // dist/api/routes/account.mjs:229 `use: [freshSessionMiddleware]`) but
    // /link-social is not (same file, :117 `use: [sessionMiddleware]` —
    // no freshness check at all). With sessions living 7 days
    // (session.expiresIn above) and freshAge at 24h, that asymmetry let a
    // >24h-old session permanently ATTACH a new sign-in credential (an
    // attacker who rides a stale/hijacked session in) while the legitimate
    // owner's own stale session could not remove it — a one-way door not
    // even closed by a password reset (auth/node_modules/better-auth/dist/
    // api/routes/password.mjs revokes sessions on reset, but does not touch
    // already-linked accounts).
    //
    // better-auth has no per-endpoint config knob for this — the fix is a
    // global hooks.before that runs before every request (dispatch.mjs:143
    // `matcher: () => true` — round-4 review finding (minor): this used to
    // cite :139, which is `function getHooks(authContext) {` itself, not the
    // matcher; the user hooks.before's own `beforeHooks.push({ matcher: ()
    // => true, ... })` is four lines further down) and applies the exact same check
    // freshSessionMiddleware runs (api/routes/session.mjs:359-371) to
    // /link-social specifically. session.freshAge above is what both sides
    // now read, so they cannot drift apart again. Proven in
    // auth/test/auth.test.js: "/link-social now requires the same session
    // freshness as /unlink-account" ages a real session's createdAt past
    // freshAge and asserts BOTH endpoints 403 identically; mutation-proof —
    // commenting this hook out (done by hand before this commit) makes that
    // test fail with AGED link-social returning 200.
    //
    // Round-3 review finding (minor) — scope of what this actually gates:
    // this hook only guards the /link-social path === '/link-social' check
    // below, i.e. INITIATING a link. The step that attaches the credential
    // — callback.mjs's `if (link)` branch, reached at /callback/:id after
    // the OAuth round trip — performs no session check of its own; it acts
    // purely on the `link` payload out of the signed state (parseState),
    // so a session that goes stale (or is signed out) between initiate and
    // callback can still complete an attach that was validly initiated
    // while fresh. That window is bounded by the state TTL (oauth2/
    // state.mjs: 600s), not by session freshness — plus the double-submit
    // state cookie and the allowDifferentEmails email match at
    // callback.mjs, which is why this is a documented bound, not a second
    // hole to close.
    //
    // ── /revoke-session (#1367, D2) ────────────────────────────────────
    //
    // The second branch below closes a CLAIM gap, not a security hole.
    // better-auth's /revoke-session does check ownership — session.mjs:434
    // deletes only when `findSession(token)?.session.userId ===
    // ctx.context.session.user.id`, so another account's session is never
    // actually revoked — but it then falls through to the SAME
    // `return ctx.json({ status: true })` at :443 regardless. A token that
    // belongs to someone else, or that does not exist at all, gets a 200
    // saying the session was revoked when nothing was. A UI that renders
    // "Session revoked." off that response states something its
    // implementation does not back, which is the exact defect class
    // CLAUDE.md § "A guard must be shown to reject something" is about, so
    // the honest answer has to come from the server rather than from
    // hopeful copy in AccountSettings.jsx.
    //
    // 404 for BOTH "not yours" and "no such token", deliberately: a
    // distinct response for the two would tell any signed-in caller
    // whether an arbitrary session token is live, which is strictly more
    // than they knew before. The caller's own tokens are the only ones
    // that produce a 200, and those are the only ones /list-sessions ever
    // hands them.
    //
    // Guard, not decoration — auth/test/auth.test.js drives a real
    // second account's token through auth.handler and asserts the 404, and
    // the mutation transcript in the PR body shows that test going green
    // (a lying 200) the moment the ownership comparison is removed.
    hooks: {
      before: createAuthMiddleware(async (ctx) => {
        // ── #1804: refuse an address SES has reported dead ───────────────
        //
        // WHAT THIS NARROWS, STATED PLAINLY. Because `autoSignIn: false` is
        // set above, better-auth answers a signup for an ALREADY-REGISTERED
        // address with a generic 200 carrying a SYNTHETIC user object and no
        // token (sign-up.mjs:162-206 `shouldReturnGenericDuplicateResponse`;
        // it even hashes the password first to equalise timing). Nothing is
        // written and nothing is disclosed — deliberate anti-enumeration, and
        // verified against the installed better-auth@1.6.25, not assumed.
        //
        // A refusal here gives that up for one subset: an address that both
        // has an account AND has had a permanent bounce or complaint recorded
        // against it now answers 422 where an unregistered address answers
        // 200. That is an account-existence disclosure, and it is a
        // deliberate trade rather than an oversight:
        //
        //   * the disclosed set is exactly the addresses that CANNOT RECEIVE
        //     MAIL. Knowing an account exists there buys an attacker nothing
        //     they can act on — no reset link, no verification link, and no
        //     sign-in help can ever be delivered to it;
        //   * the alternative is the defect #1804 is about. Someone who typos
        //     their address gets the generic 200, never receives the mail,
        //     retries the same typo, and gets the same cheerful 200 forever,
        //     with `emailVerified` (and so the free tier) stuck false and no
        //     way to find out why. The refusal is the only moment anything in
        //     the product can tell them the address is dead;
        //   * timing is unaffected — the lookup below runs for EVERY signup,
        //     not only for refused ones.
        //
        // The resend path below makes the opposite call, for a reason that is
        // spelled out there: nothing about it forces this one.
        if (ctx.path === '/sign-up/email') {
          const email = ctx.body?.email
          // A missing/ill-typed email is the endpoint's own zod schema to
          // reject; answering "your address bounced" to a malformed body
          // would be a claim about an address that was never supplied.
          if (typeof email !== 'string' || email === '') return
          const found = await ctx.context.internalAdapter.findUserByEmail(email)
          const refusal = bounceRefusal(found?.user)
          if (refusal) throw APIError.from('UNPROCESSABLE_ENTITY', refusal)
          return
        }

        // The resend is refused ONLY for the signed-in caller's own address,
        // and that restriction is deliberate rather than timidity.
        // /send-verification-email is reachable with NO session at all
        // (better-auth/dist/api/routes/email-verification.mjs:95-117) and
        // answers 200 for an unknown address, an already-verified address and
        // a genuine send alike — that uniformity is what stops it being an
        // account-existence oracle, and better-auth backs it with a 500ms
        // constant-time floor (see the sendVerificationEmail comment above,
        // and the "the anonymous resend path does not leak..." test in
        // auth/test/email-flows.test.js). A 4xx here for anonymous callers
        // would hand any stranger "this address is registered AND its mail
        // bounces" for free — strictly more than they knew before, and a
        // worse trade than the one this issue is about.
        //
        // A caller holding a session for the address already knows both facts,
        // so telling them costs nothing and is the whole point: AccountSettings
        // and the Generate page's resend control both call this while signed
        // in, which is where a locked-out user actually is. The anonymous
        // resend on the sign-in page keeps today's behaviour; #1790's
        // session-required GET /api/auth/verification-status is the honest
        // reporter for the rest.
        if (ctx.path === '/send-verification-email') {
          const email = ctx.body?.email
          if (typeof email !== 'string' || email === '') return
          const session = await getSessionFromCtx(ctx)
          const caller = session?.user
          if (!caller || String(caller.email ?? '').toLowerCase() !== email.toLowerCase()) return
          const refusal = bounceRefusal(caller)
          if (refusal) throw APIError.from('UNPROCESSABLE_ENTITY', refusal)
          return
        }

        if (ctx.path === '/link-social') {
          const session = await getSessionFromCtx(ctx)
          // No session at all: fall through to the endpoint's own
          // sessionMiddleware, which produces the correct 401 — freshness is
          // moot when there is no session to be fresh or stale.
          if (!session?.session) return
          const freshAge = ctx.context.sessionConfig.freshAge
          if (freshAge === 0) return
          const createdAt = new Date(session.session.createdAt).getTime()
          if (Date.now() - createdAt >= freshAge * 1000) {
            // Identical {message, code} shape to better-auth's own
            // BASE_ERROR_CODES.SESSION_NOT_FRESH (@better-auth/core/dist/
            // error/codes.mjs) — spelled out literally rather than imported
            // from @better-auth/core, which is a transitive dep of
            // better-auth, not one of auth/package.json's own dependencies.
            throw APIError.from('FORBIDDEN', { code: 'SESSION_NOT_FRESH', message: 'Session is not fresh' })
          }
          return
        }

        if (ctx.path === '/revoke-session') {
          const session = await getSessionFromCtx(ctx)
          // Same fall-through as above: no session is the endpoint's own
          // 401 to give, not this hook's 404.
          if (!session?.session) return
          const token = ctx.body?.token
          // A missing/ill-typed token is the endpoint's zod schema to
          // reject (session.mjs:407) — answering 404 here would turn a
          // malformed request into a misleading "no such session".
          if (typeof token !== 'string' || token === '') return
          const target = await ctx.context.internalAdapter.findSession(token)
          if (target?.session?.userId !== session.user.id) {
            throw APIError.from('NOT_FOUND', { code: 'SESSION_NOT_FOUND', message: 'Session not found' })
          }
        }
      }),
    },
    // Round-2 review finding (minor): notify the account owner's email
    // whenever a sign-in credential is added or removed — see
    // notifyAccountChange above for why it must never throw.
    databaseHooks: {
      account: {
        create: { after: (account, ctx) => notifyAccountChange(mailer, ctx, account, 'added') },
        delete: { after: (account, ctx) => notifyAccountChange(mailer, ctx, account, 'removed') },
      },
    },
    verification: {
      modelName: 'auth_verifications',
      storeIdentifier: 'hashed',
    },
    rateLimit: {
      enabled: production,
      storage: 'database',
      modelName: 'auth_rate_limits',
      customRules: {
        '/sign-in/email': { window: 60, max: 10 },
        // Signup friction (#1194 revision b). Email verification is wired
        // above (mail sent on every signup; sign-in refusal gated on
        // EMAIL_VERIFICATION_ENFORCED, which flips on once SES production
        // access clears — see docs/account-authentication.md). These rate
        // rules stay as defense-in-depth regardless: this rule (3 signups /
        // 10 min per Better Auth's rate key), nginx's /api/auth/ limit_req
        // zone, and — decisively — the per-IP DAILY generation cap
        // (services/generation_quota.py): a fresh account does not raise its
        // address's generation allowance, so disposable accounts gain
        // nothing at the endpoint that actually spends money.
        //
        // WHAT "Better Auth's rate key" RESOLVES TO (#1691, fixed here). The
        // key is `${ip}|${path}` (@better-auth/core/dist/utils/ip.mjs:225).
        // Until #1691 no ip resolved in production: getIp trusts a forwarded
        // header only when it carries exactly ONE value (ip.mjs:189 `if
        // (forwardedIps.length !== 1) return null`), and behind
        // CloudFront -> ALB -> nginx every hop appends to X-Forwarded-For, so
        // the limiter fell back to a single shared `no-trusted-ip|<path>`
        // bucket for the entire internet. advanced.ipAddress.ipAddressHeaders
        // below now points the resolver at the single-valued, nginx-SET
        // X-Client-IP header instead — the same trusted value layers 2 and 3
        // key on. These rules are per-rate-key, and the rate key is now that
        // header; see the ipAddress block for exactly whose address it is.
        // Pinned in both directions by auth/test/email-flows.test.js; do not
        // "fix" it further by trusting the leftmost XFF token, which is
        // client-controlled and strictly worse than a shared bucket.
        '/sign-up/email': { window: 600, max: 3 },
        // Pinned, not inherited — the same argument session.freshAge rests on.
        // These two are the mail-sending endpoints, so they are the pair the
        // EMAIL_VERIFICATION_ENFORCED flip puts under load and the pair SES
        // production access is judged on. The values match the library's own
        // default for this path set (better-auth/dist/api/rate-limiter/
        // index.mjs:378-382, window 60 / max 3), so this changes no behaviour;
        // it makes the bound readable here and stops a library upgrade from
        // moving a security-relevant number silently.
        '/request-password-reset': { window: 60, max: 3 },
        // Built from verification-status.js's constants rather than
        // literals (#1748 item 2): GET /api/auth/verification-status
        // QUOTES this window back to a waiting human ("wait 40s"), so the
        // number it quotes and the number the limiter enforces must be the
        // same object, not two copies. Still 60/3 — email-flows.test.js
        // asserts the literal `{ window: 60, max: 3 }` on the built options,
        // which is what stops the constants drifting.
        '/send-verification-email': { window: RESEND_WINDOW_SECONDS, max: RESEND_WINDOW_MAX },
      },
    },
    advanced: {
      // Where the rate limiter gets its client IP (#1691). Better Auth's getIp
      // walks these header names in order (@better-auth/core/dist/utils/ip.mjs
      // :203) and trusts a value only if it is single-valued; the DEFAULT list
      // is ['x-forwarded-for'], which is multi-hop behind CloudFront -> ALB ->
      // nginx and so resolved to null on every production request, collapsing
      // every rate-limit bucket into one global `no-trusted-ip|<path>`.
      //
      // x-client-ip is set — not appended — by nginx from its realip-resolved
      // $remote_addr (nginx/nginx.conf, the server-level proxy header block),
      // so a caller cannot supply it: whatever the client sends under that name
      // is overwritten before the request reaches this process. It is the same
      // value the FastAPI limiter and the daily generation cap already key on
      // via X-Real-IP. Behind CloudFront it identifies the CloudFront EDGE, not
      // the viewer (nginx trusts only the ALB CIDR) — so buckets are per-edge:
      // unspoofable, no longer global, coarser than one caller. Say that, don't
      // round it up to "per user".
      //
      // Deliberately NOT trustedProxies: that would re-admit X-Forwarded-For
      // and require carrying CloudFront's published edge ranges in this file,
      // where a stale list degrades silently back to the shared bucket. And
      // deliberately not a fallback to 'x-forwarded-for' after this one — a
      // single-valued XFF reaching this process is exactly the shape a
      // direct-to-container caller can forge. No header, no key: the limiter
      // falls back to its shared bucket, which fails safe (over-limiting), not
      // open.
      ipAddress: {
        ipAddressHeaders: ['x-client-ip'],
      },
      useSecureCookies: production,
      defaultCookieAttributes: {
        httpOnly: true,
        secure: production,
        sameSite: 'lax',
        path: '/',
      },
    },
  })
}
