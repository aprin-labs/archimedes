// Rendering helpers for the strategy passport's DSL block (#1646).
//
// `StrategyResponse.strategy_spec` is the validated machine-readable spec that
// actually runs the strategy — the same dict a fusion proposal emits. It was
// never rendered anywhere before this: the passport showed only the prose
// `methodology_summary`, so a reader had to take the writeup on faith. The
// backend serves it on the single-strategy detail route only, and only to a
// caller entitled to it (owner, or any reader of a curated house row).
//
// These live in a plain .js module rather than inside StrategyPassport.jsx so
// they can be unit-tested under bare `node --test` — a .jsx file cannot be
// imported without a build step, which is why so much of this suite is reduced
// to asserting on source text. See ui/test/passport-dsl.test.js.

// Pretty-printed specs are small in practice (a few hundred characters), but
// `strategy_spec` is an untyped JSON column: nothing at the DB or schema layer
// bounds it. Rendering an unbounded blob into a <pre> is how one bad row
// freezes the passport page, so the display is capped and SAYS it is capped.
// Silent truncation would be the worse failure — the reader would believe they
// were looking at the whole spec.
export const MAX_SPEC_CHARS = 20000;

/**
 * Pretty-print a strategy spec for display.
 *
 * Returns null for anything that is not a renderable object — null/undefined
 * (not served, or the caller is not entitled to it), a non-object, or a value
 * that cannot be serialized (a cycle is impossible over JSON-sourced data, but
 * this module must not throw into a render path). An empty object is also
 * null: `{}` is not a spec, and printing "{}" under a "the rules this strategy
 * runs on" heading would be a claim that its rules are empty.
 *
 * @param {unknown} spec
 * @returns {{ text: string, truncated: boolean, totalChars: number } | null}
 */
export function formatStrategySpec(spec) {
	if (spec == null || typeof spec !== "object" || Array.isArray(spec)) return null;
	if (Object.keys(spec).length === 0) return null;

	let full;
	try {
		full = JSON.stringify(spec, null, 2);
	} catch {
		return null;
	}
	if (typeof full !== "string" || full.length === 0) return null;

	const truncated = full.length > MAX_SPEC_CHARS;
	return {
		text: truncated ? full.slice(0, MAX_SPEC_CHARS) : full,
		truncated,
		totalChars: full.length,
	};
}

// One pass over pretty-printed JSON. Order matters: a key ("..." followed by a
// colon) must be tried before a plain string, or every key is coloured as a
// value.
const TOKEN_RE =
	/("(?:\\.|[^"\\])*")(\s*:)|("(?:\\.|[^"\\])*")|\b(true|false)\b|\b(null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

/**
 * Split pretty-printed JSON into typed tokens for syntax colouring.
 *
 * Returns plain data — `{ text, kind }` — rather than markup, so the caller
 * renders real React elements. That is the whole point: the obvious way to
 * colour JSON is to build an HTML string and hand it to
 * `dangerouslySetInnerHTML`, which would inject attacker-influenced content
 * (a spec's `asset_universe` strings originate from an LLM) straight into the
 * page. There is no HTML anywhere in this module.
 *
 * Concatenating every token's `text` reproduces the input exactly — pinned by
 * a test, because a lossy highlighter would silently drop part of a spec the
 * reader believes they are auditing.
 *
 * @param {string} text
 * @returns {{ text: string, kind: "key"|"string"|"number"|"boolean"|"null"|"punct" }[]}
 */
export function tokenizeJson(text) {
	if (typeof text !== "string" || text.length === 0) return [];

	const out = [];
	const push = (chunk, kind) => {
		if (chunk) out.push({ text: chunk, kind });
	};

	// A module-level regex with /g carries mutable lastIndex across calls;
	// reset it rather than relying on the previous run having exhausted it.
	TOKEN_RE.lastIndex = 0;
	let cursor = 0;
	let m = TOKEN_RE.exec(text);
	while (m !== null) {
		push(text.slice(cursor, m.index), "punct");
		const [whole, keyName, colon, str, bool, nul, num] = m;
		if (keyName !== undefined) {
			push(keyName, "key");
			push(colon, "punct");
		} else if (str !== undefined) push(str, "string");
		else if (bool !== undefined) push(bool, "boolean");
		else if (nul !== undefined) push(nul, "null");
		else if (num !== undefined) push(num, "number");
		cursor = m.index + whole.length;
		m = TOKEN_RE.exec(text);
	}
	push(text.slice(cursor), "punct");
	return out;
}
