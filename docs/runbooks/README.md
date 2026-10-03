# Runbooks — index, and what is missing

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-03
> **superseded-by:** —

A runbook is a procedure someone follows **under pressure**, usually during an incident.
That is the test for whether something belongs here: commands, expected output, and a way
back. If it argues rather than instructs, it belongs somewhere else — see
[`../CONVENTIONS.md`](../CONVENTIONS.md) § 1.

This index covers `docs/runbooks/`. Deploy- and infrastructure-level procedures live next to
the Terraform that owns them, under `infra/runbooks/`, and are listed here too — a reader in
an incident should not have to know which tree a procedure lives in.

Incident write-ups are evidence rather than procedure, so they live in `docs/incidents/`; the one an operator reaching for these pages is most likely to need is [`../incidents/2026-09-03-paper-advance-ddl-wedge.md`](../incidents/2026-09-03-paper-advance-ddl-wedge.md) — the paper-advance DDL lock wedge (#1818), where a boot hung for 91 minutes while `/health` kept answering 200.

## Runbooks that exist

| Runbook | Owner | What it is for |
|---|---|---|
| [`operations.md`](operations.md) | Dan Browne | Run the stack locally and in production: services, RPC deep-dive, LLM backends, security notes. The general-purpose starting point. |
| [`arc-testnet-e2e.md`](arc-testnet-e2e.md) | Dan Browne | End-to-end smoke test against Arc testnet — the check that the on-chain path is alive. |
| [`arc-testnet-e2e-evidence.md`](arc-testnet-e2e-evidence.md) | Önder Akkaya | Replayable on-chain evidence for SPEC-1. Evidence, not a procedure; kept alongside the test that produces it. |
| [`spec-1-walkthrough.md`](spec-1-walkthrough.md) | Dan Browne | SPEC-1 user-journey walkthrough, start to finish. |
| [`t3.2-contract-redeploy.md`](t3.2-contract-redeploy.md) | Dan Browne | Contract redeploy procedure and secret handling. |
| [`github-security-toggles.md`](github-security-toggles.md) | Dan Browne | Repository security settings and how to change them. |
| [`docs-site-setup.md`](docs-site-setup.md) | Dan Browne | Docs site on our own S3 + CloudFront (#1634): apply `docs-site/infra`, publish, invalidate, roll back, local preview, and the `mkdocs --strict` findings. |
| [`cost-kill-switch.md`](cost-kill-switch.md) | Dan Browne | The automatic AWS spend brake: how the 50/80/120% budget ladder and the billing tripwire fire, the one-command recovery, the billing-lag caveats, and the measured monthly baseline. || [`backtest-results-retention.md`](backtest-results-retention.md) | Dan Browne | `backtest_results` archive-then-prune procedure (v8 Lane 3.1): keep policy, the `--plan`/`--archive`/`--prune` flags, the manifest-verification guard, and the post-prune VACUUM step. |
| [`curated-backtests.md`](curated-backtests.md) | Dan Browne | Producing curated backtest rows with `run_backtests.py` (#1760) — the three triggers that justify a run, the one-off Fargate `run-task` invocation, what to check in the summary, and the four things this must never do (no clock, no boot hook, never in the serving process, never for generated strategies). |
| [`market-data-provider-proof.md`](market-data-provider-proof.md) | Dan Browne | Wiring the Tiingo token onto the backend container and proving the pull (#1798): the two task-definition registrars and which one ships, how to confirm the secret on the live revision, the fixed-window `verify_market_data.py` proof on the daily seam, and the blast radius of each of the two provider variables (#1798). |
| [`terraform-apply-and-task-definition-ownership.md`](terraform-apply-and-task-definition-ownership.md) | Dan Browne | Running `infra/apply.sh` after #1799: who owns the backend ECS task definition (`container_definitions` is pipeline-owned, everything else is Terraform's), why editing `infra/ecs.tf` no longer reaches production, how to make a deliberate task-definition change anyway, the two residuals a clean untargeted plan still shows, and how to arm the `terraform-drift` gate. |
| [`cloudfront-cache-behaviour-apply.md`](cloudfront-cache-behaviour-apply.md) | Dan Browne | Applying a CloudFront cache-behaviour change with `infra/apply.sh` — the plan to expect, whether an invalidation is needed, the `age`/`x-cache` verification, and the failure modes to roll back from. Written for #1768 (`/app`, `/app/*`, `/sign-in*` on CachingDisabled — now applied) and #1776 (`/fonts/*` and the image/icon/font suffixes onto the 1h `static_assets` policy), then generalised. |
| [`erc8004-identity-registration.md`](erc8004-identity-registration.md) | Dan Browne | Minting the ERC-8004 agent identity on Arc (#1527): live-verified registry facts, `--plan`/`--verify`/`--execute`, the Circle-signed owner step, and the honest surface flip. |
| [`runner-ec2-wedge.md`](runner-ec2-wedge.md) | Dan Browne | The `archimedes-runner` box wedging (#1402) — impaired instance check, healthy system check, dead SSM agent. Symptoms, read-only diagnosis, the recovery ladder, and what the `ec2:reboot` alarm now does for you before you get there. |
| [`email-verification-validation.md`](email-verification-validation.md) | Dan Browne | Validating signup verification and password reset against a real inbox before `EMAIL_VERIFICATION_ENFORCED` is flipped: pre-flight checks, the local and sandbox rehearsals, the reset rehearsal, the flip's blast radius and rollback, and the SES sandbox-vs-production gotchas. Human-only — every step is a person with a browser and a mailbox. |
| [`ses-bounce-signal.md`](ses-bounce-signal.md) | Dan Browne | The SES bounce/complaint feedback loop (#1804): configuration set → SNS → SQS → `ses_events drain` → `auth_users.emailBouncedAt`. How to drain the queue, how to tell a live loop from a deaf one, what the stamp means, and the **two-command** procedure for letting a wrongly-blocked address back in (the suppression list and the stamp are separate, and clearing one alone changes nothing). |
| [`ses-suppression.md`](ses-suppression.md) | Dan Browne | The AWS account-level SES suppression list (#1748 item 4): a suppressed address makes `SendEmail` succeed and the message vanish, so silence is the symptom. Read-only inspection, the three conditions that must all hold before one address comes off, why there is no bulk clear, and the verify-then-watch step. Tooling: `backend/archimedes/scripts/ses_suppression.py`, dry-run by default. |
| [`dmarc-reports.md`](dmarc-reports.md) | Dan Browne | The `dmarc-reports@` inbox (#1504): what is wired end to end, how to turn a pile of zipped XML into a per-source-IP pass/fail table, the two verdicts in an aggregate report and which one is DMARC, the four conditions that must all hold before `p=none` moves, the quarantine→reject ramp and its rollback asymmetry, the read-only ladder for "no reports are arriving", and the separate one for reports that arrive and cannot be parsed. The weekly summary that mails that table to the owner every Monday, what a healthy one looks like, and how to force one by hand. Tooling: `scripts/dmarc_report_summary.py`, `archimedes.scripts.dmarc_weekly_summary`. |
| `infra/runbooks/ecs-fargate-cutover.md` | owner of `infra/` | The 2026-07-09 EC2 → ECS Fargate cutover, **including the rollback procedure**. This is the closest thing to a break-glass path that currently exists. |
| `infra/runbooks/disaster-recovery.md` | owner of `infra/` | Recovery from data-store and account-level loss. |

Historical, **do not execute**: [`../archive/deployment-runbook.md`](../archive/deployment-runbook.md)
— the EC2-era manual/break-glass deploy runbook. Its procedure routes traffic to instance
`i-01803d3abc271d39b`, which was detached from the ALB target group during the Fargate
cutover and then decommissioned on 2026-08-19 (stopped, snapshotted, terraform deleted —
`infra/main.tf`), so following it during an incident operates on a host that no longer
exists. It is kept for its incident-response history and diagrams.

## Not yet written

Naming a gap is worth more than leaving it silent. An operator who searches this directory
during an incident and finds nothing cannot tell "no runbook" from "I searched badly."

### Fargate-era break-glass procedure — **MISSING**

**Owner:** whoever owns `infra/`. **Status:** not started. **Blocking:** nothing but the
writing — the underlying capability exists, it is just not written down.

Archiving [`../archive/deployment-runbook.md`](../archive/deployment-runbook.md) left the repo
with **no break-glass runbook at all**. `infra/runbooks/ecs-fargate-cutover.md` § rollback
covers the planned reversal of one specific migration; it is not a procedure for "production
is down at 03:00 and CI cannot deploy." Until this is written, that scenario has no
documented answer.

What it must cover, at minimum:

1. **Roll the ECS task-definition revision.** How to identify the last known-good revision,
   how to force a service update onto it, and what to do when the rollback target itself is
   the broken one. Include the deregistration/draining behaviour so the operator knows how
   long to wait rather than re-running the command.
2. **The ALB target-group state.** How to read current target health, what a healthy set
   looks like versus a draining or empty one, and how to confirm the new tasks actually
   registered. **This is precisely what the archived runbook got wrong** — it operated on a
   detached instance and gave no way to notice. The replacement must make target-group state
   something the operator verifies, not assumes.
3. **The CloudFront invalidation.** When a task rollback alone is insufficient because the
   edge is still serving cached assets, which paths to invalidate, and how long propagation
   actually takes before the operator should conclude it did not work.
4. **How you know it worked.** Explicit success criteria: which health endpoint, which
   CloudWatch alarms should clear, which target counts, and how long to watch before
   declaring the incident over. A break-glass procedure without a verification step is how
   you get a second incident on top of the first.

Write it under `infra/runbooks/`, next to the Terraform whose state it depends on, and link it
from this index and from [`../doc-index.md`](../doc-index.md) in the same commit.
