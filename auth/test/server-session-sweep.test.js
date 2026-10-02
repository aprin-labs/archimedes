// #1908: the expired-session sweep only matters if the process that runs in
// production actually runs it. This starts the real sidecar entry point
// (startServer, what `node server.js` calls) on a test database and lets the
// clock move: no request is made, so nothing but the server's own schedule can
// delete the rows.
import assert from 'node:assert/strict'
import { once } from 'node:events'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'

import { startServer } from '../server.js'
import { authWithSessions, env, remainingTokens, settle } from './fixtures/sessions.js'

const MINUTE_MS = 60 * 1000
const HOUR_MS = 60 * MINUTE_MS

test('the running auth server deletes expired sessions within an hour, then again on every hour of uptime', async t => {
  const database = new DatabaseSync(':memory:')
  const { liveTokens } = await authWithSessions(database, { live: 2, expired: 2 })
  const [laterA, laterB] = [...liveTokens].sort()
  assert.equal(remainingTokens(database).length, 4)

  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  const server = startServer({ ...env, PORT: '0' }, { database })
  t.after(() => new Promise(resolve => server.close(resolve)))
  await once(server, 'listening')

  // The clock moves through the first minute and then the rest of the hour,
  // as it does in production. (One tick of a whole hour would run every timer
  // with the clock already at 1 h, so a timer armed by the first run would be
  // indistinguishable from one armed at boot.)
  t.mock.timers.tick(MINUTE_MS)
  await settle(() => remainingTokens(database).length === 2)
  t.mock.timers.tick(HOUR_MS - MINUTE_MS)
  await settle(() => remainingTokens(database).length === 2)
  // Let the run due at 1 h finish before the clock moves on.
  await settle(() => remainingTokens(database).length < 2)
  assert.deepEqual(remainingTokens(database), [laterA, laterB], 'expired sessions survived the first hour of uptime')

  // Sessions keep expiring after boot. The schedule the PR states is a run on
  // every hour of uptime (the hourly timer counts from boot), so a session
  // that has expired by then goes at the next whole hour, not before and not
  // later: one at 2 h of uptime, the other at 3 h.
  for (const [hour, token] of [[2, laterA], [3, laterB]]) {
    const left = remainingTokens(database).length
    database.prepare('UPDATE auth_sessions SET expiresAt = ? WHERE token = ?').run(new Date(Date.now() - 1000).toISOString(), token)

    t.mock.timers.tick(HOUR_MS - 1)
    await settle(() => remainingTokens(database).length !== left)
    assert.ok(remainingTokens(database).includes(token), `a run came before ${hour} h of uptime`)

    t.mock.timers.tick(1)
    await settle(() => !remainingTokens(database).includes(token))
    assert.ok(!remainingTokens(database).includes(token), `no run at ${hour} h of uptime`)
    assert.equal(remainingTokens(database).length, left - 1)
  }
})

test('the running auth server first sweeps 60 seconds after boot, not before', async t => {
  // The schedule the PR states: first run one minute after boot (so a fresh
  // task clears a backlog without waiting an hour), not at boot itself.
  const database = new DatabaseSync(':memory:')
  const { liveTokens } = await authWithSessions(database, { live: 1, expired: 2 })

  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  const server = startServer({ ...env, PORT: '0' }, { database })
  t.after(() => new Promise(resolve => server.close(resolve)))
  await once(server, 'listening')

  t.mock.timers.tick(60 * 1000 - 1)
  await settle(() => remainingTokens(database).length !== 3)
  assert.equal(remainingTokens(database).length, 3, 'the sweep ran before 60 s of uptime')

  t.mock.timers.tick(1)
  await settle(() => remainingTokens(database).length === 1)
  assert.deepEqual(remainingTokens(database), liveTokens, 'the sweep had not run 60 s after boot')
})
