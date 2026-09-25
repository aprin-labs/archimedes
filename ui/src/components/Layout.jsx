import { useCallback, useEffect, useRef, useState } from "react";
import { fetchAdminProbe } from "../adminProbe.js";
import { filterInsightsNavItem, resolveInsightsAdminState } from "../insightsGate.js";
import { NAV } from "../navConfig.js";
import WalletConnect from "./WalletConnect";
import BrandMark from "./BrandMark";
import Breadcrumbs from "./Breadcrumbs";
import { getStoredWalletName } from "../config";
import { deriveChainStatus } from "../chainStatus";
import { fetchHealth } from "../health";
import { getStoredTheme, applyTheme } from "../theme";
import { visibleNavigation } from "../routes";
import { lockBodyScroll, unlockBodyScroll } from "../utils/scrollLock";
import { getProofStages } from "../proofStages.js";

// NAV itself now lives in ../navConfig.js (round 4 review finding) — plain
// JS, zero imports, so its own test can import the REAL array Layout renders
// instead of a hand-built stand-in. See that file for the group rationale.

export const PAGE_LABELS = {
	landing: "Home",
	explore: "Explore",
	leaderboard: "Leaderboard",
	generate: "Generate",
	architecture: "Architecture",
	library: "Library",
	strategy: "Strategy Passport",
	paper: "Paper Trading",
	corpus: "Corpus",
	quant: "Quant Lab",
	portfolio: "Portfolio",
	reasoning: "Reasoning",
	learnings: "Learnings",
	insights: "Insights",
	"vault-detail": "Vault Details",
	marketplace: "Marketplace",
	"market-strategy": "Strategy Detail",
	publish: "Publish",
	subscriptions: "Subscriptions",
	account: "Account",
};

// PROOF_STAGES logic lives in ../proofStages.js (#1354) — plain .js so the
// hermetic node test can import and call getProofStages() directly; see
// that file for the rail's 3-vs-5-stage rationale.
const PROOF_STAGES = getProofStages();

const CORE_PAGE_STAGE = {
	generate: "brief",
	strategy: "gate",
	"vault-detail": "vault",
	portfolio: "monitor",
};

export default function Layout({
	page,
	setPage,
	walletAddr,
	onConnect,
	onDisconnect,
	onOpenTour,
	user,
	features,
	journeyStage,
	children,
}) {
	const [menuOpen, setMenuOpen] = useState(false);
	const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
	const [theme, setTheme] = useState(getStoredTheme);
	const [health, setHealth] = useState(null);
	const [healthError, setHealthError] = useState(false);
	// Ops nav item (Insights) renders only after a successful admin-gate
	// probe — owner directive 2026-08-20, supersedes #1028 D8. Starts
	// `false` (not `null`): the nav renders on first paint and an
	// unresolved probe must not show the item even briefly, so "unknown"
	// and "denied" read identically here (unlike the page gate in App.jsx,
	// which distinguishes them to show a neutral loader instead of flashing
	// content). Anonymous visitors never even attempt the probe — there is
	// no account for PLATFORM_ADMIN_WALLETS to match against.
	const [isInsightsAdmin, setIsInsightsAdmin] = useState(false);
	const hamburgerRef = useRef(null);
	const sidebarRef = useRef(null);
	const closeButtonRef = useRef(null);
	const chainStatus = deriveChainStatus(health, healthError);
	const proofStage =
		(page === "generate" ? journeyStage : null) ?? CORE_PAGE_STAGE[page];

	// Chain-status pill (#1321): re-derives from /health on mount and on every
	// in-app navigation (dep: `page`) — not a polling loop, a bounded
	// re-derivation. Layout sits at a stable position in the tree
	// (AuthenticatedApp.jsx renders it with no `key`, so React reconciles
	// rather than remounts across route changes), so an empty dep array here
	// would mean an outage that begins mid-session stays invisible until the
	// tab is reloaded — the same fail-soft failure #1321 was filed for, just
	// time-bounded instead of permanent. The "unknown" tone (deriveChainStatus,
	// ../chainStatus.js) covers both the pre-resolution window and a failed
	// fetch, so a backend outage in progress at mount OR surfaced by the next
	// navigation never silently renders as "Arc · Testnet live". A tab left
	// idle on a single page for the whole session still won't repaint until
	// the user navigates — that gap is accepted, not solved, by design: it
	// stays bounded by the next click rather than growing unbounded, without
	// adding the polling loop this issue explicitly ruled out.
	//
	// The actual network call goes through fetchHealth() (../health.js), a
	// short-TTL-cached wrapper, not a direct call to the raw fetch helper
	// here: Layout isn't the only /health caller (Architecture.jsx,
	// ModelCostPanel.jsx), so re-fetching straight from this effect on every
	// navigation would fire a fresh Arc RPC round-trip + DB reads even on a
	// nav that lands on a page with its own /health read. fetchHealth() lets
	// those callers share one response instead (#1333 review).
	useEffect(() => {
		let cancelled = false;
		fetchHealth()
			.then((d) => {
				if (!cancelled) {
					setHealth(d);
					// Clear a prior failure so a fetch on a later navigation can
					// report recovery — without this, one failed /health call
					// pins the pill on "unknown" for the rest of the session even
					// after the backend comes back, defeating the re-fetch this
					// effect now does on every `page` change.
					setHealthError(false);
				}
			})
			.catch(() => {
				if (!cancelled) setHealthError(true);
			});
		return () => {
			cancelled = true;
		};
	}, [page]);

	// Ops nav item admin-gate probe. Keyed on the account id AND the linked
	// wallet address (not `[]`, not `page`): re-probes on a real
	// sign-in/sign-out transition, shares the cached result with App.jsx's
	// own page-level probe within the TTL window (adminProbeCache.js) — so
	// landing on /app/insights does not double-fire the request — and does
	// NOT re-probe on every in-app navigation the way the /health effect
	// above does, since admin membership does not change mid-session under
	// normal use FOR A FIXED WALLET. It does change on a wallet swap,
	// though: require_platform_admin (backend/archimedes/api/
	// metrics_private_routes.py) checks PLATFORM_ADMIN_WALLETS membership
	// against THIS wallet, read from the X-Wallet-Address header — so an
	// account with more than one linked wallet, only one of them an admin
	// wallet, gets a genuinely different whoami answer per wallet. A
	// `[userId]`-only dependency left the Ops nav item's admin state (and
	// the earlier-computed insights `isInsightsAdmin`) pinned to whatever
	// the FIRST wallet on this account resolved to for the rest of the
	// session, surviving a swap to a non-admin wallet even though
	// AuthenticatedApp's wallet-changed handler correctly clears the shared
	// probe cache — the cache clear only helps a FUTURE caller, and without
	// this dependency there wasn't one (round-2 finding).
	const userId = user?.id ?? null;
	useEffect(() => {
		if (!userId) {
			setIsInsightsAdmin(false);
			return;
		}
		let cancelled = false;
		fetchAdminProbe().then((result) => {
			if (!cancelled) setIsInsightsAdmin(resolveInsightsAdminState(result));
		});
		return () => {
			cancelled = true;
		};
	}, [userId, walletAddr]);

	// Lock body scroll while the mobile nav drawer is open — otherwise the
	// page content underneath can still scroll behind the fixed overlay/drawer,
	// which reads as janky rather than a clean modal-style drawer.
	//
	// Uses the shared ref-counted lock (utils/scrollLock.js) rather than
	// saving/restoring document.body.style.overflow directly: AssetModal.jsx
	// can be open at the same time as this drawer, and a naive save/restore
	// in either place can wrongly re-enable scroll while the other is still
	// open (whichever closes first "restores" a stale value). The counter
	// only clears overflow once every locker has released it. AssetModal.jsx
	// isn't touched here — it keeps its own independent lock for now — but
	// this same helper is available for it to adopt.
	const closeMenu = useCallback(() => {
		setMenuOpen(false);
		// Restore the trigger after the drawer closes.
		hamburgerRef.current?.focus();
	}, []);

	// Mobile drawer behaves like a modal: lock background scroll, move focus
	// inside, contain Tab navigation, close on Escape, then restore the trigger.
	useEffect(() => {
		if (!menuOpen) return undefined;
		lockBodyScroll();
		closeButtonRef.current?.focus();

		const onKeyDown = (event) => {
			if (event.key === "Escape") {
				event.preventDefault();
				closeMenu();
				return;
			}
			if (event.key !== "Tab" || !sidebarRef.current) return;

			const focusable = Array.from(
				sidebarRef.current.querySelectorAll(
					'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
				),
			).filter((element) => element.getClientRects().length > 0);
			if (focusable.length === 0) return;

			const first = focusable[0];
			const last = focusable[focusable.length - 1];
			if (event.shiftKey && document.activeElement === first) {
				event.preventDefault();
				last.focus();
			} else if (!event.shiftKey && document.activeElement === last) {
				event.preventDefault();
				first.focus();
			}
		};

		document.addEventListener("keydown", onKeyDown);
		return () => {
			document.removeEventListener("keydown", onKeyDown);
			unlockBodyScroll();
		};
	}, [closeMenu, menuOpen]);

	const toggleTheme = () => {
		const next = theme === "light" ? "dark" : "light";
		applyTheme(next);
		setTheme(next);
	};

	// Circle wallet names describe wallet, never application identity.
	const displayName = walletAddr ? getStoredWalletName(walletAddr) : null;

	const handleNav = (id) => {
		setPage(id);
		setMenuOpen(false);
	};

	return (
		<div
			className={`shell app-site${sidebarCollapsed ? " shell-sidebar-collapsed" : ""}`}
		>
			{/* Bypass Blocks (2.4.1). The public shell has always shipped one; the
			    authenticated shell made every /app page start with the same ~18
			    stops (sidebar close, up to 12 nav buttons, collapse, hamburger,
			    theme, tour, account chip, wallet) before any content. */}
			<a className="app-skip-link" href="#app-content">
				Skip to content
			</a>

			{/* Mobile overlay — uses UnoCSS `fixed inset-0` + App.css `.sidebar-overlay` */}
			{menuOpen && (
				<div
					className="fixed inset-0 sidebar-overlay"
					onClick={closeMenu}
					aria-hidden="true"
				/>
			)}

			<aside
				ref={sidebarRef}
				className={`sidebar${menuOpen ? " sidebar-open" : ""}${sidebarCollapsed ? " sidebar-collapsed" : ""}`}
			>
				<div className="sidebar-brand">
					<div className="sidebar-brand-main">
						<BrandMark className="logo-mark" />
						<div className="logo-copy flex-1 min-w-0">
							<div className="logo-text">Archimedes</div>
							<div className="logo-sub">Evidence workspace</div>
						</div>
						<button
							ref={closeButtonRef}
							className="sidebar-close-btn"
							onClick={closeMenu}
							aria-label="Close menu"
						>
							<span className="i-lucide-x" style={{ width: 16, height: 16 }} />
						</button>
					</div>
				</div>

				<nav aria-label="Main">
					{NAV.map((group) => ({
						...group,
						// Anonymous visitors see only the browsable pages plus
						// Generate (#1194 revision d) — visibleNavigation owns
						// the id list. Groups left empty by the filter are
						// skipped entirely below so a logged-out sidebar never
						// shows a bare "Position"/"Market" header over nothing.
						// Insights ("Ops" group) additionally requires a
						// successful admin-gate probe (owner directive
						// 2026-08-20, supersedes #1028 D8) — visibleNavigation
						// has no notion of that server-truth check, so it is
						// applied here as a second, narrower filter (
						// filterInsightsNavItem, ../insightsGate.js) rather
						// than widening that helper's contract for one item.
						items: filterInsightsNavItem(
							visibleNavigation(group.items, features, user),
							isInsightsAdmin,
						),
					}))
						.filter((group) => group.items.length > 0)
						.map((group, gi) => (
						<div key={group.group || gi} className="nav-group">
							{group.group && (
								<div className="nav-group-label">{group.group}</div>
							)}
							{group.items.map((item) => {
								const isCurrent =
									page === item.id ||
									(item.id === "portfolio" && page === "vault-detail");
								return (
								<button
									key={item.id}
									type="button"
									data-tour={item.id}
									className={`nav-link${isCurrent ? " active" : ""}`}
									onClick={() => handleNav(item.id)}
									// This is a client-routed SPA, so the `.active` class was the
									// ONLY "you are here" signal — every item read identically to
									// a screen reader. aria-label is kept only for the collapsed
									// rail, where .nav-label is display:none (App.css:501) and the
									// button would otherwise have no accessible name; leaving it
									// on the expanded state silently overrides the visible label
									// if the two ever drift (2.5.3).
									aria-current={isCurrent ? "page" : undefined}
									aria-label={sidebarCollapsed ? item.label : undefined}
									title={sidebarCollapsed ? item.label : undefined}
								>
									<span
										className={`nav-icon ${item.icon}`}
										aria-hidden="true"
									/>
									<span className="nav-label">{item.label}</span>
								</button>
								);
							})}
						</div>
					))}
				</nav>

				<div className="sidebar-footer">
					<span
						className={`live-dot live-dot-${chainStatus.tone}`}
						aria-hidden="true"
					/>
					<span className="sidebar-footer-label">{chainStatus.label}</span>
					<button
						type="button"
						className="sidebar-collapse-btn"
						onClick={() => setSidebarCollapsed((v) => !v)}
						aria-label={
							sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"
						}
						aria-expanded={!sidebarCollapsed}
						title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
					>
						<span
							className={
								sidebarCollapsed
									? "i-lucide-panel-left-open"
									: "i-lucide-panel-left-close"
							}
							style={{ width: 18, height: 18 }}
						/>
					</button>
				</div>
			</aside>

			<div className="main-area">
				<div className="topbar">
					{/* Left: hamburger (mobile) + breadcrumbs */}
					<div className="flex items-center gap-3">
						<button
							ref={hamburgerRef}
							className={`hamburger-btn${menuOpen ? " open" : ""}`}
							onClick={() => setMenuOpen((v) => !v)}
							aria-label="Toggle navigation"
							aria-expanded={menuOpen}
						>
							<span className="hamburger-line" />
							<span className="hamburger-line" />
							<span className="hamburger-line" />
						</button>
						<Breadcrumbs page={page} setPage={setPage} />
					</div>
					<div className="flex items-center gap-2">
						{/* Personalized greeting moved into the WalletConnect dropdown
                header so the topbar stays compact + the greeting lives next
                to the wallet identity it belongs to. */}
						<button
							type="button"
							className="topbar-icon-btn"
							onClick={toggleTheme}
							aria-label={
								theme === "light"
									? "Switch to dark theme"
									: "Switch to light theme"
							}
							title={
								theme === "light"
									? "Switch to dark theme"
									: "Switch to light theme"
							}
						>
							<span
								className={theme === "light" ? "i-lucide-moon" : "i-lucide-sun"}
								style={{ width: 18, height: 18 }}
							/>
						</button>
						{onOpenTour && (
							<button
								type="button"
								className="topbar-icon-btn"
								onClick={onOpenTour}
								aria-label="Open onboarding tour"
								title="What is Archimedes? — open the tour"
							>
								<span
									className="i-lucide-help-circle"
									style={{ width: 18, height: 18 }}
								/>
							</button>
						)}
						{user ? (
							<>
								<button
									type="button"
									className="wallet-chip account-chip"
									onClick={() => handleNav("account")}
									title={user?.email}
								>
									<span
										className="i-lucide-user-round"
										style={{ width: 14, height: 14 }}
									/>
									<span>{user?.name || "Account"}</span>
								</button>
								<WalletConnect
									address={walletAddr}
									displayName={displayName}
									onConnect={onConnect}
									onDisconnect={onDisconnect}
									onEditProfile={() => handleNav("account")}
								/>
							</>
						) : (
							// Anonymous browse (#1194 revision d): the account
							// chip would navigate into an auth-gated page and
							// the wallet widget links wallets to ACCOUNTS, so
							// with no session both collapse into the one honest
							// affordance — sign in. Deep-link preserved via
							// ?next so the visitor returns to the page they
							// were reading.
							<a
								className="btn-primary"
								href={`/sign-in?next=${encodeURIComponent(
									`${window.location.pathname}${window.location.search}`,
								)}`}
							>
								Sign in
							</a>
						)}
					</div>
				</div>
				{/* tabIndex={-1} so the skip link above actually lands focus here
				    rather than only moving the scroll position. */}
				<main
					id="app-content"
					tabIndex={-1}
					className={`page-content page-${page}`}
				>
					{proofStage && (
						<ol className="app-proof-rail" aria-label="Core strategy journey">
							{PROOF_STAGES.map((stage) => {
								const isCurrent = stage.id === proofStage;
								return (
									<li
										key={stage.id}
										className={isCurrent ? "is-current" : undefined}
										aria-current={isCurrent ? "step" : undefined}
									>
										{stage.label}
									</li>
								);
							})}
						</ol>
					)}
					{children}
				</main>
			</div>
		</div>
	);
}
