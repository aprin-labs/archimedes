import { useRef } from "react";
import { metrics, percent, type Study } from "./data.ts";

export default function RiskMap({
	items,
	selected,
	onSelect,
}: {
	items: Study[];
	selected: string;
	onSelect: (id: string) => void;
}) {
	const points = useRef<(HTMLButtonElement | null)[]>([]);
	const measured = items.map((item) => ({ ...item, ...metrics(item.series) }));
	const maxRisk =
		Math.ceil(
			(Math.max(...measured.map((item) => item.volatility)) * 100) / 5,
		) *
			5 +
		5;
	const minReturn = Math.min(
		0,
		Math.floor((Math.min(...measured.map((item) => item.cagr)) * 100) / 5) * 5,
	);
	const maxReturn =
		Math.ceil((Math.max(...measured.map((item) => item.cagr)) * 100) / 5) * 5 +
		5;
	const position = (item: ReturnType<typeof metrics>) => ({
		left: 10 + ((item.volatility * 100) / maxRisk) * 78,
		bottom: 12 + ((item.cagr * 100 - minReturn) / (maxReturn - minReturn)) * 73,
	});
	const current = measured.find((item) => item.id === selected) || measured[0];
	const guide = position(current);
	return (
		<figure
			className="risk-map"
			aria-label={`Simulated annualized return versus volatility for ${items.length} research strategies`}
		>
			<div className="map-axis-title vertical">Annualized return</div>
			<div className="map-field">
				<div
					className="coordinate-guide"
					aria-hidden="true"
					style={{
						left: "10%",
						width: `${guide.left - 10}%`,
						bottom: `${guide.bottom}%`,
					}}
				/>
				<div
					className="coordinate-guide vertical-guide"
					aria-hidden="true"
					style={{
						left: `${guide.left}%`,
						bottom: "12%",
						height: `${guide.bottom - 12}%`,
					}}
				/>
				{[0, 1, 2, 3].map((i) => (
					<div
						key={i}
						className="map-gridline"
						style={{ bottom: `${12 + (i / 3) * 73}%` }}
					>
						<span className="mono">
							{Math.round(minReturn + ((maxReturn - minReturn) * i) / 3)}%
						</span>
					</div>
				))}
				{measured.map((item, i) => {
					const pos = position(item);
					return (
						<button
							key={item.id}
							ref={(element) => {
								points.current[i] = element;
							}}
							tabIndex={selected === item.id ? 0 : -1}
							onKeyDown={(event) => {
								if (
									![
										"ArrowRight",
										"ArrowDown",
										"ArrowLeft",
										"ArrowUp",
										"Home",
										"End",
									].includes(event.key)
								)
									return;
								event.preventDefault();
								const next =
									event.key === "Home"
										? 0
										: event.key === "End"
											? items.length - 1
											: (i +
													(["ArrowRight", "ArrowDown"].includes(event.key)
														? 1
														: -1) +
													items.length) %
												items.length;
								onSelect(items[next].id);
								points.current[next]?.focus();
							}}
							className={`map-point point-${i + 1} ${selected === item.id ? "selected" : ""}`}
							style={{ left: `${pos.left}%`, bottom: `${pos.bottom}%` }}
							aria-label={`${item.name}, annualized return ${percent(item.cagr)}, volatility ${percent(item.volatility)}`}
							aria-pressed={selected === item.id}
							onClick={() => onSelect(item.id)}
						>
							<span />
							<span className="point-name">
								{item.lens === "momentum" && item.id === "regime"
									? "Regime"
									: item.lens[0].toUpperCase() + item.lens.slice(1)}
							</span>
						</button>
					);
				})}
				<div className="map-x-ticks mono">
					<span>0%</span>
					<span>{maxRisk / 2}%</span>
					<span>{maxRisk}%</span>
				</div>
			</div>
			<figcaption>
				Annualized volatility <span aria-hidden="true">→</span>
				<span className="map-instruction">Arrow keys to inspect</span>
			</figcaption>
		</figure>
	);
}
