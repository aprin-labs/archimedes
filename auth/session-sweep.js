// Expired-session sweep (#1908, "Data that never expires").
//
// Every auth_sessions row keeps the ipAddress and userAgent it was created
// with. Better Auth 1.6.25 deletes an expired row only when that session's
// cookie is presented again (node_modules/better-auth/dist/api/routes/
// session.mjs:182-190). The cookie's Max-Age is the session's lifetime
// (cookies/index.mjs:47), so a browser has normally dropped it by the time the
// row expires, and a user who never comes back never presents it at all.
// The library sweeps expired VERIFICATION rows on its own
// (db/internal-adapter.mjs, findVerificationValue) but has no equivalent for
// sessions, so without this every expired row would stay forever.
//
// Where it runs: inside this sidecar, on a timer started by startServer(). The
// sidecar is already running in every production task and owns this table, so
// the sweep needs no new infrastructure. Production runs two tasks, so two
// sweeps can overlap; that is safe by construction:
//   - the select and the delete both say `expiresAt < now`, so a run can only
//     ever delete a session that is expired at the moment it deletes it;
//   - if both runs pick the same ids, the second DELETE finds those rows
//     already gone and deletes nothing for them: each row is deleted once, and
//     the counts the two runs report add up to the rows actually removed. No
//     transaction or lock is held between the select and the delete;
//   - each run deletes at most batchSize rows, so one run is one short
//     statement however large a backlog is. A backlog drains at up to
//     batchSize rows per task per interval, and at least batchSize per
//     interval while any task is up. Overlapping runs are why it can be the
//     lower figure: both can select the same ids, and then the two runs
//     together delete one batch, not two;
//   - any database error (Aurora failover, or Postgres aborting one of two
//     overlapping DELETEs) fails only that run. It is logged and the next run
//     simply repeats the same idempotent statement.
//
// It goes through Better Auth's own adapter (model 'session'), not raw SQL, so
// the table name stays the one configured in auth.js and the same code runs on
// Postgres in production and on SQLite in the tests.

export const SESSION_SWEEP_INTERVAL_MS = 60 * 60 * 1000
export const SESSION_SWEEP_FIRST_RUN_MS = 60 * 1000
export const SESSION_SWEEP_BATCH_SIZE = 500

/**
 * Delete up to `batchSize` sessions whose expiresAt is before `now`.
 * Resolves to the number of rows this call deleted.
 */
export async function sweepExpiredSessions(auth, { now = new Date(), batchSize = SESSION_SWEEP_BATCH_SIZE } = {}) {
  const { adapter } = await auth.$context
  const expiredBefore = [{ field: 'expiresAt', operator: 'lt', value: now }]
  const expired = await adapter.findMany({
    model: 'session',
    where: expiredBefore,
    limit: batchSize,
    select: ['id'],
  })
  if (expired.length === 0) return 0
  return adapter.deleteMany({
    model: 'session',
    where: [{ field: 'id', operator: 'in', value: expired.map(row => row.id) }, ...expiredBefore],
  })
}

/**
 * Run sweepExpiredSessions shortly after boot and then on every interval.
 * Never throws and never keeps the process alive on its own. A run that is
 * still going when the next one is due is not doubled up.
 */
export function startSessionSweep(auth, {
  intervalMs = SESSION_SWEEP_INTERVAL_MS,
  firstRunMs = SESSION_SWEEP_FIRST_RUN_MS,
  batchSize = SESSION_SWEEP_BATCH_SIZE,
  log = console,
} = {}) {
  let inFlight = null

  function run() {
    if (inFlight) return inFlight
    inFlight = sweepExpiredSessions(auth, { batchSize })
      .then(deleted => {
        if (deleted > 0) log.log(`auth session sweep: deleted ${deleted} expired session(s)`)
        return deleted
      })
      .catch(error => {
        // Counts and error class only: never a row, a token, an IP or a user
        // agent, so never the error's message, detail or stack, which a
        // database error can fill with the statement's values. The class comes
        // from the constructor, not error.name: pg's DatabaseError sets name
        // to the protocol message type, the string 'error'.
        log.error('AUTH_SESSION_SWEEP_FAILED', { error: error instanceof Error ? error.constructor.name : 'UnknownError' })
        return 0
      })
      .finally(() => {
        inFlight = null
      })
    return inFlight
  }

  const first = setTimeout(run, firstRunMs)
  const every = setInterval(run, intervalMs)
  first.unref?.()
  every.unref?.()

  return {
    run,
    stop() {
      clearTimeout(first)
      clearInterval(every)
    },
  }
}
