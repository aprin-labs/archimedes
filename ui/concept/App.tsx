import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { motion, useReducedMotion } from "motion/react";
import { Select as AppearanceSelect } from "radix-ui";
import {
	advanceRun,
	makeStudy,
	metrics,
	percent,
	researchNotes,
	studies,
	type InvestigationRun,
	type Mandate,
	type Study as StudyRecord,
} from "./data.ts";
import Chart, { Sparkline } from "./Chart.tsx";
import RiskMap from "./RiskMap.tsx";
import { Brand, DemoFooter, Icon } from "./UI.tsx";
import { Composer, Investigation } from "./Workflows.tsx";
import { Library, Study, Comparison, type LibraryView } from "./Research.tsx";
import { Button } from "./components/ui/button";
import { Select, SelectContent, SelectItem } from "./components/ui/select";
import { BlurFade } from "./components/magicui/blur-fade";
import { Disclosure } from "./components/motion-primitives/disclosure";

// Tailark Dusk: hero nine, features one, closing action one (MIT, Irung).
// Compositions only: native React links and real interactive preview material,
// not their Next.js dependencies, stock imagery, gradients or mock dashboards.
function Discover() {
	const [selected, setSelected] = useState("momentum");
	const study = studies.find((item) => item.id === selected) || studies[0];
	const result = metrics(study.series);
	return (
		<div className="discover-page">
			<div className="cover-meta">
				<span>Independent thinking. Inspectable research.</span>
				<span className="mono">VOLUME 01 / DEMONSTRATION</span>
			</div>
			<section className="discovery-hero">
				<BlurFade className="hero-copy">
					<p className="eyebrow">
						The Research Observatory / An inspectable investment notebook
					</p>
					<h1>
						Conviction,
						<br />
						meet evidence.
					</h1>
					<p className="hero-description">
						An investment idea is a question, not a conclusion. Follow its
						assumptions, inspect its evidence, and find the next question worth
						asking.
					</p>
					<Button asChild className="button button-primary">
						<a href="#/new">
							Start an investigation <Icon name="arrow-up-right" />
						</a>
					</Button>
				</BlurFade>
				<BlurFade className="hero-observatory" delay={0.08}>
					<div className="figure-heading">
						<span className="mono">THE RESEARCH LANDSCAPE</span>
						<span className="tiny muted">
							4 fictional studies / 2021–2025 / select a coordinate
						</span>
					</div>
					<RiskMap items={studies} selected={selected} onSelect={setSelected} />
					<div className="hero-selected" aria-live="polite" aria-atomic="true">
						<div>
							<span className="tiny muted">Selected research</span>
							<h2>{study.name}</h2>
							<a className="text-link" href={`#/study/${study.id}`}>
								Open the study <Icon name="arrow-up-right" />
							</a>
						</div>
						<dl>
							<div>
								<dt>Annualized return</dt>
								<dd>{percent(result.cagr)}</dd>
							</div>
							<div>
								<dt>Maximum drawdown</dt>
								<dd>{percent(result.drawdown)}</dd>
							</div>
						</dl>
						<Chart items={[study]} compact />
						<p className="tiny muted">
							{study.risk}% risk budget · {study.cost} bps/month. Synthetic, not
							historical returns.
						</p>
					</div>
				</BlurFade>
			</section>
			<div className="discovery-rule">
				<span>An idea is a starting point.</span>
				<span>The work is finding out where it holds.</span>
				<Icon name="arrow-down" />
			</div>
			<section className="product-introduction">
				<div className="product-copy">
					<h2>
						A strategy is an argument.
						<br />
						Keep the evidence.
					</h2>
					<p>
						Follow every assumption from the original question to the final
						result. The path matters as much as the performance.
					</p>
					<a className="text-link" href="#/study/momentum/evidence">
						Examine the evidence <Icon name="arrow-up-right" />
					</a>
					<div className="research-footnote">
						<Icon name="git-branch" />
						<p>
							Not a black box.
							<br />A research record you can open.
						</p>
					</div>
				</div>
				<div className="live-product">
					<div className="mini-toolbar">
						<span>
							<Icon name="file-text" /> {study.name}
						</span>
						<span className="status-label">Research sample</span>
					</div>
					<p className="feature-hypothesis">
						<span className="label">ORIGINAL QUESTION</span>
						{study.idea}
					</p>
					<div className="mini-metrics">
						<div>
							<span>Annualized return</span>
							<b>{percent(result.cagr)}</b>
						</div>
						<div>
							<span>Sharpe ratio</span>
							<b>{result.sharpe.toFixed(2)}</b>
						</div>
						<a
							href={`#/study/${study.id}`}
							className="icon-button"
							aria-label={`Open ${study.name} study`}
						>
							<Icon name="arrow-up-right" />
						</a>
					</div>
					<Disclosure
						className="feature-evidence"
						summary={
							<>
								<span>{researchNotes[1].title}</span>
								<Icon name="chevron-down" />
							</>
						}
					>
						<p>{researchNotes[1].body}</p>
					</Disclosure>
					<p className="feature-limit">
						<strong>Known limitation.</strong> {researchNotes[1].limitation}
					</p>
					<a className="mini-evidence" href={`#/study/${study.id}/evidence`}>
						<Icon name="file-text" />
						<span>Inspect the method, not just the curve.</span>
						<Icon name="arrow-right" />
					</a>
				</div>
			</section>
			<section className="selected-research">
				<header>
					<h2>
						Different questions.
						<br />
						Different portfolios.
					</h2>
					<a className="text-link" href="#/library">
						Research library <Icon name="arrow-up-right" />
					</a>
				</header>
				<div className="editorial-studies">
					{studies.slice(1, 3).map((study) => (
						<a
							href={`#/study/${study.id}`}
							className="editorial-study"
							key={study.id}
						>
							<div className="study-category">
								<span>{study.category}</span>
								<Icon name="arrow-up-right" />
							</div>
							<h3>{study.name}</h3>
							<p>{study.description}</p>
							<Sparkline series={study.series} />
							<span className="mono tiny">
								{percent(metrics(study.series).cagr)} annualized / simulated
							</span>
						</a>
					))}
				</div>
			</section>
			<section className="closing-action">
				<p className="eyebrow">Your next investigation</p>
				<h2>
					What would change
					<br />
					your mind?
				</h2>
				<p>
					Bring a hypothesis. Make the assumptions visible.
					<br />
					Leave with a research record, not a promise.
				</p>
				<Button asChild>
					<a href="#/new">
						Form your hypothesis <Icon name="arrow-up-right" />
					</a>
				</Button>
				<span className="tiny muted">
					Fictional model. No account. Nothing sent to a live service.
				</span>
			</section>
			<div className="institution-footer">
				<Brand />
				<DemoFooter />
			</div>
		</div>
	);
}

export default function App() {
	const reduce = useReducedMotion();
	const [route, setRoute] = useState(
		() => window.location.hash.slice(1) || "/",
	);
	const [themeChoice, setThemeChoice] = useState<"light" | "dark" | "system">(
		() => {
			const initial = document.documentElement.dataset.appearance;
			return initial === "light" || initial === "dark" ? initial : "system";
		},
	);
	const [systemDark, setSystemDark] = useState(
		() => window.matchMedia("(prefers-color-scheme: dark)").matches,
	);
	const [appearanceError, setAppearanceError] = useState(false);
	const [menu, setMenu] = useState(false);
	const [run, setRun] = useState<InvestigationRun | null>(null);
	const [archive, setArchive] = useState<StudyRecord[]>([]);
	const [saved, setSaved] = useState(["momentum"]);
	const [comparison, setComparison] = useState<string[]>([]);
	const [libraryView, setLibraryView] = useState<LibraryView>({
		query: "",
		category: "All research",
		savedOnly: false,
		sort: "recent",
		direction: "desc",
		columns: ["growth", "cagr", "drawdown"],
	});
	const [mandate, setMandate] = useState<Mandate>({
		idea: "",
		lens: "momentum",
		risk: 12,
		cost: 10,
		interrupt: false,
	});
	const sequence = useRef(0);
	const previousRoute = useRef(route);
	const main = useRef<HTMLElement>(null);
	const menuTrigger = useRef<HTMLButtonElement>(null);
	const publicPage = route === "/" || route === "/library";
	const systemTheme = systemDark ? "dark" : "light";
	const theme = themeChoice === "system" ? systemTheme : themeChoice;
	const catalog = [...archive, ...studies];
	const parts = route.split("/").filter(Boolean);
	const study =
		catalog.find((item) => item.id === parts[1]) ||
		(run && run.study.id === parts[1] ? run.study : null);

	useEffect(() => {
		const onHash = () => {
			setRoute(window.location.hash.slice(1) || "/");
			setMenu(false);
		};
		window.addEventListener("hashchange", onHash);
		return () => window.removeEventListener("hashchange", onHash);
	}, []);
	useEffect(() => {
		const media = window.matchMedia("(prefers-color-scheme: dark)");
		const onChange = (event: MediaQueryListEvent) =>
			setSystemDark(event.matches);
		media.addEventListener("change", onChange);
		return () => media.removeEventListener("change", onChange);
	}, []);
	useLayoutEffect(() => {
		const root = document.documentElement;
		root.dataset.theme = theme;
		root.dataset.appearance = themeChoice;
		root.style.colorScheme = theme;
		document
			.querySelector('meta[name="theme-color"]')
			?.setAttribute(
				"content",
				getComputedStyle(root).getPropertyValue("--canvas").trim(),
			);
	}, [theme, themeChoice]);
	useEffect(() => {
		if (previousRoute.current !== route) {
			const sameStudy =
				route.startsWith("/study/") &&
				previousRoute.current.split("/").slice(0, 3).join("/") ===
					route.split("/").slice(0, 3).join("/");
			if (!sameStudy || !document.activeElement?.closest(".study-tabs")) {
				window.scrollTo({ top: 0, behavior: "instant" });
				main.current?.focus({ preventScroll: true });
			}
			previousRoute.current = route;
		}
		document.title = `${route === "/" ? "Research Observatory" : route === "/new" ? "New hypothesis" : route === "/investigation" ? "Investigation" : route === "/library" ? "Research library" : route === "/compare" ? "Compare research" : study?.name || "Research"} | Archimedes`;
	}, [route, study?.name]);
	useEffect(() => {
		if (run?.status !== "running") return;
		const timer = window.setTimeout(
			() => setRun((current) => (current ? advanceRun(current, "tick") : null)),
			1800,
		);
		return () => window.clearTimeout(timer);
	}, [run]);
	useEffect(() => {
		if (run?.status === "complete")
			setArchive((current) =>
				current.some((item) => item.id === run.study.id)
					? current
					: [run.study, ...current],
			);
	}, [run]);
	useEffect(() => {
		if (!menu) return;
		const close = (event: KeyboardEvent) => {
			if (event.key !== "Escape") return;
			setMenu(false);
			menuTrigger.current?.focus();
		};
		document.addEventListener("keydown", close);
		return () => document.removeEventListener("keydown", close);
	}, [menu]);
	const chooseAppearance = (value: string) => {
		if (value !== "light" && value !== "dark" && value !== "system") return;
		setThemeChoice(value);
		try {
			localStorage.setItem("archimedes.observatory.appearance", value);
			setAppearanceError(false);
		} catch {
			setAppearanceError(true);
		}
	};
	const start = () => {
		sequence.current++;
		setRun({
			stage: 0,
			status: "running",
			interrupt: mandate.interrupt,
			study: makeStudy(mandate, `session-${sequence.current}`),
		});
		window.location.hash = "/investigation";
	};
	const toggleSaved = (id: string) =>
		setSaved((current) =>
			current.includes(id)
				? current.filter((value) => value !== id)
				: [...current, id],
		);
	const toggleCompare = (id: string) =>
		setComparison((current) =>
			current.includes(id)
				? current.filter((value) => value !== id)
				: current.length < 3
					? [...current, id]
					: current,
		);
	let page;
	if (route === "/") page = <Discover />;
	else if (route === "/new")
		page = (
			<Composer mandate={mandate} setMandate={setMandate} onStart={start} />
		);
	else if (route === "/investigation")
		page = (
			<Investigation
				run={run}
				onAction={(action) =>
					setRun((current) => (current ? advanceRun(current, action) : null))
				}
			/>
		);
	else if (route === "/library")
		page = (
			<Library
				studies={catalog}
				view={libraryView}
				setView={setLibraryView}
				saved={saved}
				comparison={comparison}
				toggleCompare={toggleCompare}
			/>
		);
	else if (route === "/compare")
		page = (
			<Comparison
				studies={catalog.filter((item) => comparison.includes(item.id))}
			/>
		);
	else if (
		parts[0] === "study" &&
		study &&
		parts.length <= 3 &&
		(!parts[2] || ["evidence", "methodology"].includes(parts[2]))
	)
		page = (
			<Study
				key={study.id}
				study={study}
				tab={parts[2] || "overview"}
				saved={saved.includes(study.id)}
				onSave={() => toggleSaved(study.id)}
				onRefine={(current) => {
					setMandate({
						idea: current.idea,
						lens: current.lens,
						risk: current.risk,
						cost: current.cost,
						interrupt: false,
					});
					window.location.hash = "/new";
				}}
			/>
		);
	else
		page = (
			<div className="empty-page">
				<Icon name="search" />
				<h1>This research page is not here.</h1>
				<p>Return to the library to find an available demonstration study.</p>
				<a className="button button-primary" href="#/library">
					Research library <Icon name="arrow-right" />
				</a>
			</div>
		);
	return (
		<div
			className={`observatory ${publicPage ? "editorial-shell" : "workspace-shell"}`}
			data-theme={theme}
		>
			<a
				className="skip-link"
				href="#main-content"
				onClick={(event) => {
					event.preventDefault();
					main.current?.focus();
				}}
			>
				Skip to content
			</a>
			<header className="site-header">
				<Brand />
				<nav
					id="main-navigation"
					className={`main-nav ${menu ? "nav-open" : ""}`}
					aria-label="Main navigation"
				>
					<a href="#/" aria-current={route === "/" ? "page" : undefined}>
						Discover
					</a>
					<a
						href="#/library"
						aria-current={route === "/library" ? "page" : undefined}
					>
						Research library
					</a>
					<a
						href="#/study/momentum/methodology"
						aria-current={parts[2] === "methodology" ? "page" : undefined}
					>
						Methodology
					</a>
					{run?.status === "running" && (
						<a className="running-nav" href="#/investigation">
							Investigation <span className="live-dot" />
						</a>
					)}
				</nav>
				<div className="header-actions">
					<span className="demo-tag">Interactive concept</span>
					<Select value={themeChoice} onValueChange={chooseAppearance}>
						<AppearanceSelect.Trigger
							id="appearance"
							className="icon-button"
							aria-label="Appearance"
							aria-describedby="appearance-description"
							title={`Appearance: ${themeChoice} (${theme})`}
						>
							<Icon name={theme === "light" ? "moon" : "sun"} />
						</AppearanceSelect.Trigger>
						<SelectContent>
							<SelectItem value="light">Light</SelectItem>
							<SelectItem value="dark">Dark</SelectItem>
							<SelectItem value="system">System</SelectItem>
						</SelectContent>
					</Select>
					<span id="appearance-description" className="sr-only">
						Preference: {themeChoice}. Current appearance: {theme}. System
						follows your device preference.
					</span>
					<a className="header-create" href="#/new" aria-label="New hypothesis">
						<span>New hypothesis</span>
						<Icon name="plus" />
					</a>
					<button
						ref={menuTrigger}
						aria-controls="main-navigation"
						className="icon-button mobile-menu"
						aria-label={menu ? "Close navigation" : "Open navigation"}
						aria-expanded={menu}
						onClick={() => setMenu((value) => !value)}
					>
						<Icon name={menu ? "x" : "menu"} />
					</button>
				</div>
			</header>
			{appearanceError && (
				<p className="appearance-notice" role="status">
					Not saved — browser storage is unavailable. Appearance applies to this
					visit.
				</p>
			)}
			<main id="main-content" ref={main} tabIndex={-1} className="main-content">
				<motion.div
					key={parts[0] === "study" ? `study-${parts[1]}` : route}
					initial={reduce ? false : { opacity: 0, y: 5 }}
					animate={{ opacity: 1, y: 0 }}
					transition={{ duration: reduce ? 0 : 0.22 }}
				>
					{page}
				</motion.div>
			</main>
			{!publicPage && <DemoFooter />}
		</div>
	);
}
