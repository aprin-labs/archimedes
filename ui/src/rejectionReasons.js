// Rejection reasons for the Library's "Rejected (N) — did not pass the rigor
// gate" section.
//
// The section used to explain every rejected candidate with ONE shared
// paragraph ("Most rejections at this stage are 'return series too short' …
// A longer backtest window typically unlocks them"). Nothing measured that
// claim, so for a candidate rejected for a different reason the page stated
// something false about it — while the card itself showed "—" for Sharpe /
// CAGR / Max DD and nothing at all about why the gate said no.
//
// The reasons now come off the row: `rigor_reasons` is the additive field
// GET /api/strategies/generated serves per strategy, built server-side by
// backend/archimedes/services/rigor_reasons.py from that strategy's OWN stored
// rigor_verdict, against the thresholds the gate actually uses
// (services/rigor_profiles.py). Nothing here re-declares a threshold or
// re-derives a verdict — every number and every bar in the rendered text was
// computed by the gate's own module and shipped in the payload.
//
// Fail-closed by construction: a row with no `rigor_reasons` (an old payload, a
// degraded read, a curated row that never had one) yields empty lists and a
// null summary, so the card renders nothing rather than a claim it cannot
// support.

// Per-check status vocabulary, mirroring services/rigor_reasons.py. Only PASS
// clears. NOT_COMPUTED blocks admission exactly as hard as FAIL but must never
// be rendered as a failure — "the check found a problem" and "the check never
// ran" are different facts about a strategy.
export const CHECK_PASS = 'pass'
export const CHECK_FAIL = 'fail'
export const CHECK_NOT_COMPUTED = 'not_computed'

// Stable reason classifications from the backend. Prose is never matched here.
export const REASON_SHORT_SERIES = 'short_return_series'

/** The row's reasons report, or null when the row carries none. */
export function reasonReport(row) {
  const report = row?.rigor_reasons
  if (!report || typeof report !== 'object') return null
  return Array.isArray(report.checks) ? report : null
}

function checksWithStatus(row, status) {
  const report = reasonReport(row)
  if (!report) return []
  return report.checks.filter((c) => c && c.status === status && c.label && c.detail)
}

export const failedChecks = (row) => checksWithStatus(row, CHECK_FAIL)
export const notComputedChecks = (row) => checksWithStatus(row, CHECK_NOT_COMPUTED)
export const passedChecks = (row) => checksWithStatus(row, CHECK_PASS)

/** The strategy's own recorded reason string, verbatim, or null. */
export function recordedReason(row) {
  const reason = reasonReport(row)?.recorded_reason
  return typeof reason === 'string' && reason.trim() ? reason.trim() : null
}

export function reasonCode(row) {
  const code = reasonReport(row)?.reason_code
  return typeof code === 'string' && code ? code : null
}

/**
 * True when the row did not pass, carries no recorded reason, and no check
 * failed the bar — the one case where the surface must say it cannot attribute
 * the rejection instead of naming a culprit.
 */
export function isUnattributed(row) {
  return reasonReport(row)?.unattributed === true
}

/** The bar the thresholds in `detail` are quoted against, e.g. "Archimedes Verified". */
export function barName(row) {
  const bar = reasonReport(row)?.bar
  return typeof bar === 'string' && bar.trim() ? bar.trim() : null
}

/** One check as a sentence fragment: "DSR confidence — 0.08 < 0.90 required". */
export function checkLine(check) {
  return `${check.label} — ${check.detail}`
}

/**
 * Is there anything true to render for this row? Gates the whole block, so a
 * payload without the field renders no heading, no bar name, and no prose.
 */
export function hasRejectionDetail(row) {
  if (!reasonReport(row)) return false
  return (
    recordedReason(row) != null ||
    isUnattributed(row) ||
    failedChecks(row).length > 0 ||
    notComputedChecks(row).length > 0 ||
    passedChecks(row).length > 0
  )
}

/**
 * Should the card render the block at all?
 *
 * Scoped to rows the gate actually turned down (`passes_rigor_gate === false`).
 * A row still awaiting a verdict is `null` here and gets nothing — listing the
 * checks it has not been graded against yet would read as a verdict it has not
 * received. A passing row's checks belong on its passport, not under a heading
 * about rejection.
 */
export function showsRejectionReasons(row) {
  return row?.passes_rigor_gate === false && hasRejectionDetail(row)
}

/**
 * A summary sentence for the rejected SECTION — returned only when it is true
 * of the rows actually on screen, and null otherwise.
 *
 * This is what replaces the old "Most rejections at this stage are 'return
 * series too short'" paragraph: the short-series sentence appears only when
 * some row on this page really carries that reason code, and it counts them
 * rather than asserting a majority nobody measured. The minimum-observation
 * number comes from the payload (`min_returns_for_gate`), never from a literal
 * typed here.
 */
export function rejectedSectionSummary(rows) {
  const list = Array.isArray(rows) ? rows : []
  const total = list.length
  if (!total) return null
  const short = list.filter((r) => reasonCode(r) === REASON_SHORT_SERIES)
  if (!short.length) return null
  const minReturns = reasonReport(short[0])?.min_returns_for_gate
  if (typeof minReturns !== 'number' || !Number.isFinite(minReturns)) return null
  const need = `the gate needs at least ${minReturns} daily returns to score one`
  if (short.length === total) {
    return total === 1
      ? `The candidate below was rejected because its persisted return series was too short — ${need}. A longer backtest window unlocks it.`
      : `All ${total} candidates below were rejected because their persisted return series was too short — ${need}. A longer backtest window unlocks them.`
  }
  return `${short.length} of these ${total} candidates were rejected because their persisted return series was too short — ${need}. The rest failed other checks, named on each row.`
}
