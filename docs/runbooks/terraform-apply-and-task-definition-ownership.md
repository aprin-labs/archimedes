# Terraform apply, and who owns the backend task definition

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-03
> **superseded-by:** —

Two systems register revisions of the `archimedes-backend` ECS task-definition family, and
until #1799 landed they overwrote each other. This page says who owns what, what that means
for an edit you are about to make, and how to run an apply now.

Read this before `infra/apply.sh --apply`, and before editing `container_definitions` in
`infra/ecs.tf` expecting the change to reach production. It will not.

## The split, in one table

| Attribute of `aws_ecs_task_definition.backend` | Owner | Changed by |
|---|---|---|
| `container_definitions` — images, `environment`, `secrets`, healthchecks, `dependsOn`, log config | **the deploy pipeline** | every merge to `main`, via `.github/workflows/deploy.yml` + `.github/scripts/ecs_rewrite_task_def.py` |
| `cpu`, `memory`, `execution_role_arn`, `task_role_arn`, `runtime_platform`, `volume`, `network_mode`, `requires_compatibilities`, `tags` | **Terraform** | a deliberate `infra/apply.sh --apply` |
| which revision the service runs | **the deploy pipeline** | `aws ecs update-service --force-new-deployment`, in the same job |

`infra/ecs.tf` carries `lifecycle { ignore_changes = [container_definitions] }` on the task
definition, and `lifecycle { ignore_changes = [task_definition, desired_count] }` on
`aws_ecs_service.backend` — the latter predates #1799 and is deliberately kept.

## What went wrong before this (#1799)

Terraform's state held task-definition revision **213**, the last one an `apply` registered.
The pipeline had walked the live family to **233**, because `deploy.yml` clones the family's
*latest* revision on every merge, retags the three images, pins `PAPER_ADVANCE_ENABLED`, and
registers the result.

Every attribute of `aws_ecs_task_definition` is force-new, so any edit to the JSON in
`ecs.tf` — #1778's `PAPER_ADVANCE_ENABLED` flip was the one that surfaced it — made a plain
`terraform plan` say `must be replaced`. Nothing visibly breaks the minute you apply that:
the service ignores `task_definition`, so it keeps running 233. The damage lands on the
**next merge**, when the pipeline clones the family's latest — now Terraform's revision —
and twenty revisions of accumulated container state, including every commit-SHA image tag,
are rolled back into production by a deploy that looks completely ordinary.

The workaround at the time was `-target=`. That is no longer needed.

## If you want to change what runs in production

**An environment variable, a secret, an image, a healthcheck — anything inside a container.**

Editing `infra/ecs.tf` alone does **not** ship it. The values that ship are whatever the
pipeline's clone carries plus whatever `.github/scripts/ecs_rewrite_task_def.py` pins. So:

1. If the value must be *pinned on every deploy* (a kill switch, a flag whose "unset" state
   is dangerous), put it in `ecs_rewrite_task_def.py` alongside `PAPER_ADVANCE_ENABLED`, with
   a test beside the pin guards that already exist —
   `backend/tests/test_ecs_paper_advance_deploy_pin.py`,
   `test_ecs_free_generations_pin.py`, `test_ecs_backend_secrets.py`,
   `test_ecs_readiness_deploy_pin.py`. That script is the only thing that decides the value
   on every revision, and it is the route this repo has taken every time since #1799 was
   filed: `FREE_GENERATIONS_PER_ACCOUNT` (#1643 A5), the `TIINGO_API_TOKEN` secret entry
   (#1798), and the `/health/ready` container healthcheck with its `HEALTH_STALE_UNREADY_S`
   threshold (#1818 P3) are all pinned there. Every one of them writes inside
   `containerDefinitions`, which is why the `ignore_changes` list is still exactly
   `[container_definitions]`.
2. If the value is a *one-time addition* to the live definition — something you do **not**
   want re-asserted on every deploy — it must be added to the **live** definition once,
   because the clone is what propagates it forward. Prefer route 1 wherever you can: a
   one-time addition survives only until somebody registers a revision from a clone that
   predates it. Two ways:
   - Register it by hand: `aws ecs describe-task-definition --task-definition
     archimedes-backend --query taskDefinition > td.json`, edit, strip the describe-only
     fields, `aws ecs register-task-definition --cli-input-json file://td.json`, then
     `aws ecs update-service --cluster archimedes-cluster --service archimedes-backend
     --task-definition <new arn> --force-new-deployment`. The next merge clones it forward.
   - Or temporarily remove the `ignore_changes` line, apply, put it back. Heavier, and it
     rewrites the whole container block from `ecs.tf` — which is only safe if `ecs.tf` is
     genuinely current with the live definition. Check first:
     `diff <(aws ecs describe-task-definition --task-definition archimedes-backend
     --query 'taskDefinition.containerDefinitions') <(...)`.
3. **Always also update `infra/ecs.tf`.** It is no longer the thing that ships, but it is
   the declared baseline a from-scratch rebuild registers as revision 1, and it is what the
   repo-level guards assert against (`backend/tests/test_ecs_backend_secrets.py` pins the
   `secrets` membership; `test_ecs_generation_timeout.py` pins the generation timeout). A
   change that lands live and not in `ecs.tf` is a landmine for the next rebuild.

That last point is the cost of this ownership split, stated plainly: `ecs.tf` and production
can now disagree without Terraform telling you. #1798's Tiingo secret is exactly that shape —
a secret that has to land in **both** places — and it did, via route 1:
`ecs_rewrite_task_def.py` re-asserts it on every deploy and `ecs.tf` carries the declared
twin that `test_ecs_backend_secrets.py` pins.

## Running an apply

```bash
aws sts get-caller-identity              # must be account 037613907429
ls infra/terraform.tfvars                # apply.sh refuses without it
export TF_VAR_aurora_master_password="$(aws ssm get-parameter \
  --name /archimedes/prod/AURORA_MASTER_PASSWORD --with-decryption \
  --query Parameter.Value --output text)"

infra/apply.sh                           # plan — no -target needed any more
infra/apply.sh --apply                   # apply, interactive confirm
```

### What a clean plan looks like now

Untargeted, on `main`, with `infra/terraform.tfvars` carrying the real production values, the
plan should contain **no `aws_ecs_task_definition.backend` change at all**. Measured
2026-09-03 on this account, before and after #1799's change:

| | before | after |
|---|---|---|
| `aws_ecs_task_definition.backend` | `must be replaced` (state rev 213, `container_definitions` forces replacement) | *(absent)* |
| `Plan:` | 2 to add, 1 to change, 1 to destroy | 1 to add, 1 to change, 0 to destroy |

Two residuals remain, and neither is a task-definition problem:

- **`local_sensitive_file.private_key` — will be created.** `infra/main.tf` writes the deploy
  SSH key to `./archimedes-deploy-key.pem` in whatever directory Terraform ran from. The
  `local` provider drops the resource from state when that file is missing, so every checkout
  that is not the working copy which first applied plans it as a create. Nothing in AWS is
  drifting. Expect it in a fresh clone and in CI; it is the one entry on the drift gate's
  exemption list.
- **`aws_lambda_function.deploy_drift` — will be updated in place.** Only
  `source_code_hash`. Root cause, confirmed 2026-09-03 by downloading the deployed zip: it
  contains `__pycache__/index.cpython-312.pyc` next to a byte-identical `index.py`, swept in
  from the applier's working copy by `archive_file`'s `source_dir`. #1799 adds
  `excludes = ["__pycache__"]` so it cannot recur; the existing diff clears on the next
  untargeted apply.

Anything else in the plan is real drift. Read it before approving.

## The drift gate

`.github/workflows/terraform-drift.yml` runs this same plan on `infra/**` pull requests,
pushes to `main`, and every Monday, and fails when it finds changes outside that one
exemption. It is **advisory** — never a required status check (see the box in the workflow).

Two things about reading it, both worth knowing before you arm it:

- **A PR that deliberately changes `infra/**` will show planned changes, and that is
  expected.** New resources plan as `create`, and the classifier fails on every planned
  change except the `local_sensitive_file.private_key` exemption. So on the `pull_request`
  arm the classify step is `continue-on-error`: the plan still runs, the verdict is still in
  the step log, `plan.txt` is still uploaded — but an intentional infra PR is not painted
  red for doing its job. **On a PR, a green tick does not mean "no infra change here."** Read
  `plan.txt`. On pushes to `main`, the Monday schedule, and manual dispatches — where a
  planned change means production and `infra/` really have diverged — the step still fails
  the job. A *broken* plan (any exit code other than 0 or 2) fails on every arm, PRs
  included.
- **A push to `main` cannot cancel the Monday run.** The concurrency group includes
  `github.event_name`, and `cancel-in-progress` is limited to pull requests. Without both,
  a push and the schedule share a group key (`github.ref` is `refs/heads/main` for each) and
  the push would kill the one trigger that catches drift nobody committed.

It is **off until armed**. `infra/scripts/setup-github-plan-role.sh` creates the read-only
role it assumes (`archimedes-github-plan`; the existing `archimedes-github-deploy` cannot be
reused — its trust policy is `main`-only and its permissions are ECR/SSM writes, not reads),
then three repository settings switch it on:

```bash
AWS_PROFILE=ArchimedesDanAdmin bash infra/scripts/setup-github-plan-role.sh          # dry run
AWS_PROFILE=ArchimedesDanAdmin bash infra/scripts/setup-github-plan-role.sh --apply
gh variable set TF_PLAN_ROLE_ARN --body "arn:aws:iam::037613907429:role/archimedes-github-plan"
gh secret   set TF_VAR_ALARM_EMAIL --body "<address subscribed to archimedes-alerts>"
gh variable set TF_DRIFT_ENABLED --body "true"     # arm LAST
```

What that role can reach: the AWS-managed `ReadOnlyAccess` policy, plus `kms:Decrypt`
restricted to the SSM key and to calls arriving `ViaService: ssm.us-east-1.amazonaws.com`.
`ReadOnlyAccess` alone grants `ssm:Get*` on every parameter in the account, so pairing it
with that decrypt grant would be permission to read all 19 SecureStrings in cleartext —
`CIRCLE_ENTITY_SECRET`, `BETTER_AUTH_SECRET`, `DATABASE_URL` and the rest — from any in-repo
pull-request branch. The inline policy therefore carries two `NotResource` **Deny**
statements that cut it back to the one Aurora parameter and the state bucket's objects. A
`Deny` beats an `Allow` from any policy, so those override the managed attachment. The
script's header documents the whole trade, including what deliberately survives (parameter
*names* via `ssm:Describe*`, bucket *configuration* via `s3:Get*`). Neither Deny costs the
plan anything: `infra/` declares no `aws_ssm_parameter` and no `aws_s3_object`, and both
lambdas load their code from a local `filename`.

`TF_VAR_ALARM_EMAIL` is not optional: `infra/cloudwatch.tf` creates the alarm subscription
only when `var.alarm_email` is non-empty, so an unset value makes the gate report
`aws_sns_topic_subscription.alerts_email[0]` as a destroy that is not real. Verified
2026-09-03. The other four operational variables in `infra/terraform.tfvars.example` are read
only inside `container_definitions` and now produce no diff at their defaults — also
verified, by planning with all four defaulted and diffing against a plan with the real
values.

**Expect the gate's first armed run to be red**, on `aws_lambda_function.deploy_drift`. That
is the residual above, and it is the gate working. Clear it with one untargeted
`infra/apply.sh --apply`.

## Rolling back

Nothing here changes at runtime, so there is no live rollback. To restore the old behaviour,
delete the `lifecycle` block from `aws_ecs_task_definition.backend` in `infra/ecs.tf` — the
next untargeted apply will again register a revision from that file, and the next merge will
again clone it forward. That is #1799, so do it only deliberately.

## Related

- [`cloudfront-cache-behaviour-apply.md`](cloudfront-cache-behaviour-apply.md) — the other
  apply procedure; its "CI never runs plan or apply" framing is now half-true, and the half
  that changed is the drift gate described above.
- [`../../infra/runbooks/ecs-fargate-cutover.md`](../../infra/runbooks/ecs-fargate-cutover.md)
  — what the ECS service is and how it was cut over.
- [`../operations/feature-flag-fliplist.md`](../operations/feature-flag-fliplist.md) — which
  flags exist and where each one actually lives.
