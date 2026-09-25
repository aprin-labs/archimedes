import { ROADMAP_PAGES, ROADMAP_SURFACES_ENABLED } from './featureFlags.js'

const PUBLIC_PATHS = {
  '/': 'landing',
  '/architecture': 'architecture',
  '/security': 'security',
}

const AUTH_PATHS = {
  '/sign-in': 'sign-in',
  '/sign-up': 'sign-up',
  '/reset-password': 'reset-password',
}

const APP_PATHS = {
  '/app': 'explore',
  '/app/account': 'account',
  '/app/explore': 'explore',
  '/app/leaderboard': 'leaderboard',
  '/app/generate': 'generate',
  '/app/library': 'library',
  '/app/paper': 'paper',
  '/app/corpus': 'corpus',
  '/app/quant': 'quant',
  '/app/portfolio': 'portfolio',
  '/app/reasoning': 'reasoning',
  '/app/learnings': 'learnings',
  '/app/insights': 'insights',
  '/app/marketplace': 'marketplace',
  '/app/publish': 'publish',
  '/app/subscriptions': 'subscriptions',
}

// Pages under /app an anonymous visitor may browse (#1753, the owner's
// product call, narrowing #1194 revision d): Explore and Corpus
// only. Library, the leaderboard and the strategy passport are gated — an
// anonymous visitor who opens one is sent to /sign-in with `next=` carrying
// the deep link (App.jsx's redirect effect for an in-app navigation, nginx's
// @sign_in 302 for a cold load). #1194 rev d had 'leaderboard' and
// 'strategy' in this set; both were removed here, deliberately.
// DELIBERATELY a marker set, not a carve-out from APP_PATHS — PAGE_PATHS and
// LEGACY_PATHS are both derived from APP_PATHS, so removing entries there
// silently breaks pageToPath (corpus/leaderboard nav clicks would fall
// through to '/app' and land on Explore). Keep the paths where they are;
// mark them anonymous-OK instead. Must stay in lockstep with the nginx
// carve-outs in nginx/nginx.conf (the server-side half of this gate) —
// enforced by backend/tests/test_nginx_anonymous_carve_outs.py, which reads
// both files and derives one side from the other.
const ANON_APP_PAGES = new Set(['explore', 'corpus'])

export function isAnonymousAppPage(page) {
  return ANON_APP_PAGES.has(page)
}

// Where the strategy passport's "Back to Library" control should resolve
// (#1370). `library` is wallet-gated (not in ANON_APP_PAGES), so a visitor
// with no session hitting `onNavigate('library')` unconditionally tripped
// App.jsx's anonymous-page redirect and bounced them to /sign-in, signing
// out a visitor who was never signed in.
//
// Since #1753 the passport itself is gated (`strategy` left ANON_APP_PAGES,
// and nginx dropped its `^~ /app/strategy` carve-out), so the `user == null`
// branch is no longer a reachable product state: App.jsx holds a non-anon
// route on "Loading account…" until auth resolves and redirects to
// /sign-in?next= if it resolves to nobody. It is kept as a FAIL-SAFE, not as
// a described flow — a control must not be able to eject a sessionless
// render, whoever produced it (a test harness, a future re-opening of the
// carve-out, a partial hydration). It must therefore keep returning a page
// isAnonymousAppPage() actually allows.
export function passportBackPage(user) {
  return user == null ? 'explore' : 'library'
}

// The back button's own label must track passportBackPage(user) — a control
// that reads "Back to Library" while it actually lands on Explore is a
// mislabeled affordance, the same defect class #1370 fixed for the routing
// itself. Keep this as a pure sibling function (not folded into
// passportBackPage's return shape) so callers that only need the page still
// get a plain string.
export function passportBackLabel(user) {
  return user == null ? '← Back to Explore' : '← Back to Library'
}

// True when `user` may navigate to `page` — signed in, always; anonymous,
// only for the browsable ANON_APP_PAGES set above. Delegates to
// isAnonymousAppPage so the two never drift (#1364): the onboarding tour's
// navigation effect gates on this, not on a hard-coded card id, so any page
// the product later adds to ANON_APP_PAGES becomes anon-navigable for the
// tour automatically, and any page NOT added stays a centered card instead
// of ejecting the visitor to /sign-in.
export function canNavigateTo(page, user) {
  return user != null || isAnonymousAppPage(page)
}

const PAGE_PATHS = Object.fromEntries(Object.entries(APP_PATHS).map(([path, page]) => [page, path]))
const LEGACY_PATHS = Object.fromEntries(
  Object.entries(APP_PATHS)
    .filter(([path]) => path !== '/app')
    .map(([path]) => [path.replace('/app', ''), path]),
)

const route = (kind, page, extras = {}) => ({
  kind,
  page,
  vaultAddress: null,
  traceId: null,
  strategyId: null,
  highlight: null,
  tab: null,
  error: null,
  redirect: null,
  anonymousOk: kind === 'app' && ANON_APP_PAGES.has(page),
  ...extras,
})

export function featureEnabled(page, features = { quant: true }) {
  // Roadmap surfaces (#1266): hidden at BUILD time (VITE_ROADMAP_SURFACES,
  // default off), deliberately separate from the runtime features fetch —
  // parseFeatures() never emits `roadmapSurfaces`, so the override below is
  // reachable only from tests, never from the server.
  const roadmapOn = features.roadmapSurfaces ?? ROADMAP_SURFACES_ENABLED
  if (!roadmapOn && ROADMAP_PAGES.has(page)) return false
  return page !== 'quant' || features.quant === true
}

export function resolveRoute(pathname = '/', search = '', features = { quant: true }) {
  const params = new URLSearchParams(search)
  const query = {
    highlight: params.get('highlight'),
    traceId: params.get('trace_id'),
    tab: params.get('tab'),
    error: params.get('error'),
  }

  // Better Auth's account-linking guard (auth/auth.js: accountLinking.
  // disableImplicitLinking — a deliberate security posture, not a bug) rejects
  // an OAuth sign-in for an email that already owns a password account by
  // redirecting the browser straight back to `${baseURL}?error=...`, i.e. the
  // bare landing route. Landing has no sign-in form to show that error on —
  // the surface with the "Continue with Google/GitHub" buttons the visitor
  // just clicked is /sign-in. Bounce the error there so AuthPage can read and
  // render it instead of it silently vanishing.
  if (pathname === '/' && query.error) {
    return route('redirect', null, { redirect: `/sign-in?error=${encodeURIComponent(query.error)}` })
  }

  if (PUBLIC_PATHS[pathname]) return route('public', PUBLIC_PATHS[pathname], query)
  if (AUTH_PATHS[pathname]) return route('auth', AUTH_PATHS[pathname], query)

  const page = APP_PATHS[pathname]
  if (page) return featureEnabled(page, features) ? route('app', page, query) : route('not-found', null)

  const deepRoutes = [
    ['/app/portfolio/vaults/', 'vault-detail', 'vaultAddress'],
    ['/app/reasoning/', 'reasoning', 'traceId'],
    ['/app/strategy/', 'strategy', 'strategyId'],
    ['/app/marketplace/strategy/', 'market-strategy', 'strategyId'],
  ]
  for (const [prefix, deepPage, key] of deepRoutes) {
    if (!pathname.startsWith(prefix)) continue
    const value = pathname.slice(prefix.length)
    if (value) {
      // Deep routes go through the SAME feature gate as flat routes. This loop
      // previously skipped featureEnabled(), so a feature-disabled page stayed
      // reachable via its deep link — latent today (only quant is gated, and
      // quant has no deep route), but it becomes a real page-hiding bypass the
      // moment featureEnabled() gates more pages (the UI-hide work folds its
      // gating in here precisely so this check is the single gate for both
      // nav and routing).
      return featureEnabled(deepPage, features)
        ? route('app', deepPage, { ...query, [key]: decodeURIComponent(value) })
        : route('not-found', null)
    }
  }

  if (LEGACY_PATHS[pathname]) return route('redirect', null, { redirect: `${LEGACY_PATHS[pathname]}${search}` })
  if (pathname.startsWith('/portfolio/vaults/')) {
    return route('redirect', null, { redirect: `/app${pathname}${search}` })
  }
  if (pathname.startsWith('/strategy/') || pathname.startsWith('/reasoning/') || pathname.startsWith('/marketplace/strategy/')) {
    return route('redirect', null, { redirect: `/app${pathname}${search}` })
  }
  return route('not-found', null)
}

export function pageToPath(page, options = {}) {
  if (page === 'landing') return '/'
  if (page === 'architecture') return '/architecture'
  if (page === 'security') return '/security'
  if (page === 'vault-detail' && options.vaultAddress) return `/app/portfolio/vaults/${options.vaultAddress}`
  if (page === 'strategy' && options.strategyId) return `/app/strategy/${encodeURIComponent(options.strategyId)}`
  if (page === 'market-strategy' && options.strategyId) return `/app/marketplace/strategy/${encodeURIComponent(options.strategyId)}`
  const base = PAGE_PATHS[page] ?? '/app'
  const params = new URLSearchParams()
  if (options.highlight && page === 'library') params.set('highlight', options.highlight)
  if (options.traceId && page === 'reasoning') params.set('trace_id', options.traceId)
  if (options.tab && page === 'library') params.set('tab', options.tab)
  const query = params.toString()
  return query ? `${base}?${query}` : base
}

export function safeNextPath(value) {
  if (typeof value !== 'string' || !value.startsWith('/app') || value.startsWith('//')) return '/app'
  try {
    const url = new URL(value, 'https://archimedes.invalid')
    return url.origin === 'https://archimedes.invalid' && url.pathname.startsWith('/app')
      ? `${url.pathname}${url.search}${url.hash}`
      : '/app'
  } catch {
    return '/app'
  }
}

export function postAuthPath(search = '') {
  const params = new URLSearchParams(search)
  const requested = params.get('next')
  const next = safeNextPath(requested)
  if (next === '/app' && requested !== '/app') return next
  params.delete('next')
  const query = params.toString()
  return query && !next.includes('?') && !next.includes('#') ? `${next}?${query}` : next
}

// Nav ids an anonymous visitor should see: the browsable pages plus Generate,
// which is the conversion path (clicking it routes to sign-in). Everything
// else — portfolio, learnings, marketplace, account and friends — reads the
// signed-in user's own state and is noise on a logged-out screen.
// 'leaderboard' was dropped (#1753): it left ANON_APP_PAGES with the same
// owner call, and an anon nav entry for a page canNavigateTo() now refuses
// is an affordance that only ejects the visitor. 'architecture'
// was dropped (#1370 item 4): Architecture is no longer a shell NAV item (see
// Layout.jsx), so this entry has been unreachable dead config the same way the
// CRUMB_MAP 'architecture' key was before this PR removed that one too.
// 'landing' was dropped the same way (#1641): the marketing-site sidebar entry
// is gone from navConfig.js, so no nav item carries that id any more. It stays
// a real page id — PUBLIC_PATHS['/'] and pageToPath('landing') are untouched —
// it just isn't a NAV id, and this set is only ever consulted with NAV ids
// (visibleNavigation, called from Layout.jsx over NAV's groups).
const ANON_NAV_IDS = new Set(['explore', 'corpus', 'generate'])

export function visibleNavigation(items, features, user = null) {
  return items.filter(
    (item) => featureEnabled(item.id, features) && (user != null || ANON_NAV_IDS.has(item.id)),
  )
}
