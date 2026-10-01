import { useCallback, useEffect, useRef, useState } from 'react'
import { apiGet, apiPost } from '../api'
import GateVerdictChip from './GateVerdictChip'
import {
  DEPLOY_AT_WILL_NOTE,
  FORWARD_EVIDENCE_NOTE,
  MARK_BASIS_DISCLOSURE,
  driftTooltip,
  formatTotalReturn,
  markAnnouncement,
  markBasisNote,
  markLabel,
  marksStalenessNote,
  marksUnavailableNote,
  newestMark,
  noMarksNote,
  paperCadenceCopy,
  paperErrorMessage,
  paperReturnAnnouncement,
} from '../paperCopy'

// How often the live value is refetched. A 15-minute mark cadence does not
// justify an SSE channel — the generation stream already cost this repo one
// reproducible drop-under-load incident (#891), and a once-per-quarter-hour
// number is not worth re-opening that surface. Polling at a third of the
// cadence means a new mark shows up within ~5 minutes of being written.
const MARKS_POLL_MS = 5 * 60 * 1000

// /app/paper — the act-on step of the MVP spine (generate → verdict → paper).
// Lists the signed-in account's paper deployments from GET /api/paper/deployments
// (deployment_summary shape: deployment_id, strategy_id, deployed_at, status,
// days, total_return, drift_detected_at, rigor_gate_status, graded_at,
// gate_version, series[{date, daily_return, equity_index}]). Deployments are
// SIMULATED — account-owned, no wallet, no funds — and free by design (Dan's
// call: paper stays free even after the generation paywall flips). Strategy display names come from a client-side
// join against the library lists; the paper API deliberately returns ids only.
//
// The DRIFT tooltip, the total-return figure, the intraday mark labels, and
// error-message rendering are pure functions in ../paperCopy — unit-tested
// there (#1362) so this component never re-fabricates a freeze that doesn't
// happen, a measured look for an unmeasured day-0 ledger, a bare intraday
// number with no as-of time, or a raw "Backend returned NNN".
//
// Two series, two lifetimes, and the card must never let them blur:
//   - `series` (paper_daily_returns) is the SETTLED, append-only paper track
//     record — Arc testnet, no real funds (#1807);
//   - the intraday marks from GET /api/paper/deployments/{id}/marks are an
//     UNSETTLED re-pricing of that strategy's ASSET BASKET — not of its live
//     position, which v1 cannot see (MARK_BASIS_DISCLOSURE) — and the backend
//     deletes them past 90 days. They are polled, drawn as a dashed tail, and
//     always labelled with their as-of time.

function nameOf(row) {
  return row?.strategy_name || row?.name || row?.paper_title || null
}

function StatusChip({ status, driftAt }) {
  const active = status === 'active'
  return (
    <span style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
      <span
        style={{
          padding: '2px 10px',
          borderRadius: 999,
          fontSize: 12,
          fontFamily: 'var(--mono, monospace)',
          background: active ? 'var(--accent-muted)' : 'var(--surface-3)',
          color: active ? 'var(--accent)' : 'var(--text-3)',
          border: `1px solid ${active ? 'var(--accent)' : 'var(--border, var(--surface-3))'}`,
        }}
      >
        {active ? 'ACTIVE' : 'STOPPED'}
      </span>
      {driftAt && (
        <span
          title={driftTooltip(driftAt, status)}
          style={{
            padding: '2px 10px',
            borderRadius: 999,
            fontSize: 12,
            fontFamily: 'var(--mono, monospace)',
            background: 'var(--surface-3)',
            color: 'var(--warning, #b45309)',
            border: '1px solid var(--warning, #b45309)',
          }}
        >
          DRIFT
        </span>
      )}
    </span>
  )
}

// Minimal equity sparkline over series[].equity_index, with the UNSETTLED
// intraday tail (paper_marks[].portfolio_value) drawn as a separate dashed,
// half-weight segment. The visual break is load-bearing, not decoration: only
// the settled daily ledger is the recorded track record, so a reader has to
// be able to see at a glance where that record ends and the intraday view
// begins. Starts the path at the 1.0 baseline so day-1 deployments still draw
// a meaningful segment.
function Sparkline({ series, intraday }) {
  if (!series || series.length === 0) return null
  const settled = [1.0, ...series.map((p) => p.equity_index)]
  const tail = (intraday || []).map((m) => m.portfolio_value).filter((v) => v != null && !Number.isNaN(v))
  const values = [...settled, ...tail]
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const W = 220
  const H = 48
  const x = (i) => (values.length === 1 ? 2 : (i / (values.length - 1)) * (W - 4) + 2)
  const y = (v) => H - 6 - ((v - min) / span) * (H - 12)
  const pointsFor = (vals, offset) => vals.map((v, i) => `${x(i + offset).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const up = values[values.length - 1] >= values[0]
  const stroke = up ? 'var(--accent)' : 'var(--negative)'
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} aria-hidden="true">
      <polyline points={pointsFor(settled, 0)} fill="none" stroke={stroke} strokeWidth="1.5" />
      {tail.length > 0 && (
        // Joined to the last settled point so the tail reads as a continuation,
        // then immediately distinguished by weight + dash.
        <polyline
          points={pointsFor([settled[settled.length - 1], ...tail], settled.length - 1)}
          fill="none"
          stroke={stroke}
          strokeWidth="1"
          strokeDasharray="2 2"
          opacity="0.7"
        />
      )}
    </svg>
  )
}

// The live value line under the settled total return. Four states, and only
// one of them shows a number:
//
//   1. a mark exists          -> "+0.42% · as of 14:45 UTC · delayed", plus a
//                                staleness note once it stops moving;
//   2. the marks fetch failed -> says so, and shows NO number (never the last
//                                one it happened to hold);
//   3. no mark yet            -> an em-dash WITH a reason. Never "+0.00%" —
//                                that is the day-0 bug formatTotalReturn was
//                                extracted to fix, in a new place;
//   4. marks not loaded yet   -> nothing at all, so a card cannot flash an
//                                "unavailable" claim it has not established.
//
// State 1 also carries markBasisNote — the v1 limitation (marks re-price the
// asset BASKET and cannot see cash) rendered beside the number it qualifies,
// not only in the page intro. A number a reader scans to must arrive with its
// own caveat attached.
//
// The value's own colour is deliberately NOT the accent/negative pair the
// settled figure uses: an unsettled number should not be dressed like a
// settled one.
function LiveValue({ dep, marks, error }) {
  if (error) {
    return (
      <div className="caption" style={{ marginTop: 6, color: 'var(--text-3)' }}>
        {error}
      </div>
    )
  }
  if (marks === undefined) return null
  // `latest_mark` rides along on the deployment summary, so the value is
  // already correct on first paint; the polled list only refines it.
  const latest = marks.length > 0 ? marks[marks.length - 1] : dep.latest_mark
  if (!latest) {
    return (
      <div className="caption" style={{ marginTop: 6, color: 'var(--text-3)' }}>
        — {noMarksNote(dep.status)}
      </div>
    )
  }
  const stale = marksStalenessNote(latest)
  const basis = markBasisNote(latest)
  return (
    <div style={{ marginTop: 6 }}>
      <div
        className="caption"
        style={{ fontFamily: 'var(--mono, monospace)', fontVariantNumeric: 'tabular-nums', color: 'var(--text-2)' }}
      >
        <span className="sr-only">{markAnnouncement(latest)}</span>
        <span aria-hidden="true">{markLabel(latest)}</span>
      </div>
      <div className="caption" style={{ color: 'var(--text-3)' }}>
        live · unsettled
      </div>
      {basis && (
        // The v1 limitation, next to the number it qualifies — not only in the
        // page intro and not only in the docs. A reader who scans straight to
        // the figure must still be told the basket, not the position, is what
        // was re-priced. Full sentence in the title so the short form is never
        // the only version available.
        <div className="caption" style={{ color: 'var(--text-3)' }} title={MARK_BASIS_DISCLOSURE}>
          {basis}
        </div>
      )}
      {stale && (
        <div className="caption" style={{ color: 'var(--text-3)' }}>
          {stale}
        </div>
      )}
    </div>
  )
}

export default function PaperTrading({ onNavigate }) {
  const [deployments, setDeployments] = useState(null)
  const [names, setNames] = useState({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [stopping, setStopping] = useState(null)
  // Marks are kept in their OWN state, keyed by deployment id, and are never
  // merged into the deployment row: the settled ledger and the unsettled
  // intraday view have different lifetimes (the backend deletes marks past 90
  // days) and must never be confusable at the point of render.
  const [marks, setMarks] = useState({})
  const [marksErrors, setMarksErrors] = useState({})
  const deploymentIdsRef = useRef([])

  const load = useCallback(async () => {
    setError('')
    try {
      const res = await apiGet('/api/paper/deployments')
      const rows = res.deployments || []
      setDeployments(rows)
      deploymentIdsRef.current = rows.map((d) => d.deployment_id)
    } catch (e) {
      setError(paperErrorMessage(e, 'Failed to load paper deployments'))
      setDeployments([])
      return
    }
    // Name join is best-effort decoration — the list renders with ids if the
    // library calls fail, so these settle independently of the load above.
    // Each list is fetched at its endpoint's max (curated 100, generated 200 —
    // the defaults, 20/50, could miss names for older rows; anything beyond
    // the max falls back to its id honestly).
    const [seed, generated] = await Promise.allSettled([
      apiGet('/api/strategies/?limit=100'),
      apiGet('/api/strategies/generated?limit=200'),
    ])
    const map = {}
    for (const res of [seed, generated]) {
      if (res.status !== 'fulfilled') continue
      for (const row of res.value.strategies || []) {
        // id-before-strategy_id matches both list shapes today; keep this
        // order if a third list source is ever added (contract review, #1302).
        const id = row.id ?? row.strategy_id
        const label = nameOf(row)
        if (id && label) map[id] = label
      }
    }
    setNames(map)
  }, [])

  // Intraday marks, polled per deployment. Each deployment settles
  // independently: one deployment's marks endpoint failing must not blank the
  // others, and must not blank that deployment's SETTLED daily return either
  // — it is a partial failure, and the card keeps showing what it still knows.
  const loadMarks = useCallback(async () => {
    const ids = deploymentIdsRef.current
    if (ids.length === 0) return
    const results = await Promise.allSettled(
      ids.map((id) => apiGet(`/api/paper/deployments/${encodeURIComponent(id)}/marks?limit=200`)),
    )
    const nextMarks = {}
    const nextErrors = {}
    ids.forEach((id, i) => {
      const res = results[i]
      if (res.status === 'fulfilled') {
        nextMarks[id] = res.value.marks || []
      } else {
        // Deliberately do NOT carry the previous marks forward. A number the
        // last successful poll fetched, rendered under a live-looking label,
        // is a stale reading wearing a fresh timestamp — the same defect as
        // writing a duplicated stale row, moved into the UI.
        nextErrors[id] = marksUnavailableNote(res.reason)
      }
    })
    setMarks(nextMarks)
    setMarksErrors(nextErrors)
  }, [])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    if (deployments === null) return
    loadMarks()
    const timer = setInterval(loadMarks, MARKS_POLL_MS)
    return () => clearInterval(timer)
  }, [deployments, loadMarks])

  const stop = async (dep) => {
    const label = names[dep.strategy_id] || dep.strategy_id
    if (!window.confirm(`Stop paper trading "${label}"? The track record freezes where it is; this cannot be restarted in place.`)) return
    setStopping(dep.deployment_id)
    setNotice('')
    try {
      await apiPost(`/api/paper/deployments/${encodeURIComponent(dep.deployment_id)}/stop`, {})
      await load()
      // The success path used to be silent: load() re-renders, the chip flips
      // ACTIVE→STOPPED and the Stop button unmounts, so a screen-reader user
      // was left with no statement of what happened (4.1.3).
      setNotice(`Paper trading stopped for ${label}. The track record is frozen at ${dep.days} trading day${dep.days === 1 ? '' : 's'}.`)
    } catch (e) {
      setError(paperErrorMessage(e, 'Failed to stop deployment'))
    } finally {
      setStopping(null)
    }
  }

  // The intro's cadence copy is derived from the marks actually in the payload,
  // never asserted as a standing fact: no marks job is scheduled in infra/, so
  // a quarter-hourly cadence is only true where a fresh mark proves it (#1802).
  const cadence = paperCadenceCopy(newestMark(deployments, marks, marksErrors))

  return (
    <div style={{ maxWidth: 1100 }}>
      <div style={{ marginBottom: 18 }}>
        <h2 className="serif" style={{ fontSize: '2rem', marginBottom: 8 }}>
          Paper Trading
        </h2>
        <p className="body" style={{ maxWidth: 760 }}>
          Simulated deployments of your strategies — <strong>no funds move</strong>. Each one
          snapshots the strategy spec at deploy time and appends one real-data return per trading
          day; later regeneration of the strategy never rewrites a running ledger.
          {cadence.sentences.map((sentence) => (
            <span key={sentence}> {sentence}</span>
          ))}
        </p>
        {cadence.staleness && (
          <p className="caption" style={{ marginTop: 4 }}>
            {cadence.staleness}
          </p>
        )}
        <p className="caption" style={{ marginTop: 4 }}>
          {MARK_BASIS_DISCLOSURE}
        </p>
        {/* The deploy-at-will rule and its limit, stated on the page rather
            than only implied by the chips (#1764). */}
        <p className="caption" style={{ marginTop: 4 }}>
          {DEPLOY_AT_WILL_NOTE} {FORWARD_EVIDENCE_NOTE}
        </p>
      </div>

      {/* Mounted unconditionally so the message it later receives is actually
          announced — a live region created at the same moment as its content
          is routinely missed. */}
      <div role="status" aria-live="polite" className={notice ? 'caption' : 'sr-only'} style={notice ? { marginBottom: 14 } : undefined}>
        {notice}
      </div>

      {error && (
        <div role="alert" className="card" style={{ padding: 14, marginBottom: 14, color: 'var(--negative)' }}>
          {error}
        </div>
      )}

      {deployments === null && <p className="caption">Loading deployments…</p>}

      {deployments !== null && deployments.length === 0 && !error && (
        <div className="card" style={{ padding: 24, textAlign: 'center' }}>
          <p className="body" style={{ marginBottom: 12 }}>
            No paper deployments yet. Open a strategy in your Library and choose{' '}
            <strong>Start paper trading</strong> on its passport.
          </p>
          <button className="btn-primary" onClick={() => onNavigate('library')}>
            Open Library →
          </button>
        </div>
      )}

      {deployments !== null && deployments.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {deployments.map((dep) => (
            <div
              key={dep.deployment_id}
              className="card"
              style={{ padding: 16, display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center' }}
            >
              <div style={{ flex: '1 1 240px', minWidth: 0 }}>
                <button
                  className="btn-link"
                  style={{
                    fontWeight: 600,
                    fontSize: '1.05rem',
                    background: 'none',
                    border: 'none',
                    padding: 0,
                    cursor: 'pointer',
                    color: 'var(--text-1)',
                    textAlign: 'left',
                  }}
                  title="Open the strategy passport"
                  onClick={() => onNavigate('strategy', { strategyId: dep.strategy_id })}
                >
                  {names[dep.strategy_id] || dep.strategy_id}
                </button>
                <div className="caption" style={{ marginTop: 4 }}>
                  deployed {dep.deployed_at} ·{' '}
                  {dep.days === 0
                    ? 'day 0 — first return lands with the next daily advance'
                    : `${dep.days} trading day${dep.days === 1 ? '' : 's'}`}
                </div>
                <div style={{ marginTop: 8 }}>
                  <StatusChip status={dep.status} driftAt={dep.drift_detected_at} />
                </div>
              </div>

              <div style={{ flex: '0 0 auto' }}>
                <Sparkline series={dep.series} intraday={marks[dep.deployment_id]} />
              </div>

              <div style={{ flex: '0 0 auto', textAlign: 'right', minWidth: 110 }}>
                <div
                  style={{
                    fontFamily: 'var(--mono, monospace)',
                    fontSize: '1.3rem',
                    fontVariantNumeric: 'tabular-nums',
                    color:
                      dep.total_return > 0
                        ? 'var(--accent)'
                        : dep.total_return < 0
                          ? 'var(--negative)'
                          : 'var(--text-2)',
                  }}
                >
                  {/* The number and its verdict reach a screen reader as ONE
                      utterance, from one call (#1764). A visually adjacent chip
                      is not enough: the percentage would otherwise be announced
                      bare, which is the same claim-without-its-caveat defect
                      MARK_BASIS_DISCLOSURE was moved to the point of render to
                      fix. */}
                  <span className="sr-only">{paperReturnAnnouncement(dep)}</span>
                  <span aria-hidden="true">{formatTotalReturn(dep.total_return, dep.days)}</span>
                </div>
                <div className="caption" aria-hidden="true">
                  total return
                </div>
                {/* Unconditional. A performance number on this card is never
                    rendered without the gate verdict beside it — including when
                    the payload carried no verdict, which draws the explicit
                    "verdict unavailable" state rather than nothing.

                    `ariaHidden` because the figure's sr-only line above already
                    ends with this exact verdict, from the same call: without it
                    a screen reader hears the verdict twice per card. The chip
                    is the SIGHTED half of one statement, not a second one. */}
                <GateVerdictChip dep={dep} ariaHidden />
                <LiveValue dep={dep} marks={marks[dep.deployment_id]} error={marksErrors[dep.deployment_id]} />
                {dep.status === 'active' && (
                  <button
                    className="btn btn-outline btn-sm"
                    style={{ marginTop: 8 }}
                    disabled={stopping === dep.deployment_id}
                    onClick={() => stop(dep)}
                  >
                    {stopping === dep.deployment_id ? 'Stopping…' : 'Stop'}
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
