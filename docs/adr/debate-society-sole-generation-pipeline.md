# ADR: The debate society is the sole generation pipeline

> **Audience:** Archimedes team
> **Status:** Accepted
> **Date:** 2026-07-09 (architect deleted, `ccf4f2f` / PR [#1074](https://github.com/aprin-labs/archimedes/pull/1074), merged 2026-07-14)
> **Owner:** Dan Browne
> **Supersedes:** [`fusion-primary-generation.md`](fusion-primary-generation.md)
> **Superseded-by:** —
> **Question being decided:** Does generation keep routing between fusion / architect / agent paths, or does the multi-agent debate society own candidate generation outright?
> **Related:** [`backend/archimedes/agents/generation_pipeline.py:79-89`](../../backend/archimedes/agents/generation_pipeline.py), [`backend/archimedes/agents/debate_engine.py`](../../backend/archimedes/agents/debate_engine.py), [`docs/specs/multi-agent-debate-spec.md`](../specs/multi-agent-debate-spec.md), [`k1-generation-external-rigor-gate.md`](k1-generation-external-rigor-gate.md).

## TL;DR

**There is one generation path: the debate society.** `_pick_pipeline()` returns `"debate"`
unconditionally; a client-sent `mode_override` is accepted for API compatibility and routes
nothing. The Strategy Architect is deleted. The `ARCHIMEDES_DEBATE_ENABLED` flag is retired.
When the society cannot produce a conformant candidate, generation emits
`GENERATION_UNAVAILABLE` — **there is no silent fallback.**

## Context

[`fusion-primary-generation.md`](fusion-primary-generation.md) described a three-way
routing tree — fusion when enabled and the corpus was rich enough, the curated-library
Architect when it was not, a streaming LLM agent as the always-available fallback (issue
#167). Three problems with that shape:

1. **Fallbacks hide failure.** A route that silently degrades from fusion to a free-form
   LLM advisor produces output that looks like a generated strategy but carries none of the
   paper grounding the product claims. The user cannot tell which path ran. That is the
   same class of defect as the fake-strict rigor badge that
   [`rigor-gate-unification.md`](rigor-gate-unification.md) exists to close.
2. **Three paths, three sets of semantics.** Provenance, candidate-pool size and rigor
   inputs all differed by route, which meant every downstream consumer had to be
   route-aware — including, critically, the DSR trial count (see
   [`num-trials-self-containment.md`](num-trials-self-containment.md)).
3. **The society subsumes the alternatives.** The multi-agent debate society
   ([`docs/specs/multi-agent-debate-spec.md`](../specs/multi-agent-debate-spec.md)) runs
   proposers against deterministic critics and a synthesizer. Fusion did not disappear —
   it became a *step inside* the society. The Architect's curated-library selection had no
   remaining job once the society could propose from the corpus.

The spec's own plan was **additive first, delete later**: Phase 1 added the `"debate"`
branch behind `ARCHIMEDES_DEBATE_ENABLED` (default OFF) leaving every legacy runner
untouched; the deletions were explicitly deferred to a separate Phase-3 cutover PR after
the society was verified on the live path.

## Decision

**Execute the Phase-3 cutover: the society is the pipeline.**

1. **Routing is removed, not defaulted.**
   [`generation_pipeline.py:79-89`](../../backend/archimedes/agents/generation_pipeline.py)
   returns `("debate", "debate society is the generation pipeline (T1.1 Phase-3 cutover)")`
   unconditionally. A legacy `mode_override` is logged and ignored rather than rejected, so
   existing clients do not break.
2. **The Strategy Architect is deleted** — `ccf4f2f`, "[cleanup] Remove obsolete Strategy
   Architect — debate society is the sole generation path (#1064)", PR
   [#1074](https://github.com/aprin-labs/archimedes/pull/1074). Deleted, not deprecated: a dead
   route left in the tree is a route someone re-enables.
3. **`ARCHIMEDES_DEBATE_ENABLED` is retired**
   ([`debate_engine.py:6,205`](../../backend/archimedes/agents/debate_engine.py)). The flag
   existed to make Phase 1 additive; once the society is the only path, a flag that can turn
   generation off is a footgun, not a control. Residual `_pick_pipeline` vestiges and the
   dead `chainlink_covered` field were swept in `7f3a29c` (#834 flag audit).
4. **Failure is explicit.** `debate_engine.DebateUnavailable` (a subclass of
   `FusionUnavailable`) is raised when no proposal survives the critics; `run_generation`
   catches it and emits `GENERATION_UNAVAILABLE`. No fallback route runs.

This is compatible with [`k1-generation-external-rigor-gate.md`](k1-generation-external-rigor-gate.md):
the society still emits **one** winner plus considered-rejects, and the rigor gate still
runs externally on the passport.

## Consequences

### Positive
- **One path means one set of semantics.** Provenance, candidate-pool size and rigor inputs
  are defined once. This is what made the `num_trials` self-containment decision expressible
  at all — "the pool the search actually considered" is only well-defined when there is one
  search.
- **No silent degradation.** A user either gets a society-generated, critic-survived
  candidate or an explicit `GENERATION_UNAVAILABLE`. The product's paper-grounding claim
  stays true on the live path.
- **Large deletion.** The routing tree, the Architect, and the flag plumbing are gone.

### Negative / costs we accept
- **Availability is now the society's availability.** With no fallback, an LLM-backend
  outage or a critic set that rejects everything is a hard generation failure with a
  user-visible error. This is the intended trade — an honest error over a dishonest answer —
  but it is a real reduction in apparent uptime.
- **The critics are the single gate on output quality.** Their strictness now has no
  release valve; tuning them too tight raises the `GENERATION_UNAVAILABLE` rate directly.
- **Higher cost per Generate.** A multi-agent debate makes more LLM calls than single-shot
  fusion did.
- **`mode_override` is a lie by omission.** The API still accepts it and quietly ignores it.
  It is logged, but a client author reading only the schema would think it works. It should
  be removed at the next API version bump.
- **[`fusion-primary-generation.md`](fusion-primary-generation.md) is retained**, marked
  superseded, because its paper-grounding rationale (published strategies decay
  post-publication; the durable edge is unpublished combinations) is inherited by the
  society and is still the reasoning of record.

## Alternatives considered
- **Keep the routing tree, default to debate — rejected.** It preserves exactly the silent
  fallback the cutover exists to remove, and keeps three sets of semantics alive to serve a
  path nobody selects.
- **Keep the Architect for a curated-library route — rejected.** Curated single-paper
  implementations are a *library* concern, not a generation route; they are surfaced through
  the library and Marketplace, and their rigor is graded self-containedly (see
  [`num-trials-self-containment.md`](num-trials-self-containment.md)).
- **Keep the flag for emergency rollback — rejected.** Rollback is a task-definition
  revision ([`ec2-to-ecs-fargate-cutover.md`](ec2-to-ecs-fargate-cutover.md)), not a runtime
  flag that changes what the product claims about its own output.

## Addendum — 2026-08-31: the last bypass is gone

*Added, not a rewrite. The decision above is unchanged; this records that the tree
finally matches it.*

The Phase-3 cutover deleted the routing tree and the Architect, but it left one
generation path standing outside the society: **`POST /api/strategies/generate` →
`_run_fusion_job`** in `api/strategies_routes.py`, with `GET
/api/strategies/generate/{job_id}` as its poll partner. It was live, account-gated,
LLM-spending, and gated only on `ARCHIMEDES_FUSION_ENABLED` — which is set to `true`
in `infra/ecs.tf`, `docker-compose.yml` and `docker-compose.production.yml`, so it was
enabled in every deployed environment. Its own docstring called it "a second live,
SIWE-gated, LLM-spending generation endpoint". `docs/specs/multi-agent-debate-spec.md`
§ Phase 3 had listed it for deletion; that deletion had not happened.

**Removed 2026-08-31 by owner decision** (Dan Browne): the route, its poll partner,
the `_run_fusion_job` background worker, the `default_fusion()` factory that existed
only to serve it, and their tests. `StrategyFusion.propose` now has exactly one caller
in the tree — `agents/debate_engine.py::_propose_pool` — which is what "fusion is a
step inside the society" was always supposed to mean.

**The ADR now has a test.** `backend/tests/test_sole_generation_route_guard.py` walks
the live FastAPI route table and the package's import graph and fails if either a
generation route reappears under `/api/strategies` or any module outside
`agents/debate_engine.py` imports the fusion proposer. It was demonstrated failing
against a restored route before it was committed.

**Not removed here: `ARCHIMEDES_FUSION_ENABLED`.** The flag was not the bypass's
flag. It also guarded `StrategyFusion.propose` itself (`agents/strategy_fusion.py`),
which the society's proposer pool calls, and it was published on `/health` as
`fusion_enabled`. Deleting it *then* would have made the society's proposers return
inert sentinels — a live-path behaviour change, not a cleanup. **That separate
decision was taken on 2026-09-02 (deck Q4): the flag is retired and fusion is
unconditional.** Nothing about the reachability argument changed — that is exactly
why it went. The OFF branch was deleted rather than defaulted ON, the `/health`
`fusion_enabled` key was dropped on 2026-09-03 rather than frozen at a constant
`true` (no consumer was found), and
[`backend/tests/test_fusion_flag_retired.py`](../../backend/tests/test_fusion_flag_retired.py)
fails if the switch returns under any name.
