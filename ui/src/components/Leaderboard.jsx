import { useState, useEffect, useCallback } from 'react'
import { useAuth } from '../AuthContext'
import { apiGet } from '../api'
import GateVerdictChip from './GateVerdictChip'
import MetricValue from './MetricValue'
import { formatMetric } from '../metricDomain.js'

// Single-user leaderboard (MVP pivot — no publish mechanism exists yet, so
// ranking a global cohort was incoherent; nobody had opted into competing).
// Signed in: ranks YOUR OWN strategies against each other by the backend's
// transparent conviction score (real rigor gate + backtest). Toggled to
// `curated`: shows the curated seed library as an honestly-labeled
// REFERENCE set, never framed as competition. Nothing here is fabricated:
// validation metrics are real passport fields. The PAGE is auth-gated as of
// #1753 (the owner's call): `leaderboard` left ANON_APP_PAGES and nginx
// dropped its `^~ /app/leaderboard` carve-out, so a signed-out visitor gets
// `302 /sign-in?next=/app/leaderboard` and the `!user` branch below is
// defensive, not a live product state. The ENDPOINT is unchanged and still
// anonymous (leaderboard_routes.py never 401s); see its `scope` field
// (own|curated), which reports what was actually served, not what was
// requested.
//
// TWO BOARDS, NEVER BLENDED (Lane 3.4). The conviction board is entirely
// BACKTEST-ERA — gate, DSR, OOS and PBO are all measured on history the
// strategy was fitted and graded against — so it lives behind the "Research"
// tab and every one of its rows states the window it was measured over. The
// "Live paper trading" tab is a genuinely different surface fed by a
// different endpoint (/api/leaderboard/live-paper): rows only for deployments
// that are actually running forward, ranked on realised return compounded
// from the append-only paper ledger. There is no combined score and no code
// path here that makes one — a blend would let a strong backtest carry a
// strategy that has traded forward for a handful of days.
//
// The load-bearing UI rule, guarded in ui/test/leaderboard-boards.test.js:
// the live tab renders ONLY from `liveData.entries`. It must never fall back
// to the research payload, and it must never synthesise a row for a
// deployment with no ledger data — the backend already withholds those, and
// the empty state below says so plainly instead of showing a zeroed row.

const RESEARCH_BOARD = 'research'
const LIVE_BOARD = 'live'

const BOARDS = [
  { id: RESEARCH_BOARD, label: 'Research (backtest conviction)' },
  { id: LIVE_BOARD, label: 'Live paper trading' },
]

// The one honest thing to say when the forward ledger is thin or empty.
const LIVE_EMPTY_MESSAGE = 'No strategies are live paper trading yet'

const SORT_OPTIONS = [
  { id: 'conviction_score', label: 'Conviction' },
  { id: 'deflated_sharpe_ratio', label: 'Deflated Sharpe' },
  { id: 'dsr_p_value', label: 'DSR confidence' },
  { id: 'sharpe_ratio', label: 'Sharpe' },
  { id: 'cagr', label: 'CAGR' },
  { id: 'pbo_score', label: 'Overfitting (PBO)' },
]

const REGIMES = [
  { id: '', label: 'All regimes' },
  { id: 'bull', label: 'Bull' },
  { id: 'bear', label: 'Bear' },
  { id: 'regime_neutral', label: 'Neutral' },
]

const MEDAL = { gold: '🥇', silver: '🥈', bronze: '🥉' }

// Residual local formatter for page furniture that is NOT a strategy metric —
// the StockBench benchmark sentence. Every metric with a declared domain goes
// through MetricValue / metricDomain.js instead (#1651), so this must never
// grow a new call site for one. The Number.isFinite guard is the reason it is
// still allowed to exist at all: `Number(NaN).toFixed(2)` renders the literal
// string "NaN", which is its own flavour of impossible number on the page.
function fmt(v, d = 2) {
  if (v == null || !Number.isFinite(Number(v))) return '—'
  return Number(v).toFixed(d)
}

// ── Provenance labelling ────────────────────────────────────────────────────
// Every number this page displays carries the basis it was measured on. The
// two bases are the ONLY two the product measures, they mean different things,
// and a reader who sees a figure without one has been told less than the truth.
// Copy is keyed off the backend's `performance_basis` string so the label can
// never drift from what the API actually said.
const BASIS_COPY = {
  backtest_research: {
    short: 'Backtest',
    title: 'Backtest research: measured on historical data the strategy was fitted and graded against — not forward performance.',
    color: 'var(--text-3)',
  },
  live_paper: {
    short: 'Live paper',
    title: 'Live paper trading: compounded from the append-only forward ledger since this deployment went live. Simulated — no funds move.',
    color: 'var(--accent)',
  },
}

function BasisBadge({ basis }) {
  const copy = BASIS_COPY[basis]
  if (!copy) return null
  return (
    <span
      title={copy.title}
      style={{
        fontSize: 10,
        textTransform: 'uppercase',
        letterSpacing: 0.5,
        padding: '1px 5px',
        borderRadius: 3,
        border: `1px solid ${copy.color}`,
        color: copy.color,
        whiteSpace: 'nowrap',
      }}
    >
      {copy.short}
    </span>
  )
}

// The backtest window a research row's numbers were measured over. Missing
// dates say so explicitly — an unlabelled number would read as if it applied
// to now, which is the misreading this whole split exists to prevent.
function WindowLabel({ start, end }) {
  const known = start && end
  return (
    <span
      style={{ fontSize: 11, color: 'var(--text-3)' }}
      title={known ? 'The historical window these metrics were computed over' : 'This row is not stamped with a backtest window'}
    >
      {known ? `${start} → ${end}` : 'window not recorded'}
    </span>
  )
}

// ── Board-level FDR (#1564) ─────────────────────────────────────────────────
// The cohort-relational half of a row's rigor story, and the only surface that
// carries it: the passport says whether a strategy is sound on its own
// evidence, this board says whether it stands out from the field once you
// price in that you are picking one strategy out of many. Both can be true at
// once; on the prod pull recorded in #1555 (2026-08-30) they disagreed on every
// curated row, nothing clearing the board-level threshold at any conventional
// level. That disagreement is exactly why this is rendered rather than hidden
// (owner: "render it as best we can"). What renders is whatever the payload
// says today — no state of the correction is hard-coded here.
//
// The honesty rules this block exists to hold, guarded in
// ui/test/leaderboard-board-fdr.test.js:
//   • `board_fdr_significant == null` means the correction did not run for
//     this row (no DSR confidence to correct). It renders an EM-DASH. It is
//     never rendered as "not significant", and no verdict word is ever
//     synthesised for it.
//   • The headline counts come from the payload's own `board_level_fdr`
//     block, never from a literal and never recounted off the visible rows —
//     the visible rows are filtered and paged, the correction is not.
const BOARD_FDR_NOT_DISTINGUISHABLE = 'Not yet distinguishable from selection noise at board level'
const BOARD_FDR_ABSENT = '—'

// BOARD-FDR-CELL:BEGIN — the per-row renderer. The guard slices on these
// sentinels; renaming or dropping one must fail loudly there rather than
// silently widen what gets scanned.

function boardFdrTitle(entry, fdrLevel) {
  const alpha = fdrLevel != null ? `α=${fdrLevel}` : 'the board α'
  if (entry.board_fdr_significant == null) {
    return 'Not corrected: this row has no DSR confidence for the board-level correction to act on.'
  }
  const adjusted = entry.board_fdr_adjusted_p != null
    ? ` BH-adjusted p=${formatMetric('board_fdr_adjusted_p', entry.board_fdr_adjusted_p, { row: entry, digits: 3, surface: 'Leaderboard board-FDR title' }).label}.`
    : ''
  return entry.board_fdr_significant
    ? `Clears the board-level Benjamini–Hochberg correction at ${alpha}.${adjusted} Advisory — it does not change the rigor-gate badge.`
    : `${BOARD_FDR_NOT_DISTINGUISHABLE} (Benjamini–Hochberg, ${alpha}).${adjusted} This does not mean the strategy is unsound — the rigor gate answers that separately.`
}

function BoardFdrCell({ entry, fdrLevel }) {
  const title = boardFdrTitle(entry, fdrLevel)
  if (entry.board_fdr_significant == null) {
    return <span style={{ color: 'var(--text-3)' }} title={title}>{BOARD_FDR_ABSENT}</span>
  }
  return (
    <span title={title}>
      {entry.board_fdr_significant
        ? <span className="tag-positive">Clears</span>
        : <span className="tag-muted">Not distinguishable</span>}
      {entry.board_fdr_adjusted_p != null && (
        <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>
          adj p <MetricValue metric="board_fdr_adjusted_p" value={entry.board_fdr_adjusted_p} row={entry} digits={3} surface="Leaderboard board-FDR" />
        </div>
      )}
    </span>
  )
}
// BOARD-FDR-CELL:END

function rigorBadge(entry) {
  if (entry.is_backtest_placeholder) {
    return <span className="tag-muted" title="No real backtest yet">No backtest</span>
  }
  if (entry.passes_rigor_gate) {
    return <span className="tag-positive" title="Passes the selection-bias rigor gate (DSR / PBO / OOS)">✓ Rigor gate</span>
  }
  return <span className="tag-warning" title="Did not pass the rigor gate — surfaced honestly">Gate failed</span>
}

// Compact stacked bar of the four real score components, each scaled by its weight.
function ScoreBar({ components, weights }) {
  if (!components || !weights) return null
  const parts = [
    { key: 'gate', color: 'var(--accent)', label: 'Rigor gate' },
    { key: 'dsr_confidence', color: '#4f9be0', label: 'DSR confidence' },
    { key: 'oos_performance', color: '#5fc08a', label: 'Out-of-sample' },
    { key: 'overfitting_resistance', color: '#b07fd0', label: 'Overfit-resistant' },
  ]
  return (
    <div style={{ display: 'flex', height: 6, borderRadius: 3, overflow: 'hidden', background: 'var(--surface-3)', width: 120 }}>
      {parts.map(p => {
        const w = (weights[p.key] ?? 0) * (components[p.key] ?? 0) * 100
        return <div key={p.key} title={`${p.label}: ${((components[p.key] ?? 0) * 100).toFixed(0)}% × weight ${weights[p.key]}`} style={{ width: `${w}%`, background: p.color }} />
      })}
    </div>
  )
}

export default function Leaderboard() {
  const { user } = useAuth()
  const [board, setBoard] = useState(RESEARCH_BOARD)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  // The forward board is a SEPARATE payload from a separate endpoint. Kept in
  // its own state deliberately: there is no shape in which a research entry
  // could stand in for a live row, so there must be no variable either one
  // could be read out of by accident.
  const [liveData, setLiveData] = useState(null)
  const [liveLoading, setLiveLoading] = useState(false)
  const [liveError, setLiveError] = useState(null)
  const [sortBy, setSortBy] = useState('conviction_score')
  const [order, setOrder] = useState('desc')
  const [minRigor, setMinRigor] = useState(false)
  const [regime, setRegime] = useState('')
  // null = let the backend pick the default (own if signed in, else curated).
  // Explicit only when the caller toggles — see the scope switch below.
  const [scopeParam, setScopeParam] = useState(null)

  // If auth resolves (or the user signs in/out) while this page is open, drop
  // back to the server default rather than keep stale explicit scope — e.g. a
  // visitor who toggled to "Curated" pre-login should not stay stuck there
  // post-login for no reason.
  useEffect(() => { setScopeParam(null) }, [user?.id])

  const load = useCallback(() => {
    setLoading(true)
    const params = new URLSearchParams({ sort_by: sortBy, order, limit: '100' })
    if (minRigor) params.set('min_rigor', 'true')
    if (regime) params.set('regime_tag', regime)
    if (scopeParam) params.set('scope', scopeParam)
    apiGet(`/api/leaderboard?${params.toString()}`)
      .then(d => { setData(d); setError(null) })
      .catch(e => setError(e.message || 'Failed to load leaderboard'))
      .finally(() => setLoading(false))
  }, [sortBy, order, minRigor, regime, scopeParam])

  useEffect(() => { load() }, [load])

  const loadLive = useCallback(() => {
    setLiveLoading(true)
    apiGet('/api/leaderboard/live-paper?limit=100')
      .then(d => { setLiveData(d); setLiveError(null) })
      .catch(e => setLiveError(e.message || 'Failed to load the live paper board'))
      .finally(() => setLiveLoading(false))
  }, [])

  // Fetched only when the forward tab is actually opened, and re-fetched when
  // the session changes (the board is owner-scoped, so signing in/out changes
  // what it legitimately contains).
  useEffect(() => {
    if (board === LIVE_BOARD) loadLive()
  }, [board, loadLive, user?.id])

  const engine = data?.scoring_engine
  const sb = engine?.stockbench_global
  // Board-level BH-FDR block, straight off the payload (#1564). Read here and
  // nowhere else so no part of the page can recount it off the filtered/paged
  // rows: the correction runs over the whole board cohort on purpose (a
  // smaller cohort makes every row look MORE significant), so a count derived
  // from what happens to be on screen would be a different, false number.
  const boardFdr = data?.board_level_fdr
  // The scope actually served (may differ from scopeParam — an anonymous
  // request for "own" is transparently served "curated"). This, not the
  // request param, is the source of truth for labeling the page.
  const servedScope = data?.scope ?? (user ? 'own' : 'curated')
  const isOwn = servedScope === 'own'

  // Selectivity headline (WP-7, docs/sprint/a6-rerun.md's rejection-rate item):
  // "of N strategies graded, K clear the bar" — derived at render time from
  // the SAME `data.entries` array the table below renders, never a hard-coded
  // count (CLAUDE.md: "Don't quote a curated-library strategy pass count —
  // anywhere"; live-derived is the one permitted source). Counts stay `null`
  // until `data` arrives, so the loading/empty states below never render a
  // zero-over-zero claim.
  // Placeholder rows (generated, no backtest yet) have NOT been graded — a
  // row the gate never saw cannot appear in a "graded" count in either
  // direction. "Claims must be true" is about literal truth, not about which
  // way an error would lean.
  const gradedEntries = data ? data.entries.filter((e) => !e.is_backtest_placeholder) : null
  const evaluatedCount = gradedEntries ? gradedEntries.length : null
  const passingCount = gradedEntries ? gradedEntries.filter((e) => e.passes_rigor_gate).length : null

  const isResearch = board === RESEARCH_BOARD
  const isLive = board === LIVE_BOARD
  // The forward board's rows, read ONLY out of its own payload. Nothing below
  // may derive a live row from `data` — see the header comment.
  const liveEntries = liveData?.entries ?? []

  return (
    <div className="leaderboard-page" style={{ maxWidth: 1100 }}>
      <div style={{ marginBottom: 18 }}>
        <header className="app-page-heading">
          <p className="app-eyebrow">Transparent ranking</p>
          <h1>{isOwn ? 'Your strategy leaderboard' : 'Strategy leaderboard'}</h1>
        </header>

        {/* Board switch. Two surfaces, two bases, never averaged — the labels
            say which is which before a single number is read. */}
        <div role="tablist" aria-label="Leaderboard board" style={{ display: 'flex', gap: 6, margin: '10px 0 12px' }}>
          {BOARDS.map(b => (
            <button
              key={b.id}
              type="button"
              role="tab"
              aria-selected={board === b.id}
              onClick={() => setBoard(b.id)}
              className={`tag-tab ${board === b.id ? 'tag-accent' : 'tag-muted'}`}
              style={{ cursor: 'pointer', border: 'none', borderRadius: 14, fontSize: 12 }}
            >
              {b.label}
            </button>
          ))}
        </div>

        {isResearch && (
          <p className="body" style={{ maxWidth: 760 }}>
            {isOwn
              ? <>Your strategies, ranked against each other by a transparent <strong>conviction score</strong> built
                  from real rigor-gate and backtest results — the ugly numbers included. Build your track record now;
                  paper deployments record it forward on Arc testnet, with no real funds.</>
              : <>The curated seed library, ranked by a transparent <strong>conviction score</strong> built from real
                  rigor-gate and backtest results — the ugly numbers included. A reference set, not a competition.</>}
            {' '}
            <strong>Every figure on this tab is backtest-era</strong> — measured on history the strategy was fitted
            and graded against, over the window stamped on each row. Nothing here is forward performance.
          </p>
        )}

        {isLive && (
          <p className="body" style={{ maxWidth: 760 }}>
            Strategies you have <strong>actually deployed to paper trading</strong>, ranked by the return each one has
            realised since it went live — compounded from its append-only forward ledger, never annualised and never
            mixed with a backtest number. A deployment that has not produced an observation yet is withheld rather
            than shown at zero.
          </p>
        )}

        {/* Headline selectivity aggregate — see the derivation comment above
            (evaluatedCount / passingCount). Guarded identically to the
            table's non-empty state further down so a zero-over-zero claim
            never renders while loading, on error, degraded, or before any
            strategies exist. */}
        {isResearch && !loading && !error && data && !data.degraded && evaluatedCount > 0 && (
          <div
            role="status"
            style={{
              marginTop: 10,
              padding: '10px 14px',
              borderLeft: '3px solid var(--accent)',
              background: 'var(--accent-muted)',
              borderRadius: 4,
              fontSize: 14,
              color: 'var(--text-1)',
            }}
          >
            {isOwn ? (
              <>
                Of your <strong>{evaluatedCount}</strong> strateg{evaluatedCount === 1 ? 'y' : 'ies'} graded by
                the rigor gate, <strong>{passingCount}</strong> clear{passingCount === 1 ? 's' : ''} the bar.
              </>
            ) : (
              <>
                Of <strong>{evaluatedCount}</strong> library strateg{evaluatedCount === 1 ? 'y' : 'ies'} graded by
                the rigor gate, <strong>{passingCount}</strong> clear{passingCount === 1 ? 's' : ''} the bar.
              </>
            )}
          </div>
        )}

        {!user && (
          <div
            role="status"
            style={{
              marginTop: 10,
              padding: '10px 14px',
              borderLeft: '3px solid var(--accent)',
              background: 'var(--accent-muted)',
              borderRadius: 4,
              fontSize: 13,
              color: 'var(--text-2)',
              display: 'flex',
              flexWrap: 'wrap',
              gap: 8,
              alignItems: 'center',
            }}
          >
            <span>
              {isLive ? (
                <>
                  <strong style={{ color: 'var(--accent)' }}>Sign in to see your live paper trades.</strong>{' '}
                  A paper track record is private to the account that deployed it, so there is nothing to show
                  here while signed out — this board never displays someone else's deployments.
                </>
              ) : (
                <>
                  <strong style={{ color: 'var(--accent)' }}>Sign in to rank your strategies.</strong>{' '}
                  What you're seeing below is the curated library — reference rows, not strategies you're
                  competing against.
                </>
              )}
            </span>
            <a
              className="btn-primary"
              style={{ marginLeft: 'auto', flexShrink: 0 }}
              href={`/sign-in?next=${encodeURIComponent(`${window.location.pathname}${window.location.search}`)}`}
            >
              Sign in
            </a>
          </div>
        )}

        {isResearch && user && (
          <div style={{ marginTop: 10, display: 'flex', gap: 6, alignItems: 'center' }}>
            <button type="button" onClick={() => setScopeParam('own')}
              className={`tag-tab ${isOwn ? 'tag-accent' : 'tag-muted'}`}
              style={{ cursor: 'pointer', border: 'none', borderRadius: 14, fontSize: 12 }}>
              My strategies
            </button>
            <button type="button" onClick={() => setScopeParam('curated')}
              className={`tag-tab ${!isOwn ? 'tag-accent' : 'tag-muted'}`}
              style={{ cursor: 'pointer', border: 'none', borderRadius: 14, fontSize: 12 }}
              title="The curated seed library — reference rows, not strategies you're competing against">
              Curated library (reference)
            </button>
          </div>
        )}
        {/* The broad provisional-data banner that lived here named TWO defects.
            The routing defect (#1203) is retired everywhere: the post-fix
            re-run landed fresh rows for every curated strategy (verified
            against prod 2026-08-19 — backtest_results max created_at
            2026-08-20 03:28 UTC; zero curated strategies stale). The
            backtest/live interpreter divergence is fully retired as of
            2026-08-20: F1/F4–F10 landed earlier, and F2 (live position FSM)
            + F3 (rebalance cadence) landed via PR #1320 — parity-pinned
            per-bar in test_interpreter_parity.py — and were verified running
            on the redeployed agent runner, so the old divergence clause here
            was removed (its claim became false). The ONE remaining residual:
            the own view still holds July-era rows for older generated
            strategies that predate the corrections. Those numbers are FIXED
            at generation time — no re-backtest loop exists for generated
            strategies (#1365; the prior claim of a backtest-cycle refresh was
            false and is retired) — hence the single-caveat banner below,
            scoped to the own view; curated rows are reference-only backtests,
            all verified fresh. */}
        {isResearch && isOwn && (
          <div
            role="status"
            style={{
              marginTop: 10,
              padding: '10px 14px',
              borderLeft: '3px solid var(--warning, #b45309)',
              background: 'var(--warning-bg, rgba(180,83,9,0.10))',
              borderRadius: 4,
              fontSize: 13,
              color: 'var(--text-2)',
            }}
          >
            <strong style={{ color: 'var(--warning, #b45309)' }}>One caveat on your figures.</strong>{' '}
            These numbers are <strong>fixed at generation time</strong> — a strategy generated
            before the August engine corrections keeps the numbers it was scored with. Generated
            strategies are not re-backtested; generate again to get a strategy scored by the
            current engine.
          </div>
        )}
        {isResearch && engine?.disclaimer && (
          <div style={{ marginTop: 10, padding: '8px 12px', borderLeft: '3px solid var(--accent)', background: 'var(--accent-muted)', borderRadius: 4, fontSize: 13, color: 'var(--text-2)' }}>
            <strong style={{ color: 'var(--accent)' }}>Testnet — paper/simulated.</strong> {engine.disclaimer}
          </div>
        )}

        {isLive && liveData?.disclaimer && (
          <div style={{ marginTop: 10, padding: '8px 12px', borderLeft: '3px solid var(--accent)', background: 'var(--accent-muted)', borderRadius: 4, fontSize: 13, color: 'var(--text-2)' }}>
            <strong style={{ color: 'var(--accent)' }}>Testnet — paper/simulated.</strong> {liveData.disclaimer}
          </div>
        )}
      </div>

      {/* RESEARCH-BOARD:BEGIN — everything between these sentinels renders the
          BACKTEST board and reads from `data` only. The guard in
          ui/test/leaderboard-boards.test.js slices on them. */}
      {isResearch && (
      <>
      {/* Scoring engine: weights + the one-line methodology sentence + the one
          real StockBench datum, as honest context. The methodology line is the
          board's own statement of how conviction is computed — it comes from
          the API's engine metadata, never restated by hand here, so it cannot
          drift from the formula the backend actually ran. */}
      {engine && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, marginBottom: 18, padding: 14, background: 'var(--surface-2)', borderRadius: 8, border: '1px solid var(--glass-border)' }}>
          <div style={{ flex: '1 1 320px' }}>
            <div style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.5, color: 'var(--text-3)', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 6 }}>
              How this board is scored <BasisBadge basis={data?.performance_basis} />
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-2)' }}>{engine.methodology}</div>
          </div>
          <div style={{ flex: '1 1 260px' }}>
            <div style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.5, color: 'var(--text-3)', marginBottom: 6 }}>Benchmark context</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
              Realised forward results now have their own tab — see <strong>Live paper trading</strong>; they are never
              folded into the score above. <strong>StockBench</strong> today is a single whole-pipeline run (honest, not
              per-strategy):{' '}
              {sb && <span title={`${sb.window} · ${sb.source}`}>Sortino {fmt(sb.sortino)}, return {sb.return_pct}%, rank {sb.rank}</span>}.
            </div>
          </div>
          {/* BOARD-FDR-SUMMARY:BEGIN — the board-level headline. Rendered
              whenever there is a cohort to correct; suppressed only when
              nothing was tested, because a correction over an empty cohort has
              nothing to say. Every number in it comes from the payload's own
              `board_level_fdr` block — see the `boardFdr` comment above for
              why it must not be recounted off the visible rows. */}
          {boardFdr && boardFdr.n_tested > 0 && (
            <div style={{ flex: '1 1 100%', borderTop: '1px solid var(--glass-border)', paddingTop: 12 }}>
              <div style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.5, color: 'var(--text-3)', marginBottom: 6 }}>
                Board-level correction
              </div>
              <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
                {boardFdr.n_significant === 0 ? (
                  <>
                    <strong>{BOARD_FDR_NOT_DISTINGUISHABLE}.</strong> None of the{' '}
                    <strong>{boardFdr.n_tested}</strong> strateg{boardFdr.n_tested === 1 ? 'y' : 'ies'} on this board
                    with a DSR to correct clears the correction. That is a statement about the field, not a verdict
                    on any one strategy — the rigor gate answers that separately, per row.
                  </>
                ) : (
                  <>
                    <strong>{boardFdr.n_significant}</strong> of the <strong>{boardFdr.n_tested}</strong> strateg
                    {boardFdr.n_tested === 1 ? 'y' : 'ies'} on this board with a DSR to correct{' '}
                    {boardFdr.n_significant === 1 ? 'is' : 'are'} distinguishable from selection noise at board level;
                    the rest are not.
                  </>
                )}{' '}
                <span style={{ color: 'var(--text-3)' }}>{boardFdr.methodology}</span>
              </div>
            </div>
          )}
          {/* BOARD-FDR-SUMMARY:END */}
        </div>
      )}

      {/* Controls */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, alignItems: 'center', marginBottom: 14 }}>
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>Sort</span>
        {SORT_OPTIONS.map(o => (
          <button key={o.id} type="button" onClick={() => setSortBy(o.id)}
            className={`tag-tab ${sortBy === o.id ? 'tag-accent' : 'tag-muted'}`}
            style={{ cursor: 'pointer', border: 'none', borderRadius: 14, fontSize: 12 }}>
            {o.label}
          </button>
        ))}
        <button type="button" onClick={() => setOrder(o => o === 'desc' ? 'asc' : 'desc')}
          className="tag-tab tag-muted" style={{ cursor: 'pointer', border: 'none', borderRadius: 14, fontSize: 12 }}
          title="Toggle sort direction">
          {order === 'desc' ? '↓ desc' : '↑ asc'}
        </button>
        <span style={{ width: 1, height: 18, background: 'var(--glass-border)', margin: '0 4px' }} />
        <label style={{ fontSize: 12, color: 'var(--text-2)', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 4 }}>
          <input type="checkbox" checked={minRigor} onChange={e => setMinRigor(e.target.checked)} /> Rigor-gated only
        </label>
        <select value={regime} onChange={e => setRegime(e.target.value)}
          style={{ background: 'var(--surface-3)', color: 'var(--text-2)', border: '1px solid var(--glass-border)', borderRadius: 6, padding: '4px 8px', fontSize: 12 }}>
          {REGIMES.map(r => <option key={r.id} value={r.id}>{r.label}</option>)}
        </select>
        {data && (
          <span style={{ fontSize: 12, color: 'var(--text-3)', marginLeft: 'auto' }}>
            {data.degraded ? '—' : `${data.total} strateg${data.total === 1 ? 'y' : 'ies'}`}
          </span>
        )}
      </div>

      {loading && <div className="body" style={{ color: 'var(--text-3)' }}>Loading the board…</div>}
      {error && <div className="tag-warning" style={{ display: 'inline-block', padding: '6px 10px' }}>Couldn’t load the leaderboard: {error}</div>}

      {/* A degraded board (#1356: the strategy provider raised, or the
          curated cohort came back empty for a reason other than a
          legitimate filter — e.g. the corpus missing from the build) is a
          200 with intact scoring-engine metadata, not an `error` — so it
          must be surfaced here, and it must pre-empt BOTH honest-empty
          messages below, which claim something specific ("you haven't
          generated any" / "no strategies match these filters") that is not
          what actually happened. */}
      {!loading && !error && data?.degraded && (
        <div role="status" className="tag-warning" style={{ display: 'block', padding: '10px 14px', marginBottom: 14, borderRadius: 4 }}>
          <strong>Board data is degraded.</strong>{' '}
          {data.degraded_reason || 'Some strategies could not be loaded.'}{' '}
          <button type="button" className="btn btn-sm btn-outline" onClick={load} style={{ marginLeft: 4 }}>
            Retry
          </button>
        </div>
      )}

      {!loading && !error && data && !data.degraded && data.entries.length === 0 && isOwn && (
        <div className="body" style={{ color: 'var(--text-3)', padding: 20, textAlign: 'center', border: '1px dashed var(--glass-border)', borderRadius: 8 }}>
          You haven't generated any strategies yet.{' '}
          <a href="/app/generate" style={{ color: 'var(--accent)' }}>Generate one</a>, or{' '}
          <button type="button" onClick={() => setScopeParam('curated')}
            style={{ background: 'none', border: 'none', padding: 0, color: 'var(--accent)', cursor: 'pointer', textDecoration: 'underline', font: 'inherit' }}>
            browse the curated library
          </button> for reference.
        </div>
      )}

      {!loading && !error && data && !data.degraded && data.entries.length === 0 && !isOwn && (
        <div className="body" style={{ color: 'var(--text-3)', padding: 20, textAlign: 'center', border: '1px dashed var(--glass-border)', borderRadius: 8 }}>
          No strategies match these filters yet.
        </div>
      )}

      {!loading && !error && data && data.entries.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--text-3)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5 }}>
                <th style={{ padding: '8px 10px' }}>#</th>
                <th style={{ padding: '8px 10px' }}>Strategy</th>
                <th style={{ padding: '8px 10px' }}>Conviction</th>
                <th style={{ padding: '8px 10px' }}>Sharpe</th>
                <th style={{ padding: '8px 10px' }}>CAGR</th>
                <th style={{ padding: '8px 10px' }}>Max DD</th>
                <th style={{ padding: '8px 10px' }}>Rigor</th>
                <th style={{ padding: '8px 10px' }} title="Deflated Sharpe Ratio — Sharpe adjusted for selection bias / multiple testing">DSR</th>
                <th
                  style={{ padding: '8px 10px' }}
                  title="Board-level Benjamini–Hochberg FDR: does this row stand out from the whole board once the multiple testing of picking one strategy out of many is priced in? Advisory — it never changes the rigor-gate badge."
                >
                  Board FDR
                </th>
              </tr>
            </thead>
            <tbody>
              {data.entries.map(e => (
                <tr key={e.id} style={{ borderTop: '1px solid var(--glass-border)' }}>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap', fontWeight: 600 }}>
                    {e.medal ? <span style={{ marginRight: 4 }}>{MEDAL[e.medal]}</span> : null}{e.rank}
                  </td>
                  <td style={{ padding: '10px', maxWidth: 280 }}>
                    <div style={{ color: 'var(--text-1)', fontWeight: 500 }}>{e.name}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                      {isOwn
                        // Every row here is the caller's own by construction (server-scoped) —
                        // `creator` is curator_wallet provenance, not ownership, and is often
                        // unset for generated strategies, so it's redundant/misleading here.
                        ? 'Your strategy'
                        : (e.creator === 'Archimedes' ? 'Archimedes (curated)' : `by ${e.creator.slice(0, 6)}…${e.creator.slice(-4)}`)}
                      {e.regime_tag && e.regime_tag !== 'regime_neutral' ? ` · ${e.regime_tag}` : ''}
                    </div>
                    {/* Per-row basis + measurement window. Every number in this
                        row was produced over exactly this span of history; a
                        row that never recorded one says so rather than
                        implying the numbers are current. */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
                      <BasisBadge basis={e.performance_basis} />
                      <WindowLabel start={e.backtest_start} end={e.backtest_end} />
                    </div>
                  </td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>
                    <div style={{ fontWeight: 600, color: 'var(--accent)' }}>
                      <MetricValue metric="conviction_score" value={e.conviction_score} row={e} surface="Leaderboard" />
                    </div>
                    <ScoreBar components={e.score_components} weights={engine?.weights} />
                  </td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>
                    <MetricValue metric="sharpe_ratio" value={e.sharpe_ratio} row={e} surface="Leaderboard" />
                  </td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>
                    <MetricValue metric="cagr" value={e.cagr} row={e} surface="Leaderboard" />
                  </td>
                  {/* The Math.abs() that used to sit here was a second way to
                      render an impossible drawdown: a SIGNED value of −0.20 —
                      the wire contract says positive fraction — came out as
                      "−20.0%", a plausible-looking number manufactured from a
                      contract violation. MetricValue clamps it to the 0 bound
                      and says the reported figure was out of range (#1651). */}
                  <td style={{ padding: '10px', whiteSpace: 'nowrap', color: 'var(--negative)' }}>
                    <MetricValue metric="max_drawdown" value={e.max_drawdown} row={e} surface="Leaderboard" />
                  </td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>
                    {rigorBadge(e)}
                    {(e.dsr_p_value != null || e.pbo_score != null || e.out_of_sample_sharpe != null) && (
                      <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }} title="DSR confidence (0–1, higher is better): probability the Sharpe survives deflation/multiple-testing. Not a classical p-value. OOS = out-of-sample Sharpe.">
                        {[
                          e.dsr_p_value != null && (
                            <span key="dsr">DSR conf=<MetricValue metric="dsr_p_value" value={e.dsr_p_value} row={e} surface="Leaderboard" /></span>
                          ),
                          e.pbo_score != null && (
                            <span key="pbo">PBO <MetricValue metric="pbo_score" value={e.pbo_score} row={e} surface="Leaderboard" /></span>
                          ),
                          e.out_of_sample_sharpe != null && (
                            <span key="oos">OOS <MetricValue metric="out_of_sample_sharpe" value={e.out_of_sample_sharpe} row={e} surface="Leaderboard" /></span>
                          ),
                        ]
                          .filter(Boolean)
                          .flatMap((node, i) => (i === 0 ? [node] : [<span key={`sep-${i}`}> · </span>, node]))}
                      </div>
                    )}
                  </td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>
                    <MetricValue metric="deflated_sharpe_ratio" value={e.deflated_sharpe_ratio} row={e} surface="Leaderboard" />
                  </td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>
                    <BoardFdrCell entry={e} fdrLevel={boardFdr?.fdr_level} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      </>
      )}
      {/* RESEARCH-BOARD:END */}

      {/* LIVE-BOARD:BEGIN — the FORWARD board. Everything between these
          sentinels reads from `liveData` and nothing else. Two rules the
          guard enforces by slicing on these markers:
            1. no reference to `data` (the research payload) — a research
               entry must never be able to stand in for a live row;
            2. no row may be rendered without ledger-derived fields, and the
               table only renders when liveEntries.length > 0 — the backend
               already withholds ledger-less deployments, and this block must
               not re-add them from any other source. */}
      {isLive && (
      <>
      {liveData && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, marginBottom: 18, padding: 14, background: 'var(--surface-2)', borderRadius: 8, border: '1px solid var(--glass-border)' }}>
          <div style={{ flex: '1 1 380px' }}>
            <div style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.5, color: 'var(--text-3)', marginBottom: 6, display: 'flex', alignItems: 'center', gap: 6 }}>
              How this board is measured <BasisBadge basis={liveData.performance_basis} />
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-2)' }}>{liveData.methodology}</div>
          </div>
          <div style={{ flex: '1 1 200px' }}>
            <div style={{ fontSize: 12, textTransform: 'uppercase', letterSpacing: 0.5, color: 'var(--text-3)', marginBottom: 6 }}>Ledger as of</div>
            <div style={{ fontSize: 13, color: 'var(--text-2)' }}>
              {liveData.as_of || 'no observations recorded yet'}
              {liveData.withheld_no_ledger > 0 && (
                <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>
                  Deployments awaiting their first observation and therefore not shown:{' '}
                  <strong>{liveData.withheld_no_ledger}</strong>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {liveLoading && <div className="body" style={{ color: 'var(--text-3)' }}>Loading the live paper board…</div>}
      {liveError && (
        <div className="tag-warning" style={{ display: 'inline-block', padding: '6px 10px' }}>
          Couldn’t load the live paper board: {liveError}
        </div>
      )}

      {!liveLoading && !liveError && liveData?.degraded && (
        <div role="status" className="tag-warning" style={{ display: 'block', padding: '10px 14px', marginBottom: 14, borderRadius: 4 }}>
          <strong>Forward board data is degraded.</strong>{' '}
          {liveData.degraded_reason || 'The paper ledger could not be read.'}{' '}
          <button type="button" className="btn btn-sm btn-outline" onClick={loadLive} style={{ marginLeft: 4 }}>
            Retry
          </button>
        </div>
      )}

      {/* The honest empty state. A thin or empty ledger is not an error and
          not a degradation — it is the true statement that nothing has traded
          forward yet. It must never be replaced by a zeroed row, and it is
          pre-empted by the degraded banner above, which is a different claim. */}
      {!liveLoading && !liveError && liveData && !liveData.degraded && liveEntries.length === 0 && (
        <div className="body" style={{ color: 'var(--text-3)', padding: 20, textAlign: 'center', border: '1px dashed var(--glass-border)', borderRadius: 8 }}>
          {liveData.scope === 'anonymous' ? (
            <>This board ranks your own live paper deployments — sign in to see yours.</>
          ) : (
            <>
              {LIVE_EMPTY_MESSAGE}.
              {liveData.scope === 'own' && (
                <>
                  {' '}Deploy one to paper trading from its strategy page, and its forward track record starts here the
                  day it produces its first observation.
                </>
              )}
            </>
          )}
        </div>
      )}

      {!liveLoading && !liveError && liveEntries.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--text-3)', fontSize: 11, textTransform: 'uppercase', letterSpacing: 0.5 }}>
                <th style={{ padding: '8px 10px' }}>#</th>
                <th style={{ padding: '8px 10px' }}>Strategy</th>
                <th style={{ padding: '8px 10px' }} title="Compounded from every observation in the deployment's forward ledger">Return since inception</th>
                <th style={{ padding: '8px 10px' }} title="Ledger observations appended so far — not calendar days since inception">Observations</th>
                <th style={{ padding: '8px 10px' }}>Inception</th>
                <th style={{ padding: '8px 10px' }} title="The date of the last ledger observation — what the return reflects">As of</th>
                <th style={{ padding: '8px 10px' }}>Ledger</th>
              </tr>
            </thead>
            <tbody>
              {liveEntries.map(row => (
                <tr key={row.deployment_id} style={{ borderTop: '1px solid var(--glass-border)' }}>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap', fontWeight: 600 }}>{row.rank}</td>
                  <td style={{ padding: '10px', maxWidth: 280 }}>
                    <div style={{ color: 'var(--text-1)', fontWeight: 500 }}>{row.name}</div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
                      <BasisBadge basis={row.performance_basis} />
                      <span style={{ fontSize: 11, color: 'var(--text-3)' }}>
                        since {row.inception_date}
                      </span>
                    </div>
                  </td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>
                    <div style={{ fontWeight: 600, color: row.cumulative_return >= 0 ? 'var(--accent)' : 'var(--negative)' }}>
                      <MetricValue metric="cumulative_return" value={row.cumulative_return} row={row} surface="Leaderboard live" />
                    </div>
                    <div style={{ fontSize: 11, color: 'var(--text-3)' }}>realised, not annualised</div>
                    {/* The verdict of record, on the same cell as the number it
                        qualifies (#1764). Unconditional, for the same reason it
                        is unconditional on /app/paper: deploy is at will, so a
                        gate-REJECTED strategy can sit on this board, and its
                        realised return standing alone would read as an
                        endorsement. `gateVerdict` has a state for every payload
                        — including one that carried no verdict — so there is no
                        input for which drawing nothing here is correct. */}
                    <GateVerdictChip dep={row} style={{ marginTop: 4, fontSize: 11, maxWidth: 220 }} />
                  </td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>{row.days_live}</td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>{row.inception_date}</td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>{row.as_of}</td>
                  <td style={{ padding: '10px', whiteSpace: 'nowrap' }}>
                    {row.drift_detected
                      ? <span className="tag-warning" title="A fresh replay disagreed with rows already written. The ledger is append-only and was NOT rewritten — the discrepancy is surfaced, not hidden.">Drift flagged</span>
                      : <span className="tag-muted" title="Append-only forward ledger; no replay disagreement recorded">Append-only</span>}
                    {row.last_updated && (
                      <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }} title="When the last ledger row was appended">
                        updated {String(row.last_updated).slice(0, 10)}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      </>
      )}
      {/* LIVE-BOARD:END */}
    </div>
  )
}
