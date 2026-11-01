import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("production carries unchanged supplied artwork and local licensed fonts", () => {
	const logo = new URL(
		"../public/brand/archimedes-fulcro.svg",
		import.meta.url,
	);
	assert.ok(existsSync(logo), "supplied logo must ship in production");
	assert.equal(
		createHash("sha256").update(readFileSync(logo)).digest("hex"),
		"8fb9d89427563551c13544c135c3402973768c11403f487f9daa22ffb797f9b4",
	);
	const paths = (svg) =>
		[...svg.matchAll(/<path[^>]*\sd="([^"]+)"/g)].map((match) => match[1]);
	const symbolPaths = paths(read("../public/brand/fulcro-symbol.svg"));
	assert.equal(symbolPaths.length, 2);
	for (const path of symbolPaths)
		assert.ok(paths(readFileSync(logo, "utf8")).includes(path));
	for (const name of [
		"dm-sans-latin.woff2",
		"inter-latin.woff2",
		"OFL-DM-Sans.txt",
		"OFL-Inter.txt",
	]) {
		assert.ok(
			existsSync(new URL(`../public/fonts/${name}`, import.meta.url)),
			name,
		);
	}
});

test("critical Fulcro colors and fonts load before first-paint script, without shell palette overrides", () => {
	const path = new URL("../public/theme.css", import.meta.url);
	assert.ok(existsSync(path), "critical tokens must ship outside React bundle");
	const tokens = readFileSync(path, "utf8");
	const css = read("../src/App.css");
	const html = read("../index.html");
	assert.ok(
		html.indexOf('href="/theme.css"') < html.indexOf('src="/theme-init.js"'),
	);
	assert.match(html, /<html[^>]*data-theme="dark"/);
	for (const [role, light, dark] of [
		["canvas", "#f2f1e8", "#0d1917"],
		["sidebar", "#e9ebe2", "#101f1c"],
		["surface", "#ffffff", "#132b28"],
		["surface-2", "#fafaf5", "#1b3530"],
		["surface-3", "#e2e8dc", "#234038"],
		["ink", "#132b28", "#f2f1e8"],
		["muted", "#56675f", "#a6b9ae"],
		["selected", "#dbe7cb", "#294139"],
		["focus", "#132b28", "#d5f268"],
		["line-strong", "#6d8176", "#7d9689"],
		["disabled", "#e2e7df", "#263c34"],
	]) {
		assert.match(tokens, new RegExp(`--${role}:\\s*${light};`, "i"));
		assert.match(tokens, new RegExp(`--${role}:\\s*${dark};`, "i"));
	}
	assert.match(tokens, /--primary:\s*#d5f268;/i);
	assert.match(tokens, /--accent:\s*var\(--link\);/);
	assert.match(tokens, /--on-primary:\s*#132b28;/i);
	assert.match(tokens, /font-family: "DM Sans"/);
	assert.match(tokens, /font-family: Inter/);
	assert.doesNotMatch(
		css,
		/Gabarito|gabarito|Geist|Fraunces|IBM Plex|Instrument Serif/,
	);
	assert.doesNotMatch(
		css,
		/(?:^|\n)(?::root[^{}]*|\.app-site|\.public-site)\s*\{[^}]*--(?:canvas|surface-\d|accent|text-\d|sans|app-canvas|public-theatre-bg):/,
	);
});

test("both shells and auth use one appearance owner, without rerouting or a demo import", () => {
	for (const component of ["PublicLayout", "Layout", "AuthPage"]) {
		const source = read(`../src/components/${component}.jsx`);
		assert.match(source, /<ThemeSwitcher\s*\/>/);
		assert.doesNotMatch(source, /getStoredTheme|applyTheme|concept\//);
	}
	const main = read("../src/main.jsx");
	assert.match(main, /<ThemeProvider>/);
	const switcher = read("../src/components/ThemeSwitcher.jsx");
	assert.match(switcher, /<button\s/);
	assert.match(switcher, /type="button"/);
	assert.match(switcher, /aria-label=/);
	assert.match(switcher, /aria-describedby=/);
	assert.match(switcher, /onClick=/);
	assert.doesNotMatch(switcher, /Select\.|radix-ui|localStorage|hashchange/);
});
