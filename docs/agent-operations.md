# Agent operations

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31
> **superseded-by:** —

How this team dispatches AI agents against the repo: how to write work they can execute, how
to fan out without paying the fan-out tax for zero parallelism, and when an agent may act as
proxy for an absent human. Extracted from [`../CLAUDE.md`](../CLAUDE.md) on 2026-08-31 — the
session file keeps the one-line rules an agent gets wrong by default and points here for the
mechanics.

Companion docs: [`agent-gotchas.md`](agent-gotchas.md) (character limits, zsh quoting) ·
[`prompts/agentic-issue-skeleton.md`](prompts/agentic-issue-skeleton.md) (the copy-paste
issue skeleton) · [`testing-conventions.md`](testing-conventions.md).

## Spec-driven execution

> **Who executes, as of 2026-08-03.** The autonomous agent account `t2o2` (Chuan Bai's
> system) is **not an active resource.** Chuan stepped back on 2026-06-24 and no work is
> being dispatched to `t2o2`. Do not assign issues to it, do not plan around it, and do not
> infer availability from older documents. The five `*-t2o2-issue.md` specs it executed were
> removed by that series; they survive only as references inside `archive/`, and a reference
> is not a live capability. Historical references to it are preserved as record, not as
> instruction.
>
> **The discipline below still applies in full** — it was always about spec quality, not
> about which executor consumes the spec. Today the executor is a Claude Code session run by
> a human teammate, working an issue on a branch and opening a PR.

**A well-specified issue is executable work** — often the highest-value thing a human plus a
hosted Claude can produce is a judge-grade issue spec, not hand-written code. Humans plan and
spec; the session executes; humans review the PR. Vague issues produce vague code, so spec
quality is the throughput lever. Skeleton:
[`prompts/agentic-issue-skeleton.md`](prompts/agentic-issue-skeleton.md).

Operational mechanics, hard-won 2026-05-18 — the spec is only half the job:

- **Trigger = a human picking it up.** There is no dispatch bot today. An issue is executed
  when a teammate opens a session against it, so an unassigned judge-grade spec sits idle
  until someone claims it. Assign the issue to the human who is doing it, so two people don't
  start the same work. The `APIN - <Area> - <Title>` prefix is a naming convention, not a
  trigger, and never was.
- **A claimed issue is authorized. Do not close on lane grounds.** If a session has taken an
  issue, execute it — regardless of which teammate's nominal lane it touches. The
  lead/coverage table in [`team.md`](team.md) lists reviewers and memory-carriers, **not
  permission boundaries.** Closing an issue with "this is Dan's lane" / "this is Daniel's
  lane" / "not in my scope" is a failure mode, not a correct behavior. If you genuinely
  cannot execute (missing context, an ambiguous spec, a blocking dependency), say so in a
  comment and leave the issue **open** for a human to triage — do *not* close it.
- **Acceptance must be machine-checkable.** Give the exact command *and* its exact expected
  output (`pytest → 0 failed`, `coverage ≥ 80%`), never prose like "make it robust." The
  system optimizes to the literal criteria.
- **Pin the environment.** The system's env has Docker/Redis/DB; a judge's cold clone does
  not. If it must pass clean, say "clean clone, no docker, no env vars" explicitly — it won't
  infer the constraint.
- **Anti-goals are load-bearing.** State what *not* to touch ("don't weaken thresholds, don't
  edit `pytest.ini`, don't add e2e deps") to bound blast radius.
- **Cite a precedent.** Point at an existing good pattern to copy (a fixture, a sibling test
  file) — it reuses the right shape instead of inventing one.
- **Verify independently — "closed" ≠ "fixed".** Sessions sometimes close an issue without
  resolving it. Re-check against the acceptance command on a cold clone before trusting
  completion; reopen with evidence if unmet.

### Pre-close verification gate (added 2026-05-24)

Before closing *any* issue, the executing session MUST:

1. Run every acceptance-criteria command listed in the issue and verify the exact expected
   output matches.
2. For every anti-goal / "DO NOT" directive (e.g. "DO NOT keep `setMode` in `Generate.jsx`"),
   run an explicit `grep` or equivalent check proving the forbidden pattern is absent. If the
   grep finds a match → the issue is not done.
3. If any acceptance check or anti-goal check fails, do **not** close the issue. Instead,
   comment with the failing evidence and leave the issue **open**.

This gate exists because three issues (#166, #167, #168) were closed with commits that
touched unrelated files or made cosmetic edits that passed a naive heuristic without doing
the structural work. Pattern-matching on commit messages is not verification — running the
actual commands is.

### Verify your own audit claims before acting on them (added 2026-05-27)

When an agent (including yourself, earlier in the session) flags a finding like "X is in git
history" or "Y is a vulnerable dependency," verify it with the literal command before
recommending or applying the remediation. The session example: an audit message flagged
`infra/terraform.tfstate` as committed-to-git CRITICAL; subsequent verification with
`gh api search/code -f q="tfstate repo:..."` and `git rev-list --all --objects` confirmed it
was never tracked — a false alarm. Acting on unverified audit claims wastes work and erodes
trust in the agent's findings. The rule is symmetric: do not over-trust audit output from
your past self, and surface the verification command alongside any audit claim you make so
the next reader can re-run it cheaply.

## Parallel agent fan-out discipline

Hard-won 2026-05-16; ignore at your peril.

- **Probe with ONE canary agent before any fan-out.** If the canary is blocked at a step, the
  whole fan-out will be too — you pay the fan-out tax for zero parallelism.
- **The canary must match the fan-out's execution mode.** A foreground canary does *not*
  validate a background fan-out — they run under different sandboxes.
- **Background subagents are filesystem-sandboxed here** (no writes; cannot exec interpreters
  outside the project dir). Use **foreground** agents for implementation fan-out, or a scoped
  `permissions.allow` in `.claude/settings.json`.
- Parallel agents get **isolated git worktrees**, base-SHA-pinned to a recorded commit; do
  not commit to the base branch between dispatches.
- **Clean up worktrees + branches AS YOU GO, not just at session end (added 2026-06-25).**
  Parallel worktree-isolated agents accumulate fast — one session left **14 stale
  `.claude/worktrees/agent-*` dirs + ~24 branches**. Discipline: (1) when an agent finishes,
  remove its worktree (`git worktree remove --force <path>` — **never a locked /
  still-running one**) plus its local `worktree-agent-*` branch, then `git worktree prune`;
  (2) when a PR merges, delete its branch (`gh pr merge --delete-branch`, or
  `git push origin --delete <branch>` + `git branch -D`); (3) keep branches for **open PRs**
  and **in-flight agents**. Turn on the repo's "auto-delete head branches on merge" to halve
  the remote side. Always verify before bulk-deleting: cross-check `git worktree list` and
  `gh pr list --state open` so you never drop a running agent's or an open PR's branch.
- **Structure subagent responses to preserve parent context (added 2026-05-27).** When
  dispatching review-style subagents (PR review, audit, multi-file scan), specify both a
  structured response format (`Verdict / What it does / Concerns / Recommendation` per item)
  and a per-item word cap. Three subagents reviewing 8 PRs in parallel returned ~3000 words of
  structured per-PR verdicts that could be synthesized without re-reading any diff — the
  structure is what made the synthesis cheap. Unstructured "review these PRs and tell me what
  you think" produces long prose the parent has to re-read and re-organize, defeating the
  context-preservation reason for fan-out.

## Agent-as-proxy authorization (added 2026-05-27)

Teams have lanes (see the lead/coverage table in [`team.md`](team.md)) and humans have AI
agents that operate on their behalf. When a teammate is unresponsive for an extended window
(>24h) and work in their lane is blocked, their agent **is authorized to act as proxy for
backend code reviews and merges in that lane**, with two exceptions:

- **Solidity contract changes still require the human owner's explicit consent.** Contracts
  hold live funds; the owner's contract-specific judgment is load-bearing. An agent can
  review and recommend, but **Dan (the contract owner, who deploys them himself) must approve
  the merge** — and where possible **Bogdan (`mnemonik-dev`) provides the two-eyes contract
  review**. (Updated 2026-06-24: contract approval routes to Dan, not Chuan, after the
  ownership change. Updated 2026-08: Bogdan is not currently active — Dan is the sole
  required approver; two-eyes review resumes when a second contract reviewer is available.)
- **Architecture decisions and infrastructure cost commitments** (new AWS services, recurring
  spend, multi-day migrations) still warrant **Dan's** ack — he owns the AWS account.
  Operational fixes within an already-approved architecture are fine to proxy.

This unblocks work without compromising the high-stakes review surfaces. Document each
proxy-merge action in the PR description with a one-line note ("Reviewed by `<agent>` on
Dan's behalf — Dan offline since `<timestamp>`") so the human can audit on return. If the
human disagrees on return, revert and re-review — the proxy is a stop-gap, not a delegation.
