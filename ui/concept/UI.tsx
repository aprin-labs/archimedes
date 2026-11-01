import { useRef, type ReactNode } from "react";
import { metrics, percent, type SeriesPoint } from "./data.ts";
import { Button } from "./components/ui/button";
import {
	Dialog,
	DialogClose,
	DialogContent,
	DialogDescription,
	DialogTitle,
} from "./components/ui/dialog";

export function Icon({
	name,
	className = "",
}: {
	name: string;
	className?: string;
}) {
	return (
		<svg className={`icon ${className}`} aria-hidden="true">
			<use href={`/icons.svg#${name}`} />
		</svg>
	);
}
export function Brand() {
	return (
		<a className="brand" href="#/" aria-label="Archimedes home">
			<span className="brand-full" aria-hidden="true" />
			<span className="brand-symbol" aria-hidden="true" />
		</a>
	);
}
export function DemoFooter() {
	return (
		<footer className="demo-footer">
			<span>
				<Icon name="flask-conical" /> Demonstration environment
			</span>
			<p>
				Fictional data and research notes. Simulated actions. No live backtests
				or trades. Research data resets on reload.
			</p>
		</footer>
	);
}
export function MetricStrip({
	series,
	baseline,
}: {
	series: SeriesPoint[];
	baseline?: SeriesPoint[];
}) {
	const result = metrics(series);
	const recorded = baseline ? metrics(baseline) : null;
	return (
		<dl className="metric-strip">
			{(
				[
					["Annualized return", "cagr"],
					["Sharpe ratio", "sharpe"],
					["Maximum drawdown", "drawdown"],
					["Annualized volatility", "volatility"],
				] as const
			).map(([label, key]) => {
				const delta = Number(
					(recorded
						? (result[key] - recorded[key]) * (key === "sharpe" ? 1 : 100)
						: 0
					).toFixed(2),
				);
				return (
					<div key={key}>
						<dt>{label}</dt>
						<dd>
							{key === "sharpe" ? result[key].toFixed(2) : percent(result[key])}
						</dd>
						{recorded && (
							<dd className="metric-delta">
								{delta > 0 ? "+" : ""}
								{delta.toFixed(2)} {key === "sharpe" ? "ratio" : "pp"}
								<span className="sr-only">
									{" "}
									versus recorded assumptions, same window
								</span>
							</dd>
						)}
					</div>
				);
			})}
		</dl>
	);
}
export function Modal({
	title,
	onClose,
	children,
}: {
	title: string;
	onClose: () => void;
	children: ReactNode;
}) {
	const trigger = useRef<HTMLElement | null>(null);
	return (
		<Dialog
			open
			onOpenChange={(open) => {
				if (!open) onClose();
			}}
		>
			<DialogContent
				onOpenAutoFocus={() => {
					trigger.current =
						document.activeElement instanceof HTMLElement
							? document.activeElement
							: null;
				}}
				onCloseAutoFocus={(event) => {
					event.preventDefault();
					if (trigger.current?.isConnected)
						trigger.current.focus({ preventScroll: true });
				}}
			>
				<div className="dialog-header">
					<span className="mono muted">RESEARCH RECORD</span>
					<DialogClose asChild>
						<Button variant="ghost" size="icon" aria-label="Close dialog">
							<Icon name="x" />
						</Button>
					</DialogClose>
				</div>
				<DialogTitle className="font-display text-4xl tracking-tight sm:text-5xl">
					{title}
				</DialogTitle>
				<DialogDescription className="sr-only">
					Fictional demonstration research: assumptions and limitations, not
					investment evidence.
				</DialogDescription>
				{children}
			</DialogContent>
		</Dialog>
	);
}
