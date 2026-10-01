# ADR: Fusion-primary strategy generation — paper-grounded, not vibes-first

> **Audience:** Archimedes team (decision owner: Dan, strategy engine + backend lead)
> **Status:** Superseded-by-debate-society-sole-generation-pipeline
> **Date:** 2026-06-26 (recorded; decision date [unestablished — needs Dan])
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** [`debate-society-sole-generation-pipeline.md`](debate-society-sole-generation-pipeline.md) (2026-07-09)
> **Question being decided:** How does the generation engine route between free-form LLM generation, curated-library selection, and multi-paper fusion? Which is primary?
> **Related:** [`docs/corpus-architecture.md`](../corpus-architecture.md), `backend/archimedes/agents/generation_pipeline.py`, `backend/archimedes/agents/strategy_fusion.py`.

> **⛔ SUPERSEDED 2026-07-09 — the fusion/architect/agent routing tree described below is
> retired.** The multi-agent debate society is now the sole generation pipeline:
> `_pick_pipeline()` returns `"debate"` unconditionally
> ([`backend/archimedes/agents/generation_pipeline.py:79-89`](../../backend/archimedes/agents/generation_pipeline.py)),
> and the Strategy Architect was deleted in PR #1074 / commit `ccf4f2f`. Fusion survives
> only as a *step inside* the society (the synthesizer), not as a top-level route.
> **Current record:** [`debate-society-sole-generation-pipeline.md`](debate-society-sole-generation-pipeline.md).
> Retained for the paper-grounding rationale, which the society inherits.

## TL;DR

**Fusion is the primary generation path when it is enabled, an LLM backend is reachable, and the corpus is rich enough.** The engine routes: (1) **Fusion** — synthesize a new strategy by fusing ≥2 user-steered papers from the q-fin corpus; (2) **Architect** (curated-library selection) when fusion is off / corpus sparse; (3) **Agent** (streaming LLM advisor) as the always-available fallback. Fusion is the workhorse because the only durable edge is *combinations the literature has not yet published* — published strategies decay post-publication (McLean & Pontiff 2016). Fusion forces grounding (name the papers), states the novelty rationale, and defers backtest rigor to the external gate.

## Context

"Propose a strategy given what the user wants" has three solutions: free-form LLM (fast, but drifts toward vibes and can hallucinate papers/backtests), curated-library architect (rigorous but slow to onboard, narrow coverage), and fusion (synthesize from ≥2 corpus papers, user-steered, grounded). Before PR #751 the live Generate path computed a `"fusion"` label and **threw it away** — always running the single-agent loop. The dispatch gap meant users never actually got fusion. PR #751 inserts fusion into the dispatch tree as the primary path (checked first), closing the gap.

## Decision

**Fusion-primary dispatch:**
1. ~~**Feature-flagged** (`ARCHIMEDES_FUSION_ENABLED`, default OFF) so ops can disable it without code change; the live path stays deterministic for tests/offline.~~ **SUPERSEDED 2026-09-02 (deck Q4): the flag is retired and fusion is unconditional.** "Ops can disable it" was never true in the useful sense — once the debate society became the sole generation pipeline, disabling fusion did not fall back to anything, it made Generate silently return nothing. Rollback is still a task-definition revision, as below. Guard: [`backend/tests/test_fusion_flag_retired.py`](../../backend/tests/test_fusion_flag_retired.py); row: [`operations/feature-flag-fliplist.md`](../operations/feature-flag-fliplist.md).
2. **Requires a reachable LLM + a sufficiently rich corpus** — otherwise decline with an honest sentinel and fall through to architect/agent (never crash).
3. **User-steered, not free-form** — the `FusionBrief` (asset classes, risk appetite, direction, paper budget) pre-filters the corpus before the model sees it; the model may not introduce papers outside the filtered set.
4. **Output is explicitly pre-backtest** — a `novelty_rationale` + `fusion_reasoning` (which papers, how they combine, why novel), rendered on the passport so the user knows they're evaluating a hypothesis, not a validated strategy. The rigor gate ([rigor-gate-unification](rigor-gate-unification.md)) validates separately.
5. **True-model provenance** — `response.model` (the model that actually served the request) is recorded, not the configured string.

## Consequences

### Positive
- **Targets novelty, the only durable alpha** — searches for unpublished combinations instead of re-implementing already-arbitraged published strategies.
- **Grounded, no vibes** — every proposal names its source papers; anyone can verify the synthesis is faithful. A structural defense against hallucination the free-form path lacked.
- **Clean division of labor** — generation invents (hypothesis); the external rigor gate proves. No conflation of "I synthesized it" with "I proved it."
- **Additive** — architect + agent fallbacks are untouched; with the flag off, behavior is byte-identical to before.

### Negative / costs we accept
- **Requires a corpus + an LLM** — a bare install can't run fusion; it degrades gracefully to architect/agent.
- **Output is a hypothesis, not a guarantee** — the passport/UI must be explicit ("Fusion proposal — under rigor review"), which is correct but means users see "candidate," not "ready to deploy."

## Alternatives considered
- **Architect-primary — rejected** for coverage: the curated library starts at a handful of strategies; fusion can propose from the full corpus.
- **Free-form LLM only — rejected** for credibility: ungrounded output abandons the paper-grounded, verifiable wedge.
- **Run architect + fusion in parallel and blend — rejected** for UX + latency: a single primary path with explicit fallbacks is clearer.

## Ratification

Accepted; wired live via PR #751. Reconciles with [k1-generation-external-rigor-gate](k1-generation-external-rigor-gate.md) (fusion is the K=1 generator) and the empty-corpus risk is tracked separately (the embedding/RAG layer is a quality enhancement, not a blocker for basic end-to-end fusion).
