// The generation stream's own copy, as a pure module.
//
// Owner, watching a live run on a phone: "You still can't see the actual
// reasoning traces anywhere as best I can tell." What the stream showed him
// was its developer event log, verbatim:
//
//   #17 Tool result — synthesize → leader=Volatility-Relief Swing dsr=0.082005 of 1 entries
//   #19 Candidate evaluated — cand_neutral — DSR 0.082005 · OOS 0.198029
//   #21 Trace hashed — 6ae4b039607e14…
//
// Two separate problems, and this module is the fix for both:
//
//   1. The copy was the wire format. `tool_result` rendered as
//      `${tool_name} → ${result_summary}` — the backend's own machine string,
//      unedited. Every line below now leads with a human sentence
//      (`eventHeadline`) and keeps the machine string as a second, collapsed
//      line (`eventDetail`). NOTHING verifiable is dropped: the numbers a
//      reader might check — DSR, OOS, the trace-hash prefix, the strategy
//      name — stay in the headline; only the developer strings move.
//
//   2. `EVENT_LABELS` is the SUBSCRIPTION LIST, not just a lookup table:
//      GenerationStream.jsx does
//      `Object.keys(EVENT_LABELS).forEach(name => es.addEventListener(name, …))`,
//      so an event name missing from it is never received by the client at
//      all. That is exactly why `backtest_running` / `backtest_done` /
//      `backtest_failed` are emitted by the backend and invisible in the UI to
//      this day. Keeping the label map and the copy map in ONE module lets
//      ui/test/generation-reasoning.test.js pin them against each other.
//
// Lives in `src/` as plain JS (not inside the .jsx) for the same reason
// `trace-binding.js` does: the copy is testable this way, and a component
// cannot quietly re-derive its own version of it.

// ── The subscription list ───────────────────────────────────────────────
//
// Every key here is BOTH an `addEventListener` name and the bold prefix on the
// log row. Adding an event to the backend without adding it here means the
// browser never hears it.

export const EVENT_LABELS = {
	job_queued: "Queued",
	brief_validated: "Brief read",
	pipeline_selected: "Pipeline chosen",
	candidates_selected: "Candidates chosen",
	agent_iteration: "Progress",
	tool_called: "Started",
	tool_result: "Finished",
	debate_turn: "Researcher argued",
	debate_attribution: "Papers accounted for",
	candidate_drafted: "Candidate drafted",
	candidate_failed: "Candidate dropped",
	candidate_evaluated: "Candidate graded",
	best_selected: "Leader picked",
	trace_hashed: "Provenance",
	persisted: "Saved",
	done: "Done",
	error: "Error",
};

// Events whose payload carries a `regime` the row should badge.
export const REGIME_BADGED_EVENTS = new Set([
	"candidate_drafted",
	"candidate_failed",
	"persisted",
]);

// ── Tool copy ───────────────────────────────────────────────────────────
//
// One entry per `tool_name` the backend emits (debate_engine.py's `tool_called`
// / `tool_result` sites). `started` and `finished` are separate sentences
// because "backtesting every survivor" and "backtested every survivor" are
// different claims about where the run is.
//
// Most tools emit only ONE of the two phases today: `tool_called` fires for
// `propose_pool`, the four `debate_*_r1`/`_r2` turns and `evaluate_fusion_spec`;
// `tool_result` fires for `propose_pool`, `debate_paper_verdicts`, `critic_prov`,
// `critic_regime` and `synthesize`. So several sentences below are unreachable
// as the backend stands. They are written anyway, and kept in sync with their
// pair, so that a new emit site gets real copy instead of the generic
// `Running <tool>` fallback — not because all twenty lines are on screen.
//
// An unknown tool falls back to naming it rather than inventing a description —
// a new backend tool must not be narrated by copy written before it existed.

export const TOOL_COPY = {
	propose_pool: {
		started: "Searching the paper corpus and drafting candidate strategies",
		finished: "Drafted the candidate pool from the retrieved papers",
	},
	debate_bull_r1: {
		started: "Bull researcher is stating its case",
		finished: "Bull researcher stated its case",
	},
	// The bear's turn verdict on the wire is `act` | `decline` (debate_engine's
	// critique prompt), so this copy says "decline". "Abstain" is a different,
	// first-class outcome — `_abstain_result` / `generation_method="debate_abstain"`,
	// hold current weights — reached by the regime gate or by no candidate
	// beating the passive null, never by a bear turn. Naming the turn
	// "abstention" would collapse two distinct outcomes into one word.
	debate_bear_r1: {
		started: "Bear researcher is arguing to decline",
		finished: "Bear researcher argued to decline",
	},
	debate_bull_r2: {
		started: "Bull researcher is rebutting the bear",
		finished: "Bull researcher rebutted the bear",
	},
	debate_bear_r2: {
		started: "Bear researcher is rebutting the bull",
		finished: "Bear researcher rebutted the bull",
	},
	debate_paper_verdicts: {
		started: "Counting which retrieved papers the debate engaged with",
		finished: "Counted which retrieved papers the debate engaged with",
	},
	critic_prov: {
		started: "Checking every citation against the embargo window",
		finished: "Dropped candidates citing papers outside the embargo window",
	},
	critic_regime: {
		started: "Reading the live market regime",
		finished: "Read the live market regime",
	},
	evaluate_fusion_spec: {
		started: "Backtesting every candidate that survived the citation check",
		finished: "Backtested every surviving candidate",
	},
	synthesize: {
		started: "Ranking the survivors",
		finished: "Ranked the survivors and picked a leader",
	},
};

function toolSentence(toolName, phase) {
	const copy = TOOL_COPY[toolName];
	if (copy?.[phase]) return copy[phase];
	// Honest fallback for a tool this copy predates.
	return phase === "started"
		? `Running ${toolName || "a step"}`
		: `Finished ${toolName || "a step"}`;
}

// ── Small formatters ────────────────────────────────────────────────────

function excerpt(text, max) {
	const s = String(text ?? "");
	return s.length > max ? `${s.slice(0, max)}…` : s;
}

function plural(n, one, many) {
	return `${n} ${n === 1 ? one : many}`;
}

/** Client-side fallback for a `debate_turn` that arrived without the
 *  server-written `headline` — an older backend, or a partial frame. Says less
 *  than the server's version rather than inventing the parts it cannot know. */
function turnFallbackHeadline(data) {
	const who =
		data?.role === "bull"
			? "Bull researcher"
			: data?.role === "bear"
				? "Bear researcher"
				: "Researcher";
	const stage =
		data?.round === 1 ? "opening argument" : data?.round === 2 ? "rebuttal" : "argument";
	return `${who}, ${stage}`;
}

// ── Headlines ───────────────────────────────────────────────────────────
//
// The human line, one per event name. THE KEYS OF THIS OBJECT MUST MATCH
// `EVENT_LABELS` EXACTLY — ui/test/generation-reasoning.test.js asserts the two
// sets are equal in both directions, which is the guard that would have caught
// the `backtest_*` events being emitted into a client that never subscribed.
//
// House rule for every sentence below: no engine vocabulary. No candidate ids
// (`cand_neutral`), no machine keys (`dsr=`, `pool_size=`), no leaderboard
// arithmetic (`of 1 entries`). Named statistics a reader can act on (DSR, OOS,
// PBO) and identifiers they can verify (the trace-hash prefix) DO belong here —
// those are the numbers, not the jargon. Everything else lives in `eventDetail`.

export const HEADLINES = {
	// Label is "Queued"; the headline shows the brief rather than saying
	// "Queued" a second time.
	job_queued: (d) =>
		d?.brief?.intent ? `Your brief: "${excerpt(d.brief.intent, 90)}"` : "Your brief",
	brief_validated: (d) =>
		`Read your brief — ${d?.risk_appetite || "unspecified"} risk appetite`,
	pipeline_selected: (d) =>
		d?.reason
			? `Chose how to build it — ${d.reason}`
			: `Chose how to build it${d?.pipeline ? ` (${d.pipeline})` : ""}`,
	candidates_selected: (d) =>
		// No paper count here on purpose: retrieval happens inside each
		// candidate's run, so no honest count exists yet at this point.
		`Considering ${plural(Number(d?.candidate_count) || 0, "candidate strategy", "candidate strategies")}`,
	// Deliberately conservative. The debate emits stages 1-3 of a declared max
	// of 4, so any invented per-stage name map would be a false label.
	agent_iteration: (d) => `Stage ${d?.iteration_n ?? "?"} of ${d?.max_iterations ?? "?"}`,
	tool_called: (d) => toolSentence(d?.tool_name, "started"),
	tool_result: (d) => toolSentence(d?.tool_name, "finished"),
	debate_turn: (d) => String(d?.headline || turnFallbackHeadline(d)),
	debate_attribution: (d) => String(d?.verdict || "Counted how the retrieved papers were used"),
	candidate_drafted: (d) => {
		// The paper count comes from the candidate's ACTUAL, provenance-checked
		// citations (`source_arxiv_ids`) — omitted when absent, never invented.
		const n = d?.source_arxiv_ids?.length || 0;
		const name = d?.strategy_name || "A candidate";
		return n > 0 ? `Drafted "${name}" from ${plural(n, "paper", "papers")}` : `Drafted "${name}"`;
	},
	candidate_evaluated: (d) => {
		const v = d?.rigor_verdict || {};
		const bits = [];
		if (v.dsr != null) bits.push(`DSR ${v.dsr}`);
		if (v.pbo != null) bits.push(`PBO ${v.pbo}`);
		if (v.oos_sharpe != null) bits.push(`OOS ${v.oos_sharpe}`);
		return bits.length
			? `Rigor gate ran on the draft — ${bits.join(" · ")}`
			: "Rigor gate ran on the draft";
	},
	candidate_failed: (d) => d?.message || "No candidate survived for this regime",
	best_selected: (d) => `Picked the leading candidate out of ${Number(d?.considered_count) || 0} considered`,
	// NOT "hashed the reasoning", and NOT "can't be edited after the fact" —
	// both were false. `generation_pipeline._persist_candidate` keccaks the
	// canonical `{brief, candidate_id, strategy_name, weights, rigor_verdict}`
	// tuple: the debate transcript and `fusion_reasoning` are nowhere in the
	// preimage. And nothing makes it tamper-evident — it lands as
	// `strategy_store.provenance_hash`, the pipeline writes no ReasoningTrace
	// and anchors nothing on chain (the docstring's "mirrored on-chain" is v1.5),
	// while PATCH /api/strategies/{id} renames the row afterwards and
	// deliberately does NOT recompute it. Claiming immutability here would also
	// borrow the affordance `trace-binding.js` invented `anchored_only` to deny.
	trace_hashed: (d) =>
		`Stamped this generation with a content hash — ${String(d?.trace_hash || "").slice(0, 14)}…`,
	// Label is "Saved"; the headline says where, not the same word again.
	persisted: () => "Now in your Library",
	done: () => "Your strategy is ready",
	error: (d) => d?.message || "Generation failed",
};

/** The human line for one event. Unknown names degrade to the raw name rather
 *  than to silence, so a newly-added backend event is visible while its copy is
 *  still being written. */
export function eventHeadline(name, data) {
	const fn = HEADLINES[name];
	return fn ? fn(data || {}) : String(name || "");
}

// ── Details ─────────────────────────────────────────────────────────────
//
// The machine fields, kept verbatim under a collapsed toggle. This is where the
// backend's own `args_summary` / `result_summary` strings and the candidate ids
// live now — moved, not deleted, so the log is still the developer artifact it
// was while no longer being the ONLY thing on screen.

export function eventDetail(name, data) {
	const d = data || {};
	switch (name) {
		case "pipeline_selected":
			return d.pipeline ? `pipeline=${d.pipeline}` : "";
		case "agent_iteration":
			return d.candidate_id ? `${d.candidate_id}` : "";
		case "tool_called":
			return `${d.tool_name}(${d.args_summary || ""})`;
		case "tool_result":
			return `${d.tool_name} → ${d.result_summary || "ok"}`;
		case "debate_turn":
			return `${d.candidate_id || ""} ${d.role || ""} r${d.round ?? "?"}`.trim();
		case "debate_attribution":
			return [
				d.candidate_id,
				d.papers_offered != null ? `papers_offered=${d.papers_offered}` : "",
				d.distinct_mechanism_papers != null
					? `distinct_mechanism_papers=${d.distinct_mechanism_papers}`
					: "",
			]
				.filter(Boolean)
				.join(" · ");
		case "candidate_drafted":
			return [d.candidate_id, (d.source_arxiv_ids || []).map((a) => `arXiv:${a}`).join(", ")]
				.filter(Boolean)
				.join(" · ");
		case "candidate_evaluated":
			return [d.candidate_id, d.regime ? `regime=${d.regime}` : ""].filter(Boolean).join(" · ");
		case "candidate_failed":
			return [d.candidate_id, d.regime ? `regime=${d.regime}` : "", d.error]
				.filter(Boolean)
				.join(" · ");
		case "best_selected":
			return [
				d.best_candidate_id,
				d.validated_count != null ? `validated=${d.validated_count}` : "",
				d.deployable != null ? `deployable=${d.deployable}` : "",
			]
				.filter(Boolean)
				.join(" · ");
		case "trace_hashed":
			return String(d.trace_hash || "");
		case "persisted":
			return [d.strategy_id, d.redirect_url].filter(Boolean).join(" · ");
		case "done":
			return [d.strategy_id, d.served_model ? `served_model=${d.served_model}` : ""]
				.filter(Boolean)
				.join(" · ");
		case "error":
			return [d.code, d.hint].filter(Boolean).join(" · ");
		default:
			return "";
	}
}
