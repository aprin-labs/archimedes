# T1.1 — Multi-Agent Debate Engine Spec (v2, replacement-scope)

**Status:** SHIPPED 2026-07 — the debate society is the sole generation pipeline. This document is retained as design rationale, not as a build instruction. · **Owner:** Dan Browne · **Track:** Lepton Tier-1 vertical (Agentic Sophistication, 30%)
**Slot-in:** `pipeline_name="debate"` runner in `backend/archimedes/agents/generation_pipeline.py` (`run_generation` dispatch)
**Last verified against source:** 2026-06-28 (branch `dbrowneup/t1.1-debate-spec`); §2's prompt section re-verified and corrected 2026-09-01 against `9cb868eb` — see [`prompt-inventory.md`](prompt-inventory.md) (#1800 / #1801)

This is a complete, self-contained, build-ready design. It is the **v2 replacement-scope rewrite** that resolves PR #766. The earlier draft framed the debate as a *fourth, flag-gated peer* alongside fusion/architect/agent with a byte-identical live path. **Dan's call is the opposite: the debate society becomes the only generation pipeline, fusion folds in as an internal capability, and the architect + single-agent paths are deleted.** The flag exists for the *cutover window* (default OFF until the replacement is verified on the live path), and the end state has no legacy generation path to guard — **but the deletions are deferred to a separate cutover PR (Phase 3); Phase 1 is strictly additive** (see §5a and the delete-vs-byte-identical resolution below).

> **Why version refs, not line numbers.** PR #766 demonstrated empirically that raw `file.py:NNN` anchors rot — the prior draft's own anchors had already drifted from HEAD. This spec references **symbols** (functions, classes, fields, dataclass attributes). Where a single permalink is useful, pin a commit SHA. Every symbol below was re-verified against the working tree.

---

## 0. Source-of-truth corrections (verified against the code, not the briefs)

The v1 draft and the upstream current-state map carried four source-accuracy errors. All four are corrected here and the corrections are load-bearing.

1. **`.is_actionable` lives on `FusionProposal`, not `FusionEvalResult`.** `FusionProposal.is_actionable` (in `agents/strategy_fusion.py`) is a property: `status == "ok" and len(source_arxiv_ids) >= MIN_PAPERS` (MIN_PAPERS = 2). It gates **proposal viability** (did we get a parseable, paper-grounded spec). `FusionEvalResult` (in `services/fusion_evaluator.py`) exposes only `.success` (`error is None and backtest is not None`) and `.admissible` (`rigor is not None and rigor.admissible`, where admissible = `passing AND data_source != "synthetic"`). It gates **post-backtest rigor**. The debate uses both, at different stages: `FusionProposal.is_actionable` to admit a candidate into the pool, `FusionEvalResult.success`/`.admissible` to rank it after the real backtest.

2. **There is no `embargo_filter()` / `time_aware_retrieval()` callable.** The Xia Outcome-Embargo (§4.2 protocol 1) and Time-Aware-Retrieval (protocol 2) are **enforced inside `load_papers_from_db`** (`services/corpus_service.py`), which calls `apply_outcome_embargo(papers, embargo_days=...)` and `apply_time_aware_retrieval(papers, lam=regime_lambda(...))` — the real implementations live in `services/embargo_filter.py` (`apply_outcome_embargo(papers, *, at=None, embargo_days=30)`, keyword-only) and `services/time_aware_retrieval.py` (`apply_time_aware_retrieval(papers, *, now=None, lam=..., score_field="similarity")`, keyword-only). The debate does **not** re-derive embargo/decay; it **consumes** them by calling `load_corpus()` → `load_papers_from_db()`, which applies both by default. There is no standalone `time_aware_retrieval(corpus, regime, λ)` signature.

3. **`dict` ↔ `CorpusPaper` boundary, pinned.** `load_papers_from_db(...) -> list[dict]` (rows `.to_dict()`'d, embargo+decay applied). `load_corpus(path=None) -> list[CorpusPaper]` (`agents/strategy_fusion.py`) is DB-first: it calls `load_papers_from_db()` and **maps the dicts → `CorpusPaper`**, falling back to the manifest file only when the DB is empty. **The debate evidence surface takes `list[CorpusPaper]`** (it calls `load_corpus()`), and `select_candidates(brief, corpus, regime_bias=...)` consumes `list[CorpusPaper]` → returns `list[CorpusPaper]`. The dict form never crosses into the debate; the conversion happens once, inside `load_corpus()`.

4. **`strategy_fusion.py` is in `agents/`, not `services/`.** Full path: `backend/archimedes/agents/strategy_fusion.py`. (`regime_robustness_score` is *defined* in `services/_rigor_helpers.py` and only *re-exported* through `services/rigor_evaluator.py` — cite the helper as source of truth.)

**Key reused symbols (current working tree):**

| Symbol | Module | Role in the debate |
|---|---|---|
| `run_generation(*, job_id, brief, n_candidates, store, mode, model, dual_regime)` | `agents/generation_pipeline.py` | async pipeline entry; dispatches on `pipeline_name`; owns the persist/backtest/episodic tail |
| `_pick_pipeline(brief, mode_override) -> (name, reason)` | `agents/generation_pipeline.py` | pipeline selector — **gains a flag-gated `"debate"` branch** (§5); legacy tree untouched while OFF |
| `_CandidateResult` (`@dataclass`) | `agents/generation_pipeline.py` | the carrier contract every runner returns; fields incl. `passes_rigor`, `has_real_rigor`, `rigor_verdict`, `return_series`, `source_arxiv_ids`, `generation_method` |
| `_patch_pbo(candidates)` | `agents/generation_pipeline.py` | library PBO over **non-`has_real_rigor`** candidates only |
| `select_candidates(brief, corpus, regime_bias=None)` | `agents/strategy_fusion.py` | deterministic pre-LLM paper selection; `regime_bias ∈ {"bull","bear",None}` boosts `_REGIME_BIAS_TERMS`; **ranks by integer keyword-hit count first, then recency, then arxiv_id** (the diversity-theater risk — §4, fix A4) |
| `StrategyFusion.__init__(...)` · `StrategyFusion._resolve_backend(...)` | `agents/strategy_fusion.py` | constructor + backend resolver — **the model-threading edit sites** (§8 item 10, fix A3) |
| `StrategyFusion.propose(brief) -> FusionProposal` | `agents/strategy_fusion.py` | one LLM synthesis → DSL `strategy_spec`; anti-hallucination `valid_ids` drop |
| `default_fusion() -> StrategyFusion`, `MIN_PAPERS=2`, `FUSE_TARGET_MIN=5`, `FUSION_MAX_PAPERS=30`, `DEFAULT_MAX_PAPERS=8`, `fusion_enabled()` | `agents/strategy_fusion.py` | fusion factory + constants. **`default_fusion()` returns `StrategyFusion()` with NO model arg — model-blind today** (the gap A3 closes). #1636 split the one overloaded knob into three: `MIN_PAPERS` still hard-rejects, `FUSE_TARGET_MIN` is what the prompt asks for (a justified shortfall is accepted and logged, never blocked), `FUSION_MAX_PAPERS` is retrieval width |
| `load_corpus(path=None) -> list[CorpusPaper]` | `agents/strategy_fusion.py` | DB-first corpus load (embargo+decay already applied inside) |
| `load_papers_from_db(*, embargo_days, decay_lambda, regime, apply_embargo, apply_decay) -> list[dict]` | `services/corpus_service.py` | where Xia 1 + Xia 2 are enforced |
| `evaluate_fusion_spec(spec_dict, *, data_feed=None, num_trials=None) -> FusionEvalResult` | `services/fusion_evaluator.py` | validate → interpret → DSL backtest → rigor gate |
| `FusionEvalResult{spec, backtest, rigor, error}` · `.success` · `.admissible` | `services/fusion_evaluator.py` | post-backtest rigor verdict |
| `run_rigor_gate(strategy_id, daily_returns, num_trials=1, ...) -> RigorGateResult` | `services/rigor_evaluator.py` | **external** gate (primitive #5); `num_trials` is the DSR denominator |
| `selection_bias_routes.py` gate endpoint (`POST /api/selection-bias/...`) | `api/selection_bias_routes.py` | the live external-gate route; **derives `num_trials = max(len(valid_returns), 1)` internally (library size) and exposes NO `pool_size`/caller seam** — the wiring gap A1 closes; `num_trials_in_selection` is an existing persisted field on the strategy row |
| `regime_robustness_score(strategy_returns, regime_labels)` | `services/_rigor_helpers.py` (re-exported via `rigor_evaluator`) | regime-conditional robustness; `robust = min_regime_sharpe > 0` |
| `GmmRegimeDetector.get_current_regime() -> RegimeClassification \| None` · `gmm_regime_health()` | `services/gmm_regime_detector.py` | live exogenous regime read; `degraded` is the expected steady state |
| `make_llm_backend(model=...)` | `services/llm_backend.py` | per-role backend constructor (model selection seam, §8/§9 item 10) |
| `extract_json(...)` | `agents/strategy_architect.py` | JSON decode for every LLM role |
| `persist_proposal(...)` | `services/strategy_memory.py` | episodic compounding tail |
| `validate_strategy_spec(...)` | `services/strategy_dsl.py` | DSL validator (proposer-spec gate; §11) |
| `interpret_spec(...)` | `services/dsl_to_backtrader.py` | DSL → backtrader; raises `DSLError` on unsupported indicators (the `realized_vol_N` trap; §11, fix A5) |

---

## 1. TL;DR + recommendation

**Recommended architecture: a structured adversarial society that is the *sole* generation pipeline.** It generates a **larger diverse pool** of regime/mechanism/risk-steered DSL proposals (~15–20), runs a **bull/bear adversarial round with one visible rebuttal** plus a **deterministic critic panel** (rigor-backtest, GMM-regime risk, passive-null, provenance/embargo) that **culls and ranks to a top-10**, **backtests all 10 for real** via `evaluate_fusion_spec` (deterministic Python → DSR/PBO/OOS), and presents a **true top-10 leaderboard**. The user-chosen winner (**K=1**) is the only candidate that goes through the **external rigor gate + on-chain anchor**.

This is wired as the **`pipeline_name="debate"` runner** that **replaces** fusion/architect/agent dispatch *as the end state* — **but in Phase 1 it is added alongside the legacy runners, not in place of them** (see §5a). Fusion is not deleted — it folds in as the **proposer's internal capability** (`StrategyFusion.propose` is how each proposer turns a paper set into a DSL spec — but the *steering* is applied by the proposer calling `select_candidates(regime_bias=R)` itself and constructing `StrategyFusion(model=…)`; `propose()` today takes neither a `regime_bias`/explicit-evidence set nor a `model` arg, so both are **required edits**, not existing reuse — see the §4 caller-gap note and fix A3). What eventually gets deleted (in the Phase-3 cutover PR) is the *standalone fusion runner + dispatch/relabel block*, the *architect path*, the *single-agent `portfolio_agent` path*, and the *duplicate generate endpoint*.

**The reshape vs. v1 (Dan's mandatory refinements):**

- **"N=2–3, exactly one goes deep" is REPLACED.** The society proposes a **larger pool** (~15–20), the adversarial round + deterministic critics **cull & rank to a top-10**, and **all 10 survivors run the real DSL backtest** (cheap deterministic Python — `evaluate_fusion_spec` per candidate). "Deep" is no longer "one expensive generation"; it is the **external rigor gate + on-chain anchor**, which stays **K=1** (the user-chosen leaderboard winner).
- **`num_trials = pool_size` (the FULL proposed pool, not 10, not 1, not library size).** The DSR multiple-testing correction must count **every spec we proposed and backtested**, because selecting the best-of-pool against the library is exactly the multiple-testing inflation DSR deflates. **This does NOT exist in the live gate yet** — the gate computes `num_trials` internally from the strategy library. The exact edit that threads `pool_size` into the gate is named in §5c (the "Gate wiring delta") and tested in §9. `pool_size` itself is precisely defined in §5c.
- **Fourth-peer / byte-identical-live-path framing is reconciled.** The flag guards the cutover window; Phase 1 keeps every legacy runner intact and merely *adds* the `"debate"` branch, so "flag OFF ⇒ legacy path is byte-identical" is *true by construction* (the deletions move to the Phase-3 cutover PR). The earlier draft's self-contradiction — delete the runners AND keep a byte-identical legacy path — is resolved in favor of additive Phase 1 (fix A2).

**Why this topology (carried forward from v1, endorsed by #766):**

- **Genuine adversarial topology** — a real bull-vs-bear research round with one rebuttal, a moderator/synthesizer, first-class ABSTAIN — scores on Lepton's Agentic-Sophistication axis. A naked ensemble under-delivers the "multi-agent debate" the roadmap names.
- **Deterministic critics (0 tokens).** The Hierarchy-of-Truth protocol (Xia §4.4) makes regime/vault state *non-votable* — a Python gate cannot be argued out of its position. This is both a correctness win (non-votable truth in code) and the load-bearing budget win (the risk work + all backtests cost zero LLM tokens).
- **`evaluate_fusion_spec` is the real-backtest spine** — reuse, not build-new. The single biggest cost saver.

**What we reject:** the full 9-role, 12-call TradingAgents roster (deferred to stretch — it fights primitive #5's affordability reason). BYOK per-role model diversity (deferred to stretch — complicates the single-provenance story). An LLM risk agent (the risk agent stays **deterministic** — non-votable Hierarchy-of-Truth).

**The synthesis in one line:** *N>1 diversity is bought cheap (regime/mechanism-steered grounding + one adversarial round over shared embargoed evidence); the deterministic critics cull, rank, and run all 10 real backtests for zero tokens; the user picks the leaderboard winner; and K=1 survives at the **external rigor gate + single on-chain provenance anchor** — the one place "going deep" still costs.*

---

## 2. Agent roster + roles

Logical roles below. **As shipped, only the proposers + bull/bear are LLM calls** — three prompts, four turns; the four critics, all backtests AND the synthesizer are deterministic Python (the budget trick). The v2 draft planned an LLM synthesizer; the built one is `_score` / `_survives_null` in `debate_engine.py` and makes no completion call at all. Every LLM role constructs through `make_llm_backend(model=...)` and parses with `strategy_architect.extract_json` — they are *prompt modes over the shared seam*, not new backends. Every cited claim runs through the fusion `valid_ids` anti-hallucination filter (in `StrategyFusion.propose`) so no fabricated paper enters the debate record.

| # | Role | LLM? | Reuses | Behavior |
|---|------|------|--------|----------|
| **P** | **Proposer (pool)** | yes (1 call / steer; ~15–20 steers) | `select_candidates(brief, corpus, regime_bias=R)`; `StrategyFusion(model=brief.model).propose(brief)` → DSL spec; fusion `valid_ids` filter; `extract_json` | Each proposer emits ONE Archimedes-DSL `strategy_spec` grounded in a steered paper set (regime × mechanism × risk-appetite). Status-honest: a proposer whose `FusionProposal.is_actionable` is false (status≠"ok" or <2 papers) is dropped from the pool, not invented around. **A proposal that validates but is not backtestable (e.g. emits `realized_vol_N`, or omits `parameter_variants`) is also dropped by the pre-backtest conformance guard — see fix A5/§5b.** |
| **R1** | **Bull researcher** | yes (1 call, +1 rebuttal) | `extract_json`; inline empirical stats (`_tool_get_asset_stats`/corr/stress computed in Python, fed as text) | Argues FOR acting, citing only proposer papers + inline stats. Rebuttal turn sees the Bear's prior claims. Output `{verdict:'act', confidence, key_claims:[…], strongest_evidence_id}`. **As shipped (#1636):** the user message is per-candidate evidence cards (`[C1] Name — cites arXiv:xxxx "Title"`), and `key_claims` are `{claim, candidate_id, arxiv_ids}` objects plus a `discard: [{arxiv_id, reason}]` list — the "citing only proposer papers" half of this row is now enforced, not merely asked for: `_debate_round` drops any id outside the proposers' evidence set and keeps the claim with `arxiv_ids: []` so an unattributed claim is recorded as unattributed. Before that, all four turns received a `"; "`-joined list of strategy NAMES with no paper in the prompt at all. |
| **R2** | **Bear researcher** | yes (1 call, +1 rebuttal) | same | Argues for ABSTENTION — the null is buy-and-hold. Cites the StockBench base rate (active LLM agents underperform passive baselines; our own agent ranked #15/15). Attacks overfit / regime-fragility / cost-vs-benefit (<5 bps net ⇒ don't trade). **Structurally privileged toward abstention.** Output `{verdict:'decline'|'act', confidence, fatal_flaws:[…]}`. |
| **C-prov** | **Provenance/Embargo critic** | **no** | embargo+decay already applied in `load_corpus()`; fusion `valid_ids`; consulted-hash union | Hard-fails any candidate citing an arxiv_id not in the shared decay-weighted surface (post-embargo papers never enter the surface — they are filtered in `load_papers_from_db`). Enforces Xia 1/2/4. Non-votable. |
| **C-rigor** | **Rigor-backtest critic** | **no** | `evaluate_fusion_spec(spec)`, each call wrapped in try/except that drops-with-honest-emit | Runs **each top-10 survivor's** spec → real DSL backtest → `FusionEvalResult{backtest, rigor}` (DSR/PBO/OOS). Deterministic; cannot be argued with. This is the spine — all 10 backtests are cheap Python. A per-candidate `evaluate_fusion_spec` that raises (despite the A5 pre-guard) is caught and drops that one entry with an honest emit, never aborts the cohort. |
| **C-regime** | **GMM-regime risk critic** | **no** | `GmmRegimeDetector.get_current_regime()`; `regime_robustness_score` / `regime_conditional_sharpe`; `gmm_regime_health()` | Reads the live exogenous regime; penalizes candidates whose edge collapses in the *current* regime, rewards regime-robust ones (`robust = min_regime_sharpe > 0`). Discounts its weight when `gmm_regime_health().status == "degraded"` (the expected steady state). **The non-votable Hierarchy-of-Truth gate** — crisis/degraded biases the panel toward decline regardless of consensus. |
| **C-null** | **Passive-null critic ("StockBench skeptic")** | **no** | V_check `min_cost_benefit_bps ≥ 5`; buy-and-hold baseline | The standing "do nothing" debater. A candidate survives only if it beats buy-and-hold net of cost by ≥5 bps. If none clears it → ABSTAIN (first-class). |
| **S** | **Synthesizer / Fund manager** | **no — 0 calls as shipped** (the draft budgeted 1) | `_score` (passing, DSR, OOS Sharpe); `_survives_null`; `_CandidateResult` builder | Ranks the survivors on the deterministic key `(passing, DSR, OOS Sharpe)` and returns the leaderboard; ABSTAIN when no candidate clears C-null. **Built with zero LLM calls, not one** — the "collapses to 0 when the floor forces the outcome" hedge in the draft became the unconditional behaviour, so there is no Synthesizer prompt in [`docs/specs/prompt-inventory.md`](prompt-inventory.md) and none to write. |

### Prompts (as shipped — two of the sketches below were never built)

**The prompts are generated, not narrated. The inventory is
[`docs/specs/prompt-inventory.md`](prompt-inventory.md)** — every live template, with its
id, version, placeholders and full text, rendered from `backend/archimedes/agents/prompts.py`
under a CI drift test (#1800). Quote it; do not paraphrase it. The version of this section
that paraphrased documented two prompts that do not exist, and nothing failed (#1801).

**Proposer (P) — there is no proposer prompt.** The v2 draft sketched a proposer system
prompt built around a `{regime}`/`{mechanism}` slot. Nothing in the tree renders it.
`debate_engine._propose_pool` fans `StrategyFusion(model=…, candidates=evidence[R]).propose(fb)`
across the steer grid, so **the proposer prompt IS the fusion prompt** —
`fusion.proposer.system`, whose only placeholders are `${fuse_target_min}` and
`${min_papers}`. The steer reaches the model two other ways: `regime_bias` reorders the
evidence *before* the call, inside `select_candidates`; and the mechanism arrives as prose in
the **user** JSON payload, `strategic_direction = f"{brief.intent} — favor {mechanism}
mechanisms"`. It never touches the system prompt. That is not pedantry — a reader looking for
"where does the regime steer live" would hunt for a template that was never written, and miss
that the steer is user-controlled text concatenated into a prompt (the seam #1801 screens).

**Synthesizer (S) — there is no Synthesizer prompt, and no Synthesizer call.** The draft
sketched an "impartial fund manager" system prompt. Synthesis as built is
`debate_engine._score` — the deterministic key `(passing, DSR, OOS Sharpe)` — plus
`_survives_null` for the ABSTAIN branch. Zero tokens, zero prompts. The *behaviour* the sketch
described (rank for the user, ABSTAIN is first-class) is real; the LLM that was going to
produce it is not.

**Bull (R1) / Bear (R2) — this one shipped**, as `debate.turn.system` with `${role}`,
`${rnd}`, `${stance}` and `${rebuttal}`. Round 2's `${rebuttal}` is filled by
`debate.rebuttal_preamble`, carrying the opponent's round-1 claims; the user message is the
per-candidate evidence cards from `_candidate_cards` (#1636), not a template.

(The DSL contract the proposer must obey is pinned in §11, and is `fusion.spec_contract` in
the inventory; the conformance guard that enforces it before backtest is in §5b / fix A5.)

---

## 3. Control / debate flow

```
run_generation(brief, mode="debate")  ──pipeline_name="debate" (flag-ON branch; legacy tree intact)──►  _run_debate_candidate(*, candidate_id, brief, emit, regime, agent)
                                                                    (called ONCE; owns the whole society; regime collapses to ["neutral"])
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│  ONE shared decision t  ·  ONE embargoed + time-decayed evidence surface  ·  ONE consulted-hash union  (Xia 1,2,4)   │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                                    │
  STEP 0 — SHARED EVIDENCE (built ONCE per Generate, cached across all steers — the budget win)
     corpus = load_corpus()                              # -> list[CorpusPaper]; embargo (Xia 1) + time-decay (Xia 2)
                                                          #    ALREADY applied inside load_papers_from_db()
     for R in STEERS:                                    # ~15-20 (regime × mechanism × risk-appetite) — pure Python, free
         evidence[R] = select_candidates(brief, corpus, regime_bias=R)   # agents/strategy_fusion.py
     # NOTE: select_candidates ranks hit-count→recency→arxiv_id; on a DEGRADED TF-IDF corpus bull/bear
     #       steers can collapse to the same tail. The §9 divergence test (A4) guards this at unit level.
     valid_ids = sorted(union of evidence[R] arxiv_ids)
     consulted_hashes = sorted("arxiv_id:content_hash" for paper in valid_ids)   # Xia 4
     emit candidates_selected(source_arxiv_ids=valid_ids)
                                                                    │
  STEP 1 — PROPOSE A LARGER POOL  (~15-20 cheap LLM calls; asyncio.gather, bounded by DEBATE_POOL_MAX)
     Proposer×K  →  each = StrategyFusion(model=brief.model).propose(brief over evidence[R])  →  strategy_spec
                    # NB: shorthand. propose() takes neither `model` nor an explicit evidence[R] today (it
                    #     calls select_candidates(brief, corpus) internally, unbiased). The proposer must
                    #     (a) construct StrategyFusion(model=brief.model)  [fix A3 seam], and (b) pre-build
                    #     evidence[R]=select_candidates(brief, corpus, regime_bias=R) and propose over THAT
                    #     set, reusing propose()'s prompt + valid_ids filter — see §4 caller-gap + §5b.
       drop any candidate whose FusionProposal.is_actionable is False (status!="ok" OR <MIN_PAPERS)
       drop any candidate failing the pre-backtest DSL conformance guard (A5: unsupported indicator alias,
         or missing parameter_variants) — honest emit, NOT a throw inside C-rigor
       POOL = [actionable AND conformant proposals]   ◄── pool_size = len(POOL)  (the count that enters
                                                            deterministic ranking/backtest — the DSR set; see §5c)
       if POOL empty: raise DebateUnavailable   (honest fallback — see §5)
     emit agent_iteration / tool_called("propose") per proposer
                                                                    │  candidate pool  (+ inline empirical stats, computed once)
  STEP 2 — RESEARCH DEBATE  (ONE round + ONE visible rebuttal, R=1; Bull ∥ Bear via asyncio.gather)
     ┌── R1 BULL ──┐   ┌── R2 BEAR (+passive-null framing) ──┐    both see SAME pool + SAME stats
     │ argue ACT   │◄─►│ argue DECLINE                       │    rebuttal: each sees the other's prior claims
     └──────┬──────┘   └──────────────┬─────────────────────┘    emit agent_iteration(iteration_n=round) per turn
            └────────────┬────────────┘
                                                                    │  debate_transcript (sorted by fixed role order before hashing)
  STEP 3 — CRITIC PANEL  (DETERMINISTIC — ZERO LLM CALLS)  → CULL & RANK to TOP-10
     C-prov   embargo/id audit over the pool ──────────► hard FAIL ⇒ drop (Xia 1,2,4, non-votable)
     C-null   beats buy-and-hold by ≥5 bps net? ───────► else flag (StockBench honesty floor)
     C-regime GmmRegimeDetector.get_current_regime() + regime_robustness_score ──► regime-conditional score (Xia 3)
     rank the survivors; TOP10 = best 10 by the composite cull score
     C-rigor  for c in TOP10: try: evaluate_fusion_spec(c.spec) except DSLError/Exception: drop c with honest emit
              ──► real DSL backtest → FusionEvalResult{DSR,PBO,OOS}
              (ALL surviving entries backtested — deterministic Python, 0 tokens)
     emit tool_result per (candidate × critic)   # the audit trail
                                                                    │  TOP-10 LEADERBOARD (every entry carries a real backtest)
  STEP 4 — SYNTHESIZE / ADJUDICATE  (1 LLM call — or 0 if the floor forces it)
     if no survivor clears C-null  OR  C-regime flags crisis/degraded:  ───────────► ABSTAIN  (mechanical, 0 LLM)
     else:  Synthesizer(top10, scorecards, transcript) ─► ranked leaderboard + leader_id + per-candidate notes
     emit agent_iteration("synthesize")
                                                                    │
  build _CandidateResult PER TOP-10 ENTRY (the leaderboard) — leader flagged:
       generation_method="debate"  (or "debate_abstain"),
       has_real_rigor=True,                      # every leaderboard entry carries C-rigor's real backtest
       rigor_verdict=<from evaluate_fusion_spec>, return_series=<from backtest>,
       source_arxiv_ids=<candidate papers>, source_papers=<union>, reasoning=<synth note>,
       debate_transcript=[…], consulted_paper_hashes=<union>,
       num_trials_in_selection=pool_size)        # persisted so the gate can read it back (A1)
                                                                    │  returns the leaderboard _CandidateResult set (existing contract)
                                                                    ▼
  ════════ run_generation TAIL — REUSED ════════
  _patch_pbo SKIPS all (has_real_rigor=True) · best_selected (leader) · _persist_candidate per entry ·
  persist_proposal(agent="debate") · buy-and-hold backtest gather SKIPS all (has_real_rigor) · done
                                                                    │
  ════════ EXTERNAL RIGOR GATE — primitive #5, K=1, OUTSIDE the society (user-chosen winner) ════════
  POST /api/selection-bias/...  (live route on api/selection_bias_routes.py) — see §5c "Gate wiring delta":
     for a generation_method=="debate" strategy, the route reads num_trials from the persisted
     num_trials_in_selection (== pool_size) instead of max(len(valid_returns), 1) (library size).
     run_rigor_gate(strategy_id, daily_returns, strategy_code, num_trials=pool_size, library_pbo=…, average_correlation=…)
  → RigorGateResult{passes_all, gate_details}  ──renders on passport──►  enables Deploy
  ════════ V_check (deterministic floor) ════════
  weights_sum_bps==10000 ∧ max_concentration≤60% ∧ cost_benefit≥5bps  ──fail──► SKIP trace published (even on unanimous APPROVE)
```

**Reproducibility (R3):** agent ordering is fixed and deterministic; the Bull∥Bear concurrency collapses back to a fixed role order before the transcript is hashed. The transcript, each agent's `consulted_paper_hashes`, and their union go into one trace. No nondeterministic fan-out. The top-10 ranking is a deterministic sort over the critic scorecards; only the synthesizer's prose ordering is LLM-derived, and it is constrained to a permutation of the deterministically-admitted top-10.

---

## 4. How the larger pool + top-10 leaderboard emerge + K=1 reconciliation

**Diversity axis (cheap, the only N× LLM cost):** steered grounding. `select_candidates(brief, corpus, regime_bias="bull")` ranks momentum/trend/carry/breakout papers to the top; `"bear"` ranks vol-managed/defensive/hedge/tail-risk/min-variance papers (driven by `_REGIME_BIAS_TERMS`). The pool fans this out across **regime × mechanism × risk-appetite** steers (~15–20). Same embargo, same decay surface, same decision `t` — the divergence is in *which decay-weighted papers rank top* and *which mechanism the proposer is told to favor*, never in reaching past the decay to a stale paper (that's a C-prov hard-fail, not a stronger argument).

> **Diversity-theater risk (verified, load-bearing).** `select_candidates` ranks by **integer keyword-hit count first**, then recency, then `arxiv_id`; `regime_bias` adds a fixed boost per bull/bear keyword hit. On the **current DEGRADED corpus** (`paper_rag: degraded`, TF-IDF fallback, MiniLM-not-SPECTER2), if a brief's bull and bear keyword sets both hit few papers, **both steers can collapse to the same recency-sorted tail** → proposers see near-identical evidence → convergent specs → a leaderboard that *looks* like a debate but is N copies of one idea. This is not merely "no SPECTER2 yet" (§10.2); the *ranking primitive itself* is hit-count+recency, which is weakly discriminating across regime steers on abstracts. **The mitigation is a Phase-1 unit test that bull-vs-bear evidence sets actually differ on the fixture corpus (Jaccard < 1.0), not a Phase-3 embeddings promise** — see §9 test #2 (fix A4). Real embeddings (#778) are the *quality* lift; the *divergence-exists* guarantee must be testable now.

> **Caller-gap note (verified):** `select_candidates` accepts `regime_bias`, but neither `StrategyFusion.propose` nor the existing fusion callers pass it (`propose` calls `select_candidates(brief, corpus)` with no bias). The debate is the **first** caller to actually thread `regime_bias` through. The proposer step therefore calls `select_candidates(brief, corpus, regime_bias=R)` directly to build `evidence[R]`, then hands that steered set to the proposing LLM — it does not rely on `propose` to steer.

**Cull → rank → backtest-all-10:** the deterministic critic panel culls (C-prov hard-fail → C-null cost-benefit → C-regime conditioning), ranks the survivors by a composite score, takes the **top 10**, and runs `evaluate_fusion_spec` on **every one** of them (real DSL backtests — deterministic Python, 0 tokens, each wrapped in try/except per fix A5). The result is a **true top-10 leaderboard**, each entry carrying its own real DSR/PBO/OOS. The synthesizer ranks/annotates; **the user picks the winner** from the leaderboard.

**Reconciliation with primitive #5 (K=1) — explicit, reshaped:**

| What #5 protects | How it survives the larger pool + top-10 |
|---|---|
| **Deep cost** (reason #5.1) | "Deep" is no longer N expensive generations — it is the **external rigor gate + on-chain anchor**, run for **exactly one** user-chosen winner. The pool's proposers are cheap single `complete()` calls; the critics + all 10 backtests are deterministic (0 tokens). The expensive, irreversible step (on-chain anchor) stays K=1. |
| **Single re-derivable provenance** (reason #5.2) | The on-chain `methodology_hash` is anchored once, for the user-chosen winner, carrying the **union** of all proposers' `consulted_paper_hashes` + the debate transcript. The full top-10 surfaces in the UI; the on-chain provenance stays K=1-clean. |
| **Considered-Alternatives panel** (#5 surface) | The leaderboard's non-winning entries *are* the considered-alternatives — now with real backtests + per-critic / synth notes, a strict upgrade at near-zero extra cost. |
| **External rigor gate** | Only the user-chosen winner goes through the unchanged external `run_rigor_gate`, with **`num_trials = pool_size`** (§5c) — *once the gate-wiring delta in §5c lands*. The society never self-certifies. |
| **Episodic compounding** | The full leaderboard + verdicts persist to `strategy_proposals` via the unchanged `persist_proposal` tail (`agent="debate"`). |

One line: *the larger pool + top-10 leaderboard live at the cheap debate-over-shared-evidence + deterministic-backtest layer; K=1 survives at the external-rigor-gate + single-on-chain-anchor layer (the user-chosen winner).*

---

## 5. Integration — replacement scope (additive in Phase 1, deletions deferred to cutover)

> **The Phase-3 cutover has happened (2026-07-09).** The society is the sole generation
> pipeline, the Strategy Architect is deleted (`ccf4f2f`, PR #1074) and
> `ARCHIMEDES_DEBATE_ENABLED` is retired. Decision record:
> [`../adr/debate-society-sole-generation-pipeline.md`](../adr/debate-society-sole-generation-pipeline.md).
> The phased plan below is retained as the *record of how it was sequenced*, not as
> current state — the flag and the legacy runners it describes no longer exist.

The society **becomes** the sole generation pipeline as the end state — but the path there is **additive first, delete later**. Phase 1 *adds* the `"debate"` branch behind `ARCHIMEDES_DEBATE_ENABLED` (default OFF) and leaves every legacy runner untouched; the deletions land in a separate Phase-3 cutover PR after the society is verified on the live path. Reuse stays ~80% (the persist/backtest/episodic tail, `evaluate_fusion_spec`, `StrategyFusion.propose`, `select_candidates`, SSE vocabulary, the external gate). The ~20% new is the society runner + the deterministic critics + the synthesizer adapter.

### 5a. Phase 1 is additive; the deletions are the Phase-3 cutover PR (fix A2)

**The delete-vs-byte-identical contradiction is resolved in favor of additive Phase 1.** v1 mandated *deleting* the architect path, the `portfolio_agent` runners, and the duplicate endpoint in Phase 1 while *also* promising a byte-identical legacy path under flag-OFF. Both cannot hold. The resolution:

- **Phase 1 (this spec's first increment): ADD only.** `_pick_pipeline` gains a flag-gated branch: when `ARCHIMEDES_DEBATE_ENABLED` is set, return `("debate", "society pipeline (flag-on)")`; **otherwise the existing legacy decision tree runs unchanged.** No runner is deleted, no endpoint is removed, `"architect"` stays in the mode allowlist. This makes "flag OFF ⇒ legacy live path is byte-identical" *true by construction* and the §9 test (grep proves `_run_debate_candidate` unreachable while OFF) actually passable.
- **Phase 3 (the cutover PR, after the society is verified on the live path): DELETE.** Only then do we remove (1) the pipeline-selection decision tree's legacy branches (collapse `_pick_pipeline` to the `"debate"` constant), (2) the standalone fusion runner + dispatch/relabel block in `run_generation`, (3) the architect `"architect"` selection + silent fall-through, (4) the single-agent `portfolio_agent` runners (`generation_method` default `"portfolio_agent_streaming"`), and (5) the duplicate generate endpoint in `api/strategies_routes.py` (the older `generate_strategy` → `_run_fusion_job` poll/generate path; one SSE `/api/generate` survives). Fusion's *capability* is preserved throughout — it folds into the proposer (`StrategyFusion.propose`); only the standalone runner is eventually removed.

**KEEP as fixtures/docs only:** the example strategies (Faber 2007 SMA200, Moreira-Muir 2017 vol-managed, Moskowitz-Ooi-Pedersen 2012 TSMOM, buy-and-hold) remain as test fixtures + library seeds. They are not a generation pipeline.

**The `ARCHIMEDES_DEBATE_ENABLED` flag is a cutover guard, not a fourth-peer guard.** While OFF, `_pick_pipeline` returns the legacy selection (so the live path is unchanged during the cutover window); while ON, it returns the `"debate"` branch. Once the replacement is verified on the live path, the legacy branches — and eventually the flag — are removed in the Phase-3 cutover PR. There is no permanent "fourth peer."

### 5b. New runner + the carrier contract

**New file** `backend/archimedes/agents/debate_engine.py`: `_run_debate_candidate` + `DebateUnavailable(Exception)` + `_debate_can_run(brief)` (mirrors the existing fusion viability precheck — true iff `fusion_enabled()` AND `len(select_candidates(FusionBrief(...), load_corpus())) >= MIN_PAPERS`) + the four deterministic critics + the synthesizer adapter + `_dsl_conformance_ok(spec_dict) -> bool` (the A5 pre-backtest guard).

**Exact signature (matches the runner contract `run_generation` invokes):**
```python
async def _run_debate_candidate(
    *, candidate_id: str, brief: GenerateBrief, emit: _Emitter,
    regime: str = "neutral", agent: Any = None,
) -> _CandidateResult: ...
```

**Returned `_CandidateResult`:** fully populated per the existing contract. For the winner, set `has_real_rigor=True` (so `_patch_pbo` and the buy-and-hold gather both correctly skip it — the CSCV PBO from `evaluate_fusion_spec` must not be clobbered). For ABSTAIN, return a populated result with empty `weights`, a passive-baseline `return_series`, and an abstain `rigor_verdict` stub — this does **not** hit the empty-guard (which would wrongly emit a `NO_CANDIDATES` error); it is a first-class SKIP.

> **Carrier-contract mismatch — RESOLVED (Phase 1, option b):** the existing runners return a *single* `_CandidateResult` but the society produces a leaderboard. Phase 1 takes option (b): `_run_debate_candidate` returns the **leader** `_CandidateResult`; the full top-N leaderboard is built by the pure, unit-tested `build_leaderboard` helper. The Considered-Alternatives fan-out (persisting the non-winning entries) is deferred to Phase 2. Buy-and-hold/PBO skip is keyed on `generation_method` (`"debate"`/`"debate_abstain"`) — reconciled with #829's `!= "fusion"` skip so debate candidates (which emit a DSL spec, `weights={}`) never hit the static portfolio backtester.

When `pipeline_name == "debate"`, set `regimes = ["neutral"]` so the per-regime loop runs **once** — the society owns its own internal regime/mechanism split, and the expensive PBO/persist tail stays a single self-contained unit. The runner returns the **top-10 leaderboard** (leader flagged via `best_selected`).

**Returned `_CandidateResult` (per leaderboard entry):** fully populated per the existing contract. Set `has_real_rigor=True` on every entry (each carries C-rigor's real backtest), so `_patch_pbo` and the buy-and-hold gather both correctly **skip** them — the CSCV PBO from `evaluate_fusion_spec` must not be clobbered. Set `generation_method="debate"`; extend the episodic `agent=` ternary so `generation_method=="debate"` persists `agent="debate"`. **Persist `num_trials_in_selection=pool_size` on each entry** so the external gate can read it back (the A1 wiring delta, §5c).

**Pre-backtest DSL conformance guard (fix A5).** After `StrategyFusion.propose`, before a spec is admitted to POOL, the proposer step runs `_dsl_conformance_ok(spec_dict)`: it **rejects** specs whose indicator aliases are not in `{sma, ema, rsi, momentum}` (an alias like `realized_vol_N` *validates* via `validate_strategy_spec` but raises `DSLError("unsupported indicator")` in `interpret_spec`), and **(optionally) injects a default `parameter_variants` grid on the entry indicator** so PBO is computable (a missing/<2-variant grid yields PBO `None` → fail-closed). An unbacktestable-but-actionable proposal is **dropped from POOL with an honest emit — not allowed to throw in C-rigor.** As a belt-and-suspenders backstop, every per-candidate `evaluate_fusion_spec` in C-rigor is additionally wrapped in try/except that drops-with-honest-emit, so a single bad spec never aborts the whole leaderboard build.

**ABSTAIN is a real `_CandidateResult` state routed through the existing emit path — not a new error.** A deliberate all-abstain returns a populated `_CandidateResult` with empty `weights`, a passive-baseline `return_series`, and an abstain `rigor_verdict` stub (`generation_method="debate_abstain"`). **A genuinely empty pool** (no actionable+conformant proposal) raises `DebateUnavailable`, which the runner-fallback path catches and relabels honestly; the existing `RIGOR_FAIL` emit (the empty-candidates path) is the route for a deliberate all-abstain that produces zero deployable candidates — do **not** invent a new error. (`passes_rigor` / `has_real_rigor` remain the live carrier fields; `_patch_pbo` and the buy-and-hold skip key on `has_real_rigor`, verified.)

**Streaming:** emit `agent_iteration` / `tool_called` / `tool_result` per role, reusing the existing SSE vocabulary (`job_queued`, `brief_validated`, `pipeline_selected`, `candidates_selected`, `agent_iteration`, `tool_called`, `tool_result`, `candidate_drafted`, `candidate_evaluated`, `candidate_failed`, `best_selected`, `trace_hashed`, `persisted`, `backtest_running`, `backtest_done`, `backtest_failed`, `done`, `error`) → **zero frontend changes**.

**Reused unchanged:** `strategy_fusion.py` (`select_candidates`, `propose`, `load_corpus`), `strategy_architect.py` (`extract_json`), `fusion_evaluator.py` (`evaluate_fusion_spec`), `llm_backend.py` (`make_llm_backend`), `gmm_regime_detector.py`, `rigor_evaluator.py` / `_rigor_helpers.py`, `strategy_memory.py` (`persist_proposal`). **Newly threaded (not unchanged):** `StrategyFusion.__init__` / `_resolve_backend` / `default_fusion` gain a `model` parameter (§8 item 10, fix A3), and `api/selection_bias_routes.py` gains the debate-aware `num_trials` read (§5c, fix A1).

### 5c. External rigor gate (primitive #5 — stays OUTSIDE the society, K=1) + the gate-wiring delta

The society emits the leaderboard; the user picks; the existing external path adjudicates the winner. The authoritative verdict comes from the live external gate on `api/selection_bias_routes.py` (`POST /api/selection-bias/...`).

**Defining `pool_size` precisely (fix A6).** `pool_size` is **the count of specs that entered the deterministic ranking/backtest stage** — i.e. proposals that were both *actionable* (`FusionProposal.is_actionable`) **and** *conformant* (passed the A5 DSL guard). It is **not** the number of `complete()` calls fanned out, and **not** the post-cull top-10; it is exactly the multiple-testing selection set DSR must deflate (we proposed this many candidate specs and selected the best against the library). The raw-vs-conformant choice, and the library-context adjustment, are folded into the OPEN-Önder denominator question below — not left as a separate ambiguity.

**The gate-wiring delta (fix A1 — the single most important wire, and it does NOT exist yet).** The live gate does **not** accept a `pool_size`/`num_trials` argument from the caller. `api/selection_bias_routes.py` computes `num_trials = max(len(valid_returns), 1)` **internally** (the size of the strategy library that has returns) and passes that to `run_rigor_gate`; the in-pipeline path (`generation_pipeline.py`) likewise uses `num_trials = max(1, len(strategies))` (library size). There is no `GET /api/selection-bias/gate/{winner_id}` route in that shape. So `assert num_trials == pool_size` is **unrunnable until a code change threads `pool_size` into the gate.** The required edit, named exactly:

1. **Persist `pool_size` at debate time** on the winner's strategy row / passport — reuse the **existing** `num_trials_in_selection` field (already persisted on the strategy row) by writing `num_trials_in_selection = pool_size` per leaderboard entry (§5b).
2. **Modify `selection_bias_routes.py`** so that for a `generation_method == "debate"` strategy it reads `num_trials` from the persisted `num_trials_in_selection` value **instead of** `max(len(valid_returns), 1)`.
3. **Gate the override behind `generation_method == "debate"`** so all non-debate strategies keep the current library-size behavior exactly.

The effective call for a debate winner then becomes:
```python
gate_result = run_rigor_gate(
    strategy_id=winner.candidate_id,
    daily_returns=winner.return_series,
    num_trials=pool_size,               # ◄── read from persisted num_trials_in_selection for debate strategies
    library_pbo=library_pbo,
    strategy_code=winner_strategy_code,
    average_correlation=avg_correlation,
)
deploy_enabled = gate_result.passes_all
```

**Why this matters: the "build on sand" failure.** A society that proposes ~15–20 specs and selects the best introduces exactly the multiple-testing inflation DSR is meant to deflate; leaving `num_trials` at library size (or 1, or 10) silently weakens the correction and produces a *false* strict-rigor badge. **Assert `num_trials == pool_size` in a test** (§9 test #5) and surface the pool size on the passport. `gate_details` (per-criterion PASS/FAIL with `source=library|cohort`) renders on the passport; `passes_all` gates Deploy. The society never sets `passing` itself. **Do not flip `ARCHIMEDES_DEBATE_ENABLED` ON until the persisted-`pool_size`→gate path is proven on the live route.**

> **OPEN — Önder owns the DSR-deflation denominator.** Whether `num_trials` should be the raw `pool_size` (count of conformant proposed specs), or that count adjusted for the library-context selection (the best-of-pool is selected *against* the curated library, which changes the effective multiple-testing count), is **Önder's call** — not a value this spec asserts beyond "it is the full conformant proposed pool, never 1, never library size." The raw-vs-conformant precision from fix A6 is part of this same denominator question. Do not bake a specific arithmetic into the gate without Önder's sign-off.

### 5d. GMM-regime risk-agent input (deterministic C-regime)

```python
detector = GmmRegimeDetector(fallback=VixRegimeDetector())
rc = detector.get_current_regime()          # RegimeClassification | None
degraded = gmm_regime_health().status == "degraded"   # the EXPECTED steady state (no fitted artifact)
```
Branch on `rc.regime` (`risk_on|risk_off|transition|crisis`), weight by `rc.confidence`, and **discount when degraded** (bias toward decline — honest degradation per contract). Score each candidate's regime-conditional edge via `regime_robustness_score` (`robust = min_regime_sharpe > 0`). **Never** read ensemble-consensus state as a market regime (endogenous, issue #659). Because C-regime is deterministic Python, the regime gate is structurally non-votable — Xia §4.4 enforced.

---

## 6. Enforced Xia protocols mapping

| # | Protocol | Where enforced in the society | Non-votable? |
|---|----------|-------------------------------|--------------|
| 1 | **Outcome Embargo** (§4.2) | `load_corpus()` → `load_papers_from_db()` applies `apply_outcome_embargo(papers, embargo_days=30)` before the surface is built; all agents share one decision `t`. **C-prov** hard-fails any candidate citing a paper not in the embargoed surface. | yes |
| 2 | **Time-Aware Retrieval** (§4.2) | `load_papers_from_db()` applies `apply_time_aware_retrieval(papers, lam=regime_lambda(...))`; every proposer draws from the same decay-weighted surface. Diversity = reasoning over shared evidence; reaching past the decay is a **C-prov** hard-fail. | yes |
| 3 | **Hierarchy of Truth** (§4.4) | **C-regime is deterministic Python** — regime/vault state structurally override consensus; crisis/degraded biases toward decline regardless of what the bull argues. On-chain vault state + curated-over-uncurated outrank any synthesis. | yes |
| 4 | **Source Tracking** (§4.3) | Every cited claim runs through the fusion `valid_ids` filter (in `StrategyFusion.propose`). The **union** of all agents' `consulted_paper_hashes` + transcript goes into ONE `methodology_hash` anchored via `ReasoningTraceRegistry`. | — |
| 5 | **Reasoning I/O — V_check** (§5) | The user-chosen winner is an **input** to V_check (`weights_sum_bps==10000` ∧ `max_concentration≤60%` ∧ `min_cost_benefit_bps≥5`). Fail → SKIP trace, even on unanimous agreement. | yes |

**R3 target:** fixed deterministic agent order + transcript-sorted-before-hashing + union-of-hashes + the external gate = fully replayable, immutable provenance. No non-replayable element introduced.

---

## 7. The honesty mechanism — concluding "decline / do-nothing" and surfacing underperformance

The StockBench finding (our agent ranked **#15/15**, Sortino −0.91 vs the raw GLM-4.5's +1.94 — adding the agent made it *worse*) is load-bearing and surfaced, not hidden. Three structural mechanisms:

1. **First-class ABSTAIN.** The synthesizer can output `{decision:'abstain'}` (hold current weights) → a populated `_CandidateResult` (empty weights, passive-baseline `return_series`, abstain rigor stub, `generation_method="debate_abstain"`) that flows to V_check's SKIP-trace mechanism. A deliberate all-abstain that yields zero deployable candidates routes through the existing `RIGOR_FAIL` emit — not a new error. A society structurally forced to produce a trade would be dishonest by construction; ours is not.
2. **The passive-null is a standing position.** **C-null** (deterministic) is the StockBench skeptic — a candidate survives only if it beats buy-and-hold net of cost by ≥5 bps (the V_check `min_cost_benefit_bps` bar). The **Bear researcher** argues this in natural language and is structurally privileged toward abstention. Action is never privileged; the null is a hard-to-beat default.
3. **Underperformance is surfaced on the passport.** The honest-comparison discipline carries forward: mean ± stdev, no seed cherry-picking, the paper-claim delta. The synthesizer is instructed to cite the unfavorable active-agent base rate even on APPROVE. Abstention renders as a *confident, explained* verdict, not a broken product.

---

## 8. LLM budget — calls per Generate, recomputed for the new shape

The new shape proposes a **larger pool** (~15–20), but the critics and **all 10 backtests are 0 tokens**. The cost is the proposers + bull/bear + synthesizer.

**Current K=1 baseline (legacy fusion path):** ~1 brief-validation + ~1 fusion synthesis ≈ **~2–3 calls/Generate**.

**Society path (pool P≈15–20, R=1 round + 1 rebuttal):**

| Step | Calls | Serial depth |
|------|-------|--------------|
| Brief validation (existing) | 1 | 1 |
| Proposers (P≈15–20, `asyncio.gather`, bounded by `DEBATE_POOL_MAX`) | P | 1 (parallel fan-out) |
| Research debate — Bull ∥ Bear, round 1 | 2 | +1 (Bull∥Bear parallel) |
| Research debate — Bull ∥ Bear, rebuttal | 2 | +1 (depends on round 1) |
| Critic panel (4 critics, **deterministic**) | **0** | 0 |
| **All-10 real backtests** (`evaluate_fusion_spec` ×10, **deterministic**) | **0** | 0 |
| Synthesizer (or 0 when the floor forces it) | 1 | +1 (depends on critics) |
| **Total** | **≈ P + 6 ≈ 21–27 calls/Generate** at the upper pool, **≈ 12–14** at a leaner P≈8 | **serial depth ≈ 4** |

> **Token cost ≠ latency (latency note — a clarification, not one of the gating A1–A6 fixes).** The "≈ P + 6 ≈ 21–27 calls" headline is an honest *token-cost* figure, not a latency budget: the proposers and Bull∥Bear run in parallel via `asyncio.gather`, but the rebuttal is serial-after-round-1 and the synthesizer is serial-after-critics, giving a **serial depth of ≈4** (round1 ∥ → rebuttal ∥ → critics(0) → synth). Read the call count for cost and the serial-depth column for latency; do not conflate them. Each proposer is a `complete()` that can retry, which adds tail latency without adding to the nominal count.

**The cost lever is `DEBATE_POOL_MAX`, not the backtests.** The reshape's "all 10 backtested" adds **zero** LLM cost — `evaluate_fusion_spec` is deterministic Python (validate → interpret → backtrader → rigor gate). The N× LLM cost is purely the proposer fan-out. Tune `DEBATE_POOL_MAX` for the demo budget; the leaderboard top-10 and the rigor story are unaffected by trimming the pool toward, say, P≈10–12.

**The cheap-by-default moves:**

1. **Critics + all backtests are code, not models** — rigor backtest (×10), regime conditioning, cost-benefit, embargo cost **zero tokens**. This is the core win over an all-LLM TradingAgents society.
2. **Shared evidence built once** — `load_corpus()` (embargo+decay) runs once; only `select_candidates` re-ranks per steer (pure Python, free).
3. **Bull ∥ Bear concurrent** (`asyncio.gather`) — latency ≈ 1 call per round.
4. **Inline stats, not live tool-use** — `_tool_get_asset_stats`/corr/stress computed in Python and fed as text. Deliberately avoids the Anthropic-SDK-only tool loop that silently no-ops on the live Bedrock/Nova path — no portability cliff.
5. **Synthesizer collapses to 0 LLM calls** when the deterministic floor forces the outcome.

**Bounded + configurable:** `DEBATE_POOL_MAX` (default ~15, hard cap 24), `DEBATE_ROUNDS` (default 1 + 1 rebuttal), `DEBATE_MAX_LLM_CALLS` (hard ceiling → `DebateUnavailable` if exceeded), all env-gated; `ARCHIMEDES_DEBATE_ENABLED` OFF during cutover.

**Model selection (T1.1 acceptance criterion — item 10).** Every LLM role constructs via `make_llm_backend(model=...)`, so the user's Generate-page model pick must thread into **every** candidate-generation path. **The gap is real and verified:** `default_fusion()` is `return StrategyFusion()` (no model arg), and `_run_fusion_candidate` calls `default_fusion()`, so the dominant fusion path runs on the env-default model (Nova) and a user's pick is **silently ignored** when fusion runs — only the PortfolioAgent fallback threads the pick (`make_llm_backend(model=model)`). The society's proposers call `StrategyFusion.propose`, so the society MUST construct fusion with the selected model rather than the shared model-blind singleton. **Required edit sites (fix A3):** add a `model` parameter to `StrategyFusion.__init__` and `_resolve_backend` (and to `default_fusion(model=...)`), and have the debate proposer call `StrategyFusion(model=brief.model)` / `make_llm_backend(model=brief.model)` explicitly — **not** `default_fusion()`. **Required in this spec (acceptance):** (a) the selected model is threaded into *every* proposer + bull/bear + synthesizer call; (b) `served_model` provenance reflects the actual model used (`FusionProposal.model` = `backend.served_model`, the field of record) and is persisted per candidate; (c) the UI model selection persists across the generation session; (d) premium (Anthropic) models stay gated by `enforce_model_entitlement`. Without the A3 seam, (a)/(b) are aspirational, not built. **BYOK per-role model diversity is deferred to stretch.**

---

## 9. Phased build plan

### Phase 1 — flag-gated, additive society skeleton (the concrete first increment)

> **PR: "feat: debate society as flag-gated generation pipeline (additive, deterministic-critic, tool-free) !minor"**

> **SUPERSEDED — do not follow this scope fence.** The Phase-3 cutover happened: the
> debate branch is no longer flag-gated (`ARCHIMEDES_DEBATE_ENABLED` is gone) and
> `backend/archimedes/agents/strategy_architect.py` was **deleted in PR #1074**. The debate
> engine is the sole generation pipeline. The original Phase-1 fence read: *"ADD the `\"debate\"`
> branch behind `ARCHIMEDES_DEBATE_ENABLED` (default OFF). Do NOT delete the architect path,
> the `portfolio_agent` runners, or the duplicate endpoint."* That instruction is historical.

> **🚧 Phase-1 blocker (explicit — per Bogdan's #808 review): the flag stays OFF until A1 is wired.** The strict-rigor path (`pool_size` → the external selection-bias gate) is **not wired yet** — `api/selection_bias_routes.py` derives `num_trials` internally from library size. Flipping `ARCHIMEDES_DEBATE_ENABLED` ON before the §5c gate-wiring delta lands risks a **false-PASS on the rigor badge**, which violates the #1 "claims must be true" rule. A1 (thread `pool_size` into the gate) + its test #5 are therefore hard Phase-1 acceptance gates for flag-ON; the flag-OFF additive skeleton may merge before A1, but **must not be enabled on the live path until A1 is proven there.**

**Minimal society (proposers + bull/bear + 2 deterministic critics):** Proposer pool (reuse `StrategyFusion(model=brief.model).propose`, leaner P≈8–10) → Bull ∥ Bear (one round) → **C-rigor** (`evaluate_fusion_spec`, all survivors, each try/except-wrapped) + **C-null** (cost-benefit) → Synthesizer (rank/abstain). While OFF, `_pick_pipeline` returns the legacy selection so the live path is byte-identical during cutover.

**New files:**
- `backend/archimedes/agents/debate_engine.py`:
  - `async def _run_debate_candidate(*, candidate_id, brief, emit, regime="neutral", agent=None) -> _CandidateResult` (returns the leader, with the full leaderboard stashed for the persist tail; **resolve the single-vs-list carrier mismatch in this PR — see §5b**).
  - `DebateUnavailable(Exception)`, `_debate_can_run(brief) -> bool` (mirror the fusion viability precheck).
  - `_propose_pool(brief, corpus, steers, model) -> list[FusionProposal]` — fans `StrategyFusion(model=...)` across `select_candidates(..., regime_bias=R)`; drops non-actionable (`is_actionable`) and non-conformant (A5) specs.
  - `_critic_rigor(top) -> dict` (calls `evaluate_fusion_spec` per entry, try/except-wrapped), `_critic_null(...)` (≥5 bps vs buy-and-hold).
  - `_synthesize(top, scorecards, transcript, model) -> decision` (thin `extract_json` adapter; can return ABSTAIN).
  - `_dsl_conformance_ok(spec_dict) -> bool` (the A5 guard).

**Edit sites (by symbol, additive):**
- `backend/archimedes/agents/generation_pipeline.py`
  - `_pick_pipeline` → if `ARCHIMEDES_DEBATE_ENABLED` return `("debate", "society pipeline (flag-on)")`; else **unchanged** legacy tree (no `"architect"` removal).
  - `run_generation` dispatch → when `pipeline_name == "debate"`, set `regimes = ["neutral"]` and `runner = _run_debate_candidate`; leave the existing fusion/agent dispatch in the `else`.
  - episodic `agent=` ternary → add the `"debate"` case.
- `backend/archimedes/agents/strategy_fusion.py`
  - `StrategyFusion.__init__` + `_resolve_backend` + `default_fusion` → accept/forward `model` (fix A3).
- `backend/archimedes/api/selection_bias_routes.py`
  - the `num_trials` derivation → for `generation_method == "debate"` strategies, read the persisted `num_trials_in_selection` instead of `max(len(valid_returns), 1)` (fix A1).
- the Generate request schema (`api/strategies_routes.py` brief model) → **add** `"debate"` to the mode allowlist (do **not** drop `"architect"` yet — fix A2).
- thread the selected model into the proposer's fusion construction (item 10 (a)/(b), fix A3).

**Reuse verbatim:** `select_candidates(regime_bias=...)`, `extract_json` + the fusion `valid_ids` filter, `make_llm_backend`, `evaluate_fusion_spec`, `GmmRegimeDetector.get_current_regime()` / `gmm_regime_health()`, `_patch_pbo`/buy-and-hold-skip (keyed on `has_real_rigor`), `persist_proposal`, the SSE emit vocabulary, the unchanged PBO/backtest/persist/external-gate tail.

**Tests (hermetic — no live LLM/Redis/Postgres; copy `_FIXTURE_ROWS` + `_MockBackend` from `backend/tests/test_strategy_fusion.py` and the tmp-sqlite DB path from `backend/tests/test_corpus_service.py`; file `backend/tests/test_debate_engine.py`):**
1. canned-backend society → top-N `_CandidateResult` leaderboard, **every entry `has_real_rigor=True`**.
2. **regime divergence (fix A4):** on a fixture corpus seeded with ≥3 momentum-flavored and ≥3 defensive-flavored papers, `select_candidates(brief, corpus, regime_bias='bull')` and `regime_bias='bear'` differ in their top-`paper_budget` sets (Jaccard < 1.0). Catches diversity-theater at the unit level — this is the divergence-exists guarantee, not a Phase-3 embeddings promise.
3. ABSTAIN path → populated SKIP-shaped `_CandidateResult` (`generation_method="debate_abstain"`); a deliberate all-abstain routes through the existing `RIGOR_FAIL` emit (assert no new error code).
4. `DebateUnavailable` on empty pool → honest fallback (assert relabel, not crash).
5. **`pool_size` denominator + gate wiring (fix A1/A6):** assert the value persisted to `num_trials_in_selection` equals the count of conformant proposed specs, and that the debate-aware gate path reads it back as `num_trials` — `assert num_trials == pool_size`. (Untestable until the §5c gate-wiring delta lands; this test ships *with* that edit.)
6. **model threading (fix A3):** with a `_MockBackend` whose `served_model != model_id`, assert every proposer's `FusionProposal.model` reflects the user-selected model, not the env default.
7. **DSL conformance (fix A5):** a proposal emitting `realized_vol_5` is dropped from POOL (not passed to `evaluate_fusion_spec`); assert no `DSLError` escapes.
8. **flag-OFF byte-identical (fix A2):** with `ARCHIMEDES_DEBATE_ENABLED` unset, `_pick_pipeline` returns a legacy label and a grep-equivalent assert proves `_run_debate_candidate` is unreachable.
9. cited-paper union non-empty; transcript sorted by fixed role order before hashing (R3 determinism).

**Hermetic gate:** `env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest backend/tests/test_debate_engine.py -q` → `N passed, 0 failed`.

### Phase 2 — full deterministic panel + GMM risk + rebuttal + all-10 backtests

Add **C-regime** (GMM, Xia §4.4) and **C-prov** (embargo/provenance, Xia 1/2/4). Scale the pool to ~15–20 steers (regime × mechanism × risk-appetite). Add the visible rebuttal round (Bull sees Bear's claims and vice versa). Run `evaluate_fusion_spec` on **all top-10** survivors to build the true leaderboard. Wire Considered Alternatives to render the full leaderboard + per-critic / synth notes. Add the synthesizer-collapse-to-0 optimization.

### Phase 3 — cutover (deletions) + provenance hardening + corpus/RAG prerequisite

This is the PR where the deletions deferred from Phase 1 (fix A2) finally land: collapse `_pick_pipeline` to the `"debate"` constant, delete the standalone fusion runner + dispatch/relabel block, the architect selection, the `portfolio_agent` runners, and the duplicate `generate_strategy` → `_run_fusion_job` endpoint; drop `"architect"` from the mode allowlist. Then provenance hardening: union `consulted_paper_hashes` into the single `methodology_hash` anchored via `ReasoningTraceRegistry`; persist the full leaderboard + verdicts to `strategy_proposals` content-hashed; render the panel transcript + the top-10 leaderboard on the passport; regime-conditional rigor scoring on the passport. **Land the KnowledgeBase pipeline prerequisite (§10, #778)** so the proposer pool diverges on real embeddings, not TF-IDF. Flip `ARCHIMEDES_DEBATE_ENABLED` ON for the demo **only once** `num_trials = pool_size` (the §5c gate-wiring delta), the model-threading (A3), and the divergence test (A4) are verified on the live path; then remove the legacy `_pick_pipeline` branch + eventually the flag.

### Stretch (only if judges want maximal sophistication) — full TradingAgents roster + BYOK model diversity

Expand to the 9-role A-style roster (separate fundamental/sentiment/technical analysts, an aggressive-vs-conservative risk *debate* as LLMs rather than the deterministic gate, a REVISE re-synth loop). Per-role model diversity via BYOK (Nova bull vs. Llama bear). **This is where the 30% Agentic-Sophistication axis peaks — but at materially higher call count, gated behind Phases 1–3 proving the budget + honesty constraints hold.** Explicitly a stretch, not the ship.

### Scale-out compute (Phase-2 infra layer, parallel track)

The **10 real backtests + the rigor stats are embarrassingly parallel and deterministic** — a natural fit for **AWS Lambda**: fan the per-candidate `evaluate_fusion_spec` runs out to metered Lambda invocations, **nanopayment-settled** (x402/Gateway, sub-cent USDC), **freemium with a free cap + wallet-to-pay beyond**. Phase-1 runs the backtests in-process on **EC2** (no infra change needed for the ship); the Lambda fan-out is a Phase-2 infra layer that ties the society directly into the nanopayment marketplace narrative (Circle tools 20% + Traction 30%). The society's clean per-candidate boundary (each backtest is a pure `spec_dict → FusionEvalResult`) is what makes this metered split cheap to add later.

---

## 10. Risks + open questions for Dan

**Risks (with mitigations):**

1. **`num_trials` never actually becomes `pool_size` — the "build on sand" failure (highest-stakes).** The spec's single most important wire targets a route (`api/selection_bias_routes.py`) that derives the denominator internally (`max(len(valid_returns), 1)`, library size) and exposes no caller seam; if the §5c gate-wiring delta (A1) isn't built, the strict-rigor badge is computed against library size and the pool-of-20 multiple-testing inflation goes uncorrected — a *false* PASS. *Mitigation:* A1 is a hard Phase-1 acceptance item with test #5 (which ships *with* the gate edit); **do not flip the flag ON until the persisted-`pool_size`→gate path is proven on the live route.** Hold the exact arithmetic for Önder (§5c OPEN).
2. **Diversity theater on the DEGRADED corpus.** Reality on `main`: the q-fin papers ARE in prod (`corpus_db_count` matches `wc -l data/corpus/manifest.jsonl`, idempotently seeded at startup; the manifest was uncapped from 10,000 in #1635 — ask `/health` for the number, do not quote one from this page), but they are **metadata-only** — `corpus_embedded: false`, `corpus_kg_built: false`, `paper_rag: degraded` (TF-IDF fallback; `sentence-transformers` commented out in `backend/requirements.txt`; the "live" reranker is MiniLM, **not** SPECTER2). `select_candidates` ranks by **integer keyword-hit count first, then recency** — a weakly discriminating primitive across regime steers on abstracts, so bull/bear proposers can converge → "diversity theater" (§4). *Mitigation:* the **unit-level divergence test (#2 / fix A4)** enforces bull≠bear at the fixture level **now** — it is the divergence-exists guarantee, not a Phase-3 promise. The corpus is **real and Dan is driving population** — see the corpus-population plan (`scripts/bulk_ingest_arxiv.py` to refresh; `CORPUS_MAX` defaults to 25000 since #1635; uncomment `sentence-transformers` to flip `paper_rag` → live MiniLM). **Promote the KnowledgeBase pipeline (embeddings + KG) to a named in-spec dependency, tracked as #778** — but as the *quality* lift, not the divergence guarantee. *Honesty trap for the stage:* do **not** claim SPECTER2/RAG until `corpus_embedded` reads `true` and the KB pipeline (`/api/corpus/graph`, currently 503) has run.
3. **Unbacktestable proposals crash a leaderboard entry.** A proposer can emit `realized_vol_N` (validates via `validate_strategy_spec`, raises `DSLError("unsupported indicator")` in `interpret_spec`) or omit `parameter_variants` (→ PBO `None` → fail-closed); a throw inside C-rigor's `evaluate_fusion_spec` loop can take down the whole leaderboard build. *Mitigation:* the A5 pre-backtest conformance guard (`_dsl_conformance_ok`, drop-with-honest-emit before POOL admission) + the per-candidate try/except in C-rigor + test #7.
4. **Model-pick silently ignored (a provenance lie, violates the CLAUDE.md "claims must be true" hard constraint).** `default_fusion()` is `return StrategyFusion()` — model-blind; the debate proposer would inherit Nova regardless of the user's pick, and `served_model` would misreport. *Mitigation:* the A3 constructor seam (`StrategyFusion(model=...)` / `default_fusion(model=...)`) + test #6; persist `FusionProposal.model` (the true served model) per candidate; gate premium models via `enforce_model_entitlement`.
5. **Phase-1 over-reach breaks the live path during the sprint.** Deleting three runners + an endpoint in the same PR that adds the society maximizes blast radius on a build-on-deploy `main`. *Mitigation:* fix A2 — Phase 1 is strictly additive behind a default-OFF flag; the deletions are the separate Phase-3 cutover PR after the society is verified on the live path. This also makes the flag-OFF byte-identical test (#8) honest.
6. **Budget creep.** The proposer fan-out (P≈15–20) is the real N× cost. *Mitigation:* deterministic critics + all-10 backtests cost 0 tokens; `DEBATE_POOL_MAX` cap; synthesizer-collapse-to-0; flag-off during cutover. Read the call count for token cost and the §8 serial-depth column for latency — they are different budgets (see the §8 latency note).
7. **Latency on prod Nova/Bedrock.** *Mitigation:* `asyncio.gather` the proposer pool and Bull∥Bear; stream `agent_iteration` (zero frontend change). Phase-2 Lambda fan-out for the backtests (§9).
8. **Compounding hallucination** (more agents = more error surface, Xia §5). *Mitigation:* `valid_ids` filter on every cited turn; deterministic critics bound the blast radius; V_check is the non-overridable floor.
9. **GMM degradation read as data-driven.** *Mitigation:* C-regime discounts its weight on `status == "degraded"` (the expected steady state) and the passport surfaces "regime risk: rule-based fallback."
10. **Non-replayable agent ordering** from Bull∥Bear concurrency. *Mitigation:* sort transcript by fixed role order before hashing.
11. **The synthesizer is new code with no analog** — highest defect density. *Mitigation:* keep it a thin rank/ABSTAIN + rationale JSON adapter; reuse `extract_json` + anti-hallucination filters.
12. **Execution-agent boundary (scope fence).** This society is **generation-only.** The agentic keeper/rebalancer is a sibling first-class pillar with its own spec — the live-execution path lives in `services/strategy_signal_evaluator.py` (now a loud FLAT-with-reason fallback after #783's DSL `_spec_signal` evaluator merged; completion tracked as #784). The generation-only fence holds: nothing in this spec touches live execution.

**Open questions for Dan:**

- **A. Pool size P for the demo.** The reshape says ~15–20 proposed, top-10 backtested. Is P≈15 the demo target, or trim to P≈10–12 for budget/latency while keeping the top-10 leaderboard intact? (`DEBATE_POOL_MAX` makes this a config call.)
- **B. `num_trials` denominator — Önder's call.** Raw `pool_size` (count of conformant proposed specs), or pool size adjusted for library-context selection? This changes the DSR deflation; the raw-vs-conformant precision (A6) is part of the same question. Confirm Önder owns and signs off before the §5c gate wiring lands.
- **C. Risk agent — deterministic (recommended, decided) or LLM?** Decided deterministic for non-votable Hierarchy-of-Truth + token savings. The stretch phase can add an LLM aggressive-vs-conservative risk *debate* on top if judges want visible risk argument.
- **D. Corpus embeddings before the demo?** The corpus is real (10k papers) and Dan is driving population; do we flip `sentence-transformers` on (MiniLM, one-line requirements change) for the demo, or ship on keyword+recency selection and treat embeddings as fast-follow (#778)? The divergence test (#2/A4) makes the keyword path *honest* either way, but embeddings are what make the diversity *genuine* rather than fixture-passing.
- **E. Cutover timing for the flag.** Phase-3 flips `ARCHIMEDES_DEBATE_ENABLED` ON and removes the legacy `_pick_pipeline` branch + the deferred deletions. Confirm the cutover lands before the demo (so the society is the live path), or stays flag-gated for the demo with a manual toggle.
- **F. Process to-do (not a spec edit).** `danielscoffee`'s APPROVE asked for a status issue documenting the real agent-engine state and linking this spec. File it.

---

### Honest bottom line

This ships because it's ~80% reuse (fusion grounding via `StrategyFusion.propose`, `evaluate_fusion_spec`, the `run_generation` loop, the SSE vocabulary, the rigor/V_check/persist tail) and ~20% new (the proposer fan-out, four deterministic critics, one society runner, one thin synthesizer). The reshape — a **larger proposed pool, top-10 all backtested for real, a true leaderboard, the user picks K=1** — adds **zero LLM cost** for the backtests (deterministic Python) and buys a far stronger Agentic-Sophistication story. The topology, the deterministic-critic budget trick, and the §0 source-accuracy work are sound and endorsed. The spec is blocked only by the three wiring claims that did not match the live code — **A1** (thread `pool_size` into the gate; it derives `num_trials` from library size today), **A2** (additive Phase 1; deletions deferred so flag-OFF is byte-identical), **A3** (the `StrategyFusion(model=...)` seam; `default_fusion()` is model-blind) — plus **A4–A6** (the unit-level divergence test, the pre-backtest DSL conformance guard, and the precise `pool_size` denominator). Fix those six and Phase 1 is a clean, hermetic, flag-gated, additive PR. The single most important wire is `num_trials = pool_size` at the external gate (via the §5c delta); the single most important honesty surface is the StockBench-grounded first-class ABSTAIN. The corpus is real and Dan is driving population; the remaining prerequisite is real embeddings (#778) so the diversity is genuine, not theater.

---

## 11. Appendix — Archimedes DSL contract the proposer MUST obey

The proposer emits a `strategy_spec` JSON that must pass `validate_strategy_spec` (`services/strategy_dsl.py`) **and** be backtestable by `interpret_spec` (`services/dsl_to_backtrader.py`). The validator/interpreter gap below is load-bearing — and it is exactly why the §5b `_dsl_conformance_ok` pre-backtest guard (fix A5) exists: a spec that validates but won't interpret must be dropped from POOL with an honest emit, never allowed to throw inside C-rigor.

**Required top-level fields (all 7):** `name` (non-empty str), `asset_universe` (`list[str]`, ≥1; **overridden** by the platform via `derive_asset_universe(brief.asset_classes)` — list real tickers), `rebalance_frequency` (`daily|weekly|monthly`), `entry` + `exit` (condition trees), `position_sizing` (`dict` with `.type`), `source_arxiv_ids` (`list[str]`). There is **no `look_ahead_safe` field** — the proposer never declares its own look-ahead safety, and there is no field in which it could: the LEAK verdict is DERIVED from the validated spec by the structural audit in [`dsl_lookahead_audit.py`](../../backend/archimedes/services/dsl_lookahead_audit.py), which fails any spec using a construct outside the audited interpreter surface. A legacy spec still carrying the key validates fine and the key is ignored in both directions. Optional: `parameter_variants` (`dict[str, list[number]]`, each key an indicator alias used in entry/exit, each list 2–8 numerics — **required for a non-`None` PBO**; <2 variants → PBO `None` → fail-closed). **The proposer SHOULD always emit a `parameter_variants` grid on its entry indicator; if it does not, the A5 guard injects a default grid so PBO is computable.**

**Condition trees:** single-key dicts. Logic: `and`/`or` (value = list of **≥2** conditions), `not` (value = one condition). Comparison: `gt`/`lt`/`gte`/`lte` (value = list of **exactly 2** operands). Operands: a number, a price operand (`close|open|high|low|volume`), or an indicator alias `"{indicator}_{period}"`.

**Two gaps the proposer MUST respect (enforced by the A5 guard):**
1. **Do NOT emit `realized_vol_N`.** It passes the validator (it is in `INDICATOR_NAMES`) but the interpreter's `_make_indicator` only handles `sma, ema, rsi, momentum` — `realized_vol` raises `DSLError("unsupported indicator")` at interpret time, which would otherwise throw inside C-rigor. Use only `sma_N`, `ema_N`, `rsi_N`, `momentum_N` (int N ≤ 10000). The `_dsl_conformance_ok` guard rejects any spec whose indicator aliases fall outside `{sma, ema, rsi, momentum}` before it reaches `evaluate_fusion_spec`.
2. **`equal_weight`/`inverse_vol` sizing validate but silently fall through to full-invest** in the interpreter. Only `full_invested_when_in_market` and `volatility_target` (requires `annual_pct > 0`) produce distinct behavior.

**Admissibility:** a Tier-1 (`FusionEvalResult.admissible`) result requires the backtest to run on **real** data (not the default deterministic synthetic feed; `data_source != "synthetic"`). That is the caller's data-feed responsibility, not the spec's — the proposer controls validity + backtestability; admissibility is gated downstream on data provenance.
