// Run after `npm run concept:build` and `npm run concept:preview`:
// playwright-cli -s=observatory open http://127.0.0.1:5181
// playwright-cli -s=observatory run-code --filename=concept/browser-check.js
// Named declaration stays formatter-safe when CLI evaluates this as an expression.
async function _observatoryCheck(page) {
	const base = "http://127.0.0.1:5181";
	const out = "/tmp/archimedes-observatory-v3";
	const errors = [];
	const apiCalls = [];
	page.on("pageerror", (error) => errors.push(error.message));
	page.on("request", (request) => {
		if (request.url().includes("/api/")) apiCalls.push(request.url());
	});
	const check = (condition, message) => {
		if (!condition) throw new Error(message);
	};
	const frames = () =>
		page.evaluate(
			() =>
				new Promise((resolve) =>
					requestAnimationFrame(() => requestAnimationFrame(resolve)),
				),
		);
	const shot = async (name) => {
		await page.evaluate(() => {
			window.scrollTo({ top: 0, behavior: "instant" });
			return document.fonts.ready;
		});
		await frames();
		await page.screenshot({ path: `${out}/${name}.png`, fullPage: true });
	};
	const hash = async (route) => {
		await page.evaluate((route) => {
			window.location.hash = route;
		}, route);
		await page.locator("h1").waitFor();
		await frames();
	};
	const setTheme = async (value) => {
		if (
			(await page.locator(".observatory").getAttribute("data-theme")) !== value
		) {
			await page
				.getByRole("combobox", { name: "Appearance", exact: true })
				.click();
			await page
				.getByRole("option", {
					name: value === "light" ? "Light" : "Dark",
					exact: true,
				})
				.click();
		}
	};
	await page.emulateMedia({ reducedMotion: "reduce" });
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page.goto(base);
	await page.reload();
	await page
		.getByRole("heading", { name: "Conviction, meet evidence." })
		.waitFor();
	check(
		(await page.locator("h1").count()) === 1,
		"Initial route did not render exactly one h1",
	);
	check(
		(await page.locator("a a, button button, footer footer").count()) === 0,
		"Nested interactive elements or footers",
	);
	await page
		.getByRole("button", {
			name: /The defensive allocation, annualized return/,
		})
		.click();
	check(
		(await page.locator(".hero-selected").innerText()).includes(
			"The defensive allocation",
		),
		"Risk map selection did not update record",
	);
	await page.setViewportSize({ width: 768, height: 900 });
	const tabletAction = page.getByRole("link", {
		name: "New hypothesis",
		exact: true,
	});
	check(
		await tabletAction.isVisible(),
		"Tablet create link lost its accessible name",
	);
	await tabletAction.focus();
	await tabletAction.press("Enter");
	await page.locator("#hypothesis").waitFor();
	await hash("/");
	await page.setViewportSize({ width: 1440, height: 1000 });
	await page
		.getByRole("link", { name: "Start an investigation", exact: true })
		.click();
	await page
		.getByRole("button", { name: "Begin investigation", exact: true })
		.click();
	await page.getByRole("alert").waitFor();
	await page
		.getByRole("radio", { name: "Follow persistent trends", exact: true })
		.click();
	check(
		(await page.locator("#hypothesis").inputValue()).length > 24,
		"Example did not fill hypothesis",
	);
	const budget = page.getByRole("slider", {
		name: "Annualized risk budget",
		exact: true,
	});
	await budget.focus();
	await budget.press("ArrowRight");
	check(
		(await budget.getAttribute("aria-valuenow")) === "13",
		"Radix risk slider did not change mandate from keyboard",
	);
	await budget.press("ArrowLeft");
	await page.locator("#cost-assumption").click();
	await page
		.getByRole("option", { name: "20 basis points", exact: true })
		.click();
	await shot("hypothesis-desktop");
	await page.locator(".simulation-settings summary").click();
	await page.locator("#simulation-mode").selectOption("interrupt");
	await page
		.getByRole("button", { name: "Begin investigation", exact: true })
		.click();
	await page.locator(".investigation-canvas").waitFor();
	await shot("investigation-loading");
	await page.locator(".recovery-banner").waitFor();
	await shot("investigation-error");
	await page
		.getByRole("button", { name: "Retry retrieval", exact: true })
		.click();
	await page.getByRole("link", { name: "Open study", exact: true }).waitFor();
	await shot("investigation-complete");
	await page.getByRole("link", { name: "Open study", exact: true }).click();
	await page.locator(".metric-strip").waitFor();
	check(
		(await page.locator("h1").innerText()) === "Momentum research",
		"Generated study lost selected lens",
	);
	const fiveYear = await page.locator(".metric-strip dd").first().innerText();
	await page.getByRole("button", { name: "1Y", exact: true }).click();
	check(
		(await page.locator(".metric-strip dd").first().innerText()) !== fiveYear,
		"Window did not update summary metrics",
	);
	const slider = page.getByRole("slider", { name: /Inspect month/ });
	await slider.focus();
	await slider.press("Home");
	check(
		(await page.locator(".chart-inspection output").innerText()) === "$10,000",
		"Window does not rebase to starting capital",
	);
	await slider.press("ArrowRight");
	check(
		(await page.locator(".chart-inspection output").innerText()) !== "$10,000",
		"Keyboard chart inspection did not advance",
	);
	await page
		.getByRole("checkbox", { name: "Benchmark", exact: true })
		.uncheck();
	check(
		(await page.locator(".benchmark-line").count()) === 0,
		"Benchmark toggle did not remove line",
	);
	await page.getByRole("button", { name: "5Y", exact: true }).click();
	await page.getByRole("checkbox", { name: "Benchmark", exact: true }).check();
	await page.getByRole("tab", { name: "Growth", exact: true }).focus();
	await page.keyboard.press("ArrowRight");
	await page
		.getByRole("tab", { name: "Drawdown", exact: true, selected: true })
		.waitFor();
	check(
		(await page
			.getByRole("tab", { name: "Drawdown", exact: true })
			.getAttribute("aria-selected")) === "true",
		"Chart tabs did not activate through arrow-key navigation",
	);
	await page.getByRole("slider", { name: /Inspect month/ }).focus();
	await page.keyboard.press("Home");
	check(
		(await page.locator(".chart-inspection output").innerText()) === "0.0%",
		"Drawdown does not start at zero",
	);
	await page.keyboard.press("ArrowRight");
	check(
		(await page.locator(".chart-caption").innerText()).includes(
			"Decline from prior peak",
		),
		"Drawdown tab still renders growth",
	);
	await shot("drawdown-desktop");
	await page.getByRole("tab", { name: "Growth", exact: true }).click();
	await page.getByRole("button", { name: "Save study", exact: true }).click();
	check(
		(await page
			.getByRole("button", { name: "Saved for this session", exact: true })
			.getAttribute("aria-pressed")) === "true",
		"Study did not save",
	);
	const downloadPromise = page.waitForEvent("download");
	await page
		.getByRole("button", { name: "Export research as JSON", exact: true })
		.click();
	const download = await downloadPromise;
	await download.saveAs(`${out}/exported-study.json`);
	await shot("study-generated-desktop");
	await page
		.locator(".study-tabs")
		.getByRole("tab", { name: "Evidence record", exact: true })
		.click();
	await page.locator(".source-summary > button").first().click();
	await page
		.getByRole("button", { name: "Inspect source RN-014", exact: true })
		.click();
	await page.getByRole("dialog").waitFor();
	check(
		await page.evaluate(
			() => document.activeElement.closest('[role="dialog"]') !== null,
		),
		"Dialog did not receive focus",
	);
	await shot("source-dialog");
	await page.keyboard.press("Escape");
	await page.waitForFunction(
		() =>
			document.activeElement?.closest(".source-records") !== null &&
			!document.querySelector('[role="dialog"]'),
	);
	check(
		(await page.getByRole("dialog").count()) === 0,
		"Escape did not close research note",
	);
	check(
		await page.evaluate(
			() => document.activeElement.closest(".source-records") !== null,
		),
		"Dialog did not restore focus",
	);
	await page.locator(".validation-row > button").first().click();
	check(
		(await page
			.locator(".validation-row > button")
			.first()
			.getAttribute("aria-expanded")) === "true",
		"Evidence expansion failed",
	);
	await shot("evidence-desktop");
	await page
		.locator(".study-tabs")
		.getByRole("tab", { name: "Methodology", exact: true })
		.click();
	await shot("methodology-desktop");
	await page
		.locator(".main-nav")
		.getByRole("link", { name: "Research library", exact: true })
		.click();
	await page.locator("#research-search").fill("no-such-idea");
	await page
		.getByRole("heading", { name: "No research matches those filters." })
		.waitFor();
	await shot("library-empty");
	await page
		.getByRole("button", { name: "Clear filters", exact: true })
		.click();
	await frames();
	const catalogCount = await page.locator(".research-entry").count();
	for (const query of ["gold", " GLD "]) {
		await page.locator("#research-search").fill(query);
		await frames();
		check(
			(await page.locator(".research-entry").count()) === catalogCount,
			`Holdings search failed for ${query}`,
		);
	}
	await page.locator("#research-search").fill("");
	await page
		.getByRole("button", { name: "Capital preservation", exact: true })
		.click();
	check(
		(await page.locator(".research-entry").count()) === 1,
		"Category did not filter library",
	);
	await page.getByRole("button", { name: "All research", exact: true }).click();
	await page
		.getByRole("checkbox", { name: "Compare Adaptive momentum", exact: true })
		.check();
	await page
		.getByRole("checkbox", {
			name: "Compare The defensive allocation",
			exact: true,
		})
		.check();
	await page
		.getByRole("button", { name: "Compare research", exact: true })
		.click();
	await page.locator(".comparison-table").waitFor();
	check(
		(await page.locator(".comparison-table thead th").count()) === 3,
		"Comparison columns do not match two selections",
	);
	await shot("comparison-desktop");
	for (const id of ["quality", "defensive"]) {
		await hash(`/study/${id}/evidence`);
		await page.locator(".source-summary > button").first().click();
		await page
			.getByRole("button", { name: "Inspect source RN-014", exact: true })
			.click();
		const noteBody = await page
			.getByRole("dialog")
			.locator(".note-lede + p")
			.innerText();
		check(
			noteBody.includes("does not implement"),
			"Shared note implies a live or lens-specific signal",
		);
		await page.keyboard.press("Escape");
		const pending = page.waitForEvent("download");
		await page
			.getByRole("button", { name: "Export research as JSON", exact: true })
			.click();
		const downloaded = await pending;
		await downloaded.saveAs(`${out}/exported-${id}.json`);
		let json = "";
		for await (const chunk of await downloaded.createReadStream())
			json += chunk;
		let record;
		try {
			record = JSON.parse(json);
		} catch (error) {
			throw new Error(`Invalid ${id} research export`, { cause: error });
		}
		check(
			record.demonstration === true &&
				record.study.id === id &&
				record.notes[0].body === noteBody,
			"Export does not match visible evidence",
		);
	}
	const layouts = [];
	for (const width of [1440, 1024, 768, 375, 320]) {
		await page.setViewportSize({ width, height: 900 });
		for (const route of [
			"/",
			"/new",
			"/investigation",
			"/study/momentum",
			"/study/momentum/evidence",
			"/study/momentum/methodology",
			"/library",
			"/compare",
		]) {
			await hash(route);
			for (const theme of ["light", "dark"]) {
				await setTheme(theme);
				await frames();
				const size = await page.evaluate(() => ({
					width: document.documentElement.clientWidth,
					scroll: document.documentElement.scrollWidth,
				}));
				check(
					size.scroll <= size.width + 1,
					`Overflow ${route}/${width}/${theme}: ${JSON.stringify(size)}`,
				);
				check(
					(await page.locator("h1").count()) === 1,
					`Heading count ${route}`,
				);
				if (width !== 320)
					await shot(
						`view-${route.replaceAll("/", "_") || "home"}-${width}-${theme}`,
					);
				layouts.push(`${route}/${width}/${theme}`);
			}
		}
	}
	await hash("/new");
	check(
		(await page.locator("#hypothesis").inputValue()).length > 24,
		"Navigation lost the draft",
	);
	await page.locator(".simulation-settings summary").click();
	await page.locator("#simulation-mode").selectOption("normal");
	await page
		.getByRole("button", { name: "Begin investigation", exact: true })
		.click();
	await page
		.getByRole("button", { name: "Cancel investigation", exact: true })
		.click();
	await page.getByRole("heading", { name: "Investigation stopped." }).waitFor();
	await hash("/missing");
	await page
		.getByRole("heading", { name: "This research page is not here." })
		.waitFor();
	await page.setViewportSize({ width: 1440, height: 900 });
	await hash("/");
	await setTheme("light");
	check(errors.length === 0, `Runtime errors: ${errors.join("; ")}`);
	check(apiCalls.length === 0, "Concept attempted production API access");
	return {
		layouts: layouts.length,
		journey:
			"hypothesis validation/example, keyboard risk slider, interrupted research/retry, completed record, window/benchmark/keyboard chart, growth/drawdown keyboard tabs, save/export, evidence/disclosure/dialog focus, methodology, filters/empty, compare, cancel, draft preservation, missing route",
		runtimeErrors: errors,
		apiCalls,
	};
}
