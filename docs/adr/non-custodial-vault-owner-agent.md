# ADR: Non-custodial vault architecture — owner ≠ agent

> **Audience:** Archimedes team (decision owner: Dan; preferred contract reviewer: Bogdan `mnemonik-dev`)
> **Status:** Accepted
> **Date:** 2026-06-26 (recorded; PR #731 merge date [unestablished — needs Dan])
> **Owner:** Dan Browne (contract reviewer: Bogdan Sivochkin)
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** Who holds withdrawal authority in user vaults? Can a compromised agent key drain user funds?
> **Related:** [`docs/specs/ecosystem-design-spec.md` § 3.2](../specs/ecosystem-design-spec.md), [`docs/architectural-principles.md`](../architectural-principles.md) (primitive #3), `contracts/src/Vault.sol`, `contracts/src/VaultFactory.sol`.

## TL;DR

**Separate the vault `owner` (the user, holds withdrawal authority) from the `agent` (Archimedes AI, holds rebalance authority only).** The agent can call `rebalance()` to move assets internally; it cannot withdraw, transfer, or redirect funds to a platform wallet. Only the owner can `withdraw()` / `redeem()`. This makes "non-custodial" a structural property of the contracts, not a claim backed by key hygiene alone — the same instinct as the Chainlink-primary oracle (remove the lone trusted writer in front of funds), applied to custody.

## Context

The Archimedes vault is an ERC-4626 tokenized vault holding user USDC + synth tokens. Rebalancing is continuous (drift thresholds, regime shifts, strategy rotation) — that is the agent's job. But rebalance authority sits one step from the funds: it moves USDC between assets and can mis-execute, yet must never be able to send user USDC to a platform address. An earlier shape conflated `creator` (deployer) with withdrawal authority, leaving a vector where a compromised agent key could rebalance *and* withdraw on the user's behalf. PR #731 makes the separation explicit and structural.

## Decision

**Distinct, bounded roles on the ERC-4626 vault:**
- **`owner`** (the user) — withdrawal authority only: `withdraw()` / `redeem()`. Cannot `rebalance()` or `setAgent()`.
- **`creator`** (deployer) — configuration authority: `setAgent()`, oracle/slippage config. Cannot `rebalance()`.
- **`agent`** (Archimedes AI, or a Tier-2 community manager) — `rebalance()` / `setTargetAllocations()` only. Cannot `withdraw()`, `redeem()`, `transfer()`, or `setAgent()`.

A rebalance cannot exfiltrate to a platform-held wallet: it operates only over the creator-curated asset allowlist, and only `withdraw()`/`redeem()` (owner-gated) route USDC to a receiver. `transferOwnership` lets the user retain/transfer their authority explicitly.

## Consequences

### Positive
- **Non-custodial becomes a structural guarantee.** A compromised agent key can mis-rebalance (a *performance* risk) but cannot *steal* (a custody risk). The user's withdrawal signature is still required for any custodial action.
- **Clear Tier-1 / Tier-2 governance** — Tier-1 vaults are agent-deployed with the user as `owner`; Tier-2 community vaults use the same structure with a real-user creator.
- **Consistent with the oracle work** ([chainlink-primary-oracle](chainlink-primary-oracle.md)) — both remove a single trusted authority in front of funds. Together, "non-custodial" is a property, not a slogan.

### Negative / costs we accept
- **More roles + guards = more contract surface + test surface.** Mitigated by dedicated Foundry tests covering the role boundaries.
- **Operational care** — setting the agent to a wrong address blocks rebalancing until corrected (`setAgent()` is owner-only and deliberate). This is the correct fail-safe default.

## Alternatives considered
- **Single-role vault (creator == agent) — rejected** for security: it gives a compromised agent key full control over user funds — the exact vector PR #731 closes.
- **Time-lock / multi-sig on agent rotation — rejected as incomplete:** it slows key swaps but doesn't narrow the agent's authority *now*. The real defense is narrow authority, not slow rotation.

## Ratification

Accepted; implemented via PR #731 (live on Arc testnet). Commit-before-trade ([commit-reveal] enforcement, #589/#755) layers on top so a rebalance also requires a fresh, causally-prior reasoning-trace commitment.

## Amendment — 2026-08-31 (product framing only; the decision is unchanged)

> Appended, not rewritten. Everything above still holds and was re-verified on this date:
> `contracts/src/Vault.sol` and `VaultFactory.sol` carry the owner / creator / agent
> separation exactly as decided, and the ownership check is live in the backend
> (`api/vaults_routes.py`).

What has changed is not the mechanism but **what the product is allowed to say about it.**
The Ratification line's "live on Arc testnet" is true of the *contracts* and has been read as
a claim about the *product*. It is not one. The vault-deploy journey — deploy, deposit,
rebalance, withdraw — is gated off every public surface behind `ROADMAP_SURFACES_ENABLED`
(off by default, [`ui/src/featureFlags.js`](../../ui/src/featureFlags.js), #1266), the
public-page copy was moved into `ui/src/roadmapCopy*.js` and pinned by
`ui/test/roadmap-copy.test.js` (#1354), and #1469 is open to scrub the last present-tense
phrasing.

So, precisely:

- **Deployed and verifiable:** the contracts, their role separation, and the guarantee that
  a compromised agent key cannot withdraw. This ADR's decision stands.
- **Roadmap, not shipped product:** the user-facing vault journey. Any doc, UI string, or
  pitch that implies users are running funds in Archimedes vaults today is wrong, whatever
  the contract addresses say.

Both sentences are needed. Dropping the first understates a real security property that was
paid for; dropping the second is the claim-integrity failure `CLAUDE.md` § "The hard
constraint" exists to prevent. Related: [`../architecture.md`](../architecture.md) § 8
(trust boundaries) and § 9 item 11.
