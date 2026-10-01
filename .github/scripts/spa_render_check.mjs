#!/usr/bin/env node
// Render the SPA in a real headless Chrome and fail if it comes up blank or
// crashed.
//
// Usage: node .github/scripts/spa_render_check.mjs <url> [<url> ...]
//
// Why this exists: from 2026-09-25 to 2026-10-01 https://archimedes-arc.com
// served a blank page. Dependabot PR #1872 bumped `react` to 19.3.0 without
// `react-dom` (19.2.8); react-dom refuses to start against a different react
// version and throws React error #527 at module load, so nothing ever mounted
// into #root. Every check the deploy ran stayed green: curl got a 200 with a
// valid index.html, /health answered, the rollout completed. A static fetch
// cannot see this class at all. Only executing the bundle can.
//
// Each URL is loaded in its own fresh browser context (an anonymous first
// visit: no cookies, no storage, cache disabled) and must pass all of:
//   1. the document request answers (no navigation error, HTTP status < 400)
//   2. no uncaught exception during startup (CDP Runtime.exceptionThrown,
//      which also reports unhandled promise rejections and React's
//      reportError() of a render error no boundary caught)
//   3. React logged no render error via console.error. React 19 production
//      builds report an error an error boundary CAUGHT with a bare
//      console.error(error), never as an uncaught exception, so check 2 is
//      blind to it; this repo's ErrorBoundary also logs "[ErrorBoundary]"
//      from componentDidCatch. See RENDER_ERROR_MARKERS.
//   4. no error-boundary fallback is showing. ui/src/components/ErrorBoundary.jsx
//      renders its "This page crashed / <message> / Reload" card with
//      data-error-boundary="crashed"; that card fills #root, so the #root
//      check below would otherwise call a crashed app "rendered".
//   5. #root exists and has rendered children once the page has loaded
// Any failure prints a `::error::` line (a GitHub Actions annotation; plain
// text elsewhere) and exits 1. Exit 2 means the check itself could not run
// (no Chrome, Node without a global WebSocket, bad arguments).
//
// Retries (RENDER_CHECK_ATTEMPTS, default 1): an attempt is retried, from a
// fresh browser context, ONLY when everything that went wrong is transport:
// the navigation failed or timed out, the document answered 5xx, or a script
// answered 5xx / failed at the network level and the page is blank because of
// it. A transient CloudFront/ALB 5xx says nothing about the build. An uncaught
// exception, a React render error or a crash card is never retried, even if a
// flaky asset might have caused it: from the browser, an intermittent startup
// crash looks the same as a flaky edge, and a crash that only happens on some
// loads is a crash real visitors get. Retrying it until it passes would turn
// the gate into a coin toss. A 4xx is never retried either: a missing
// index.html or asset is a deploy fault, not a blip.
//
// No npm dependencies on purpose: it speaks the Chrome DevTools Protocol over
// Node's built-in WebSocket (Node >= 22), so it runs from a bare checkout with
// the runner's preinstalled Google Chrome.
//
// Environment (all optional):
//   CHROME_PATH                  Chrome/Chromium binary (default: probe the usual names)
//   RENDER_CHECK_MAX_SECONDS     hard wall-clock ceiling for the WHOLE run: every URL,
//                                every attempt and every retry delay (default 60).
//                                deploy.yml's tail-reserve arithmetic relies on it.
//   RENDER_CHECK_ATTEMPTS        attempts per URL, extra ones only after a transport-only
//                                failure (default 1; the post-rollout smoke uses 3)
//   RENDER_CHECK_RETRY_DELAY_MS  pause before a retry (default 5000)
//   RENDER_CHECK_NAV_MS          how long the document request may take before the
//                                attempt counts as a navigation failure (default 15000)
//   RENDER_CHECK_MOUNT_MS        how long after `load` #root may stay empty before
//                                the page counts as blank (default 15000)
//   RENDER_CHECK_SETTLE_MS       how long to keep listening for startup errors after
//                                #root first has children, the page has loaded and no
//                                script is in flight (lazy route chunks) (default 2000)

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';

const MAX_SECONDS = Number(process.env.RENDER_CHECK_MAX_SECONDS ?? 60);
const ATTEMPTS = Number(process.env.RENDER_CHECK_ATTEMPTS ?? 1);
const RETRY_DELAY_MS = Number(process.env.RENDER_CHECK_RETRY_DELAY_MS ?? 5000);
const NAV_MS = Number(process.env.RENDER_CHECK_NAV_MS ?? 15000);
const MOUNT_MS = Number(process.env.RENDER_CHECK_MOUNT_MS ?? 15000);
const SETTLE_MS = Number(process.env.RENDER_CHECK_SETTLE_MS ?? 2000);

// The attribute ui/src/components/ErrorBoundary.jsx puts on its fallback.
// backend/tests/test_spa_render_check.py asserts the component still sets it.
const CRASH_SELECTOR = '[data-error-boundary="crashed"]';

// A console.error carrying any of these is React reporting a render error.
const RENDER_ERROR_MARKERS = [
  // ErrorBoundary.jsx's componentDidCatch: every render error any of this
  // app's boundaries catches, production and development builds alike.
  '[ErrorBoundary]',
  // React 19 development builds, for an error a boundary caught.
  'The above error occurred in',
  // A React invariant (production builds strip the message to this).
  'Minified React error #',
];

function usage(message) {
  console.log(`::error::spa_render_check: ${message}`);
  console.log('usage: node .github/scripts/spa_render_check.mjs <url> [<url> ...]');
  process.exit(2);
}

const targets = process.argv.slice(2);
if (targets.length === 0) usage('at least one argument (a URL to load) is required');
for (const target of targets) {
  let parsed;
  try {
    parsed = new URL(target);
  } catch {
    usage(`not a URL: ${target}`);
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) usage(`not an http(s) URL: ${target}`);
}
if (!Number.isFinite(MAX_SECONDS) || MAX_SECONDS <= 0) usage('RENDER_CHECK_MAX_SECONDS must be a positive number');
if (!Number.isInteger(ATTEMPTS) || ATTEMPTS < 1) usage('RENDER_CHECK_ATTEMPTS must be a positive integer');
if (!Number.isFinite(RETRY_DELAY_MS) || RETRY_DELAY_MS < 0) usage('RENDER_CHECK_RETRY_DELAY_MS must be a non-negative number');
if (!Number.isFinite(NAV_MS) || NAV_MS <= 0) usage('RENDER_CHECK_NAV_MS must be a positive number');
if (!Number.isFinite(MOUNT_MS) || MOUNT_MS <= 0) usage('RENDER_CHECK_MOUNT_MS must be a positive number');
if (!Number.isFinite(SETTLE_MS) || SETTLE_MS < 0) usage('RENDER_CHECK_SETTLE_MS must be a non-negative number');
if (typeof WebSocket !== 'function') {
  usage(`Node ${process.versions.node} has no global WebSocket; Node >= 22 is required`);
}

function findChrome() {
  if (process.env.CHROME_PATH) return process.env.CHROME_PATH;
  const absolute = [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
  ];
  for (const candidate of absolute) if (existsSync(candidate)) return candidate;
  const names = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'];
  for (const dir of (process.env.PATH ?? '').split(delimiter)) {
    for (const name of names) {
      const candidate = join(dir, name);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

const chromePath = findChrome();
if (!chromePath) usage('no Chrome/Chromium found; set CHROME_PATH');

const profileDir = mkdtempSync(join(tmpdir(), 'spa-render-check-'));
const chromeArgs = [
  '--headless=new',
  '--remote-debugging-port=0',
  `--user-data-dir=${profileDir}`,
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-extensions',
  '--disable-gpu',
  '--window-size=1280,900',
  'about:blank',
];
// GitHub's Ubuntu runners restrict unprivileged user namespaces (AppArmor),
// which Chrome's Linux sandbox needs. The runner is a throwaway VM loading
// our own site, so the sandbox buys nothing there.
if (process.platform === 'linux') chromeArgs.unshift('--no-sandbox');

const chrome = spawn(chromePath, chromeArgs, { stdio: ['ignore', 'ignore', 'pipe'] });
let finished = false;
let current = 'starting Chrome';

function cleanup() {
  try {
    chrome.kill('SIGKILL');
  } catch {
    // already gone
  }
  try {
    rmSync(profileDir, { recursive: true, force: true });
  } catch {
    // best effort; a leftover temp profile is harmless
  }
}

function finish(code, lines) {
  if (finished) return;
  finished = true;
  for (const line of lines) console.log(line);
  cleanup();
  process.exit(code);
}

// The one hard ceiling. Attempts, retry delays and every URL all run inside
// it, so the worst case of a run is MAX_SECONDS no matter how many URLs or
// attempts it was given.
setTimeout(() => {
  finish(1, [
    `::error::spa_render_check: did not finish within ${MAX_SECONDS}s (RENDER_CHECK_MAX_SECONDS); it was ${current}`,
  ]);
}, MAX_SECONDS * 1000);

chrome.on('error', (err) => finish(2, [`::error::spa_render_check: could not start Chrome at ${chromePath}: ${err.message}`]));
chrome.on('exit', (code, signal) => {
  if (!finished) finish(2, [`::error::spa_render_check: Chrome exited early (code ${code}, signal ${signal})`]);
});

const browserWsUrl = await new Promise((resolve) => {
  let buffered = '';
  // Keep draining stderr after the endpoint is found so Chrome never blocks
  // on a full pipe.
  chrome.stderr.on('data', (chunk) => {
    if (buffered === null) return;
    buffered += chunk.toString();
    const match = buffered.match(/DevTools listening on (ws:\/\/\S+)/);
    if (match) {
      buffered = null;
      resolve(match[1]);
    }
  });
});

const ws = new WebSocket(browserWsUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener('open', resolve, { once: true });
  ws.addEventListener('error', () => reject(new Error(`could not connect to ${browserWsUrl}`)), { once: true });
}).catch((err) => finish(2, [`::error::spa_render_check: ${err.message}`]));

let nextId = 0;
const pending = new Map();
const listeners = new Set();

ws.addEventListener('message', (event) => {
  const msg = JSON.parse(typeof event.data === 'string' ? event.data : event.data.toString());
  if (msg.id !== undefined && pending.has(msg.id)) {
    const { resolve, reject, method } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(`${method}: ${msg.error.message}`));
    else resolve(msg.result);
    return;
  }
  for (const listener of listeners) listener(msg);
});

function send(method, params = {}, sessionId) {
  const id = ++nextId;
  const message = { id, method, params };
  if (sessionId) message.sessionId = sessionId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, method });
    ws.send(JSON.stringify(message));
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const TIMED_OUT = Symbol('timed out');
const within = (promise, ms) => Promise.race([promise, sleep(ms).then(() => TIMED_OUT)]);

function describeException(details) {
  const text = details.exception?.description ?? details.exception?.value ?? details.text ?? 'unknown exception';
  const firstLines = String(text).split('\n').slice(0, 3).join(' | ');
  const where = details.url ? ` at ${details.url}:${(details.lineNumber ?? 0) + 1}` : '';
  return `${firstLines}${where}`;
}

function describeConsoleArgs(args) {
  const text = (args ?? [])
    .map((arg) => (arg.type === 'string' ? arg.value : (arg.description ?? String(arg.value ?? arg.type))))
    .join(' ');
  return text.split('\n').slice(0, 3).join(' | ').slice(0, 400);
}

// Problem kinds, for the retry decision:
//   transport  the request never produced a page (retryable)
//   blank      #root never got children (retryable only alongside a transport problem)
//   app        the bundle ran and failed (never retryable)
//   deploy     a 4xx: something the deploy should have shipped is missing (never retryable)
const TRANSPORT = 'transport';
const BLANK = 'blank';
const APP = 'app';
const DEPLOY = 'deploy';

function retryable(problems) {
  return problems.some((p) => p.kind === TRANSPORT) && problems.every((p) => p.kind === TRANSPORT || p.kind === BLANK);
}

const ROOT_PROBE = `(() => {
  const root = document.getElementById('root');
  const crash = document.querySelector(${JSON.stringify(CRASH_SELECTOR)});
  return {
    present: Boolean(root),
    children: root ? root.childElementCount : 0,
    text: root ? root.textContent.trim().length : 0,
    crashed: crash ? crash.innerText.split('\\n').map((s) => s.trim()).filter(Boolean).join(' / ').slice(0, 300) : null,
  };
})()`;

async function loadOnce(target) {
  const { browserContextId } = await send('Target.createBrowserContext', { disposeOnDetach: true });
  const { targetId } = await send('Target.createTarget', { url: 'about:blank', browserContextId });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });

  const exceptions = [];
  const renderErrors = [];
  const otherConsoleErrors = [];
  const scriptsInFlight = new Map();
  const scriptFailures = [];
  let documentStatus = null;
  let loadedAt = null;
  let lastScriptActivity = 0;

  const listener = (msg) => {
    if (msg.sessionId !== sessionId) return;
    const p = msg.params;
    switch (msg.method) {
      case 'Runtime.exceptionThrown':
        exceptions.push(describeException(p.exceptionDetails));
        break;
      case 'Runtime.consoleAPICalled':
        if (p.type === 'error') {
          const text = describeConsoleArgs(p.args);
          (RENDER_ERROR_MARKERS.some((marker) => text.includes(marker)) ? renderErrors : otherConsoleErrors).push(text);
        }
        break;
      case 'Page.loadEventFired':
        loadedAt ??= Date.now();
        break;
      case 'Network.requestWillBeSent':
        if (p.type === 'Script') {
          scriptsInFlight.set(p.requestId, p.request.url);
          lastScriptActivity = Date.now();
        }
        break;
      case 'Network.responseReceived':
        if (p.type === 'Document' && documentStatus === null) documentStatus = p.response.status;
        if (p.type === 'Script' && p.response.status >= 400) {
          scriptFailures.push({ url: p.response.url, why: `HTTP ${p.response.status}`, status: p.response.status });
        }
        break;
      case 'Network.loadingFinished':
      case 'Network.loadingFailed':
        if (scriptsInFlight.has(p.requestId)) {
          if (msg.method === 'Network.loadingFailed' && !p.canceled) {
            scriptFailures.push({ url: scriptsInFlight.get(p.requestId), why: p.errorText, status: null });
          }
          scriptsInFlight.delete(p.requestId);
          lastScriptActivity = Date.now();
        }
        break;
      default:
    }
  };
  listeners.add(listener);

  try {
    await send('Runtime.enable', {}, sessionId);
    await send('Page.enable', {}, sessionId);
    await send('Network.enable', {}, sessionId);
    // A fresh context already has an empty cache; this keeps it that way
    // across retries, so a retry never renders a bundle a failed attempt
    // half-fetched.
    await send('Network.setCacheDisabled', { cacheDisabled: true }, sessionId);

    const nav = await within(send('Page.navigate', { url: target }, sessionId), NAV_MS);
    if (nav === TIMED_OUT) {
      return { problems: [{ kind: TRANSPORT, text: `no response to the document request within ${NAV_MS}ms (RENDER_CHECK_NAV_MS)` }] };
    }
    if (nav.errorText) {
      const kind = documentStatus !== null && documentStatus < 500 ? DEPLOY : TRANSPORT;
      const status = documentStatus !== null ? ` (HTTP ${documentStatus})` : '';
      return { problems: [{ kind, text: `navigation failed: ${nav.errorText}${status}` }] };
    }
    const committedAt = Date.now();

    // React mounts after the module graph evaluates, which can be after
    // `load`, and a lazy route chunk (AuthenticatedApp on /app) renders after
    // the first paint. Poll until #root has children, `load` has fired and no
    // script has been in flight for SETTLE_MS, so an error thrown by the
    // first paint or by a lazy chunk still counts. A crash card ends the wait
    // at once. A page whose #root is still empty MOUNT_MS after load is blank;
    // one still empty SETTLE_MS after an uncaught error will not recover.
    let root = { present: false, children: 0, text: 0, crashed: null };
    let renderedAt = null;
    for (;;) {
      const { result } = await send('Runtime.evaluate', { expression: ROOT_PROBE, returnByValue: true }, sessionId);
      root = result.value;
      const now = Date.now();
      const since = loadedAt ?? committedAt;
      if (root.crashed) break;
      if (root.children > 0 && renderedAt === null) renderedAt = now;
      if (renderedAt !== null) {
        const quietSince = Math.max(renderedAt, loadedAt ?? Infinity, lastScriptActivity);
        if (scriptsInFlight.size === 0 && now - quietSince >= SETTLE_MS) break;
        // Scripts that never stop loading: judge what is on screen.
        if (now - renderedAt >= MOUNT_MS) break;
      } else {
        if (now - since >= MOUNT_MS) break;
        // Nothing is coming: the bundle threw, the document is an error
        // page, or a script failed and none is still loading. Waiting the
        // full MOUNT_MS for each would also eat the retries' share of the
        // ceiling.
        const doomed =
          exceptions.length + renderErrors.length > 0 ||
          (documentStatus !== null && documentStatus >= 400) ||
          (scriptFailures.length > 0 && scriptsInFlight.size === 0 && loadedAt !== null);
        if (doomed && now - since >= SETTLE_MS) break;
      }
      await sleep(250);
    }

    const problems = [];
    if (documentStatus !== null && documentStatus >= 500) {
      problems.push({ kind: TRANSPORT, text: `document answered HTTP ${documentStatus}` });
    } else if (documentStatus !== null && documentStatus >= 400) {
      problems.push({ kind: DEPLOY, text: `document answered HTTP ${documentStatus}` });
    }
    for (const exception of exceptions) problems.push({ kind: APP, text: `uncaught exception during startup: ${exception}` });
    for (const logged of renderErrors) problems.push({ kind: APP, text: `React logged a render error: ${logged}` });
    if (root.crashed) problems.push({ kind: APP, text: `the error-boundary fallback is showing: "${root.crashed}"` });
    if (!root.present) problems.push({ kind: BLANK, text: 'no #root element in the page' });
    else if (root.children === 0) problems.push({ kind: BLANK, text: '#root is empty after load (the app never mounted)' });
    // A failed script only matters, and is only reported, when the page is
    // broken: it is then the likeliest cause, and it decides whether a retry
    // can help.
    if (problems.length > 0) {
      for (const failure of scriptFailures) {
        const kind = failure.status !== null && failure.status < 500 ? DEPLOY : TRANSPORT;
        problems.push({ kind, text: `script ${failure.url} failed: ${failure.why}` });
      }
    }
    return { problems, documentStatus, root, exceptions, otherConsoleErrors };
  } finally {
    listeners.delete(listener);
    try {
      await within(send('Target.disposeBrowserContext', { browserContextId }), 5000);
    } catch {
      // disposeOnDetach covers it when Chrome exits
    }
  }
}

const failures = [];
try {
  console.log(`spa_render_check: ${chromePath}; ${targets.length} URL(s), up to ${ATTEMPTS} attempt(s) each`);
  for (const target of targets) {
    for (let attempt = 1; ; attempt++) {
      current = `loading ${target} (attempt ${attempt}/${ATTEMPTS})`;
      console.log(`spa_render_check: ${current}`);
      const outcome = await loadOnce(target);
      for (const logged of (outcome.otherConsoleErrors ?? []).slice(0, 3)) {
        console.log(`spa_render_check: ${target}: console.error without a render-error marker (not fatal on its own): ${logged}`);
      }
      if (outcome.problems.length === 0) {
        const { documentStatus, root } = outcome;
        console.log(
          `spa_render_check: OK ${target} rendered (HTTP ${documentStatus ?? 'n/a'}, #root has ${root.children} child element(s), ` +
            `${root.text} chars of text, 0 uncaught exceptions, 0 render errors, no crash card)`,
        );
        break;
      }
      if (attempt < ATTEMPTS && retryable(outcome.problems)) {
        const summary = outcome.problems.map((p) => p.text).join('; ');
        console.log(
          `::warning::spa_render_check: ${target}: attempt ${attempt}/${ATTEMPTS} failed at the transport level (${summary}); retrying in ${RETRY_DELAY_MS}ms`,
        );
        current = `waiting to retry ${target}`;
        await sleep(RETRY_DELAY_MS);
        continue;
      }
      const note = attempt > 1 ? ` (attempt ${attempt}/${ATTEMPTS})` : '';
      for (const p of outcome.problems) console.log(`::error::spa_render_check: ${target}: ${p.text}${note}`);
      failures.push({ target, count: outcome.problems.length });
      break;
    }
  }
} catch (err) {
  finish(2, [`::error::spa_render_check: the check could not run (${current}): ${err.message}`]);
}

if (failures.length > 0) {
  finish(1, [
    `spa_render_check: FAIL; a visitor loading ${failures.map((f) => f.target).join(' or ')} sees a blank or broken page`,
  ]);
}
finish(0, [`spa_render_check: OK, all ${targets.length} URL(s) rendered`]);
