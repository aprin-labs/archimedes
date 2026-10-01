// #1908 "Data that never expires": expired auth_sessions rows, each holding the
// ipAddress and userAgent it was created with, were never deleted. Better Auth
// 1.6.25 deletes an expired session only when its cookie is presented again,
// and that cookie's Max-Age is the session's own lifetime.
import assert from 'node:assert/strict'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'

import { sweepExpiredSessions } from '../session-sweep.js'
import { authWithSessions, credentials, remainingTokens } from './fixtures/sessions.js'

test('the sweep deletes sessions whose expiresAt has passed and keeps live ones', async () => {
  const database = new DatabaseSync(':memory:')
  const { auth, liveCookies, liveTokens, expiredTokens } = await authWithSessions(database, { live: 2, expired: 3 })
  // Every row carries the user agent it was created with (and, in
  // production, an IP address): that is the data that used to stay forever.
  assert.equal(database.prepare('SELECT COUNT(*) AS n FROM auth_sessions WHERE userAgent IS NOT NULL').get().n, 5)

  assert.equal(await sweepExpiredSessions(auth), expiredTokens.length)
  assert.deepEqual(remainingTokens(database), [...liveTokens].sort())

  // The live sessions still sign their holders in.
  for (const cookie of liveCookies) {
    const session = await auth.api.getSession({ headers: new Headers({ cookie }) })
    assert.equal(session?.user?.email, credentials.email)
  }
})

test('the sweep is idempotent: a second run finds nothing to delete', async () => {
  const database = new DatabaseSync(':memory:')
  const { auth, liveTokens } = await authWithSessions(database, { live: 1, expired: 2 })

  assert.equal(await sweepExpiredSessions(auth), 2)
  assert.equal(await sweepExpiredSessions(auth), 0)
  assert.deepEqual(remainingTokens(database), liveTokens)
})

test('the sweep is bounded: one run deletes at most batchSize rows', async () => {
  const database = new DatabaseSync(':memory:')
  const { auth, liveTokens } = await authWithSessions(database, { live: 1, expired: 3 })

  assert.equal(await sweepExpiredSessions(auth, { batchSize: 2 }), 2)
  assert.equal(remainingTokens(database).length, 2)
  assert.equal(await sweepExpiredSessions(auth, { batchSize: 2 }), 1)
  assert.deepEqual(remainingTokens(database), liveTokens)
})

test('two sweeps racing over the same expired rows delete each row once and neither fails', async () => {
  // Production runs one auth sidecar per ECS task, two tasks, each with its
  // own timer: both can select the same expired ids before either deletes.
  const database = new DatabaseSync(':memory:')
  const { auth, liveTokens, expiredTokens } = await authWithSessions(database, { live: 2, expired: 4 })

  const [a, b] = await Promise.all([sweepExpiredSessions(auth), sweepExpiredSessions(auth)])

  assert.equal(a + b, expiredTokens.length, `deleted ${a} + ${b}, expected ${expiredTokens.length} in total`)
  assert.deepEqual(remainingTokens(database), [...liveTokens].sort())
})

test('a session that stops being expired between the select and the delete is kept', async () => {
  // The delete repeats `expiresAt < now` rather than trusting the ids it was
  // handed, so it can only remove rows that are expired when it runs.
  const database = new DatabaseSync(':memory:')
  const { auth, expiredTokens } = await authWithSessions(database, { live: 0, expired: 2 })
  const { adapter } = await auth.$context
  const findMany = adapter.findMany
  adapter.findMany = async args => {
    const rows = await findMany(args)
    const later = new Date(Date.now() + 60 * 60 * 1000).toISOString()
    database.prepare('UPDATE auth_sessions SET expiresAt = ? WHERE token = ?').run(later, expiredTokens[0])
    return rows
  }

  assert.equal(await sweepExpiredSessions(auth), 1)
  assert.deepEqual(remainingTokens(database), [expiredTokens[0]])
})
