// #1908 "Data that never expires": expired auth_sessions rows, each holding the
// ipAddress and userAgent it was created with, were never deleted. Better Auth
// 1.6.25 deletes an expired session only when its cookie is presented again,
// and that cookie's Max-Age is the session's own lifetime.
import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'
import { inspect } from 'node:util'

import pg from 'pg'

import { startSessionSweep, sweepExpiredSessions } from '../session-sweep.js'
import { authWithSessions, credentials, remainingTokens, settle } from './fixtures/sessions.js'

const MINUTE_MS = 60 * 1000
const HOUR_MS = 60 * MINUTE_MS

function recordingLog() {
  const lines = []
  return { lines, log: { log: (...args) => lines.push(['log', ...args]), error: (...args) => lines.push(['error', ...args]) } }
}

test('the sweep deletes sessions whose expiresAt has passed and keeps live ones', async () => {
  const database = new DatabaseSync(':memory:')
  const { auth, liveCookies, liveTokens, expiredTokens } = await authWithSessions(database, { live: 2, expired: 3 })
  // Every row carries the IP address and user agent it was created with:
  // that is the data that used to stay forever.
  assert.equal(database.prepare('SELECT COUNT(*) AS n FROM auth_sessions WHERE ipAddress IS NOT NULL AND userAgent IS NOT NULL').get().n, 5)

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

test('a database error fails only that run: it is logged by error class alone and the next run retries', async t => {
  // Production runs the sweep on a timer inside the auth sidecar. An Aurora
  // failover, or Postgres aborting one of two overlapping DELETEs, must cost
  // one run, not the timer, and must not put session data in the logs.
  const database = new DatabaseSync(':memory:')
  const { auth, liveTokens, expiredTokens } = await authWithSessions(database, { live: 1, expired: 2 })
  const row = database.prepare('SELECT id, token, ipAddress, userAgent FROM auth_sessions WHERE token = ?').get(expiredTokens[0])
  const { adapter } = await auth.$context
  const deleteMany = adapter.deleteMany
  let failuresLeft = 1
  adapter.deleteMany = async args => {
    if (failuresLeft-- > 0) {
      // The error pg raises, carrying session data the way a real one can
      // (message, detail, where), so a log of the error itself would leak it.
      const error = new pg.DatabaseError(`deadlock detected on session ${row.id} (${row.token})`, 0, 'error')
      Object.assign(error, { code: '40P01', detail: `Key (token)=(${row.token}) ipAddress=${row.ipAddress}`, where: `userAgent=${row.userAgent}` })
      throw error
    }
    return deleteMany(args)
  }
  const { lines, log } = recordingLog()

  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  const sweep = startSessionSweep(auth, { log })
  t.after(sweep.stop)

  t.mock.timers.tick(MINUTE_MS)
  await settle(() => lines.length > 0)
  assert.deepEqual(lines, [['error', 'AUTH_SESSION_SWEEP_FAILED', { error: 'DatabaseError' }]])
  const logged = inspect(lines, { depth: null })
  for (const value of [row.id, row.token, row.ipAddress, row.userAgent, 'deadlock', 'Key (token)']) {
    assert.ok(!logged.includes(value), `the failure log contains ${JSON.stringify(value)}`)
  }
  assert.equal(remainingTokens(database).length, 3, 'the failed run still deleted rows')

  // Next tick: the timer is still armed and the same statement now succeeds.
  t.mock.timers.tick(HOUR_MS - MINUTE_MS)
  await settle(() => remainingTokens(database).length === 1)
  assert.deepEqual(remainingTokens(database), liveTokens, 'the run after a failed one did not retry')
  assert.deepEqual(lines.at(-1), ['log', 'auth session sweep: deleted 2 expired session(s)'])
})

test('a failed run resolves to 0 rather than throwing, whatever was thrown', async () => {
  // run() is called from timers, where a rejection would be unhandled.
  for (const [thrown, logged] of [[new TypeError('t'), 'TypeError'], ['a bare string', 'UnknownError']]) {
    const auth = { $context: Promise.resolve({ adapter: { findMany: async () => { throw thrown } } }) }
    const { lines, log } = recordingLog()
    const sweep = startSessionSweep(auth, { log })
    try {
      assert.equal(await sweep.run(), 0)
    } finally {
      sweep.stop()
    }
    assert.deepEqual(lines, [['error', 'AUTH_SESSION_SWEEP_FAILED', { error: logged }]])
  }
})

test('a run still going when the next one is due is not doubled up, and the one after that runs', async t => {
  const database = new DatabaseSync(':memory:')
  const { auth, liveTokens } = await authWithSessions(database, { live: 1, expired: 2 })
  const { adapter } = await auth.$context
  const findMany = adapter.findMany
  let selects = 0
  let release
  const stuck = new Promise(resolve => { release = resolve })
  adapter.findMany = async args => {
    selects++
    if (selects === 1) await stuck // the first run hangs, e.g. on a lock or a slow failover
    return findMany(args)
  }
  const { log } = recordingLog()

  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  const sweep = startSessionSweep(auth, { log })
  t.after(sweep.stop)

  t.mock.timers.tick(MINUTE_MS)
  await settle(() => selects === 1)
  assert.equal(selects, 1, 'the first run never started')

  // The hourly run comes due while the first is still stuck, and so does a
  // direct call: both join the run in flight instead of starting another.
  t.mock.timers.tick(HOUR_MS - MINUTE_MS)
  const joined = sweep.run()
  await settle(() => selects > 1)
  assert.equal(selects, 1, 'a second run started while the first was still going')

  release()
  assert.equal(await joined, 2, 'the joined call did not get the stuck run\'s result')
  assert.deepEqual(remainingTokens(database), liveTokens)

  // Once that run has finished, the next one due must actually run.
  t.mock.timers.tick(HOUR_MS)
  await settle(() => selects === 2)
  assert.equal(selects, 2, 'no run started after the stuck one finished')
})

test('the sweep never keeps the process alive on its own', async () => {
  // A process that has started the sweep and nothing else must exit by itself,
  // without waiting for the first run (60 s) or the hourly timer (forever).
  const sweepModule = new URL('../session-sweep.js', import.meta.url).href
  const script = `import { startSessionSweep } from ${JSON.stringify(sweepModule)}\nstartSessionSweep({})`
  const outcome = await new Promise(resolve => {
    execFile(process.execPath, ['--input-type=module', '-e', script], { timeout: 10_000 }, error => {
      resolve(error ? `still running after 10 s (${error.signal ?? error.code})` : 'exited')
    })
  })
  assert.equal(outcome, 'exited', 'the sweep timers keep the process alive')
})
