# SES bounce and complaint feedback — draining the queue, reading the stamp, clearing it

> **status:** runbook
> **owner:** Dan Browne
> **updated:** 2026-09-03
> **superseded-by:** —

**Scope:** the SES **event feedback** loop added by
[#1804](https://github.com/aprin-labs/archimedes/issues/1804) — the push half. What SES
publishes, where it lands, how the fact gets onto the user row, and the two-command
procedure for letting a wrongly-blocked address back in.

**Read this first if:** a user says signup or the resend button refuses their address with
"mail to this address bounced", or you need to know whether bounces are being recorded at
all. For the account-level suppression list itself — the AWS-side list that makes
`SendEmail` succeed and then drop the message — see
[`ses-suppression.md`](ses-suppression.md). **The two are different things and clearing one
does not clear the other; § 5 is the procedure that does both.**

---

## 1. The loop, end to end

```
auth container ──SendEmail(ConfigurationSetName = archimedes-mail)──▶ SES
                                                                      │
                                                    BOUNCE / COMPLAINT / REJECT / DELIVERY
                                                                      ▼
                                                       SNS  archimedes-ses-events
                                                                      ▼
                                                       SQS  archimedes-ses-events   (14-day retention)
                                                                      │                        │
                                    python -m archimedes.scripts.ses_events drain   ◀──────────┘
                                                                      ▼
                                            auth_users.emailBouncedAt / emailBounceKind
                                                                      ▼
                                    signup + the signed-in resend refuse the address
```

Terraform: [`infra/ses_events.tf`](../../infra/ses_events.tf) (configuration set, topic,
queue, DLQ, IAM, **the scheduled drain task and its two alarms**) plus the
`SES_CONFIGURATION_SET` / `SES_EVENTS_QUEUE_URL` variables in
[`infra/ecs.tf`](../../infra/ecs.tf). Consumer:
[`backend/archimedes/scripts/ses_events.py`](../../backend/archimedes/scripts/ses_events.py).
Refusal: [`auth/auth.js`](../../auth/auth.js) (`bounceRefusal`).

**Every link fails silently.** A send that names no configuration set produces no event; a
topic with no subscriber drops notifications; a drain that never runs leaves the queue
filling. In all three cases mail still goes out and **no AWS error is raised anywhere** —
the alarms in § 3 exist because that third case is otherwise indistinguishable from a quiet
week.

## 2. Draining the queue

**A schedule already does this.** `aws_scheduler_schedule.ses_events_drain`
(`infra/ses_events.tf`) invokes the `archimedes-ses-events-drain` Fargate task every
15 minutes by default — a dedicated single-container task definition running
`python -m archimedes.scripts.ses_events drain`, the same one-off shape as the Alembic
migrate task. The interval is `var.ses_events_drain_schedule_expression`; the queue's 14-day
retention is what makes a periodic drain honest rather than lossy, so loosening it makes a
refusal **later, never wrong**.

```bash
# what the schedule is set to, and whether it is ENABLED
aws scheduler get-schedule --name archimedes-ses-events-drain

# the last runs' output (the summary below is the whole of it). The prefix is a
# STREAM prefix, not a content filter: every drain writes to its own stream
# under /archimedes/app named ses-events-drain/…
aws logs tail /archimedes/app --log-stream-name-prefix ses-events-drain --since 1h

# force one now, without waiting for the next tick
aws ecs run-task --cluster archimedes-cluster --launch-type FARGATE \
    --task-definition "$(cd infra && terraform output -raw ses_events_drain_task_definition_family)" \
    --network-configuration "$(cd infra && terraform output -raw ecs_migrate_network_configuration)"
```

The same command runs by hand when you want to watch it, from the backend task (whose role
already carries the queue grant and whose environment already has the URL) or from an
operator shell:

```bash
# inside the running backend container
python -m archimedes.scripts.ses_events drain

# from an operator shell
PYTHONPATH=backend python -m archimedes.scripts.ses_events drain \
    --queue-url "$(cd infra && terraform output -raw ses_events_queue_url)"

# look first, change nothing
PYTHONPATH=backend python -m archimedes.scripts.ses_events drain --dry-run
```

The summary is the whole output, and every line is a count of something that happened:

```
drain complete.
  received        7
  stamped         2      ← rows that gained emailBouncedAt for the first time
  already stamped 1      ← redelivery, or a second bounce for the same address
  no such user    0      ← mail to an address with no account (deleted, or never one)
  not recorded    4      (deliveries, rejects, transient bounces)
  unparseable     0      (left on the queue -> dead-letter queue)
  deleted         7
```

**`not recorded` is not a failure.** Deliveries and rejects are carried on the same stream
deliberately, and a **transient** bounce — a full mailbox, a DNS blip — is a real person, so
it is never stamped. Stamping one would lock out exactly the user this whole loop exists to
stop locking out.

**`unparseable` above zero** means AWS changed a payload shape, or something else is
publishing into the topic. Those messages are deliberately NOT deleted: after five receives
SQS moves them to `archimedes-ses-events-dlq`, where you can read one:

```bash
aws sqs receive-message --queue-url "$(cd infra && terraform output -raw ses_events_dlq_url)" \
    --max-number-of-messages 1 --visibility-timeout 0
```

## 3. Is the loop alive at all?

Silence is ambiguous — no bounces and no events look identical from the outside. Three
checks, cheapest first:

```bash
# 1. Is the configuration set still attached to a destination, and ENABLED?
aws sesv2 get-configuration-set-event-destinations \
    --configuration-set-name "$(cd infra && terraform output -raw ses_configuration_set_name)"

# 2. Is anything arriving? DELIVERY events flow on every successful send, so a
#    long-idle queue with live signups means the mailer is not naming the set.
aws sqs get-queue-attributes --queue-url "$(cd infra && terraform output -raw ses_events_queue_url)" \
    --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible

# 3. Is the auth container actually sending with it?
aws ecs describe-task-definition --task-definition archimedes-backend \
    --query "taskDefinition.containerDefinitions[?name=='auth'].environment[?name=='SES_CONFIGURATION_SET']"
```

Check 3 returning empty is the most likely cause of an otherwise healthy-looking loop that
records nothing: the mailer treats a blank `SES_CONFIGURATION_SET` as "send without a
configuration set", which is the pre-#1804 behaviour, so mail keeps flowing and no event is
ever published.

**Two alarms answer the other half of the question without you asking.** Both publish to
`cloudwatch.tf`'s existing alerts topic:

| Alarm | Fires when | What it means |
|---|---|---|
| `archimedes-ses-events-not-drained` | the queue's oldest message is over an hour old, twice running | the schedule is off, failing, or has lost its grant — bounces are safe (14-day retention) but **nothing is being stamped** |
| `archimedes-ses-events-dlq-not-empty` | one message reaches the DLQ | the consumer's parser is behind an SES payload change — read one with the `receive-message` command above |

Neither treats missing data as breaching: an **empty** queue publishes no age datapoints at
all, and empty is the healthy steady state, so the opposite setting would fire every quiet
week until somebody muted it.

## 4. Reading the stamp

```sql
SELECT id, email, "emailVerified", "emailBouncedAt", "emailBounceKind"
FROM auth_users WHERE lower(email) = lower('someone@example.com');
```

* `emailBouncedAt IS NULL` — SES has never reported anything bad about this address.
  An unverified account here is just someone who has not clicked the link.
* `emailBounceKind = 'bounce'` — a **permanent** bounce. The mailbox does not exist.
  Signup and the signed-in resend answer `EMAIL_ADDRESS_BOUNCED`.
* `emailBounceKind = 'complaint'` — a human reported our mail as spam. Signup and the
  signed-in resend answer `EMAIL_ADDRESS_COMPLAINED`.

Rows stamped before #1804 do not exist: there was no configuration set, so those bounces
were published nowhere and cannot be reconstructed. The SES suppression list
(`ses_suppression list`) is the only record of the pre-loop past.

The **account owner** sees the same fact without SQL: `GET /api/auth/verification-status`
answers `state: "bounced"` with `bounce: {at, kind}` for their own address, and the resend
control on Account Settings and the Generate page renders it and disables the button. That
state outranks `suppressed` — it is the same conclusion from a stronger source (SES pushed
it) and it still answers when the suppression lookup cannot run. See
[`../api/auth-and-accounts.md`](../api/auth-and-accounts.md).

## 5. Letting an address back in — **both halves, or neither**

A wrongly-blocked address is blocked in **two** independent places, and clearing one alone
changes nothing the user can see:

| Where | What it does | How to clear |
|---|---|---|
| AWS account suppression list | `SendEmail` succeeds, message dropped inside AWS | `ses_suppression remove <address> --apply` |
| `auth_users.emailBouncedAt` | signup + signed-in resend refuse with a typed code | `ses_events clear <address> --apply` |

Clear the suppression list only and the product still refuses the address. Clear the stamp
only and the product accepts it, sends mail, and AWS bins it — back to the eternal silence.

Both tools are a **dry run by default**. Preview, then apply:

```bash
PYTHONPATH=backend python -m archimedes.scripts.ses_events clear dan@example.com
PYTHONPATH=backend python -m archimedes.scripts.ses_suppression check dan@example.com

PYTHONPATH=backend python -m archimedes.scripts.ses_events clear dan@example.com --apply
PYTHONPATH=backend python -m archimedes.scripts.ses_suppression remove dan@example.com --apply
```

**The bar for doing this is unchanged** and is set in
[`ses-suppression.md`](ses-suppression.md) § 3: the address's owner has confirmed it is real
and reachable. A permanent bounce is AWS telling us a mailbox does not exist; re-sending to
addresses that bounce is precisely how a sender's reputation — and with it delivery for every
real user — is destroyed. There is deliberately no bulk clear in either tool.

Afterwards, watch: if the address bounces again, the next drain re-stamps it (the clear sets
the column back to NULL, so a genuinely new bounce finds a NULL and records itself).

## 6. What this does NOT do

* **It is not a continuous consumer.** The drain is a scheduled batch task (§ 2), not an
  SQS-triggered or always-on poller, so a bounce is acted on within one interval rather than
  within seconds. Fargate has no native SQS trigger, and the alternatives — a Lambda with a
  second runtime and dependency set for one file that already runs in the backend image, or
  a long-lived poller as a new always-on singleton — buy latency that nothing here needs:
  the deadline is a human eventually retrying a signup.
* **It does not write per-send delivery rows, and it does not write feedback rows into
  `auth_email_deliveries` either.** One row per SEND is
  [#1790](https://github.com/aprin-labs/archimedes/pull/1790)'s, written by `auth/mailer.js`;
  that table's `seq` is a database-assigned IDENTITY column with no SQLite equivalent, so a
  second writer in Python could not be covered by this repo's SQLite-backed tests without
  either diverging from the production schema or supplying `seq` by hand — and an explicit
  `seq` does not advance the Postgres sequence, which would collide with the sidecar's next
  insert and lose a real receipt. (On Postgres alone an insert that OMITS `seq` is fine; the
  obstacle is the test substrate, not the database.) The per-user stamp above carries the
  fact the product acts on. **The residual #1804 stays open for** is the per-event audit
  trail the issue's sketch names — `(address, message_id, type, sub-type, timestamp)`. The
  consumer parses `MessageId` and the SES sub-type (`bounceSubType` /
  `complaintFeedbackType`) and logs them, but persists neither, so once the log rotates
  nothing can answer "**which** verification email died". Closing it needs a table keyed on
  `MessageId` — a new `bounce_events`, or `auth_email_deliveries` once `seq` is reachable
  from Python.
* **It does not refuse the anonymous resend.** `/send-verification-email` is reachable with
  no session and answers identically for unknown, verified and genuine sends; that
  uniformity is what stops it being an account-existence oracle. Only a caller signed in AS
  the address is told. This is deliberate — see the comment in `auth/auth.js`.
* **It does not touch password reset.** A reset for a bounced address still answers the same
  200 it always did, for the same anti-enumeration reason.
