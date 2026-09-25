import { useCallback, useEffect, useState } from "react";

import { apiDelete, apiGet } from "../api";
import { getAddress } from "../config";

function shortAddr(address) {
	return address
		? `${address.slice(0, 6)}…${address.slice(-4)}`
		: "Not available";
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

export default function SubscriptionsPage({ onNavigate }) {
	const walletAddr = getAddress();
	const [subs, setSubs] = useState([]);
	const [loading, setLoading] = useState(true);
	const [error, setError] = useState("");
	const [unsubscribing, setUnsubscribing] = useState(null);
	const [confirming, setConfirming] = useState(null);

	const load = useCallback(async () => {
		setLoading(true);
		setError("");
		try {
			setSubs(await apiGet("/api/marketplace/my-subscriptions"));
		} catch (err) {
			setError(err.message || "Subscriptions could not load");
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		if (walletAddr) load();
		else setLoading(false);
	}, [walletAddr, load]);

	const handleUnsubscribe = async (strategyId) => {
		setUnsubscribing(strategyId);
		setError("");
		try {
			await apiDelete(
				`/api/marketplace/subscribe/${encodeURIComponent(strategyId)}`,
			);
			setConfirming(null);
			await load();
		} catch (err) {
			setError(err.message || "Unsubscribe failed");
		} finally {
			setUnsubscribing(null);
		}
	};

	return (
		<div className="subscriptions-page">
			<header className="app-page-heading">
				<p className="app-eyebrow">Marketplace control</p>
				<h1>Subscriptions</h1>
				<p>Review active copy-trading subscriptions and stop future charges.</p>
			</header>

			{!walletAddr ? (
				<section className="card subscription-state">
					<h2>Linked wallet required</h2>
					<p>
						Connect the wallet that owns your subscriptions to read or change
						them.
					</p>
				</section>
			) : loading ? (
				<section className="card subscription-state" aria-live="polite">
					<p>Reading subscriptions…</p>
				</section>
			) : (
				<>
					{error && (
						<div className="status mb-4" role="alert">
							{error}
						</div>
					)}
					{subs.length === 0 ? (
						<section className="card subscription-state">
							<p className="label">No subscriptions</p>
							<h2>Nothing is mirroring trades.</h2>
							<p>
								Browse published strategies and inspect each one before
								subscribing.
							</p>
							<button
								type="button"
								className="btn btn-primary"
								onClick={() => onNavigate("marketplace")}
							>
								Browse marketplace
							</button>
						</section>
					) : (
						<div className="subscription-list">
							{subs.map((subscription) => (
								<article
									key={subscription.sub_id}
									className="card subscription-row"
								>
									<div>
										<h2>{subscription.strategy_id}</h2>
										<dl>
											<div>
												<dt>Pool</dt>
												<dd>{shortAddr(subscription.pool_id)}</dd>
											</div>
											<div>
												<dt>Wallet</dt>
												<dd>{shortAddr(subscription.subscriber_wallet)}</dd>
											</div>
											<div>
												<dt>Created</dt>
												<dd>{fmtTime(subscription.created_at)}</dd>
											</div>
										</dl>
									</div>
									<div className="subscription-actions">
										<span
											className={`tag ${subscription.status === "running" ? "tag-positive" : "tag-muted"}`}
										>
											{subscription.status}
										</span>
										{subscription.status === "running" &&
											confirming !== subscription.strategy_id && (
												<button
													type="button"
													className="btn btn-outline btn-sm"
													onClick={() =>
														setConfirming(subscription.strategy_id)
													}
												>
													Unsubscribe
												</button>
											)}
										{confirming === subscription.strategy_id && (
											<div className="subscription-confirm" role="alert">
												<p>
													This stops future charges. Remaining wallet funds do
													not move.
												</p>
												<div>
													<button
														type="button"
														className="btn btn-outline btn-sm"
														onClick={() => setConfirming(null)}
													>
														Cancel
													</button>
													<button
														type="button"
														className="btn btn-outline-danger btn-sm"
														disabled={
															unsubscribing === subscription.strategy_id
														}
														onClick={() =>
															handleUnsubscribe(subscription.strategy_id)
														}
													>
														{unsubscribing === subscription.strategy_id
															? "Stopping…"
															: "Confirm"}
													</button>
												</div>
											</div>
										)}
									</div>
								</article>
							))}
						</div>
					)}
				</>
			)}
		</div>
	);
}
