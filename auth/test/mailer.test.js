import assert from 'node:assert/strict'
import test from 'node:test'

import { createMailer } from '../mailer.js'

test('defaults to the console mailer so compose needs no AWS', () => {
  assert.equal(createMailer({}).kind, 'console')
  assert.equal(createMailer({ EMAIL_MAILER: 'console' }).kind, 'console')
})

test('selects SES only when explicitly configured', () => {
  assert.equal(createMailer({ EMAIL_MAILER: 'ses' }).kind, 'ses')
})

test('sender defaults to no-reply@archimedes-arc.com and honors EMAIL_SENDER', () => {
  assert.equal(createMailer({}).sender, 'no-reply@archimedes-arc.com')
  assert.equal(createMailer({ EMAIL_SENDER: 'auth@example.com' }).sender, 'auth@example.com')
})

test('console mailer prints the full message including the URL', async () => {
  const lines = []
  const original = console.log
  console.log = (...args) => lines.push(args.join(' '))
  try {
    await createMailer({}).send({
      to: 'user@example.com',
      subject: 'Verify',
      text: 'https://archimedes-arc.com/verify?token=abc',
    })
  } finally {
    console.log = original
  }
  assert.equal(lines.length, 1)
  assert.ok(lines[0].includes('to=user@example.com'))
  assert.ok(lines[0].includes('https://archimedes-arc.com/verify?token=abc'))
})

// ── #1804: the configuration set is what makes a send observable ───────────
//
// SES publishes bounce/complaint events per CONFIGURATION SET, not per
// identity. A send that names none is a perfectly successful send that
// produces no event, which is the deaf state the whole issue is about. The
// name itself is never hardcoded: infra/ses_events.tf owns it and
// infra/ecs.tf hands it to this process as SES_CONFIGURATION_SET, a pairing
// backend/tests/test_ses_event_wiring.py fails on if the two files drift.

test('the configuration set comes from the environment, never from a literal', () => {
  assert.equal(createMailer({ EMAIL_MAILER: 'ses', SES_CONFIGURATION_SET: 'archimedes-mail' }).configurationSet,
    'archimedes-mail')
  // Whitespace-only is not a name — SES rejects a blank ConfigurationSetName,
  // so it has to collapse to "unset", not to "send an empty string".
  assert.equal(createMailer({ EMAIL_MAILER: 'ses', SES_CONFIGURATION_SET: '   ' }).configurationSet, '')
})

test('an unset configuration set leaves the mailer exactly as it was', () => {
  // This file lands before the terraform apply that creates the set. Until
  // then the variable is unset and mail must keep going out unchanged — the
  // loop is merely not yet listening.
  assert.equal(createMailer({ EMAIL_MAILER: 'ses' }).configurationSet, '')
  assert.equal(createMailer({}).configurationSet, '')
})

// The property above is only half the claim. What actually makes SES publish a
// bounce event is `ConfigurationSetName` appearing in the SendEmailCommand
// INPUT, so that is what this asserts — against the real `ses` branch, through
// #1790's `loadClient` seam, with no AWS SDK loaded.
function sendCapturingSes() {
  const inputs = []
  const ses = { SendEmailCommand: class { constructor(input) { this.input = input } } }
  const client = { async send(command) { inputs.push(command.input); return { MessageId: 'ses-1' } } }
  return { inputs, loadClient: async () => ({ ses, client }) }
}

test('a configured set is actually named on the SendEmailCommand SES receives', async () => {
  const spy = sendCapturingSes()
  await createMailer(
    { EMAIL_MAILER: 'ses', SES_CONFIGURATION_SET: 'archimedes-mail' },
    { loadClient: spy.loadClient },
  ).send({ to: 'dan@example.com', subject: 's', text: 't' })
  assert.equal(spy.inputs.length, 1)
  assert.equal(spy.inputs[0].ConfigurationSetName, 'archimedes-mail')
})

test('with no configured set the property is ABSENT, not an empty string', async () => {
  // SES rejects `ConfigurationSetName: ""` outright, so "unset" has to mean
  // "omit the key". Sending an empty string would turn the pre-apply state
  // from "deaf" into "every verification email fails".
  const spy = sendCapturingSes()
  await createMailer({ EMAIL_MAILER: 'ses' }, { loadClient: spy.loadClient })
    .send({ to: 'dan@example.com', subject: 's', text: 't' })
  assert.equal(spy.inputs.length, 1)
  assert.equal(Object.hasOwn(spy.inputs[0], 'ConfigurationSetName'), false)
})
