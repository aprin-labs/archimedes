# Repo Map — Backend / API / Services

Scope: `backend/archimedes/{api,services,agents,chain,models}` + `backend/tests`.
Date: 2026-07-04. Read-only scout pass. HEAD `306408a` (PR #877).

> **Historical snapshot.** Findings may be resolved or invalid on current HEAD. Re-run cited checks before opening work from this report.

## Method

Structural map (`ls`/`wc`), TODO/placeholder/fallback greps, test-reference
cross-check, and targeted reads of claim-integrity hot paths (rigor gate, LLM
backend, strategy metrics serving). No files edited.

---

## Ranked findings

### 1. `portfolio_agent.py` (873 LOC) is production-critical and untested — HIGH

- Evidence: imported by `api/strategies_routes.py`, `agents/generation_pipeline.py`,
  `evaluation/stockbench/adapter.py`, `services/__init__.py` — but no test file
  references it (`grep -rl portfolio_agent tests/` → none). LLM-driven, parses model
  JSON into `AgentPick`/`AgentToolCall`, picks individual instruments.
- Risk: LLM-output-parsing + fallback logic on the live Generate path with zero
  regression coverage. Brittle JSON parsing (`import json, re` at top) is a classic
  silent-failure surface.
- Severity: HIGH (claim-integrity + core vertical, untested).
- Next issue: "APIN - Backend - Add hermetic unit coverage for `portfolio_agent` LLM-output parsing + rule-based fallback"

### 2. `generation_pipeline.py` god-module (2009 LOC) — HIGH (architecture seam)

- Evidence: `agents/generation_pipeline.py` 2009 lines; owns candidate selection,
  backtest+persist, live-gate re-grade, passport refresh, considered-rejects, fixture
  path. 67 `candidate`-related hits; multiple placeholder/fixture branches
  (lines 285, 294, 379, 657 "deterministic stub", 1466, 1621, 1852).
- Risk: single-file blast radius; the K=1/N>1 candidate logic, the `passes_rigor_gate`
  re-grade (lines 1679–1826), and the fixture path all coexist — hard to review, easy
  to regress the #821 live-gate guarantee.
- Severity: HIGH (maintainability + claim-integrity concentration).
- Next issue: "APIN - Backend - Extract candidate-backtest-persist + live-gate re-grade from `generation_pipeline` into a testable seam"

### 3. `chain/agent_runner.py` (1297 LOC) large + thin coverage relative to size — MED/HIGH

- Evidence: 1297 lines; on-chain rebalance authority path. Tests exist
  (`test_agent_runner*.py`, 3 files) but size vs. live-funds risk warrants an explicit
  coverage audit. Imports `services.portfolio_constructor` (active, 200 LOC).
- Risk: on-chain execution seam; oracle/rebalance authority. Bus-factor concentrated
  on Dan per CLAUDE.md risk section.
- Severity: MED-HIGH (contract-adjacent).
- Next issue: "APIN - Chain - Coverage audit + decomposition plan for `agent_runner` (1297 LOC)"

### 4. Dead / duplicated code in `services/_deprecated/` — MED

- Evidence: `services/_deprecated/{kelly_portfolio.py (523), portfolio_constructor.py
  (282)}`. No production importer of `_deprecated`; only `tests/services/test_kelly_portfolio.py`
  imports `_deprecated.kelly_portfolio`. Active `services/portfolio_constructor.py` (200)
  duplicates the deprecated name → import-confusion hazard.
- Risk: tested-but-dead code (kelly) implies maintenance on a module nothing ships;
  duplicated `portfolio_constructor` name invites wrong-import bugs.
- Severity: MED (dead code / duplication).
- Next issue: "APIN - Backend - Remove `services/_deprecated/` (or document why kelly stays) + resolve `portfolio_constructor` name collision"
- **RESOLVED 2026-09-01 (audit P1-1).** `_deprecated/{kelly_portfolio,portfolio_constructor}.py`
  and `tests/services/test_kelly_portfolio.py` were removed earlier; the empty
  `_deprecated/__init__.py` shell was deleted in the dead-module sweep. `services/_deprecated/`
  no longer exists, so the import-confusion hazard is gone with it. The rest of this section is
  the point-in-time record from when the map was taken.

### 5. Stubbed metrics still served when no real backtest — MED (claim-integrity, needs UI confirm)

- Evidence: `api/strategies_routes.py:119–130` serves `stub_sharpe/stub_cagr/...`
  (from `BACKTEST_*` fixture constants in `strategy_provider.py:301–307`) whenever
  `has_real` is false; only signalled via `is_backtest_placeholder=not has_real`
  (line 137). The badge (`passes_rigor_gate`) is correctly live/tri-state (#821), but
  the *numbers* are still fixture stubs.
- Risk: a consumer that ignores `is_backtest_placeholder` renders fixture Sharpe/CAGR
  as if real — the exact #1-rule pattern. Integrity depends entirely on every UI
  surface honoring the flag.
- Severity: MED (mitigated by flag; residual if any consumer ignores it).
- Next issue: "APIN - Backend - Audit every consumer of stub_* metrics honors `is_backtest_placeholder`; consider nulling stubs at the API boundary"
- Clarify: is the frontend contractually required to gray-out placeholder metrics? Any pitch/screenshot showing stub numbers?

### 6. Untested service modules — MED

- Evidence (no test reference): `services/stress_engine.py` (394+ LOC, feeds
  `portfolio_agent` + risk UI), `services/user_stats.py`, `services/corpus_categories.py`,
  `services/_fusion_helpers.py`, `agents/portfolio_agent.py` (see #1).
- Risk: `stress_engine` powers user-facing tail-risk numbers with no coverage;
  `_fusion_helpers` backs the multi-paper fusion path.
- Severity: MED.
- Next issue: "APIN - Backend - Backfill hermetic tests for `stress_engine`, `user_stats`, `_fusion_helpers`"

### 7. Broad `except Exception` fail-open/fallback density — MED (needs telemetry audit)

- Evidence: `portfolio_optimizer.py` (7+ broad excepts, several `# defensive`/`pragma:
  no cover`, lines 522/605/682/711/999/1094/1154); `amm_bootstrap.py` `$5/pool`
  fallback (lines 70–73); `fusion_market_data.py:199`; `price_source.py:123`.
  `live_rigor_gate.py` deliberately fails *closed* to `pending` (good pattern).
- Risk: silent degradation where a fallback substitutes for real data without loud
  telemetry. `test_loud_fallback_telemetry.py` exists — confirm it covers these paths,
  not just the rigor gate.
- Severity: MED.
- Next issue: "APIN - Backend - Audit broad-except fallbacks in optimizer/market-data for loud-fallback telemetry parity"

### 8. `.env.example` provider default stale vs live (`anthropic_compatible` vs `bedrock_converse`/Nova) — LOW (known, T3.10)

- Evidence: CLAUDE.md notes `.env.example` still defaults `LLM_PROVIDER=anthropic_compatible`;
  `llm_backend.py` DEFAULT_CONVERSE_MODEL=`amazon.nova-micro-v1:0`. Legacy `ANTHROPIC_*`
  path still live (`_legacy_backend`).
- Risk: cold-clone dev hits legacy path; provenance-of-record confusion.
- Severity: LOW (tracked).
- Next issue: "APIN - Infra - Align `.env.example` LLM defaults with live bedrock_converse/Nova (T3.10)"

### 9. `scripts/run_kb_pipeline.py` raises `NotImplementedError` — LOW (expected, gated on infra #147/#151)

- Evidence: `scripts/run_kb_pipeline.py:98-99`. Corpus page returns 503 until artifact
  exists (by design per CLAUDE.md).
- Severity: LOW (documented empty-state). Note only; no action unless KB infra lands.

---

## Positive notes (don't regress)

- `services/live_rigor_gate.py` (#821): tri-state `pass|fail|pending`, fail-closed,
  reuses `run_rigor_gate` — the single-source-of-truth pattern the #1 rule demands.
  `strategies_routes.py` wires it (lines 40–43, 135, 219, 493). Do not reintroduce the
  fixture `passes_rigor_gate` boolean as a badge source.
- `CannedBackend` is honest: `available=False`, `model_id="canned-fallback"`, returns
  `{"fallback": true}` — no fabricated reasoning. `FREE_TIER_MODELS` allowlist is
  server-side defense-in-depth (`is_allowed_model`).
- `swap_routes_error_leak` + `user_profile_privacy` + `security_hardening` tests show
  active error-leak/PII discipline.

## Architecture seams worth naming

- Strategy metric serving: `strategy_provider` (stub source) → `strategies_routes._to_strategy_response`
  (real|bt|stub cascade) → `live_rigor_gate` (badge). The cascade logic is repeated
  inline (lines 119–130) — a candidate for a single `resolve_metric(real, bt, stub)` helper.
- LLM seam: `llm_backend.make_llm_backend` (Protocol `LLMBackend`) consumed by
  `strategy_architect`, `strategy_fusion`, `debate_engine`, `portfolio_agent`. Clean seam;
  each has an offline placeholder backend — good for hermetic tests, but portfolio_agent's
  isn't exercised (#1).
- Multi-agent (Lepton Tier 1): `debate_engine.py` (753) + `generation_pipeline` n_candidates
  (default 1). Only wired through generation_pipeline — confirm N>1 diverse-candidate path
  is actually reachable on the live Generate request, not just fixture.

## Start here

`api/strategies_routes.py:60–150` (`_to_strategy_response`) — the confluence of live
rigor gate (#821), stub-vs-real metric cascade (#5), and placeholder flag. It's the
single richest claim-integrity surface and the best entry to trace data flow outward.

## Clarification questions

1. ~~Is `services/_deprecated/kelly_portfolio.py` intentionally retained (tested) or
   should it + its test be deleted?~~ (Finding #4) — **Answered: deleted.** Module and test
   are gone; the empty `_deprecated/` package shell followed on 2026-09-01 (audit P1-1).
2. Does every frontend metric panel honor `is_backtest_placeholder`, or do any render
   stub Sharpe/CAGR as real? (Finding #5 — blocks severity call)
3. Is the N>1 multi-agent candidate path (`n_candidates` > 1) live on the Generate
   request path yet, or still default-1 / fixture-only? (affects Lepton Tier-1 claim)
4. Does `test_loud_fallback_telemetry.py` cover the optimizer/market-data fallbacks
   (#7), or only the rigor gate?
5. Is `agent_runner.py` (1297 LOC) in scope for decomposition, or frozen as
   contract-adjacent (Dan/Bogdan-only)? (Finding #3 — sets whether to file an issue)
