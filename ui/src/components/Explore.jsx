import { useEffect, useMemo, useState } from 'react'
import AssetModal from './AssetModal'
import AssetGroupModal from './AssetGroupModal'
import AssetGroupIcon from './AssetGroupIcon'
import { groupMeta } from '../assetGroups'
import { median, changeWindowLabel, groupChangeWindowLabel } from '../statUtils'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

// /explore — Read-only viewer for the market data the strategy engine sees.
// No wallet required, no trade affordance. Per docs/specs/page-roles-spec.md,
// this is the discovery surface that helps a user form an opinion about what
// to ask Generate to build around.

function fmtPrice(v) {
  if (v == null || Number.isNaN(v)) return '—'
  if (v >= 1000) return `$${v.toFixed(0)}`
  if (v >= 10) return `$${v.toFixed(2)}`
  return `$${v.toFixed(4)}`
}

function fmtPct(v, digits = 2) {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(digits)}%`
}

function changeClass(v) {
  if (v == null || Number.isNaN(v)) return ''
  return v >= 0 ? 'positive' : 'negative'
}

export default function Explore() {
  const [assets, setAssets] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [filterClass, setFilterClass] = useState('all')
  const [openAsset, setOpenAsset] = useState(null)
  const [openGroup, setOpenGroup] = useState(null)
  const [view, setView] = useState('groups')

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/explore/assets`)
        if (!res.ok) throw new Error(`Backend returned ${res.status}`)
        const data = await res.json()
        if (!cancelled) {
          setAssets(data.assets || [])
          setError('')
        }
      } catch (e) {
        // Never render the response body as the error message — nginx 502s
        // come back as multi-line HTML and would splat across the page.
        // Same anti-pattern fixed in GenerationStatus.jsx (#323).
        const msg = e?.message && e.message.length < 120 ? e.message : 'Failed to load assets'
        if (!cancelled) setError(msg)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    // Reload every minute — page is read-only but oracle data drifts.
    const interval = setInterval(load, 60_000)
    return () => { cancelled = true; clearInterval(interval) }
  }, [])

  const classes = ['all', ...Array.from(new Set(assets.map(a => a.asset_class).filter(Boolean)))]
  const filtered = filterClass === 'all' ? assets : assets.filter(a => a.asset_class === filterClass)

  // Grouped-card view (#464): one card per asset_class bucket, the same
  // grouping the filter pills above already use. Sorted by member count so
  // the largest, most-populated groups surface first.
  const groups = useMemo(() => {
    const byAssetClass = new Map()
    for (const a of assets) {
      if (!a.asset_class) continue
      if (!byAssetClass.has(a.asset_class)) byAssetClass.set(a.asset_class, [])
      byAssetClass.get(a.asset_class).push(a)
    }
    return Array.from(byAssetClass.entries())
      .map(([assetClass, members]) => ({ assetClass, members, meta: groupMeta(assetClass) }))
      .sort((a, b) => b.members.length - a.members.length)
  }, [assets])

  // Banner only fires when *every* asset's displayed price is itself stale.
  // The backend now treats a missing on-chain oracle as "not stale" when
  // yfinance is the actual price source, so this banner is honest: it means
  // the feed pipeline is genuinely broken, not just "the oracle slot is
  // unused for this asset". See asset_market_service.py docstring.
  const allStale = assets.length > 0 && assets.every(a => a.is_stale)
  const staleCount = assets.filter(a => a.is_stale).length
  // Distinguish "majority stale" (markets closed + yfinance daily-close —
  // expected) from "minority stale" (a few feeds drifting — unusual). The
  // page used to say "Most assets are current" any time someStale was true,
  // which read as a lie on weekends when 60+ of 84 cards show STALE.
  const majorityStale = !allStale && staleCount > assets.length / 2
  const minorityStale = !allStale && !majorityStale && staleCount > 0

  // Honest oracle-coverage count (#1371) — derived from the served assets'
  // price_source, never a literal. Today this resolves to 2 (sSPY + sBTC,
  // oracle_updater's only pushed symbols) of the ~281-asset universe; the
  // copy below must say so rather than implying oracle-primary pricing.
  const oracleBackedCount = assets.filter(a => a.price_source === 'oracle').length
  const oracleCoverageNote = assets.length > 0
    ? `on-chain oracle for ${oracleBackedCount} of the ${assets.length} assets below today`
    : 'on-chain oracle for a small subset of assets today'

  return (
    <div>
      {/* Top-of-page header & explanation */}
      <div style={{ maxWidth: 760, marginBottom: 28 }}>
        <h2 className="serif" style={{ fontSize: '2rem', marginBottom: 10 }}>Explore</h2>
        <p className="body" style={{ marginBottom: 8, fontWeight: 500 }}>
          Explore is a read-only viewer for the market data the strategy engine sees.
        </p>
        <p className="body" style={{ color: 'var(--text-3)' }}>
          Browse the universe of synthetic assets that Archimedes can allocate into,
          look at current spot prices, recent moves, and 30-day volatility, then form
          an opinion about what looks over- or under-valued. When you're ready,
          head to Generate and describe a strategy around the names that caught your eye —
          nothing on this page places a trade or moves a position.
        </p>
        <p className="caption" style={{ color: 'var(--text-4)', marginTop: 8 }}>
          Click any card for full detail, price-history chart, and the upstream source the
          price came from. Most assets here are priced from off-chain market-data feeds
          (yfinance) — it's {oracleCoverageNote}; each card's "Source" field says which
          applies to it.
        </p>
      </div>

      {/* Grouped-cards vs. flat-list view toggle */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 14 }}>
        <button
          type="button"
          className={`btn btn-sm ${view === 'groups' ? '' : 'btn-outline'}`}
          onClick={() => setView('groups')}
          style={{
            background: view === 'groups' ? 'var(--accent-muted)' : undefined,
            color: view === 'groups' ? 'var(--accent)' : undefined,
            borderColor: view === 'groups' ? 'var(--accent)' : undefined,
          }}
          aria-pressed={view === 'groups'}
        >
          By group
        </button>
        <button
          type="button"
          className={`btn btn-sm ${view === 'assets' ? '' : 'btn-outline'}`}
          onClick={() => setView('assets')}
          style={{
            background: view === 'assets' ? 'var(--accent-muted)' : undefined,
            color: view === 'assets' ? 'var(--accent)' : undefined,
            borderColor: view === 'assets' ? 'var(--accent)' : undefined,
          }}
          aria-pressed={view === 'assets'}
        >
          All assets
        </button>
      </div>

      {/* Filter pills — only meaningful for the flat asset list */}
      {view === 'assets' && (
        <div className="strat-filter-bar" style={{ marginBottom: 18 }}>
          {/* Buttons with aria-pressed, matching the view toggles directly
              above — as click-only spans these were unreachable from the
              keyboard on the default /app landing page (2.1.1 / 4.1.2). */}
          {classes.map(c => (
            <button
              key={c}
              type="button"
              className={`tag ${filterClass === c ? 'tag-accent' : 'tag-muted'}`}
              aria-pressed={filterClass === c}
              onClick={() => setFilterClass(c)}
            >
              {c === 'all' ? 'All' : c.replace(/_/g, ' ')}
              {c !== 'all' && ` (${assets.filter(a => a.asset_class === c).length})`}
            </button>
          ))}
        </div>
      )}

      {/* Loading / error / empty states */}
      {loading && !assets.length && <div className="caption">Loading market data…</div>}
      {error && !assets.length && (
        <div className="info-box warning" style={{ marginBottom: 16 }}>
          Couldn't load assets: {error}.
        </div>
      )}
      {!loading && !error && assets.length === 0 && (
        <div className="info-box" style={{ marginBottom: 16 }}>
          No market data available right now. This page refreshes automatically.
        </div>
      )}

      {/* Banner — only when something is actually wrong with the feed. */}
      {allStale && (
        <div className="info-box warning" style={{ marginBottom: 16 }}>
          Every asset's price feed is older than the freshness threshold.
          The upstream market-data pipeline appears to be paused; values shown may be outdated.
        </div>
      )}
      {majorityStale && (
        <div className="info-box" style={{ marginBottom: 16, fontSize: '0.84rem' }}>
          <strong>{staleCount}/{assets.length}</strong> assets show STALE — most equity / ETF feeds
          run on yfinance daily-close prices and read as stale outside the US trading window.
          24/7 markets (crypto, FX, futures) stay current.
        </div>
      )}
      {minorityStale && (
        <div className="info-box" style={{ marginBottom: 16, fontSize: '0.84rem' }}>
          A few assets ({staleCount} of {assets.length}) have stale price feeds (STALE badge on the card).
          Most feeds are current.
        </div>
      )}

      {/* Grouped-asset card grid */}
      {view === 'groups' && groups.length > 0 && (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
            gap: 14,
          }}
        >
          {groups.map(g => {
            const medianChange = (() => {
              const vals = g.members.map(a => a.change_24h_pct).filter(v => v != null && !Number.isNaN(v))
              return median(vals)
            })()
            // Null when the members' windows disagree — a group spanning a
            // holiday genuinely has no single true window (#1378).
            const medianWindow = groupChangeWindowLabel(g.members)
            return (
              <button
                key={g.assetClass}
                type="button"
                onClick={() => setOpenGroup(g)}
                className="card-flat"
                style={{
                  textAlign: 'left',
                  padding: 16,
                  background: 'var(--glass)',
                  border: '1px solid var(--glass-border)',
                  borderRadius: 8,
                  cursor: 'pointer',
                  color: 'inherit',
                  font: 'inherit',
                  transition: 'background 0.15s, border-color 0.15s',
                }}
                onMouseEnter={e => {
                  e.currentTarget.style.background = 'var(--glass-hover)'
                  e.currentTarget.style.borderColor = 'var(--text-4)'
                }}
                onMouseLeave={e => {
                  e.currentTarget.style.background = 'var(--glass)'
                  e.currentTarget.style.borderColor = 'var(--glass-border)'
                }}
                aria-label={`Open details for ${g.meta.label} group`}
              >
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
                  <div
                    style={{
                      width: 34, height: 34, borderRadius: 7, flexShrink: 0,
                      background: 'var(--surface-1)', border: '1px solid var(--glass-border)',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      color: 'var(--accent)',
                    }}
                  >
                    <AssetGroupIcon icon={g.meta.icon} size={18} />
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: '1.02rem', fontWeight: 700, lineHeight: 1.2 }}>{g.meta.label}</div>
                    <div className="caption" style={{ color: 'var(--text-4)', fontSize: '0.7rem', marginTop: 2 }}>
                      {g.members.length} asset{g.members.length === 1 ? '' : 's'}
                    </div>
                  </div>
                </div>

                <p
                  className="caption"
                  style={{
                    color: 'var(--text-3)',
                    fontSize: '0.76rem',
                    marginTop: 10,
                    lineHeight: 1.4,
                    display: '-webkit-box',
                    WebkitLineClamp: 3,
                    WebkitBoxOrient: 'vertical',
                    overflow: 'hidden',
                  }}
                >
                  {g.meta.description}
                </p>

                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginTop: 12 }}>
                  <span className={`mono ${changeClass(medianChange)}`} style={{ fontSize: '0.85rem' }}>
                    {fmtPct(medianChange)}
                  </span>
                  <span className="caption" style={{ color: 'var(--text-4)', fontSize: '0.65rem' }}>
                    {medianWindow ? `median ${medianWindow}` : 'median change'}
                  </span>
                </div>
              </button>
            )
          })}
        </div>
      )}

      {/* Asset card grid */}
      {view === 'assets' && filtered.length > 0 && (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
            gap: 14,
          }}
        >
          {filtered.map(a => (
            <button
              key={a.symbol}
              type="button"
              onClick={() => setOpenAsset(a)}
              className="card-flat"
              style={{
                textAlign: 'left',
                padding: 16,
                background: 'var(--glass)',
                border: '1px solid var(--glass-border)',
                borderRadius: 8,
                cursor: 'pointer',
                color: 'inherit',
                font: 'inherit',
                transition: 'background 0.15s, border-color 0.15s',
              }}
              onMouseEnter={e => {
                e.currentTarget.style.background = 'var(--glass-hover)'
                e.currentTarget.style.borderColor = 'var(--text-4)'
              }}
              onMouseLeave={e => {
                e.currentTarget.style.background = 'var(--glass)'
                e.currentTarget.style.borderColor = 'var(--glass-border)'
              }}
              aria-label={`Open details for ${a.symbol}`}
            >
              <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 6 }}>
                <div>
                  <div style={{ fontSize: '1.15rem', fontWeight: 700, lineHeight: 1.1 }}>{a.symbol}</div>
                  <div className="caption" style={{
                    color: 'var(--text-4)',
                    fontSize: '0.7rem',
                    marginTop: 2,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    maxWidth: 180,
                  }}>
                    {a.name || '—'}
                  </div>
                </div>
                {a.is_stale && (
                  <span
                    className="tag"
                    style={{
                      fontSize: '0.6rem',
                      background: 'rgba(239,68,68,0.10)',
                      color: 'var(--negative)',
                      borderRadius: 4,
                      padding: '1px 5px',
                      whiteSpace: 'nowrap',
                    }}
                    title="The displayed price is older than the freshness window"
                  >
                    STALE
                  </span>
                )}
              </div>

              <div className="mono" style={{ fontSize: '1.4rem', fontWeight: 600, marginTop: 12 }}>
                {fmtPrice(a.current_price)}
              </div>

              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginTop: 6 }}>
                <span
                  className={`mono ${changeClass(a.change_24h_pct)}`}
                  style={{ fontSize: '0.85rem' }}
                  title={
                    a.rejected_fields?.includes('change_24h_pct')
                      ? 'Suppressed: the computed change was arithmetically implausible (likely a bad tick), not a real move'
                      : undefined
                  }
                >
                  {fmtPct(a.change_24h_pct)}
                </span>
                <span
                  className="caption"
                  style={{ color: 'var(--text-4)', fontSize: '0.65rem' }}
                  title={
                    a.change_window_hours != null
                      ? `Change over the ${a.change_window_hours.toFixed(0)}h between the last two bars`
                      : 'Change since the previous close; the elapsed window could not be determined'
                  }
                >
                  {changeWindowLabel(a)}
                </span>
              </div>
            </button>
          ))}
        </div>
      )}

      {/* Footer disclosure */}
      <p className="caption" style={{ marginTop: 22, color: 'var(--text-4)' }}>
        {oracleBackedCount} of the {assets.length || 'these'} assets above are priced
        from the on-chain PriceOracle today; the rest are priced from yfinance
        (off-chain market data) — each card's "Source" field says which applies to it.
        "STALE" means the displayed price is itself older than the freshness window
        (5 minutes for the oracle, ~4 days for daily-close fallback). The "Vol 30d"
        metric in the detail modal is annualized realized volatility (std of daily
        returns × √252).
      </p>

      {/* Data-sourcing disclosure (#1218) — see docs/adr/market-data-sourcing.md.
          Deliberately plain-language and deliberately here rather than buried in a
          legal page: the honest thing to say is that this page and the paid
          analysis run on DIFFERENT data under different terms, and a reader can
          only check that claim if we make it where the data is shown.
          Pinned by ui/test/explore-data-disclosure.test.js. */}
      <p className="caption" style={{ marginTop: 14, color: 'var(--text-4)' }}>
        <strong>About this data.</strong> Explore is a free, open-source viewer over
        yfinance market-data streams. Nothing on this page is sold or commercially
        redistributed — it is here to look at, and that is the whole of it. Paid
        analysis runs on separately licensed data, not on this feed; the two are
        sourced independently on purpose.
      </p>

      {openAsset && <AssetModal asset={openAsset} onClose={() => setOpenAsset(null)} />}
      {openGroup && (
        <AssetGroupModal
          assetClass={openGroup.assetClass}
          assets={openGroup.members}
          onClose={() => setOpenGroup(null)}
        />
      )}
    </div>
  )
}
