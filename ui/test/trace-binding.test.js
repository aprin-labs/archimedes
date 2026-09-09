import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { blockOrderCopy, verificationTone } from "../src/trace-binding.js";

// ── blockOrderCopy (#1359) ──────────────────────────────────────────────
//
// The panel must key off `temporal_binding_source`, not off
// `temporal_binding_valid` alone — the backend's TraceResponse validator
// (schemas.py) coerces `temporal_binding_valid` to None whenever source
// isn't "chain", so a bare-boolean check can never distinguish "the
// contract's commit-reveal path actually ran" from "off-chain record
// only". These assertions pin the exact strings criterion A specifies for
// this brand-new pure helper (it has no pre-fix tree of its own to fail
// against); the component-level demonstration against the pre-fix
// Reasoning.jsx source is in the "Source-regex pins" section below.

test("chain source renders the contract-enforced heading, note, and tone", () => {
	const copy = blockOrderCopy({ source: "chain", valid: true });
	assert.equal(copy.heading, "Commit → trade → reveal (contract-enforced)");
	assert.equal(
		copy.note,
		"Vault.rebalance() reverts unless this commitment existed in an earlier block — enforced by ReasoningTraceRegistry.executeTrade(), not by our code being well-behaved.",
	);
	assert.equal(copy.tone, "verified");
});

test("non-chain source renders the off-chain heading, note, and tone", () => {
	const copy = blockOrderCopy({ source: "none", valid: false });
	assert.equal(copy.heading, "Block order (off-chain record)");
	assert.equal(
		copy.note,
		"Off-chain record only — this trace was anchored without the commit-reveal path, so the ordering is not contract-proven.",
	);
	assert.equal(copy.tone, "unproven");
});

test("source is the sole discriminant — valid does not flip the copy", () => {
	// A source==="chain" trace whose valid flag is momentarily false (e.g. a
	// block field hasn't landed in Redis yet) still gets the contract-backed
	// copy: the guarantee is the contract's, not a computed boolean's.
	const chainFalse = blockOrderCopy({ source: "chain", valid: false });
	const chainTrue = blockOrderCopy({ source: "chain", valid: true });
	assert.deepEqual(chainFalse, chainTrue);

	// A non-chain source never gets the contract-enforced copy, whatever
	// valid says.
	const noneTrue = blockOrderCopy({ source: "none", valid: true });
	const noneFalse = blockOrderCopy({ source: "none", valid: false });
	assert.deepEqual(noneTrue, noneFalse);
	assert.notEqual(noneTrue.tone, "verified");
});

// ── verificationTone (#1359) ────────────────────────────────────────────

test("hash_matched maps to the verified tone", () => {
	assert.equal(verificationTone("hash_matched"), "verified");
});

test("anchored_only maps to its own tone, distinct from verified", () => {
	assert.equal(verificationTone("anchored_only"), "anchored");
	assert.notEqual(
		verificationTone("anchored_only"),
		verificationTone("hash_matched"),
	);
});

test("failed, unrecognised, and missing modes all degrade to failed", () => {
	for (const mode of ["failed", undefined, null, "", "bogus_mode"]) {
		assert.equal(verificationTone(mode), "failed");
	}
});

// ── Source-regex pins on Reasoning.jsx ──────────────────────────────────
//
// Same shape as ui/test/a11y.test.js: readFileSync + regex/substring pins
// on the rendered source, confirming the claim-integrity fixes actually
// landed in the component (not just in the pure helper). Every assertion
// pinning Reasoning.jsx below was confirmed to FAIL against the pre-fix
// Reasoning.jsx tree (it always rendered the off-chain / roadmap copy
// regardless of source). The Architecture.jsx anti-goal check has its own
// header further down — it reads a different, untouched file, so that
// pre-fix-Reasoning.jsx-tree claim does not apply to it.

const reasoningSrc = readFileSync(
	new URL("../src/components/Reasoning.jsx", import.meta.url),
	"utf8",
);

test("the stale off-chain-forever claim is gone from Reasoning.jsx", () => {
	assert.ok(
		!reasoningSrc.includes("commit-reveal wiring is on the roadmap"),
		"the roadmap disclaimer must not still be quoted verbatim as live UI copy",
	);
	assert.ok(
		!reasoningSrc.includes("not yet wired into the"),
		"the stale not-yet-wired comment must be gone",
	);
});

test("Reasoning.jsx reads temporal_binding_source, not just temporal_binding_valid", () => {
	assert.ok(reasoningSrc.includes("temporal_binding_source"));
});

test("the verify button's details render unconditionally, not gated on failure", () => {
	// The old gate hid the one honest caveat (anchored_only's "zero hashes
	// compared") in exactly the case it existed to qualify — a Redis outage
	// or an anchored-only response painted the same silence as success.
	assert.ok(!reasoningSrc.includes("vResult && !vResult.is_verified"));
});

test("Reasoning.jsx names the anchored_only mode explicitly", () => {
	assert.ok(reasoningSrc.includes("anchored_only"));
});

test("Reasoning.jsx no longer claims the button recomputes the hash", () => {
	assert.ok(!reasoningSrc.includes("recompute"));
});

test("the prose names the button by its real label at least twice", () => {
	const count = (reasoningSrc.match(/Verify hash on-chain/g) || []).length;
	assert.ok(count >= 2, `expected >= 2 occurrences, found ${count}`);
});

test("the anchored tone's button branch never reuses the verified check icon or class", () => {
	// The button label ternary is: verifying? -> 'verified'? -> 'anchored'?
	// -> default. Skip past the 'Hash verified' (tone === 'verified') branch
	// first, so the 'anchored' match found is the label ternary's own branch
	// rather than the earlier `title={vTone === 'anchored' ? ... }` check —
	// then extract up to the next ('verify hash on-chain' / default)
	// branch's icon. Avoids a full-JSX parse while still pinning the actual
	// rendered branch, not just a substring anywhere in the file.
	const verifiedLabelIdx = reasoningSrc.indexOf("Hash verified");
	assert.ok(
		verifiedLabelIdx !== -1,
		"no verified-tone label branch found in Reasoning.jsx",
	);

	const anchoredIdx = reasoningSrc.indexOf(
		"vTone === 'anchored'",
		verifiedLabelIdx,
	);
	assert.ok(
		anchoredIdx !== -1,
		"no anchored-tone label branch found in Reasoning.jsx",
	);

	const branchEnd = reasoningSrc.indexOf("i-lucide-search", anchoredIdx);
	assert.ok(branchEnd !== -1, "could not find the end of the anchored branch");

	const anchoredBranch = reasoningSrc.slice(anchoredIdx, branchEnd);
	assert.ok(
		!anchoredBranch.includes("i-lucide-check"),
		"the anchored_only branch must not reuse the verified check icon",
	);
	assert.ok(
		!anchoredBranch.includes("positive"),
		"the anchored_only branch must not reuse the verified/positive styling class",
	);
});

test("the block-order affordance requires an actual valid reveal, not just a chain source", () => {
	// A source==="chain" trace can still have temporal_binding_valid===false
	// — a dangling commit whose reveal never landed (the #1275
	// honest-degradation contract; see agent_runner.py's
	// _reconcile_failure). blockOrderCopy's `tone` is deliberately keyed on
	// source alone (see the tests above), so the component must NOT derive
	// its green/red affordance from `copy.tone` by itself — it must also
	// check the real `temporal_binding_valid` flag. Confirmed to FAIL
	// against the tree this PR round shipped (`const isChainEnforced =
	// copy.tone === 'verified'`), which rendered a green check-circle for
	// this exact dangling-reveal state.
	assert.ok(
		reasoningSrc.includes("t.temporal_binding_valid === true"),
		"isChainEnforced must require temporal_binding_valid === true, not just copy.tone === 'verified'",
	);
});

test("a chain-sourced trace with an invalid reveal renders the red invalid-binding state, never the green check-circle", () => {
	const danglingIdx = reasoningSrc.indexOf(
		"temporal_binding_source === 'chain' && t.temporal_binding_valid === false",
	);
	assert.ok(
		danglingIdx !== -1,
		"no explicit dangling-reveal (source===chain, valid===false) detection found",
	);

	// Extract the block-order panel body (from the dangling-reveal check to
	// the panel's closing IIFE) and confirm the affordance branches use the
	// negative/x-circle treatment for this state, not the positive one.
	const panelEnd = reasoningSrc.indexOf("})()}", danglingIdx);
	const panel = reasoningSrc.slice(danglingIdx, panelEnd);

	assert.ok(
		panel.includes("i-lucide-x-circle") &&
			panel.includes("text-[var(--negative)]"),
		"the dangling-reveal case must render the red x-circle / negative styling",
	);
	assert.ok(
		!/isDanglingReveal\s*\?\s*'i-lucide-check-circle/.test(panel),
		"the dangling-reveal case must not map to the verified check-circle icon",
	);
});

// ── Anti-goal pins ───────────────────────────────────────────────────────
//
// Architecture.jsx is not touched by this PR — the contract is the source
// of truth for the block-order guarantee, and Architecture.jsx's own prose
// already states that correctly. This pin is not a "confirmed to fail
// against the pre-fix tree" claim like the Reasoning.jsx section above (a
// file this PR never modifies can't regress against "pre-fix"); it exists
// to catch a *future* PR accidentally weakening or removing that prose.

test("Architecture.jsx's contract-enforced claims are untouched (anti-goal)", () => {
	const architectureSrc = readFileSync(
		new URL("../src/components/Architecture.jsx", import.meta.url),
		"utf8",
	);
	assert.match(architectureSrc, /the ordering is enforced by the\s+contract/);
	assert.ok(architectureSrc.includes("contract-enforced ordering"));
});
