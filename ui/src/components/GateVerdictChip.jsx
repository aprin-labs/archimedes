import { gateVerdict, gateVerdictText } from '../paperCopy'

// THE VERDICT OF RECORD, RENDERED BESIDE A FORWARD PERFORMANCE NUMBER (#1764).
//
// Deploy is at will — a gate-FAILED strategy can be paper-traded, and
// deliberately so — which is exactly why this component has no conditional
// render arm: `gateVerdict` returns a state for every payload, including one
// that carried no verdict at all, so there is no input for which this draws
// nothing beside the percentage it accompanies. Callers must render it
// unconditionally too; `ui/test/paper-gate-verdict.test.js` pins that at every
// call site, because a number without its verdict reads as an endorsement.
//
// ONE implementation for every surface that shows a paper return — /app/paper's
// deployment card and the leaderboard's "Live paper trading" board. Two copies
// of the four-state → words → colour mapping is how two pages start describing
// the same strategy differently (#1358).
//
// Colour is decoration and never the message: the words say "Gate: failed", so
// a colour-blind reader, a high-contrast theme and a screenshot all still carry
// the verdict (1.4.1). `pass` is the only state that is ever green.
//
// `ariaHidden` exists for the ONE surface whose figure already announces the
// verdict in its own accessible line (PaperTrading's `paperReturnAnnouncement`):
// there the chip is the visible half of a statement a screen reader has already
// heard in full, and repeating it makes the card announce the verdict twice.
// Where nothing else announces it — the board's table cell — the chip is the
// spoken source and this must stay false.
export default function GateVerdictChip({ dep, ariaHidden = false, style }) {
  const v = gateVerdict(dep)
  const color =
    v.tone === 'positive' ? 'var(--accent)' : v.tone === 'negative' ? 'var(--negative)' : 'var(--text-3)'
  return (
    <div
      className="caption"
      aria-hidden={ariaHidden ? 'true' : undefined}
      title={v.title}
      style={{
        marginTop: 6,
        color,
        fontFamily: 'var(--mono, monospace)',
        // The verdict is a statement, not a truncatable decoration: a long
        // state ("Gate: unevaluable — flat returns") wraps rather than being
        // clipped by the column's min-width.
        whiteSpace: 'normal',
        ...style,
      }}
    >
      {gateVerdictText(dep)}
    </div>
  )
}
