import { useState, useEffect, useLayoutEffect, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { roadmapSurfaceHidden, ROADMAP_SURFACES_ENABLED } from '../featureFlags.js'
import { canNavigateTo } from '../routes.js'
import { rectOnScreen } from '../tourGeometry.js'
import useDialogFocus from '../hooks/useDialogFocus'
import { canStore } from '../storage-consent.js'

const STORAGE_KEY = 'archimedes.onboarding.v1'

// Card content — kept here, not in a separate JSON, so contributors can
// edit the copy alongside the visuals. Order matches the user journey:
// intro → generate → review the library → deploy → audit.
//
// `anchor` is a nav id (see Layout.jsx `data-tour`). When set, the step
// spotlights that real nav button; when null the step is a centered card.
// Steps anchored to a hidden roadmap surface (#1266: the 'deploy' step's
// anchor-navigation effect calls setPage('portfolio') on mount — a dead door
// for every first-time visitor) are filtered out below.
const ALL_CARDS = [
  {
    id: 'what',
    title: 'What is Archimedes?',
    body: (
      <>
        <strong>Research-grounded strategy generation</strong> — not a robo-advisor.
        Describe what you want; we fuse your intent with primary academic
        research in quantitative finance, machine learning, agentic systems, and
        pure mathematics, then gate the result through selection-bias rigor.
      </>
    ),
    anchor: null,
    illustration: 'archimedes',
  },
  {
    id: 'generate',
    title: 'Generate a strategy',
    body: (
      <>
        Describe your goal in plain English; the agent weights arxiv-grounded
        strategies under hard risk constraints and streams its deliberation live.
      </>
    ),
    anchor: 'generate',
    illustration: 'generate',
  },
  {
    id: 'library',
    title: 'Review the library',
    body: (
      <>
        Your generated strategies accumulate here — each carries a passport linking
        back to the source papers, the backtest, and the rigor verdict.
      </>
    ),
    anchor: 'library',
    illustration: 'corpus',
  },
  {
    // Real regardless of the flag — paper trading isn't a ROADMAP_PAGES
    // surface (routes.js), it's the MVP spine's act-on step (Layout.jsx
    // "the act-on step of the MVP spine"). Named here so the tour has an
    // honest next step even when 'deploy' below is filtered out (#1354).
    //
    // anchor is intentionally null, not 'paper': no nav item carries
    // data-tour="paper" (Layout.jsx's NAV has no 'paper' entry), so the
    // measure() effect would never find the element and would fall through
    // to its "not mounted yet" branch, which calls setPage('paper') as a
    // side effect. For a signed-out visitor that navigates to a page kind:
    // 'app' route outside ANON_APP_PAGES (routes.js) and App.jsx bounces
    // straight to /sign-in — the exact anon-bounce #1354's anti-goal
    // forbids. A null anchor renders this as a centered, text-only card
    // (rect stays null either way) with no navigation side effect at all.
    id: 'paper',
    title: 'Try it as paper trading',
    body: (
      <>
        Every rigor-gate winner can run as a <strong>simulated, account-owned
        paper trade</strong> — free, immediate, no wallet signature required.
      </>
    ),
    anchor: null,
    illustration: 'paper',
  },
  {
    id: 'deploy',
    title: 'Deploy as a vault',
    body: (
      <>
        Strategies are <strong>time-bound</strong> — deploy into an ERC-4626 vault
        before the window expires. Funds stay non-custodial; the agent has
        rebalance authority only.
      </>
    ),
    anchor: 'portfolio',
    illustration: 'vault',
  },
  {
    id: 'reasoning',
    title: 'Audit the reasoning',
    body: ROADMAP_SURFACES_ENABLED ? (
      <>
        Every autonomous decision is <strong>hashed and anchored on Arc</strong> via
        the ReasoningTraceRegistry. Trace any action back to the strategy and the
        academic research that grounds it.
      </>
    ) : (
      <>
        Every rigor verdict carries the <strong>passport</strong> that produced it
        — source papers, backtest, and gate values, all traceable. Trade-level
        commit-reveal (hashed and anchored on Arc) activates once vault deploys
        are live.
      </>
    ),
    anchor: 'reasoning',
    illustration: 'reasoning',
  },
]

const CARDS = ALL_CARDS.filter((card) => !roadmapSurfaceHidden(card.anchor))

function Illustration({ name }) {
  // Simple geometric SVGs — no external dependencies, theme-aware via
  // CSS variables. Each is ~80px tall, sized to fit the card hero.
  const accent = 'var(--accent)'
  const text = 'var(--text-2)'
  const muted = 'var(--text-4)'
  const bg = 'var(--surface-2)'

  switch (name) {
    case 'archimedes':
      return (
        <svg viewBox="0 0 160 80" width="100%" height="80" aria-hidden="true">
          <rect x="2" y="2" width="156" height="76" rx="6" fill={bg} />
          <text x="80" y="50" textAnchor="middle" fontFamily="serif" fontSize="44" fill={accent}>A</text>
          <text x="80" y="68" textAnchor="middle" fontFamily="serif" fontStyle="italic" fontSize="9" fill={muted}>archimedes</text>
        </svg>
      )
    case 'corpus':
      return (
        <svg viewBox="0 0 160 80" width="100%" height="80" aria-hidden="true">
          <rect x="2" y="2" width="156" height="76" rx="6" fill={bg} />
          {[0, 1, 2, 3].map(i => (
            <rect key={i} x={20 + i * 30} y={20 + (i % 2) * 6} width="22" height="40" rx="2" fill={i === 2 ? accent : muted} opacity={i === 2 ? 1 : 0.5} />
          ))}
          <text x="80" y="73" textAnchor="middle" fontFamily="monospace" fontSize="8" fill={muted}>q-fin paper records</text>
        </svg>
      )
    case 'generate':
      return (
        <svg viewBox="0 0 160 80" width="100%" height="80" aria-hidden="true">
          <rect x="2" y="2" width="156" height="76" rx="6" fill={bg} />
          <circle cx="40" cy="40" r="6" fill={muted} opacity="0.5" />
          <circle cx="40" cy="22" r="6" fill={muted} opacity="0.5" />
          <circle cx="40" cy="58" r="6" fill={muted} opacity="0.5" />
          <path d="M 50 22 L 110 40 M 50 40 L 110 40 M 50 58 L 110 40" stroke={accent} strokeWidth="1.5" fill="none" />
          <circle cx="118" cy="40" r="8" fill={accent} />
          <text x="118" y="44" textAnchor="middle" fontFamily="monospace" fontSize="8" fill="#000">α</text>
          <text x="80" y="73" textAnchor="middle" fontFamily="monospace" fontSize="8" fill={muted}>fusion → strategy</text>
        </svg>
      )
    case 'reasoning':
      return (
        <svg viewBox="0 0 160 80" width="100%" height="80" aria-hidden="true">
          <rect x="2" y="2" width="156" height="76" rx="6" fill={bg} />
          <rect x="20" y="20" width="50" height="40" rx="3" fill="none" stroke={text} strokeWidth="1" />
          <text x="45" y="44" textAnchor="middle" fontFamily="monospace" fontSize="9" fill={text}>0xc4a3…</text>
          <path d="M 75 40 L 100 40" stroke={accent} strokeWidth="1.5" strokeDasharray="3 2" />
          <polygon points="100,36 108,40 100,44" fill={accent} />
          <rect x="113" y="20" width="42" height="40" rx="3" fill={accent} opacity="0.2" stroke={accent} strokeWidth="1" />
          <text x="134" y="38" textAnchor="middle" fontFamily="monospace" fontSize="7" fill={accent}>arc</text>
          <text x="134" y="50" textAnchor="middle" fontFamily="monospace" fontSize="7" fill={accent}>anchor</text>
          <text x="80" y="73" textAnchor="middle" fontFamily="monospace" fontSize="8" fill={muted}>hash → on-chain</text>
        </svg>
      )
    case 'paper':
      return (
        <svg viewBox="0 0 160 80" width="100%" height="80" aria-hidden="true">
          <rect x="2" y="2" width="156" height="76" rx="6" fill={bg} />
          <rect x="30" y="16" width="100" height="48" rx="3" fill="none" stroke={text} strokeWidth="1" strokeDasharray="3 2" />
          <polyline points="42,48 58,38 72,44 90,26 108,32 120,20" fill="none" stroke={accent} strokeWidth="1.5" />
          <text x="80" y="73" textAnchor="middle" fontFamily="monospace" fontSize="8" fill={muted}>simulated · no funds</text>
        </svg>
      )
    case 'vault':
      return (
        <svg viewBox="0 0 160 80" width="100%" height="80" aria-hidden="true">
          <rect x="2" y="2" width="156" height="76" rx="6" fill={bg} />
          <rect x="55" y="18" width="50" height="46" rx="3" fill="none" stroke={text} strokeWidth="1.5" />
          <circle cx="80" cy="41" r="10" fill={accent} opacity="0.3" stroke={accent} strokeWidth="1.5" />
          <text x="80" y="45" textAnchor="middle" fontFamily="monospace" fontSize="10" fill={accent}>$</text>
          <text x="80" y="73" textAnchor="middle" fontFamily="monospace" fontSize="8" fill={muted}>non-custodial · ERC-4626</text>
        </svg>
      )
    case 'watch':
      return (
        <svg viewBox="0 0 160 80" width="100%" height="80" aria-hidden="true">
          <rect x="2" y="2" width="156" height="76" rx="6" fill={bg} />
          <polyline points="20,55 40,42 55,48 75,30 95,35 115,22 140,28" fill="none" stroke={accent} strokeWidth="1.5" />
          {[20, 40, 55, 75, 95, 115, 140].map((x, i) => {
            const ys = [55, 42, 48, 30, 35, 22, 28]
            return <circle key={i} cx={x} cy={ys[i]} r="1.8" fill={accent} />
          })}
          <text x="80" y="73" textAnchor="middle" fontFamily="monospace" fontSize="8" fill={muted}>live performance + traces</text>
        </svg>
      )
    default:
      return null
  }
}

export function hasCompletedOnboarding() {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'completed'
  } catch {
    return false
  }
}

// Spotlight geometry constants.
const HOLE_PAD = 6        // px of breathing room around the highlighted element
const TIP_W = 340         // tooltip width
const TIP_GAP = 16        // gap between hole and tooltip
const TIP_EST_H = 380     // height estimate used only to keep the tooltip on-screen

export default function OnboardingTour({ open, onClose, setPage, user }) {
  const [cardIndex, setCardIndex] = useState(0)
  // Bounding rect of the spotlighted element, in viewport coords. `null`
  // means "no anchor / element not measurable" → render a centered card.
  const [rect, setRect] = useState(null)

  // Reset to first card every time the tour is opened, so the "?" reopen
  // affordance always starts the user at card 1.
  useEffect(() => {
    if (open) setCardIndex(0)
  }, [open])

  const card = CARDS[cardIndex]
  const isLast = cardIndex === CARDS.length - 1

  // Measure the current step's anchor element. Falls back to `null` (centered
  // card) when there's no anchor, the element is absent, or it's not on
  // screen — mobile drawer closed → a full-size rect translated to
  // left ≈ -260 (non-zero, off-viewport). A zero-size rect (e.g. a
  // `display:none` ancestor) is rejected too. The collapsed rail (72px)
  // still measures a normal on-screen box and spotlights as usual.
  // See rectOnScreen in ../tourGeometry.js.
  const measure = useCallback(() => {
    const c = CARDS[cardIndex]
    if (!c.anchor) { setRect(null); return }
    const el = document.querySelector(`[data-tour="${c.anchor}"]`)
    if (!el) { setRect(null); return }
    const r = el.getBoundingClientRect()
    if (!rectOnScreen(r, window.innerWidth, window.innerHeight)) { setRect(null); return }
    setRect({ top: r.top, left: r.left, width: r.width, height: r.height })
  }, [cardIndex])

  // Re-measure on open, step change, resize, and scroll. `scroll` is captured
  // (true) so it fires for scrolling inside the sidebar/main, not just window.
  useLayoutEffect(() => {
    if (!open) return
    measure()
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => {
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [open, measure])

  // Drive the app into view when a step's anchor isn't mounted yet. The most
  // common case: the tour auto-opens on the Landing page, which renders without
  // the sidebar — so the nav anchors don't exist. Navigating to the step's page
  // mounts the Layout shell (sidebar + nav), then we re-measure once it paints.
  // Guarded by `rect === null` so it fires at most once per step.
  //
  // Also guarded by `canNavigateTo` (#1364): `library` and `reasoning` are
  // app pages an anonymous visitor may not open — before this guard, a
  // missing anchor for those pages (anonymous viewer, or any page not yet
  // mounted) drove setPage() anyway, App.jsx answered with
  // `window.location.replace('/sign-in?next=…')`, and the tour unmounted
  // along with the page the visitor was reading. When navigation isn't
  // allowed we deliberately do nothing and fall through to the
  // centered-card render below (`rect` stays null), which needs no anchor.
  useEffect(() => {
    if (!open || rect !== null || !card.anchor) return
    if (!canNavigateTo(card.anchor, user)) return
    // `rect === null` covers two cases and only one wants a navigation: the
    // anchor absent from the DOM (navigating mounts it, e.g. from Landing),
    // or the anchor mounted but off-screen (mobile drawer closed) — same
    // page already, navigating just pushes a pointless history entry per
    // card. Only navigate when the anchor truly isn't mounted yet.
    if (document.querySelector(`[data-tour="${card.anchor}"]`)) return
    setPage(card.anchor)
    // Sidebar mounts on the next render; re-measure after it paints.
    const id = setTimeout(measure, 60)
    return () => clearTimeout(id)
  }, [open, rect, card, setPage, measure, user])

  const finish = useCallback(() => {
    try {
      // Functional category (#1647): with functional storage rejected the
      // dismissal is not remembered and the tour offers itself again next
      // visit — the stated fallback in the storage disclosure.
      if (canStore(STORAGE_KEY)) localStorage.setItem(STORAGE_KEY, 'completed')
    } catch {
      // localStorage unavailable (e.g. SSR / private mode) — non-fatal;
      // we just won't remember the dismissal next visit.
    }
    onClose()
  }, [onClose])

  const handleContinue = useCallback(() => {
    if (isLast) finish()
    else setCardIndex(i => i + 1)
  }, [isLast, finish])

  // The tour auto-opens for every first-time signed-in user and declares
  // aria-modal="true", which removes the rest of the page from the
  // accessibility tree — but focus was never moved in, never trapped and never
  // restored, so the tour was announced as nothing while a sighted keyboard
  // user tabbed straight through the four dim panels into the app behind them.
  // refocusKey: the centered-card branch and the spotlight branch are two
  // different portal trees, so advancing off card 0 (the only unanchored card)
  // destroys the panel the user's focus is in and strands it on <body>.
  const panelRef = useDialogFocus(open, { refocusKey: cardIndex })

  // Keyboard support — Esc closes, ArrowRight advances, ArrowLeft goes back.
  useEffect(() => {
    if (!open) return
    const onKey = (e) => {
      if (e.key === 'Escape') finish()
      if (e.key === 'ArrowRight') handleContinue()
      if (e.key === 'ArrowLeft' && cardIndex > 0) setCardIndex(i => i - 1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, cardIndex, finish, handleContinue])

  if (!open) return null

  // Shared panel content (counter, hero, copy, dots, actions). Rendered either
  // as a centered card or as a tooltip beside the spotlight hole.
  const panel = (
    <>
      <div className="caption mb-2 text-[var(--text-4)] uppercase tracking-wider">
        Welcome · {cardIndex + 1} of {CARDS.length}
      </div>

      <Illustration name={card.illustration} />

      <h3 id="onboarding-title" className="font-serif text-[1.4rem] mt-4 mb-2">
        {card.title}
      </h3>

      <p className="body leading-relaxed mb-5">
        {card.body}
      </p>

      {/* Pagination dots. The 8px visual dot is unchanged, but the hit area is
          now the 24x24 minimum (2.5.8) — at 8px with a 6px gap the 24px
          targets centred on adjacent dots overlapped, so the spacing exception
          did not rescue them either. The tablist/tab roles are dropped rather
          than kept: it promised arrow-key navigation between tabs and matching
          tabpanels, neither of which this component implements. */}
      <div className="flex justify-center mb-5" role="group" aria-label="Tour progress">
        {CARDS.map((c, i) => (
          <button
            key={c.id}
            type="button"
            aria-current={i === cardIndex ? 'true' : undefined}
            aria-label={`Card ${i + 1}: ${c.title}`}
            onClick={() => setCardIndex(i)}
            className="w-6 h-6 flex items-center justify-center border-none cursor-pointer"
            style={{ background: 'transparent', padding: 0 }}
          >
            <span
              aria-hidden="true"
              className="w-2 h-2 rounded-full"
              style={{
                display: 'block',
                background: i === cardIndex ? 'var(--accent)' : 'var(--text-4)',
                opacity: i === cardIndex ? 1 : 0.4,
              }}
            />
          </button>
        ))}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 justify-between flex-nowrap">
        <button type="button" className="btn btn-outline btn-sm" onClick={finish}>
          Skip
        </button>
        <div className="flex gap-2 flex-nowrap">
          {cardIndex > 0 && (
            <button type="button" className="btn btn-outline btn-sm" onClick={() => setCardIndex(i => i - 1)}>
              Back
            </button>
          )}
          <button type="button" className="btn btn-primary btn-sm" onClick={handleContinue}>
            {isLast ? 'Done' : 'Continue'}
          </button>
        </div>
      </div>
    </>
  )

  // ── Centered card (no anchor, or element not measurable) ───────────────
  if (!rect) {
    return createPortal(
      <div
        className="tour-overlay fixed inset-0 flex items-center justify-center z-[10001]"
        onClick={finish}
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
      >
        <div
          ref={panelRef}
          tabIndex={-1}
          className="card-elevated p-6 max-w-[460px] w-[90vw]"
          onClick={e => e.stopPropagation()}
          style={{ background: 'var(--surface-1)', opacity: 1 }}
        >
          {panel}
        </div>
      </div>,
      document.body,
    )
  }

  // ── Spotlight: 4 dim panels frame a transparent hole over the element ──
  const hole = {
    top: rect.top - HOLE_PAD,
    left: rect.left - HOLE_PAD,
    width: rect.width + HOLE_PAD * 2,
    height: rect.height + HOLE_PAD * 2,
  }
  const holeRight = hole.left + hole.width
  const holeBottom = hole.top + hole.height

  // Place the tooltip to the right of the hole (sidebar lives on the left).
  // If it would overflow the right edge, flip to the left of the hole. Clamp
  // the top so the tooltip stays fully on-screen.
  const vw = window.innerWidth
  const vh = window.innerHeight
  const placeRight = holeRight + TIP_GAP + TIP_W <= vw
  const tipLeft = placeRight ? holeRight + TIP_GAP : Math.max(TIP_GAP, hole.left - TIP_GAP - TIP_W)
  const tipTop = Math.min(Math.max(TIP_GAP, hole.top), Math.max(TIP_GAP, vh - TIP_EST_H))

  // pointer-events on each dim panel catch stray clicks (→ finish); the hole
  // between them has no element, so clicks reach the live nav button.
  // z-indices (10001/10002/10003) clear the mobile drawer's 10000 (App.css)
  // so a visitor who opens the drawer to find the spotlighted item sees the
  // tooltip painted above it, not behind it (#1364).
  const dim = 'rgba(0,0,0,0.78)'
  const panelStyle = { position: 'fixed', background: dim, zIndex: 10001 }

  return createPortal(
    <div role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
      {/* Top */}
      <div style={{ ...panelStyle, top: 0, left: 0, width: '100vw', height: Math.max(0, hole.top) }} onClick={finish} aria-hidden="true" />
      {/* Bottom */}
      <div style={{ ...panelStyle, top: holeBottom, left: 0, width: '100vw', bottom: 0 }} onClick={finish} aria-hidden="true" />
      {/* Left */}
      <div style={{ ...panelStyle, top: hole.top, left: 0, width: Math.max(0, hole.left), height: hole.height }} onClick={finish} aria-hidden="true" />
      {/* Right */}
      <div style={{ ...panelStyle, top: hole.top, left: holeRight, right: 0, height: hole.height }} onClick={finish} aria-hidden="true" />

      {/* Highlight ring around the hole — purely decorative, clicks pass through */}
      <div
        className="tour-ring"
        style={{
          position: 'fixed',
          top: hole.top, left: hole.left, width: hole.width, height: hole.height,
          zIndex: 10002, pointerEvents: 'none',
        }}
        aria-hidden="true"
      />

      {/* Tooltip */}
      <div
        ref={panelRef}
        tabIndex={-1}
        className="card-elevated tour-tooltip p-5"
        style={{
          position: 'fixed', top: tipTop, left: tipLeft, width: TIP_W, maxWidth: '90vw',
          zIndex: 10003, background: 'var(--surface-1)',
        }}
        onClick={e => e.stopPropagation()}
      >
        {panel}
      </div>
    </div>,
    document.body,
  )
}
