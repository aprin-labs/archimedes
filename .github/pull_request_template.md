<!--
One logical change per PR. Merge with `gh pr merge <n> --merge` — squash and rebase are off.

Release tag: the bump marker is read from the END of the PR TITLE. `!minor` for a new
user-facing capability, `!version-release` for a major milestone, nothing otherwise.

Delete any section below that does not apply. Comments like this one do not render.
-->

## What

<!-- One or two sentences. What changed. -->

## Why

<!-- The problem this solves. Link the issue, ADR, or incident. -->

## Issues

<!--
Use a real closing keyword so the issue closes when this merges: `Closes #123`,
`Fixes #123`, `Resolves #123`. The keyword goes immediately before each `#`, once per
issue — `Closes #1 and #2` closes only #1.

Without a keyword the work lands and the issue stays open forever. That is how a pile of
already-fixed-but-still-open issues accumulates, and nobody ever goes back to drain it.

Referencing without closing is correct for partial work — write it so the intent is
legible: `Part of #123`, `See #123`, `Supersedes #123`.
-->

Closes #

## Verification

<!--
The commands you ran and their output. Not "tests pass".

Two things a reviewer cannot check for you (CLAUDE.md § Before you approve a merge):

  - A regression test must fail with the fix reverted. Revert it, run the test, paste the
    failure. A test that passes against the unfixed code guards nothing.
  - A guard must be shown to reject something. Build the input that should fail it, run it,
    paste the rejection.

If the PR body asserts a property, the code has to enforce it — a claim in prose is the
same defect, just harder to grep for.

Drop this section for pure copy or docs changes that cannot be exercised.
-->

## What a reviewer should push back on

<!-- Trade-offs, scope you deliberately left out, anything you are unsure about. -->
