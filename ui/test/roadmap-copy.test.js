// Claim-integrity guard for the public/app surfaces that #1266 gated at the
// ROUTE level but left selling in COPY (#1354): with ROADMAP_SURFACES_ENABLED
// off (the default in every production build), the UI must not advertise the
// vault-deploy / marketplace journey it can no longer reach.
//
// Two guards, both hermetic (no network, no DB, no Redis, no `.env`, no
// import.meta.env mutation):
//
// 1. Prose guard — the seven surface files below must not contain any of the
//    phrase-shapes in OVERCLAIM_PATTERNS. This is a raw source-text scan
//    (readFileSync, no JSX parsing), so it does not distinguish "rendered
//    unconditionally" from "rendered inside a ROADMAP_SURFACES_ENABLED
//    branch" — the literal must not appear in these files AT ALL. Roadmap
//    copy that legitimately needs to exist for the flag-on build lives in
//    ui/src/roadmapCopy.js / roadmapCopyApp.js instead (deliberately not
//    scanned here) and is pulled in by reference. See those files' module
//    docstrings for why there are two of them, not one.
//
// 2. Proof-rail guard — Layout.jsx's "Core strategy journey" rail
//    (proofStages.js) renders 3 stages with the flag off and 5 with it on,
//    asserted through an explicit override argument rather than mutating
//    import.meta.env (mirrors routes.js's `featureEnabled(page, features)`).
//
// Each guard carries anti-vacuity coverage: every declared surface file must
// exist (a rename must fail loudly, not shrink the scan to nothing), and
// every pattern must reject its own canonical example (a pattern that stops
// matching anything is guarding nothing).
//
// Scope note, stated plainly (same discipline as
// backend/tests/test_corpus_claim_integrity.py): OVERCLAIM_PATTERNS is the
// floor named in issue #1354's acceptance criterion 1, not the ceiling. Other
// roadmap-flavored prose (e.g. Architecture.jsx's "Autonomous rebalance
// loop" ledger row, the flow-diagram.svg marketplace cluster) is gated in
// the same PR but isn't independently pattern-matched here; a human read is
// still worth doing on future roadmap-copy PRs.

import { existsSync, readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

import { getProofStages } from "../src/proofStages.js";

function repoFile(rel) {
	return new URL(`../${rel}`, import.meta.url);
}

function escapeRegExp(literal) {
	return literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

//: Renaming a file here without updating this list is caught by
//: test_every_declared_surface_exists — otherwise the scan would silently
//: shrink and the guard would pass vacuously.
const SURFACE_FILES = [
	"src/components/Landing.jsx",
	"src/components/Architecture.jsx",
	"src/components/Layout.jsx",
	"src/components/Insights.jsx",
	"src/components/Strategies.jsx",
	"src/components/OnboardingTour.jsx",
];

//: `(name, regex, canonical_example)` — the example is the shortest string
//: that must trip the pattern; test_every_pattern_rejects_its_canonical_example
//: runs it, so a pattern that stops matching anything fails loudly instead of
//: silently guarding nothing. The first eight mirror issue #1354's
//: acceptance criterion 1 dist-grep list verbatim; the next two are its
//: criterion 3 additions; the last (deploy_as_vault_cta) is a PR #1396
//: review fix — deploy_as_vault is case-sensitive on a lowercase "vault"
//: and does not reject FusionResult.jsx's actual literal,
//: roadmapCopyApp.js's `deployAsVaultLabel: "Deploy as Vault — coming in
//: Phase 4"` (capital V). Both patterns are kept: deploy_as_vault still
//: guards the lowercase phrasing used elsewhere.
const OVERCLAIM_PATTERNS = [
	["deploy_as_vault", new RegExp(escapeRegExp("Deploy as vault")), "Deploy as vault"],
	[
		"non_custodial_vault_on_arc",
		new RegExp(escapeRegExp("non-custodial vault on Arc")),
		"strategy runs live in a non-custodial vault on Arc.",
	],
	["deployed_vault", new RegExp(escapeRegExp("Deployed Vault")), "vault_deployed: 'Deployed Vault'"],
	[
		"pay_creators_not_the_house",
		new RegExp(escapeRegExp("Pay creators, not the house")),
		"Pay creators, not the house",
	],
	[
		"withdraw_assets_from_your_vault",
		new RegExp(escapeRegExp("Withdraw assets from your vault")),
		"Withdraw assets from your vault",
	],
	[
		"agent_activity_feed_on_portfolio",
		new RegExp(escapeRegExp("agent activity feed on Portfolio")),
		"show in the agent activity feed on Portfolio and Reasoning",
	],
	[
		"five_wallet_signatures",
		new RegExp(escapeRegExp("five wallet signatures")),
		"five wallet signatures, all yours",
	],
	[
		"non_custodial_vault_deploy",
		new RegExp(escapeRegExp("Non-custodial vault deploy")),
		"Non-custodial vault deploy + deposits",
	],
	[
		"kicker_rigor_arrow_vault",
		/rigor\s*<span>→<\/span>\s*vault/,
		"Research <span>→</span> rigor <span>→</span> vault",
	],
	["legend_is_user_vault", /is-user">Vault</, '<li className="is-user">Vault</li>'],
	[
		"deploy_as_vault_cta",
		/Deploy as [Vv]ault/,
		"Deploy as Vault",
	],
];

function findOverclaims(text) {
	const hits = [];
	for (const [name, pattern] of OVERCLAIM_PATTERNS) {
		if (pattern.test(text)) hits.push(name);
	}
	return hits;
}

test("every declared surface file exists", () => {
	const missing = SURFACE_FILES.filter((rel) => !existsSync(repoFile(rel)));
	assert.deepEqual(
		missing,
		[],
		`SURFACE_FILES names files that do not exist — a rename shrank the scan silently: ${missing}`,
	);
});

test("every overclaim pattern rejects its canonical example", () => {
	for (const [name, pattern, example] of OVERCLAIM_PATTERNS) {
		assert.match(
			example,
			pattern,
			`pattern ${name} no longer rejects its canonical example ${JSON.stringify(example)} — it is guarding nothing`,
		);
	}
});

for (const rel of SURFACE_FILES) {
	test(`${rel} carries no roadmap-copy overclaim (#1354)`, () => {
		const text = readFileSync(repoFile(rel), "utf8");
		const hits = findOverclaims(text);
		assert.deepEqual(
			hits,
			[],
			`${rel} contains gated roadmap copy literally: ${hits.join(", ")}. ` +
				"Move the phrase to ui/src/roadmapCopy.js (public pages) or " +
				"roadmapCopyApp.js (authenticated pages) and reference it from a " +
				"ROADMAP_SURFACES_ENABLED-gated branch instead of inlining it here.",
		);
	});
}

// ── Guard 3: the surfaces that must carry NO execution claim at all ──────
//
// Owner decision, 2026-08-30. There are zero live user deployments of the
// on-chain execution path, so the two surfaces a visitor reads as a promise
// — the landing page and the public security-posture page — must not mention
// it in ANY form, gated or not. /architecture keeps a single explicitly
// roadmap-framed mention and is therefore deliberately NOT in this list; it
// is still covered by the OVERCLAIM_PATTERNS scan above.
//
// This is a stricter guard than guard 1 on purpose. Guard 1 forbids specific
// marketing phrase-shapes and lets the words survive in incidental prose;
// this one forbids the vocabulary outright, because on these two pages any
// occurrence is either a claim or reads as one. It is a raw source scan, so
// it also applies to comments — the scrubbed files' own comments are written
// around it rather than exempted, which keeps the guard free of carve-outs
// that would later be used to smuggle copy back in.
//
// index.html is included because its meta description, OpenGraph/Twitter
// cards, and JSON-LD blob are the copy that actually gets syndicated — a
// claim retracted on the page but left in the share card is still shipped.
const EXECUTION_CLAIM_FREE_SURFACES = [
	"src/components/Landing.jsx",
	"src/components/Security.jsx",
	"index.html",
];

const EXECUTION_CLAIM_PATTERN = /vault|non-?custodial|custody/i;

//: Anti-vacuity for the pattern: the literal copy that these three files
//: carried BEFORE the scrub. Each must trip the pattern, so a future edit
//: that neuters the regex fails here instead of silently passing every file.
const PRE_SCRUB_EXAMPLES = [
	'question: "Can Archimedes withdraw from my vault?"',
	"<strong>You retain vault ownership.</strong>",
	"Propose allocations and rebalance within vault rules",
	"authority, and vault ownership. These controls describe current",
	"Vault share owner",
	"Custody decision record",
	"authorize a non-custodial vault only when you choose.",
	"selection-bias checks and non-custodial testnet vaults.",
];

test("every execution-claim-free surface exists", () => {
	const missing = EXECUTION_CLAIM_FREE_SURFACES.filter(
		(rel) => !existsSync(repoFile(rel)),
	);
	assert.deepEqual(
		missing,
		[],
		`EXECUTION_CLAIM_FREE_SURFACES names files that do not exist — a rename shrank the scan silently: ${missing}`,
	);
});

test("the execution-claim pattern rejects every pre-scrub example", () => {
	for (const example of PRE_SCRUB_EXAMPLES) {
		assert.match(
			example,
			EXECUTION_CLAIM_PATTERN,
			`EXECUTION_CLAIM_PATTERN no longer rejects ${JSON.stringify(example)} — ` +
				"it has been weakened and is guarding less than it claims",
		);
	}
});

for (const rel of EXECUTION_CLAIM_FREE_SURFACES) {
	test(`${rel} makes no on-chain execution claim (owner decision 2026-08-30)`, () => {
		const lines = readFileSync(repoFile(rel), "utf8").split("\n");
		const hits = lines
			.map((line, i) => [i + 1, line])
			.filter(([, line]) => EXECUTION_CLAIM_PATTERN.test(line))
			.map(([n, line]) => `${n}: ${line.trim()}`);
		assert.deepEqual(
			hits,
			[],
			`${rel} mentions the on-chain execution path, which has zero live user ` +
				`deployments:\n${hits.join("\n")}\n` +
				"This surface must describe only what runs today (research, the rigor " +
				"gate, paper trading, on-chain trace anchoring). The single permitted " +
				"roadmap mention lives on /architecture (Architecture.jsx).",
		);
	});
}

// ── Guard 4: the MACHINE surfaces may mention vaults only in roadmap tense ──
//
// #1650. Guards 1-3 cover the pages a human reads. The files below are the
// ones an AI agent reads and then quotes verbatim to its own user, and they
// were the last place the pre-#1469 sentence survived: `/.well-known/agent.json`
// described the product as a strategy "executed in a non-custodial USDC vault
// on the Arc testnet" and `/llms.txt` as "executed in non-custodial USDC
// vaults", present tense, while no user vault has ever been created and the
// journey is gated off every shipped surface.
//
// The rule is a TENSE rule, not a vocabulary ban, because the issue's
// anti-goal is explicit: the vault roadmap mention must SURVIVE ("future tense
// is honest and good marketing"). So guard 3's shape — forbid the words — is
// wrong here. Instead:
//
//   every sentence on these surfaces that mentions a vault, custody, or
//   "real capital" must also carry a roadmap marker (`roadmap` or `not
//   shipped`) in that SAME sentence.
//
// Sentence granularity, not line granularity, is the point. These files carry
// multi-sentence JSON `note` values; a line-level check would let "Creates a
// real, non-custodial vault on Arc." pass because some later sentence on the
// same line said "roadmap".
//
// One exemption, and it is shaped so a claim cannot fit inside it: a segment
// whose whole content is `"key": "<METHOD /path>"` or `"key": "<bare-token>"`
// — the route table (`"createVault": "POST /api/vaults/create"`) and the skill
// ids (`"id": "deploy-vault"`). Those name things; they assert nothing. Any
// prose at all — a capital letter, a space in the value, a second clause —
// falls outside it and must carry the marker.
// "the identifier exemption cannot swallow a claim" feeds it every sentence
// that was actually on these surfaces before this change and asserts none of
// them is exempted.
//
// Hermetic: readFileSync only. No network, no build, no env.
const MACHINE_SURFACE_FILES = [
	"public/llms.txt",
	"public/.well-known/agent.json",
	"public/.well-known/agent-registration.json",
	"public/.well-known/agent-registration.domain.json",
	"public/robots.txt",
	"public/site.webmanifest",
];

//: The vocabulary that makes a segment an on-chain EXECUTION claim. `vault` and
//: `custod` are #1650's own words; `real capital` is here because the fourth
//: sentence this change removed — agent.json's "deploy is also live and puts
//: real capital on-chain" — is the same defect wearing different words, and a
//: guard that caught only the ones already written down would have to be
//: rewritten for the next paraphrase.
const EXECUTION_MENTION = /vault|custod|real capital/i;

//: Deliberately two literals and no synonyms. "planned", "soon", "future",
//: "coming" are all words a marketing edit reaches for while still leaving the
//: reader believing the thing works; `roadmap` and `not shipped` are the two
//: CLAUDE.md itself uses ("is roadmap, not shipped product — write it in the
//: future tense"). `\b` matters: it makes the FLAG NAME
//: ROADMAP_SURFACES_ENABLED not count as a marker, since `_` is a word
//: character — naming the gate is not the same as saying the thing is gated.
const ROADMAP_MARKER = /\broadmap\b|\bnot shipped\b/i;

const IDENTIFIER_SEGMENT =
	/^\s*"[A-Za-z_]+"\s*:\s*"(?:(?:GET|POST|PUT|PATCH|DELETE) \/\S+|[a-z][a-z0-9-]*)",?\s*$/;

//: llms.txt is hard-wrapped markdown, so one sentence spans four lines; agent.json
//: is one JSON field per line, so one line holds four sentences. Splitting on
//: newlines alone gets llms.txt wrong (it cut "…is roadmap, not shipped: the
//: contracts are deployed … and no user vault has ever been created." in half and
//: reported the tail as unmarked); joining everything gets agent.json wrong (it
//: would glue an unmarked claim to some other field's "roadmap" and pass).
//:
//: So: rejoin a hard wrap first, then split on sentence ends. A line is a
//: continuation only when it opens lowercase / backtick / paren AND the line
//: above did not end a sentence — which is true of wrapped prose and false of
//: every JSON line (they all open with `"`, `{`, `}`, `[`, `]`) and of every new
//: markdown bullet or heading. That asymmetry is what lets one segmenter serve
//: both file shapes without a per-format branch.
const CONTINUATION_LINE = /^[a-z`(]/;

function reflow(text) {
	const out = [];
	for (const raw of text.split("\n")) {
		//: strip the llms.txt blockquote marker and any indent, so a wrapped
		//: `> ` line is compared on its prose, not on its punctuation.
		const line = raw.replace(/^\s*>\s?/, "").trim();
		const prev = out.length ? out[out.length - 1] : "";
		if (prev && line && CONTINUATION_LINE.test(line) && !/[.!?]$/.test(prev)) {
			out[out.length - 1] = `${prev} ${line}`;
		} else {
			out.push(line);
		}
	}
	return out;
}

function segments(text) {
	return reflow(text)
		.flatMap((line) => line.split(/(?<=[.!?])\s+/))
		.map((s) => s.trim())
		.filter(Boolean);
}

function unmarkedVaultClaims(text) {
	return segments(text).filter(
		(s) =>
			EXECUTION_MENTION.test(s) &&
			!ROADMAP_MARKER.test(s) &&
			!IDENTIFIER_SEGMENT.test(s),
	);
}

//: The exact sentences these surfaces carried BEFORE this change, verbatim.
//: Every one must be caught by the guard — that is what makes it a guard and
//: not a formality. In order: (1) agent.json `description`, which
//: agent-registration.json `description` carried byte for byte;
//: (2) `skills[deploy-vault].description`; (3) `endpoints.deploy.note`;
//: (4) `endpoints.paper.note`; (5) the llms.txt header; and
//: (6) `skills[monitor].description`.
const PRE_1650_MACHINE_CLAIMS = [
	"Turns a natural-language investment intent into a research-grounded, rigor-gated portfolio strategy, executed in a non-custodial USDC vault on the Arc testnet (chain ID 5042002).",
	"Execute a generated, rigor-passing strategy into a non-custodial USDC vault on Arc.",
	"Creates a real, non-custodial vault on Arc: the caller's linked wallet owns it and the agent holds rebalance authority only, never withdraw-to-platform.",
	"deploy is also live and puts real capital on-chain — choosing between them trades simulation for capital at risk, not working for not working.",
	"strategy backed by quantitative-finance literature and executed in non-custodial USDC",
	"Read a deployed vault's live health, allocations, and performance.",
];

//: The route/id segments the exemption exists for, verbatim from agent.json.
//: If a rename made these stop matching, the exemption would be dead weight and
//: the guard would fail on them for the wrong reason — loudly, but confusingly.
const EXEMPT_SEGMENT_EXAMPLES = [
	'"createVault": "POST /api/vaults/create"',
	'"vaultHealth": "GET /api/vaults/{address}/health"',
	'"id": "deploy-vault",',
];

test("every declared machine surface exists (#1650)", () => {
	const missing = MACHINE_SURFACE_FILES.filter((rel) => !existsSync(repoFile(rel)));
	assert.deepEqual(
		missing,
		[],
		`MACHINE_SURFACE_FILES names files that do not exist — a rename shrank the scan silently: ${missing}`,
	);
});

test("the machine-surface guard catches every pre-#1650 claim", () => {
	for (const claim of PRE_1650_MACHINE_CLAIMS) {
		assert.deepEqual(
			unmarkedVaultClaims(claim),
			[claim],
			`the guard no longer rejects ${JSON.stringify(claim)} — it has been weakened ` +
				"and would let the exact sentence #1650 was filed about back onto the agent surfaces",
		);
	}
});

test("the identifier exemption cannot swallow a claim (#1650)", () => {
	const swallowed = PRE_1650_MACHINE_CLAIMS.filter((c) => IDENTIFIER_SEGMENT.test(c));
	assert.deepEqual(
		swallowed,
		[],
		`IDENTIFIER_SEGMENT exempts real claim sentences: ${swallowed.join(" | ")}. ` +
			"It must only match a bare route or identifier value.",
	);
	//: and the flag name alone must not read as a roadmap marker
	assert.equal(
		ROADMAP_MARKER.test("gated behind ROADMAP_SURFACES_ENABLED in a vault"),
		false,
		"naming the ROADMAP_SURFACES_ENABLED flag must not count as saying the thing is roadmap",
	);
});

test("the identifier exemption still matches the segments it exists for", () => {
	const unmatched = EXEMPT_SEGMENT_EXAMPLES.filter((s) => !IDENTIFIER_SEGMENT.test(s));
	assert.deepEqual(
		unmatched,
		[],
		`these route/id segments are no longer exempt: ${unmatched.join(" | ")}`,
	);
});

for (const rel of MACHINE_SURFACE_FILES) {
	test(`${rel} mentions vaults only in roadmap tense (#1650)`, () => {
		const hits = unmarkedVaultClaims(readFileSync(repoFile(rel), "utf8"));
		assert.deepEqual(
			hits,
			[],
			`${rel} makes a vault claim with no roadmap marker in the same sentence:\n` +
				hits.map((h) => `  ${h}`).join("\n") +
				"\nAn AI agent reads this file and quotes it verbatim. No user vault has " +
				"ever been created and the journey is gated off every shipped surface, so " +
				"any vault sentence here must say 'roadmap' or 'not shipped' itself — " +
				"not rely on a caveat elsewhere in the file.",
		);
	});
}

//: Anti-vacuity for the scan, and the issue's anti-goal in test form. #1650
//: says do NOT delete the vault roadmap mention — future tense is honest and
//: is good marketing. A scrub that removed the word "vault" from these two
//: surfaces would satisfy every check above by making them vacuous, and would
//: also lose the roadmap. So the mention is required to be present, in tense.
test("agent.json and llms.txt still carry the vault roadmap mention, in roadmap tense (#1650)", () => {
	for (const rel of ["public/.well-known/agent.json", "public/llms.txt"]) {
		const marked = segments(readFileSync(repoFile(rel), "utf8")).filter(
			(s) => EXECUTION_MENTION.test(s) && ROADMAP_MARKER.test(s),
		);
		assert.ok(
			marked.length > 0,
			`${rel} no longer mentions the vault roadmap at all. #1650's anti-goal: do not ` +
				"delete it — state it in the future tense.",
		);
	}
});

//: The literal acceptance criterion from #1650, pinned by value on both
//: surfaces rather than left to the tense rule above. The tense rule would
//: also pass on a sentence that said 'roadmap' while still reading as shipped;
//: this pins the framing CLAUDE.md itself uses.
test("both agent surfaces state the vault journey as roadmap, not shipped (#1650)", () => {
	for (const rel of ["public/.well-known/agent.json", "public/llms.txt"]) {
		const text = readFileSync(repoFile(rel), "utf8");
		assert.match(
			text,
			/Executing strategies in non-custodial USDC vaults on Arc is roadmap, not shipped/,
			`${rel} lost the roadmap-tense framing of the headline claim`,
		);
		assert.doesNotMatch(
			text,
			/executed in (?:a )?non-custodial/i,
			`${rel} states present-tense vault execution again — the #1650 defect verbatim`,
		);
	}
});

test("proof rail is flag-derived: 3 stages off, 5 on (explicit override, no import.meta.env mutation)", () => {
	assert.deepEqual(
		getProofStages(false).map((s) => s.id),
		["brief", "debate", "gate"],
	);
	assert.deepEqual(
		getProofStages(true).map((s) => s.id),
		["brief", "debate", "gate", "vault", "monitor"],
	);
	// Anti-vacuity: the default argument really does read the build-time
	// flag (ROADMAP_SURFACES_ENABLED, off under plain node), not a
	// hardcoded value independent of it.
	assert.deepEqual(
		getProofStages().map((s) => s.id),
		["brief", "debate", "gate"],
	);
});

test("onboarding tour's paper card has no nav anchor — anon-bounce guard (#1354)", () => {
	// No nav item carries data-tour="paper" (the NAV array — extracted out of
	// Layout.jsx into src/navConfig.js by PR #1437 — has no 'paper'
	// entry), so an `anchor: 'paper'` here would make measure() always miss,
	// which falls through to OnboardingTour.jsx's "not mounted yet" effect
	// and calls setPage('paper') as a side effect. 'paper' is a page kind:
	// 'app' route outside ANON_APP_PAGES (routes.js), so for a signed-out
	// visitor App.jsx bounces straight to /sign-in — the exact anon-bounce
	// #1354's anti-goal forbids ("linking or navigating to [paper trading]
	// from an anon-reachable surface is not [fine]"). The manual help-icon
	// path that (re)opens the tour has no `user` gate (AuthenticatedApp.jsx
	// passes onOpenTour unconditionally), so an anonymous visitor really can
	// reach this card.
	const onboardingTour = readFileSync(
		repoFile("src/components/OnboardingTour.jsx"),
		"utf8",
	);
	const paperCard = onboardingTour.match(/id:\s*'paper',[\s\S]*?anchor:\s*(\S+),/);
	assert.ok(paperCard, "could not find the 'paper' tour card in OnboardingTour.jsx");
	assert.equal(
		paperCard[1],
		"null",
		"the 'paper' tour card must keep anchor: null (no reachable nav element to spotlight, " +
			"and a non-null anchor triggers an unconditional setPage() navigation side effect)",
	);
});
