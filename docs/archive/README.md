# Archived design documents

> **Ten business/competition documents were routed to the private docs repo on 2026-08-19**
> (pitch deck outline, judging self-assessment, pitch talking points, Arc alignment strategy,
> submission packets, RFB mapping, market-sizing analysis, launch GTM plan) — moved under the
> content-routing policy in `docs/CONVENTIONS.md`; private copies live in the docs repo's
> `ported-from-public-2026-08-02/`. Do not re-create them here.

> **Status:** Day-14 refresh (2026-05-25, submission day). Curation: docs in
> this folder were authoritative at an earlier phase of the project and have
> since been superseded by a current document or by shipped code. They're
> kept for traceability — judges, reviewers, and future readers can see what
> we considered and what we settled on — but they should not be read as the
> current shape of the product.
> **Rule:** if you're trying to understand Archimedes *today*, read the current
> docs listed under each entry below. The archived doc is the history, not the
> contract.

## What's in here, and what supersedes each

| Archived doc | Was the canonical… | Now superseded by |
|---|---|---|
| [`mvp-scope-memo.md`](mvp-scope-memo.md) | Day-3 MVP scope memo (single-vault → marketplace pivot framing) | [`docs/user-stories.md`](../user-stories.md) for the product spine; [`AUDIT_2026-06-14.md`](../audits/2026-06-14-full-tree-audit.md) for current submission scope |
| ``rfb-alignment.md`` (routed to the private docs repo, 2026-08-19) | Day-1/2 RFB (Request-for-Build) mapping for the hackathon brief | The current Arc-Circle alignment doc ``docs/arc-alignment.md`` (routed to the private docs repo, 2026-08-19) + the deck framing in ``docs/demo-script-pitch-deck-outline.md`` (routed to the private docs repo, 2026-08-19) |
| [`qfin-paper-corpus-seed.md`](qfin-paper-corpus-seed.md) | Original 200-paper q-fin corpus seed-curation spec | [`docs/corpus-architecture.md`](../corpus-architecture.md) — covers the current DB-backed substrate end-to-end (live row count: `GET /health`; do not freeze a number) |
| ``agora_project_analysis.md`` (routed to the private docs repo, 2026-08-19) | Day-2 red-team synthesis that drove the Day-3 rigor-as-wedge pivot | The pivot is now codified in [`docs/architectural-principles.md`](../architectural-principles.md) + [`docs/specs/selection-bias-corrections-spec.md`](../specs/selection-bias-corrections-spec.md); the rigor wedge is shipped — the analysis that argued for it lives here as historical reasoning |
| ``launch-plan-2026-05-19.md`` (routed to the private docs repo, 2026-08-19) | Day-8 coordinated 3-repo reveal launch plan | Launch executed during the Day-12 / Day-13 window (HTTPS landed via PR #240; demo recorded; v1 form submitted). Forward planning now lives in [`AUDIT_2026-06-14.md`](../audits/2026-06-14-full-tree-audit.md). |
| [`ui-simplification-proposal-2026-05-20.md`](ui-simplification-proposal-2026-05-20.md) | Day-9 12-page → 5-page spine consolidation proposal | Spine Phases 0–3 + 6 + 7 shipped via [`docs/specs/spine-plus-v2-plan.md`](../plans/spine-plus-v2-plan.md); current page roles live in [`docs/specs/page-roles-spec.md`](../specs/page-roles-spec.md). |
| [`evening-execution-plan-2026-05-24.md`](evening-execution-plan-2026-05-24.md) | Sunday-evening session plan + red-team report after PR-3 #213 merged | All 21+ red-team items either shipped (PR #220–#240) or escalated to issues. State after this session is captured in `sunday-night-handoff-2026-05-24.md` (also archived) and the audit lineage head `AUDIT_2026-06-14.md`. |
| [`sunday-night-handoff-2026-05-24.md`](sunday-night-handoff-2026-05-24.md) | Post-HTTPS-landing Sunday-night handoff between Claude sessions | HTTPS live; wallet flow shipped; the remaining priorities (verify_arc_e2e --execute, v_check production validation, judge-trust polish) folded into [`AUDIT_2026-06-14.md`](../audits/2026-06-14-full-tree-audit.md). |
| [`deployment-runbook.md`](deployment-runbook.md) | Manual / break-glass AWS deploy runbook for the EC2 era (account 037613907429) | **Nothing yet for break-glass.** Rollback: [`infra/runbooks/ecs-fargate-cutover.md`](../../infra/runbooks/ecs-fargate-cutover.md) § rollback + [`infra/runbooks/disaster-recovery.md`](../../infra/runbooks/disaster-recovery.md). The Fargate-era break-glass runbook is an open gap tracked in [`docs/runbooks/README.md`](../runbooks/README.md). |

### Launch-window execution plans and runbooks (2026-05-19 → 2026-05-25)

Session-scoped plans from the launch and submission window. Each was accurate for the hours it
covered and is now history; none describes the current system. Superseded collectively by
[`docs/audits/2026-06-14-full-tree-audit.md`](../audits/2026-06-14-full-tree-audit.md) for state
and by [`docs/runbooks/README.md`](../runbooks/README.md) for operational procedure.

| Archived doc | What it was |
|---|---|
| [`launch-execution-plan-2026-05-23.md`](launch-execution-plan-2026-05-23.md) | Day-11 launch execution plan. |
| [`launch-night-operational-runbook.md`](launch-night-operational-runbook.md) | Overnight 2026-05-23 → 05-24 launch-night operational runbook. |
| [`morning-execution-plan-2026-05-24.md`](morning-execution-plan-2026-05-24.md) | Morning plan, post-overnight and post-pi-verification. |
| [`afternoon-execution-plan-2026-05-24.md`](afternoon-execution-plan-2026-05-24.md) | Afternoon plan, post-merge-train pre-compact handoff. |
| [`phase5-execution-runbook.md`](phase5-execution-runbook.md) | Phase-5 real-testnet trade-execution runbook. Superseded by the ADR [`adr/arc-settlement-chain.md`](../adr/arc-settlement-chain.md) and the live settlement path. |

### `agora-2026-05/` — the Agora submission packet (2026-05-25)

The hackathon submission set, frozen on submission day. Kept whole because its value is as a
snapshot: it records what was claimed, to whom, on a specific date. Do not read any of it as
current architecture — [`docs/architecture.md`](../architecture.md) is.

| Archived doc | What it was |
|---|---|
| ``agora-2026-05/AGORA-SUBMISSION-PACKET.md`` (routed to the private docs repo, 2026-08-19) | The submission packet itself. |
| ``agora-2026-05/ARC-OSS-SHOWCASE.md`` (routed to the private docs repo, 2026-08-19) | Arc OSS showcase entry. |
| ``agora-2026-05/arc-alignment.md`` (routed to the private docs repo, 2026-08-19) | Arc/Circle alignment argument as submitted. |
| ``agora-2026-05/judging-rubric-assessment.md`` (routed to the private docs repo, 2026-08-19) | Day-13 self-assessment against the judging rubric. |
| ``agora-2026-05/pitch-talking-points-rigor-track.md`` (routed to the private docs repo, 2026-08-19) | Talking points for the rigor / provenance / agent track. |
| [`agora-2026-05/aws-architecture.md`](agora-2026-05/aws-architecture.md) | EC2-era AWS architecture. Superseded by [`docs/architecture.md`](../architecture.md) and [`adr/ec2-to-ecs-fargate-cutover.md`](../adr/ec2-to-ecs-fargate-cutover.md). |
| [`agora-2026-05/design.md`](agora-2026-05/design.md) | The original design document. |
| [`agora-2026-05/infra-setup.md`](agora-2026-05/infra-setup.md) | Infrastructure and CI/CD setup as it stood at submission. |
| [`agora-2026-05/chuan-architecture-survey.md`](agora-2026-05/chuan-architecture-survey.md) | Chuan's survey of `backend/archimedes/`. Predates the Chuan → Dan ownership transition. |
| ``agora-2026-05/demo-script-pitch-deck-outline.md`` (routed to the private docs repo, 2026-08-19) | Demo script and pitch-deck outline. Archived demo script: [`demo-script-lepton.md`](demo-script-lepton.md). |
| [`agora-2026-05/portfolio-advisor-demo-cues.md`](agora-2026-05/portfolio-advisor-demo-cues.md) | 60-second Portfolio Advisor demo cue card. |
| ``agora-2026-05/claude-design-prompts.md`` (routed to the private docs repo, 2026-08-19) | Design prompts used to generate the submission UI; routed because its slide prompts embed competitive comparisons, market-sizing figures, and judging strategy. |
| [`agora-2026-05/traction-logging.md`](agora-2026-05/traction-logging.md) | `arc-canteen` traction-logging cheat sheet. |

## Why these specifically

The archived docs share a pattern: each was *load-bearing during its phase* but has been **(a)** displaced by a current doc that's tighter and more accurate, **(b)** rendered partly obsolete by shipped code, or **(c)** both. Reading them alongside current docs would create noise — they argue for things we now treat as settled, in vocabulary that's drifted.

The current docs (listed in the right column above) are the canonical references going forward. If something looks wrong or missing in a current doc, fix the current doc — don't reach for the archived one.

## Architecture decision records

`docs/specs/backtrader-vs-vectorbt-decision-memo.md` was moved to [`docs/adr/backtrader-backtest-engine.md`](../adr/backtrader-backtest-engine.md) (renamed 2026-07-28 to match the directory convention)
rather than here. ADRs are durable decisions that future contributors need to understand
even though they're "settled" — they're not stale, they're load-bearing context.
