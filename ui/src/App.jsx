import { lazy, Suspense, useCallback, useEffect, useState } from "react";

import { fetchAdminProbe } from "./adminProbe.js";
import {
	isInsightsPageBlocked,
	resolveInsightsAdminState,
	resolveInsightsView,
} from "./insightsGate.js";
import {
	adminIdentityKey,
	normalizeWalletAddress,
	readInsightsAdmin,
	rememberInsightsAdmin,
} from "./insightsAdminMemo.js";
import { useAuth } from "./AuthContext";
import { getAddress } from "./config";
import { defaultFeatures, fetchFeatures } from "./features";
import { pageToPath, resolveRoute } from "./routes";
import { canStore } from "./storage-consent.js";
import { useStorageConsent } from "./hooks/useStorageConsent.js";
import Architecture from "./components/Architecture";
import AuthPage from "./components/AuthPage";
import Landing from "./components/Landing";
import NotFound from "./components/NotFound";
import PublicLayout from "./components/PublicLayout";
import Security from "./components/Security";
import "./App.css";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";
const AuthenticatedApp = lazy(() => import("./AuthenticatedApp"));

function currentRoute(features) {
	return resolveRoute(
		window.location.pathname,
		window.location.search,
		features,
	);
}

export default function App() {
	const { user, loading: authLoading } = useAuth();
	// Storage-consent record (#1647). Read here only so the analytics beacon
	// effect below re-evaluates the moment the visitor answers the banner.
	const [consent] = useStorageConsent();
	const [features, setFeatures] = useState(defaultFeatures);
	const [route, setRoute] = useState(() => currentRoute(defaultFeatures));
	// Admin-gate probe result for /app/insights (owner directive 2026-08-20,
	// supersedes #1028 D8): null while checking, true/false once the server
	// has answered. Server truth only — never derived from `user`/wallet
	// local state, which cannot know PLATFORM_ADMIN_WALLETS membership.
	const [insightsAdmin, setInsightsAdmin] = useState(null);
	// The connected wallet ADDRESS (config.js's 'wallet-changed' event — fired
	// on a raw account swap in the injected/EIP-6963 wallet, independent of
	// sign-in state), tracked so the insights-admin-probe effect below re-runs
	// even while already sitting on /app/insights. A [route.page]-only
	// dependency left a stale `insightsAdmin === true` (and the live dashboard
	// it gates) rendering after switching from an admin-linked wallet to a
	// non-admin one on the same account, because route.page never changes on
	// an in-place wallet swap (round-2 finding: the cache-reset in
	// AuthenticatedApp's wallet-changed handler only helps a FUTURE probe
	// caller — this effect is the one that has to actually become that
	// caller).
	//
	// #1648 / I-8 B2: this used to be a plain counter bumped on EVERY
	// wallet-changed event. That made the re-probe fire on events that carry
	// no change at all — an injected provider re-announces `accountsChanged`
	// with the SAME account on tab focus — and each fire blanked the page an
	// admin was already using. Holding the address VALUE instead means React
	// bails out of the state update when the announced account is unchanged,
	// so a no-op announcement no longer re-runs the effect at all. Seeded from
	// getAddress() so a wallet connected before this component mounted is part
	// of the identity the first probe is recorded under, not a null that a
	// later announcement would look like a swap away from. Normalized on both
	// paths so a checksummed re-announcement of the same account is the same
	// VALUE, and React's bail-out actually fires. (config.js is already in
	// this chunk — api.js imports it — so this adds no bundle weight.)
	const [walletAddr, setWalletAddr] = useState(() =>
		normalizeWalletAddress(getAddress()),
	);

	useEffect(() => {
		const onWalletChanged = (event) =>
			setWalletAddr(normalizeWalletAddress(event?.detail?.address));
		window.addEventListener("wallet-changed", onWalletChanged);
		return () => window.removeEventListener("wallet-changed", onWalletChanged);
	}, []);

	useEffect(() => {
		fetchFeatures()
			.then((next) => {
				setFeatures(next);
				setRoute(currentRoute(next));
			})
			.catch(() => {});
	}, []);

	useEffect(() => {
		if (route.kind !== "redirect") return;
		window.history.replaceState({}, "", route.redirect);
		setRoute(currentRoute(features));
	}, [route, features]);

	useEffect(() => {
		const onPopState = () => setRoute(currentRoute(features));
		window.addEventListener("popstate", onPopState);
		return () => window.removeEventListener("popstate", onPopState);
	}, [features]);

	// Analytics category (#1647). The per-tab dedupe marker AND the beacon it
	// guards are both suppressed when analytics consent is withheld — which
	// includes the state before any choice is made. Suppressing only the
	// marker would have been worse than useless: the beacon would then fire on
	// every route change. Re-runs when the recorded choice changes, so
	// accepting analytics reports the landing without needing a reload.
	useEffect(() => {
		const LANDED_KEY = "archimedes_landed";
		if (!canStore(LANDED_KEY)) return;
		try {
			if (sessionStorage.getItem(LANDED_KEY)) return;
			sessionStorage.setItem(LANDED_KEY, "1");
		} catch {
			// Storage may be blocked; metric stays best-effort.
		}
		fetch(`${API_BASE}/api/metrics/funnel/event`, {
			method: "POST",
			credentials: "include",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ stage: "landed" }),
		}).catch(() => {});
	}, [consent]);

	useEffect(() => {
		// Anonymous-OK app pages never bounce to sign-in; auth is required only
		// to generate, pay, or paper-deploy. Keep aligned with nginx carve-outs.
		//
		// `insights` is EXCLUDED here too (owner directive 2026-08-20): a
		// client-side navigation onto /app/insights must land on the identical
		// not-found treatment a truly unknown path gets, never a sign-in
		// prompt — bouncing to /sign-in?next=/app/insights would itself
		// advertise that the page exists and is worth signing in for. (nginx
		// gates the FIRST-LOAD request for that path exactly like every other
		// /app path, so a direct anonymous GET is indistinguishable from a GET
		// for any unknown /app URL; this branch covers the in-app navigation
		// case, which never reaches nginx at all.)
		if (route.kind !== "app" || route.anonymousOk || route.page === "insights" || authLoading || user)
			return;
		const next = `${window.location.pathname}${window.location.search}`;
		window.location.replace(`/sign-in?next=${encodeURIComponent(next)}`);
	}, [route.kind, route.anonymousOk, route.page, authLoading, user]);

	const userId = user?.id ?? null;
	useEffect(() => {
		// Re-probe every time navigation LANDS on insights (not on every
		// render while already there), and on a wallet swap that happens WHILE
		// already sitting on insights (walletAddr dep, see above) — route.page
		// alone would miss that transition entirely.
		//
		// #1648 / I-8 B2: this used to hard-reset to `null` on entry, and
		// `null` renders the not-found treatment — so an admin saw a visible
		// NotFound → dashboard flip on every single entry, and again on every
		// tab-focus `accountsChanged`. The fix is NOT to assume admin while
		// waiting (that would render the gated dashboard to whoever asks); it
		// is to seed from the last answer THIS SERVER actually gave for THIS
		// exact identity (account + connected wallet — see
		// insightsAdminMemo.js for why the key has to include the wallet), and
		// to render a quiet holding state rather than a decision when there is
		// no such answer. A key miss still yields `null`, so a swap to an
		// unseen wallet resolves from the server exactly as before.
		if (route.page !== "insights") {
			setInsightsAdmin(null);
			return;
		}
		const identity = adminIdentityKey(userId, walletAddr);
		let cancelled = false;
		setInsightsAdmin(readInsightsAdmin(identity));
		fetchAdminProbe().then((result) => {
			if (cancelled) return;
			const admin = resolveInsightsAdminState(result);
			rememberInsightsAdmin(identity, admin);
			setInsightsAdmin(admin);
		});
		return () => {
			cancelled = true;
		};
	}, [route.page, walletAddr, userId]);

	useEffect(() => {
		const titles = {
			landing: "Archimedes",
			explore: "Explore · Archimedes",
			leaderboard: "Leaderboard · Archimedes",
			generate: "Generate · Archimedes",
			architecture: "Architecture · Archimedes",
			security: "Security · Archimedes",
			library: "Library · Archimedes",
			corpus: "Corpus · Archimedes",
			quant: "Quant Lab · Archimedes",
			portfolio: "Portfolio · Archimedes",
			reasoning: "Reasoning · Archimedes",
			learnings: "Learnings · Archimedes",
			insights: "Insights · Archimedes",
			account: "Account · Archimedes",
			"vault-detail": "Vault · Archimedes",
			strategy: "Strategy · Archimedes",
			paper: "Paper Trading · Archimedes",
			"sign-in": "Sign in · Archimedes",
			"sign-up": "Create account · Archimedes",
			"reset-password": "Reset password · Archimedes",
			// resolveRoute() returns page === null for not-found, so this branch used
			// to fall through to the bare 'Archimedes' title — byte-identical to the
			// landing page, leaving a screen-reader or many-tabs user unable to tell
			// a failed deep link from home (2.4.2 Page Titled).
			"not-found": "Page not found · Archimedes",
		};
		// A denied OR still-resolving insights probe titles the tab identically
		// to a real 404 — "do not advertise existence" applies to the tab title
		// too, not just the rendered page (a many-tabs user should not be able
		// to tell "unknown route" from "gated route I'm not allowed on" — or
		// from "gate still resolving" — apart). isInsightsPageBlocked treats
		// `null` (unresolved) the same as `false` (denied) here (round 3 fix):
		// titling the tab "Insights · Archimedes" while the probe is still in
		// flight was itself a disclosure a genuinely unknown route never makes.
		const deniedInsights = isInsightsPageBlocked(route.page, insightsAdmin);
		const key = route.kind === "not-found" || deniedInsights ? "not-found" : route.page;
		document.title = titles[key] ?? "Archimedes";
	}, [route.kind, route.page, insightsAdmin]);

	useEffect(() => {
		const currentCanonical = document.querySelector('link[rel="canonical"]');
		if (route.kind !== "public") {
			currentCanonical?.remove();
			return;
		}

		const canonical = currentCanonical ?? document.createElement("link");
		canonical.rel = "canonical";
		const canonicalPaths = {
			architecture: "/architecture",
			security: "/security",
		};
		canonical.href = new URL(
			canonicalPaths[route.page] ?? "/",
			window.location.origin,
		).href;
		if (!currentCanonical) document.head.append(canonical);
	}, [route.kind, route.page]);

	const navigateToPage = useCallback(
		(page, options = {}) => {
			const path = pageToPath(page, options);
			if (`${window.location.pathname}${window.location.search}` !== path) {
				window.history[options.replace ? "replaceState" : "pushState"](
					{},
					"",
					path,
				);
			}
			setRoute(
				resolveRoute(
					window.location.pathname,
					window.location.search,
					features,
				),
			);
		},
		[features],
	);

	if (route.kind === "redirect") return null;
	if (route.kind === "auth") {
		return <AuthPage mode={route.page} oauthError={route.error} />;
	}

	if (route.kind === "public") {
		let content = <Landing onNavigate={navigateToPage} />;
		if (route.page === "security") content = <Security />;
		if (route.page === "architecture") {
			content = <Architecture onNavigate={navigateToPage} />;
		}
		return <PublicLayout user={user}>{content}</PublicLayout>;
	}

	// The not-found body lives in ./components/NotFound so the admin gate
	// below renders the byte-identical treatment (see that file). The rebrand
	// restyled this branch's markup in place; the markup moved wholesale into
	// NotFound.jsx rather than being duplicated back here — two copies is
	// exactly the drift this extraction exists to prevent.
	if (route.kind === "not-found") {
		return <NotFound user={user} />;
	}

	if (route.kind === "app" && route.page === "insights") {
		// Server-truth admin gate (owner directive 2026-08-20, supersedes
		// #1028 D8): a DENIED probe renders EXACTLY the not-found page — the
		// same component the true 404 above uses — never a "you need admin
		// access" message, which would itself confirm the page exists.
		//
		// #1648 / I-8 B2 changes only the UNRESOLVED (`null`) branch, and only
		// inside a session. Round 3 had collapsed unresolved into denied so
		// that a chrome-free loader could not be used to tell "gated route"
		// from "unknown route"; that reasoning holds for an anonymous caller
		// and resolveInsightsView keeps it exactly (`hasSession === false` →
		// not-found, first paint identical to an unknown route, matching what
		// nginx's pre-auth gate already returns for both). What it cost was
		// paid entirely by the admin: `null` on every entry meant a visible
		// NotFound → dashboard flip on a page they are allowed to use. Inside
		// a session, unresolved now renders a quiet neutral holding state that
		// asserts nothing — not the dashboard (which would leak the page to
		// whoever waited), not a denial (which is a claim the server has not
		// made yet). See resolveInsightsView for the residual this leaves.
		const view = resolveInsightsView(
			route.page,
			insightsAdmin,
			authLoading || Boolean(user),
		);
		if (view === "not-found") return <NotFound user={user} />;
		if (view === "resolving") {
			return (
				<main
					className="min-h-screen grid place-items-center"
					aria-busy="true"
				>
					{/* Deliberately the same neutral wording the auth-resolution
					    loader below uses, and deliberately NOT "Loading
					    Insights…" — the holding state must not name a page the
					    server has not yet said this visitor may see. */}
					Loading…
				</main>
			);
		}
		// admin === true falls through to the normal authenticated render below.
	}

	// Anonymous-OK pages render immediately with user === null rather than
	// blocking on auth resolution. Signed-in chrome upgrades when auth resolves.
	if (!route.anonymousOk && (authLoading || !user)) {
		return (
			<main className="min-h-screen grid place-items-center">
				Loading account…
			</main>
		);
	}

	return (
		<Suspense
			fallback={
				<main className="min-h-screen grid place-items-center">
					Loading application…
				</main>
			}
		>
			<AuthenticatedApp
				route={route}
				features={features}
				navigateToPage={navigateToPage}
				user={user}
			/>
		</Suspense>
	);
}
