import { useState, useEffect, useCallback } from "react";
import {
	publicClient,
	VAULT_ABI,
	VAULT_FACTORY_ABI,
	NEW_CONTRACTS,
	getUsdcBalance,
} from "../config";
import { apiGet } from "../api";
import { anchorState } from "../trace-binding";
import PaymentReceipts from "./PaymentReceipts";
import RegimePanel from "./RegimePanel";
import StressScenarioPanel from "./StressScenarioPanel";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// /portfolio — Personal dashboard. YOUR AUM, YOUR vaults, YOUR traces.
// The vault marketplace (every vault ever deployed) briefly lived at
// /marketplace but was cut on 2026-05-25: bot-seeded deploys dragged the
// surface down and there was no time to fix it before submission. Discovery
// happens via /library (curated examples) instead.

function timeAgo(iso) {
	if (!iso) return "—";
	const d = typeof iso === "string" ? new Date(iso) : new Date(iso * 1000);
	// A missing timestamp often round-trips as epoch 0 rather than an empty
	// value — that's not a real multi-decade-old event, it's an absent
	// timestamp, so render it the same as the no-value case.
	if (Number.isNaN(d.getTime()) || d.getTime() <= 0) return "—";
	const secs = Math.floor((Date.now() - d.getTime()) / 1000);
	if (secs < 60) return `${secs}s ago`;
	if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
	if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
	return `${Math.floor(secs / 86400)}d ago`;
}

function shortAddr(a) {
	return a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "—";
}

// Vault names default to the backing paper's title (e.g. "Volatility-Managed
// Portfolios" — Moreira & Muir 2017), which is plural because that's the real
// paper title, not because there are multiple vaults. Displayed verbatim as a
// single vault's card title, it reads as if the item represents a collection
// of portfolios rather than the one vault it actually is. Append a "Vault"
// suffix so each card reads as a single, specific position — "Volatility-
// Managed Portfolios Vault" — without touching the underlying on-chain name.
function vaultDisplayName(name, address) {
	if (!name) return `Vault ${shortAddr(address)}`;
	return /\bvault\b/i.test(name) ? name : `${name} Vault`;
}

// Collapse runs of identical "skip" traces (same decision_type + trigger)
// into a single summary row. Recent reality: the AMM pools are dry while the
// liquidity-bootstrap fix is still in flight, so the agent ticks emit
// repeated "Swap skipped — thin pool" traces every cycle. Honest signal,
// noisy display — group them so one real rebalance isn't buried.
function groupRepeatedSkips(traces) {
	const out = [];
	for (const t of traces) {
		const last = out[out.length - 1];
		const isSkip = t.decision_type === "skip";
		const sameAsLast =
			last &&
			last._isGroup &&
			last.decision_type === t.decision_type &&
			last.trigger === t.trigger;
		if (isSkip && sameAsLast) {
			last._count += 1;
			last._oldest_ts = t.timestamp;
		} else if (isSkip) {
			out.push({
				...t,
				_isGroup: true,
				_count: 1,
				_newest_ts: t.timestamp,
				_oldest_ts: t.timestamp,
			});
		} else {
			out.push(t);
		}
	}
	return out;
}

export default function Portfolio({
	walletAddr,
	onSelectVault,
	onSelectTrace,
	onNavigate,
}) {
	const [userVaults, setUserVaults] = useState([]);
	const [walletUsdc, setWalletUsdc] = useState(null); // null = not loaded yet; show "—"
	const [agentStatus, setAgentStatus] = useState(null);
	const [recentTraces, setRecentTraces] = useState([]);
	const [tracesLoading, setTracesLoading] = useState(false);
	// Traces default to a short list (3) so the feed doesn't grow into a long
	// scrollbar as agent activity accumulates — expand on demand.
	const [tracesExpanded, setTracesExpanded] = useState(false);
	const [vaultsLoading, setVaultsLoading] = useState(false);
	// True when at least one vault's on-chain reads reverted (e.g. totalAssets()
	// on a stale oracle) so the row is a placeholder rather than fully fetched.
	const [vaultsPartial, setVaultsPartial] = useState(false);

	// Load user's own vaults (wallet-gated, from on-chain via VaultFactory.getVaultsByCreator).
	// This is the personal surface; we deliberately do NOT pull /api/vaults/ here —
	// the marketplace listing lives on /marketplace now.
	//
	// Per vault we read totalAssets (current USDC value), totalSupply (cumulative
	// shares minted), and balanceOf(walletAddr) (the connected wallet's share
	// count). Together they give us a price-per-share and an unrealized PnL for
	// the user's position relative to the 1:1 deposit basis ERC-4626 vaults
	// start at. PnL is approximate vs deposit-time basis — accurate while shares
	// were minted at PPS≈1.0 (true for new vaults; degrades as PPS drifts and
	// additional deposits land at the drifted PPS).
	const loadVaults = useCallback(async () => {
		const factoryAddr = NEW_CONTRACTS.vaultFactory;
		if (!factoryAddr || !walletAddr) {
			setUserVaults([]);
			setVaultsPartial(false);
			return;
		}
		setVaultsLoading(true);
		try {
			const creatorVaults = await publicClient.readContract({
				address: factoryAddr,
				abi: VAULT_FACTORY_ABI,
				functionName: "getVaultsByCreator",
				args: [walletAddr],
			});
			// Per-vault reads are independent: a revert on one vault (e.g. a stale
			// oracle failing totalAssets()) must NOT drop the whole list. On failure
			// we keep the vault as an `incomplete` placeholder row rather than
			// filtering it out, and flag the batch as partial so the UI can warn.
			const rows = await Promise.all(
				(creatorVaults || []).map(async (addr) => {
					try {
						const [totalAssets, totalSupply, userShares, tier, name] =
							await Promise.all([
								publicClient.readContract({
									address: addr,
									abi: VAULT_ABI,
									functionName: "totalAssets",
								}),
								publicClient.readContract({
									address: addr,
									abi: VAULT_ABI,
									functionName: "totalSupply",
								}),
								publicClient.readContract({
									address: addr,
									abi: VAULT_ABI,
									functionName: "balanceOf",
									args: [walletAddr],
								}),
								publicClient.readContract({
									address: addr,
									abi: VAULT_ABI,
									functionName: "tier",
								}),
								publicClient
									.readContract({
										address: addr,
										abi: VAULT_ABI,
										functionName: "name",
									})
									.catch(() => ""),
							]);
						const aum = Number(totalAssets) / 1e6;
						const supply = Number(totalSupply) / 1e6;
						const shares = Number(userShares) / 1e6;
						// Price-per-share: 1.0 baseline, drifts as the vault gains/loses value.
						const pps = supply > 0 ? aum / supply : 1;
						// User's current position value + unrealized PnL vs 1:1 basis.
						const userValue = shares * pps;
						const userPnlUsdc = userValue - shares; // $ above (or below) basis
						const userPnlPct = shares > 0 ? (pps - 1) * 100 : null;
						return {
							address: addr,
							aum,
							tier: Number(tier),
							name,
							shares,
							userValue,
							userPnlUsdc,
							userPnlPct,
							incomplete: false,
						};
					} catch {
						// One vault's reads reverted — keep it visible as a placeholder so the
						// list isn't emptied, and mark it so the card renders "—" / "loading".
						return { address: addr, incomplete: true };
					}
				}),
			);
			setUserVaults(rows);
			setVaultsPartial(rows.some((r) => r.incomplete));
		} catch {
			setUserVaults([]);
			setVaultsPartial(false);
		} finally {
			setVaultsLoading(false);
		}
	}, [walletAddr]);

	// Wallet USDC balance — idle funds, "ready to deploy". Polled with the
	// vault list because the two numbers compose the user's total position.
	const loadWalletUsdc = useCallback(async () => {
		if (!walletAddr) {
			setWalletUsdc(null);
			return;
		}
		const v = await getUsdcBalance(walletAddr);
		setWalletUsdc(v);
	}, [walletAddr]);

	const loadAgentAndRegime = useCallback(async () => {
		try {
			const r = await fetch(`${API_BASE}/api/agent/status`);
			if (r.ok) setAgentStatus(await r.json());
		} catch {
			// Network blip — leave prior agentStatus intact; next poll retries.
		}
		// Regime is loaded + rendered by <RegimePanel /> above, not duplicated here.
	}, []);

	// Own vaults only. Since #1556 /api/traces/ IS ownership-gated, so an
	// unfiltered call no longer returns other users' traces — but it still
	// returns the house agent's public proof surface, which is not this
	// panel's subject. `vault_address` stays the right filter for "my
	// activity"; the gate is what stops it being the only thing between the
	// reader and the platform.
	// Key on the joined address list (not the userVaults array reference)
	// so loadTraces only gets a new identity when the actual vault set
	// changes, not on every re-fetch of the same vaults.
	const vaultAddrKey = userVaults.map((v) => v.address).join(",");

	const loadTraces = useCallback(async () => {
		const addrs = vaultAddrKey ? vaultAddrKey.split(",") : [];
		if (addrs.length === 0) {
			setRecentTraces([]);
			return;
		}
		setTracesLoading(true);
		try {
			// list_traces() (traces_routes.py) only accepts a single vault_address
			// filter, not a list — one request per owned vault, merged and
			// re-sorted client-side, rather than inventing a filter shape the
			// backend doesn't support.
			// apiGet, not a bare fetch: it sends `credentials: "include"` and the
			// X-Wallet-Address header. Since #1556 those ARE the caller's
			// identity on this route — a bare fetch is an anonymous read, so the
			// user's own vault traces would be filtered out of their own
			// activity feed and the panel would silently show only house rows.
			const results = await Promise.all(
				addrs.map((addr) =>
					apiGet(
						`/api/traces/?limit=20&vault_address=${encodeURIComponent(addr)}`,
					).catch(() => ({ traces: [] })),
				),
			);
			const tsMs = (t) => {
				const d = new Date(t);
				return Number.isNaN(d.getTime()) ? 0 : d.getTime();
			};
			const merged = results.flatMap((d) => d.traces || []);
			merged.sort((a, b) => tsMs(b.timestamp) - tsMs(a.timestamp));
			setRecentTraces(merged.slice(0, 20));
		} catch {
			// Network blip — leave prior recentTraces intact; next poll retries.
		}
		setTracesLoading(false);
	}, [vaultAddrKey]);

	useEffect(() => {
		loadVaults();
		loadWalletUsdc();
	}, [loadVaults, loadWalletUsdc]);
	useEffect(() => {
		loadAgentAndRegime();
		loadTraces();
	}, [loadAgentAndRegime, loadTraces]);
	useEffect(() => {
		const t = setInterval(() => {
			loadAgentAndRegime();
			loadTraces();
			loadWalletUsdc();
			loadVaults();
		}, 30_000);
		return () => clearInterval(t);
	}, [loadAgentAndRegime, loadTraces, loadWalletUsdc, loadVaults]);

	// YOUR AUM — sum of the connected wallet's OWN share value across vaults it
	// created, not each vault's totalAssets() (which includes every depositor).
	// Vault.deposit() has no access-control modifier, so a vault the wallet
	// created can hold other depositors' USDC too; using totalAssets() here
	// would overstate the creator's own exposure. userValue (below) is already
	// scoped to the wallet's own shares — reuse it instead of re-deriving from
	// aum. Placeholder rows for vaults whose reads reverted contribute 0 (their
	// value is unknown), so a stale-oracle vault doesn't poison the total with
	// NaN. Wallet-disconnected users see 0; wallet-connected users see real $ at risk.
	const yourAum = userVaults.reduce((s, v) => s + (v.userValue || 0), 0);
	const hasVaults = userVaults.length > 0;

	// Aggregate unrealized PnL across the user's vault positions (positions they
	// hold shares in, not just vaults they created). Honest "—" when there are
	// no shares to compute against.
	const totalShares = userVaults.reduce((s, v) => s + (v.shares || 0), 0);
	const totalPositionValue = userVaults.reduce(
		(s, v) => s + (v.userValue || 0),
		0,
	);
	const aggregatePnlUsdc =
		totalShares > 0 ? totalPositionValue - totalShares : null;
	const aggregatePnlPct =
		totalShares > 0 ? (totalPositionValue / totalShares - 1) * 100 : null;

	// Format helpers — "—" everywhere a number isn't computable so we never
	// show a misleading 0.
	const fmtUsd = (n) =>
		n == null
			? "—"
			: `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
	const fmtPct = (n) =>
		n == null ? "—" : `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
	const pnlColor = (n) =>
		n == null
			? "var(--text-3)"
			: n > 0
				? "var(--positive)"
				: n < 0
					? "var(--negative)"
					: "var(--text-1)";

	return (
		<div className="portfolio-page">
			<header className="app-page-heading portfolio-heading">
				<div>
					<p className="app-eyebrow">Owned positions</p>
					<h1>Portfolio</h1>
					<p>
						Your USDC, vault positions, and the agent's rebalance record. Every
						action stays attached to a reasoning trace you can inspect.
					</p>
				</div>
				{/* Regime context — small pill; full breakdown lives on /learnings. */}
				<RegimePanel compact />
			</header>

			{/* Account header — the four numbers a wallet-connected user wants to
          see at a glance. Wallet USDC = idle, ready to deploy. Vault AUM =
          deployed at risk. Unrealized PnL = since-deposit drift (approximate;
          tooltip explains). Agent = system status, since live execution
          depends on it. */}
			<div className="portfolio-ledger">
				<div className="card-flat p-4">
					<div className="label mb-2">Wallet USDC</div>
					<div className="font-bold acct-stat-value">{fmtUsd(walletUsdc)}</div>
					<div className="caption mt-1.5">Idle — ready to deploy</div>
				</div>
				<div className="card-flat p-4">
					<div className="label mb-2">Vault AUM</div>
					<div className="font-bold acct-stat-value">{fmtUsd(yourAum)}</div>
					<div className="caption mt-1.5">
						Across {userVaults.length}{" "}
						{userVaults.length === 1 ? "vault" : "vaults"}
					</div>
				</div>
				<div
					className="card-flat p-4"
					title="Unrealized PnL is the current value of your vault shares minus the 1:1 USDC basis they were minted at. Approximate while shares were minted at PPS≈1.0 (true for new vaults; drifts as PPS does)."
				>
					<div className="label mb-2">Unrealized PnL</div>
					<div
						className="font-bold acct-stat-value"
						style={{ color: pnlColor(aggregatePnlUsdc) }}
					>
						{fmtUsd(aggregatePnlUsdc)}
					</div>
					<div
						className="caption mt-1.5"
						style={{ color: pnlColor(aggregatePnlPct) }}
					>
						{fmtPct(aggregatePnlPct)} since deposit
					</div>
				</div>
				<div className="card-flat p-4">
					<div className="label mb-2">Agent</div>
					<div className="flex items-center gap-2 font-bold acct-stat-value">
						<span
							className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${agentStatus?.alive ? "bg-[var(--positive)] shadow-[0_0_6px_var(--positive)]" : "bg-[var(--negative)]"}`}
						/>
						{agentStatus?.alive ? "Alive" : "Offline"}
					</div>
					<div className="caption mt-1.5">
						{agentStatus?.last_heartbeat
							? `Heartbeat ${timeAgo(agentStatus.last_heartbeat)}`
							: "No heartbeat yet"}
					</div>
				</div>
			</div>

			<div className="portfolio-workspace">
				<section className="portfolio-positions">
					{/* Your Vaults — the hero. Empty state stays tight: one line + two
          buttons. We used to center-align with 24px padding and lots of
          vertical air; the wallet-connected user lands here, sees the empty
          state, and just wants to know what to do next. */}
					{!vaultsLoading && !hasVaults && (
						<div
							className="card mb-7 flex flex-wrap items-center justify-between gap-3"
							style={{ padding: "16px 18px" }}
						>
							<div style={{ flex: "1 1 280px" }}>
								<div className="label mb-1">Your Vault Positions</div>
								<p
									className="body"
									style={{ margin: 0, color: "var(--text-3)" }}
								>
									No vaults yet. Generate a paper-grounded strategy or browse
									the example library to deploy your first non-custodial vault.
								</p>
							</div>
							<div className="flex gap-2 flex-wrap">
								<button
									className="btn btn-primary btn-sm"
									onClick={() => onNavigate?.("generate")}
								>
									Generate →
								</button>
								<button
									className="btn btn-outline btn-sm"
									onClick={() => onNavigate?.("library")}
								>
									Browse Library
								</button>
							</div>
						</div>
					)}

					{vaultsLoading && (
						<div className="mb-7">
							<div className="label mb-3">Your Vault Positions</div>
							<div className="caption">Loading vaults…</div>
						</div>
					)}

					{hasVaults && (
						<div className="mb-7">
							<div className="label mb-3">Your Vault Positions</div>
							{/* Non-blocking warning — some vaults couldn't be fully read (usually a
              stale-oracle revert on totalAssets()). The affected rows still show
              below as placeholders; the next 30s poll retries them. */}
							{vaultsPartial && (
								<div
									className="card mb-3 flex items-center gap-2"
									style={{ padding: "10px 14px", color: "var(--text-3)" }}
								>
									<span
										className="i-lucide-alert-triangle w-3.5 h-3.5 flex-shrink-0"
										style={{ color: "var(--warning, var(--negative))" }}
									/>
									<span className="caption">
										Some vaults couldn't be fully loaded — showing what we have.
										This is usually a transient on-chain read (e.g. a stale
										oracle) and retries automatically.
									</span>
								</div>
							)}
							<div className="portfolio-vault-grid">
								{userVaults.map((v) =>
									v.incomplete ? (
										// Placeholder row for a vault whose reads reverted — kept visible
										// instead of dropped, so the user never sees a phantom "0 vaults".
										<div
											key={v.address}
											className="card vault-card-clickable"
											onClick={() => onSelectVault?.(v.address)}
											style={{ padding: 18 }}
											title="This vault's on-chain data couldn't be read right now — retrying."
										>
											<div className="flex justify-between items-center mb-2">
												<span
													className="font-semibold"
													style={{ fontSize: "0.95rem" }}
												>
													{`Vault ${shortAddr(v.address)}`}
												</span>
												<span className="tag tag-muted">
													<span className="i-lucide-clock w-3.5 h-3.5" />{" "}
													Loading
												</span>
											</div>
											<div className="flex items-baseline gap-2 mt-3 mb-1">
												<span
													className="text-[1.5rem] font-bold"
													style={{ color: "var(--text-3)" }}
												>
													—
												</span>
												<span className="caption">AUM</span>
											</div>
											<div className="flex justify-between items-center mt-2 flex-wrap gap-2">
												<code
													className="caption"
													style={{ color: "var(--text-3)" }}
												>
													{shortAddr(v.address)}
												</code>
												<span
													className="caption"
													style={{ color: "var(--text-3)" }}
													title="On-chain read pending"
												>
													—
												</span>
											</div>
										</div>
									) : (
										<div
											key={v.address}
											className="card vault-card-clickable"
											onClick={() => onSelectVault?.(v.address)}
											style={{ padding: 18 }}
										>
											<div className="flex justify-between items-center mb-2">
												<span
													className="font-semibold"
													style={{ fontSize: "0.95rem" }}
												>
													{vaultDisplayName(v.name, v.address)}
												</span>
												<span
													className={`tag ${v.tier === 1 ? "tag-accent" : "tag-muted"}`}
												>
													{v.tier === 1 ? (
														<>
															<span className="i-lucide-trophy w-3.5 h-3.5" />{" "}
															Verified
														</>
													) : (
														<>
															<span className="i-lucide-users w-3.5 h-3.5" />{" "}
															Community
														</>
													)}
												</span>
											</div>
											<div className="flex items-baseline gap-2 mt-3 mb-1">
												<span className="text-[1.5rem] font-bold">
													$
													{v.userValue.toLocaleString(undefined, {
														minimumFractionDigits: 2,
														maximumFractionDigits: 2,
													})}
												</span>
												<span className="caption">Your value</span>
											</div>
											<div className="flex justify-between items-center mt-2 flex-wrap gap-2">
												<code
													className="caption"
													style={{ color: "var(--text-3)" }}
												>
													{shortAddr(v.address)}
												</code>
												{v.shares > 0 ? (
													<span
														className="caption font-medium"
														style={{ color: pnlColor(v.userPnlUsdc) }}
														title={`Your shares: ${v.shares.toFixed(2)} • PPS: ${(v.userValue / v.shares).toFixed(4)}`}
													>
														{fmtPct(v.userPnlPct)}
													</span>
												) : (
													<span
														className="caption"
														style={{ color: "var(--text-3)" }}
														title="You don't hold shares in this vault"
													>
														—
													</span>
												)}
											</div>
										</div>
									),
								)}
							</div>
						</div>
					)}

					{/* Stress scenarios — collapsed by default, only when user has vaults */}
					{hasVaults && (
						<div className="portfolio-stress">
							<details className="card" style={{ padding: 0 }}>
								<summary
									className="label cursor-pointer flex items-center gap-1.5"
									style={{ padding: "14px 18px" }}
								>
									<span className="i-lucide-flame w-3.5 h-3.5" /> Stress
									Scenarios
								</summary>
								<div style={{ padding: "0 18px 18px" }}>
									<StressScenarioPanel
										allocations={[]}
										usdcWeight={0}
										portfolioValue={yourAum || 10000}
									/>
								</div>
							</details>
						</div>
					)}
				</section>

				{/* Agent activity feed — real traces from /api/traces */}
				<section className="portfolio-activity">
					<div className="label mb-3">Your Agent's Trace</div>
					{/* Only show the loading label before the FIRST successful load —
            loadTraces() also re-runs on the 30s poll, and gating this purely
            on `tracesLoading` meant "Loading traces…" re-appeared above the
            already-rendered trace list on every background refresh, reading
            as permanently stuck rather than a live-updating feed. */}
					{tracesLoading && recentTraces.length === 0 && (
						<div className="caption">Loading traces…</div>
					)}
					{!tracesLoading && recentTraces.length === 0 && (
						<div className="card" style={{ padding: 18 }}>
							<p className="body" style={{ marginBottom: 6 }}>
								No agent activity yet.
							</p>
							<p className="caption">
								Deploy a vault to see agent decisions here, or{" "}
								<a
									onClick={() => onSelectTrace?.(undefined)}
									style={{
										color: "var(--accent)",
										cursor: "pointer",
										textDecoration: "underline",
									}}
								>
									browse all traces on the Reasoning page
								</a>
								.
							</p>
						</div>
					)}
					{recentTraces.length > 0 &&
						(() => {
							const groupedTraces = groupRepeatedSkips(recentTraces);
							const TRACES_DEFAULT_LIMIT = 3;
							const visibleTraces = tracesExpanded
								? groupedTraces
								: groupedTraces.slice(0, TRACES_DEFAULT_LIMIT);
							const hiddenCount = groupedTraces.length - visibleTraces.length;
							return (
								<>
									<div className="flex flex-col gap-2">
										{visibleTraces.map((t) => {
											// Group rows render compactly — "N skipped" + window — to keep
											// a real rebalance from being buried under repeated thin-pool
											// notices. Click jumps to Reasoning for the full audit.
											if (t._isGroup && t._count > 1) {
												return (
													<div
														key={`group-${t.id}`}
														className="trace-card vault-card-clickable"
														onClick={() => onNavigate?.("reasoning")}
														style={{ cursor: "pointer" }}
														title="Open the full trace audit on Reasoning"
													>
														<div className="flex justify-between items-center gap-3 flex-wrap">
															<div className="flex items-center gap-2 flex-wrap">
																<span className="tag tag-warning capitalize">
																	{t._count}× skip
																</span>
																<strong style={{ fontSize: "0.9rem" }}>
																	{t.trigger}
																</strong>
															</div>
															<span className="caption">
																{t._oldest_ts && t._newest_ts
																	? `${timeAgo(t._oldest_ts)} → ${timeAgo(t._newest_ts)}`
																	: t._newest_ts
																		? timeAgo(t._newest_ts)
																		: ""}
															</span>
														</div>
														<div
															className="caption mt-1.5 leading-relaxed"
															style={{ color: "var(--text-3)" }}
														>
															Agent declined to swap on {t._count} consecutive
															ticks — pool reserves below threshold. The skip
															itself is recorded honestly rather than faking a
															trade.{" "}
															<span style={{ color: "var(--accent)" }}>
																Open Reasoning →
															</span>
														</div>
													</div>
												);
											}
											return (
												<div
													key={t.id}
													className="trace-card vault-card-clickable"
													onClick={() => onSelectTrace?.(t.id)}
													style={{ cursor: "pointer" }}
													title={`Click to view trace ${t.id}`}
												>
													<div className="flex justify-between items-center gap-3 flex-wrap">
														<div>
															<span
																className={`tag mr-2 capitalize ${t.decision_type === "skip" ? "tag-warning" : t.decision_type === "rebalance" ? "tag-positive" : "tag-muted"}`}
															>
																{t.decision_type}
															</span>
															<strong style={{ fontSize: "0.9rem" }}>
																{t.trigger}
															</strong>
														</div>
														<span className="caption">
															{t.timestamp ? timeAgo(t.timestamp) : ""}
														</span>
													</div>
													{t.reasoning && (
														<div className="caption mt-1.5 leading-relaxed">
															{t.reasoning.slice(0, 180)}
															{t.reasoning.length > 180 ? "…" : ""}
														</div>
													)}
													<div className="caption mt-1.5 flex gap-3 text-[var(--text-3)]">
														{t.vault_address && (
															<span>vault {shortAddr(t.vault_address)}</span>
														)}
														{t.arc_tx_hash ? (
															<a
																href={`https://testnet.arcscan.app/tx/${t.arc_tx_hash}`}
																target="_blank"
																rel="noopener noreferrer"
																className="mono underline decoration-dotted underline-offset-2 hover:text-[var(--accent)] transition-colors"
																onClick={(e) => e.stopPropagation()}
															>
																{t.arc_tx_hash.slice(0, 10)}… ↗
															</a>
														) : t.trace_hash ? (
															<span className="mono">
																{t.trace_hash.slice(0, 10)}…
															</span>
														) : null}
														{/* Four honest states, one shared derivation
														    (src/trace-binding.js). This SUBSUMES the inline
														    three-state ternary #714 landed on main: that
														    ternary was already right about SKIP ("not
														    anchored (no trade to bind)" — a skip has no
														    trade for commit() to bind, so no registry write
														    is ever attempted), and `anchorState` keeps its
														    exact labels, icons and tooltips, pinned
														    character-for-character in
														    ui/test/anchor-state.test.js. What it adds is the
														    fourth state main still lacked: the registry-only
														    `anchored_only` projection, which main rendered as
														    a plain green check — i.e. as a hash comparison
														    that never happened (#1407). */}
														{(() => {
															const a = anchorState(t);
															return (
																<span
																	className={`flex items-center gap-1 ${a.tone === "verified" ? "text-[var(--positive)]" : "text-[var(--text-3)]"}`}
																	title={a.title}
																	data-anchor-state={a.state}
																>
																	<span className={`${a.icon} w-3.5 h-3.5`} />{" "}
																	{a.label}
																</span>
															);
														})()}
													</div>
												</div>
											);
										})}
									</div>
									{hiddenCount > 0 && (
										<button
											type="button"
											className="btn btn-outline btn-sm mt-3"
											onClick={() => setTracesExpanded(true)}
										>
											Show {hiddenCount} more
										</button>
									)}
									{tracesExpanded &&
										groupedTraces.length > TRACES_DEFAULT_LIMIT && (
											<button
												type="button"
												className="btn btn-outline btn-sm mt-3"
												onClick={() => setTracesExpanded(false)}
											>
												Show less
											</button>
										)}
								</>
							);
						})()}
				</section>

				{/* Payment receipts — settled generation payments (Dan's directive:
				    "we must provide people with their receipts"). Own records only;
				    GET /api/payments/receipts is owner-scoped server-side. */}
				<section className="portfolio-payments">
					<div className="label mb-3">Payment Receipts</div>
					<PaymentReceipts />
				</section>
			</div>
		</div>
	);
}
