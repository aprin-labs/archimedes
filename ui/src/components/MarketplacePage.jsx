import { useCallback, useEffect, useState } from "react";

import { apiGet } from "../api";

function shortAddr(address) {
	return address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "Unknown";
}

function fmtTime(iso) {
	if (!iso) return "";
	return new Intl.DateTimeFormat(undefined, {
		month: "short",
		day: "numeric",
		hour: "2-digit",
		minute: "2-digit",
	}).format(new Date(iso));
}

export default function MarketplacePage({ onNavigate }) {
	const [strategies, setStrategies] = useState([]);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");

	const load = useCallback(async () => {
		setLoading(true);
		setError("");
		try {
			setStrategies(await apiGet("/api/marketplace/published"));
		} catch (err) {
			setError(err.message || "Marketplace could not load");
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		load();
	}, [load]);

	const openStrategy = (event, strategyId) => {
		if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
			return;
		event.preventDefault();
		onNavigate("market-strategy", { strategyId });
	};

	return (
		<div className="page-panel marketplace-page">
			<header className="app-page-heading">
				<p className="app-eyebrow">Copy-trading research</p>
				<h1>Marketplace</h1>
				<p>
					Browse published strategies and inspect creator, pool, and subscriber
					state before you consider a subscription.
				</p>
			</header>

			{loading && (
				<div className="card marketplace-state" aria-live="polite">
					<span className="label">Loading marketplace…</span>
					<div className="marketplace-skeleton" aria-hidden="true" />
				</div>
			)}

			{!loading && error && (
				<div className="card marketplace-state" role="alert">
					<h2>Marketplace unavailable</h2>
					<p>{error}. Check the connection, then retry.</p>
					<button type="button" className="btn btn-outline" onClick={load}>
						Retry
					</button>
				</div>
			)}

			{!loading && !error && strategies.length === 0 && (
				<div className="card marketplace-state">
					<p className="label">No published strategies</p>
					<h2>Marketplace is empty.</h2>
					<p>
						Publish a rigor-gated strategy when you have a funded testnet vault.
					</p>
					<button
						type="button"
						className="btn btn-primary"
						onClick={() => onNavigate("publish")}
					>
						Review publish flow
					</button>
				</div>
			)}

			{!loading && !error && strategies.length > 0 && (
				<div className="marketplace-grid">
					{strategies.map((strategy) => (
						<a
							key={strategy.strategy_id}
							className="card marketplace-card-link"
							href={`/app/marketplace/strategy/${encodeURIComponent(strategy.strategy_id)}`}
							onClick={(event) => openStrategy(event, strategy.strategy_id)}
						>
							<div className="marketplace-card__header">
								<h2>{strategy.strategy_id}</h2>
								<span className="tag tag-muted">
									{strategy.subscriber_count} subscriber
									{strategy.subscriber_count === 1 ? "" : "s"}
								</span>
							</div>
							<dl>
								<div>
									<dt>Creator</dt>
									<dd>{shortAddr(strategy.creator_wallet)}</dd>
								</div>
								<div>
									<dt>Pool</dt>
									<dd>{shortAddr(strategy.pool_id)}</dd>
								</div>
								<div>
									<dt>Published</dt>
									<dd>{fmtTime(strategy.created_at)}</dd>
								</div>
							</dl>
							{strategy.events?.length > 0 && (
								<p>Latest event: {strategy.events[0].type}</p>
							)}
						</a>
					))}
				</div>
			)}
		</div>
	);
}
