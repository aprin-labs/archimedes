import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	ANCHOR_STATES,
	STRATEGY_REFERENCE_DECISION_TYPES,
	anchorState,
	referencedStrategyId,
} from "../src/trace-binding.js";

// The per-row anchoring claim, and the three surfaces that make it.
//
// Reasoning, the Portfolio activity feed, and the new strategy-passport panel
// each have to answer "is this decision anchored?" for every row. Two of them
// answered it with their own inline ternary on `is_verified`, and both were
// wrong in the same two ways:
//
//   * a SKIP trace was labelled "anchor pending", promising a registry write
//     that by design never arrives — a skip has no trade for commit() to bind
//     (#714), so no anchor is ever attempted;
//   * the `anchored_only` projection (an anchor exists, no off-chain body was
//     there to compare against — #1407) rendered as a plain green check, i.e.
//     as a hash comparison that never happened.
//
// These pin the honest four-state derivation and then pin, per component,
// that the component actually calls it instead of re-deriving it.

const src = (p) => readFileSync(new URL(`../src/${p}`, import.meta.url), "utf8");

// ── The derivation ──────────────────────────────────────────────────────

test("anchored_only wins over is_verified — the anchor is real, the comparison is not", () => {
	// That path sets is_verified: true ON PURPOSE (the anchor genuinely is
	// confirmed). An is_verified check placed first would swallow it and
	// re-render it as a hash match.
	const a = anchorState({
		verification_mode: "anchored_only",
		is_verified: true,
		arc_tx_hash: null,
	});
	assert.equal(a.state, "anchored_unverified");
	assert.notEqual(a.tone, "verified");
	assert.match(a.label, /not re-hashed/);
	assert.match(a.title, /Zero hashes were compared/);
});

test("an anchored trace is labelled anchored, and claims no hash comparison", () => {
	const a = anchorState({ arc_tx_hash: "0xabc", is_verified: true });
	assert.equal(a.state, "anchored");
	assert.equal(a.tone, "verified");
	assert.equal(a.label, "anchored on Arc");
	// The label must not say "verified": that word belongs to
	// /api/traces/{id}/verify, and only after a real re-fetch and compare.
	assert.ok(!/verified/i.test(a.label), `label overclaims: ${a.label}`);
	assert.match(a.title, /not that the stored body still hashes to it/);
});

test("a skip with no anchor is a permanent explained absence, never 'pending'", () => {
	const a = anchorState({ decision_type: "skip", arc_tx_hash: null, is_verified: false });
	assert.equal(a.state, "not_anchored_no_trade");
	assert.equal(a.label, "not anchored (no trade to bind)");
	assert.ok(
		!/pending/i.test(a.label),
		"a skip must never be labelled pending — no write is coming",
	);
	// The title may only mention pending to DENY it. Pin the denial rather
	// than the substring, so a future edit cannot reintroduce the promise
	// while still matching a naive `!includes("pending")` check.
	assert.match(a.title, /no anchor is attempted or pending/);
	assert.notEqual(a.state, ANCHOR_STATES.anchor_pending.state);
});

test("a non-skip trace with no anchor is still genuinely pending", () => {
	const a = anchorState({ decision_type: "rebalance", arc_tx_hash: null, is_verified: false });
	assert.equal(a.state, "anchor_pending");
	assert.match(a.label, /pending/);
});

test("a skip that DID anchor keeps its anchor — 'no trade to bind' explains an absence, not a presence", () => {
	// The legacy publishTrace fallback could anchor a skip. Ordering the skip
	// check before the anchored checks would erase a real on-chain reference.
	const a = anchorState({ decision_type: "skip", arc_tx_hash: "0xabc" });
	assert.equal(a.state, "anchored");
});

test("the four states are mutually distinct in state, label, and icon", () => {
	const all = Object.values(ANCHOR_STATES);
	assert.equal(all.length, 4);
	for (const key of ["state", "label", "icon"]) {
		const seen = new Set(all.map((s) => s[key]));
		assert.equal(seen.size, 4, `two states share a ${key} — they would be indistinguishable`);
	}
	// Exactly one state may carry the positive/verified affordance.
	assert.equal(all.filter((s) => s.tone === "verified").length, 1);
});

test("a missing or empty trace degrades to pending, never to anchored", () => {
	for (const t of [undefined, null, {}]) {
		assert.equal(anchorState(t).state, "anchor_pending");
	}
});

// ── The components actually use it ───────────────────────────────────────
//
// Confirmed to FAIL against the pre-fix tree: Portfolio.jsx and Reasoning.jsx
// both carried their own inline `is_verified` ternary and neither imported
// anchorState.

test("Portfolio.jsx derives the badge from anchorState, not its own ternary", () => {
	const portfolio = src("components/Portfolio.jsx");
	assert.ok(portfolio.includes('import { anchorState } from "../trace-binding"'));
	assert.ok(portfolio.includes("anchorState(t)"));
	// The old two-state ternary and its hardcoded copy must be gone.
	assert.ok(
		!portfolio.includes("{t.is_verified ? ("),
		"the inline is_verified ternary is still deciding the anchoring claim",
	);
	assert.ok(
		!/title="Trace hashed \+ persisted off-chain; on-chain anchor pending/.test(portfolio),
		"the hardcoded 'anchor pending' copy is still inline — a skip would still get it",
	);
});

test("Reasoning.jsx derives the badge from anchorState, not its own ternary", () => {
	const reasoning = src("components/Reasoning.jsx");
	assert.ok(reasoning.includes("anchorState(t)"));
	assert.ok(
		!reasoning.includes("t.is_verified ? ("),
		"the inline is_verified ternary is still deciding the anchoring claim",
	);
	// The old badge said the bare word "verified" for an anchor that was never
	// re-hashed. It must not come back.
	assert.ok(
		!/w-3 h-3" \/> verified</.test(reasoning),
		"the bare 'verified' badge is back on a row that compared no hashes",
	);
});

test("the skip copy matches the wording the #714 branch settled on", () => {
	// That branch made SKIP honest in the Portfolio feed with this exact
	// label + tooltip. Keeping one copy of the strings, in the shared helper,
	// is what stops the two surfaces from wording the same fact differently.
	const a = anchorState({ decision_type: "skip" });
	assert.equal(a.label, "not anchored (no trade to bind)");
	assert.equal(
		a.title,
		"No trade was made, so there is nothing for an on-chain commitment to bind. The trace is hashed and persisted off-chain; no anchor is attempted or pending.",
	);
});

test("anchorState carries #714's Portfolio copy forward verbatim", () => {
	// Portfolio.jsx's inline three-state ternary — what #714 landed on main —
	// was replaced by anchorState during the rebase. That resolution is only
	// lossless if the shared helper reproduces main's strings and icons
	// EXACTLY; a reworded tooltip would be a silent product-copy regression
	// smuggled in through a merge conflict. Pinned character-for-character.
	const anchored = anchorState({ is_verified: true });
	assert.equal(anchored.label, "anchored on Arc");
	assert.equal(anchored.icon, "i-lucide-check-circle");
	assert.equal(anchored.tone, "verified");

	const skip = anchorState({ decision_type: "skip" });
	assert.equal(skip.icon, "i-lucide-skip-forward");

	const pending = anchorState({ decision_type: "rebalance" });
	assert.equal(pending.label, "anchor pending");
	assert.equal(pending.icon, "i-lucide-clock");
	assert.equal(
		pending.title,
		"Trace hashed + persisted off-chain; on-chain anchor pending (registry write didn't complete yet — usually transient).",
	);
});

test("the ordering fix survives: an anchored skip is not relabelled by its type", () => {
	// main's #714 ternary tested is_verified before decision_type, so an
	// anchored skip read "anchored on Arc". anchorState must keep that
	// precedence AND extend it to arc_tx_hash, which main did not consider.
	assert.equal(anchorState({ decision_type: "skip", is_verified: true }).state, "anchored");
	assert.equal(anchorState({ decision_type: "skip", arc_tx_hash: "0xabc" }).state, "anchored");
});

// ── Which rows may offer a follow-back ───────────────────────────────────

test("referencedStrategyId ignores construction traces, which cite papers", () => {
	// `strategies_referenced` is named for strategy ids and does not uniformly
	// hold them: api/strategies_routes.py writes arXiv ids and paper anchors
	// into the same field on CONSTRUCTION traces. Linking those would deep-link
	// the reader to a passport for an arXiv id — a 404 dressed as provenance.
	assert.equal(
		referencedStrategyId({ decision_type: "construction", strategies_referenced: ["2301.00001"] }),
		null,
	);
	assert.equal(
		referencedStrategyId({
			decision_type: "construction",
			strategies_referenced: ["arxiv:2301.00001#momentum"],
		}),
		null,
	);
});

test("referencedStrategyId returns the id for every agent decision type", () => {
	// Must agree with STRATEGY_REFERENCE_DECISION_TYPES in
	// backend/archimedes/services/redis_state.py — a row that offers the
	// button is exactly a row that strategy's scoped listing returns.
	assert.deepEqual(
		[...STRATEGY_REFERENCE_DECISION_TYPES].sort(),
		["rebalance", "regime_change", "rotation", "skip"],
	);
	for (const decision_type of STRATEGY_REFERENCE_DECISION_TYPES) {
		assert.equal(
			referencedStrategyId({ decision_type, strategies_referenced: ["strat-1"] }),
			"strat-1",
		);
	}
});

test("referencedStrategyId returns null rather than guessing on an odd shape", () => {
	for (const strategies_referenced of [undefined, null, [], {}, [null], [""], 7]) {
		assert.equal(
			referencedStrategyId({ decision_type: "rebalance", strategies_referenced }),
			null,
			`guessed a strategy id from ${JSON.stringify(strategies_referenced)}`,
		);
	}
	assert.equal(referencedStrategyId(undefined), null);
	// A bare string is a whole id, not a list to index into.
	assert.equal(
		referencedStrategyId({ decision_type: "rebalance", strategies_referenced: "strat-1" }),
		"strat-1",
	);
});

test("Reasoning.jsx gates the passport button on referencedStrategyId", () => {
	const reasoning = src("components/Reasoning.jsx");
	assert.ok(reasoning.includes("referencedStrategyId(t) && onNavigate"));
	assert.ok(
		!/t\.strategies_referenced\?\.\[0\] && onNavigate/.test(reasoning),
		"the button links strategies_referenced[0] raw — an arXiv id on a construction trace",
	);
});

test("the Reasoning page's intro copy describes buttons that exist", () => {
	const reasoning = src("components/Reasoning.jsx");
	// It used to promise "→ Strategy in Library", a button gated on a field
	// the API never sends — so the promise was unreachable on every row.
	assert.ok(!reasoning.includes("→ Strategy in Library"));
	assert.ok(reasoning.includes("→ Strategy passport"));
	// And it now explains the skip state rather than leaving the reader to
	// guess whether an unanchored row is a failure.
	assert.ok(reasoning.includes("not anchored (no trade to bind)"));
});

test("BOTH intros on the Reasoning page were corrected, not just the panel's", () => {
	// The page has two intro paragraphs: the <Reasoning> page header and the
	// <OnChainTraces> panel blurb. Only the panel's was updated when the button
	// changed, so the page header went on promising a follow-back "to the
	// source strategy in the Library" while the button deep-linked the
	// passport. The earlier version of this test asserted on the file as a
	// whole, which the panel's corrected copy satisfied on its own — so it
	// reported covering a paragraph it never read. Slice the header out and
	// assert against that text specifically.
	const reasoning = src("components/Reasoning.jsx");
	const start = reasoning.indexOf("export default function Reasoning(");
	assert.ok(start !== -1, "page component not found");
	const header = reasoning.slice(start);

	assert.ok(
		!/in the Library/.test(header),
		"the page header still sends the reader to the Library; the button opens the passport",
	);
	assert.match(header, /passport of the strategy it consulted/);

	// The panel blurb is the other half, and it must not overclaim either: the
	// button is absent on construction traces, which cite papers, not a strategy.
	const panelStart = reasoning.indexOf("function OnChainTraces(");
	const blurb = reasoning.slice(panelStart, start);
	assert.match(blurb, /on the trading decisions that\s*\n?\s*name one/);
	assert.match(blurb, /construction trace cites papers rather/);
});

test("no surface still points a trace back to the Library", () => {
	// One grep for the retired destination across every file that renders a
	// trace row, so the next surface to grow a follow-back cannot quietly
	// reintroduce it.
	for (const file of [
		"components/Reasoning.jsx",
		"components/StrategyReasoning.jsx",
		"components/Portfolio.jsx",
	]) {
		assert.ok(
			!/onNavigate\(\s*["']library["']/.test(src(file)),
			`${file} navigates a trace back to the Library`,
		);
	}
});

test("the dead t.strategy_id follow-back is gone from Reasoning.jsx", () => {
	// TraceResponse has never had a `strategy_id` field — the API emits
	// `strategies_referenced` — so the "→ Strategy" button rendered on zero
	// rows, and the page's own promise of a follow-back was unreachable.
	const reasoning = src("components/Reasoning.jsx");
	assert.ok(
		!/\{t\.strategy_id && onNavigate/.test(reasoning),
		"the button still gates on a field the API never sends",
	);
	assert.ok(reasoning.includes("referencedStrategyId(t) && onNavigate"));
});

// ── The passport panel ───────────────────────────────────────────────────

test("StrategyReasoning keeps the debate and the trading decisions separate", () => {
	const panel = src("components/StrategyReasoning.jsx");
	// The RENDERED headings, not the section-divider comments. This pair used to
	// read `"Generation debate"` / `"Trading decisions"`, which the engine-named
	// headings ("Strategy engine — generation debate") no longer contain in that
	// casing — the assertion survived only on the `// ── Generation debate ──`
	// comment above the function and went vacuous. Verified: blanking both
	// headings now fails here.
	assert.ok(panel.includes("Strategy engine — generation debate"));
	assert.ok(panel.includes("Execution engine — trading decisions"));
	// A debate turn is anchored nowhere; it must not be rendered through the
	// anchoring badge that trace rows use.
	const debateStart = panel.indexOf("function GenerationDebate");
	const debateEnd = panel.indexOf("function TradingDecisions");
	assert.ok(debateStart !== -1 && debateEnd > debateStart);
	const debateBody = panel.slice(debateStart, debateEnd);
	assert.ok(
		!debateBody.includes("AnchorBadge"),
		"a debate turn must never render an anchoring claim",
	);
});

test("StrategyReasoning scopes its trace query to the strategy", () => {
	const panel = src("components/StrategyReasoning.jsx");
	assert.ok(panel.includes("/api/traces/?strategy_id=${encodeURIComponent(strategyId)}"));
	assert.ok(panel.includes("/api/strategies/${encodeURIComponent(strategyId)}/debate"));
});

test("empty states are honest and specific, not a generic 'nothing here'", () => {
	const panel = src("components/StrategyReasoning.jsx");
	assert.ok(panel.includes("No trading decisions recorded yet for this strategy."));
	assert.ok(panel.includes("No generation debate is shown here."));
	// Both explain WHY the absence exists rather than implying a failure.
	// Whitespace-tolerant: the sentence is one claim, wrapped across source lines.
	assert.match(panel, /curated\s+library\s+strategies\s+never\s+ran\s+one/);
	assert.match(panel, /once the autonomous agent runs a vault that holds it/);
	// The debate empty state used to assert ONE reason ("No debate transcript
	// for this strategy") when the 404 it renders has TWO: the transcript was
	// never persisted, OR the caller does not own the row
	// (is_strategy_reasoning_visible, #1557). Naming only the first made a
	// false statement to every non-owner reading a published strategy.
	assert.match(panel, /or this strategy is not yours/);
	assert.match(panel, /cannot tell you which of the two it is/);
});

test("the panel says what the filter actually matches, and what it cannot", () => {
	const panel = src("components/StrategyReasoning.jsx");
	// The backend scopes ?strategy_id= to decision types, because a
	// construction trace's strategies_referenced holds arXiv ids and paper
	// anchors. Copy that says "agent decisions that consulted this strategy"
	// while a whole class of trace can never match is an overclaim by omission.
	assert.match(panel, /rebalances, rotations, regime changes and skips/);
	assert.match(panel, /does not include the\s*\n?\s*strategy's own construction/);

	// And it must not call the list "anchored decisions": a SKIP is a decision
	// this list shows and by design has no anchor (#714).
	assert.ok(
		!/No anchored decisions yet/.test(panel),
		"the empty state calls every row anchored; skips are shown here and never anchor",
	);
	assert.match(panel, /a skip has no trade for an anchor to bind/);
});

test("the 'N of TOTAL' count is only rendered against a filtered total", () => {
	// `total` comes from the API, which now counts rows AFTER the empty_vault
	// drop and the #1556 ownership filter and windows the same list — so
	// "showing 20 of 57" cannot promise rows no page will ever contain. The
	// word "showing" is what makes the partial-page reading explicit.
	const panel = src("components/StrategyReasoning.jsx");
	assert.match(panel, /total > traces\.length \? ` \(showing \$\{traces\.length\} of \$\{total\}\)`/);
});

test("the Portfolio trace feed sends the caller's identity with the request", () => {
	// Since #1556 /api/traces/ is ownership-gated, so a bare `fetch` is an
	// ANONYMOUS read: the user's own vault traces would be filtered out of
	// their own activity feed. apiGet sends credentials + the wallet header.
	const portfolio = src("components/Portfolio.jsx");
	assert.ok(portfolio.includes('import { apiGet } from "../api"'));
	assert.ok(
		!/fetch\(`\$\{API_BASE\}\/api\/traces\//.test(portfolio),
		"the trace feed still reads /api/traces/ anonymously through a bare fetch",
	);
	assert.ok(portfolio.includes("apiGet(\n\t\t\t\t\t\t`/api/traces/?limit=20&vault_address="));
	// The stale claim that justified the bare fetch must be gone with it.
	assert.ok(
		!/\/api\/traces\/ has no auth/.test(portfolio),
		"the comment still says /api/traces/ has no auth — #1556 gated it",
	);
});

test("a 404 from either endpoint is an empty state, not an error banner", () => {
	// 404 is the honest "never persisted" answer from /debate, and the
	// visibility answer from the scoped trace listing. Rendering it as a red
	// failure would tell the user something broke when nothing did.
	const panel = src("components/StrategyReasoning.jsx");
	const guards = panel.match(/e\.status !== 404/g) || [];
	assert.equal(guards.length, 2, "both fetches must exempt 404 from the error path");
});

test("async regions announce themselves (role=status) and failures announce as alerts", () => {
	const panel = src("components/StrategyReasoning.jsx");
	assert.equal((panel.match(/role="status"/g) || []).length, 2);
	assert.equal((panel.match(/role="alert"/g) || []).length, 2);
	// The trace list on Reasoning is async too.
	assert.ok(src("components/Reasoning.jsx").includes('role="status">Loading traces…'));
});

test("the passport mounts the panel unconditionally", () => {
	// Its empty states are load-bearing product copy. A panel that vanishes
	// when there is nothing to show teaches the reader provenance is optional.
	const passport = src("components/StrategyPassport.jsx");
	assert.ok(passport.includes('import StrategyReasoning from "./StrategyReasoning"'));
	assert.match(
		passport,
		/<StrategyReasoning strategyId=\{strategyId\} onNavigate=\{onNavigate\} \/>/,
	);
});

// ── #1636: debate claims carry paper attribution, in two shapes ────────────

test("StrategyReasoning renders BOTH claim shapes — the legacy string and the attributed dict", () => {
	// Claims used to be plain strings; since the debate carries paper
	// attribution they are `{claim, candidate_id, arxiv_ids}` dicts. Rows
	// persisted before the change are still strings and still render, so the
	// panel must read the text through a helper rather than interpolating the
	// claim directly — `{claim}` on a dict renders as a React child error.
	// The turn renderer moved to ./DebatePaperVerdicts so the passport and the
	// live generation stream draw the same rows from the same shapes; the rule
	// is unchanged, the file carrying it is not.
	const panel = src("components/DebatePaperVerdicts.jsx");
	assert.match(panel, /export function claimText/);
	assert.match(panel, /typeof claim === "string"/);
	assert.match(panel, /claimText\(claim\)/);
	assert.ok(
		!/<li key=\{j\}>\{claim\}<\/li>/.test(panel),
		"the raw-claim interpolation cannot survive the dict shape",
	);
	// …and the passport reaches it through that shared renderer rather than
	// growing a second copy.
	const passportPanel = src("components/StrategyReasoning.jsx");
	assert.match(passportPanel, /from "\.\/DebatePaperVerdicts"/);
	assert.match(passportPanel, /<DebateTurn\b/);
});

test("StrategyReasoning says so when a claim is attributed to no listed paper", () => {
	// An empty arxiv_ids array is meaningful data, not missing data: the
	// backend guard keeps an unattributable claim rather than deleting it, so
	// the panel must show that it was unattributed instead of rendering it
	// indistinguishably from a grounded one.
	const panel = src("components/DebatePaperVerdicts.jsx");
	assert.match(panel, /not attributed to a listed paper/);
	assert.match(panel, /arXiv:\$\{id\}/);
});
