// SESv2 account-level suppression-list lookup (#1748 item 2).
//
// THE FAILURE THIS EXISTS TO NAME. SES's SendEmail call SUCCEEDS for an
// address on the account suppression list — it returns a MessageId and then
// drops the message. So "we recorded a MessageId" is not "the mail was
// delivered", and a delivery log alone would still let the resend button lie,
// just with more rows behind it. GetSuppressedDestination is the only way this
// service can tell the difference from the outside.
//
// Credentials: none here. Same posture as mailer.js's SES branch — the ECS
// task role supplies them and the SDK is imported LAZILY inside the ses branch
// so console-mode (local compose, and every test that does not opt in) never
// loads AWS code at all. The IAM statement is `ses:GetSuppressedDestination`
// on `*` in infra/ecs.tf: the suppression list is an ACCOUNT-level resource,
// not an identity-level one, so it cannot be scoped to the domain identity ARN
// the send statement uses.
//
// Cost: one API call per verification-status request, not per send. Deliberate
// — the suppression state is what a waiting human asks about, and asking on
// every send would put an extra AWS round trip inside the mail path for no
// reader.

/** SESv2's "this address is not suppressed" answer is an exception, not a flag. */
const NOT_SUPPRESSED_ERRORS = new Set(['NotFoundException', 'ResourceNotFoundException'])

/**
 * A check that was never made. `checked: false` is load-bearing: callers must
 * render it as "we could not look", never as "not suppressed" — the two are
 * different claims and only one of them is knowledge.
 */
function unchecked(detail, error = null) {
  return { checked: false, suppressed: false, reason: null, since: null, detail, error }
}

/**
 * @param {object} env
 * @param {{loadClient?: () => Promise<{ses: object, client: object}>}} [options]
 *   Test seam. Production leaves it unset and gets the lazily-imported SDK.
 */
export function createSuppressionLookup(env = process.env, { loadClient } = {}) {
  const mode = env.EMAIL_MAILER || 'console'

  if (!loadClient && mode !== 'ses') {
    // Console mailer: nothing was ever handed to SES, so there is no
    // suppression state to have. Says so, rather than answering "clean".
    return {
      kind: 'disabled',
      async check() {
        return unchecked('suppression is only observable with the SES mailer')
      },
    }
  }

  let clientPromise = null
  function sesClient() {
    clientPromise ??= loadClient
      ? loadClient()
      : import('@aws-sdk/client-sesv2').then(ses => ({ ses, client: new ses.SESv2Client({}) }))
    return clientPromise
  }

  return {
    kind: 'ses',
    async check(address) {
      const email = String(address ?? '').trim()
      if (!email) return unchecked('no address to check')
      try {
        const { ses, client } = await sesClient()
        const output = await client.send(new ses.GetSuppressedDestinationCommand({ EmailAddress: email }))
        const destination = output?.SuppressedDestination ?? {}
        const lastUpdate = destination.LastUpdateTime
        return {
          checked: true,
          suppressed: true,
          // BOUNCE or COMPLAINT per the SESv2 API; passed through rather than
          // mapped, so a value AWS adds later reaches the reader intact.
          reason: destination.Reason ?? 'UNKNOWN',
          since: lastUpdate instanceof Date
            ? lastUpdate.toISOString()
            : (typeof lastUpdate === 'string' ? lastUpdate : null),
          detail: null,
          error: null,
        }
      } catch (error) {
        const name = error instanceof Error ? error.name : 'UnknownError'
        if (NOT_SUPPRESSED_ERRORS.has(name)) {
          return { checked: true, suppressed: false, reason: null, since: null, detail: null, error: null }
        }
        // Throttling, AccessDenied (the IAM statement never applied), a region
        // with no SES — all of them mean "we do not know", and none of them
        // may be rendered as "not suppressed".
        console.error('SUPPRESSION_CHECK_FAILED', { error: name })
        return unchecked('suppression lookup failed', name)
      }
    },
  }
}
