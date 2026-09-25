// x-axis tick logic for the Explore price charts (#1602).
//
// Extracted as a plain module (same precedent as statUtils.js) so the tick
// formatter is directly unit-testable — `node --test` cannot parse JSX, so
// logic that lives inside PriceHistoryChart.jsx can only ever be text-scanned,
// never executed, by the UI suite.
//
// ── Why this exists ──────────────────────────────────────────────────────────
//
// The previous implementation round-tripped each timestamp through a Date
// object and then read it back with LOCAL calendar accessors:
//
//     const d = new Date(Date.parse(ts.replace(' ', 'T')))
//     `${d.getMonth() + 1}-${d.getDate()}`
//
// That is an off-by-one-day bug for every viewer west of UTC. A crypto daily
// close stamped "2026-05-25 00:00:00+00:00" is UTC midnight; read back through
// America/New_York's calendar it is 2026-05-24 20:00, so the axis labelled the
// bar "05-24" — a date that does not appear anywhere in the series. Worse, it
// was inconsistent: ECMA-262 parses an offset-bearing or date-ONLY string as an
// absolute instant (shifted by local getters) but a tz-naive date-TIME string as
// local wall time (not shifted). So "2026-05-25 00:00:00-04:00" rendered
// correctly while "2026-05-25 00:00:00+00:00" rendered a day early, in the same
// chart, depending on which range the user picked.
//
// The fix: never convert. A bar's label must report the bar's own wall clock,
// which the series already carries in the string. We read the calendar fields
// LEXICALLY and never construct a Date for display, so the rendered label is
// the series timestamp by construction, in every timezone.
//
// (Re-zoning would also be wrong on the merits: a daily close for a US equity
// is the May 25 *session*, and must not be relabelled "May 24" because the
// viewer happens to be in Tokyo.)

/** Leading calendar fields of an ISO-ish timestamp. The trailing UTC offset,
 * if any, is deliberately NOT captured — see the module note above. Accepts
 * "2026-05-25", "2026-05-25 00:00:00", "2026-05-25T09:35:00+00:00". */
const TS_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

const MS_PER_DAY = 24 * 60 * 60 * 1000

/**
 * Read the wall-clock calendar fields out of a series timestamp, lexically.
 * Returns null for anything unparseable, so callers can fall back honestly
 * instead of rendering "NaN-NaN".
 */
export function parseSeriesTimestamp(ts) {
  if (typeof ts !== 'string') return null
  const m = TS_RE.exec(ts.trim())
  if (!m) return null
  const year = Number(m[1])
  const month = Number(m[2])
  const day = Number(m[3])
  if (month < 1 || month > 12 || day < 1 || day > 31) return null
  const hour = m[4] === undefined ? 0 : Number(m[4])
  const minute = m[5] === undefined ? 0 : Number(m[5])
  if (hour > 23 || minute > 59) return null
  return { year, month, day, hour, minute }
}

/** Wall-clock fields → a comparable scalar. Date.UTC is used purely as
 * calendar arithmetic on fields we already hold; because every stamp in a
 * series goes through the same basis, differences are exact. No timezone
 * conversion happens here and the result is never formatted for display. */
function toComparable(f) {
  return Date.UTC(f.year, f.month - 1, f.day, f.hour, f.minute)
}

/** Median gap between consecutive samples, in ms. Median (not the first pair,
 * which is what the old `isIntraday` heuristic used) so one weekend gap or one
 * missing bar cannot flip a whole axis into the wrong format. */
export function medianStepMs(fields) {
  if (!fields || fields.length < 2) return null
  const steps = []
  for (let i = 1; i < fields.length; i += 1) {
    steps.push(Math.abs(toComparable(fields[i]) - toComparable(fields[i - 1])))
  }
  steps.sort((a, b) => a - b)
  const mid = Math.floor(steps.length / 2)
  return steps.length % 2 === 0 ? (steps[mid - 1] + steps[mid]) / 2 : steps[mid]
}

/**
 * Choose the label format for a whole series.
 *
 * Resolution comes from the bar interval; the year/day qualifier comes from the
 * span, so a label is never ambiguous about which day (or year) it marks:
 *
 *   intraday bars, one calendar day    → 'time'     "09:35"
 *   intraday bars, several days        → 'dayTime'  "May 25 09:35"
 *   daily+ bars, < 180d, one year      → 'day'      "May 25"
 *   daily+ bars, < 180d, spans a NY    → 'dayYear'  "May 25, 2026"
 *   daily+ bars, < ~3y                 → 'month'    "May 2026"
 *   longer                             → 'year'     "2026"
 *
 * The 'dayTime' case is not hypothetical: the 1D range is served as period
 * "2d" (asset_market_service._HISTORY_RANGE_MAP), so a 1D chart normally holds
 * two calendar days of 5-minute bars. Labelling those with bare HH:MM repeats
 * the same clock times twice across the axis.
 */
export function pickTickFormat(points) {
  const fields = (points || []).map(p => parseSeriesTimestamp(p && p.ts)).filter(Boolean)
  if (fields.length === 0) return 'day'

  const step = medianStepMs(fields)
  const intraday = step !== null && step < 12 * 60 * 60 * 1000

  const first = fields[0]
  const last = fields[fields.length - 1]
  const spanMs = Math.abs(toComparable(last) - toComparable(first))

  if (intraday) {
    const sameDay =
      first.year === last.year && first.month === last.month && first.day === last.day
    return sameDay ? 'time' : 'dayTime'
  }
  if (spanMs < 180 * MS_PER_DAY) {
    return first.year === last.year ? 'day' : 'dayYear'
  }
  if (spanMs < 1100 * MS_PER_DAY) return 'month'
  return 'year'
}

const pad2 = n => String(n).padStart(2, '0')

/**
 * Render one series timestamp in the given format. Unparseable input falls
 * back to the raw leading date text rather than inventing a date.
 */
export function formatTickLabel(ts, format) {
  const f = parseSeriesTimestamp(ts)
  if (!f) return typeof ts === 'string' ? ts.slice(0, 10) : ''
  const mon = MONTHS[f.month - 1]
  const hhmm = `${pad2(f.hour)}:${pad2(f.minute)}`
  switch (format) {
    case 'time':
      return hhmm
    case 'dayTime':
      return `${mon} ${f.day} ${hhmm}`
    case 'dayYear':
      return `${mon} ${f.day}, ${f.year}`
    case 'month':
      return `${mon} ${f.year}`
    case 'year':
      return String(f.year)
    case 'day':
    default:
      return `${mon} ${f.day}`
  }
}

// Widest label each format produces, in characters — the basis for how many
// ticks fit without colliding.
const LABEL_CHARS = { time: 5, dayTime: 12, day: 6, dayYear: 12, month: 8, year: 4 }

/** x-axis label size, in SVG user units. PriceHistoryChart sizes its viewBox to
 * the measured container width, so one user unit is one CSS pixel and this is a
 * real 11px at every screen width. */
export const TICK_FONT_SIZE = 11
/** Minimum clear space between two adjacent labels, in the same units. */
const TICK_GAP = 16
const MAX_TICKS = 6
const MIN_TICKS = 2
// Mean advance width per character for the UI sans stack at this size.
const CHAR_WIDTH_RATIO = 0.62

/**
 * How many ticks fit across `plotWidth` without the labels touching.
 *
 * With n ticks the spacing is plotWidth/(n-1), so requiring
 * spacing >= labelWidth + gap gives n <= plotWidth/(labelWidth + gap) + 1.
 * Clamped to [2, 6]: two ticks always render (the first and last sample, which
 * are the two a reader actually looks up), and six is as dense as this chart
 * reads well even when there is room for more.
 */
export function chooseTickCount(plotWidth, format) {
  const chars = LABEL_CHARS[format] ?? LABEL_CHARS.day
  const labelWidth = chars * TICK_FONT_SIZE * CHAR_WIDTH_RATIO
  const perTick = labelWidth + TICK_GAP
  if (!Number.isFinite(plotWidth) || plotWidth <= 0) return MIN_TICKS
  const fits = Math.floor(plotWidth / perTick) + 1
  return Math.max(MIN_TICKS, Math.min(MAX_TICKS, fits))
}

/**
 * Build the x-axis ticks for a series.
 *
 * Returns `{ index, label, anchor }` per tick. `index` is the position in
 * `points` the tick marks, so the caller maps it through the same x-scale the
 * line uses and a label can never drift from the sample it names.
 *
 * `anchor` keeps the first and last labels inside the plot: centring them (the
 * previous behaviour) hung the first label into the y-axis gutter and pushed
 * the last one past the right edge of the viewBox, where it was clipped.
 */
export function buildXTicks(points, { plotWidth, format } = {}) {
  if (!points || points.length === 0) return []
  const fmt = format || pickTickFormat(points)
  const wanted = Math.min(chooseTickCount(plotWidth, fmt), points.length)
  const lastIdx = points.length - 1

  const indices = []
  for (let i = 0; i < wanted; i += 1) {
    const idx = wanted === 1 ? 0 : Math.round((i * lastIdx) / (wanted - 1))
    if (indices[indices.length - 1] !== idx) indices.push(idx)
  }

  return indices.map((index, i) => ({
    index,
    label: formatTickLabel(points[index] && points[index].ts, fmt),
    anchor: i === 0 ? 'start' : i === indices.length - 1 ? 'end' : 'middle',
  }))
}
