# ADR: Build-on-deploy, main-only branch model

> **Audience:** Archimedes team (decision owner: Dan Browne)
> **Status:** Accepted
> **Date:** 2026-05-18
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** How does a 5-person async team across 5 timezones integrate work continuously without a queue-prone `develop` branch?
> **Related:** CLAUDE.md § "Branch model (build-on-deploy, main-only)", `.github/workflows/deploy.yml`, `release-tag.yml`.

## TL;DR

**`main` is the only long-lived branch, and it is the deploy branch.** Every merge to `main` triggers a CI build + deploy to the live stack — today a build-and-push to ECR followed by an Alembic migrate task and an ECS Fargate force-redeploy ([`.github/workflows/deploy.yml`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/.github/workflows/deploy.yml); see [`ec2-to-ecs-fargate-cutover.md`](ec2-to-ecs-fargate-cutover.md)). It was an in-place EC2 build + restart when this ADR was codified; the branch model is unchanged by that swap. The `develop`/integration branch is retired (2026-05-18, drifted unused). Short-lived per-owner branches (`<handle>/<name>`) → PR → merge to `main`. Merge commits only, so `git log --graph` shows unit-of-work boundaries. Parallel Claude Code sessions land work on `main` and iterate on CI there, so `main` moves continuously — branch late, rebase right before merge, merge fast.

## Context

The hackathon started with a classic gitflow `main` + `develop`. Reality diverged: the agentic system and Dan's sessions merged directly to `main` and iterated on CI failures there, so `develop` drifted behind and became a high-touch, error-prone re-merge nobody wanted to do. Gitflow's `develop` is a queue — bearable when colocated and synchronous, invisible-until-you-merge when distributed. In a 5-timezone async team the cost of maintaining the queue (rebase, conflict, re-test) grows faster than the cost of shipping from `main` + hotfixes.

## Decision

1. **`main` is the single live + deploy branch.** Every merge → CI build + deploy via `deploy.yml`. No `develop`.
2. **Short-lived per-owner branches** (`<discord-handle>/<short-name>`) → PR → merge → delete. Rebase onto `main` right before merging.
3. **`main` moves continuously** — treat it as a fast-moving deploy queue, not a staging area. Don't wait for it to "settle"; it won't.
4. **Merge commits only** (squash/rebase-merge disabled in repo settings) so branch topology + the "this was one PR" signal survive for forensics and release tagging.
5. **Hard rules:** never force-push `main`; never commit secrets/`.env`; one logical change per PR. Force-pushing your own unmerged branch is fine. Contract changes still warrant Dan's review + Bogdan's audit.

## Consequences

### Positive
- **No queue bottleneck** — one branch, no invisible `develop` backlog.
- **Continuous delivery by default** — every merge is a deploy; no "merged but not shipped" limbo.
- **Tight agentic iteration** — a session can land, observe CI, fix, re-land, all on `main`.
  (This was originally written for the `t2o2` agent account, which is dormant as of
  2026-08-03; the property holds for any executor and is why the decision stands.)
- **Forensically clear history** — merge commits preserve "this was PR #NNN"; `git log --graph` reads cleanly.

### Negative / costs we accept
- **`main` is always live** — a broken commit breaks prod immediately. Mitigated by hard CI gates (`quality-gate.yml`), smoke-test discipline, and one-`revert`-away rollback.
- **Rebase discipline required** — rebase before merge or conflicts pile up. Mitigated by short-lived branches (<24h).
- **Release tagging is post-hoc** — `release-tag.yml` infers the version bump from the PR title marker (`!minor`, `!version-release`), so titles must be accurate.

## Alternatives considered
- **Keep gitflow (`develop` → `main`) — rejected:** it drifted in practice; the queue is invisible in a distributed team and the agentic system works better on `main` directly.
- **Deploy on every push to any branch — rejected:** too expensive (gates per push) and rollback breaks down with concurrent pushers. Single PR → `main` → deploy is the right middle ground.

## Ratification

Adopted 2026-05-18; merge-commit-only enforced 2026-05-27. The decision reflected how the team actually works. Enforced via repo settings + `release-tag.yml`; `main` self-heals formatting via `main-format-guard.yml`.
