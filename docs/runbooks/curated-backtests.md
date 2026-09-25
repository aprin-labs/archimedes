# Running the curated backtests — the only path, and it is manual

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-04
> **superseded-by:** —

**Scope:** producing persisted backtest rows for the **curated** strategy library by running
`backend/archimedes/scripts/run_backtests.py`, and **grading** them — the two are one job.
This is the only mechanism that produces a curated backtest. It runs when a person decides
it should run.

**Grading rides the run (#1746).** A curated strategy's rigor verdict is produced ONCE, by
the real gate, at backtest time, and stored on its passport row; every surface reads that
row and nothing recomputes a verdict on read
([`../adr/rigor-verdict-of-record.md`](../adr/rigor-verdict-of-record.md)). `run_backtests`
grades the library at the end of its own run. § "Grading on its own" is the standalone
job — the one-time backfill, and the re-grade.

**Policy this implements:** [`../adr/backtests-are-frozen-evidence.md`](../adr/backtests-are-frozen-evidence.md)
— a backtest is a one-time artifact of evidence with a stated data window, never revisited on
a clock. The in-app refresh loop that used to do this automatically was deleted in
[#1760](https://github.com/aprin-labs/archimedes/issues/1760) after it re-ran the whole library
in the serving process on every cold boot and got ECS tasks killed by their own container
health check. Read the ADR before arguing for automation here.

**Not in scope:** generated strategies. Those are backtested exactly once, inside the
generation pipeline, and are never re-run by this script or anything else.

---

## 1. When to run it

Three triggers, and only three. Each is a decision someone made, which is the point.

| # | Trigger | Why it justifies a run |
|---|---|---|
| 1 | **A curated strategy file changed** — anything under `analytics-engine/strategies/` | Different code, different evidence. Run it after the change is deployed. |
| 2 | **A data-quality or engine fix that changes results** — a market-data correction, a cost-model change, an analytics-engine fix | The old rows describe a computation we no longer believe. This is the "full library re-run with a before/after diff" case; do it deliberately, not casually. |
| 3 | **An explicit owner decision** — e.g. verifying the curated examples are correct before a demo or a launch | Dan asks; you run it. |

**A calendar is not a trigger.** "It has been a week" is exactly the reasoning the ADR retires.

### What makes an unnecessary run cheap

`insert_backtest_if_missing` dedups on the **content hash of the result artifact**
(`services/backtest_mapper.py::canonical_artifact_hash`). An unchanged strategy producing an
unchanged result inserts **nothing** — the run logs `skip duplicate content hash` and the
summary counts it under `skipped`. So a run against an unchanged library is compute you paid
for and zero rows. That makes trigger 1 safe to over-apply and makes it pointless to run
"just in case".

### What it looks like when a strategy has no row

Some curated strategies legitimately produce **no** persisted row. The pairs family is the
standing example: `run_backtests` computes the artifact, the realized-vol plausibility guard
rejects it, and the script logs

```
REFUSING to persist backtest for <file>: <VolPlausibilityError ...>
```

and counts it under `failed` **without writing anything**. That is fail-closed working as
designed. The passport stays `pending`, which is the honest surface. **Re-running does not
fix it** — the fix is code. (This is precisely what the deleted scheduler could not
understand: a permanently-missing row made its staleness check true forever, on every boot.)

---

## 2. How to run it — production

`run_backtests` needs three things: the prod `DATABASE_URL`, the curated strategy files, and
network egress for market data. The **one-off Fargate task** gives it all three and — critically
— runs it **off the serving tasks**, which is the whole lesson of #1760.

Use the dedicated single-container `archimedes-migrate` task-definition family
([`../../infra/ecs_migrate.tf`](../../infra/ecs_migrate.tf)) with a command override. It exists
for exactly this shape of work: same image, same task/execution roles, same SSM-backed
`DATABASE_URL`, no nginx sidecar, no health check, runs to completion and stops. This is the
same mechanism `.github/workflows/deploy.yml`'s `migrate` job uses for
`alembic_migrate_preflight` — copy its invocation, change the command.

```bash
# Constants are the same literals deploy.yml uses (it cannot run `terraform output`).
CLUSTER=archimedes-cluster
FAMILY=archimedes-migrate
NETCFG='{"awsvpcConfiguration":{"subnets":["subnet-0f412b89a025ca15b","subnet-010efb75b1093ba92"],"securityGroups":["sg-0f21c2773901a14e7"],"assignPublicIp":"DISABLED"}}'

TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$FAMILY" \
  --launch-type FARGATE \
  --network-configuration "$NETCFG" \
  --overrides '{"containerOverrides":[{
      "name":"migrate",
      "command":["python","-m","archimedes.scripts.run_backtests"],
      "environment":[{"name":"ARCHIMEDES_ARTIFACT_DIR","value":"/tmp/archimedes-artifacts"}]
  }]}' \
  --query 'tasks[0].taskArn' --output text)

echo "backtest run: $TASK_ARN"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[?name==`migrate`].exitCode | [0]' --output text
```

Logs land in the `/archimedes/app` CloudWatch log group under the `ecs-migrate-*` stream
prefix:

```bash
aws logs tail /archimedes/app --since 1h --filter-pattern '"backtest run summary"'
```

**Expect this to take a while, and expect the waiter to give up before the task does.** The
migrate family is sized at `ecs_backend_cpu` / `ecs_backend_memory` — **1 vCPU / 3072 MiB**
([`../../infra/variables.tf`](../../infra/variables.tf)) — and a full curated-library run was
observed at ~15 minutes on a 2-vCPU box. `aws ecs wait tasks-stopped` polls every 6 s for 100
attempts (**10 minutes**) and then fails; that is the *waiter* timing out, **not** the task.
Do not re-run on a waiter timeout — re-issue the wait, or poll `describe-tasks` yourself:

```bash
until [ "$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
            --query 'tasks[0].lastStatus' --output text)" = "STOPPED" ]; do sleep 30; done
```

Two runs at once are not corrupting — content-hash dedup makes the second one's inserts
idempotent — but they are two full-library computations and, if one is on a serving task,
exactly the CPU contention #1760 was about. Run one at a time.

### The five things to check before you trust the run

1. **Exit code 0.** A nonzero exit means the script itself failed; per-strategy failures do
   **not** change the exit code — they show up in the summary.
2. **The summary line.** `run_backtests` ends with
   `backtest run summary: {'inserted': N, 'skipped': M, 'failed': K, 'errors': {...},
   'graded': {...}}`.
   `inserted` is the only number that means new evidence. `skipped` is content-hash dedup
   (nothing changed). `failed` needs reading — see § "When to run it"'s `REFUSING to
   persist` note.
   `graded` is the grading pass that ran after them: `{'graded': N, 'pending': M, 'counts':
   {'pass': …, 'fail': …, 'pending': …, 'degenerate': …}, 'errors': {…}}`. `pending` counts
   the strategies with no gradeable persisted series — that number is expected to be
   non-zero (the pairs family lives there permanently) and is not an error. **A `graded`
   block carrying an `error` key means the backtests were written but nothing was graded,
   and — this is the point — nothing was overwritten either: a grading pass that cannot
   read the persisted returns writes no verdict at all rather than blanking every stored
   one to `pending`. Re-run § "Grading on its own" once the cause is fixed; it needs no
   backtest.**
3. **`ARCHIMEDES_ARTIFACT_DIR` was set.** The packaged `/app/analytics-engine/artifacts` is
   **not writable** by the nonroot task user. Unset, the script falls back to a temp dir and
   logs a WARNING; before that fallback existed this was a silent `PermissionError` that froze
   the leaderboard at its 2026-07-01 rows for six weeks. The override above pins it to
   task-local scratch, matching what [`../../infra/ecs.tf`](../../infra/ecs.tf) sets for the
   service container. The durable record is the DB row's `artifact_json`, not the file — the
   ephemeral path is deliberate.
4. **The data window.** Defaults are `BACKTEST_START=2004-01-02` and `BACKTEST_END` = today,
   `SPY` operation, `$100,000` initial cash, 10 bps costs, 5 bps slippage
   (`_read_config()`). If you override any of them, the rows you produce state a *different*
   window from the rest of the library — say so wherever the numbers get used.
5. **Which image actually ran.** `--task-definition archimedes-migrate` resolves to that
   family's last **Terraform-applied** revision, whose image tag is `var.backend_image_tag` =
   `latest` ([`../../infra/ecs_migrate.tf`](../../infra/ecs_migrate.tf),
   [`../../infra/variables.tf`](../../infra/variables.tf)). CI re-registers only the
   `archimedes-backend` family, but it does push `:latest` alongside `:<commit-sha>` on every
   deploy — so the run executes current `main`, which is **not necessarily the digest the
   service is serving**. This ADR's whole thesis is that a backtest is dated evidence, so
   record what produced it:

   ```bash
   aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
     --query 'tasks[0].containers[0].imageDigest' --output text
   ```

### Alternative: `aws ecs execute-command` into a running task

The backend service has `enable_execute_command = true`
([`../../infra/ecs.tf`](../../infra/ecs.tf)), so shelling into a serving task and running the
module there is *possible*.

**Do not do it for a full library run.** That is #1760 reproduced by hand: the library run
pegs the 1-vCPU task, `/health` misses its 5 s container-health-check budget, and ECS kills
the task you are sitting in — taking your run with it. Exec is for reading state, not for
this.

---

## 3. How to run it — local / against a snapshot

The same code path, no AWS. Point `DATABASE_URL` at a local Postgres or a restored prod
snapshot:

```bash
cd backend
DATABASE_URL=postgresql://... python -m archimedes.scripts.run_backtests
```

Run it against a **snapshot first** whenever the run is trigger 2 (a fix that changes
results): diff the pass count before touching prod. A library that comes back with zero
passing rows is a finding, not a deploy.

`backend/archimedes/scripts/audit_backtest_universe.py` is read-only and reports the
cross-store delta — use it for the before/after comparison.

---

## 4. Grading on its own — the one-time backfill and the re-grade

`python -m archimedes.scripts.grade_curated` runs the grading pass with no backtest. It
reads persisted returns, runs the real gate over the full curated cohort, and stores each
verdict with its provenance (`graded_at`, `gate_version`, `cohort_n`) and the four numbers
that run produced. It fetches no market data and computes no backtest, so it is minutes
rather than the ~15 a library backtest run costs.

**Run it in exactly two situations.**

| # | Trigger | Why |
|---|---|---|
| 1 | **The one-time backfill after #1746 / PR-B deploys.** | Before it, nothing ever wrote a curated verdict. Every curated passport reads `pending` and the Library badge says "Not yet graded" until this runs. **This is the deploy step for that PR — merging it grades nothing.** |
| 2 | **A gate change.** New thresholds, a new criterion, a bumped `GATE_CODE_REVISION`. | The stored verdicts name a gate that no longer exists. A re-grade is an explicit, versioned event — that is what this script is. |

**A calendar is not a trigger here either.** A strategy with no new evidence grades to the
same verdict it already has, and `run_backtests` already grades as part of its own run, so
a backtest run does not need a grading run after it.

Production, same one-off Fargate task as § "How to run it — production" with a different
command override:

```bash
TASK_ARN=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$FAMILY" \
  --launch-type FARGATE \
  --network-configuration "$NETCFG" \
  --overrides '{"containerOverrides":[{
      "name":"migrate",
      "command":["python","-m","archimedes.scripts.grade_curated"]
  }]}' \
  --query 'tasks[0].taskArn' --output text)
```

Local / against a snapshot:

```bash
cd backend
DATABASE_URL=postgresql://... python -m archimedes.scripts.grade_curated
```

**What to check.** The script prints its summary as JSON and exits non-zero if it wrote
nothing at all — which is what a run that could not read the persisted returns does, on
purpose: it aborts before the write loop rather than stamping the whole library `pending`
and wiping every `graded_at`. Then verify on the API — the point of the whole exercise is
that the two routes agree:

```bash
curl -s $BASE/api/strategies/$SID            | jq '{status, rigor_gate_status, passes_rigor_gate, sharpe_ratio}'
curl -s $BASE/api/strategies/passports/$SID  | jq '{status, served_status, rigor_gate_status, passes_rigor_gate, sharpe_ratio, graded_at, gate_version, cohort_n}'
```

`rigor_gate_status`, `passes_rigor_gate` and `sharpe_ratio` must match, and the detail
route's `status` must equal the passport's `served_status`. `graded_at` and `gate_version`
being non-null is the proof a real gate ran.

**Read the summary before you read the rows.** If it carried an `error` key, the run wrote
nothing — every row still holds whatever verdict it held before, and the run needs
repeating once the cause is fixed. Only for a run that reported **no** error does
`graded_at: null` mean "this strategy was looked at and had nothing gradeable"; the honest
reasons for that are in § "When to run it" (no persisted series), and it is not a reason to
re-run.

**A strategy this cannot help.** No persisted returns means no verdict, permanently, until
the code that refuses to persist its artifact is fixed. The pairs family is the standing
example (§ "When to run it"). `pending` is the correct surface for it and re-running
changes nothing.

## 5. What this must never do

These are the constraints the ADR fixes in place. A change that violates one of them is a
change to the ADR, not a change to this runbook.

- **No clock.** No cron, no interval, no cadence, no `BACKTEST_MAX_AGE_HOURS`. Backtest age
  is not a reason to re-run a backtest.
- **No boot hook, under any name.** Nothing in the FastAPI lifespan, on any tier, may schedule
  this. Guarded at the choke point by
  [`../../backend/tests/test_backtests_are_frozen.py`](../../backend/tests/test_backtests_are_frozen.py):
  `scripts/run_backtests.py` is the only site under `backend/archimedes/` permitted to import or
  call `run_backtests`, so renaming the loop does not get past it. (The same test also bans the
  `backtest_refresh` / `backtest_scheduler` spellings outright, case-insensitively.)
- **Never in the serving process.** Not `asyncio.to_thread`, not a subprocess of the web
  container. A one-off task, or an operator's shell.
- **Never for generated strategies.** They are backtested once, at generation. If a generated
  backtest is wrong, the generation was wrong — fix the pipeline and regenerate; do not re-run
  the artifact. The same holds for grading: a generated strategy is graded by the generation
  pipeline's own post-backtest write, and `grade_curated` never touches those rows.
- **Never grade from the serving process.** No route, no lifespan hook, no "cheap" cached
  wrapper. A verdict computed during a request is the #1746 defect — two endpoints, one
  strategy id, two answers — whatever it is called. Guarded at the choke point by
  [`../../backend/tests/test_curated_grading_is_write_side_only.py`](../../backend/tests/test_curated_grading_is_write_side_only.py):
  only these two scripts may reach `grade_curated_library`.
- **Do not "fix" a stale board by automating this.** A stale board shows as `pending` on the
  passport, which is honest. The 2026-07 → 08 freeze happened *while* an automated loop was
  armed and running daily; automation is what let it go unnoticed for six weeks.

## 6. Related

- [`../adr/backtests-are-frozen-evidence.md`](../adr/backtests-are-frozen-evidence.md) — the policy, the incident, and the alternatives that were rejected.
- [`../adr/rigor-verdict-of-record.md`](../adr/rigor-verdict-of-record.md) — why grading is a one-time event that writes a row, and what the provenance fields mean.
- [`backtest-results-retention.md`](backtest-results-retention.md) — archiving and pruning the rows this script produces.
- [`../operations/feature-flag-fliplist.md`](../operations/feature-flag-fliplist.md) § DEAD / RETIRED — the retired `BACKTEST_REFRESH_*` knobs, and the inert `BACKTEST_REFRESH_ENABLED=false` pin that the deploy script now strips from the task definition.
- [`operations.md`](operations.md) — the general operations runbook.
