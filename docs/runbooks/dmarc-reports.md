# DMARC aggregate reports — reading them, and when they justify tightening the policy

> **status:** runbook
> **owner:** Dan Browne
> **updated:** 2026-09-04
> **superseded-by:** —

**Scope:** the `dmarc-reports@archimedes-arc.com` inbox — where reports land, the weekly
summary that mails the table to the owner, how to turn a pile of reports into a
per-source-IP pass/fail table by hand, how to read that table, and the specific evidence
that has to exist before `_dmarc.archimedes-arc.com` moves off `p=none`.

**Read this first if:** you are about to change `p=` in
[`infra/dns_email.tf`](../../infra/dns_email.tf), or someone has asked whether the domain is
being spoofed.

**What is live, and what is not.** The receipt rule that collects reports
([`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf),
[#1836](https://github.com/aprin-labs/archimedes/pull/1836)) **was applied on 2026-09-04 at
13:19 UTC** — `6 added, 0 changed, 0 destroyed`, with a test object landing under `reports/`
minutes later. So §§2–5 describe today: the bucket exists and is accumulating. **§7's weekly
summary is not live yet** — its terraform ships with
[#1849](https://github.com/aprin-labs/archimedes/pull/1849) and is unapplied, so nothing is
mailing you the table until the owner applies it.

---

## 1. What is wired, and what each piece does

| Piece | Where | What it does |
|---|---|---|
| `rua=mailto:dmarc-reports@archimedes-arc.com` | [`infra/dns_email.tf`](../../infra/dns_email.tf) `aws_route53_record.dmarc` | Tells every receiver where to send aggregate reports. Published since #1462. |
| `MX 10 inbound-smtp.us-east-1.amazonaws.com` | [`infra/ses_inbound.tf`](../../infra/ses_inbound.tf) `aws_route53_record.mx` | Points the domain's inbound mail at SES. Stood up for `privacy@` in #1460. |
| Receipt rule set `archimedes-inbound` | [`infra/ses_inbound.tf`](../../infra/ses_inbound.tf) | The one **active** rule set. A second rule set would not be active and would collect nothing. |
| Receipt rule `dmarc-reports` | [`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf) | Matches `dmarc-reports@archimedes-arc.com` and writes the message to S3 under `reports/`. **This is the piece that was missing.** |
| S3 bucket `archimedes-dmarc-reports-<account>` | [`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf) | Private (public-access block on all four flags), SSE-S3, 180-day expiry. |
| Parser | [`backend/archimedes/scripts/dmarc_reports.py`](../../backend/archimedes/scripts/dmarc_reports.py) | Turns the pile into one table. Lives in the backend package because `backend/Dockerfile` copies `backend/` and nothing else, so the scheduled job below can import it. |
| Operator command | [`scripts/dmarc_report_summary.py`](../../scripts/dmarc_report_summary.py) | The CLI over that parser. What §2 runs. |
| Weekly summary | [`backend/archimedes/scripts/dmarc_weekly_summary.py`](../../backend/archimedes/scripts/dmarc_weekly_summary.py) + `aws_scheduler_schedule.dmarc_weekly_summary` ([`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf)) | Mails the same table to the owner every Monday 13:00 UTC. **§7.** |

The MX and the `rua` were already live before #1504. That is exactly why nothing was being
collected and nobody noticed: the world was being *told* to send reports to an address that
had no receipt rule behind it. Whether SES refuses those messages at SMTP time or takes them
and discards them, the outcome on our side is identical and it is the problem — **nothing is
stored, and no failure surfaces in our account.** Any delivery error is handled by the
reporting receiver, on their side, where we never see it.

So the symptom of the whole thing being broken is silence, which is also the symptom of
nobody spoofing us. Everything below is designed around not confusing the two.

## 2. Get the table

Once §7's weekly summary is applied you should already have it: it mails this table every
Monday. Until then, and whenever you want it now, want a different window, or are checking
that the summary you were sent is the truth, run the command below.

```bash
# Everything collected in the last fortnight, straight from the bucket.
python scripts/dmarc_report_summary.py \
    --bucket "$(terraform -chdir=infra output -raw dmarc_reports_bucket)" \
    --since-days 14
```

Needs `s3:ListBucket` and `s3:GetObject` on that bucket — an operator profile, not the ECS
task role. Reports downloaded by hand work too:

```bash
python scripts/dmarc_report_summary.py --path ~/Downloads/dmarc/   # file or directory
python scripts/dmarc_report_summary.py --path ... --json           # machine-readable
```

The script accepts every shape a report actually arrives in: the raw MIME message SES stores
in S3 (the report is a **base64 attachment**, not the object body), a `.zip` (Google,
Microsoft), a `.gz` (Yahoo and most others), or a bare `.xml`.

Exit codes: **0** reports parsed · **2** no reports parsed · **3** failures present under
`--require-all-aligned` · **4** the S3 call itself failed. `2` and `4` are deliberately
distinct from `0` — *"could not look"* must never read as *"nothing found"*.

## 3. Read the table

```
DMARC aggregate reports: 2 parsed
Window:   2025-08-24 01:46 UTC → 2025-08-26 05:33 UTC
Domain:   archimedes-arc.com
Policy:   none (as the reporters saw it)

SOURCE IP     MSGS  PASS  FAIL  DKIM-A  SPF-A  DISPOSITION  REPORTERS
------------  ----  ----  ----  ------  -----  -----------  --------------------
192.0.2.55    8     0     8     0       0      none:8       yahoo.com
203.0.113.77  3     0     3     0       0      none:3       google.com
54.240.8.1    162   162   0     162     42     none:162     google.com,yahoo.com

VERDICT: 11 of 173 messages FAILED DMARC alignment, from 2 source(s): 192.0.2.55, 203.0.113.77.
```

One row per source IP, **failures first**. Column by column:

- **MSGS** — messages that source sent claiming `From: …@archimedes-arc.com`, in this window.
- **PASS / FAIL** — DMARC verdict. A message passes if **either** aligned DKIM **or** aligned
  SPF passed (RFC 7489 §6.6.2). `FAIL` is the only number that gates the policy move.
- **DKIM-A / SPF-A** — which aligned mechanism carried each message. Both zero on a passing
  row is impossible by construction. For our own SES egress expect `DKIM-A == MSGS` and
  `SPF-A == 0` — see the note below on why that is healthy.
- **DISPOSITION** — what the receiver actually did. At `p=none` this is `none:` for
  everything, including the failures. **That is the point of this issue:** the forgeries are
  visible and still being delivered.
- **REPORTERS** — which receivers saw this source. One reporter seeing a source that others
  do not is usually a low-volume path, not a discrepancy.

### The one column that is not in the table, and why

An aggregate report carries **two** verdicts per row and they disagree constantly:

| Element | What it says | Is it DMARC? |
|---|---|---|
| `<auth_results>` | Did SPF/DKIM pass **at all**, for whatever domain the sender used. | **No.** |
| `<policy_evaluated>` | Did SPF/DKIM pass **and align** with the `From:` domain. | **Yes.** |

A forwarder, a bulk-mail vendor, or an attacker sending from a domain they legitimately
control shows `auth_results` SPF = **pass** while `policy_evaluated` SPF = **fail**. Reading
the first and concluding "everything passes" is precisely how you arrive at `p=reject` with
a sending path that is about to start disappearing. The parser counts **only**
`policy_evaluated`, and
[`backend/tests/scripts/test_dmarc_report_summary.py`](../../backend/tests/scripts/test_dmarc_report_summary.py)
`::test_unaligned_spf_pass_counts_as_dmarc_failure` fails if that ever changes.

### Expect aligned SPF to fail on our own mail

Our transactional mail goes out through SES with an envelope sender in `amazonses.com`, so
**aligned SPF fails and aligned DKIM carries DMARC on its own**. That is fine — DMARC needs
one of the two — and it is already recorded in
[`email-verification-validation.md`](email-verification-validation.md). A row showing
`DKIM-A == MSGS` and `SPF-A == 0` for an SES egress IP is healthy, not a finding.

The consequence to keep in mind before tightening: **DKIM is our only aligned mechanism.**
Anything that breaks DKIM signing — a rotated selector, a dropped CNAME, a new sending path
that is not SES — takes the whole domain's mail with it at `p=reject`.

## 4. When to move to quarantine

`p=none` publishes a policy that asks receivers to do nothing. Only `quarantine` or `reject`
makes a forgery actually fail. Moving there is **evidence-led**, and the evidence is this
table. All four conditions, together:

1. **At least 14 days of reports**, covering a full send cycle — signup verification and
   password reset at minimum. `--since-days 14` is the window for a reason.
2. **Every source enumerated and explained.** Not "the failures are small". Each source IP in
   the table is either ours, or a forgery, and you can say which.
3. **Zero failing messages from any legitimate source.** A legitimate source that fails
   alignment is a real sending path that starts bouncing at `p=reject`. Fix it or remove it
   *before* the policy moves, never after.
4. **The gate command exits 0:**

   ```bash
   python scripts/dmarc_report_summary.py \
       --bucket "$(terraform -chdir=infra output -raw dmarc_reports_bucket)" \
       --since-days 14 --require-all-aligned
   ```

   Exit 3 means failures are present and it is not clear to tighten. Exit 2 means no reports
   were parsed — **not** a pass.

Condition 3 has one honest exception: a failing source you have positively identified as **a
forgery** is the *reason* to tighten, not a blocker. The distinction is whether you can name
it, and that judgement is a person's, which is why `--require-all-aligned` is a gate and not
an automation.

### The ramp

Each step is a one-line change to `aws_route53_record.dmarc` in
[`infra/dns_email.tf`](../../infra/dns_email.tf), applied by the owner, then **watched**.

| Step | Record | Hold for | Watch for |
|---|---|---|---|
| 1 | `p=none` (today) | ≥ 14 days | Reports arriving at all; sources enumerated. |
| 2 | `p=quarantine; pct=25` | ~1 week | A drop in delivered auth mail. Send yourself a verification and a reset and confirm both arrive **in the inbox**, not spam. |
| 3 | `p=quarantine; pct=100` | ~1 week | Same, plus `DISPOSITION` in the table shifting to `quarantine:` for the failing sources only. |
| 4 | `p=reject` | — | `dig +short TXT _dmarc.archimedes-arc.com @8.8.8.8` contains `p=reject`; a test send still arrives at Gmail with `dmarc=pass` in the headers. |

**Do not skip to step 4.** The failure mode is our own sign-in and password-reset mail
disappearing, and it disappears *at the receiver* — nothing in our logs, nothing in
CloudWatch, and a `SendEmail` that returned a MessageId. That is the same silent-success
shape as the suppression list ([`ses-suppression.md`](ses-suppression.md)), and it is why
this ramp exists.

### Rolling back

Set `p=` back to the previous step and apply. DNS TTL is 300s, so recovery is minutes, not
hours — but only for mail sent *after* the change. Anything a receiver already quarantined or
rejected stays that way; there is no replay. That asymmetry is the reason for `pct=25` at
step 2.

## 5. No reports are arriving

The parser printing `NO REPORTS PARSED` and exiting 2 is the symptom. Work down this list —
it is ordered by how likely each cause is, and every step is read-only.

1. **Has [`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf) been applied?** Before the
   apply this is the expected state, not an incident.

   ```bash
   aws ses describe-active-receipt-rule-set --region us-east-1 \
     --query 'Rules[].{name:Name,recipients:Recipients,enabled:Enabled}'
   ```

   You want a `dmarc-reports` rule, `Enabled: true`, recipients containing
   `dmarc-reports@archimedes-arc.com`.

2. **Is the rule set the active one?** SES has exactly one active rule set. A rule in an
   inactive set is invisible and silent. The command above reads the *active* set
   specifically, which is why it is written that way.

3. **Is the MX still pointing at SES?**

   ```bash
   dig +short MX archimedes-arc.com @8.8.8.8   # expect 10 inbound-smtp.us-east-1.amazonaws.com
   ```

4. **Is the `rua` still published?**

   ```bash
   dig +short TXT _dmarc.archimedes-arc.com @8.8.8.8
   ```

5. **Is SES failing to write to the bucket?** A rule that matches but whose action fails
   still leaves you with an empty bucket, and SES reports that failure to itself, not to us.
   The `Allow` in [`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf) is the policy AWS
   documents for this action verbatim, so it is unlikely to be the cause. The `DenyNonTLS`
   statement alongside it mirrors [`infra/alb.tf`](../../infra/alb.tf), where the same shape
   is live and does not block AWS's own log delivery — but that is a *different* service's
   write path, and this one **has not been exercised against live SES**. If steps 1–4 are
   clean and reports still never land, drop that statement, re-apply, and re-test before
   looking further; it is the one condition in this stack not proven on the live write path.

   Note that a broken write shows up at **apply** time, not silently: SES validates the
   `PutObject` when the receipt rule is created, and a rule that cannot write fails the
   create with `Could not write to bucket`. So a clean apply is itself evidence this step is
   not your problem.

6. **Are you looking at the right prefix?** The receipt rule writes under `reports/`, which is
   the parser's default `--prefix`. `aws s3 ls s3://<bucket>/reports/` settles it.

Reports are generated **daily** by most receivers and only for domains they actually received
mail claiming to be from. If the product has sent no mail in the window, no reports is
correct.

## 6. Reports arrive but cannot be parsed

**This is not §5, and the ladder above is not where to look.** Objects *are* landing in the
bucket — so the MX, the receipt rule, the active rule set and the prefix are all fine — and
the parser is refusing their contents. An unreadable object is counted **nowhere**: not in
the table, not in the failure count, not in the denominator. That is why it is named out
loud rather than folded into a quiet week.

You are here because you saw one of these:

- the weekly summary's subject line says **`NO READABLE REPORTS (n unreadable)`**, or its body
  carries an `UNREADABLE (n) — these were NOT counted above` block under an otherwise normal
  table;
- `python scripts/dmarc_report_summary.py …` printed indented lines under `NO REPORTS PARSED`.

Each line names the object and why it was refused. The three you will actually see:

| Message | What it means | What to do |
|---|---|---|
| `no DMARC aggregate report found inside` | The object is a message with no report attachment — a bounce, a probe, an autoresponder, or somebody mailing the published address by hand. | Nothing, if it is one-off. `dmarc-reports@` is in public DNS; junk arriving there is expected. |
| `zip holds N members (limit 64); refusing` | An archive with far more members than the one XML a real report carries. | Fetch the object and look. A real receiver does not send these. |
| `expands past N bytes; refusing to decompress` | A compression bomb, or a genuinely enormous report. | Same — fetch and look. The bound is `MAX_UNCOMPRESSED_BYTES` in [`backend/archimedes/scripts/dmarc_reports.py`](../../backend/archimedes/scripts/dmarc_reports.py). |

The refusals are deliberate and they are **refusals, not truncations**: reading the first 64
members of an over-full archive would drop reports out of the denominator without saying so,
which is the same quiet undercount §5 exists to prevent, one level down. Every bound is a
module constant with the reasoning next to it.

To look at the object itself, read-only:

```bash
aws s3 ls s3://$(cd infra && terraform output -raw dmarc_reports_bucket)/reports/
aws s3 cp s3://<bucket>/reports/<key> /tmp/obj.eml
python scripts/dmarc_report_summary.py --path /tmp/obj.eml
```

**When it is every object in the window,** treat it as an incident rather than noise: some
sender has found the address, or a receiver has changed the shape it sends. The summary says
so in its subject line precisely so that it cannot be mistaken for a quiet week.

## 7. The weekly summary

> **Not live yet.** Everything in this section describes the state after
> [#1849](https://github.com/aprin-labs/archimedes/pull/1849)'s terraform is applied. The
> collection in §§2–5 is live; nothing is mailing you a summary until that apply happens.
>
> **Apply it only after the branch is merged and CI has pushed the backend image.** The task
> definition runs `archimedes-backend:latest`, and `archimedes.scripts.dmarc_weekly_summary`
> ships with that PR — apply the terraform against an older `:latest` and the first Monday
> task dies with `ModuleNotFoundError`, mails nothing, and (by the no-alarm design below)
> says so nowhere but a task exit code. So: merge → let `deploy.yml` push `:latest` → apply →
> then prove it with the `--dry-run` or `aws ecs run-task` invocation under
> *Run one by hand*, rather than waiting for a Monday to find out.

`p=none` is a policy nobody looks at unless something makes them. The owner's call
([#1504](https://github.com/aprin-labs/archimedes/issues/1504), 2026-09-03) was that the
parser runs on a schedule and posts a per-source table; the destination was settled on
2026-09-04 as **email to the alert address**. So every **Monday at 13:00 UTC** a Fargate task
reads the last seven days out of the bucket and mails you the table in §3.

**A quiet week still sends.** If no reports landed, the message is a one-line "NO REPORTS
RECEIVED" with the §5 ladder attached. That is not a formality — it is the whole design:

- an empty bucket and an un-forged domain look identical, and only one is good news;
- a job that sends nothing on a quiet week is indistinguishable from a job that stopped
  running, a task role that lost its S3 grant, or a schedule somebody disabled.

**And a window of unreadable objects is a third thing.** If objects landed but none of them
parsed, the subject says `NO READABLE REPORTS (n unreadable)` and the body lists every
failure — never `NO REPORTS RECEIVED`, which would point you at §5's arrival ladder when
arrival is demonstrably working. §6 is that case.

**So the arrival of the Monday mail is the heartbeat, and its absence is the alarm.** There is
deliberately no CloudWatch alarm on this task: the monitor is you noticing that the email did
not come. The residual, stated plainly — **a run that fails to send leaves nothing but a
non-zero task exit in the log group.** If a Monday goes by with no summary, go straight to
"read the last run" below.

### What is wired

| Piece | Where |
|---|---|
| Task definition `archimedes-dmarc-weekly-summary` | [`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf) — one container, the existing backend image, no new build |
| Schedule (Monday 13:00 UTC) | `aws_scheduler_schedule.dmarc_weekly_summary`, expression in `var.dmarc_summary_schedule_expression` |
| Destination | `DMARC_SUMMARY_TO` = `var.owner_alert_email` — the address [#1818](https://github.com/aprin-labs/archimedes/issues/1818) P5 established the owner actually reads |
| Sender | `no-reply@archimedes-arc.com`, the same verified domain identity the verification mail uses. No second identity, no second IAM grant |
| Read grant | `s3:ListBucket` + `s3:GetObject` on this bucket only, on the shared ECS task role. It never writes, so the 180-day lifecycle rule stays the only thing that removes a report |

The job carries **no database secrets** — it reads S3, parses in memory, and calls SES.

### What a healthy summary looks like

The subject line carries the verdict, so a clean week and a spoofed one do not read the same
in an inbox list:

```
DMARC weekly [archimedes-arc.com]: all 173 messages aligned across 3 source(s)
DMARC weekly [archimedes-arc.com]: 11 of 173 messages FAILED alignment, 2 source(s), 1 new source(s)
DMARC weekly [archimedes-arc.com]: NO REPORTS RECEIVED in 7 days
DMARC weekly [archimedes-arc.com]: NO READABLE REPORTS (7 unreadable) in 7 days
```

The last two are **not** the same line. `NO REPORTS RECEIVED` means the window was empty;
`NO READABLE REPORTS` means objects landed and the parser refused all of them, and it sends
you to §6 rather than §5.

The body is the §3 table — **byte for byte the output of the same parser** §2 prints, not a
second rendering that could disagree with it — followed by what changed:

```
CHANGES SINCE THE PREVIOUS WINDOW
  NEW sources (1): 203.0.113.77 (FAILING)
  A source sending as this domain that was not here last week is either a
  sending path someone added, or a forgery. Name it before the policy moves.
```

A new source IP is the line worth reading on a Monday. A steady list is not.

Two things that are **not** findings: `SPF-A 0` on our own SES egress (aligned SPF always
fails for us — see §3), and `nothing to compare against` in the first week or two, when the
previous window holds no reports.

### Run one by hand

```bash
# Force a summary now, on the schedule's own task definition.
aws ecs run-task --cluster archimedes-cluster --launch-type FARGATE \
    --task-definition "$(cd infra && terraform output -raw dmarc_summary_task_definition_family)" \
    --network-configuration "$(cd infra && terraform output -raw ecs_migrate_network_configuration)"
```

`ecs_migrate_network_configuration` is not a mistake: private subnets plus the
`ecs_backend` security group is the same static pair the schedule itself uses, and the same
one [`ses-bounce-signal.md`](ses-bounce-signal.md) reaches for. One output, three one-off
tasks.

This **sends real mail** to `owner_alert_email`. To see the summary without sending anything,
run the module against the live bucket from an operator shell:

```bash
PYTHONPATH=backend python -m archimedes.scripts.dmarc_weekly_summary \
    --bucket "$(terraform -chdir=infra output -raw dmarc_reports_bucket)" \
    --dry-run
```

### Read the last run

```bash
aws logs tail /archimedes/app --log-stream-name-prefix dmarc-weekly-summary --since 8d
```

A successful run prints `sent: <subject>` and the SES `MessageId`. Exit codes: **0** sent ·
**2** misconfigured (no bucket, no recipient) · **3** could not read the bucket · **4** could
not send. `3` and `4` are deliberately distinct from `0` **and from each other** — *"I could
not look"* and *"I looked and found nothing"* are different facts, and only one of them is
about DMARC. *"I looked, I found things, and I could not read any of them"* is a third, and it
is in the **subject line** rather than an exit code: the run itself succeeded, so it exits `0`
and mails you §6.

**A MessageId is SES accepting the message, not delivering it.** SES returns one for an
address on the account suppression list and then drops the mail
([`ses-suppression.md`](ses-suppression.md)). If the logs say `sent` and no mail arrived,
that is where to look.

## 8. What this runbook does not cover

- **Failure (`ruf`) reports.** `fo=1` is published but there is no `ruf=` address, so per-message
  forensic reports are not collected. They carry recipient addresses and are a privacy
  liability; aggregate reports are sufficient for the policy decision.
- **Moving the policy.** §4 says when the evidence justifies it and what the ramp is, but the
  change itself is a one-line edit to `aws_route53_record.dmarc` applied by the owner. Nothing
  automates it, deliberately: the judgement in §4's condition 3 — is this failing source ours
  or a forgery? — is a person's.
