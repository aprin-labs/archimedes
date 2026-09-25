// Pure picker for the Generate page's "Surprise me" button (issue #1642).
//
// Plain JS, no React, no module-level state: the caller owns the "what did I
// show last" memory and passes it back in. That is what makes the two
// load-bearing behaviours testable without a DOM (ui/test/surprise-briefs.test.js):
//
//   1. Nothing from the bank is visible until this is called. The component
//      renders no brief text at mount, on error, or as a fallback — the only
//      way any entry reaches the screen is a press that calls this function.
//   2. Two consecutive presses never return the same entry. With a plain
//      `bank[Math.floor(Math.random() * bank.length)]` a repeat reads as a
//      broken button, and on a 124-entry bank one press in 124 would be a
//      no-op. The previous id is excluded from the draw rather than being
//      re-rolled, so the guarantee is structural, not probabilistic.
//
// `random` is injectable so the tests can pin the boundary cases (a random()
// of exactly 0 and of the largest float below 1) instead of trusting 200
// samples of Math.random to happen to hit them.

/**
 * Draw one entry from the bank, never repeating `previousId`.
 *
 * @param {Array<{id: string}>} bank - the brief bank (SURPRISE_BRIEFS).
 * @param {string|null} [previousId] - id of the entry shown last, excluded
 *   from this draw. Ignored when the bank holds only one entry (there is
 *   nothing else to return, and returning null would blank the box).
 * @param {() => number} [random] - source of randomness in [0, 1).
 * @returns {object|null} an entry from `bank`, or null when the bank is empty.
 */
export function pickSurpriseBrief(bank, previousId = null, random = Math.random) {
  if (!Array.isArray(bank) || bank.length === 0) return null
  if (bank.length === 1) return bank[0]

  const pool = bank.filter((entry) => entry && entry.id !== previousId)
  // `previousId` matching nothing (first press, or a bank edit that dropped
  // the last-shown entry) leaves the full bank in play — correct, not a bug.
  const candidates = pool.length > 0 ? pool : bank

  // Math.floor(random() * n) is n only if random() returns exactly 1, which
  // the contract forbids but a caller-supplied stub can do; clamp rather than
  // return undefined.
  const index = Math.min(Math.floor(random() * candidates.length), candidates.length - 1)
  return candidates[index]
}

export default pickSurpriseBrief
