# Agentic issue spec — copy-paste skeleton

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31
> **superseded-by:** —

The template for an issue a Claude Code session can execute end-to-end, with the human in
the loop at review rather than during the work. The **gates** that make an issue safe to
hand off — a named owner, machine-checkable acceptance, the pre-close verification gate —
live in [`../agent-operations.md`](../agent-operations.md) § Spec-driven execution and are
not repeated here. This file is just the shape.

There is no autonomous dispatch bot. `t2o2` is dormant (see
[`../agent-operations.md`](../agent-operations.md)); a spec is executed when a person opens
a session against it.

```markdown
## Summary             <!-- one paragraph: the problem, why it matters -->
## Scope               <!-- exact files/interfaces; "do exactly this, nothing more" -->
## Acceptance criteria <!-- checkboxes, each a runnable command + expected output -->
## Verify              <!-- the literal commands a reviewer runs -->
## Anti-goals          <!-- what NOT to do; what NOT to touch -->
```

Then claim it — the spec is inert until someone picks it up:

```bash
gh issue edit <n> --add-assignee <your-github-handle>
```

## Section notes

- **Summary** — the problem, not the solution. If the agent can only infer the goal from the
  proposed fix, it will optimise the fix rather than the goal.
- **Scope** — name the files and interfaces. "Do exactly this, nothing more" is load-bearing
  phrasing; it measurably narrows blast radius.
- **Acceptance criteria** — every checkbox is a command *plus its exact expected output*
  (`pytest -q → 0 failed`, `coverage ≥ 80%`). Never prose like "make it robust": the system
  optimises to the literal criteria.
- **Verify** — the same commands a human reviewer runs. State environment assumptions
  explicitly ("clean clone, no docker, no env vars") — the system's environment has
  Docker/Redis/DB and it will not infer a colder constraint.
- **Anti-goals** — what not to touch ("don't weaken thresholds, don't edit `pytest.ini`,
  don't add e2e deps"). These are what the pre-close anti-goal grep checks against, so write
  them as things a `grep` can prove absent.
- **Cite a precedent** — point at an existing good pattern to copy (a fixture, a sibling
  test file). It reuses the right shape instead of inventing one.

Exemplars: issues #76 and #77 are written to this standard.
