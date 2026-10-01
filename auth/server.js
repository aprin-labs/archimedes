import { createServer } from 'node:http'
import { pathToFileURL } from 'node:url'

import { fromNodeHeaders, toNodeHandler } from 'better-auth/node'

import { createAuth, createPool, enabledProviders } from './auth.js'
import { createDeliveryLog } from './delivery-log.js'
import { createMailer } from './mailer.js'
import { createSuppressionLookup } from './suppression.js'
import { resolveVerificationStatus } from './verification-status.js'

function json(res, status, body) {
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json')
  res.setHeader('Cache-Control', 'no-store')
  res.end(JSON.stringify(body))
}

// `req.url` carries the query string; every route below matches on the path
// alone. (The two pre-existing exact-match routes keep comparing `req.url`
// verbatim — narrowing them is a behaviour change this issue did not ask for.)
function pathOf(url) {
  return String(url ?? '').split('?', 1)[0]
}

export function createRequestHandler({
  auth,
  providers = enabledProviders(),
  nodeHandler = toNodeHandler(auth),
  // #1748 item 2. `(user) => Promise<statusBody>`. Left unset the endpoint
  // answers 503, never a cheerful default: an unwired status route saying
  // "sent" would be the same eternal-200 lie in a new place.
  verificationStatus = null,
} = {}) {
  return async (req, res) => {
    try {
      if (req.url === '/health') {
        res.statusCode = 204
        res.end()
        return
      }

      if (req.url === '/api/auth/providers' && req.method === 'GET') {
        json(res, 200, {
          emailPassword: true,
          google: providers.includes('google'),
          github: providers.includes('github'),
          passkey: false,
        })
        return
      }

      if (req.url === '/_internal/auth/session' && req.method === 'GET') {
        const session = await auth.api.getSession({ headers: fromNodeHeaders(req.headers) })
        res.statusCode = session?.user?.id ? 204 : 401
        res.end()
        return
      }

      // ── GET /api/auth/verification-status (#1748 item 2) ────────────────
      //
      // Delivery feedback for the resend button. SESSION-REQUIRED and
      // self-only: it reports on `session.user.email` and never on an address
      // the caller names, so it cannot become the account-existence /
      // verification-state oracle that Better Auth's own constant-time floor
      // on POST /send-verification-email exists to prevent (see
      // verification-status.js's header for why the richer answer could not
      // live on that POST instead).
      //
      // Declared BEFORE the `/api/auth/` catch-all below — that branch hands
      // everything under the prefix to Better Auth, which does not know this
      // path and would answer 404.
      if (pathOf(req.url) === '/api/auth/verification-status' && req.method === 'GET') {
        const session = await auth.api.getSession({ headers: fromNodeHeaders(req.headers) })
        if (!session?.user?.id) {
          json(res, 401, { error: 'Not authenticated' })
          return
        }
        if (!verificationStatus) {
          json(res, 503, { error: 'Verification delivery status is not configured' })
          return
        }
        json(res, 200, await verificationStatus(session.user))
        return
      }

      if (req.url?.startsWith('/api/auth/')) {
        await nodeHandler(req, res)
        return
      }

      json(res, 404, { error: 'Not found' })
    } catch (error) {
      console.error('auth request failed:', error instanceof Error ? error.name : 'UnknownError')
      if (!res.headersSent) json(res, 500, { error: 'Authentication service unavailable' })
      else res.end()
    }
  }
}

export function startServer(env = process.env) {
  // One pool, three consumers: Better Auth's adapter, the delivery log's
  // inserts, and the status endpoint's reads. createPool lives in auth.js so
  // the Aurora sslmode translation has exactly one implementation.
  const db = createPool(env)
  const deliveryLog = createDeliveryLog(db)
  const mailer = createMailer(env, { deliveryLog })
  const suppression = createSuppressionLookup(env)
  const auth = createAuth({ database: db, env, mailer })
  const port = Number(env.PORT || 3000)
  return createServer(createRequestHandler({
    auth,
    providers: enabledProviders(env),
    verificationStatus: user => resolveVerificationStatus({ user, deliveryLog, suppression }),
  })).listen(port, '0.0.0.0', () => {
    console.log(`Archimedes auth listening on ${port}`)
  })
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  startServer()
}
