import { apiGet } from '../api'
import { useState, useEffect, useCallback } from 'react'
import { isAddress, parseUnits, formatUnits } from 'viem'
import {
  publicClient, getWalletClient, getAddress,
  getConnectedProvider, getSmartAccount, getSmartAccountClient, CIRCLE_PROVIDER_ID,
  USDC, TOKEN_ABI, VAULT_ABI,
  ASSETS, USDC_DECIMALS,
} from '../config'
import { executeUserOp, encodeCall } from '../circle-tx-executor'

// Wallet routing: EOA wallets sign via viem writeContract; passkey/Circle
// Modular Wallets have no EIP-1193 provider and must sign via the bundler
// instead — getWalletClient() throws for that path (see config.js). Mirrors
// the sendContractCall helper added to CreateVaultModal.jsx (#1089).
//
// Both branches must guard against a mined-but-reverted tx reading as
// success: executeUserOp already throws on receipt.success === false, but
// viem's waitForTransactionReceipt does NOT throw on a reverted tx — it only
// throws on things like a dropped/replaced tx — so the EOA branch checks
// receipt.status explicitly (same guard as DepositFlow.jsx's EOA flow).
function sendContractCall({ address, abi, functionName, args }) {
  if (getConnectedProvider() === CIRCLE_PROVIDER_ID) {
    const smartAccount = getSmartAccount()
    const client = getSmartAccountClient()
    if (!smartAccount || !client) {
      throw new Error('Passkey wallet not initialized — please reconnect.')
    }
    return executeUserOp({
      smartAccount,
      client,
      calls: [encodeCall({ address, abi, functionName, args })],
    }).then(out => out.txHash)
  }
  return getWalletClient().then(async walletClient => {
    const hash = await walletClient.writeContract({ address, abi, functionName, args })
    const receipt = await publicClient.waitForTransactionReceipt({ hash })
    if (receipt.status !== 'success') {
      throw new Error(`Transaction reverted on-chain (${hash.slice(0, 10)}…${hash.slice(-6)})`)
    }
    return hash
  })
}

function timeAgo(ts) {
  if (!ts) return '—'
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  // A missing timestamp often round-trips as epoch 0 rather than an empty
  // value — that's not a real multi-decade-old event, it's an absent
  // timestamp, so render it the same as the no-value case.
  if (d.getTime() <= 0) return '—'
  const secs = Math.floor((Date.now() - d.getTime()) / 1000)
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

function shortAddr(addr) {
  if (!addr) return '—'
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`
}

function _knownTokenSymbol(addr) {
  if (!addr) return '—'
  const addrLower = addr.toLowerCase()
  if (addrLower === USDC.toLowerCase()) return 'USDC'
  for (const asset of ASSETS) {
    if (asset.token.toLowerCase() === addrLower) return asset.sym
  }
  return `${addr.slice(0, 6)}...`
}

function SharpeDriftBadge({ drift }) {
  if (!drift) return null
  const statusColors = {
    NORMAL: { bg: 'rgba(16,185,129,0.12)', color: 'var(--positive)', border: 'rgba(16,185,129,0.3)', label: 'NORMAL' },
    WARNING: { bg: 'rgba(245,158,11,0.12)', color: 'var(--warning)', border: 'rgba(245,158,11,0.3)', label: 'WARNING' },
    CRITICAL: { bg: 'rgba(239,68,68,0.12)', color: 'var(--negative)', border: 'rgba(239,68,68,0.3)', label: 'CRITICAL' },
    INSUFFICIENT_DATA: { bg: 'var(--glass-hover)', color: 'var(--text-3)', border: 'var(--glass-border)', label: 'INSUFFICIENT DATA' },
  }
  const s = statusColors[drift.status] || statusColors.INSUFFICIENT_DATA
  return (
    <div style={{ padding: '12px 16px', background: 'var(--surface-2)', borderRadius: 8, border: '1px solid var(--glass-border)', marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-3)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          Performance Tracker
        </span>
        <span style={{ fontSize: '0.72rem', fontWeight: 700, padding: '2px 8px', borderRadius: 4, background: s.bg, color: s.color, border: `1px solid ${s.border}` }}>
          {s.label}
        </span>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10 }}>
        <div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-3)', marginBottom: 2 }}>Live Sharpe</div>
          <div style={{ fontWeight: 700, fontSize: '1rem', color: 'var(--text-1)' }}>
            {drift.live_sharpe != null ? drift.live_sharpe.toFixed(3) : '—'}
          </div>
        </div>
        <div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-3)', marginBottom: 2 }}>Backtest</div>
          <div style={{ fontWeight: 700, fontSize: '1rem', color: 'var(--text-1)' }}>
            {drift.backtest_sharpe != null ? drift.backtest_sharpe.toFixed(3) : '—'}
          </div>
        </div>
        <div>
          <div style={{ fontSize: '0.7rem', color: 'var(--text-3)', marginBottom: 2 }}>Decay Floor</div>
          <div style={{ fontWeight: 700, fontSize: '1rem', color: 'var(--text-2)' }}>
            {drift.decay_floor != null ? drift.decay_floor.toFixed(3) : '—'}
          </div>
        </div>
      </div>
      {/* Only show McLean-Pontiff footnote when we have real data to compare against (#389) */}
      {drift.live_sharpe != null && (
        <div style={{ marginTop: 6, fontSize: '0.68rem', color: 'var(--text-4)' }}>
          McLean-Pontiff 42% post-publication retention floor
          {drift.drift_sigma != null && ` · drift ${drift.drift_sigma > 0 ? '+' : ''}${drift.drift_sigma.toFixed(2)}σ`}
        </div>
      )}
    </div>
  )
}

export default function VaultDetail({ address, onBack }) {
  const [detail, setDetail] = useState(null)
  const [onChainData, setOnChainData] = useState(null)
  const [health, setHealth] = useState(null)
  const [depositAmt, setDepositAmt] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  // Withdraw / redeem state (Issue #466). `withdrawShares` is the user's input
  // in vault-share units; `shareBalance` is their on-chain balanceOf; `previewOut`
  // is the previewRedeem(shares) USDC quote the user confirms before signing.
  const [withdrawShares, setWithdrawShares] = useState('')
  const [shareBalance, setShareBalance] = useState(null)
  const [previewOut, setPreviewOut] = useState(null)
  const wallet = getAddress()

  // Fetch backend data
  useEffect(() => {
    if (!address) return
    let cancelled = false
    apiGet(`/api/vaults/${address}`)
      .then(data => { if (!cancelled) setDetail(data) })
      .catch(() => {})
    // Also fetch off-chain metadata (name, symbol, strategies)
    apiGet(`/api/vaults/${address}/metadata`)
      .then(meta => {
        if (!cancelled && meta) {
          setDetail(prev => prev ? { ...prev, ...meta } : prev)
        }
      })
      .catch(() => {})
    // Fetch vault health (includes Sharpe drift)
    apiGet(`/api/vaults/${address}/health`)
      .then(h => { if (!cancelled) setHealth(h) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [address])

  // Fetch on-chain data
  const loadOnChain = useCallback(async () => {
    if (!address) return
    try {
      // Read each individually — totalAssets may revert on stale oracle prices
      const [totalSupply, creator, tier, paused, asset] = await Promise.all([
        publicClient.readContract({ address, abi: VAULT_ABI, functionName: 'totalSupply' }),
        publicClient.readContract({ address, abi: VAULT_ABI, functionName: 'creator' }),
        publicClient.readContract({ address, abi: VAULT_ABI, functionName: 'tier' }),
        publicClient.readContract({ address, abi: VAULT_ABI, functionName: 'paused' }),
        publicClient.readContract({ address, abi: VAULT_ABI, functionName: 'asset' }),
      ])

      // totalAssets may revert (StalePrice) — read separately with fallback to backend API
      let totalAssets
      try {
        totalAssets = await publicClient.readContract({ address, abi: VAULT_ABI, functionName: 'totalAssets' })
      } catch {
        // Fallback: use backend API which handles stale prices gracefully
        try {
          const backendData = await apiGet(`/api/vaults/${address}`)
          totalAssets = BigInt(Math.round((backendData.aum_usdc || 0) * 1e6))
        } catch {
          totalAssets = 0n
        }
      }

      // Read target allocations
      let targetAllocs = []
      try {
        const [tokens, weights] = await publicClient.readContract({
          address, abi: VAULT_ABI, functionName: 'getTargetAllocations',
        })
        targetAllocs = tokens.map((t, i) => ({
          token: t,
          weightBps: Number(weights[i]),
          symbol: _knownTokenSymbol(t),
        }))
      } catch {
        // getTargetAllocations not supported on older vault deployments — leave targetAllocs empty.
      }

      // Read vault name from on-chain
      let onChainName = ''
      try {
        onChainName = await publicClient.readContract({ address, abi: VAULT_ABI, functionName: 'name' })
      } catch {
        // name() not available on legacy vaults — fall back to the detail.name or shortAddr below.
      }

      setOnChainData({
        totalAssets: Number(totalAssets) / 1e6,
        totalSupply: Number(totalSupply),
        sharePrice: Number(totalSupply) > 0 ? Number(totalAssets) / Number(totalSupply) / 1e6 : 1,
        name: onChainName,
        creator, tier: Number(tier), paused, asset,
        targetAllocations: targetAllocs,
      })
    } catch {
      // Top-level vault read failed (RPC down or bad address) — leave onChainData null; UI renders the detail-prop fallback.
    }
  }, [address])

  useEffect(() => { loadOnChain() }, [loadOnChain])

  // ── Withdraw / redeem (Issue #466) ───────────────────────────
  // Vault shares are 18-decimal ERC20 tokens, but the vault preserves a strict
  // 1:1 raw share:asset scale (see Vault.sol — "dead shares" inflation guard),
  // so 1 USDC deposited (1e6 raw) mints 1e6 raw shares. We therefore display +
  // parse shares in the same 6-decimal "USDC-equivalent" unit the user deposited
  // in, which keeps the numbers legible and exact. The ground truth for USDC out
  // is always previewRedeem(shares), read fresh from chain before the user signs.
  const SHARE_DECIMALS = USDC_DECIMALS

  // Read the connected wallet's vault-share balance (balanceOf).
  const loadShareBalance = useCallback(async () => {
    if (!address || !isAddress(address)) return
    let owner
    try {
      owner = getAddress()
    } catch {
      // Wallet hook not ready — leave shareBalance null; the UI shows a connect prompt.
      return
    }
    if (!owner) return
    try {
      const bal = await publicClient.readContract({
        address, abi: VAULT_ABI, functionName: 'balanceOf', args: [owner],
      })
      setShareBalance(bal)
    } catch {
      // balanceOf revert (RPC down / bad address) — leave prior value; don't crash the panel.
    }
  }, [address])

  useEffect(() => { loadShareBalance() }, [loadShareBalance])

  // Quote USDC out for the entered share amount via previewRedeem. Debounced so we
  // don't hammer the RPC on every keystroke. previewRedeem returns USDC (6-dec).
  useEffect(() => {
    if (!address || !isAddress(address) || !withdrawShares) {
      setPreviewOut(null)
      return
    }
    let cancelled = false
    const t = setTimeout(async () => {
      let shares
      try {
        shares = parseUnits(withdrawShares, SHARE_DECIMALS)
      } catch {
        if (!cancelled) setPreviewOut(null)
        return
      }
      if (shares <= 0n) { if (!cancelled) setPreviewOut(null); return }
      try {
        const out = await publicClient.readContract({
          address, abi: VAULT_ABI, functionName: 'previewRedeem', args: [shares],
        })
        if (!cancelled) setPreviewOut(out)
      } catch {
        if (!cancelled) setPreviewOut(null)
      }
    }, 300)
    return () => { cancelled = true; clearTimeout(t) }
  }, [address, withdrawShares, SHARE_DECIMALS])

  const setMaxShares = () => {
    if (shareBalance == null) return
    setWithdrawShares(formatUnits(shareBalance, SHARE_DECIMALS))
  }

  const withdraw = async () => {
    if (!address || !withdrawShares) return
    // Same anti-phishing guard as deposit: never route a redeem to an
    // unvalidated address from a deep-link.
    if (!isAddress(address)) {
      setStatus('Invalid vault address — refusing to redeem.')
      return
    }
    let shares
    try {
      shares = parseUnits(withdrawShares, SHARE_DECIMALS)
    } catch {
      setStatus('Invalid share amount.')
      return
    }
    if (shares <= 0n) { setStatus('Enter a share amount greater than zero.'); return }
    if (shareBalance != null && shares > shareBalance) {
      setStatus('Amount exceeds your share balance.')
      return
    }
    setBusy(true); setStatus('')
    try {
      const owner = getAddress()
      // Non-custodial: the USER signs redeem(shares, receiver, owner). The backend
      // never holds keys for this — receiver and owner are both the connected wallet,
      // so funds can only flow back to the signer. redeem burns `shares` and returns
      // the previewed USDC.
      setStatus('Redeeming…')
      const hash = await sendContractCall({
        address, abi: VAULT_ABI, functionName: 'redeem', args: [shares, owner, owner],
      })
      setStatus(
        <span>
          Withdrawn! TX:{' '}
          <a
            href={`https://testnet.arcscan.app/tx/${hash}`}
            target="_blank"
            rel="noopener noreferrer"
            className="mono"
            style={{ color: 'var(--accent)' }}
          >
            {hash.slice(0, 10)}…{hash.slice(-6)} ↗
          </a>
        </span>
      )
      setWithdrawShares('')
      setPreviewOut(null)
      loadOnChain()
      loadShareBalance()
    } catch (err) { setStatus(err.shortMessage || err.message) }
    setBusy(false)
  }

  const deposit = async () => {
    if (!address || !depositAmt) return
    // The vault address comes from the URL (deep-link); never route a USDC
    // approve/deposit to an unvalidated address — a phishing link like
    // /portfolio/vaults/0xATTACKER would otherwise receive the approval.
    if (!isAddress(address)) {
      setStatus('Invalid vault address — refusing to send funds.')
      return
    }
    setBusy(true); setStatus('')
    try {
      // parseUnits is exact for decimal strings; float math (parseFloat * 1e6)
      // is lossy for large/many-decimal inputs.
      const amount = parseUnits(depositAmt, USDC_DECIMALS)
      const approveCall = { address: USDC, abi: TOKEN_ABI, functionName: 'approve', args: [address, amount] }
      const depositCall = { address, abi: VAULT_ABI, functionName: 'deposit', args: [amount, getAddress()] }
      let hash
      if (getConnectedProvider() === CIRCLE_PROVIDER_ID) {
        // Passkey wallets: batch approve + deposit into a single signed user
        // operation (mirrors PasskeyDepositFlow in DepositFlow.jsx) — one
        // WebAuthn prompt instead of two, and no partial state where the
        // approval lands but the deposit doesn't. Both target addresses
        // (USDC, vault) are known up front, so there's no CREATE-address
        // ordering constraint blocking the batch.
        setStatus('Approving USDC and depositing…')
        const smartAccount = getSmartAccount()
        const client = getSmartAccountClient()
        if (!smartAccount || !client) {
          throw new Error('Passkey wallet not initialized — please reconnect.')
        }
        const out = await executeUserOp({
          smartAccount,
          client,
          calls: [encodeCall(approveCall), encodeCall(depositCall)],
        })
        hash = out.txHash
      } else {
        setStatus('Approving USDC…')
        await sendContractCall(approveCall)
        setStatus('Depositing…')
        hash = await sendContractCall(depositCall)
      }
      // Render TX hash as a clickable arcscan link (Issue #389 follow-up — Pi
      // missed this in the original bundle). status is a ReactNode so we can
      // mix string + <a>; the renderer at line 376 already passes through.
      setStatus(
        <span>
          Deposited! TX:{' '}
          <a
            href={`https://testnet.arcscan.app/tx/${hash}`}
            target="_blank"
            rel="noopener noreferrer"
            className="mono"
            style={{ color: 'var(--accent)' }}
          >
            {hash.slice(0, 10)}…{hash.slice(-6)} ↗
          </a>
        </span>
      )
      setDepositAmt('')
      loadOnChain()
      loadShareBalance()
    } catch (err) { setStatus(err.shortMessage || err.message) }
    setBusy(false)
  }

  const name = detail?.name || onChainData?.name || `Vault ${shortAddr(address)}`
  const tier = onChainData?.tier ?? detail?.tier ?? 1
  const aum = onChainData?.totalAssets ?? detail?.aum_usdc ?? 0
  const sharePrice = onChainData?.sharePrice ?? detail?.share_price ?? 1

  return (
    <div className="vault-detail-page">
      {/* Back button */}
      <button className="back-btn flex items-center gap-1.5" onClick={onBack}><span className="i-lucide-arrow-left w-4 h-4" /> Back to Vaults</button>

      {/* Vault Header */}
      <div className="vault-detail-header">
        <div className="vault-detail-title-row">
          <h2 className="vault-detail-name">{name}</h2>
          <span className={`vault-tier-badge tier-${tier}`}>
            <span className={tier === 1 ? 'i-lucide-trophy' : 'i-lucide-users'} style={{width:12,height:12,marginRight:4}} />{tier === 1 ? 'Verified' : 'Community'}
          </span>
        </div>
        <div className="vault-detail-meta">
          <code>{address}</code>
        </div>
      </div>

      {/* Stats Grid */}
      <div className="vault-stats-grid">
        <div className="vault-stat-card">
          <div className="vault-stat-label">AUM</div>
          <div className="vault-stat-value">${aum.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
        </div>
        <div className="vault-stat-card">
          <div className="vault-stat-label">Share Price</div>
          <div className="vault-stat-value">${sharePrice.toFixed(6)}</div>
        </div>
        <div className="vault-stat-card">
          <div className="vault-stat-label">Tier</div>
          <div className="vault-stat-value">{tier === 1 ? 'Paper-grounded' : 'Community'}</div>
        </div>
        <div className="vault-stat-card">
          <div className="vault-stat-label">Creator</div>
          <div className="vault-stat-value"><code>{(() => {
            const creator = onChainData?.creator || detail?.creator || ''
            try {
              const connected = getAddress()
              if (connected && creator.toLowerCase() === connected.toLowerCase()) return 'Created by you'
            } catch { /* getAddress() may throw if wallet hook isn't ready — fall through to hex */ }
            return shortAddr(creator)
          })()}</code></div>
        </div>
      </div>

      {/* Performance Tracker / Sharpe Drift */}
      {health?.sharpe_drift && (
        <div className="vault-section">
          <SharpeDriftBadge drift={health.sharpe_drift} />
        </div>
      )}

      {/* Holdings */}
      {detail?.holdings && detail.holdings.length > 0 && (
        <div className="vault-section">
          <h3>Holdings</h3>
          <div className="vault-holdings-list">
            {detail.holdings.map((h, i) => (
              <div key={i} className="vault-holding-row">
                <span className="vault-holding-symbol">{h.symbol}</span>
                <span className="vault-holding-amount">{h.amount?.toFixed(6) ?? '—'}</span>
                <span className="vault-holding-value">${h.value_usdc?.toFixed(2) ?? '—'}</span>
                <span className="vault-holding-weight">{h.weight_pct?.toFixed(1) ?? '—'}%</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Associated Strategies */}

      {/* Target Allocations (from on-chain) */}
      {onChainData?.targetAllocations && onChainData.targetAllocations.length > 0 && (
        <div className="vault-section">
          <h3>Target Allocations</h3>
          <div style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', height: 24, borderRadius: 6, overflow: 'hidden', background: 'var(--surface-3)' }}>
              {onChainData.targetAllocations.map((a, i) => {
                const colors = {
                  'USDC': '#22C55E', 'sTSLA': '#3B82F6', 'sNVDA': '#8B5CF6', 'sSPY': '#6366F1',
                  'sBTC': '#F97316', 'sGOLD': '#D4A853', 'sOIL': '#92400E', 'sNKY': '#EC4899',
                }
                return (
                  <div key={i} style={{
                    width: `${a.weightBps / 100}%`,
                    background: colors[a.symbol] || '#6B7280',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: '0.65rem', fontWeight: 600, color: '#fff',
                    minWidth: a.weightBps > 500 ? undefined : 0,
                  }} title={`${a.symbol}: ${a.weightBps / 100}%`}>
                    {a.weightBps >= 500 ? a.symbol.replace('s', '') : ''}
                  </div>
                )
              })}
            </div>
          </div>
          <div className="vault-holdings-list">
            {onChainData.targetAllocations.map((a, i) => (
              <div key={i} className="vault-holding-row">
                <span className="vault-holding-symbol">{a.symbol}</span>
                <span className="vault-holding-weight">{(a.weightBps / 100).toFixed(1)}%</span>
                <code className="caption">{a.token.slice(0, 10)}...{a.token.slice(-4)}</code>
              </div>
            ))}
          </div>
        </div>
      )}

      {detail?.strategy_ids && detail.strategy_ids.length > 0 && (
        <div className="vault-section">
          <h3>Associated Strategies</h3>
          <div className="vault-holdings-list">
            {detail.strategy_ids.map((sid, i) => (
              <div key={i} className="vault-holding-row">
                <span className="vault-holding-symbol">{(detail.strategy_names?.[i]) || sid.slice(0, 12)}…</span>
                <span className="badge tier-1">paper-grounded</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Recent Traces */}
      {detail?.recent_traces && detail.recent_traces.length > 0 && (
        <div className="vault-section">
          <h3>Recent Reasoning Traces</h3>
          <div className="vault-traces-list">
            {detail.recent_traces.map((t, i) => (
              <div key={i} className="vault-trace-row">
                <span className="vault-trace-type">{t.decision_type}</span>
                {t.arc_tx_hash ? (
                  <a
                    href={`https://testnet.arcscan.app/tx/${t.arc_tx_hash}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="vault-trace-hash mono underline decoration-dotted underline-offset-2 hover:text-[var(--accent)] transition-colors"
                  >
                    {t.arc_tx_hash.slice(0, 16)}… ↗
                  </a>
                ) : (
                  <code className="vault-trace-hash">{t.trace_hash?.slice(0, 16)}…</code>
                )}
                <span className="vault-trace-time">{timeAgo(t.timestamp)}</span>
                {t.is_verified && (
                  <span
                    className={t.verification_mode === 'anchored_only' ? 'vault-trace-hash i-lucide-anchor' : 'vault-trace-verified i-lucide-check'}
                    style={{width:12,height:12}}
                    title={t.verification_mode === 'anchored_only'
                      ? 'Anchored on-chain; no off-chain body to compare, so no hashes were checked.'
                      : 'On-chain hash confirmed.'}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Deposit */}
      <div className="vault-section">
        <h3>Deposit</h3>
        <div className="vault-deposit-row">
          <input
            type="number"
            value={depositAmt}
            onChange={e => setDepositAmt(e.target.value)}
            placeholder="USDC amount"
            className="vault-deposit-input"
          />
          <button
            className="btn btn-primary"
            onClick={deposit}
            disabled={busy || !wallet}
          >
            {busy ? 'Waiting…' : 'Deposit'}
          </button>
        </div>
      </div>

      {/* Withdraw (Issue #466 — non-custodial redeem) */}
      <div className="vault-section">
        <h3>Withdraw</h3>
        <div className="vault-deposit-row">
          <input
            type="number"
            value={withdrawShares}
            onChange={e => setWithdrawShares(e.target.value)}
            placeholder="Shares to redeem"
            className="vault-deposit-input"
          />
          <button
            className="btn"
            onClick={setMaxShares}
            disabled={busy || !wallet || shareBalance == null || shareBalance === 0n}
            title="Redeem your full share balance"
          >
            Max
          </button>
          <button
            className="btn btn-primary"
            onClick={withdraw}
            disabled={busy || !wallet || !withdrawShares}
          >
            {busy ? 'Waiting…' : 'Withdraw'}
          </button>
        </div>
        {/* Share balance + previewRedeem confirmation — the user sees the exact USDC
            they'll receive before signing. */}
        <div className="caption" style={{ marginTop: 8, color: 'var(--text-3)' }}>
          {shareBalance != null && (
            <span>
              Your shares: <span className="mono">{formatUnits(shareBalance, SHARE_DECIMALS)}</span>
            </span>
          )}
          {previewOut != null && (
            <span style={{ marginLeft: shareBalance != null ? 12 : 0 }}>
              You receive ≈{' '}
              <span className="mono" style={{ color: 'var(--text-1)' }}>
                ${Number(formatUnits(previewOut, USDC_DECIMALS)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 6 })} USDC
              </span>
            </span>
          )}
        </div>
      </div>

      {/* Shared deposit/withdraw status (last action's result + TX link) */}
      {status && (
        <div className="vault-section">
          <div className="status-msg">{status}</div>
        </div>
      )}

    </div>
  )
}
