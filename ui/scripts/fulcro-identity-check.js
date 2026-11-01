// From ui/: npm run preview -- --host 127.0.0.1 --port 5182
// From repository root:
// mkdir -p /tmp/archimedes-public-polish
// playwright-cli -s=fulcro-production open http://127.0.0.1:5182
// playwright-cli -s=fulcro-production run-code --filename=ui/scripts/fulcro-identity-check.js
async function _fulcroProductionCheck(page) {
	const base = page
		.url()
		.match(/^http:\/\/(?:127\.0\.0\.1|localhost):\d+(?=\/)/)?.[0];
	if (!base)
		throw new Error("Use a local production preview, not the live site");
	const out = "/tmp/archimedes-public-polish";
	const passed = [];
	const check = (condition, message) => {
		if (!condition) throw new Error(message);
	};
	const withBrowser = async (
		run,
		{
			stored,
			consent = false,
			system = "light",
			blocked = false,
			blockReact = false,
			signedIn = false,
		} = {},
	) => {
		const context = await page
			.context()
			.browser()
			.newContext({
				colorScheme: system,
				reducedMotion: "reduce",
				viewport: { width: 1440, height: 1000 },
			});
		const errors = [];
		try {
			await context.addInitScript(
				({ stored, consent, blocked }) => {
					// Seed once, not on reload: persistence tests must exercise real writes.
					if (!sessionStorage.getItem("fulcro-fixture-seeded")) {
						if (consent !== null)
							localStorage.setItem(
								"archimedes.cookieConsent",
								JSON.stringify({
									version: 1,
									functional: consent,
									analytics: false,
								}),
							);
						if (stored) localStorage.setItem("archimedes.theme", stored);
						sessionStorage.setItem("fulcro-fixture-seeded", "1");
					}
					if (blocked)
						Object.defineProperty(window, "localStorage", {
							get() {
								throw new Error("storage blocked for fixture");
							},
						});
				},
				{ stored, consent, blocked },
			);
			await context.route("**/*", (route) => {
				const request = route.request();
				if (!request.url().startsWith(`${base}/`)) return route.abort();
				const path = request.url().slice(base.length).split("?")[0];
				if (
					blockReact &&
					request.resourceType() === "script" &&
					path !== "/theme-init.js"
				)
					return route.abort();
				if (path === "/api/auth/get-session")
					return route.fulfill({
						json: signedIn
							? {
									user: { id: "ui-fixture", name: "UI fixture" },
									session: { id: "fixture" },
								}
							: null,
					});
				if (path === "/api/features")
					return route.fulfill({ json: { quant: false } });
				// Honest absence/error states; never substitute preview research data.
				if (path.startsWith("/api/") || path === "/health")
					return route.fulfill({
						status: 503,
						json: { detail: "Unavailable in identity fixture" },
					});
				return route.continue();
			});
			const view = await context.newPage();
			view.on("pageerror", (error) => errors.push(error.message));
			await run(view, context);
			check(errors.length === 0, `Uncaught errors: ${errors.join("; ")}`);
		} catch (error) {
			const failed = context.pages().at(-1);
			if (failed)
				await failed.screenshot({ path: `${out}/failure.png`, fullPage: true });
			throw new Error(`After ${passed.length} check groups: ${error.message}`, {
				cause: error,
			});
		} finally {
			await context.close();
		}
	};
	const themeIs = async (view, choice, resolved) => {
		try {
			await view.waitForFunction(
				({ choice, resolved }) =>
					document.documentElement.dataset.appearance === choice &&
					document.documentElement.dataset.theme === resolved,
				{ choice, resolved },
				{ timeout: 4000 },
			);
		} catch (error) {
			const actual = await view.evaluate(
				() =>
					`${document.documentElement.dataset.appearance}/${document.documentElement.dataset.theme}`,
			);
			throw new Error(
				`${view.url().slice(base.length).split("?")[0]}: expected ${choice}/${resolved}, got ${actual}`,
				{ cause: error },
			);
		}
		const state = await view.evaluate(() => ({
			scheme: getComputedStyle(document.documentElement).colorScheme,
			metadata: [...document.querySelectorAll('meta[name="theme-color"]')].map(
				(m) => m.content,
			),
			canvas: getComputedStyle(document.documentElement).backgroundColor,
		}));
		check(state.scheme === resolved, "Native color-scheme differs");
		check(
			state.metadata.length === 1 &&
				state.metadata[0] === (resolved === "dark" ? "#0d1917" : "#f2f1e8"),
			"Browser metadata differs",
		);
		check(
			state.canvas ===
				(resolved === "dark" ? "rgb(13, 25, 23)" : "rgb(242, 241, 232)"),
			"Root palette differs",
		);
	};
	const settle = (view) =>
		view.evaluate(async () => {
			await document.fonts.ready;
			await new Promise((resolve) =>
				requestAnimationFrame(() => requestAnimationFrame(resolve)),
			);
			await Promise.all(
				document
					.getAnimations()
					.filter(
						(animation) =>
							animation.effect?.getTiming().iterations !== Infinity,
					)
					.map((animation) => animation.finished.catch(() => {})),
			);
		});
	const trigger = (view) =>
		view.getByRole("button", { name: /^Switch to (light|dark) theme$/ });
	const choose = async (view, label) => {
		// Screenshot/fixture setup may need two clicks to make the current
		// System resolution an explicit override. Direct one-click tests follow.
		const name = `Switch to ${label.toLowerCase()} theme`;
		if ((await trigger(view).getAttribute("aria-label")) !== name)
			await trigger(view).click();
		await view.getByRole("button", { name, exact: true }).click();
	};
	const navigate = async (view, path) => {
		// Exercise the actual popstate routing backbone without a document reload.
		await view.evaluate((path) => {
			history.pushState({}, "", path);
			dispatchEvent(new PopStateEvent("popstate"));
		}, path);
		await trigger(view).waitFor();
	};

	for (const [stored, consent, system, expected, blocked] of [
		[null, null, "light", "light", false],
		[null, true, "dark", "dark", false],
		["invalid", true, "light", "light", false],
		["light", true, "dark", "light", false],
		["dark", true, "light", "dark", false],
		["system", true, "light", "light", false],
		["system", true, "dark", "dark", false],
		["light", false, "dark", "dark", false],
		["system", true, "light", "light", true],
	]) {
		await withBrowser(
			async (view) => {
				await view.goto(base);
				await themeIs(
					view,
					!blocked && consent && ["light", "dark", "system"].includes(stored)
						? stored
						: "system",
					expected,
				);
				check(
					await view
						.locator("#root")
						.evaluate((root) => root.childElementCount === 0),
					"React ran during first-paint check",
				);
			},
			{ stored, consent, system, blocked, blockReact: true },
		);
	}
	passed.push(
		"Nine System/override/blocked-storage first-paint cases with React blocked",
	);

	await withBrowser(
		async (view) => {
			await view.goto(`${base}/sign-in`);
			await themeIs(view, "system", "light");
			await view.emulateMedia({ colorScheme: "dark" });
			await themeIs(view, "system", "dark");
			check(
				await view.evaluate(
					() => localStorage.getItem("archimedes.theme") === null,
				),
				"System default wrote an override",
			);
			await trigger(view).click();
			await themeIs(view, "light", "light");
			check(
				(await view.getByRole("listbox").count()) === 0,
				"Theme click opened a menu",
			);
			await view.emulateMedia({ colorScheme: "light" });
			await view.emulateMedia({ colorScheme: "dark" });
			await themeIs(view, "light", "light");
			await view.reload();
			await themeIs(view, "light", "light");
		},
		{ consent: true },
	);
	passed.push(
		"System follows OS without persisting; one click selects the opposite explicit theme and stops following OS",
	);

	await withBrowser(
		async (view) => {
			await view.goto(`${base}/app/generate`);
			await themeIs(view, "system", "light");
			await view.getByRole("button", { name: "Skip", exact: true }).click();
			const draft = view.locator("textarea.generate-brief-input");
			await draft.fill(
				"Preserve this research brief while changing appearance.",
			);
			await trigger(view).click();
			await themeIs(view, "dark", "dark");
			check(
				(await draft.inputValue()) ===
					"Preserve this research brief while changing appearance.",
				"Theme switch changed draft",
			);
			check(
				await view.evaluate(
					() => localStorage.getItem("archimedes.theme") === null,
				),
				"Rejected preference was persisted",
			);
			await navigate(view, "/security");
			await themeIs(view, "dark", "dark");
			await view
				.getByRole("checkbox", { name: "Functional", exact: true })
				.check();
			await view
				.getByRole("button", { name: "Save choices", exact: true })
				.click();
			await view.waitForFunction(
				() => localStorage.getItem("archimedes.theme") === "dark",
			);
			await view
				.getByRole("checkbox", { name: "Functional", exact: true })
				.uncheck();
			await view
				.getByRole("button", { name: "Save choices", exact: true })
				.click();
			check(
				await view.evaluate(
					() => localStorage.getItem("archimedes.theme") === null,
				),
				"Revocation retained preference",
			);
			await navigate(view, "/app/generate");
			await themeIs(view, "dark", "dark");
			await view.reload();
			await themeIs(view, "system", "light");
		},
		{ signedIn: true },
	);
	passed.push(
		"Exact draft continuity, shell remount, consent grant/revocation; reload without an override returns to System",
	);

	await withBrowser(
		async (view) => {
			await view.goto(`${base}/sign-in`);
			await themeIs(view, "system", "light");
			await trigger(view).click();
			await themeIs(view, "dark", "dark");
			const feedback = await trigger(view).evaluate(
				(el) =>
					document.getElementById(el.getAttribute("aria-describedby"))
						?.textContent,
			);
			check(
				feedback?.includes("Not saved"),
				"Blocked storage feedback is not truthful",
			);
			await view.reload();
			await themeIs(view, "system", "light");
		},
		{ blocked: true },
	);
	passed.push(
		"Blocked localStorage still renders and toggles this visit; accessible feedback remains truthful",
	);

	await withBrowser(
		async (view) => {
			await view.goto(base);
			await themeIs(view, "system", "light");
			await trigger(view).focus();
			const box = await trigger(view).boundingBox();
			check(
				box.width >= 44 && box.height >= 44,
				"Appearance target below 44px",
			);
			await view.keyboard.press("Space");
			await themeIs(view, "dark", "dark");
			await view.locator(".appearance-trigger:focus").waitFor();
			await view.keyboard.press("Enter");
			await themeIs(view, "light", "light");
			await view.locator(".appearance-trigger:focus").waitFor();
			await view.reload();
			await themeIs(view, "light", "light");
			await trigger(view).click();
			await themeIs(view, "dark", "dark");
			await view.emulateMedia({ colorScheme: "light" });
			await themeIs(view, "dark", "dark");
			await view.reload();
			await themeIs(view, "dark", "dark");
		},
		{ consent: true },
	);
	passed.push(
		"Space/Enter toggle directly, retain focus and persist explicit Light/Dark choices",
	);

	await withBrowser(
		async (view, context) => {
			await view.goto(base);
			await choose(view, "Light");
			const peer = await context.newPage();
			await peer.goto(base);
			await trigger(peer).waitFor();
			await choose(peer, "Dark");
			await themeIs(view, "dark", "dark");
			await choose(peer, "Light");
			await themeIs(view, "light", "light");
			await peer.evaluate(() => localStorage.removeItem("archimedes.theme"));
			await view.waitForFunction(
				() =>
					document
						.querySelector(".appearance-trigger")
						?.title.includes("Not saved"),
				null,
				{ timeout: 4000 },
			);
			await themeIs(view, "light", "light");
			await view.reload();
			await themeIs(view, "system", "light");
		},
		{ consent: true },
	);
	passed.push(
		"Cross-tab preference sync; removal stops claiming persistence without resetting this visit",
	);

	await withBrowser(
		async (view) => {
			await view.goto(`${base}/app/generate`);
			await view.getByRole("button", { name: "Skip", exact: true }).click();
			await trigger(view).waitFor();
			check(
				(await view
					.getByRole("button", { name: "UI fixture", exact: true })
					.count()) === 1,
				"Authenticated shell not exercised",
			);
			await choose(view, "Dark");
			await themeIs(view, "dark", "dark");
			await view.keyboard.press("Tab");
			await trigger(view).focus();
			await settle(view);
			check(
				await trigger(view).evaluate(
					(el) =>
						el.matches(":focus-visible") &&
						getComputedStyle(el).outlineColor === "rgb(213, 242, 104)",
				),
				"App keyboard focus ring does not use Fulcro focus role",
			);
			// Reproducible hero asset: real editor, example input, no submitted run.
			await view
				.locator(".generate-brief-input")
				.fill(
					"Compare a weekly momentum strategy across broad-market ETFs with buy-and-hold. Include transaction costs and flag weak out-of-sample evidence.",
				);
			await view
				.getByRole("heading", { name: "Generate a strategy", exact: true })
				.click();
			await settle(view);
			await view.locator(".generate-brief-editor").screenshot({
				path: `${out}/product-workspace.png`,
				animations: "disabled",
			});
			await choose(view, "Light");
			await themeIs(view, "light", "light");
			await navigate(view, "/app/insights");
			await view
				.getByRole("heading", { name: "Page not found", exact: true })
				.waitFor();
			await themeIs(view, "light", "light");
		},
		{ signedIn: true, consent: true },
	);
	passed.push(
		"Authenticated shell and server-denied route retain appearance without leaking gated view",
	);

	await withBrowser(
		async (view) => {
			let footerMarkup;
			for (const [route, name] of [
				["/", "public"],
				["/security", "security"],
				["/architecture", "architecture"],
				["/app/explore", "explore"],
				["/sign-in", "auth"],
				["/missing", "not-found"],
			]) {
				for (const width of [1440, 320]) {
					await view.setViewportSize({ width, height: 1000 });
					await view.goto(`${base}${route}`);
					await trigger(view).waitFor();
					for (const choice of ["light", "dark"]) {
						await choose(view, choice);
						await themeIs(view, choice, choice);
						await settle(view);
						check(
							await view.evaluate(
								() => document.documentElement.scrollWidth <= innerWidth,
							),
							`${route} overflows at ${width}px`,
						);
						const ink = await view
							.locator("h1")
							.first()
							.evaluate((el) => getComputedStyle(el).fontFamily);
						check(
							ink.includes("DM Sans"),
							`${route} heading is not DM Sans: ${ink}`,
						);
						check(
							await view.evaluate(
								() =>
									document.fonts.check('500 16px "DM Sans"') &&
									document.fonts.check("400 16px Inter"),
							),
							"Local fonts did not load",
						);
						await view.screenshot({
							path: `${out}/${name}-${width}-${choice}.png`,
							fullPage: true,
						});
						if (["public", "security", "architecture"].includes(name)) {
							const footer = view.getByRole("contentinfo");
							check(
								(await footer.count()) === 1,
								`${route}: expected one footer landmark`,
							);
							check(
								(await view.locator("main footer").count()) === 0,
								`${route}: footer nested in main`,
							);
							const markup = await footer.innerHTML();
							footerMarkup ??= markup;
							check(
								markup === footerMarkup,
								`${route}: footer differs from landing`,
							);
							await footer.screenshot({
								path: `${out}/${name}-footer-${width}-${choice}.png`,
							});
						}
						if (name === "public" || name === "security") {
							const buttons = await view.evaluate((hasHero) => {
								const pick = (selector) => {
									const el = document.querySelector(selector);
									const style = getComputedStyle(el);
									const properties = [
										"background-color",
										"color",
										"border",
										"border-radius",
										"font-family",
										"font-size",
										"font-weight",
										"line-height",
										"letter-spacing",
										"padding",
										"height",
										"width",
										"text-transform",
										"box-shadow",
									];
									return {
										href: el.getAttribute("href"),
										text: el.textContent.trim(),
										children: el.childElementCount,
										...Object.fromEntries(
											properties.map((key) => [
												key,
												style.getPropertyValue(key),
											]),
										),
									};
								};
								return {
									nav: pick(".public-nav .public-auth-link"),
									hero: hasHero
										? pick('.public-hero a[href="/app/generate"]')
										: null,
									cta: pick('.public-final a[href="/app/generate"]'),
									section: getComputedStyle(
										document.querySelector(".public-final"),
									).backgroundColor,
								};
							}, name === "public");
							check(
								JSON.stringify(buttons.nav) === JSON.stringify(buttons.cta),
								`Closing CTA differs from navbar at ${width}px/${choice}`,
							);
							if (name === "public") {
								check(
									JSON.stringify(buttons.nav) === JSON.stringify(buttons.hero),
									`Hero CTA differs from navbar at ${width}px/${choice}`,
								);
								check(
									(await view
										.getByRole("link", { name: "See the product", exact: true })
										.count()) === 0,
									"Secondary hero action remains",
								);
								const architectureLink = await view
									.locator(".public-path__sequence-header a")
									.textContent();
								check(
									architectureLink?.trim() === "Read system architecture",
									"Architecture link still has an icon",
								);
								const image = view.locator(".public-product-frame img");
								await image.evaluate((el) => el.decode());
								check(
									await image.evaluate(
										(el) =>
											el.width > 0 &&
											el.naturalWidth === 725 &&
											el.naturalHeight === 289 &&
											Math.abs(el.clientWidth / el.clientHeight - 725 / 289) <
												0.03,
									),
									"Editor capture is stale, missing, or cropped",
								);
								await view.locator(".public-hero").screenshot({
									path: `${out}/hero-${width}-${choice}.png`,
								});
							}
							check(
								buttons.section !== buttons.cta["background-color"],
								"Closing CTA disappears into its section",
							);
							await view.locator(".public-final").screenshot({
								path: `${out}/${name}-cta-${width}-${choice}.png`,
							});
						}
						if (name === "architecture") {
							const cta = view
								.locator("main")
								.getByRole("button", {
									name: "Generate a strategy",
									exact: true,
								});
							check(
								(await cta.count()) === 1,
								"Architecture CTA still has an icon or is missing",
							);
							await cta
								.locator("..")
								.locator("..")
								.screenshot({
									path: `${out}/architecture-cta-${width}-${choice}.png`,
								});
						}
						if (name === "explore" && width === 320) {
							await view
								.getByRole("button", { name: "Toggle navigation", exact: true })
								.click();
							const logo = await view
								.locator(".sidebar-open .brand-mark")
								.boundingBox();
							check(
								logo.width > 140 &&
									Math.abs(logo.width / logo.height - 660 / 127) < 0.05,
								"Mobile navigation wordmark is cramped or distorted",
							);
							await settle(view);
							await view.screenshot({
								path: `${out}/sidebar-320-${choice}.png`,
							});
							await view
								.getByRole("button", { name: "Close menu", exact: true })
								.click();
						}
					}
				}
			}
			await view.evaluate(() => {
				document.documentElement.style.fontSize = "32px";
			});
			await trigger(view).focus();
			await view.keyboard.press("Enter");
			await themeIs(view, "light", "light");
			await settle(view);
			const bounds = await trigger(view).boundingBox();
			check(
				bounds.width >= 44 &&
					bounds.height >= 44 &&
					bounds.x >= 0 &&
					bounds.x + bounds.width <= 320,
				"Enlarged-text theme button leaves viewport or shrinks its target",
			);
			await view.screenshot({ path: `${out}/appearance-320-enlarged.png` });
		},
		{ consent: true },
	);
	passed.push(
		"Desktop/320px routes, shared footer on landing/security/architecture, CTA/navbar parity in both themes, local fonts, no overflow; enlarged-text toggle",
	);
	await withBrowser(
		async (view) => {
			await view.goto(base);
			await view
				.getByRole("link", { name: "Read system architecture", exact: true })
				.click();
			await view.waitForURL(`${base}/architecture`);
			for (const [route, role] of [
				["/architecture", "button"],
				["/security", "link"],
			]) {
				await view.goto(`${base}${route}`);
				await view
					.locator("main")
					.getByRole(role, { name: "Generate a strategy", exact: true })
					.click();
				await view.waitForURL(`${base}/app/generate`);
				if (route === "/architecture")
					await view.getByRole("button", { name: "Skip", exact: true }).click();
				await view
					.getByRole("heading", { name: "Generate a strategy", exact: true })
					.waitFor();
			}
		},
		{ signedIn: true, consent: true },
	);
	passed.push(
		"Text-only architecture link navigates; Architecture and Security CTAs open Generate",
	);
	return { passed, screenshots: out };
}
