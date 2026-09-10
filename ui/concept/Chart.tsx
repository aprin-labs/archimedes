import { useId, useState, type PointerEvent } from "react";
import { motion, useReducedMotion } from "motion/react";
import {
	benchmark,
	dateLabel,
	drawdownSeries,
	money,
	percent,
	windowSeries,
	type SeriesPoint,
} from "./data.ts";

export type ChartMode = "growth" | "drawdown";
type ChartItem = { id: string; name: string; series: SeriesPoint[] };

function linePath(
	series: SeriesPoint[],
	min: number,
	max: number,
	height = 240,
) {
	return series
		.map(
			(point, index) =>
				`${index ? "L" : "M"}${((index / Math.max(1, series.length - 1)) * 800).toFixed(2)},${(height - ((point.value - min) / (max - min || 1)) * height).toFixed(2)}`,
		)
		.join(" ");
}

export function Sparkline({ series }: { series: SeriesPoint[] }) {
	const values = series.map((point) => point.value);
	return (
		<svg
			className="sparkline"
			viewBox="0 0 800 240"
			preserveAspectRatio="none"
			aria-hidden="true"
		>
			<path
				d={linePath(
					series,
					Math.min(...values) * 0.98,
					Math.max(...values) * 1.02,
				)}
			/>
		</svg>
	);
}

export default function Chart({
	items,
	years = 5,
	compact = false,
	showBenchmark = false,
	mode = "growth",
}: {
	items: ChartItem[];
	years?: number;
	compact?: boolean;
	showBenchmark?: boolean;
	mode?: ChartMode;
}) {
	const id = useId();
	const reduce = useReducedMotion();
	const [inspection, setInspection] = useState<number | null>(null);
	const [hover, setHover] = useState<number | null>(null);
	const plotted = [...items, ...(showBenchmark ? [benchmark] : [])].map(
		(item) => {
			const points = windowSeries(item.series, years);
			return {
				...item,
				points: mode === "drawdown" ? drawdownSeries(points) : points,
			};
		},
	);
	const values = plotted.flatMap((item) =>
		item.points.map((point) => point.value),
	);
	const min =
		mode === "drawdown"
			? Math.min(-0.05, Math.floor(Math.min(...values) * 20) / 20)
			: Math.floor(Math.min(...values) / 20) * 20;
	const max =
		mode === "drawdown"
			? 0
			: Math.ceil(Math.max(...values) / 20) * 20 +
				(Math.max(...values) % 20 === 0 ? 20 : 0);
	const count = plotted[0].points.length;
	const index = Math.min(hover ?? inspection ?? count - 1, count - 1);
	const date = plotted[0].points[index].date;
	const selected = hover !== null || inspection !== null;
	const format = (value: number) =>
		mode === "drawdown" ? percent(value) : money(value * 100);
	const ticks = Array.from(
		{ length: 5 },
		(_, i) => min + ((max - min) * i) / 4,
	);
	const inspect = (event: PointerEvent<HTMLDivElement>) => {
		const bounds = event.currentTarget.getBoundingClientRect();
		setHover(
			Math.max(
				0,
				Math.min(
					count - 1,
					Math.round(
						((event.clientX - bounds.left) / bounds.width) * (count - 1),
					),
				),
			),
		);
	};
	return (
		<figure
			className={`equity-chart ${compact ? "chart-compact" : ""}`}
			aria-labelledby={`${id}-title`}
		>
			<figcaption id={`${id}-title`} className="chart-caption">
				<span>
					{mode === "drawdown"
						? "Decline from prior peak"
						: "Growth of $10,000"}{" "}
					<span className="muted">/ {mode === "drawdown" ? "%" : "USD"}</span>
				</span>
				<span className="mono muted">{years}-year simulation</span>
			</figcaption>
			<div className="chart-frame">
				<div
					className="plot-body"
					onPointerMove={inspect}
					onPointerDown={inspect}
					onPointerLeave={() => setHover(null)}
				>
					<svg
						viewBox="0 0 800 240"
						preserveAspectRatio="none"
						role="img"
						aria-label={`Simulated ${mode === "drawdown" ? "drawdown" : "investment growth"} for ${items.map((item) => item.name).join(", ")}. ${compact ? "Open the study for month-by-month values." : "Use the month slider below for exact values."}`}
					>
						{ticks.map((tick, i) => (
							<line
								className="chart-grid"
								key={tick}
								x1="0"
								x2="800"
								y1={240 - i * 60}
								y2={240 - i * 60}
							/>
						))}
						<motion.g
							key={`${items.map((item) => item.id).join("-")}-${mode}-${years}`}
							initial={reduce ? false : { opacity: 0 }}
							animate={{ opacity: 1 }}
							transition={{ duration: reduce ? 0 : 0.3 }}
						>
							{plotted.map((item, i) => (
								<g
									key={item.id}
									style={{
										color:
											item.id === "benchmark"
												? "var(--chart-benchmark)"
												: `var(--series-${i + 1})`,
									}}
								>
									{i === 0 && (
										<path
											className="chart-area"
											d={`${linePath(item.points, min, max)} L800,${mode === "drawdown" ? 0 : 240} L0,${mode === "drawdown" ? 0 : 240} Z`}
										/>
									)}
									<path
										className={
											item.id === "benchmark"
												? "chart-line benchmark-line"
												: "chart-line"
										}
										d={linePath(item.points, min, max)}
									/>
									{selected && (
										<circle
											className="chart-inspection-dot"
											cx={(index / (count - 1)) * 800}
											cy={
												240 -
												((item.points[index].value - min) / (max - min)) * 240
											}
											r="3.5"
										/>
									)}
								</g>
							))}
						</motion.g>
						{selected && (
							<line
								className="crosshair"
								x1={(index / (count - 1)) * 800}
								x2={(index / (count - 1)) * 800}
								y1="0"
								y2="240"
							/>
						)}
					</svg>
					{selected && (
						<div
							className="chart-tooltip"
							style={{
								left: `clamp(98px, ${(index / (count - 1)) * 100}%, calc(100% - 98px))`,
							}}
						>
							<strong className="mono">{dateLabel(date)}</strong>
							{plotted.map((item) => (
								<div key={item.id}>
									<span>
										{item.id === "benchmark" ? "Benchmark" : item.name}
									</span>
									<b className="mono">{format(item.points[index].value)}</b>
								</div>
							))}
						</div>
					)}
				</div>
				<div className="y-axis" aria-hidden="true">
					{ticks.map((tick, i) => (
						<span key={tick} style={{ bottom: `${i * 25}%` }}>
							{mode === "drawdown"
								? percent(tick, 0)
								: `${(tick / 10).toFixed(tick % 10 ? 1 : 0)}k`}
						</span>
					))}
				</div>
			</div>
			<div className="x-axis mono" aria-hidden="true">
				{[
					0,
					Math.round((count - 1) / 3),
					Math.round(((count - 1) * 2) / 3),
					count - 1,
				].map((i) => (
					<span key={i}>{dateLabel(plotted[0].points[i].date)}</span>
				))}
			</div>
			{!compact && (
				<div className="chart-inspection">
					<label htmlFor={`${id}-month`}>
						Inspect month <span className="mono">{dateLabel(date)}</span>
					</label>
					<input
						id={`${id}-month`}
						type="range"
						min="0"
						max={count - 1}
						value={index}
						onChange={(event) => {
							setHover(null);
							setInspection(Number(event.target.value));
						}}
						onFocus={() => {
							setHover(null);
							setInspection(index);
						}}
						aria-valuetext={`${dateLabel(date)}; ${plotted.map((item) => `${item.name}, ${format(item.points[index].value)}`).join("; ")}`}
					/>
					<output className="mono" htmlFor={`${id}-month`}>
						{format(plotted[0].points[index].value)}
					</output>
				</div>
			)}
			<div className="chart-legend">
				{plotted.map((item, i) => (
					<span key={item.id}>
						<i
							className={
								item.id === "benchmark" ? "legend-line dashed" : "legend-line"
							}
							style={{
								color:
									item.id === "benchmark"
										? "var(--chart-benchmark)"
										: `var(--series-${i + 1})`,
							}}
						/>
						{item.name}
					</span>
				))}
			</div>
		</figure>
	);
}
