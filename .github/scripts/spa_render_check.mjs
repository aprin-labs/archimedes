#!/usr/bin/env node
// Render the SPA in a real headless Chrome and fail if it comes up blank.
//
// Usage: node .github/scripts/spa_render_check.mjs <url>
//
// Why this exists: from 2026-09-25 to 2026-10-01 https://archimedes-arc.com
// served a blank page. Dependabot PR #1872 bumped `react` to 19.3.0 without
// `react-dom` (19.2.8); react-dom refuses to start against a different react
// version and throws React error #527 at module load, so nothing ever mounted
// into #root. Every check the deploy ran stayed green: curl got a 200 with a
// valid index.html, /health answered, the rollout completed. A static fetch
// cannot see this class at all. Only executing the bundle can.
//
// What it checks, in a fresh profile:
//   1. the document request answers (no navigation error, HTTP status < 400)
//   2. no uncaught exception during startup (CDP Runtime.exceptionThrown,
//      which also reports unhandled promise rejections)
//   3. #root exists and has rendered children once the page has loaded
// Any failure prints a `::error::` line (a GitHub Actions annotation; plain
// text elsewhere) and exits 1. Exit 2 means the check itself could not run
// (no Chrome, Node without a global WebSocket, bad arguments).
//
// No npm dependencies on purpose: it speaks the Chrome DevTools Protocol over
// Node's built-in WebSocket (Node >= 22), so it runs from a bare checkout with
// the runner's preinstalled Google Chrome.
//
// Environment (all optional):
//   CHROME_PATH                 Chrome/Chromium binary (default: probe the usual names)
//   RENDER_CHECK_MAX_SECONDS    hard wall-clock ceiling for the whole check (default 60)
//   RENDER_CHECK_MOUNT_MS       how long after `load` #root may stay empty before
//                               the page counts as blank (default 15000)
//   RENDER_CHECK_SETTLE_MS      how long to keep listening for startup
//                               exceptions after #root first has children (default 2000)

import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';

const MAX_SECONDS = Number(process.env.RENDER_CHECK_MAX_SECONDS ?? 60);
const MOUNT_MS = Number(process.env.RENDER_CHECK_MOUNT_MS ?? 15000);
const SETTLE_MS = Number(process.env.RENDER_CHECK_SETTLE_MS ?? 2000);

function usage(message) {
  console.log(`::error::spa_render_check: ${message}`);
  console.log('usage: node .github/scripts/spa_render_check.mjs <url>');
  process.exit(2);
}

const target = process.argv[2];
if (!target || process.argv.length > 3) usage('exactly one argument (the URL to load) is required');
try {
  const parsed = new URL(target);
  if (!['http:', 'https:'].includes(parsed.protocol)) usage(`not an http(s) URL: ${target}`);
} catch {
  usage(`not a URL: ${target}`);
}
if (!Number.isFinite(MAX_SECONDS) || MAX_SECONDS <= 0) usage('RENDER_CHECK_MAX_SECONDS must be a positive number');
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

setTimeout(() => {
  finish(1, [`::error::spa_render_check: ${target} did not finish rendering within ${MAX_SECONDS}s (RENDER_CHECK_MAX_SECONDS)`]);
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
const listeners = [];

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

function describeException(details) {
  const text = details.exception?.description ?? details.exception?.value ?? details.text ?? 'unknown exception';
  const firstLines = String(text).split('\n').slice(0, 3).join(' | ');
  const where = details.url ? ` at ${details.url}:${(details.lineNumber ?? 0) + 1}` : '';
  return `${firstLines}${where}`;
}

try {
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });

  const exceptions = [];
  let documentStatus = null;
  let loaded = false;
  listeners.push((msg) => {
    if (msg.sessionId !== sessionId) return;
    if (msg.method === 'Runtime.exceptionThrown') exceptions.push(describeException(msg.params.exceptionDetails));
    if (msg.method === 'Page.loadEventFired') loaded = true;
    if (msg.method === 'Network.responseReceived' && msg.params.type === 'Document' && documentStatus === null) {
      documentStatus = msg.params.response.status;
    }
  });

  await send('Runtime.enable', {}, sessionId);
  await send('Page.enable', {}, sessionId);
  await send('Network.enable', {}, sessionId);
  // A fresh profile already has an empty cache; this also keeps a reused
  // profile from rendering a stale bundle.
  await send('Network.setCacheDisabled', { cacheDisabled: true }, sessionId);

  console.log(`spa_render_check: loading ${target} in ${chromePath}`);
  const nav = await send('Page.navigate', { url: target }, sessionId);
  if (nav.errorText) {
    finish(1, [`::error::spa_render_check: navigation to ${target} failed: ${nav.errorText}`]);
  }

  while (!loaded) await sleep(100);
  const loadedAt = Date.now();

  // React mounts after the module graph evaluates, which can be after `load`.
  // Poll #root until it has children, then keep listening for SETTLE_MS so an
  // exception thrown right after the first paint still counts. A page whose
  // #root is still empty MOUNT_MS after load is blank; one that is still
  // empty SETTLE_MS after an uncaught exception is not going to recover.
  const rootProbe = `(() => {
    const root = document.getElementById('root');
    if (!root) return { present: false, children: 0, text: 0 };
    return { present: true, children: root.childElementCount, text: root.textContent.trim().length };
  })()`;
  let root = { present: false, children: 0, text: 0 };
  let renderedAt = null;
  for (;;) {
    const { result } = await send('Runtime.evaluate', { expression: rootProbe, returnByValue: true }, sessionId);
    root = result.value;
    const now = Date.now();
    if (root.children > 0 && renderedAt === null) renderedAt = now;
    if (renderedAt !== null && now - renderedAt >= SETTLE_MS) break;
    if (renderedAt === null && now - loadedAt >= MOUNT_MS) break;
    if (renderedAt === null && exceptions.length > 0 && now - loadedAt >= SETTLE_MS) break;
    await sleep(250);
  }

  const problems = [];
  if (documentStatus !== null && documentStatus >= 400) problems.push(`document answered HTTP ${documentStatus}`);
  for (const exception of exceptions) problems.push(`uncaught exception during startup: ${exception}`);
  if (!root.present) problems.push('no #root element in the page');
  else if (root.children === 0) problems.push('#root is empty after load (the app never mounted)');

  if (problems.length > 0) {
    finish(1, [
      ...problems.map((p) => `::error::spa_render_check: ${target}: ${p}`),
      `spa_render_check: FAIL (${problems.length} problem(s)); a user loading ${target} sees a blank or broken page`,
    ]);
  }
  finish(0, [
    `spa_render_check: OK ${target} rendered (HTTP ${documentStatus ?? 'n/a'}, #root has ${root.children} child element(s), ${root.text} chars of text, 0 uncaught exceptions)`,
  ]);
} catch (err) {
  finish(2, [`::error::spa_render_check: the check could not run against ${target}: ${err.message}`]);
}
