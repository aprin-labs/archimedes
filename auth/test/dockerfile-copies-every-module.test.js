// Every local module the auth sidecar imports must be COPIED into its image.
//
// 2026-09-04: #1790 added delivery-log.js, suppression.js and
// verification-status.js and server.js imported them, but auth/Dockerfile
// still copied an explicit list (auth.js server.js mailer.js). The unit tests
// run from source and passed; only the deploy build starts the image, where
// node exited at boot with ERR_MODULE_NOT_FOUND and nginx answered 502 for
// /api/auth/*. The eleven-PR deploy bf76999c failed on that step. This test
// resolves every `./x.js` import in auth/*.js and checks the Dockerfile's
// COPY patterns cover it, so the gap is red on the PR, not on main.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const authDir = join(here, '..')

function copiedPatterns(dockerfile) {
  const out = []
  for (const raw of dockerfile.split('\n')) {
    const line = raw.trim()
    if (!/^(COPY|ADD)\b/.test(line)) continue
    const parts = line.replace(/^(COPY|ADD)\s+/, '').split(/\s+/).filter((p) => !p.startsWith('--'))
    out.push(...parts.slice(0, -1)) // last token is the destination
  }
  return out
}

function globToRegExp(glob) {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '[^/]*').replace(/\?/g, '[^/]')
  return new RegExp('^' + esc + '$')
}

export function importedLocalModules(dir = authDir) {
  const mods = new Set()
  for (const f of readdirSync(dir)) {
    if (!f.endsWith('.js')) continue
    const src = readFileSync(join(dir, f), 'utf8')
    for (const m of src.matchAll(/from\s+['"]\.\/([A-Za-z0-9_./-]+)['"]/g)) mods.add(m[1])
  }
  return [...mods].sort()
}

export function uncoveredModules(dockerfile, mods) {
  const regexes = copiedPatterns(dockerfile).map(globToRegExp)
  return mods.filter((m) => !regexes.some((r) => r.test(m)))
}

test('auth/Dockerfile copies every local module the sidecar imports', () => {
  const dockerfile = readFileSync(join(authDir, 'Dockerfile'), 'utf8')
  const mods = importedLocalModules()
  assert.ok(mods.length >= 3, `expected the sidecar to import local modules, found ${mods.length}`)
  const missing = uncoveredModules(dockerfile, mods)
  assert.deepEqual(
    missing,
    [],
    `auth/Dockerfile does not COPY ${missing.join(', ')} — the image exits at boot with ERR_MODULE_NOT_FOUND (deploy bf76999c, 2026-09-04)`,
  )
})

test('the check is not vacuous: an explicit COPY list that omits a module is red', () => {
  const stale = 'FROM node\nCOPY package.json ./\nCOPY auth.js server.js mailer.js ./\n'
  const missing = uncoveredModules(stale, ['auth.js', 'delivery-log.js', 'mailer.js', 'server.js'])
  assert.deepEqual(missing, ['delivery-log.js'])
})
