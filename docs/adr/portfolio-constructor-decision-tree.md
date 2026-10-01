# Portfolio Constructor Decision Tree

> **Status:** Accepted
> **Date:** 2026-05-22
> **Owner:** Önder Akkaya
> **Supersedes:** —
> **Superseded-by:** —
> [Spine+ v2 plan](../plans/spine-plus-v2-plan.md). Closes survey gap cluster #5
> (load-bearing) from [`chuan-architecture-survey.md`](../archive/agora-2026-05/chuan-architecture-survey.md).
>
> **Lineage:** Day-10 introduced `portfolio_agent.py` (850 lines) as an
> LLM-agentic top-level constructor. Three pre-Day-10 deterministic constructors
> remained in the tree:
> - [`services/portfolio_constructor.py`](../../backend/archimedes/services/portfolio_constructor.py) (285 lines)
> - ``services/kelly_portfolio.py`` (deleted in #1259) (505 lines)
> - [`services/portfolio_optimizer.py`](../../backend/archimedes/services/portfolio_optimizer.py) (488 lines)
>
> Most have **zero production call sites** today. This spec decides who fires
> when, and which files become formally retired.
>
> **Note added 2026-08-31 (not a rewrite of the decision below).** The
> "top-level constructor" row for `portfolio_agent.py` describes a tool-calling
> loop (`MAX_AGENT_ITERATIONS=12`, `AgentPortfolio` with a tool trace) that no
> longer exists. That loop — `propose_portfolio_with_tools()` and its
> `get_asset_stats` / `get_correlation` / `stress_test_portfolio` tools — was
> found to have zero callers and was deleted. What remains in
> `agents/portfolio_agent.py` (note: `agents/`, not `services/`) is a
> **single-turn** LLM constructor, `PortfolioAgent.propose_portfolio`, whose only
> caller is the StockBench benchmark harness; the user-facing Generate flow is
> owned by the debate society, per
> [`debate-society-sole-generation-pipeline.md`](debate-society-sole-generation-pipeline.md).
> The decision recorded below (which constructor is the keeper, which are
> retired) is unaffected and stands as written.

## Current call-site reality (verify before editing)

```bash
# In repo root:
$ grep -rn "PortfolioConstructor()\|KellyRiskParityConstructor\|get_portfolio_agent\|optimize_weights\|compute_efficient_frontier" \
    backend/archimedes/api/ backend/archimedes/chain/ --include="*.py"
```

Findings as of 2026-05-22:

| Constructor | Production callers | Status |
|---|---|---|
| `portfolio_agent.PortfolioAgent` (Day-10) | `api/routes.py:677` (agent recommend endpoint) | **Active — top-level** |
| `portfolio_optimizer.optimize_weights` + `compute_efficient_frontier` | `api/routes.py:509, 1056, 1259` (efficient frontier viz + agent stats) | **Active — leaf** |
| `portfolio_constructor.PortfolioConstructor` | None in `api/` or `chain/`. Imported only via `interfaces/`. | **Dead in production** |
| `kelly_portfolio.KellyRiskParityConstructor` | None in `api/` or `chain/`. Test-only. | **Dead in production** |

This is the gap: two deterministic constructors are kept alive by the
`IPortfolioConstructor` Protocol but no production code instantiates them.
The agentic path (`portfolio_agent`) replaced the orchestrator role; the pure-MVO
path (`portfolio_optimizer`) survives because the UI's efficient frontier chart
needs it.

## Decision tree

```
Entry point
    │
    ├── /generate (user brief)
    │       ▼
    │   portfolio_agent.PortfolioAgent.recommend()
    │       │
    │       ├── (internally tool-calls)
    │       │       └── portfolio_optimizer.optimize_weights()  ← used as a tool
    │       │
    │       └── returns AgentPortfolio
    │
    ├── /api/agent/recommend (cron / test harness)
    │       ▼
    │   portfolio_agent.PortfolioAgent.recommend()
    │       (same path as above)
    │
    ├── /portfolio efficient frontier chart
    │       ▼
    │   portfolio_optimizer.compute_efficient_frontier()
    │
    └── /strategy/:id passport "what would these weights look like?"
            ▼
        portfolio_optimizer.optimize_weights()
```

### One rule

**`portfolio_agent.PortfolioAgent` is the only top-level constructor.** It is
the LLM-agentic Day-10 implementation that owns the user-facing Generate flow
and the agent runner tick. Everything else is either a tool it calls or a
visualization helper.

### Constructor roles

| File | Role | Lifetime |
|---|---|---|
| `services/portfolio_agent.py` | **Top-level constructor.** Receives user brief or regime tick, runs ≤ `MAX_AGENT_ITERATIONS=12` LLM iterations with tool-calling, produces `AgentPortfolio` with weights + reasoning + trace. Owns regime handling. | Keep — actively developed |
| `services/portfolio_optimizer.py` | **Math leaf.** Pure NumPy/cvxpy MVO + efficient frontier. Pure-function API (`optimize_weights(mu, sigma, constraints) → weights`). Called by `portfolio_agent` as a tool **and** by UI endpoints for visualization. | Keep — Önder's clean math primitive |
| `services/portfolio_constructor.py` | **Deprecated orchestrator** (pre-Day-10). Was the old regime → weights router; superseded by `portfolio_agent`. | **Retire** in Phase 4 (move to `services/_deprecated/` or delete after one release cycle confirms no consumers) |
| `services/kelly_portfolio.py` | **Deprecated Kelly path** (pre-Day-10). Test-only. Math primitives (Kelly fraction calc, risk-parity weighting) are partially duplicated in `portfolio_optimizer.py`. | **Retire** in Phase 4; lift any unique math into `portfolio_optimizer.py` first |

### Composability question, answered

> Are `kelly_portfolio.py` and `portfolio_optimizer.py` alternatives, or
> composable layers?

**Neither — they're partially-overlapping legacy.** `portfolio_optimizer.py` is
the keeper. Any Kelly-specific sizing logic worth saving moves into it as a
function (e.g., `kelly_size(weights, edge_estimates) → scaled_weights`) before
`kelly_portfolio.py` retires. Don't keep both alive "for composability" — that's
the trap that produced this gap.

### LLM unavailability fallback

If `ANTHROPIC_API_KEY` is missing or the LLM call fails:

- `portfolio_agent.PortfolioAgent` raises a typed exception (`AgentUnavailableError`).
- The caller (typically the Generate route) catches it and either:
  - Returns a clear error to the user ("Strategy generation requires LLM connectivity; retry in a moment"), **or**
  - Falls back to a deterministic baseline: equal-weight across the regime's
    asset class, computed via `portfolio_optimizer.optimize_weights` with
    identity covariance.

**The fallback is not a route to the old `PortfolioConstructor`.** That path
is dead.

## The `_DRIFT_THRESHOLD` constant — canonical value

Both surviving callers use **`0.05` (5% absolute weight deviation)** as the
rebalance trigger. Verified:

- `services/portfolio_constructor.py:60` → `_DRIFT_THRESHOLD = 0.05`
- `services/kelly_portfolio.py:53` → `_DRIFT_THRESHOLD = 0.05`
- `services/statistical_regime.py:42` → `_MA_DRIFT_THRESHOLD = 0.03` (separate
  semantic — moving-average deviation, not portfolio drift; do not unify)

The earlier plan-doc claim of "0.15 vs 0.05" was incorrect; the two values
match. **Canonical:** `0.05` for portfolio drift. When the deprecated constructors
retire, move the constant to a single home (`services/portfolio_optimizer.py`
or a new `services/constants.py`).

## The `pick_constructor()` helper

Given that there is now exactly one top-level constructor, the helper the
original plan proposed (`pick_constructor(mode, llm_available, regime)`) is
**not needed**. Replace with a flat call:

```python
# Anywhere a portfolio decision is needed:
from archimedes.services.portfolio_agent import get_portfolio_agent
agent = get_portfolio_agent()
result = await agent.recommend(brief=..., regime=...)  # raises AgentUnavailableError if LLM down
```

If we ever re-introduce a deterministic top-level constructor (e.g., for a
"no-LLM mode" CI fixture), revisit this and add the helper. Don't add it
preemptively.

## Phase 2 instrumentation hook

The SSE generation stream (`generation-streaming-spec.md`) emits events
per LLM iteration **inside `portfolio_agent.PortfolioAgent.recommend()`**. The
emit point is a callback parameter passed by the route handler, so the
constructor doesn't reach up into FastAPI:

```python
async def recommend(self, brief, regime, *, emit: Callable[[str, dict], Awaitable[None]] | None = None):
    ...
    if emit: await emit("agent_iteration", {"iteration_n": i, ...})
```

Phase 2 wires `emit` to the SSE channel.

## Acceptance

A reviewer can answer each of these from this doc alone:

- Which constructor fires when the user clicks Generate? → `portfolio_agent`
- Which constructor fires on the agent runner's 5-min tick? → `portfolio_agent`
- Where does the efficient frontier UI data come from? → `portfolio_optimizer.compute_efficient_frontier`
- What happens if the LLM is down? → `AgentUnavailableError`, optional deterministic fallback via `portfolio_optimizer`
- Which files are formally retired in Phase 4? → `portfolio_constructor.py` and `kelly_portfolio.py`

## Open questions

1. **Soft-retire vs hard-delete** the two deprecated files — move to
   `services/_deprecated/` for one release, or delete outright? (Recommendation:
   move; CI green for one PR cycle, then delete.)
2. **Equal-weight fallback location** — should the deterministic LLM-unavailable
   fallback live in `portfolio_agent.py` or in the route handler? (Recommendation:
   route handler — keeps the agent module purely about LLM, fallback is a
   policy decision the route makes.)
3. **Kelly math worth lifting** — is there sizing logic in `kelly_portfolio.py`
   that isn't already in `portfolio_optimizer.py`? Önder to verify before we
   delete.

## Resolution note — 2026-09-01 (audit P1-1)

Open question 1 (**soft-retire vs hard-delete**) is closed: the recommendation was
followed. Both files moved to `services/_deprecated/` (#131), stayed there for a
release cycle, and were then deleted. The empty `_deprecated/` package shell was
removed on 2026-09-01 as part of the dead-module sweep, so `services/_deprecated/`
no longer exists — the Lifetime column above and the open questions are the
historical decision record, not the current tree.
