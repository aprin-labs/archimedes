// Pure helpers for the Reasoning trace panel's two claim-integrity surfaces
// (#1359): the commit-reveal "block order" copy, and the "Verify hash
// on-chain" button's tri-state result. Dependency-free (no React, no
// ./config) so node --test can exercise it directly — same shape as
// wallet-providers.js.

// ── Block-order panel copy ──────────────────────────────────────────────
//
// Keys off `source` (the API's `temporal_binding_source`), NOT off a bare
// boolean. `TraceResponse` in backend/archimedes/api/schemas.py enforces
// that `temporal_binding_valid` can never be True unless
// `temporal_binding_source === "chain"` — so `source` is the only field
// that can honestly distinguish "the contract's commit()/reveal()/
// executeTrade() path actually ran" from "an off-chain record only".
//
// Before this fix the panel keyed off `temporal_binding_valid` alone and
// unconditionally rendered "(off-chain)" / "not yet enforced on-chain —
// commit-reveal wiring is [still] on the roadmap" copy. Because that copy only
// rendered when `temporal_binding_valid` was truthy, and truthy requires
// `source === "chain"`, the roadmap sentence was wrong 100% of the times a
// user could see it: the contract already enforces the ordering
// (`Vault.rebalance()` reverts without a matching earlier-block commitment
// — see `ReasoningTraceRegistry.executeTrade()`), and `/architecture` says
// so. `valid` is accepted for call-site symmetry with the trace object
// (`{ source: t.temporal_binding_source, valid: t.temporal_binding_valid }`)
// but is not branched on: once `source === "chain"`, the ordering guarantee
// is the contract's, not a computed boolean's.
export function blockOrderCopy({ source, valid: _valid }) {
  if (source === 'chain') {
    return {
      heading: 'Commit → trade → reveal (contract-enforced)',
      note: 'Vault.rebalance() reverts unless this commitment existed in an earlier block — enforced by ReasoningTraceRegistry.executeTrade(), not by our code being well-behaved.',
      tone: 'verified',
    }
  }
  return {
    heading: 'Block order (off-chain record)',
    note: 'Off-chain record only — this trace was anchored without the commit-reveal path, so the ordering is not contract-proven.',
    tone: 'unproven',
  }
}

// ── Verify-hash tri-state ────────────────────────────────────────────────
//
// Mirrors the backend's `TraceVerifyResponse.verification_mode`
// (hash_matched | anchored_only | failed — see traces_routes.py
// `verify_trace`). `anchored_only` is the honest name for "the trace is
// anchored on-chain but nothing was actually re-hashed and compared" — it
// must never render with the same affordance as a real hash match
// (anti-goal: "a hash that was never compared does not get the same
// affordance as one that matched"). Any unrecognised/missing mode
// (including the network-error fallback the UI constructs client-side,
// which never sets verification_mode) degrades to 'failed' rather than
// silently passing.
export function verificationTone(mode) {
  if (mode === 'hash_matched') return 'verified'
  if (mode === 'anchored_only') return 'anchored'
  return 'failed'
}

// ── Source-paper check copy ──────────────────────────────────────────────
//
// `GET /api/traces/{id}/verify` now runs a SECOND, independent check
// (#1637): the on-chain half asks "were these bytes anchored", this half asks
// "does the corpus have the papers this decision cites". They are reported
// separately and must be rendered separately — a trace can be correctly
// anchored while citing a paper that has since left the corpus, and averaging
// the two into one badge hides exactly the fact a reader came for.
//
// The word "verified" is deliberately NOT used here, and that is an owner
// decision, not a style preference (Q8 on #1688): corpus `content_hash` /
// `pdf_sha256` are NULL in production until #1091, so the backend compares
// nothing — it checks EXISTENCE. "Papers verified" would promise a hash
// comparison that did not happen, on a product whose whole claim is that it
// does not overstate what it checked. The copy says "exists in the corpus".
//
// Tri-state, mirroring `papers_verified` / `source_paper_verification.mode`:
//
//   checked + true   → every cited paper was found
//   checked + false  → at least one is missing, or a claimed hash disagreed
//   no_papers_claimed → the trace cites no papers; nothing was attempted
//   corpus_unavailable → the corpus was unreachable; nothing was attempted
//
// Plus one case that carries no mode at all, because it carries no detail
// object: a `verification_mode: 'anchored_only'` response, where the store
// held no off-chain body and so recorded no cited set. That is not "not yet
// checked" — it is "there is nothing to check", and it must not be rendered
// with a "Run Verify" prompt the reader has already followed.
//
// The two `null` modes are NOT collapsed into a failure or a pass. An outage
// that reads as "these papers do not exist" reports fabricated provenance;
// one that reads as a pass is #1359's bug with a different name. And the mode
// is SURFACED, not merely returned — the owner's Q8 wording is "the tri-state
// mode must be surfaced, not just returned", so the mode string itself rides
// in `detail` where a reader can see which check actually ran.
export function sourcePapersCopy(result) {
  const r = result || {}
  const mode = r.source_paper_verification?.mode
  const d = r.source_paper_verification || {}

  // `anchored_only`: the trace store is reachable and simply has no off-chain
  // body for this trace, so `/verify` returns `papers_verified: null` and
  // `source_paper_verification: null` (traces_routes.verify_trace). There is
  // no consulted-paper list recorded anywhere, so nothing was checked and
  // nothing CAN be — the generic fallback at the bottom of this function
  // ("Run Verify to check…") would be a straight lie on this branch: Verify
  // already ran, this IS its answer, and running it again returns the same
  // null. Checked before the `mode` branches because there is no mode to
  // read: the whole detail object is absent.
  if (!mode && r.verification_mode === 'anchored_only') {
    return {
      mode: null,
      tone: 'absent',
      label: 'cited papers not recorded',
      detail:
        'Only the trace hash was anchored on-chain — no off-chain trace body was stored, so there is no record of which papers this decision cited and nothing to look up (verification_mode: anchored_only). Re-running Verify cannot change that.',
    }
  }
  if (mode === 'corpus_unavailable') {
    return {
      mode,
      tone: 'unproven',
      label: 'source papers not checked',
      detail:
        'The paper corpus was unreachable, so the cited papers were not checked (mode: corpus_unavailable). This is not a failure and not a pass — nothing was attempted.',
    }
  }
  if (mode === 'no_papers_claimed') {
    return {
      mode,
      tone: 'absent',
      label: 'no papers cited',
      detail:
        'This trace cites no source papers, so there was nothing to look up (mode: no_papers_claimed). Not a failure — an absence.',
    }
  }
  if (mode === 'checked' && r.papers_verified === true) {
    const n = d.checked ?? 0
    return {
      mode,
      tone: 'verified',
      label: n === 1 ? '1 cited paper exists in the corpus' : `${n} cited papers exist in the corpus`,
      // The limit is stated in the same breath as the result. Corpus
      // content_hash / pdf_sha256 are NULL until #1091, so this is an
      // existence check, not a hash comparison, and saying "verified" here
      // would claim the comparison that did not run.
      detail:
        'Every arXiv id this decision cites was found in the corpus. This checks that the papers EXIST — the corpus stores no per-paper content hash yet (#1091), so no hash was compared.',
    }
  }
  if (mode === 'checked') {
    const missing = d.missing || []
    const mismatch = d.hash_mismatch || []
    const parts = []
    if (missing.length) parts.push(`not in the corpus: ${missing.join(', ')}`)
    if (mismatch.length) parts.push(`content hash disagrees: ${mismatch.join(', ')}`)
    return {
      mode,
      tone: 'failed',
      label: 'cited paper not found',
      detail: `This decision cites papers the corpus cannot account for — ${parts.join('; ')}.`,
    }
  }
  // No result yet, or a response that predates this field. Say so; never
  // default to either verdict.
  return {
    mode: mode || null,
    tone: 'absent',
    label: 'source papers not checked',
    detail: 'Run Verify to check the cited papers against the corpus.',
  }
}

// ── Per-row anchoring state ──────────────────────────────────────────────
//
// Every surface that lists traces — Reasoning, the Portfolio activity feed,
// the strategy passport's Trading-decisions panel — has to answer "is this
// decision anchored?" for each row, and each one used to answer it with its
// own inline ternary on `is_verified`. Three copies of a claim is three
// chances for one of them to be wrong, and two of them already were:
//
//   * Both fell through to "anchor pending — registry write didn't complete
//     yet (usually transient)" for SKIP traces. A skip anchors nothing BY
//     DESIGN (#714): with no trade there is no tradeId for commit() to bind,
//     so the agent never attempts a registry write. "Pending" promises a
//     write that is never coming; the honest state is a permanent, explained
//     absence.
//   * Neither handled `verification_mode === "anchored_only"`, the projection
//     the API returns when an anchor exists but no off-chain body was there
//     to compare against (#1407). Both rendered it via `is_verified` — which
//     that path deliberately leaves true — as a plain green check, i.e. as a
//     hash match that never happened.
//
// Order is load-bearing. `anchored_only` is checked FIRST because that path
// sets `is_verified: true` on purpose (the anchor genuinely is confirmed; it
// is the *comparison* that didn't happen), so an `is_verified` check placed
// first would swallow it. The skip check comes after the anchored checks
// because a legacy publishTrace-fallback skip can carry a real `arc_tx_hash`
// — "no trade to bind" describes why an anchor is absent, and must not
// override one that is present.
//
// None of these labels claims a hash was compared. That claim belongs only to
// `GET /api/traces/{id}/verify` (see `verificationTone` above) and is only
// ever earned by clicking Verify.
export const ANCHOR_STATES = {
  anchored: {
    state: 'anchored',
    label: 'anchored on Arc',
    icon: 'i-lucide-check-circle',
    tone: 'verified',
    title:
      'The trace hash is anchored on Arc. That confirms the anchor exists, not that the stored body still hashes to it — use Verify to re-fetch the receipt and compare.',
  },
  anchored_unverified: {
    state: 'anchored_unverified',
    label: 'anchored — not re-hashed',
    icon: 'i-lucide-anchor',
    tone: 'anchored',
    title:
      'An anchor exists in the on-chain registry, but no off-chain trace body was stored to compare against it. Zero hashes were compared — this is not a verification.',
  },
  not_anchored_no_trade: {
    state: 'not_anchored_no_trade',
    label: 'not anchored (no trade to bind)',
    icon: 'i-lucide-skip-forward',
    tone: 'absent',
    title:
      'No trade was made, so there is nothing for an on-chain commitment to bind. The trace is hashed and persisted off-chain; no anchor is attempted or pending.',
  },
  anchor_pending: {
    state: 'anchor_pending',
    label: 'anchor pending',
    icon: 'i-lucide-clock',
    tone: 'pending',
    title:
      "Trace hashed + persisted off-chain; on-chain anchor pending (registry write didn't complete yet — usually transient).",
  },
}

export function anchorState(trace) {
  const t = trace || {}
  if (t.verification_mode === 'anchored_only') return ANCHOR_STATES.anchored_unverified
  if (t.arc_tx_hash || t.is_verified) return ANCHOR_STATES.anchored
  if (t.decision_type === 'skip') return ANCHOR_STATES.not_anchored_no_trade
  return ANCHOR_STATES.anchor_pending
}

// ── Which traces actually name a strategy ────────────────────────────────
//
// `strategies_referenced` is named for strategy ids and does not uniformly
// hold them. The agent runner writes real strategy ids on its decision traces;
// the two CONSTRUCTION writers in api/strategies_routes.py write arXiv ids and
// paper-anchor strings into the same field. So "the first entry of
// strategies_referenced is a strategy id" is true for decision traces and
// false for construction ones, and a follow-back button that ignores the
// difference deep-links a reader to a passport for an arXiv id — a 404 dressed
// up as provenance.
//
// This mirrors `STRATEGY_REFERENCE_DECISION_TYPES` /
// `trace_references_strategy` in backend/archimedes/services/redis_state.py,
// which is what `GET /api/traces/?strategy_id=` filters on. The two must agree:
// a row that offers a follow-back here is exactly a row the scoped listing on
// that strategy's passport would return. Change one, change the other.
export const STRATEGY_REFERENCE_DECISION_TYPES = new Set([
  'rebalance',
  'rotation',
  'regime_change',
  'skip',
])

/**
 * The strategy id this trace consulted, or null when it names none.
 *
 * Null — not a guess — for construction/generation traces, for an unknown
 * shape, and for an empty list. The caller renders no follow-back rather than
 * one that leads nowhere.
 */
export function referencedStrategyId(trace) {
  const t = trace || {}
  if (!STRATEGY_REFERENCE_DECISION_TYPES.has(t.decision_type)) return null
  const refs = t.strategies_referenced
  if (typeof refs === 'string') return refs || null
  if (!Array.isArray(refs)) return null
  const first = refs.find((r) => typeof r === 'string' && r)
  return first || null
}
