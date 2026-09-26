import { lazy, Suspense, useEffect, useState } from "react";

import { _resetAdminProbeCache } from "./adminProbe.js";
import { disconnectWallet, reconnectWallet } from "./config";
import { EXECUTION_CHAIN_ID } from "./chain-config";
import { checkLegacyWallet, listLinkedWallets } from "./linked-wallets";
import ErrorBoundary from "./components/ErrorBoundary";
import Layout from "./components/Layout";
import OnboardingTour, {
	hasCompletedOnboarding,
} from "./components/OnboardingTour";
import WalletGate from "./components/WalletGate";
import { canStore } from "./storage-consent.js";

const AccountSettings = lazy(() => import("./components/AccountSettings"));
const CorpusExplorer = lazy(() => import("./components/CorpusExplorer"));
const Explore = lazy(() => import("./components/Explore"));
const Generate = lazy(() => import("./components/Generate"));
const Insights = lazy(() => import("./components/Insights"));
const Leaderboard = lazy(() => import("./components/Leaderboard"));
const Learnings = lazy(() => import("./components/Learnings"));
const MarketplacePage = lazy(() => import("./components/MarketplacePage"));
const PaperTrading = lazy(() => import("./components/PaperTrading"));
const Portfolio = lazy(() => import("./components/Portfolio"));
const PublishPage = lazy(() => import("./components/PublishPage"));
const QuantLab = lazy(() => import("./components/QuantLab"));
const Reasoning = lazy(() => import("./components/Reasoning"));
const Strategies = lazy(() => import("./components/Strategies"));
const StrategyDetailPage = lazy(
	() => import("./components/StrategyDetailPage"),
);
const StrategyPassport = lazy(() => import("./components/StrategyPassport"));
const SubscriptionsPage = lazy(() => import("./components/SubscriptionsPage"));
const VaultDetail = lazy(() => import("./components/VaultDetail"));

function AppRouteFallback() {
	return (
		<div className="app-route-fallback" role="status" aria-live="polite">
			<span className="spinner" aria-hidden="true" />
			Loading view…
		</div>
	);
}

const openConnectModal = () =>
	window.dispatchEvent(new Event("open-wallet-modal"));

export default function AuthenticatedApp({
	route,
	features,
	navigateToPage,
	user,
}) {
	const [walletAddr, setWalletAddr] = useState(null);
	// No AUTO-open for anonymous visitors: it would announce itself as a
	// modal the instant an anonymous session mounts, before the visitor has
	// done anything. The manual help-icon path (Layout's "?" button, always
	// rendered — see onOpenTour below) does work for everyone: OnboardingTour
	// is passed `user` and gates its own anchor-navigation on
	// canNavigateTo(anchor, user) (#1364), so an anonymous viewer who opens
	// the tour and reaches the library/reasoning cards gets a centered card
	// with no anchor instead of being bounced to /sign-in.
	const [tourOpen, setTourOpen] = useState(() => (user ? !hasCompletedOnboarding() : false));
	const [journeyStage, setJourneyStage] = useState(null);
	// A detected-but-unlinked browser wallet that backs pre-account (SIWE-era)
	// data — drives the relink banner below (#1194 revision e).
	const [legacyWalletDetected, setLegacyWalletDetected] = useState(null);

	useEffect(() => {
		if (route.page !== "generate") setJourneyStage(null);
	}, [route.page]);

	const userId = user?.id ?? null;
	useEffect(() => {
		// Wallets link to ACCOUNTS — with no session there is nothing to link
		// (and the linked-wallet/legacy-check calls would just 401).
		if (!userId) return;
		reconnectWallet().then(async (result) => {
			if (!result) return;
			try {
				const wallets = await listLinkedWallets();
				if (
					wallets.some(
						(wallet) =>
							wallet.address === result.address.toLowerCase() &&
							wallet.chain_id === EXECUTION_CHAIN_ID,
					)
				) {
					setWalletAddr(result.address);
				} else {
					// The browser wallet is present but NOT linked to this
					// account. If it backs pre-account (SIWE-era) data, the
					// user's strategies are sitting invisible until they
					// relink — surface that instead of silently dropping the
					// address (#1194 revision e).
					const { has_legacy_data } = await checkLegacyWallet(
						result.address,
					);
					if (has_legacy_data) setLegacyWalletDetected(result.address);
				}
			} catch {
				// Account remains usable without wallet service.
			}
		});
		// Keyed on the account id, not []: with the anonymous guard above, a
		// visitor who signs in after an anonymous mount still gets their wallet
		// reconnected when the session resolves.
	}, [userId]);

	useEffect(() => {
		const handler = async (event) => {
			const address = event.detail.address;
			// The admin-gate probe (owner directive 2026-08-20) can key off
			// the connected wallet (X-Wallet-Address), so a wallet swap must
			// not leave a stale admin/non-admin determination cached for the
			// rest of its TTL window — connecting a DIFFERENT wallet is
			// exactly the case the gate exists to re-check.
			_resetAdminProbeCache();
			if (!address) {
				setWalletAddr(null);
				return;
			}
			try {
				const wallets = await listLinkedWallets();
				setWalletAddr(
					wallets.some((wallet) => wallet.address === address.toLowerCase())
						? address
						: null,
				);
			} catch {
				setWalletAddr(null);
			}
		};
		window.addEventListener("wallet-changed", handler);
		return () => window.removeEventListener("wallet-changed", handler);
	}, []);

	const handleDisconnect = () => {
		disconnectWallet();
		setWalletAddr(null);
	};
	const selectVault = (address) =>
		navigateToPage("vault-detail", { vaultAddress: address });
	const selectTrace = (traceId) => navigateToPage("reasoning", { traceId });

	const renderPage = () => {
		switch (route.page) {
			case "explore":
				return <Explore />;
			case "leaderboard":
				return <Leaderboard />;
			case "generate":
				return (
					<Generate
						onNavigate={navigateToPage}
						onStageChange={setJourneyStage}
						user={user}
					/>
				);
			case "library":
				return (
					<Strategies
						highlightStrategyId={route.highlight}
						defaultTab={route.tab}
						onNavigate={navigateToPage}
					/>
				);
			case "paper":
				return <PaperTrading onNavigate={navigateToPage} />;
			case "strategy":
				return (
					<StrategyPassport
						strategyId={route.strategyId}
						onNavigate={navigateToPage}
						walletAddr={walletAddr}
						user={user}
					/>
				);
			case "corpus":
				return <CorpusExplorer />;
			case "quant":
				return <QuantLab />;
			case "portfolio":
				return (
					<WalletGate
						walletAddr={walletAddr}
						pageName="Portfolio"
						description="Portfolio needs a verified linked wallet because vault deposits and withdrawals are on-chain actions."
						onConnect={openConnectModal}
					>
						<Portfolio
							walletAddr={walletAddr}
							onSelectVault={selectVault}
							onSelectTrace={selectTrace}
							onNavigate={navigateToPage}
						/>
					</WalletGate>
				);
			case "reasoning":
				return <Reasoning onNavigate={navigateToPage} />;
			case "learnings":
				return (
					<WalletGate
						walletAddr={walletAddr}
						pageName="Learnings"
						description="Link wallet controlling your deployed vaults to review their outcomes."
						onConnect={openConnectModal}
					>
						<Learnings onNavigate={navigateToPage} />
					</WalletGate>
				);
			case "insights":
				return <Insights />;
			case "vault-detail":
				return (
					<VaultDetail
						address={route.vaultAddress}
						onBack={() => navigateToPage("portfolio")}
					/>
				);
			case "marketplace":
				return <MarketplacePage onNavigate={navigateToPage} />;
			case "market-strategy":
				return (
					<StrategyDetailPage
						strategyId={route.strategyId}
						onNavigate={navigateToPage}
					/>
				);
			case "publish":
				return <PublishPage onNavigate={navigateToPage} />;
			case "subscriptions":
				return <SubscriptionsPage onNavigate={navigateToPage} />;
			case "account":
				return (
					<AccountSettings
						walletAddr={walletAddr}
						onDisconnect={handleDisconnect}
						linkError={route.error}
					/>
				);
			default:
				return null;
		}
	};

	return (
		<>
			<Layout
				page={route.page}
				setPage={navigateToPage}
				walletAddr={walletAddr}
				onConnect={setWalletAddr}
				onDisconnect={handleDisconnect}
				onOpenTour={() => setTourOpen(true)}
				user={user}
				features={features}
				journeyStage={journeyStage}
			>
				{/* Also renders for a wallet whose rows #1283's adoption
				    migration stamped with the platform account: /api/wallets/check
				    counts the adoption ledger, so an adopted row still reports
				    has_legacy_data and linking still hands it back. The copy below
				    stays true for that case — nothing was deleted, and only the
				    holder of that address can claim it. */}
				{legacyWalletDetected && !walletAddr && (
					<div
						role="status"
						style={{
							margin: "12px 16px 0",
							padding: "10px 14px",
							borderLeft: "3px solid var(--accent)",
							background: "var(--accent-muted)",
							borderRadius: 4,
							fontSize: 13,
							color: "var(--text-2)",
							display: "flex",
							alignItems: "center",
							gap: 12,
							flexWrap: "wrap",
						}}
					>
						<span style={{ flex: "1 1 320px" }}>
							<strong style={{ color: "var(--accent)" }}>
								Your earlier strategies are waiting.
							</strong>{" "}
							We found data tied to wallet{" "}
							{`${legacyWalletDetected.slice(0, 6)}…${legacyWalletDetected.slice(-4)}`}{" "}
							from before you had an account. Connect and sign with
							that wallet to bring it into this account — nothing is
							deleted, and only you can claim it.
						</span>
						<button
							type="button"
							className="btn-primary"
							onClick={() =>
								// The connect modal's listener lives in
								// WalletConnect (mounted inside Layout). NOT
								// Layout's onConnect prop — that is a state
								// setter, not the modal opener.
								window.dispatchEvent(new Event("open-wallet-modal"))
							}
						>
							Connect wallet
						</button>
						<button
							type="button"
							onClick={() => setLegacyWalletDetected(null)}
							aria-label="Dismiss"
							style={{
								background: "none",
								border: "none",
								cursor: "pointer",
								color: "var(--text-2)",
								fontSize: 16,
							}}
						>
							×
						</button>
					</div>
				)}
				<ErrorBoundary
					key={`${route.page}:${route.strategyId ?? ""}:${route.vaultAddress ?? ""}:${route.traceId ?? ""}:${route.highlight ?? ""}:${route.tab ?? ""}`}
				>
					<Suspense fallback={<AppRouteFallback />}>
						{renderPage()}
					</Suspense>
				</ErrorBoundary>
			</Layout>
			<OnboardingTour
				open={tourOpen}
				onClose={() => {
					setTourOpen(false);
					try {
						// Functional category (#1647) — same key OnboardingTour's own
						// finish() writes; both sites are gated so rejecting functional
						// storage cannot be defeated by dismissing from this one.
						if (canStore("archimedes.onboarding.v1"))
							localStorage.setItem("archimedes.onboarding.v1", "completed");
					} catch {
						/* non-fatal */
					}
				}}
				setPage={navigateToPage}
				user={user}
			/>
		</>
	);
}
