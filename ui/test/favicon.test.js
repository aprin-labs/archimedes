import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// ── favicon coverage for major platforms (#443) ─────────────────────────────
// The SVG favicon alone covers modern desktop/mobile browsers but not the
// iOS home-screen (needs a real PNG apple-touch-icon; iOS ignores SVG) or
// Android/desktop PWA installs (needs a manifest + PNG icon set). This test
// pins both halves: the <head> actually references the assets, and the
// referenced files are real PNGs at the sizes the tags claim -- a wrong-size
// or missing file is exactly the kind of thing that renders fine in a text
// diff and broken on an actual device.

const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
// Sliced to <head> only: these tags are meaningless (or simply inert) outside
// it, so a variant that moved/duplicated them into <body> should fail this
// test, not pass it by matching anywhere in the document.
const head = html.match(/<head[^>]*>([\s\S]*?)<\/head>/)[1];

function pngDimensions(path) {
	const buf = readFileSync(new URL(path, import.meta.url));
	assert.deepEqual(
		[...buf.subarray(0, 8)],
		[0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a],
		`${path} is not a PNG file`,
	);
	// IHDR is always the first chunk: width/height are big-endian u32 at
	// offsets 16 and 20.
	return { width: buf.readUInt32BE(16), height: buf.readUInt32BE(20) };
}

test("index.html head references an apple-touch-icon, PNG fallbacks, and a manifest", () => {
	assert.match(head, /<link rel="icon" type="image\/svg\+xml" href="\/favicon\.svg" \/>/);
	assert.match(
		head,
		/<link rel="icon" type="image\/png" sizes="32x32" href="\/favicon-32x32\.png" \/>/,
	);
	assert.match(
		head,
		/<link rel="icon" type="image\/png" sizes="16x16" href="\/favicon-16x16\.png" \/>/,
	);
	assert.match(
		head,
		/<link rel="apple-touch-icon" sizes="180x180" href="\/apple-touch-icon\.png" \/>/,
	);
	assert.match(head, /<link rel="manifest" href="\/site\.webmanifest" \/>/);
	// Rebrand palette: the plain fallback meta and the manifest agree on the
	// brand dark (#15131D); media-scoped light/dark variants sit beside it.
	assert.match(head, /<meta name="theme-color" content="#15131D" \/>/);
});

test("apple-touch-icon.png is a real 180x180 PNG", () => {
	assert.deepEqual(pngDimensions("../public/apple-touch-icon.png"), {
		width: 180,
		height: 180,
	});
});

test("favicon PNG fallbacks match their declared sizes", () => {
	assert.deepEqual(pngDimensions("../public/favicon-32x32.png"), {
		width: 32,
		height: 32,
	});
	assert.deepEqual(pngDimensions("../public/favicon-16x16.png"), {
		width: 16,
		height: 16,
	});
});

test("site.webmanifest declares 192x192 and 512x512 icons that exist on disk at the right size", () => {
	const manifest = JSON.parse(
		readFileSync(new URL("../public/site.webmanifest", import.meta.url)),
	);
	assert.equal(manifest.name, "Archimedes");
	assert.ok(Array.isArray(manifest.icons) && manifest.icons.length >= 2);

	const sizesDeclared = manifest.icons.map((icon) => icon.sizes).sort();
	assert.deepEqual(sizesDeclared, ["192x192", "512x512"]);

	for (const icon of manifest.icons) {
		const [w, h] = icon.sizes.split("x").map(Number);
		const onDisk = pngDimensions(`../public${icon.src}`);
		assert.deepEqual(
			onDisk,
			{ width: w, height: h },
			`${icon.src} on-disk size does not match manifest-declared ${icon.sizes}`,
		);
	}
});
