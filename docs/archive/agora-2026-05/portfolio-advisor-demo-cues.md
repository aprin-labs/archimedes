> **ARCHIVED 2026-07-28 — historical. See [`docs/archive/demo-script-lepton.md`](../demo-script-lepton.md)**
>
> May-2026 Agora demo cue sheet.

# Portfolio Advisor — 60-second demo cue card

> **Status:** Supplement, not replacement. Dan owns the master pitch deck +
> demo script at ``demo-script-pitch-deck-outline.md`` (routed to the private docs repo, 2026-08-19).
> This file is **the verbatim cues for the Portfolio Advisor moment** inside
> that demo — the WOW #3 (post-fusion, post-corpus): "here's what Archimedes
> actually recommends, with every number defensible."
>
> Read the master script first. This is the page you put on the second monitor
> while running the live demo.

## Pre-demo checklist (90 seconds)

- [ ] Open `https://archimedes-arc.com/generate` in a fresh tab (Portfolio Advisor preview now renders on /generate post-PR #216; the legacy `/intelligence/advisor` route was retired in the spine-plus-v2 consolidation)
- [ ] Already-warmed cache: hit the page once 5 min before so yfinance is in cache
- [ ] Risk profile selector starts on **Moderate**
- [ ] Devtools closed; screen at 1.25× zoom for stage readability
- [ ] Backup screen recording open in a hidden tab (in case live yfinance is slow)
- [ ] If `agent.used: true` (LLM creds wired per Issue #120) → use the **agent script** below
- [ ] If `agent.used: false` (rule-based only) → use the **rule-based script** below

---

## Path A — Agent live (Issue #120 resolved, `agent.used: true`)

### Beat 1 — Rigor is visible (0–15s)

> *"This is the Portfolio Advisor. A user with idle USDC describes their risk
> appetite — Moderate — and Archimedes builds the portfolio. Look at this row
> here:"*  **[Point to the "Selection-Bias Rigor (Tier-1 Gate)" panel.]**
>
> *"6 of 7 picks pass our admission gate. We're not just citing Sharpe ratios —
> we discount them by López de Prado's Deflated Sharpe, we run probability of
> backtest overfitting, we test walk-forward out-of-sample. These four numbers
> are the wedge. 96 other Arc HackMoney AI-portfolio submissions don't have
> them."*

### Beat 2 — The agent is actually investigating (15–35s)

> **[Click the "Agent investigation trace" disclosure under the thesis card.]**
>
> *"This is the agent's tool-use trace. It's not just an LLM saying 'buy NVDA' —
> it ran `get_asset_stats` on candidate names, `get_correlation` on its top
> picks, `stress_test_portfolio` on its tentative allocation. Each of these is
> a real tool call against live yfinance + our covariance optimizer."*
>
> **[Point to a specific line — ideally a correlation check that affected a
> pick.]**
>
> *"Here — it checked correlation between [pick A] and [pick B]. Saw it was
> above 0.6. Dropped one of them because the diversification was illusory.
> That's the difference between Claude 'making up a portfolio' and an agent
> doing research."*

### Beat 3 — The provenance is on Arc (35–55s)

> **[Scroll to "Reasoning Trace" panel.]**
>
> *"Every recommendation produces a keccak-256 of the canonical content —
> regime, picks, paper anchors, market context. The hash you see here is
> deterministic: I can give you the JSON, you can re-derive this hash offline
> with any keccak library and verify against our `ReasoningTraceRegistry`
> contract on Arc."*
>
> *"This is non-custodial portfolio management with **proof of reasoning**.
> The agent could be lying. The hash can't."*

### Beat 4 — Tie back (55–60s)

> *"Paper-grounded. Rigor-gated. Optimized with proper Kelly math under a
> covariance constraint. Stressed against six historical scenarios — there's
> the panel — and anchored on-chain. That's the whole stack."*

---

## Path B — Rule-based only (`agent.used: false`)

If LLM creds aren't yet wired, the live demo loses Beat 2's reasoning trace.
**Don't try to fake it.** Substitute this:

### Beat 1 — Rigor is visible (0–15s)

> *Same as Path A.*

### Beat 2 — The math is real (15–35s)

> **[Point to the Variance Decomposition + Top Correlations tables.]**
>
> *"Even without the agent layer, the optimizer is doing real work. The
> portfolio is constructed with constrained Markowitz under a Kelly objective:
> γ-mapped to the risk profile, identity-shrinkage covariance, per-asset cap
> at 20%. This table here shows Euler variance decomposition — for every
> percentage of weight, here's what percentage of portfolio variance it
> actually contributes."*
>
> **[Point to the Top Correlations table.]**
>
> *"And here's why: top correlations across picks. The optimizer saw NVDA-QQQ
> at 0.6 and accordingly down-weighted one. This is Citadel-style risk
> attribution, not Sharpe-by-itself."*

### Beat 3 — Provenance + stress (35–55s)

> **[Scroll between Stress Tests + Reasoning Trace panels.]**
>
> *"Six historical scenarios baked into every response: 2008, 2022, COVID,
> energy supercycle, EM/FX crisis, crypto winter. Worst case for this profile:
> 22% drawdown in a COVID-style liquidity event. Best case: +8% in an EM/FX
> crisis where our USD/TRY position pays. We don't hide this."*
>
> *"And every recommendation has a keccak hash you can verify against Arc.
> Non-custodial, paper-grounded, proof-of-reasoning."*

### Beat 4 — Honest closer (55–60s)

> *"When the LLM agent path lights up — and that's a deploy-config item, not
> code — you'll also see the agent's tool-use trace explaining each pick. The
> math is already real; the natural-language reasoning is one env-var away."*

---

## Q&A — likely judge questions, scripted

**Q: "What's your alpha source?"**
> *"The strategies are paper-grounded — Faber 2007, Moskowitz-Ooi-Pedersen
> 2012, Moreira-Muir 2017, George-Hwang 2004. We're not claiming proprietary
> alpha; we're claiming **proof-grounded portfolio construction**. The edge
> is that every position is auditable to its source paper, every backtest
> survives selection-bias correction, and every allocation is solved via a
> proper constrained Kelly MVO. That's a defensible product even when the
> alpha is the literature's."*

**Q: "How do you know the strategies aren't overfit?"**
> *"That's exactly what Deflated Sharpe and PBO are for. Bailey & López de
> Prado 2014 — we deflate the Sharpe by the number of trials, we compute the
> probability that an in-sample Sharpe of N corresponds to an out-of-sample
> Sharpe below zero. Those numbers are on the screen. We surface the failing
> ones too. Honesty is the credential."*

**Q: "Why not just hold an index fund?"**
> *"Two answers. One: a user who wants thoughtful portfolio management gets
> something the index can't give — paper-grounded provenance, regime-aware
> rebalancing, stress-tested risk attribution. Two: this is also a substrate
> — once the framework can replicate any quant paper rigorously, the same
> machinery extends to any signal the literature publishes next month."*

**Q: "What if the LLM hallucinates a paper anchor?"**
> *"Two guards. (1) The system prompt restricts anchors to a fixed allow-list
> of strategy IDs that exist in our library — anchors outside the list get
> rejected at parse time. (2) Even if it slips through, every pick's
> `paper_anchor` is checked against `strategy_code_path` substring + an alias
> table. Worst case is a fallback to a default strategy, which we log — not
> a phantom paper."*

**Q: "Can I trust the hash?"**
> *"Keccak-256 is deterministic. The canonical JSON we hash is also
> deterministic — we sort the strategies list so set-iteration order can't
> change the hash across processes (we fixed this; you can see the commit).
> If you give me the recommendation JSON, you can recompute the hash on your
> laptop and verify it matches the one we returned. That's the point — it's
> not 'trust us', it's 'verify it'."*

---

## Don't say

- "We have alpha" → we don't claim that; the strategies are textbook.
- "It's better than [some specific competitor]" → don't pick fights you don't need.
- "We've live-traded this" → testnet only, by design (per master script).
- "The agent is autonomous" → it's bounded by the strategy library + paper anchors.

## Do say

- "Paper-grounded" / "rigor-gated" / "proof of reasoning"
- "Verifiable on-chain"
- "Honest about what we don't claim"
- "Non-custodial — funds never touch our wallet"

## Backup if live demo breaks

1. **yfinance slow on stage** → switch to the screen recording in your hidden tab. Say: *"For the live one, refresh — the first request takes a moment to warm the cache."*
2. **API 500** → narrate from the recording. Don't pretend it's working.
3. **Wrong regime shown** → that's a Redis cache; say *"The regime detector pulls from a 5-minute Redis cache; mid-demo we're seeing a stale read."* Don't pretend you don't see it.
