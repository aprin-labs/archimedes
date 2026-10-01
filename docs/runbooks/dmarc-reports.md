# DMARC aggregate reports — reading them, and when they justify tightening the policy

> **status:** runbook
> **owner:** Dan Browne
> **updated:** 2026-09-03
> **superseded-by:** —

**Scope:** the `dmarc-reports@archimedes-arc.com` inbox — where reports land, how to turn a
pile of them into a per-source-IP pass/fail table, how to read that table, and the specific
evidence that has to exist before `_dmarc.archimedes-arc.com` moves off `p=none`.

**Read this first if:** you are about to change `p=` in
[`infra/dns_email.tf`](../../infra/dns_email.tf), or someone has asked whether the domain is
being spoofed.

**Do not read this expecting** a way to see reports today. The receipt rule that collects
them is [`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf), added by
[#1504](https://github.com/aprin-labs/archimedes/issues/1504), and **the bucket is empty
until it is applied**. Everything below describes the state after that apply.

---

## 1. What is wired, and what each piece does

| Piece | Where | What it does |
|---|---|---|
| `rua=mailto:dmarc-reports@archimedes-arc.com` | [`infra/dns_email.tf`](../../infra/dns_email.tf) `aws_route53_record.dmarc` | Tells every receiver where to send aggregate reports. Published since #1462. |
| `MX 10 inbound-smtp.us-east-1.amazonaws.com` | [`infra/ses_inbound.tf`](../../infra/ses_inbound.tf) `aws_route53_record.mx` | Points the domain's inbound mail at SES. Stood up for `privacy@` in #1460. |
| Receipt rule set `archimedes-inbound` | [`infra/ses_inbound.tf`](../../infra/ses_inbound.tf) | The one **active** rule set. A second rule set would not be active and would collect nothing. |
| Receipt rule `dmarc-reports` | [`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf) | Matches `dmarc-reports@archimedes-arc.com` and writes the message to S3 under `reports/`. **This is the piece that was missing.** |
| S3 bucket `archimedes-dmarc-reports-<account>` | [`infra/dmarc_reports.tf`](../../infra/dmarc_reports.tf) | Private (public-access block on all four flags), SSE-S3, 180-day expiry. |
| Parser | [`scripts/dmarc_report_summary.py`](../../scripts/dmarc_report_summary.py) | Turns the pile into one table. |

The MX and the `rua` were already live before #1504. That is exactly why nothing was being
collected and nobody noticed: the world was being *told* to send reports to an address that
had no receipt rule behind it. Whether SES refuses those messages at SMTP time or takes them
and discards them, the outcome on our side is identical and it is the problem — **nothing is
stored, and no failure surfaces in our account.** Any delivery error is handled by the
reporting receiver, on their side, where we never see it.

So the symptom of the whole thing being broken is silence, which is also the symptom of
nobody spoofing us. Everything below is designed around not confusing the two.

## 2. Get the table

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

## 6. What this runbook does not cover

- **Failure (`ruf`) reports.** `fo=1` is published but there is no `ruf=` address, so per-message
  forensic reports are not collected. They carry recipient addresses and are a privacy
  liability; aggregate reports are sufficient for the policy decision.
- **A scheduled weekly summary.** The owner asked for the parser to run on a schedule and post
  a per-source table ([#1504](https://github.com/aprin-labs/archimedes/issues/1504), 2026-09-03).
  Not built yet — today this is a command an operator runs. Until it exists, running §2 once a
  week during the ramp is the procedure.
