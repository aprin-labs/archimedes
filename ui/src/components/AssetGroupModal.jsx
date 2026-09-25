import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import PriceHistoryChart from './PriceHistoryChart'
import AssetGroupIcon from './AssetGroupIcon'
import { groupMeta } from '../assetGroups'
import { median, groupChangeWindowLabel } from '../statUtils'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const RANGES = ['1D', '1W', '1M', '1Y', '5Y', '10Y', 'MAX']
// Aggregating every member of a large bucket (crypto = 71 symbols) on every
// range change is expensive for both the browser and the backend cache. Cap
// how many symbols feed the aggregate chart; the card/list below still shows
// the full membership and true asset count.
const MAX_AGGREGATE_MEMBERS = 20

function fmtPct(v) {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(2)}%`
}

function changeClass(v) {
  if (v == null || Number.isNaN(v)) return ''
  return v >= 0 ? 'positive' : 'negative'
}

/**
 * Aggregate several symbols' history series into a single equal-weight
 * "index" series, expressed as % change from each series' own first point.
 * Normalizing to % change is what makes it valid to average across symbols
 * with wildly different price scales (e.g. a $0.001 token next to a $60,000
 * one) — anchoring on any single symbol's price units would misrepresent
 * the group.
 *
 * We align by TIMESTAMP on the common overlapping window — never by array
 * index. Same-range requests share a cadence, but NOT a start date: group
 * members have wildly different history lengths (sBTC ~15y vs sARB ~2y on
 * MAX), so index i lands on different calendar dates per symbol. And
 * rebasing each series to its OWN first bar mixes epochs (%-since-2010
 * averaged with %-since-2023 at the same x position). Instead: the group
 * index starts at the LATEST first-bar across members (the youngest
 * member's inception within the range), every series is rebased to its
 * first bar at/after that common start, and each time bucket averages only
 * the symbols that actually have a bar there. Consequence, by design: on
 * long ranges the group chart's window is capped by its youngest member —
 * a shorter, honest window beats a longer, distorted one.
 */
function buildGroupIndex(seriesList, range) {
  const usable = seriesList.filter(s => s.points && s.points.length > 1)
  if (usable.length === 0) return []

  // Daily+ ranges bucket on the ISO date part; intraday (1D) on the full ts.
  const bucketKey = (ts) => (range === '1D' ? String(ts) : String(ts).slice(0, 10))

  const seriesMaps = usable
    .map(s => {
      const map = new Map()
      for (const pt of s.points) {
        if (pt?.price == null) continue
        map.set(bucketKey(pt.ts), pt)
      }
      return { map, firstKey: bucketKey(s.points[0].ts) }
    })
    .filter(m => m.map.size > 1)
  if (seriesMaps.length === 0) return []

  // ISO8601 strings compare lexicographically — the max firstKey is the
  // youngest member's inception, i.e. the common window start.
  const commonStartKey = seriesMaps.map(m => m.firstKey).sort().at(-1)

  const rebased = []
  for (const { map } of seriesMaps) {
    const keys = [...map.keys()].sort()
    const startIdx = keys.findIndex(k => k >= commonStartKey)
    if (startIdx === -1) continue
    const base = map.get(keys[startIdx])?.price
    if (base == null || base === 0) continue
    rebased.push({ map, keys: keys.slice(startIdx), base })
  }
  if (rebased.length === 0) return []

  const allKeys = [...new Set(rebased.flatMap(r => r.keys))].sort()
  const out = []
  for (const k of allKeys) {
    let sum = 0
    let count = 0
    let ts = null
    for (const r of rebased) {
      const pt = r.map.get(k)
      if (!pt) continue
      sum += ((pt.price - r.base) / r.base) * 100
      count += 1
      if (!ts) ts = pt.ts
    }
    if (count > 0) out.push({ ts: ts || k, price: sum / count })
  }
  return out
}

export default function AssetGroupModal({ assetClass, assets, onClose }) {
  const [range, setRange] = useState('1M')
  const [seriesBySymbol, setSeriesBySymbol] = useState({})
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const meta = groupMeta(assetClass)
  const members = useMemo(
    () => [...assets].sort((a, b) => (b.current_price ?? 0) - (a.current_price ?? 0)),
    [assets]
  )
  const aggregateMembers = useMemo(() => members.slice(0, MAX_AGGREGATE_MEMBERS), [members])

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  useEffect(() => {
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = prev }
  }, [])

  useEffect(() => {
    if (aggregateMembers.length === 0) return
    let cancelled = false
    setLoading(true)
    setError('')
    Promise.all(
      aggregateMembers.map(a =>
        fetch(`${API_BASE}/api/explore/assets/${a.symbol}/history?range=${range}`)
          .then(res => (res.ok
            ? res.json().then(data => ({ symbol: a.symbol, points: Array.isArray(data.points) ? data.points : [], failed: false }))
            : { symbol: a.symbol, points: [], failed: true }))
          .catch(() => ({ symbol: a.symbol, points: [], failed: true }))
      )
    )
      .then(results => {
        if (cancelled) return
        const bySymbol = {}
        for (const r of results) bySymbol[r.symbol] = r.points
        setSeriesBySymbol(bySymbol)
        if (results.every(r => r.points.length === 0)) {
          // Distinguish "backend/network failing" from "genuinely no bars in
          // this range" — telling a user there's no data when every request
          // 5xx'd is misleading (review).
          setError(results.some(r => r.failed)
            ? 'Could not load history for this group right now — retry shortly.'
            : 'No historical data available for this group in the selected range.')
        }
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [aggregateMembers, range])

  const groupIndexPoints = useMemo(
    () => buildGroupIndex(Object.entries(seriesBySymbol).map(([symbol, points]) => ({ symbol, points })), range),
    [seriesBySymbol, range]
  )

  // Aggregate 24h change across the whole group (not just the capped
  // aggregate-chart subset) — cheap since it's already in the /assets payload.
  const medianChange24h = useMemo(() => {
    const vals = members.map(a => a.change_24h_pct).filter(v => v != null && !Number.isNaN(v))
    return median(vals)
  }, [members])
  // Null when the contributing members' windows disagree, which a group
  // spanning a holiday legitimately can (#1378).
  const medianWindow = useMemo(() => groupChangeWindowLabel(members), [members])

  if (!assetClass) return null

  return createPortal(
    <div
      className="fixed inset-0 flex items-center justify-center z-[1000]"
      style={{ background: 'rgba(0,0,0,0.78)', backdropFilter: 'blur(6px)' }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="asset-group-modal-title"
    >
      <div
        className="card-elevated p-6"
        onClick={e => e.stopPropagation()}
        style={{
          background: 'var(--surface-1)',
          maxHeight: '90vh', overflowY: 'auto',
          width: 'min(860px, 94vw)',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16 }}>
          <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
            <div
              style={{
                width: 40, height: 40, borderRadius: 8,
                background: 'var(--glass)', border: '1px solid var(--glass-border)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: 'var(--accent)', flexShrink: 0,
              }}
            >
              <AssetGroupIcon icon={meta.icon} size={22} />
            </div>
            <div>
              <h3 id="asset-group-modal-title" className="serif" style={{ fontSize: '1.5rem', margin: 0 }}>
                {meta.label}
              </h3>
              <div className="caption" style={{ color: 'var(--text-3)', marginTop: 2 }}>
                {members.length} asset{members.length === 1 ? '' : 's'} in this group
              </div>
            </div>
          </div>
          <button className="btn btn-outline btn-sm" onClick={onClose} aria-label="Close group details">
            Close (Esc)
          </button>
        </div>

        {/* Plain-English description */}
        <p className="body" style={{ marginTop: 14, color: 'var(--text-3)', maxWidth: 700 }}>
          {meta.description}
        </p>

        {/* Aggregate stat */}
        <div style={{ display: 'flex', gap: 24, alignItems: 'baseline', marginTop: 16, flexWrap: 'wrap' }}>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>
              {medianWindow ? `Median ${medianWindow} change` : 'Median change'} ({members.length} assets)
            </div>
            <div className={`mono ${changeClass(medianChange24h)}`} style={{ fontSize: '1.3rem', fontWeight: 600 }}>
              {fmtPct(medianChange24h)}
            </div>
          </div>
        </div>

        {/* Range toggle */}
        <div style={{ marginTop: 18, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          {RANGES.map(r => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={`btn btn-sm ${range === r ? '' : 'btn-outline'}`}
              style={{
                minWidth: 48,
                background: range === r ? 'var(--accent-muted)' : undefined,
                color: range === r ? 'var(--accent)' : undefined,
                borderColor: range === r ? 'var(--accent)' : undefined,
              }}
              aria-pressed={range === r}
            >
              {r}
            </button>
          ))}
          <span className="caption" style={{ marginLeft: 'auto', color: 'var(--text-3)', fontSize: '0.7rem' }}>
            Equal-weight % change index
            {members.length > MAX_AGGREGATE_MEMBERS ? ` · first ${MAX_AGGREGATE_MEMBERS} of ${members.length} assets` : ''}
          </span>
        </div>

        {/* Aggregated chart — reuses the same PriceHistoryChart component/pattern
            as the single-asset detail view (AssetModal.jsx), plotting % change
            instead of $ price via the formatValue override. */}
        <div style={{ marginTop: 10 }}>
          <PriceHistoryChart
            points={groupIndexPoints}
            loading={loading}
            error={error}
            formatValue={v => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`}
          />
        </div>

        {/* Member list */}
        <div style={{ marginTop: 20, paddingTop: 14, borderTop: '1px solid var(--glass-border)' }}>
          <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem', marginBottom: 8 }}>
            Assets in this group
          </div>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))',
              gap: 8,
              maxHeight: 220,
              overflowY: 'auto',
            }}
          >
            {members.map(a => (
              <div
                key={a.symbol}
                style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
                  padding: '6px 8px', background: 'var(--glass)', borderRadius: 6,
                  border: '1px solid var(--glass-border)',
                }}
              >
                <span className="mono" style={{ fontSize: '0.78rem', fontWeight: 600 }}>{a.symbol}</span>
                <span className={`mono ${changeClass(a.change_24h_pct)}`} style={{ fontSize: '0.7rem' }}>
                  {fmtPct(a.change_24h_pct)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>,
    document.body
  )
}
