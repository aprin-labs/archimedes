# Agent-generated wiki (OpenWiki)

> **Status:** current. **Owner:** Dan Browne. **Last verified:** 2026-08-31.

Every page under [`../openwiki/`](../openwiki/) was written by an AI agent, not by a
person. This page exists so that fact is stated once, in full, in a place a reader
passes through. (Until 2026-11-01 the wiki was also mounted into the mkdocs docs site,
whose build stamped a short version of this notice on each page; that site and its
build were removed with the hosted service.)

Nothing on this page is agent-written. The wiki itself lives in
[`../openwiki/`](../openwiki/) at the repository root, outside `docs/`.

## What produced it

| | |
|---|---|
| Generator | [OpenWiki](https://github.com/langchain-ai/openwiki) 0.4.3 |
| Mode | coding-agent integration — the host agent's own model session, recorded as `host-agent/claude` in [`../openwiki/.last-update.json`](../openwiki/.last-update.json) |
| Command | `init` |
| Repository state documented | commit `9a84fa25ce861756eec2262b754f07acfaa2790c` |
| Written | 2026-08-31 |
| Landed by | [#1597](https://github.com/aprin-labs/archimedes/pull/1597) |

The scheduled regeneration workflow
([`../.github/workflows/openwiki-update.yml`](https://github.com/aprin-labs/archimedes/blob/84c0fac4758e9572f34e4f70fe48e9d68c5acab0/.github/workflows/openwiki-update.yml),
removed 2026-11-01) **could not run** — its Bedrock path was blocked on an Anthropic
use-case form that had not been submitted for the production AWS account, and its
`schedule:` trigger was commented out for that reason. The committed wiki was not produced by
that workflow. The tooling decision, the blocker, and what the run cost are
recorded in
[`decisions/tooling-adoptions-2026-08.md`](decisions/tooling-adoptions-2026-08.md).

## What it covers, and what it therefore cannot say

The read boundary is [`../.openwikiignore`](../.openwikiignore), an **allow-list**
that excludes the whole repository and re-includes the readable slice.

**The boundary and the published pages are currently out of step, and that gap is
the most important thing on this page.** The boundary is now codebase-wide —
`backend/archimedes/` and its migrations, `analytics-engine/src` and `strategies`,
`ui/src/`, `contracts/`, `infra/`, `.github/`, `scripts/`, all of `docs/`, and the
root contract files — about 825 files and 199,000 lines, measured 2026-08-31 with
OpenWiki's own matcher. **Every page published
today predates that.** They came from the bootstrap run above, which could read
`docs/quant/` and nothing else — not `backend/`, not `contracts/`, not `ui/`, not
any other documentation tree. The codebase-wide regeneration is a separate run
that has not happened yet.

So, for the pages as they stand:

- **Every claim is grounded in a document, not in an implementation.** A wiki page
  reporting a threshold is evidence that a doc asserts that threshold — not
  evidence that the code enforces it. For any current verdict, go to the running
  system.
- **Where the wiki disagrees with the hand-written docs, the frozen spec, or the
  live system, they win.** The wiki is a navigational summary layer over
  `docs/quant/`, never an authority over it.

The rules the next run is given are in the human-authored brief
[`../openwiki/INSTRUCTIONS.md`](../openwiki/INSTRUCTIONS.md), which OpenWiki reads
and never rewrites. It is worth reading before you review a regenerated wiki: it
sets the purpose (code↔docs alignment, and making bloat, dead code, long import
chains and deep inheritance enumerable), the citation discipline (a claim about
behaviour cites the line that implements it, never the line that documents it),
the ceilings the pages must state (the test suites and the live system are both
outside the boundary), and the standing accuracy overrides — vaults are roadmap;
never quote a library pass count; the FDR helpers are disclosed, not implemented.

## What review it did and did not get

**These pages were not reviewed line by line**, and once a page is in the nav it
republishes automatically on every docs-path merge. Treat them the way you would
treat a colleague's competent summary of documents you have not read yourself:
useful for routing, not citable as the source.

What they did get is machine-checked grounding, not human sign-off. Each of the
89 persisted claims carries one or more `repo://docs/quant/...#Lx-Ly` evidence
spans, content-hashed with surrounding context so a claim goes stale when its
evidence moves, and OpenWiki refused to persist claims whose citations did not
resolve (two were rejected on this run). The spans live in one JSON sidecar per
page under `openwiki/.claims/`; those sidecars are **not** published to this site
— they are for re-verification tooling, and they are what to read if you want to
check a specific sentence against its source. The full verdict, costs, and the
seven `docs/quant/` defects the run surfaced are in
[`decisions/tooling-adoptions-2026-08.md`](decisions/tooling-adoptions-2026-08.md).

## Adding to it

Admitting a page is an explicit act: add it in a reviewed PR. (The mkdocs nav row and
the test that required one went with the docs site on 2026-11-01.)

Widening the slice means moving a path from `DENIED` to `ALLOWED` in
[`../scripts/check_openwiki_ignore.mjs`](../scripts/check_openwiki_ignore.mjs) —
every directory the boundary deliberately excludes has a case there asserting it
is denied today, so the guard goes red before the `!` line is added and the change
is visible in the diff. The rules are at the top of
[`../.openwikiignore`](../.openwikiignore), any edit to it must be re-verified with
`node scripts/check_openwiki_ignore.mjs`, and each generation wave adds a row to
the tooling-adoptions register.

## Where it is published

Only in this repository. The docs site at `docs.archimedes-arc.com` that used to serve
it was shut down with the hosted service on 2026-11-01.
