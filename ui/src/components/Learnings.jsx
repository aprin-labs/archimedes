// Learnings page — per docs/user-stories.md §⑤ Explore (Library + Learnings).
// This is the "we don't hide losses" surface: shows which strategies (yours and
// the system's) have performed well, which haven't, and the agent's reasoning
// for each. Honest empty state until strategies accumulate runtime data.

import RegimePanel from "./RegimePanel";

export default function Learnings({ onNavigate }) {
	const goGenerate = (e) => {
		e.preventDefault();
		onNavigate?.("generate");
	};
	return (
		<div>
			<div className="max-w-[720px] mb-7">
				<div
					style={{
						display: "flex",
						alignItems: "center",
						gap: 10,
						marginBottom: 10,
						flexWrap: "wrap",
					}}
				>
					<h1 className="text-[2rem] font-semibold" style={{ margin: 0 }}>
						Learnings
					</h1>
					<span
						className="tag"
						style={{
							fontSize: "0.72rem",
							fontWeight: 700,
							letterSpacing: "0.06em",
							textTransform: "uppercase",
							padding: "4px 10px",
							background: "rgba(96, 165, 250, 0.12)",
							border: "1px solid rgba(96, 165, 250, 0.35)",
							color: "var(--info, #60A5FA)",
						}}
						title="This page is a roadmap surface — the runtime that populates it lands post-hackathon."
					>
						Roadmap · post-hackathon
					</span>
				</div>
				<p className="body mb-2.5">
					<strong>
						This page is a roadmap preview, not a shipped feature.
					</strong>{" "}
					The intent: surface the strategies you've deployed — winners and
					losers, both first-class — with the agent's reasoning available for
					each rebalance. Develop your own intuition rather than treat the
					system as a black box.
				</p>
				<p className="body text-[var(--text-3)]">
					Why it isn't live yet: the rebalance-trace history needs to accumulate
					before per-strategy winner/loser splits become meaningful. The data
					model and reasoning surface are in place (see{" "}
					<a
						href="/app/reasoning"
						onClick={(e) => {
							e.preventDefault();
							onNavigate?.("reasoning");
						}}
						style={{ color: "var(--accent)" }}
					>
						Reasoning
					</a>
					); the per-strategy aggregation view lands after the hackathon as a
					thin layer on top.
				</p>
				<p className="body text-[var(--text-3)]">
					Losing trades are not hidden when this lands. Silently rotating away
					from losses is the failure mode of every "AI fund" — we explicitly
					don't.
				</p>
			</div>

			{/* Market regime — full breakdown. The agent's lens on current
          conditions: VIX + MA cross signals, transition probabilities,
          and which library strategies it leans into. /portfolio shows
          only the compact pill so users can stay focused on their funds;
          the educational view lives here. */}
			<div className="max-w-[860px] mb-7">
				<h3 className="serif text-[1.4rem] mb-2.5">Current market regime</h3>
				<p className="body mb-4 text-[var(--text-3)]">
					The regime is the agent's read of market conditions, derived from VIX
					+ SPX moving-average cross signals. It biases strategy selection —
					<em>Calm</em> leans into momentum + TSMOM,
					<em> Crisis</em> leans into t-bill alternatives + capital
					preservation. The four regimes are defined in full below.
				</p>
				<RegimePanel />
			</div>

			<div className="card p-6">
				<div className="label mb-2">Nothing to show yet</div>
				<p className="body mb-3">
					This page populates as you deploy strategies into vaults and time
					accumulates performance + reasoning data. To get started:
				</p>
				<ol className="pl-5 leading-loose">
					<li>
						<strong>Generate</strong> a strategy from the{" "}
						<a
							href="/app/generate"
							onClick={goGenerate}
							className="text-[var(--accent)]"
						>
							Generate
						</a>{" "}
						page.
					</li>
					<li>
						Connect your wallet and <strong>deploy it into a vault</strong>{" "}
						before the strategy expires — generated strategies are{" "}
						<strong>time-bound</strong> to the market context captured at
						generation time, so they go stale.
					</li>
					<li>
						Come back here over time to see how each strategy is doing, and
						click through to the agent's reasoning at each decision point.
					</li>
				</ol>
				<p className="caption mt-4 text-[var(--text-4)]">
					On time-bound strategies: each Generate result captures the live
					regime + signals as its frame of reference. Deploy windows enforce
					that a strategy keyed to Tuesday's market context can't be executed on
					Friday — if you miss the window, regenerate. Server-side enforcement
					of expiry is on the roadmap.
				</p>
				<p className="caption mt-2 text-[var(--text-4)]">
					Roadmap layout: two columns — currently-profitable strategies on the
					left, currently-underperforming on the right; plus an "expired
					un-deployed" section so generated-but-not-acted-on strategies stay
					visible with their original reasoning for post-hoc inspection.
				</p>
			</div>
		</div>
	);
}
