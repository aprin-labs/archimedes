// Wallet-gate wrapper. Renders a Connect-Wallet CTA card when no wallet
// is connected; renders children when one is. Used to gate per-user
// surfaces (Portfolio, Learnings) so the logged-out experience doesn't
// render "$0.00 across 0 vaults you created" or "27 traces (you've
// deployed)" — personalization the user doesn't actually have without a
// wallet. The strategy list renders ungated (AuthenticatedApp.jsx
// `case "library"`) — it needs a signed-in session, not a wallet, so it
// isn't this gate's job.
//
// Public pages (Generate, Corpus, Reasoning, Explore, Landing) deliberately
// do NOT use this gate — they're either browse-only or paper-grounded
// and useful without a wallet.

export default function WalletGate({
	walletAddr,
	pageName,
	description,
	onConnect,
	children,
}) {
	if (walletAddr) return children;

	const titleId = `wallet-gate-${pageName.toLowerCase().replaceAll(" ", "-")}`;
	return (
		<section className="wallet-gate" aria-labelledby={titleId}>
			<div className="wallet-gate__icon" aria-hidden="true">
				<span className="i-lucide-lock" />
			</div>
			<p className="app-eyebrow">Verified wallet required</p>
			<h1 id={titleId}>Connect to view {pageName}</h1>
			<p>{description}</p>
			<button type="button" className="btn btn-primary" onClick={onConnect}>
				Connect wallet
			</button>
			<small>
				Connection requires signature proof before a wallet links to your
				account. Circle passkeys control Circle wallets only.
			</small>
		</section>
	);
}
