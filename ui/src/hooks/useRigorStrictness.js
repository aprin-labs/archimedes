import { useState, useEffect, useCallback } from 'react'

import { canStore } from '../storage-consent.js'

// Per-user rigor strictness (1 = Conservative/badge … 5 = Speculative). This is
// a personal DEPLOY preference — it decides what YOU can deploy and how the
// passport reads for you. It never changes the global "Archimedes Verified 🏆"
// badge, which is always evaluated at the strictest level (backend rigor_profiles).
//
// Persisted in localStorage and mirrored across every mounted component in the
// tab (Library slider ↔ Passport slider stay in sync) via a module-level
// listener set, so we don't have to prop-drill the level through the app shell.

const STORAGE_KEY = 'archimedes.rigorStrictness'
export const MIN_LEVEL = 1
export const MAX_LEVEL = 5
export const BADGE_LEVEL = 1 // the Archimedes Verified bar (strictest)

function readInitial() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const n = raw != null ? parseInt(raw, 10) : NaN
    if (Number.isFinite(n) && n >= MIN_LEVEL && n <= MAX_LEVEL) return n
  } catch {
    /* localStorage unavailable (private mode / SSR) — fall through */
  }
  return BADGE_LEVEL // fail-safe: default to the strictest level, never the loosest
}

const listeners = new Set()

export function useRigorStrictness() {
  const [level, setLevelState] = useState(readInitial)

  useEffect(() => {
    const fn = (v) => setLevelState(v)
    listeners.add(fn)
    return () => listeners.delete(fn)
  }, [])

  const setLevel = useCallback((v) => {
    const parsed = parseInt(v, 10)
    const n = Math.min(Math.max(Number.isFinite(parsed) ? parsed : BADGE_LEVEL, MIN_LEVEL), MAX_LEVEL)
    try {
      // Functional category (#1647): with functional storage rejected the
      // level still applies for this session (listeners fire below) but is
      // not persisted — every new load starts at BADGE_LEVEL, the strictest
      // setting, which is the fail-safe direction to lose a preference in.
      if (canStore(STORAGE_KEY)) localStorage.setItem(STORAGE_KEY, String(n))
    } catch {
      /* ignore persistence failure — in-memory state still updates */
    }
    // Notify every mounted hook (including this component's own) so all sliders
    // and deploy gates in the tab reflect the new level immediately.
    listeners.forEach((fn) => fn(n))
  }, [])

  return [level, setLevel]
}
