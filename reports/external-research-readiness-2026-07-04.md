# Research: External-research readiness for unscoped context-gathering request

> **Historical snapshot — 2026-07-04.** This records why external research was skipped for that unscoped request; it is not current product guidance.

## Summary

External research is **premature**. Parent prompt asks for context gathering before planning but names no concrete feature, bug, or issue target. No specific external topic is inferable without inventing scope, so the correct move is to request a concrete target before spending research budget.

## Findings

1. **No concrete target present** — Parent prompt gives "gather context before planning" with zero feature/bug/issue anchor. Project rule forbids inventing scope. External web research needs a specific question (a lib version, an API contract, a spec) — none exists here.
2. **Local context is rich but broad, not a research trigger** — `CLAUDE.md` names many external anchors (Arc/Circle docs in `submodules/context-arc/`, Xia 2026 [arxiv 2605.19337], StockBench [arxiv 2510.02209], Bailey/López de Prado DSR/PBO papers). All are *already vendored or cited* locally. None flags a stale-fact gap needing a fresh web lookup absent a task.
3. **Canonical references are local-first** — Any Arc/Circle integration question routes to `submodules/context-arc/AGENTS.md` before web. Any q-fin rigor question routes to `docs/specs/`. Web research would duplicate vendored primary sources unless a task exposes a version drift or a new-topic gap.
4. **Where external research *would* pay off (conditional)** — If a concrete task lands, likely high-value web angles: (a) Circle x402 / Gateway nanopayment current API surface (recent, moves fast); (b) Arc testnet chain params / RPC changes; (c) latest DSR/PBO methodology updates; (d) Bedrock Converse multi-provider model availability. All require a named task first.

## Sources

- Kept: `CLAUDE.md` (local) — enumerates all external anchors already vendored/cited; confirms no open web gap.
- Kept: `submodules/context-arc/AGENTS.md` (local, per CLAUDE.md) — canonical Arc/Circle reference; supersedes web for integration Qs.
- Dropped: general web search — no query is well-posed without a concrete target; searching now = inventing scope.

## Gaps

- No feature/bug/issue named → cannot select a research angle.
- Unknown which Lepton-Sprint tier (T0 claim-integrity vs T1 vertical) the parent intends.
- Unknown whether target is contracts, backend, frontend, or infra.

## Clarification questions needed before useful external research

1. What is the concrete target — feature, bug, or issue number?
2. Which subsystem: contracts / `chain/` / backend API / UI / infra?
3. Is this Tier 0 (claim integrity) or Tier 1 (multi-agent/nanopayment) work?
4. Any external dependency/version in question (Circle SDK, x402, Bedrock, Arc RPC), or is this purely internal-code context?

## Supervisor coordination

No blocker requiring a decision. Returning brief normally.
