// Built-preview contract: playwright-cli run-code --filename=concept/component-check.js
async function _componentCheck(page) {
	const failures = [];
	const passed = [];
	const check = (ok, message) => {
		if (!ok) throw new Error(message);
	};
	const visit = async (route) => {
		await page.goto(`http://127.0.0.1:5181/#${route}`);
		// Hash changes do not reload Vite's built assets or reset session state.
		await page.reload();
		await page.locator("h1").waitFor();
	};
	const test = async (name, run) => {
		try {
			await run();
			passed.push(name);
		} catch (error) {
			failures.push({ name, message: error.message });
		}
	};
	const choose = async (id, label) => {
		await page.locator(`#${id}`).click();
		await page.getByRole("option", { name: label, exact: true }).click();
	};
	await page.emulateMedia({ reducedMotion: "reduce" });
	await page.setViewportSize({ width: 1440, height: 1000 });
	await test("Choicebox controls the real mandate and precise risk input", async () => {
		await visit("/new");
		const choices = page.getByRole("radiogroup", { name: "Research presets" });
		check((await choices.count()) === 1, "Missing accessible preset Choicebox");
		await choices
			.getByRole("radio", { name: "Protect the downside", exact: true })
			.click();
		check(
			(await page.locator("#hypothesis").inputValue()).includes(
				"defensive allocation",
			),
			"Preset did not change hypothesis",
		);
		const risk = page.getByRole("spinbutton", {
			name: "Annualized risk budget (%)",
			exact: true,
		});
		check(
			(await risk.count()) === 1,
			"Risk slider lacks precise numeric partner",
		);
		check((await risk.inputValue()) === "8", "Preset did not change risk");
		await risk.fill("11");
		const slider = page.getByRole("slider", {
			name: "Annualized risk budget",
			exact: true,
		});
		check(
			(await slider.getAttribute("aria-valuenow")) === "11",
			"Numeric input did not update slider",
		);
		await slider.focus();
		await slider.press("ArrowRight");
		check(
			(await risk.inputValue()) === "12",
			"Slider did not update numeric input",
		);
		await risk.fill("30");
		await page
			.getByRole("button", { name: "Begin investigation", exact: true })
			.click();
		check(
			(await page.getByRole("alert").count()) > 0,
			"Out-of-range risk was accepted",
		);
		check(
			(await risk.getAttribute("aria-describedby")).includes("risk-error"),
			"Risk error is not associated with risk input",
		);
		check(
			(await page.locator("#hypothesis").getAttribute("aria-invalid")) ===
				"false",
			"Risk error incorrectly marks hypothesis invalid",
		);
		await risk.fill("12");
		await choose("cost-assumption", "20 basis points");
		await page
			.getByRole("button", { name: "Begin investigation", exact: true })
			.click();
		await page.locator(".question-node").waitFor();
		check(
			(await page.locator(".question-node").innerText()).includes("20 bps"),
			"Submission lost cost assumption",
		);
	});
	await test("A customized preset can be reapplied without changing lens", async () => {
		await visit("/new");
		const choices = page.getByRole("radiogroup", { name: "Research presets" });
		const momentum = choices.getByRole("radio", {
			name: "Follow persistent trends",
			exact: true,
		});
		await momentum.click();
		const preset = await page.locator("#hypothesis").inputValue();
		await page
			.locator("#hypothesis")
			.fill(
				"My custom question about persistent trends and changing market volatility.",
			);
		await page.locator("#risk-input").fill("18");
		check(
			(await choices.getByRole("radio", { checked: true }).count()) === 0,
			"Custom draft is falsely marked as an unchanged preset",
		);
		await momentum.click();
		check(
			(await page.locator("#hypothesis").inputValue()) === preset,
			"Same-lens preset did not restore its narrative",
		);
		check(
			(await page.locator("#risk-input").inputValue()) === "12",
			"Same-lens preset did not restore risk budget",
		);
	});
	await test("Keyboard chart inspection overrides parked pointer", async () => {
		await visit("/study/momentum");
		const plot = page.locator(".plot-body");
		await plot.scrollIntoViewIfNeeded();
		const box = await plot.boundingBox();
		await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
		const slider = page.getByRole("slider", { name: /Inspect month/ });
		await slider.focus();
		const before = Number(await slider.inputValue());
		await slider.press("ArrowLeft");
		check(
			Number(await slider.inputValue()) === before - 1,
			"Parked pointer masks keyboard month changes",
		);
		await slider.press("ArrowLeft");
		check(
			Number(await slider.inputValue()) === before - 2,
			"Repeated keyboard inspection is stuck",
		);
	});
	await test("Study tabs keep focus, context and chart window", async () => {
		await visit("/study/momentum");
		const overview = page.getByRole("tab", { name: "Overview", exact: true });
		check(
			(await overview.count()) === 1,
			"Study views are links, not Radix tabs",
		);
		await page.getByRole("button", { name: "1Y", exact: true }).click();
		await overview.focus();
		await overview.press("ArrowRight");
		const evidence = page.getByRole("tab", {
			name: "Evidence record",
			selected: true,
		});
		await evidence.waitFor();
		check(
			await evidence.evaluate((el) => el === document.activeElement),
			"Hash navigation stole tab focus",
		);
		check(
			(await page.locator(".study-context").innerText()).includes("12%"),
			"Study assumptions disappeared",
		);
		await evidence.press("ArrowLeft");
		await page.getByRole("tab", { name: "Overview", selected: true }).waitFor();
		check(
			(await page
				.getByRole("button", { name: "1Y", exact: true })
				.getAttribute("aria-pressed")) === "true",
			"Study window reset",
		);
	});
	await test("Evidence expands inline, retains limitations and restores dialog focus", async () => {
		await visit("/study/momentum/evidence");
		const summary = page
			.locator(".source-records .source-summary > button")
			.first();
		check((await summary.count()) === 1, "Source notes lack inline Disclosure");
		check(
			await page
				.locator(".source-records .source-limitation")
				.first()
				.isVisible(),
			"Limitations hidden behind expansion",
		);
		await summary.click();
		const trigger = page.getByRole("button", {
			name: "Inspect source RN-014",
			exact: true,
		});
		await trigger.click();
		await page.getByRole("dialog").waitFor();
		await page.keyboard.press("Escape");
		await page.waitForFunction(
			() =>
				document.activeElement?.textContent?.includes(
					"Inspect source RN-014",
				) && !document.querySelector('[role="dialog"]'),
		);
		check(
			await trigger.evaluate((el) => el === document.activeElement),
			"Dialog did not restore source trigger",
		);
		await summary.click();
		check(
			!(await trigger.isVisible()),
			"Collapsed source action remains visible",
		);
	});
	await test("Table sorts metrics, customizes columns and preserves filters on return", async () => {
		await visit("/library");
		const table = page.getByRole("table", {
			name: "Demonstration strategy metrics",
		});
		check(
			(await table.count()) === 1,
			"Library lacks semantic Kibo table adaptation",
		);
		const sort = page.getByRole("button", {
			name: "Sort by Annualized return",
			exact: true,
		});
		await sort.click();
		const values = await page
			.locator('.research-entry [data-metric="cagr"]')
			.allTextContents();
		check(
			parseFloat(values[0]) >= parseFloat(values.at(-1)),
			"Return sort is not descending",
		);
		await sort.click();
		check(
			(await table.locator('th[aria-sort="ascending"]').count()) === 1,
			"Sort direction not exposed",
		);
		await page.getByRole("button", { name: "Columns", exact: true }).click();
		await page
			.getByRole("checkbox", { name: "Sharpe ratio column", exact: true })
			.check();
		check(
			(await table
				.getByRole("columnheader", { name: /Sharpe ratio/ })
				.count()) === 1,
			"Column toggle did not change table",
		);
		await page
			.getByRole("checkbox", { name: "Maximum drawdown column", exact: true })
			.uncheck();
		check(
			(await table
				.getByRole("columnheader", { name: /Maximum drawdown/ })
				.count()) === 0,
			"Column remains visible",
		);
		await page.locator("#research-search").fill("adaptive");
		await page
			.getByRole("checkbox", { name: "Compare Adaptive momentum", exact: true })
			.check();
		await page.locator(".entry-title a").first().click();
		await page
			.locator(".page-context")
			.getByRole("link", { name: "Research library", exact: true })
			.click();
		check(
			(await page.locator("#research-search").inputValue()) === "adaptive",
			"Navigation lost library query",
		);
		check(
			await page
				.getByRole("checkbox", {
					name: "Compare Adaptive momentum",
					exact: true,
				})
				.isChecked(),
			"Navigation lost comparison",
		);
		await page.setViewportSize({ width: 375, height: 900 });
		check(
			(await page
				.getByRole("button", { name: "Sort by Annualized return", exact: true })
				.count()) === 0,
			"Mobile table exposes invisible header controls to keyboard users",
		);
		check(
			await page.locator(".tray-studies").isVisible(),
			"Mobile tray hides selected study names",
		);
		check(
			await page.evaluate(
				() => document.documentElement.scrollWidth <= innerWidth + 1,
			),
			"Mobile table overflows page",
		);
		await page.setViewportSize({ width: 1440, height: 1000 });
	});
	await test("Temporary saves say Saved for this session", async () => {
		await visit("/study/quality");
		await page.getByRole("button", { name: "Save study", exact: true }).click();
		check(
			(await page
				.getByRole("button", { name: "Saved for this session", exact: true })
				.count()) === 1,
			"Save implies durable persistence",
		);
		check(
			(await page.locator('[role="status"]').allTextContents()).some((text) =>
				text.includes("Saved for this session"),
			),
			"Save is not announced",
		);
	});
	await test("Supported cost scenario shows labeled differences and exports same series", async () => {
		await visit("/study/momentum");
		await page.locator("#study-cost").click();
		await page
			.getByRole("option", { name: "20 basis points", exact: true })
			.click();
		check(
			!(await page.locator(".metric-delta").allTextContents()).some((text) =>
				/^[+-]0\.00\b/.test(text),
			),
			"Rounded-zero differences falsely imply direction",
		);
		await visit("/study/regime");
		check(
			(await page.locator("#study-cost").count()) === 1,
			"Study lacks supported scenario control",
		);
		check(
			(await page.locator(".study-opening").count()) === 1,
			"Study lacks finding/limitation/next opening",
		);
		const original = await page.locator(".metric-strip dd").first().innerText();
		await choose("study-cost", "20 basis points");
		check(
			(await page.locator(".metric-strip dd").first().innerText()) !== original,
			"Scenario did not change metrics",
		);
		check(
			(await page.locator(".metric-delta").first().innerText()).includes("pp"),
			"Difference lacks percentage-point units",
		);
		check(
			(await page.locator(".scenario-note").innerText()).includes("10 bps"),
			"Difference lacks recorded baseline",
		);
		const pending = page.waitForEvent("download");
		await page
			.getByRole("button", { name: "Export research as JSON", exact: true })
			.click();
		const download = await pending;
		let json = "";
		for await (const chunk of await download.createReadStream()) json += chunk;
		let record;
		try {
			record = JSON.parse(json);
		} catch (error) {
			throw new Error("Invalid scenario export", { cause: error });
		}
		check(
			record.study.cost === 20 && record.study.id === "regime",
			"Export lost scenario or study identity",
		);
		check(
			record.recordedAssumptions.cost === 10,
			"Export lost recorded baseline",
		);
		check(
			record.study.rawSeries.length === 61,
			"Export lost source observations",
		);
		await page.getByRole("slider", { name: /Inspect month/ }).focus();
		await page.keyboard.press("End");
		const shown = (
			await page.locator(".chart-inspection output").innerText()
		).replace(/[$,]/g, "");
		check(
			Number(shown) === Math.round(record.study.series.at(-1).value * 100),
			"Chart and export disagree",
		);
		await choose("study-cost", "10 basis points");
		check(
			(await page.locator(".metric-strip dd").first().innerText()) === original,
			"Restoring assumptions did not restore results",
		);
	});
	await test("Coordinate keyboard navigation updates shared landing study material", async () => {
		await visit("/");
		const point = page.locator(".map-point").first();
		await point.focus();
		await point.press("ArrowRight");
		check(
			(await page.locator(".map-point").nth(1).getAttribute("aria-pressed")) ===
				"true",
			"Coordinate arrow navigation missing",
		);
		const selected = await page.locator(".hero-selected h2").innerText();
		check(
			(
				await page.locator(".hero-observatory .chart-legend").innerText()
			).includes(selected),
			"Landing chart is unrelated to selected coordinates",
		);
	});
	await test("Landing labels wrap with enlarged text on narrow screens", async () => {
		await visit("/");
		await page.evaluate(() => {
			document.documentElement.style.fontSize = "32px";
		});
		try {
			for (const width of [375, 320]) {
				await page.setViewportSize({ width, height: 450 });
				check(
					await page.evaluate(
						() => document.documentElement.scrollWidth <= innerWidth + 1,
					),
					`Enlarged landing overflows at ${width}`,
				);
			}
		} finally {
			await page.evaluate(() => {
				document.documentElement.style.fontSize = "";
			});
			await page.setViewportSize({ width: 1440, height: 1000 });
		}
	});
	return { passed, failures };
}
