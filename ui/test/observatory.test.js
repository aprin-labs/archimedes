import test from "node:test";
import assert from "node:assert/strict";
import * as model from "../concept/data.ts";
import {
	metrics,
	studies,
	makeStudy,
	validateMandate,
	advanceRun,
	validation,
} from "../concept/data.ts";

const mandate = {
	idea: "Invest in persistent trends while limiting exposure during volatile markets.",
	lens: "momentum",
	risk: 12,
	cost: 10,
};

test("concept metrics derive from the visible series, including drawdown", () => {
	const result = metrics([
		{ value: 100 },
		{ value: 120 },
		{ value: 90 },
		{ value: 110 },
	]);
	assert.ok(Math.abs(result.total - 0.1) < 1e-9);
	assert.ok(Math.abs(result.cagr - (1.1 ** 4 - 1)) < 1e-9);
	assert.equal(result.drawdown, -0.25);
	assert.ok(Number.isFinite(result.sharpe));
	assert.equal(metrics([{ value: 100 }]).cagr, 0);
});

test("drawdown chart preserves dates, recovers at a new peak, and agrees with metrics", () => {
	assert.equal(typeof model.drawdownSeries, "function");
	const points = [100, 120, 90, 110, 130].map((value, i) => ({
		date: `month-${i}`,
		value,
	}));
	const result = model.drawdownSeries(points);
	assert.deepEqual(
		result.map((point) => point.date),
		points.map((point) => point.date),
	);
	assert.deepEqual(
		result.map((point) => point.value),
		[0, 0, -0.25, 110 / 120 - 1, 0],
	);
	assert.equal(
		Math.min(...result.map((point) => point.value)),
		metrics(points).drawdown,
	);
	assert.deepEqual(model.drawdownSeries([]), []);
});

test("fictional studies share dated windows and normalized allocations", () => {
	assert.equal(studies.length, 4);
	for (const study of studies) {
		assert.equal(study.series.length, 61);
		assert.equal(study.series[0].date, "2020-12-31");
		assert.equal(study.series.at(-1).date, "2025-12-31");
		assert.equal(study.series[0].value, 100);
		assert.equal(
			study.allocation.reduce((sum, item) => sum + item.weight, 0),
			100,
		);
		assert.ok(study.demo);
	}
});

test("a mandate changes the synthetic risk and cost path, not its provenance", () => {
	const first = makeStudy(mandate, "session-1");
	assert.ok(first);
	assert.deepEqual(first, makeStudy(mandate, "session-1"));
	assert.equal(first.idea, mandate.idea);
	assert.equal(first.demo, true);
	assert.deepEqual(
		first.series,
		studies[0].series,
		"preset and matching mandate use the same cost/risk assumptions",
	);
	const safer = makeStudy({ ...mandate, risk: 8 }, "session-2");
	const costly = makeStudy({ ...mandate, cost: 20 }, "session-3");
	assert.ok(
		metrics(first.series).volatility > metrics(safer.series).volatility,
	);
	assert.ok(costly.series.at(-1).value < first.series.at(-1).value);
});

test("study cost scenarios retain each recorded raw path, including the regime preset", () => {
	assert.equal(typeof model.applyScenario, "function");
	for (const study of studies) {
		const raw = structuredClone(study.rawSeries);
		assert.deepEqual(
			model.applyScenario(raw, study.risk, study.cost),
			study.series,
		);
		const changed = model.applyScenario(raw, study.risk, 20);
		assert.equal(changed[0].value, 100);
		assert.deepEqual(
			changed.map((point) => point.date),
			raw.map((point) => point.date),
		);
		assert.ok(metrics(changed).cagr < metrics(study.series).cagr);
		assert.deepEqual(
			raw,
			study.rawSeries,
			"scenario must not mutate recorded observations",
		);
	}
});

test("displayed validation distinguishes the near-threshold result", () => {
	const check = validation(studies[0]).find(
		(item) => item.name === "Risk-adjusted return",
	);
	assert.equal(check.met, Number(check.value) > 0.6);
});

test("the mandate boundary rejects empty text, unknown lenses and invalid risk", () => {
	assert.equal(validateMandate(mandate), "");
	for (const invalid of [
		{ idea: " " },
		{ lens: "unknown" },
		{ risk: NaN },
		{ risk: 100 },
		{ cost: -1 },
	]) {
		assert.ok(validateMandate({ ...mandate, ...invalid }));
	}
});

test("simulation pauses on an error, retries once, and completes exactly once", () => {
	let run = { stage: 0, status: "running", interrupt: true };
	run = advanceRun(run, "tick");
	assert.equal(run.stage, 1);
	run = advanceRun(run, "tick");
	assert.equal(run.status, "error");
	assert.deepEqual(advanceRun(run, "tick"), run);
	run = advanceRun(run, "retry");
	assert.equal(run.status, "running");
	assert.equal(run.interrupt, false);
	for (let i = 0; i < 4; i++) run = advanceRun(run, "tick");
	assert.equal(run.stage, 4);
	assert.equal(run.status, "complete");
	assert.deepEqual(advanceRun(run, "tick"), run);
	assert.equal(
		advanceRun({ stage: 1, status: "running" }, "cancel").status,
		"cancelled",
	);
});
