# Disaster Recovery Runbook — Archimedes

> **Status:** Authored 2026-06-12. **Not yet drilled.** Commands below were
> written against the Terraform in `infra/` (`aws_instance.archimedes`,
> `aws_lb.main`, `aws_rds_cluster.main`) and the AWS-access protocol in the root
> `CLAUDE.md`, but have **not** been executed end-to-end in this environment (no
> AWS credentials here). Treat every command as *review-then-run*, and schedule a
> real game-day drill (see § Drills) before relying on this.

Region: `us-east-1`. Account / profile: `ArchimedesDanAdmin` (see `CLAUDE.md` § AWS
account access). All admin access is via **SSM Session Manager**, not SSH.

---

## Objectives (proposed — confirm with stakeholders)

| Metric | Target | Rationale |
|---|---|---|
| **RTO** (time to restore service) | ≤ 1 hour | Single-region, single-EC2 app host; Aurora restore dominates. |
| **RPO** (max data loss) | ≤ 5 minutes | Aurora continuous backup → PITR to ~any second within the retention window. |

Aurora `backup_retention_period = 7` (days) is set in `infra/aurora.tf`, so
point-in-time recovery is available across a rolling 7-day window with no extra
configuration.

---

## Paging: what actually happened on 2026-09-03

Every "**Detect:** `archimedes-…` alarm" line below assumes a fired alarm
reaches a human. On 2026-09-03 that assumption failed, and it failed in a place
nobody was looking.

**Issue #1818 P5 says "there was no alarm, the owner found out by using the
site". The first half is wrong.** Read from the account (CloudWatch alarm
history + AWS/SNS metrics, all times UTC):

| time | event |
|---|---|
| 13:29 | outage begins — both targets unhealthy |
| 13:38:46 | `archimedes-alb-unhealthy-hosts` **OK → ALARM** |
| 13:39:16 | `archimedes-alb-5xx-rate-high` **OK → ALARM** (flaps 4× to 13:45) |
| 13:35–13:50 | SNS `NumberOfNotificationsDelivered` = **5**, `NumberOfNotificationsFailed` = **0** |
| 15:03 | outage ends (OOM kill) |
| 15:06:46 | `archimedes-alb-unhealthy-hosts` **ALARM → OK** (1 more delivered) |

Two alarms fired. Six emails were delivered, with zero failures, to a
confirmed subscriber — the first of them 9m46s into a 94-minute outage, 84
minutes before it ended. The owner still discovered it by loading the site.

Re-derive any of that yourself:

```bash
aws cloudwatch describe-alarm-history --alarm-name archimedes-alb-unhealthy-hosts \
  --history-item-type StateUpdate --start-date 2026-09-03T00:00:00Z \
  --end-date 2026-09-04T00:00:00Z --query 'AlarmHistoryItems[].[Timestamp,HistorySummary]' --output text

aws cloudwatch get-metric-statistics --namespace AWS/SNS \
  --metric-name NumberOfNotificationsDelivered \
  --dimensions Name=TopicName,Value=archimedes-alerts \
  --start-time 2026-09-03T13:00:00Z --end-time 2026-09-03T16:00:00Z \
  --period 300 --statistics Sum --output text
```

### So the gap is three things, and only two are Terraform's

1. **Detection shape.** Nothing watched the signals that were already abnormal
   ten hours earlier — Aurora connections flat at 33 from 03:33, ECS memory
   ramping. Fixed below: the new connections alarm fires at ~03:48.
2. **Detection latency.** 9m46s is the 5-of-5-minute window plus evaluation
   lag. `-alb-unhealthy-hosts` is retuned to 2-of-2, which drops three of the
   five one-minute datapoints and so fires about **three** minutes earlier.
   (Measured: `UnHealthyHostCount` read 0 through 13:30Z, first read 2 at
   13:31Z, and the 5-of-5 alarm transitioned at 13:38:46Z — a 2m46s evaluation
   lag on top of the window, which the retune does not remove.)
3. **The page did not reach the owner.** Six delivered emails and it still had
   to be found by hand. **No Terraform change fixes this.** What the code can
   do is refuse to let the question go unanswered: `var.owner_alert_email` has
   no default, so a plan fails until a destination is named, and a
   `precondition` fails the plan if that destination is the same mailbox that
   already received six ignored emails. Choosing the channel — a watched
   mailbox, SMS, PagerDuty/Opsgenie intake — is the owner's call and is the
   open half of P5.

**Do not "fix" this by pointing `owner_alert_email` at `alarm_email`'s
address.** SNS keys a subscription by (topic, protocol, endpoint), so both
resources would hold one `SubscriptionArn`, and any later apply that dropped
either would `Unsubscribe` the one the other still claims — zero subscribers,
with state saying there is one. The precondition blocks it.

> **Before your next incident, populate `infra/terraform.tfvars`.**
> `owner_alert_email` is deliberately defaultless, so **every** Terraform
> invocation in `infra/` — including the emergency `-target`ed recreates in
> §1 (host loss) and §4 (NAT down) below — now fails with
> `Error: No value for required variable` until it has a value. `-target` does
> **not** exempt root-module variables. The error text says nothing about why
> an unrelated recreate is blocked, so set this now rather than discovering it
> at 03:00 with the site down.

**Check the subscription is confirmed** after any change (an unconfirmed one
looks identical to a working one from Terraform's side):

```bash
aws sns list-subscriptions-by-topic \
  --topic-arn "$(aws sns list-topics --query "Topics[?contains(TopicArn,'archimedes-alerts')].TopicArn" --output text)" \
  --region us-east-1 --query 'Subscriptions[].{Endpoint:Endpoint,Arn:SubscriptionArn}' --output table
```

A `SubscriptionArn` of `PendingConfirmation` means no page will be delivered.

### 2026-09-03 alarm set (issue #1818 P5)

| Alarm | Fires when | Why this number |
|---|---|---|
| `archimedes-alb-unhealthy-hosts` | `UnHealthyHostCount >= 1` for **2 min** | Retuned from 5 min. Measured: the ALB first saw an unhealthy target at 13:31Z and this alarm fired at 13:38:46Z. Dropping three of five one-minute datapoints buys back ~3 min (not the ~6 an earlier draft claimed — the 2m46s evaluation lag stays). Costs some deploy-time flapping — revert `datapoints_to_alarm` if that trade goes the other way. |
| `archimedes-alb-elb-5xx-high` | `HTTPCode_ELB_5XX_Count >= 5` in 5 min | The **balancer's own** 5xx (504 = no healthy target), which nothing watched before. **Not a 2026-09-03 detector** — the ELB-side metric published *no datapoints at all* that day. General cover for a real recurring signal: 1,254 ELB-side 5xx on UTC day 2026-08-20, 306 on 2026-09-01. |
| `archimedes-ecs-backend-memory-high` | `MemoryUtilization` **max** > 85% for 5 min | The wedged tasks ramped 39% → 100% over 90 min. `max`, not average — fresh replacement tasks sat near idle and would have averaged it away. |
| `archimedes-ecs-backend-cpu-high` | `CPUUtilization` avg > 90% for 10 min | General saturation cover: average, because the autoscaling policy tracks average against a 60% target, so it means "the autoscaler is out of room". **Would not have fired on 2026-09-03** — CPU was 3%. |
| `archimedes-aurora-connections-elevated` | `DatabaseConnections` > 30 for 15 min | Connections went 16 → 33 and sat **flat** at 33 for 11.5 h at 4% CPU — sessions parked on a lock, not load. 30 sits just under the observed wedge; the `>80` alarm is a pool-pressure alarm, and it never fired. |

Replayed against the timeline, three of the five would have fired and two would
not: `-aurora-connections-elevated` at **~03:48** — nearly ten hours before the
first alarm that actually fired that day, and the single reason this set is
worth applying — `-alb-unhealthy-hosts` at ~13:36, and
`-ecs-backend-memory-high` at ~14:39 (70 minutes into a 94-minute outage; a
real detector, not early warning). `-alb-elb-5xx-high` would **not** have
fired: the ELB-side metric had no datapoints at all on 2026-09-03 (the only
5xx that day were four *target*-side responses at 13:31–13:33Z, which is what
put `-alb-5xx-rate-high` into ALARM at 13:39:16Z). `-ecs-backend-cpu-high`
never left single digits. Do not read "five alarms" as five nets under this
failure.

#### Applying them

```bash
cd infra && terraform plan \
  -target=aws_sns_topic_subscription.owner_alerts_email \
  -target=aws_cloudwatch_metric_alarm.alb_unhealthy_hosts \
  -target=aws_cloudwatch_metric_alarm.alb_elb_5xx_count \
  -target=aws_cloudwatch_metric_alarm.ecs_service_memory_high \
  -target=aws_cloudwatch_metric_alarm.ecs_service_cpu_high \
  -target=aws_cloudwatch_metric_alarm.aurora_connections_wedge
# expected: Plan: 5 to add, 1 to change, 0 to destroy.
```

**If that plan says anything about `aws_ecs_task_definition.backend`, stop.**
The two ECS alarms reach the cluster and service through `data` sources
(`cloudwatch.tf`) for exactly this reason: a direct reference to
`aws_ecs_service.backend` makes the alarm depend on the task definition, and a
targeted plan taken on 2026-09-03 with that spelling proposed replacing it
with `PAPER_ADVANCE_ENABLED "false" -> "true"` and `PLATFORM_ADMIN_WALLETS
"0x2a29…5105" -> ""`. Applying an *observability* change would have re-armed
the paper-advance loop that caused this outage (prod is pinned `false` — the
#1778 / #1818 pull-back, and `ecs.tf` still says `true`) and blanked the live
admin-wallet list. `backend/tests/test_outage_alarms_1818.py` fails if the
alarms drift back onto that dependency chain, but the plan is the last check.

---

## Failure scenarios & responses

### 1. Application host (EC2) down / unhealthy
**Detect:** `archimedes-ec2-status-check-failed` or `archimedes-alb-unhealthy-hosts`
alarm (see `infra/cloudwatch.tf`), or `https://archimedes-arc.com/` 502/503.

**Respond:**
1. Confirm the target health:
   ```bash
   aws elbv2 describe-target-health \
     --target-group-arn "$(aws elbv2 describe-target-groups \
        --names archimedes-backend-tg --query 'TargetGroups[0].TargetGroupArn' --output text)" \
     --region us-east-1
   ```
2. Try an in-place recovery first (fastest). Open a session and restart the stack:
   ```bash
   aws ssm start-session --target <instance-id> --region us-east-1
   # on host:
   cd /opt/archimedes && docker compose ps && docker compose up -d
   ```
3. If the host itself is gone, recreate it from Terraform (the app is
   stateless — all state is in Aurora/ElastiCache):
   ```bash
   cd infra && terraform plan -target=aws_instance.archimedes
   terraform apply -target=aws_instance.archimedes
   ```
   > **Two caveats, both verified 2026-09-03.** (a) `aws_instance.archimedes`
   > **no longer exists in the configuration** — commit `652225b8` removed it
   > with the EC2 decommission, and the web tier is Fargate now, so this recipe
   > and the `archimedes-ec2-status-check-failed` alarm named above are both
   > stale. Rewriting §1 for the ECS topology is out of scope here (#1818 P5 is
   > observability); it is flagged so nobody follows it mid-incident.
   > (b) Whatever replaces it will need `owner_alert_email` set in
   > `terraform.tfvars` — `-target` does **not** exempt root-module variables.
   > See § Paging.

   `user-data.sh` re-bootstraps Docker + pulls the stack on first boot. The ALB
   target group re-attaches via `aws_lb_target_group_attachment.backend`.

### 2. Database (Aurora) corruption or bad migration
**Detect:** app 5xx spike, `archimedes-aurora-*` alarms, or a known-bad deploy.
**Respond:** point-in-time restore — see
[`aurora-backup-restore.md`](aurora-backup-restore.md). RPO ≤ 5 min, RTO bounded
by clone+failover (typically 10–30 min for a small cluster).

### 3. Accidental WAF/SG lockout
The WAF (`infra/waf.tf`) and security groups can lock out legitimate traffic if
mis-tuned. Recover by reverting the offending Terraform change and re-applying;
if the console is reachable, temporarily set the WAF default action to `allow`
on `aws_wafv2_web_acl.main` while you diagnose. Never leave it on `allow`.

### 4. Region outage
Out of scope for the current single-region design. Documented gap: there is no
cross-region replica today. If this becomes a requirement, the cheapest first
step is an Aurora cross-region automated-backup replication
(`aws rds start-db-instance-automated-backups-replication`) into a DR region,
plus Terraform parameterized on region.

### 5. NAT instance down (issue #1039 N1)
**Detect:** `archimedes-nat-status-check-failed-<0|1>` alarm (see
`infra/cloudwatch.tf` — `StatusCheckFailed_System`, 2×1min evaluation
periods) fires to the SNS topic, or ECS/EC2 resources in that NAT's AZ
private subnet (10.0.10.0/24 for AZ 0 / NAT-0, 10.0.11.0/24 for AZ 1 /
NAT-1 — `vpc.tf`) start failing outbound calls: ECR image pulls timing out,
Bedrock/Arc RPC calls hanging (the `HEALTH_CHAIN_DISCONNECTED` log line +
`archimedes-chain-disconnected` alarm, issue #1039 N2, are one visible
symptom if the affected subnet's tasks can't reach Arc RPC), SSM Parameter
Store secret resolution failing at task launch. **A single NAT instance
outage affects only ITS OWN AZ's private subnet** — the other AZ's NAT +
subnet keep working; this is a partial-capacity degradation, not a full
outage (ECS/ALB keep routing to the healthy AZ's tasks).

**Respond:**
1. Identify which NAT is down and confirm via the alarm + target health:
   ```bash
   aws ec2 describe-instances --filters "Name=tag:Name,Values=archimedes-nat-*" \
     --query 'Reservations[*].Instances[*].{id:InstanceId,state:State.Name,az:Placement.AvailabilityZone}' \
     --output table
   aws cloudwatch describe-alarms --alarm-name-prefix archimedes-nat-status-check-failed \
     --query 'MetricAlarms[*].{name:AlarmName,state:StateValue}'
   ```
2. **A hardware-level system status check failure (what the N1 alarm
   monitors) self-heals automatically** — the alarm's own `alarm_actions`
   includes `arn:aws:automate:<region>:ec2:recover`, which AWS attempts
   without any human action (same-instance-ID, same private IP — routes
   don't need touching). Give it a few minutes and re-check `describe-alarms`
   for the state returning to `OK`.
3. **If the instance is merely `stopped`** (an operator action, or a
   recovery attempt that didn't auto-restart it) rather than genuinely
   impaired, start it directly — cheaper and faster than a Terraform apply:
   ```bash
   aws ec2 start-instances --instance-ids <nat-instance-id>
   aws ec2 wait instance-running --instance-ids <nat-instance-id>
   ```
4. **If the instance is gone / terminated / recovery genuinely failed**,
   recreate it from Terraform (targeted, so nothing else in the VPC is
   touched):
   ```bash
   cd infra && terraform plan -target='aws_instance.nat[0]'   # or [1] for the other AZ
   terraform apply -target='aws_instance.nat[0]'
   ```
   (Needs `owner_alert_email` in `terraform.tfvars` — `-target` does not
   exempt root-module variables. See § Paging.)

   `aws_route.private_nat` (`vpc.tf`) points at
   `aws_instance.nat[*].primary_network_interface_id` — a NEW instance gets a
   NEW ENI, so this targeted apply also updates the affected private route
   table's default route to the new NAT automatically; no separate route fix
   needed. Confirm affected-AZ tasks regain egress (a fresh ECR pull or a
   `/health` check with `chain_connected: true` on a task in that subnet)
   before considering this resolved.

See `infra/runbooks/ecs-fargate-cutover.md` § "NAT-kill drill" (Phase 6) for
a rehearsal of the detect step (stop-instance, watch the alarm, restart)
against a live NAT before trusting this playbook.

---

## Restore-order dependency

When rebuilding from scratch, apply in this order (the Terraform graph mostly
enforces it, but for `-target` restores follow it manually):

1. `vpc.tf` (network) → 2. `aurora.tf` + `elasticache.tf` (data) →
3. `alb.tf` + `waf.tf` (edge) → 4. `main.tf` (`aws_instance.archimedes`, app) →
5. `cloudwatch.tf` (observability).

---

## Drills (do this before trusting the runbook)

- [ ] **PITR drill:** restore Aurora to a *new* cluster at a timestamp 1 h ago,
      point a throwaway app instance at it, confirm data, then destroy. Time it
      — record the actual RTO.
- [ ] **Host-loss drill:** terminate the EC2 in a maintenance window, run the
      §1.3 Terraform recreate, confirm the ALB target goes healthy. Time it.
- [ ] **Alarm drill:** stop the backend container; confirm
      `archimedes-alb-unhealthy-hosts` fires, that SNS reports it delivered,
      **and that you personally notice it within N minutes without going
      looking.** That last clause is the whole drill. On 2026-09-03 the alarm
      fired and six emails were delivered with zero failures, and the owner
      still found the outage by loading the site (§ "Paging: what actually
      happened" above) — so "the mail was sent" is precisely the assertion
      that was already true and already useless. Check the subscription is
      `Confirmed`, not `PendingConfirmation`, first. Time it: alarm → noticed.
- [ ] **NAT-kill drill (issue #1039 N1, § "NAT instance down" above):** stop
      one NAT instance, confirm `archimedes-nat-status-check-failed-<n>` fires
      to the SNS topic within its 2-min evaluation window, confirm the OTHER
      AZ's tasks are unaffected, restart the instance, confirm the alarm
      clears. Time it. Shared drill script:
      `infra/runbooks/ecs-fargate-cutover.md` § "NAT-kill drill" (Phase 6).

Record actual measured RTO/RPO here after the first drill and revise the targets
above to reality.
