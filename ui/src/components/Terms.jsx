// /terms — the license and terms for the open-source, local-first project.
//
// Rewritten 2026-11-01 (#1432) when the hosted site was shut down. The page it
// replaces was a terms of service for a hosted service: accounts, closing
// accounts, the generation paywall and its price, the 18-or-older rule, and
// the owner's promises about how the service would change. That service no
// longer exists, so those clauses described nothing and were dropped.
// ui/test/policy-pages.test.js pins their absence.
//
// What makes each section true:
//   - Public domain, no warranty: LICENSE is the Unlicense (LICENSE:1-24).
//     The warranty and liability sentences paraphrase LICENSE:16-22, and the
//     page says the LICENSE file is the full text. Bundled or depended-on
//     code keeps its own license: ui/public/fonts/OFL-*.txt, the git
//     submodules in .gitmodules, and the npm and Python packages.
//   - Research and testnet only: paper trading is a replay of the graded
//     backtest engine against market data (services/paper_trading.py module
//     docstring). It never calls the chain executor. PAPER_TRACE_ANCHOR
//     (off in .env.example) plus a deployment's own opt-in writes its
//     reasoning records, holdings included, on-chain (.env.example's
//     PAPER_TRACE_ANCHOR note; models/paper_store.py anchor_traces). That is
//     a public record, not a trade, and Privacy.jsx says so. The chain
//     defaults are the Arc testnet (chain/client.py chain_id 5042002,
//     ui/src/chain-config.js).
//   - Keys and credentials: every credential lives in the operator's own
//     .env (see .env.example) or the browser that holds a wallet. The
//     project holds none of them.
//
// Governing law (Illinois) was DROPPED, on purpose. The clause chose the law
// for a contract between a user and the hosted service: "using the site means
// you accept these terms". With no service there is no such contract. The
// Unlicense is a public-domain dedication with no choice-of-law clause of its
// own (LICENSE). A forum clause on this page would govern nothing, and it
// would suggest an agreement that does not exist. If the owner wants one
// back, it belongs in LICENSE-level text he chooses, not on this page.
export default function Terms() {
	return (
		<div className="page-content policy-page">
			<header>
				<p className="public-kicker">Terms</p>
				<h1>License and terms</h1>
				<p className="policy-meta">
					Last updated 2026-11-01 · Archimedes is open-source software published
					under the name APRIN&nbsp;Labs, which is not a registered company
				</p>
			</header>

			<section>
				<h2>Public domain, no warranty</h2>
				<p>
					Archimedes is free and unencumbered software released into the public
					domain under the{" "}
					<a
						href="https://github.com/aprin-labs/archimedes/blob/main/LICENSE"
						target="_blank"
						rel="noopener noreferrer"
					>
						Unlicense
					</a>
					. Anyone may copy, modify, publish, use, compile, sell or distribute
					it, for any purpose.
				</p>
				<p>
					It is provided &ldquo;as is&rdquo;, without warranty of any kind. Its
					authors are not liable for any claim, damages or other liability that
					arises from the software or its use. The LICENSE file is the full
					text.
				</p>
				<p>
					Code that Archimedes depends on or bundles keeps its own license. That
					covers the npm and Python packages, the git submodules, and the
					self-hosted fonts (SIL Open Font License).
				</p>
			</section>

			<section>
				<h2>Research and testnet only</h2>
				<ul>
					<li>Nothing Archimedes produces is financial advice.</li>
					<li>
						Backtests and rigor verdicts are research outputs: statistical tests
						on historical data. They can be wrong, and they do not predict future
						returns.
					</li>
					<li>
						Paper trading is simulated. It replays a strategy against market
						data. No order is placed, and nothing is bought or sold.
					</li>
					<li>
						The software is built for the Arc public testnet, whose test assets
						have no monetary value.
					</li>
				</ul>
			</section>

			<section>
				<h2>Your keys, funds and credentials</h2>
				<p>
					You are responsible for every key, wallet, fund and API credential you
					give an instance or connect to it, and for keeping them safe. You are
					also responsible for following the terms of every service you connect.
					That includes your language-model provider, market-data vendors,
					Circle, sign-in providers, email provider and RPC endpoint.
				</p>
				<p>
					If you run an instance for other people, the terms you offer them are
					yours to set.
				</p>
			</section>

			<section>
				<h2>Questions</h2>
				<p>
					Questions and corrections go to the project&rsquo;s issue tracker:{" "}
					<a
						href="https://github.com/aprin-labs/archimedes/issues"
						target="_blank"
						rel="noopener noreferrer"
					>
						github.com/aprin-labs/archimedes/issues
					</a>
					. It is public, so please do not post personal details there.
				</p>
			</section>
		</div>
	);
}
