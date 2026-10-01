# ADR: Chainlink-primary price oracles with a thin, bounded admin fallback

> **Audience:** Archimedes team (decision owner: Dan, contract + on-chain-integration owner; preferred contract reviewer: Bogdan `mnemonik-dev`)
> **Status:** Accepted
> **Date:** 2026-07-01
> **Owner:** Dan Browne (contract reviewer / ratifier: Bogdan Sivochkin)
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** Where does the on-chain USD price that feeds vault collateral math come from — our admin-set `PriceOracle`, a well-validated external oracle (Chainlink), or some composition of the two?
> **Related issues/PRs:** [#724](https://github.com/aprin-labs/archimedes/pull/724) (Chainlink-first read path **+ the #724-review hardening**), [#731](https://github.com/aprin-labs/archimedes/pull/731) (owner≠agent non-custodial vaults — closed the drain vector). Feed-outage telemetry/alerting is operational follow-up.

> **✅ Update 2026-07-01 — RATIFIED as Chainlink-only.** This ADR is now **Accepted**. We
> commit to **one well-validated decentralized feed (Chainlink) as primary + the thin,
> bounded admin fallback** — explicitly **not** a multi-source composition. A multi-source
> **quorum / medianizer** (on-chain median of Chainlink + Stork + Pyth; explored in **PR
> [#840](https://github.com/aprin-labs/archimedes/pull/840)**) and a **standalone Stork
> `AggregatorV3` adapter** (**PR [#828](https://github.com/aprin-labs/archimedes/pull/828)**) were
> both **considered and CLOSED** to reduce complexity — *"we need any one validated feed, not
> all of them"* (Bogdan). One audited feed per asset is simpler to reason about and review
> than an N-feed medianizer, and sufficient for our trust model. This settles the "some
> composition of the two" option in the Question above as **single-feed, not multi-feed**.
>
> **Current reality (2026-07-01):** Chainlink's Arc price-feed proxy contracts
> (**`EACAggregatorProxy`**, one per asset) are **not published yet** — no DON is writing
> price data to an on-chain feed on Arc, so the *primary* isn't wireable today. In the interim
> the **bounded admin path is the active on-chain price source**, fed **real market prices**
> (Pyth Hermes + yfinance, off-chain) relayed on-chain by the `oracle_updater` — **real
> prices, not hardcoded** (the #1 rule holds). Each asset **promotes to the decentralized feed
> with a single `setPriceFeed(<EACAggregatorProxy>)` call** the moment Arc publishes it — the
> #724 read path is already `AggregatorV3`-ready, no consumer change, no migration.

## TL;DR

**Make Chainlink (or an equivalent well-validated, generally-accepted oracle) PRIMARY everywhere a feed exists. Keep our custom admin-set `PriceOracle` ONLY as an explicit, logged, tightly-bounded fallback** for the long tail of assets that have no native feed and for the window when a feed is stale or down. This is exactly the "Chainlink-first, admin fallback" architecture PR #724 implements: `getPrice()` reads `latestRoundData()` from a configured `AggregatorV3Interface` feed with full staleness/round-completeness/non-negative validation, and **degrades to the admin-fed `price` whenever the feed is absent (`priceFeed == address(0)`), unusable (reverts/stale/bad answer), or grossly out of band** — reverting only when *both* sources are unusable. The fallback is the weaker link, so it stays bounded (deviation caps + staleness + rate-limit) and loud (distinct events), and it is used only on feed absence or outage — never as the default trust source.

## Implemented design — post-review hardening (#724)

The adversarial #724 review found the feed path, as *first* written, **bypassed every
manipulation guard the admin write-path earned (#587)** and **hard-reverted on a bad feed
(a DoS, not a failover)**. The merged design folds in four changes, so the implemented feed
path **degrades to the bounded admin fallback** on any problem (it reverts only when *both*
sources are unusable — see "Decision" below). Where this ADR discusses a feed outage causing
a *halt*, that describes the **rejected strict-Chainlink-only alternative**, not the shipped
design:

1. **Degrade, don't brick.** A feed that reverts, stales, returns a bad answer, or floors
   to zero degrades to the admin `price` (which keeps its own staleness check). `getPrice()`
   reverts only when *both* sources are unusable. `isFresh()` no longer self-calls.
2. **Sanity band** (`maxFeedDeviationBps`, default 50%) — reject a feed answer that grossly
   deviates from a *fresh* admin reference (catches mis-pointed / wrong-denomination /
   compromised feeds before they reprice vaults); skipped when the admin ref is stale or the
   band is set to 0. The band comparison is computed **overflow-safe and revert-free**: both a
   malicious huge feed answer *and* a ratcheted-large admin `price` degrade out of band rather
   than overflow-reverting and bricking the read.
3. **Per-feed heartbeat** (`feedStaleness`, default 1h) instead of the admin path's 24h
   `MAX_STALENESS` — a *live* feed frozen for hours must read stale on a funds path.
4. **Post-scale zero-reject**, and `decimals()` **cached at config time** (TOCTOU + gas).
   The Chainlink **sequencer-uptime** gate is explicitly scoped out (Arc has no Chainlink
   uptime feed; the underlying risk is covered by the degrade + tight heartbeat).

Net behavior is **automatic, bounded failover** (feed → admin), not a hard halt — which
directly resolves the "Open risk" Dan raised below.

## Context

`PriceOracle.getPrice()` returns a 6-decimal USDC price that flows **straight into `Vault` / `SyntheticVault` collateral math** — `getPrice()` → `totalAssets()` → mint/redeem/withdraw quantities. Whatever sets that number effectively sets how much of a user's USDC the system thinks each synth is worth. That makes the oracle the single most funds-adjacent contract we operate.

The shipped `PriceOracle` (pre-#724) is **a single admin-set scalar**: an `updater` (or owner) pushes `setPrice(...)`, and that value is the price. We hardened the *write* path over several iterations — a per-update deviation cap (`maxDeviationBps`, default 20%), an inter-update cooldown (`updateCooldown`, issue #587, defeating the same-block ratchet), a `forceSetPrice` escape hatch bounded by a non-configurable `FORCE_MAX_DEVIATION_BPS` (10×), and a 24h `MAX_STALENESS`. Those guards matter and we are keeping them. But they bound *how badly a single writer can move the number* — they do not change the fundamental shape:

**A single admin-set price is a trusted-third-party.** The protocol's correctness rests on one key behaving honestly and one off-chain runner staying live. That is precisely the trust assumption Archimedes' whole pitch is built to *eliminate* — paper-grounded, on-chain-verifiable, non-custodial — and it is the same class of vector PR #731 just closed on the vault side. #731 separated owner from agent so a compromised agent key can rebalance but cannot drain to the platform. An admin-set oracle re-opens an economically equivalent door from a different angle: control the price the vault reads and you control redemptions, regardless of who can sign a withdrawal. "Owner ≠ agent" removes the custody drain; "Chainlink-primary" removes the *pricing* drain. They are the same architectural instinct — **don't leave a single trusted writer standing in front of user funds** — applied to two different contracts.

The original contract header even said the quiet part out loud: *"In production, replace with Chainlink feed. For the hackathon, the backend agent pushes price updates periodically."* PR #724 is that replacement, done as a precedence change rather than a wholesale swap, so every existing consumer keeps the unchanged no-arg `getPrice()` signature.

## Decision

**Chainlink-first, admin-fallback. Specifically:**

1. **Where a well-validated feed exists, it is PRIMARY.** "Well-validated" means Chainlink or an equivalent generally-accepted, audited, decentralized oracle exposing the canonical `AggregatorV3Interface` (Arc-native / Chainlink-compatible aggregators qualify). When `priceFeed != address(0)` and the feed is usable + in band, `getPrice()` returns the feed value; the admin-fed `price` storage is **not the live source** — though it is still consulted as the sanity-band reference and as the automatic degrade target (bullet 2).

2. **A bad feed read degrades to the bounded admin fallback — it does not revert.** `_tryReadChainlink()` validates, in order: a non-reverting `latestRoundData()` call, `answer > 0` (negative/zero), `updatedAt != 0` (incomplete round), `updatedAt <= block.timestamp` (no future timestamp), `answeredInRound >= roundId` (stale round), and `block.timestamp - updatedAt <= feedStaleness` (the per-feed 1h heartbeat, tighter than the admin path's 24h `MAX_STALENESS`). It also bounds-checks the decimal up-scale so a malicious huge answer can't overflow-revert inside the read. **Any failure returns `(false, …)` and `getPrice()` falls back to the admin `price`** (which keeps its own staleness check); `getPrice()` reverts only when *both* the feed and the admin price are unusable. A feed answer that passes validation but deviates from the fresh admin reference by more than `maxFeedDeviationBps` is also rejected (degrade), catching mis-pointed / wrong-denomination / compromised feeds. Feed answers are scaled to 6 decimals (8-decimal USD feeds → `/100`); `setPriceFeed` validates `decimals() <= 36` at config time and caches `decimals()` so the scaling math can't overflow on read.

3. **The custom admin oracle survives ONLY as a bounded fallback.** It is used (a) for assets with no native feed — the long tail — and (b) implicitly never, for feed-backed assets, because precedence routes around it. To switch an asset to fallback, the owner sets `priceFeed = address(0)` — an **explicit, owner-only, evented** action (`PriceFeedChanged`), not a silent failover.

4. **The fallback stays bounded and loud.** All existing write-path guards remain in force on the fallback path: `maxDeviationBps`, `updateCooldown` / `lastSetPriceTime`, `forceSetPrice` + `FORCE_MAX_DEVIATION_BPS`, and `MAX_STALENESS`. Every fallback write emits `PriceUpdated` / `PriceForced`; every feed (re)configuration emits `PriceFeedChanged`. These are the telemetry hooks #725 builds alerting on.

5. **Provenance follows the same discipline as everywhere else in Archimedes.** Which source priced a vault at decision time is an auditable on-chain fact (feed address vs `address(0)`, plus the emitted events) — consistent with the "claims must be true on the live path" rule.

## Consequences

### Positive

- **Removes the pricing SPOF / trusted-third-party.** For every asset with a feed, no single Archimedes key or off-chain runner can move the price that values user funds. This is the security win and it is the main point.
- **A recognized trust signal.** "Vault collateral is priced by Chainlink" is a claim judges, grant reviewers, and real users already know how to evaluate. It is strictly more credible than "our backend pushes a number," and it is *true on the live path* once a feed is configured — not a fixture.
- **Architecturally consistent with #731.** Same instinct (no lone trusted writer in front of funds), now applied to pricing as well as custody. The two together make "non-custodial" a structural property, not a slogan.
- **Degrade-not-brick by construction.** A degraded feed automatically falls back to the bounded admin `price` rather than reverting the whole read; `getPrice()` halts only when *both* sources are unusable. This keeps a single feed outage from being a hard DoS on every dependent vault while still refusing to price on a manipulated feed (the sanity band degrades it). That is the correct default for funds-adjacent reads that must also stay live.
- **No consumer churn.** The no-arg `getPrice()` signature is preserved, so `Vault`, `SyntheticVault`, `SyntheticFactory`, and the backend reference read all keep working unchanged. Per-asset, reversible migration via `setPriceFeed`.

### Negative / costs we are accepting

- **The fallback is a WEAKER link, and we are deliberately keeping a weaker link in the tree.** When an asset is on the admin path (no feed, or feed cleared), we are back to a single trusted writer — bounded, but trusted. We accept this *only* because:
  - it is **bounded** — deviation caps + the non-configurable force cap + the inter-update cooldown limit how far a single compromised key can move the price per window;
  - it is **staleness-checked** — the same 24h `MAX_STALENESS` reverts a fallback that stops updating;
  - it is **loud** — every fallback write and every feed reconfiguration emits a distinct event for #725's alerting; and
  - it is **used only on feed absence/outage**, never as the default trust source for a feed-backed asset.
- **A second read path to maintain and reason about.** Two code paths (feed + admin) means more surface, more tests, and a precedence rule reviewers must hold in their heads. PR #724 ships a dedicated `PriceOracleChainlink.t.sol` covering scaling, staleness, precedence, fallback, and every fail-soft degrade guard to keep this honest.
- **Operational responsibility shifts but does not vanish.** We trade "keep the pusher live and honest" for "configure the *right* feed (correct asset/USD pair) and watch for feed outage." `setPriceFeed` pointing at the wrong feed silently reprices every vault that reads the oracle — so feed configuration is contract-review-grade work (Dan approves, Bogdan reviews).

## Alternatives considered

### Strict Chainlink-only (no fallback at all) — **rejected**

Read exclusively from feeds; if no feed exists or the feed is down, the oracle simply can't price the asset and every dependent vault halts.

- **Pro:** maximal trust-minimization; zero admin trusted-writer surface; the simplest possible story.
- **Rejected for liveness + coverage.** Archimedes' strategy library is paper-grounded and reaches into the **long tail** — assets (and synths) that have no Chainlink feed on Arc testnet today. Strict-only would mean those assets are simply un-priceable and un-tradeable, gutting coverage of exactly the strategies that differentiate us. It also makes a single feed outage a hard, unmitigated halt with no operator recourse. We want the *primacy* of Chainlink without surrendering liveness for the long tail. (Note this is **not** custom-only-with-extra-steps: feed-backed assets get zero admin trusted-writer surface; the trusted writer exists only where no feed does.)

### Custom admin oracle only (status quo ante) — **rejected**

Keep shipping the single admin-set price, just better-hardened.

- **Pro:** one code path; covers every asset uniformly; we already operate the runner.
- **Rejected for security.** No amount of deviation-cap/cooldown hardening changes the fact that it is a trusted-third-party in front of user funds — the exact class of vector #731 closed and the exact thing the pitch claims we *don't* do. Bounding the blast radius is necessary but not sufficient; the right answer is to not have the single trusted writer at all wherever a validated feed exists. Hardening the writer is the *fallback* story, not the *primary* one.

## Open risk

**Dan's concern, stated plainly: when Chainlink goes offline, precedence is preserved but the feed stops being the trust source — for that asset we automatically degrade to the *less-hardened* admin fallback** (a stale / unusable / out-of-band feed degrades without any operator action, by design; deliberately clearing the feed is just the explicit, evented version of the same move). That is a real reduction in trust-minimization exactly at the moment of stress, and it is the price of choosing liveness over strict-only — but the degrade is bounded by the admin path's own deviation/cooldown/staleness guards, so a feed outage cannot misprice without also tripping those.

**We mitigate it by hardening and alerting, not by removing the fallback:**

- **Harden the fallback (#725).** Treat the admin path as a funds-safety surface in its own right: keep the deviation + cooldown + force caps tight, keep `MAX_STALENESS` enforced, and revisit the bounds against the real update cadence rather than letting them drift to "generous."
- **Alert loudly on every failover.** Feed staleness, `PriceFeedChanged` to `address(0)`, and any `forceSetPrice` must page the operator — a switch onto the fallback is an incident, not a routine event. The events are already emitted; #725 wires the alerting.
- **Make failover a deliberate, logged, reversible operator action.** Clearing a feed is owner-only and evented; re-pointing to the feed when it recovers is one `setPriceFeed` call. Document the runbook so the on-the-spot decision (halt vs. fall back) is a checklist, not an improvisation.
- **Do not "solve" this by deleting the fallback.** Strict-only trades this risk for a worse one (hard halts + zero long-tail coverage). The fallback stays; we make it as strong and as observable as we can.

## Ratification

**Adopt Chainlink-primary with a thin, bounded, loud admin fallback — the PR #724 architecture.** **Status: Accepted (2026-07-01) — ratified Chainlink-ONLY** by Bogdan (contract reviewer), accepted by Dan (owner); the multi-source quorum (#840) and standalone Stork adapter (#828) were considered and **closed** (see the 2026-07-01 update up top). #724 is merged. Feeds are configured per-asset where they exist on Arc — but Chainlink's Arc `EACAggregatorProxy` addresses are **not published yet**, so today assets run on the bounded admin fallback fed **real** Pyth/yfinance prices via the `oracle_updater`; each promotes to the decentralized feed with a single `setPriceFeed` call the moment Arc publishes it — no consumer change, no migration. #725 lands the fallback-hardening + failover alerting.