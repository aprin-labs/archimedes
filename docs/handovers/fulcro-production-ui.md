# Fulcro production UI handoff

> **status:** reference
> **owner:** Dan Browne
> **updated:** 2026-09-09
> **superseded-by:** —

## Start here

Carry the Fulcro preview into the existing production application, preserving real
workflows, backend contracts and truthful data. This handoff records the visual direction
and integration constraints. The identity/appearance foundation and first public-page
slice are implemented locally; **full screen migration and deployment have not happened**.
The approved branch scope also includes the existing app presentation work, independent
`ui/concept/` preview, dependencies/lockfile, tests and this handoff.

Latest user clarification: **one click on the compact sun/moon icon switches directly
between Light and Dark. With no saved choice, default to System.** No dropdown or labeled
“Appearance” field. Keep the 44px target, Fulcro colors, supplied logo and DM Sans/Inter.
This supersedes the dropdown and production-dark-default requirements in earlier versions;
the unchanged preview sources still show the former dropdown interaction.

Read [CLAUDE.md](../../CLAUDE.md) and inspect current Git status/diff before implementation.
The source of this handoff was the dirty `daniel/visual-concepts` worktree, not a clean
commit. `ui/concept/` and `ui/test/observatory.test.js` were initially untracked; production
and package files already contained other UI work. That work is now part of the explicitly
approved commit scope. An earlier index lock was left untouched. The preview source,
not this document alone, is required to reproduce the design.

## Authoritative reference

| Concern | Read / reuse |
| --- | --- |
| Exact light/dark roles, fonts | [`ui/concept/public/theme.css`](../../ui/concept/public/theme.css) |
| Layout, type scale, responsive rules | [`ui/concept/styles.css`](../../ui/concept/styles.css) |
| Supplied vector and symbol | `ui/concept/public/brand/archimedes-fulcro.svg`, `fulcro-symbol.svg` |
| Local fonts and OFL notices | `ui/concept/public/fonts/dm-sans-latin.woff2`, `inter-latin.woff2`, `OFL-DM-Sans.txt`, `OFL-Inter.txt` |
| Branding and icon rendering | [`UI.tsx`](../../ui/concept/UI.tsx) |
| Appearance lifecycle and compact trigger | [`App.tsx`](../../ui/concept/App.tsx), [`index.html`](../../ui/concept/index.html) |
| Composer and investigation presentation | [`Workflows.tsx`](../../ui/concept/Workflows.tsx) |
| Study, evidence, library and comparison | [`Research.tsx`](../../ui/concept/Research.tsx) |
| Custom analytical interactions | [`Chart.tsx`](../../ui/concept/Chart.tsx), [`RiskMap.tsx`](../../ui/concept/RiskMap.tsx) |
| Component adaptations and licenses | [`component-usage.json`](../../ui/concept/public/component-usage.json), [`THIRD_PARTY.txt`](../../ui/concept/public/THIRD_PARTY.txt) |
| Executable appearance contracts | [`identity-check.js`](../../ui/concept/identity-check.js) |

The full horizontal SVG is the supplied artwork, unchanged:
`8fb9d89427563551c13544c135c3402973768c11403f487f9daa22ffb797f9b4` (SHA-256).
The narrow symbol/favicon reuses its original two paths. Preserve geometry and proportions;
use Forest on light and Ivory on dark. DM Sans Medium headings, original outlined
SemiBold wordmark, Inter Regular/Medium interface and tabular numbers. Exact roles live
in `theme.css`; stronger control boundaries supplement its subtle divider colors.

Figma returned HTTP 403. The supplied SVG and written Fulcro specification were used;
inaccessible Figma screen contents were not inspected. The earlier graphite and
warm-neutral palette experiments are superseded, not alternate production targets.

Local reference: `http://127.0.0.1:5181/?v=fulcro#/` when the preview server is running.
Evidence directory: `/tmp/archimedes-fulcro/`. `screenshots.md` indexes 13 light/dark pairs;
`screenshots.json` records state and viewport. Application PNGs were refreshed after the
icon-switcher change. `switcher-result.md` holds its fresh checks; `result.md` and review
files retain the preceding full identity review. These `/tmp` artifacts are local and
volatile, not published or guaranteed to exist in another checkout. Preserve source and
needed evidence before handing work to another machine; never inspect historical agent
sessions to reconstruct it.

## Integration sequence

### 1. Keep production ownership and data flow

Use production [`App.jsx`](../../ui/src/App.jsx),
[`AuthenticatedApp.jsx`](../../ui/src/AuthenticatedApp.jsx) and
[`routes.js`](../../ui/src/routes.js) as the routing/authentication backbone. Port
presentation into the existing shells and screens, not the preview's hash router.

- Shells: [`PublicLayout.jsx`](../../ui/src/components/PublicLayout.jsx),
  [`Layout.jsx`](../../ui/src/components/Layout.jsx),
  [`BrandMark.jsx`](../../ui/src/components/BrandMark.jsx), auth/error/404 surfaces.
- Composer/progress: existing `Generate.jsx`, `GenerationStatus.jsx`,
  `GenerationStream.jsx`; retain real requests, validation, cancellation, retry,
  idempotency, authentication and error handling.
- Research: existing `Explore.jsx`, `Strategies.jsx`, `StrategyDetailPage.jsx`,
  `StrategyPassport.jsx`, `CorpusExplorer.jsx` and their current data paths. Match the
  relevant preview presentation only where the production contract supports it.

**Done when:** every migrated screen has an explicit production route/data owner and
retains existing account, consent, wallet and feature-gate behavior. Real rigor verdicts,
admin visibility and roadmap gates stay backed by server truth.

### 2. Port identity without importing a second application

Production uses **UnoCSS** in [`ui/vite.config.js`](../../ui/vite.config.js); the preview
uses a separate Tailwind 4 build. Reuse semantic CSS and installed components where useful.
Do not copy preview resets or its Vite configuration over production, or add a second
utility build as an incidental identity change. Review package changes independently;
the dirty lockfile is not a ready-made integration patch.

[`ui/src/App.css`](../../ui/src/App.css) has legacy variables plus later semantic and
shell layers. Reconcile effective root, `.app-site` and `.public-site` values rather than
appending another palette. Include body-level portals, authentication and error states.
Keep the supplied elevation, focus, selected and disabled roles; neutral controls should
not become additional lime primary actions.

Carry only needed vector/font assets and their notices into `ui/public`. Update the real
[`ui/index.html`](../../ui/index.html), font preloads, favicon fallbacks and web manifest;
the existing Gabarito and older brand metadata are not Fulcro. Check supplied symbol at
small sizes and readable navigation wordmark at desktop/mobile widths. Keep CSP intact.

**Done when:** both themes match the reference on every migrated surface, including
portals; exact assets/fonts load locally; no production routing or backend changes are
needed for the identity layer.

### 3. Reconcile appearance with production consent

Preview appearance code is a reference, **not a drop-in production storage policy**.
Production [`theme.js`](../../ui/src/theme.js) uses `archimedes.theme` and
[`storage-consent.js`](../../ui/src/storage-consent.js) gates writes as functional storage.
The preview key `archimedes.observatory.appearance` is isolated and writes directly.
Preserve production consent and revocation behavior; never copy that bypass into production.

Read `ui/test/theme.test.js` and all theme callers first. The user has explicitly approved
System as the default. Without a usable, consented override, follow OS appearance without
writing a preference. Retain compatibility with stored Light/Dark/System values. Keep
choice separate from resolved Light/Dark; if OS detection fails, render dark safely.
Bootstrap and the mounted application must implement the same contract.

- Retain the compact sun/moon icon and a 44px target. Use a native button whose accessible
  name describes the next action: “Switch to light theme” or “Switch to dark theme”.
- Click, Space or Enter must select the opposite resolved theme directly, retaining
  focus. No appearance menu, extra gesture or second click to select an option.
- Explicit choice survives reload only where consent/storage permits; denied storage
  must not prevent the current-visit switch or crash rendering. Show truthful feedback.
- System follows OS changes until an explicit choice, with listener cleanup. Routes and
  shell remounts must not silently choose a different theme or erase drafts, jobs or
  selected study state.
- Load critical tokens before bootstrap reads them. Test blocked React bundles. Retain
  one application-owned `theme-color` tag so media-scoped metadata cannot override an
  explicit application choice. Resolve native `color-scheme` too.

**Done when:** first paint, mounted app, public/app navigation, native controls and browser
metadata agree for every supported choice, including invalid/denied storage and consent
changes. Production theme/consent tests cover the deliberately updated contract.

### 4. Connect presentation to real evidence

The preview's `data.ts`, in-memory saves, simulated investigation timers, fictional notes
and deterministic returns are **test/demo fixtures only**. They are not API fallbacks,
production seed data, a real evidence corpus or proof of rigor. Preserve real data absence,
errors, pending and degenerate states rather than displaying plausible demonstration values.

Keep custom chart inspection and comparison behavior where the production data supports
it. Define field units, dates, frequency, currency, missing observations, aligned windows,
benchmark provenance and return/drawdown sign conventions before binding series. The
preview's 5/10/20 bps **monthly synthetic scenario** is not a live fee or a generally valid
way to recalculate a production backtest. Preserve existing methodology or obtain an
explicitly reviewed domain change. Never relabel the synthetic model as historical evidence.

Keep source disclosure/dialog focus, precise input validation, comparison scales and
export/display agreement. Persistence claims must describe actual account/session storage.
Roadmap vault execution and settlement remain gated; no public product-analogy branding
or curated strategy pass-count claim. See the repo's existing product and rigor contracts.

**Done when:** every displayed result, status and export has a production source and an
honest failure/absence path; no fixture can silently stand in for live evidence.

## Verification and exit gate

Run focused checks first, then the production UI gates. Resolve actual Node/npm versions
from this checkout; use hermetic fixtures and mocked boundaries, not credentials or `.env`.

```bash
cd ui
npm run concept:check
node --test test/observatory.test.js
npm run concept:build
# Serve separately when needed; default preview port is not assumed by browser checks.
npm run concept:preview -- --host 127.0.0.1 --port 5181
```

Against that server, run the retained `concept/identity-check.js`, `component-check.js`
and `browser-check.js` with Playwright CLI. For production changes also run:

```bash
cd ui
npm run lint
npm test
npm run build
npm run check:routes
```

- Port relevant browser contracts to actual production routes, not only the standalone
  demo. Test anonymous/authenticated/gated routes, running/error/retry jobs, consent,
  keyboard-only navigation and dialogs; retain source-contract tests rather than
  weakening them to accommodate restyling.
- Capture identical live-fixture states in light/dark at desktop/mobile widths. Check
  320px layout and text enlargement, text contrast (4.5:1 normal / 3:1 large), essential
  boundaries (3:1), disabled states and reduced motion. Inspect actual screenshots.
- The preview's identity/component checks passed after the compact-switcher change.
  Earlier bounded independent reviews are not production or general visual approval.
  The initial handoff retained nine source-contract failures; later formatting exposed
  two more. Integration preparation traced all of them to quote, whitespace and trailing-
  comma sensitivity. Guards now accept equivalent formatting without dropping the
  payment, disclosure or sourcing assertions; negative source mutations still reject
  missing protections. Re-run the full suite rather than inheriting an old result.
- Existing preview chunk-size warning remains. Chromium evidence is not cross-engine,
  screen-reader, native zoom or complete WCAG certification. A synthetic disabled+pressed
  Button retained selected colors in review; no current preview caller combines them.
  Define/test that combination if production introduces it.

**Handoff exit:** identify changed routes/files, report exact fresh command outcomes and
residual risks, and obtain production review before integration. This document does not
authorize staging unrelated work, discarding changes, lock removal, merge, push or deploy.
