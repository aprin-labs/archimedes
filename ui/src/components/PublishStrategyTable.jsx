/** Compact strategy table for the Publish page.
 *
 * Renders a dense table of strategies with a "Publish" affordance.
 * Clicking a row selects it; onSelect fires with the strategy object.
 *
 * Mirrors the StrategyTable from Strategies.jsx but simplified —
 * no expandable detail, no rigor metrics, just the essentials needed
 * to pick and publish a strategy.
 */
import { useState } from 'react'
import MetricValue from './MetricValue'

function fmtUsd(n, fractionDigits = 0) {
  if (n == null || !Number.isFinite(n)) return '—'
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits })}`
}
function projectedEndValue(principal, cagr, years) {
  if (cagr == null || years == null) return null
  return principal * (1 + cagr) ** years
}
function periodInYears(startIso, endIso) {
  if (!startIso || !endIso) return null
  const s = new Date(startIso), e = new Date(endIso)
  return (e - s) / (365.25 * 86400 * 1000)
}
function statusLabel(status) {
  if (!status) return '—'
  const labels = { candidate: 'Candidate', validated: 'Validated', live: 'Live', retired: 'Retired', rejected: 'Rejected' }
  return labels[status] || status
}

export default function StrategyTable({ strategies, onSelect }) {
  if (!strategies.length) {
    return <div className="caption">No strategies available.</div>
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-[var(--glass-border)]">
      <table className="lib-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
        <thead>
          <tr style={{ background: 'var(--glass)', textAlign: 'left', borderBottom: '1px solid var(--glass-border)' }}>
            <th style={{ padding: '10px 14px' }}>Strategy</th>
            <th style={{ padding: '10px 14px' }}>Status</th>
            <th style={{ padding: '10px 14px', textAlign: 'right' }}>Sharpe</th>
            <th style={{ padding: '10px 14px', textAlign: 'right' }}>CAGR</th>
            <th style={{ padding: '10px 14px', textAlign: 'right' }}>Max DD</th>
            <th style={{ padding: '10px 14px', textAlign: 'right' }}>$1k →</th>
            <th style={{ padding: '10px 14px' }} />
          </tr>
        </thead>
        <tbody>
          {strategies.map(s => (
            <StrategyRow key={s.id} s={s} onSelect={onSelect} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function StrategyRow({ s, onSelect }) {
  const [open, setOpen] = useState(false)
  const years = periodInYears(s.backtest_start, s.backtest_end)
  const endValue = projectedEndValue(1000, s.cagr, years)

  return (
    <>
      <tr
        className="lib-row cursor-pointer"
        onClick={() => setOpen(o => !o)}
        style={{ cursor: 'pointer' }}
      >
        <td className="font-semibold">
          <span
            className={`${open ? 'i-lucide-chevron-down' : 'i-lucide-chevron-right'} w-3 h-3 mr-1.5 text-[var(--text-4)] flex-shrink-0 inline-block`}
          />
          {s.paper_title || s.strategy_name || s.id}
        </td>
        <td>
          <span className="tag tag-muted">{statusLabel(s.status)}</span>
        </td>
        <td className="mono" style={{ textAlign: 'right' }}>
          <MetricValue metric="sharpe_ratio" value={s.sharpe_ratio} row={s} surface="Publish table" />
        </td>
        {/* Color/class checks use Number.isFinite to match what MetricValue
            actually renders — a NaN/Infinity CAGR renders "—" and must not
            carry a positive/negative color (review). The Math.abs that used to
            sit on the drawdown cell is gone: it turned a contract-violating
            SIGNED drawdown into a plausible-looking positive one instead of
            reporting that the value was outside its domain (#1651). */}
        <td className={`mono ${!Number.isFinite(s.cagr) ? '' : s.cagr >= 0 ? 'positive' : 'negative'}`} style={{ textAlign: 'right' }}>
          <MetricValue metric="cagr" value={s.cagr} row={s} surface="Publish table" />
        </td>
        <td className="mono negative" style={{ textAlign: 'right' }}>
          <MetricValue metric="max_drawdown" value={s.max_drawdown} row={s} surface="Publish table" />
        </td>
        <td className="mono" style={{ textAlign: 'right', color: !Number.isFinite(s.cagr) ? 'var(--text-3)' : s.cagr >= 0 ? 'var(--positive)' : 'var(--negative)' }}>{fmtUsd(endValue)}</td>
        <td style={{ textAlign: 'right', padding: '6px 10px' }}>
          {s.can_publish ? (
            <button
              className="btn btn-primary btn-sm"
              onClick={(e) => { e.stopPropagation(); onSelect(s) }}
            >
              Publish
            </button>
          ) : (
            <span className="caption text-[var(--text-4)]">—</span>
          )}
        </td>
      </tr>
      {open && (
        <tr className="lib-row-detail">
          <td colSpan={7} style={{ padding: '12px 18px', background: 'var(--glass)' }}>
            <div className="text-[0.82rem]" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 18 }}>
              <div>
                <div className="label mb-2">Methodology</div>
                <div className="body">{s.methodology_summary || '—'}</div>
              </div>
              <div>
                <div className="label mb-2">Asset universe</div>
                <div className="body">{(s.asset_universe || []).join(', ') || '—'}</div>
              </div>
            </div>
            <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {s.can_publish && (
                <button
                  className="btn btn-primary btn-sm"
                  onClick={(e) => { e.stopPropagation(); onSelect(s) }}
                >
                  Publish
                </button>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}
