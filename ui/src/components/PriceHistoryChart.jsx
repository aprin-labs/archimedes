// Shared SVG line-chart for a price/index time series. Extracted from
// AssetModal.jsx (#464) so the grouped-asset detail view can reuse the exact
// same charting approach instead of introducing a second pattern or a new
// charting library.

import { useEffect, useState } from 'react'

import { buildXTicks, TICK_FONT_SIZE } from '../chartTicks'

export function fmtPrice(v) {
  if (v == null || Number.isNaN(v)) return '—'
  if (v >= 1000) return `$${v.toFixed(0)}`
  if (v >= 10) return `$${v.toFixed(2)}`
  return `$${v.toFixed(4)}`
}

// Fallback viewBox width for the first paint, before the container is measured
// (and for any environment without ResizeObserver).
const DEFAULT_SVG_W = 720
const MIN_SVG_W = 260
const PAD_R = 18
const PAD_T = 16
const PAD_B = 36

// Plot height tracks width within bounds. Because the viewBox is now sized in
// CSS pixels the old `maxHeight: 320` letterbox no longer applies, so the same
// shape is kept explicitly: ~260 at the 720 desktop width it used to assume,
// up to 320 in a wide modal, and never below 220 on a phone (where a purely
// proportional height would collapse the plot to ~130px).
const heightFor = w => Math.min(320, Math.max(220, Math.round(w * 0.36)))

/**
 * PriceHistoryChart — SVG line chart of one series of {ts, price} points.
 *
 * Honest fallback: when `points` is empty (range unsupported or upstream
 * feed returned nothing) we render an explicit empty state. We never
 * synthesize a flat line.
 *
 * `formatValue` lets callers (e.g. a group-index chart plotting a % change
 * rather than a dollar price) override the y-axis / label formatting without
 * forking the chart.
 */
export default function PriceHistoryChart({
  points,
  loading,
  error,
  formatValue = fmtPrice,
  // Callers can restore context lost when this chart was extracted from
  // AssetModal (e.g. the single-asset modal's more specific 'on this asset'
  // wording) without forking the component (review).
  emptyHeadline = 'Historical chart unavailable for the selected range.',
}) {
  // Measure the container so the viewBox is 1:1 with CSS pixels (#1602).
  // A fixed 720-unit viewBox scaled into a ~360px phone rendered the 9-unit
  // axis text at ~4.5px, which is the "hard to read" half of the report.
  // Sizing the viewBox to the real width keeps every label at a true
  // TICK_FONT_SIZE px and lets the tick count adapt to the space that
  // actually exists rather than to a constant.
  const [wrapEl, setWrapEl] = useState(null)
  const [measuredW, setMeasuredW] = useState(DEFAULT_SVG_W)

  useEffect(() => {
    if (!wrapEl || typeof ResizeObserver === 'undefined') return undefined
    const apply = w => { if (w > 0) setMeasuredW(Math.round(w)) }
    apply(wrapEl.getBoundingClientRect().width)
    const ro = new ResizeObserver(entries => {
      for (const entry of entries) apply(entry.contentRect.width)
    })
    ro.observe(wrapEl)
    return () => ro.disconnect()
  }, [wrapEl])

  const SVG_W = Math.max(MIN_SVG_W, measuredW)
  const SVG_H = heightFor(SVG_W)
  // The y-label gutter is most of a phone's width at the desktop value, so it
  // narrows with the chart. Axis prices stay right-aligned against it either way.
  const PAD_L = SVG_W < 420 ? 44 : 56

  if (loading) {
    return (
      <div ref={setWrapEl} style={{ height: SVG_H, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div className="caption" style={{ color: 'var(--text-4)' }}>Loading price history…</div>
      </div>
    )
  }
  if (error || !points || points.length === 0) {
    return (
      <div
        ref={setWrapEl}
        style={{
          height: SVG_H,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'var(--glass)',
          border: '1px dashed var(--glass-border)',
          borderRadius: 6,
        }}
      >
        <div className="caption" style={{ color: 'var(--text-4)', textAlign: 'center', maxWidth: 380 }}>
          {emptyHeadline}
          {error ? <><br /><span style={{ fontSize: '0.7rem', opacity: 0.7 }}>{error}</span></> : null}
        </div>
      </div>
    )
  }

  const prices = points.map(p => p.price)
  const minP = Math.min(...prices)
  const maxP = Math.max(...prices)
  const range = maxP - minP || Math.max(1e-6, Math.abs(maxP) * 0.01)
  // Light vertical padding so the line never touches the top/bottom border.
  const yPad = range * 0.08

  const toX = i => PAD_L + (i / Math.max(1, points.length - 1)) * (SVG_W - PAD_L - PAD_R)
  const toY = p => SVG_H - PAD_B - ((p - minP + yPad) / (range + 2 * yPad)) * (SVG_H - PAD_T - PAD_B)

  const linePath = points
    .map((pt, i) => `${i === 0 ? 'M' : 'L'} ${toX(i).toFixed(1)} ${toY(pt.price).toFixed(1)}`)
    .join(' ')
  const areaPath =
    `M ${toX(0).toFixed(1)} ${(SVG_H - PAD_B).toFixed(1)} ` +
    points.map((pt, i) => `L ${toX(i).toFixed(1)} ${toY(pt.price).toFixed(1)}`).join(' ') +
    ` L ${toX(points.length - 1).toFixed(1)} ${(SVG_H - PAD_B).toFixed(1)} Z`

  // y-axis labels (5 ticks)
  const yTicks = [0, 1, 2, 3, 4].map(i => {
    const p = minP + (range * i) / 4
    return { y: toY(p), label: formatValue(p) }
  })

  // x-axis labels. Count, format and edge anchoring all come from chartTicks —
  // see that module for why the label is read lexically out of the series
  // timestamp instead of via a Date round-trip (#1602).
  const xTicks = buildXTicks(points, { plotWidth: SVG_W - PAD_L - PAD_R })
    .map(t => ({ ...t, x: toX(t.index) }))

  // Coloring: green if last > first, red otherwise — matches the 24h
  // change badge convention.
  const first = points[0].price
  const last = points[points.length - 1].price
  const stroke = last >= first ? 'var(--positive)' : 'var(--negative)'
  const fill = last >= first ? 'rgba(34,197,94,0.10)' : 'rgba(239,68,68,0.10)'

  return (
    <div ref={setWrapEl}>
      <svg viewBox={`0 0 ${SVG_W} ${SVG_H}`} width="100%" height={SVG_H} style={{ display: 'block' }}>
        {/* horizontal grid */}
        {yTicks.map((t, i) => (
          <line key={i} x1={PAD_L} y1={t.y} x2={SVG_W - PAD_R} y2={t.y}
            stroke="var(--chart-grid)" strokeWidth="1" />
        ))}
        {/* axes */}
        <line x1={PAD_L} y1={PAD_T} x2={PAD_L} y2={SVG_H - PAD_B}
          stroke="var(--chart-grid-strong)" strokeWidth="1" />
        <line x1={PAD_L} y1={SVG_H - PAD_B} x2={SVG_W - PAD_R} y2={SVG_H - PAD_B}
          stroke="var(--chart-grid-strong)" strokeWidth="1" />

        {/* y-axis labels */}
        {yTicks.map((t, i) => (
          <text key={i} x={PAD_L - 6} y={t.y + 3.5} textAnchor="end"
            fill="var(--chart-label)" fontSize="10" className="mono">
            {t.label}
          </text>
        ))}
        {/* x-axis labels. The first and last are anchored to the plot edges
            rather than centred on them, so neither hangs into the y-label
            gutter nor gets clipped by the right edge of the viewBox. */}
        {xTicks.map((t, i) => (
          <text key={i} x={t.x} y={SVG_H - PAD_B + 15} textAnchor={t.anchor}
            fill="var(--chart-label)" fontSize={TICK_FONT_SIZE}>
            {t.label}
          </text>
        ))}

        {/* area under the line */}
        <path d={areaPath} fill={fill} stroke="none" />
        {/* main line */}
        <path d={linePath} fill="none" stroke={stroke} strokeWidth="1.8" strokeLinejoin="round" />
      </svg>
    </div>
  )
}
