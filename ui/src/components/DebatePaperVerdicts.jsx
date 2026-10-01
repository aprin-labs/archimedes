// The generation debate's readable parts, rendered once and used on two
// surfaces: live in the generation stream (GenerationStream.jsx) and after the
// fact on the strategy passport (StrategyReasoning.jsx).
//
// Shared on purpose. The passport and the stream are showing the SAME rows —
// the SSE `debate_turn` / `debate_attribution` events carry exactly what
// `_persist_debate_transcripts` writes to `debate_transcripts.transcript_json`,
// which is what GET /api/strategies/{id}/debate reads back. Two renderers would
// be two accounts of one run, free to disagree.
//
// Everything here is ALREADY SANITIZED where it is produced: the DB writer runs
// `sanitize_transcript` inside `record_debate_transcript`, and the SSE emitter
// (`debate_engine._emit_debate_turn`) runs the same scrubber before the frame
// leaves the server. That is the module contract in
// backend/archimedes/models/debate_transcript.py — a raw read of the stored
// transcript is already safe — so nothing below re-scrubs, and nothing below
// may be pointed at a source that has not been through it.
//
// Nothing here renders an anchoring claim. A debate turn moved no money and is
// anchored nowhere; putting it in a list whose rows imply an on-chain
// commitment is the dishonest merge that ui/test/anchor-state.test.js guards.

// ── Claims ──────────────────────────────────────────────────────────────
//
// A claim arrives in one of TWO shapes and both are real data (#1636):
//   - a plain string — rows persisted before the debate carried attribution;
//   - `{claim, candidate_id, arxiv_ids}` — the current shape. `arxiv_ids` was
//     filtered server-side against the papers the proposers actually read, so
//     an id here is real, and an EMPTY array is meaningful: the claim was made
//     without grounding it in a listed paper, and saying so is the point.

export function claimText(claim) {
	if (typeof claim === "string") return claim;
	if (claim && typeof claim === "object") return String(claim.claim ?? "");
	return "";
}

export function claimArxivIds(claim) {
	if (!claim || typeof claim !== "object" || Array.isArray(claim)) return null;
	return Array.isArray(claim.arxiv_ids) ? claim.arxiv_ids : null;
}

/** Split a `/debate` transcript into the argued turns and the single trailing
 *  paper-attribution entry (#1739).
 *
 *  Without this split the attribution entry renders as an almost-blank turn
 *  card: a walker that reads only `role`/`round`/`verdict`/`claims`/`discard`
 *  shows its summary sentence and silently drops `paper_verdicts` and
 *  `fusion_reasoning` — i.e. exactly the per-paper record and the discard
 *  reasons a reader came for. */
export function splitTranscript(transcript) {
	const entries = Array.isArray(transcript) ? transcript : [];
	return {
		turns: entries.filter((e) => e && e.role !== "attribution"),
		attribution: entries.find((e) => e && e.role === "attribution") || null,
	};
}

const VERDICT_TAG = {
	cited: "tag-positive",
	discarded: "tag-muted",
	contested: "tag-warning",
	unused: "tag-muted",
};

// What each per-paper verdict MEANS, stated rather than left to be inferred
// from a one-word chip.
//
// Two of these are carefully weaker than they look, because the backend is:
//
//   * "unused" is NOT "shown to the researchers and ignored". The rows come
//     from `evidence_by_id`, which `_propose_pool` defines as every paper that
//     entered any PROPOSER prompt on this run. What the bull/bear turns are
//     actually handed is `_candidate_cards` — only the papers cited by the top
//     `_DEBATE_CARD_MAX` pool candidates — so most unused rows are papers the
//     debaters never saw. The absence is still worth recording; it is just an
//     absence of citation, not of attention.
//   * "contested" does NOT imply two researchers. `_aggregate_paper_verdicts`
//     sets it on `cited_by AND discarded_by` and never compares the lists, so
//     the bull citing a paper in round 1 and dropping it in round 2 lands here
//     with one role on both sides.
const VERDICT_TITLE = {
	cited: "At least one researcher rested a claim on this paper.",
	discarded: "A researcher named this paper and threw it out.",
	contested: "Cited in one turn and thrown out in another.",
	unused: "Retrieved for this run, but named by neither researcher.",
};

// Legend order — most-engaged first. Rendered visibly (below), not only as a
// `title=` tooltip: this panel was built for an owner reading a generation on a
// phone, where nothing hovers.
const VERDICT_ORDER = ["cited", "contested", "discarded", "unused"];

// ── One debate turn ─────────────────────────────────────────────────────

export function DebateTurn({ turn, headline }) {
	if (!turn) return null;
	const role = turn.role;
	const claims = Array.isArray(turn.claims) ? turn.claims : [];
	const discards = Array.isArray(turn.discard) ? turn.discard : [];
	return (
		<div className="card" style={{ padding: 12 }}>
			<div className="flex gap-2 items-center flex-wrap mb-1.5">
				<span
					className={`tag ${role === "bull" ? "tag-positive" : role === "bear" ? "tag-negative" : "tag-muted"}`}
				>
					{role || "turn"}
				</span>
				{turn.round != null && <span className="caption">Round {turn.round}</span>}
			</div>
			{headline && (
				<p className="caption mb-1" style={{ color: "var(--text-2)" }}>
					{headline}
				</p>
			)}
			{turn.verdict && (
				<p className="body" style={{ fontSize: "0.85rem", lineHeight: 1.5 }}>
					{turn.verdict}
				</p>
			)}
			{claims.length > 0 && (
				<ul className="caption mt-1.5 leading-relaxed pl-4 list-disc">
					{claims.map((claim, j) => {
						const ids = claimArxivIds(claim);
						return (
							<li key={j}>
								{claimText(claim)}
								{ids !== null &&
									(ids.length > 0 ? (
										<span className="caption" style={{ opacity: 0.75 }}>
											{" "}
											— {ids.map((id) => `arXiv:${id}`).join(", ")}
										</span>
									) : (
										<span className="caption" style={{ opacity: 0.75 }}>
											{" "}
											— not attributed to a listed paper
										</span>
									))}
							</li>
						);
					})}
				</ul>
			)}
			{discards.length > 0 && (
				<ul className="caption mt-1.5 leading-relaxed pl-4 list-disc">
					{discards.map((d, j) => (
						<li key={j} style={{ opacity: 0.75 }}>
							Discarded arXiv:{d?.arxiv_id}
							{d?.reason ? ` — ${d.reason}` : ""}
						</li>
					))}
				</ul>
			)}
		</div>
	);
}

// ── The paper-attribution entry ─────────────────────────────────────────

/**
 * The per-paper record: the summary sentence, the proposer's own per-paper
 * mechanism prose (`fusion_reasoning`), and a row per RETRIEVED paper saying
 * who cited it, who threw it out, and why.
 *
 * `entry` is the `role: "attribution"` transcript entry — from the persisted
 * transcript on the passport, or from the live `debate_attribution` SSE frame,
 * which carry the same keys because one helper
 * (`generation_pipeline._paper_attribution_entry`) builds both.
 *
 * `showSummary` is false on the generation stream, where the summary sentence is
 * already the log row's headline — the card there adds the prose and the table,
 * not a second copy of the same sentence.
 */
export default function DebatePaperVerdicts({ entry, compact = false, showSummary = true }) {
	if (!entry) return null;
	const rows = Array.isArray(entry.paper_verdicts) ? entry.paper_verdicts : [];
	const reasoning = entry.fusion_reasoning || "";
	if (!entry.verdict && !rows.length && !reasoning) return null;

	return (
		<div className="card" style={{ padding: 12 }}>
			{showSummary && entry.verdict && (
				<p className="body" style={{ fontSize: "0.85rem", lineHeight: 1.5 }}>
					{entry.verdict}
				</p>
			)}
			{reasoning && (
				<p className="caption mt-1.5 leading-relaxed" style={{ color: "var(--text-2)" }}>
					{reasoning}
				</p>
			)}
			{rows.length > 0 && (
				<div style={{ overflowX: "auto", marginTop: 10 }}>
					<table className="caption" style={{ width: "100%", borderCollapse: "collapse" }}>
						<caption className="caption" style={{ captionSide: "top", textAlign: "left", paddingBottom: 6, color: "var(--text-3)" }}>
							Every paper this run retrieved and put in front of the proposers —
							including the ones the debate never named.
						</caption>
						<thead>
							<tr style={{ textAlign: "left", color: "var(--text-3)" }}>
								<th style={{ padding: "4px 8px 4px 0" }}>Paper</th>
								<th style={{ padding: "4px 8px" }}>Verdict</th>
								{!compact && <th style={{ padding: "4px 8px" }}>Who</th>}
								<th style={{ padding: "4px 0 4px 8px" }}>Why it was thrown out</th>
							</tr>
						</thead>
						<tbody>
							{rows.map((row, i) => {
								const verdict = row?.verdict || "unused";
								const citedBy = Array.isArray(row?.cited_by) ? row.cited_by : [];
								const discardedBy = Array.isArray(row?.discarded_by) ? row.discarded_by : [];
								const reasons = Array.isArray(row?.discard_reasons) ? row.discard_reasons : [];
								return (
									<tr key={row?.arxiv_id || i} style={{ borderTop: "1px solid var(--glass-border)" }}>
										<td style={{ padding: "6px 8px 6px 0", verticalAlign: "top" }}>
											<span className="mono">arXiv:{row?.arxiv_id}</span>
											{row?.title && (
												<div style={{ opacity: 0.75 }}>{row.title}</div>
											)}
										</td>
										<td style={{ padding: "6px 8px", verticalAlign: "top" }}>
											<span
												className={`tag ${VERDICT_TAG[verdict] || "tag-muted"}`}
												title={VERDICT_TITLE[verdict] || ""}
											>
												{verdict}
											</span>
										</td>
										{!compact && (
											<td style={{ padding: "6px 8px", verticalAlign: "top", opacity: 0.8 }}>
												{citedBy.length > 0 && <div>cited by {citedBy.join(", ")}</div>}
												{discardedBy.length > 0 && <div>thrown out by {discardedBy.join(", ")}</div>}
												{citedBy.length === 0 && discardedBy.length === 0 && <span>—</span>}
											</td>
										)}
										<td style={{ padding: "6px 0 6px 8px", verticalAlign: "top", opacity: 0.8 }}>
											{reasons.length > 0 ? reasons.join("; ") : "—"}
										</td>
									</tr>
								);
							})}
						</tbody>
					</table>
				</div>
			)}
			{/* The key, OUTSIDE the horizontal scroller above: on a narrow screen
			    the table scrolls sideways and a legend inside it would scroll out
			    of view with the columns it explains. Only the verdicts this run
			    actually produced are listed — an empty category needs no key. */}
			{rows.length > 0 && (
				<ul
					className="caption"
					style={{ margin: "8px 0 0", padding: 0, listStyle: "none", color: "var(--text-3)" }}
				>
					{VERDICT_ORDER.filter((v) => rows.some((r) => (r?.verdict || "unused") === v)).map(
						(v) => (
							<li key={v} style={{ marginTop: 3 }}>
								<span className={`tag ${VERDICT_TAG[v]}`}>{v}</span> {VERDICT_TITLE[v]}
							</li>
						),
					)}
				</ul>
			)}
		</div>
	);
}
