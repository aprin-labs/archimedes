# Calm Precision Rebrand Implementation Plan

> **REQUIRED SUB-SKILL:** Use the executing-plans skill to implement this plan task-by-task.

**Goal:** Rebuild public landing, authentication, and every existing application route around one original calm-precision design system while preserving business logic, API contracts, data models, authentication, and wallet behavior.

**Architecture:** Keep React 19, Vite 8, UnoCSS, and plain CSS. Replace visual tokens at shared shell level, then make minimal structural changes where semantics or legacy route styling block consistency. Reuse real product UI and existing API evidence. Add no dependencies and no backend changes.

**Tech Stack:** React 19, Vite 8, UnoCSS, plain CSS, Node test runner, Playwright CLI.

---

## Audit

### Product

- **Primary customer:** capable non-expert with idle USDC who wants research-backed portfolio automation without black-box custody.
- **Job to be done:** describe portfolio intent, evaluate one generated strategy against named research and selection-bias controls, authorize a non-custodial testnet vault, then inspect decisions.
- **Value proposition:** research-grounded generation plus visible rejection criteria plus user-owned vault authority.
- **Conversion goal:** authenticated strategy generation. Canonical CTA: **Generate a strategy**.
- **Primary workflow:** Landing -> account -> Generate -> Strategy Passport -> vault authorization -> Portfolio -> Reasoning.
- **Verified limitations:** Arc is public testnet; no real-funds promise; most generated strategies can fail; knowledge-graph artifact pending; marketplace settlement can run dry; Learnings remains roadmap preview; no account-recovery UI or valid public pricing exists.

### Current interface

- Public site is polished but uses a dark/gold editorial identity, large serif type, and Archimedean spiral. This conflicts with requested warm/cobalt calm-precision direction and creates literal name-based branding.
- Landing explains rigor and custody but lacks complete problem, workflow, use-case, security, FAQ, footer, canonical, and social-preview structure.
- Auth and recent core app routes are strong. Marketplace, subscriptions, account, ops, and several modal surfaces still use legacy colors, inline styling, and mixed token names.
- Navigation exposes 15+ destinations without clear daily-task prioritization.
- Existing honest loading, empty, failed-gate, unavailable-data, wallet-gated, and testnet states are valuable and must stay.
- Current sitemap predates `/app` boundary and lists protected legacy paths.

### Baseline evidence

- UI tests: 23 passed, 0 failed.
- ESLint: passed.
- Production build: passed with existing chunks over 500 kB warning.
- Playwright: landing inspected at 1440x900, 1024x768, 768x1024, and 390x844; no horizontal overflow.
- Auth, architecture, and all 14 top-level app routes inspected using Playwright-only account fixture. No production account or data mutated.
- Console 502 errors are expected while Vite runs without local backend; full stack build continues separately.

## Task 1: Brand contract and visual tests

**Files:**

- Create: `DESIGN.md`
- Create: `docs/plans/2026-08-22-calm-precision-rebrand.md`
- Modify: `ui/test/public-visuals.test.js`
- Modify: `ui/test/app-visuals.test.js`

1. Record positioning, tokens, type, spacing, density, component states, motion, responsive, accessibility, voice, and anti-patterns.
2. Update visual-contract tests for warm canvas, cobalt action, verdigris verification, proof-frame mark, complete landing sections, app skip link, system theme default, and semantic navigation.
3. Run `cd ui && npm test`; verify tests fail for missing implementation.

## Task 2: Shared foundation and identity

**Files:**

- Create: `ui/src/components/BrandMark.jsx`
- Modify: `ui/src/App.css`
- Modify: `ui/src/theme.js`
- Modify: `ui/src/components/PublicLayout.jsx`
- Modify: `ui/src/components/Layout.jsx`
- Modify: `ui/src/components/Breadcrumbs.jsx`
- Modify: `ui/public/favicon.svg`
- Modify: `ui/public/logo.svg`

1. Replace remote-font and gold/serif foundations with local system type and semantic design tokens.
2. Support warm light and precise dark themes; default to system preference when no saved choice exists.
3. Add reusable proof-frame mark and wordmark.
4. Add app skip link, responsive public navigation, semantic anchor navigation, and unified focus treatment.
5. Preserve router and feature-flag behavior.

## Task 3: Complete landing and metadata

**Files:**

- Modify: `ui/src/components/Landing.jsx`
- Modify: `ui/index.html`
- Modify: `ui/public/robots.txt`
- Modify: `ui/public/sitemap.xml`
- Create during browser QA: `ui/public/product-workspace.png`
- Create during browser QA: `ui/public/og-image.png`

1. Build announcement, hero, verified evidence, problem, authentic product view, capabilities, workflow, use cases, integrations, security, FAQ, final CTA, and destination-backed footer.
2. Keep all claims source-backed. Skip pricing, testimonials, customer logos, and certifications because repository contains no valid evidence.
3. Use one CTA label: `Generate a strategy`.
4. Add canonical, Open Graph, Twitter card, current JSON-LD, theme color, and real social image.
5. Restrict sitemap to public canonical routes.

## Task 4: Auth and application cohesion

**Files:**

- Modify: `ui/src/components/AuthPage.jsx`
- Modify: `ui/src/App.jsx`
- Modify targeted legacy route components only where shared styles cannot fix semantics.

1. Recompose auth into branded, responsive account/wallet-boundary layout without adding unsupported recovery.
2. Unify headings, panels, forms, tables, badges, dialogs, wallet gates, and empty/error states through shared tokens.
3. Keep Generate, Passport, Portfolio, Library, Explore, Corpus, Reasoning, Quant Lab, Marketplace, Publish, Subscriptions, Insights, Account, Learnings, Architecture, Leaderboard, Vault, and marketplace detail routes intact.
4. Add missing document titles for marketplace surfaces.

## Task 5: Accessibility and interaction remediation

**Files:**

- Modify: `ui/src/components/Generate.jsx`
- Modify: `ui/src/components/MarketplacePage.jsx`
- Modify: `ui/src/components/RegimePanel.jsx`
- Modify: `ui/src/components/VaultChat.jsx`
- Modify modal/tour code only where verified defects remain.
- Modify: `ui/src/App.css`

1. Replace clickable spans/divs used as controls with buttons or anchors.
2. Replace `transition: all` with explicit properties.
3. Preserve focus replacements where `outline: none` exists.
4. Add live announcements and control labels where missing.
5. Verify keyboard navigation, dialogs, destructive confirmation, reduced motion, touch targets, and no color-only status.

## Task 6: Verification and visual iteration

1. Run LSP diagnostics before build.
2. Run `cd ui && npm test`, `npm run lint`, and `npm run build`.
3. Run `git diff --check`.
4. Use Playwright at 1440x900, 1024x768, 768x1024, and 390x844.
5. Capture public, auth, Generate, Library, Portfolio gate, Reasoning, Account, and representative dense-data route screenshots.
6. Test links, forms, validation, disclosure, mobile drawer, theme, keyboard sequence, reduced motion, overflow, asset loading, and console errors.
7. Re-run `lens_diagnostics mode=all` and report exact residuals.

## Anti-goals

- No API, database, auth, wallet, contract, infra, or deployment changes.
- No new dependency, framework migration, fabricated fixture in production code, pricing, testimonial, customer logo, certification, or unsupported account recovery.
- No edits to pre-existing unrelated infra/docs/report work.
- No push, deploy, production mutation, or secret access.
