// Pure, framework-free copy + formatters for the Paper Trading page
// (PaperTrading.jsx). Extracted so the DRIFT tooltip, the total-return
// formatter, and the error-message mapping are unit-testable without a DOM —
// the pattern ui/src/generateQuote.js and ui/src/password-rules.js already
// establish (see ui/test/generate-quote.test.js, ui/test/password-rules.test.js).
//
// Every export here exists to fix a specific honesty bug (#1362):
//   - driftTooltip: the old inline string promised a freeze/investigation
//     that never happens while a deployment is ACTIVE — advance_all
//     (paper_trading.py) filters on STATUS_ACTIVE and never consults
//     drift_detected_at, so an active, drifted ledger keeps appending. But
//     drift_detected_at is never cleared, so the same chip can still be
//     showing on a STOPPED deployment, where the record genuinely IS
//     frozen — the tooltip must not claim it "keeps advancing" there
//     either. It gates its closing clause on `status` for that reason, and
//     must never interpolate the raw machine timestamp paper_trading.py's
//     deployment_summary emits (`drift_detected_at.isoformat()`) into
//     English prose.
//   - formatTotalReturn: deployment_summary's `total_return` is a real
//     `0.0` (not null) at day 0 — the OLD `pct()` rendered that as a
//     measured-looking "+0.00%". Day 0 is the normal state right after
//     deploy, not an edge case; the discriminator is `days`, never the
//     value, so a genuinely measured zero at day N still prints.
//   - paperErrorMessage: api.js's apiGet/apiPost throw
//     `Error("Backend returned ${status}")` on any non-2xx — that literal
//     string must never reach the `role="alert"` card verbatim. Mirrors
//     the status -> sentence mapping StrategyPassport.jsx's PaperDeployCard
//     already established for the sibling paper CTA.

import { DEGENERATE_TITLE, NOT_GRADED_TITLE } from './libraryStatus.js'
import {
  UNKNOWN_RIGOR_LABEL,
  UNKNOWN_RIGOR_TITLE,
  isUnknownRigorGateStatus,
  warnUnknownRigorGateStatus,
} from './rigorGateStatus.js'

/**
 * Render a `drift_detected_at` ISO timestamp as a plain human date, in UTC
 * so the output is independent of the caller's local timezone (Node's
 * `node --test` and the browser must agree). Never throws on a malformed
 * input — falls back to a neutral phrase rather than rendering "Invalid
 * Date" or crashing the tooltip.
 */
function formatDriftDate(driftAtIso) {
  const d = new Date(driftAtIso)
  if (Number.isNaN(d.getTime())) return 'an earlier date'
  return d.toLocaleDateString('en-US', { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric' })
}

/**
 * The DRIFT chip's tooltip. States what actually happens when a fresh
 * replay disagrees with rows already written: the ledger is append-only and
 * was NOT rewritten (mirrors the backend's own warning in
 * `paper_trading.py:advance_deployment`) — never a promise of a halt or an
 * investigation, which only the Stop path (PaperTrading.jsx's `stop()`) has
 * actually earned. `drift_detected_at` is overwritten on every recurrence
 * (paper_trading.py:158), so this reads as the MOST RECENT disagreement,
 * not the first.
 *
 * `status` gates the closing clause, because "keeps advancing" is only true
 * while the deployment is active: `advance_all` (paper_trading.py:173)
 * filters on `STATUS_ACTIVE`, so a STOPPED deployment does not advance —
 * Stop (paper_routes.py:142) is the one path that genuinely halts it, and
 * `drift_detected_at` is never cleared, so the chip can still be showing on
 * a stopped row. Pass the same `status` the STOPPED/ACTIVE pill renders
 * from; anything other than `'active'` gets the stopped-true clause.
 */
export function driftTooltip(driftAtIso, status) {
  const when = formatDriftDate(driftAtIso)
  const base =
    `A fresh replay disagreed with rows already recorded, most recently on ${when}. ` +
    'The ledger is append-only and was not rewritten — the discrepancy is surfaced, not hidden.'
  return status === 'active'
    ? `${base} The track record keeps advancing.`
    : `${base} No rows have been added since the deployment was stopped, and the recorded disagreement stands.`
}

/**
 * `deployment_summary.total_return` formatted for the headline figure.
 * Returns '—' when there is nothing measured yet: `days === 0` (the normal
 * state right after deploy — `replay_spec` only emits dates
 * `>= deployed_at`, so a same-day deploy legitimately has zero rows) or the
 * value itself is missing/NaN. Otherwise renders today's signed percentage
 * — including a genuinely measured `0.0` at day N, which is a fact and must
 * print, never suppressed just because the number is zero. The gate is
 * `days`, never the value.
 */
export function formatTotalReturn(totalReturn, days) {
  if (days === 0 || totalReturn == null || Number.isNaN(totalReturn)) return '—'
  return `${totalReturn >= 0 ? '+' : ''}${(totalReturn * 100).toFixed(2)}%`
}

/**
 * Map an api.js error (apiGet/apiPost — `err.status` set, `err.message`
 * always the literal `Backend returned ${status}`) to a human sentence,
 * mirroring StrategyPassport.jsx's PaperDeployCard mapping. Never falls
 * back to `err.message` once `err.status` is set — that message is always
 * the raw "Backend returned NNN" string and must never reach the
 * `role="alert"` card verbatim. `fallback` is used only for a status-less
 * error (a genuine network failure, where `err.message` — e.g. "Failed to
 * fetch" — is actually informative) or a missing error object.
 */
export function paperErrorMessage(err, fallback = 'Something went wrong.') {
  if (!err) return fallback
  if (err.status === 401) return 'Your session expired — sign in again to see your paper deployments.'
  if (err.status === 404) return 'This deployment is no longer available on your account — reload the list.'
  if (err.status != null) return 'Paper trading is temporarily unavailable — try again in a moment.'
  return err.message || fallback
}

// ── Intraday marks (design §5.1) ─────────────────────────────────────────────
//
// A mark is a re-PRICING of the ASSET BASKET the daily replay established —
// not a re-decision, and NOT a claim about what the strategy is holding right
// now (see MARK_BASIS_DISCLOSURE: v1 has no position vector, so a strategy
// sitting in cash is still marked as if invested). The settled daily ledger is
// the paper track record — Arc testnet, no real funds (#1807); a mark is an
// unsettled decoration the backend deletes past 90 days.
// Every helper below exists so the card can never state more than that:
//
//   - markLabel: never a bare number. Always value + as-of time, and the word
//     "delayed" whenever the row says so. `is_delayed` is a STORED column set
//     by the fetch path from what the provider declares — this function reads
//     that fact, it does not infer delay from a timestamp.
//   - The existence gate is `mark == null`, never the mark's value — the same
//     discriminator lesson as formatTotalReturn's `days`: a genuinely marked
//     flat 0.00% is a measurement and must print, while an absent mark must
//     never be dressed as "+0.00%".
//   - marksStalenessNote: a frozen number must read as "last marked Friday
//     16:00", not as a broken ticker. This is the #1378 shape — a time-labelled
//     number going stale across a weekend/gap — so the note states the OBSERVED
//     age and never asserts a market state ("closed", "halted") the client has
//     no way to know.
//   - markBasisNote / MARK_BASIS_DISCLOSURE: the v1 limitation, disclosed AT
//     THE POINT OF RENDER rather than only in docs — see below.

/** HH:MM in UTC for a mark's `ts`, or null if the timestamp is unusable.
 * Never renders "Invalid Date" and never throws — same defensive contract as
 * formatDriftDate. */
function formatUtcTime(iso) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleTimeString('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' })
}

/** "Fri 16:00" in UTC, for a mark old enough that the day matters. */
function formatUtcDayTime(iso) {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const day = d.toLocaleDateString('en-US', { timeZone: 'UTC', weekday: 'short' })
  return `${day} ${formatUtcTime(iso)}`
}

/**
 * The live-value line: `+0.42% · as of 14:45 UTC · delayed`.
 *
 * `portfolio_value` is an INDEX with 1.0 == deploy-time capital (there is no
 * deployed-capital amount anywhere in the system, so a dollar figure would be
 * invented), which makes the percentage `value - 1`.
 *
 * Returns '—' only when there is no mark at all, or when the mark carries no
 * usable value or timestamp — because a value without its as-of time is
 * exactly the bare number §2.4 rule 3 forbids, and half a claim is worse than
 * none. The gate is never the value itself.
 */
export function markLabel(mark) {
  if (!mark) return '—'
  const value = mark.portfolio_value
  if (value == null || Number.isNaN(value)) return '—'
  const at = formatUtcTime(mark.ts)
  if (!at) return '—'
  const pct = (value - 1) * 100
  const signed = `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`
  return `${signed} · as of ${at} UTC${mark.is_delayed ? ' · delayed' : ''}`
}

/**
 * A screen-reader-friendly restatement of the same line. The number updates
 * silently inside a live region otherwise, which is invisible to a screen
 * reader (4.1.3) — the as-of time belongs in the accessible name, not only in
 * the visual glyphs.
 */
export function markAnnouncement(mark) {
  if (!mark) return ''
  const label = markLabel(mark)
  if (label === '—') return ''
  const at = formatUtcTime(mark.ts)
  const pct = ((mark.portfolio_value - 1) * 100).toFixed(2)
  const delayed = mark.is_delayed ? ', from a delayed feed' : ''
  return `Live value ${pct} percent, as of ${at} UTC${delayed}. Unsettled — the daily ledger is the track record.`
}

/**
 * A note when the newest mark has stopped moving — null while it is fresh.
 *
 * "Fresh" is two cadence intervals (`intervalMinutes`, default 15): one missed
 * tick is a hiccup, two is a state worth naming. Equities have market hours and
 * crypto does not, so an equity deployment's value is GENUINELY frozen
 * overnight and at weekends; the note makes that read as an observation age
 * rather than as a broken ticker. It deliberately does NOT say "market closed"
 * — the client cannot observe that, and #1378 is exactly the defect of
 * labelling a gap with a window nobody measured.
 */
export function marksStalenessNote(mark, now = Date.now(), intervalMinutes = 15) {
  if (!mark) return null
  const t = new Date(mark.ts).getTime()
  if (Number.isNaN(t)) return null
  const ageMinutes = (now - t) / 60000
  if (ageMinutes < intervalMinutes * 2) return null
  const when = formatUtcDayTime(mark.ts)
  return when ? `Last marked ${when} UTC — no newer price has been observed since.` : null
}

/**
 * The no-marks-yet state's reason. A deployment created between ticks, or one
 * on SPY before the session opens, legitimately has zero marks — a real state,
 * not an error, and one that renders as an em-dash WITH this reason rather
 * than as a measured-looking +0.00%.
 *
 * Gated on `status` for the same reason driftTooltip is: the marks loop filters
 * on STATUS_ACTIVE, so a stopped deployment will never get a mark and telling
 * its owner to wait for one would be false.
 */
export function noMarksNote(status) {
  return status === 'active'
    ? 'No live value yet — none has been marked for this deployment. The daily settle is the graded number.'
    : 'No live value — marks stop when a deployment is stopped.'
}

/**
 * THE DISCLOSED v1 LIMITATION, in the two places a reader can actually meet
 * it: the page intro and the live-value line itself.
 *
 * A mark re-prices the strategy's ASSET BASKET — the sleeve weights the last
 * daily settle established — by applying each asset's move since that settle.
 * It does NOT know whether the strategy is currently invested or sitting in
 * CASH: the backend's `replay_spec` returns dated portfolio returns, not a
 * per-sleeve invested/flat vector, so there is no position vector to read and
 * inferring one from the return series would be a guess dressed as a
 * measurement.
 *
 * The consequence is user-visible and must be stated, not implied: a strategy
 * that is flat can show a live value that moves. A settled +0.00% day can sit
 * under a +10.00% mark if the underlying rose 10% (the backend pins that exact
 * fixture in test_a_cash_sleeve_is_still_marked_as_if_invested).
 *
 * The earlier copy here said a mark "re-prices that same position", which is
 * precisely the claim v1 cannot make — it re-prices the BASKET, and the
 * position may be cash. That wording is what this constant replaces.
 *
 * Kept as an exported constant rather than inlined in the JSX so the wording
 * is pinned by a test: the honesty claim is the product, and a silent edit to
 * it should fail a build, not ship.
 */
export const MARK_BASIS_DISCLOSURE =
  "Marks re-price the strategy's asset basket and do not know whether the strategy is currently in cash — " +
  'a flat strategy can still show a live value that moves. The daily settle is the honest number.'

/**
 * The short form of the same disclosure, for the live-value line under a
 * single deployment's total return, where the long sentence does not fit.
 * Says the same thing — never a softer version of it.
 */
export const MARK_BASIS_SHORT = 'basket re-priced · cash not modelled'

/**
 * The disclosure to render beside a live value. Returns the short form when
 * there is a value on screen to qualify, and `null` when there is not: a
 * limitation notice attached to an em-dash qualifies nothing and just adds
 * noise to the one state that is already fully honest.
 */
export function markBasisNote(mark) {
  return mark && markLabel(mark) !== '—' ? MARK_BASIS_SHORT : null
}

/**
 * The marks-fetch-failure state. The deployment card itself loaded fine; only
 * the live value is missing — a partial failure the card must state rather
 * than paper over. Routes through paperErrorMessage so a raw
 * "Backend returned 503" can no more reach this line than the main error card,
 * and says the value is UNAVAILABLE rather than showing the last mark it
 * happened to hold: a stale number under a fresh-looking label is the same
 * defect as writing a duplicated stale row, just in the UI.
 */
export function marksUnavailableNote(err) {
  return `Live value unavailable — ${paperErrorMessage(err, 'the intraday feed could not be reached.')}`
}

// ── The verdict of record, beside the forward record (#1764) ─────────────────
//
// Deploy is AT WILL: paper_routes checks ownership and that the stored spec
// still validates, and nothing else — a strategy whose gate said `fail`,
// `pending` or `degenerate` can be paper-traded exactly like one that passed
// (docs/claims-ledger.md: "A failing strategy stays a failing strategy.
// Paper-trading one is allowed. Relabelling one is not."). Owner decision, Dan
// 2026-09-01: that is the POINT — a gate-failed strategy that performs badly
// forward validates the gate, and one that passed and tracks its backtest
// validates it too.
//
// That freedom is only honest if the verdict travels with the numbers. Without
// it, /app/paper renders "+2.10% · total return" for a strategy the rigor gate
// rejected, with nothing on the card saying so — a performance figure standing
// alone reads as an endorsement, and the reader has no way to know the gate
// ever ran. So every helper below exists to make one thing structurally true:
//
//   THE PAPER CARD NEVER SHOWS A PERFORMANCE NUMBER WITHOUT THE GATE VERDICT
//   BESIDE IT — including when the payload carries no verdict at all.
//
// `gateVerdict` has no silent arm. A missing `rigor_gate_status` (an old
// backend behind a new bundle, a truncated payload) yields the explicit
// "verdict unavailable" state, never `null` and never an empty chip: an
// absence has to be rendered as an absence, which is the same rule
// formatTotalReturn's day-0 em-dash follows.
//
// The four states and their words come from the shared modules, not from
// re-typed literals here: `rigorGateStatus.js` owns the four-state list and
// the unknown-state fallback, `libraryStatus.js` owns the tooltip sentences
// for ungraded and degenerate rows. #1358 is what happens when two surfaces
// keep their own copies of the same verdict vocabulary.

/** The `gate_version` the verdict-of-record migration writes on a verdict it
 * INFERRED from pre-existing columns rather than one a gate run produced.
 *
 * Byte-identical to `rigor_gate_version.LEGACY_DERIVED` on the backend —
 * `backend/tests/test_paper_deploy_verdict.py` reads this file and asserts the
 * two literals match, so the UI cannot start describing a legacy row as a real
 * grade because someone renamed the marker on one side.
 */
export const LEGACY_DERIVED_GATE_VERSION = 'legacy-derived'

/** What the chip says when the payload carried no verdict at all.
 *
 * NOT "not yet graded": that is a CLAIM about the strategy (no gate ran), and
 * this state cannot support it — the gate may well have run and the answer
 * simply did not arrive. The honest statement is about the payload.
 */
export const VERDICT_UNAVAILABLE_LABEL = 'Gate: verdict unavailable'

export const VERDICT_UNAVAILABLE_TITLE =
  'This deployment payload carried no rigor-gate verdict, so none is shown. ' +
  'That is a gap in what was served — not a statement that the strategy passed, ' +
  'failed, or was never graded. Reload; if it persists the passport page carries the verdict of record.'

/** Why a legacy-derived verdict is not a grade. Appended to the tooltip of any
 * verdict whose `gate_version` is the migration marker. */
export const LEGACY_DERIVED_NOTE =
  'This verdict was inferred from pre-existing columns by the verdict-of-record migration — ' +
  'no gate run produced it, and it is not comparable to a freshly graded verdict.'

/** The page-level statement of the deploy-at-will rule.
 *
 * Kept as a pinned constant for the same reason MARK_BASIS_DISCLOSURE is: the
 * honesty claim is the product. It states the permission AND its limit in one
 * breath — the gate result is unchanged by deploying, and the forward record is
 * not a re-grade. Checked against docs/claims-ledger.md ("A failing strategy
 * stays a failing strategy. Paper-trading one is allowed. Relabelling one is
 * not."): no "validated", no "proves", no promotion of a paper return into a
 * verdict.
 */
export const DEPLOY_AT_WILL_NOTE =
  'Any strategy you can open can be paper-traded — whether its rigor gate passed, failed, ' +
  'never ran, or had nothing it could score. Deploying changes no verdict: every card below ' +
  'shows the gate result it was graded with, beside the forward record it is building.'

/** The one sentence that must be on this surface and nowhere weakened: the
 * forward ledger is evidence ABOUT the gate, not a re-grade of the strategy. */
export const FORWARD_EVIDENCE_NOTE =
  'The gate verdict was graded once, before deployment; the forward record beside it is ' +
  'evidence that tests the gate. Neither re-labels the other — a paper return does not ' +
  'overturn a failed gate, and a passed gate does not vouch for a paper return.'

/** `graded_at` as a plain human date, or null when it is absent/unusable.
 *
 * Parses the leading `YYYY-MM-DD` and rebuilds the day in UTC rather than
 * handing the raw string to `new Date`. `graded_at` is `datetime.isoformat()`
 * of a NAIVE column, so it arrives without an offset ("2026-08-30T12:00:00")
 * and `new Date` would read it as LOCAL time — which silently renders the
 * previous day for anyone west of UTC. A grade date that moves with the
 * reader's timezone is a fabricated date.
 *
 * Returns null rather than a placeholder so the caller can omit the clause
 * entirely: "(graded —)" would be worse than no parenthetical at all.
 */
export function formatGradedAt(gradedAtIso) {
  if (typeof gradedAtIso !== 'string') return null
  const m = gradedAtIso.match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!m) return null
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleDateString('en-US', { timeZone: 'UTC', year: 'numeric', month: 'short', day: 'numeric' })
}

// The four states' words. Written as "Gate: <verdict>" so the chip is
// self-describing next to a percentage — "failed" alone, beside +2.10%, is
// ambiguous about WHAT failed.
const _VERDICT_WORDS = Object.freeze({
  pass: 'Gate: passed',
  fail: 'Gate: failed',
  pending: 'Gate: not yet graded',
  degenerate: 'Gate: unevaluable — flat returns',
})

const _VERDICT_TONES = Object.freeze({
  pass: 'positive',
  fail: 'negative',
  pending: 'muted',
  degenerate: 'muted',
})

// The four explanations, WITHOUT the shared forward-evidence sentence —
// `gateVerdict` appends that to all four, so the sentence exists once and every
// graded state carries it. `pending` and `degenerate` borrow the Library's own
// sentences rather than re-typing them: two surfaces, one explanation.
const _VERDICT_TITLES = Object.freeze({
  pass: 'The rigor gate graded this strategy and it passed.',
  fail:
    'The rigor gate graded this strategy and it did not pass. Paper-trading it anyway is ' +
    "deliberate: a rejected strategy's forward record is evidence about the gate's call.",
  pending: NOT_GRADED_TITLE,
  degenerate: DEGENERATE_TITLE,
})

/**
 * The gate verdict for one deployment-summary row. NEVER null, for any input.
 *
 * Returns `{ status, label, title, tone, gradedLabel }`:
 *   - `status`     the raw four-state string, or null when none was served;
 *   - `label`      the chip's words, always a non-empty string;
 *   - `gradedLabel` "graded Aug 30, 2026", or null when there is no usable
 *                  date — never a fabricated one;
 *   - `tone`       'positive' | 'negative' | 'muted' | 'unknown', for colour
 *                  only. Only a literal `pass` is ever 'positive'.
 *
 * The arm order matters and mirrors `libraryStatus.js`: a payload with no
 * verdict field is caught FIRST, because every arm below it would otherwise
 * answer a question the payload never asked. An unrecognised state renders the
 * shared em-dash and warns in dev — the UI must not map a fifth state the API
 * grew onto "failed" (or onto "passed", which would be worse).
 */
export function gateVerdict(dep) {
  const status = dep?.rigor_gate_status
  if (status == null) {
    return {
      status: null,
      label: VERDICT_UNAVAILABLE_LABEL,
      title: VERDICT_UNAVAILABLE_TITLE,
      tone: 'unknown',
      gradedLabel: null,
    }
  }
  if (isUnknownRigorGateStatus(status)) {
    warnUnknownRigorGateStatus(status, 'PaperTrading')
    return {
      status,
      label: `Gate: ${UNKNOWN_RIGOR_LABEL}`,
      title: UNKNOWN_RIGOR_TITLE,
      tone: 'unknown',
      gradedLabel: null,
    }
  }
  const graded = formatGradedAt(dep?.graded_at)
  const legacy = dep?.gate_version === LEGACY_DERIVED_GATE_VERSION
  // Composed here rather than baked into the constants so the forward-evidence
  // sentence lands on all four states exactly once — appending it at the render
  // site instead would double it on the two arms that already carried it.
  const base = legacy ? `${_VERDICT_TITLES[status]} ${LEGACY_DERIVED_NOTE}` : _VERDICT_TITLES[status]
  const title = `${base} ${FORWARD_EVIDENCE_NOTE}`
  return {
    status,
    label: _VERDICT_WORDS[status],
    title,
    tone: _VERDICT_TONES[status],
    gradedLabel: graded ? `graded ${graded}` : null,
  }
}

/**
 * The verdict as ONE line — "Gate: failed (graded Aug 30, 2026)".
 *
 * Both the visible chip and the screen-reader announcement render from this,
 * so the two can never state different verdicts for the same row.
 */
export function gateVerdictText(dep) {
  const v = gateVerdict(dep)
  return v.gradedLabel ? `${v.label} (${v.gradedLabel})` : v.label
}

/**
 * The screen-reader line for the headline figure — the number and the verdict
 * in ONE accessible name.
 *
 * This is where the "never a number without its verdict" rule is enforced for
 * assistive tech rather than merely arranged visually: the percentage is a
 * `<span aria-hidden>` and THIS string is what a screen reader gets, so a
 * future edit that drops the chip from the layout cannot also silently drop
 * the verdict from the announcement — they come from the same call.
 *
 * The performance clause follows formatTotalReturn's discriminator exactly:
 * `days === 0` is the normal post-deploy state, not a measurement, and must
 * never be announced as one.
 */
export function paperReturnAnnouncement(dep) {
  const figure = formatTotalReturn(dep?.total_return, dep?.days)
  const days = dep?.days
  const perf =
    figure === '—'
      ? 'No settled paper return yet'
      : `Paper total return ${figure} over ${days} trading day${days === 1 ? '' : 's'}`
  return `${perf}. ${gateVerdictText(dep)}.`
}

// ── Page-intro cadence copy (#1802) ──────────────────────────────────────────
//
// The intro used to tell EVERY reader, unconditionally, that the live value
// "re-prices the strategy's asset basket every 15 minutes". That sentence is a
// claim about a job that runs: `backend/archimedes/services/paper_marks.py` and
// `backend/archimedes/scripts/run_paper_marks.py` exist, but nothing under `infra/` schedules
// them (grep -rn paper_marks infra/ -> no hits), so in production no marks are
// written and the 15-minute cadence is a promise the deployment does not keep.
//
// The graded truth is the DAILY settle: `paper_trading.py`'s advance loop runs
// on PAPER_ADVANCE_INTERVAL_HOURS (default 24) and appends one real-data return
// per trading day. That sentence is true today and is therefore unconditional.
// The intraday sentence is now earned per-render: it appears only when the
// payload actually carries a mark fresh enough to back it, and a mark that has
// stopped moving gets the existing staleness note instead of a cadence claim.
//
// The freshness rule is NOT a new number: it is exactly marksStalenessNote's —
// two cadence intervals — so the page and the per-card line can never disagree
// about whether the same mark is live.

/**
 * The unconditional sentence: what the ledger does today, in production,
 * without any job that is not deployed. `advance_all` settles one graded
 * trading day at a time; a mark, when one exists, is decoration on top of it
 * and is labelled unsettled.
 *
 * It says what the settled series IS — a paper track record on Arc testnet,
 * with no real funds — never what it may one day become. An earlier draft of
 * this constant promised the series would carry over at a cutover that #1240
 * cancelled by owner call, with no date scheduled; #1822 retracted that promise
 * from every paper-trading copy surface, and this wording is what it pins. The
 * sentence is written across a `' + '` concatenation on purpose: #1822's guard
 * flattens that before reading it, so where this line happens to wrap can never
 * decide the verdict.
 */
export const PAPER_SETTLE_CADENCE =
  'The ledger settles once per trading day from the graded replay — that settled series is a paper ' +
  'track record on Arc testnet, with no real funds. A live value is shown beneath it only when one ' +
  'has been marked, and it is always unsettled.'

/**
 * The conditional sentence. Only rendered when `paperCadenceCopy` is handed a
 * mark that is actually fresh — never as a standing promise about a job.
 */
export const PAPER_INTRADAY_CADENCE =
  "The live value re-prices the strategy's asset basket every 15 minutes — it is unsettled, carries the " +
  'time it was observed, and never changes what the strategy does.'

/**
 * The intro's cadence copy for the marks actually in the payload.
 *
 * - no usable mark  -> the daily-settle sentence ALONE. No cadence is claimed,
 *   because with no marks job deployed there is no cadence to claim.
 * - fresh mark      -> the settle sentence plus the 15-minute sentence, which
 *   the observed mark has now earned.
 * - stale mark      -> the settle sentence plus `marksStalenessNote`'s existing
 *   observation-age wording. Never the cadence sentence: a mark that stopped
 *   arriving is the exact case where "every 15 minutes" is false.
 *
 * `intervalMinutes` is threaded straight through to marksStalenessNote so the
 * two surfaces share one definition of "fresh".
 */
export function paperCadenceCopy(mark, now = Date.now(), intervalMinutes = 15) {
  const sentences = [PAPER_SETTLE_CADENCE]
  if (!mark || markLabel(mark) === '—') return { sentences, intraday: false, staleness: null }
  const staleness = marksStalenessNote(mark, now, intervalMinutes)
  if (staleness) return { sentences, intraday: false, staleness }
  sentences.push(PAPER_INTRADAY_CADENCE)
  return { sentences, intraday: true, staleness: null }
}

/**
 * The newest mark across every deployment on the page, or null when there is
 * none — the input `paperCadenceCopy` is gated on.
 *
 * Prefers the polled list for a deployment and falls back to the summary's
 * `latest_mark` (paper_trading.py:1088), matching LiveValue's own precedence so
 * the intro can never claim a cadence the cards below it are not showing. A
 * mark with an unusable timestamp is skipped rather than ordered arbitrarily.
 */
export function newestMark(deployments, marksById = {}, errorsById = {}) {
  let best = null
  let bestTs = -Infinity
  for (const dep of deployments || []) {
    const id = dep?.deployment_id
    // Same precedence as LiveValue, error branch included: a deployment whose
    // marks fetch failed shows NO number on its card, so it must not supply
    // the intro's cadence claim either.
    if (errorsById[id]) continue
    const polled = marksById[id]
    const latest = polled && polled.length > 0 ? polled[polled.length - 1] : dep?.latest_mark
    if (!latest) continue
    const t = new Date(latest.ts).getTime()
    if (Number.isNaN(t)) continue
    if (t > bestTs) {
      best = latest
      bestTs = t
    }
  }
  return best
}
