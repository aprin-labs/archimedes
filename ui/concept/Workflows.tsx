import {
	useState,
	type Dispatch,
	type SetStateAction,
	type SubmitEvent,
} from "react";
import { motion, useReducedMotion } from "motion/react";
import { Button } from "./components/ui/button";
import { Input, Textarea } from "./components/ui/fields";
import {
	Field,
	FieldDescription,
	FieldError,
	FieldLabel,
} from "./components/ui/field";
import {
	Select,
	SelectContent,
	SelectItem,
	SelectTrigger,
	SelectValue,
} from "./components/ui/select";
import { Badge } from "./components/ui/badge";
import { Choicebox, ChoiceboxItem } from "./components/kibo-ui/choicebox";
import { Slider } from "./components/ui/slider";
import {
	lenses,
	metrics,
	percent,
	researchNotes,
	studies,
	validateMandate,
	type Mandate,
	type Lens,
	type InvestigationRun,
	type ResearchNote,
	type RunAction,
} from "./data.ts";
import { Icon, Modal } from "./UI.tsx";
import { Sparkline } from "./Chart.tsx";

export function Composer({
	mandate,
	setMandate,
	onStart,
}: {
	mandate: Mandate;
	setMandate: Dispatch<SetStateAction<Mandate>>;
	onStart: () => void;
}) {
	const [submitted, setSubmitted] = useState(false);
	const error = submitted ? validateMandate(mandate) : "";
	const invalidHypothesis =
		mandate.idea.trim().length < 24 || mandate.idea.length > 800;
	const invalidRisk =
		!Number.isFinite(mandate.risk) || mandate.risk < 8 || mandate.risk > 18;
	const hypothesisError = invalidHypothesis ? error : "";
	const riskError = !invalidHypothesis && invalidRisk ? error : "";
	const selectedPreset = lenses.find(
		(lens) =>
			mandate.idea === lens.idea &&
			mandate.lens === lens.id &&
			mandate.risk === studies.find((study) => study.lens === lens.id)?.risk,
	);
	const update = (patch: Partial<Mandate>) =>
		setMandate((current) => ({ ...current, ...patch }));
	const example = (lens: Lens) => {
		update({
			idea: lens.idea,
			lens: lens.id,
			risk: studies.find((study) => study.lens === lens.id)!.risk,
		});
		setSubmitted(false);
	};
	const submit = (event: SubmitEvent<HTMLFormElement>) => {
		event.preventDefault();
		const message = validateMandate(mandate);
		setSubmitted(true);
		if (!message) onStart();
		else
			document
				.getElementById(
					invalidHypothesis
						? "hypothesis"
						: invalidRisk
							? "risk-input"
							: "investment-lens",
				)
				?.focus();
	};
	return (
		<div className="workspace-page composer-page">
			<div className="page-context">
				<a href="#/library">Research desk</a>
				<Icon name="chevron-right" />
				<span>New investigation</span>
			</div>
			<header className="page-heading">
				<h1>
					What do you want
					<br />
					to find out?
				</h1>
				<p>
					Start with an idea, not a ticker. Set the boundaries. Let the research
					challenge the rest.
				</p>
			</header>
			<form className="composer-grid" onSubmit={submit} noValidate>
				<section className="hypothesis-sheet">
					<Field>
						<div className="sheet-heading">
							<FieldLabel htmlFor="hypothesis">
								Your investment hypothesis
							</FieldLabel>
							<span className="mono muted">{mandate.idea.length}/800</span>
						</div>
						<Textarea
							className="hypothesis-input bg-secondary text-xl leading-relaxed"
							id="hypothesis"
							maxLength={800}
							value={mandate.idea}
							onChange={(event) => update({ idea: event.target.value })}
							placeholder="I think persistent trends across stocks, bonds and gold could outperform a fixed allocation, with less downside…"
							aria-describedby={
								hypothesisError
									? "hypothesis-error hypothesis-help"
									: "hypothesis-help"
							}
							aria-invalid={!!hypothesisError}
						/>
						<div id="hypothesis-help" className="composer-hint">
							<Icon name="file-text" />
							<span>
								A useful hypothesis names a pattern, an investment universe, and
								a trade-off.
							</span>
						</div>
						{hypothesisError && (
							<FieldError id="hypothesis-error" className="form-error">
								<Icon name="circle-alert" />
								{hypothesisError}
							</FieldError>
						)}
					</Field>
					<div className="example-area">
						<span className="label">Or start with a question</span>
						<Choicebox
							aria-label="Research presets"
							value={selectedPreset?.id ?? ""}
							onValueChange={(value) => {
								const lens = lenses.find((item) => item.id === value);
								if (lens) example(lens);
							}}
						>
							{lenses.map((lens) => (
								<ChoiceboxItem
									key={lens.id}
									value={lens.id}
									title={lens.name}
									description={lens.idea}
								/>
							))}
						</Choicebox>
						<FieldDescription>
							Choosing a preset replaces the draft and sets its lens and risk
							budget. Your cost assumption stays unchanged.
						</FieldDescription>
					</div>
				</section>
				<aside className="mandate-panel">
					<header>
						<Icon name="sliders-horizontal" />
						<h2>Research mandate</h2>
					</header>
					<p className="muted small">The assumptions are part of the result.</p>
					<Field className="mandate-field">
						<FieldLabel htmlFor="investment-lens">Investment lens</FieldLabel>
						<Select
							value={mandate.lens}
							onValueChange={(value) => {
								const lens = lenses.find((item) => item.id === value);
								if (lens) update({ lens: lens.id });
							}}
						>
							<SelectTrigger id="investment-lens" aria-describedby="lens-help">
								<SelectValue />
							</SelectTrigger>
							<SelectContent>
								{lenses.map((lens) => (
									<SelectItem key={lens.id} value={lens.id}>
										{lens.short}
									</SelectItem>
								))}
							</SelectContent>
						</Select>
						<FieldDescription id="lens-help">
							Changes the model lens, not your written hypothesis.
						</FieldDescription>
					</Field>
					<Field className="mandate-field">
						<FieldLabel htmlFor="risk-input">
							Annualized risk budget (%)
						</FieldLabel>
						<div className="precise-input">
							<Input
								className="w-24 shrink-0"
								id="risk-input"
								type="number"
								min={8}
								max={18}
								step={1}
								value={Number.isFinite(mandate.risk) ? mandate.risk : ""}
								onChange={(event) =>
									update({ risk: event.target.valueAsNumber })
								}
								aria-describedby={
									riskError ? "risk-help risk-error" : "risk-help"
								}
								aria-invalid={invalidRisk}
							/>
							<span aria-hidden="true">% / year</span>
						</div>
						<Slider
							id="risk-budget"
							label="Annualized risk budget"
							valueText={
								invalidRisk
									? "Enter a risk budget between 8% and 18%."
									: `${mandate.risk}% annualized volatility budget`
							}
							min={8}
							max={18}
							step={1}
							value={[
								Number.isFinite(mandate.risk)
									? Math.max(8, Math.min(18, mandate.risk))
									: 8,
							]}
							onValueChange={([risk]) => update({ risk })}
						/>
						<FieldDescription id="risk-help">
							8–18% annualized volatility. A risk target is not a loss limit.
						</FieldDescription>
						{riskError && <FieldError id="risk-error">{riskError}</FieldError>}
					</Field>
					<Field className="mandate-field">
						<FieldLabel htmlFor="cost-assumption">
							Monthly cost assumption
						</FieldLabel>
						<Select
							value={String(mandate.cost)}
							onValueChange={(value) => update({ cost: Number(value) })}
						>
							<SelectTrigger id="cost-assumption" aria-describedby="cost-help">
								<SelectValue />
							</SelectTrigger>
							<SelectContent>
								{[5, 10, 20].map((cost) => (
									<SelectItem key={cost} value={String(cost)}>
										{cost} basis points
									</SelectItem>
								))}
							</SelectContent>
						</Select>
						<FieldDescription id="cost-help">
							{mandate.cost} bps = {(mandate.cost / 100).toFixed(2)}% deducted
							per month.
						</FieldDescription>
					</Field>
					<dl className="mandate-fixed">
						<div>
							<dt>Research window</dt>
							<dd>2021-2025</dd>
						</div>
						<div>
							<dt>Observation frequency</dt>
							<dd>Monthly</dd>
						</div>
						<div>
							<dt>Starting capital</dt>
							<dd>$10,000 USD</dd>
						</div>
					</dl>
					<details className="simulation-settings">
						<summary>
							About this simulation <Icon name="chevron-down" />
						</summary>
						<p>
							Your narrative is kept in the record. The selected lens, risk and
							costs shape a deterministic synthetic scenario. No language model
							or live backtest runs.
						</p>
						<label htmlFor="simulation-mode">Demonstrate recovery</label>
						<select
							id="simulation-mode"
							value={mandate.interrupt ? "interrupt" : "normal"}
							onChange={(event) =>
								update({ interrupt: event.target.value === "interrupt" })
							}
						>
							<option value="normal">Normal investigation</option>
							<option value="interrupt">Interrupt source retrieval</option>
						</select>
					</details>
				</aside>
				<div className="composer-submit">
					{error && !hypothesisError && !riskError && (
						<FieldError>{error}</FieldError>
					)}
					<p>
						<strong>In-browser simulation</strong> · About 8 seconds
						<br />
						<span className="muted">
							{mandate.lens} ·{" "}
							{Number.isFinite(mandate.risk) ? mandate.risk : "Unset"}% risk
							budget · {mandate.cost} bps/month
						</span>
					</p>
					<Button type="submit" className="button button-primary">
						Begin investigation <Icon name="arrow-right" />
					</Button>
				</div>
			</form>
		</div>
	);
}

const phases = [
	{
		name: "Frame the question",
		detail: "Preserve the hypothesis and make the assumptions explicit.",
		icon: "file-text",
	},
	{
		name: "Gather the evidence",
		detail:
			"Inspect three demonstration research notes and identify their limitations.",
		icon: "search",
	},
	{
		name: "Challenge the model",
		detail:
			"Keep competing approaches visible rather than presenting a single inevitable answer.",
		icon: "git-branch",
	},
	{
		name: "Test the argument",
		detail:
			"Compare the synthetic path with a benchmark and record what remains untested.",
		icon: "activity",
	},
];

export function Investigation({
	run,
	onAction,
}: {
	run: InvestigationRun | null;
	onAction: (action: RunAction) => void;
}) {
	const reduce = useReducedMotion();
	const [viewedStage, setViewedStage] = useState<number | null>(null);
	const [note, setNote] = useState<ResearchNote | null>(null);
	if (!run)
		return (
			<div className="empty-page">
				<Icon name="flask-conical" />
				<h1>Every investigation starts with a question.</h1>
				<p>Describe an idea to create a new research record.</p>
				<a className="button button-primary" href="#/new">
					Form a hypothesis <Icon name="arrow-right" />
				</a>
			</div>
		);
	if (run.status === "cancelled")
		return (
			<div className="empty-page">
				<Icon name="pause" />
				<h1>Investigation stopped.</h1>
				<p>
					Your hypothesis and assumptions are still in the composer. Nothing was
					submitted to a live service.
				</p>
				<a className="button button-primary" href="#/new">
					Return to hypothesis <Icon name="arrow-left" />
				</a>
			</div>
		);
	const complete = run.status === "complete";
	const stage = viewedStage ?? Math.min(run.stage, 3);
	const candidates = [
		run.study,
		...studies.filter((study) => study.lens !== run.study.lens).slice(0, 2),
	];
	return (
		<div className="workspace-page investigation-page">
			<div className="page-context">
				<a href="#/new">Your hypothesis</a>
				<Icon name="chevron-right" />
				<span>Investigation</span>
				<span className="context-end mono">
					{complete
						? "RESEARCH COMPLETE"
						: run.status === "error"
							? "AWAITING RECOVERY"
							: "SIMULATION IN PROGRESS"}
				</span>
			</div>
			<header className="investigation-heading">
				<div>
					<h1>
						{complete
							? "A result. And the reasoning behind it."
							: "An idea, under investigation."}
					</h1>
					<p>
						{complete
							? "The candidate is ready to inspect. The limitations travel with it."
							: "Watch the question become a research record. No hidden hand-offs."}
					</p>
				</div>
				<Badge className={`run-indicator ${complete ? "is-complete" : ""}`}>
					<Icon
						name={
							complete
								? "circle-check"
								: run.status === "error"
									? "circle-alert"
									: "activity"
						}
					/>
					{complete
						? "Complete"
						: run.status === "error"
							? "Paused"
							: `${run.stage + 1} of 4`}
				</Badge>
			</header>
			<ol className="research-stages">
				{phases.map((phase, i) => (
					<li
						key={phase.name}
						className={`${i < run.stage ? "stage-done" : ""} ${i === stage ? "stage-active" : ""}`}
					>
						<Button
							variant="ghost"
							className="disabled:opacity-100"
							disabled={i > run.stage}
							onClick={() => setViewedStage(i)}
							aria-current={i === stage ? "step" : undefined}
						>
							<motion.span
								className="stage-symbol"
								key={`${i < run.stage}-${i === run.stage}-${run.status}`}
								initial={reduce ? false : { scale: 0.85 }}
								animate={{ scale: 1 }}
								transition={{ duration: reduce ? 0 : 0.2 }}
							>
								<Icon
									name={
										i < run.stage
											? "check"
											: i === run.stage && run.status === "error"
												? "circle-alert"
												: phase.icon
									}
								/>
							</motion.span>
							<span className="stage-copy">
								<strong>{phase.name}</strong>
								<span>{phase.detail}</span>
							</span>
							<Badge>
								{i < run.stage
									? "Done"
									: i > run.stage
										? "Pending"
										: run.status === "error"
											? "Interrupted"
											: "Running"}
							</Badge>
						</Button>
					</li>
				))}
			</ol>
			<div className="investigation-status" role="status" aria-live="polite">
				<strong>
					{run.status === "error"
						? "Source retrieval interrupted"
						: complete
							? "Research record complete"
							: phases[Math.min(run.stage, 3)].name}
				</strong>
				<span>
					{run.status === "error"
						? "A simulated source outage. Retry keeps your progress."
						: `${viewedStage === null ? "" : `Viewing ${phases[stage].name}. `}${phases[stage].detail}`}
				</span>
			</div>
			{run.status === "error" && (
				<div className="recovery-banner" role="alert">
					<Icon name="circle-alert" />
					<div>
						<strong>One source did not respond.</strong>
						<p>
							The hypothesis is safe. Resume from the interrupted retrieval.
						</p>
					</div>
					<Button
						className="button button-primary"
						onClick={() => onAction("retry")}
					>
						<Icon name="rotate-ccw" /> Retry retrieval
					</Button>
				</div>
			)}
			<section
				className="investigation-canvas"
				aria-label="Research branching from hypothesis through candidate models to a result"
			>
				<article className="question-node">
					<span className="node-label">
						<Icon name="file-text" /> HYPOTHESIS
					</span>
					<h2>Your starting point</h2>
					<p>“{run.study.idea}”</p>
					<dl>
						<div>
							<dt>Risk budget</dt>
							<dd>{run.study.risk}%</dd>
						</div>
						<div>
							<dt>Monthly cost</dt>
							<dd>{run.study.cost} bps</dd>
						</div>
					</dl>
				</article>
				<div className="candidate-branches" aria-busy={run.stage < 3}>
					<span className="node-label">COMPETING APPROACHES</span>
					{candidates.map((candidate, i) => (
						<motion.article
							layout={reduce ? false : "position"}
							transition={{ duration: reduce ? 0 : 0.3 }}
							key={candidate.id}
							className={`candidate-node ${i === 0 ? "candidate-selected" : ""} ${run.stage < 2 ? "candidate-pending" : ""}`}
						>
							<div>
								<Icon name={i === 0 ? "git-branch" : "layers"} />
								<span>{candidate.name}</span>
								<span className="candidate-state">
									{run.stage < 2
										? "Waiting"
										: i === 0
											? "Your lens"
											: "Alternative"}
								</span>
							</div>
							{run.stage >= 2 ? (
								<Sparkline series={candidate.series} />
							) : (
								<div className="chart-skeleton" />
							)}
						</motion.article>
					))}
				</div>
				<article className={`result-node ${complete ? "result-ready" : ""}`}>
					<span className="node-label">
						<Icon name={complete ? "circle-check" : "activity"} /> RESEARCH
						RECORD
					</span>
					<h2>
						{complete ? "Ready for scrutiny." : "The evidence comes first."}
					</h2>
					{complete ? (
						<>
							<dl>
								<div>
									<dt>Annualized return</dt>
									<dd>{percent(metrics(run.study.series).cagr)}</dd>
								</div>
								<div>
									<dt>Maximum drawdown</dt>
									<dd>{percent(metrics(run.study.series).drawdown)}</dd>
								</div>
							</dl>
							<a
								className="button button-primary"
								href={`#/study/${run.study.id}`}
							>
								Open study <Icon name="arrow-right" />
							</a>
						</>
					) : (
						<>
							<p>Performance, assumptions and limitations stay together.</p>
							<div className="record-skeleton">
								<span />
								<span />
								<span />
							</div>
						</>
					)}
				</article>
			</section>
			<section className="investigation-sources">
				<header>
					<h2>Research on the desk</h2>
					<span className="tiny muted">
						Illustrative notes, not real publications
					</span>
				</header>
				<div>
					{researchNotes.map((source, i) => (
						<button
							key={source.id}
							disabled={run.stage < 1}
							onClick={() => setNote(source)}
						>
							<span className="source-glyph">
								<Icon name="file-text" />
							</span>
							<span>
								<span className="mono tiny muted">
									{source.id} / {source.kind}
								</span>
								<strong>{source.title}</strong>
							</span>
							<Icon
								name={
									run.stage > 1
										? "check"
										: i === 0
											? "activity"
											: "chevron-right"
								}
							/>
						</button>
					))}
				</div>
			</section>
			{!complete && (
				<div className="run-footer">
					<span className="muted small">
						Your hypothesis stays editable in this session.
					</span>
					<Button variant="ghost" onClick={() => onAction("cancel")}>
						Cancel investigation
					</Button>
				</div>
			)}
			{note && (
				<Modal title={note.title} onClose={() => setNote(null)}>
					<p className="note-type">{note.id} / Demonstration research note</p>
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
