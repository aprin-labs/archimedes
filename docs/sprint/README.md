# Arc mainnet sprint — session cards

Working cards sharded from `arc-goes-mainnet-sleepy-marble.md` (2026-08-10 measurement).
Purpose: one card per session instead of a 45k-token plan re-read.

> **The source doc is not in this repo** — it lives in the private `docs` repo per
> [`CLAUDE.md`](../../CLAUDE.md). Every `Doc items` label below (A1, A7-surgical, B0–B5, D1)
> dereferences against it and cannot be resolved from here. The cards themselves are
> self-contained; the labels are provenance only. Cards run 64–133 lines, not the ~50 the
> original intent stated.

## State — 2026-08-21

Cards are committed (`16324bd`, PR #1238) and indexed in [`docs/doc-index.md`](../doc-index.md).
The Aug-16 State section this replaces claimed *"zero sprint work has landed"* and listed four
PRs as still open. **Both were accurate when written** — the card was authored 2026-08-16
00:18 +0300, and the four sprint commits (`5327dbf` 00:27, `5c601fb` 00:39, `d7073f1` 00:46,
`ba102c3` 01:09) were written later that same night on sibling branches, reaching `main` only on
2026-08-18 via #1238 and #1242. They are stale now, not wrong then: that work is in, #1224
(`1f3788d`), #1226 (`0d22af7`), #1201 (`ddd21fc`) and #1095 (`8abf1cd`) are all merged, and the
2026-08-20 frontend series landed on top.

> **Scope of this section vs [#1442](https://github.com/aprin-labs/archimedes/pull/1442).** Dan's
> #1442 is the **dispatch state**: which work packages are out, which PRs close which partials
> (#1379, #1401, #1439, #1441), and which cards are retired outright. This section is the
> **verification layer**: what a `grep` finds in the tree today, and where a card's own text is
> wrong. Read #1442 for what is being worked; read this for what is true. Where they disagree,
> re-run the command — and one disagreement is flagged in the table below (cluster-5).
>
> **This section decays in days, not weeks — and it has, twice.** `main` took 170 commits
> between this doc's first draft and its rebase, closing three findings outright (Engine C's
> cost floor, `cost_model_id`-NULL, fabricated `0.0` correlations). In the five days after it
> merged, four more closed: the `/api/rigor/verify` quorum (#1481 → #1484), cluster-2's A4 and
> A8 together (#1485), and `metrics_source` (#1491). **Re-verify before acting on any row** —
> and note the direction of travel: every correction so far has been a finding closing, not a
> finding turning out to be wrong.

### Four things a session must know before picking up any card

1. **The re-run happened ahead of its gate; the gate has since closed behind it.**
   [cluster-1](cluster-1-cost-ssot.md) makes a passing cost-parity test the sole authorisation
   for [the re-run](a6-rerun.md) ("do not run it anyway"). The real A6 blocker was none of the
   card's three hypotheses — it was `PermissionError` on a read-only packaged artifact dir on
   Fargate, at the artifact write that *precedes* the DB insert, so every computed backtest was
   discarded. Fixed 2026-08-18 (`8e6554c`), after which the armed in-app scheduler self-healed
   the leaderboard: fresh curated rows reached prod 2026-08-19/20 with Engine C still charging
   commission only. **`4f60971`, in #1379 (2026-08-20), then put Engine C on the cost floor** —
   slippage at `fusion_evaluator.py:230-232` and `:393-395`, `cost_model_id` stamped on fusion
   rows, and `test_cost_parity.py` now exercising engine C directly. So the floor is complete,
   but it completed *after* those rows published. Two residuals, and only one is tracked:
   #1449 covers the **paper-trading** side (a full-history replay under the new floor will
   register drift on every past date of every open deployment, and the ledger is append-only by
   design). Nothing covers the **curated leaderboard** side: whether the rows now on the board
   were graded before or after `4f60971`, and so whether they need a re-grade. Settle it by
   checking `backtest_results.cost_model_id` on prod against the post-`4f60971` fingerprint —
   it cannot be answered from this repo.
2. ~~**`POST /api/rigor/verify` can answer `passes: true` on four bars.**~~ **Fixed.**
   `passes` was `all(...)` over *evaluable* legs only while PBO and look-ahead were hard-coded
   `not_evaluable` always, so a short series could pass on one leg of four — and `archimedes
   verify` exited `OK` on that boolean. Filed as
   [#1481](https://github.com/aprin-labs/archimedes/issues/1481), fixed by
   [#1484](https://github.com/aprin-labs/archimedes/pull/1484) (`e8a0644`, 2026-08-29): the verdict
   is now a quorum over *runnable* legs — `passes = legs_evaluated == len(_RUNNABLE_LEGS) and
   all(...)` — so a leg that could not run makes the verdict `false` rather than invisible.
   Kept here rather than deleted because cluster-8's row still points at it.
3. **Do not trust this directory's anchors.** Follow session rule 1 without exception, and
   note that this rule's own first draft got a correction backwards. It said `ui/src/siwe.js`
   **"has never existed"**, on the evidence that `grep -rn VITE_ARC_CHAIN_ID .` matched only
   the card. That grep searches the working tree, where a deleted file and a fictional one look
   identical. The file was real: `7415b245` (2026-06-13) gave it
   `VITE_ARC_CHAIN_ID ?? '5042002'`, and `95c9faf7` (2026-07-28) deleted all 89 lines, taking
   the seam with it. So cluster-0's anchor was accurate when written and the seam was **lost,
   not never built** — a regression, which is a different thing to plan around than an
   imaginary file. Verify a "never existed" claim with `git log --all --oneline -- <path>`
   before recording it. The hardcoded ids it left behind (`config.js:10`, `circle-wallet.js:36`,
   `linked-wallets.js:13`, `api.js:16`, `AuthenticatedApp.jsx:71`) are removed by #1240's UI
   half. Four acceptance commands still name files that do not exist (see Corrections).
4. **Two big items shipped by a different design than their card specifies.** #1194's quota
   rebuild closed cluster-5's headline bug without building the meter; #1266's
   `VITE_ROADMAP_SURFACES` flag closed cluster-7's nav work while *reversing* two of its
   decisions. Both are disclosed in code. Read the card for intent, the tree for state.

### Per-card status

Verified against source at `0057518`, 2026-08-21, by an 11-way item-level audit. Fractions are
that audit's own decomposition (acceptance clauses, anti-goals and test asks counted
separately) — a shape, not a score. Adversarial re-verification **covered 3 of 11 cards and
stopped there** (two resume attempts wedged; not retried again). Over 88 claims attacked it
**overturned 16, every one in the less-complete direction** — including this section's own
first draft, which wrongly called the Aug-16 State section inaccurate. So: the three
re-verified cards (this one, cluster-0, cluster-1) are twice-checked; the other eight are
single-source. Treat their DONEs as strong-but-unconfirmed and read every fraction here as an
upper bound on completeness. Re-verification is the cheapest thing to extend if anyone doubts
a row.

| Session | Card | Status | What is actually left |
|---|---|---|---|
| 1 | [cluster-0](cluster-0-unblock.md) | **code done, asks unmet** (23/37) | **Ask 1 did not just go unsent — it was overtaken.** #1129 and #1200 both changed `contracts/src/Vault.sol` (fee caps; NAV decimals + performance-fee share mint) and merged 2026-08-19/20 with **zero human reviews** — every review on #1129 is `copilot-pull-request-reviewer[bot]`, state `COMMENTED`, none `APPROVED`. `CLAUDE.md` makes Dan the sole required approver for contract changes. Raise this before any further contract merge. Also: PyPI `archimedes-cli` unreserved · `PAYMENTS_DRY_RUN` pinned in `ecs.tf` only, still unset in all three compose files and `infra/scripts/setup-ssm-secrets.sh`. A6 is **no longer blocked** — diagnosed 2026-08-18, do not re-run it |
| 2 | [cluster-1](cluster-1-cost-ssot.md) | **done** | Code edits landed in `5c601fb`; `4f60971` closed the Engine C leg and the test gap. "Identical floor everywhere" is now true. Audited at 12/21 before that commit — re-read the row above, not the fraction |
| 2 | [cluster-3](cluster-3-backtest-models.md) | **done at the DB, open at the surface** | A7 shipped in full — the sprint's cleanest win. `4f60971` closed the `cost_model_id`-NULL and fabricated-`0.0`-correlation gaps (both now `None`, with `portfolio_backtester.py:447` no longer defaulting `correlation_to_spy` to `0.0`). What remains is surfacing: provenance fields are declared on `StrategyResponse` but never assigned, and absent from `leaderboard_schemas.py` and all UI |
| 3 | [cluster-2](cluster-2-fusion-engine.md) | **done** | `4f60971` landed A1c; [#1485](https://github.com/aprin-labs/archimedes/pull/1485) (`2f1517e`, "Make the DSL row describe the run that happened") closed A4 and A8 together — the fabricated `date(2004, 1, 2)` is gone and the sleeve label now reaches the row, so an N-asset generated strategy no longer reads as one portfolio backtest without saying otherwise |
| 4 | [cluster-4](cluster-4-strategies-route.md) | **A3 done, §3/§4 open** | Both A3 items landed 2026-08-20 (`757341a`, `14db21d`). `metrics_source` — absent when this was written — arrived in [#1491](https://github.com/aprin-labs/archimedes/pull/1491) (`b49609a`), which went further than the card asked and also names the display-metric chain's `stub_placeholder` link. Still open: §3's TODO markers and §4's unmetered generate endpoint |
| 5 | [a6-rerun](a6-rerun.md) | **executed, prerequisites skipped** (12/26) | The A4 read-path fix was not taken and its stated window ("the one moment in the year") has closed; the A5 fetch memo was never threaded (`run_backtests.py` passes no `fetcher`); **the before/after table — the card's named deliverable — does not exist**; rejection-rate copy never written |
| 6–8 | [cluster-5](cluster-5-meter.md) | **retired, with one live tail** (9/48) | #1442 retires this card (#1194 + #1296/#1300 rebuilt the space; refund/release is now #1441) and that reading is right about the metering design. **One item it does not cover: B5's silent model downgrade is still live** at `generate_routes.py:335` — an entitled premium request is still downgraded to the env default, and #1441 is about refund/release, not this. Either fix it or track it before the card is closed |
| 6–8 | [cluster-6](cluster-6-boot-paywall.md) | **barely started** (7/31) | An x402 generation paywall **shipped** 2026-08-19 (`ab712a1`), flag-off in prod — so the card's premise that no route emits a satisfiable 402 is false. **Neither boot assertion exists**, and `GATEWAY_CHAIN` still silently falls back to `arcTestnet` in three places — plus a fourth, `revenue_sweep.py:88`, which passes the default as a bare kwarg and never reads the env var at all, so the sweep stays on testnet after cutover ([#1495](https://github.com/aprin-labs/archimedes/issues/1495)). A live paywall with no mainnet-chain guard is the dangerous half of this card. No `/api/v1/`, no manifest-honesty edits |
| 9 | [cluster-8](cluster-8-returns-csv.md) | **barely started** (11/53) | None of the specified deliverable: no `returns_import.py`, no `/api/v1/rigor/verdict`, no CSV transport, none of the eight validations, no test file. Of the two prohibitions #1305's overlapping endpoint breached, the `passes` scalar is now fixed (#1481 → #1484); **A1 is not** — gate compute still runs on the event loop rather than `asyncio.to_thread`. Rescope this card against what #1305 and #1484 shipped rather than writing beside it |
| 10 | [cluster-7](cluster-7-ui-surface.md) | **barely started** (14/38) | `sitemap.xml` is the one clean win. A9/A10 are **product reversals**: the card says keep /portfolio and /marketplace live with a banner; #1266 hid them instead. All five orphan deletions untouched — `FusionResult.jsx` (198L) is being *actively maintained while orphaned*, `PortfolioAdvisor.jsx` (477L) leaves `/api/strategies/advisor` with no consumer. No `WorkInProgress.jsx`, so A4/A8/A10 have no banner to render. All three check scripts missing |

## Session rules — apply to every card

1. **Anchor-trust — inverted.** Re-anchor with `grep -n "<symbol>" <file>`, then `Read` a ±40-line
   window. Never `Read` a file whole to get oriented. **This rule overrides every line number in
   this directory.** The Aug-16 note claiming the anchors were "re-verified 6/6 fresh — trust
   them" was retired 2026-08-21: every rule-2 count had drifted and two anchors named a file
   no longer in the tree. (Those two named `ui/src/siwe.js`, which was deleted rather than
   fictional — see item 3 below. The anchors were stale, which is what this rule is for.)
2. **Never read whole** — counts at `f3d4103` (2026-08-21): `strategies_routes.py` (2621) ·
   `Architecture.jsx` (1329) · `rigor_evaluator.py` (1246) · `_rigor_helpers.py` (1176, under
   `services/`, not `api/`) · `main.py` (928) · `fusion_evaluator.py` (884).
   **`portfolio_backtester.py` has left this list** — `c02d5fa` (2026-08-18) cut it 1180 → 694,
   so it is readable whole and cluster-1's "window-read only" note on it is moot.
   These drift by tens of lines per day; re-measure rather than cite.
3. **One cluster per session.** Do not drift into an adjacent item because it is nearby.
4. **Test narrow:** `pytest backend/tests/test_<module>.py -q`. Full suite once, pre-merge.
5. ~~**No subagents, no workflows.**~~ **Superseded** by the owner's execution-style call
   (Dan, 2026-08-20, recorded in [#1442](https://github.com/aprin-labs/archimedes/pull/1442)):
   rules 5 and 7 were written for a solo token-constrained session and do not bind sessions
   running the repo's parallel-agent pipeline (CLAUDE.md § parallel agent fan-out). Rule 6's
   anti-goals stay in force.
6. **Universal anti-goals:** no vitest/playwright · no `python-multipart` · no server-side user
   Python · no DSL-JSON upload · don't delete `_run_fusion_job` · don't repair QuantLab's mocks ·
   no KB pipeline · no distributed meter reaper · don't un-pin `circlekit` · no vectorbt.
   **Never weaken a rigor threshold.** All verified still held except two, both breached by
   post-card work and both **superseding rather than violating** the intent — but cluster-7's
   premise rests on them, so restate it before using that card: `React.lazy` is now in
   `ui/src/App.jsx:13` (the #1194 auth boundary, `e76c1c7`), and `Architecture.jsx` was
   effectively rewritten (896 → 1322 lines, +750/−324 whitespace-insensitive) by eight
   claim-integrity commits on 2026-08-19/20.
7. ~~**Merge discipline: max 2 merges/day.**~~ **Superseded by the same 2026-08-20 call** — see
   rule 5. For the record of what it was measuring: 21 merges landed 2026-08-18, 34 on 08-19,
   58 on 08-20, and `main` took 170 commits in the day to 2026-08-21 alone. The two failure
   modes the rule was written against are still open issues — #1346 (deploy starvation from a
   fast merge train) and #1309 (~2-minute 502 window per deploy) — so the *risk* it named is
   real even though the *rule* is retired; treat those as issues to fix, not a cadence to obey.

## Corrections to the cards

Substantive errors only. Line drift is pervasive and rule 1 handles it.

| Card | Claim | Correction |
|---|---|---|
| cluster-0 | `ui/src/siwe.js:15` hardcodes `5042002` / is `VITE_ARC_CHAIN_ID ?? '5042002'`, recorded as an executed result | The card was right; **this table's first answer was wrong**. The file existed with that exact seam (`7415b245`) until `95c9faf7` deleted it on 2026-07-28. The working-tree grep behind "never existed" cannot see a deleted file. Seam lost, not absent — restored in `ui/src/chain-config.js` (#1240) |
| cluster-0 | "Zero code. Bash and `gh` only." | Contradicted by the card's own two-bug-fix section, which shipped +14/−1 in PR #1239 |
| cluster-1 | `cd analytics-engine && uv run pytest tests/test_cost_parity.py` | Never created; the analytics half went into `tests/test_costs.py:287-378`. The command cannot pass |
| cluster-2 | `pytest backend/tests/test_fusion_evaluator.py` | Path is `backend/tests/services/test_fusion_evaluator.py` |
| cluster-2 | `CostModel.apply_to_broker(cerebro)` is a one-liner per site | Real signature is `apply_to_broker(self, cerebro, feed_names)` — the literal call does not compile |
| cluster-2 | "DSL rows always get `backtest_start=None`" | Overbroad — real-data fusion runs thread real dates. And `:430` is worse than dead: it fabricates a hard-coded `date(2004, 1, 2)` |
| cluster-2 | A8 rename to `dsl-fusion-sleeves` | `_SEARCH_TRACKED_ENGINES` (`selection_bias_routes.py:605`) matches the literal `"dsl-fusion"`; a bare rename breaks num-trials attribution |
| cluster-3 | `look_ahead_audit_passed` is the OR of a real AST audit and a broker-config check | **Neither leg is the AST audit.** `cli.py:378` reduces the same field with `all(...)`, so the OR compares a value against a reduction of itself. The real AST audit is never invoked on this path. Shipped as disclosure (`look_ahead_audit_source`), flag flip deferred to the re-run — which has now passed |
| cluster-3 | Done-when: `grep passes_rigor_gate models/backtest.py` is empty | Contradicts the card's own ask for a retraction comment containing that string. Trust `test_gate_equivalence.py` instead |
| cluster-4 | `pytest backend/tests/test_live_rigor_gate.py` | No such file. Coverage is in `test_gate_equivalence.py`, `test_live_gate_returns.py`, `test_rigor_evaluator.py`, `test_strategies_routes.py` |
| cluster-5 | `enforce_generation_quota()` opens `if wallet: return`, so the cap never applies | Inverted. Rebuilt for #1194: `(request, user_id)`, no bypass, stacked user+IP day caps (10/20). `WALLET_LESS_GENERATION_DAILY_CAP` no longer exists |
| cluster-5 | Proof test: an N+1 route test reads as unlimited today | `generate_routes.py:144` skips enforcement when `TESTING` is set, so such a test passes against unfixed code — the exact trap CLAUDE.md § *a test that passes against the unfixed code proves nothing* warns about |
| cluster-6 | No route emits a satisfiable 402 | False since 2026-08-19: `POST /api/generate/start` emits one with a real circlekit `PAYMENT-REQUIRED` header |
| cluster-6 | Delete "Arc has no mainnet yet" from `llms.txt` | Never in that file. It is at `README.md:28` and, in variant form, `docs/user-stories.md:20`. Retarget the edit |
| cluster-6 | The CLI's `login`/`meter` depend on keys, so keys come first | The CLI shipped without keys — `meter` runs against `GET /api/account/usage` with a cached Better Auth session |
| cluster-7 | "`App.jsx` statically imports all 20 page components (25 imports, zero `React.lazy`)" | The 25 imports moved to `AuthenticatedApp.jsx`; `App.jsx` has 9 and one `lazy()`. The conclusion (a `VITE_*` flag is a policy selector, not a code-elimination device) holds; the premise does not |
| cluster-7 | Delete `assets/logo_old.svg` "(zero importers, verified)" | The file did not exist when the card was written, so that cannot have been verified |
| cluster-7 | The picker offers 281 assets | 252 unique tickers in `assetUniverse.js` `ASSET_GROUPS` |
| cluster-8 | "Mostly a new pure module — cheap to write" | Stale: #1305 shipped an overlapping `POST /api/rigor/verify` + `archimedes verify` on 2026-08-19. Rescope the card against what exists rather than writing beside it |
| a6-rerun | "Rollback is free — the gate can be pinned to a `run_id`" | Only half implemented. `run_id` is persisted (`backtest_repository.py:59`, `:115`) but no reader consumes it — `get_daily_returns` resolves by `created_at desc` and takes no `run_id`. Rollback is add-only survival, not pinning |
| README | "~20 working days remain against a 24-day estimate" | On 2026-08-16 it was 23 business days (22 excluding Labor Day, Sep 7). As of 2026-08-21, ~18 |

## Re-cut vs the source doc

- **B1 splits.** The claim-honesty *outcome* landed via #1354/#1266, but not as cluster-7 specs
  it — see that row. The `routes.js` consolidation and three check scripts stay in buffer;
  #1237 merged 2026-08-18, so the `Breadcrumbs.jsx` coordination window is closed.
- **A2 stays lite.** `backtest_engine` + `cost_model_id` surfaced; the 9-column migration stays
  in buffer. Not fully closed — see cluster-3's row.
- **A7 surgical only.** Landed. Full unification (`cohort_results`, golden vectors) stays buffer;
  the badge/number cohort divergence in cluster-4 §3 is still live and still unmarked.
- Everything already on the doc's cut list stays cut.
