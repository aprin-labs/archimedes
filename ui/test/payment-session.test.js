import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// #1467 — the passkey payment rail. Circle's nanopayments facilitator
// verifies EOA ERC-3009 signatures ONLY (field-proven 2026-08-21: SCA
// ERC-1271 → invalid_signature; an on-chain-registered Gateway delegate,
// re-probed past the 5-minute validation view window → invalid_signature;
// the depositor's own EOA key → verifies). So the passkey kind pays through
// a DEVICE PAYMENT KEY: a local EOA the smart account funds with ONE
// batched approve+depositFor user-op, which then signs each authorization
// locally — zero prompts, no WebAuthn activation constraints. These pins
// hold that rail's load-bearing pieces.

const session = readFileSync(new URL("../src/payment-session.js", import.meta.url), "utf8");
const x402 = readFileSync(new URL("../src/x402.js", import.meta.url), "utf8");
const generate = readFileSync(new URL("../src/components/Generate.jsx", import.meta.url), "utf8");

test("the payment key is scoped per smart account and persists locally", () => {
	assert.match(session, /archimedes_payment_key:/);
	assert.match(session, /generatePrivateKey\(\)/);
	// Reuse before create — a fresh key on every visit would strand the
	// previous key's Gateway balance.
	assert.match(session, /const existing = getSessionAccount\(scaAddress\);\s*\n\s*if \(existing\) return existing;/);
});

test("the payment key links itself through the normal SIWE challenge/verify flow", () => {
	// Idempotent: checks the linked list first…
	assert.match(session, /listLinkedWallets\(\)/);
	// …then the same two endpoints every wallet link uses, as a headless
	// (programmatic) provider — no new backend surface.
	assert.match(session, /\/api\/wallets\/challenge/);
	assert.match(session, /\/api\/wallets\/verify/);
	assert.match(session, /provider: "headless"/);
	assert.match(session, /account\.signMessage\(\{ message: challenge\.message \}\)/);
});

test("passkey funding deposits INTO the payment key's balance (depositFor)", () => {
	// The SCA's own Gateway balance is unspendable on this rail — deposits
	// must land under the payment key's address or the money is stuck.
	const branch = x402.match(/if \(paymentWalletKind\(\) === "circle"\) \{([\s\S]*?)\n\t\}/);
	assert.ok(branch, "circle deposit branch missing");
	assert.match(branch[1], /getOrCreateSessionAccount\(getAddress\(\)\)/);
	assert.match(branch[1], /functionName: "depositFor"/);
	assert.match(branch[1], /args: \[token, payer\.address, amountRaw\]/);
});

test("the paid /start names the payment key in X-Wallet-Address", () => {
	// enforce_generation_payment binds authorization.from to the linked
	// wallet resolved from this header — sending the connected passkey
	// address instead fails every payment with payer_mismatch.
	assert.match(generate, /"X-Wallet-Address": session\.address/);
	// And the signature's payer is the payment key, not the passkey account.
	assert.match(generate, /payerAddress: session\.address/);
});

test("device payment key deposits are capped (FE-01: cleartext key, bounded blast radius)", () => {
	assert.match(session, /export const MAX_SESSION_KEY_DEPOSIT_RAW = 50_000_000n/);
	// Both handlePayAndGenerate call sites import and enforce the cap above
	// the existing lower bound, never in place of it.
	const guards = [...generate.matchAll(/if \(amountRaw < need\) \{[\s\S]*?\n\t*\}\n\t*if \(amountRaw > MAX_SESSION_KEY_DEPOSIT_RAW\) \{[\s\S]*?\n\t*\}/g)];
	assert.equal(guards.length, 2, "expected both call sites to enforce the deposit cap after the lower bound");
});

test("the balance shown is the PAYER's, and the panel says where the key lives", () => {
	assert.match(generate, /paymentPayerAddress\(\)/);
	assert.match(generate, /device payment key/);
	// The custody bound is stated to the user, not hidden.
	assert.match(generate, /only controls its own\s+deposited balance/);
	// The #1466 dead-rail guard is gone — this rail replaces it.
	assert.doesNotMatch(generate, /can't verify passkey signatures/);
});
