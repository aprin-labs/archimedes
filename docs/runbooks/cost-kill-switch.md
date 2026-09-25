# AWS cost kill switch — how it fires, how to recover

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31
> **superseded-by:** —

**Scope:** the automatic spend brake defined in [`../../infra/cost_kill_switch.tf`](../../infra/cost_kill_switch.tf).
It scales the ECS backend to zero and stops the runner EC2 instance when the AWS bill crosses
a threshold, with nobody watching. This page is what you read when you get the notification,
and what you read before you turn it back on.

**Why it exists.** Owner directive, 2026-08-31: *"If costs spike hard and I'm not around, we
need a mechanism to cut things off."* Everything that existed before this — the two budgets
[`../../infra/scripts/setup-budgets.sh`](../../infra/scripts/setup-budgets.sh) created, the
Bedrock-deny budget action, Cost Anomaly Detection — either sends an email or removes one
service's permissions. All of it assumes a human reads the mail. This does not.

---

## Read this first: what this is not

**This bounds damage. It does not make overspend impossible, and nothing in AWS does.**

AWS bills on a lag. Budget evaluation runs roughly every 8–12 hours. The
`AWS/Billing EstimatedCharges` metric publishes roughly every 6 hours. A resource that
starts burning money at 02:00 keeps burning until the first evaluation that can see it. On
this account's current shape the realistic worst case is a **half-day of runaway spend before
either brake can trip**, plus a few minutes for tasks to drain.

There is no AWS feature that hard-stops at a dollar figure. Budgets alert; budget actions
detach permissions; this Lambda turns compute off. Layers, not a wall.

Two other honest limits:

- **It does not zero the bill.** Aurora, ElastiCache, the ALB, NAT and WAF keep billing while
  the compute is off — about **$5 of the ~$8/day baseline** survives a fire. This removes the
  *spike*, not the floor. Killing the floor means `terraform destroy`, which is a human
  decision, not a Lambda's.
- **It only knows about total account spend.** A spike concentrated in one service still has
  to move the whole-account number far enough to cross the threshold.

---

## Current baseline spend — measured, not estimated

Read from **Cost Explorer** on 2026-08-31 via `aws ce get-cost-and-usage`.

| Period | Total | Notes |
|---|---:|---|
| July 2026 (complete, clean) | **$241.41** | No one-offs. The reference month. |
| August 2026 (through the 31st) | **$530.16** | Includes a one-time **$274.00** Amazon Registrar domain renewal on 2026-08-20. |
| August 2026, excluding that renewal | **$256.16** | |
| Recurring daily run rate | **$7.97/day** | Mean of 14 clean days, 2026-08-15 → 08-29, excluding the 20th. Range $7.06–$8.93. |

> **Baseline: ≈ $243/month recurring (≈ $8/day).** July's $241.41 is an independent
> confirmation of the same figure from a month with no one-offs at all.

Where the recurring money goes (August 2026, one-off renewal excluded):

| Service | Aug 2026 | Jul 2026 |
|---|---:|---:|
| Amazon ECS (Fargate) | $65.79 | $41.59 |
| Amazon RDS (Aurora) | $59.57 | $47.57 |
| Amazon EC2 – Compute | $38.48 | $39.14 |
| Amazon CloudWatch | $17.51 | $18.96 |
| Amazon VPC | $16.76 | $18.61 |
| Elastic Load Balancing | $16.35 | $16.76 |
| AWS WAF | $15.68 | $16.20 |
| Amazon ElastiCache | $12.34 | $12.65 |
| EC2 – Other | $11.04 | $28.61 |
| everything else | ~$2.6 | ~$1.3 |

Two things worth noticing before you tune any number below. First, **Bedrock is $0.12/month** —
the LLM spend everyone worries about is not where the money is. Second, the ECS line grew
58% month-over-month while nothing else moved much; that is the line to watch.

**Re-read the real numbers any time:**

```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-08-01,End=2026-09-01 \
  --granularity MONTHLY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE
```

---

## How it fires

```
                       spend crosses ...
                              │
      ┌───────────────────────┼────────────────────────┐
      │                       │                        │
   50% ($250)             80% ($400)             120% ($600)
      │                       │                        │
      ▼                       ▼                        ▼
  archimedes-alerts     archimedes-alerts     archimedes-alerts  (a human hears it)
   (email: FYI)          (email: look now)             +
                                                archimedes-cost-kill-switch
                                                        │
                                                        ▼
                                          Lambda archimedes-cost-kill-switch
                                                        │
                                    ┌───────────────────┼───────────────────┐
                                    ▼                   ▼                   ▼
                        autoscaling target      ECS service           runner EC2
                        min=0, max=0          desiredCount=0        StopInstances
                                    └───────────────────┼───────────────────┘
                                                        ▼
                                            loud SNS notification with the
                                            recovery command in the body

  ── the fast lane, same $600 boundary ──────────────────────────────────────
  CloudWatch alarm  archimedes-billing-estimated-charges-high
  AWS/Billing EstimatedCharges > $600, ~6h lag  ──►  both topics (same as 120%)
```

Percentages are of `var.cost_budget_monthly_limit`, default **$500/month** — roughly 2× the
measured $243 baseline. The headroom is chosen, not arbitrary: the annual **$274 Amazon
Registrar renewal** makes a renewal month cost about **$517 = 103% of budget**, which rings
the 80% bell and does **not** reach the 120% kill line. Tighten the budget below ~$450 and a
routine domain renewal starts taking production down.

The CloudWatch alarm exists purely to arrive sooner at the same boundary. It uses
`treat_missing_data = "notBreaching"` — the opposite of the runner status-check alarm's
setting, deliberately: a missing billing datapoint is routine at month rollover, and
`breaching` would shut production down every 1st of the month.

Within a month the alarm fires **once**: CloudWatch runs alarm actions on state *transition*,
and `EstimatedCharges` resets at month rollover, which is also what re-arms it.

---

## What it does, exactly

In this order — the order is load-bearing:

1. `application-autoscaling:RegisterScalableTarget` → `MinCapacity=0`, `MaxCapacity=0` on
   `service/archimedes-cluster/archimedes-backend`.
   *Floor first.* Application Auto Scaling **enforces** the floor; with `MinCapacity=1` still
   registered, step 2 is reverted within minutes and the kill switch looks like it worked
   while the bill keeps running. The ceiling goes to 0 too, or target-tracking scales back out.
2. `ecs:UpdateService` → `desiredCount=0`. Tasks drain. Task definitions, target-group
   registration and log groups are untouched.
3. `ec2:StopInstances` on the `archimedes-runner` box. **Stopped, never terminated** — the
   root EBS volume is preserved.
4. `sns:Publish` to `archimedes-alerts` with a message stating exactly what it did, what
   failed if anything did, and the recovery command.

Firing twice — both triggers, or a duplicate SNS delivery — changes nothing the second time
and says so. Steps 1 and 2 read current state first and skip the write. Step 3 re-issues
`StopInstances` and lets AWS no-op it, deliberately: `ec2:DescribeInstances` supports no
resource-level IAM scoping, so reading the instance state first would force the EC2
statement in the policy from one instance ARN to `"*"`. One redundant API call is cheaper
than that widening.

A failure in one step does not abort the others; errors are collected, published, and then
re-raised so the Lambda error metric shows it too.

### What it can never do

No Aurora. No ElastiCache. No S3. No EBS snapshots. No DynamoDB. **Not even a read.** No
`Delete*`, `Terminate*` or `Destroy*` permission of any kind.

This is not a promise about the Python — the Python can be rewritten in a one-line PR. It is
enforced by the IAM policy in `infra/cost_kill_switch.tf`, and
[`../../backend/tests/test_cost_kill_switch_guards.py`](../../backend/tests/test_cost_kill_switch_guards.py)
fails the build if that policy grows a wildcard, a data-store service prefix, or a
destructive verb.

**The trade is availability for spend, and only availability. The site goes down; no data is
at risk.** That asymmetry is the entire reason an unattended automatic trigger is acceptable.

---

## Recovery — one command

Copy-paste into AWS CloudShell or any shell with the admin profile. Order matters here for
the same reason it matters above: restoring `desiredCount` while the scaling target is still
pinned at 0 gets it scaled straight back to zero, and the recovery looks like it failed for
no reason.

```bash
aws application-autoscaling register-scalable-target --service-namespace ecs \
    --resource-id service/archimedes-cluster/archimedes-backend \
    --scalable-dimension ecs:service:DesiredCount --min-capacity 1 --max-capacity 4 \
  && aws ecs update-service --cluster archimedes-cluster \
    --service archimedes-backend --desired-count 1 \
  && aws ec2 start-instances --instance-ids "$(aws ec2 describe-instances \
       --filters Name=tag:Name,Values=archimedes-runner \
                 Name=instance-state-name,Values=stopped \
       --query 'Reservations[].Instances[].InstanceId | [0]' --output text)"
```

The same command, with a dry run and the instance lookup built in, is in the repo:

```bash
./infra/scripts/cost-kill-switch-recover.sh            # prints what it would do
./infra/scripts/cost-kill-switch-recover.sh --apply    # does it
```

`terraform apply` also restores the ECS pieces (the scalable target's min/max are Terraform-
managed), but it is the slower path and it will want to reconcile everything else in the
plan at the same time. Use the command above during an incident; reconcile later.

Watch it come back:

```bash
aws ecs wait services-stable --cluster archimedes-cluster --services archimedes-backend
```

The `desired_count`/`task_definition` drift is already in `lifecycle.ignore_changes` on the
service ([`../../infra/ecs.tf`](../../infra/ecs.tf)), so a later unrelated `terraform apply`
will not fight the recovery.

### Before you recover

**Find out what spent the money.** Recovering without fixing the cause re-arms the same
spike, and the budget threshold for the month has already been crossed — so the switch will
**not** fire again at the same boundary. You would be running unbraked for the rest of the
month.

```bash
# What changed, day by day, by service
aws ce get-cost-and-usage --time-period Start=2026-08-20,End=2026-09-01 \
  --granularity DAILY --metrics UnblendedCost --group-by Type=DIMENSION,Key=SERVICE
```

If the cause is not fixable immediately, raise `cost_budget_monthly_limit` deliberately and
apply, rather than leaving the account with a threshold already behind it.

---

## Rehearsal

Test it on purpose, before it matters. Publishing to the kill topic invokes the Lambda for
real, so do this when a few minutes of downtime is acceptable.

```bash
# 1. Fire it
aws sns publish --topic-arn "$(terraform -chdir=infra output -raw cost_kill_switch_topic_arn)" \
  --subject "REHEARSAL" --message "manual kill-switch rehearsal"

# 2. Confirm: expect desiredCount 0, min/max 0, instance stopping
aws ecs describe-services --cluster archimedes-cluster --services archimedes-backend \
  --query 'services[0].desiredCount'
aws application-autoscaling describe-scalable-targets --service-namespace ecs \
  --resource-ids service/archimedes-cluster/archimedes-backend \
  --query 'ScalableTargets[0].{min:MinCapacity,max:MaxCapacity}'

# 3. Recover
./infra/scripts/cost-kill-switch-recover.sh --apply
```

Fire it twice in a row to confirm the idempotency claim: the second SNS notification should
say "ALREADY" on all three lines.

The Lambda has a `COST_KILL_SWITCH_DRY_RUN` env var, and Terraform pins it to `"false"` with
a guard test asserting so. **Do not flip it to rehearse.** A kill switch left in rehearsal
mode is a kill switch that does not exist, and nobody finds out until the month it mattered.

---

## Tuning

| Variable | Default | Change it when |
|---|---:|---|
| `cost_budget_monthly_limit` | `500` | Baseline moves. Keep ≥ 1.8× the measured monthly recurring figure, and above `baseline + $274` so a registrar renewal cannot reach 120%. |
| `cost_kill_switch_billing_threshold_usd` | `600` | Always move it with the budget — it is meant to be 120% of it, reached sooner. |
| `cost_kill_switch_billing_alarm_arms_kill_switch` | `true` | Set `false` if you lower the billing threshold to something a normal month reaches. `EstimatedCharges` is cumulative month-to-date, so a low armed threshold takes production down on a schedule. |
| `ecs_service_min_count` / `max_count` | `1` / `4` | These are what the recovery command restores; the Lambda reads them from Terraform, so they stay in sync automatically. |

---

## Relationship to the pre-existing brakes

Both of these are live and **unmanaged by Terraform** — created out-of-band by
`infra/scripts/setup-budgets.sh`. The kill switch uses a deliberately different budget name
so `terraform apply` neither collides with nor adopts them.

| Brake | Where | What it does |
|---|---|---|
| `archimedes-monthly-200` budget | script, live | Emails at 50/80/100% + forecast. **Note: at the $243 baseline this budget is already exceeded every month**, so its 100% alert has stopped carrying information. Retire or re-base it. |
| `archimedes-tripwire-25` budget | script, live | Emails at $25. Fires in the first ~3 days of every month. Same problem. |
| `archimedes-monitor` Cost Anomaly Detection | script, live | ML anomaly detection, daily email at ≥$10 impact. Complementary — it catches *shape* changes this switch's absolute thresholds miss. |
| Bedrock-deny budget action | script, `--with-deny-action` | Detaches Bedrock invoke permissions at 90%. Given Bedrock is $0.12/month, this is now close to a no-op. |
| App-level generation caps | `infra/ecs.tf` | `GENERATION_DAILY_CAP_PER_USER=100`, `..._PER_IP=200`. The brake furthest upstream, and the only one with no lag at all. |

Cleaning up the two stale budgets is worth doing, but it is a separate change — this PR
deliberately does not touch live objects it did not create.

---

## Cost of the mechanism itself

| Item | Cost |
|---|---|
| AWS Budgets | First 2 budgets free; this is the 3rd on the account, so **~$0.02/day (~$0.60/month)** |
| CloudWatch alarm | **$0.10/month** (1 standard alarm) |
| Lambda | Effectively **$0** — a handful of invocations per year, well inside the free tier |
| SNS | Effectively **$0** at this volume |
| **Total** | **≈ $0.70/month** |

Against a $243/month baseline that is 0.3%, and it buys a bounded worst case instead of an
unbounded one.
