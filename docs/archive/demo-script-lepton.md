# Lepton Demo Video Script — July 6, 2026

> **Target:** 3 minutes 20 seconds of recorded video.
> **Audience:** Lepton judges; treat them as technical operators who will check every
> claim against the live URL and the repo.
> **Honesty rule (non-negotiable):** every spoken claim must be backed by the live path
> described in the Claims-Integrity Checklist below. If a live path is uncertain on
> the day, use the fallback column — never wing a number or a badge.

---

## Shot Table (21 rows, ~3:20 total)

> **Column key**
> - `t` — clock position (mm:ss from recording start)
> - `screen` — what is visible to camera
> - `action` — demo-runner's mouse/keyboard move
> - `voiceover` — exact or near-exact spoken line
> - `fallback` — what to do if the live path hiccups

| # | t | screen | action | voiceover | fallback if live path hiccups |
|---|---|--------|--------|-----------|-------------------------------|
| 1 | 0:00 | `archimedes-arc.com` landing page | Land on root; hero and tagline visible | "Archimedes is Linus for quantitative finance. Brief in, rigor-gated strategy out, every reasoning step traceable to its source paper." | n/a — static page |
| 2 | 0:15 | Landing — spine visualization | Scroll to show the five-step spine (Generate → Rigor Gate → Execute → Monitor → Explore) | "The spine is five steps: describe your intent, run the multi-agent debate, survive the rigor gate, deploy into a non-custodial vault, and build a compounding library of what worked and what didn't." | n/a — static page |
| 3 | 0:30 | `/generate` — full page loaded | Click Generate in nav; pause so the model selector, brief input, and job table are visible | "This is the instrument. Model selector at the top — we pick the LLM cost point. Below it, the brief. At the bottom, every generation job you have ever run." | n/a — static page |
| 4 | 0:38 | Generate page — example-brief chips | Hover over the three example-brief chips | "Three example briefs ship on the page. We're going to use the one that has already passed the gate — momentum, quality, and a gold hedge across major ETFs with vol-managed sizing." | Click any chip; the dogfood brief is the first one |
| 5 | 0:46 | Generate page — brief filled | Click the momentum+gold chip; brief populates textarea | "Assets auto-fill: SPY, QQQ, GLD, IWM, VTV. We hit Generate." | If chip prefill fails: type brief manually |
| 6 | 0:52 | Generate page — job table, new row | Click Generate; new row appears with status QUEUED → RUNNING | "The request queues a background job — no blocking spinner. The debate society is now running: proposer pool, adversarial rebuttal, four deterministic critics, synthesizer." | If POST fails: drill directly into a pre-seeded completed-job row from a prior run |
| 7 | 1:00 | `/library` — Strategies page | Navigate to Library while job runs in background | "While that runs — the library. Paper-grounded example strategies, real backtest rows, live PBO and DSR chips. Every row is backed by actual backtrader data." | If library is slow to load: hover on a cached row |
| 8 | 1:10 | Library — a strategy card showing rigor chips | Click a Tier-1 strategy card (one with PASS badge) | "DSR p-value 0.95 or above, PBO below 0.5 — those are the thresholds. Most strategies that enter the pipeline never come out the other side. That's the point." | Use any card with a visible DSR/PBO chip |
| 9 | 1:20 | Generate page — job table, row DONE | Navigate back to Generate; click the completed job row | "The job is done. Click the row — we drill into the generation stream." | If job still running: drill into the pre-seeded completed row from the pre-kick |
| 10 | 1:28 | Generation stream — SSE event log | Let the stream replay (Last-Event-ID replay shows full history) | "Proposer pool fanned out across regime-and-mechanism steers — bull momentum, bear volatility-managed, carry, breakout, mean-reversion. The adversarial round ran: bull versus bear, one rebuttal each. Then the deterministic critics: C-rigor backtested every spec, C-null checked it beats buy-and-hold, C-regime read the live market, C-prov verified every citation is in the corpus." | If SSE drops: navigate back (← Back to generations) and re-drill; Last-Event-ID replay reconstitutes the full history |
| 11 | 1:48 | Generation stream — 'Considered N candidates' button | Click "Considered N candidates" button at stream bottom | "Here is the wow moment: every candidate the society evaluated, ranked. One won. The rest are here with their reject reasons." | If stream button is not visible: use the pre-seeded completed job which always has a candidates endpoint |
| 12 | 1:52 | Considered Alternatives modal | Modal open; alternates visible with DSR/PBO chips and reject labels | "Two candidates failed C-rigor — DSR p-value below 0.95. One was abstained by the regime critic. The winner is at the top with its rigor verdict." | If modal loads empty: note that the job may have produced a single candidate; proceed to passport |
| 13 | 2:02 | Considered Alternatives modal | Point at a failed candidate's reject reason | "That strategy was outranked. Its PBO was 0.52 — just above the 0.5 threshold. The gate caught it. Honest failure is the wedge — the industry doesn't show you this." | Narrate over any candidate that failed rigor |
| 14 | 2:10 | Strategy Passport for the winner | Click 'View strategy passport →' on winner row | "The passport. Four gate panels: Deflated Sharpe Ratio p-value, Probability of Backtest Overfitting, out-of-sample Sharpe, look-ahead audit. All computed on real persisted returns." | If winner has no strategy_id yet (DB write lag): navigate to the Library and open the most recent strategy with a DSR/PBO badge |
| 15 | 2:20 | Strategy Passport — rigor panel | Hover the DSR panel; scroll to equity curve | "A DSR p-value at or above 0.95 means the excess Sharpe is positive at 95 percent one-sided confidence, under standard errors robust to fat tails and autocorrelation. For a generated strategy that also deflates against the candidate pool the society tried; for a curated one num_trials is 1, so there is no multiple-testing correction — say so if asked, it is on the Architecture page. PBO below 0.5 means the probability of backtest overfitting is below the coin-flip line. OOS Sharpe is positive. Passes all four gates: DEPLOYABLE." Read the badge on screen; do not quote a pass count. | If passport shows 'pending' (returns not yet written): narrate that the gate is running and will resolve in seconds; switch to a pre-existing library strategy with a PASS badge |
| 16 | 2:32 | Terminal — `scripts/agent_journey.py` | Switch to terminal with script visible (pre-run, output frozen) | "An agent can use this product programmatically — same journey as human. Better Auth account over HTTP, zero browser. Wallet proof is separate only for on-chain actions." | If terminal is not pre-set: narrate over script file in editor. |
| 17 | 2:42 | `/explore` — Explore page | Navigate to Explore | "The Explore page is the universe SSOT — approximately 280 deploy-eligible assets across US equity, international equity, crypto, FX, metals, commodities, and fixed income. Only what the on-chain path can actually settle." | n/a — static data page |
| 18 | 2:50 | `/insights` — Insights page | Navigate to Insights | "Insights is the traction dashboard. The conversion funnel: landed visitors, then wallet-connected, then generation started. Every step is real telemetry, not a fixture." | If funnel shows zero entries: explain it is live telemetry and counts are genuinely sparse on a new EC2; the funnel infrastructure is wired |
| 19 | 2:58 | Library — a Tier-1 strategy — Deploy CTA | Navigate back to Library; click a DEPLOYABLE strategy's Deploy button | "Deploying is the only step that requires a wallet. It creates a non-custodial ERC-4626 vault on Arc testnet with faucet USDC — no real funds, by design. The vault is real; the settlement is real testnet on-chain." | If wallet not pre-connected: show the wallet gate appearing; say "the wallet wall appears only here" |
| 20 | 3:08 | Deposit flow — approve + deposit steps | Show the two-step deposit flow (Approve USDC → Deposit into vault) | "Approve USDC, then deposit. The vault never custodies; the agent's only authorities are rebalance and position sizing — it cannot withdraw to the platform." | If vault TX is slow: narrate over the spinner; arcscan link appears on completion |
| 21 | 3:18 | Landing page — tagline | Navigate back to `/` | "Brief in. Debate society. Rigor gate. Passport. Compounding library. Archimedes — the original empiricist applied to your portfolio." | n/a — static page |

---

## Narrative Arc (one page)

### Hook (0:00–0:30) — "Linus for q-fin"
Open on the landing page. Establish the product in one sentence: an instrument that turns
academic research into rigor-gated investable strategies, not a confident chatbot assertion.
Name the architecture: brief → debate society → gate → passport. Set the honest tone in
the first fifteen seconds — testnet USDC, no real funds, by design.

### Problem (embedded in shots 7–8) — "Most ideas die in the gate"
The Library interlude (while the background job runs) surfaces the problem without a separate
slide: real PBO and DSR chips on every strategy, some passing, some flagged. This is the
point the audience needs to feel before they see the gate run live — that the system is
opinionated about what clears, not permissive.

### Generate live (shots 3–10) — the instrument in motion
The user types a brief (or clicks a dogfood chip) and submits. The job queues, the library
interlude fills dead time, then we return to a completed row. The SSE replay shows the
debate unfolding: proposer pool, adversarial rebuttal, four deterministic critics. The
audience sees the engine working, not just a spinner.

### Gate verdict + Considered Alternatives (shots 11–15) — the wow moment
Open the Considered Alternatives modal. Show candidates that failed — with their reject
reasons and DSR/PBO chips. Then open the winner's passport: four gate panels, equity curve,
DEPLOYABLE badge. The honest-failure panel is the product's primary differentiator.
Say explicitly: "the industry doesn't show you this."

### Agent-as-user (shot 16) — 10 seconds, high impact
Brief terminal cut: `scripts/agent_journey.py --ephemeral` shows an AI agent signing in
through Better Auth and traversing same journey as human. Optional wallet proof remains separate. This is a differentiator worth naming
once. Do not over-explain; 10 seconds is enough.

### Explore + Insights (shots 17–18) — breadth, traction
Explore confirms the universe is real and deploy-eligible. Insights shows real conversion
telemetry, not placeholder numbers.

### Close — the wallet gate and the one funded action (shots 19–21)
Deploy a DEPLOYABLE strategy. The wallet wall appears only here — the entire prior
journey was read-only. Show the two-step deposit (Approve USDC → Deposit into vault).
Return to the landing page for a clean close.

---

## Pre-Recording Checklist

Complete these steps **before the camera starts recording**, in order:

| # | Step | Why |
|---|------|-----|
| 1 | **Kick off the generation job NOW.** Submit the momentum+gold dogfood brief at `archimedes-arc.com/generate`. Note the job_id. | The 2vCPU box takes 30–90 seconds to complete a debate run. Starting it before the intro means the job is ready by shot 9. |
| 2 | **Confirm a pre-seeded completed job exists in the DB.** Log in, open `/generate`, verify the job table shows at least one DONE row with a strategy that has a PASS rigor badge. If not: run `python scripts/agent_journey.py --base https://archimedes-arc.com --ephemeral` and wait for completion. | Insurance if the live job (step 1) is slow or the SSE drops terminally. |
| 3 | **Account session active.** Sign in with test account. Link testnet wallet only before on-chain demo. | Generation requires Better Auth account; wallet linking never creates session. |
| 4 | **Library seeded.** Open `/library` and confirm at least 5 strategies are visible with real DSR/PBO chips (not just "pending"). | Shot 7–8 require visible rigor chips. If library is sparse, run the analytics-engine backtest fixtures to populate. |
| 5 | **Terminal pre-loaded.** Open a terminal with `scripts/agent_journey.py --base https://archimedes-arc.com --ephemeral` output already frozen on screen (run it and let it complete before recording). | Shot 16 is a 10-second cut; no live re-run during recording. |
| 6 | **Testnet wallet funded with faucet USDC.** Confirm balance at `faucet.circle.com` (20 USDC / 2h, USDC-is-gas). | Shots 19–20 require an actual vault deposit. |
| 7 | **Test SSE reconnect.** Drill into the pre-seeded completed job, verify the stream replays the full history. Navigate away and back — confirm Last-Event-ID replay works. | Shot 10 fallback depends on this working. |
| 8 | **Browser.** Chrome private window, `archimedes-arc.com`, zoom at 100%, 1920×1080, cursor visible. Close all DevTools. | Clean recording; no localhost/port confusion. |
| 9 | **If generation is still slow at recording time.** During shots 7–8 (Library interlude), open a second tab and check job status. If it is not DONE by shot 9, switch to the pre-seeded completed row — the narration holds either way. | The job table shows both live and pre-seeded rows; clicking any DONE row produces the same demo beat. |

---

## Claims-Integrity Checklist

Every spoken claim below is mapped to the code or live path that backs it. If a claim
cannot be backed on the day, the fallback column governs what is said instead.

| Spoken claim | Backed by | Status | Fallback if uncertain |
|---|---|---|---|
| "Multi-agent debate society: proposer pool across regime-and-mechanism steers" | `backend/archimedes/agents/debate_engine.py` — `_STEERS` = 3 regime × 6 mechanism steers; `_propose_pool()` fans across them | **VERIFIED** | n/a |
| "Adversarial rebuttal: bull versus bear, one rebuttal round" | `debate_engine.py` — `_debate_round()` runs 4 turns: bull-r1, bear-r1, bull-r2, bear-r2 | **VERIFIED** | n/a |
| "Four deterministic critics: C-rigor, C-null, C-regime, C-prov" | `debate_engine.py` — `_critic_rigor()`, `_survives_null()`, `_critic_regime()`, `_critic_prov()` | **VERIFIED** | n/a |
| "One winner persists; rest surface in Considered Alternatives with reject reasons" | `debate_engine.py` `build_leaderboard()` (K=1 leader + alternates); `ui/src/components/RejectedCandidates.jsx` — Considered Alternatives modal | **VERIFIED** | n/a |
| "Rigor gate: DSR p-value ≥ 0.95, PBO < 0.5, OOS checks, look-ahead audit" | `backend/archimedes/services/rigor_evaluator.py` lines 499, 508, 535, 573 — thresholds are literal code constants | **VERIFIED** | n/a |
| "DEPLOYABLE badge on strategy passport" | `generation_pipeline.py` line 930; live rigor gate `live_rigor_gate.py` — four-state: pass / fail / pending / degenerate | **VERIFIED** | If badge shows 'pending': say "the gate is still computing; the badge resolves to DEPLOYABLE once returns are written" |
| "Agents use product via Better Auth over HTTP" | `scripts/agent_journey.py` — account session, optional EIP-4361 wallet link, zero browser | **VERIFIED** | n/a |
| "~280 deploy-eligible assets in Explore" | `ui/src/data/assetUniverse.js` — SUPPORTED_ASSETS computed as flat de-duped union across 8 asset groups; manual count = 282 | **VERIFIED** | Say "approximately 280" not "~300" |
| "A Surprise Me button on the Generate page draws from a ~100-brief bank" | `ui/src/data/surpriseBriefs.js` — 124 entries; the first three are the DOGFOOD PROVEN carry-overs from the retired `exampleBriefs.js`, the other 121 are curated copy that has never been run live (#1642). Machine-checked against `cheap_brief_reject` and the supported asset universe only. | **VERIFIED (with caveat)** | Do NOT say "124 validated strategies" or quote a pass rate for the bank — say "a bank of example briefs; three of them have cleared the rigor gate in real dogfood runs". The old always-visible three-example list is gone; nothing from the bank is on screen until the button is pressed. |
| "Non-custodial ERC-4626 vault, testnet USDC, no real funds" | `ui/src/components/DepositFlow.jsx` — approve + deposit into vault on Arc testnet (chain ID 5042002); `ARCSCAN_TX` points to testnet.arcscan.app | **VERIFIED** | n/a |
| "Insights page: real conversion funnel (landed → generation started → ...)" | `ui/src/components/Insights.jsx` — `GET /api/metrics/funnel`; `App.jsx` emits the `landed` beacon once per browser session (sessionStorage-gated) | **VERIFIED** | If funnel shows zero: say "live telemetry; counts are sparse on a fresh EC2 — the funnel infrastructure is wired" |
| "The corpus the generation engine reads" | Live row count is `GET /health` `corpus_papers` / `corpus_db_count`. Do not freeze a number in the script or on `/architecture`. | **Live `/health` only** | Do NOT cite a specific count during the recording; say "a q-fin research corpus" and let the UI show the number |
| "Trend-Crypto-Network Fusion (dsr_p=0.993, PBO=0.336, BTC/ETH/MSTR universe, DEPLOYABLE)" | Cited in session memory; NOT found in repo code or data files. MSTR is not in `ui/src/data/assetUniverse.js` (not deploy-eligible). Specific numeric values cannot be verified from code. | **NOT VERIFIED FROM CODE** — see note below | Do NOT cite this specific strategy by name or cite these specific numbers unless you can confirm them against the live DB before recording; instead, let the live generation run produce its own result and read the real numbers off the screen |
| "Library: real backtest rows with PBO/DSR chips" | `Strategies.jsx` + `strategy_provider.py` + `live_rigor_gate.py` — chips are computed from persisted real returns | **VERIFIED** | n/a |
| "Sole pipeline since PR #880 — no legacy fallback" | `debate_engine.py` module docstring: "The society is unconditional as of Phase-3 (T1.1 flag audit, issue #834)"; `ARCHIMEDES_DEBATE_ENABLED` flag retired | **VERIFIED** | n/a |
| "Payments: real testnet USDC on-chain; settlement not mainnet" | `DepositFlow.jsx` uses real ERC-4626 vault on Arc testnet; x402 nanopayment marketplace (issue #713, Ricardo) is not yet shipped | **VERIFIED (testnet deposit only)** | Do NOT claim "nanopayments live"; say "testnet USDC deposits on-chain; the nanopayment marketplace is the next milestone" |

> **Corpus count note.** Do not freeze a paper count in the script. Live row counts
> are `GET /health` `corpus_papers` / `corpus_db_count` (the corpus probe can timeout).
> `/architecture` reads those fields and must not quote a hardcoded library size.
> **Do not claim "10,000 papers" during the recording** — let the live UI show the
> current number and read it off the screen. If the UI shows a number, quote it; if
> not, say "a curated q-fin research corpus."
>
> **Trend-Crypto strategy note.** The session memory records a first dogfood-proven
> generation pass named "Trend-Crypto-Network Fusion" with dsr_p=0.993, PBO=0.336,
> BTC/ETH/MSTR universe, DEPLOYABLE badge. MSTR is **not present** in the deploy-
> eligible SSOT (`ui/src/data/assetUniverse.js` — single-name equities are held back
> per compliance note in that file). These specific values cannot be verified from
> the repository. **If this strategy exists in the live DB, verify the numbers
> against the actual passport before citing them on camera.** If you cannot confirm
> them, do not name or cite them — let the live generation run produce its own
> result, and read the real dsr_p and PBO values off the screen.

---

## Quick Reference — page routes

| Page | URL path | Verified live? |
|------|----------|----------------|
| Landing | `/` | yes |
| Generate | `/generate` | yes |
| Library | `/library` | yes |
| Strategy Passport | `/strategy/:id` | yes |
| Explore | `/explore` | yes |
| Insights | `/insights` | yes |
| Corpus Explorer | `/corpus` | yes |
| Portfolio | `/portfolio` | yes |
| Learnings | `/learnings` | yes |
| Reasoning trace | `/reasoning` | yes |

---

*Supersedes `docs/demo-script-pitch-deck-outline.md` (routed to the private docs repo,
2026-08-19) for the July 6 Lepton video recording.*
