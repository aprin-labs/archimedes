// #1651 — no metric the Library, the Passport or the Leaderboard renders may
// leave its mathematical domain.
//
// The defect: `avellaneda_lee_2010_pca_statarb` persisted
// `max_drawdown = 1.303` and every surface rendered "−130.3%". A drawdown is a
// fraction of a peak — above 100% means the equity curve went NEGATIVE — so
// that figure is not a bad result, it is an impossible one, and it is the most
// falsifiable "your numbers are wrong" claim on the product.
//
// The engine-side computation is already fixed and pinned upstream
// (analytics-engine/tests/test_drawdown_invariant.py: `max_drawdown_pct < 100
// <=> the reported equity curve stayed positive`, held by a non-positive-price
// guard in `normalize_ohlcv`). This file guards the OTHER half, which that fix
// deliberately does not touch: the persisted 1.303 is still in the ledger and
// stays there (append-only), so the DISPLAY layer has to refuse to draw it as
// if it were possible — visibly, never silently.
//
// Source-text assertions follow ui/test/app-visuals.test.js and
// ui/test/rigor-tristate.test.js (no jsdom/vitest, per CLAUDE.md's ui/ testing
// convention); the property tests import the real module and run it.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
	ABSENT_LABEL,
	DEGENERATE_METRIC_TITLE,
	METRIC_DOMAINS,
	METRIC_STATES,
	NOT_MEASURED_METRIC_TITLE,
	PENDING_METRIC_TITLE,
	absenceReason,
	domainLabel,
	formatMetric,
} from "../src/metricDomain.js";
import { NOT_MEASURED } from "../src/generationCost.js";
import { UNKNOWN_RIGOR_LABEL, UNKNOWN_RIGOR_TITLE } from "../src/rigorGateStatus.js";
import { equityFromReturns, maxDrawdown } from "../src/utils/riskMath.js";

const read = (rel) => readFileSync(new URL(rel, import.meta.url), "utf8");
const SURFACES = {
	"Library (Strategies.jsx)": read("../src/components/Strategies.jsx"),
	"Passport (StrategyPassport.jsx)": read("../src/components/StrategyPassport.jsx"),
	"Leaderboard (Leaderboard.jsx)": read("../src/components/Leaderboard.jsx"),
	"Publish table (PublishStrategyTable.jsx)": read(
		"../src/components/PublishStrategyTable.jsx",
	),
};
const metricValueSrc = read("../src/components/MetricValue.jsx");

// ── Parsing what a cell actually shows ──────────────────────────────────────
//
// The tests below assert on the NUMBER A READER SEES, not on the input, which
// is the whole point: the input is allowed to be 1.303 forever.

const MINUS = "−"; // U+2212, the glyph every surface renders

/** "−130.3%" -> -130.3 ; "12.0%" -> 12 ; "—" -> null. */
function renderedPercent(label) {
	if (label === ABSENT_LABEL) return null;
	const m = label.match(/^([+−-]?)([\d,]+(?:\.\d+)?)%$/);
	assert.ok(m, `label ${JSON.stringify(label)} is not a percentage`);
	const magnitude = Number(m[2].replace(/,/g, ""));
	return m[1] === MINUS || m[1] === "-" ? -magnitude : magnitude;
}

/** A deterministic PRNG so the property runs are hermetic and reproducible. */
function rng(seed) {
	let s = seed >>> 0 || 1;
	return () => {
		// xorshift32
		s ^= s << 13;
		s >>>= 0;
		s ^= s >> 17;
		s ^= s << 5;
		s >>>= 0;
		return s / 0x100000000;
	};
}

// ═══════════════════════════════════════════════════════════════════════════
// 1. THE PROPERTY: for ANY return series, the rendered max drawdown is in
//    [−100%, 0%].
// ═══════════════════════════════════════════════════════════════════════════

/** Random simple-return series. `allowRuin` lets a bar lose more than
 *  everything, which is the only way an equity curve reaches or crosses zero
 *  and therefore the only way a drawdown above 100% can arise at all. */
function randomReturns(next, { allowRuin }) {
	const n = 2 + Math.floor(next() * 60);
	const floor = allowRuin ? -1.9 : -0.6;
	const out = [];
	for (let i = 0; i < n; i++) out.push(floor + next() * (0.5 - floor));
	return out;
}

test("property: for any return series, the rendered max drawdown lands in [−100%, 0%]", () => {
	let ruined = 0;
	let survived = 0;
	let clamped = 0;

	for (let seed = 1; seed <= 4000; seed++) {
		const next = rng(seed);
		const returns = randomReturns(next, { allowRuin: seed % 2 === 0 });
		// The UI's own equity/drawdown math (src/utils/riskMath.js), so this is
		// the number a chart on the page would derive from the same series.
		const curve = equityFromReturns(returns, 100_000);
		const dd = maxDrawdown(curve);

		const shown = renderedPercent(formatMetric("max_drawdown", dd).label);
		assert.ok(
			shown <= 0 && shown >= -100,
			`seed=${seed}: rendered ${shown}% from max_drawdown=${dd} — outside [−100%, 0%]`,
		);

		if (Math.min(...curve) <= 0) ruined++;
		else survived++;
		if (dd > 1) clamped++;
	}

	// Anti-vacuity: the sampler must actually reach the ruin regime, or the
	// property above is only ever checked on inputs that could never have
	// violated it. This is the guard the earlier version of this bug slipped
	// through — a test that never generates the offending shape proves nothing.
	assert.ok(ruined > 100, `only ${ruined} ruined curves sampled`);
	assert.ok(survived > 100, `only ${survived} surviving curves sampled`);
	assert.ok(
		clamped > 100,
		`only ${clamped} series produced a >100% drawdown — the clamp branch is barely exercised`,
	);
});

test("property: the same bound holds for ANY raw number the API could serve", () => {
	// The Library does not derive the drawdown from a series — it renders a
	// persisted float. So quantify over the wire value directly, including the
	// shapes a well-behaved backend should never emit.
	const adversarial = [
		0, 1, 1.303, 5, 1e6, Number.MAX_VALUE, -0.0001, -0.2, -1, -1e9,
		0.9999999, 1.0000001,
	];
	for (const v of adversarial) {
		const shown = renderedPercent(formatMetric("max_drawdown", v).label);
		assert.ok(
			shown <= 0 && shown >= -100,
			`max_drawdown=${v} rendered ${shown}%, outside [−100%, 0%]`,
		);
	}
	for (let seed = 1; seed <= 3000; seed++) {
		const next = rng(seed * 7919);
		const v = (next() - 0.5) * 20; // roughly [-10, 10]
		const shown = renderedPercent(formatMetric("max_drawdown", v).label);
		assert.ok(shown <= 0 && shown >= -100, `max_drawdown=${v} rendered ${shown}%`);
	}
});

test("the exact audited −130.3% series renders a possible drawdown, and says why", () => {
	// docs/audits/2026-07-09-curated-consolidation.md §2.4: peak 100,000 →
	// trough −30,300 is (100000 − −30300)/100000 = 130.3%.
	const ruinedCurve = [100_000, 80_000, 20_000, -30_300, -5_000];
	const dd = maxDrawdown(ruinedCurve);
	assert.ok(Math.abs(dd - 1.303) < 1e-9, `expected 1.303, got ${dd}`);

	const m = formatMetric("max_drawdown", dd, { row: { passes_rigor_gate: false } });
	assert.equal(m.label, "−100.0%");
	assert.equal(m.state, "clamped");
	assert.equal(m.impossible, true);
	// Not silent: the reader is told the recorded figure, in the cell.
	assert.match(m.note, /reported −130\.3% — outside the possible range/);
	assert.match(m.title, /outside the possible range/);
	assert.match(m.title, /the recorded value is unchanged/);
	// Anti-goal: nothing here rewrites history. The reported number survives on
	// the result object exactly as the ledger holds it.
	assert.equal(m.reported, dd);
});

test("a negative served drawdown is reported as out-of-domain, never Math.abs()'d into a plausible one", () => {
	// The Leaderboard and the Publish table used to render
	// `−fmtPct(Math.abs(v))`, which turns a contract-violating −0.20 into a
	// perfectly ordinary-looking "−20.0%" — a manufactured number, and the
	// harder half of this bug to notice.
	const m = formatMetric("max_drawdown", -0.2);
	assert.equal(m.state, "clamped");
	assert.equal(m.label, "0.0%");
	// The sign flip is the tell: a drawdown displayed with a "+" is visibly not
	// a loss, which is the whole point of not Math.abs()-ing it away.
	assert.match(m.note, /reported \+20\.0%/);
	assert.notEqual(m.label, "−20.0%");
});

// ── The real persisted row, not a hand-made one ─────────────────────────────

const FIXTURES = JSON.parse(
	read("../../backend/tests/fixtures/backtest_fixtures_snapshot.json"),
);

test("the strategy that produced the screenshot renders a possible drawdown, from its own persisted row", () => {
	// Not a synthetic value: this is the number the repo actually stores for
	// `avellaneda_lee_2010_pca_statarb` in
	// backend/tests/fixtures/backtest_fixtures_snapshot.json. The append-only
	// rule says it stays 1.3030656663525957; the display rule says the reader
	// must never see it drawn as a survivable loss.
	const row = FIXTURES.avellaneda_lee_2010_pca_statarb;
	assert.ok(row, "the audited strategy is missing from the fixture snapshot");
	assert.ok(
		row.max_drawdown > 1,
		"fixture no longer carries the >100% drawdown — re-point this test at whatever row does, do not delete it",
	);

	const m = formatMetric("max_drawdown", row.max_drawdown, { row });
	const shown = renderedPercent(m.label);
	assert.ok(shown <= 0 && shown >= -100, `rendered ${shown}%`);
	assert.equal(m.label, "−100.0%");
	assert.match(m.note, /reported −130\.3%/);
	// And the stored number is untouched by having been displayed.
	assert.equal(FIXTURES.avellaneda_lee_2010_pca_statarb.max_drawdown, row.max_drawdown);
});

test("every persisted fixture row renders every one of its metrics inside the domain", () => {
	let checked = 0;
	let outOfDomain = 0;
	for (const [name, row] of Object.entries(FIXTURES)) {
		if (!row || typeof row !== "object") continue;
		for (const key of Object.keys(METRIC_DOMAINS)) {
			if (!(key in row)) continue;
			const m = formatMetric(key, row[key], { row });
			checked++;
			if (m.state === "clamped") {
				outOfDomain++;
				assert.ok(m.note, `${name}.${key}: clamped with no visible annotation`);
			}
			if (m.label === ABSENT_LABEL) {
				assert.ok(m.title, `${name}.${key}: em-dash with no reason`);
				continue;
			}
			const domain = METRIC_DOMAINS[key];
			if (domain.format === "negpct") {
				const shown = renderedPercent(m.label);
				assert.ok(
					shown <= 0 && shown >= -100,
					`${name}.${key}: rendered ${shown}%`,
				);
			}
		}
	}
	assert.ok(checked > 200, `only ${checked} fixture metrics checked`);
	// Non-vacuity, and the reason this whole change exists: the shipped fixture
	// set really does contain an impossible value today.
	assert.equal(
		outOfDomain,
		1,
		`expected exactly the one known out-of-domain fixture value, found ${outOfDomain}`,
	);
});

// ═══════════════════════════════════════════════════════════════════════════
// 2. THE ENUMERATION: every metric these surfaces render is bounded here, and
//    nothing outside its domain can be rendered.
// ═══════════════════════════════════════════════════════════════════════════

/** Every `metric="…"` a surface hands to MetricValue. */
function renderedMetrics(src) {
	return [...src.matchAll(/metric="([a-z0-9_]+)"/g)].map((m) => m[1]);
}

test("every metric rendered on Library / Passport / Leaderboard has a declared domain", () => {
	let total = 0;
	for (const [surface, src] of Object.entries(SURFACES)) {
		const metrics = renderedMetrics(src);
		assert.ok(metrics.length > 0, `${surface} renders no metric through MetricValue`);
		for (const key of metrics) {
			total++;
			assert.ok(
				Object.hasOwn(METRIC_DOMAINS, key),
				`${surface} renders metric ${JSON.stringify(key)} with no entry in METRIC_DOMAINS`,
			);
		}
	}
	// Guard against a vacuous pass if the regex ever stops matching.
	assert.ok(total >= 25, `only ${total} metric render sites found`);
});

test("no declared metric can render outside its domain, for any input", () => {
	// The enumeration, exercised. For each metric, hammer the formatter with
	// values well outside its bounds and assert the rendered number is inside.
	const parseNumeric = (label, format) => {
		if (label === ABSENT_LABEL) return null;
		// `negpct` displays the LOSS, so the served value is the negation of what
		// the cell shows: "−100.0%" came from a stored 1.0.
		if (format === "negpct") return -renderedPercent(label) / 100;
		if (format === "pct" || format === "signedpct") return renderedPercent(label) / 100;
		return Number(label.replace(/,/g, ""));
	};

	for (const [key, domain] of Object.entries(METRIC_DOMAINS)) {
		const probes = [
			domain.min - 1,
			domain.min - 1e6,
			domain.max + 1,
			domain.max + 1e6,
			-1e12,
			1e12,
			0,
			Number.isFinite(domain.min) ? domain.min : -1e3,
			Number.isFinite(domain.max) ? domain.max : 1e3,
		].filter((v) => Number.isFinite(v));

		for (const v of probes) {
			const m = formatMetric(key, v);
			assert.ok(
				METRIC_STATES.includes(m.state),
				`${key}: unknown state ${m.state}`,
			);
			const shown = parseNumeric(m.label, domain.format);
			if (shown === null) continue;
			// Rounding at the display precision can push a value a hair past the
			// bound (0.99999 → "1.00"), so compare with the tolerance the format
			// itself introduces rather than pretending the text is exact.
			const tol = 5e-3;
			assert.ok(
				shown >= domain.min - tol && shown <= domain.max + tol,
				`${key}: input ${v} rendered "${m.label}" (=${shown}), outside ${domainLabel(key)}`,
			);
			// A displayed number that is not the reported one must be annotated.
			if (v < domain.min || v > domain.max) {
				assert.equal(m.state, "clamped", `${key}: input ${v} was not flagged`);
				assert.ok(m.note, `${key}: clamped input ${v} carries no visible note`);
			}
		}
	}
});

test("a non-finite or non-numeric value renders as unmeasured, never as the text 'NaN'", () => {
	// `Number(NaN).toFixed(2)` is the string "NaN"; the old per-component `fmt`
	// helpers on the Library and the Leaderboard did exactly that.
	for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, "1.303", {}, true]) {
		const m = formatMetric("sharpe_ratio", bad);
		assert.equal(m.state, "unreadable");
		assert.equal(m.label, ABSENT_LABEL);
		assert.ok(m.note, "an unreadable value must be annotated, not silently blank");
		assert.doesNotMatch(m.label, /NaN|Infinity/);
	}
});

// ═══════════════════════════════════════════════════════════════════════════
// 3. BLANK DASHES GET A REASON — in the vocabulary that already exists.
// ═══════════════════════════════════════════════════════════════════════════

test("the absence glyph is the one this codebase already uses", () => {
	assert.equal(ABSENT_LABEL, NOT_MEASURED);
	assert.equal(ABSENT_LABEL, UNKNOWN_RIGOR_LABEL);
});

test("an empty metric cell always carries a reason, never a bare em-dash", () => {
	const rows = [
		{ rigor_gate_status: "pending", passes_rigor_gate: false },
		{ is_backtest_placeholder: true, passes_rigor_gate: false },
		// Was `{ status: "pending_backtest", ... }`. That status was a client-side
		// invention retired with the verdict of record, so nothing emits it and
		// the row proved nothing. The real shape it stood in for: a strategy the
		// gate GRADED and failed, whose backtest computed no number for this
		// cell. Its dash must still carry a reason.
		{ rigor_gate_status: "fail", passes_rigor_gate: false },
		{ passes_rigor_gate: null },
		{ rigor_gate_status: "degenerate", passes_rigor_gate: false },
		{ rigor_gate_status: "pass", passes_rigor_gate: true },
		{ rigor_gate_status: "banana", passes_rigor_gate: true },
		{},
		null,
	];
	for (const row of rows) {
		const m = formatMetric("max_drawdown", null, { row });
		assert.equal(m.label, ABSENT_LABEL);
		assert.ok(
			m.title && m.title.length > 20,
			`row ${JSON.stringify(row)} produced a dash with no reason`,
		);
	}
});

test("the reason reuses the rigor gate's four-state vocabulary rather than inventing one", () => {
	assert.deepEqual(absenceReason({ rigor_gate_status: "pending", passes_rigor_gate: false }), {
		state: "pending",
		title: PENDING_METRIC_TITLE,
	});
	assert.deepEqual(absenceReason({ rigor_gate_status: "degenerate", passes_rigor_gate: false }), {
		state: "degenerate",
		title: DEGENERATE_METRIC_TITLE,
	});
	assert.deepEqual(absenceReason({ rigor_gate_status: "pass", passes_rigor_gate: true }), {
		state: "not_measured",
		title: NOT_MEASURED_METRIC_TITLE,
	});
	// A fifth state the API might grow is not silently mapped onto a known one —
	// it reuses rigorGateStatus.js's own unknown wording (#1358).
	assert.deepEqual(absenceReason({ rigor_gate_status: "banana", passes_rigor_gate: true }), {
		state: "unknown",
		title: UNKNOWN_RIGOR_TITLE,
	});

	// The three sentences must stay distinct: "degenerate" must never borrow
	// pending's "no backtest has run", because a degenerate row HAS returns.
	assert.doesNotMatch(DEGENERATE_METRIC_TITLE, /no backtest has run/);
	assert.match(PENDING_METRIC_TITLE, /no backtest has run/);
	assert.notEqual(PENDING_METRIC_TITLE, NOT_MEASURED_METRIC_TITLE);
});

test("a live-paper row's empty cell does not claim a backtest is pending", () => {
	// The forward board's rows carry neither `rigor_gate_status` nor
	// `passes_rigor_gate`. Reading a missing key as "no verdict yet" would put
	// "no backtest has run on this strategy yet" under a column whose numbers
	// are explicitly not backtests — the one board built to keep the two kinds
	// of evidence apart.
	const liveRow = {
		deployment_id: "d-1",
		name: "x",
		performance_basis: "live_paper",
		days_live: 12,
	};
	const reason = absenceReason(liveRow);
	assert.equal(reason.state, "not_measured");
	assert.doesNotMatch(reason.title, /backtest/i);
	// ...while a strategy row that explicitly carries a null verdict still reads
	// as pending, which is the case Strategies.jsx's coerceGenerated relies on.
	assert.equal(absenceReason({ passes_rigor_gate: null }).state, "pending");
});

test("the unevaluable states are ruled out before the row is described as measured-but-empty", () => {
	// Same branch order the rigor badge was fixed into in #1358: a degenerate
	// row that also carries passes_rigor_gate === null must read as degenerate,
	// not pending.
	assert.equal(
		absenceReason({ rigor_gate_status: "degenerate", passes_rigor_gate: null }).state,
		"degenerate",
	);
	assert.equal(
		absenceReason({ rigor_gate_status: "banana", passes_rigor_gate: null }).state,
		"unknown",
	);
});

// ═══════════════════════════════════════════════════════════════════════════
// 4. THE CLAMP IS STRUCTURALLY VISIBLE — the surfaces cannot drop it.
// ═══════════════════════════════════════════════════════════════════════════

test("MetricValue renders the annotation whenever formatMetric produces one", () => {
	assert.match(metricValueSrc, /\{m\.note && \(/);
	// Visible text, not just a tooltip: a title alone is invisible on touch.
	assert.match(metricValueSrc, /\{m\.note\}/);
	assert.match(metricValueSrc, /className="sr-only"/);
	assert.match(metricValueSrc, /aria-hidden="true"/);
	assert.match(metricValueSrc, /title=\{m\.title \|\| undefined\}/);
});

test("no surface formats a metric behind MetricValue's back", () => {
	// The old per-component `fmt`/`fmtPct` helpers are what made four
	// independent, domain-blind renderings of the same field possible — one of
	// them even Math.abs()'d the drawdown. Re-declaring either name in these
	// files is the regression this asserts against.
	for (const [surface, src] of Object.entries(SURFACES)) {
		assert.doesNotMatch(
			src,
			/function fmtPct\s*\(/,
			`${surface} re-declared a local percent formatter`,
		);
		// Direct `−${...}%` drawdown construction — the literal shape of the bug.
		assert.doesNotMatch(
			src,
			/−\$\{fmtPct/,
			`${surface} still builds a drawdown string by hand`,
		);
		assert.doesNotMatch(
			src,
			/Math\.abs\(\s*\w+\.max_drawdown/,
			`${surface} still Math.abs()es a drawdown into plausibility`,
		);
	}
	// Strategies.jsx and StrategyPassport.jsx keep no numeric formatter at all.
	assert.doesNotMatch(SURFACES["Library (Strategies.jsx)"], /function fmt\s*\(/);
	assert.doesNotMatch(SURFACES["Passport (StrategyPassport.jsx)"], /function fmt\s*\(/);
	// The one that survives (Leaderboard's StockBench sentence) must at least
	// refuse to print "NaN".
	assert.match(
		SURFACES["Leaderboard (Leaderboard.jsx)"],
		/function fmt\([\s\S]{0,200}Number\.isFinite/,
	);
});

test("every surface imports the shared renderer", () => {
	for (const [surface, src] of Object.entries(SURFACES)) {
		assert.match(
			src,
			/import MetricValue from ["']\.\/MetricValue["']/,
			`${surface} does not import MetricValue`,
		);
	}
});

// ═══════════════════════════════════════════════════════════════════════════
// 5. IN-DOMAIN VALUES ARE UNTOUCHED (anti-goal: no threshold loosening, no
//    cosmetic rewriting of honest numbers).
// ═══════════════════════════════════════════════════════════════════════════

test("a possible value renders exactly as before, with no annotation", () => {
	const cases = [
		["max_drawdown", 0.153, "−15.3%"],
		["max_drawdown", 0, "0.0%"],
		["max_drawdown", 1, "−100.0%"],
		["cagr", 0.1234, "12.3%"],
		// ASCII hyphen, deliberately: this is byte-for-byte what the previous
		// per-component `fmtPct` rendered for a losing CAGR. The clamp discipline
		// must not quietly restyle honest numbers.
		["cagr", -0.42, "-42.0%"],
		["sharpe_ratio", 1.4567, "1.46"],
		["sharpe_ratio", -0.5, "-0.50"],
		["pbo_score", 0.62, "0.62"],
		["win_rate", 0.5, "50.0%"],
		["correlation_to_spy", -1, "-1.00"],
		["conviction_score", 82.51, "82.5"],
		["total_trades", 1234, "1,234"],
		["cumulative_return", 0.0731, "+7.31%"],
		["cumulative_return", -0.0731, "−7.31%"],
	];
	for (const [key, value, expected] of cases) {
		const m = formatMetric(key, value);
		assert.equal(m.label, expected, `${key}(${value})`);
		assert.equal(m.state, "measured");
		assert.equal(m.note, null);
		assert.equal(m.title, null);
	}
});

test("a metric with NO declared domain still renders safely, and warns a developer", () => {
	// The enumeration test above is a source-text check, so it can only see
	// metrics rendered through the `metric="…"` prop. This is the runtime half:
	// an unlisted key must not crash the page and must not be silently trusted
	// either — the same dev-warn / prod-silent shape as
	// rigorGateStatus.warnUnknownRigorGateStatus.
	const warnings = [];
	const original = console.warn;
	console.warn = (...args) => warnings.push(args.join(" "));
	try {
		const ok = formatMetric("vibes_index", 3.14159, { surface: "test" });
		assert.equal(ok.state, "measured");
		assert.equal(ok.label, "3.14");
		const bad = formatMetric("vibes_index", Number.NaN, { surface: "test" });
		assert.equal(bad.state, "unreadable");
		assert.equal(bad.label, ABSENT_LABEL);
	} finally {
		console.warn = original;
	}
	assert.equal(warnings.length, 2);
	assert.match(warnings[0], /no domain declared for metric "vibes_index"/);
	assert.match(warnings[0], /no test bounds it/);
	assert.equal(domainLabel("vibes_index"), null);
});

test("the domain table states a reason for every bound it asserts", () => {
	for (const [key, domain] of Object.entries(METRIC_DOMAINS)) {
		assert.ok(domain.label, `${key} has no display label`);
		assert.ok(
			typeof domain.why === "string" && domain.why.length > 30,
			`${key} declares a bound with no justification — a threshold somebody picked`,
		);
		assert.ok(domain.min <= domain.max, `${key} has an inverted domain`);
	}
});
