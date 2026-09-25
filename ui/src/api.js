/**
 * Shared API fetch helper — safe error handling for the Archimedes frontend.
 *
 * When nginx returns a 502/503 during deploys, res.text() is multi-line HTML
 * (`<html><body>502 Bad Gateway</body></html>`) that would splat raw across
 * the UI if thrown as an Error message. This helper throws a clean, concise
 * error string instead.
 */

import { getAddress } from './config'
import { EXECUTION_CHAIN_ID } from './chain-config'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

function walletHeaders() {
  const address = getAddress()
  return address ? { 'X-Wallet-Address': address, 'X-Wallet-Chain-Id': String(EXECUTION_CHAIN_ID) } : {}
}

/**
 * GET a JSON endpoint. Throws a clean error on non-2xx responses.
 *
 * `signal` is optional and additive — every existing caller omits it and is
 * unaffected. It exists so a caller that supersedes its own requests (the
 * corpus catalog's debounced search, #1665) can actually *cancel* the request
 * rather than merely ignore the answer: without the signal reaching `fetch`,
 * an abandoned keystroke still costs a full backend table scan. An aborted
 * request rejects with an `AbortError`; see `corpusSearch.isSupersededError`
 * for why that must not be painted as an outage.
 *
 * @param {string} path — API path (e.g. "/api/strategies/")
 * @param {{signal?: AbortSignal}} [options]
 * @returns {Promise<any>} parsed JSON
 */
export async function apiGet(path, { signal } = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: walletHeaders(),
    signal,
  })
  if (!res.ok) {
    const err = new Error(`Backend returned ${res.status}`)
    err.status = res.status // so callers can distinguish 404 (not-deployed) from real failures
    throw err
  }
  return res.json()
}

/**
 * POST JSON to an endpoint. Throws a clean error on non-2xx responses.
 * @param {string} path — API path
 * @param {object} body — JSON-serializable body
 * @returns {Promise<any>} parsed JSON
 */
export async function apiPost(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...walletHeaders() },
    credentials: 'include',
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const err = new Error(`Backend returned ${res.status}`)
    err.status = res.status
    throw err
  }
  return res.json()
}

/**
 * POST JSON to an endpoint, returning both the parsed body AND the raw
 * response `Headers` — for callers that need a response header alongside
 * the body (the generation paywall's PAYMENT-RESPONSE settlement receipt,
 * #1296) rather than just the JSON plain apiPost returns. On a non-2xx
 * response the thrown Error also carries `err.detail` — the parsed body's
 * `detail` field (FastAPI's error-body convention) — so callers can read a
 * structured `{reason, message, ...}` instead of only the status code
 * apiPost's Error already carries via `err.status` — AND `err.headers`, the
 * raw response `Headers` (e.g. the paywall's PAYMENT-REQUIRED header on a
 * 402, read via generateQuote.js's extractPaymentRequiredHeader), since a
 * non-2xx response still carries response headers that a caller may need
 * even though the call is throwing.
 * @param {string} path — API path
 * @param {object} body — JSON-serializable body
 * @param {object} [extraHeaders] — additional request headers (e.g. Payment-Signature)
 * @returns {Promise<{data: any, headers: Headers}>}
 */
export async function apiPostWithMeta(path, body, extraHeaders = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...walletHeaders(), ...extraHeaders },
    credentials: 'include',
    body: JSON.stringify(body),
  })
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    const err = new Error(`Backend returned ${res.status}`)
    err.status = res.status
    err.detail = data?.detail ?? null
    err.headers = res.headers
    throw err
  }
  return { data, headers: res.headers }
}

/**
 * DELETE an endpoint. Throws a clean error on non-2xx responses.
 * @param {string} path — API path
 * @returns {Promise<any>} parsed JSON
 */
export async function apiDelete(path) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'DELETE',
    credentials: 'include',
    headers: walletHeaders(),
  })
  if (!res.ok) {
    const err = new Error(`Backend returned ${res.status}`)
    err.status = res.status
    throw err
  }
  return res.status === 204 ? null : res.json()
}
