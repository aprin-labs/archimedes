# Cited literature

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-01
> **superseded-by:** —

The rigor gate and the product claims around it rest on a specific reading of the academic
record. The five papers below are load-bearing: read them first if you want to audit what
Archimedes asserts. Two of the five are cited against us — that is deliberate.

| Citation | What it gives us |
|---|---|
| **Xia et al. 2026** — *Agentic Trading: When LLM Agents Meet Financial Markets*. ESWA. [arxiv 2605.19337](https://arxiv.org/abs/2605.19337) | The audit-grade survey our R3 reproducibility target is built against (**15/19 trading-agent papers are R0**, **0/19 reach R3**). We implement all five named protocols Xia formalizes (Outcome Embargo, Time-Aware Retrieval, Hierarchy of Truth, Source Tracking, `V_check`) as enforced mechanisms — see [`specs/xia-2026-protocols.md`](specs/xia-2026-protocols.md). |
| **Chen et al. 2026** — *StockBench: A Contamination-Free, Closed-Loop Trading Agent Benchmark*. [arxiv 2510.02209](https://arxiv.org/abs/2510.02209) | The harness we ran our own Strategy Generation Agent through. It landed **#15/15 (Sortino −0.91)** — surfaced in [`benchmarks/stockbench-results.md`](benchmarks/stockbench-results.md) rather than hidden. |
| **Bailey & López de Prado 2014** — *The Deflated Sharpe Ratio*. Journal of Portfolio Management. | The first of the four admission controls every Tier-1 strategy must pass. The deflation prices in how many candidates were searched before this one was picked. Detail in [`specs/selection-bias-corrections-spec.md`](specs/selection-bias-corrections-spec.md) and [`rigor-methods.md`](rigor-methods.md). |
| **Bailey, Borwein, López de Prado & Zhu 2014** — *Pseudo-Mathematics and Financial Charlatanism*. Notices of the AMS. | The CSCV-PBO procedure (probability of backtest overfitting) — admission control #2. `fusion_evaluator` computes real CSCV PBO over the parameter-variant grid generated per strategy. |
| **Ang & Bekaert 2002** — *International Asset Allocation with Regime Shifts*. Review of Financial Studies. | Empirical basis for the regime-conditional risk-aversion γ scaling in the Kelly optimizer — γ widens in `risk_off` / `crisis` regimes so a single strategy generates regime-appropriate sizing without needing two parallel agents. |

## Where else literature is cited

Every Tier-1 strategy passport (`/app/library?tab=examples`) links to the paper that backs
it. A reasoning trace that is actually anchored on-chain via `ReasoningTraceRegistry`
carries a `consulted_paper_hashes` field binding the decision to a specific corpus
snapshot — **generation traces are not anchored today.** Check `arc_tx_hash` before
treating a trace as on-chain. The implementation is
[`backend/archimedes/services/source_tracker.py`](../backend/archimedes/services/source_tracker.py).

The corpus is arXiv q-fin **preprints** — not peer-reviewed, and not the same
thing as the five papers above. Do not freeze a paper count here: live row counts are
`GET /health` `corpus_papers` / `corpus_db_count` (the corpus probe can timeout). How it
is built, stored, and selected from: [`corpus-architecture.md`](corpus-architecture.md).

## Related

- [`rigor-methods.md`](rigor-methods.md) — the four gates end to end
- [`quant/admission-criteria.md`](quant/admission-criteria.md) — Tier-1 admission thresholds
- [`analysis/faber-dsr-finding.md`](analysis/faber-dsr-finding.md) — a published strategy
  failing the gate, and why that is the correct outcome
