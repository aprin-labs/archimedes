import { useState, useEffect } from 'react'
import { apiGet } from '../api'
import { MIN_LEVEL, MAX_LEVEL, BADGE_LEVEL } from '../hooks/useRigorStrictness'

// A 1–5 slider for the user's rigor strictness. 1 = Conservative (the Archimedes
// Verified badge bar), 5 = Speculative (riskiest). Higher = the user accepts
// statistically weaker strategies into THEIR OWN vaults — it never lowers the
// global badge.
//
// The ladder (labels, per-level thresholds, always-on floors) is fetched from
// /api/selection-bias/strictness-ladder — the backend's single source of truth.
// A static fallback keeps the control usable if that call fails.

const FALLBACK_LADDER = {
  levels: [
    { level: 1, label: 'Conservative', dsr_p_min: 0.95, pbo_max: 0.5, oos_is_ratio_min: 0.5 },
    { level: 2, label: 'Balanced', dsr_p_min: 0.8, pbo_max: 0.55, oos_is_ratio_min: 0.45 },
    { level: 3, label: 'Moderate', dsr_p_min: 0.7, pbo_max: 0.6, oos_is_ratio_min: 0.4 },
    { level: 4, label: 'Aggressive', dsr_p_min: 0.6, pbo_max: 0.65, oos_is_ratio_min: 0.35 },
    { level: 5, label: 'Speculative', dsr_p_min: 0.5, pbo_max: 0.7, oos_is_ratio_min: 0.3 },
  ],
  badge_level: 1,
  floors: { dsr_p_floor: 0.5, oos_abs_floor: 0.0 },
}

// Colour ramp: green at the Verified bar → amber → red as risk rises.
const LEVEL_COLOR = {
  1: 'var(--positive, #10B981)',
  2: '#84cc16',
  3: '#f59e0b',
  4: '#f97316',
  5: 'var(--negative, #ef4444)',
}

// `levelLabel` now lives in ../rigorLevels.js (#1645) so a caller that only
// needs the LABEL does not have to import the module that defines the control.
// Re-exported here so existing importers (StrategyPassport.jsx) are unchanged.
export { levelLabel } from '../rigorLevels'

export default function RigorStrictnessControl({ level, onChange }) {
  const [ladder, setLadder] = useState(FALLBACK_LADDER)

  useEffect(() => {
    let cancelled = false
    apiGet('/api/selection-bias/strictness-ladder')
      .then((data) => {
        if (!cancelled && data?.levels?.length) setLadder(data)
      })
      .catch(() => {
        /* keep the static fallback — the control must never hard-fail */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const badgeLevel = ladder.badge_level ?? BADGE_LEVEL
  const current = (ladder.levels || []).find((l) => l.level === level) || ladder.levels?.[0]
  const aboveBadge = level > badgeLevel
  const color = LEVEL_COLOR[level] || 'var(--accent)'
  const floors = ladder.floors || FALLBACK_LADDER.floors

  return (
    <div className="card p-5">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-1">
        <div className="label">Rigor strictness</div>
        <span className="tag" style={{ background: 'transparent', border: `1px solid ${color}`, color }}>
          {level} · {current?.label}
        </span>
      </div>
      <p className="caption leading-relaxed mb-3 text-[var(--text-3)]">
        How strict the rigor gate is for <em>your</em> deploys. Higher = more strategies become
        deployable, at lower statistical confidence. This is your personal choice — it never
        changes the global{' '}
        <span className="tag tag-positive" style={{ fontSize: '0.68rem' }}>
          Archimedes Verified
        </span>{' '}
        badge, which always stays at the strictest level.
      </p>

      <input
        type="range"
        min={MIN_LEVEL}
        max={MAX_LEVEL}
        step={1}
        value={level}
        onChange={(e) => onChange(parseInt(e.target.value, 10))}
        aria-label="Rigor strictness level"
        // The whole meaning of this control is the named rung, not the ordinal:
        // without aria-valuetext a screen-reader user dragging 1 → 4 hears
        // "4 of 5" and is never told they have moved below the Verified bar.
        aria-valuetext={`${level} — ${current?.label ?? `Level ${level}`}`}
        style={{ width: '100%', accentColor: color, cursor: 'pointer' }}
      />
      <div className="flex justify-between mt-1" style={{ fontSize: '0.68rem', color: 'var(--text-4)' }}>
        {(ladder.levels || []).map((l) => (
          <button
            key={l.level}
            type="button"
            onClick={() => onChange(l.level)}
            aria-pressed={l.level === level}
            style={{
              background: 'none',
              border: 'none',
              padding: 0,
              font: 'inherit',
              cursor: 'pointer',
              fontWeight: l.level === level ? 700 : 400,
              color: l.level === level ? color : 'var(--text-4)',
            }}
            title={l.description || l.label}
          >
            {l.label}
          </button>
        ))}
      </div>

      {current && (
        <div
          className="grid grid-cols-3 gap-3 mt-4 pt-3"
          style={{ borderTop: '1px solid var(--glass-border)' }}
        >
          <Threshold label="DSR confidence" value={`≥ ${current.dsr_p_min.toFixed(2)}`} />
          <Threshold label="PBO" value={`< ${current.pbo_max.toFixed(2)}`} />
          <Threshold label="OOS / IS Sharpe" value={`≥ ${current.oos_is_ratio_min.toFixed(2)}`} />
        </div>
      )}

      {aboveBadge && (
        <div
          className="info-box warning mt-4"
          role="status"
          style={{ fontSize: '0.82rem' }}
        >
          <strong>You're below the Archimedes Verified bar.</strong> At{' '}
          <strong>{current?.label}</strong> you accept strategies whose edge is less statistically
          certain than the Conservative standard. Size positions accordingly.
        </div>
      )}

      <p className="caption mt-3 text-[var(--text-4)] leading-relaxed">
        <strong>Always enforced, at every level:</strong> the look-ahead audit must pass, the
        out-of-sample Sharpe must be positive, and the deflated Sharpe confidence must be ≥{' '}
        {(floors.dsr_p_floor ?? 0.5).toFixed(2)}. You can trade statistical confidence for breadth —
        you can never fully bypass the gate.
      </p>
    </div>
  )
}

function Threshold({ label, value }) {
  return (
    <div>
      <div className="caption text-[var(--text-4)]">{label}</div>
      <div className="body font-semibold tabular-nums">{value}</div>
    </div>
  )
}
