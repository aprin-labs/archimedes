import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import test from "node:test";

import {
	accountHasPassword,
	DELETE_CONFIRMATION_PHRASE,
	deleteConfirmationMatches,
	DELETION_DETACHED,
	DELETION_ERASED,
	DELETION_RETAINED,
} from "../src/account-deletion.js";

// ── #1367 (D4): the Delete-account control ──────────────────────────────
//
// Two separable things are guarded here.
//
// 1. The typed-confirmation guard, unit-tested directly, including the
//    inputs that MUST be refused.
// 2. The claim the page makes about what deletion does — pinned against the
//    schema that actually does it. Same cross-language source-mirror idiom
//    as password-rules.test.js, which reads auth/auth.js from a UI test so
//    the UI can never promise what the server rejects. Here the far side is
//    the SQLAlchemy models' `ondelete=` on their FK to auth_users.id, which
//    is what migration 85ca5310b7a1 enforces and what
//    backend/tests/test_account_deletion_cascade.py proves fires.

// ── The typed confirmation ──────────────────────────────────────────────

test("deleteConfirmationMatches accepts the exact phrase", () => {
	assert.equal(deleteConfirmationMatches(DELETE_CONFIRMATION_PHRASE), true);
});

test("deleteConfirmationMatches tolerates surrounding whitespace and casing, and nothing else", () => {
	assert.equal(deleteConfirmationMatches("  delete my account  "), true);
	assert.equal(deleteConfirmationMatches("Delete My Account"), true);
	assert.equal(deleteConfirmationMatches("delete my accounts"), false);
	assert.equal(deleteConfirmationMatches("delete  my account"), false); // internal spacing is not normalised
});

// THE GUARD, SHOWN TO REJECT (CLAUDE.md § "A guard must be shown to reject
// something"). Every one of these is an input a distracted or fat-fingered
// user could produce, and every one must leave the button inert.
test("deleteConfirmationMatches refuses empty, partial, adjacent, and non-string input", () => {
	for (const refused of ["", " ", "delete", "delete my", "my account", "yes", "DELETE", "confirm", "delete my account!", "\ndelete my account\t"]) {
		assert.equal(
			deleteConfirmationMatches(refused),
			refused.trim().toLowerCase() === DELETE_CONFIRMATION_PHRASE,
			`"${refused}" was accepted as the deletion confirmation`,
		);
	}
	assert.equal(deleteConfirmationMatches(""), false);
	assert.equal(deleteConfirmationMatches("delete"), false);
	assert.equal(deleteConfirmationMatches(undefined), false);
	assert.equal(deleteConfirmationMatches(null), false);
	assert.equal(deleteConfirmationMatches(42), false);
	assert.equal(deleteConfirmationMatches({ toString: () => DELETE_CONFIRMATION_PHRASE }), false);
});

// Mutation-prove: change the comparison in account-deletion.js to
// `input.trim().length > 0` and the test above fails on "delete", "yes" and
// "confirm". Confirmed by hand before commit — transcript in the PR body.
test("deleteConfirmationMatches actually compares against the phrase, not merely 'something was typed'", () => {
	assert.notEqual(deleteConfirmationMatches("anything at all"), deleteConfirmationMatches(DELETE_CONFIRMATION_PHRASE));
});

// ── Which /delete-user branch to take ───────────────────────────────────

test("accountHasPassword is true only when a credential row is present", () => {
	assert.equal(accountHasPassword([{ providerId: "credential" }]), true);
	assert.equal(accountHasPassword([{ providerId: "google" }, { providerId: "credential" }]), true);
	assert.equal(accountHasPassword([{ providerId: "google" }]), false);
	assert.equal(accountHasPassword([{ providerId: "google" }, { providerId: "github" }]), false);
	assert.equal(accountHasPassword([]), false);
	assert.equal(accountHasPassword(undefined), false);
	assert.equal(accountHasPassword(null), false);
});

// ── The schema-mirror guard ─────────────────────────────────────────────

const MODELS_DIR = new URL("../../backend/archimedes/models/", import.meta.url);

/**
 * Every `owner`-ish column in the Python models that carries a foreign key to
 * `auth_users.id`, mapped to its declared ON DELETE action.
 *
 * Parsed rather than hard-coded on purpose: a hard-coded copy is the thing
 * this test exists to make impossible.
 */
function ownershipActionsByTable() {
	const actions = new Map();
	for (const file of readdirSync(MODELS_DIR).filter((name) => name.endsWith(".py"))) {
		let table = null;
		for (const line of readFileSync(new URL(file, MODELS_DIR), "utf8").split("\n")) {
			const tableName = line.match(/__tablename__\s*=\s*"([^"]+)"/);
			if (tableName) table = tableName[1];
			const fk = line.match(/ForeignKey\("auth_users\.id",\s*ondelete="([^"]+)"\)/);
			if (fk && table) actions.set(table, fk[1]);
		}
	}
	return actions;
}

test("the parser actually finds the ownership FKs (a silently empty map would make every assertion below vacuous)", () => {
	const actions = ownershipActionsByTable();
	assert.ok(actions.size >= 10, `expected the models to declare at least 10 FKs to auth_users.id, found ${actions.size}`);
	assert.equal(actions.get("user_profiles"), "CASCADE");
	assert.equal(actions.get("strategy_store"), "SET NULL");
});

test("every table the UI says is ERASED really cascades off auth_users in the models", () => {
	const actions = ownershipActionsByTable();
	for (const { table } of DELETION_ERASED) {
		assert.equal(
			actions.get(table),
			"CASCADE",
			`AccountSettings promises ${table} is erased, but the model declares ondelete=${actions.get(table) ?? "(no FK to auth_users at all)"}`,
		);
	}
});

test("every table the UI says is DETACHED really SET NULLs", () => {
	const actions = ownershipActionsByTable();
	for (const { table } of DELETION_DETACHED) {
		assert.equal(
			actions.get(table),
			"SET NULL",
			`AccountSettings promises ${table} is only detached, but the model declares ondelete=${actions.get(table) ?? "(no FK to auth_users at all)"}`,
		);
	}
});

test("every table the UI says is RETAINED really has no FK to auth_users, so nothing removes it", () => {
	const actions = ownershipActionsByTable();
	for (const { table } of DELETION_RETAINED) {
		assert.equal(
			actions.has(table),
			false,
			`AccountSettings tells the user ${table} is NOT removed, but the model now declares ondelete=${actions.get(table)} — the copy is stale and the user is being told the wrong thing`,
		);
	}
});

// The other direction: a table that gains an ownership FK must be classified,
// not silently omitted. Without this, adding a CASCADE to a new PII table
// would leave the deletion statement quietly incomplete rather than wrong —
// harder to notice and exactly as dishonest.
test("no table declares an ownership FK the deletion copy does not account for", () => {
	const classified = new Set([...DELETION_ERASED, ...DELETION_DETACHED].map((row) => row.table));
	const unclassified = [...ownershipActionsByTable().keys()].filter((table) => !classified.has(table));
	assert.deepEqual(
		unclassified,
		[],
		`these tables are deleted or detached with the account but are not described in account-deletion.js: ${unclassified.join(", ")}`,
	);
});

test("the three lists are disjoint — no table can be erased and detached at once", () => {
	const all = [...DELETION_ERASED, ...DELETION_DETACHED, ...DELETION_RETAINED].map((row) => row.table);
	assert.equal(new Set(all).size, all.length, `a table appears in more than one deletion bucket: ${all.join(", ")}`);
});

test("every deletion row carries a plain-language sentence, not a bare table name", () => {
	for (const row of [...DELETION_ERASED, ...DELETION_DETACHED, ...DELETION_RETAINED]) {
		assert.equal(typeof row.label, "string");
		assert.ok(row.label.length > 10, `${row.table} has no plain-language description`);
		assert.doesNotMatch(row.label, /_/, `${row.table}'s description leaks a database identifier: ${row.label}`);
	}
});
