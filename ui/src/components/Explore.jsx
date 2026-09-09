import { useEffect, useMemo, useState } from "react";
import AssetModal from "./AssetModal";
import AssetGroupModal from "./AssetGroupModal";
import AssetGroupIcon from "./AssetGroupIcon";
import { groupMeta } from "../assetGroups";
import {
	median,
	changeWindowLabel,
	groupChangeWindowLabel,
} from "../statUtils";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// /explore — Read-only viewer for the market data the strategy engine sees.
// No wallet required, no trade affordance. Per docs/specs/page-roles-spec.md,
// this is the discovery surface that helps a user form an opinion about what
// to ask Generate to build around.

function fmtPrice(v) {
	if (v == null || Number.isNaN(v)) return "—";
	if (v >= 1000) return `$${v.toFixed(0)}`;
	if (v >= 10) return `$${v.toFixed(2)}`;
	return `$${v.toFixed(4)}`;
}

function fmtPct(v, digits = 2) {
	if (v == null || Number.isNaN(v)) return "—";
	const sign = v >= 0 ? "+" : "";
	return `${sign}${v.toFixed(digits)}%`;
}

function changeClass(v) {
	if (v == null || Number.isNaN(v)) return "";
	return v >= 0 ? "positive" : "negative";
}

export default function Explore() {
	const [assets, setAssets] = useState([]);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [filterClass, setFilterClass] = useState("all");
	const [openAsset, setOpenAsset] = useState(null);
	const [openGroup, setOpenGroup] = useState(null);
	const [view, setView] = useState("groups");

	useEffect(() => {
		let cancelled = false;
		const load = async () => {
			try {
				const res = await fetch(`${API_BASE}/api/explore/assets`);
				if (!res.ok) throw new Error(`Backend returned ${res.status}`);
				const data = await res.json();
				if (!cancelled) {
					setAssets(data.assets || []);
					setError("");
				}
			} catch (e) {
				// Never render the response body as the error message — nginx 502s
				// come back as multi-line HTML and would splat across the page.
				// Same anti-pattern fixed in GenerationStatus.jsx (#323).
				const msg =
					e?.message && e.message.length < 120
						? e.message
						: "Failed to load assets";
				if (!cancelled) setError(msg);
			} finally {
				if (!cancelled) setLoading(false);
			}
		};
		load();
		// Reload every minute — page is read-only but oracle data drifts.
		const interval = setInterval(load, 60_000);
		return () => {
			cancelled = true;
			clearInterval(interval);
		};
	}, []);

	const classes = [
		"all",
		...Array.from(new Set(assets.map((a) => a.asset_class).filter(Boolean))),
	];
	const filtered =
		filterClass === "all"
			? assets
			: assets.filter((a) => a.asset_class === filterClass);

	// Grouped-card view (#464): one card per asset_class bucket, the same
	// grouping the filter pills above already use. Sorted by member count so
	// the largest, most-populated groups surface first.
	const groups = useMemo(() => {
		const byAssetClass = new Map();
		for (const a of assets) {
			if (!a.asset_class) continue;
			if (!byAssetClass.has(a.asset_class)) byAssetClass.set(a.asset_class, []);
			byAssetClass.get(a.asset_class).push(a);
		}
		return Array.from(byAssetClass.entries())
			.map(([assetClass, members]) => ({
				assetClass,
				members,
				meta: groupMeta(assetClass),
			}))
			.sort((a, b) => b.members.length - a.members.length);
	}, [assets]);

	// Banner only fires when *every* asset's displayed price is itself stale.
	// The backend now treats a missing on-chain oracle as "not stale" when
	// yfinance is the actual price source, so this banner is honest: it means
	// the feed pipeline is genuinely broken, not just "the oracle slot is
	// unused for this asset". See asset_market_service.py docstring.
	const allStale = assets.length > 0 && assets.every((a) => a.is_stale);
	const staleCount = assets.filter((a) => a.is_stale).length;
	// Distinguish "majority stale" (markets closed + yfinance daily-close —
	// expected) from "minority stale" (a few feeds drifting — unusual). The
	// page used to say "Most assets are current" any time someStale was true,
	// which read as a lie on weekends when 60+ of 84 cards show STALE.
	const majorityStale = !allStale && staleCount > assets.length / 2;
	const minorityStale = !allStale && !majorityStale && staleCount > 0;

	// Honest oracle-coverage count (#1371) — derived from the served assets'
	// price_source, never a literal. Today this resolves to 2 (sSPY + sBTC,
	// oracle_updater's only pushed symbols) of the ~281-asset universe; the
	// copy below must say so rather than implying oracle-primary pricing.
	const oracleBackedCount = assets.filter(
		(a) => a.price_source === "oracle",
	).length;
	const oracleCoverageNote =
		assets.length > 0
			? `on-chain oracle for ${oracleBackedCount} of the ${assets.length} assets below today`
			: "on-chain oracle for a small subset of assets today";

	return (
		<div className="explore-page">
			<header className="app-page-heading explore-heading">
				<div>
					<p className="app-eyebrow">Market discovery · read-only</p>
					<h1>Explore</h1>
					<p className="explore-heading__lede">
						Inspect prices, recent moves, and sources. Nothing here places a
						trade or moves a position.
					</p>
				</div>
				<a
					className="btn btn-primary explore-generate-link"
					href="/app/generate"
				>
					Generate a strategy
					<span
						className="i-lucide-arrow-up-right w-4 h-4"
						aria-hidden="true"
					/>
				</a>
			</header>
			{assets.length > 0 && (
				<dl className="explore-summary" aria-label="Market data coverage">
					<div>
						<dt>Assets tracked</dt>
						<dd>{assets.length}</dd>
					</div>
					<div>
						<dt>Asset groups</dt>
						<dd>{groups.length}</dd>
					</div>
					<div>
						<dt>Oracle-priced</dt>
						<dd>{oracleBackedCount}</dd>
					</div>
					<div>
						<dt>Stale feeds</dt>
						<dd>{staleCount}</dd>
					</div>
				</dl>
			)}
			<p className="explore-source-note">
				Open any entry for price history and its upstream source. Most prices
				come from yfinance — it's {oracleCoverageNote}.
			</p>

			<div className="explore-views" role="group" aria-label="Market views">
				<button
					type="button"
					className="corpus-tab"
					onClick={() => setView("groups")}
					aria-pressed={view === "groups"}
				>
					By group
				</button>
				<button
					type="button"
					className="corpus-tab"
					onClick={() => setView("assets")}
					aria-pressed={view === "assets"}
				>
					All assets
				</button>
			</div>

			{/* Filter pills — only meaningful for the flat asset list */}
			{view === "assets" && (
				<div className="strat-filter-bar" style={{ marginBottom: 18 }}>
					{/* Buttons with aria-pressed, matching the view toggles directly
              above — as click-only spans these were unreachable from the
              keyboard on the default /app landing page (2.1.1 / 4.1.2). */}
					{classes.map((c) => (
						<button
							key={c}
							type="button"
							className={`tag ${filterClass === c ? "tag-accent" : "tag-muted"}`}
							aria-pressed={filterClass === c}
							onClick={() => setFilterClass(c)}
						>
							{c === "all" ? "All" : c.replace(/_/g, " ")}
							{c !== "all" &&
								` (${assets.filter((a) => a.asset_class === c).length})`}
						</button>
					))}
				</div>
			)}

			{/* Loading / error / empty states */}
			{loading && !assets.length && (
				<div className="caption">Loading market data…</div>
			)}
			{error && !assets.length && (
				<div className="info-box warning" style={{ marginBottom: 16 }}>
					Couldn't load assets: {error}.
				</div>
			)}
			{!loading && !error && assets.length === 0 && (
				<div className="info-box" style={{ marginBottom: 16 }}>
					No market data available right now. This page refreshes automatically.
				</div>
			)}

			{/* Banner — only when something is actually wrong with the feed. */}
			{allStale && (
				<div className="info-box warning" style={{ marginBottom: 16 }}>
					Every asset's price feed is older than the freshness threshold. The
					upstream market-data pipeline appears to be paused; values shown may
					be outdated.
				</div>
			)}
			{majorityStale && (
				<div
					className="info-box"
					style={{ marginBottom: 16, fontSize: "0.84rem" }}
				>
					<strong>
						{staleCount}/{assets.length}
					</strong>{" "}
					assets show STALE — most equity / ETF feeds run on yfinance
					daily-close prices and read as stale outside the US trading window.
					24/7 markets (crypto, FX, futures) stay current.
				</div>
			)}
			{minorityStale && (
				<div
					className="info-box"
					style={{ marginBottom: 16, fontSize: "0.84rem" }}
				>
					A few assets ({staleCount} of {assets.length}) have stale price feeds
					(STALE badge on the card). Most feeds are current.
				</div>
			)}

			{/* Grouped market index */}
			{view === "groups" && groups.length > 0 && (
				<div className="explore-groups">
					{groups.map((g) => {
						const medianChange = (() => {
							const vals = g.members
								.map((a) => a.change_24h_pct)
								.filter((v) => v != null && !Number.isNaN(v));
							return median(vals);
						})();
						// Null when the members' windows disagree — a group spanning a
						// holiday genuinely has no single true window (#1378).
						const medianWindow = groupChangeWindowLabel(g.members);
						return (
							<button
								key={g.assetClass}
								type="button"
								onClick={() => setOpenGroup(g)}
								className="explore-entry explore-group"
								aria-label={`Open details for ${g.meta.label} group`}
							>
								<span className="explore-group__icon" aria-hidden="true">
									<AssetGroupIcon icon={g.meta.icon} size={22} />
								</span>
								<span className="explore-group__body">
									<strong className="explore-entry__title">
										{g.meta.label}
									</strong>
									<span className="explore-entry__meta">
										{g.members.length} asset{g.members.length === 1 ? "" : "s"}
									</span>
									<span className="explore-group__description">
										{g.meta.description}
									</span>
								</span>
								<span className="explore-group__change">
									<span className={`mono ${changeClass(medianChange)}`}>
										{fmtPct(medianChange)}
									</span>
									<span className="explore-entry__meta">
										{medianWindow ? `median ${medianWindow}` : "median change"}
									</span>
								</span>
								<span
									className="i-lucide-arrow-up-right explore-entry__arrow"
									aria-hidden="true"
								/>
							</button>
						);
					})}
				</div>
			)}

			{/* Asset card grid */}
			{view === "assets" && filtered.length > 0 && (
				<div className="explore-assets">
					{filtered.map((a) => (
						<button
							key={a.symbol}
							type="button"
							onClick={() => setOpenAsset(a)}
							className="explore-entry explore-asset"
							aria-label={`Open details for ${a.symbol}`}
						>
							<div className="explore-asset__header">
								<div>
									<div className="explore-entry__title">{a.symbol}</div>
									<div className="explore-entry__meta">{a.name || "—"}</div>
								</div>
								{a.is_stale && (
									<span
										className="tag tag-negative"
										title="The displayed price is older than the freshness window"
									>
										STALE
									</span>
								)}
							</div>

							<div className="mono explore-asset__price">
								{fmtPrice(a.current_price)}
							</div>

							<div className="explore-asset__change">
								<span
									className={`mono ${changeClass(a.change_24h_pct)}`}
									style={{ fontSize: "0.85rem" }}
									title={
										a.rejected_fields?.includes("change_24h_pct")
											? "Suppressed: the computed change was arithmetically implausible (likely a bad tick), not a real move"
											: undefined
									}
								>
									{fmtPct(a.change_24h_pct)}
								</span>
								<span
									className="explore-entry__meta"
									title={
										a.change_window_hours != null
											? `Change over the ${a.change_window_hours.toFixed(0)}h between the last two bars`
											: "Change since the previous close; the elapsed window could not be determined"
									}
								>
									{changeWindowLabel(a)}
								</span>
							</div>
						</button>
					))}
				</div>
			)}

			{/* Footer disclosure */}
			<p className="caption" style={{ marginTop: 22, color: "var(--text-4)" }}>
				{oracleBackedCount} of the {assets.length || "these"} assets above are
				priced from the on-chain PriceOracle today; the rest are priced from
				yfinance (off-chain market data) — each card's "Source" field says which
				applies to it. "STALE" means the displayed price is itself older than
				the freshness window (5 minutes for the oracle, ~4 days for daily-close
				fallback). The "Vol 30d" metric in the detail modal is annualized
				realized volatility (std of daily returns × √252).
			</p>

			{/* Data-sourcing disclosure (#1218) — see docs/adr/market-data-sourcing.md.
          Deliberately plain-language and deliberately here rather than buried in a
          legal page: the honest thing to say is that this page and the paid
          analysis run on DIFFERENT data under different terms, and a reader can
          only check that claim if we make it where the data is shown.
          Pinned by ui/test/explore-data-disclosure.test.js. */}
			<p className="caption" style={{ marginTop: 14, color: "var(--text-4)" }}>
				<strong>About this data.</strong> Explore is a free, open-source viewer
				over yfinance market-data streams. Nothing on this page is sold or
				commercially redistributed — it is here to look at, and that is the
				whole of it. Paid analysis runs on separately licensed data, not on this
				feed; the two are sourced independently on purpose.
			</p>

			{openAsset && (
				<AssetModal asset={openAsset} onClose={() => setOpenAsset(null)} />
			)}
			{openGroup && (
				<AssetGroupModal
					assetClass={openGroup.assetClass}
					assets={openGroup.members}
					onClose={() => setOpenGroup(null)}
				/>
			)}
		</div>
	);
}
