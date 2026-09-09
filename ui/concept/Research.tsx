import { useState, type Dispatch, type SetStateAction } from "react";
import { motion, useReducedMotion } from "motion/react";
import Chart, { Sparkline, type ChartMode } from "./Chart.tsx";
import { Button } from "./components/ui/button";
import { Input } from "./components/ui/fields";
import { Field, FieldLabel } from "./components/ui/field";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "./components/ui/select";
import { Checkbox } from "./components/ui/checkbox";
import { Table, TableColumnHeader } from "./components/kibo-ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./components/ui/tabs";
import { Disclosure } from "./components/motion-primitives/disclosure";
import {
	dateLabel,
	applyScenario,
	metrics,
	percent,
	researchNotes,
	validation,
	windowSeries,
	type Study as StudyRecord,
	type ResearchNote,
} from "./data.ts";
import { DemoFooter, Icon, MetricStrip, Modal } from "./UI.tsx";

function PeriodControl({
	years,
	onChange,
}: {
	years: number;
	onChange: (years: number) => void;
}) {
	return (
		<div
			className="period-control"
			role="group"
			aria-label="Performance period"
		>
			{[1, 3, 5].map((value) => (
				<button
					key={value}
					aria-pressed={years === value}
					onClick={() => onChange(value)}
				>
					{value}Y
				</button>
			))}
		</div>
	);
}

function ValidationGrid({ study }: { study: StudyRecord }) {
	return (
		<section className="validation-section">
			<header className="section-heading">
				<h2>What the checks tell us</h2>
				<span className="tiny muted">Illustrative research thresholds</span>
			</header>
			<div className="validation-column-labels mono">
				<span>Research check</span>
				<span>Observed</span>
				<span>Threshold</span>
				<span>Outcome</span>
			</div>
			{validation(study).map((check) => (
				<Disclosure
					className="validation-row"
					key={check.name}
					summary={
						<>
							<strong>{check.name}</strong>
							<span className="mono observed">{check.value}</span>
							<span className="threshold muted">{check.threshold}</span>
							<span
								className={`check-outcome ${check.met === null ? "unassessed" : check.met ? "met" : "attention"}`}
							>
								<Icon
									name={
										check.met === null
											? "circle-alert"
											: check.met
												? "check"
												: "circle-alert"
									}
								/>
								{check.met === null
									? "Not assessed"
									: check.met
										? "Met"
										: "Attention"}
								<Icon name="chevron-down" />
							</span>
						</>
					}
				>
					<p>{check.detail}</p>
				</Disclosure>
			))}
		</section>
	);
}

function Methodology({ study }: { study: StudyRecord }) {
	const first = study.series[0].date;
	const last = study.series[study.series.length - 1].date;
	return (
		<div className="methodology-view">
			<header className="method-intro">
				<span className="label">METHOD / REPRODUCIBILITY</span>
				<h2>
					The assumptions are
					<br />
					part of the instrument.
				</h2>
				<p>
					A result is only useful if you can describe how it was made. This is
					the complete recipe for the demonstration.
				</p>
			</header>
			<dl className="method-specification">
				<div>
					<dt>Observation window</dt>
					<dd>
						{dateLabel(first)}
						<br />
						{dateLabel(last)}
					</dd>
				</div>
				<div>
					<dt>Frequency</dt>
					<dd>
						Monthly<span>60 return observations</span>
					</dd>
				</div>
				<div>
					<dt>Risk-free assumption</dt>
					<dd>
						3.0%<span>Constant annual rate</span>
					</dd>
				</div>
				<div>
					<dt>Starting value</dt>
					<dd>
						$10,000<span>USD, rebased per window</span>
					</dd>
				</div>
			</dl>
			<div className="method-body">
				<section>
					<h3>A deliberately inspectable model</h3>
					<ol className="method-recipe">
						<li>
							<span>01</span>
							<div>
								<h4>Construct a synthetic price path</h4>
								<p>
									A seeded monthly process supplies repeatable trends, noise and
									a 2022-style correction. These are not historical market
									prices.
								</p>
							</div>
						</li>
						<li>
							<span>02</span>
							<div>
								<h4>Set the exposure assumption</h4>
								<p>
									The chosen lens sets the scenario. The mandate scales its
									monthly return volatility toward a {study.risk}% annual
									budget.
								</p>
							</div>
						</li>
						<li>
							<span>03</span>
							<div>
								<h4>Account for changing positions</h4>
								<p>
									The selected {study.cost} basis points are deducted each month
									in the synthetic scenario. This simplified cost is not an
									execution model.
								</p>
							</div>
						</li>
						<li>
							<span>04</span>
							<div>
								<h4>Keep the uncomfortable details</h4>
								<p>
									Calculate return, volatility and drawdown from the displayed
									series. Leave selection-bias corrections explicitly
									unassessed.
								</p>
							</div>
						</li>
					</ol>
				</section>
				<aside className="formula-sheet">
					<h3>Metric definitions</h3>
					<dl>
						<div>
							<dt>Annualized return</dt>
							<dd>
								<code>(ending / starting)^(12 / months) − 1</code>
							</dd>
						</div>
						<div>
							<dt>Annualized volatility</dt>
							<dd>
								<code>sd(monthly returns) × √12</code>
							</dd>
						</div>
						<div>
							<dt>Sharpe ratio</dt>
							<dd>
								<code>(mean(r) − 0.03/12) / sd(r) × √12</code>
							</dd>
						</div>
						<div>
							<dt>Maximum drawdown</dt>
							<dd>
								<code>min(value / prior peak − 1)</code>
							</dd>
						</div>
					</dl>
					<p className="tiny muted">
						Sample standard deviation. Month-end observations. No intramonth
						risk is represented.
					</p>
				</aside>
			</div>
			<ValidationGrid study={study} />
			<aside className="research-limit">
				<Icon name="circle-alert" />
				<div>
					<h3>Where the demonstration ends</h3>
					<p>
						No real data ingestion, language-model reasoning, trade execution,
						tax modeling, liquidity analysis or independent model-selection
						process occurs. The allocation is illustrative. Do not treat any
						value here as investment evidence.
					</p>
				</div>
			</aside>
		</div>
	);
}

export function Study({
	study: recorded,
	tab,
	saved,
	onSave,
	onRefine,
}: {
	study: StudyRecord;
	tab: string;
	saved: boolean;
	onSave: () => void;
	onRefine: (study: StudyRecord) => void;
}) {
	const reduce = useReducedMotion();
	const [cost, setCost] = useState(recorded.cost);
	const study =
		cost === recorded.cost
			? recorded
			: {
					...recorded,
					cost,
					series: applyScenario(recorded.rawSeries, recorded.risk, cost),
				};
	const [saveMessage, setSaveMessage] = useState("");
	const [years, setYears] = useState(5);
	const [chartMode, setChartMode] = useState<ChartMode>("growth");
	const [withBenchmark, setBenchmark] = useState(true);
	const [note, setNote] = useState<ResearchNote | null>(null);
	const [exported, setExported] = useState(false);
	const checks = validation(study);
	const exportStudy = () => {
		const blob = new Blob(
			[
				JSON.stringify(
					{
						demonstration: true,
						notice: "Fictional research. Not investment advice.",
						study,
						recordedAssumptions: { risk: recorded.risk, cost: recorded.cost },
						view: {
							years,
							chartMode,
							withBenchmark,
							series: windowSeries(study.series, years),
						},
						checks,
						notes: researchNotes,
					},
					null,
					2,
				),
			],
			{ type: "application/json" },
		);
		const url = URL.createObjectURL(blob);
		const anchor = document.createElement("a");
		anchor.href = url;
		anchor.download = `archimedes-${study.id}.json`;
		anchor.click();
		window.setTimeout(() => URL.revokeObjectURL(url), 1000);
		setExported(true);
	};
	return (
		<div className="workspace-page study-page">
			<div className="page-context">
				<a href="#/library">Research library</a>
				<Icon name="chevron-right" />
				<span>{study.category}</span>
				<span className="context-end mono">DEMONSTRATION STUDY</span>
			</div>
			<header className="study-heading">
				<div>
					<span className="study-kind">
						<Icon name="git-branch" />
						{study.category}
					</span>
					<h1>{study.name}</h1>
					<p>{study.subtitle}</p>
				</div>
				<div className="study-actions">
					<Button
						variant="outline"
						className={`button button-secondary ${saved ? "is-saved" : ""}`}
						onClick={() => {
							onSave();
							setSaveMessage(
								saved
									? "Removed from session saves."
									: "Saved for this session. Resets on reload.",
							);
						}}
						aria-pressed={saved}
					>
						<Icon name={saved ? "check" : "bookmark"} />
						{saved ? "Saved for this session" : "Save study"}
					</Button>
					<button
						className="icon-button"
						onClick={exportStudy}
						aria-label="Export research as JSON"
					>
						<Icon name="download" />
					</button>
				</div>
			</header>
			<p role="status" className="action-feedback">
				{saveMessage}
			</p>
			{exported && (
				<p role="status" className="export-message">
					Demonstration research exported as JSON with the active assumptions.
				</p>
			)}
			<section
				className="study-context"
				aria-label="Current hypothesis and assumptions"
			>
				<div>
					<span className="label muted">CURRENT HYPOTHESIS</span>
					<p>{study.idea}</p>
				</div>
				<dl>
					<div>
						<dt>Lens</dt>
						<dd>{study.lens}</dd>
					</div>
					<div>
						<dt>Risk budget</dt>
						<dd>{study.risk}% / year</dd>
					</div>
					<div>
						<dt>Monthly cost</dt>
						<dd>{study.cost} bps</dd>
					</div>
					<div>
						<dt>Model window</dt>
						<dd>2021–2025</dd>
					</div>
				</dl>
			</section>
			<Tabs
				value={tab}
				onValueChange={(value) => {
					window.location.hash = `/study/${study.id}${value === "overview" ? "" : `/${value}`}`;
				}}
			>
				<TabsList
					className="study-tabs flex w-full gap-6 rounded-none border-0 border-b p-0"
					aria-label="Study views"
				>
					{[
						["overview", "Overview"],
						["evidence", "Evidence record"],
						["methodology", "Methodology"],
					].map(([id, label]) => (
						<TabsTrigger
							key={id}
							value={id}
							className="rounded-none px-0 data-[state=active]:bg-transparent"
						>
							{label}
						</TabsTrigger>
					))}
				</TabsList>
				<TabsContent value="overview">
					<motion.div
						initial={reduce ? false : { y: 4 }}
						animate={{ y: 0 }}
						transition={{ duration: reduce ? 0 : 0.18 }}
					>
						<section className="study-opening" aria-label="Study opening">
							<div>
								<span className="label muted">THE FINDING</span>
								<h2>The trade-off, in view.</h2>
							</div>
							<div>
								<p>
									<strong>In this fictional model:</strong>{" "}
									{percent(metrics(windowSeries(study.series, years)).cagr)}{" "}
									annualized return with a{" "}
									{percent(metrics(windowSeries(study.series, years)).drawdown)}{" "}
									maximum drawdown over {years} years.
								</p>
								<p>
									<strong>Limit:</strong> Monthly synthetic observations cannot
									establish a durable signal. Selection-bias correction remains
									unassessed.
								</p>
								<p>
									<strong>Next question:</strong> Does the trade-off hold with
									lower exposure or a higher monthly cost?
								</p>
								<Button
									variant="ghost"
									className="text-link"
									onClick={() => onRefine(study)}
								>
									Refine this hypothesis <Icon name="arrow-right" />
								</Button>
								<a className="text-link" href={`#/study/${study.id}/evidence`}>
									Inspect the evidence <Icon name="arrow-up-right" />
								</a>
							</div>
						</section>
						<div className="scenario-controls">
							<Field>
								<FieldLabel htmlFor="study-cost">
									Explore monthly costs
								</FieldLabel>
								<Select
									value={String(cost)}
									onValueChange={(value) => {
										setCost(Number(value));
										setExported(false);
									}}
								>
									<SelectTrigger
										id="study-cost"
										aria-describedby="scenario-help"
									>
										<SelectValue />
									</SelectTrigger>
									<SelectContent>
										{[5, 10, 20].map((value) => (
											<SelectItem key={value} value={String(value)}>
												{value} basis points
											</SelectItem>
										))}
									</SelectContent>
								</Select>
							</Field>
							<p id="scenario-help" className="scenario-note" role="status">
								{cost === recorded.cost
									? "Recorded assumptions."
									: "Scenario preview."}{" "}
								Differences compare with recorded {recorded.cost} bps/month,
								same {years}-year window. pp = percentage points. Session saves
								bookmark the recorded study; export includes this scenario.
							</p>
						</div>
						<div className="study-overview-heading">
							<span className="label">PERFORMANCE / {years}-YEAR WINDOW</span>
							<PeriodControl years={years} onChange={setYears} />
						</div>
						<MetricStrip
							series={windowSeries(study.series, years)}
							baseline={
								cost !== recorded.cost
									? windowSeries(recorded.series, years)
									: undefined
							}
						/>
						<div className="study-workbench">
							<section className="performance-panel">
								<div className="performance-header">
									<h2>The path, not just the endpoint.</h2>
									<label className="checkbox-label">
										<Checkbox
											checked={withBenchmark}
											onCheckedChange={(value) => setBenchmark(value === true)}
										/>
										Benchmark
									</label>
								</div>
								<Tabs
									value={chartMode}
									onValueChange={(value) =>
										setChartMode(value === "drawdown" ? "drawdown" : "growth")
									}
								>
									<TabsList aria-label="Chart presentation">
										<TabsTrigger value="growth">Growth</TabsTrigger>
										<TabsTrigger value="drawdown">Drawdown</TabsTrigger>
									</TabsList>
									<TabsContent value={chartMode}>
										<Chart
											items={[study]}
											years={years}
											showBenchmark={withBenchmark}
											mode={chartMode}
										/>
									</TabsContent>
								</Tabs>
								<p className="chart-caveat">
									Synthetic monthly values. Hover or use the month slider to
									inspect. Not historical performance.
								</p>
							</section>
							<aside className="allocation-panel">
								<h2>The allocation</h2>
								<p className="small muted">Illustrative portfolio weights</p>
								<div
									className="allocation-bar"
									role="img"
									aria-label={study.allocation
										.map((asset) => `${asset.name} ${asset.weight}%`)
										.join(", ")}
								>
									{study.allocation.map((asset, i) => (
										<span
											key={asset.symbol}
											style={{
												width: `${asset.weight}%`,
												background: `var(--series-${i + 1})`,
											}}
										/>
									))}
								</div>
								<dl>
									{study.allocation.map((asset, i) => (
										<div key={asset.symbol}>
											<dt>
												<i style={{ background: `var(--series-${i + 1})` }} />
												<span>
													{asset.symbol}
													<small>{asset.name}</small>
												</span>
											</dt>
											<dd>{asset.weight}%</dd>
										</div>
									))}
								</dl>
								<details className="allocation-note">
									<summary>
										Why this mix? <Icon name="chevron-down" />
									</summary>
									<p>
										{study.description} These weights illustrate the investment
										universe; the demo does not backtest individual holdings.
									</p>
								</details>
							</aside>
						</div>
					</motion.div>
				</TabsContent>
				<TabsContent value="evidence">
					<motion.div
						className="evidence-view"
						initial={reduce ? false : { y: 4 }}
						animate={{ y: 0 }}
						transition={{ duration: reduce ? 0 : 0.18 }}
					>
						<header className="evidence-heading">
							<h2>Open the reasoning.</h2>
							<p>
								Three research notes, their assumptions, and the places they
								could fail.
							</p>
						</header>
						<div className="source-records">
							{researchNotes.map((source) => (
								<article className="source-record" key={source.id}>
									<span className="mono tiny muted">
										{source.id} / {source.kind}
									</span>
									<h3>{source.title}</h3>
									<Disclosure
										className="source-summary"
										summary={
											<>
												<span>{source.summary}</span>
												<Icon name="chevron-down" />
											</>
										}
									>
										<p>{source.body}</p>
										<Button
											variant="outline"
											size="sm"
											onClick={() => setNote(source)}
										>
											Inspect source {source.id}
											<Icon name="arrow-up-right" />
										</Button>
									</Disclosure>
									<p className="source-limitation">
										<strong>Limitation</strong> {source.limitation}
									</p>
								</article>
							))}
						</div>
						<p className="evidence-disclosure">
							<Icon name="file-text" />
							These are fictional demonstration notes, not citations to real
							publications.
						</p>
						<ValidationGrid study={study} />
						<section className="provenance-band">
							<Icon name="git-branch" />
							<div>
								<h3>A record, not a badge.</h3>
								<p>
									Your hypothesis, the model assumptions and the synthetic
									series stay together in the exported research file.
								</p>
							</div>
							<button className="text-link" onClick={exportStudy}>
								Export record <Icon name="download" />
							</button>
						</section>
					</motion.div>
				</TabsContent>
				<TabsContent value="methodology">
					<motion.div
						initial={reduce ? false : { y: 4 }}
						animate={{ y: 0 }}
						transition={{ duration: reduce ? 0 : 0.18 }}
					>
						<Methodology study={study} />
					</motion.div>
				</TabsContent>
			</Tabs>
			{note && (
				<Modal title={note.title} onClose={() => setNote(null)}>
					<p className="note-type">{note.id} / Demonstration research note</p>
					<p className="note-lede">{note.summary}</p>
					<p>{note.body}</p>
					<div className="limitation">
						<h3>Where this stops being useful</h3>
						<p>{note.limitation}</p>
					</div>
					<p className="tiny muted">
						Fictional source material. No real publication or empirical result
						is asserted.
					</p>
				</Modal>
			)}
		</div>
	);
}

type MetricKey = "cagr" | "drawdown" | "sharpe" | "volatility";
export type LibraryView = {
	query: string;
	category: string;
	savedOnly: boolean;
	sort: "recent" | "name" | MetricKey;
	direction: "asc" | "desc";
	columns: string[];
};
const metricColumns: { key: MetricKey; label: string }[] = [
	{ key: "cagr", label: "Annualized return" },
	{ key: "drawdown", label: "Maximum drawdown" },
	{ key: "sharpe", label: "Sharpe ratio" },
	{ key: "volatility", label: "Annualized volatility" },
];

export function Library({
	studies,
	saved,
	comparison,
	toggleCompare,
	view,
	setView,
}: {
	studies: StudyRecord[];
	saved: string[];
	comparison: string[];
	toggleCompare: (id: string) => void;
	view: LibraryView;
	setView: Dispatch<SetStateAction<LibraryView>>;
}) {
	const { query, category, savedOnly, sort, direction, columns } = view;
	const update = (patch: Partial<LibraryView>) =>
		setView((current) => ({ ...current, ...patch }));
	const setSort = (key: LibraryView["sort"]) =>
		update({
			sort: key,
			direction: sort === key && direction === "desc" ? "asc" : "desc",
		});
	const visibleMetrics = metricColumns.filter((column) =>
		columns.includes(column.key),
	);
	const categories = [
		"All research",
		...new Set(studies.map((study) => study.category)),
	];
	const filtered = studies
		.filter(
			(study) =>
				(category === "All research" || study.category === category) &&
				(!savedOnly || saved.includes(study.id)) &&
				[
					study.name,
					study.description,
					study.category,
					study.idea,
					study.lens,
					...study.allocation.flatMap((asset) => [asset.name, asset.symbol]),
				]
					.join(" ")
					.toLowerCase()
					.includes(query.toLowerCase().trim()),
		)
		.sort(
			(a, b) =>
				(direction === "asc" ? 1 : -1) *
				(sort === "recent"
					? 0
					: sort === "name"
						? a.name.localeCompare(b.name)
						: metrics(a.series)[sort] - metrics(b.series)[sort]),
		);
	const clear = () =>
		update({ query: "", category: "All research", savedOnly: false });
	return (
		<div className="library-page">
			<header className="library-heading">
				<div>
					<p className="eyebrow">The research library</p>
					<h1>
						Ideas worth
						<br />
						looking into.
					</h1>
					<p>
						Different methods. Shared standards. Compare what each strategy asks
						you to believe.
					</p>
				</div>
				<div className="library-introduction">
					<span className="library-count">
						{String(studies.length).padStart(2, "0")}
					</span>
					<span>
						demonstration studies
						<br />
						open for inspection
					</span>
					<a className="text-link" href="#/new">
						Bring your own question <Icon name="arrow-up-right" />
					</a>
				</div>
			</header>
			<section className="library-controls" aria-label="Filter research">
				<div className="library-search">
					<Icon name="search" />
					<label className="sr-only" htmlFor="research-search">
						Search research
					</label>
					<Input
						className="border-0 bg-transparent pl-0 shadow-none"
						id="research-search"
						type="search"
						value={query}
						onChange={(event) => update({ query: event.target.value })}
						placeholder="Search ideas, methods, or assets"
					/>
				</div>
				<label className="checkbox-label saved-filter">
					<Checkbox
						checked={savedOnly}
						onCheckedChange={(value) => update({ savedOnly: value === true })}
					/>
					Saved studies
				</label>
				<label className="sort-control">
					<span className="sr-only">Sort research</span>
					<select
						value={`${sort}:${direction}`}
						onChange={(event) => {
							const [key, order] = event.target.value.split(":");
							update({
								sort: key as LibraryView["sort"],
								direction: order as LibraryView["direction"],
							});
						}}
					>
						<option value="recent:desc">Recently added</option>
						{[
							{ key: "name", label: "Research name" },
							...metricColumns,
						].flatMap((column) => [
							<option key={`${column.key}:desc`} value={`${column.key}:desc`}>
								{column.label} ↓
							</option>,
							<option key={`${column.key}:asc`} value={`${column.key}:asc`}>
								{column.label} ↑
							</option>,
						])}
					</select>
				</label>
				<div
					className="research-filters"
					role="group"
					aria-label="Research categories"
				>
					{categories.map((value) => (
						<Button
							variant="ghost"
							size="sm"
							key={value}
							aria-pressed={category === value}
							onClick={() => update({ category: value })}
						>
							{value}
						</Button>
					))}
				</div>
			</section>
			<Disclosure
				className="column-picker"
				summary={
					<>
						<Icon name="sliders-horizontal" />
						<span>Columns</span>
						<Icon name="chevron-down" />
					</>
				}
			>
				<fieldset>
					<legend>Visible columns</legend>
					{[{ key: "growth", label: "Growth path" }, ...metricColumns].map(
						(column) => (
							<label className="checkbox-label" key={column.key}>
								<Checkbox
									aria-label={`${column.label} column`}
									checked={columns.includes(column.key)}
									onCheckedChange={(checked) =>
										update({
											columns: checked
												? [...columns, column.key]
												: columns.filter((key) => key !== column.key),
										})
									}
								/>
								{column.label}
							</label>
						),
					)}
				</fieldset>
			</Disclosure>
			<div className="library-results-caption">
				<span role="status">
					{filtered.length} {filtered.length === 1 ? "study" : "studies"}
				</span>
				<span>2021-2025 / all figures simulated</span>
			</div>
			<div
				className="strategy-table-wrap"
				role="region"
				aria-label="Strategy library table"
				tabIndex={0}
			>
				<Table>
					<caption className="sr-only">Demonstration strategy metrics</caption>
					<thead>
						<tr>
							<th
								scope="col"
								aria-sort={
									sort === "name"
										? direction === "asc"
											? "ascending"
											: "descending"
										: "none"
								}
							>
								<TableColumnHeader
									title="Research"
									direction={sort === "name" ? direction : undefined}
									onSort={() => setSort("name")}
								/>
							</th>
							{columns.includes("growth") && <th scope="col">Growth path</th>}
							{visibleMetrics.map((column) => (
								<th
									key={column.key}
									scope="col"
									aria-sort={
										sort === column.key
											? direction === "asc"
												? "ascending"
												: "descending"
											: "none"
									}
								>
									<TableColumnHeader
										title={column.label}
										direction={sort === column.key ? direction : undefined}
										onSort={() => setSort(column.key)}
									/>
								</th>
							))}
							<th scope="col">Compare</th>
						</tr>
					</thead>
					<tbody className="research-list">
						{filtered.map((study) => {
							const result = metrics(study.series);
							return (
								<tr
									key={study.id}
									className={`research-entry ${comparison.includes(study.id) ? "entry-selected" : ""}`}
								>
									<th scope="row" className="entry-title">
										<span className="entry-category">{study.category}</span>
										<h2>
											<a href={`#/study/${study.id}`}>
												{study.name}
												<Icon name="arrow-up-right" />
											</a>
										</h2>
										<p>{study.subtitle}</p>
										<span
											className={`entry-status ${study.status === "Caution" ? "status-caution" : ""}`}
										>
											<Icon
												name={
													study.status === "Reviewed"
														? "check"
														: study.status === "Caution"
															? "circle-alert"
															: "activity"
												}
											/>
											{study.status}
										</span>
									</th>
									{columns.includes("growth") && (
										<td className="entry-chart">
											<Sparkline series={study.series} />
											<span className="sr-only">
												Individually scaled; use comparison for a shared scale.
											</span>
										</td>
									)}
									{visibleMetrics.map((column) => (
										<td className="entry-number" key={column.key}>
											<span className="mobile-label">{column.label}</span>
											<strong data-metric={column.key}>
												{column.key === "sharpe"
													? result[column.key].toFixed(2)
													: percent(result[column.key])}
											</strong>
										</td>
									))}
									<td className="entry-compare">
										<label className="compare-checkbox">
											<Checkbox
												checked={comparison.includes(study.id)}
												disabled={
													comparison.length >= 3 &&
													!comparison.includes(study.id)
												}
												onCheckedChange={() => toggleCompare(study.id)}
												aria-label={`Compare ${study.name}`}
											/>
											<span className="mobile-label">Compare</span>
										</label>
									</td>
								</tr>
							);
						})}
					</tbody>
				</Table>
			</div>
			{!filtered.length && (
				<div className="empty-state">
					<Icon name="search" />
					<h2>No research matches those filters.</h2>
					<p>Try a different question, or return to the full collection.</p>
					<Button className="button button-primary" onClick={clear}>
						Clear filters <Icon name="rotate-ccw" />
					</Button>
				</div>
			)}
			<p className="library-note">
				Compare up to three studies. A strong historical-looking curve is not
				proof of a durable strategy.
			</p>
			{comparison.length > 0 && (
				<div className="comparison-tray">
					<span>
						<Icon name="columns-2" />
						<strong>{comparison.length} selected</strong>
						<span className="tray-hint">Choose 2 or 3 for a shared view</span>
					</span>
					<div className="tray-studies">
						{studies
							.filter((study) => comparison.includes(study.id))
							.map((study) => (
								<button
									key={study.id}
									className="selection-chip"
									aria-label={`Remove ${study.name} from comparison`}
									onClick={() => toggleCompare(study.id)}
								>
									<span>{study.name}</span>
									<Icon name="x" />
								</button>
							))}
					</div>
					<Button
						className="button button-primary"
						disabled={comparison.length < 2}
						onClick={() => {
							window.location.hash = "/compare";
						}}
					>
						Compare research <Icon name="arrow-right" />
					</Button>
				</div>
			)}
			<DemoFooter />
		</div>
	);
}

export function Comparison({ studies }: { studies: StudyRecord[] }) {
	const [years, setYears] = useState(5);
	if (studies.length < 2)
		return (
			<div className="empty-page">
				<Icon name="columns-2" />
				<h1>Comparison needs context.</h1>
				<p>
					Select two or three studies from the library to see them on the same
					scale.
				</p>
				<a href="#/library" className="button button-primary">
					Choose studies <Icon name="arrow-right" />
				</a>
			</div>
		);
	const data = studies.map((study) => ({
		...study,
		result: metrics(windowSeries(study.series, years)),
	}));
	return (
		<div className="workspace-page comparison-page">
			<div className="page-context">
				<a href="#/library">Research library</a>
				<Icon name="chevron-right" />
				<span>Comparison</span>
			</div>
			<header className="comparison-heading">
				<div>
					<h1>
						Same window.
						<br />
						Different trade-offs.
					</h1>
					<p>
						Shared starting capital. Shared dates. No hidden change of scale.
					</p>
				</div>
				<a className="text-link" href="#/library">
					Change selection <Icon name="sliders-horizontal" />
				</a>
			</header>
			<div className="comparison-chart">
				<header>
					<h2>{studies.length} ideas, in context.</h2>
					<PeriodControl years={years} onChange={setYears} />
				</header>
				<Chart items={studies} years={years} />
			</div>
			<div
				className="comparison-table-wrap"
				role="region"
				aria-label="Research comparison metrics"
				tabIndex={0}
			>
				<table className="comparison-table">
					<caption>{years}-year simulated research comparison</caption>
					<thead>
						<tr>
							<th scope="col">Research measure</th>
							{data.map((study, i) => (
								<th key={study.id} scope="col">
									<span
										className="comparison-key"
										style={{ background: `var(--series-${i + 1})` }}
									/>
									<a href={`#/study/${study.id}`}>
										{study.name}
										<Icon name="arrow-up-right" />
									</a>
								</th>
							))}
						</tr>
					</thead>
					<tbody>
						{(
							[
								["Annualized return", "cagr"],
								["Sharpe ratio", "sharpe"],
								["Maximum drawdown", "drawdown"],
								["Annualized volatility", "volatility"],
							] as const
						).map(([label, key]) => (
							<tr key={key}>
								<th scope="row">{label}</th>
								{data.map((study) => (
									<td key={study.id}>
										{key === "sharpe"
											? study.result[key].toFixed(2)
											: percent(study.result[key])}
									</td>
								))}
							</tr>
						))}
						<tr>
							<th scope="row">Research lens</th>
							{data.map((study) => (
								<td key={study.id} className="comparison-prose">
									{study.category}
								</td>
							))}
						</tr>
						<tr>
							<th scope="row">Unresolved question</th>
							{data.map((study) => (
								<td key={study.id} className="comparison-prose">
									{study.lens === "momentum"
										? "Does the trend persist after crowded positioning?"
										: study.lens === "defensive"
											? "Will bonds diversify the next equity decline?"
											: "Does the quality premium survive valuation changes?"}
								</td>
							))}
						</tr>
					</tbody>
				</table>
			</div>
			<aside className="research-limit">
				<Icon name="circle-alert" />
				<div>
					<h2>A comparison, not a recommendation.</h2>
					<p>
						These synthetic results illustrate differences in assumptions. There
						is no universal winner, and no demonstrated future return.
					</p>
				</div>
				<a href="#/new" className="text-link">
					Form your own hypothesis <Icon name="arrow-right" />
				</a>
			</aside>
		</div>
	);
}
