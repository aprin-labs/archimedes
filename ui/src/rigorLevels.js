// Rigor-strictness level LABELS, split out from RigorStrictnessControl.jsx (#1645).
//
// `levelLabel` is a pure lookup — it renders a rung's name from a level number
// and never touches the slider. It lived next to the control because that is
// where it was first needed, which meant every consumer of the *label* had to
// import the *control's* module. When #1645 removed the control's card from the
// Library page, `Strategies.jsx` still needed the label for DeployabilityChip,
// so the page kept a `from './RigorStrictnessControl'` import for a component it
// no longer renders — a false signal to anyone grepping for where that control
// is mounted, and the exact grep the issue's acceptance criterion runs.
//
// RigorStrictnessControl.jsx re-exports `levelLabel` from here, so existing
// importers (StrategyPassport.jsx) are unchanged.

// Static label map so callers without the fetched ladder (e.g. the passport's
// deploy copy) still render names, not "Level 3".
export const STATIC_LABELS = {
  1: 'Conservative',
  2: 'Balanced',
  3: 'Moderate',
  4: 'Aggressive',
  5: 'Speculative',
}

export function levelLabel(ladder, level) {
  const found = (ladder?.levels || []).find((l) => l.level === level)
  return found?.label || STATIC_LABELS[level] || `Level ${level}`
}
