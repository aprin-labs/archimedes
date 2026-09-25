import { useEffect, useRef, useState } from 'react'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

// Recent Generations job table for the Generate page.
//
// Polls /api/generate/jobs every 5 s so the table stays live while the user
// stays on the page. Clicking a row calls `onDrillIn(job_id)` to open that
// job's SSE stream as a drill-down view.
//
// Recently-generated → Library linkage (#1646). `_job_summary`
// (generate_routes.py) has always put `best_strategy_id` on every job row;
// this table simply never read it, so the only thing a finished generation
// could open was a replay of its own event stream. A `done` row that produced
// a strategy now also offers "strategy →", navigating to that strategy's
// passport with the SAME call shape Library already uses
// (`Strategies.jsx:774`). The stream drill-in stays — watching a past job's
// reasoning and reading the strategy it produced are different questions, and
// removing either would answer only one of them.
//
// Protected app route already requires Better Auth account. On 401, stop polling
// until manual retry; wallet presence is unrelated to account authentication.

const STATE_TAGS = {
  queued:    { label: 'queued',    cls: 'tag-muted' },
  running:   { label: 'running',   cls: 'tag-accent' },
  // Read-time derived state (#1355): a "running" job whose backend heartbeat
  // has gone stale for over 5 minutes — the process most likely died mid-run
  // (routine trigger: build-on-deploy rolling the Fargate task). Never
  // written to Redis; the server computes it on every read so this table and
  // the drill-in stream can't disagree about a dead job's state.
  stalled:   { label: 'stalled',   cls: 'tag-warning' },
  done:      { label: 'done',      cls: 'tag-positive' },
  error:     { label: 'error',     cls: 'tag-negative' },
  cancelled: { label: 'cancelled', cls: 'tag-muted' },
}

// Shared by the two row actions so a second link cannot drift visually from
// the first — they sit side by side in the same cell.
const LINK_BTN = {
  background: 'none',
  border: 'none',
  padding: 0,
  font: 'inherit',
  cursor: 'pointer',
  color: 'var(--accent)',
  whiteSpace: 'nowrap',
}

function timeAgo(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  // epoch-0 sentinel means absent timestamp — render like a missing value
  if (d.getTime() <= 0) return '—'
  const secs = Math.floor((Date.now() - d.getTime()) / 1000)
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

export default function GenerationStatus({ activeJobId, onDrillIn, onNavigate }) {
  const [jobs, setJobs] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // When endpoint returns 401, block further polling until manual retry.
  const [blocked, setBlocked] = useState(false)
  const intervalRef = useRef(null)

  const stopPoll = () => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current)
      intervalRef.current = null
    }
  }

  useEffect(() => {
    if (blocked) {
      setLoading(false)
      stopPoll()
      return
    }

    let cancelled = false

    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/generate/jobs?limit=20`, {
          credentials: 'include',
        })
        if (res.status === 401) {
          // Stop polling immediately.  Clear stale rows so the user does not
          // see outdated data while unauthenticated, and surface a sign-in
          // prompt with manual retry path.
          if (!cancelled) {
            stopPoll()
            setBlocked(true)
            setJobs([])
            setError('Session expired — sign in again to view your generations.')
          }
          return
        }
        if (!res.ok) throw new Error(`Backend returned ${res.status}`)
        const data = await res.json()
        if (!cancelled) {
          setJobs(data.jobs || [])
          setError('')
        }
      } catch (e) {
        const msg = e?.message && e.message.length < 120 ? e.message : 'Failed to load jobs'
        if (!cancelled) setError(msg)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    intervalRef.current = setInterval(load, 5000)

    return () => {
      cancelled = true
      stopPoll()
    }
  }, [blocked])

  if (loading && !jobs.length) return null

  return (
    <div className="card" style={{ padding: 16 }}>
      <div className="label mb-2">Recent generations</div>
      {error && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className="caption" style={{ color: 'var(--negative)' }}>{error}</span>
          {blocked && (
            <button
              className="btn btn-outline btn-sm"
              style={{ fontSize: '0.75rem', padding: '2px 8px' }}
              onClick={() => setBlocked(false)}
            >
              Retry
            </button>
          )}
        </div>
      )}
      {jobs.length === 0 && !error && (
        <div className="caption" style={{ color: 'var(--text-3)' }}>
          No generations yet — submit your first brief above.
        </div>
      )}
      {jobs.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table
            style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}
          >
            <caption className="sr-only">Recent generations</caption>
            <thead>
              <tr
                style={{ textAlign: 'left', borderBottom: '1px solid var(--glass-border)' }}
              >
                <th scope="col" style={{ padding: '6px 8px' }}>Brief</th>
                <th scope="col" style={{ padding: '6px 8px' }}>State</th>
                <th scope="col" style={{ padding: '6px 8px' }}>N</th>
                <th scope="col" style={{ padding: '6px 8px' }}>Updated</th>
                <th scope="col" style={{ padding: '6px 8px' }}>
                  <span className="sr-only">Open</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {jobs.map(j => {
                const tag = STATE_TAGS[j.state] || STATE_TAGS.queued
                const isActive = j.job_id === activeJobId
                return (
                  // role="button" on a <tr> destroys the row's table semantics:
                  // the <td>s stop being exposed as cells and the aria-label
                  // replaces the row content wholesale, so a screen-reader user
                  // heard only "Open generation: <brief>, button" and never the
                  // state, candidate count or updated time this table exists to
                  // convey (4.1.2 / 1.3.1). The control now lives in the last
                  // cell; the row keeps onClick as a mouse convenience.
                  <tr
                    key={j.job_id}
                    onClick={() => onDrillIn?.(j.job_id)}
                    style={{
                      borderBottom: '1px solid var(--glass)',
                      background: isActive
                        ? 'rgba(255,209,102,0.07)'
                        : 'transparent',
                      cursor: 'pointer',
                      transition: 'background 0.12s',
                    }}
                    onMouseEnter={e => {
                      if (!isActive) e.currentTarget.style.background = 'var(--glass)'
                    }}
                    onMouseLeave={e => {
                      if (!isActive) e.currentTarget.style.background = 'transparent'
                    }}
                  >
                    <td
                      style={{
                        padding: '8px 8px',
                        maxWidth: 300,
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {j.brief_intent || '—'}
                    </td>
                    <td style={{ padding: '8px 8px' }}>
                      <span className={`tag ${tag.cls}`}>{tag.label}</span>
                    </td>
                    <td style={{ padding: '8px 8px' }}>{j.n_candidates ?? '—'}</td>
                    <td
                      style={{ padding: '8px 8px', whiteSpace: 'nowrap' }}
                      className="caption"
                    >
                      {timeAgo(j.updated_at)}
                    </td>
                    <td style={{ padding: '8px 8px', textAlign: 'right' }}>
                      <span style={{ display: 'inline-flex', gap: 12, whiteSpace: 'nowrap' }}>
                        <button
                          type="button"
                          className="caption"
                          onClick={e => { e.stopPropagation(); onDrillIn?.(j.job_id) }}
                          aria-label={`Open generation: ${j.brief_intent || j.job_id}`}
                          style={LINK_BTN}
                        >
                          {/* "stalled" intentionally falls through to "view →"
                              (#1355) — it is a "running" row the server has
                              already determined is dead; offering "resume →"
                              would repeat the exact false liveness claim this
                              state exists to correct. */}
                          {j.state === 'running' || j.state === 'queued' ? 'resume →' : 'view →'}
                        </button>
                        {/* The passport link (#1646). Rendered only when the job
                            actually produced a strategy — gated on
                            `best_strategy_id` rather than on `state === 'done'`
                            alone, because a job can finish having persisted
                            nothing (every candidate rejected), and a link that
                            navigates to a 404 is worse than no link. Never
                            replaces "view →" beside it: the stream and the
                            strategy answer different questions. */}
                        {j.best_strategy_id && (
                          <button
                            type="button"
                            className="caption"
                            onClick={e => {
                              e.stopPropagation()
                              // Written as the guarded plain call rather than
                              // this file's local `fn?.()` idiom so it is
                              // BYTE-IDENTICAL to Strategies.jsx:774's
                              // `onNavigate('strategy', { strategyId })` — one
                              // grep finds every passport navigation in the
                              // app, and the two call sites cannot drift into
                              // two different route contracts.
                              if (onNavigate) onNavigate('strategy', { strategyId: j.best_strategy_id })
                            }}
                            aria-label={`Open the strategy generated by: ${j.brief_intent || j.job_id}`}
                            style={LINK_BTN}
                          >
                            strategy →
                          </button>
                        )}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
