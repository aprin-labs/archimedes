import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// Library page: rigor-strictness card removed, progressive load, skeleton (#1645).
//
// Same shape as quant-lab.test.js / a11y.test.js: readFileSync + regex pins on
// the source, no DOM renderer (ui/package.json runs plain `node --test`). That
// makes these structural rather than behavioural assertions, which is the
// idiom this suite has — each one is anchored on the specific construct whose
// absence WAS the defect, and every assertion below was confirmed to FAIL
// against the pre-fix tree (transcript in the PR body).

const strategies = readFileSync(new URL('../src/components/Strategies.jsx', import.meta.url), 'utf8')
const passport = readFileSync(new URL('../src/components/StrategyPassport.jsx', import.meta.url), 'utf8')
const appCss = readFileSync(new URL('../src/App.css', import.meta.url), 'utf8')

// ── 1. The strictness card is gone from Library ──────────────────────────
//
// The issue's own acceptance grep. It is stated as "no output", so the page
// must not even import the control's module — `levelLabel` moved to
// src/rigorLevels.js precisely so this page carries no reference to it.
test('Library does not mount, import, or otherwise reference the rigor-strictness control', () => {
  assert.equal(
    strategies.includes('RigorStrictnessControl'),
    false,
    'Strategies.jsx must contain no reference to RigorStrictnessControl',
  )
  assert.equal(
    /className="mb-5"/.test(strategies),
    false,
    'the mb-5 wrapper the control was mounted in must be gone',
  )
})

// ── 2. …but the level is still SETTABLE somewhere (issue anti-goal) ──────
//
// "Do NOT remove the user's ability to set their own rigor strictness level
// entirely from the product." Removal from Library is only acceptable because
// the Passport still mounts the same control and useRigorStrictness keeps the
// two in sync. If someone later removes it there too, this fails.
test('the strictness control is still mounted on the Strategy Passport', () => {
  assert.match(passport, /import\s+RigorStrictnessControl[\s\S]{0,80}from\s+["']\.\/RigorStrictnessControl["']/)
  assert.match(passport, /<RigorStrictnessControl\s+level=\{level\}\s+onChange=\{setLevel\}\s*\/>/)
})

// Library still READS the level — the chips are annotated at the user's
// strictness. Removing the card must not have silently pinned the rows to a
// constant.
test('Library still annotates rows at the user-selected level', () => {
  assert.match(strategies, /const \[level\] = useRigorStrictness\(\)/)
  assert.match(strategies, /<DeployabilityChip[^>]*level=\{level\}/)
})

// ── 3. A loading state renders before data resolves ──────────────────────
test('a skeleton renders while the strategy lists are in flight, not a bare caption', () => {
  assert.match(strategies, /function StrategyListSkeleton\(/)
  assert.match(strategies, /\{loading \? \(\s*<StrategyListSkeleton \/>/)
  // Every former "Loading…" caption is replaced, not just the first one.
  assert.equal(
    (strategies.match(/<StrategyListSkeleton \/>/g) || []).length,
    3,
    'all three loading branches (generated / examples / published) render the skeleton',
  )
  assert.equal(
    strategies.includes('<div className="caption mb-4">Loading…</div>'),
    false,
    'the bare Loading… caption must be gone',
  )
  // The skeleton is decorative; its accessible name is a single live region,
  // not six rows of unlabelled boxes.
  assert.match(strategies, /aria-hidden="true"/)
  assert.match(strategies, /role="status"[\s\S]{0,40}aria-live="polite"/)
  // The styles it needs actually exist.
  assert.match(appCss, /\.lib-skeleton-row\s*\{/)
  assert.match(appCss, /@keyframes lib-skeleton-pulse/)
  // App.css is gradient-free by design (ui/test/public-visuals.test.js) — the
  // skeleton must not be the exception that quietly reintroduces one.
  assert.equal(/gradient\(/i.test(appCss), false)
})

// ── 4. The SLOW call does not hold the rows hostage ──────────────────────
//
// This is the load-speed half of the issue. `/api/selection-bias/gate`
// recomputes the whole cohort rigor gate (rigor_cache.py's docstring measures
// it at ~8-10s, and it was returning ALB 504s in prod on 2026-08-31). It used
// to sit inside the same awaited Promise.allSettled as the two calls that
// produce rows, so the page painted nothing until it finished.
test('the skeleton comes down without waiting for the deployability gate', () => {
  const awaited = strategies.match(/await Promise\.all\(\[([^\]]*)\]\)/)
  assert.ok(awaited, 'expected an awaited Promise.all gating setLoading(false)')
  const members = awaited[1]
  assert.match(members, /applySeed/)
  assert.match(members, /applyGen\b/)
  assert.equal(
    /gate/i.test(members),
    false,
    `the gate call must not be awaited before the rows paint; awaited: ${members.trim()}`,
  )
  // The gate still runs — under VITE_ROADMAP_SURFACES=true — still lands, and
  // still reports its own errors: this is a reordering, not a removal. The
  // flag prefix is part of the pattern on purpose: the call now sits inside a
  // `ROADMAP_SURFACES_ENABLED ? ... : Promise.resolve(...)` ternary, so a bare
  // substring match is satisfied by a default build that never makes the
  // request (0 hits for `selection-bias/gate` in the built chunk).
  assert.match(
    strategies,
    /ROADMAP_SURFACES_ENABLED \? apiGet\('\/api\/selection-bias\/gate'\)/,
  )
  assert.match(strategies, /setGateError\(/)
  // Its in-flight flag is cleared on BOTH outcomes: a rejected gate must reach
  // the error banner, never leave every chip on a permanent "checking…". The
  // `.catch` also stops a now-detached promise becoming an unhandled rejection.
  assert.match(strategies, /applyGate\s*\n?\s*\.catch\(/)
  assert.match(strategies, /\.finally\(\(\) => \{\s*\n\s*if \(current\(\)\) setGateLoading\(false\)/)
  // And the rows' own skeleton is released unconditionally — a throw in any of
  // the three row handlers must not leave the skeleton up forever.
  assert.match(strategies, /\} finally \{\s*\n\s*if \(current\(\)\) setLoading\(false\)/)
})

// A late response from a superseded load() must not overwrite a newer one —
// load() is also the Retry handler, so two runs can genuinely overlap now.
test('a superseded load() run discards its own results', () => {
  assert.match(strategies, /const runId = \+\+runIdRef\.current/)
  assert.match(strategies, /const current = \(\) => runIdRef\.current === runId/)
  // Every branch that writes state checks it first.
  assert.ok(
    (strategies.match(/if \(!current\(\)\) return/g) || []).length >= 4,
    'each of the four apply* handlers must bail if it has been superseded',
  )
})

// ── 5. Progressive rendering must not render a silence as a verdict ──────
//
// Rows now paint before the gate answers, so for a few seconds there is no
// deployability entry for any row. An absent chip is indistinguishable from
// "the gate had nothing to say", which is a claim we have not earned yet —
// CLAUDE.md's fail-soft rule. The in-flight window gets its own honest state.
test('a row with no gate answer yet says so instead of rendering nothing', () => {
  assert.match(strategies, /function DeployabilityChip\(\{ deploy, level, gatePending \}\)/)
  const guard = strategies.match(/if \(!deploy\) \{[\s\S]*?\n {2}\}/)
  assert.ok(guard, 'expected the !deploy branch to be a block, not a bare `return null`')
  assert.match(guard[0], /if \(!gatePending\) return null/)
  assert.match(guard[0], /checking/)
  assert.match(guard[0], /not a verdict/)
  // It is only claimed while the gate is genuinely in flight AND healthy — a
  // failed gate shows the existing error banner, not a permanent "checking…".
  assert.match(strategies, /gatePending=\{gateLoading && !gateError\}/)
})

// …and only for rows that call can actually answer for.
// `/api/selection-bias/gate` iterates the curated provider library
// (selection_bias_routes.evaluate_rigor_gate -> _provider().list_strategies()),
// so a GENERATED row has no deployMap entry before or after it lands. Claiming
// "still loading the gate for this row" there would be false, and the chip
// would flicker in and back out on the default tab. Caught on an adversarial
// re-read of this PR's own first cut, which wired it to all three tables.
test('only the curated Examples table claims a pending gate', () => {
  assert.equal(
    (strategies.match(/gatePending=\{gateLoading && !gateError\}/g) || []).length,
    1,
    'exactly one StrategyTable — the curated Examples one — may claim a pending gate',
  )
  // Anchored to that table specifically: the prop must sit between the
  // `strategies={examples}` this table renders and its own empty state.
  const examplesTable = strategies.match(
    /strategies=\{examples\}[\s\S]*?emptyState=\{<p className="caption">No example strategies loaded\.<\/p>\}/,
  )
  assert.ok(examplesTable, 'expected the Examples StrategyTable to still be there')
  assert.match(examplesTable[0], /gatePending=\{gateLoading && !gateError\}/)
})
