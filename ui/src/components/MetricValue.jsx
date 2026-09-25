/** MetricValue — the one place a backtest metric becomes pixels (#1651).
 *
 * `metricDomain.formatMetric` decides WHAT to render; this decides HOW, and it
 * exists so that the "what" cannot be partly ignored. A clamped value comes
 * back as `{ label: "−100.0%", note: "reported −130.3% — outside the possible
 * range" }`, and a caller free to render `label` while dropping `note` would
 * turn an honest clamp back into a silent one — which is the anti-goal the
 * issue names. Routing all four surfaces (Library table, Library card,
 * Passport, Leaderboard, Publish table) through this component makes the
 * annotation structural rather than a convention every future cell has to
 * remember.
 *
 * Markup notes:
 *  - The wrapper is a `<span>`, not a `<div>`, so the same component is valid
 *    inside a `<td>`, a card `<div>` and the Passport's `Metric` value slot.
 *  - The annotation carries a visible glyph AND a text sentence AND an sr-only
 *    expansion: colour alone has never been an acceptable signal here (1.4.1),
 *    and a tooltip alone is invisible on touch.
 */
import { formatMetric } from '../metricDomain.js'

export default function MetricValue({
  metric,
  value,
  row = null,
  format,
  digits,
  surface = '',
  className = '',
  style,
}) {
  const m = formatMetric(metric, value, { row, format, digits, surface })
  return (
    <span className={className} style={style} title={m.title || undefined}>
      {m.label}
      {m.note && (
        <span
          className="metric-annotation"
          style={{
            display: 'block',
            fontSize: '0.68rem',
            fontWeight: 400,
            color: 'var(--negative)',
          }}
        >
          <span aria-hidden="true">⚠ </span>
          {m.note}
          <span className="sr-only"> — {m.title}</span>
        </span>
      )}
    </span>
  )
}
