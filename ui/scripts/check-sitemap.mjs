#!/usr/bin/env node
// ui/scripts/check-sitemap.mjs
//
// Fails (exit 1) if a route src/routes.js marks as genuinely public — either
// a fully public path (PUBLIC_PATHS) or an /app page any anonymous visitor
// may browse (ANON_APP_PAGES) — is missing a <loc> entry in
// public/sitemap.xml. Dynamic/parameterized pages (e.g. `strategy`, which
// has no static PAGE_PATHS entry) are naturally excluded: there is no single
// canonical URL for them, which sitemap.xml's own header comment already
// documents.
//
// Deliberately ONE-DIRECTIONAL (routes.js's public set must be a SUBSET of
// the sitemap, not proven equal to it). The reverse direction — "every
// sitemap <loc> is actually anonymous-accessible" — is still NOT enforced
// here.
//
// The concrete example this comment used to cite has since been fixed: it
// named /insights as listed-but-not-anonymous-accessible. PR #1437 (owner
// directive 2026-08-20, supersedes issue #1028 D8) resolved that the other
// way — /app/insights is ADMIN-ONLY, its sitemap entry was removed, and
// ui/test/sitemap.test.js now asserts the whole served file never names it.
// The general reverse-direction gap remains a documented gap: enforcing it
// here would need every <loc> reconciled against routes.js's gating tables,
// which is a bigger change than this script's scope.
//
// This script parses src/routes.js and public/sitemap.xml AS TEXT (regex,
// no bundler, no module import of routes.js) — same structural-check idiom
// as ui/test/*.test.js — so it never needs routes.js to export its private
// path tables, keeping this a pure read (no route-file changes).
//
// Usage: node scripts/check-sitemap.mjs

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const UI_ROOT = path.resolve(__dirname, "..");
const ROUTES_PATH = path.join(UI_ROOT, "src", "routes.js");
const SITEMAP_PATH = path.join(UI_ROOT, "public", "sitemap.xml");

const routesSrc = readFileSync(ROUTES_PATH, "utf8");
const sitemapSrc = readFileSync(SITEMAP_PATH, "utf8");

function extractBlock(src, constName) {
	const re = new RegExp(`const\\s+${constName}\\s*=\\s*\\{([\\s\\S]*?)\\n\\}`);
	const match = src.match(re);
	if (!match) {
		throw new Error(`check-sitemap: could not find "const ${constName} = { ... }" in routes.js`);
	}
	return match[1];
}

function parsePairs(block) {
	const pairs = [];
	for (const m of block.matchAll(/['"]([^'"]+)['"]\s*:\s*['"]([^'"]+)['"]/g)) {
		pairs.push([m[1], m[2]]);
	}
	return pairs;
}

function extractSet(src, constName) {
	const re = new RegExp(`const\\s+${constName}\\s*=\\s*new Set\\(\\[([^\\]]*)\\]\\)`);
	const match = src.match(re);
	if (!match) {
		throw new Error(`check-sitemap: could not find "const ${constName} = new Set([...])" in routes.js`);
	}
	return [...match[1].matchAll(/['"]([^'"]+)['"]/g)].map((m) => m[1]);
}

const publicPathPairs = parsePairs(extractBlock(routesSrc, "PUBLIC_PATHS")); // [path, page][]
const appPathPairs = parsePairs(extractBlock(routesSrc, "APP_PATHS")); // [path, page][]
const anonAppPages = extractSet(routesSrc, "ANON_APP_PAGES"); // page[]

// PAGE_PATHS = Object.fromEntries(APP_PATHS entries mapped to [page, path]) —
// mirror routes.js's own construction exactly: later entries win on a
// duplicate page (this is how '/app' and '/app/explore' both mapping to
// 'explore' resolve to the more specific '/app/explore').
const pagePaths = {};
for (const [appPath, page] of appPathPairs) pagePaths[page] = appPath;

const derivedPublicPaths = new Set();
for (const [pathKey] of publicPathPairs) derivedPublicPaths.add(pathKey);
for (const page of anonAppPages) {
	const appPath = pagePaths[page];
	if (!appPath || appPath === "/app") continue; // no static/canonical path for this page (e.g. dynamic-only 'strategy')
	derivedPublicPaths.add(appPath.replace(/^\/app/, ""));
}

const sitemapPaths = new Set(
	[...sitemapSrc.matchAll(/<loc>\s*https?:\/\/[^/]+([^<\s]*)\s*<\/loc>/g)].map(
		(m) => m[1] || "/",
	),
);

const missing = [...derivedPublicPaths].filter((p) => !sitemapPaths.has(p));

if (missing.length > 0) {
	console.error(
		"check-sitemap: route(s) routes.js marks public are missing from public/sitemap.xml:",
	);
	for (const p of missing) console.error(`  - ${p}`);
	console.error(
		"\nEither add the <loc> entry to public/sitemap.xml, or if the route " +
			"should no longer be public, remove it from PUBLIC_PATHS / " +
			"ANON_APP_PAGES in src/routes.js (a route/gating change — get review).",
	);
	process.exit(1);
}

console.log(
	`check-sitemap: OK (${derivedPublicPaths.size} public route(s) from routes.js all present in sitemap.xml)`,
);
