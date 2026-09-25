/**
 * Breadcrumb navigation for interior pages.
 *
 * Derives the crumb trail from the current page ID. Each crumb is
 * clickable and navigates via the `setPage` callback. The sidebar's
 * section structure lives in ../navConfig.js (#1437); a crumb trail may
 * name one of its sections only when a real landing page for that section
 * exists to link to — see CRUMB_MAP below and #1405.
 */

import { PAGE_LABELS } from './Layout'
import { roadmapSurfaceHidden } from '../featureFlags.js'

// `group: null` means "no intermediate label" — breadcrumb reads "Home / <page>".
// Group labels, when used, must be current navConfig.js NAV sections
// (Strategy, Position, Market, Ops — "Discover" was dissolved into Strategy
// by #1641, and the allowed-label test moved with it). Every key must exist in
// routes.js (#1194 moved routing out of App.jsx's PAGE_TO_PATH) — subset
// invariant enforced by backend/tests/test_breadcrumbs.py (prevents a fifth
// stale-map occurrence).
//
// Deliberately excluded:
//   - landing — it *is* Home; breadcrumb would be circular.
//   - explore — it *is* the Home crumb's own target below. Its section has
//     no landing page distinct from Home/Explore, so it gets no group entry
//     either (#1370: the then-"Discover" mid-crumb pointed at the same page
//     as "Home" and repeated a stop, and on /app/explore itself "Home" and
//     the current-page crumb both named 'explore' — the same page listed
//     twice in one trail either way. Explore moved into Strategy in #1641;
//     the reasoning is unchanged, Strategy has no landing page either).
//   - architecture — moved out of the shell nav (#1370, see Layout.jsx); it
//     renders under PublicLayout, which never mounts Breadcrumbs, so a
//     CRUMB_MAP entry for it was unreachable dead config.
//   - vault-detail, strategy, market-strategy — dynamic routes with an id/address
//     param, not entries in PAGE_TO_PATH; reached via deep-link only.
// Primary path (generate, leaderboard, library, corpus) all have entries
// below; if one is intentionally omitted a comment must explain why.
//
// Every entry is flat as of #1405. The `group`/`groupPage` fields survive
// because they are the shape a *real* section landing page would use, and
// the component below still renders one — but the product has no such page
// today, so nothing may claim one. The rule, enforced by
// backend/tests/test_breadcrumbs.py::
// test_group_crumb_does_not_alias_a_sibling_nav_page:
//
//   a groupPage may not be a page that navConfig.js already lists as a
//   sidebar item of that same group.
//
// #1370 item 1 named the defect; #1400 fixed the Discover instance (which
// also repeated a page inside one trail) and flattened Ops on the same
// reasoning, and #1405 finished the job for the three below. Each of them
// rendered a mid-crumb labelled with a section name that navigated to a
// sibling page — "Strategy" landing on Generate, "Position" landing on the
// deployed-state overview, "Market" landing on Marketplace. The label named
// a section, the destination was a page with its own different name in the
// sidebar and its own different title on arrival, so the crumb was a
// mislabelled control: it never went where it said. Option 1 in #1405 —
// build real section landing pages — stays open; the moment one exists it
// gets a route, a nav-less identity of its own, and these entries can name
// it without tripping the guard.
export const CRUMB_MAP = {
  // Comment headers below mirror navConfig.js's sections as of #1641; they
  // are reader orientation only. The `group`/`groupPage` fields are what the
  // renderer and the tests read, and every one of them is still null (#1405).
  // Write no value literal in a comment inside this block: the parsers in
  // test_breadcrumbs.py match on the whole block text, so a commented-out
  // field counts as a real one and the entry/field tallies diverge.
  // Strategy — find and build one (explore is excluded above, it is Home)
  corpus:       { group: null, groupPage: null },
  generate:     { group: null, groupPage: null },
  library:      { group: null, groupPage: null },
  // Position — act on one and review the result
  paper:        { group: null, groupPage: null },
  reasoning:    { group: null, groupPage: null },
  leaderboard:  { group: null, groupPage: null },
  portfolio:    { group: null, groupPage: null },
  quant:        { group: null, groupPage: null },
  learnings:    { group: null, groupPage: null },
  // Market — strategy marketplace
  marketplace:  { group: null, groupPage: null },
  publish:      { group: null, groupPage: null },
  subscriptions:{ group: null, groupPage: null },
  // Ops — only two members (Insights, Account); with two pages there is no
  // third to justify a group crumb even if a landing page existed.
  insights:     { group: null, groupPage: null },
  account:      { group: null, groupPage: null },
}

export default function Breadcrumbs({ page, setPage }) {
  const info = CRUMB_MAP[page]
  if (!info) return null

  // When the group's landing page is a hidden roadmap surface (#1266), the
  // mid-crumb would link into a not-found route: fall back to a flat
  // Home / <page> crumb. No CRUMB_MAP entry declares a group today (#1405),
  // so this branch is inert — it is the contract a future section landing
  // page inherits, not live behaviour; the entries that used to reach it
  // (quant/reasoning aliasing the deployed-state overview, publish/
  // subscriptions aliasing Marketplace) were the aliases #1405 removed.
  const crumbs = [
    { label: 'Home', page: 'explore' },
    ...(info.group && !roadmapSurfaceHidden(info.groupPage)
      ? [{ label: info.group, page: info.groupPage }]
      : []),
    { label: PAGE_LABELS[page] ?? page, page: null },
  ]

  return (
    <nav className="breadcrumbs" aria-label="Breadcrumb">
      {crumbs.map((crumb, i) => {
        const isLast = i === crumbs.length - 1
        return (
          <span key={i} className="breadcrumb-item">
            {i > 0 && <span className="breadcrumb-sep">/</span>}
            {isLast ? (
              <span className="breadcrumb-current">{crumb.label}</span>
            ) : (
              <button
                type="button"
                className="breadcrumb-link"
                onClick={() => setPage(crumb.page)}
              >
                {crumb.label}
              </button>
            )}
          </span>
        )
      })}
    </nav>
  )
}
