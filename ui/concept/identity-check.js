// Built preview: playwright-cli run-code --filename=concept/identity-check.js
async function _identityCheck(page) {
	const base = "http://127.0.0.1:5181/";
	const key = "archimedes.observatory.appearance";
	const passed = [];
	const failures = [];
	const check = (ok, message) => {
		if (!ok) throw new Error(message);
	};
	const test = async (name, run) => {
		try {
			await run();
			passed.push(name);
		} catch (error) {
			failures.push({ name, message: error.message });
		}
	};
	const withBrowser = async (run, options = {}) => {
		const context = await page
			.context()
			.browser()
			.newContext({
				colorScheme: "light",
				reducedMotion: "reduce",
				viewport: { width: 1440, height: 1000 },
				...options,
			});
		try {
			await run(await context.newPage(), context);
		} finally {
			await context.close();
		}
	};
	const choose = async (view, label) => {
		const control = view.getByRole("combobox", {
			name: "Appearance",
			exact: true,
		});
		check(
			(await control.count()) === 1,
			"Missing Light / Dark / System appearance selector",
		);
		await control.click();
		await view.getByRole("option", { name: label, exact: true }).click();
	};
	const themeIs = async (view, expected) => {
		try {
			await view.waitForFunction(
				(value) => document.documentElement.dataset.theme === value,
				expected,
				{ timeout: 3000 },
			);
		} catch {
			const actual = await view.evaluate(() => ({
				theme: document.documentElement.dataset.theme,
				preference: document.documentElement.dataset.appearance,
				systemDark: matchMedia("(prefers-color-scheme: dark)").matches,
			}));
			throw new Error(
				`Expected ${expected} at ${view.url()}: ${JSON.stringify(actual)}`,
			);
		}
	};

	await test("Correct stored/system theme before React loads", async () => {
		for (const [stored, system, expected] of [
			[null, "dark", "dark"],
			[null, "light", "light"],
			["light", "dark", "light"],
			["dark", "light", "dark"],
			["system", "dark", "dark"],
			["invalid", "dark", "dark"],
		]) {
			await withBrowser(
				async (view, context) => {
					await context.addInitScript(
						({ key, stored }) => {
							if (stored !== null) localStorage.setItem(key, stored);
						},
						{ key, stored },
					);
					// Inline bootstrap must work even if the React bundle never arrives.
					await context.route("**/*", (route) =>
						route.request().resourceType() === "script"
							? route.abort()
							: route.continue(),
					);
					await view.goto(`${base}#/study/momentum`);
					const initial = await view.evaluate(() => ({
						theme: document.documentElement.dataset.theme,
						preference: document.documentElement.dataset.appearance,
						scheme: getComputedStyle(document.documentElement).colorScheme,
						canvas: getComputedStyle(document.documentElement).backgroundColor,
						metadata: document.querySelector('meta[name="theme-color"]')
							.content,
						mounted: document.getElementById("root").childElementCount,
					}));
					check(
						initial.theme === expected,
						`Before React: expected ${expected}, got ${initial.theme}`,
					);
					check(
						initial.preference ===
							(["light", "dark", "system"].includes(stored)
								? stored
								: "system"),
						"Invalid initial preference",
					);
					check(
						initial.scheme === expected,
						"Initial native color scheme differs",
					);
					check(
						initial.canvas ===
							(expected === "dark" ? "rgb(13, 25, 23)" : "rgb(242, 241, 232)"),
						"Initial canvas flashes another palette",
					);
					check(
						initial.metadata.toUpperCase() ===
							(expected === "dark" ? "#0D1917" : "#F2F1E8"),
						"Initial browser chrome differs",
					);
					check(
						initial.mounted === 0,
						"First-paint test accidentally ran React",
					);
				},
				{ colorScheme: system },
			);
		}
	});

	await test("Explicit appearance persists; System follows OS, not route", async () => {
		await withBrowser(async (view) => {
			await view.goto(`${base}#/new`);
			await view.locator("h1").waitFor();
			const draft =
				"Keep my research hypothesis intact while changing appearance.";
			await view.locator("#hypothesis").fill(draft);
			await choose(view, "Dark");
			await themeIs(view, "dark");
			check(
				(await view.locator("#hypothesis").inputValue()) === draft,
				"Appearance reset research draft",
			);
			check(
				(await view.evaluate((key) => localStorage.getItem(key), key)) ===
					"dark",
				"Explicit dark choice not stored",
			);
			await view.reload();
			await view.locator("h1").waitFor();
			await themeIs(view, "dark");
			await choose(view, "Light");
			await view.emulateMedia({ colorScheme: "dark" });
			await themeIs(view, "light");
			await view.reload();
			await view.locator("h1").waitFor();
			await themeIs(view, "light");
			await choose(view, "System");
			await themeIs(view, "dark");
			check(
				(await view.evaluate((key) => localStorage.getItem(key), key)) ===
					"system",
				"Explicit system choice not stored",
			);
			await view.emulateMedia({ colorScheme: "light" });
			await themeIs(view, "light");
			await view.evaluate(() => {
				location.hash = "/study/momentum/evidence";
			});
			await view.getByRole("tab", { name: /Evidence/ }).waitFor();
			await themeIs(view, "light");
			await view.emulateMedia({ colorScheme: "dark" });
			await themeIs(view, "dark");
			await view
				.getByRole("combobox", { name: "Appearance", exact: true })
				.focus();
			await view.keyboard.press("Enter");
			await view.getByRole("listbox").waitFor();
			await view.keyboard.press("Home");
			await view.waitForFunction(
				() => document.activeElement?.textContent === "Light",
			);
			await view.keyboard.press("Enter");
			await themeIs(view, "light");
			check(
				(await view
					.getByRole("tab", { name: /Evidence/ })
					.getAttribute("aria-selected")) === "true",
				"Appearance lost study tab",
			);
		});
	});

	await test("Unavailable storage falls back safely and reports unsaved choice", async () => {
		await withBrowser(
			async (view, context) => {
				const errors = [];
				view.on("pageerror", (error) => errors.push(error.message));
				await context.addInitScript(() => {
					Object.defineProperty(window, "localStorage", {
						get() {
							throw new DOMException("Storage blocked", "SecurityError");
						},
					});
				});
				await view.goto(`${base}#/new`);
				await view.locator("h1").waitFor();
				await themeIs(view, "dark");
				await choose(view, "Light");
				await themeIs(view, "light");
				check(
					await view
						.getByRole("status")
						.filter({ hasText: "browser storage is unavailable" })
						.isVisible(),
					"Unsaved appearance is silently lost",
				);
				await view.reload();
				await view.locator("h1").waitFor();
				await themeIs(view, "dark");
				check(
					errors.length === 0,
					`Storage restriction crashed preview: ${errors.join("; ")}`,
				);
			},
			{ colorScheme: "dark" },
		);
	});

	await test("Exact Fulcro roles, typography and original logo asset", async () => {
		await withBrowser(async (view) => {
			await view.goto(base);
			await view.locator("h1").waitFor();
			const names = [
				"canvas",
				"sidebar",
				"surface",
				"surface-2",
				"line",
				"ink",
				"muted",
				"accent",
				"on-accent",
				"selected",
				"focus",
			];
			const palettes = {
				Light: [
					"#F2F1E8",
					"#E9EBE2",
					"#FFFFFF",
					"#FAFAF5",
					"#D5DDD3",
					"#132B28",
					"#56675F",
					"#D5F268",
					"#132B28",
					"#DBE7CB",
					"#132B28",
				],
				Dark: [
					"#0D1917",
					"#101F1C",
					"#132B28",
					"#1B3530",
					"#34504A",
					"#F2F1E8",
					"#A6B9AE",
					"#D5F268",
					"#132B28",
					"#294139",
					"#D5F268",
				],
			};
			for (const [label, expected] of Object.entries(palettes)) {
				await choose(view, label);
				await themeIs(view, label.toLowerCase());
				const colors = await view.evaluate(
					(names) =>
						names.map((name) =>
							getComputedStyle(document.documentElement)
								.getPropertyValue(`--${name}`)
								.trim()
								.toUpperCase(),
						),
					names,
				);
				check(
					JSON.stringify(colors) === JSON.stringify(expected),
					`${label} tokens deviate from supplied specification: ${colors.join(", ")}`,
				);
				check(
					(await view.locator(".live-product[data-theme]").count()) === 0,
					"Landing inset still forces opposite theme",
				);
			}
			await view.evaluate(() => document.fonts.ready);
			const type = await view.evaluate(() => ({
				body: getComputedStyle(document.body).fontFamily,
				heading: getComputedStyle(document.querySelector("h1")).fontFamily,
				weight: getComputedStyle(document.querySelector("h1")).fontWeight,
				fonts: [...document.fonts]
					.filter((font) => font.status === "loaded")
					.map((font) => font.family),
			}));
			check(
				type.body.includes("Inter") &&
					type.heading.includes("DM Sans") &&
					type.weight === "500",
				"Typography does not follow DM Sans Medium / Inter",
			);
			check(
				type.fonts.some((font) => font.includes("Inter")) &&
					type.fonts.some((font) => font.includes("DM Sans")),
				"Local brand fonts did not load",
			);
			const hash = await view.evaluate(async () => {
				const source = await fetch("/brand/archimedes-fulcro.svg");
				const digest = await crypto.subtle.digest(
					"SHA-256",
					await source.arrayBuffer(),
				);
				return [...new Uint8Array(digest)]
					.map((byte) => byte.toString(16).padStart(2, "0"))
					.join("");
			});
			check(
				hash ===
					"8fb9d89427563551c13544c135c3402973768c11403f487f9daa22ffb797f9b4",
				"Supplied logo asset changed or missing",
			);
			check(
				(await view.locator('link[rel="icon"]').getAttribute("href")).includes(
					"fulcro",
				),
				"Fulcro favicon missing",
			);
			check(
				await view
					.getByRole("link", { name: "Archimedes home", exact: true })
					.first()
					.isVisible(),
				"Logo lacks accessible home link",
			);
		});
	});

	await test("Appearance keeps navigation compact at desktop and mobile sizes", async () => {
		await withBrowser(async (view) => {
			await view.goto(base);
			await view.locator("h1").waitFor();
			for (const width of [1440, 390, 320]) {
				await view.setViewportSize({ width, height: 844 });
				for (const label of ["Light", "Dark"]) {
					await choose(view, label);
					const switcher = await view
						.getByRole("combobox", { name: "Appearance", exact: true })
						.evaluate((element) => {
							const box = element.getBoundingClientRect();
							return {
								width: box.width,
								height: box.height,
								text: element.innerText.trim(),
								icons: element.querySelectorAll("svg").length,
							};
						});
					check(
						switcher.width === 44 &&
							switcher.height === 44 &&
							switcher.text === "" &&
							switcher.icons === 1,
						`${label} switcher at ${width}px is not the previous compact icon control: ${JSON.stringify(switcher)}`,
					);
				}
				const header = await view.locator(".site-header").boundingBox();
				check(
					header.height <= 104,
					`Appearance needlessly wraps navigation at ${width}px: ${header.height}px header`,
				);
			}
		});
	});

	await test("Essential research control boundaries retain 3:1 contrast", async () => {
		await withBrowser(async (view) => {
			await view.goto(`${base}#/new`);
			await view.locator("h1").waitFor();
			for (const label of ["Light", "Dark"]) {
				await choose(view, label);
				const violations = await view.evaluate(() => {
					const context = document
						.createElement("canvas")
						.getContext("2d", { willReadFrequently: true });
					const rgb = (text) => {
						context.clearRect(0, 0, 1, 1);
						context.fillStyle = text;
						context.fillRect(0, 0, 1, 1);
						return [...context.getImageData(0, 0, 1, 1).data];
					};
					const luminance = (c) =>
						c
							.slice(0, 3)
							.map((v) => v / 255)
							.map((v) =>
								v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4,
							)
							.reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
					const failures = [];
					for (const element of document.querySelectorAll(
						'#hypothesis, #risk-input, .select-trigger, .choicebox-item, [role="slider"], .button-primary',
					)) {
						const style = getComputedStyle(element);
						let parent = element.parentElement;
						while (
							parent.parentElement &&
							rgb(getComputedStyle(parent).backgroundColor)[3] === 0
						)
							parent = parent.parentElement;
						const a = luminance(rgb(style.borderTopColor)),
							b = luminance(rgb(getComputedStyle(parent).backgroundColor));
						const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
						if (
							parseFloat(style.borderTopWidth) < 1 ||
							style.borderTopStyle === "none" ||
							ratio < 3
						)
							failures.push({
								name: element.id || element.className,
								border: style.borderTop,
								ratio,
							});
					}
					return failures;
				});
				check(
					violations.length === 0,
					`${label} control boundaries fail: ${JSON.stringify(violations)}`,
				);
			}
		});
	});

	await test("Reading dialogs use the elevated Fulcro surface in both themes", async () => {
		await withBrowser(async (view) => {
			await view.goto(`${base}#/study/momentum/evidence`);
			await view.locator("h1").waitFor();
			await view.locator(".source-summary > button").first().click();
			for (const label of ["Light", "Dark"]) {
				await choose(view, label);
				await view
					.getByRole("button", { name: "Inspect source RN-014", exact: true })
					.click();
				const surface = await view
					.getByRole("dialog")
					.evaluate((element) => getComputedStyle(element).backgroundColor);
				check(
					surface ===
						(label === "Light" ? "rgb(255, 255, 255)" : "rgb(19, 43, 40)"),
					`${label} dialog uses canvas instead of raised surface: ${surface}`,
				);
				await view.keyboard.press("Escape");
				await view.getByRole("dialog").waitFor({ state: "hidden" });
			}
		});
	});

	await test("Pressed filters and session saves expose tonal selection", async () => {
		await withBrowser(async (view) => {
			await view.goto(`${base}#/library`);
			await view.locator("h1").waitFor();
			for (const label of ["Light", "Dark"]) {
				await choose(view, label);
				const expected =
					label === "Light" ? "rgb(219, 231, 203)" : "rgb(41, 65, 57)";
				for (const category of ["All research", "Trend following"]) {
					const button = view.getByRole("button", {
						name: category,
						exact: true,
					});
					await button.click();
					check(
						(await button.getAttribute("aria-pressed")) === "true",
						"Filter not pressed",
					);
					check(
						(await button.evaluate(
							(el) => getComputedStyle(el).backgroundColor,
						)) === expected,
						`${label} pressed filter loses its selected surface`,
					);
				}
				await view.evaluate(() => {
					location.hash = "/study/momentum";
				});
				const saved = view.getByRole("button", {
					name: "Saved for this session",
					exact: true,
				});
				await saved.waitFor();
				check(
					(await saved.evaluate(
						(el) => getComputedStyle(el).backgroundColor,
					)) === expected,
					`${label} saved study loses its selected surface`,
				);
				await view.evaluate(() => {
					location.hash = "/library";
				});
				await view.locator(".strategy-table").waitFor();
			}
		});
	});

	if (failures.length) throw new Error(JSON.stringify({ passed, failures }));
	return { passed, failures };
}
