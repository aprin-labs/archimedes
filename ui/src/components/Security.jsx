import StorageDisclosure from "./StorageDisclosure";

export default function Security() {
	return (
		<main className="security-page">
			<section className="security-hero" aria-labelledby="security-title">
				<div className="public-shell security-hero__layout">
					<div>
						<p className="public-overline">Public security posture</p>
						<h1 id="security-title">
							Security is enforced boundaries, not a guarantee.
						</h1>
						<p className="security-hero__lede">
							Archimedes separates account identity, wallet proof, and the
							service credentials its own agents run under. Generation is a
							paid call on this testnet, and that boundary is described here
							too. These controls describe current code—not a promise that
							failure is impossible.
						</p>
					</div>
					<ul className="security-status" aria-label="Current product status">
						<li>
							<span>Environment</span>
							<strong>Arc public testnet</strong>
						</li>
						<li>
							<span>Product status</span>
							<strong>Research prototype</strong>
						</li>
						<li>
							<span>Value at risk</span>
							<strong>Testnet USDC only</strong>
						</li>
						<li>
							<span>Paid surface</span>
							<strong>Generation — quoted live</strong>
						</li>
					</ul>
				</div>
			</section>

			<section
				className="public-section security-authority"
				aria-labelledby="authority-model-title"
			>
				<div className="public-shell">
					<div className="security-section-heading">
						<p className="public-overline">Authority model</p>
						<h2 id="authority-model-title">
							Three boundaries. No role inflation.
						</h2>
						<p>
							Signing in, proving a wallet, and running an internal agent are
							separate capabilities. None of them implies the others.
						</p>
					</div>
					<dl className="security-role-ledger">
						<div>
							<dt>
								<span>01</span>
								Better Auth account
							</dt>
							<dd>
								Canonical application identity. A connected wallet never creates
								or replaces the account session.
							</dd>
						</div>
						<div>
							<dt>
								<span>02</span>
								Proof-linked wallet
							</dt>
							<dd>
								A five-minute, single-use EIP-4361 challenge proves control
								before a wallet is linked. Wallet state is not an app
								credential.
							</dd>
						</div>
						<div>
							<dt>
								<span>03</span>
								Bounded internal agent
							</dt>
							<dd>
								Archimedes&apos; own generation and research agents authenticate
								with a service credential, not a user session. A user session
								cannot assume that role, and that role cannot read or write
								another account&apos;s private records.
							</dd>
						</div>
					</dl>
				</div>
			</section>

			<section
				className="public-section security-controls"
				aria-labelledby="controls-title"
			>
				<div className="public-shell security-controls__layout">
					<div className="security-section-heading">
						<p className="public-overline">Verified controls</p>
						<h2 id="controls-title">Where enforcement happens.</h2>
						<p>
							Each control maps to an application, edge, database, or contract
							boundary in the current repository.
						</p>
					</div>
					<ol className="security-controls__list">
						<li>
							<span>Session</span>
							<div>
								<h3>Account access is checked at three layers.</h3>
								<p>
									Production session cookies are HttpOnly, Secure, and
									SameSite=Lax. nginx, the UI route guard, and FastAPI
									independently protect private surfaces. Two browse pages are
									deliberately anonymous, and the edge and client halves of
									that carve-out list are kept in lockstep.
								</p>
							</div>
						</li>
						<li>
							<span>Scope</span>
							<div>
								<h3>Private records follow the canonical user ID.</h3>
								<p>
									Profile, strategy, job, and linked-wallet reads resolve
									through the authenticated Better Auth user—not a
									client-supplied address.
								</p>
							</div>
						</li>
						<li>
							<span>Integrity</span>
							<div>
								<h3>Agent-only writes require a service credential.</h3>
								<p>
									User sessions cannot forge internal reasoning traces or any
									other integrity-critical record written by an agent role.
								</p>
							</div>
						</li>
						<li>
							<span>Payment</span>
							<div>
								<h3>Generation is a paid call, bound to the wallet you proved.</h3>
								<p>
									Starting a generation without a signed payment returns HTTP
									402 carrying the full x402 requirements, and the same terms —
									price, asset, chain, recipient, and whether the rail is live
									or dry — are published anonymously at GET /api/generate/quote,
									so they are readable before anything is signed. The payer
									inside the signed authorization must be the wallet linked to
									the account; a mismatch is refused before any settlement
									round-trip. An operator kill switch refuses service rather
									than serving the paid product unpaid. Paper trading is free.
								</p>
							</div>
						</li>
						<li>
							<span>Edge</span>
							<div>
								<h3>Browser and API ingress is constrained.</h3>
								<p>
									Same-origin defaults, a script policy admitting same-origin
									bundles plus one hashed inline bootstrap, HSTS, framing denied
									outright, and a permissions policy that turns off geolocation,
									microphone, and camera. Two per-IP request-rate zones run at
									the edge — the tighter one on the credential surface — with
									tighter per-route limits on expensive endpoints behind them.
								</p>
							</div>
						</li>
						<li>
							<span>Verdict</span>
							<div>
								<h3>A rigor verdict is computed server-side, never asserted.</h3>
								<p>
									The gate runs outside the generator, on persisted returns, so
									the thing being graded cannot influence its own grade. A failed
									or pending result cannot be relabelled as verified from the
									client.
								</p>
							</div>
						</li>
						<li>
							<span>Provenance</span>
							<div>
								<h3>
									The agent&apos;s rebalance decisions are content-hashed and
									anchored on Arc.
								</h3>
								<p>
									A published trace can be re-hashed and compared against its
									on-chain anchor. That proves the record was not rewritten
									afterwards—it does not prove the reasoning was good. Two
									limits the sentence would otherwise hide: a generation run
									computes the same kind of hash but will only be anchored in a
									later version, and a decision that produced no transaction has
									no anchor to check. A trace reports which case it is rather
									than implying the first.
								</p>
							</div>
						</li>
					</ol>
				</div>
			</section>

			<section
				id="known-limits"
				className="public-section security-limits"
				aria-labelledby="known-limits-title"
			>
				<div className="public-shell security-limits__layout">
					<div>
						<p className="public-overline">Known limits</p>
						<h2 id="known-limits-title">
							Controls reduce risk. They do not erase it.
						</h2>
					</div>
					<ul>
						<li>
							<strong>Testnet:</strong> Arc public testnet only, and the USDC in
							play is faucet USDC. Do not connect a wallet holding mainnet
							assets.
						</li>
						<li>
							<strong>No execution:</strong> Archimedes does not trade with
							capital today. Paid generation, the rigor gate, paper trading, and
							the agent&apos;s trace anchoring are what run; nothing here should
							be read as a claim that funds are being managed.
						</li>
						<li>
							<strong>Live charge:</strong> the generation paywall is on and not
							in dry-run, so a signed payment settles testnet USDC for real —
							anyone can confirm that anonymously at GET /api/generate/quote. A
							settled fee lands in a platform-operated wallet Archimedes signs
							for through its payment provider. It is a fee, not a balance held
							for you, and there is nothing there to withdraw.
						</li>
						<li>
							<strong>Model risk:</strong> Generation is LLM-driven and can be
							wrong in ways the gate does not measure. A passed verdict bounds
							selection bias, not judgement.
						</li>
						<li>
							<strong>Demo inputs:</strong> Some current oracle and risk inputs
							are mock data and must not support live financial decisions.
						</li>
						<li>
							<strong>Immutable contracts:</strong> A defect requires a new
							deployment and migration rather than an in-place upgrade.
						</li>
						<li>
							<strong>No assurance:</strong> No independent security audit,
							production-readiness, regulatory, or return guarantee is claimed
							by this page.
						</li>
					</ul>
				</div>
			</section>

			{/* Browser-storage disclosure + the live consent controls (#1647).
			    Rendered from src/storage-consent.js — the same inventory the
			    gate reads before any write — so this section cannot drift from
			    the keys the app actually sets. #1432's /privacy page can mount
			    this identical component; nothing here is transcribed. */}
			<StorageDisclosure />

			<section
				className="public-section security-evidence"
				aria-labelledby="evidence-title"
			>
				<div className="public-shell security-evidence__layout">
					<div className="security-section-heading">
						<p className="public-overline">Inspect the evidence</p>
						<h2 id="evidence-title">Read controls at source.</h2>
						<p>
							Security posture should be reviewable, not accepted from copy.
							Every sentence above has a row in the claims ledger naming the file
							that makes it true.
						</p>
					</div>
					<ul>
						<li>
							<a href="/architecture">System architecture</a>
							<span>Identity, services, and chain boundary</span>
						</li>
						<li>
							<a
								href="https://github.com/aprin-labs/archimedes/blob/main/docs/claims-ledger.md"
								target="_blank"
								rel="noreferrer"
							>
								Claims ledger
							</a>
							<span>Each claim on this page, and the code behind it</span>
						</li>
						<li>
							<a href="/api/generate/quote" target="_blank" rel="noreferrer">
								Live generation quote
							</a>
							<span>
								The paywall&apos;s own state, readable without an account
							</span>
						</li>
						<li>
							<a
								href="https://github.com/aprin-labs/archimedes/blob/main/backend/archimedes/services/generation_payment.py"
								target="_blank"
								rel="noreferrer"
							>
								Generation paywall
							</a>
							<span>The 402, the payer binding, and the kill switch</span>
						</li>
						<li>
							<a
								href="https://github.com/aprin-labs/archimedes/blob/main/docs/security/auth-model.md"
								target="_blank"
								rel="noreferrer"
							>
								Authentication model
							</a>
							<span>Session, wallet proof, and user scoping</span>
						</li>
						<li>
							<a
								href="https://github.com/aprin-labs/archimedes/blob/main/contracts/src/ReasoningTraceRegistry.sol"
								target="_blank"
								rel="noreferrer"
							>
								Trace registry contract
							</a>
							<span>How a reasoning record is anchored on Arc</span>
						</li>
						<li>
							<a
								href="https://github.com/aprin-labs/archimedes/blob/main/backend/archimedes/services/live_rigor_gate.py"
								target="_blank"
								rel="noreferrer"
							>
								Live rigor gate
							</a>
							<span>The admission checks, as they actually run</span>
						</li>
					</ul>
				</div>
			</section>
		</main>
	);
}
