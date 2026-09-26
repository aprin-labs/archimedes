# ADR: Offloading generation to Lambda — measured, and deferred

> **Audience:** Archimedes team
> **Status:** Proposed — verdict **DEFER** (spike for [#1411](https://github.com/aprin-labs/archimedes/issues/1411); feeds [#1217](https://github.com/aprin-labs/archimedes/issues/1217))
> **Date:** 2026-08-30
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** Should the generation pipeline stop running inside the serving Fargate task and run per-invocation on Lambda instead — and can a measured per-generation cost back the x402 quote?
> **Related:** [`backend/archimedes/scripts/run_generation_job.py`](../../backend/archimedes/scripts/run_generation_job.py), [`backend/archimedes/services/generation_cost_model.py`](../../backend/archimedes/services/generation_cost_model.py), [`backend/archimedes/services/cost_meter.py`](../../backend/archimedes/services/cost_meter.py), [`backend/archimedes/services/generation_payment.py`](../../backend/archimedes/services/generation_payment.py), [`infra/spike-1411/`](../../infra/spike-1411/), [`adr/ec2-to-ecs-fargate-cutover.md`](ec2-to-ecs-fargate-cutover.md)

## TL;DR

A real Lambda container function, built from the **unmodified production backend image**
and attached to the production VPC, was deployed and invoked. Every dependency a
generation needs works from inside it: Redis (30 ms), Aurora (2 ms), Bedrock (495 ms),
the baked MiniLM reranker. The image is 2.1 GB against a 10 GB cap. **None of the
suspected blockers is real.**

The one that is real is **cold start**, and it is large relative to the job:
**≈ 5.5–8.8 s** before the pipeline is importable, plus **8.1–9.5 s** more the first
time a run reranks papers (the lazy torch/sentence-transformers load), plus a
**44.6 s** first-invocation penalty after every new image is published. Against a
~48 s generation that is a 28 % latency tax in steady state and more than 100 % on the
first run after a deploy.

**Verdict: DEFER the Lambda lane. ADOPT two of its parts now**, both of which are
inert until something calls them:

1. [`run_generation_job.py`](../../backend/archimedes/scripts/run_generation_job.py) — a
   single-job entrypoint that calls the production `run_generation` with the production
   job store, so events still land in the Redis log the SSE route reads. It works for a
   Lambda invocation, an `ecs:RunTask`, or a `python -m` on a box; the lane is a
   deployment choice, not a code fork.
2. [`generation_cost_model.py`](../../backend/archimedes/services/generation_cost_model.py)
   — the pure arithmetic that turns a `cost_v1` measurement into dollars, with the rates
   supplied by the environment. Not wired into `quote()`; the flat
   `GENERATION_PRICE_USD` remains the default.

The spike also found **a defect that would have shipped with any offload** (§ Blockers,
B1) and **two errors in the way a measured price would have been wired** (§ Quote seam).

## Context

Generation runs in-process on the web tier. `generate_routes.py` fires
`asyncio.create_task(_run_with_cleanup(...))` on the serving event loop; the debate
engine fans backtrader runs across threads (GIL-bound onto one core); the MiniLM
reranker runs up to ten times per generation and has a documented 2026-07-04
loop-starvation incident recorded in its own source comments. Admission control (#1408)
makes this *safe* — it does not make it *scalable*: throughput is capped at
`GENERATION_MAX_CONCURRENT`, and the only lever is to scale the whole web tier for a
bursty batch workload.

Two measured corrections to the issue's framing, taken from the live service on
2026-08-30:

- The web tier is **two** Fargate tasks (`desiredCount: 2`), not one. The
  "one shared task" premise is out of date, and it doubles the cost of the
  zero-architecture alternative below.
- The backend task is 1024 CPU units / 3072 MiB, confirming the ~1 vCPU baseline
  a generation competes for.

## What was built

`infra/spike-1411/` — a throwaway harness, committed so the numbers can be re-taken.

The image is the production backend image plus **three `COPY`s and an `ENTRYPOINT`**:

```
FROM 037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:3dd63e5e…
COPY infra/spike-1411/lambda_bootstrap.py /app/lambda_bootstrap.py
COPY infra/spike-1411/spike_probe.py      /app/spike_probe.py
COPY backend/archimedes/scripts/run_generation_job.py /app/archimedes/scripts/…
ENTRYPOINT ["python", "/app/lambda_bootstrap.py"]
```

Two choices in there are load-bearing for the honesty of the measurement:

- **The base image is the deployed artifact, not a rebuild.** The tag is
  `3dd63e5e727a95e52fc2c22b228fb15ddcfa7370` — the same commit as `origin/main`. A
  rebuild on a Lambda base image would have re-resolved wheels and re-baked MiniLM, and
  would have measured a different thing.
- **No `awslambdaric`.** The AWS runtime client is a C++ extension; installing it means
  a `RUN pip install` inside the artifact under measurement (and cross-architecture
  emulation to build an x86_64 image on an arm64 workstation). Lambda's runtime contract
  is a documented HTTP protocol, so
  [`lambda_bootstrap.py`](../../infra/spike-1411/lambda_bootstrap.py) implements it in
  ~60 lines of stdlib `urllib`. Because no build step runs code, the cross-architecture
  build needs no emulation at all.

The function: `archimedes-spike-1411`, x86_64, 1769 MB (one full vCPU — the same
allocation a generation gets on Fargate today), 900 s timeout, attached to the two
private subnets and **the existing ECS backend security group**. Attaching an existing
SG to a new ENI does not modify that SG, which is how the spike reached Aurora and
ElastiCache without editing a single production rule.

## Measured results

All figures from `aws lambda invoke --log-type Tail` against `archimedes-spike-1411`,
us-east-1, 2026-08-30/31.

### Image and provisioning

| | Measured | Limit / note |
|---|---|---|
| Uncompressed image | **2.147 GB** | Lambda cap 10 GB — **21 % used** |
| Compressed (ECR) | 645 MB | |
| Largest layer | 1.89 GB (`pip install --target=/deps`) | MiniLM bake adds 97.8 MB |
| First push to a fresh ECR repo | 2 m 32 s | later pushes send only the delta layer |
| `create-function` → `Active` | **≈ 200 s** | image optimisation + VPC ENI |

Package size is not a blocker and is not close to being one.

### Cold start

`Init Duration` is Lambda's own number: container start plus interpreter start. The
bootstrap imports the handler lazily *on purpose*, so the Python import cost is timed
separately by the handler instead of being blended into `Init`.

| | Init Duration | Note |
|---|---|---|
| First invocation of a newly published image | **2402.72 ms** | image blocks not yet cached |
| Later cold environments (n=3) | **287.15 / 303.85 / 309.80 ms** | |
| Warm invocation, end to end | **1.80 – 2.47 ms** | |

Import cost, timed inside the handler across three separate cold environments:

| Import step | Run A (blocks uncached) | Run B | Run C |
|---|---:|---:|---:|
| bootstrap: `.env` + SSM (20 parameters) — **also pulls the whole pipeline, see B1** | 4.327 s | 3.888 s | 3.489 s |
| `archimedes.db` | 0.152 s | 0.157 s | 0.121 s |
| `debate_engine` | 0.248 s | 0.260 s | 0.225 s |
| `fusion_evaluator` (pulls backtrader) | 1.714 s | 1.585 s | 1.319 s |
| `paper_rag` | 0.005 s | 0.006 s | 0.007 s |
| `sentence_transformers` (**lazy in production**) | **44.571 s** | 9.546 s | 8.076 s |
| **Total** | **51.017 s** | **15.442 s** | **13.237 s** |

RSS after imports: 549.7 MB. Lambda-reported `Max Memory Used`: **866 MB** at 1769 MB
allocated.

Two readings matter more than the totals:

- **`sentence_transformers` is the cold start.** It is 60–70 % of the steady-state
  import cost and 87 % of the uncached one. In production it is imported *lazily*, inside
  `paper_rag._get_embedding_model()`, so a generation pays it at the first rerank rather
  than at start. The honest decomposition is therefore
  **≈ 5.5–8.8 s to become importable, then ≈ 8.1–9.5 s more at the first rerank** — and
  **≈ 51 s** for the unlucky first caller after every deploy.
- **The 44.6 s → 8.1 s drop is Lambda's image-block cache warming, not variance.** The
  same code, same memory, three environments. Whatever the cold-start budget is, it has
  to be stated as a range with the post-deploy case named, because that case is the one a
  demo hits.

### Every dependency works from inside the VPC

| Dependency | Result | Latency |
|---|---|---|
| ElastiCache Redis (TLS, `rediss://`) | `PING` ok | **30.19 ms** |
| Aurora PostgreSQL **18.3** | `SELECT 1` ok; `papers` = **10 000** | **2.27 ms** |
| Bedrock `amazon.nova-micro-v1:0` via `make_llm_backend()` | real completion; `cost_meter` recorded 13 in / 2 out | **494.7 ms** |
| MiniLM reranker (baked, `HF_HUB_OFFLINE=1`) | `paper_rag_health(probe=True)` → `live` | 0.87 s (already imported) |
| SSM Parameter Store | 20 parameters | 3.5 – 4.3 s |

The Bedrock call went through `make_llm_backend()` rather than raw boto3 deliberately: it
proves the *configured* provider/model/IAM triple and exercises the same `cost_meter`
hook that would price a real generation.

**Not a blocker after all:** VPC access to Redis and Aurora, Bedrock IAM, package size,
and the 15-minute limit (a 48 s job has 18× headroom; even a 90 s cold-start-inclusive
run uses 10 % of the ceiling).

## Blockers

### B1 — CONFIRMED, and it would have shipped: `REDIS_URL` frozen before the secrets load

The first real invocation failed:

```
redis.exceptions.ConnectionError: Error Multiple exceptions:
[Errno 111] Connect call failed ('127.0.0.1', 6379) …
```

Root cause: [`archimedes/services/__init__.py`](../../backend/archimedes/services/__init__.py)
eagerly re-exports `generation_pipeline` for backwards compatibility. So importing **any**
`archimedes.services.*` module — including `secrets_service`, the module the worker needs
in order to *fetch* `REDIS_URL` — transitively imports `job_queue`, whose module-level
`REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")` executes *before* the
SSM load can populate the environment. **No ordering of statements in the worker can win
that race.**

Production has never tripped over this because ECS injects `REDIS_URL` and `DATABASE_URL`
as native task-definition `secrets`, so the module-level read already sees the right
value. The SSM loader is belt-and-braces there — and it is load-bearing for any worker
that does not have a task definition.

The dangerous version of this bug is not the crash. It is the near miss: a worker where a
loopback Redis happens to answer pushes a **paid** generation's events into a store the
SSE route will never read, and reports success. That is the fail-soft-shaped defect
[`architectural-principles.md`](../architectural-principles.md) § fail-soft names as worse
than a crash.

Fixed worker-side, two ways, both tested:
`_bind_job_store()` constructs the store from the environment as it is at run time, and
`_require_configured_store()` refuses to start when `PUBLIC_DOMAIN` is set and the store
resolved to loopback.

**Follow-up worth filing regardless of this ADR's verdict:** the `services/__init__.py`
re-export block makes `import archimedes.services.anything` cost the entire generation
pipeline. It is also why the `bootstrap` row in the import table is 3.5 s rather than the
~0.3 s an SSM round trip should cost.

### B2 — Not measured: the end-to-end `run_generation` duration on Lambda

The AWS SSO session expired between fixing B1 and completing the write-isolated
end-to-end run. What exists is a bounded extrapolation, not a measurement, and it is
labelled as such below.

One full-run invocation (job `spike1411b`) **was dispatched with the fix in place** and
kept running inside Lambda after the CLI's 60 s read timeout gave up on the client side.
**Its `REPORT` line — the real per-run duration and peak memory — is in CloudWatch Logs
and can be read in one command:**

```bash
aws logs filter-log-events --log-group-name /aws/lambda/archimedes-spike-1411 \
  --filter-pattern REPORT --query 'events[].message' --output text
```

(Invoke with `--cli-read-timeout 900`; the default 60 s client timeout is not the
function timing out.)

Extrapolation in the meantime: Lambda at 1769 MB is one vCPU, the same allocation the
generation competes for on Fargate today, running byte-identical code. The compute term
should be within noise of the current ~48 s, with cold start added on top. **Lambda does
not make a single generation faster.** It moves it off the serving loop.

## The cost model, and the quote seam

### What was built

[`generation_cost_model.py`](../../backend/archimedes/services/generation_cost_model.py)
is pure arithmetic over a `cost_v1` snapshot plus a **rate card**:

```
per-generation cost = Σ_model (tokens ÷ 1e6 × vendor rate)     ← measured by cost_meter
                    + billed_seconds × memory_GB × lane rate   ← measured by the runtime
                    + fixed overhead                           ← rate card
```

Three properties are enforced in code, each mirroring a rule the measurement layer
already keeps:

- **The rates are not in the repo.** `GENERATION_COST_RATE_CARD` carries them as JSON.
  Vendor prices change without a deploy, and pricing/margin strategy lives in the private
  docs repo by policy.
- **A missing rate is never a zero.** A model id the card does not know, an incomplete
  `usage_complete`, or an unusable `wall_seconds` produces `complete: false` with a stated
  reason; `assert_priceable()` is what stands between an incomplete measurement and a
  customer price. This is `cost_meter`'s "a missing measurement is never a zero" applied
  to the other side of the boundary.
- **The output is a quote, not a measurement.** Its keys are priced, so
  `cost_meter.assert_measurement_only()` **raises** on it — proved by a test. That is what
  keeps the `generation_costs` row's two-column separation (#1326) enforced rather than
  merely intended.

Lane billing rules are card fields, not constants: Lambda bills per millisecond with no
floor, a per-task lane bills whole seconds with a one-minute minimum, and a 4 s run in
the second lane costs sixty seconds. Pricing a short run as if it were billed
continuously is the easiest way to under-quote, so `billed_seconds()` rounds **up** to the
lane's granularity and then applies its minimum.

### Two errors the spike caught in the intended wiring

**The documented seam is not the real one.** `quote()`'s docstring says it is "the single
seam #1217's measured per-generation budget replaces". It is not. `_price()` is, and it
has **three** call sites per paid request:

| Call site | What it feeds |
|---|---|
| `quote()` | the JSON body of `GET /api/generate/quote` **and** of every 402 |
| `_quote_402()` | `middleware.require(_price(), …)` → the `PAYMENT-REQUIRED` **header** |
| `enforce_generation_payment()` | `middleware.verify(...)` and `middleware.settle(...)` |

Putting measured pricing inside `quote()` would leave the header and the settle amount on
the flat price — **breaking the public-quote == 402-quote invariant in the one place it
is machine-read.** The measured branch must go in `_price()`.

**And `_price()` must become memoized per request.** Three calls that each recompute a
measured price can return three different numbers; the header a caller signs would then
not be the amount the facilitator is asked to settle. A flat env price hides this because
it is constant. Recommended shape:

```python
def _price(request: Request | None = None) -> str:
    card = generation_cost_model.rate_card_from_env()
    if card is None:                       # default today, and the fallback forever
        return _flat_price()
    ...                                    # memoize on request.state for the request
```

**A measured price also invalidates three static surfaces.**
`backend/tests/test_erc8004_identity.py::test_every_surface_that_prices_generation_quotes_the_same_price`
pins the literal `$2.000000` in `agent-registration.json`, `agent.json`, and
`docs/agent-quickstart.md`. A per-generation price cannot be a literal in a static
document; those three surfaces must be changed to *cite* `GET /api/generate/quote`
instead of restating it, in the same PR that flips the model.

### What the quote would say

`GET /api/generate/quote` gains a `cost` block beside the existing fields, never
replacing them:

```json
{
  "pricing_model": "measured_v1",
  "price": "$0.045000",
  "cost": {
    "schema": "cost_model_v1", "lane": "lambda", "complete": true,
    "components": {"llm_usd": "0.03000000", "compute_usd": "0.00138000", "overhead_usd": "0.0"},
    "total_usd": "0.031380"
  }
}
```

with `pricing_model` naming the scheme, exactly as `flat_v1` does now. The per-run lane
is recorded on the measurement side as a `compute_lane` meta key (`cost_meter.set_meta`
accepts it — it carries no pricing vocabulary), which is what lets a stored `cost_v1` row
be re-priced later against the card that was in force.

### The arithmetic, at today's numbers

Lambda us-east-1 x86_64 list price is $0.0000166667 per GB-second plus $0.20 per million
requests; 1769 MB is 1.7275 GB. **The two rates are quoted from AWS's published price
list, not measured here** — re-read them before using this arithmetic for anything but the
lane comparison below, and note that they are exactly the kind of number this ADR argues
belongs on an environment-supplied rate card rather than in a document.

| Scenario | Compute |
|---|---|
| 48 s warm | 1.7275 × 48 × 0.0000166667 = **$0.00138** |
| 48 s + 13.6 s steady-state cold start | **$0.00177** |
| Request charge | $0.0000002 — noise |

Against the ~$0.03 of LLM spend per generation (#1411's measured figure, ~15 nova-micro
calls), **compute is ~5 % of the cost of a generation in either lane.** The issue's
$0.0015 estimate is confirmed to within 20 %. Whatever decides this question, it is not
the compute bill.

## Decision

**DEFER the Lambda lane.** Adopt the entrypoint and the cost model now; do not build the
Lambda deployment.

Why:

1. **Cold start is a latency tax on the flagship interaction.** 13.6 s steady state on a
   48 s job is 28 %; the post-deploy 51 s case is worse than the job itself. Provisioned
   concurrency removes it and simultaneously removes the reason to use Lambda — you are
   paying continuously for a warm capacity you were trying not to reserve.
2. **It does not make a generation faster.** Same vCPU, same code. The benefit is
   isolation and elasticity, and today's concurrency ceiling is 1.
3. **It adds a second deploy artifact.** A second image, a second IAM role, a VPC ENI
   pool, and a second place for configuration to drift — for a workload nothing is
   currently queueing behind. `GENERATION_MAX_CONCURRENT` / `GENERATION_MAX_QUEUE` are
   already absent from [`infra/ecs.tf`](../../infra/ecs.tf) — the admission-control knobs
   run on their code defaults in production because nothing plumbs them — which is the
   same drift class, one lane earlier.
4. **The cheaper interim exists and is one variable.** `ecs_backend_cpu` 1024 → 2048.
   At `desiredCount: 2` that is **≈ $59/month**, not the ~$29.50 the issue's scoping
   comment assumed — it is per task. It needs Dan's cost ack and no new architecture.

**Adopt when any of these becomes true** (each is checkable, not a feeling):

- Sustained `queued` depth > 0 on the admission gate — i.e. callers are actually waiting.
- Generation moves to a fire-and-forget UX where a 15 s start is invisible (an emailed or
  notified result rather than a watched stream).
- The image is slimmed enough that a cold start fits the budget — the lever is
  `sentence_transformers`, which is 60–87 % of it. A CPU-only torch wheel, or moving the
  reranker behind a small service, is the change that would make this decision flip.
- Generation volume exceeds ~33 000/month, where the per-invocation bill starts to beat
  a permanently larger task.

**Do not adopt on cost grounds.** Compute is 5 % of a generation either way.

## Alternatives considered

| Option | Verdict |
|---|---|
| **Lambda container per generation** | Deferred — this ADR. Works end to end; cold start and a second artifact are the price. |
| **`ecs:RunTask` per generation** (kb_runner-shaped) | Not measured — the SSO session expired. Expected to be *worse* on latency: Fargate task launch is tens of seconds and has no warm path at all, where Lambda at least has a 2 ms one. Cheap to measure with the same entrypoint. |
| **Raise `ecs_backend_cpu` 1024 → 2048** | The zero-architecture interim. One terraform variable, ≈ $59/mo at `desiredCount: 2`, needs Dan's cost ack. Scales the web tier for a batch workload — the thing this issue objected to — but buys time honestly. |
| **Keep it in-process, raise `GENERATION_MAX_CONCURRENT`** | Rejected. The starvation incident the reranker's comments record is what admission control exists to prevent; raising the cap re-opens it. |
| **Provisioned-concurrency Lambda** | Rejected. Pays continuously for warm capacity, which is the opposite of the per-invocation pricing that motivated the issue. |

## Consequences

- Two new modules exist and neither is on a payment path. `run_generation_job.py` has no
  importer in the serving path; `generation_cost_model.py` is still not referenced by
  `generation_payment`. The flat `GENERATION_PRICE_USD` is untouched and remains the
  default; no payment flag changed.
  **Update (2026-08-31, #1217):** `generation_cost_model.py` now has one reader —
  `services/generation_cost_rollup.py`, which aggregates its estimates for the admin-only
  `GET /api/metrics/private/cost` dashboard. That is a report, not a charge: the quote seam
  is untouched and the customer price is still flat.
  **Correction (2026-09-03, [#1793](https://github.com/aprin-labs/archimedes/issues/1793)):**
  "neither is on a payment path" was wrong in the direction that costs a payer money.
  `run_generation_job.py` still has no importer in the serving path, but the *generation it
  runs* has already spent an entitlement at enqueue time — a paid generation credit, or a
  free-tier slot ([#1785](https://github.com/aprin-labs/archimedes/issues/1785)) — and the
  `finally` that hands it back when the run delivers nothing lived inside
  `generate_routes._run_with_cleanup`, which this entrypoint never enters. Any lane built on
  it (Lambda, `RunTask`, a worker process) would have kept a payer charged, and a free-tier
  account one generation poorer, for every thin-corpus, crashed, cancelled or timed-out run.
  Both refunds now live in one seam, `generate_routes.release_entitlements_if_undelivered`,
  which is the only thing either run path's `finally` calls. Three tests hold that shape: one
  discovers every release helper on the module and fails if the seam does not reach it, and
  one per run path pins that path's cleanup to the seam rather than to a helper. The discovery
  half matches a naming convention — `_(release|void|refund|restore)_<thing>_(if|when)_undelivered`
  — which the seam's own docstring states as its limit. The offload verdict is unchanged.
- The entrypoint is lane-agnostic on purpose. Whichever lane wins — Lambda, `RunTask`, or
  a worker process — it is a deployment decision on top of the same function, not a fork
  of the pipeline.
- B1's fix lives in the worker, not in `job_queue`. The underlying landmine (a
  module-level `REDIS_URL`, plus a `services/__init__.py` that imports the world) is
  still there for the next caller to find.
- `infra/spike-1411/` is committed as a reproduction harness. It creates nothing on
  import and is not part of any terraform configuration (terraform reads `*.tf` in
  `infra/` only, not subdirectories).

## What the spike deliberately did not do

- **No production writes.** The end-to-end run points `DATABASE_URL` at
  `sqlite:////tmp/spike-1411.db`, and the probe *refuses to run* if it does not — a
  measurement run has no business creating a strategy row in the live library, where it
  would surface on the leaderboard as a real result. The `papers` corpus is **copied**
  out of Aurora (SELECT only) into the throwaway DB so corpus retrieval stays
  representative while every write stays local.
- **No production mutations.** The VPC subnets and the ECS backend security group were
  *referenced*, never edited. No SG rule, no task definition, no terraform.
- **No terraform.** Per the issue's anti-goals. The `GENERATION_MAX_CONCURRENT` /
  `GENERATION_MAX_QUEUE` drift in `infra/ecs.tf` is recorded below rather than patched.
- **No change to the price or any payment flag.**

## AWS resources created — ⚠️ TEARDOWN PENDING as of 2026-08-30

The AWS SSO session expired mid-spike (token lapsed 22:58Z; re-login needs a human at the
browser), so the resources below are **still live** and were not deleted by the session
that made them. Nothing here serves traffic, nothing is attached to the production
service, and the standing cost is an idle Lambda function (billed only when invoked) plus
645 MB of ECR storage — but it is not zero and it should not be left.

```bash
aws sso login --profile ArchimedesDanAdmin
./infra/spike-1411/deploy.sh destroy
```

All tagged `Project=archimedes-spike-1411`, all sandbox-scoped, all in
`037613907429`/`us-east-1`:

| Resource | Name |
|---|---|
| ECR repository (+ one image, tag `probe`) | `archimedes-spike-1411` |
| IAM role (+ inline policy `archimedes-spike-1411-app`, + `AWSLambdaVPCAccessExecutionRole`) | `archimedes-spike-1411-lambda` |
| Lambda function | `archimedes-spike-1411` |
| CloudWatch log group (auto-created) | `/aws/lambda/archimedes-spike-1411` |

Nothing outside that list was created, and nothing existing was modified: no security
group rule, no task definition, no terraform, no production database row. The function's
environment does carry a literal `REDIS_URL` (§ B1 forced it) — it is deleted with the
function, and ElastiCache is VPC-internal with no auth, so nothing is exposed by it. Read
the pending full-run `REPORT` line out of the log group **before** running `destroy`; the
log group goes with it.

## Reproducing

```bash
./infra/spike-1411/deploy.sh build     # derive the image from the prod backend image
./infra/spike-1411/deploy.sh create    # role + function (VPC-attached)
./infra/spike-1411/deploy.sh invoke '{"action":"noop"}'      # Init Duration
./infra/spike-1411/deploy.sh invoke '{"action":"imports"}'   # the import table
./infra/spike-1411/deploy.sh invoke '{"action":"deps"}'      # Redis/Aurora/Bedrock/MiniLM
./infra/spike-1411/deploy.sh destroy
```

A fresh cold environment requires a configuration change (`update-function-configuration`
recycles them); repeated concurrent invocations do **not** — four parallel invokes were
all served by the same warm environment in 2 ms each.
