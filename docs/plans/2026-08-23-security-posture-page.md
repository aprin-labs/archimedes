# Security Posture Page Implementation Plan

> **REQUIRED SUB-SKILL:** Use the executing-plans skill to implement this plan task-by-task.

**Goal:** Add a standalone `/security` page that explains verified trust boundaries, implemented controls, and known limitations without making audit or production-readiness claims.

**Architecture:** Add one static React page behind the existing public router and `PublicLayout`. Reuse current public tokens and components; add no API, dependency, or new state. Keep the landing `#security` summary, but route header/footer links and a new summary link to the complete page.

**Tech Stack:** React 19, existing hand-rolled router, CSS, Node test runner, Playwright CLI.

---

## Approved design

Page sections:

1. Status header: research prototype, Arc public testnet, no real funds.
2. Authority model: Better Auth account identity, proof-linked wallet, bounded agent, vault owner.
3. Verified controls ledger: production session cookies, five-minute single-use wallet proof, user-scoped data, internal agent-key writes, same-origin/CSP/security-header/rate-limit protections, and contract role separation.
4. Known limits: testnet only, agent mis-rebalance risk, mock oracle/risk inputs, immutable-contract migration requirement, and no audit, production-readiness, regulatory, security, or return guarantee.
5. Evidence links: architecture, auth model, non-custodial ADR, contract source, and repository.

Claim sources:

- `docs/security/auth-model.md`
- `docs/adr/non-custodial-vault-owner-agent.md`
- `docs/anti-features.md`
- `nginx/nginx.conf`
- `contracts/src/Vault.sol`

## Task 1: Add public route contract

**Files:**

- Modify: `ui/test/routes.test.js`
- Modify: `ui/src/routes.js`

**Step 1: Write failing route assertions**

Extend the public-route test:

```js
test("landing, security, and architecture remain public", () => {
  assert.equal(resolveRoute("/").page, "landing");
  assert.equal(resolveRoute("/security").page, "security");
  assert.equal(resolveRoute("/architecture").page, "architecture");
  assert.equal(pageToPath("security"), "/security");
});
```

**Step 2: Verify RED**

Run: `cd ui && node --test test/routes.test.js`

Expected: FAIL because `/security` resolves to `not-found`.

**Step 3: Implement minimum route**

Add `"/security": "security"` to `PUBLIC_PATHS` and return `/security` from `pageToPath("security")`.

**Step 4: Verify GREEN**

Run: `cd ui && node --test test/routes.test.js`

Expected: route tests pass.

## Task 2: Define truthful page contract

**Files:**

- Create: `ui/src/components/Security.jsx`
- Modify: `ui/test/public-visuals.test.js`

**Step 1: Write failing content assertions**

Read `Security.jsx` in the existing source-contract test, then assert:

```js
test("security page separates verified controls from known limits", () => {
  assert.match(security, /Security is enforced boundaries, not a guarantee/i);
  assert.match(security, /Better Auth/);
  assert.match(security, /five-minute/i);
  assert.match(security, /Agent may mis-rebalance/i);
  assert.match(security, /cannot withdraw/i);
  assert.match(security, /No independent security audit/i);
  assert.match(security, /Arc public testnet/i);
});
```

**Step 2: Verify RED**

Run: `cd ui && node --test test/public-visuals.test.js`

Expected: FAIL because `Security.jsx` does not exist.

**Step 3: Create minimum semantic page**

Create one `<main className="security-page">` with one `h1`, semantic sections, lists or description lists, and normal links. Use only claims listed in approved design. No dynamic data, animation, form, disclosure workflow, or security score.

**Step 4: Verify GREEN**

Run: `cd ui && node --test test/public-visuals.test.js`

Expected: content contract passes.

## Task 3: Wire page, metadata, navigation, and styles

**Files:**

- Modify: `ui/src/App.jsx`
- Modify: `ui/src/components/PublicLayout.jsx`
- Modify: `ui/src/components/Landing.jsx`
- Modify: `ui/src/App.css`
- Modify: `ui/public/sitemap.xml`
- Modify: `ui/test/public-visuals.test.js`

**Step 1: Write failing integration assertions**

Assert that:

- `App.jsx` imports and renders `Security` for the `security` page.
- title is `Security · Archimedes`.
- canonical path includes `/security`.
- public header uses `href="/security"`.
- sitemap includes `https://archimedes-arc.com/security`.
- landing security summary links to `/security`.

**Step 2: Verify RED**

Run: `cd ui && npm test`

Expected: FAIL on missing security wiring.

**Step 3: Implement minimum integration**

Use a small page-component map or explicit public-page branches in `App.jsx`; do not add a routing library. Extend canonical selection to `/`, `/security`, and `/architecture`. Reuse public layout widths, colors, typography, focus treatment, and 44px link targets. Add page-specific CSS only where shared public styles do not cover the design.

**Step 4: Verify GREEN**

Run: `cd ui && npm test`

Expected: all tests pass.

## Task 4: Browser and source verification

**Files:** No new files.

**Step 1: Run static gates**

```bash
cd ui
npm test
npm run lint
npm run build
```

Expected: all tests pass, ESLint clean, Vite build succeeds.

**Step 2: Verify browser behavior**

Serve the production build and check `/security` at 1440×900 and 390×844:

- one `main` and one `h1`
- canonical ends in `/security`
- no duplicate IDs
- no horizontal overflow
- theme toggle remains 44×44 and functional
- header/footer Security links resolve to `/security`
- no console errors

**Step 3: Inspect final diff**

Run: `git diff --check` and confirm only planned source, test, sitemap, CSS, and documentation paths changed. Preserve existing formatter-only changes in `ui/src/App.jsx` and `ui/test/public-visuals.test.js` when staging.
