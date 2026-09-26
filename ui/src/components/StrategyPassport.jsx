import { useEffect, useState } from "react";
import CreateVaultModal from "./CreateVaultModal";
import { ROADMAP_SURFACES_ENABLED } from "../featureFlags.js";
import { passportBackPage, passportBackLabel } from "../routes.js";
import RigorStrictnessControl, { levelLabel } from "./RigorStrictnessControl";
import { apiGet, apiPostWithMeta } from "../api";
import StrategyReasoning from "./StrategyReasoning";
import MetricValue from "./MetricValue";
import { useRigorStrictness, BADGE_LEVEL } from "../hooks/useRigorStrictness";
import {
	NOT_MEASURED,
	NOT_MEASURED_HINT,
	deriveGenerationCostView,
	formatDuration,
	formatTokenCount,
	quoteLabel,
	quoteNote,
	stageLabel,
	tokensLabel,
	usageNote,
} from "../generationCost.js";
import {
	isUnknownRigorGateStatus,
	warnUnknownRigorGateStatus,
	UNKNOWN_RIGOR_LABEL,
	UNKNOWN_RIGOR_TITLE,
} from "../rigorGateStatus.js";
import { statusTag, statusLabel, statusTitle } from "../libraryStatus.js";
import { formatStrategySpec, tokenizeJson } from "../strategySpec.js";
import { paperAttributionHeader } from "../paperAttribution.js";
import {
	hasSharpeReconciliation,
	rigorBarClause,
} from "../sharpeReconciliation.js";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// /strategy/:id deep-link route. Renders the full passport per
// docs/specs/strategy-passport-spec.md — bigger, scrollable version of the row
// expansion that lives on /library. Includes the Deploy CTA that opens
// CreateVaultModal (Phase 4 scaffold).
//
// Reachable from: Library row title click (deep-link), Reasoning trace
// "→ Strategy in Library" follow-back, FusionResult "Open in Library →" CTA.

// No local number formatter lives here any more. Every metric this file renders
// goes through MetricValue / metricDomain.js (#1651) — see the note in
// Strategies.jsx; ui/test/metric-domain.test.js fails if `fmt`/`fmtPct` return.

// ── Source-papers table (#1646) ────────────────────────────────────────────
//
// Every paper feeding a strategy belongs in ONE table with fixed columns, not
// a stack of cards that grows unbounded. The passport spec has said so since
// day one (docs/specs/strategy-passport-spec.md: "All N PaperRefs as a table
// (single-paper strategies show one row; fusion strategies show N)"; "DO NOT
// collapse fusion strategies to a scalar paper field for UI convenience …
// the UI table is the right primitive"). What shipped instead was two
// divergent card layouts — one for `papers.length > 1`, one scalar-field
// fallback below it — which is why a 6-paper strategy pushed the backtest
// panel off the bottom of the screen.
//
// These helpers are deliberately defensive about how EMPTY the data is today.
// Authors, venue, year, DOI, citation count and contribution are structurally
// NULL for every generated row on `main` right now: the generation path stores
// arXiv ids and titles and nothing else, and `contribution` has no writer at
// all. Filling those columns is issue #1637's job (the association contract);
// this table's job is to render honestly whatever #1637 has landed at read
// time. So: blanks are em-dashes, never zeroes or "Unknown", and a footnote
// names the columns that are empty for EVERY row so a reader does not conclude
// a paper has no authors when the truth is that nobody recorded them.

function fmtAuthors(authors) {
	const list = Array.isArray(authors) ? authors.filter(Boolean) : [];
	if (list.length === 0) return null;
	return list.length > 3 ? `${list.slice(0, 3).join(", ")} et al.` : list.join(", ");
}

function paperByline(p) {
	const parts = [fmtAuthors(p.authors), p.venue || null, p.year != null ? String(p.year) : null];
	return parts.filter(Boolean).join(" · ") || null;
}

/** Normalized paper rows for the table, newest contract first.
 *
 * `papers[]` is the contract. The legacy `paper_*` scalars are read ONLY when
 * `papers[]` is empty — a payload shape old enough to predate the list, or a
 * curated row whose passport carries scalars but no refs. Reading them as a
 * fallback rather than as a separate layout is what collapses the two
 * divergent card branches into one table.
 */
function paperRows(s) {
	const refs = Array.isArray(s.papers) ? s.papers : [];
	if (refs.length > 0) return refs;

	const legacy = {
		arxiv_id: s.paper_arxiv_id ?? null,
		title: s.paper_title ?? "",
		authors: s.paper_authors ?? [],
		venue: s.paper_venue ?? null,
		year: s.paper_year ?? null,
		doi: s.paper_doi ?? null,
		citation_count: s.paper_citation_count ?? null,
		contribution: null,
	};
	const hasAnything =
		Boolean(legacy.arxiv_id) ||
		Boolean((legacy.title || "").trim()) ||
		Boolean(legacy.doi) ||
		legacy.year != null;
	return hasAnything ? [legacy] : [];
}

// Which columns are empty across EVERY row. Computed rather than hardcoded so
// the footnote shrinks by itself as #1637 backfills each field — a hardcoded
// "authors are never recorded" line would become a false claim the day it is.
const COLUMN_LABELS = {
	byline: "authors, venue and year",
	source: "arXiv id and DOI",
	citations: "citation counts",
	contribution: "per-paper contribution",
};

function blankColumns(rows) {
	const seen = { byline: false, source: false, citations: false, contribution: false };
	for (const p of rows) {
		if (fmtAuthors(p.authors) || p.venue || p.year != null) seen.byline = true;
		if (p.arxiv_id || p.doi) seen.source = true;
		if (p.citation_count != null) seen.citations = true;
		if ((p.contribution || "").trim()) seen.contribution = true;
	}
	return Object.keys(COLUMN_LABELS).filter((k) => !seen[k]);
}

function joinWords(items) {
	if (items.length <= 1) return items.join("");
	return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

function PapersTable({ papers, methodologyHash, distinctMechanismPapers }) {
	const rows = papers;
	const blanks = blankColumns(rows);
	// #1769: the heading counts CITATIONS and ATTRIBUTIONS separately. It used
	// to read "Fused from N papers", which presented a citation count as fusion
	// depth — on the strategy that surfaced this, all 5 contribution cells were
	// em-dashes. The sub-line is no longer conditional on `rows.length > 1`
	// either: it was the claim "One methodology synthesized from every row
	// below", which is the same overclaim in a sentence.
	const attribution = paperAttributionHeader(rows, distinctMechanismPapers, "passport");
	// `null` only for an empty `rows`, which the one call site already routes to
	// its own "no source papers are recorded" panel. Guarded anyway so a second
	// call site can never white-screen the passport on an empty list.
	if (!attribution) return null;

	return (
		<div className="card passport-panel passport-papers">
			<div className="passport-papers__head">
				<div className="label">{attribution.heading}</div>
				<p className="caption text-[var(--text-3)]">{attribution.note}</p>
			</div>

			<div className="passport-papers__scroll">
				<table className="passport-papers__table">
					<caption className="sr-only">
						Research papers this strategy was derived from
					</caption>
					<thead>
						<tr>
							<th scope="col">Paper</th>
							<th scope="col">Authors · venue · year</th>
							<th scope="col">Source</th>
							<th scope="col" className="passport-papers__num">
								Cited by
							</th>
							<th scope="col">Contribution</th>
						</tr>
					</thead>
					<tbody>
						{rows.map((p, idx) => {
							const title = (p.title || "").trim();
							const byline = paperByline(p);
							const contribution = (p.contribution || "").trim();
							return (
								<tr key={p.arxiv_id || p.doi || `${title}-${idx}`}>
									<th scope="row" className="passport-papers__title">
										{/* An unrecorded title falls back to the arXiv id rather
										    than rendering an empty pair of quotation marks, which
										    is what the old card layout printed for a null title. */}
										{title || p.arxiv_id || "—"}
									</th>
									<td>{byline || "—"}</td>
									<td className="passport-papers__source">
										{p.arxiv_id ? (
											<a
												href={`https://arxiv.org/abs/${p.arxiv_id}`}
												target="_blank"
												rel="noreferrer"
												className="mono"
											>
												arxiv:{p.arxiv_id} ↗
											</a>
										) : null}
										{p.doi ? (
											<a
												href={`https://doi.org/${p.doi}`}
												target="_blank"
												rel="noreferrer"
												className="mono"
											>
												doi:{p.doi} ↗
											</a>
										) : null}
										{!p.arxiv_id && !p.doi ? "—" : null}
									</td>
									<td className="passport-papers__num mono">
										{p.citation_count != null ? p.citation_count : "—"}
									</td>
									<td>{contribution || "—"}</td>
								</tr>
							);
						})}
					</tbody>
				</table>
			</div>

			{blanks.length > 0 && (
				<p className="caption passport-papers__note">
					Blank cells are unrecorded, not zero — {joinWords(blanks.map((k) => COLUMN_LABELS[k]))}{" "}
					{blanks.length === 1 ? "is" : "are"} not stored for these references yet.
				</p>
			)}

			{methodologyHash && (
				<div className="caption mono text-[var(--text-4)] passport-papers__hash">
					hash: {methodologyHash.slice(0, 24)}…
				</div>
			)}
		</div>
	);
}

// ── Generated DSL (#1646) ──────────────────────────────────────────────────
//
// The executable spec was invisible on this page: the API stripped it before
// the boundary, so the only thing a reader could inspect was prose. Rendering
// it is the difference between "trust the writeup" and "read the rules".
//
// `strategy_spec` is null for three different reasons and the caption below
// says so instead of leaving a reader to guess: the row genuinely has no spec
// (curated strategies bind a code path instead; rows generated before the
// column existed have nothing), or the server redacted it because the spec is
// REASONING and the reader is not the owner.
function StrategySpecPanel({ spec }) {
	const formatted = formatStrategySpec(spec);

	return (
		<div className="card passport-panel passport-dsl">
			<div className="passport-dsl__head">
				<div className="label">Generated DSL</div>
				<p className="caption text-[var(--text-3)]">
					The machine-readable spec the backtest and the live evaluator both
					interpret — not a restatement of the methodology above.
				</p>
			</div>
			{formatted ? (
				<>
					<pre className="passport-dsl__code" tabIndex={0}>
						<code>
							{tokenizeJson(formatted.text).map((tok, i) => (
								<span key={i} className={`passport-dsl__t-${tok.kind}`}>
									{tok.text}
								</span>
							))}
						</code>
					</pre>
					{formatted.truncated && (
						<p className="caption passport-dsl__note">
							Showing the first {formatted.text.length.toLocaleString()} of{" "}
							{formatted.totalChars.toLocaleString()} characters.
						</p>
					)}
				</>
			) : (
				<p className="caption passport-dsl__note">
					No spec to show. Either this strategy carries none — curated
					strategies bind a code path instead, and rows generated before the
					DSL landed have nothing stored — or it is withheld because the
					executable spec is visible only to the strategy&rsquo;s owner.
				</p>
			)}
		</div>
	);
}

// The status pill's class, its words and its tooltip are `../libraryStatus.js`'s
// — this file defines none of them.
//
// Two local two-argument helpers used to live here. They read `status` and
// `passes_rigor_gate`, and nothing else, which left this surface the last place
// the BOOLEAN could still stand in for the verdict. The Library moved its pill
// onto the four-state `rigor_gate_status` (#1747); the passport imported only
// the shared demotion LABEL and kept its own two-arm logic — so it looked
// unified and was not. `passes_rigor_gate` is false for a PENDING row and for a
// DEGENERATE one too, by fail-closed design, so a live row no gate had ever
// graded painted its pill "Reference only — gate failed" — a graded-and-lost
// claim — a few pixels from this same header's rigor chip reading "rigor gate
// pending". One vocabulary, one module: a pill may say "gate failed" only when
// `rigor_gate_status === "fail"`; pending says "Not yet graded", degenerate
// says "Unevaluable — flat returns".
//
// Two pill COLOURS move as a result, on purpose. `validated` was green here and
// is accent in the shared helper; an unknown/`candidate` status was accent here
// and is muted there. Green is reachable from exactly one place now — a `live`
// row with a literal `true` verdict — which is the whole point of the shared
// module: a second green door on the passport would re-open the gap this
// closes. No graded state's WORDS change.

// Derive a brief-specific display title. The unified passport table doesn't
// persist `strategy_name`, but Pi's #336 fix ensures methodology_summary
// always starts with "For brief 'XXX': YYY" — extract XXX as the title so
// each generated strategy reads differently. Falls back to paper title for
// legacy strategies and curated ones (whose methodology_summary doesn't
// follow that template).
function deriveDisplayTitle(s) {
	const m = (s.methodology_summary || "").match(
		/^For brief ['"](.+?)['"]\s*:/i,
	);
	if (m && m[1]) return m[1].trim();
	return s.paper_title || s.id;
}

function regimeChip(tag) {
	if (tag === "bull") {
		return {
			label: (
				<>
					<span className="i-lucide-trending-up w-3.5 h-3.5" /> Bull regime
				</>
			),
			cls: "tag-positive",
		};
	}
	if (tag === "bear") {
		return {
			label: (
				<>
					<span className="i-lucide-trending-down w-3.5 h-3.5" /> Bear regime
				</>
			),
			cls: "tag-negative",
		};
	}
	return null;
}

// Return-source classification (T2.5) — the dominant economic source of the
// strategy's return. Maps the backend enum to a human label + tag colour. A
// durable, compensated source (risk_premium / productive_growth) reads neutral;
// mispricing reads accent (decays as it crowds); noise reads muted (no source).
const RETURN_SOURCE_META = {
	risk_premium: { label: "Risk premium", cls: "tag-accent" },
	mispricing: { label: "Mispricing", cls: "tag-accent" },
	productive_growth: { label: "Productive growth", cls: "tag-positive" },
	noise: { label: "Noise", cls: "tag-muted" },
};

function returnSourceChip(src) {
	return RETURN_SOURCE_META[src] || RETURN_SOURCE_META.noise;
}

export default function StrategyPassport({
	strategyId,
	onNavigate,
	walletAddr,
	user = null,
}) {
	const [strategy, setStrategy] = useState(null);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [deployOpen, setDeployOpen] = useState(false);
	// Per-user deploy strictness (localStorage-backed, tab-synced). Gates the
	// Deploy CTA and is passed to the deploy flow so the server enforces it too.
	const [level, setLevel] = useRigorStrictness();
	// The strategy's rigor-ladder verdict: min_passing_level + blocked_by_floor,
	// computed live over the whole-library cohort. Curated strategies resolve here;
	// generated strategies 404 → we fall back to the badge boolean.
	const [gate, setGate] = useState(null);

	useEffect(() => {
		let cancelled = false;
		if (!strategyId) {
			setError("No strategy id in URL.");
			setLoading(false);
			return;
		}
		fetch(`${API_BASE}/api/strategies/${encodeURIComponent(strategyId)}`)
			.then((r) =>
				r.ok
					? r.json()
					: r.text().then((t) => {
							throw new Error(t || r.statusText);
						}),
			)
			.then((data) => {
				if (!cancelled) setStrategy(data);
			})
			.catch((e) => {
				if (!cancelled) setError(e.message || "Failed to load strategy");
			})
			.finally(() => {
				if (!cancelled) setLoading(false);
			});
		return () => {
			cancelled = true;
		};
	}, [strategyId]);

	useEffect(() => {
		let cancelled = false;
		setGate(null);
		if (!strategyId) return;
		// min_passing_level is strictness-independent, so one call (default level)
		// gives the whole ladder.
		//
		// The old comment here said "404 (generated strategy not in the curated
		// cohort) is expected — we fall back to the badge boolean below". That
		// stopped being true when `_generated_strategy_rigor` landed:
		// `evaluate_strategy_rigor` (backend/archimedes/api/selection_bias_routes.py)
		// falls through to it and runs `run_rigor_gate` LIVE on a generated row's
		// own persisted returns, so a generated id gets a 200 with a real ladder.
		// What this call is, precisely: the DEPLOY ladder (which strictness levels
		// this strategy clears), computed live, and the same source the vault
		// deploy gate reads. The BADGE beside it is the stored verdict of record
		// (docs/adr/rigor-verdict-of-record.md) — graded once, at backtest time.
		// The two can differ in vintage, and that is a known, named seam: the
		// badge is the verdict, the ladder is the deploy check. A 404 is still
		// handled (an id neither path resolves) and still falls back to the badge.
		fetch(
			`${API_BASE}/api/selection-bias/gate/${encodeURIComponent(strategyId)}`,
		)
			.then((r) => (r.ok ? r.json() : null))
			.then((data) => {
				if (!cancelled && data) setGate(data);
			})
			.catch(() => {
				/* fall back to badge */
			});
		return () => {
			cancelled = true;
		};
	}, [strategyId]);

	// Exhaustiveness alarm for rigor_gate_status, shared with Strategies.jsx so
	// both surfaces answer a fifth state identically (#1358). Declared up here,
	// above the `loading` / `error` early returns below, because a hook after a
	// conditional return is a hook that sometimes does not run.
	const unknownRigor = isUnknownRigorGateStatus(strategy?.rigor_gate_status);
	useEffect(() => {
		if (unknownRigor)
			warnUnknownRigorGateStatus(
				strategy?.rigor_gate_status,
				"StrategyPassport",
			);
	}, [unknownRigor, strategy?.rigor_gate_status]);

	if (loading) return <div className="caption">Loading strategy passport…</div>;

	if (error || !strategy) {
		return (
			<div className="max-w-[640px]">
				<button
					className="btn btn-outline btn-sm mb-3"
					onClick={() => onNavigate(passportBackPage(user))}
				>
					{passportBackLabel(user)}
				</button>
				<div className="info-box warning">
					Could not load strategy: {error || "unknown error"}
				</div>
			</div>
		);
	}

	const s = strategy;
	// Normalized once here, not per-render-branch: the papers table and the
	// empty state must agree on what "has papers" means (#1646).
	const papers = paperRows(s);
	const passingRigor = s.passes_rigor_gate === true;
	// #1358: rigor_gate_status carries the honest four-state badge
	// ("pass"|"fail"|"pending"|"degenerate") the API has served since #1184 —
	// nothing under ui/src/ read it before this fix, so every strategy with
	// zero statistics computed (rigor_gate_status === "pending") rendered as
	// though it had FAILED the gate (passes_rigor_gate is false for pending
	// too, by fail-closed design). Checked before any passes_rigor_gate
	// branch below so "never evaluated" can't be mistaken for "evaluated and
	// lost".
	const rigorPending = s.rigor_gate_status === "pending";
	// #1358 round-3: the fourth state. Same NEUTRAL treatment as pending — never
	// the "not Verified" / "not passed" wording, because nothing was measurable
	// to fail — but its own sentence, since a degenerate row HAS persisted
	// returns and pending's "not yet evaluated" would be a fresh false claim.
	// Copy follows agents/portfolio_agent.py `_format_strategies`, which already
	// tells the LLM exactly this.
	const rigorDegenerate = s.rigor_gate_status === "degenerate";
	// ── Deploy gating at the user's chosen strictness ──
	const badgePass = s.passes_rigor_gate === true;
	// Lowest level (1..5) at which this strategy is deployable — from the live
	// ladder when loaded, else the level-1 badge boolean (curated strategies load
	// the ladder; generated strategies fall back to the badge). null = deployable
	// at no level; undefined = unknown yet (pending backtest).
	const minLevel = gate
		? gate.min_passing_level
		: badgePass
			? BADGE_LEVEL
			: s.passes_rigor_gate === false
				? null
				: undefined;
	// #1358 round-3: a zero-variance series leaves dsr_p_value and oos_sharpe at
	// None, and RigorGateResult.blocked_by_floor treats a None on either as a
	// floor failure — so the gate returns blocked_by_floor=true for a strategy
	// no floor ever measured. Every consumer of blockedByFloor below says "fails
	// an always-on correctness floor", which is an assertion about a measurement
	// that did not happen. Suppress that claim here (once, so all five sites
	// agree) and let the degenerate sentence beside it carry the honest reason.
	//
	// This loosens no permission: `deployable` additionally requires
	// `minLevel != null`, and min_passing_level is null for a degenerate row, so
	// the deploy button stays disabled either way. Only the EXPLANATION changes.
	const gateDegenerate = gate ? gate.degenerate === true : false;
	const blockedByFloor = gate
		? gate.blocked_by_floor === true && !gateDegenerate
		: false;
	const deployable = minLevel != null && minLevel <= level && !blockedByFloor;
	const needsHigherLevel =
		minLevel != null && minLevel > level && !blockedByFloor;
	const belowBadge = deployable && minLevel > BADGE_LEVEL;
	const paperCite = [
		s.paper_authors?.[0]?.split(" ").pop(),
		s.paper_year && `(${s.paper_year})`,
	]
		.filter(Boolean)
		.join(" ");
	const displayTitle = deriveDisplayTitle(s);
	const regime = regimeChip(s.regime_tag);
	// Show the anchor-paper title as a sub-line only when the derived title is
	// brief-specific (i.e. we did extract it from methodology_summary) AND a
	// paper title exists. Otherwise the sub-line would duplicate the heading.
	const paperAnchorLine =
		displayTitle !== s.paper_title && s.paper_title
			? `Anchored on: ${s.paper_title}`
			: null;

	return (
		<div className="passport-page">
			<button
				className="btn btn-outline btn-sm app-back-link"
				onClick={() => onNavigate(passportBackPage(user))}
			>
				{passportBackLabel(user)}
			</button>

			{/* Header */}
			<header className="app-page-heading passport-heading fade-up fade-up-1">
				<p className="app-eyebrow">Strategy passport</p>
				<h1>{displayTitle}</h1>
				{paperAnchorLine && (
					<div className="caption mb-1" style={{ color: "var(--text-3)" }}>
						{paperAnchorLine}
					</div>
				)}
				<div className="caption mb-3">
					{paperCite || (s.paper_year ? `(${s.paper_year})` : "")}
					{s.paper_venue && <> · {s.paper_venue}</>}
				</div>
				<div className="flex gap-2 items-center flex-wrap">
					{regime && (
						<span className={`tag ${regime.cls}`}>{regime.label}</span>
					)}
					<span
						className={`tag ${statusTag(s.status, s.passes_rigor_gate, s.rigor_gate_status)}`}
						title={statusTitle(s.status, s.passes_rigor_gate, s.rigor_gate_status)}
					>
						{statusLabel(s.status, s.passes_rigor_gate, s.rigor_gate_status)}
					</span>
					{unknownRigor ? (
						<span className="tag tag-muted" title={UNKNOWN_RIGOR_TITLE}>
							rigor gate {UNKNOWN_RIGOR_LABEL}
						</span>
					) : rigorDegenerate ? (
						<span
							className="tag tag-muted"
							title="DEGENERATE — the persisted return series is zero-variance (broken data or a zero-trade backtest), not a real evaluation"
						>
							rigor gate unevaluable
						</span>
					) : rigorPending ? (
						<span className="tag tag-muted">rigor gate pending</span>
					) : s.passes_rigor_gate === true ? (
						<span className="tag tag-positive inline-flex items-center gap-1">
							<span className="i-lucide-check w-3.5 h-3.5" /> rigor gate passed
						</span>
					) : (
						s.passes_rigor_gate === false && (
							<span className="tag tag-muted">rigor gate not passed</span>
						)
					)}
					{(s.papers || []).length > 1 ? (
						<span className="tag tag-accent inline-flex items-center gap-1">
							<span className="i-lucide-layers w-3.5 h-3.5" />
							{/* "fused papers" was the same citation-count-as-fusion-depth
							    claim the table heading carried (#1769). The chip is a
							    count, so it says what it counts; the attribution split
							    lives in the table heading, which has room for it. */}
							{s.papers.length} papers cited
						</span>
					) : (
						s.paper_arxiv_id && (
							<a
								href={`https://arxiv.org/abs/${s.paper_arxiv_id}`}
								target="_blank"
								rel="noreferrer"
								className="tag tag-muted"
								style={{
									fontFamily: "var(--mono, monospace)",
									fontSize: "0.75rem",
								}}
							>
								arxiv:{s.paper_arxiv_id} ↗
							</a>
						)
					)}
				</div>
			</header>

			<div className="passport-workspace">
				<aside className="passport-authority">
					{/* Deploy CTA — top, gated on rigor-at-your-level + wallet. The card
					    leads into the vault surface, so it hides with it (#1266). */}
					{ROADMAP_SURFACES_ENABLED && (
					<div className="card passport-deploy fade-up fade-up-2">
						<div className="flex-1 min-w-[240px]">
							<div className="label mb-1">Deploy as a vault</div>
							<p className="caption leading-relaxed">
								Time-bound, non-custodial execution. Funds stay in an ERC-4626
								vault you control; the agent has rebalance authority only, no
								withdraw.
								{!walletAddr && (
									<> Connect a wallet (top right) to enable deployment.</>
								)}
								{blockedByFloor && (
									<>
										{" "}
										This strategy fails an always-on correctness floor
										(look-ahead / positive OOS / DSR ≥ 0.50) — it cannot be
										deployed at any strictness level.
									</>
								)}
								{gateDegenerate && (
									<>
										{" "}
										This strategy's persisted return series is zero-variance
										(broken data or a zero-trade backtest), so the rigor gate had
										nothing to measure — it was not graded and lost, it was never
										gradeable. It cannot be deployed until a real backtest
										replaces that series.
									</>
								)}
								{needsHigherLevel && (
									<>
										{" "}
										This strategy passes only at{" "}
										<strong>{levelLabel(null, minLevel)}</strong> (level{" "}
										{minLevel}) or riskier. Raise your strictness to deploy it.
									</>
								)}
								{belowBadge && !needsHigherLevel && (
									<>
										{" "}
										Deploying below the Archimedes Verified bar, at{" "}
										<strong>{levelLabel(null, level)}</strong> risk.
									</>
								)}
							</p>
						</div>
						<div className="flex flex-col items-end gap-2">
							<button
								className="btn btn-primary"
								onClick={() => setDeployOpen(true)}
								disabled={!walletAddr || !deployable}
								style={
									!walletAddr || !deployable
										? {
												opacity: 0.45,
												cursor: "not-allowed",
												filter: "grayscale(0.6)",
											}
										: undefined
								}
								title={
									!walletAddr
										? "Connect wallet to deploy"
										: gateDegenerate
											? "Zero-variance persisted return series — nothing for the rigor gate to measure; not a graded failure"
											: blockedByFloor
												? "Fails an always-on rigor floor — cannot deploy at any level"
												: needsHigherLevel
													? `Raise strictness to level ${minLevel} to deploy`
													: "Open deploy modal"
								}
							>
								Deploy as Vault →
							</button>
							{needsHigherLevel && (
								<button
									className="btn btn-outline btn-sm"
									onClick={() => setLevel(minLevel)}
									title={`Set your strictness to ${levelLabel(null, minLevel)}`}
								>
									Raise to {levelLabel(null, minLevel)} →
								</button>
							)}
						</div>
					</div>
					)}

					{/* Per-user strictness slider — the deploy gate above reads from this. */}
					<div className="passport-strictness fade-up fade-up-2">
						<RigorStrictnessControl level={level} onChange={setLevel} />
					</div>

					{/* Paper trading — the MVP act-on step. Account-owned and simulated
					    (POST /api/paper/deployments): no wallet, no rigor precondition,
					    free by design. The server allows duplicate deployments, so the
					    already-running state below is client-side dedupe UX. */}
					<PaperDeployCard
						strategyId={strategyId}
						user={user}
						onNavigate={onNavigate}
					/>

					{/* What the generation run that produced this strategy actually
					    consumed (#1326). Durable — read from the DB, not from the
					    job record, which expires an hour after the run ends. */}
					<GenerationCostCard record={s.generation_cost} />
				</aside>

				<div className="passport-evidence">
					{/* Methodology + source paper(s) */}
					{/* `passport-dense` (#1646) is the viewport-fit scope: it tightens
					    padding and type on THIS page's panels without touching the
					    shared `.passport-panel` rule other pages inherit. Every rule it
					    carries lives in the appended #1646 block at the end of
					    App.css. */}
					<div className="passport-sources passport-dense fade-up fade-up-3">
						{/* The user's own free-text ask (v8 Lane 3.3) — distinct from
						    Methodology below, which is the DERIVED writeup. Only the
						    single-strategy detail fetch populates this field, only for
						    the row's OWNER (the server redacts it for everyone else,
						    published rows included), and only when the brief is known —
						    so this renders for neither someone else's strategy, nor a
						    curated one, nor a legacy generated one the backfill could
						    not resolve. No viewer-side ownership check is needed or
						    wanted here: the redaction is the server's, and this guard
						    only declines to draw an empty card. That is what keeps the
						    "Your brief" copy literally true for whoever is reading. */}
						{s.brief_intent && (
							<div className="card passport-panel">
								<div className="label mb-2">Your brief</div>
								<p
									className="body leading-relaxed"
									style={{ fontStyle: "italic" }}
								>
									"{s.brief_intent}"
								</p>
							</div>
						)}
						<div className="card passport-panel">
							<div className="label mb-3">Methodology</div>
							<p className="body leading-relaxed">
								{s.methodology_summary || "—"}
							</p>
							<div className="mt-4 grid grid-cols-2 gap-3">
								<div>
									<div className="caption text-[var(--text-4)]">
										Position sizing
									</div>
									<div className="body capitalize">
										{s.position_sizing || "—"}
									</div>
								</div>
								<div>
									<div className="caption text-[var(--text-4)]">Rebalance</div>
									<div className="body capitalize">
										{s.rebalance_frequency || "—"}
									</div>
								</div>
								<div>
									<div className="caption text-[var(--text-4)]">
										Asset universe
									</div>
									<div className="body">
										{(s.asset_universe || []).join(", ") || "—"}
									</div>
								</div>
								{s.kelly_fraction != null && (
									<div>
										<div className="caption text-[var(--text-4)]">
											Kelly fraction
										</div>
										<div className="body mono">
											<MetricValue
												metric="kelly_fraction"
												value={s.kelly_fraction}
												row={s}
												surface="Passport"
											/>
										</div>
									</div>
								)}
							</div>
						</div>

						{/* ONE table for every paper count — the two divergent card
						    layouts (multi-paper stack / single-paper scalar card) are
						    gone. `paperRows` folds the legacy `paper_*` scalars in as a
						    fallback row, so a payload that predates `papers[]` still
						    renders, and a strategy with no recorded papers says so
						    instead of printing an empty pair of quotation marks. */}
						{papers.length > 0 ? (
							<PapersTable
								papers={papers}
								methodologyHash={s.methodology_hash}
								distinctMechanismPapers={s.distinct_mechanism_papers}
							/>
						) : (
							<div className="card passport-panel passport-papers">
								<div className="label mb-2">Source papers</div>
								<p className="caption">
									No source papers are recorded for this strategy.
								</p>
								{s.methodology_hash && (
									<div className="caption mono text-[var(--text-4)] passport-papers__hash">
										hash: {s.methodology_hash.slice(0, 24)}…
									</div>
								)}
							</div>
						)}

						<StrategySpecPanel spec={s.strategy_spec} />
					</div>

					{/* Backtest metrics */}
					<div className="card passport-panel passport-backtest fade-up fade-up-4">
						<div className="label mb-3">Backtest</div>
						{s.is_backtest_placeholder && (
							<div className="info-box mb-3" style={{ fontSize: "0.85rem" }}>
								Pre-backtest hypothesis — empirical metrics pending evaluation.
							</div>
						)}
						<div className="grid grid-cols-2 md:grid-cols-4 gap-3">
							{/* Every cell below goes through MetricValue so the passport
							    cannot render a number outside its metric's domain without
							    saying so, and an empty cell carries a reason rather than a
							    bare em-dash (#1651). */}
							<Metric
								label="Sharpe"
								value={
									<MetricValue
										metric="sharpe_ratio"
										value={s.sharpe_ratio}
										row={s}
										surface="Passport"
									/>
								}
								hint={
									s.sharpe_ci_lower != null && s.sharpe_ci_upper != null ? (
										<>
											[
											<MetricValue
												metric="sharpe_ci_lower"
												value={s.sharpe_ci_lower}
												row={s}
												surface="Passport"
											/>
											,{" "}
											<MetricValue
												metric="sharpe_ci_upper"
												value={s.sharpe_ci_upper}
												row={s}
												surface="Passport"
											/>
											]
										</>
									) : null
								}
							/>
							<Metric
								label="CAGR"
								value={
									<MetricValue
										metric="cagr"
										value={s.cagr}
										row={s}
										surface="Passport"
									/>
								}
							/>
							<Metric
								label="Max DD"
								value={
									<MetricValue
										metric="max_drawdown"
										value={s.max_drawdown}
										row={s}
										surface="Passport"
									/>
								}
							/>
							<Metric
								label="Calmar"
								value={
									<MetricValue
										metric="calmar_ratio"
										value={s.calmar_ratio}
										row={s}
										surface="Passport"
									/>
								}
							/>
							<Metric
								label="Sortino"
								value={
									<MetricValue
										metric="sortino_ratio"
										value={s.sortino_ratio}
										row={s}
										surface="Passport"
									/>
								}
							/>
							<Metric
								label="Win rate"
								value={
									<MetricValue
										metric="win_rate"
										value={s.win_rate}
										row={s}
										surface="Passport"
									/>
								}
							/>
							<Metric
								label="Trades"
								value={
									<MetricValue
										metric="total_trades"
										value={s.total_trades}
										row={s}
										surface="Passport"
									/>
								}
							/>
							<Metric
								label="ρ to SPY"
								value={
									<MetricValue
										metric="correlation_to_spy"
										value={s.correlation_to_spy}
										row={s}
										surface="Passport"
									/>
								}
							/>
						</div>
						{/* Raw vs deflated (#1769). The card above prints a Sharpe and
						    the Rigor card below prints a Deflated Sharpe beside a
						    pass/fail pill; nothing said they are the same edge measured
						    twice, so the gap read as two contradictory results. Both
						    figures go through MetricValue like every other number on this
						    page (#1651) — this sentence introduces no arithmetic and no
						    threshold of its own, and it renders only when BOTH numbers
						    exist, because the relationship is its entire subject. */}
						{hasSharpeReconciliation(s) && (
							<p className="caption mt-3 leading-relaxed text-[var(--text-3)]">
								Sharpe{" "}
								<strong>
									<MetricValue
										metric="sharpe_ratio"
										value={s.sharpe_ratio}
										row={s}
										surface="Passport"
									/>
								</strong>{" "}
								is the raw backtest figure; deflated for selection bias it is{" "}
								<strong>
									<MetricValue
										metric="deflated_sharpe_ratio"
										value={s.deflated_sharpe_ratio}
										row={s}
										surface="Passport"
									/>
								</strong>
								. The verdict below reads that deflation — not the raw
								Sharpe — alongside PBO, out-of-sample Sharpe and the
								look-ahead audit
								{rigorBarClause(s) ? `, ${rigorBarClause(s)}.` : "."}
							</p>
						)}
						{(s.backtest_start || s.backtest_end) && (
							<div className="caption mt-3 text-[var(--text-3)]">
								Window:{" "}
								<span className="mono">
									{(s.backtest_start || "").slice(0, 10)} →{" "}
									{(s.backtest_end || "").slice(0, 10)}
								</span>
							</div>
						)}
						{s.paper_claimed_sharpe != null && (
							<div className="caption mt-2">
								Paper-claimed Sharpe:{" "}
								<strong>
									<MetricValue
										metric="paper_claimed_sharpe"
										value={s.paper_claimed_sharpe}
										row={s}
										surface="Passport"
									/>
								</strong>{" "}
								· realized:{" "}
								<strong>
									<MetricValue
										metric="sharpe_ratio"
										value={s.sharpe_ratio}
										row={s}
										surface="Passport"
									/>
								</strong>{" "}
								{s.sharpe_ratio != null &&
									(() => {
										const ratio =
											s.paper_claimed_sharpe > 0.01
												? s.sharpe_ratio / s.paper_claimed_sharpe
												: null;
										return (
											<span
												className={
													ratio != null && ratio >= 0.5
														? "positive"
														: "negative"
												}
											>
												(
												{ratio != null
													? `${(ratio * 100).toFixed(0)}% of paper claim`
													: "—"}
												)
											</span>
										);
									})()}
							</div>
						)}
					</div>

					{/* Rigor gate */}
					<div className="card passport-panel passport-rigor fade-up fade-up-5">
						<div className="flex items-center justify-between flex-wrap gap-2 mb-3">
							<div className="label">
								Rigor verdict — selection-bias controls
							</div>
							<div className="flex items-center gap-2 flex-wrap">
								<span
									className={`tag inline-flex items-center gap-1 ${passingRigor ? "tag-positive" : "tag-muted"}`}
									title={
										unknownRigor
											? UNKNOWN_RIGOR_TITLE
											: rigorDegenerate
												? "DEGENERATE — the persisted return series is zero-variance (broken data or a zero-trade backtest), not a real evaluation"
												: undefined
									}
								>
									{/* Unevaluable states are tested FIRST, ahead of passingRigor:
									    a verdict nothing could compute must never be able to reach
									    a "Verified" or a "not Verified" claim through a stale
									    boolean. (Both are already false for these rows today —
									    this ordering is what keeps that true if the boolean ever
									    drifts.) */}
									{unknownRigor ? (
										UNKNOWN_RIGOR_LABEL
									) : rigorDegenerate ? (
										"unevaluable — zero-variance return series"
									) : passingRigor ? (
										<>
											<span className="i-lucide-check w-3.5 h-3.5" /> Verified
											(Conservative)
										</>
									) : rigorPending ? (
										"pending — not yet evaluated"
									) : (
										"not Verified"
									)}
								</span>
								{blockedByFloor ? (
									<span
										className="tag tag-negative"
										title="Fails an always-on correctness floor — look-ahead audit, positive OOS Sharpe, and DSR ≥ 0.50 — independent of strictness level; cannot be deployed at any level"
									>
										blocked — correctness floor
									</span>
								) : minLevel != null && minLevel > BADGE_LEVEL ? (
									<span className="tag tag-accent">
										deployable at {levelLabel(null, minLevel)}+
									</span>
								) : null}
							</div>
						</div>
						<div className="grid grid-cols-2 md:grid-cols-4 gap-3">
							<Metric
								label="DSR"
								value={
									<MetricValue
										metric="deflated_sharpe_ratio"
										value={s.deflated_sharpe_ratio}
										row={s}
										surface="Passport rigor"
									/>
								}
								hint={
									// Despite the wire field's legacy name (dsr_p_value), higher
									// is better here — a confidence, not a classical significance
									// statistic where lower is better (leaderboard_schemas.py).
									// "p = 0.93" reads as catastrophic to a reader expecting the
									// classical convention; it is in fact the passing case (#1358).
									s.dsr_p_value != null ? (
										<>
											confidence ={" "}
											<MetricValue
												metric="dsr_p_value"
												value={s.dsr_p_value}
												row={s}
												digits={3}
												surface="Passport rigor"
											/>
										</>
									) : null
								}
							/>
							{/* PBO of exactly 0 paired with missing DSR/OOS is almost always a
              placeholder, not a real measurement. Render "—" in that case so
              we don't show a fake 0.0% next to honest unknowns elsewhere. */}
							<Metric
								label="PBO"
								value={
									<MetricValue
										metric="pbo_score"
										// The placeholder case above is expressed by handing
										// MetricValue a null, so the cell picks up the SAME
										// pending/degenerate reason every other empty cell on
										// this page gets instead of a bare em-dash (#1651).
										value={
											s.pbo_score === 0 &&
											s.deflated_sharpe_ratio == null &&
											s.out_of_sample_sharpe == null
												? null
												: s.pbo_score
										}
										row={s}
										format="pct"
										surface="Passport rigor"
									/>
								}
								hint="lower = less overfit"
							/>
							<Metric
								label="OOS Sharpe"
								value={
									<MetricValue
										metric="out_of_sample_sharpe"
										value={s.out_of_sample_sharpe}
										row={s}
										surface="Passport rigor"
									/>
								}
							/>
							<Metric
								label="Trades"
								value={
									<MetricValue
										metric="total_trades"
										value={s.total_trades}
										row={s}
										surface="Passport rigor"
									/>
								}
								hint="executed in backtest"
							/>
						</div>
						<p className="caption mt-3 leading-relaxed text-[var(--text-3)]">
							{/* #1358 round-2 review: a strategy the live gate hasn't graded
							    yet (no num_trials_in_selection / an "unspecified" provenance
							    — see schemas.py, which covers BOTH "no persisted returns" and
							    a batch/DB-failure pending case, so this sentence must not
							    assert a specific data fact) must not claim EITHER a
							    self-contained N=1 grading OR a real multi-candidate
							    correction; both would assert a statistic nothing computed.
							    "generated_untracked_default" (schemas.py) is a DISTINCT
							    third case: the strategy WAS graded, but its generation
							    pipeline never proved it tracks its own selection-pool size,
							    so num_trials was forced to 1 and the scope says so
							    explicitly — that is not "ungraded" and must not reuse either
							    the "not graded yet" sentence or the true-self-contained N=1
							    sentence below. Four branches total, not a fallback chain. */}
							{s.num_trials_in_selection == null ||
							s.num_trials_scope === "unspecified" ? (
								<>This strategy has not been graded by the rigor gate yet.</>
							) : s.num_trials_scope === "generated_untracked_default" ? (
								<>
									This strategy's generation pipeline did not record its own
									selection-pool size, so it is graded at num_trials = 1 (no
									multiple-testing correction applied) — the same treatment as
									a self-contained strategy, but here because the pool size is
									unknown, not because there was only one candidate.
								</>
							) : s.num_trials_in_selection > 1 ? (
								<>
									The Deflated Sharpe Ratio corrects the realized Sharpe for
									multiple-testing inflation across the{" "}
									{s.num_trials_in_selection} candidates in this strategy's own
									selection pool (Bailey & López de Prado 2014).
								</>
							) : (
								<>
									This strategy is graded on its own Sharpe (num_trials = 1 — no
									multiple-testing correction applied); the Deflated Sharpe
									Ratio here still uses a standard error robust to non-normality
									and serial correlation (Newey–West HAC; Bailey & López de
									Prado 2014).
								</>
							)}{" "}
							PBO estimates how much of the in-sample Sharpe is overfit (Bailey
							et al. 2014). OOS Sharpe is the chronological out-of-sample
							number. A strategy passes the rigor gate only when all four
							signals — DSR, PBO, OOS Sharpe, and the look-ahead audit — align.
						</p>

						{/* Return source (T2.5) — the rigor gate says whether the edge survives;
            this says WHY it exists, and how durable that source is. */}
						{s.return_source && (
							<div className="mt-4 pt-4 border-t border-[var(--glass-border)]">
								<div className="flex items-center gap-2 flex-wrap mb-1">
									<span className="caption text-[var(--text-4)]">
										Dominant return source
									</span>
									<span
										className={`tag ${returnSourceChip(s.return_source).cls}`}
									>
										{returnSourceChip(s.return_source).label}
									</span>
								</div>
								{s.return_source_note && (
									<p className="caption leading-relaxed text-[var(--text-3)]">
										{s.return_source_note}
									</p>
								)}
							</div>
						)}
					</div>

					{/* Provenance */}
					{(s.curator_wallet ||
						s.on_chain_registration_tx ||
						s.extraction_llm) && (
						<div className="card passport-panel passport-provenance">
							<div className="label mb-3">Provenance</div>
							<div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-[0.85rem]">
								{s.extraction_llm && (
									<div>
										<div className="caption text-[var(--text-4)]">
											Extracted by
										</div>
										<div className="mono">{s.extraction_llm}</div>
									</div>
								)}
								{s.curator_wallet && (
									<div>
										<div className="caption text-[var(--text-4)]">Curator</div>
										<div className="mono">
											{s.curator_wallet.slice(0, 12)}…
											{s.curator_wallet.slice(-6)}
										</div>
									</div>
								)}
								{s.on_chain_registration_tx && (
									<div>
										<div className="caption text-[var(--text-4)]">
											Registration tx
										</div>
										<a
											href={`https://testnet.arcscan.app/tx/${s.on_chain_registration_tx}`}
											target="_blank"
											rel="noopener noreferrer"
											className="mono underline decoration-dotted underline-offset-2 hover:text-[var(--accent)] transition-colors"
										>
											{s.on_chain_registration_tx.slice(0, 14)}… ↗
										</a>
									</div>
								)}
								{s.curator_note && (
									<div style={{ gridColumn: "1 / -1" }}>
										<div className="caption text-[var(--text-4)]">
											Curator note
										</div>
										<div className="body">{s.curator_note}</div>
									</div>
								)}
							</div>
						</div>
					)}

					{/* Reasoning — the generation debate that produced this strategy
					    and the anchored agent decisions that consulted it, kept as
					    two separately-headed sections. Renders unconditionally: its
					    empty states are load-bearing product copy ("No anchored
					    decisions yet for this strategy"), not a placeholder to hide.
					    A panel that disappears when there is nothing to show teaches
					    the reader that provenance is optional. */}
					<StrategyReasoning strategyId={strategyId} onNavigate={onNavigate} />
				</div>
			</div>

			{ROADMAP_SURFACES_ENABLED && deployOpen && (
				<CreateVaultModal
					strategy={s}
					walletAddr={walletAddr}
					strictnessLevel={level}
					onClose={() => setDeployOpen(false)}
					onDeployed={(vaultAddress) => {
						setDeployOpen(false);
						if (onNavigate) {
							onNavigate("portfolio", { vaultAddress });
						}
					}}
				/>
			)}
		</div>
	);
}

function Metric({ label, value, hint }) {
	return (
		<div className="passport-metric">
			<div className="caption text-[var(--text-4)]">{label}</div>
			<div className="text-[1.1rem] font-semibold tabular-nums">{value}</div>
			{hint && <div className="caption text-[var(--text-4)]">{hint}</div>}
		</div>
	);
}

// Generation cost (#1326) — the measurement of the run that produced this
// strategy, beside the price that was quoted for it.
//
// Two facts, two sources, never a conversion: the tokens/seconds come from the
// cost meter, the price comes from the recorded `generation_payment.quote()`
// payload. Nothing here turns one into the other — converting measured tokens
// into dollars is #1217's remaining pricing work and it happens off-server.
//
// Absence is the honest default. No record at all (every curated strategy, and
// every generated one from before the meter) renders as "not measured", never
// as zero; a run whose token usage was only partly readable renders its totals
// with a `≥` so a floor is never read as a total.
function GenerationCostCard({ record }) {
	const view = deriveGenerationCostView(record);
	return (
		<div className="card fade-up fade-up-2" style={{ marginTop: 12 }}>
			<div className="label mb-1">Generation cost</div>
			{!view ? (
				<p className="caption leading-relaxed">{NOT_MEASURED_HINT}</p>
			) : (
				<>
					<p className="caption leading-relaxed">
						What this generation run consumed — a raw measurement, recorded when
						the run finished. Token counts are <strong>not</strong> converted to
						dollars.
					</p>
					<div className="mt-3 grid grid-cols-2 gap-3">
						<Metric
							label="Tokens"
							value={tokensLabel(view)}
							hint={`in ${formatTokenCount(view.tokens.input)} / out ${formatTokenCount(view.tokens.output)}`}
						/>
						<Metric
							label="Wall time"
							value={formatDuration(view.wallSeconds)}
							hint={
								view.tokens.calls != null
									? `${view.tokens.calls} LLM calls`
									: "LLM calls not measured"
							}
						/>
					</div>
					<div className="mt-3">
						<div className="caption text-[var(--text-4)]">Dominant stage</div>
						<div className="body">
							{view.dominantStage
								? `${stageLabel(view.dominantStage.name)} · ${formatDuration(view.dominantStage.wallSeconds)}`
								: NOT_MEASURED}
						</div>
					</div>
					<p
						className="caption mt-2 leading-relaxed"
						style={{
							color: view.usageComplete ? "var(--text-3)" : "var(--warning)",
						}}
					>
						{usageNote(view)}
					</p>
					<div
						className="mt-3 pt-3"
						style={{ borderTop: "1px solid var(--glass-border)" }}
					>
						<div className="caption text-[var(--text-4)]">Quoted price</div>
						<div className="body">{quoteLabel(view) || NOT_MEASURED}</div>
						<p className="caption leading-relaxed text-[var(--text-3)]">
							{quoteNote(view)}
						</p>
					</div>
				</>
			)}
		</div>
	);
}

// Paper-trading CTA — the free, account-owned act-on step (MVP spine). Kept
// separate from the vault Deploy card above: no wallet, no strictness gate,
// nothing moves. The server permits duplicate deployments for one strategy,
// so the "already running" state is client-side dedupe UX only.
function PaperDeployCard({ strategyId, user, onNavigate }) {
	const [existing, setExisting] = useState(null); // active deployment for THIS strategy
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState("");

	useEffect(() => {
		if (!user || !strategyId) return;
		let cancelled = false;
		apiGet("/api/paper/deployments")
			.then((res) => {
				if (cancelled) return;
				const hit = (res.deployments || []).find(
					(d) => d.strategy_id === strategyId && d.status === "active",
				);
				setExisting(hit || null);
			})
			.catch(() => {}); // decoration only — the CTA still works without it
		return () => {
			cancelled = true;
		};
	}, [user, strategyId]);

	const start = async () => {
		setBusy(true);
		setError("");
		try {
			await apiPostWithMeta("/api/paper/deployments", {
				strategy_id: strategyId,
			});
			onNavigate("paper");
		} catch (e) {
			if (e.status === 401) {
				setError("Your session expired — sign in again to paper trade.");
			} else if (e.detail?.reason === "no_strategy_spec") {
				setError(
					"This strategy has no machine-readable spec, so it cannot be paper-traded.",
				);
			} else if (e.detail?.reason === "invalid_strategy_spec") {
				setError(
					"This strategy's stored spec fails validation — it cannot be paper-traded.",
				);
			} else {
				setError(e.detail?.message || e.message || "Paper deploy failed.");
			}
		} finally {
			setBusy(false);
		}
	};

	return (
		<div className="card fade-up fade-up-2" style={{ marginTop: 12 }}>
			<div className="label mb-1">Paper trading</div>
			<p className="caption leading-relaxed">
				Simulated deployment — <strong>free, no funds move</strong>. Snapshots
				this strategy's spec and appends one real-data return per trading day,
				building a paper track record on Arc testnet — no real funds.
			</p>
			{error && (
				<p className="caption" role="alert" style={{ color: "var(--negative)", marginTop: 6 }}>
					{error}
				</p>
			)}
			<div style={{ marginTop: 10 }}>
				{!user ? (
					<a
						className="btn btn-outline btn-sm"
						href={`/sign-in?next=${encodeURIComponent(`/app/strategy/${strategyId}`)}`}
					>
						Sign in to paper trade →
					</a>
				) : existing ? (
					<button
						className="btn btn-outline btn-sm"
						onClick={() => onNavigate("paper")}
						title={`Active since ${existing.deployed_at}`}
					>
						Paper trading — day {existing.days} · view →
					</button>
				) : (
					<button
						className="btn btn-primary btn-sm"
						disabled={busy}
						onClick={start}
					>
						{busy ? "Deploying…" : "Start paper trading (free) →"}
					</button>
				)}
			</div>
		</div>
	);
}
