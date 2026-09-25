// Guards for the double-charge fix (2026-08-30 external review: a successful
// $2 payment showed a receipt, quietly re-armed "Approve & Generate", and the
// reviewer — reading that as failure — paid again; the second click signed a
// fresh EIP-3009 nonce, which settles as a brand-new payment).
//
// Two independent halves, both source-asserted here:
//   1. Every /api/generate/start call carries an Idempotency-Key scoped to the
//      payment ATTEMPT — that header is what engages the backend credit
//      ledger's dedup (#1498); without it the ledger issues a fresh credit per
//      click by design (NULLs don't collide under the UNIQUE constraint).
//   2. An accepted job (202) takes the user INTO the running job's stream
//      (setDrillInJobId) instead of leaving the armed submit button beside a
//      one-line receipt.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const generate = readFileSync(
	new URL("../src/components/Generate.jsx", import.meta.url),
	"utf8",
);

test("every /start call carries the attempt-scoped Idempotency-Key", () => {
	// The header is merged inside submitStart itself, so no caller (bare
	// probe, signed retry, passkey branch) can forget it.
	assert.match(
		generate,
		/apiPostWithMeta\("\/api\/generate\/start", buildBrief\(\), \{\s*"Idempotency-Key": paymentAttemptKey\(\),\s*\.\.\.extraHeaders,\s*\}\)/,
	);
});

test("the attempt key is reused until a job is accepted, then regenerated", () => {
	// Created lazily once per attempt…
	assert.match(generate, /paymentAttemptKeyRef\.current = crypto\.randomUUID\(\)/);
	// …and consumed only on acceptance, inside the shared success helper.
	assert.match(
		generate,
		/const enterStartedJob = \(data\) => \{\s*paymentAttemptKeyRef\.current = null;\s*if \(data\?\.job_id\) setDrillInJobId\(data\.job_id\);/,
	);
});

test("every success path enters the started job's stream", () => {
	// finishStart (EOA path), the passkey branch, and the free/dry-run
	// startJob path must all end in enterStartedJob — count the call sites.
	const calls = generate.match(/enterStartedJob\(data\)/g) ?? [];
	assert.ok(
		calls.length >= 4,
		`expected >=4 enterStartedJob(data) call sites (EOA, passkey, free path, pay-panel refetch), found ${calls.length}`,
	);
});

test("a pay-panel refetch that starts the job still enters the stream", () => {
	// refreshPaymentRequirements's success path used to return null after
	// submitStart without enterStartedJob — the user stayed on the form
	// with the pay button re-armed, which reads as a failed start.
	const fn = generate.match(
		/const refreshPaymentRequirements = async \(\) => \{[\s\S]*?\n\t\};/,
	);
	assert.ok(fn, "refreshPaymentRequirements missing");
	assert.match(fn[0], /enterStartedJob\(data\)/);
	assert.match(fn[0], /const \{ data, receipt: settledReceipt \}/);
});
