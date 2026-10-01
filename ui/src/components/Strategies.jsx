import { useState, useEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
// EfficientFrontier + CorrelationMatrix deleted (Issue #383) — synthetic RNG data
import RigorExplainer from './RigorExplainer'
// The rigor-strictness CONTROL is no longer mounted on this page (#1645) — it
// lives on the Strategy Passport, where the level is applied to a decision.
// Only its label lookup is still needed here, for DeployabilityChip, and that
// now has its own module so this page imports no part of the control.
import { levelLabel } from '../rigorLevels'
import { useRigorStrictness, BADGE_LEVEL } from '../hooks/useRigorStrictness'
import useDialogFocus from '../hooks/useDialogFocus'
import { ROADMAP_SURFACES_ENABLED } from '../featureFlags.js'

import { apiGet, apiPost, apiDelete } from '../api'
import { compactCostCell } from '../generationCost.js'
import MetricValue from './MetricValue'
import {
  isUnknownRigorGateStatus,
  warnUnknownRigorGateStatus,
  UNKNOWN_RIGOR_LABEL,
  UNKNOWN_RIGOR_TITLE,
} from '../rigorGateStatus.js'
import { statusTag, statusLabel, statusTitle } from '../libraryStatus.js'
import { signClass } from '../signClass.js'
import { metricsSourceNote } from '../metricsSource.js'
import { paperAttributionHeader } from '../paperAttribution.js'
import { strategies as ROADMAP_COPY } from '../roadmapCopyApp.js'
import {
  barName,
  checkLine,
  failedChecks,
  isUnattributed,
  notComputedChecks,
  passedChecks,
  recordedReason,
  rejectedSectionSummary,
  showsRejectionReasons,
} from '../rejectionReasons.js'

// A compact "deployable at your level" chip for a library row, driven by the
// strategy's min_passing_level (from the live gate) and the user's strictness.
function DeployabilityChip({ deploy, level, gatePending }) {
  // Vaults are out of the MVP cut (#1266) — and EVERY branch below answers a
  // VAULT question. "deployable" / "needs level N" / "blocked" all grade a
  // strategy against `/api/selection-bias/gate`, the same verdict the vault
  // deploy gate reads (`api/vaults_routes.py::_deployable_levels`). With the
  // roadmap flag off there is no vault to deploy into, so every one of those
  // chips grades a strategy against a capability the shipped build does not
  // have. On the Examples tab that is what put a red `blocked` pill on
  // hand-curated reference implementations.
  //
  // Suppressing only the red branch was the other option, and it is worse: it
  // would leave the green "deployable" claim standing on a surface where
  // nothing can be deployed — the flattering half of a verdict kept and the
  // rest hidden. The whole chip belongs to the flag, and comes back intact
  // (blocked branch included) under VITE_ROADMAP_SURFACES=true.
  //
  // The gate FETCH is gated on the same flag in load() below: a hidden
  // annotation that still costs the page its slowest request is the #1324
  // defect, not a fix.
  if (!ROADMAP_SURFACES_ENABLED) return null
  // #1645: the Library now renders rows as soon as the strategy list resolves,
  // without waiting for the (much slower) /api/selection-bias/gate call. During
  // that window there is no entry for this row yet — and a SILENTLY ABSENT chip
  // is not an honest rendering of "we haven't asked yet": the reader cannot tell
  // it apart from a strategy the gate genuinely had nothing to say about. Say so.
  if (!deploy) {
    if (!gatePending) return null
    return (
      <span
        className="tag tag-muted lib-chip-checking"
        style={{ fontSize: '0.66rem' }}
        title="Still loading the live rigor gate for this row — not a verdict"
      >
        checking&hellip;
      </span>
    )
  }
  // #1358: a strategy the gate never scored (no persisted backtest data) is
  // NOT the same claim as "fails the rigor gate even at the loosest level" —
  // both used to collapse to min_passing_level == null below. Checked before
  // blocked_by_floor too: a never-scored row can't have failed a floor it was
  // never evaluated against. Neutral tag-muted, no "not deployable" wording.
  if (deploy.pending) {
    return <span className="tag tag-muted" style={{ fontSize: '0.66rem' }} title="Not yet evaluated — no backtest data for the rigor gate to score">pending</span>
  }
  // #1358 round-3: a zero-variance persisted series is the THIRD unevaluable
  // case, and it was the loudest wrong one. A degenerate series leaves
  // dsr_p_value and oos_sharpe at None, which trips blocked_by_floor — so this
  // row arrived with blocked_by_floor === true and rendered "Fails an always-on
  // correctness floor", asserting a measurement that never happened. Checked
  // before blocked_by_floor for exactly the reason pending is: you cannot fail
  // a floor nothing measured you against. Same neutral treatment as pending,
  // with its own honest sentence (the same wording the backend hands the LLM in
  // agents/portfolio_agent.py `_format_strategies`) rather than borrowing pending's —
  // "not yet evaluated" would be a second false claim, since the data IS here.
  if (deploy.degenerate) {
    return <span className="tag tag-muted" style={{ fontSize: '0.66rem' }} title="DEGENERATE — the persisted return series is zero-variance (broken data or a zero-trade backtest), not a real evaluation">unevaluable</span>
  }
  if (deploy.blocked_by_floor) {
    return <span className="tag tag-negative" style={{ fontSize: '0.66rem' }} title="Fails an always-on correctness floor — cannot deploy at any level">blocked</span>
  }
  const min = deploy.min_passing_level
  if (min == null) {
    return <span className="tag tag-muted" style={{ fontSize: '0.66rem' }} title="Does not pass the rigor gate even at the most permissive level">not deployable</span>
  }
  if (min <= level) {
    const label = min > BADGE_LEVEL ? `deployable @ ${levelLabel(null, min)}+` : 'deployable'
    return <span className="tag tag-positive" style={{ fontSize: '0.66rem' }} title={`Passes at your strictness (level ${level})`}>{label}</span>
  }
  return <span className="tag tag-accent" style={{ fontSize: '0.66rem' }} title={`Raise your strictness to level ${min} to deploy`}>needs {levelLabel(null, min)}</span>
}

const STATUS_ORDER = ['live', 'validated', 'candidate', 'retired']

// A one-word provenance mark that rides directly under a row's headline
// number. `metricsSourceNote` allow-lists only the sources that are NOT a run
// made here (see ../metricsSource.js), so a real persisted backtest renders
// unmarked and an unknown/absent value makes no claim in either direction.
function MetricsSourceTag({ source }) {
  const note = metricsSourceNote(source)
  if (!note) return null
  return (
    <div style={{ fontSize: '0.68rem', color: 'var(--text-4)' }} title={note.title}>
      {note.label}
    </div>
  )
}

function downloadStrategy(strategy, format) {
  let content, filename, type
  if (format === 'json') {
    content = JSON.stringify(strategy, null, 2)
    filename = `strategy-${(strategy.id || 'unknown').slice(0, 8)}.json`
    type = 'application/json'
  } else {
    const rows = [
      ['Field', 'Value'],
      // Two distinct facts, exported as two rows. 'Paper Title' sits directly
      // above the paper's authors and year, so labelling it 'Title' while a
      // generated row's own name occupied it made the export claim the
      // strategy name was the citation.
      ...(strategy.strategy_name ? [['Strategy Name', strategy.strategy_name]] : []),
      ['Paper Title', strategy.paper_title],
      ['Authors', strategy.paper_authors?.join(', ')],
      ['Year', strategy.paper_year],
      ['Status', strategy.status],
      ['Sharpe', strategy.sharpe_ratio],
      ['CAGR', strategy.cagr],
      ['Max Drawdown', strategy.max_drawdown],
      ['Methodology', strategy.methodology_summary],
      ['Assets', strategy.asset_universe?.join(', ')],
      ['Methodology Hash', strategy.methodology_hash],
    ]
    content = rows.map(r => r.map(c => `"${String(c ?? '').replace(/"/g, '""')}"`).join(',')).join('\n')
    filename = `strategy-${(strategy.id || 'unknown').slice(0, 8)}.csv`
    type = 'text/csv'
  }
  const blob = new Blob([content], { type })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url; a.download = filename; a.click()
  URL.revokeObjectURL(url)
}

// No local number formatter lives here any more. Every metric this file renders
// goes through MetricValue / metricDomain.js (#1651): the old `fmt`/`fmtPct`
// pair had no idea what quantity it was formatting, which is precisely how a
// stored max_drawdown of 1.303 became a displayed "−130.3%". Re-adding one
// would give the next cell a way around the domain check —
// ui/test/metric-domain.test.js fails if either name comes back.

// "2002-01-01" -> Date; null on bad input
function isoToDate(iso) {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

// Backtest window in fractional years; null if either bound missing/bad
export function periodInYears(startIso, endIso) {
  const a = isoToDate(startIso), b = isoToDate(endIso)
  if (!a || !b) return null
  const days = (b - a) / 86_400_000
  return days > 0 ? days / 365.25 : null
}

// $1k -> $X over `years` at compound `cagr`. Returns null if either missing.
export function projectedEndValue(principal, cagr, years) {
  if (cagr == null || years == null) return null
  return principal * Math.pow(1 + cagr, years)
}

export function fmtUsd(n, fractionDigits = 0) {
  if (n == null) return '—'
  return n.toLocaleString('en-US', {
    style: 'currency', currency: 'USD',
    minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits,
  })
}

// ── Library Table ─────────────────────────────────────────────
//
// (The grid-card view's `BacktestHorizon` helper was deleted in #1361: it was
// never mounted anywhere in src/ or test/, and it carried the identical
// hard-coded `var(--positive)` bug this issue fixes below. Dead code that
// mis-colours a loss is worse than no code — resurrect it wired to
// `signClass` if the grid-card view comes back.)

// Compact tabular view — replaces the old big-card grid. Dense, scannable,
// sortable. Click a row → expand inline detail (period + paper-claim delta
// + rigor metrics). One row per strategy; no visual hierarchy by status (the
// STATUS column does that job).

// The multi-paper chip beside a strategy's name, in BOTH layouts (#1796).
//
// It used to read "N papers" under a `Fused from N papers` tooltip — the same
// citation-count-as-fusion-depth claim #1783 took off the passport (#1769), in
// two more places. A count of cited papers says nothing about how many of them
// were tied to a mechanism the strategy trades; on the strategy that surfaced
// this, all five contribution cells were em-dashes and the chip still said
// "fused".
//
// The chip is a COUNT, so it says what it counts — the same words the passport
// chip settled on. The attribution split has nowhere to fit in a 0.66rem pill,
// so it goes in the tooltip, where the old copy made its claim: the reader who
// hovers gets `paperAttributionHeader`'s heading verbatim, the same sentence
// the passport prints over its source-papers table. One vocabulary, three
// surfaces, one helper.
function PapersCitedChip({ papers, distinctMechanismPapers, style }) {
  const attribution = paperAttributionHeader(papers, distinctMechanismPapers, 'library')
  // Single-paper rows show their arxiv link / "Source paper" block instead —
  // a "1 paper cited" pill next to a name is noise, not information.
  if (!attribution || attribution.cited <= 1) return null
  return (
    <span className="tag tag-accent" style={style} title={attribution.heading}>
      {attribution.cited} papers cited
    </span>
  )
}

function StrategyRow({ s, isHighlighted, onOpenRigorExplainer, onOpenPassport, deploy, level, gatePending, extraActions }) {
  const [open, setOpen] = useState(isHighlighted)
  const rowRef = useRef(null)
  const years = periodInYears(s.backtest_start, s.backtest_end)
  const principal = 1000
  const endValue = projectedEndValue(principal, s.cagr, years)
  const startStr = (s.backtest_start || '').slice(0, 10)
  const endStr = (s.backtest_end || '').slice(0, 10)
  const paperCite = [
    s.paper_authors?.[0]?.split(' ').pop(),
    s.paper_year && `(${s.paper_year})`,
  ].filter(Boolean).join(' ')
  // A generated row carries its own name (the LLM's name for the strategy),
  // which is a DIFFERENT fact from the title of the paper it cites. Curated
  // rows have no separate name — there the paper title IS the strategy's
  // identity — so `strategy_name` is absent and nothing below changes for
  // them. See coerceGenerated: paper_title is the CITED PAPER, always.
  const rowLabel = s.strategy_name || s.paper_title
  const citedPaperTitle = s.strategy_name ? s.paper_title : null

  // Real API fields (backend/archimedes/api/schemas.py) — the singular-CI
  // and drift-boolean fields this used to read never existed in any API
  // response (#1361).
  const sharpeCI = s.sharpe_ci_lower != null && s.sharpe_ci_upper != null ? [s.sharpe_ci_lower, s.sharpe_ci_upper] : null
  // #1358: a strategy with ZERO statistics computed must render as honestly
  // unknown, never as a failed rigor gate. rigor_gate_status is the
  // four-state badge curated/generated StrategyResponse rows carry
  // ("pass"|"fail"|"pending"|"degenerate"). coerceGenerated now carries it
  // through on generated rows too (it is the verdict of record, overlaid from
  // strategy_passports), falling back to null only when the API sent none —
  // the same "no verdict yet" case passes_rigor_gate === null encodes. Both
  // checked so the same pending treatment applies on the Examples and
  // Generated tabs alike. Checked BEFORE the true/false badge below so a
  // pending row can never fall through to the "does not pass" X.
  const isPending = s.rigor_gate_status === 'pending' || s.passes_rigor_gate == null
  // #1358 round-3: "degenerate" is the fourth state, and it belongs in the same
  // NEUTRAL bucket as pending — never the red "does not pass" X, because
  // nothing was measurable to fail. It does NOT get pending's sentence: a
  // degenerate row HAS persisted returns (they are just flat), so "no backtest
  // data" would be a fresh lie. Kept as its own flag rather than folded into
  // isPending so the two states cannot share a tooltip.
  const isDegenerate = s.rigor_gate_status === 'degenerate'
  // Exhaustiveness default. Four states are known; a fifth would otherwise fall
  // through to the passes_rigor_gate booleans and render a confident verdict
  // this build cannot justify. Em-dash + a dev-time warning instead, shared
  // with StrategyPassport so both surfaces answer identically (#1358).
  const unknownRigor = isUnknownRigorGateStatus(s.rigor_gate_status)
  useEffect(() => {
    if (unknownRigor) warnUnknownRigorGateStatus(s.rigor_gate_status, 'Strategies')
  }, [unknownRigor, s.rigor_gate_status])
  const detailId = `lib-detail-${s.id}`
  // Absence is the point: a strategy nothing measured gets an em-dash and a
  // tooltip that says so, never a zero (#1326).
  const genCost = compactCostCell(s.generation_cost)

  useEffect(() => {
    if (isHighlighted && rowRef.current) {
      rowRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [isHighlighted])

  const rowStyle = {
    cursor: 'pointer',
    ...(isHighlighted ? { background: 'rgba(255,209,102,0.10)', outline: '1px solid var(--accent)' } : {}),
  }

  return (
    <>
      <tr ref={rowRef} className="lib-row cursor-pointer" onClick={() => setOpen(o => !o)} style={rowStyle}>
        <td className="font-semibold">
          {/* The disclosure is a real <button> in the first cell rather than a
              bare onClick on the <tr>: App.css hides the keyboard-accessible
              card list above 768px, so on desktop a keyboard-only user could
              open no strategy's detail panel at all — and with it none of
              "Open Passport", the exports, the source links or the DSR/PBO
              numbers, which live only in the expanded row (2.1.1 / 4.1.2).
              The row keeps its onClick as a mouse convenience; the button
              stops propagation so one activation is one toggle.
              aria-controls is conditional on `open` (same pattern as
              CustomSelect's listbox): the <tr id={detailId}> below only
              exists in the DOM while open, so an unconditional aria-controls
              pointed at a nonexistent id on every collapsed row — a #1318
              residual of the #1311/#1319 pass. */}
          <button
            type="button"
            className="lib-row-toggle"
            aria-expanded={open}
            aria-controls={open ? detailId : undefined}
            onClick={(e) => { e.stopPropagation(); setOpen(o => !o) }}
          >
            <span aria-hidden="true" className={`${open ? 'i-lucide-chevron-down' : 'i-lucide-chevron-right'} w-3 h-3 mr-1.5 text-[var(--text-4)] flex-shrink-0 inline-block`} />
            {rowLabel}
          </button>
          <PapersCitedChip
            papers={s.papers}
            distinctMechanismPapers={s.distinct_mechanism_papers}
            style={{ fontSize: '0.68rem', marginLeft: 6, verticalAlign: 'middle', padding: '1px 5px' }}
          />
        </td>
        <td className="caption">
          {citedPaperTitle && <div>{citedPaperTitle}</div>}
          {paperCite || (citedPaperTitle ? null : (s.paper_year ? `(${s.paper_year})` : '—'))}
        </td>
        <td>
          <div className="flex items-center gap-1.5 flex-wrap">
            <span
              className={`tag ${statusTag(s.status, s.passes_rigor_gate, s.rigor_gate_status)}`}
              title={statusTitle(s.status, s.passes_rigor_gate, s.rigor_gate_status)}
            >
              {statusLabel(s.status, s.passes_rigor_gate, s.rigor_gate_status)}
            </span>
            {/* These UnoCSS icons render as a CSS mask on an empty <span>, so
                without role/aria-label they contribute nothing to the
                accessible name tree and `title` on a bare span is not
                reliably exposed — the rigor-gate verdict, the fact that
                decides whether a strategy may be deployed, was sighted-only
                (1.1.1). */}
            {unknownRigor ? (
              <span role="img" aria-label="Rigor gate status unrecognised" className="mono text-[var(--text-4)]" title={UNKNOWN_RIGOR_TITLE}>{UNKNOWN_RIGOR_LABEL}</span>
            ) : isDegenerate ? (
              <span role="img" aria-label="Rigor gate could not evaluate — zero-variance return series" className="i-lucide-alert-circle w-3.5 h-3.5 text-[var(--text-4)]" title="DEGENERATE — the persisted return series is zero-variance (broken data or a zero-trade backtest), not a real evaluation" />
            ) : isPending ? (
              <span role="img" aria-label="Rigor gate pending" className="i-lucide-clock w-3.5 h-3.5 text-[var(--text-4)]" title="Not yet evaluated — no backtest data for the rigor gate to score" />
            ) : s.passes_rigor_gate === true ? (
              <span role="img" aria-label="Passes rigor gate" className="i-lucide-check w-3.5 h-3.5 text-[var(--positive)]" title="Passes rigor gate" />
            ) : s.passes_rigor_gate === false && (
              <span role="img" aria-label="Does not pass rigor gate" className="i-lucide-x w-3.5 h-3.5 text-[var(--text-4)]" title="Does not pass rigor gate" />
            )}
            <DeployabilityChip deploy={deploy} level={level} gatePending={gatePending} />
          </div>
        </td>
        <td className="mono" style={{ textAlign: 'right' }}>
          {/* Every measured number in this row goes through MetricValue, which
              is the only formatter that knows each metric's domain and cannot
              render a value outside it without saying so (#1651). */}
          <MetricValue metric="sharpe_ratio" value={s.sharpe_ratio} row={s} surface="Library table" />
          {sharpeCI && (
            <div style={{ fontSize: '0.68rem', color: 'var(--text-4)' }}>
              [<MetricValue metric="sharpe_ci_lower" value={sharpeCI[0]} row={s} surface="Library table" />, <MetricValue metric="sharpe_ci_upper" value={sharpeCI[1]} row={s} surface="Library table" />]
            </div>
          )}
          {s.dsr_p_value != null && (
            <div style={{ fontSize: '0.68rem', color: 'var(--text-4)' }}>
              (DSR conf=<MetricValue metric="dsr_p_value" value={s.dsr_p_value} row={s} surface="Library table" />)
            </div>
          )}
          {/* Sharpe is the representative field of the whole display block —
              the backend derives `display_metrics_source` from it because one
              link of the fallback chain populates all of them — so one mark in
              this cell describes the row's numbers, not four repeats. */}
          <MetricsSourceTag source={s.display_metrics_source} />
        </td>
        <td className={`mono ${signClass(s.cagr)}`} style={{ textAlign: 'right' }}>
          <MetricValue metric="cagr" value={s.cagr} row={s} surface="Library table" />
        </td>
        <td className="mono negative" style={{ textAlign: 'right' }}>
          <MetricValue metric="max_drawdown" value={s.max_drawdown} row={s} surface="Library table" />
          {/* Same defect: crossing the 0.5 overfitting threshold was signalled
              only by the colour swap to --negative (1.4.1). */}
          {s.pbo_score != null && (
            <div style={{ fontSize: '0.68rem', color: s.pbo_score > 0.5 ? 'var(--negative)' : 'var(--text-4)' }}>
              (PBO <MetricValue metric="pbo_score" value={s.pbo_score} row={s} surface="Library table" />{s.pbo_score > 0.5 && <span aria-hidden="true"> ⚠</span>})
              {s.pbo_score > 0.5 && (
                <span className="sr-only"> — above the 0.50 overfitting threshold</span>
              )}
            </div>
          )}
        </td>
        <td className={`mono ${signClass(endValue != null ? endValue - principal : null)}`} style={{ textAlign: 'right' }}>
          {fmtUsd(endValue)}
          {/* fmtPct prepends '-' for a losing CAGR, so colour alone is never the
              only signal there; fmtUsd never emits a sign (endValue can't go
              below 0), so this cell needs its own text alternative (1.4.1). */}
          {endValue != null && endValue < principal && (
            <span className="sr-only"> — below the {fmtUsd(principal)} starting principal</span>
          )}
        </td>
        <td className="caption" style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>{years != null ? `${years.toFixed(1)} yrs` : '—'}</td>
        <td
          className={genCost.measured ? 'mono' : 'caption'}
          style={{ textAlign: 'right', whiteSpace: 'nowrap', color: genCost.measured ? undefined : 'var(--text-4)' }}
          title={genCost.title}
        >{genCost.label}</td>
      </tr>
      {open && (
        <tr className="lib-row-detail" id={detailId}>
          <td colSpan={9} style={{ padding: '12px 18px', background: 'var(--glass)' }}>
            <StrategyDetailContent
              s={s}
              onOpenRigorExplainer={onOpenRigorExplainer}
              onOpenPassport={onOpenPassport}
              extraActions={extraActions}
              years={years}
              startStr={startStr}
              endStr={endStr}
            />
          </td>
        </tr>
      )}
    </>
  )
}

// Why THIS strategy was rejected — read off the row, never guessed.
//
// The rejected card showed "—" for Sharpe / CAGR / Max DD (a pre-backtest
// hypothesis has none) and "Gen tokens", and nothing else: the one fact a
// reader wanted — which rigor check said no — was the one fact absent, while
// the section above it asserted a population-wide reason nobody had measured.
//
// Everything below comes from `s.rigor_reasons`, the additive per-row field
// GET /api/strategies/generated serves, built from that strategy's OWN stored
// rigor_verdict against the gate's own thresholds (see
// backend/archimedes/services/rigor_reasons.py). Failed and not-computed are
// kept in SEPARATE lines on purpose: a check that ran and found a problem and a
// check that never ran are different claims, and only one of them may be
// printed as a failure. With no field on the row — an old payload, a degraded
// read, a curated row that never had one — the whole block renders nothing: no
// heading, no bar name, no prose. Scoped to rows the gate turned down
// (showsRejectionReasons), so a row still awaiting a verdict is never handed
// one it has not received.
function RejectionReasons({ s }) {
  if (!showsRejectionReasons(s)) return null
  const reason = recordedReason(s)
  const failed = failedChecks(s)
  const notComputed = notComputedChecks(s)
  const passed = passedChecks(s)
  const bar = barName(s)
  return (
    <div className="caption" style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 3, fontSize: '0.78rem' }}>
      {reason && (
        <div style={{ color: 'var(--text-3)' }}>
          <span style={{ color: 'var(--text-4)' }}>Reason on record:</span>{' '}
          <strong>{reason}</strong>
        </div>
      )}
      {failed.length > 0 && (
        <div style={{ color: 'var(--text-3)' }}>
          {/* Colour on a className, not an inline `color:` — the literal
              var(--positive)/var(--negative) here encodes a boolean check
              verdict, not the sign of a number, exactly like the rigor-gate
              check icon in StrategyRow. ui/test/sign-class.test.js polices the
              inline form so a signed cell can never quietly hard-code green. */}
          <span className="text-[var(--negative)]" style={{ fontWeight: 700 }}>Failed:</span>{' '}
          {failed.map(checkLine).join(' · ')}
        </div>
      )}
      {notComputed.length > 0 && (
        <div style={{ color: 'var(--text-4)' }}>
          <span style={{ fontWeight: 700 }}>Not computed:</span>{' '}
          {notComputed.map(checkLine).join(' · ')} — a check with nothing on record does not count as passed.
        </div>
      )}
      {passed.length > 0 && (
        <div style={{ color: 'var(--text-3)' }}>
          <span className="text-[var(--positive)]" style={{ fontWeight: 700 }}>Passed:</span>{' '}
          {passed.map(checkLine).join(' · ')}
        </div>
      )}
      {isUnattributed(s) && (
        <div style={{ color: 'var(--text-4)' }}>
          This strategy did not pass, but no check on record falls below the bar
          quoted here — the stored verdict does not attribute the rejection.
          Open the passport for the full record.
        </div>
      )}
      {bar && (failed.length > 0 || passed.length > 0) && (
        <div style={{ color: 'var(--text-4)' }}>Thresholds are the {bar} bar.</div>
      )}
    </div>
  )
}

// Shared "expanded" detail content — methodology / source paper(s) / rigor
// metrics / export actions. Used by both the desktop table row's expanded
// <tr> and the mobile card list's expanded panel, so the two layouts never
// drift out of sync with each other.
//
// `hideRejectionReasons` is set by the mobile card, which already renders the
// same block un-collapsed above the fold — the desktop table row has nowhere
// else to put it, so there it stays on.
function StrategyDetailContent({ s, onOpenRigorExplainer, onOpenPassport, extraActions, years, startStr, endStr, hideRejectionReasons }) {
  // The multi-paper heading, from the same helper the passport's source-papers
  // panel uses (#1783's `paperAttributionHeader`, #1796). It replaces
  // "Fused from N papers" — a citation count presented as fusion depth — with
  // the two counts kept apart, plus the sub-line that says what the smaller of
  // them means. The note is not optional: a heading ending in "· 0" with
  // nothing after it reads as a rendering bug, and the zero case is the one a
  // reader most needs told (#1636's honest-shortfall rule).
  //
  // The 'library' surface is not decoration. The COUNTS are the same on both
  // panels; the sentence under them is not, because a different thing is
  // underneath it. The passport's note says "the table below cites them",
  // pointing at a Contribution column the reader can scan. This panel renders
  // one italic title and an arXiv link per reference and nothing else — no
  // contribution cell, no per-paper split — so the passport's sentence would
  // describe a table that is not on the page. That is the same class of defect
  // as the header this replaced, one surface over. The helper owns both
  // wordings so neither can be retyped out of agreement with the other.
  //
  // `null` for an empty `papers`, which the `length > 1` branch below never
  // reaches — the single-paper "Source paper" block owns that case.
  const attribution = paperAttributionHeader(s.papers, s.distinct_mechanism_papers, 'library')
  return (
    <>
      <div className="text-[0.82rem]" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 18 }}>
        <div>
          <div className="label mb-2">Methodology</div>
          <div className="body">{s.methodology_summary || '—'}</div>
        </div>
        <div>
          {(s.papers || []).length > 1 ? (
            <>
              <div className="label mb-2">{attribution.heading}</div>
              <p className="caption mb-2">{attribution.note}</p>
              <div className="flex flex-col gap-2">
                {s.papers.map((p, idx) => (
                  <div key={p.arxiv_id || idx}>
                    <div className="body" style={{ fontStyle: 'italic' }}>
                      "{p.title || p.arxiv_id || '—'}"
                    </div>
                    {p.arxiv_id && (
                      <a
                        href={`https://arxiv.org/abs/${p.arxiv_id}`}
                        target="_blank" rel="noreferrer"
                        style={{ color: 'var(--accent)', fontSize: '0.75rem', marginTop: 3, display: 'inline-block' }}
                        onClick={(e) => e.stopPropagation()}
                      >
                        arxiv:{p.arxiv_id} ↗
                      </a>
                    )}
                  </div>
                ))}
              </div>
            </>
          ) : (
            <>
              <div className="label mb-2">Source paper</div>
              {/* `paper_title` is now null, not "", when no title resolves
                  (#1637 — an arXiv id printed in a title slot is a small
                  fabrication, so the server stopped substituting one). This
                  card printed the value inside literal quotation marks, so a
                  null rendered as an empty pair of quotes. Same phrasing the
                  Library card already uses when resolution genuinely fails:
                  say so, and name the id a reader can look up. */}
              {s.paper_title ? (
                <div className="body">"{s.paper_title}"</div>
              ) : (
                <div className="body text-[var(--text-3)]">
                  {s.paper_arxiv_id ? `title unavailable — arXiv:${s.paper_arxiv_id}` : 'no cited paper'}
                </div>
              )}
              <div className="caption mt-2">
                {s.paper_authors?.slice(0, 3).join(', ')}{s.paper_authors?.length > 3 ? ' et al.' : ''}
                {s.paper_year ? ` (${s.paper_year})` : ''}
                {s.paper_venue ? ` · ${s.paper_venue}` : ''}
              </div>
              {s.paper_arxiv_id && (
                <a
                  href={`https://arxiv.org/abs/${s.paper_arxiv_id}`}
                  target="_blank" rel="noreferrer"
                  style={{ color: 'var(--accent)', fontSize: '0.78rem', marginTop: 6, display: 'inline-block' }}
                  onClick={(e) => e.stopPropagation()}
                >
                  arxiv:{s.paper_arxiv_id} ↗
                </a>
              )}
            </>
          )}
        </div>
        <div>
          <div className="label mb-2 flex items-center gap-2">
            Rigor metrics
            {onOpenRigorExplainer && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); onOpenRigorExplainer() }}
                className="rigor-help-btn"
                aria-label="What is the rigor gate?"
                title="What is the rigor gate?"
              >
                ?
              </button>
            )}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10 }}>
            <div><div className="caption">DSR</div><div className="mono" style={{ fontWeight: 700 }}><MetricValue metric="deflated_sharpe_ratio" value={s.deflated_sharpe_ratio} row={s} surface="Library detail" /></div></div>
            {/* PBO is a probability in [0,1] and this cell renders it as a
                percentage — the DOMAIN is keyed off the metric, the format is
                the cell's choice, so the table's "0.62" and this "62.0%" stay
                two renderings of one bounded quantity. */}
            <div><div className="caption">PBO</div><div className="mono" style={{ fontWeight: 700 }}><MetricValue metric="pbo_score" value={s.pbo_score} row={s} format="pct" surface="Library detail" /></div></div>
            <div><div className="caption">OOS Sharpe</div><div className="mono" style={{ fontWeight: 700 }}><MetricValue metric="out_of_sample_sharpe" value={s.out_of_sample_sharpe} row={s} surface="Library detail" /></div></div>
          </div>
          {s.paper_claimed_sharpe != null && (
            <div className="caption mt-2">
              Paper claim: <strong><MetricValue metric="paper_claimed_sharpe" value={s.paper_claimed_sharpe} row={s} surface="Library detail" /></strong> · Backtest: <strong><MetricValue metric="sharpe_ratio" value={s.sharpe_ratio} row={s} surface="Library detail" /></strong>
              {/* The pass/fail judgement against the 50% replication threshold
                  used to live in the green/red class alone — "(43%)" and
                  "(97%)" rendered identically to a colourblind reader (1.4.1).
                  The ✓/✗ glyph carries it now, with the threshold spelled out
                  for assistive tech; colour stays as reinforcement. */}
              {s.sharpe_ratio != null && (() => {
                const ratio = s.paper_claimed_sharpe > 0.01 ? s.sharpe_ratio / s.paper_claimed_sharpe : null
                const replicated = ratio != null && ratio >= 0.5
                return (
                  <span className={replicated ? 'positive' : 'negative'} style={{ marginLeft: 6 }}>
                    <span aria-hidden="true">{replicated ? '✓' : '✗'}</span>{' '}
                    ({ratio != null ? `${(ratio * 100).toFixed(0)}%` : '—'})
                    <span className="sr-only">
                      {' '}
                      {ratio == null
                        ? 'replication ratio unavailable'
                        : replicated
                          ? 'of the paper claim — at or above the 50% replication threshold'
                          : 'of the paper claim — below the 50% replication threshold'}
                    </span>
                  </span>
                )
              })()}
            </div>
          )}
          {years != null && (
            <div className="caption mt-1.5">
              Window: <span className="mono">{startStr} → {endStr}</span>
            </div>
          )}
        </div>
      </div>
      {!hideRejectionReasons && <RejectionReasons s={s} />}
      <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {onOpenPassport && (
          <button
            className="btn btn-primary btn-sm"
            onClick={(e) => { e.stopPropagation(); onOpenPassport(s.id) }}
            title="Open the full strategy passport"
          >
            Open Passport →
          </button>
        )}
        {extraActions?.(s)}
        <button
          className="btn btn-outline btn-sm"
          onClick={(e) => { e.stopPropagation(); downloadStrategy(s, 'json') }}
          title="Download this strategy as JSON"
        >
          Export JSON
        </button>
        <button
          className="btn btn-outline btn-sm"
          onClick={(e) => { e.stopPropagation(); downloadStrategy(s, 'csv') }}
          title="Download this strategy as CSV"
        >
          Export CSV
        </button>
      </div>
    </>
  )
}

// ── Library Cards (mobile) ──────────────────────────────────────
//
// Same data as StrategyRow, collapsed into a stacked label:value card.
// Visibility is toggled with a plain CSS media query (.lib-table-wrap /
// .lib-cards in App.css) rather than a UnoCSS `hidden md:block` utility —
// the prior attempt at a card layout used UnoCSS's `hidden` utility, which
// this build doesn't generate, so both views rendered simultaneously on
// desktop and every strategy showed twice. A plain media query has no such
// build-tool dependency.
function StrategyCard({ s, isHighlighted, onOpenRigorExplainer, onOpenPassport, deploy, level, gatePending, extraActions }) {
  const [open, setOpen] = useState(isHighlighted)
  const cardRef = useRef(null)
  const years = periodInYears(s.backtest_start, s.backtest_end)
  const startStr = (s.backtest_start || '').slice(0, 10)
  const endStr = (s.backtest_end || '').slice(0, 10)
  const genCost = compactCostCell(s.generation_cost)

  useEffect(() => {
    if (isHighlighted && cardRef.current) {
      cardRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }, [isHighlighted])

  return (
    <div
      ref={cardRef}
      className="lib-card"
      style={isHighlighted ? { background: 'rgba(255,209,102,0.10)', outline: '1px solid var(--accent)' } : undefined}
      onClick={() => setOpen(o => !o)}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') {
          if (e.key === ' ') e.preventDefault()
          setOpen(o => !o)
        }
      }}
    >
      <div className="lib-card-header">
        <span className={`${open ? 'i-lucide-chevron-down' : 'i-lucide-chevron-right'} w-3.5 h-3.5 text-[var(--text-4)] flex-shrink-0`} />
        <div className="lib-card-title">
          {/* Same split as StrategyRow: the card's headline is the strategy's
              own name when it has one; paper_title is the CITED PAPER and
              belongs under the "Source paper" heading in the detail panel. */}
          {s.strategy_name || s.paper_title}
          <PapersCitedChip
            papers={s.papers}
            distinctMechanismPapers={s.distinct_mechanism_papers}
            style={{ fontSize: '0.66rem', marginLeft: 6 }}
          />
        </div>
      </div>
      <div className="lib-card-badges">
        {/* The mobile card renders the SAME helpers as the desktop row above,
            with the same four-state input. It has no rigor icon of its own, so
            the pill is the only verdict signal here — which is exactly why it
            must not be able to say "Live" for a row nothing graded. */}
        <span
          className={`tag ${statusTag(s.status, s.passes_rigor_gate, s.rigor_gate_status)}`}
          title={statusTitle(s.status, s.passes_rigor_gate, s.rigor_gate_status)}
        >
          {statusLabel(s.status, s.passes_rigor_gate, s.rigor_gate_status)}
        </span>
        <DeployabilityChip deploy={deploy} level={level} gatePending={gatePending} />
      </div>
      <div className="lib-card-stats">
        <div><div className="caption">Sharpe</div><div className="mono"><MetricValue metric="sharpe_ratio" value={s.sharpe_ratio} row={s} surface="Library card" /></div><MetricsSourceTag source={s.display_metrics_source} /></div>
        <div><div className="caption">CAGR</div><div className={`mono ${signClass(s.cagr)}`}><MetricValue metric="cagr" value={s.cagr} row={s} surface="Library card" /></div></div>
        <div><div className="caption">Max DD</div><div className="mono negative"><MetricValue metric="max_drawdown" value={s.max_drawdown} row={s} surface="Library card" /></div></div>
        <div title={genCost.title}><div className="caption">Gen tokens</div><div className={genCost.measured ? 'mono' : 'caption'}>{genCost.label}</div></div>
      </div>
      {/* The rejected card's missing half. Four "—" tiles and a token count
          told the reader nothing about the verdict; this names the checks from
          this strategy's own record, above the fold, without expanding. */}
      <RejectionReasons s={s} />
      {open && (
        <div className="lib-card-detail" onClick={(e) => e.stopPropagation()}>
          <StrategyDetailContent
            s={s}
            onOpenRigorExplainer={onOpenRigorExplainer}
            onOpenPassport={onOpenPassport}
            extraActions={extraActions}
            years={years}
            startStr={startStr}
            endStr={endStr}
            hideRejectionReasons
          />
        </div>
      )}
    </div>
  )
}

// #1645: what the Library shows while the strategy lists are in flight. The
// page previously rendered a single "Loading…" caption — 12 characters where a
// full table was about to be, for as long as the slowest of four calls took.
// This holds the shape of the content instead, so the wait reads as "arriving"
// rather than "empty".
//
// `aria-hidden` on the bars plus one sibling live region, rather than labelled
// placeholder rows: a screen-reader user should hear "Loading strategies" once,
// not six rows of meaningless boxes. The animation is a pure-CSS opacity pulse
// (App.css, `lib-skeleton-pulse` — flat, no gradient, which App.css forbids
// file-wide), and the existing `prefers-reduced-motion` block there already
// disables it for everything inside `.app-site`.
function StrategyListSkeleton({ rows = 6 }) {
  return (
    <div className="lib-skeleton mb-4">
      <span className="sr-only" role="status" aria-live="polite">
        Loading strategies…
      </span>
      <div className="lib-skeleton-rows" aria-hidden="true">
        {Array.from({ length: rows }, (_, i) => (
          <div className="lib-skeleton-row" key={i}>
            <div className="lib-skeleton-bar lib-skeleton-name" />
            <div className="lib-skeleton-bar lib-skeleton-meta" />
            <div className="lib-skeleton-bar lib-skeleton-num" />
            <div className="lib-skeleton-bar lib-skeleton-num" />
          </div>
        ))}
      </div>
    </div>
  )
}

function StrategyTable({ strategies, emptyState, highlightStrategyId, onOpenRigorExplainer, onOpenPassport, deployMap, level, gatePending, extraActions }) {
  if (!strategies.length) return emptyState
  return (
    <>
      {/* Table — visible ≥769px, horizontal-scrolls if it still doesn't fit.
          Card list below is the mobile-native replacement (≤768px). Visibility
          is toggled by a plain CSS media query (App.css .lib-table-wrap /
          .lib-cards) — NOT a UnoCSS `hidden` utility, which this build
          doesn't generate (both views rendered on desktop in the prior
          attempt and every strategy showed twice). */}
      <div className="lib-table-wrap overflow-x-auto rounded-lg border border-[var(--glass-border)]">
        <table className="lib-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ background: 'var(--glass)', textAlign: 'left', borderBottom: '1px solid var(--glass-border)' }}>
              <th style={{ padding: '10px 14px' }}>Strategy</th>
              <th style={{ padding: '10px 14px' }}>Paper</th>
              <th style={{ padding: '10px 14px' }}>Status</th>
              <th style={{ padding: '10px 14px', textAlign: 'right' }}>Sharpe</th>
              <th style={{ padding: '10px 14px', textAlign: 'right' }}>CAGR</th>
              <th style={{ padding: '10px 14px', textAlign: 'right' }}>Max DD</th>
              <th style={{ padding: '10px 14px', textAlign: 'right' }}>$1k →</th>
              <th style={{ padding: '10px 14px', textAlign: 'right' }}>Period</th>
              {/* Generation cost (#1326) — total tokens is the design call: it's
                  the term that scales with the model and the one #1217 exists to
                  pin down, while wall time is dominated by backtests and moves
                  with whatever else the worker is doing. Wall time + dominant
                  stage ride in the cell's tooltip. */}
              <th
                style={{ padding: '10px 14px', textAlign: 'right' }}
                title="Tokens consumed by the generation run that produced this strategy. A raw measurement — never converted to dollars."
              >Gen tokens</th>
            </tr>
          </thead>
          <tbody>
            {strategies.map(s => (
              <StrategyRow
                key={s.id}
                s={s}
                isHighlighted={highlightStrategyId && s.id === highlightStrategyId}
                onOpenRigorExplainer={onOpenRigorExplainer}
                onOpenPassport={onOpenPassport}
                deploy={deployMap?.[s.id]}
                level={level}
                gatePending={gatePending}
                extraActions={extraActions}
              />
            ))}
          </tbody>
        </table>
      </div>

      <div className="lib-cards">
        {strategies.map(s => (
          <StrategyCard
            key={s.id}
            s={s}
            isHighlighted={highlightStrategyId && s.id === highlightStrategyId}
            onOpenRigorExplainer={onOpenRigorExplainer}
            onOpenPassport={onOpenPassport}
            deploy={deployMap?.[s.id]}
            level={level}
            gatePending={gatePending}
            extraActions={extraActions}
          />
        ))}
      </div>
    </>
  )
}

// ── Main export ───────────────────────────────────────────────

// Map a strategy_store row (fusion/architect output) into the same shape
// StrategyRow expects. Most metric fields are null on a pre-backtest
// hypothesis — the row will render those columns as "—", which is the honest
// signal that fusion-to-backtest hasn't run yet.
function coerceGenerated(row) {
  const sourcePapers = Array.isArray(row.source_papers) ? row.source_papers : []
  const citedPaper = sourcePapers[0] || null
  const firstPaper = citedPaper?.arxiv_id || ''
  // Citation truth: this column is the CITED PAPER's title. It used to be
  // bound to row.strategy_name — the LLM's name for the strategy rendered
  // where a real citation belongs, which is a fabricated citation, not a
  // missing one. The backend now resolves real titles from the papers corpus
  // (list_generated_strategies -> _resolve_source_papers). When resolution
  // genuinely fails we say so and name the id a reader can look up; the one
  // thing that must never appear here is the strategy's own name.
  const resolvedTitle = (citedPaper?.resolved_title || '').trim()
  const paperTitle = citedPaper
    ? (resolvedTitle || `title unavailable — arXiv:${firstPaper || 'unknown'}`)
    : 'no cited paper'
  // Publication year of the CITED PAPER, resolved from the corpus alongside
  // the title. Never row.created_at — that is when the STRATEGY was generated,
  // and printing it under a paper citation invented a publication date.
  const paperYear = typeof citedPaper?.resolved_year === 'number' ? citedPaper.resolved_year : null
  // NOTHING here reads row.rigor_verdict any more.
  //
  // That blob is the GENERATION-TIME fusion verdict, written once by the debate
  // society and never rewritten after a backtest. Reading `passing` out of it
  // for the badge — and `dsr`/`pbo`/`oos_sharpe`/`dsr_p_value` for the numbers
  // beside it — is what made this tab disagree with every other surface
  // (#1747): the row said "Live ✓" while the strategy's own passport said
  // "Reference only — gate failed", because the two answers came from two
  // different gates. The verdict of record is the one the backend now overlays
  // onto these rows from strategy_passports
  // (docs/adr/rigor-verdict-of-record.md, `_passport_verdicts_for`), and it
  // arrives on real top-level fields. The fusion verdict is still persisted and
  // still worth having — it is the debate record — but it is not a grade.
  // The store status travels UNCHANGED. This used to rewrite "rejected" to an
  // invented `pending_backtest` whenever no metric number was present, to avoid
  // calling a pre-backtest hypothesis "Rejected". That workaround predates the
  // four-state: it asked "is there a NUMBER?" when the real question is "has a
  // GATE run?", and the two answers diverge. A strategy the real gate graded
  // `fail` whose backtest produced no DSR (deflated_sharpe_ratio is Optional)
  // has no number and was relabelled amber "Pending Backtest" — a badge
  // asserting no gate had run, on a row a gate ran and failed, which is exactly
  // the #1747 claim class. A `degenerate` row got the same rewrite and with it
  // a tooltip announcing that no backtest had run, over flat returns that HAD
  // one.
  //
  // The honest answer now comes from the verdict of record instead: `pending`
  // renders "Not yet graded" through statusLabel's ungraded arm, for BOTH
  // "rejected" and "candidate" store statuses, and with the tooltip that
  // explains it (statusTitle). Nothing has to be invented here to get it.
  return {
    id: row.id,
    // The strategy's OWN name, kept as its own field rather than smuggled into
    // paper_title. It is the row's headline; the citation is paper_title.
    strategy_name: row.strategy_name || '(unnamed)',
    paper_title: paperTitle,
    paper_arxiv_id: firstPaper,
    paper_authors: [],
    paper_year: paperYear,
    paper_venue: row.generation_method,
    methodology_summary: row.thesis || '',
    status: row.status || 'candidate',
    asset_universe: row.asset_universe || [],
    sharpe_ratio: null,
    cagr: null,
    max_drawdown: null,
    correlation_to_spy: null,
    // The rigor numbers the SAME grading event produced as the verdict below —
    // served from the passport row, never from row.rigor_verdict. A badge from
    // one gate printed beside DSR/PBO from another is the shape #1187/#1340
    // removed from the curated path.
    deflated_sharpe_ratio: row.deflated_sharpe_ratio ?? null,
    pbo_score: row.pbo_score ?? null,
    out_of_sample_sharpe: row.out_of_sample_sharpe ?? null,
    paper_claimed_sharpe: null,
    backtest_start: null,
    backtest_end: null,
    is_backtest_placeholder: true,
    // A LITERAL boolean or nothing. `typeof === 'boolean'` rather than a
    // truthiness coercion: the API sends null for a strategy no gate has graded
    // (see _UNGRADED_VERDICT_FIELDS), and null must stay null all the way to the
    // pill — Boolean(null) would silently become an assertion that the gate ran
    // and the strategy lost.
    passes_rigor_gate: typeof row.passes_rigor_gate === 'boolean' ? row.passes_rigor_gate : null,
    // The four-state verdict of record, carried through so statusTag/statusLabel
    // and the row's rigor icon read the same field.
    rigor_gate_status: row.rigor_gate_status ?? null,
    dsr_p_value: row.dsr_p_value ?? null,
    // No real backtest has run yet on a pre-backtest hypothesis, so there is
    // no CI to report — honestly null, on the real field names (#1361).
    sharpe_ci_lower: null,
    sharpe_ci_upper: null,
    // Durable generation-cost record (#1326) served by /api/strategies/generated.
    // Absent for anything generated before the meter — passed through as null so
    // the cost column renders "not measured" rather than a fabricated zero.
    generation_cost: row.generation_cost ?? null,
    // Per-check rejection reasons for THIS strategy, derived server-side from
    // its own stored rigor_verdict (backend/archimedes/services/rigor_reasons.py)
    // and served by /api/strategies/generated. Carried through untouched;
    // `null` on any payload without the field, which renders as no block at all.
    rigor_reasons: row.rigor_reasons ?? null,
  }
}

export default function Strategies({ highlightStrategyId, defaultTab, onNavigate }) {
  const [examples, setExamples] = useState([])
  const [generated, setGenerated] = useState([])
  const [published, setPublished] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  // Per-feed failure messages (#1356): genRes/gateRes/publishedRes failing
  // used to be silently swallowed — the generated panel painted "No
  // generated strategies yet" (a different, false claim from "the fetch
  // failed"), every deployability chip vanished with no signal, and
  // Published painted "Nothing published yet". Each gets its own visible,
  // near-the-panel error instead.
  const [genError, setGenError] = useState('')
  const [gateError, setGateError] = useState('')
  const [publishedError, setPublishedError] = useState('')
  // Per-user rigor strictness (shared with the Passport slider via localStorage).
  // Read-only here since #1645: Library renders the chips AT the user's level
  // but no longer offers the setter — the rigor-strictness card moved off this
  // page (#1645). The hook still subscribes to the shared listener set, so
  // changing the level on the Passport re-annotates these rows on the next
  // render.
  const [level] = useRigorStrictness()
  // {strategy_id: {min_passing_level, blocked_by_floor, pending, degenerate}} from the live
  // gate — strictness-independent, so we fetch once and re-annotate rows
  // client-side as the slider moves. Curated strategies resolve here; generated
  // ones fall back to their badge boolean (no chip).
  const [deployMap, setDeployMap] = useState({})
  // #1645: the deployability gate resolves independently of (and much later
  // than) the strategy lists now, so its in-flight state is its own. True
  // until /api/selection-bias/gate settles — success or failure.
  const [gateLoading, setGateLoading] = useState(true)
  // Monotonic id for the in-flight load(); a superseded run's late response is
  // discarded rather than allowed to overwrite a newer one. See load().
  const runIdRef = useRef(0)
  // 'generated' is the first-class tab per product feedback — pushes user
  // toward Generate when empty. Published is a hidden roadmap surface
  // (#1266/#1324) — a ?tab=published deep link must not land there with the
  // flag off, since the tab button that would normally set this is gone too.
  const [activeTab, setActiveTab] = useState(() => {
    if (defaultTab === 'published' && !ROADMAP_SURFACES_ENABLED) return 'generated'
    return defaultTab || 'generated'
  })
  // Page-level rigor explainer modal, opened from any row expansion's "?"
  // affordance. Single modal instance per page keeps state simple.
  const [rigorModalOpen, setRigorModalOpen] = useState(false)
  const openRigorExplainer = useCallback(() => setRigorModalOpen(true), [])
  const closeRigorExplainer = useCallback(() => setRigorModalOpen(false), [])
  const rigorModalRef = useDialogFocus(rigorModalOpen, { onEscape: closeRigorExplainer })

  // Deep-link to the strategy passport route — added in Phase 4.
  const openPassport = useCallback(
    (strategyId) => { if (onNavigate) onNavigate('strategy', { strategyId }) },
    [onNavigate]
  )

  // If we arrived via ?highlight=<id> and the strategy is only in Examples,
  // auto-switch to the Examples tab so the scrollIntoView lands a real row.
  useEffect(() => {
    if (!highlightStrategyId) return
    const inGenerated = generated.some(s => s.id === highlightStrategyId)
    const inExamples = examples.some(s => s.id === highlightStrategyId)
    if (!inGenerated && inExamples) setActiveTab('examples')
  }, [highlightStrategyId, generated, examples])

  // #1645: PROGRESSIVE LOAD. This used to `await Promise.allSettled([...])` and
  // paint nothing until the SLOWEST of the four calls returned — and the slowest
  // is structurally `/api/selection-bias/gate`, which recomputes the whole
  // cohort rigor gate (its own module docstring measures ~8-10s, and it was
  // returning ALB 504s against prod on 2026-08-31). So a page whose strategy
  // list had been sitting in the browser for seconds still showed a bare
  // "Loading…" line, which reads as broken rather than as slow.
  //
  // Now each response is applied the moment it settles, and only the two calls
  // that produce ROWS hold the skeleton up. The gate is an annotation on rows
  // that are already readable, so it lands late and fills the chips in; while
  // it is in flight `gatePending` makes every chip say "checking…" rather than
  // vanish (see DeployabilityChip).
  //
  // `runIdRef` discards a superseded run's results: `load` is also the Retry
  // button's handler, so two runs can now genuinely overlap and an older, slower
  // response must not overwrite a newer one.
  const load = useCallback(async () => {
    const runId = ++runIdRef.current
    const current = () => runIdRef.current === runId
    setLoading(true)
    setGateLoading(true)
    setLoadError('')
    setGenError('')
    setGateError('')
    setPublishedError('')
    // Published is a hidden roadmap surface (#1266/#1324) — its fetch must
    // not fire with the flag off, not just its tab stay unclickable.
    //
    // limit=100 (the backend's max): the endpoint defaults to 20 of the
    // 34-strategy curated library, alphabetically — which structurally hid
    // every currently-passing strategy (all sort past row 20). Found in
    // the 2026-08-30 external product review ("0 of 20 examples pass").
    const seedPromise = apiGet('/api/strategies/?limit=100')
    const genPromise = apiGet('/api/strategies/generated')
    // Deployability is a VAULT question and vaults are out of the MVP cut
    // (#1266): DeployabilityChip returns null with the flag off, so an
    // unconditional call here would spend the slowest request on the page
    // (this route recomputes the whole cohort gate — ~8-10s, and 504-ing
    // against prod on 2026-08-31) on an annotation nothing renders. Same
    // ternary shape, and the same reason, as publishedPromise below: gating
    // the render while still hitting the hidden API every load is the exact
    // defect #1324 was filed against. The empty payload keeps every consumer
    // downstream — deployMap, gateLoading, the gateError banner — unchanged.
    const gatePromise = ROADMAP_SURFACES_ENABLED ? apiGet('/api/selection-bias/gate') : Promise.resolve({ strategies: [] })
    // Kept on one line deliberately: ui/test/routes.test.js's #1324 guard
    // anchors on this exact expression, and reformatting it would silently
    // disarm someone else's regression test rather than break it loudly.
    const publishedPromise = ROADMAP_SURFACES_ENABLED ? apiGet('/api/marketplace/my-published') : Promise.resolve([])

    const settle = (promise) =>
      promise.then(
        (value) => ({ status: 'fulfilled', value }),
        (reason) => ({ status: 'rejected', reason }),
      )

    const applySeed = settle(seedPromise).then((seedRes) => {
      if (!current()) return
      if (seedRes.status === 'fulfilled') {
        const sorted = [...(seedRes.value.strategies || [])].sort(
          (a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status)
        )
        setExamples(sorted)
        // A 2xx response can still mean the fetch failed: the backend swallows a
        // provider exception into `degraded: true` with an empty list rather than
        // a 500 (#1356's own fix, applied to this same route) so a fulfilled
        // promise is not proof of a real empty library — check the flag before
        // trusting the empty state.
        if (seedRes.value.degraded) {
          setLoadError(seedRes.value.degraded_reason || 'Failed to load examples')
        }
      } else {
        setLoadError(seedRes.reason?.message || 'Failed to load examples')
      }
    })

    const applyGen = settle(genPromise).then((genRes) => {
      if (!current()) return
      if (genRes.status === 'fulfilled') {
        setGenerated((genRes.value.strategies || []).map(coerceGenerated))
        // Same fulfilled-but-degraded shape as seedRes above (#1356 review
        // round 2): the backend swallows a store exception into a 200 with
        // `degraded: true` rather than a 500, so a fulfilled promise alone
        // is not proof the fetch actually succeeded.
        if (genRes.value.degraded) {
          setGenError(genRes.value.degraded_reason || 'Failed to load generated strategies')
        }
      } else {
        setGenError(genRes.reason?.message || 'Failed to load generated strategies')
      }
    })

    const applyGate = settle(gatePromise).then((gateRes) => {
      if (!current()) return
      if (gateRes.status === 'fulfilled') {
        const map = {}
        for (const r of gateRes.value.strategies || []) {
          map[r.strategy_id] = {
            min_passing_level: r.min_passing_level,
            blocked_by_floor: r.blocked_by_floor,
            // #1358: the gate's own honest signal that this row had zero
            // statistics computed (< 10 persisted returns) — distinct from a
            // real "fails every strictness level" verdict, which also has
            // min_passing_level === null. Without this, DeployabilityChip
            // could not tell the two apart and rendered "not deployable" for
            // both.
            pending: r.pending,
            // #1358 round-3: the gate's honest signal that this row's persisted
            // series is zero-variance. Distinct from BOTH neighbours above:
            // there is data (so not pending), and no floor ever measured it (so
            // the blocked_by_floor === true this row also arrives with is not a
            // claim the chip may repeat).
            degenerate: r.degenerate,
          }
        }
        setDeployMap(map)
      } else {
        setGateError(gateRes.reason?.message || 'Failed to load deployability status')
      }
    })

    const applyPublished = settle(publishedPromise).then((publishedRes) => {
      if (!current()) return
      if (publishedRes.status === 'fulfilled') {
        setPublished(Array.isArray(publishedRes.value) ? publishedRes.value : [])
      } else {
        setPublishedError(publishedRes.reason?.message || 'Failed to load published strategies')
      }
    })

    // The skeleton comes down when there are ROWS to show. The gate is
    // deliberately NOT in this list — waiting on it is the whole defect.
    //
    // Still inside try/finally: the pre-#1645 shape wrapped the whole load in
    // one, and dropping it would mean a throw anywhere in the three handlers
    // above (e.g. a 200 whose `strategies` field is not iterable) leaves
    // `loading` true and the skeleton up forever. Same guarantee, narrower
    // scope.
    try {
      await Promise.all([applySeed, applyGen, applyPublished])
    } finally {
      if (current()) setLoading(false)
    }
    // Not awaited: the caller (and the Retry button) must not block on the
    // slowest call. `.catch` matters twice here — a detached promise's throw
    // would otherwise be an unhandled rejection, AND a malformed gate payload
    // would strand every chip on "checking…" instead of showing the banner.
    applyGate
      .catch((err) => {
        if (current()) setGateError(err?.message || 'Failed to load deployability status')
      })
      .finally(() => {
        if (current()) setGateLoading(false)
      })
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <div className="strategies-page">
      {/* #1370 item 7: the heading must name the same place the nav item, the
          tab title and the breadcrumb name — all three say "Library". */}
      <header className="app-page-heading">
        <p className="app-eyebrow">Evidence library</p>
        <h1>Library</h1>
        <p>
          Your strategies, plus a clearly-separated set of example strategies
          drawn from published research so you can learn the metric format.
        </p>
      </header>

      {/* The rigor-strictness card used to sit HERE, above every strategy
          (#1645). It was a full `card p-5` — heading, explanatory paragraph,
          1-5 slider, five labelled buttons, a three-metric threshold table and
          a conditional warning box — between the user and the thing they came
          to browse. It is not deleted from the product: the same control is
          mounted on the Strategy Passport
          (`StrategyPassport.jsx`, "Your strictness"), which is where the level
          is actually applied to a decision, and `useRigorStrictness` keeps the
          two in sync through localStorage. `level` is still read here — the
          DeployabilityChip below annotates every row against it. */}

      {/* Real <button>s, not click-only <span>s: activeTab defaults to
          'generated', so a keyboard-only user was permanently pinned to that
          one view and could never reach Examples or Published, and nothing in
          the accessibility tree said which view was active (2.1.1 / 4.1.2).
          Same shape Leaderboard.jsx:239 already uses. */}
      <div className="strat-filter-bar mb-4" role="group" aria-label="Strategy view">
        <button
          type="button"
          className={`tag ${activeTab === 'generated' ? 'tag-accent' : 'tag-muted'}`}
          aria-pressed={activeTab === 'generated'}
          onClick={() => setActiveTab('generated')}
        >
          Generated ({genError ? '—' : generated.length})
        </button>
        <button
          type="button"
          className={`tag ${activeTab === 'examples' ? 'tag-accent' : 'tag-muted'}`}
          aria-pressed={activeTab === 'examples'}
          onClick={() => setActiveTab('examples')}
        >
          Examples ({loadError ? '—' : examples.length})
        </button>
        {/* Published leads into the marketplace surface #1266 hid — hides
            with it (#1324). Anti-goal: gating render alone without gating
            the fetch above would still hit the hidden API every load. */}
        {ROADMAP_SURFACES_ENABLED && (
          <button
            type="button"
            className={`tag ${activeTab === 'published' ? 'tag-accent' : 'tag-muted'}`}
            aria-pressed={activeTab === 'published'}
            onClick={() => setActiveTab('published')}
          >
            Published ({published.length})
          </button>
        )}
      </div>

      {loadError && (
        <div className="info-box warning mb-4">
          Couldn't load library: {loadError}{' '}
          <button type="button" className="btn btn-sm btn-outline" onClick={load} style={{ marginLeft: 4 }}>
            Retry
          </button>
        </div>
      )}

      {activeTab === 'generated' && (
        <>
          {/* The gate feed is independent of the generated-strategies feed: a
              gate failure alone used to leave deployMap at {} with no
              signal — DeployabilityChip short-circuits to `null` for every
              row (:14), so every chip silently vanished (#1356). This banner
              is the visible signal that replaces that silence, near the
              chips it describes.

              Scope note (this PR): with ROADMAP_SURFACES_ENABLED off, load()
              no longer fetches /api/selection-bias/gate at all, so gateError
              stays null and this banner cannot fire — there are no chips left
              for it to describe. Banner and chips come back together under
              VITE_ROADMAP_SURFACES=true. */}
          {gateError && (
            <div className="info-box warning mb-3">
              Deployability status unavailable: {gateError}. Chips below may not reflect the live gate.{' '}
              <button type="button" className="btn btn-sm btn-outline" onClick={load} style={{ marginLeft: 4 }}>
                Retry
              </button>
            </div>
          )}
          {loading ? (
            <StrategyListSkeleton />
          ) : genError ? (
            <div className="info-box warning mb-4">
              Couldn't load generated strategies: {genError}{' '}
              <button type="button" className="btn btn-sm btn-outline" onClick={load} style={{ marginLeft: 4 }}>
                Retry
              </button>
            </div>
          ) : (() => {
            // Split generated strategies by rigor verdict. The main table shows
            // [...passing, ...pending] — cleared strategies plus candidates still
            // awaiting a verdict, NOT "only what passed" (a stricter claim this
            // file used to make; #1370 item 7 corrected copy and comment to match
            // what's actually shown). Rejected candidates stay accessible in a collapsed
            // section below so the user can inspect *why* they failed — honest
            // rather than hidden, but visually de-prioritised.
            const passing = generated.filter(s => s.passes_rigor_gate === true)
            const rejected = generated.filter(s => s.passes_rigor_gate === false)
            const pending = generated.filter(s => s.passes_rigor_gate == null)
            const mainTableStrategies = [...passing, ...pending]
            return (
              <>
                <StrategyTable
              strategies={mainTableStrategies}
              highlightStrategyId={highlightStrategyId}
              onOpenRigorExplainer={openRigorExplainer}
              onOpenPassport={openPassport}
              deployMap={deployMap}
              level={level}
              emptyState={
                rejected.length > 0 ? (
                  <div className="card" style={{ padding: 22 }}>
                    <div className="label mb-2">No strategies have passed the rigor gate yet</div>
                    {/* Same correction as the rejected section's own copy: the
                        parenthetical here named a single reason for a
                        population it never counted. The sentence below is
                        computed from these exact rows and is omitted entirely
                        when none of them carry that reason. */}
                    <p className="body" style={{ marginBottom: 10 }}>
                      You've generated {rejected.length} {rejected.length === 1 ? 'candidate' : 'candidates'}, but
                      none have cleared the rigor gate yet. Expand the <strong>Rejected</strong> section below
                      to see each candidate's own rigor verdict — the checks it failed, and the ones it passed.
                    </p>
                    {rejectedSectionSummary(rejected) && (
                      <p className="body" style={{ marginBottom: 10 }}>
                        {rejectedSectionSummary(rejected)}
                      </p>
                    )}
                    <p className="caption" style={{ color: 'var(--text-3)' }}>
                      This table holds strategies that passed the rigor gate (DSR + PBO +
                      chronological OOS + look-ahead audit) plus candidates still awaiting a
                      verdict — rejected strategies are the only ones filtered out, into the
                      section below.
                    </p>
                  </div>
                ) : (
                  <div className="card" style={{ padding: 22 }}>
                    <div className="label mb-2">No generated strategies yet</div>
                    <p className="body" style={{ marginBottom: 10 }}>
                      Multi-paper fusion strategies you create from the{' '}
                      <a href="/app/generate" style={{ color: 'var(--accent)' }}>Generate</a> page will
                      appear here once they've been backtested; the rigor verdict (pass/fail) lands
                      here after, once DSR / PBO / OOS scoring completes.
                    </p>
                    <p className="caption" style={{ color: 'var(--text-3)' }}>
                      {ROADMAP_SURFACES_ENABLED
                        ? ROADMAP_COPY.emptyLibraryNoteRoadmap
                        : 'Generations in flight show in the agent activity feed on Reasoning. They land in this table once backtesting finishes, verdict or not.'}
                    </p>
                  </div>
                )
              }
            />
            {rejected.length > 0 && (
              <details className="mt-5">
                <summary
                  className="caption cursor-pointer select-none"
                  style={{
                    color: 'var(--text-3)',
                    padding: '10px 14px',
                    background: 'var(--surface-2)',
                    border: '1px solid var(--glass-border)',
                    borderRadius: 6,
                    listStyle: 'none',
                  }}
                >
                  Rejected ({rejected.length}) — did not pass the rigor gate. Click to inspect.
                </summary>
                <div style={{ marginTop: 12 }}>
                  {/* The paragraph that used to sit here named one reason (a
                      too-short return series) for the whole population and
                      promised a longer backtest window would unlock them.
                      Nothing had counted that, and it was false for any
                      candidate rejected on the numbers instead. Each row now
                      names its own checks (RejectionReasons), and the only
                      sentence about the group is the one below, computed from
                      the rows on screen — null when there is nothing true to
                      say. ui/test/rejected-reason.test.js keeps the old prose
                      out. */}
                  <p className="caption mb-3" style={{ color: 'var(--text-3)', fontSize: '0.82rem' }}>
                    These candidates were generated and did not pass the rigor gate (DSR, PBO,
                    chronological OOS, or look-ahead audit). Each row names the checks it failed,
                    read from that strategy's own recorded verdict.
                  </p>
                  {rejectedSectionSummary(rejected) && (
                    <p className="caption mb-3" style={{ color: 'var(--text-3)', fontSize: '0.82rem' }}>
                      {rejectedSectionSummary(rejected)}
                    </p>
                  )}
                  <StrategyTable
                    strategies={rejected}
                    highlightStrategyId={highlightStrategyId}
                    onOpenRigorExplainer={openRigorExplainer}
                    onOpenPassport={openPassport}
                    deployMap={deployMap}
                    level={level}
                    emptyState={<p className="caption">No rejected strategies.</p>}
                  />
                </div>
              </details>
            )}
              </>
            )
          })()}
        </>
      )}

      {activeTab === 'examples' && (
        <>
          {/* This tab is REFERENCE MATERIAL, and the intro has to say so before
              the reader starts reading the rows as scores. The previous copy
              ("see what a rigor-gate verdict looks like") invited exactly the
              opposite reading, and the rows obliged: 34 hand-curated
              implementations of published papers, each one carrying a red
              failure pill next to numbers nobody re-ran here.

              Three claims, each of which has to stay true:
                1. what they are — reference implementations, to learn the card
                   format; not fusion-engine output;
                2. what a missing verdict means — a curated example is graded
                   only once a backtest has actually been run for it (the live
                   gate needs persisted returns), so "no verdict" is a state,
                   not a failure, and the passport is where the verdict lives;
                3. where the numbers come from — the mark under each row's
                   Sharpe, driven by the API's own `display_metrics_source`
                   (see ../metricsSource.js), so this paragraph never has to
                   generalise about rows it cannot see. */}
          <div className="caption mb-3 text-[var(--text-3)] leading-relaxed">
            <p style={{ marginBottom: 6 }}>
              <strong>Example strategies</strong> — hand-curated reference implementations
              of single published papers. They are here to be read: open one to learn the
              card format — what each field means, which paper it came from, and where a
              verdict appears once there is one. They are <em>not</em> outputs of the
              fusion engine.
            </p>
            {/* MERGE ORDER — depends on PR #1792 (dbrowneup/verdict-of-record-a).
                The claim below ("until then it carries no verdict, and an absent
                verdict is not a failure") is only true once #1792 lands.
                statusLabel() (defined at :161 on this branch) still returns
                'Reference only — gate failed' for
                status === 'live' && passes_rigor_gate === false, and
                passes_rigor_gate is the FAIL-CLOSED boolean: false for a pending
                verdict too. #1792 replaces it with a four-state rigor_gate_status
                and NOT_GRADED_LABEL = "Not yet graded" (ui/src/libraryStatus.js),
                which is what stops an ungraded curated row from rendering a
                failure pill. Merged alone, this paragraph contradicts the pill two
                inches below it, so land this after or with #1792.

                This is an ORDERING constraint, not a conflict one:
                `git merge-tree --write-tree --name-only` of this branch against
                origin/dbrowneup/verdict-of-record-a is EXIT=0 with no conflicted
                paths. Do not "fix" it by softening the copy — the copy is the
                correct end state; #1792 is what makes the rows agree with it. */}
            <p style={{ marginBottom: 6 }}>
              They are <strong>not a scoreboard</strong>. A curated example is graded only
              once a backtest has been run for it here; until then it carries no verdict,
              and an absent verdict is not a failure. The numbers beside them are not all
              measurements either: where a row's metrics trace to a stored fixture snapshot
              rather than to a backtest run here, the row is marked <strong>fixture</strong> —
              or <strong>placeholder</strong>, where only the strategy module's declared
              constants exist. A strategy's passport is its verdict of record.
            </p>
            <p>
              Generate's curated-library path picks and weights its candidates from this
              same set.
            </p>
          </div>
          {loading && <StrategyListSkeleton />}
          {/* Gated on !loadError, matching the Published branch below (#1356
              review round 2): loadError is set from the seed route's own
              `degraded` flag on a *fulfilled* response (see load() above), so
              without this gate a degraded fetch painted the loadError banner
              at :815 AND the false "No example strategies loaded." empty
              state simultaneously — the exact claim #1356 was filed against. */}
          {!loading && !loadError && (
            <StrategyTable
              strategies={examples}
              highlightStrategyId={highlightStrategyId}
              onOpenRigorExplainer={openRigorExplainer}
              onOpenPassport={openPassport}
              deployMap={deployMap}
              level={level}
              // Only the EXAMPLES table claims "checking…". /api/selection-bias/gate
              // iterates the curated provider library
              // (selection_bias_routes.evaluate_rigor_gate -> _provider().list_strategies()),
              // so a generated row has no deployMap entry before OR after that call
              // lands — telling the reader we are "still loading the gate for this
              // row" would be false for those rows, and the chip would flicker in
              // and back out on the default tab. Generated rows are covered by the
              // per-strategy /gate/{id} route on the Passport, not by this one.
              gatePending={gateLoading && !gateError}
              emptyState={<p className="caption">No example strategies loaded.</p>}
            />
          )}
          {examples.some(s => s.is_backtest_placeholder) && (
            <div className="caption mt-4 text-[var(--text-4)]">
              Pre-backtest hypothesis — empirical metrics pending evaluation. Real
              numbers replace the placeholder once the analytics engine runs.
            </div>
          )}
        </>
      )}

      {activeTab === 'published' && ROADMAP_SURFACES_ENABLED && (
        <>
          <div className="caption mb-3 text-[var(--text-3)] leading-relaxed">
            Strategies you have published to the on-chain marketplace. Subscribers
            you approve can mirror trades from your vault.
          </div>
          {loading && <StrategyListSkeleton />}
          {!loading && publishedError && (
            <div className="info-box warning mb-4">
              Couldn't load published strategies: {publishedError}{' '}
              <button type="button" className="btn btn-sm btn-outline" onClick={load} style={{ marginLeft: 4 }}>
                Retry
              </button>
            </div>
          )}
          {!loading && !publishedError && (
            <StrategyTable
              strategies={published}
              highlightStrategyId={highlightStrategyId}
              onOpenPassport={openPassport}
              extraActions={(row) =>
                row.status === 'running' ? (
                  <>
                    <button
                      type="button"
                      className="btn btn-sm btn-outline"
                      onClick={async () => {
                        const res = await apiPost(`/api/marketplace/publish/${row.strategy_id}/withdraw`, {})
                        if (res.status === 'withdrawn') {
                          alert(`Withdrew ${res.amount_raw / 1e6} USDC`)
                        } else if (res.status === 'nothing_to_withdraw') {
                          alert('Nothing to withdraw yet')
                        }
                        load()
                      }}
                      style={{ marginLeft: 8 }}
                    >
                      Withdraw
                    </button>
                    <button
                      type="button"
                      className="btn btn-sm btn-outline-danger"
                      onClick={async () => {
                        if (window.confirm(`Stop publishing "` + (row.strategy_name || row.strategy_id) + `"?`)) {
                          await apiDelete(`/api/marketplace/publish/${row.strategy_id}`)
                          load()
                        }
                      }}
                      style={{ marginLeft: 8 }}
                    >
                      Stop
                    </button>
                  </>
                ) : null
              }
              emptyState={
                <div className="card" style={{ padding: 22 }}>
                  <div className="label mb-2">Nothing published yet</div>
                  <p className="body" style={{ marginBottom: 10 }}>
                    Strategies you publish from the strategy passport page will
                    appear here. Publishing lets subscribers mirror your trades
                    on-chain.
                  </p>
                </div>
              }
            />
          )}
        </>
      )}

      {/* EfficientFrontier + CorrelationMatrix removed (Issue #383) — synthetic RNG data */}

      {/* Rigor Explainer modal (portal-rendered, page-level).
          It had no dialog role, no accessible name, no Escape handler and no
          focus management — and because the portal appends after #root its
          Close button was the LAST focus stop in the document, so reaching it
          meant tabbing the whole Library page underneath a blurred overlay
          (2.4.3 / 4.1.2). */}
      {rigorModalOpen && createPortal(
        <div
          className="modal-overlay"
          onClick={() => setRigorModalOpen(false)}
          style={{ zIndex: 1000 }}
        >
          <div
            ref={rigorModalRef}
            tabIndex={-1}
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="rigor-explainer-title"
            onClick={e => e.stopPropagation()}
            style={{ maxWidth: 820, maxHeight: '85vh', overflowY: 'auto', width: '90vw' }}
          >
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 8 }}>
              <button
                type="button"
                onClick={closeRigorExplainer}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-3)' }}
                aria-label="Close"
              >
                <span className="i-lucide-x" style={{ width: 20, height: 20 }} />
              </button>
            </div>
            <RigorExplainer />
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
