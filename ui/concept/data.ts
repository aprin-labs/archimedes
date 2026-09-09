// Demonstration data only. This module never imports production APIs or models.
export type LensId = "momentum" | "defensive" | "quality";
export type SeriesPoint = { date: string; value: number };
export type Lens = {
	id: LensId;
	name: string;
	short: string;
	icon: string;
	idea: string;
};
export type Mandate = {
	idea: string;
	lens: LensId;
	risk: number;
	cost: number;
	interrupt: boolean;
};
export type Study = {
	id: string;
	name: string;
	subtitle: string;
	category: string;
	lens: LensId;
	risk: number;
	cost: number;
	status: "Reviewed" | "Caution" | "Exploring";
	description: string;
	idea: string;
	series: SeriesPoint[];
	rawSeries: SeriesPoint[];
	allocation: { symbol: string; name: string; weight: number }[];
	demo: boolean;
};
export type RunAction = "tick" | "retry" | "cancel";
export type InvestigationRun = {
	stage: number;
	status: "running" | "error" | "complete" | "cancelled";
	interrupt: boolean;
	study: Study;
};
export type ResearchNote = {
	id: string;
	title: string;
	kind: string;
	summary: string;
	body: string;
	limitation: string;
};

export const lenses: Lens[] = [
	{
		id: "momentum",
		name: "Follow persistent trends",
		short: "Momentum",
		icon: "activity",
		idea: "Capture persistent trends across equities, bonds and gold. Reduce exposure when volatility rises, without trying to predict the next market turn.",
	},
	{
		id: "defensive",
		name: "Protect the downside",
		short: "Defensive",
		icon: "layers",
		idea: "Build a defensive allocation that participates in growth but prioritizes smaller drawdowns. Diversify across bonds, gold and broad equities.",
	},
	{
		id: "quality",
		name: "Find durable value",
		short: "Quality",
		icon: "search",
		idea: "Favor profitable, reasonably valued companies over speculative growth. Test whether a quality tilt survives costs and changing market conditions.",
	},
];

export function metrics(series: readonly { value: number }[]) {
	const returns = series
		.slice(1)
		.map((point, index) => point.value / series[index].value - 1);
	const n = returns.length;
	if (!n) return { total: 0, cagr: 0, volatility: 0, sharpe: 0, drawdown: 0 };
	const total = series[series.length - 1].value / series[0].value - 1;
	const mean = returns.reduce((sum, value) => sum + value, 0) / n;
	const deviation = Math.sqrt(
		returns.reduce((sum, value) => sum + (value - mean) ** 2, 0) /
			Math.max(n - 1, 1),
	);
	let peak = series[0].value;
	let drawdown = 0;
	for (const point of series) {
		peak = Math.max(peak, point.value);
		drawdown = Math.min(drawdown, point.value / peak - 1);
	}
	return {
		total,
		cagr: (1 + total) ** (12 / n) - 1,
		volatility: deviation * Math.sqrt(12),
		sharpe: deviation ? ((mean - 0.03 / 12) / deviation) * Math.sqrt(12) : 0,
		drawdown,
	};
}

function syntheticSeries(
	seed: number,
	drift: number,
	amplitude: number,
	shock: number,
): SeriesPoint[] {
	let random = seed;
	let value = 100;
	const series = [{ date: "2020-12-31", value }];
	for (let month = 1; month <= 60; month++) {
		random = (random * 1664525 + 1013904223) >>> 0;
		const noise = (random / 4294967296 - 0.5) * 2;
		const cycle = Math.sin(month * 1.7 + seed) * 0.45;
		const correction = month >= 14 && month <= 22 ? shock : 0;
		value *= 1 + drift + amplitude * (noise + cycle) - correction;
		series.push({
			date: new Date(Date.UTC(2021, month, 0)).toISOString().slice(0, 10),
			value,
		});
	}
	return series;
}

export const benchmark = {
	id: "benchmark",
	name: "Balanced benchmark",
	series: syntheticSeries(51, 0.011, 0.058, 0.032),
};

const presets: Omit<Study, "rawSeries" | "demo">[] = [
	{
		id: "momentum",
		name: "Adaptive momentum",
		subtitle: "Follow the signal. Respect the noise.",
		category: "Trend following",
		lens: "momentum",
		risk: 12,
		cost: 10,
		status: "Reviewed",
		description:
			"A cross-asset trend strategy with a volatility-aware allocation. The model steps back when markets become less predictable.",
		idea: lenses[0].idea,
		series: syntheticSeries(18, 0.014, 0.041, 0.019),
		allocation: [
			{ symbol: "SPY", name: "US equities", weight: 40 },
			{ symbol: "TLT", name: "Treasury bonds", weight: 25 },
			{ symbol: "GLD", name: "Gold", weight: 20 },
			{ symbol: "SHY", name: "Short-term bonds", weight: 15 },
		],
	},
	{
		id: "defensive",
		name: "The defensive allocation",
		subtitle: "Stay invested, with room to breathe.",
		category: "Capital preservation",
		lens: "defensive",
		risk: 8,
		cost: 10,
		status: "Reviewed",
		description:
			"A bond-led allocation designed to trade some upside for a less demanding path. Diversification is tested, not assumed.",
		idea: lenses[1].idea,
		series: syntheticSeries(27, 0.009, 0.029, 0.015),
		allocation: [
			{ symbol: "TLT", name: "Treasury bonds", weight: 45 },
			{ symbol: "SPY", name: "US equities", weight: 25 },
			{ symbol: "GLD", name: "Gold", weight: 20 },
			{ symbol: "SHY", name: "Short-term bonds", weight: 10 },
		],
	},
	{
		id: "quality",
		name: "Quality, at a fair price",
		subtitle: "A longer view of business quality.",
		category: "Factor investing",
		lens: "quality",
		risk: 16,
		cost: 10,
		status: "Caution",
		description:
			"An equity-heavy quality tilt. Strong periods coexist with a difficult drawdown, making timing assumptions important.",
		idea: lenses[2].idea,
		series: syntheticSeries(49, 0.012, 0.072, 0.037),
		allocation: [
			{ symbol: "QUAL", name: "Quality equities", weight: 50 },
			{ symbol: "SPY", name: "US equities", weight: 35 },
			{ symbol: "GLD", name: "Gold", weight: 10 },
			{ symbol: "SHY", name: "Short-term bonds", weight: 5 },
		],
	},
	{
		id: "regime",
		name: "Across market regimes",
		subtitle: "Different conditions. Different exposures.",
		category: "Multi-asset",
		lens: "momentum",
		risk: 14,
		cost: 10,
		status: "Exploring",
		description:
			"An exploratory allocation that changes exposure with the volatility regime. Regime selection still needs independent testing.",
		idea: "Adapt a diversified portfolio to changing market volatility without relying on a single fixed allocation.",
		series: syntheticSeries(71, 0.012, 0.052, 0.022),
		allocation: [
			{ symbol: "SPY", name: "US equities", weight: 35 },
			{ symbol: "TLT", name: "Treasury bonds", weight: 30 },
			{ symbol: "GLD", name: "Gold", weight: 25 },
			{ symbol: "SHY", name: "Short-term bonds", weight: 10 },
		],
	},
];

export const studies: Study[] = presets.map((study) => ({
	...study,
	rawSeries: study.series,
	series: applyScenario(study.series, study.risk, study.cost),
	demo: true,
}));

export function validateMandate(mandate: Omit<Mandate, "interrupt">) {
	if (typeof mandate.idea !== "string" || mandate.idea.trim().length < 24)
		return "Describe your investment idea in at least 24 characters.";
	if (mandate.idea.length > 800)
		return "Keep the hypothesis under 800 characters.";
	if (!lenses.some((lens) => lens.id === mandate.lens))
		return "Choose a research lens.";
	if (!Number.isFinite(mandate.risk) || mandate.risk < 8 || mandate.risk > 18)
		return "Risk budget must be between 8% and 18%.";
	if (![5, 10, 20].includes(mandate.cost))
		return "Choose a supported trading-cost assumption.";
	return "";
}

export function applyScenario(
	rawSeries: SeriesPoint[],
	risk: number,
	cost: number,
): SeriesPoint[] {
	const scale = risk / 100 / metrics(rawSeries).volatility;
	let value = 100;
	return rawSeries.map((point, index) => {
		if (index)
			value *=
				1 +
				(point.value / rawSeries[index - 1].value - 1) * scale -
				cost / 10000;
		return { ...point, value };
	});
}

export function makeStudy(
	mandate: Omit<Mandate, "interrupt">,
	id: string,
): Study {
	const error = validateMandate(mandate);
	if (error) throw new Error(error);
	const base = studies.find((study) => study.lens === mandate.lens)!;
	const lens = lenses.find((lens) => lens.id === mandate.lens)!;
	const series = applyScenario(base.rawSeries, mandate.risk, mandate.cost);
	return {
		...base,
		id,
		name: `${lens.short} research`,
		idea: mandate.idea.trim(),
		risk: mandate.risk,
		cost: mandate.cost,
		series,
		status: "Reviewed",
		demo: true,
	};
}

export function advanceRun(
	state: InvestigationRun,
	action: RunAction,
): InvestigationRun {
	if (action === "cancel" && state.status !== "complete")
		return { ...state, status: "cancelled" };
	if (action === "retry" && state.status === "error")
		return { ...state, status: "running", interrupt: false };
	if (action !== "tick" || state.status !== "running") return state;
	if (state.stage === 1 && state.interrupt)
		return { ...state, status: "error" };
	const stage = Math.min(4, state.stage + 1);
	return { ...state, stage, status: stage === 4 ? "complete" : "running" };
}

export function drawdownSeries(series: SeriesPoint[]): SeriesPoint[] {
	let peak = 0;
	return series.map((point) => {
		peak = Math.max(peak, point.value);
		return { ...point, value: peak ? point.value / peak - 1 : 0 };
	});
}

export function windowSeries(
	series: SeriesPoint[],
	years: number,
): SeriesPoint[] {
	const points = series.slice(-(years * 12 + 1));
	return points.map((point) => ({
		...point,
		value: (point.value / points[0].value) * 100,
	}));
}

export function validation(study: Study) {
	const full = metrics(study.series);
	const recent = metrics(study.series.slice(-25));
	return [
		{
			name: "Final two-year growth",
			value: percent(recent.cagr),
			threshold: "Above 0%",
			met: recent.cagr > 0,
			detail:
				"Annualized growth over the final 24 months. Exposure scaling uses the full path, so this is not an independent out-of-sample test.",
		},
		{
			name: "Maximum drawdown",
			value: percent(full.drawdown),
			threshold: "Shallower than −20%",
			met: full.drawdown > -0.2,
			detail:
				"Largest peak-to-trough decline, calculated from the same monthly points shown in the performance chart. Intramonth losses are not represented.",
		},
		{
			name: "Risk-adjusted return",
			value: full.sharpe.toFixed(3),
			threshold: "Sharpe above 0.60",
			met: full.sharpe > 0.6,
			detail:
				"Annualized monthly excess return divided by monthly return volatility. A constant 3% annual risk-free assumption is used.",
		},
		{
			name: "Selection-bias correction",
			value: "Not assessed",
			threshold: "Independent trials required",
			met: null,
			detail:
				"Deflated Sharpe and probability of backtest overfitting require a real set of tested alternatives. This demo does not calculate or claim either statistic.",
		},
	];
}

export const researchNotes: ResearchNote[] = [
	{
		id: "RN-014",
		title: "From hypothesis to a testable signal",
		kind: "Signal rationale",
		summary:
			"Every investment lens needs an explicit rule, a defined universe, and evidence that survives challenge.",
		body: "The selected lens is a research starting point. Its hypothesis and illustrative allocation are recorded with the study. This demonstration builds a seeded return path; it does not implement or empirically validate a trend, quality, or allocation signal.",
		limitation:
			"A shared macro exposure can make apparently diverse positions behave like one trade.",
	},
	{
		id: "RN-021",
		title: "Volatility scaling and left-tail risk",
		kind: "Risk assumption",
		summary:
			"Position size should be a research decision, not an accidental consequence of choosing a volatile asset.",
		body: "The concept scales synthetic monthly returns toward the selected annualized volatility budget. A live strategy would need lagged volatility estimates, exposure caps, liquidity constraints and a separate turnover model.",
		limitation:
			"A risk target is not a loss limit. Volatility can change faster than an estimate reacts.",
	},
	{
		id: "RN-032",
		title: "The cost of changing your mind",
		kind: "Implementation",
		summary:
			"More trading is not free. A plausible strategy should survive conservative assumptions about execution costs.",
		body: "The simulated mandate deducts the selected basis-point cost once per month. This is a simple scenario assumption, not a realistic brokerage or market-impact calculation.",
		limitation:
			"Taxes, financing, spreads, market impact and intramonth turnover are not modeled.",
	},
];

export function percent(value: number, digits = 1) {
	return new Intl.NumberFormat("en-US", {
		style: "percent",
		minimumFractionDigits: digits,
		maximumFractionDigits: digits,
	}).format(value);
}
export function money(value: number) {
	return new Intl.NumberFormat("en-US", {
		style: "currency",
		currency: "USD",
		maximumFractionDigits: 0,
	}).format(value);
}
export function dateLabel(date: string) {
	return new Intl.DateTimeFormat("en-US", {
		month: "short",
		year: "numeric",
		timeZone: "UTC",
	}).format(new Date(`${date}T00:00:00Z`));
}
