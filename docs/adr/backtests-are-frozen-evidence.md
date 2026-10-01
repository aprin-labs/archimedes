# ADR: A backtest is frozen evidence — no periodic or boot-time refresh

> **Status:** Accepted
> **Date:** 2026-09-01
> **Owner:** Dan Browne
> **Supersedes:** the rationale in `services/backtest_scheduler.py`'s own module docstring ("prod sat at *pending* for weeks", "a product whose wedge is *autonomous* rigor cannot depend on an operator for its evidence")
> **Superseded-by:** —
> **Question being decided:** The in-app refresh loop re-ran the whole curated library in the serving process on every cold boot and killed ECS tasks ([#1760](https://github.com/aprin-labs/archimedes/issues/1760)). Do we make the refresh cheaper (subprocess + fleet-shared backoff + a longer settle delay), or retire scheduled backtesting entirely?
> **Related:** [`../runbooks/curated-backtests.md`](../runbooks/curated-backtests.md), [`../operations/feature-flag-fliplist.md`](../operations/feature-flag-fliplist.md), [`../runbooks/backtest-results-retention.md`](../runbooks/backtest-results-retention.md), [`num-trials-self-containment.md`](num-trials-self-containment.md), [`../quant/backtest-interpretation.md`](../quant/backtest-interpretation.md)

## TL;DR

**A backtest is a one-time artifact with a stated data window. It is never revisited on a clock.**

- A **generated** strategy is backtested exactly once, at generation, as part of the pipeline that grades it. It is correct from the beginning or it does not ship.
- A **curated** strategy is backtested when its strategy file changes, when a data-quality defect is fixed, or when the owner asks — always by an explicit out-of-band run of `python -m archimedes.scripts.run_backtests`, never from the web tier.
- **No periodic refresh, no boot hook, no staleness check, anywhere.** No backtest is on a clock. The web tier's other scheduled work is untouched by this decision: the paper-trading advance arm (isolated to a child process), the opt-in revenue sweep (`REVENUE_SWEEP_ENABLED`, off today), and one tick loop per rehydrated marketplace publisher.
- Forward performance is the **paper-trading ledger's** job. That is the surface that is supposed to move with time; the backtest is not.

## Context

### What the loop was

`services/backtest_scheduler.py` armed a long-lived asyncio task from the FastAPI lifespan. After a settle delay (`BACKTEST_REFRESH_STARTUP_DELAY_S`, default 180 s) it checked staleness — any curated strategy with **no** persisted backtest row, or a latest row older than `BACKTEST_MAX_AGE_HOURS` (default 168 h) — and, if stale, called the same `run_backtests()` the CLI calls, on a worker thread, then slept `BACKTEST_REFRESH_INTERVAL_HOURS` (default 24 h) and did it again. `BACKTEST_REFRESH_ENABLED` was the kill switch; `TESTING` forced it off.

Its stated reason for existing was operator dependence: `run_backtests` was manual, so the curated library refreshed "exactly as often as a human remembered to run it — which is how prod sat at *pending* for weeks."

### What it actually did

On the 2026-09-01 deploy (task-def 215, ~20:14–20:27 Z) it took the production fleet down repeatedly:

| Time (UTC) | Task `33a36e53` |
|---|---|
| 20:14:11 | started; `/health` 200 at ~0.1 s; ECS marks HEALTHY |
| 20:16:26–48 | a real visitor hits `/api/features`, `/api/explore/assets`, `/api/corpus/overview`, `/api/papers`, `/api/generate/quote`, `GET /api/strategies/` |
| **20:17:11 (+180 s)** | settle delay expires → `run_backtests()` for the whole curated library, in the web process |
| 20:17 | Container Insights: **CpuUtilized 972 of 1024**, memory pinned at 1537 MB |
| 20:17–20:18:21 | container health check (`urlopen(localhost:8000/health)`, timeout **5 s**) fails 3× → **task killed** |
| 20:19:29 | the draining process logs `slow request: GET /api/strategies/ status=200 duration_ms=159933.7` |

ECS replaced each dead task with a fresh one, which booted into the same storm three minutes later. Whether a task survived was a coin flip on what else was running at +180 s — which, on a deploy, is exactly when the first cold visitor request lands. Four structural reasons it recurred on **every** boot:

1. The missing-row trigger fired whenever *any* curated strategy had no row, and the pairs family **never** gets one — `run_backtests` refuses to persist it (`VolPlausibilityError`, realized-vol check). So `needs_refresh()` was true on every cold start, forever.
2. The backoff that existed to stop exactly that (`_last_unresolved_missing`) was a **process-global set**, empty in every new process. Per-task memory cannot back off a per-task event.
3. The module's single-writer premise ("prod runs one backend replica") was false: `ecs_service_min_count = 2`, and every task ran its own loop.
4. The work ran in-process on a **1-vCPU** task. `asyncio.to_thread` does not keep the event loop responsive when pandas/numpy hold the core.

Mitigation on the night: task-def **216** = 215 + `BACKTEST_REFRESH_ENABLED=false`. Both 216 tasks passed +180 s with zero refresh lines and `/health` at 0.45 s from outside. The storm was the loop.

### Why "make it cheaper" was rejected

The obvious fix — `nice -n 19` subprocess, a Redis-backed fleet-shared backoff, settle delay 180 → 900 s — was drafted and would have worked. It was rejected because it answers the wrong question. It keeps a clock on a thing that should not have one, and the clock had already cost something other than uptime:

- **`backtest_results` is ~6.3 GB across 14,857 rows for 96 strategies — about 155 runs per strategy** (measured 2026-08-30, [`../runbooks/backtest-results-retention.md`](../runbooks/backtest-results-retention.md)). Almost all of that is the daily re-run.
- Every reader in the tree defines canonical as **latest by `created_at`**. With a daily refresh, "latest" moves on its own. That moving target is what produced the Sharpe drift in [#1746](https://github.com/aprin-labs/archimedes/issues/1746): the same strategy, the same code, a different number on the board on a different day, with no decision behind the change.

A number that moves with no decision behind it is not evidence. That is the defect, and it is a policy defect, not a scheduling one.

### The honest answer to "prod sat at pending for weeks"

The scheduler's docstring named a real failure. Its remedy did not hold, and the record says so plainly:

- The scheduler landed 2026-07-03 (`018b2170`, "retire the operator-invoked CLI ritual").
- The board's newest row then sat at `backtest_end 2026-07-01` until **2026-08-18** — six weeks — *while the loop was armed and ticking daily*.
- The cause (`8e6554c5`, "Fix the frozen leaderboard refresh: artifact dir is read-only on Fargate"): the packaged `analytics-engine/artifacts` directory is not writable by the nonroot task user, so every scheduled run died at the artifact write, **before** the DB insert.

An unattended loop did not prevent the stale board. It hid it, for longer than the operator ritual it replaced, because nobody watches a job nobody has to run. Automation removed the person without removing the failure — it removed the person who would have noticed.

The answer to a stale board is a **runbook someone runs, plus an honest surface while it is stale** — the passport's `pending` state — not a clock that fails silently. Autonomy is a claim about how a strategy is produced and graded, not about who types the command that produces evidence for the hand-curated ones.

## Decision

1. **Delete `services/backtest_scheduler.py` and its lifespan wiring.** Delete the `BACKTEST_REFRESH_ENABLED`, `BACKTEST_REFRESH_INTERVAL_HOURS`, `BACKTEST_MAX_AGE_HOURS` and `BACKTEST_REFRESH_STARTUP_DELAY_S` knobs from code, from the flip-list's actionable tables, and from the docs.
2. **`scripts/run_backtests.py` is the only way to produce a curated backtest**, invoked deliberately and out-of-band. Procedure: [`../runbooks/curated-backtests.md`](../runbooks/curated-backtests.md). It keeps its content-hash dedup, so re-running an unchanged strategy is a no-op rather than another row.
3. **Generated strategies are backtested exactly once**, inside the generation pipeline, against the window stated on the passport. There is no second run and no re-grade on a clock. If a generated backtest is wrong, the generation was wrong; fix the pipeline and regenerate, do not re-run the artifact.
4. **No periodic or boot-time refresh anywhere** — not in the web tier, not on a scheduled Fargate task, not on the runner box. The follow-up floated in #1760 ("move the refresh off the web tier, like kb-runner") is explicitly **not** taken: relocating a clock keeps the clock.
5. **Forward performance belongs to the paper-trading ledger** (`services/paper_trading.py`). That is the one scheduled tick that is supposed to exist, because a track record is by definition a thing that accumulates with time. A backtest is not.
6. **The passport tells the truth while a backtest is missing.** `pending` is a correct, publishable state. It is better than a number produced by a job nobody asked for.

## Consequences

### Positive

- **The cold-boot storm is gone by construction.** No boot hook, no +180 s spike, no coin-flip on whether a fresh task survives its own health check. Task-def 216 already demonstrated the end state; this makes it the code's behaviour rather than an env pin.
- **"Latest" stops moving.** A strategy's board numbers change only when someone changed the strategy or fixed the data — which is the only reason a number *should* change. The #1746 Sharpe-drift class disappears.
- **~155 rows per strategy stop accumulating.** New rows arrive at the rate of real changes, which makes [`../runbooks/backtest-results-retention.md`](../runbooks/backtest-results-retention.md) a one-time cleanup rather than a recurring chore, and makes the archive-then-prune keep-rules meaningful.
- **The failure mode is visible.** A stale board is now a `pending` passport and a runbook nobody ran — legible — instead of a daily job dying silently at a `PermissionError` for six weeks.
- **One less thing on the 1-vCPU serving task.** The heaviest scheduled work — a full-library `run_backtests()` in the serving process — is gone. What still ticks on the web tier is the paper-advance arm, which refuses to run in-process ([`../../backend/archimedes/services/paper_trading.py`](../../backend/archimedes/services/paper_trading.py) `arm_paper_advance_for_web_tier`); the opt-in revenue sweep ([`../../backend/archimedes/services/revenue_sweep.py`](../../backend/archimedes/services/revenue_sweep.py) `revenue_sweep_loop`, hourly, off today); and one `_run_loop` per rehydrated marketplace publisher. None of them re-runs a backtest. This ADR does not adjudicate those three — it removes the one that took the fleet down.

### Negative / costs we accept

- **A curated backtest is now a person's decision.** If the strategy library changes and nobody runs the CLI, the board is stale until someone does. We accept this: the runbook names the three triggers, and `pending` is honest in the meantime.
- **The env var pinned in the live task definition becomes inert.** `BACKTEST_REFRESH_ENABLED=false` on task-def 216+ now names a flag nothing reads. It was a **deliberate operator action** at the time of this merge — CI deploys clone the live task-def, so it kept riding along. *Superseded:* the deploy script now strips it (and the other three retired knobs) from the `backend` container on every register, so the first deploy after that change drops it with no operator step. It is recorded in § DEAD / RETIRED of the flip-list.
- **No automatic re-grade when the engine changes.** If a fix to the analytics engine or the cost model would change every curated number, that is a re-run someone must decide to perform — a full library re-run, deliberately, with a before/after diff, which is what the A6 sprint card already said it should be.
- **`TESTING` no longer disables anything here**, because there is nothing to disable. The hermetic-suite property (tests never reach yfinance) now holds structurally rather than by a flag check.

## Alternatives considered

| Option | Verdict |
|---|---|
| `nice -n 19` subprocess + Redis-shared backoff + 900 s settle delay | **Rejected.** Fixes the symptom, keeps the clock, keeps the moving "latest" and the 155-rows-per-strategy growth. |
| Move the refresh to a scheduled Fargate task (kb-runner shape) | **Rejected.** Relocating a clock is still a clock. The web tier stops dying; the evidence still drifts for no reason. |
| Keep the age-driven refresh, drop only the missing-driven trigger | **Rejected.** The age cadence is the half that produced the drift. The missing trigger was merely the half that fired on every boot. |
| Refresh only when the strategy file's content hash changes | **Rejected as a scheduler.** This is the right *trigger* — and it is exactly what an operator run already gets for free from `insert_backtest_if_missing`'s content-hash dedup. Building a watcher to notice a change that only ever arrives with a deploy adds a mechanism to do what running the CLI after that deploy does. |

## Verification

- `services/backtest_scheduler.py` does not exist; nothing under `backend/archimedes/` names `backtest_refresh` or `backtest_scheduler` outside comments, in any case — asserted by [`../../backend/tests/test_backtests_are_frozen.py`](../../backend/tests/test_backtests_are_frozen.py).
- **The ban is on the behaviour, not on two spellings.** `scripts/run_backtests.py` is the only site under `backend/archimedes/` that imports or calls `run_backtests` — an AST assertion in the same test. A rebranded loop (`services/evidence_freshness.py::curated_evidence_tick`) passes every token scan and still trips this one, because it cannot produce a backtest without reaching the runner.
- The FastAPI lifespan's source contains no backtest refresh — same test, same shape as [`../../backend/tests/test_lifespan_no_rigor_backfill.py`](../../backend/tests/test_lifespan_no_rigor_backfill.py).
- The four `BACKTEST_*` knobs appear nowhere in the tree's actionable flag tables — enforced in both directions by [`../../backend/tests/test_feature_flag_fliplist_drift.py`](../../backend/tests/test_feature_flag_fliplist_drift.py).
