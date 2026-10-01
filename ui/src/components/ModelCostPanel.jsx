import { useEffect, useMemo, useState } from 'react'
import { fetchHealth } from '../health'
import pricing from '../data/modelPricing.json'

// Cost-comparison + model picker for the Generate page. Surfaces the Bedrock
// model cost landscape (us-east-1 on-demand, $/1M tokens), highlights the model
// the backend is actually using right now (from /health → llm_model), and lets
// the user PICK a model for the next generation.
//
// SAFETY (critical): only FREE/default-tier models (works_now:true) are
// selectable. Premium rows (Claude Haiku/Sonnet — works_now:false) stay
// display-only with a "Premium model — activating soon (pending Bedrock approval)" affordance; selecting
// them is a no-op until the paid-tier 402 entitlement (#723) lands. The server
// re-enforces this allowlist, so the UI restriction is defense-in-depth.
//
// `selectedModel` / `onSelectModel` lift the choice into the Generate flow. The
// data is a snapshot (ui/src/data/modelPricing.json); the active row is live.
//
// COLLAPSED HEADER copy: it leads with what the engine IS (a debate society
// over the research corpus — agents/debate_engine.py, the sole generation
// pipeline) and only then quotes the per-token rate, labelled as the
// provider's list price. The old header led with "Running <model> · $x in /
// $y out per 1M tokens", which is true but reads as a bill on a page whose
// first job is to tell you what you are about to run. The served model is
// never a literal in this file — it comes from /health's `llm_model` and is
// matched against the snapshot, so the card cannot name a model the backend
// is not running. Guarded by ui/test/model-cost-line.test.js.

const blended = (m) => m.input * 0.75 + m.output * 0.25
const fmt = (x) => (x == null ? '—' : `$${x.toFixed(x < 1 ? 3 : 2)}`)

export default function ModelCostPanel({ selectedModel = null, onSelectModel }) {
  const [open, setOpen] = useState(false)
  const [activeModel, setActiveModel] = useState(null)

  useEffect(() => {
    let cancelled = false
    // Shared TTL-cached call (#1333 review) — Layout.jsx's footer pill and
    // Architecture.jsx also read /health; fetchHealth() (../health.js) lets
    // this reuse whichever of those just fetched it instead of firing a
    // third independent Arc RPC round-trip + DB reads.
    fetchHealth()
      .then((d) => { if (!cancelled) setActiveModel(d?.llm_model || null) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  const rows = useMemo(() => [...pricing.models].sort((a, b) => blended(a) - blended(b)), [])
  const active = rows.find((m) => m.model_id && m.model_id === activeModel)
  // The chosen model row (for the collapsed-header summary), if any.
  const chosen = rows.find((m) => m.model_id && m.model_id === selectedModel)
  // The row whose published rate the collapsed header quotes: the user's pick
  // if they made one, else the model /health reports as being served. Null
  // when neither is known (or the served id is not in the snapshot) — the
  // header then quotes no price at all rather than guessing one.
  const priced = chosen || active

  // Free-tier-only selection. Premium rows are never selectable here.
  const selectable = (m) => Boolean(m.works_now && m.model_id && onSelectModel)
  const handleSelect = (m) => {
    if (!selectable(m)) return
    // Toggle: re-clicking the chosen row clears the override (back to default).
    onSelectModel(m.model_id === selectedModel ? null : m.model_id)
  }

  return (
    <div className="card mb-4" style={{ padding: 0, overflow: 'hidden' }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        style={{
          display: 'flex', width: '100%', alignItems: 'center', justifyContent: 'space-between',
          gap: 12, padding: '12px 18px', background: 'transparent', border: 'none',
          cursor: 'pointer', textAlign: 'left', color: 'inherit',
        }}
      >
        <div>
          <div className="label" style={{ marginBottom: 2 }}>Strategy engine</div>
          {/* Lead line — WHAT you are running. The served model is read from
              /health (activeModel) or the user's own pick; it is never a
              literal here, so this line cannot name a model we are not
              running. */}
          <div className="caption" style={{ color: 'var(--text-3)' }}>
            Debate society over the research corpus
            {chosen ? (
              <>, next run served by <strong style={{ color: 'var(--text-1)' }}>{chosen.provider} {chosen.name}</strong></>
            ) : active ? (
              <>, served by <strong style={{ color: 'var(--text-1)' }}>{active.provider} {active.name}</strong></>
            ) : activeModel ? (
              <>, served by <strong style={{ color: 'var(--text-1)' }}>{activeModel}</strong></>
            ) : null}
          </div>
          {/* Secondary line — the rate, explicitly labelled as the provider's
              published list price so it does not read as your bill. */}
          <div className="caption" style={{ color: 'var(--text-3)', marginTop: 2 }}>
            {priced ? (
              <>Provider list price{' · '}{fmt(priced.input)} in / {fmt(priced.output)} out per 1M tokens</>
            ) : (
              <>Compare provider list prices across {rows.length} models on Bedrock</>
            )}
          </div>
        </div>
        <span
          className={`${open ? 'i-lucide-chevron-down' : 'i-lucide-chevron-right'} w-4 h-4`}
          style={{ color: 'var(--text-3)', flexShrink: 0 }}
        />
      </button>

      {open && (
        <div style={{ padding: '6px 18px 18px', borderTop: '1px solid var(--glass-border)' }}>
          <p className="caption mb-3" style={{ color: 'var(--text-3)' }}>
            {pricing.unit} · {pricing.region} · snapshot {pricing.generated}. Cheaper is usually
            less capable — pick for your budget. <strong style={{ color: 'var(--positive)' }}>✓</strong> = invokable
            now and selectable; <strong>premium</strong> models (Claude) are <em>activating soon — pending AWS Bedrock approval</em>.
          </p>
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
              <thead>
                <tr style={{ color: 'var(--text-3)', textAlign: 'left' }}>
                  <th style={{ padding: '6px 8px', fontWeight: 500 }}>Model</th>
                  <th style={{ padding: '6px 8px', fontWeight: 500, textAlign: 'right' }}>In $/1M</th>
                  <th style={{ padding: '6px 8px', fontWeight: 500, textAlign: 'right' }}>Out $/1M</th>
                  <th style={{ padding: '6px 8px', fontWeight: 500 }} />
                </tr>
              </thead>
              <tbody>
                {rows.map((m) => {
                  const isActive = m.model_id && m.model_id === activeModel
                  const isChosen = m.model_id && m.model_id === selectedModel
                  const canSelect = selectable(m)
                  return (
                    <tr
                      key={m.model_id || m.name}
                      onClick={() => handleSelect(m)}
                      role={canSelect ? 'button' : undefined}
                      aria-disabled={canSelect ? undefined : true}
                      aria-pressed={canSelect ? isChosen : undefined}
                      title={
                        canSelect
                          ? 'Use this model for the next generation'
                          : 'Premium model — activating soon (pending Bedrock approval)'
                      }
                      style={{
                        borderTop: '1px solid var(--glass-border)',
                        cursor: canSelect ? 'pointer' : 'not-allowed',
                        opacity: m.works_now ? 1 : 0.55,
                        background: isChosen
                          ? 'color-mix(in srgb, var(--accent) 18%, transparent)'
                          : isActive
                            ? 'color-mix(in srgb, var(--accent) 12%, transparent)'
                            : 'transparent',
                      }}
                    >
                      <td style={{ padding: '7px 8px', color: 'var(--text-1)' }}>
                        <span style={{ fontWeight: (isActive || isChosen) ? 600 : 400 }}>{m.name}</span>
                        <span className="caption" style={{ color: 'var(--text-3)', marginLeft: 6 }}>{m.provider}</span>
                      </td>
                      <td style={{ padding: '7px 8px', textAlign: 'right', color: 'var(--text-1)', fontVariantNumeric: 'tabular-nums' }}>{fmt(m.input)}</td>
                      <td style={{ padding: '7px 8px', textAlign: 'right', color: 'var(--text-1)', fontVariantNumeric: 'tabular-nums' }}>{fmt(m.output)}</td>
                      <td style={{ padding: '7px 8px', whiteSpace: 'nowrap' }}>
                        {isChosen && <span className="tag tag-accent" style={{ marginRight: 4 }}>selected</span>}
                        {isActive && !isChosen && <span className="tag tag-accent" style={{ marginRight: 4 }}>active</span>}
                        {m.recommended && !isActive && !isChosen && (
                          <span style={{ color: 'var(--accent)', marginRight: 4 }} title="Recommended cheap pick">★</span>
                        )}
                        {m.works_now ? (
                          <span style={{ color: 'var(--positive)' }} title="Invokable now">✓</span>
                        ) : (
                          <span className="caption" style={{ color: 'var(--text-3)' }} title="Premium model — activating soon (pending Bedrock approval)">
                            Premium · soon
                          </span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {chosen && (
            <p className="caption mt-3" style={{ color: 'var(--accent)' }}>
              Selected <strong>{chosen.provider} {chosen.name}</strong> for your next generation.{' '}
              <button
                type="button"
                onClick={() => onSelectModel?.(null)}
                style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', textDecoration: 'underline', padding: 0 }}
              >
                Use default
              </button>
            </p>
          )}
          <p className="caption mt-3" style={{ color: 'var(--text-3)' }}>{pricing.note}</p>
        </div>
      )}
    </div>
  )
}
