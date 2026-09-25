import { useEffect, useState } from 'react'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

// Considered-Alternatives panel — upgraded for the T1.1 Phase-3 fan-out.
//
// Data shape (GET /api/generate/jobs/{job_id}/candidates):
//   candidates: ranked leaderboard (entry order = society rank); one entry has
//   selected=true (the winner); alternates carry strategy_id=null (episodic
//   proposals, not library strategies — no dead links); every entry has a
//   rigor_verdict with dsr, dsr_p_value, pbo, oos_sharpe, or a 'reason' for
//   abstain/text-only entries; generation_method="debate_abstain" entries get
//   the abstain framing.

// ── Helpers ──────────────────────────────────────────────────────────────

function fmt(val, digits = 3) {
  if (val == null || (typeof val === 'number' && !isFinite(val))) return '—'
  return Number(val).toFixed(digits)
}

// Compact rigor chip: coloured label + value
function RigorChip({ label, value, bad }) {
  const color = value === '—'
    ? 'var(--text-4)'
    : bad
      ? 'var(--negative, #ef4444)'
      : 'var(--positive, #22c55e)'
  return (
    <span style={{ marginRight: 10, whiteSpace: 'nowrap' }}>
      <span style={{ color: 'var(--text-4)' }}>{label}</span>
      {' '}
      <strong style={{ color }}>{value}</strong>
    </span>
  )
}

// Regime tag: bull / bear / neutral
function RegimeTag({ regime }) {
  if (!regime || regime === 'neutral') return null
  const bull = regime === 'bull'
  return (
    <span className="tag" style={{
      background: bull ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)',
      color: bull ? 'var(--positive, #22c55e)' : 'var(--negative, #ef4444)',
      marginLeft: 6,
    }}>
      <span className={bull ? 'i-lucide-trending-up w-3.5 h-3.5' : 'i-lucide-trending-down w-3.5 h-3.5'} />
      {' '}{bull ? 'Bull' : 'Bear'}
    </span>
  )
}

function isAbstain(c) {
  return c.generation_method === 'debate_abstain'
}

function rejectReason(c) {
  if (isAbstain(c)) return c.rigor_verdict?.reason || 'Debate abstain — hold current weights'
  if (c.rigor_verdict?.reason) return c.rigor_verdict.reason
  return 'Outranked by the society leader'
}

// ── Component ────────────────────────────────────────────────────────────

export default function RejectedCandidates({ jobId, onClose, onNavigate }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    // credentials:'include' sends the account session cookie. The job endpoints
    // are account-owned (#1194: require_current_user + _require_job_access), so
    // without it this 401s and the considered-alternatives panel renders empty.
    fetch(`${API_BASE}/api/generate/jobs/${encodeURIComponent(jobId)}/candidates`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : Promise.reject(new Error(`Backend returned ${r.status}`)))
      .then(d => { if (!cancelled) setData(d) })
      .catch(e => { if (!cancelled) setError(e.message || 'Failed to load candidates') })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [jobId])

  // Partition: winner first, then up to 10 alternates (society order preserved)
  const candidates = data?.candidates ?? []
  const winner = candidates.find(c => c.selected)
  const alternates = candidates.filter(c => !c.selected).slice(0, 10)

  const goToStrategy = (strategyId) => {
    if (!strategyId) return
    if (onNavigate) {
      onNavigate('strategy', { strategyId })
    } else {
      window.location.assign(`/app/strategy/${encodeURIComponent(strategyId)}`)
    }
    onClose?.()
  }

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.85)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 1000, padding: 20,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        className="card"
        style={{
          maxWidth: 720, width: '100%', maxHeight: '80vh', overflowY: 'auto', padding: 24,
          background: 'var(--canvas)',
          border: '1px solid var(--glass-border)',
          boxShadow: '0 24px 48px rgba(0,0,0,0.5)',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
          <div>
            <h3 style={{ marginBottom: 4 }}>Considered alternatives</h3>
            <p className="caption" style={{ marginBottom: 0 }}>
              The debate society ranked {candidates.length} candidate{candidates.length !== 1 ? 's' : ''};
              the top-ranked candidate was persisted to the library (see its tag for rigor status).
              Alternates are episodic proposals — no library entry.
            </p>
          </div>
          <button className="btn btn-outline btn-sm" onClick={onClose} style={{ flexShrink: 0, marginLeft: 12 }}>
            Close
          </button>
        </div>

        {loading && <div className="caption">Loading…</div>}
        {error && <div className="info-box warning">{error}</div>}

        {!loading && !error && candidates.length === 0 && (
          <div className="caption">No candidate data on file for this job.</div>
        )}

        {!loading && !error && candidates.length > 0 && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>

            {/* Winner — pinned first with trophy treatment */}
            {winner && (
              <CandidateRow
                c={winner}
                rank={1}
                isWinner
                onNavigate={winner.strategy_id ? goToStrategy : null}
              />
            )}

            {/* Alternates (up to 10, society order) */}
            {alternates.length > 0 && (
              <>
                {candidates.length > 1 && (
                  <div className="label" style={{ marginTop: 4, marginBottom: 2, color: 'var(--text-4)', fontSize: '0.75rem' }}>
                    ALTERNATES
                  </div>
                )}
                {alternates.map((c, i) => (
                  <CandidateRow key={c.candidate_id} c={c} rank={i + 2} isWinner={false} onNavigate={null} />
                ))}
              </>
            )}

            {/* Edge case: exactly 1 candidate, no alternates */}
            {candidates.length === 1 && (
              <div className="caption" style={{ color: 'var(--text-4)', marginTop: 4 }}>
                The society produced a single candidate this run.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// ── CandidateRow ─────────────────────────────────────────────────────────

function CandidateRow({ c, rank, isWinner, onNavigate }) {
  const rv = c.rigor_verdict || {}
  const abstain = isAbstain(c)

  // Rigor chip values
  const dsrP = rv.dsr_p_value != null ? fmt(rv.dsr_p_value) : '—'
  const pbo   = rv.pbo != null ? fmt(rv.pbo) : '—'
  const oos   = rv.oos_sharpe != null ? fmt(rv.oos_sharpe) : '—'

  // Colour guidance
  const dsrBad = rv.dsr_p_value != null && rv.dsr_p_value < 0.95
  const pboBad  = rv.pbo != null && rv.pbo >= 0.5
  const oosBad  = rv.oos_sharpe != null && rv.oos_sharpe <= 0

  return (
    <div
      className="card-flat"
      style={{
        padding: 12,
        border: isWinner ? '1px solid var(--accent)' : '1px solid var(--glass-border)',
        background: isWinner ? 'rgba(255,209,102,0.06)' : 'transparent',
      }}
    >
      {/* Row header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 6 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          {/* Rank / trophy */}
          {isWinner
            ? <span style={{ fontSize: '1rem' }}>&#x1F3C6;</span>
            : <span className="caption" style={{ color: 'var(--text-4)', minWidth: 20 }}>#{rank}</span>}
          <strong style={{ fontSize: '0.9rem' }}>{c.strategy_name || c.candidate_id}</strong>
          <RegimeTag regime={c.regime} />
          {abstain && (
            <span className="tag" style={{ background: 'rgba(255,209,102,0.15)', color: 'var(--accent)' }}>
              Abstain
            </span>
          )}
        </div>

        {/* Tags: selected / rigor pass */}
        <div style={{ display: 'flex', gap: 6, flexShrink: 0, marginLeft: 8 }}>
          {isWinner && <span className="tag tag-accent">Selected</span>}
          {!abstain && (
            c.passes_rigor
              ? <span className="tag tag-positive">Passes rigor</span>
              : <span className="tag tag-negative">Failed rigor</span>
          )}
        </div>
      </div>

      {/* Rigor chips (skip for abstain — those have no real stats) */}
      {!abstain && (
        <div className="caption" style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', marginBottom: 4 }}>
          <RigorChip label="DSR conf" value={dsrP} bad={dsrBad} />
          <RigorChip label="PBO" value={pbo} bad={pboBad} />
          <RigorChip label="OOS" value={oos} bad={oosBad} />
        </div>
      )}

      {/* Abstain body */}
      {abstain && rv.reason && (
        <div className="caption" style={{ color: 'var(--text-3)', fontStyle: 'italic', marginBottom: 4 }}>
          {rv.reason}
        </div>
      )}

      {/* Reject reason (alternates only) */}
      {!isWinner && !abstain && (
        <div className="caption" style={{ color: 'var(--text-4)', marginTop: 2 }}>
          {rejectReason(c)}
        </div>
      )}

      {/* Winner link — only when strategy_id is present */}
      {isWinner && onNavigate && c.strategy_id && (
        <button
          className="btn btn-outline btn-sm"
          style={{ marginTop: 8 }}
          onClick={() => onNavigate(c.strategy_id)}
        >
          View strategy passport →
        </button>
      )}
    </div>
  )
}
