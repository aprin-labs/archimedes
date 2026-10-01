// Shared by session-sweep.test.js and server-session-sweep.test.js (#1908).
// No tests of its own.
import assert from 'node:assert/strict'

import { getMigrations } from 'better-auth/db/migration'

import { createAuth } from '../../auth.js'

export const env = {
  NODE_ENV: 'test',
  BETTER_AUTH_SECRET: 'test-only-secret-with-at-least-thirty-two-characters',
  BETTER_AUTH_URL: 'http://localhost:3000',
  BETTER_AUTH_TRUSTED_ORIGINS: 'http://localhost:3000',
}

export const credentials = { email: 'sweep@example.com', password: 'correct horse battery staple' }

function sessionCookie(response) {
  return response.headers.get('set-cookie').split(';', 1)[0]
}

function tokenOf(cookie) {
  return cookie.split('=')[1].split('.')[0]
}

// One user signed in `live + expired` times; the first `expired` sessions are
// then moved a minute into the past, the way time would have moved them.
export async function authWithSessions(database, { live, expired }) {
  const auth = createAuth({ database, env })
  await (await getMigrations(auth.options)).runMigrations()
  await auth.api.signUpEmail({ body: { ...credentials, name: 'Sweep' } })
  const cookies = []
  for (let i = 0; i < live + expired; i++) {
    const response = await auth.api.signInEmail({
      body: credentials,
      headers: new Headers({ 'user-agent': `sweep-test-agent/${i}` }),
      asResponse: true,
    })
    assert.equal(response.status, 200)
    cookies.push(sessionCookie(response))
  }
  const past = new Date(Date.now() - 60 * 1000).toISOString()
  const age = database.prepare('UPDATE auth_sessions SET expiresAt = ? WHERE token = ?')
  for (const cookie of cookies.slice(0, expired)) age.run(past, tokenOf(cookie))
  return {
    auth,
    liveCookies: cookies.slice(expired),
    liveTokens: cookies.slice(expired).map(tokenOf),
    expiredTokens: cookies.slice(0, expired).map(tokenOf),
  }
}

export function remainingTokens(database) {
  return database.prepare('SELECT token FROM auth_sessions ORDER BY token').all().map(row => row.token)
}
