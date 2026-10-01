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

const HOUR_MS = 60 * 60 * 1000

test('the running auth server deletes expired sessions within an hour, and keeps doing it', async t => {
  const database = new DatabaseSync(':memory:')
  const { liveTokens } = await authWithSessions(database, { live: 1, expired: 2 })
  assert.equal(remainingTokens(database).length, 3)

  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  const server = startServer({ ...env, PORT: '0' }, { database })
  t.after(() => new Promise(resolve => server.close(resolve)))
  await once(server, 'listening')

  t.mock.timers.tick(HOUR_MS)
  await settle(() => remainingTokens(database).length === 1)
  assert.deepEqual(remainingTokens(database), liveTokens, 'expired sessions survived the first hour of uptime')

  // The live session expires later; the next scheduled run must take it too.
  database.prepare('UPDATE auth_sessions SET expiresAt = ?').run(new Date(Date.now() - 1000).toISOString())
  t.mock.timers.tick(HOUR_MS)
  await settle(() => remainingTokens(database).length === 0)
  assert.deepEqual(remainingTokens(database), [], 'the sweep ran once and never again')
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
