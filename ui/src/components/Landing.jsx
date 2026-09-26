import { useEffect, useState } from "react";

import { apiGet } from "../api";

// ConfigService exposes these core singleton fields. The list is deliberately
// scoped to the contracts backing the claims this page actually makes —
// research, the rigor gate, and on-chain trace anchoring. Arc-native USDC and
// per-asset oracles are not fully represented either, so the derived total
// stays a floor rather than a complete inventory.
//
// The execution-side factory field is deliberately omitted: this page makes no
// execution claim at all, and the claim-integrity guard in
// ui/test/roadmap-copy.test.js asserts that literally by source-scanning this
// whole file — see EXECUTION_CLAIM_FREE_SURFACES there. That scan is why the
// words it forbids do not appear in these comments either.
const CORE_CONTRACT_FIELDS = [
	"synthetic_factory",
	"amm_router",
	"reasoning_trace_registry",
	"asset_registry",
	"price_oracle",
];

// The four rejection checks, plus the honest limit of each one. `limit` is not
// a disclaimer bolted on afterwards — it is the differentiator: every check
// below states what it does NOT prove, in the same card, at the same weight.
// Each `limit` is quoted from the code that computes the check:
//   DSR   — rigor_profiles.py's DSR_P_BADGE_MIN note ("say 'deflated-Sharpe
//           evidence at the 95% one-sided level', not 'statistically proven'");
//           level-1 dsr_p_min IS that constant — one bar, one definition (#1794).
//   PBO   — _rigor_helpers.compute_pbo docstring, "Known limitations": CSCV is
//           a selection-set property, so a neighbour entering the set can flip it.
//   OOS   — _rigor_helpers.compute_oos_sharpe:567-574, "a single chronological
//           hold-out, NOT a rolling walk-forward re-estimation ... no purge/embargo
//           gap at the train/test boundary." train_fraction defaults to 0.70.
//   LEAK  — dsl_lookahead_audit.py proves every bar-indexed read in the
//           interpreter is offset <= 0 (AST pass, #1566) and walks the
//           validated spec against that audited surface; the generator's own
//           safety claim is retired and ignored if present
//           (strategy_dsl.LEGACY_IGNORED_FIELDS, #1599) — the verdict is
//           derived, never self-attested.
const RIGOR_CRITERIA = [
	{
		code: "DSR",
		name: "Deflated Sharpe Ratio",
		question: "Could this Sharpe be luck after trying many ideas?",
		method:
			"Deflates the Sharpe by how many candidates the search actually tried, and corrects for returns that are skewed or fat-tailed rather than normal.",
		limit:
			"The Verified bar is deflated-Sharpe evidence at the 95% one-sided level — real, not proof.",
	},
	{
		code: "PBO",
		name: "Probability of Backtest Overfitting",
		question: "Would this collapse outside the sample it looks best on?",
		method:
			"Re-cuts the history into many equal time partitions and counts how often the in-sample winner lands below the out-of-sample median.",
		limit:
			"A property of the whole selection set, not one strategy — read it as a library signal.",
	},
	{
		code: "OOS",
		name: "Walk-forward out-of-sample",
		question: "Does the method survive data it never fit on?",
		method:
			"One chronological 70/30 cut. The held-out Sharpe must clear zero and stay within half of the in-sample Sharpe, so an in-sample blowout cannot pass on a sliver.",
		limit:
			"A single hold-out, not a rolling refit, and no purge gap at the boundary.",
	},
	{
		code: "LEAK",
		name: "Look-ahead audit",
		question: "Did information from the future reach a decision?",
		method:
			"Generated strategies are checked structurally, against a compiler proven to read only the current bar and earlier — not on the generator's own say-so.",
		limit:
			"The proof covers the strategy's decision path in our closed language — it cannot audit the market data itself for after-the-fact revisions.",
	},
];

// The fifth check, and the one nobody else runs: a correction across the whole
// board rather than within one strategy. Real today, and served publicly —
// GET /api/selection-bias/gate returns `board_level_fdr` {fdr_level, n_tested,
// n_significant}, recomputed over the exact cohort each response serves
// (selection_bias_routes.py:535-552). Every claim below is checkable there:
//   α = 0.05        — rigor_evaluator.DEFAULT_BOARD_FDR_LEVEL.
//   "never flips a verdict" — compute_board_level_fdr's scope decision, stated
//                     in its own docstring: ADVISORY, deliberately NOT wired
//                     into passes_all / blocked_by_floor at any level.
//   "zero included" — an empty or all-rejected cohort yields n_significant = 0
//                     and that is what is reported; there is no floor and no
//                     substitute value.
// Deliberately NOT stated here: how many strategies clear it right now. That is
// a live number, it is served on the endpoint above, and hard-coding today's
// value into shipped copy is exactly the staleness this page refuses elsewhere.
const BOARD_FDR = {
	code: "BH-FDR",
	name: "Board-level false-discovery rate",
	question: "Is the top of a ranked board an edge, or the best of N searches?",
	method:
		"Benjamini–Hochberg corrects every ranked strategy's “true Sharpe is positive” claim together, at α = 0.05, recomputed over the exact cohort each response serves.",
	limit:
		"Advisory — it never flips a gate verdict. The count that clears it is reported as measured, zero included.",
};

// The gate's four states, verbatim from services/live_rigor_gate.py (PASS /
// FAIL / PENDING / DEGENERATE). `passes` is fail-closed: only "pass" is truthy,
// so pending and degenerate can never round up into a badge.
const VERDICT_STATES = [
	{ state: "pass", body: "Every check cleared at the Verified bar." },
	{
		state: "fail",
		body: "Graded, and it lost. The measured values stay on the record.",
	},
	{
		state: "pending",
		body: "Not evaluated yet. Never rounded up into a pass.",
	},
	{
		state: "degenerate",
		body: "The return series has no variance to grade. Blocked at every strictness level.",
	},
];

const WORKFLOW = [
	{
		title: "Describe",
		body: "State the outcome, assets, risk appetite, and time horizon. The brief stays attached to named research and current market context.",
	},
	{
		title: "Debate",
		body: "Candidate methods are challenged, ranked, and kept beside the alternatives they beat.",
	},
	{
		title: "Gate",
		body: "The selected method faces DSR, PBO, out-of-sample, and look-ahead checks before sizing diagnostics expose its tradeoffs.",
	},
	{
		// This step used to promise that every run left a reasoning trace bound
		// to the chain. Retracted 2026-08-30 — false for this path, and pinned
		// as retracted in ui/test/public-visuals.test.js, whose comment carries
		// the exact wording. A generation run computes a keccak provenance hash
		// over (brief, candidate, weights, verdict) and persists it on the
		// strategy row (generation_pipeline._persist_candidate), whose own
		// comment says that identifier is "mirrored on-chain in v1.5" — i.e. not
		// today. The only code that writes to ReasoningTraceRegistry is the
		// agent rebalance tick (chain/agent_runner._commit_trace /
		// _reveal_trace), which no generation run reaches. What survives is the
		// part that is true: nothing is thrown away, and a fail is kept as
		// durably as a pass.
		title: "Inspect",
		body: "Nothing is discarded. The brief, the papers it cited, the candidates that lost, and the measured verdict are all kept with the strategy — a fail as durably as a pass.",
	},
];

const FAQS = [
	{
		question: "Does a passed rigor gate guarantee returns?",
		answer:
			"No. The gate reduces known sources of false confidence. It cannot remove market risk or guarantee future performance.",
	},
	{
		// Was: "… cannot bypass the server-side deployment gate." That gate is
		// real code, but it guards a path this surface no longer describes, and
		// the act-on step a visitor CAN reach — paper trading — has no rigor
		// precondition at all (api/paper_routes.py:85-125 checks ownership and
		// spec validity, nothing else; StrategyPassport.jsx:381-382 says so in
		// as many words). Answering with a gate the reader cannot hit would be a
		// claim the code does not enforce. The true answer is stronger anyway:
		// running a failing idea is allowed, relabelling one is not.
		question: "What happens when a strategy fails?",
		answer:
			"The failure stays visible with the measured reason, and a failed or pending strategy never receives the verified badge. The verdict is computed server-side on persisted returns, so it cannot be relabelled from the browser. You can still paper-trade a failing candidate — simulated, no capital — and its verdict does not change because you did.",
	},
	{
		question: "Do I need a wallet to explore Archimedes?",
		answer:
			"No. Create an account to generate and save strategies. Link a wallet only when you want proof of on-chain control.",
	},
	{
		question: "Does Archimedes trade for me?",
		answer:
			"No. Today Archimedes generates, gates, and records strategies. Paper trading, when used, is simulated: paper_daily_returns is the graded track record the rigor gate sees, not on-chain execution proof. It never takes the other side of a trade.",
	},
	{
		question: "Is this running with real money?",
		answer:
			"No mainnet money. Archimedes runs on Arc public testnet. Generation settles real testnet USDC — read GET /api/generate/quote (prod answers dry_run: false). Faucet USDC is not mainnet cash. It is a research prototype, not a production investment product.",
	},
];

export default function Landing() {
	const [contracts, setContracts] = useState(null);
	const [contractsError, setContractsError] = useState(false);

	useEffect(() => {
		apiGet("/api/config/contracts")
			.then(setContracts)
			.catch(() => setContractsError(true));
	}, []);

	const coreCount = contracts
		? CORE_CONTRACT_FIELDS.filter((field) => contracts[field]).length
		: null;
	const synthCount = contracts?.synthetics
		? Object.keys(contracts.synthetics).length
		: null;
	const poolCount = contracts?.pools
		? Object.keys(contracts.pools).length
		: null;
	const poolsUnread = contracts != null && contracts.pools == null;
	const totalLive =
		coreCount != null && synthCount != null && poolCount != null
			? coreCount + synthCount + poolCount
			: null;
	// Every step and answer below describes a path that runs today, so there
	// is no roadmap branch left to filter on — the execution tail was removed
	// in the 2026-08-30 claim scrub (owner decision).
	const visibleWorkflow = WORKFLOW;
	const visibleFaqs = FAQS;

	return (
		<main className="public-landing">
			<section className="public-hero" aria-labelledby="public-hero-title">
				<div className="public-shell">
					<div className="public-hero__stage">
						<div className="public-hero__copy">
							<p className="public-hero__eyebrow">
								One cited candidate. Four ways to reject it.
							</p>
							<h1 id="public-hero-title">
								<span>Portfolio strategy,</span> <span>under scrutiny.</span>
							</h1>
							{/* "…before anything runs live" retired 2026-08-30: nothing runs
							    live from this surface, and the conditional reads as a promise.
							    The replacement says what actually happens, and lands on the
							    part that is hardest to fake — the verdict is kept either way. */}
							<p className="public-hero__lede">
								Archimedes turns a plain-language brief into a strategy grounded in
								named research, then spends the rest of its effort trying to reject
								it. Four independent checks, one measured verdict — recorded
								whichever way it lands.
							</p>
							<div className="public-actions">
								<a
									className="public-cta public-cta--primary"
									href="/app/generate"
								>
									Generate a strategy
									<span aria-hidden="true">↗</span>
								</a>
								<a className="public-cta public-cta--quiet" href="#product">
									See the product
								</a>
							</div>
						</div>

						<ProductWorkspace
							contractsError={contractsError}
							coreCount={coreCount}
							poolCount={poolCount}
							poolsUnread={poolsUnread}
							synthCount={synthCount}
							totalLive={totalLive}
						/>
					</div>
				</div>
			</section>

			<EvidenceLedger />

			<section
				id="problem"
				className="public-section public-rigor-story"
				aria-labelledby="problem-title"
			>
				<div className="public-shell">
					<div className="public-rigor-story__intro">
						<h2 id="problem-title">A good-looking backtest is not enough.</h2>
						<div>
							<p>
								Trying enough ideas can manufacture a winner. Archimedes records
								the search, tests the selected method, and shows weak evidence
								instead of hiding it.
							</p>
							<ul>
								<li>Named papers stay attached.</li>
								<li>Failed values remain visible.</li>
								<li>Wallet authority stays separate.</li>
							</ul>
						</div>
					</div>

					<div className="public-rigor-story__proof">
						<div className="public-rigor-story__proof-copy">
							<h3 id="rigor-title">Most candidates should fail here.</h3>
							<p>
								Four independent checks run outside the generator, on persisted
								returns, so the thing being graded cannot influence its own grade.
								Each one answers a different way a backtest can fool you — and
								each one states, in the same card, what it does not prove.
							</p>
							<strong>Any failed check keeps the candidate unverified.</strong>
						</div>
						<RigorMatrix />
					</div>
				</div>
			</section>

			<section
				id="capabilities"
				className="public-section public-path"
				aria-labelledby="capabilities-title"
			>
				<div className="public-shell">
					<div className="public-path__intro">
						<p className="public-overline">
							One path. {visibleWorkflow.length} records.
						</p>
						<h2 id="capabilities-title">
							Built for inspection, not spectacle.
						</h2>
						<p>
							Every surface answers one question: what was requested, what was
							rejected, and what survived the rigor gate.
						</p>
					</div>

					<section
						id="workflow"
						className="public-path__sequence"
						aria-labelledby="workflow-title"
					>
						<div className="public-path__sequence-header">
							<h3 id="workflow-title">From intent to accountable action.</h3>
							<a href="/architecture">
								Read system architecture
								<span aria-hidden="true">↗</span>
							</a>
						</div>
						<ol>
							{visibleWorkflow.map((item, index) => (
								<li key={item.title}>
									<span aria-hidden="true">
										{String(index + 1).padStart(2, "0")}
									</span>
									<div>
										<h4>{item.title}</h4>
										<p>{item.body}</p>
									</div>
								</li>
							))}
						</ol>
					</section>
				</div>
			</section>

			<section
				id="use-cases"
				className="public-section public-context"
				aria-labelledby="use-cases-title"
			>
				<div className="public-shell">
					<div className="public-context__intro">
						<h2 id="use-cases-title">Useful when trust needs evidence.</h2>
						<p>
							Archimedes fits research decisions where sources, rejected
							candidates, and measured limits must stay visible.
						</p>
					</div>

					<div className="public-use-case-scenes">
						<article className="is-rigor">
							<span>Measured admission</span>
							<h3>Find out whether an idea survives its own backtest.</h3>
							<p>
								Four independent checks run outside the generator, on real
								persisted returns.
							</p>
						</article>
						<article className="is-research">
							<span>Legible evidence</span>
							<h3>Run quant research without building a quant desk.</h3>
							<p>
								Start in plain language. Inspect papers, backtests, and gates.
							</p>
						</article>
						{/* Was "Audit what the agent saw, cited, decided, and recorded" /
						    "Context and transaction evidence stay in one reviewable
						    trail." Both describe the rebalance-tick trace, which a
						    visitor here cannot reach — and "transaction evidence" is an
						    execution claim this surface no longer makes. Narrowed to the
						    record a generation run really does leave. */}
						<article className="is-audit">
							<span>Traceable reasoning</span>
							<h3>Audit what was asked, what was cited, and what lost.</h3>
							<p>
								The brief, its sources, the rejected candidates, and the measured
								verdict stay together.
							</p>
						</article>
					</div>

					<section
						id="integrations"
						className="public-rail-stack"
						aria-labelledby="integrations-title"
					>
						<div className="public-rail-stack__header">
							<p>Working substrate</p>
							<h3 id="integrations-title">Built on rails you can name.</h3>
						</div>
						<ul aria-label="Platform integrations">
							<li>
								<strong>Arc</strong>
								<span>public testnet settlement</span>
							</li>
							<li>
								<strong>Circle</strong>
								<span>native testnet USDC and wallet tooling</span>
							</li>
							<li>
								<strong>AWS Bedrock</strong>
								<span>strategy reasoning</span>
							</li>
							<li>
								<strong>Foundry</strong>
								<span>contract testing and deployment</span>
							</li>
							<li>
								<strong>FastAPI + React</strong>
								<span>agent API and interface</span>
							</li>
						</ul>
					</section>
				</div>
			</section>

			<AuthorityBoundary />

			<section
				id="faq"
				className="public-section public-faq"
				aria-labelledby="faq-title"
			>
				<div className="public-shell public-faq__grid">
					<div className="public-faq__intro">
						<h2 id="faq-title">Before you generate.</h2>
						<p>{visibleFaqs.length} direct answers. No return promises.</p>
					</div>
					<div className="public-faq__list">
						{visibleFaqs.map((item, index) => (
							<details key={item.question} open={index === 0}>
								<summary>{item.question}</summary>
								<p>{item.answer}</p>
							</details>
						))}
					</div>
				</div>
			</section>

			<section className="public-final" aria-labelledby="final-title">
				<div className="public-shell public-final__layout">
					<div>
						<p className="public-final__eyebrow">Start with a brief.</p>
						<h2 id="final-title">Describe the portfolio you want to test.</h2>
					</div>
					<div>
						<a className="public-cta public-cta--primary" href="/app/generate">
							Generate a strategy
							<span aria-hidden="true">↗</span>
						</a>
						<p>
							Arc public testnet only. Past performance is not a promise. A
							rigor gate can reject weak evidence; it cannot remove market risk.
						</p>
					</div>
				</div>
			</section>

			<PublicFooter />
		</main>
	);
}

function ProductWorkspace({
	contractsError,
	coreCount,
	poolCount,
	poolsUnread,
	synthCount,
	totalLive,
}) {
	return (
		<figure id="product" className="public-product-frame">
			<div className="public-product-frame__bar">
				<span>Strategy workspace</span>
				<span>Brief → debate → gate</span>
			</div>
			<img
				src="/product-workspace.png"
				width={1600}
				height={1000}
				fetchPriority="high"
				alt="Archimedes Generate workspace with a strategy brief, model context, and visible path from brief to rigor gate."
			/>
			<figcaption aria-live="polite">
				{contractsError || poolsUnread ? (
					<>
						<strong className="census-state census-state--error">Live census unavailable</strong>
						<span>
							{contractsError
								? "Contract API did not respond. No cached count substituted."
								: "Contract API responded, but the on-chain pool count could not be read. No cached count substituted."}
						</span>
					</>
				) : totalLive == null ? (
					<>
						<strong className="census-state">Reading Arc contract census</strong>
						<span>Waiting for live deployment data…</span>
					</>
				) : (
					<>
						<strong className="census-state census-state--live">Arc census live</strong>
						<span>{`≥${totalLive} reported instances · ${coreCount}/${CORE_CONTRACT_FIELDS.length} core · ${synthCount} synths · ${poolCount} pools`}</span>
					</>
				)}
			</figcaption>
		</figure>
	);
}

function EvidenceLedger() {
	return (
		<section
			className="public-proof-strip"
			aria-label="Verified product evidence"
		>
			<div className="public-shell">
				<ul>
					<li>
						<strong>Cited methods</strong>
						<span>Source papers remain attached.</span>
					</li>
					<li>
						<strong>Four rejection checks</strong>
						<span>Each one names its own limit.</span>
					</li>
					{/* Was a second "measured failures remain part of the record", a
					    near-verbatim repeat of the rail above it. Replaced with the
					    board-level correction — real, served on
					    GET /api/selection-bias/gate, and the one claim on this page
					    nobody else on the board is making. */}
					<li>
						<strong>Board-level correction</strong>
						<span>Ranking N strategies is counted as N tests.</span>
					</li>
				</ul>
				<nav className="public-proof-strip__links" aria-label="Evidence links">
					<a href="/architecture">System architecture</a>
					<a
						href="https://github.com/aprin-labs/archimedes"
						target="_blank"
						rel="noreferrer"
					>
						Source code
					</a>
					<a href="/llms.txt">Agent API entry point</a>
				</nav>
			</div>
		</section>
	);
}

// The four rejection checks, readable together rather than one at a time.
// This was a sticky card stack — four 340px cards pinned at staggered offsets,
// so scrolling revealed one and buried the last. The four-panel comparison is
// the differentiator, and a stack is the one layout that cannot show it, so the
// deck is a plain grid now: four panels at once, the board-level correction
// spanning underneath them, and the verdict states closing the section.
function RigorMatrix() {
	return (
		<div className="public-proof-deck">
			{RIGOR_CRITERIA.map((criterion) => (
				<article key={criterion.code}>
					<header>
						<span>{criterion.code}</span>
						<span>Independent rejection test</span>
					</header>
					<h4>{criterion.name}</h4>
					<p>{criterion.question}</p>
					<small>{criterion.method}</small>
					<small className="public-proof-deck__limit">
						<span>Limit</span>
						<span>{criterion.limit}</span>
					</small>
				</article>
			))}

			<article className="public-proof-deck__board">
				<header>
					<span>{BOARD_FDR.code}</span>
					<span>Across every ranked strategy</span>
				</header>
				<h4>{BOARD_FDR.name}</h4>
				<p>{BOARD_FDR.question}</p>
				<small>{BOARD_FDR.method}</small>
				<small className="public-proof-deck__limit">
					<span>Limit</span>
					<span>{BOARD_FDR.limit}</span>
				</small>
			</article>

			<div className="public-proof-deck__rule" role="note">
				<strong>Four verdicts, not two.</strong>
				<dl>
					{VERDICT_STATES.map((v) => (
						<div key={v.state}>
							<dt>{v.state}</dt>
							<dd>{v.body}</dd>
						</div>
					))}
				</dl>
			</div>
		</div>
	);
}

// The admission boundary — what the system decides versus what you decide.
// Rewritten in the 2026-08-30 claim scrub (owner decision): this section used
// to describe an owner/agent authority split over an on-chain execution path
// that is not live — there are zero live user deployments of it. Every line
// below describes a path that runs today: generation, the external rigor
// gate, paper trading, and on-chain trace anchoring. Structure and class
// names are unchanged so the section keeps its existing layout.
function AuthorityBoundary() {
	return (
		<section
			id="security"
			className="public-section authority-boundary"
			aria-labelledby="authority-title"
		>
			<div className="public-shell">
				<div className="public-section__intro">
					<h2 id="authority-title">The gate decides admission. You decide what runs.</h2>
					<p>
						Account identity, wallet proof, and the research pipeline stay
						separate. Nothing earns a verdict by asserting one.
					</p>
				</div>
				<div className="authority-boundary__grid">
					<div className="authority-boundary__side authority-boundary__side--agent">
						<p className="authority-boundary__owner">Archimedes may</p>
						<ul>
							<li>Read market conditions and cited research</li>
							<li>Propose, rank, and reject candidate strategies</li>
							{/* The retired third bullet claimed a chain commitment ahead of
							    the verdict — retracted with the Inspect step above and for
							    the same reason: no generation run writes to the trace
							    registry. What Archimedes does do here is grade a candidate
							    outside the generator, which is the claim the Security page's
							    "Verdict" control backs. */}
							<li>Grade a candidate outside the generator that produced it</li>
						</ul>
					</div>
					<div className="authority-boundary__line" aria-hidden="true">
						<span>admission boundary</span>
					</div>
					<div className="authority-boundary__side authority-boundary__side--user">
						<p className="authority-boundary__owner">Only you may</p>
						<ul>
							<li>Set the brief, the assets, and the risk appetite</li>
							<li>Keep or discard a strategy after reading its passport</li>
							<li>Link a wallet, when you want proof of on-chain control</li>
						</ul>
					</div>
				</div>
				{/* The retired invariant claimed a failed gate could not be
				    overridden. That is false, and the owner has overridden one
				    himself — the exact retracted wording is pinned in
				    ui/test/public-visuals.test.js. POST /api/paper/deployments
				    (api/paper_routes.py:85-125) checks ownership of the source
				    strategy and that its stored spec still validates — and nothing
				    else. There is no rigor precondition on the act-on step a visitor
				    can actually reach, and StrategyPassport.jsx:381-382 says so in
				    the code. The invariant that IS true is narrower and better: the
				    verdict is not yours to move. Running a failing idea in
				    simulation is allowed; relabelling it is not, because `passes` is
				    computed server-side on persisted returns and only "pass" is
				    truthy (services/live_rigor_gate.py). */}
				<div className="authority-boundary__verdict" role="note">
					<span>Admission invariant</span>
					<strong>A failing strategy stays a failing strategy.</strong>
					<span>
						Paper-trading one is allowed. Relabelling one is not — the verdict is
						measured server-side, and the measured reason stays on the record.
					</span>
				</div>
				<a className="authority-boundary__link" href="/security">
					Read security posture
					<span aria-hidden="true">↗</span>
				</a>
			</div>
		</section>
	);
}

function PublicFooter() {
	return (
		<footer className="public-footer">
			<div className="public-shell public-footer__grid">
				<div className="public-footer__brand">
					<strong>Archimedes</strong>
					<p>Research-grounded strategy generation on Arc public testnet.</p>
				</div>
				<nav aria-label="Product links">
					<strong>Product</strong>
					<a href="/app/generate">Generate</a>
					<a href="/app/explore">Explore</a>
					<a href="/security">Security</a>
					<a href="/architecture">Architecture</a>
				</nav>
				<nav aria-label="Resource links">
					<strong>Resources</strong>
					{/* docs.archimedes-arc.com — our own S3 + CloudFront, not GitHub
					    Pages (#1634). The trailing slash is load-bearing: the docs
					    site uses mkdocs directory URLs, and the CloudFront function
					    in docs-site/infra/main.tf 301s the slashless form. Guarded by
					    ui/test/docs-link.test.js. */}
					<a
						href="https://docs.archimedes-arc.com/"
						target="_blank"
						rel="noreferrer"
					>
						Docs
					</a>
					<a href="/llms.txt">Agent API</a>
					<a href="/.well-known/agent.json">Agent manifest</a>
					<a
						href="https://github.com/aprin-labs/archimedes"
						target="_blank"
						rel="noreferrer"
					>
						GitHub
					</a>
				</nav>
				<nav aria-label="Project links">
					<strong>Project</strong>
					<a
						href="https://github.com/aprin-labs/archimedes/blob/main/LICENSE"
						target="_blank"
						rel="noreferrer"
					>
						Unlicense
					</a>
					<a href="https://faucet.circle.com/" target="_blank" rel="noreferrer">
						Arc faucet
					</a>
					<span>No privacy or terms page published</span>
				</nav>
			</div>
			<div className="public-shell public-footer__base">
				<span>Research prototype. No mainnet money. Generation fee is real testnet USDC.</span>
				<span>Past performance does not guarantee future results.</span>
			</div>
		</footer>
	);
}
