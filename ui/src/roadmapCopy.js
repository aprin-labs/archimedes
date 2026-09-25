// Roadmap-only copy (#1354) — every string here describes the vault-deploy /
// marketplace journey that ROADMAP_SURFACES_ENABLED gates off by default
// (#1266). It lives in its own module, deliberately NOT one of the six
// "public surface" files `ui/test/roadmap-copy.test.js` source-scans, so
// that scan can assert those files carry none of this prose literally —
// consumers import these constants and render them only inside a
// `ROADMAP_SURFACES_ENABLED && (...)` (or an equivalent ternary) branch.
//
// Plain strings only (no JSX) — this file has a `.js` extension and Vite's
// default esbuild loader does not parse JSX in `.js`, only `.jsx`/`.tsx`.
// Markup that wraps these strings (`<strong>`, `<code>`, `<span>`, …) stays
// in the consuming component; only the literal phrase text that trips the
// claim-integrity guard moves here.
//
// Split in two (this file + roadmapCopyApp.js), not because the content
// differs in kind, but because of a build-output constraint verified while
// building this fix: a single roadmapCopy module imported by BOTH the
// public bundle (Landing/Architecture, chunk `index-*.js`) and the
// authenticated bundle (Insights/Strategies, chunk `AuthenticatedApp-*.js`)
// gets promoted by Rollup to its own always-loaded shared chunk — and a
// shared chunk's exports can't be pruned per-consumer, so the flag-off
// build kept the vault/marketplace strings in that chunk even though every
// individual usage site was dead. Splitting so each file is imported only
// by consumers that already land in the SAME chunk lets it inline there
// instead, where the dead branches (and this data) really do get dropped —
// confirmed by building with the flag off and grepping dist/assets/*.js for
// the phrase list in criterion 1 (see the PR body for the exact command
// and output). This file: the public pages (Landing.jsx, Architecture.jsx).
//
// Two rules for anyone editing either file:
//   1. Every string here is dead weight once the flag flips permanently on
//      or the vault/marketplace journey ships for real — that's fine, this
//      file's whole job is to be deletable in one place on that day.
//   2. Don't add anything here that ISN'T gated somewhere. An unused export
//      is a sign the gate it was meant for got skipped.
//   3. Don't import this file from an authenticated-app-only component —
//      that reintroduces the cross-chunk sharing problem above. Add to
//      roadmapCopyApp.js instead.

// The `landing` export was deleted in the 2026-08-30 claim scrub (owner
// decision): Landing.jsx now makes no execution claim in either flag state,
// so it imports nothing from here and every string that used to live in this
// object was dead weight — exactly the case rule 2 above says to remove.
// `architecture.ledgerVaultLabel` went with it: the honesty ledger's
// execution row is now ungated and roadmap-toned, so the label is inline.

export const architecture = {
	pipelineDeployTitle: "Deploy as vault",
	pipelineDeployActLabel: "five wallet signatures, all yours",
	marketplaceTitle: "Pay creators, not the house",
	ctaLede:
		"Describe a strategy in plain English. Sign in with any wallet or a " +
		"passkey; deploying into a vault uses free testnet USDC.",
};

