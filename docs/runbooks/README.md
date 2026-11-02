# Runbooks — index, and what is missing

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-11-01
> **superseded-by:** —

A runbook is a procedure someone follows **under pressure**, usually during an incident.
That is the test for whether something belongs here: commands, expected output, and a way
back. If it argues rather than instructs, it belongs somewhere else — see
[`../CONVENTIONS.md`](../CONVENTIONS.md) § 1.

This index covers `docs/runbooks/`. The hosted deployment (ECS Fargate, ALB, CloudFront,
Aurora, the runner EC2) was torn down on 2026-11-01, and its procedures went with it: the
`infra/runbooks/` tree and the AWS-only runbooks that were here are preserved at
[the last commit that had them](https://github.com/aprin-labs/archimedes/tree/84c0fac4758e9572f34e4f70fe48e9d68c5acab0). Several runbooks below still describe a hosted
step alongside the local one; the hosted step is historical.

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
| [`backtest-results-retention.md`](backtest-results-retention.md) | Dan Browne | `backtest_results` archive-then-prune procedure (v8 Lane 3.1): keep policy, the `--plan`/`--archive`/`--prune` flags, the manifest-verification guard, and the post-prune VACUUM step. |
| [`curated-backtests.md`](curated-backtests.md) | Dan Browne | Producing curated backtest rows with `run_backtests.py` (#1760) — the three triggers that justify a run, the one-off Fargate `run-task` invocation, what to check in the summary, and the four things this must never do (no clock, no boot hook, never in the serving process, never for generated strategies). |
| [`market-data-provider-proof.md`](market-data-provider-proof.md) | Dan Browne | Wiring the Tiingo token onto the backend container and proving the pull (#1798): the two task-definition registrars and which one ships, how to confirm the secret on the live revision, the fixed-window `verify_market_data.py` proof on the daily seam, and the blast radius of each of the two provider variables (#1798). |
| [`erc8004-identity-registration.md`](erc8004-identity-registration.md) | Dan Browne | Minting the ERC-8004 agent identity on Arc (#1527): live-verified registry facts, `--plan`/`--verify`/`--execute`, the Circle-signed owner step, and the honest surface flip. |
| [`email-verification-validation.md`](email-verification-validation.md) | Dan Browne | Validating signup verification and password reset against a real inbox before `EMAIL_VERIFICATION_ENFORCED` is flipped: pre-flight checks, the local and sandbox rehearsals, the reset rehearsal, the flip's blast radius and rollback, and the SES sandbox-vs-production gotchas. Human-only — every step is a person with a browser and a mailbox. |
| [`ses-bounce-signal.md`](ses-bounce-signal.md) | Dan Browne | The SES bounce/complaint feedback loop (#1804): configuration set → SNS → SQS → `ses_events drain` → `auth_users.emailBouncedAt`. How to drain the queue, how to tell a live loop from a deaf one, what the stamp means, and the **two-command** procedure for letting a wrongly-blocked address back in (the suppression list and the stamp are separate, and clearing one alone changes nothing). |
| [`ses-suppression.md`](ses-suppression.md) | Dan Browne | The AWS account-level SES suppression list (#1748 item 4): a suppressed address makes `SendEmail` succeed and the message vanish, so silence is the symptom. Read-only inspection, the three conditions that must all hold before one address comes off, why there is no bulk clear, and the verify-then-watch step. Tooling: `backend/archimedes/scripts/ses_suppression.py`, dry-run by default. |

Historical, **do not execute**: [`../archive/deployment-runbook.md`](../archive/deployment-runbook.md)
— the EC2-era manual/break-glass deploy runbook. Its procedure routes traffic to instance
`i-01803d3abc271d39b`, which was detached from the ALB target group during the Fargate
cutover and then decommissioned on 2026-08-19, so following it operates on a host that no
longer exists. It is kept for its incident-response history and diagrams.

## Not yet written

Naming a gap is worth more than leaving it silent. An operator who searches this directory
during an incident and finds nothing cannot tell "no runbook" from "I searched badly."

No gaps are recorded today. The Fargate-era break-glass procedure that used to be listed
here as missing is moot: there is no hosted stack left to break.
