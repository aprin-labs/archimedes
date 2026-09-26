import { apiGet } from '../api'
import { useState, useEffect, useCallback } from 'react'
import {
  publicClient,
  TRACE_REGISTRY_ABI, NEW_CONTRACTS,
} from '../config'
import { regimeMeta } from '../regime'
import { anchorState, blockOrderCopy, referencedStrategyId, sourcePapersCopy, verificationTone } from '../trace-binding'



function formatAgo(secs) {
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

function timeAgo(ts) {
  if (!ts) return 'unknown'
  // Resolve to an epoch-ms value via both a date-string parse and a raw
  // numeric (Unix seconds) parse, then use whichever succeeds.
  const asDate = new Date(ts)
  const epochMs = !isNaN(asDate.getTime()) ? asDate.getTime() : Number(ts) * 1000
  // A missing/null timestamp from the backend often serializes as epoch 0
  // ("1970-01-01T00:00:00Z" or numeric 0) rather than an empty value — that
  // is not a real 56-years-ago event, it's an absent timestamp. Guard it
  // explicitly so it renders "unknown" instead of a nonsensical multi-decade
  // relative delta (observed live as "494774h ago").
  if (!Number.isFinite(epochMs) || epochMs <= 0) return 'unknown'
  const secs = Math.floor((Date.now() - epochMs) / 1000)
  if (secs < 0) return 'unknown'
  return formatAgo(secs)
}

function shortAddr(addr) {
  if (!addr) return '—'
  return `${addr.slice(0, 8)}…${addr.slice(-4)}`
}

function shortHash(hash) {
  if (!hash) return '—'
  return `${hash.slice(0, 12)}…${hash.slice(-6)}`
}

// ─── On-chain Traces Panel ───────────────────────────────────
// Reasoning is now the dedicated trace browser per page-roles-spec.md.
// Strategy detail (export, paper-claim delta, EfficientFrontier,
// CorrelationMatrix, RigorExplainer) lives on Library where the strategy
// itself does — open via ?highlight=<id> deep-link from any trace card.

function OnChainTraces({ onNavigate, highlightTraceId }) {
  const [traces, setTraces] = useState([])
  const [totalCount, setTotalCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [verifying, setVerifying] = useState({})
  const [verifyResults, setVerifyResults] = useState({})
  const [filter, setFilter] = useState('all') // 'all' | 'rebalance' | 'construction' | 'skip'

  const loadTraces = useCallback(async () => {
    setLoading(true)
    try {
      // Load from backend API (enriched with off-chain metadata)
      const data = await apiGet('/api/traces/?limit=50')
      setTraces(data.traces || [])
      setTotalCount(data.total || 0)
    } catch {
      // Fallback: read directly from on-chain
      const regAddr = NEW_CONTRACTS.traceRegistry
      if (!regAddr) return
      try {
        const count = await publicClient.readContract({
          address: regAddr, abi: TRACE_REGISTRY_ABI, functionName: 'traceCount'
        })
        setTotalCount(Number(count))
        const loaded = []
        const start = Math.max(1, Number(count) - 19)
        for (let i = Number(count); i >= start; i--) {
          try {
            const [agent, vault, traceHash, timestamp] = await publicClient.readContract({
              address: regAddr, abi: TRACE_REGISTRY_ABI, functionName: 'getTraceById', args: [BigInt(i)]
            })
            loaded.push({ id: i, agent, vault, trace_hash: traceHash, timestamp: Number(timestamp), decision_type: 'on-chain' })
          } catch {
            // Individual trace read failure — skip; the loop keeps going so a single bad row doesn't break the list.
          }
        }
        setTraces(loaded)
      } catch {
        // Initial registry-count read failure — leave traces empty; the user sees the "no traces" empty state.
      }
    }
    setLoading(false)
  }, [])

  useEffect(() => { loadTraces() }, [loadTraces])

  // Scroll + highlight the trace specified by ?trace_id=<id>
  useEffect(() => {
    if (!highlightTraceId || traces.length === 0) return
    // Small delay to ensure DOM is painted
    const timer = setTimeout(() => {
      const el = document.getElementById(`trace-${highlightTraceId}`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        el.classList.add('trace-highlighted')
        setTimeout(() => el.classList.remove('trace-highlighted'), 3000)
      }
    }, 150)
    return () => clearTimeout(timer)
  }, [highlightTraceId, traces])

  const verifyTrace = async (traceId) => {
    setVerifying(prev => ({ ...prev, [traceId]: true }))
    try {
      const data = await apiGet(`/api/traces/${encodeURIComponent(traceId)}/verify`)
      setVerifyResults(prev => ({
        ...prev,
        [traceId]: data,
      }))
    } catch (err) {
      setVerifyResults(prev => ({
        ...prev,
        [traceId]: { is_verified: false, details: err.message },
      }))
    }
    setVerifying(prev => ({ ...prev, [traceId]: false }))
  }

  return (
    <div>
      <div className="label mb-3">Reasoning Trace Registry ({totalCount} total)</div>
      <p className="caption mb-4 max-w-[640px] leading-relaxed">
        Every trace below is a real agent decision: an autonomous rebalance, a
        regime change, or a strategy construction from the Generate page. The hash
        is computed deterministically off-chain and anchored on Arc via the
        <code style={{ marginLeft: 4 }}>ReasoningTraceRegistry</code> contract.
        Click <strong>Verify hash on-chain</strong> on any trace to re-fetch the
        on-chain receipt and confirm the stored hash matches — traces anchored
        without an off-chain body to compare against report{' '}
        <strong>anchored, not re-hashed</strong> instead of a false match; click{' '}
        <strong>→ Strategy passport</strong> — on the trading decisions that
        name one — to jump to that strategy, where its generation debate and its
        other trading decisions live. A construction trace cites papers rather
        than a strategy, so it offers no passport link. Skips
        read <strong>not anchored (no trade to bind)</strong>: with no trade
        there is nothing for an on-chain commitment to bind, so no anchor is
        attempted — that is a permanent, explained absence, not a pending write.
      </p>

      {/* Filter chips — hide types with zero traces (Issue #338 item 3) */}
      <div className="flex gap-2 flex-wrap mb-3">
        {['all', 'rebalance', 'construction', 'skip']
          .filter(f => f === 'all' || traces.some(t => t.decision_type === f))
          .map(f => (
          <button
            key={f}
            className={`tag cursor-pointer ${filter === f ? 'tag-accent' : 'tag-muted'}`}
            onClick={() => setFilter(f)}
            style={{ border: 'none', padding: '4px 12px' }}
          >
            {f === 'all'
              ? 'All'
              : f === 'rebalance'
                ? <><span className="i-lucide-check-circle-2 w-3.5 h-3.5" /> Rebalances</>
                : f === 'construction'
                  ? <><span className="i-lucide-landmark w-3.5 h-3.5" /> Constructions</>
                  : <><span className="i-lucide-skip-forward w-3.5 h-3.5" /> Skips</>}
          </button>
        ))}
      </div>

      {/* Trace list */}
      {loading ? (
        <div className="caption" role="status">Loading traces…</div>
      ) : traces.length === 0 ? (
        <div className="card" style={{ padding: 18 }}>
          <p className="body" style={{ marginBottom: 6 }}>No reasoning traces yet.</p>
          <p className="caption">
            Traces accumulate when the autonomous agent rebalances vaults, or when you
            use the <a href="/app/generate" style={{ color: 'var(--accent)' }}>Generate</a> page to construct a portfolio (each
            construction emits a trace). If the page stays empty after generating, the
            agent runner may not be running locally — check <code>docker compose logs oracle</code>.
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2.5">
          {traces
          .sort((a, b) => {
            // Most recent first
            const ta = a.timestamp ? new Date(typeof a.timestamp === 'number' && a.timestamp > 1e12 ? a.timestamp : a.timestamp * 1000).getTime() : 0
            const tb = b.timestamp ? new Date(typeof b.timestamp === 'number' && b.timestamp > 1e12 ? b.timestamp : b.timestamp * 1000).getTime() : 0
            return tb - ta
          })
          .filter(t => filter === 'all' || t.decision_type === filter)
          .map((t, i) => {
            const vResult = verifyResults[t.id]
            // Skip-type traces are honest noise — when AMM pools are dry,
            // the agent legitimately emits one per cycle. Render them
            // compactly so a real rebalance isn't buried.
            const isSkip = t.decision_type === 'skip'
            return (
              <div key={i} id={`trace-${t.id}`} className="card" style={{ padding: isSkip ? 10 : 14 }}>
                <div className="flex justify-between items-start mb-2">
                  <div className="flex gap-2 items-center flex-wrap">
                    <span style={{ fontWeight: 700, color: 'var(--accent)' }}>#{typeof t.id === 'string' ? t.id.slice(0, 8) : t.id}</span>
                    <span className={`tag ${t.decision_type === 'rebalance' ? 'tag-positive' : t.decision_type === 'construction' ? 'tag-accent' : t.decision_type === 'skip' ? 'tag-warning' : 'tag-muted'}`}>
                      {t.decision_type}
                    </span>
                    {/* One shared derivation with the Portfolio feed and the
                        passport panel (src/trace-binding.js). "verified" is
                        reserved for a real hash comparison — the on-chain-only
                        path (verification_mode === 'anchored_only') confirmed an
                        anchor exists and compared nothing, so it must not borrow
                        the same word or the same green check (#1407). This row
                        previously rendered NOTHING at all for an unanchored
                        trace, so a skip — which by design never anchors, having
                        no trade for commit() to bind (#714) — silently looked
                        identical to a trace whose anchor was still in flight. */}
                    {(() => {
                      const a = anchorState(t)
                      return (
                        <span
                          className={`flex items-center gap-1 text-xs ${a.tone === 'verified' ? 'text-[var(--positive)]' : 'text-[var(--text-3)]'}`}
                          title={a.title}
                          data-anchor-state={a.state}
                        >
                          <span className={`${a.icon} w-3 h-3`} /> {a.label}
                        </span>
                      )
                    })()}
                  </div>
                  <div className="caption">{timeAgo(t.timestamp)}</div>
                </div>

                {/* Skip traces: render only a one-line summary; everything
                    else lives in an inline "Details" disclosure so the
                    page doesn't become a wall of identical skip cards. */}
                {isSkip ? (
                  <details>
                    <summary className="caption cursor-pointer select-none" style={{ color: 'var(--text-3)', lineHeight: 1.5 }}>
                      {(t.trigger || 'skip')} — {(t.reasoning || '').slice(0, 100)}{(t.reasoning || '').length > 100 ? '…' : ''}
                      <span style={{ color: 'var(--text-4)', marginLeft: 6 }}>(click for details)</span>
                    </summary>
                    <div className="mt-2 grid grid-cols-2 gap-2">
                      <div>
                        <div className="caption">Vault</div>
                        <code style={{ fontSize: '0.75rem' }}>{shortAddr(t.vault_address || t.vault)}</code>
                      </div>
                      <div>
                        <div className="caption">Trace Hash</div>
                        <code style={{ fontSize: '0.75rem' }}>{shortHash(t.trace_hash || t.traceHash)}</code>
                      </div>
                    </div>
                    {t.reasoning && (
                      <p className="body mt-2" style={{ fontSize: '0.85rem', lineHeight: 1.4 }}>{t.reasoning}</p>
                    )}
                  </details>
                ) : (
                  <>
                    <div className="grid grid-cols-2 gap-2 mb-2">
                      <div>
                        <div className="caption">Vault</div>
                        <code style={{ fontSize: '0.75rem' }}>{shortAddr(t.vault_address || t.vault)}</code>
                      </div>
                      <div>
                        <div className="caption">Trace Hash</div>
                        <code style={{ fontSize: '0.75rem' }}>{shortHash(t.trace_hash || t.traceHash)}</code>
                      </div>
                    </div>

                    {t.reasoning && (
                      <div className="mb-2">
                        <div className="caption">Reasoning</div>
                        <p className="body" style={{ fontSize: '0.85rem', lineHeight: 1.4 }}>{t.reasoning.slice(0, 200)}{t.reasoning.length > 200 ? '…' : ''}</p>
                      </div>
                    )}

                    {t.regime_at_decision && (
                      <div style={{ marginBottom: 8 }}>
                        <span className="caption">Regime: </span>
                        <span className={`tag ${regimeMeta(t.regime_at_decision).tag}`}
                          title={regimeMeta(t.regime_at_decision).definition}>
                          {regimeMeta(t.regime_at_decision).label}
                        </span>
                      </div>
                    )}

                    {t.confidence > 0 && (
                      <div className="mb-2">
                        <div className="caption">Confidence: {(t.confidence * 100).toFixed(0)}%</div>
                        <div className="rounded h-1 w-full" style={{ background: 'var(--surface-2)' }}>
                          <div className="rounded h-1 bg-[var(--accent)]" style={{ width: `${t.confidence * 100}%` }} />
                        </div>
                      </div>
                    )}
                  </>
                )}

                {/* On-chain link — shown upfront whenever the trace has an
                    arc_tx_hash, regardless of whether the user has clicked
                    Verify yet. The trace already knows its tx + blocks from
                    the API response; we don't need a verify roundtrip to
                    expose the arcscan link. The Verify button below still
                    re-fetches the on-chain receipt and confirms hash match,
                    but the link doesn't gate on it.
                    Skip-type traces hide all of this in their <details>
                    disclosure above — these blocks render only for
                    rebalance/construction. */}
                {!isSkip && t.arc_tx_hash && (
                  <div className="flex items-center gap-3 flex-wrap text-xs text-[var(--text-3)] mb-2">
                    <span className="flex items-center gap-1">
                      <span className="i-lucide-file-text w-3 h-3" />
                      Tx: <a
                        href={`https://testnet.arcscan.app/tx/${t.arc_tx_hash}`}
                        target="_blank"
                        rel="noreferrer"
                        className="mono underline decoration-dotted underline-offset-2 hover:text-[var(--accent)] transition-colors"
                      >
                        {shortHash(t.arc_tx_hash)}
                      </a>
                      <span className="i-lucide-external-link w-2.5 h-2.5" />
                    </span>
                    {t.commit_block_number != null && (
                      <span className="flex items-center gap-1">
                        <span className="i-lucide-box w-3 h-3" />
                        Block #{t.commit_block_number.toLocaleString()}
                      </span>
                    )}
                  </div>
                )}
                {/* The unanchored case used to be stated twice on this card and
                    only for non-skip rows. The badge above now carries it for
                    every row, with the state named honestly — including the
                    skip case this block excluded, which is exactly the row a
                    reader is most likely to misread as a failure. */}

                {/* Verify button + strategy back-link — only for non-skip
                    traces. Skip rows already collapsed their detail above. */}
                {!isSkip && (() => {
                  // vTone: 'verified' (hash re-fetched and matched) |
                  // 'anchored' (reachable store, no off-chain body — the
                  // on-chain anchor exists but ZERO hashes were compared) |
                  // 'failed' (mismatch, missing receipt, network error, or
                  // no vResult yet). 'anchored' is deliberately never given
                  // the same icon/class as 'verified' — a hash that was
                  // never compared does not get the affordance of one that
                  // matched (#1359 anti-goal).
                  const vTone = verificationTone(vResult?.verification_mode)
                  return (
                <div className="flex gap-2 items-center flex-wrap">
                  <button
                    className="btn btn-outline btn-sm flex items-center gap-1.5"
                    onClick={() => verifyTrace(t.id)}
                    disabled={verifying[t.id]}
                    title={
                      // verification_mode: "anchored_only" means the store had
                      // no off-chain body to compare — zero hashes were
                      // checked, so the tooltip says so instead of implying a
                      // real re-hash happened.
                      vTone === 'anchored'
                        ? 'Anchored on-chain, but no off-chain trace body was stored to re-hash against (verification_mode: anchored_only)'
                        : 'Re-fetch the on-chain receipt and confirm the trace hash matches'
                    }
                  >
                    {verifying[t.id] ? (
                      'Verifying…'
                    ) : vResult && vTone === 'verified' ? (
                      <><span className="i-lucide-check w-3.5 h-3.5 positive" /> Hash verified</>
                    ) : vResult && vTone === 'anchored' ? (
                      <><span className="i-lucide-anchor w-3.5 h-3.5" /> Anchored — not re-hashed</>
                    ) : (
                      <><span className="i-lucide-search w-3.5 h-3.5" /> Verify hash on-chain</>
                    )}
                  </button>
                  {/* `t.strategy_id` does not exist on TraceResponse and never
                      has — the API emits `strategies_referenced`, the list of
                      strategies the decision actually consulted. This button
                      was therefore dead on every row, which is why the copy at
                      the top of the page promising a follow-back to "the source
                      strategy and its full passport" was unreachable.

                      `referencedStrategyId` rather than
                      `strategies_referenced[0]`: on a CONSTRUCTION trace that
                      first entry is an arXiv id or a paper anchor, not a
                      strategy id (see the helper), so linking it would send the
                      reader to a passport that does not exist. Same rule the
                      backend's ?strategy_id= filter uses, so a row that offers
                      this button is exactly a row that strategy's passport
                      lists back. */}
                  {referencedStrategyId(t) && onNavigate && (
                    <button
                      className="btn btn-outline btn-sm"
                      onClick={() => onNavigate('strategy', { strategyId: referencedStrategyId(t) })}
                      title="Open this trace's strategy passport"
                    >
                      → Strategy passport
                    </button>
                  )}
                  {vResult && (
                    <span className={`caption flex items-center gap-1 ${vTone === 'verified' ? 'positive' : vTone === 'failed' ? 'negative' : ''}`}>
                      <span className={vTone === 'verified' ? 'i-lucide-check w-3 h-3' : vTone === 'anchored' ? 'i-lucide-anchor w-3 h-3' : 'i-lucide-x w-3 h-3'} />
                      {vResult.details}
                    </span>
                  )}

                  {/* The SECOND, independent check /verify now runs (#1637):
                      do the papers this decision cites exist in the corpus?
                      Rendered as its own line, never merged into the hash
                      badge above — "anchored" and "the cited research is
                      there" are different facts and a reader needs both.

                      Copy lives in src/trace-binding.js:sourcePapersCopy and
                      says "exists in the corpus", not "verified": the corpus
                      stores no per-paper content hash yet (#1091), so the
                      backend checks existence and compares nothing. The
                      tri-state `mode` is surfaced in the tooltip rather than
                      only returned (owner decision Q8 on #1688). */}
                  {vResult && vResult.papers_verified !== undefined && (() => {
                    const papers = sourcePapersCopy(vResult)
                    return (
                      <span
                        className={`caption flex items-center gap-1 w-full ${papers.tone === 'verified' ? 'positive' : papers.tone === 'failed' ? 'negative' : ''}`}
                        title={papers.detail}
                      >
                        <span
                          // Icons reused from this file's existing set (and
                          // the uno safelist) rather than new lucide names: an
                          // icon class that does not resolve renders as an
                          // invisible span, which would silently drop the
                          // whole line.
                          className={
                            papers.tone === 'verified'
                              ? 'i-lucide-check w-3 h-3'
                              : papers.tone === 'failed'
                                ? 'i-lucide-x w-3 h-3'
                                : 'i-lucide-file-text w-3 h-3'
                          }
                        />
                        {papers.label}
                      </span>
                    )
                  })()}

                  {/* Why does this matter? disclosure — always available,
                      not gated behind clicking Verify. */}
                  <details className="mt-1.5 w-full">
                    <summary className="caption text-[var(--text-4)] cursor-pointer hover:text-[var(--text-2)] transition-colors select-none">
                      Why does this matter?
                    </summary>
                    <div className="caption text-[var(--text-3)] mt-1.5 max-w-[480px] leading-relaxed">
                      The hash is computed deterministically from the agent's reasoning, allocations, and regime context. By anchoring it on Arc's <code>ReasoningTraceRegistry</code>, anyone can independently fetch the canonical trace bytes (<code>{'GET /api/traces/{id}/canonical'}</code>), hash them, and confirm the agent's decision existed at the recorded block — proving the reasoning preceded the trade, not the other way around.
                    </div>
                  </details>
                </div>
                  )
                })()}

                {/* Block-order panel — the COPY keys off temporal_binding_source,
                    the field TraceResponse's claim-integrity validator
                    (schemas.py) guarantees can never carry a True binding
                    without the real on-chain commit()/reveal()/executeTrade()
                    path having run. Previously this keyed off
                    temporal_binding_valid alone and always rendered
                    "(off-chain)" / roadmap-disclaimer copy — wrong 100% of
                    the times a user could see it,
                    because that copy only rendered when valid was truthy,
                    and truthy requires source === "chain" (#1359). See
                    docs/specs/commit-reveal-trace-spec.md (v1.5) and
                    src/trace-binding.js for the pinned copy.

                    The AFFORDANCE (icon/background) is a separate decision
                    from the copy: source === "chain" alone is not enough,
                    because temporal_binding_valid can legitimately be False
                    for a chain-sourced trace — a minted commit whose reveal
                    never landed (dangling commit; see agent_runner.py
                    _reconcile_failure and the #1275 honest-degradation
                    contract). That state must keep rendering as unresolved
                    (red x-circle + caveat line), never as a false green
                    pass, even though the heading text still correctly says
                    the commit step is contract-enforced. */}
                {!isSkip && t.temporal_binding_valid != null && (() => {
                  const copy = blockOrderCopy({ source: t.temporal_binding_source, valid: t.temporal_binding_valid })
                  const isChainEnforced = copy.tone === 'verified' && t.temporal_binding_valid === true
                  const isDanglingReveal = t.temporal_binding_source === 'chain' && t.temporal_binding_valid === false
                  return (
                    <div className="mt-2 rounded-md px-3 py-2" style={{ background: isChainEnforced ? 'rgba(34,197,94,0.1)' : isDanglingReveal ? 'rgba(239,68,68,0.1)' : 'rgba(148,163,184,0.12)' }}>
                      <div className="flex items-center gap-1.5 mb-1">
                        <span className={`w-4 h-4 flex-shrink-0 ${isChainEnforced ? 'i-lucide-check-circle text-[var(--positive)]' : isDanglingReveal ? 'i-lucide-x-circle text-[var(--negative)]' : 'i-lucide-info text-[var(--text-3)]'}`} />
                        <strong className="text-[0.85rem]">{copy.heading}</strong>
                      </div>
                      <div className="text-xs text-[var(--text-3)] leading-relaxed">
                        {t.commit_block_number != null && <div>Commit block: <strong>#{t.commit_block_number}</strong></div>}
                        {t.trade_block_number != null && <div>Trade block: <strong>#{t.trade_block_number}</strong></div>}
                        {t.reveal_block_number != null && <div>Reveal block: <strong>#{t.reveal_block_number}</strong></div>}
                        {isDanglingReveal && (
                          <div className="negative" style={{ marginTop: 4 }}>
                            Commit is contract-enforced, but this trace's commit → trade → reveal blocks are
                            incomplete or out of order — binding unproven.
                          </div>
                        )}
                        <div style={{ marginTop: 4, fontStyle: 'italic' }}>{copy.note}</div>
                      </div>
                    </div>
                  )
                })()}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ─── Main Export ─────────────────────────────────────────────

export default function Reasoning({ onNavigate }) {
  // Read ?trace_id= from URL for deep-link navigation from Portfolio
  const highlightTraceId = typeof window !== 'undefined'
    ? new URLSearchParams(window.location.search).get('trace_id')
    : null

  return (
    <div className="reasoning-page">
      <header className="app-page-heading">
        <p className="app-eyebrow">Trace and verification</p>
        <h1>Reasoning</h1>
        <p>
          Every autonomous agent decision is hashed, and every decision that
          traded is anchored on-chain by that hash. Browse the trace timeline
          below, verify any hash against the on-chain registry, and follow a
          trading decision back to the passport of the strategy it consulted.
        </p>
      </header>
      <OnChainTraces onNavigate={onNavigate} highlightTraceId={highlightTraceId} />
    </div>
  )
}
