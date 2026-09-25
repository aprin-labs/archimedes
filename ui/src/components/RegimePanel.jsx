import { apiGet } from "../api";
import { useState, useEffect, useCallback, useRef } from "react";
import { regimeMeta, regimeLabel, REGIME_ORDER } from "../regime";

function fmtPct(v) {
	return v != null ? `${(v * 100).toFixed(1)}%` : "—";
}

function fmt(v, d = 1) {
	return v != null ? v.toFixed(d) : "—";
}

// Presentation pulled from the shared regime map (src/regime.js) so the
// labels, colors, and definitions stay consistent across every surface.
function regimeBg(regime) {
	return regimeMeta(regime).bg;
}

function regimeBorder(regime) {
	return regimeMeta(regime).border;
}

function MiniBar({ value, max, color }) {
	const pct = Math.max(0, Math.min((value / (max || 1)) * 100, 100));
	return (
		<div
			style={{
				background: "var(--glass-hover)",
				borderRadius: 3,
				height: 6,
				overflow: "hidden",
				flex: 1,
			}}
		>
			<div
				style={{
					width: `${pct.toFixed(1)}%`,
					height: "100%",
					background: color || "var(--accent)",
				}}
			/>
		</div>
	);
}

// Compact pill — page-header context, not a full panel. Used on /portfolio
// where the user cares about their funds; the full educational view lives
// on /learnings.
function CompactRegimePill({ regime }) {
	const r = regime.regime;
	const meta = regimeMeta(r);
	const rColor = meta.color;
	const rLabel = meta.label;
	const conf = regime.confidence ?? 0;
	return (
		<span
			title={`${meta.label} — ${meta.definition} ${meta.exposure}.`}
			style={{
				display: "inline-flex",
				alignItems: "center",
				gap: 8,
				padding: "4px 10px",
				borderRadius: 999,
				background: regimeBg(r),
				border: `1px solid ${regimeBorder(r)}`,
				fontSize: "0.78rem",
				cursor: "help",
			}}
		>
			<span
				style={{
					width: 8,
					height: 8,
					borderRadius: "50%",
					background: rColor,
					display: "inline-block",
				}}
			/>
			<span
				style={{
					fontWeight: 700,
					textTransform: "uppercase",
					letterSpacing: "0.04em",
					color: rColor,
				}}
			>
				{rLabel}
			</span>
			<span className="caption" style={{ color: "var(--text-3)" }}>
				{fmtPct(conf)} conf
			</span>
		</span>
	);
}

function TransitionRow({ from, to, prob }) {
	return (
		<div
			style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}
		>
			<span
				className="caption"
				style={{ width: 100, color: "var(--text-3)", fontSize: "0.75rem" }}
			>
				{from === to ? `Stay ${regimeLabel(from)}` : `→ ${regimeLabel(to)}`}
			</span>
			<MiniBar
				value={prob}
				max={1}
				color={from === to ? "var(--accent)" : "var(--glass-border)"}
			/>
			<span
				className="mono"
				style={{ fontSize: "0.75rem", width: 36, textAlign: "right" }}
			>
				{fmtPct(prob)}
			</span>
		</div>
	);
}

const FETCH_TIMEOUT_MS = 10_000;

export default function RegimePanel({
	regime: regimeProp = null,
	compact = false,
}) {
	const [fetchedRegime, setFetchedRegime] = useState(null);
	const [loading, setLoading] = useState(regimeProp == null);
	const [failed, setFailed] = useState(false);
	// Tracks the most recently *started* request so a slow, superseded call
	// (e.g. an in-flight auto-fetch that loses a race with a manual Retry
	// click) can't overwrite state after a newer one has already resolved.
	const requestIdRef = useRef(0);
	// The pending watchdog timer, so a superseding fetch or an unmount can
	// clear it — otherwise a superseded request's timer stays pending for up
	// to FETCH_TIMEOUT_MS doing nothing (review).
	const timeoutRef = useRef(null);

	const fetchRegime = useCallback(() => {
		const requestId = ++requestIdRef.current;
		setLoading(true);
		setFailed(false);
		if (timeoutRef.current) clearTimeout(timeoutRef.current);
		const timeout = setTimeout(() => {
			if (requestIdRef.current !== requestId) return;
			setLoading(false);
			setFailed(true);
			console.error("Regime fetch timed out after", FETCH_TIMEOUT_MS, "ms");
		}, FETCH_TIMEOUT_MS);
		timeoutRef.current = timeout;
		apiGet("/api/regime/current")
			.then((data) => {
				clearTimeout(timeout);
				if (requestIdRef.current === requestId) {
					setFetchedRegime(data);
					setFailed(false);
				}
			})
			.catch((err) => {
				clearTimeout(timeout);
				if (requestIdRef.current === requestId) setFailed(true);
				console.error("Regime fetch failed:", err);
			})
			.finally(() => {
				if (requestIdRef.current === requestId) setLoading(false);
			});
		return () => {
			if (requestIdRef.current === requestId) requestIdRef.current += 1;
		};
	}, []);

	useEffect(() => {
		if (regimeProp != null) return;
		return fetchRegime();
	}, [regimeProp, fetchRegime]);

	// Unmount: invalidate ANY in-flight request — including one started by the
	// manual Retry onClick, which has no effect-cleanup path — and kill the
	// pending watchdog so neither can setState after unmount (review).
	useEffect(
		() => () => {
			requestIdRef.current += 1;
			if (timeoutRef.current) clearTimeout(timeoutRef.current);
		},
		[],
	);

	const regime = regimeProp ?? fetchedRegime;

	if (loading) {
		if (compact)
			return (
				<span className="caption" style={{ color: "var(--text-4)" }}>
					Loading regime…
				</span>
			);
		return (
			<div className="card-flat" style={{ padding: 20, marginBottom: 24 }}>
				<div className="caption">Loading regime data…</div>
			</div>
		);
	}

	if (!regime || regime.regime === "unknown") {
		if (compact) {
			return (
				<button
					type="button"
					className="caption regime-retry"
					onClick={fetchRegime}
					title="Retry regime data. Agent may not be running or Redis may be unavailable."
				>
					Regime unavailable{failed ? " - Retry" : ""}
				</button>
			);
		}
		return (
			<div className="card-flat" style={{ padding: 20, marginBottom: 24 }}>
				<div className="label mb-2">Current Market Regime</div>
				<div className="caption" style={{ color: "var(--text-4)" }}>
					Regime data unavailable — agent not running or Redis not connected.{" "}
					<button
						onClick={fetchRegime}
						style={{
							color: "var(--accent)",
							background: "none",
							border: "none",
							cursor: "pointer",
							textDecoration: "underline",
							padding: 0,
							fontSize: "inherit",
						}}
					>
						Retry
					</button>
				</div>
			</div>
		);
	}

	if (compact) {
		return <CompactRegimePill regime={regime} />;
	}

	const r = regime.regime;
	const meta = regimeMeta(r);
	const rColor = meta.color;
	const rLabel = meta.label;
	const confidencePct = regime.confidence ?? 0;
	const signals = regime.signals || {};
	const transitions = regime.transition_probabilities;
	const currentTransitions = transitions?.[r];
	const recommended = regime.recommended_strategies || [];
	const recommendedTitles = regime.recommended_strategy_titles || [];

	// VIX honesty (red-team 2026-05-24 H2): the agent's VIX feed reports null
	// when no data is available. VIX is never literally 0 — it's a price-of-
	// insurance index that floors around 10. So treat 0 (and NaN) as "no data"
	// and refuse to render a misleading row/bar.
	const isUsable = (v) => v != null && Number.isFinite(v);
	const vixUsable = isUsable(signals.vix_level) && signals.vix_level !== 0;
	const vixRocUsable = isUsable(signals.vix_rate_of_change);
	const compositeUsable = isUsable(signals.composite_score);
	const anySignalUsable = vixUsable || vixRocUsable || compositeUsable;

	// VIX score: rough scale — VIX 10=calm, VIX 40=crisis. Only meaningful
	// when vix_level itself is usable.
	const vixScore = vixUsable
		? (signals.vix_score ?? Math.min((signals.vix_level - 10) / 30, 1))
		: 0;
	const vixBarColor =
		vixScore > 0.7
			? "var(--negative)"
			: vixScore > 0.4
				? "var(--warning)"
				: "var(--positive)";

	return (
		<div
			className="card-flat"
			style={{
				padding: 20,
				marginBottom: 24,
				background: regimeBg(r),
				border: `1px solid ${regimeBorder(r)}`,
				borderRadius: 10,
			}}
		>
			<div className="label mb-3">Current Market Regime</div>

			{/* Regime header */}
			<div
				style={{
					display: "flex",
					alignItems: "center",
					gap: 12,
					marginBottom: 14,
					flexWrap: "wrap",
				}}
			>
				<span
					style={{
						fontWeight: 800,
						fontSize: "1.4rem",
						color: rColor,
						textTransform: "uppercase",
						letterSpacing: "0.04em",
					}}
				>
					{rLabel}
				</span>
				<span className="caption">confidence {fmtPct(confidencePct)}</span>
				{regime.regime_changed && (
					<span
						style={{
							fontSize: "0.72rem",
							fontWeight: 600,
							padding: "2px 7px",
							borderRadius: 4,
							background: "var(--warning-bg)",
							color: "var(--warning)",
							border: "1px solid var(--warning-bd)",
						}}
					>
						REGIME CHANGE
					</span>
				)}
			</div>

			{/* Plain-language meaning of the current regime */}
			<p
				className="body"
				style={{
					margin: "0 0 14px",
					color: "var(--text-3)",
					fontSize: "0.86rem",
					lineHeight: 1.5,
				}}
			>
				{meta.definition}{" "}
				<span style={{ color: "var(--text-4)" }}>{meta.exposure}.</span>
			</p>

			{/* Confidence bar */}
			<div style={{ marginBottom: 18 }}>
				<div
					style={{
						background: "var(--glass-hover)",
						borderRadius: 4,
						height: 8,
						overflow: "hidden",
					}}
				>
					<div
						style={{
							width: `${(confidencePct * 100).toFixed(1)}%`,
							height: "100%",
							background: rColor,
							transition: "width 0.4s ease",
						}}
					/>
				</div>
			</div>

			<div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
				{/* Signal breakdown */}
				<div>
					<div className="label mb-3" style={{ fontSize: "0.72rem" }}>
						Signal Breakdown
					</div>

					{vixUsable && (
						<div style={{ marginBottom: 10 }}>
							<div
								style={{
									display: "flex",
									justifyContent: "space-between",
									marginBottom: 3,
								}}
							>
								<span className="caption">VIX Level</span>
								<span className="mono" style={{ fontSize: "0.8rem" }}>
									{fmt(signals.vix_level, 1)}
								</span>
							</div>
							<MiniBar value={vixScore} max={1} color={vixBarColor} />
						</div>
					)}

					{vixRocUsable && (
						<div style={{ marginBottom: 10 }}>
							<div
								style={{
									display: "flex",
									justifyContent: "space-between",
									marginBottom: 3,
								}}
							>
								<span className="caption">VIX Momentum</span>
								<span
									className="mono"
									style={{
										fontSize: "0.8rem",
										color:
											signals.vix_rate_of_change > 0
												? "var(--negative)"
												: "var(--positive)",
									}}
								>
									{signals.vix_rate_of_change > 0 ? "+" : ""}
									{fmtPct(signals.vix_rate_of_change)}
								</span>
							</div>
							<MiniBar
								value={Math.abs(signals.vix_rate_of_change)}
								max={0.5}
								color={
									signals.vix_rate_of_change > 0
										? "var(--negative)"
										: "var(--positive)"
								}
							/>
						</div>
					)}

					{compositeUsable && (
						<div style={{ marginBottom: 10 }}>
							<div
								style={{
									display: "flex",
									justifyContent: "space-between",
									marginBottom: 3,
								}}
							>
								<span className="caption">Composite Score</span>
								<span className="mono" style={{ fontSize: "0.8rem" }}>
									{fmt(signals.composite_score, 2)}
								</span>
							</div>
							<MiniBar value={signals.composite_score} max={1} color={rColor} />
						</div>
					)}

					{!anySignalUsable && (
						<div
							className="caption"
							style={{
								color: "var(--text-4)",
								marginBottom: 10,
								fontStyle: "italic",
							}}
						>
							Signal unavailable — agent feed not connected.
						</div>
					)}

					<div className="flex gap-2.5 flex-wrap mt-2.5">
						<div className="flex items-center gap-1">
							<span
								className={`w-3 h-3 flex-shrink-0 ${signals.sp500_above_ma200 ? "i-lucide-check text-[var(--positive)]" : "i-lucide-x text-[var(--negative)]"}`}
							/>
							<span className="caption text-[0.72rem]">above MA200</span>
						</div>
						<div className="flex items-center gap-1">
							<span
								className={`w-3 h-3 flex-shrink-0 ${signals.sp500_above_ma50 ? "i-lucide-check text-[var(--positive)]" : "i-lucide-x text-[var(--negative)]"}`}
							/>
							<span className="caption text-[0.72rem]">above MA50</span>
						</div>
					</div>
				</div>

				{/* Transition probabilities */}
				<div>
					<div
						className="label mb-3"
						style={{
							fontSize: "0.72rem",
							display: "flex",
							alignItems: "center",
							gap: 8,
						}}
					>
						Regime Persistence
						{regime.transitions_source === "default_prior" && (
							<span
								className="caption"
								style={{
									fontSize: "0.6rem",
									padding: "1px 6px",
									borderRadius: 4,
									background: "var(--glass-hover)",
									color: "var(--text-4)",
								}}
							>
								prior
							</span>
						)}
					</div>
					{currentTransitions ? (
						Object.entries(currentTransitions)
							.sort(([, a], [, b]) => b - a)
							.map(([to, prob]) => (
								<TransitionRow key={to} from={r} to={to} prob={prob} />
							))
					) : (
						<div className="caption" style={{ color: "var(--text-4)" }}>
							No transition data
						</div>
					)}
				</div>
			</div>

			{/* Recommended strategies — show paper titles, never raw hashes
          (red-team 2026-05-24 H3). Fall back to an 8-char id prefix only if
          the backend didn't return a title for that index. */}
			{recommended.length > 0 && (
				<div
					style={{
						marginTop: 16,
						paddingTop: 14,
						borderTop: "1px solid var(--glass-hover)",
					}}
				>
					<span
						className="caption"
						style={{ color: "var(--text-3)", marginRight: 10 }}
					>
						Best strategies for this regime:
					</span>
					{recommended.map((id, i) => {
						const title = recommendedTitles[i];
						const label = title
							? title
									.replace(/_/g, " ")
									.replace(/\b\w/g, (c) => c.toUpperCase())
							: id.slice(0, 8);
						return (
							<span
								key={id}
								title={title || id}
								style={{
									display: "inline-block",
									marginRight: 6,
									padding: "2px 9px",
									borderRadius: 4,
									fontSize: "0.75rem",
									fontWeight: 600,
									background: "var(--glass-hover)",
									color: "var(--text-2)",
									border: "1px solid var(--glass-border)",
								}}
							>
								{label}
							</span>
						);
					})}
				</div>
			)}

			{/* Findable definitions — every regime explained inline, so the labels
          above are never a black box. The current regime is highlighted. */}
			<details
				style={{
					marginTop: 16,
					paddingTop: 14,
					borderTop: "1px solid var(--glass-hover)",
				}}
			>
				<summary
					className="caption"
					style={{ cursor: "pointer", color: "var(--text-3)" }}
				>
					What do these regimes mean?
				</summary>
				<div style={{ marginTop: 10, display: "grid", gap: 10 }}>
					{REGIME_ORDER.map((key) => {
						const m = regimeMeta(key);
						const active = key === r;
						return (
							<div
								key={key}
								style={{
									padding: "8px 10px",
									borderRadius: 6,
									background: active ? m.bg : "var(--glass)",
									border: `1px solid ${active ? m.border : "var(--glass-border)"}`,
								}}
							>
								<div
									style={{
										display: "flex",
										alignItems: "center",
										gap: 8,
										marginBottom: 3,
									}}
								>
									<span
										style={{
											width: 8,
											height: 8,
											borderRadius: "50%",
											background: m.color,
											display: "inline-block",
										}}
									/>
									<span
										style={{
											fontWeight: 700,
											fontSize: "0.84rem",
											color: m.color,
										}}
									>
										{m.label}
									</span>
									{active && (
										<span
											className="caption"
											style={{ color: "var(--text-4)" }}
										>
											· current
										</span>
									)}
								</div>
								<div
									className="caption"
									style={{ color: "var(--text-3)", lineHeight: 1.45 }}
								>
									{m.definition}{" "}
									<span style={{ color: "var(--text-4)" }}>{m.exposure}.</span>
								</div>
							</div>
						);
					})}
				</div>
			</details>
		</div>
	);
}
