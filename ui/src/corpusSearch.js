/**
 * Corpus catalog search: debounce + cancel (#1665).
 *
 * `CorpusExplorer` fired one `/api/papers/?search=…` request per keystroke,
 * with no debounce and no `AbortController`. That is two separate defects:
 *
 *   * **load** — typing an 8-character word issued 8 requests, each of which
 *     made the backend scan the papers table for an unanchored `ILIKE`;
 *   * **ordering** — nothing tied a response to the request that asked for it,
 *     so a slow early response could land *after* a fast later one and repaint
 *     the results for a prefix the user had already finished typing. That is
 *     the failure a user actually sees: the box says "momentum", the list says
 *     "mom".
 *
 * The debounce fixes the first. The `AbortController` fixes the second, and it
 * has to fix it on *both* legs — a settled request whose controller has since
 * been aborted must not write state either, because `abort()` cannot un-resolve
 * a promise that already resolved.
 *
 * This lives in a plain module rather than inline in the JSX so the behaviour
 * is testable: `ui/` runs tests under `node --test` with no DOM and no JSX
 * transform, so anything left inside a `.jsx` component can only ever be
 * checked by scanning source text. Same idiom as `rigorGateStatus.js`.
 */

/** Milliseconds of quiet before a keystroke becomes a request. */
export const SEARCH_DEBOUNCE_MS = 300

/**
 * Schedule one debounced catalog fetch and return the cancel function.
 *
 * The return value is exactly what a React effect returns: calling it clears
 * the pending timer (so a superseded keystroke never becomes a request at all)
 * *and* aborts the controller (so one that already became a request is
 * cancelled in flight, and its late response is marked superseded). Aborting a
 * controller whose timer never fired is a no-op, which is why one call handles
 * both cases.
 *
 * @param {(signal: AbortSignal) => void} run — runs once, after `delay` ms of quiet
 * @param {{delay?: number}} [options]
 * @returns {() => void} cancel — clears the timer and aborts the request
 */
export function scheduleCatalogFetch(run, { delay = SEARCH_DEBOUNCE_MS } = {}) {
  const controller = new AbortController()
  const timer = setTimeout(() => { run(controller.signal) }, delay)
  return () => {
    clearTimeout(timer)
    controller.abort()
  }
}

/**
 * True when a rejection is this request being cancelled rather than failing.
 *
 * Cancellation is not an outage, and must never be painted as one: `fetch`
 * rejects an aborted request with an `AbortError`, so without this check every
 * keystroke would flash the catalog's error state.
 *
 * Both arms are load-bearing. `signal.aborted` catches a rejection that lost
 * its `name` crossing a wrapper (`api.js` re-throws its own `Error` on a
 * non-2xx), and the `AbortError` name catches the window where the abort
 * happened but the signal object is not to hand.
 *
 * @param {unknown} err
 * @param {AbortSignal} [signal]
 */
export function isSupersededError(err, signal) {
  return Boolean(signal?.aborted) || err?.name === 'AbortError'
}

/**
 * Run one catalog request and apply its result **only if it is still current**.
 *
 * Returns the outcome rather than throwing, so the caller can tell "this
 * request was replaced" apart from "the backend is down" — they look identical
 * from a `catch` and must not look identical to the user.
 *
 * @param {AbortSignal} signal
 * @param {{
 *   fetchPage: (signal: AbortSignal) => Promise<any>,
 *   onResult: (data: any) => void,
 *   onError: (err: unknown) => void,
 * }} handlers
 * @returns {Promise<'applied'|'superseded'|'failed'>}
 */
export async function runCatalogFetch(signal, { fetchPage, onResult, onError }) {
  try {
    const data = await fetchPage(signal)
    // The ordering fix. `abort()` cannot un-resolve a promise that already
    // resolved, so a request cancelled *after* its response arrived would
    // otherwise still repaint the list with stale rows. Checked here, after
    // the await, is the only place that catches it.
    if (signal?.aborted) return 'superseded'
    onResult(data)
    return 'applied'
  } catch (err) {
    if (isSupersededError(err, signal)) return 'superseded'
    onError(err)
    return 'failed'
  }
}
