# `archimedes-runner` wedges — impaired instance check, dead SSM agent

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31
> **superseded-by:** —

**Scope:** the oracle+agent runner EC2 (`archimedes-runner`, `infra/runner_ec2.tf`) becoming
unreachable and unresponsive while the hypervisor reports it healthy. Issue
[#1402](https://github.com/aprin-labs/archimedes/issues/1402). This is the box that runs
`archimedes-oracle.service` and `archimedes-agent.service`, and it doubles as the SSM jump
host used to tunnel to Aurora.

**Not in scope:** ECS/Fargate outages (the web tier), Aurora, or the scheduled kb task.
Those live in [`../../infra/runbooks/ecs-fargate-cutover.md`](../../infra/runbooks/ecs-fargate-cutover.md)
and [`../../infra/runbooks/disaster-recovery.md`](../../infra/runbooks/disaster-recovery.md).

**Read this first if:** you got an `archimedes-alerts` page naming any alarm starting
`archimedes-runner-`, or an SSM Session Manager / `aws ssm send-command` call against the
runner hangs or returns nothing.

---

## 1. Symptoms — how to recognise this and not something else

The signature is specific, and it is the same in all four recorded incidents:

| Signal | Wedge | Ordinary outage |
|---|---|---|
| `InstanceStatus` (OS-level check) | `impaired` | `ok` |
| `SystemStatus` (hardware/hypervisor check) | **`ok`** | `impaired` if hardware |
| SSM agent | `ConnectionLost`, `LastPingDateTime` frozen | `Online` |
| `/archimedes/runners` log streams | stop mid-cadence, no error, no exit line | error or stack trace before silence |
| `CPUUtilization` | climbs to ~100% and stays there | normal or zero |
| `CPUCreditBalance` | **healthy** (~490 during the 2026-08-20 incident) | exhausted, if this were credit starvation |

`SystemStatus: ok` with `InstanceStatus: impaired` is the whole diagnosis: the hardware is
fine and the operating system is not. Nothing needs to be recovered onto new hardware; the
box needs to be restarted.

**Recorded incidents (all pre-mitigation):**

| When | Duration | Notes |
|---|---|---|
| discovered 2026-08-19 | ~13 days of SSM `ConnectionLost` | found by accident, not by an alarm |
| 2026-08-20, ended 02:38 CDT | short | reconstructed from alarm-transition history |
| 2026-08-20, 06:12→10:23 CDT | **4h 11m** | oracle log stream silent for the whole window |
| 2026-08-20, from ~13:30 CDT | until manual reboot | `InstanceStatus: impaired`, `SystemStatus: ok` |

**Cause, as far as it is actually known:** host-level resource starvation. Two uncapped
containers on a 2 GB `t3.small` with no swap. That much is evidenced. The *trigger* — what
starts the CPU spiral in the agent process, which went silent at `10:56Z` and never wrote
again across two boots — has **not** been root-caused. Do not write "fixed" on this issue
because a reboot worked.

---

## 2. What is already automated — read before touching anything

Two things changed after the incidents above. Knowing which one you are watching decides
whether you act at all.

**Mitigation ([#1413](https://github.com/aprin-labs/archimedes/pull/1413), applied to the live box):**
a 1 GiB swapfile, and per-container caps — oracle `--memory=512m --cpus=0.50`, agent
`--memory=900m --cpus=1.00`. A leaking container now gets OOM-killed and restarted by
systemd instead of taking the host down with it. Applied idempotently by
`.github/workflows/deploy-runners.yml`'s host-prep step and mirrored into
`infra/runner-user-data.sh` for future instances.

**Automatic recovery (`infra/cloudwatch.tf`, this runbook's reason for existing):**

| Alarm | Condition | What it does |
|---|---|---|
| `archimedes-runner-instance-reboot` | `StatusCheckFailed_Instance` > 0 for **3 × 1 min** | **Reboots the instance** (`ec2:reboot`) and pages |
| `archimedes-runner-system-recover` | `StatusCheckFailed_System` > 0 for **2 × 1 min** | **Migrates it to new hardware** (`ec2:recover`) and pages |
| `archimedes-runner-log-silence` | `IncomingLogEvents` on `/archimedes/runners` < 1 for **3 × 5 min** | Pages only |
| `archimedes-runner-instance-impaired` | `StatusCheckFailed_Instance` ≥ 1 for **2 × 5 min** | Pages only |
| `archimedes-runner-ec2-status-check-failed` | combined `StatusCheckFailed` > 0 for **3 × 1 min** | Pages only |

So the expected shape of a wedge is now: page at ~3 minutes, automatic reboot at ~3 minutes,
recovery page ~2 minutes later. **If you are reading a page and the box is already back, the
automation worked — go to § 5 (after a self-heal) rather than § 4.**

Two things it deliberately does **not** do. Nothing stops, terminates, or replaces the
instance: this is a funds-adjacent exactly-once singleton, and reboot/recover both preserve
the instance id, the root volume, and the host-prep state above. And the log-silence alarm
has no automatic action, because a stopped container looks identical to a dead agent — see
§ 3 step 1 for how to tell them apart.

---

## 3. Diagnosis

Run these in order. Every command is read-only. Region is `us-east-1` throughout.

**Step 0 — get the instance id.** Do not paste an id from an old ticket; the box has been
replaced before.

```bash
INSTANCE_ID=$(aws ec2 describe-instances --region us-east-1 \
  --filters "Name=tag:Name,Values=archimedes-runner" "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" --output text)
echo "$INSTANCE_ID"
```

An empty result or `None` means no *running* instance tagged `archimedes-runner` — that is a
different (worse) problem than a wedge; check whether it is `stopped` by dropping the
state filter.

**Step 1 — is the SSM agent alive?** This is the direct check. There is no CloudWatch metric
for it; `LastPingDateTime` is an API field, which is why the log-silence alarm exists as a
proxy instead.

```bash
aws ssm describe-instance-information --region us-east-1 \
  --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
  --query "InstanceInformationList[0].[PingStatus,LastPingDateTime,AgentVersion]" --output text
```

- `Online` plus a `LastPingDateTime` within the last few minutes → the agent is fine. The
  page was probably log silence from a stopped container; go to step 3, skip the reboot.
- `ConnectionLost`, or an empty list, or a ping timestamp minutes-to-days old → **this is the
  wedge.** Note the exact timestamp; it is the incident's start marker and it lines up with
  the log-stream gap to the minute.

**Step 2 — which status check is failing?**

```bash
aws ec2 describe-instance-status --region us-east-1 --instance-ids "$INSTANCE_ID" \
  --query "InstanceStatuses[0].[InstanceState.Name,InstanceStatus.Status,SystemStatus.Status]" \
  --output text
```

`running impaired ok` is the #1402 signature. `running ok impaired` is a hardware fault
instead — `archimedes-runner-system-recover` handles that one on its own; give it five
minutes before intervening.

**Step 3 — when did the runners stop talking?**

```bash
aws logs describe-log-streams --region us-east-1 --log-group-name /archimedes/runners \
  --query "logStreams[].[logStreamName,lastEventTimestamp]" --output table
```

Convert `lastEventTimestamp` (epoch ms) with `date -r $((TS/1000))` on macOS. The oracle
stream writes on a verified 60-second cadence, so a gap over ~2 minutes there is real. In the
2026-08-20 incident the **agent** stream died first (`10:56Z`) and the oracle kept its exact
60s cadence until the host itself choked — so compare the two streams rather than reading
either alone.

**Step 4 — confirm the resource story (and check whether #1413 held).**

```bash
aws cloudwatch get-metric-statistics --region us-east-1 --namespace AWS/EC2 \
  --metric-name CPUUtilization --dimensions Name=InstanceId,Value="$INSTANCE_ID" \
  --start-time "$(date -u -v-3H +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --period 300 --statistics Average --output table
```

A steady climb to ~100% is the known spiral. Also pull `CPUCreditBalance` the same way — if
it is healthy (it was ~490 during the incident), this is a genuine spin and **not** burst
exhaustion, and right-sizing the instance will not fix it.

**Step 5 — only if SSM still answers, look inside.** This is the window that matters for
root-causing the still-unexplained trigger, and it is the window #1413's CPU caps were added
to keep open. Take it before rebooting.

```bash
aws ssm send-command --region us-east-1 --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["free -h","swapon --show","docker stats --no-stream","top -b -n1 -H -o %CPU | head -25","journalctl -k --since \"-2h\" | grep -i -E \"oom|killed process\" | tail -40","tail -50 /var/log/messages 2>/dev/null || journalctl -n 50 --no-pager"]' \
  --query "Command.CommandId" --output text
```

Then read it back with `aws ssm get-command-invocation --region us-east-1 --command-id <id>
--instance-id "$INSTANCE_ID" --query StandardOutputContent --output text`. Save the output
into the issue **before** § 4 — a reboot destroys it, and this is the evidence #1402's
acceptance criteria still want ("root cause identified from logs, not guessed").

---

## 4. Recovery

Try these in order. Stop at the first one that works.

**4a — wait ~3 minutes.** If `StatusCheckFailed_Instance` is breaching, the reboot alarm is
already counting. Intervening in the first three minutes just races it.

**4b — restart the units, if SSM answers.** Cheapest recovery; leaves the box up and keeps
the diagnostic state.

```bash
aws ssm send-command --region us-east-1 --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl restart archimedes-oracle.service archimedes-agent.service","sleep 5","systemctl is-active archimedes-oracle.service archimedes-agent.service"]' \
  --query "Command.CommandId" --output text
```

**4c — reboot.** This is what recovered the box in every recorded incident, and it is what
the alarm now does for you. Use it when SSM is dead or 4b did not restore log flow.

```bash
aws ec2 reboot-instances --region us-east-1 --instance-ids "$INSTANCE_ID"
```

**4d — stop/start, only if the reboot does not take.** A stop/start moves the instance to a
different host and *changes its public-facing placement*; the instance id, private IP and
root volume survive.

```bash
aws ec2 stop-instances  --region us-east-1 --instance-ids "$INSTANCE_ID"
aws ec2 wait instance-stopped --region us-east-1 --instance-ids "$INSTANCE_ID"
aws ec2 start-instances --region us-east-1 --instance-ids "$INSTANCE_ID"
```

**Do not terminate the instance.** Recreating it from Terraform is a last resort with its own
consequences (fresh host-prep, re-seeded runner env) and is not a routine recovery step.

**Verify recovery — all four, not just the first:**

```bash
aws ec2 describe-instance-status --region us-east-1 --instance-ids "$INSTANCE_ID" \
  --query "InstanceStatuses[0].[InstanceStatus.Status,SystemStatus.Status]" --output text   # ok ok
aws ssm describe-instance-information --region us-east-1 \
  --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
  --query "InstanceInformationList[0].PingStatus" --output text                             # Online
aws logs describe-log-streams --region us-east-1 --log-group-name /archimedes/runners \
  --query "logStreams[].[logStreamName,lastEventTimestamp]" --output table                  # both moving
aws cloudwatch describe-alarms --region us-east-1 --alarm-name-prefix archimedes-runner- \
  --query "MetricAlarms[].[AlarmName,StateValue]" --output table                            # all OK
```

Status checks take ~2 minutes to re-report after a boot; log streams resume within ~1 minute
of the containers starting. If the oracle stream stays silent while the agent recovers, the
runners came up but a unit did not — re-run 4b.

---

## 5. After it recovers

1. **Record it in [#1402](https://github.com/aprin-labs/archimedes/issues/1402)**: the start
   timestamp from step 1, the log gap from step 3, whether the reboot was automatic or
   manual, and any step-5 output. The acceptance criterion is *7 days without a manual
   reboot*, which is unmeasurable if incidents go unlogged.
2. **A wedge that happens after #1413's host-prep landed falsifies the memory-exhaustion
   theory.** That is the issue's own stated test. Say so in the comment; do not quietly
   re-mitigate.
3. **Two automatic reboots in a day is not a working system, it is a hidden outage.** The
   automation exists to shorten incidents, not to hide a recurring one. At that point escalate
   to the still-open options in #1402: right-size the instance, or move the runners off the
   pet box entirely ([#1044](https://github.com/aprin-labs/archimedes/issues/1044)).
4. If the runners came back but no trades or oracle pushes followed, that is a separate
   failure — the box being up is not the same as the loops working.

---

## 6. Operator steps — not automated, not done by any PR

**These are Dan's to run.** The Terraform in `infra/cloudwatch.tf` is code in the repo until
somebody applies it; until then every alarm in the § 2 table is inert.

1. **Apply the Terraform.**
   ```bash
   cd infra && terraform plan   # expect: 3 alarms to add, 1 dashboard to update, 0 to destroy
   terraform apply
   ```
   Read the plan. Anything proposing to *replace* `aws_instance.runner` is wrong — stop and
   investigate rather than accepting it.

2. **Confirm AWS accepted the automatic actions.** CloudWatch validates the action against
   the metric at `PutMetricAlarm` time, so a rejected pairing shows up here and nowhere else:
   ```bash
   aws cloudwatch describe-alarms --region us-east-1 \
     --alarm-names archimedes-runner-instance-reboot archimedes-runner-system-recover \
     --query "MetricAlarms[].[AlarmName,MetricName,AlarmActions]" --output json
   ```
   Expect `ec2:reboot` on the `StatusCheckFailed_Instance` alarm and `ec2:recover` on the
   `StatusCheckFailed_System` one, each alongside the `archimedes-alerts` topic ARN.

3. **Confirm someone is actually subscribed to the pages.** An alarm firing into an
   unsubscribed topic is the failure mode this whole issue is about, repeated:
   ```bash
   aws sns list-subscriptions-by-topic --region us-east-1 \
     --topic-arn "$(aws sns list-topics --region us-east-1 \
       --query "Topics[?contains(TopicArn, 'archimedes-alerts')].TopicArn | [0]" --output text)" \
     --query "Subscriptions[].[Protocol,Endpoint,SubscriptionArn]" --output table
   ```
   A `SubscriptionArn` of `PendingConfirmation` means the confirmation email was never
   clicked and nothing will be delivered.

4. **Live-test the reboot path, once, deliberately.** Nothing in CI can prove an EC2
   automatic action fires — only AWS can. Set the alarm to `ALARM` by hand and watch:
   ```bash
   aws cloudwatch set-alarm-state --region us-east-1 \
     --alarm-name archimedes-runner-instance-reboot --state-value ALARM \
     --state-reason "manual verification of ec2:reboot action, issue #1402"
   ```
   Then confirm a reboot actually happened, rather than trusting the alarm's state:
   ```bash
   aws ec2 describe-instances --region us-east-1 --instance-ids "$INSTANCE_ID" \
     --query "Reservations[0].Instances[0].LaunchTime" --output text   # unchanged: reboot ≠ relaunch
   aws ssm send-command --region us-east-1 --instance-ids "$INSTANCE_ID" \
     --document-name AWS-RunShellScript --parameters 'commands=["uptime -s"]' \
     --query "Command.CommandId" --output text                          # boot time should be just now
   ```
   This **will** interrupt the oracle and agent loops for ~2 minutes. Run it in a quiet
   window. The alarm returns to `OK` on its own at the next healthy datapoint — no reset
   needed.

5. **Do not live-test `ec2:recover`.** Forcing that alarm stops and starts the instance on
   new hardware, which is a real outage of several minutes with no benefit; the NAT pair
   already exercises the same action pattern in `nat_status_check_failed`.

---

## Related

- [#1402](https://github.com/aprin-labs/archimedes/issues/1402) — the open issue. Still open:
  root cause, and the 7-day quiet window.
- [`README.md`](README.md) — runbook index.
- [`../../infra/runbooks/disaster-recovery.md`](../../infra/runbooks/disaster-recovery.md) —
  when the problem is bigger than one box.
- `infra/cloudwatch.tf` § "Runner EC2 automatic recovery" — why `ec2:reboot` and not
  `ec2:recover`, and why the automation alarms treat missing data as `missing`.
- `backend/tests/test_runner_recovery_alarms.py` — the guard that keeps that distinction
  from being edited away.
