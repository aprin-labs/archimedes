# Architecture Decision Records

> **Status:** current (last updated 2026-09-01). The ADR pattern is: capture a non-trivial technical decision
> once, with the alternatives considered and the reasoning, so future contributors can
> understand the choice without needing to relitigate it. The records below were written
> as decisions landed; the 2026-06-27 batch back-fills decisions that shipped before the
> ADR habit was established (each cites the PR/commit/spec it documents).

## Status vocabulary

Every ADR carries the same five-field front-matter block directly under its title:

```
> **Status:** Proposed | Accepted | Superseded-by-NNNN | Rejected
> **Date:** YYYY-MM-DD
> **Owner:** <name>
> **Supersedes:** <adr> or —
> **Superseded-by:** <adr> or —
```

- **Proposed** — written, not yet decided. A `Proposed` ADR is the only kind that is still
  open to argument.
- **Accepted** — decided and in force. Do not relitigate an `Accepted` ADR in a spec, a
  handover, or a comment; open a superseding ADR instead.
- **Superseded-by-NNNN** — reversed or replaced. The record stays (the reasoning is still
  the history), and `Superseded-by` names the ADR that replaced it. This directory
  identifies ADRs by slug rather than number, so the slug stands in for `NNNN` —
  e.g. `Superseded-by-debate-society-sole-generation-pipeline`.
- **Rejected** — considered and declined. Kept so the option is not re-proposed blind.

A decision that has shipped but is awaiting a named reviewer's sign-off is recorded as
`Accepted, pending <lane> sign-off` — accepted because it is live in code, pending because
the review named in the code has not happened.

## Index

Twenty-five records. Status and date are authoritative in each ADR's front-matter block;
this table mirrors them. (The count read "eighteen" while the table already held nineteen —
the `generation-payment-credit-not-refund` row landed 2026-08-29 without a count bump.
Corrected here, and bumped again for `lambda-generation-offload` on 2026-08-30, and
`ipfs-pinning-not-live` + `backtests-are-frozen-evidence` + `rigor-verdict-of-record` on
2026-09-01.)

| ADR | Status | Date | Owner | Decision |
|---|---|---|---|---|
| [`unlicense-public-domain.md`](unlicense-public-domain.md) | Accepted | initial commit `292f543` | Dan Browne | Why the project is released under **the Unlicense** — a public-domain dedication — and what that costs: the code is not an ownable asset, and contributors retain copyright independent of it |
| [`arc-settlement-chain.md`](arc-settlement-chain.md) | Accepted | 2026-05-13 | Dan Browne | Arc testnet (chain `5042002`) settles, with **USDC as both settlement asset and native gas token**; Circle Developer-Controlled Wallets on the write path |
| [`two-tier-marketplace.md`](two-tier-marketplace.md) | Accepted | 2026-05-13 | Dan Browne | The Day-3 pivot from a single vault to a **Verified / Community two-tier marketplace**, with rigor as a badge rather than an entry requirement |
| [`backtrader-backtest-engine.md`](backtrader-backtest-engine.md) | Accepted | 2026-05-13 | Dan Browne | Why we picked **backtrader** over **vectorbt** for the v1 backtest engine |
| [`build-on-deploy-main-only.md`](build-on-deploy-main-only.md) | Accepted | 2026-05-18 | Dan Browne | Why `main` is the only long-lived branch and every merge auto-deploys (no `develop`) |
| [`portfolio-constructor-decision-tree.md`](portfolio-constructor-decision-tree.md) | Accepted | 2026-05-22 | Önder Akkaya | Which portfolio constructor runs when |
| [`k1-generation-external-rigor-gate.md`](k1-generation-external-rigor-gate.md) | Accepted | 2026-05-23 | Dan Browne | Why generation emits **K=1** winner + considered-rejects, with the rigor gate run **externally** |
| [`aws-account-migration.md`](aws-account-migration.md) | Accepted | 2026-06-24 | Dan Browne | Why prod moved to Dan's own AWS account (`037613907429`/`us-east-1`) post-Agora |
| [`generation-payment-credit-not-refund.md`](generation-payment-credit-not-refund.md) | Accepted | 2026-08-29 | Önder Akkaya | Why an undelivered generation is repaid as a **durable credit, never a refund**, and why the idempotency claim is taken before the money moves (#1441) |
| [`glm-to-bedrock-llm-migration.md`](glm-to-bedrock-llm-migration.md) | Accepted | 2026-06-24 | Dan Browne | Why the live LLM moved from **GLM to AWS Bedrock** (Nova Micro default, Converse backend) (#717) |
| [`non-custodial-vault-owner-agent.md`](non-custodial-vault-owner-agent.md) | Accepted | 2026-06-26 | Dan Browne | Why vaults separate **owner (withdrawal)** from **agent (rebalance-only)** so a compromised agent key can't drain (#731). **Amended 2026-08-31**: decision unchanged and contracts deployed, but the user-facing vault journey is roadmap, not shipped product (#1266/#1354/#1469) |
| [`portfolio-constructor-consolidation.md`](portfolio-constructor-consolidation.md) | Accepted | 2026-06-26 | Önder Akkaya | Why legacy constructors were retired and a **dual-signal** (regime × consensus) sizer activated (#131, #662) |
| [`rigor-gate-unification.md`](rigor-gate-unification.md) | Accepted | 2026-06-26 | Dan Browne | Why the four selection-bias controls run through **one** authoritative gate, surfaced honestly (post-#710) |
| [`fusion-primary-generation.md`](fusion-primary-generation.md) | **Superseded-by-debate-society-sole-generation-pipeline** | 2026-06-26 | Dan Browne | Why strategy generation was **fusion-primary** (paper-grounded), not free-form LLM (#751). Routing tree retired 2026-07-09 |
| [`chainlink-primary-oracle.md`](chainlink-primary-oracle.md) | Accepted | 2026-07-01 | Dan Browne (reviewer: Bogdan Sivochkin) | Why on-chain prices are **Chainlink-primary** with a thin, bounded admin fallback that **degrades (not reverts)** on feed outage (#724) |
| [`ec2-to-ecs-fargate-cutover.md`](ec2-to-ecs-fargate-cutover.md) | Accepted | 2026-07-09 | Dan Browne | Why the serving tier moved from one docker-compose EC2 box to an **ECS Fargate** service behind the existing ALB (#1039, #1056–#1059) |
| [`debate-society-sole-generation-pipeline.md`](debate-society-sole-generation-pipeline.md) | Accepted | 2026-07-09 | Dan Browne | Why the **debate society is the only generation path** — no routing tree, no flag, no silent fallback (#1064/#1074) |
| [`num-trials-self-containment.md`](num-trials-self-containment.md) | Accepted (ratified 2026-08-31, #1555; option 1 board FDR 2026-09-01, #1654) | 2026-07-09 | Dan Browne (quant reviewer: Önder Akkaya) | Why a strategy's DSR trial count depends **only on that strategy** — never `N + library_size`; curated single-paper strategies grade at `num_trials = 1`. **Option 1 recorded (#1654):** board FDR is ranking-surface and advisory; wiring shipped ([#1564](https://github.com/aprin-labs/archimedes/issues/1564) / [PR #1580](https://github.com/aprin-labs/archimedes/pull/1580), `Leaderboard.jsx`); never flips the badge; passport stays out |
| [`aurora-postgres-alembic-datastore.md`](aurora-postgres-alembic-datastore.md) | Accepted | 2026-07-28 | Dan Browne | Why **Aurora PostgreSQL Serverless v2 (18.3)** is the system of record, **Alembic** the only schema-change mechanism, **Redis 7.1** ephemeral-only |
| [`strategy-dsl-hardening-over-lean4.md`](strategy-dsl-hardening-over-lean4.md) | Accepted | 2026-08-30 | Dan Browne | Why the generator's emission target stays the **closed-enum JSON DSL, hardened**, and **not Lean 4** — the no-generated-code property is already structural; a restricted sandbox is reserved for shapes the DSL cannot express |
| [`market-data-sourcing.md`](market-data-sourcing.md) | Accepted | 2026-08-31 | Dan Browne | Why market data is sourced **per surface** — Tiingo (starting on the Free tier, for testing) for backtesting and paid analysis, yfinance for the free, ungated Explore viewer that sells and redistributes nothing. Flags a **Tiingo commercial plan as a mainnet prerequisite** and records that the split is reversible by build (#1218, #1282, #1455) |
| [`lambda-generation-offload.md`](lambda-generation-offload.md) | **Proposed — verdict DEFER** | 2026-08-30 | Dan Browne | Why generation does **not** move to Lambda yet, measured on a real VPC-attached container built from the production image: no dependency or size blocker, but a **13.6 s** steady-state / **51 s** post-deploy cold start on a ~48 s job. Adopts the lane-agnostic worker entrypoint + the measured-cost model, and corrects the quote seam from `quote()` to `_price()` (#1411, feeds #1217) |
| [`ipfs-pinning-not-live.md`](ipfs-pinning-not-live.md) | Accepted | 2026-09-01 | Dan Browne | Reasoning-trace reveal is **hash-only** (empty `storagePointer`). The Pinata pin path is removed, not half-wired: `PINATA_JWT` was never in prod ECS secrets, and public copy must not claim IPFS pinning (#1526) |
| [`backtests-are-frozen-evidence.md`](backtests-are-frozen-evidence.md) | Accepted | 2026-09-01 | Dan Browne | A backtest is a **one-time artifact with a stated data window** — generated strategies are backtested exactly once at generation, curated ones only when their code changes or by explicit operator action. **No periodic or boot-time refresh anywhere**; forward performance is the paper-trading ledger's job. Retires `services/backtest_scheduler.py`, whose +180 s cold-boot storm killed ECS tasks on 2026-09-01 (#1760) and whose moving "latest" drove #1746's Sharpe drift |
| [`rigor-verdict-of-record.md`](rigor-verdict-of-record.md) | **Accepted, pending quant sign-off** | 2026-09-01 | Dan Browne (quant reviewer: Önder Akkaya) | A strategy is graded **once, at backtest time**, by the real gate; the verdict is persisted on its passport with `graded_at` / `gate_version` / `cohort_n`, and every surface **reads** it. A re-grade is an explicit, versioned event — never a silent overwrite, never a recompute on read. Supersedes #868's read-time-derivation premise; #821's "never a verdict no gate produced" survives tightened (#1746/#1747) |

### Open review debt

- **The `rigor-verdict-of-record.md` quant sign-off** — the ADR names Önder Akkaya
  as quant reviewer of record and that review has not happened, so the record is
  `Accepted, pending quant sign-off` per the rule above: live in code, awaiting
  the named lane. Two things specifically want a quant's eye — that
  `gate_version` covers every input that can move a verdict without the
  strategy's returns moving (its module docstring lists what is deliberately
  out), and that the migration's `legacy-derived` backfill rule reproduces what
  the old read-time derivation meant. Same shape as the
  `num-trials-self-containment.md` sign-off, which is the precedent for both the
  status string and this row.

- ~~The `num-trials-self-containment.md` portfolio-math sign-off~~ — **resolved
  2026-08-31**: ratified by Önder Akkaya
  ([#1555](https://github.com/aprin-labs/archimedes/issues/1555), outcome 3), with four
  corrections folded into the ADR. (History of the residual, as of that stamp: the
  served board-level BH FDR disagreed with the per-strategy gate on every strategy —
  min adjusted p 0.319 board-wide — and the ranking-surface product decision was
  still open. That sentence is no longer current; see the next item.)
- ~~Board-level BH FDR ranking-surface product decision~~ — **resolved 2026-09-01**:
  option 1 is recorded ([#1654](https://github.com/aprin-labs/archimedes/issues/1654));
  wiring shipped ([#1564](https://github.com/aprin-labs/archimedes/issues/1564) /
  [PR #1580](https://github.com/aprin-labs/archimedes/pull/1580), `Leaderboard.jsx`).
  Board FDR is ranking-surface and advisory; never flips the badge (`passes_all`);
  passport stays out. See the 2026-09-01 amendment in
  [`num-trials-self-containment.md`](num-trials-self-containment.md).

## When to add an ADR

- A library/framework choice with a real alternative
- A protocol / interface contract that downstream code will depend on
- A trade-off that future readers will look back at and ask "why did they do it that way?"

## When NOT to add an ADR

- Routine implementation choices (variable names, function shape)
- Decisions captured implicitly in shipped code + tests
- Things best captured in inline comments or commit messages
