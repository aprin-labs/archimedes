# ADR: Portfolio-constructor consolidation — retire legacy paths, activate dual-signal sizing

> **Audience:** Archimedes team (decision owner: Önder, portfolio math; Dan, agent integration)
> **Status:** Accepted
> **Date:** 2026-06-26 (recorded; decision date [unestablished — needs Dan])
> **Owner:** Önder Akkaya
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** Which portfolio-construction path is canonical — the legacy `kelly_portfolio` + old generic `portfolio_constructor`, or a new dual-signal implementation that throttles by market regime × ensemble consensus?
> **Related:** [#659](https://github.com/aprin-labs/archimedes/issues/659) (consensus rename), [#660](https://github.com/aprin-labs/archimedes/issues/660) (regime detector), `backend/archimedes/services/portfolio_constructor.py`, `backend/archimedes/chain/agent_runner.py`.

## TL;DR

**Retire the legacy portfolio constructors (no production callers) to `services/_deprecated/`, and activate a NEW `PortfolioConstructor` that implements dual-signal position sizing.** Note the nuance: `portfolio_constructor.py` still exists and is used today — what was retired is the *legacy* generic constructor + `kelly_portfolio`; what's live is the new dual-signal one. It throttles risk-asset exposure by `position_scale = regime_mult × consensus_mult ∈ [0, 1]`, moving freed mass into USDC — unifying two orthogonal risk signals (macro environment vs strategy conviction) into one conservative, non-circular sizer.

## Context

Two earlier refactors collide here, so precision matters:
1. **Phase 7 (issue #131, commit a4a09fb)** moved the *legacy* generic `portfolio_constructor.py` + `kelly_portfolio.py` to `services/_deprecated/`. Both had **zero production call sites** (only test imports) — dead code from an earlier design phase. The `IPortfolioConstructor` Protocol in `interfaces/` was kept as a stable seam.
2. **Dual-signal wiring (issue #662, commit c74e825)** then built a *new*, actually-called `PortfolioConstructor`. The agent runner already computed two independent risk signals every tick — the exogenous **market regime** (#660) and the endogenous **ensemble consensus** (#659) — but neither fed position sizing; raw aggregated weights mapped straight to vault allocations with no throttle.

## Decision

**Retire the legacy constructors and route sizing through the new dual-signal `PortfolioConstructor`:**
- Computes `regime_mult ∈ [0,1]` (lower in high-risk regimes) and `consensus_mult ∈ [0,1]` (higher with stronger ensemble agreement), then `position_scale = regime_mult × consensus_mult`.
- Throttles raw weights by `position_scale`, routes freed mass to USDC (the safe asset), renormalizes to sum 1.0.
- **Degrades gracefully** — if either signal is unavailable, defaults to conservative scaling rather than crashing, so the agent stays live.
- Preserves a hard USDC safe-asset floor so even 0× signal scaling never de-risks below the floor.
- Extends `IPortfolioConstructor` with optional keyword-only params so the Protocol stays stable (no existing caller breaks).

## Consequences

### Positive
- **Removes dead code** and clarifies the canonical path (the `_deprecated/` move answers "which one does the agent use?").
- **Closes the measure-but-ignore gap** — the agent was computing regime + consensus but not using them to size; now they throttle exposure.
- **Non-circular, conservative** — multiplying two orthogonal [0,1] signals shrinks exposure if *either* warns; matches regime-conditional Kelly sizing (Lo 2002; López de Prado 2018 §11).
- **More credible on-chain traces** — each rebalance trace can carry the regime + consensus multipliers as a paper-grounded sizing justification.

### Negative / costs we accept
- **Two detector dependencies** — needs both the regime detector (#660) and ensemble consensus (#659) live; degrades to conservative scaling if one is down (ops should monitor both).
- **Multiplicative, not additive** — `0.7× × 0.8× = 0.56×`, not `0.75×`. Correct Kelly behavior, but can feel aggressive to risk managers used to linear blending; the safe-asset floor + drift rebalancing keep the agent from being fully parked.

## Alternatives considered
- **Keep the legacy paths alive — rejected:** dead code is a liability; one canonical implementation is clearer.
- **Regime-only (ignore consensus) — rejected:** regime is macro, consensus is the ensemble's own conviction; ignoring consensus defeats the ensemble's purpose.

## Ratification

Decided; legacy retired (Phase 7, #131) and the dual-signal path live (#662). The deprecated modules were kept in `services/_deprecated/` for a release cycle and hard-deleted once a full test pass confirmed no survivors.

> **Note 2026-09-01 (audit P1-1):** the release cycle is over. `kelly_portfolio.py` and the
> legacy `portfolio_constructor.py` were removed earlier; the last thing left in the folder
> was an empty `__init__.py`, and that package shell is now deleted too. There is no
> `services/_deprecated/` in the tree any more — the paragraph above is the historical record
> of how it got there, not a description of the current layout.

This is the regime layer's *execution* surface; regime-aware *rigor* (per-regime Sharpe robustness) is surfaced separately in the rigor gate.
