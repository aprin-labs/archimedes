import PolicyBanner from "./PolicyBanner";

// /terms — the public terms of service.
//
// Same standard as Privacy.jsx: every factual claim about what the service
// does today is grounded in main plus the live stack, re-checked on
// 2026-10-01 (#1432). The claims most likely to rot, and what makes them true:
//
//   - the payment position, which is SPLIT and must stay split on the page.
//     The generation paywall SETTLES FOR REAL: infra/ecs.tf pins
//     GENERATION_PAYMENT_REQUIRED="true", GENERATION_PAYMENTS_DRY_RUN="false"
//     and GENERATION_PRICE_USD="2.00", so services/generation_payment.py runs
//     its verify+settle path through Circle's Gateway facilitator. The money
//     moves between GATEWAY BALANCES: the payer first deposits into Circle's
//     GatewayWallet contract (ui/src/x402.js depositToGateway; 20 USDC default
//     in Generate.jsx; a passkey wallet funds a device payment key instead,
//     capped at $50 per deposit by ui/src/payment-deposit-cap.js), and each
//     settle moves $2.00 to the platform's Gateway balance. There is no
//     withdraw control in the UI. The MARKETPLACE rail is the one switched
//     off: PAYMENTS_DRY_RUN="true", and the marketplace pages are roadmap-
//     hidden (ui/src/featureFlags.js ROADMAP_PAGES). If any of these flags
//     move, THIS PAGE IS PART OF THAT CHANGE.
//   - the free allowance, price and limits: 3 free generations per verified
//     account (FREE_GENERATIONS_PER_ACCOUNT, services/free_generations.py),
//     $2.00, and 100/account/day + 200/IP/day (GENERATION_DAILY_CAP_PER_USER /
//     _PER_IP), all pinned in infra/ecs.tf. The code fallbacks in
//     services/generation_quota.py are 10/20; the live values are 100/200.
//     The quota runs BEFORE the paywall and counts every POST /start,
//     including the unpaid one Generate.jsx sends to fetch a fresh 402.
//   - what is on-chain: the user's own Gateway approve + deposit, and Circle's
//     settlement. Paper trading is a database replay (services/paper_trading.py)
//     with anchoring off (PAPER_TRACE_ANCHOR unset). The live agent runner
//     takes AGENT_DRY_RUN from SSM and logs "DRY RUN" lines, so it signs
//     nothing. Vault deployment and on-chain trace publishing are behind
//     ROADMAP_SURFACES_ENABLED. IPFS pinning never ran in production
//     (docs/adr/ipfs-pinning-not-live.md).
export default function Terms() {
	return (
		<div className="page-content policy-page">
			<PolicyBanner />

			<header>
				<p className="public-kicker">Terms</p>
				<h1>Terms of Service</h1>
				<p className="policy-meta">
					Last updated: [pending owner approval] · Archimedes
					(archimedes-arc.com) is operated under the name APRIN&nbsp;Labs, which
					is not a registered company
				</p>
			</header>

			<section>
				<h2>What this is</h2>
				<p>
					Archimedes reads quantitative-finance research and turns it into
					candidate trading strategies, then puts each one through statistical
					tests designed to catch results that only look good. It runs on the Arc
					public testnet.
				</p>
				<p>
					Using the site means you accept these terms. If you do not, please do
					not use it.
				</p>
			</section>

			<section>
				<h2>Test assets only — but paid generation is really charged</h2>
				<p>
					This is a testnet service. The assets are test assets with no monetary
					value, obtained free from a faucet.
				</p>
				<p>
					<strong>
						Your first three generations are free once your email address is
						verified. After that, generating a strategy is behind a paywall, and
						that paywall settles for real.
					</strong>{" "}
					Each paid generation currently costs $2.00 in testnet USDC. Payment
					works through Circle&rsquo;s Gateway:
				</p>
				<ul>
					<li>
						Your first payment includes a deposit: your wallet approves and
						deposits test USDC (20 by default; you can change the amount) into
						Circle&rsquo;s Gateway contract, where it is held as a balance for
						your address. With a Circle passkey wallet, the deposit goes instead
						to a device payment key kept in your browser, and each deposit to it
						is capped at $50.
					</li>
					<li>
						For each generation you sign a payment authorisation with a wallet
						linked to your account. We verify it and settle it through
						Circle&rsquo;s payment facilitator, and $2.00 of test USDC moves from
						your Gateway balance to ours. That is a real transfer, not a
						simulated one.
					</li>
					<li>
						Whatever you do not spend stays in Circle&rsquo;s Gateway. The site
						does not currently offer a way to withdraw it, so deposit only what
						you expect to use.
					</li>
				</ul>
				<p>
					Because the currency is test USDC you obtained free from a faucet,
					nothing of monetary value leaves you — but do not read
					&ldquo;testnet&rdquo; as &ldquo;the payment step is fake&rdquo;. The
					site shows a settlement reference when a payment completes. That is
					Circle&rsquo;s reference, not a chain transaction hash; Circle performs
					the on-chain settlement on its own schedule. The records we keep of
					your payments are described in the{" "}
					<a href="/privacy">Privacy Policy</a>.
				</p>
				<p>
					<strong>
						If a generation you paid for does not deliver, you are repaid as a
						credit, not as a refund.
					</strong>{" "}
					A settled payment buys a credit, and a generation spends it; if the run
					fails, crashes, or never starts, the credit stays yours and your next
					attempt spends it instead of charging you again. Credits do not expire.
					A free generation that does not produce a strategy is handed back the
					same way. We are being direct that this is not a money-back guarantee:
					settlement runs one way through our payment provider and the product
					has no way to send test USDC back to you, so we do not offer refunds.
				</p>
				<p>
					The <em>other</em> payment path is the one that is switched off. The
					strategy marketplace (publishing strategies and subscribing to them) is
					not offered on the site today, and its payment rail is off in
					production: nothing is verified and nothing settles there, and no
					balance moves. If that ever changes, it will be an announced change
					with this page updated first, not a silent flip.
				</p>
				<p>
					One thing that is <em>not</em> simulated: funding your Gateway balance
					is a real transaction on a public chain, and Circle settles your
					payments on-chain. Real chain, real permanence — play money. Paper
					trading, by contrast, is simulated and writes nothing to a chain, and
					the site does not currently offer vault deployment or on-chain
					publication of reasoning traces.
				</p>
				<p>
					<strong>
						Do not connect a wallet holding assets you care about.
					</strong>{" "}
					Use a fresh wallet made for this.
				</p>
			</section>

			<section>
				<h2>This is not investment advice</h2>
				<p>
					Nothing here is a recommendation to buy, sell, or hold anything. We are
					not a broker, an adviser, or a fiduciary, and no relationship of that
					kind is created by using the site.
				</p>
				<p>
					A strategy Archimedes produces is a research artifact with a
					statistical verdict attached — not a promise and not a forecast. The
					rigor gate exists to reduce known sources of false confidence:
					multiple-testing inflation, overfitting to one lucky sample,
					look-ahead leakage. Reducing those is not the same as predicting the
					future. Past performance, simulated or real, tells you nothing certain
					about what comes next, and no gate removes market risk.
				</p>
				<p>
					A verdict of &ldquo;pass&rdquo; means a strategy survived our tests. It
					does not mean it will make money. If you take anything from here into a
					live market, that decision and its consequences are entirely yours.
				</p>
			</section>

			<section>
				<h2>Your account</h2>
				<ul>
					<li>You must be 18 or older.</li>
					<li>
						Give accurate registration details and keep your password to
						yourself. You are responsible for what happens under your account.
					</li>
					<li>
						Accounts are for one person. Do not share, sell, or transfer one.
					</li>
					<li>
						Linking a wallet or a sign-in provider happens only through something
						you do, and we never merge accounts on your behalf. Paying from a
						Circle passkey wallet links that wallet&rsquo;s device payment key to
						your account; you can unlink it in Account Settings.
					</li>
					<li>Tell us if you think your account has been compromised.</li>
				</ul>
			</section>

			<section>
				<h2>Limits and fair use</h2>
				<p>
					Generation costs us real compute, so it is both priced and capped.{" "}
					<strong>
						After your three free generations, each generation currently costs
						$2.00 in testnet USDC, taken from your Circle Gateway balance before
						the work starts.
					</strong>{" "}
					An unspent credit from a paid run that failed is used first. The price
					is an operational setting we may change; when we do, the quote you are
					shown before you pay is the price that applies.
				</p>
				<p>
					On top of the price there are caps. Currently they are one hundred
					generations per account per day and two hundred per IP address per day
					(an IPv6 network counts as one address), and individual endpoints are
					rate limited as well. Every request to start a generation counts toward
					the daily caps, including the unpaid one the site sends to fetch payment
					details. These numbers are operational settings, not entitlements — we
					may change them, and we will not treat a limit as a promise. Caps are
					checked before any payment is verified or settled, so a request refused
					for being over a cap is never charged.
				</p>
				<p>
					Do not try to get around the limits: creating extra accounts for that
					purpose, rotating addresses, or scripting the interface to defeat a cap
					are all misuse, whatever the technical means.
				</p>
			</section>

			<section>
				<h2>Acceptable use</h2>
				<p>Do not:</p>
				<ul>
					<li>Break the law, or help someone else break it, using this service.</li>
					<li>
						Attack, overload, probe, or attempt to gain unauthorised access to
						the service or anyone else&rsquo;s account or data.
					</li>
					<li>
						Use it to plan or carry out market manipulation, or to deceive people
						about a strategy&rsquo;s provenance or results.
					</li>
					<li>
						Present output from here as reviewed, endorsed, or guaranteed by us,
						or as advice from a licensed professional.
					</li>
					<li>
						Upload malicious content, or attempt to make the system act outside
						its intended function.
					</li>
					<li>Scrape or bulk-extract beyond what the interface offers.</li>
				</ul>
			</section>

			<section>
				<h2>What you make here</h2>
				<p>
					You keep whatever rights you have in the briefs you write and the
					strategies generated from them. To run the service we need permission
					to store, process, and show that material back to you, and to send your
					brief to the language model that answers it — that permission is what
					you are giving by using the product, and it is limited to operating and
					improving the service.
				</p>
				<p>
					Nothing you make here is published automatically: your strategies and
					paper-trading records are private to your account, and the site does
					not currently offer publishing to the marketplace or to a blockchain.
					Anything that does reach a public blockchain, such as your Gateway
					deposit, cannot be undone, so do not put anything there you would not
					want permanently readable by anyone.
				</p>
				<p>
					If you send us feedback or bug reports, we may act on them freely and
					without obligation to you.
				</p>
			</section>

			<section>
				<h2>Availability</h2>
				<p>
					This is early software under active development. Features appear,
					change, and are removed. We make no uptime commitment. Testnet state,
					including deployed contracts and data, may be reset — by us or by the
					network — and we may have to clear or rebuild data during that. Keep
					your own copy of anything you would be sorry to lose.
				</p>
			</section>

			<section>
				<h2>Suspension and termination</h2>
				<p>
					We may close an account that breaks these terms, abuses the service, or
					puts it or its users at risk — immediately where the risk is immediate,
					and otherwise with notice where we reasonably can. Closing an account
					deletes it as the Privacy Policy describes. We may also stop offering
					the service entirely.
				</p>
				<p>
					You can stop using it whenever you like, and you can delete your account
					yourself in Account Settings; the{" "}
					<a href="/privacy">Privacy Policy</a> describes what that removes and
					what it leaves behind. Records on a public blockchain survive account
					closure — nobody can delete those.
				</p>
			</section>

			<section>
				<h2>No warranty</h2>
				<p>
					The service is provided &ldquo;as is&rdquo; and &ldquo;as
					available&rdquo;, without warranties of any kind, express or implied,
					including any implied warranty of merchantability, fitness for a
					particular purpose, or non-infringement, to the fullest extent the law
					allows.
				</p>
				<p>
					Specifically, we do not warrant that generated strategies are correct,
					profitable, novel, or suitable for any purpose; that backtests are free
					of error; that the papers cited support the strategy as the model
					claims; or that the service will be uninterrupted or secure.
				</p>
			</section>

			<section>
				<h2>Limitation of liability</h2>
				<p>
					To the fullest extent the law allows, we are not liable for lost
					profits, lost data, trading losses, or any indirect or consequential
					loss arising out of your use of the service — including any decision
					you make on the basis of something it produced.
				</p>
			</section>

			<section>
				<h2>Changes to these terms</h2>
				<p>
					We will update this page as the service changes. Once it is approved,
					the &ldquo;last updated&rdquo; line will record when; until then it is
					an undated draft. If a change materially affects your rights or what we
					do, we will say so plainly on this page rather than editing quietly.
					Continuing to use the service after a change means you accept the
					updated terms.
				</p>
			</section>

			<section>
				<h2>Governing law</h2>
				<p>
					These terms are governed by the laws of the State of Illinois, United
					States, without regard to its conflict-of-laws rules. Any dispute
					arising out of or relating to them will be brought in the state or
					federal courts located in Illinois, and you and we each consent to
					those courts&rsquo; jurisdiction.
				</p>
			</section>

			<section>
				<h2>Contact</h2>
				<p>
					Questions about these terms, and anything involving your own account,
					go to{" "}
					<a href="mailto:privacy@archimedes-arc.com">
						privacy@archimedes-arc.com
					</a>
					. That address forwards to the operator&rsquo;s personal Gmail inbox;
					the Privacy Policy explains how, and why to keep attachments out.
				</p>
				<p>
					Anything you would rather raise in the open, including a mistake on
					this page, can go to the project&rsquo;s public issue tracker instead:{" "}
					<a
						href="https://github.com/aprin-labs/archimedes/issues"
						target="_blank"
						rel="noopener noreferrer"
					>
						github.com/aprin-labs/archimedes/issues
					</a>
					. Please keep personal details out of it.
				</p>
			</section>
		</div>
	);
}
