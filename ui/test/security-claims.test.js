// Claim-alignment guard for the public Security page (owner directive
// 2026-08-31: "our Security page on the UI must be accurate and in good
// alignment with the reality of the code").
//
// The Security page is the one public surface whose entire content is claims
// about enforcement. `docs/claims-ledger.md` records which of those claims are
// true and cites the file that makes each one true, and
// `backend/tests/test_claims_ledger.py` fails when a citation stops resolving.
// That guard protects the LEDGER. Nothing protected the PAGE: the ledger row
// could stay perfectly resolvable while `Security.jsx` drifted away from it,
// or while the enforcing code changed underneath a sentence that still reads
// as verified. This file closes that gap from the other end.
//
// Three guard families, in increasing order of what they catch:
//
// 1. LIVE_CLAIMS — the corrected sentence must be on the page. Deleting a
//    claim silently is as much a drift as adding a false one, because the
//    ledger row for it keeps asserting a surface that no longer says it.
//
// 2. RETRACTED_CLAIMS — the pre-2026-08-31 wording must be gone, and each
//    pattern must still reject its own literal (anti-vacuity). Without the
//    second half a weakened regex would pass while guarding nothing, which is
//    the failure mode `roadmap-copy.test.js` documents at length.
//
// 3. CLAIM_BINDINGS — the drift-hardener, and the reason this file exists
//    rather than a few more asserts in `public-visuals.test.js`. Each sentence
//    is bound to the literal in the enforcing file that makes it true. Change
//    Better Auth's cookie flags, delete an nginx header, widen the anonymous
//    page set, or rename the paywall's payer-binding rejection, and the PAGE's
//    claim goes red — in the same PR that made it false, not months later in a
//    manual audit. A guard on the claim alone can only see the copy; this sees
//    both ends of the sentence.
//
// Deliberately NOT asserted here: that a claim's cited file *implements* the
// claim. A literal is a tripwire on the code that was read, not a proof of
// behaviour — the reading is in the ledger row, and the behaviour is in that
// module's own tests. Saying so plainly is the point; a guard that oversells
// its reach is the defect this page is about.
//
// Scope note: `Security.jsx` is one of the three surfaces
// `roadmap-copy.test.js` scans for the retracted execution-claim vocabulary,
// comments included and with no carve-outs. That is why the page's payment
// wording describes the recipient mechanically ("a platform-operated wallet
// Archimedes signs for through its payment provider") rather than reaching for
// the shorter word. This test file is not in that scan.
//
// Hermetic: reads committed files off disk. No network, no DOM, no `.env`.

import { existsSync, readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

function repoFile(rel) {
	return new URL(`../../${rel}`, import.meta.url);
}

const SECURITY_PAGE = "ui/src/components/Security.jsx";
const security = readFileSync(repoFile(SECURITY_PAGE), "utf8");

//: Whitespace on the page is prettier's, and it re-wraps on edit. Every
//: pattern below is built from prose with runs of whitespace relaxed to \s+,
//: so a re-wrap never fails a guard that has nothing to say about layout.
function phrase(literal) {
	const escaped = literal.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	return new RegExp(escaped.replace(/\s+/g, "\\s+"));
}

//: `[claim, evidence]` — the sentence the page must carry, and the ledger row
//: it belongs to. The evidence string is for the failure message: a reader who
//: trips this guard needs to know what to go read, not just what broke.
const LIVE_CLAIMS = [
	[
		"Production session cookies are HttpOnly, Secure, and SameSite=Lax",
		"auth/auth.js advanced.defaultCookieAttributes",
	],
	[
		"Two browse pages are deliberately anonymous",
		"nginx.conf carve-outs + ANON_APP_PAGES in ui/src/routes.js",
	],
	[
		"Generation is a paid call, bound to the wallet you proved.",
		"services/generation_payment.py enforce_generation_payment",
	],
	[
		"published anonymously at GET /api/generate/quote",
		"generate_routes.py generation_quote -> generation_payment.quote()",
	],
	[
		"a mismatch is refused before any settlement round-trip",
		"generation_payment.py payer_mismatch",
	],
	[
		"An operator kill switch refuses service rather than serving the paid product unpaid.",
		"generation_payment.py payments_halted -> 503",
	],
	[
		"a script policy admitting same-origin bundles plus one hashed inline bootstrap",
		"nginx.conf $csp_policy map",
	],
	[
		"Two per-IP request-rate zones run at the edge",
		"nginx.conf limit_req_zone api_read / api_write",
	],
	[
		"tighter per-route limits on expensive endpoints behind them",
		"generate_routes.py @limiter.limit",
	],
	[
		"The agent&apos;s rebalance decisions are content-hashed and anchored on Arc.",
		"chain/agent_runner.py _commit_trace / _reveal_trace",
	],
	[
		"will only be anchored in a later version",
		"generation_pipeline.py 'mirrored on-chain in v1.5'",
	],
	[
		"a decision that produced no transaction has no anchor to check",
		"README.md 'Not every reasoning trace is anchored on-chain'",
	],
	[
		"the generation paywall is on and not in dry-run",
		"infra/ecs.tf GENERATION_PAYMENT_REQUIRED / GENERATION_PAYMENTS_DRY_RUN",
	],
	[
		"It is a fee, not a balance held for you, and there is nothing there to withdraw.",
		"infra/ecs.tf GENERATION_PAYMENT_RECIPIENT + REVENUE_WALLET_ID",
	],
	["Testnet USDC only", "hero status list — replaces 'No real funds'"],
];

//: `[name, pattern, canonicalExample]` — the wording that was on the page
//: before 2026-08-31 and must not come back. The example is the literal that
//: was actually there; `test_every_retraction_rejects_its_example` runs it, so
//: a pattern that stops matching fails loudly instead of guarding nothing.
const RETRACTED_CLAIMS = [
	[
		"no_real_funds",
		phrase("<strong>No real funds</strong>"),
		"<strong>No real funds</strong>",
	],
	[
		"separate_read_write_rate_limits",
		phrase("separate read/write rate limits"),
		"limited browser permissions, and separate read/write rate limits reduce",
	],
	[
		"hash_restricted_script_policy",
		phrase("a hash-restricted script policy"),
		"Same-origin rules, a hash-restricted script policy, HSTS,",
	],
	[
		"unscoped_reasoning_records_anchored",
		/Reasoning\s+records\s+are\s+content-hashed\s+and\s+anchored/,
		"<h3>Reasoning records are content-hashed and anchored on Arc.</h3>",
	],
	[
		"cookies_without_samesite",
		phrase("Production cookies are HttpOnly and Secure."),
		"Production cookies are HttpOnly and Secure. nginx, the UI",
	],
	[
		"what_runs_omits_payment",
		/Generation,\s+the\s+rigor\s+gate,\s+paper\s+trading,\s+and\s+trace\s+anchoring/,
		"capital today. Generation, the rigor gate, paper trading, and trace anchoring are what run",
	],
];

//: `[claimFragment, enforcingFile, literal]` — the tripwire. `literal` is a
//: substring that must still be present in `enforcingFile` for the page's
//: sentence to have anything behind it. Substring, not regex, on purpose:
//: these are pins on code, and a pin that needs escaping is a pin someone
//: will get subtly wrong.
const CLAIM_BINDINGS = [
	// Session
	["HttpOnly, Secure, and SameSite=Lax", "auth/auth.js", "httpOnly: true"],
	["HttpOnly, Secure, and SameSite=Lax", "auth/auth.js", "sameSite: 'lax'"],
	[
		"HttpOnly, Secure, and SameSite=Lax",
		"auth/auth.js",
		"useSecureCookies: production",
	],
	[
		"independently protect private surfaces",
		"nginx/nginx.conf",
		"auth_request /_auth_session;",
	],
	[
		"Two browse pages are deliberately anonymous",
		"ui/src/routes.js",
		"const ANON_APP_PAGES = new Set(['explore', 'corpus'])",
	],

	// Authority model
	[
		"A connected wallet never creates",
		"backend/archimedes/api/account_auth.py",
		'"cookie": request.headers.get("cookie", "")',
	],
	[
		"five-minute, single-use EIP-4361 challenge",
		"backend/archimedes/api/wallet_routes.py",
		"_CHALLENGE_TTL = timedelta(minutes=5)",
	],
	[
		"authenticate with a service credential",
		"backend/archimedes/api/auth_guard.py",
		"hmac.compare_digest",
	],

	// Payment
	[
		"bound to the wallet you proved",
		"backend/archimedes/services/generation_payment.py",
		"async def enforce_generation_payment",
	],
	[
		"a mismatch is refused before any settlement round-trip",
		"backend/archimedes/services/generation_payment.py",
		"payer_mismatch",
	],
	[
		"An operator kill switch refuses service",
		"backend/archimedes/services/generation_payment.py",
		"payments_halted",
	],
	[
		"published anonymously at GET /api/generate/quote",
		"backend/archimedes/api/generate_routes.py",
		"return generation_payment.quote()",
	],
	[
		"the generation paywall is on and not in dry-run",
		"infra/ecs.tf",
		'{ name = "GENERATION_PAYMENT_REQUIRED", value = "true" }',
	],
	[
		"the generation paywall is on and not in dry-run",
		"infra/ecs.tf",
		'{ name = "GENERATION_PAYMENTS_DRY_RUN", value = "false" }',
	],

	// Edge
	[
		"one hashed inline bootstrap",
		"nginx/nginx.conf",
		"'sha256-ASECgocBcGWh7BG/yec7jR6PLvDGDA9r96mYGyxhh/A='",
	],
	//: HSTS and framing have TWO sources, and only probing the live site shows
	//: it: `infra/cloudfront.tf`'s response-headers policy sets both with
	//: `override = true`, so the wire value is CloudFront's, not nginx's. (The
	//: tell is Referrer-Policy — nginx says `same-origin`, the response says
	//: `strict-origin-when-cross-origin`.) Pinning nginx alone would raise a
	//: false alarm when the header moves to the edge, and worse, would go quiet
	//: if the edge policy were the thing that got dropped. Both ends, therefore.
	["HSTS", "nginx/nginx.conf", "add_header Strict-Transport-Security"],
	["HSTS", "infra/cloudfront.tf", "strict_transport_security {"],
	[
		"framing denied outright",
		"nginx/nginx.conf",
		'add_header X-Frame-Options "DENY" always;',
	],
	["framing denied outright", "infra/cloudfront.tf", 'frame_option = "DENY"'],
	//: CSP and Permissions-Policy are nginx-only — the CloudFront policy sets
	//: neither — so those two keep a single pin, correctly.
	[
		"turns off geolocation, microphone, and camera",
		"nginx/nginx.conf",
		'Permissions-Policy "geolocation=(), microphone=(), camera=()"',
	],
	[
		"Two per-IP request-rate zones",
		"nginx/nginx.conf",
		"limit_req_zone $binary_remote_addr zone=api_read:10m rate=60r/m;",
	],
	[
		"the tighter one on the credential surface",
		"nginx/nginx.conf",
		"limit_req_zone $binary_remote_addr zone=api_write:10m rate=20r/m;",
	],
	[
		"tighter per-route limits on expensive endpoints",
		"backend/archimedes/api/generate_routes.py",
		'@limiter.limit("5/minute")',
	],

	// Verdict + provenance
	[
		"computed server-side, never asserted",
		"backend/archimedes/services/live_rigor_gate.py",
		"def verdict_from_returns",
	],
	[
		"re-hashed and compared against its on-chain anchor",
		"backend/archimedes/api/traces_routes.py",
		"async def verify_trace",
	],
	[
		"rebalance decisions are content-hashed",
		"backend/archimedes/chain/agent_runner.py",
		"_commit_trace",
	],
	[
		"rebalance decisions are content-hashed",
		"backend/archimedes/chain/agent_runner.py",
		"_reveal_trace",
	],
	[
		"will only be anchored in a later version",
		"backend/archimedes/agents/generation_pipeline.py",
		"mirrored on-chain in v1.5",
	],
];

test("the security page exists where every guard here expects it", () => {
	assert.equal(
		existsSync(repoFile(SECURITY_PAGE)),
		true,
		`${SECURITY_PAGE} is missing — a rename would shrink every scan below to nothing`,
	);
});

for (const [claim, evidence] of LIVE_CLAIMS) {
	test(`security page still claims: ${claim.slice(0, 62)}`, () => {
		assert.match(
			security,
			phrase(claim),
			`${SECURITY_PAGE} no longer carries this sentence. If the claim was removed on ` +
				`purpose, move its docs/claims-ledger.md row to RETRACTED in the same change — ` +
				`a ledger row asserting a sentence the page does not say is the drift this ` +
				`guard exists to catch. Evidence for the claim: ${evidence}`,
		);
	});
}

test("every retraction pattern rejects its own canonical example", () => {
	for (const [name, pattern, example] of RETRACTED_CLAIMS) {
		assert.match(
			example,
			pattern,
			`RETRACTED_CLAIMS[${name}] no longer matches the literal it was written ` +
				`against — it has been weakened and is guarding less than it claims`,
		);
	}
});

for (const [name, pattern] of RETRACTED_CLAIMS) {
	test(`security page no longer over-claims: ${name}`, () => {
		assert.doesNotMatch(
			security,
			pattern,
			`${SECURITY_PAGE} has re-acquired the ${name} wording, retracted 2026-08-31 ` +
				`because the code does not back it. See docs/claims-ledger.md's Security ` +
				`page section for what the corrected sentence is and why.`,
		);
	});
}

test("every claim binding names a fragment the page actually carries", () => {
	//: Anti-vacuity for CLAIM_BINDINGS. A binding whose claim fragment has been
	//: reworded off the page still passes its own literal check while guarding a
	//: sentence nobody reads — the pin would sit there looking green forever.
	const orphaned = CLAIM_BINDINGS.filter(
		([claim]) => !phrase(claim).test(security),
	).map(([claim]) => claim);
	assert.deepEqual(
		[...new Set(orphaned)],
		[],
		`these CLAIM_BINDINGS pin code to wording that is no longer on ${SECURITY_PAGE}. ` +
			`Re-point the binding at the new sentence, or drop it along with the claim.`,
	);
});

test("every enforcing file the page leans on still exists", () => {
	const missing = [...new Set(CLAIM_BINDINGS.map(([, file]) => file))].filter(
		(file) => !existsSync(repoFile(file)),
	);
	assert.deepEqual(
		missing,
		[],
		`the security page claims enforcement in files that are gone: ${missing.join(", ")}`,
	);
});

for (const [claim, file, literal] of CLAIM_BINDINGS) {
	test(`${file} still backs: ${claim.slice(0, 52)}`, () => {
		const source = readFileSync(repoFile(file), "utf8");
		assert.equal(
			source.includes(literal),
			true,
			`${file} no longer contains ${JSON.stringify(literal)}, which is what made ` +
				`the security page's "${claim}" true. Either restore it, or change the page ` +
				`and its docs/claims-ledger.md row in this same PR. A public security claim ` +
				`outliving its enforcement by even one merge is the defect being guarded.`,
		);
	});
}

test("'Paper trading is free' is backed by an absence, so it is pinned as one", () => {
	//: The page's one claim whose evidence is that something is NOT there. A
	//: presence pin cannot express it: what makes paper trading free is that
	//: `paper_routes.py` never reaches the paywall. Pinning some unrelated
	//: symbol in that file and calling it evidence would be the exact
	//: looks-verified-proves-nothing shape this file is written against, so the
	//: absence is asserted directly and the claim fragment is re-checked here
	//: rather than riding the CLAIM_BINDINGS loop.
	assert.match(security, phrase("Paper trading is free."));
	const paperRoutes = readFileSync(
		repoFile("backend/archimedes/api/paper_routes.py"),
		"utf8",
	);
	assert.equal(
		paperRoutes.includes("generation_payment"),
		false,
		"backend/archimedes/api/paper_routes.py now references the generation paywall. " +
			"If paper trading became a paid surface, the security page's 'Paper trading is " +
			"free.' is false as of that merge — fix the page and its claims-ledger row here.",
	);
});

test("the binding table is not vacuous", () => {
	//: Every check above is a for-loop over a list. An empty list passes them
	//: all. These floors are deliberately below the current counts so ordinary
	//: edits do not trip them, and far enough above zero that a gutted table does.
	assert.ok(
		LIVE_CLAIMS.length >= 12,
		`only ${LIVE_CLAIMS.length} live claims pinned — the table has been gutted`,
	);
	assert.ok(
		RETRACTED_CLAIMS.length >= 5,
		`only ${RETRACTED_CLAIMS.length} retractions pinned — the table has been gutted`,
	);
	assert.ok(
		CLAIM_BINDINGS.length >= 20,
		`only ${CLAIM_BINDINGS.length} claim bindings — the table has been gutted`,
	);
});

test("the ledger carries a Security page section for these rows to live in", () => {
	const ledger = readFileSync(repoFile("docs/claims-ledger.md"), "utf8");
	assert.match(
		ledger,
		/##\s+Security page\s+—\s+`ui\/src\/components\/Security\.jsx`/,
		"docs/claims-ledger.md lost its Security page section — the page's claims have " +
			"nowhere to be recorded, and backend/tests/test_claims_ledger.py can no longer " +
			"resolve their citations",
	);
});
