# AGENTS.md

Two different kinds of agent read this repo — point each at its own doc rather than
duplicating content here.

- **Working on this codebase** (an AI coding agent contributing code, tests, or infra to
  Archimedes itself): start at [`CLAUDE.md`](CLAUDE.md) — the engineering rules, review
  gates, and agent discipline for this repo. It deliberately holds only what you would get
  wrong by default; for everything else it points at
  [`docs/doc-index.md`](docs/doc-index.md), the doc register — architecture in
  [`docs/architecture.md`](docs/architecture.md), decisions in [`docs/adr/`](docs/adr/README.md),
  team and ownership in [`docs/team.md`](docs/team.md), and how to file a new doc in
  [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md).
- **Using the deployed product** (an autonomous AI agent driving Archimedes as a user — an
  investor, a trading bot, a research assistant): start at
  [`docs/agent-api.md`](docs/agent-api.md) for the full programmatic API contract, or
  [`/llms.txt`](https://archimedes-arc.com/llms.txt) on the live site for a curated,
  low-token entry point. Both cover same Better Auth browser-free journey:
  read → authenticate account → generate → read rigor verdict; wallet proof stays optional.

A machine-readable manifest is also live at
[`/api/agent/manifest`](https://archimedes-arc.com/api/agent/manifest) and
[`/.well-known/agent.json`](https://archimedes-arc.com/.well-known/agent.json).

<!-- OPENWIKI:START -->

## OpenWiki

This repository has a generated `openwiki/` evidence index. It is optional just-in-time
context, not required startup reading.

**Know its boundary before you trust it, and know that the boundary and the pages are
currently out of step.** `.openwikiignore` is an **allow-list**. It is now **codebase-wide**
— `backend/archimedes/`, `analytics-engine/src` + `strategies`, `ui/src/`, `contracts/`,
`infra/`, `.github/`, `scripts/`, all of `docs/`, and the root contracts; ~825 files and
~199k lines as measured on 2026-08-31. **The committed pages predate that widening.** They came from the 2026-08-31
bootstrap run against `docs/quant/` alone, so every page you can read today is grounded in
*documentation*, not implementation: a claim there records what a doc asserts, never what
the code enforces. Treat source code and tests as authoritative — a page's unknowns and
conflicts are verification gaps, not automatic requirements. The codebase-wide regeneration
is a separate run and has not happened yet.

Start at [`openwiki/quickstart.md`](openwiki/quickstart.md). Before quoting a threshold, a
pass/fail, or a library size from any page, read
[`openwiki/rigor/documented-conflicts.md`](openwiki/rigor/documented-conflicts.md) — the
bootstrap slice contradicts itself in seven places, all since reconciled in `docs/quant/`.

Do not hand-edit generated pages unless explicitly asked; fix the source and let OpenWiki
regenerate. [`openwiki/INSTRUCTIONS.md`](openwiki/INSTRUCTIONS.md) is the one user-authored
file OpenWiki reads and never rewrites — read it before running or reviewing a wiki
generation. It sets what the wiki is *for*: code↔docs alignment, and making bloat, dead
code, long import chains, and deep inheritance enumerable rather than adjectival.

The GitHub Actions workflow is `workflow_dispatch`-only and **cannot run today** — Bedrock's
Anthropic models are not enabled on the AWS account. The committed wiki was generated
through OpenWiki's coding-agent integration, which needs no provider credentials. Both the
blocker and the cost of the run are recorded in
[`docs/decisions/tooling-adoptions-2026-08.md`](docs/decisions/tooling-adoptions-2026-08.md);
add a row there for each generation wave. Any edit to `.openwikiignore` must be re-verified
with `node scripts/check_openwiki_ignore.mjs`.

Pages publish to **`docs.archimedes-arc.com`**, served from our own infrastructure rather
than GitHub Pages ([#1634](https://github.com/aprin-labs/archimedes/issues/1634)); the
`mkdocs build --strict` wiring from #1624 is the build step and stays.

<!-- OPENWIKI:END -->
