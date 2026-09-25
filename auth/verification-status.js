// What `GET /api/auth/verification-status` answers, and the whole of the
// decision behind it (#1748 item 2).
//
// Pure and I/O-free — it takes an already-fetched delivery history and an
// already-performed suppression lookup and returns one state. server.js owns
// the session check and the two awaits; everything that DECIDES lives here, so
// every rule below is executable in auth/test/delivery-feedback.test.js
// without a socket, a database, or an AWS client. (Same split ui/'s
// freeGenerations.js makes for the free-tier banner.)
//
// WHY A SIBLING GET RATHER THAN A RICHER RESPONSE FROM THE RESEND POST.
// POST /api/auth/send-verification-email is reachable with NO session
// (better-auth/dist/api/routes/email-verification.mjs:95-117) and Better Auth
// defends it with a 500ms constant-time floor precisely so that "unknown or
// already-verified address" and "known and unverified address" are
// indistinguishable to an anonymous caller — the reason auth.js's
// sendVerificationEmail is fire-and-forget (see its comment; the measured leak
// was 504ms vs 922ms). Teaching THAT response to say `suppressed` vs `sent`
// would hand any anonymous caller a per-address oracle and undo the fix. This
// endpoint requires a live session and reports on that session's OWN address
// only, so it can be as specific as it likes: the caller already owns the
// account it is asking about.

import { DELIVERY_KINDS } from './delivery-log.js'

/**
 * The resend rate rule, in ONE place.
 *
 * auth.js's `rateLimit.customRules['/send-verification-email']` is built from
 * these two constants, so the number this endpoint quotes to a human and the
 * number the limiter actually enforces cannot drift apart. (60/3 is also
 * better-auth's own default for that path set — see auth.js — so naming them
 * here changed no behaviour; auth/test/email-flows.test.js still pins the
 * literal `{ window: 60, max: 3 }` on the built options, which is what keeps
 * these two honest.)
 */
export const RESEND_WINDOW_SECONDS = 60
export const RESEND_WINDOW_MAX = 3

/**
 * After this many recorded sends in 24h, the answer stops being "wait" and
 * starts being "look in your spam folder". Two, not one: the first send
 * arriving late is ordinary, a second one going missing is a signal.
 */
export const SPAM_HINT_AFTER_SENDS = 2

export const VERIFICATION_STATES = Object.freeze({
  VERIFIED: 'verified',
  // #1804 — SES itself told us this address is dead, through the bounce /
  // complaint feedback queue. Distinct from `suppressed`, which is the PULL
  // half (#1790 asking the account suppression list at status time) and can
  // only ever answer while the lookup is permitted and healthy.
  BOUNCED: 'bounced',
  SUPPRESSED: 'suppressed',
  FAILED: 'failed',
  RATE_LIMITED: 'rate_limited',
  SENT: 'sent',
  UNKNOWN: 'unknown',
})

function isoOrNull(value) {
  if (!value) return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date.toISOString()
}

/**
 * Resolve one account's verification-mail delivery state.
 *
 * @param {object} args
 * @param {{id?: string, email: string, emailVerified?: boolean}} args.user — the SESSION's user
 * @param {{recent: Function}} args.deliveryLog
 * @param {{check: Function}} args.suppression
 * @param {number} [args.now]
 * @returns {Promise<object>} the response body, verbatim
 */
export async function resolveVerificationStatus({ user, deliveryLog, suppression, now = Date.now() }) {
  const email = String(user?.email ?? '')

  // Already verified: there is no pending delivery to have an opinion about,
  // and no reason to spend an SES call proving it.
  if (user?.emailVerified === true) {
    return {
      state: VERIFICATION_STATES.VERIFIED,
      email,
      // Reported here too so the body has ONE shape across every state. A
      // verified account can still carry an old stamp (it bounced, the owner
      // fixed their mailbox, the link then landed) and the fact stays visible
      // — it just stops being the headline, because mail demonstrably arrives.
      bounce: { at: isoOrNull(user?.emailBouncedAt), kind: user?.emailBounceKind ?? null },
      sends: null,
      sendsInWindow: 0,
      lastSentAt: null,
      lastError: null,
      retryAfterSeconds: 0,
      checkSpam: false,
      suppression: { checked: false, suppressed: false, reason: null, since: null, detail: 'account is verified' },
      resendWindowSeconds: RESEND_WINDOW_SECONDS,
      resendWindowMax: RESEND_WINDOW_MAX,
    }
  }

  const [history, suppressionResult] = await Promise.all([
    deliveryLog.recent({ email, kind: DELIVERY_KINDS.VERIFICATION, now }),
    suppression.check(email),
  ])

  // `null` (not `[]`) means the log could not be read — see delivery-log.js's
  // nullDeliveryLog comment. Everything below distinguishes the two.
  const rows = Array.isArray(history) ? history : null
  // `sends` counts rows the mailer ACCEPTED, because that is the number the
  // spam hint quotes back to a human ("3 have gone out") — a send that threw
  // did not go anywhere and must not be counted as one.
  const sends = rows ? rows.filter(row => row.status === 'sent').length : null
  const windowCutoff = now - RESEND_WINDOW_SECONDS * 1000
  // `inWindow` counts ALL rows, accepted or not, because the rate limiter
  // counts REQUESTS: a send that threw still consumed a slot on its way in.
  const inWindow = rows ? rows.filter(row => row.createdAt.getTime() > windowCutoff) : []
  // THE latest attempt. `recent()` orders by the delivery table's DB-assigned
  // `seq`, so "latest" is the order the database saw across every task of this
  // service — not the order one process's clock happened to record.
  const newest = rows?.[0] ?? null
  const lastSent = rows?.find(row => row.status === 'sent') ?? null

  // Seconds until the limiter would accept another resend, computed from OUR
  // OWN rows. Reported on every state, so the UI can disable the button
  // without needing the state to be `rate_limited`.
  // `inWindow` is newest-first, so index RESEND_WINDOW_MAX - 1 is the send
  // whose ageing-out drops the in-window count back below the cap — the
  // sliding-window answer. Using the OLDEST row instead would clear the wait
  // too early whenever more than RESEND_WINDOW_MAX sends are in the window.
  let retryAfterSeconds = 0
  if (inWindow.length >= RESEND_WINDOW_MAX) {
    const blocking = inWindow[RESEND_WINDOW_MAX - 1]
    const freeAt = blocking.createdAt.getTime() + RESEND_WINDOW_SECONDS * 1000
    retryAfterSeconds = Math.max(1, Math.ceil((freeAt - now) / 1000))
  }

  const base = {
    email,
    // #1804. The push half's fact, reported on EVERY state so a surface can
    // show it without having to be in the `bounced` branch. `at` is an ISO
    // string or null; `kind` is `bounce` | `complaint` | null.
    bounce: { at: isoOrNull(user?.emailBouncedAt), kind: user?.emailBounceKind ?? null },
    sends,
    sendsInWindow: inWindow.length,
    lastSentAt: isoOrNull(lastSent?.createdAt),
    lastError: newest?.status === 'failed' ? (newest.error ?? 'UnknownError') : null,
    retryAfterSeconds,
    checkSpam: sends !== null && sends >= SPAM_HINT_AFTER_SENDS,
    suppression: {
      checked: Boolean(suppressionResult?.checked),
      suppressed: Boolean(suppressionResult?.suppressed),
      reason: suppressionResult?.reason ?? null,
      since: suppressionResult?.since ?? null,
      detail: suppressionResult?.detail ?? null,
    },
    resendWindowSeconds: RESEND_WINDOW_SECONDS,
    resendWindowMax: RESEND_WINDOW_MAX,
  }

  // ── State precedence, most-dominant fact first ──────────────────────────
  //
  // BOUNCED first (#1804). It outranks `suppressed` because it is the same
  // conclusion reached from a STRONGER source: SES pushed the bounce to us,
  // rather than us asking the suppression list and hoping the lookup was
  // permitted and healthy. It is also available when that lookup is not — a
  // missing IAM grant makes `suppression.checked` false, and the pull half
  // then correctly refuses to guess, which is exactly when the pushed fact is
  // the only one there is. And it is the state the signup / resend refusal in
  // auth.js keys off, so this endpoint saying anything softer would contradict
  // the 422 the same account gets from the button next to it.
  if (base.bounce.at) {
    return { ...base, state: VERIFICATION_STATES.BOUNCED, checkSpam: false }
  }

  // SUPPRESSED next: it is the only other state where sending again cannot work,
  // so nothing below it is worth telling the user instead. It is asserted
  // ONLY when the lookup actually ran (`checked && suppressed`); a failed
  // lookup falls through to the states below rather than guessing either way.
  if (base.suppression.checked && base.suppression.suppressed) {
    return { ...base, state: VERIFICATION_STATES.SUPPRESSED, checkSpam: false }
  }

  // FAILED before RATE_LIMITED: "the last attempt errored" is more actionable
  // than "wait 40 seconds", and retryAfterSeconds rides along on every state
  // anyway, so the wait is not lost by ordering it second.
  if (newest?.status === 'failed') {
    return { ...base, state: VERIFICATION_STATES.FAILED }
  }

  if (retryAfterSeconds > 0) {
    return { ...base, state: VERIFICATION_STATES.RATE_LIMITED }
  }

  if (lastSent) {
    return { ...base, state: VERIFICATION_STATES.SENT }
  }

  // No send on record — either none was ever made, or the log is unreadable.
  // Both render as "we have no record", never as "it was sent".
  return { ...base, state: VERIFICATION_STATES.UNKNOWN }
}
