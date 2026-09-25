import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import PriceHistoryChart, { fmtPrice } from './PriceHistoryChart'
import { changeWindowLabel } from '../statUtils'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const RANGES = ['1D', '1W', '1M', '1Y', '5Y', '10Y', 'MAX']

function fmtPct(v) {
  if (v == null || Number.isNaN(v)) return '—'
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(2)}%`
}

function changeClass(v) {
  if (v == null || Number.isNaN(v)) return ''
  return v >= 0 ? 'positive' : 'negative'
}

function sourceLabel(price_source) {
  if (price_source === 'oracle') return 'On-chain PriceOracle (Arc)'
  if (price_source === 'yfinance') return 'yfinance (off-chain fallback)'
  return 'No source available'
}

// A rejected field (#1322) is a computed value actively suppressed as
// arithmetically implausible (a bad tick) — distinct from simply not having
// enough history yet. Surfaced as a tooltip on the "—" so it doesn't read
// as an unexplained gap.
function rejectedTitle(asset, field) {
  return asset.rejected_fields?.includes(field)
    ? 'Suppressed: the computed value was arithmetically implausible (likely a bad tick), not a real move'
    : undefined
}

export default function AssetModal({ asset, onClose }) {
  const [range, setRange] = useState('1M')
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // Esc closes
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Lock body scroll while modal is open
  useEffect(() => {
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = prev }
  }, [])

  // Fetch on range change. We do NOT silently swallow errors — if the
  // upstream feed returns nothing, the chart renders an explicit empty
  // state instead of a faked flat line.
  useEffect(() => {
    if (!asset?.symbol) return
    let cancelled = false
    setLoading(true)
    setError('')
    fetch(`${API_BASE}/api/explore/assets/${asset.symbol}/history?range=${range}`)
      .then(async res => {
        if (!res.ok) {
          // 404 just means "no series for this range"; fall through with empty data.
          if (res.status === 404) {
            if (!cancelled) { setHistory([]); setError('') }
            return null
          }
          throw new Error(`History fetch failed (${res.status})`)
        }
        return res.json()
      })
      .then(data => {
        if (cancelled || data == null) return
        setHistory(Array.isArray(data.points) ? data.points : [])
      })
      .catch(e => {
        if (!cancelled) {
          setError(e?.message || 'Failed to load price history')
          setHistory([])
        }
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [asset?.symbol, range])

  // 24h high/low: try to compute from the 1D intraday series when available.
  // This is the honest computation — falls back to "—" if we don't have data.
  const intradayStats = useMemo(() => {
    if (range !== '1D' || history.length === 0) return { high: null, low: null }
    const prices = history.map(p => p.price).filter(p => p != null && !Number.isNaN(p))
    if (prices.length === 0) return { high: null, low: null }
    return { high: Math.max(...prices), low: Math.min(...prices) }
  }, [history, range])

  if (!asset) return null

  const high24 = asset.high_24h ?? intradayStats.high
  const low24 = asset.low_24h ?? intradayStats.low

  return createPortal(
    <div
      className="fixed inset-0 flex items-center justify-center z-[1000]"
      style={{ background: 'rgba(0,0,0,0.78)', backdropFilter: 'blur(6px)' }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-labelledby="asset-modal-title"
    >
      <div
        className="card-elevated p-6"
        onClick={e => e.stopPropagation()}
        style={{
          background: 'var(--surface-1)',
          maxHeight: '90vh', overflowY: 'auto',
          width: 'min(820px, 94vw)',
        }}
      >
        {/* Header */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16 }}>
          <div>
            <h3 id="asset-modal-title" className="serif" style={{ fontSize: '1.6rem', margin: 0 }}>
              {asset.symbol}
            </h3>
            <div className="caption" style={{ color: 'var(--text-3)', marginTop: 2 }}>
              {asset.name || '—'}
              {asset.asset_class && (
                <span className="tag tag-muted" style={{ marginLeft: 8, fontSize: '0.65rem' }}>
                  {asset.asset_class.replace(/_/g, ' ')}
                </span>
              )}
            </div>
          </div>
          <button
            className="btn btn-outline btn-sm"
            onClick={onClose}
            aria-label="Close asset details"
          >
            Close (Esc)
          </button>
        </div>

        {/* Price block */}
        <div style={{ display: 'flex', gap: 24, alignItems: 'baseline', marginTop: 18, flexWrap: 'wrap' }}>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>Current price</div>
            <div className="mono" style={{ fontSize: '2rem', fontWeight: 600 }}>
              {fmtPrice(asset.current_price)}
            </div>
          </div>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>
              {changeWindowLabel(asset)} change
            </div>
            <div
              className={`mono ${changeClass(asset.change_24h_pct)}`}
              style={{ fontSize: '1.1rem', fontWeight: 600 }}
              title={rejectedTitle(asset, 'change_24h_pct')}
            >
              {fmtPct(asset.change_24h_pct)}
            </div>
          </div>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>24h high</div>
            <div className="mono" style={{ fontSize: '1.1rem' }}>{fmtPrice(high24)}</div>
          </div>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>24h low</div>
            <div className="mono" style={{ fontSize: '1.1rem' }}>{fmtPrice(low24)}</div>
          </div>
        </div>

        {/* Range toggle */}
        <div style={{ marginTop: 18, display: 'flex', gap: 6, alignItems: 'center' }}>
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
            {range === '1D' ? 'Intraday 5-minute bars' : 'Daily close'}
          </span>
        </div>

        {/* Chart */}
        <div style={{ marginTop: 10 }}>
          <PriceHistoryChart points={history} loading={loading} error={error} emptyHeadline="Historical chart unavailable for this asset in the selected range." />
        </div>

        {/* Meta grid: source, last updated, longer-window changes, vol */}
        <div
          style={{
            marginTop: 18,
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
            gap: 14,
            paddingTop: 14,
            borderTop: '1px solid var(--glass-border)',
          }}
        >
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>Source</div>
            <div className="body" style={{ fontSize: '0.9rem' }}>{sourceLabel(asset.price_source)}</div>
          </div>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>Last updated</div>
            <div className="mono" style={{ fontSize: '0.82rem' }}>
              {asset.last_updated ? new Date(asset.last_updated).toLocaleString() : '—'}
            </div>
          </div>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>7d change</div>
            <div className={`mono ${changeClass(asset.change_7d_pct)}`} title={rejectedTitle(asset, 'change_7d_pct')}>
              {fmtPct(asset.change_7d_pct)}
            </div>
          </div>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>30d change</div>
            <div className={`mono ${changeClass(asset.change_30d_pct)}`} title={rejectedTitle(asset, 'change_30d_pct')}>
              {fmtPct(asset.change_30d_pct)}
            </div>
          </div>
          <div>
            <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>
              Realized vol (30d, annualized)
            </div>
            <div className="mono" title={rejectedTitle(asset, 'realized_vol_30d')}>
              {asset.realized_vol_30d != null ? asset.realized_vol_30d.toFixed(2) : '—'}
            </div>
          </div>
          {/* oracle_address is a capability marker (populated whenever a deployed
              PriceOracle contract exists for this symbol, regardless of whether
              its price is actually being displayed — see asset_market_service.py's
              "capability marker" comment, issue #346). Rendering it whenever it's
              merely present implied every card with an address was oracle-priced;
              gate on price_source === 'oracle' too, so the address only shows for
              a card whose displayed price actually came from that oracle (#1371). */}
          {asset.oracle_address && asset.price_source === 'oracle' && (
            <div>
              <div className="caption" style={{ color: 'var(--text-3)', fontSize: '0.7rem' }}>Oracle address</div>
              <div className="mono" style={{ fontSize: '0.72rem', wordBreak: 'break-all' }}>
                {asset.oracle_address}
              </div>
            </div>
          )}
        </div>

        {asset.is_stale && (
          <div className="info-box warning" style={{ marginTop: 14, fontSize: '0.8rem' }}>
            The displayed price for this asset is older than the freshness threshold.
            The upstream feed ({sourceLabel(asset.price_source)}) has not updated recently.
          </div>
        )}
      </div>
    </div>,
    document.body
  )
}
