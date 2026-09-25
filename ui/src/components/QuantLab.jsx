// QuantLab — container for the /quant page (#1060). Owns all live-data
// fetching and passes real series into the three panel components through the
// props they already accept, so each section's sample-data badge drops off on
// its own once its live source responds:
//
//   /api/strategies/                    → library list (subject selector)
//   /api/strategies/{id}/returns        → persisted real daily returns
//     - drawdown, rolling Sharpe, VaR fallback (RiskAnalysis)
//     - walk-forward folds, computed client-side (BacktestVisualizer)
//     - equity curve (BacktestVisualizer fetches by strategyId itself)
//   returns across N strategies         → correlation matrix + frontier scatter
//   /api/vaults/ + /api/vaults/{addr}   → allocation drift (current vs target)
//   /api/traces/                        → trade/rebalance log rows
//
// Sections with no live source yet keep their synthetic render + badge:
// nothing mock is presented as real (issue #1060 anti-goal).

import { useEffect, useMemo, useState } from 'react'
import { apiGet } from '../api'
import RiskAnalysis from './RiskAnalysis'
import PortfolioAdvisorPanels from './PortfolioAdvisorPanels'
import BacktestVisualizer from './BacktestVisualizer'

// How many library strategies we pull returns for on page load (correlation +
// frontier). Each series is tens of KB of floats and some rows are degenerate
// all-zero runs, so scan a dozen; anything else loads on demand when selected.
const MAX_RETURN_SERIES = 12

// Default sweep subject: the parameter sweep backtests {ticker: weight} over
// real market data, so a plain SPY book exercises rebalance_days × tx_cost_bps
// honestly without inventing an allocation the user never chose.
const SWEEP_WEIGHTS = { SPY: 1.0 }

function annualizedRiskReturn(returns) {
  const n = returns.length
  if (n < 2) return null
  const mean = returns.reduce((s, v) => s + v, 0) / n
  const variance = returns.reduce((s, v) => s + (v - mean) ** 2, 0) / (n - 1)
  const risk = Math.sqrt(variance) * Math.sqrt(252)
  if (!Number.isFinite(risk) || risk < 1e-9) return null
  return { risk, ret: mean * 252 }
}

function shortLabel(s) {
  const t = s.paper_title || s.name || s.id
  return t.length > 14 ? `${t.slice(0, 13)}…` : t
}

export default function QuantLab() {
  const [strategies, setStrategies] = useState([])
  const [returnsById, setReturnsById] = useState({}) // id → float[]
  const [missingIds, setMissingIds] = useState(() => new Set()) // ids with no usable returns
  const [selectedId, setSelectedId] = useState('')
  const [driftRows, setDriftRows] = useState(null)
  const [driftVaultName, setDriftVaultName] = useState('')
  const [trades, setTrades] = useState(null)
  const [loading, setLoading] = useState(true)
  // Distinguishes "the library really is empty" from "the library fetch
  // failed" (#1369 review finding) — both leave `strategies` at `[]`, but
  // conflating them makes a backend outage read as an honest empty library.
  const [libraryError, setLibraryError] = useState(false)

  // ── Library list + per-strategy persisted returns ─────────────────────────
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const list = await apiGet('/api/strategies/?limit=50')
        const strats = list?.strategies ?? []
        if (cancelled) return
        setStrategies(strats)

        const candidates = strats.slice(0, MAX_RETURN_SERIES)
        const settled = await Promise.allSettled(
          candidates.map((s) => apiGet(`/api/strategies/${encodeURIComponent(s.id)}/returns`)),
        )
        if (cancelled) return
        const byId = {}
        const misses = new Set()
        settled.forEach((r, i) => {
          // 404 = no persisted backtest for this strategy; skip silently.
          // Some rows persist an all-zero series (the rigor cohort excludes
          // these as degenerate too) — no variance means nothing to diagnose,
          // so treat them the same as missing data.
          const dr = r.status === 'fulfilled' ? r.value?.daily_returns : null
          if (Array.isArray(dr) && dr.length >= 60 && dr.some((v) => v !== 0)) {
            byId[candidates[i].id] = dr
          } else {
            misses.add(candidates[i].id)
          }
        })
        setMissingIds(misses)
        setReturnsById(byId)
        const firstWithData = candidates.find((s) => byId[s.id])
        setSelectedId((cur) => cur || firstWithData?.id || strats[0]?.id || '')
      } catch (_) {
        // Backend down: panels keep their synthetic render + badges.
        if (!cancelled) setLibraryError(true)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  // ── Live vault → allocation drift ──────────────────────────────────────────
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const list = await apiGet('/api/vaults/')
        const vaults = (list?.vaults ?? []).sort((a, b) => (b.aum_usdc ?? 0) - (a.aum_usdc ?? 0))
        // Take the first vault that actually holds positions — an empty vault
        // with only target weights makes a meaningless drift chart. Cap the
        // scan so a long vault list doesn't turn into a request storm.
        for (const v of vaults.slice(0, 4)) {
          if (cancelled) return
          const detail = await apiGet(`/api/vaults/${encodeURIComponent(v.address)}`)
          if (!detail.holdings?.length) continue
          // Sanity gate: the vault endpoint currently reports weight_pct in
          // the millions for some synth holdings (token-decimals bug upstream).
          // Corrupt numbers rendered as live data would be worse than the
          // sample — skip any vault whose weights aren't plausible fractions.
          const weightsPlausible = detail.holdings.every((h) => h.weight_pct >= 0 && h.weight_pct <= 150)
          if (!weightsPlausible) continue
          const current = new Map(detail.holdings.map((h) => [h.symbol, h.weight_pct / 100]))
          const target = new Map((detail.target_allocations ?? []).map((h) => [h.symbol, h.weight_pct / 100]))
          const symbols = [...new Set([...current.keys(), ...target.keys()])]
          if (cancelled) return
          setDriftRows(symbols.map((symbol) => ({
            symbol,
            current: current.get(symbol) ?? 0,
            target: target.get(symbol) ?? 0,
          })))
          setDriftVaultName(detail.name || v.address)
          return
        }
      } catch (_) {
        // No vaults reachable — drift keeps its sample render + badge.
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  // ── Recorded rebalance traces → trade log rows ─────────────────────────────
  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const data = await apiGet('/api/traces/?limit=50')
        if (cancelled) return
        const rows = (data?.traces ?? []).flatMap((t) =>
          (t.trades_executed ?? []).map((tr) => ({
            date: (t.timestamp || '').slice(0, 10),
            action: (tr.direction || '').toUpperCase(),
            asset: tr.symbol,
            amount: tr.amount,
          })),
        )
        if (rows.length) setTrades(rows)
      } catch (_) {
        // Trace store unreachable — log keeps its sample render + badge.
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  // On-demand returns for strategies outside the initial scan window, so any
  // library entry works as the selected subject.
  useEffect(() => {
    if (!selectedId || returnsById[selectedId] || missingIds.has(selectedId)) return
    let cancelled = false
    apiGet(`/api/strategies/${encodeURIComponent(selectedId)}/returns`)
      .then((d) => {
        if (cancelled) return
        const dr = d?.daily_returns
        if (Array.isArray(dr) && dr.length >= 60 && dr.some((v) => v !== 0)) {
          setReturnsById((cur) => ({ ...cur, [selectedId]: dr }))
        } else {
          setMissingIds((cur) => new Set(cur).add(selectedId))
        }
      })
      .catch(() => {
        if (!cancelled) setMissingIds((cur) => new Set(cur).add(selectedId))
      })
    return () => {
      cancelled = true
    }
  }, [selectedId, returnsById, missingIds])

  const selectedReturns = returnsById[selectedId] ?? null

  // Correlation inputs: every strategy with persisted returns, trimmed to the
  // shared trailing window so the series compare over the same span.
  const correlation = useMemo(() => {
    const withData = strategies.filter((s) => returnsById[s.id])
    if (withData.length < 2) return null
    const minLen = Math.min(...withData.map((s) => returnsById[s.id].length))
    return {
      assets: withData.map(shortLabel),
      series: withData.map((s) => returnsById[s.id].slice(-minLen)),
    }
  }, [strategies, returnsById])

  // Frontier scatter: one dot per strategy, annualized from its real returns.
  const frontierPoints = useMemo(() => {
    const pts = strategies
      .filter((s) => returnsById[s.id])
      .map((s) => {
        const rr = annualizedRiskReturn(returnsById[s.id])
        return rr ? { ...rr, label: s.paper_title || s.name || s.id } : null
      })
      .filter(Boolean)
    return pts.length >= 3 ? pts : null
  }, [strategies, returnsById])

  const liveCount = Object.keys(returnsById).length
  const strategiesWithAny = strategies.length

  return (
    <div className="quant-lab">
      <header className="app-page-heading">
        <p className="app-eyebrow">Risk and diagnostics</p>
        <h1>Quant Lab</h1>
        <p>
          Risk, optimization, and backtest diagnostics computed from the live library: persisted
          backtest returns are live today. Vault-allocation drift and the recorded-trade log activate
          once a vault is deployed; until then, those two panels render a synthetic sample and say so
          on their badge.
        </p>
      </header>

      {/* Subject selector + live-data status line */}
      <div className="card-flat" style={{ padding: 16, marginBottom: 20, display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <label className="caption" style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
          Strategy
          <select
            value={selectedId}
            onChange={(e) => setSelectedId(e.target.value)}
            disabled={strategies.length === 0}
            style={{ fontSize: '0.82rem', padding: '4px 8px', background: 'var(--glass)', border: '1px solid var(--glass-border)', borderRadius: 4, color: 'var(--text-1)' }}
          >
            {strategies.length === 0 ? (
              <option value="">
                {loading ? 'Loading strategies…' : libraryError ? 'Strategy library unavailable' : 'No strategies in library'}
              </option>
            ) : (
              strategies.map((s) => (
                <option key={s.id} value={s.id}>
                  {(s.paper_title || s.name || s.id) + (missingIds.has(s.id) ? ' (no persisted returns)' : '')}
                </option>
              ))
            )}
          </select>
        </label>
        <span className="caption" style={{ color: 'var(--text-4)' }}>
          {loading
            ? 'Loading library returns…'
            : `Persisted returns for ${liveCount} of ${strategiesWithAny} strategies` +
              (driftRows ? ` · drift from vault "${driftVaultName}"` : '') +
              (trades ? ` · ${trades.length} recorded trades` : '')}
        </span>
      </div>

      <RiskAnalysis
        returns={selectedReturns ?? undefined}
        assets={correlation?.assets}
        series={correlation?.series}
      />
      <div className="mt-8">
        <PortfolioAdvisorPanels
          frontierPoints={frontierPoints ?? undefined}
          driftRows={driftRows ?? undefined}
        />
      </div>
      <div className="mt-8">
        <BacktestVisualizer
          strategyId={selectedId || undefined}
          weights={SWEEP_WEIGHTS}
          realTrades={trades ?? undefined}
          libraryLoading={loading}
          libraryError={libraryError}
        />
      </div>
    </div>
  )
}
