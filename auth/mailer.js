// Email delivery for Better Auth verification mail.
//
// Two modes, selected explicitly via EMAIL_MAILER:
//   "ses"     — Amazon SESv2, credentials/region from the task role and
//               AWS_REGION (nothing stored here). Deployed environments.
//   "console" — logs the message to stdout. Local dev/compose: composing the
//               stack needs no AWS account, and the verification URL is
//               readable straight from `docker compose logs auth`.
//
// The default is "console" so a deploy that forgets the env var degrades to a
// loudly-visible log line rather than a silent SES failure. The SES SDK is
// imported lazily inside the ses branch only, so console mode (and its tests)
// never load AWS code at all.
//
// #1748 item 2 — EVERY SEND LEAVES A RECEIPT. Both modes now write one row per
// send to the delivery log (auth/delivery-log.js): the SES MessageId when SES
// accepted the message, the error NAME when it refused. Before this, the only
// trace a send left was a `console.error` on the failure path, so
// `POST /api/auth/send-verification-email` had nothing to report but the
// eternal `200 {status:true}` — see delivery-log.js's header for the full
// argument, and verification-status.js for what reads these rows back.
//
// The record is AWAITED inside send(), which is safe precisely because the
// callers that carry an anti-enumeration timing claim (auth.js's
// sendVerificationEmail / sendResetPassword / sendChangeEmailConfirmation) do
// not await send() at all — their whole point is that this function's duration
// is invisible to the response. `notifyAccountChange` does await it, and there
// the added round trip is one insert on the /link-social path, which carries no
// timing claim. Awaiting is what makes the receipt testable without polling.

import { nullDeliveryLog } from './delivery-log.js'

// `loadClient` is a TEST SEAM, unset in production: it lets
// auth/test/mailer.test.js drive the real ses branch — MessageId capture,
// the failure record, the rethrow — against a stub instead of AWS, so the
// receipt logic is covered by tests rather than by inspection. Same shape
// and same reason as suppression.js's.
export function createMailer(env = process.env, { deliveryLog = nullDeliveryLog(), loadClient } = {}) {
  const sender = env.EMAIL_SENDER || 'no-reply@archimedes-arc.com'
  // #1804. A send that does NOT name a configuration set produces no bounce
  // event, no complaint event, nothing — SES's per-message feedback is
  // published by the configuration set, not by the identity, so an unset
  // ConfigurationSetName is exactly the deaf state this repo was in.
  //
  // Blank/unset is still honoured rather than defaulted to a literal, and the
  // property is omitted (not sent as an empty string, which SES rejects):
  // `infra/ses_events.tf` is what creates the set and `infra/ecs.tf` is what
  // sets this variable, both in the same terraform apply, so before that apply
  // the only truthful thing to do is send the way we always have. The name
  // itself is never hardcoded here for the same reason — the terraform
  // resource is its single source, and backend/tests/test_ses_event_wiring.py
  // fails if the two files stop agreeing.
  const configurationSet = (env.SES_CONFIGURATION_SET || '').trim()

  // Never throws, never rejects — delivery-log.js's record() already swallows
  // its own failures, and this wrapper keeps a mis-shaped custom log from
  // taking a real send down with it.
  async function record(fields) {
    try {
      return await deliveryLog.record(fields)
    } catch (error) {
      console.error('EMAIL_DELIVERY_RECORD_FAILED', {
        kind: fields?.kind, status: fields?.status,
        error: error instanceof Error ? error.name : 'UnknownError',
      })
      return null
    }
  }

  if (loadClient || (env.EMAIL_MAILER || 'console') === 'ses') {
    let clientPromise = null
    function sesClient() {
      clientPromise ??= loadClient
        ? loadClient()
        : import('@aws-sdk/client-sesv2').then(ses => ({ ses, client: new ses.SESv2Client({}) }))
      return clientPromise
    }
    return {
      kind: 'ses',
      sender,
      configurationSet,
      async send({ to, subject, text, kind = 'unknown', userId = null }) {
        const { ses, client } = await sesClient()
        let messageId = null
        try {
          const output = await client.send(new ses.SendEmailCommand({
            FromEmailAddress: sender,
            Destination: { ToAddresses: [to] },
            Content: { Simple: { Subject: { Data: subject }, Body: { Text: { Data: text } } } },
            // #1804: the ONLY thing that makes SES publish a bounce or
            // complaint event for this message. Omitted entirely when blank —
            // SES rejects an empty ConfigurationSetName — so the send keeps
            // working before the terraform apply that sets the variable.
            ...(configurationSet ? { ConfigurationSetName: configurationSet } : {}),
          }))
          messageId = output?.MessageId ?? null
        } catch (error) {
          // Error NAME only, never the message — see delivery-log.js on what
          // is deliberately not stored. Recorded BEFORE the rethrow so the
          // caller's own `.catch` cannot swallow the receipt with the error.
          await record({
            userId, email: to, kind, status: 'failed',
            error: error instanceof Error ? error.name : 'UnknownError',
          })
          throw error
        }
        // A MessageId means SES ACCEPTED the message. It does not mean the
        // message was delivered: SES accepts (and silently drops) mail to an
        // address on the account suppression list, which is exactly why
        // suppression.js exists and why nothing downstream calls this
        // "delivered".
        await record({ userId, email: to, kind, status: 'sent', messageId })
        return { messageId }
      },
    }
  }

  return {
    kind: 'console',
    sender,
    configurationSet,
    async send({ to, subject, text, kind = 'unknown', userId = null }) {
      console.log(`[mailer:console] from=${sender} to=${to} subject=${subject}\n${text}`)
      // Recorded in console mode too, so local dev exercises the same
      // verification-status states production does. messageId stays null:
      // nothing was handed to SES, so there is no id to claim.
      await record({ userId, email: to, kind, status: 'sent', messageId: null })
      return { messageId: null }
    },
  }
}
