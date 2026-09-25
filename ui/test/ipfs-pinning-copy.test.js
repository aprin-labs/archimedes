import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const architecture = readFileSync(
	new URL("../src/components/Architecture.jsx", import.meta.url),
	"utf8",
);
const accountSettings = readFileSync(
	new URL("../src/components/AccountSettings.jsx", import.meta.url),
	"utf8",
);

// #1526: public copy must not claim IPFS pinning. The live reveal path is
// hash-only (empty storagePointer); a visitor who believed "IPFS-pointed"
// would be wrong. These needles were the live claims on main before the
// retraction — reverting the copy without this file going red is the defect.

test("Architecture.jsx does not claim the reveal is IPFS-pointed", () => {
	assert.equal(architecture.includes("IPFS-pointed"), false);
	assert.equal(architecture.includes("pinned to IPFS"), false);
	assert.ok(
		architecture.includes("the full trace is published off-chain"),
		"reveal step must still describe the hash-only publish, not go silent",
	);
});

test("AccountSettings.jsx does not claim traces are pinned to IPFS", () => {
	assert.equal(accountSettings.includes("pinned to IPFS"), false);
	assert.ok(
		accountSettings.includes("published to a blockchain stays there"),
		"deletion copy must still warn that chain writes are unreachable",
	);
});
