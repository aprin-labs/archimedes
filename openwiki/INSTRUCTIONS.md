# OpenWiki brief — Archimedes

User-authored. OpenWiki reads this for scope and priorities and never rewrites it. It is the
one file in `openwiki/` that a person owns; everything else under this directory is
generated output and is replaced wholesale by the next run.

---

## What this wiki is for

**Keeping the code and the documentation honest about each other, and making the system's
complexity countable.**

Not a tour. Not an onboarding guide. Not a prose restatement of the docs — we already have
`docs/`, and a second, agent-written copy of it is worse than none, because it decays on its
own schedule and nobody owns it. The wiki earns its place by doing two things a person
cannot do cheaply by hand across ~200,000 lines:

1. **Read the implementation and the documentation together, and record every place they
   disagree.** A doc-only wiki can find that two documents contradict each other. It cannot
   find that both are wrong. The bootstrap run (2026-08-31, `docs/quant/` only) surfaced
   seven contradictions and resolved none of them; three — the DSR bar's provenance, the two
   values of `num_trials`, and a misattributed out-of-sample Sharpe — could not be settled
   from documentation at all, and an eighth defect, a stale `N_eff` formula that every doc
   in the slice reproduced *consistently*, was invisible to it by construction. That failure
   mode is why the boundary now includes the code.

2. **Make bloat, dead code, long import chains, deep inheritance, and duplication visible
   and enumerable** — as ranked tables with file:line anchors, not as adjectives. The
   maintainer's doctrine is **flatter, more functional, clearer**: fewer layers, fewer
   abstractions with one implementation, fewer objects that exist to hold a method, shorter
   paths from an entry point to the thing that does the work. The wiki does not perform that
   simplification. It produces the list that makes it decidable, ordered by what it would
   actually buy.

A page that serves neither of those two purposes should not be written.

---

## Scope: the whole system, not one document tree

`.openwikiignore` is an allow-list and it is now codebase-wide. Read its Section A header
before assuming anything about the boundary; it is the authority, this section is the
summary.

**In:** `backend/archimedes/` (services, api, chain, models, agents, marketplace,
interfaces) and `backend/migrations/` · `analytics-engine/src/` and
`analytics-engine/strategies/` · `ui/src/` · `contracts/` · `infra/` · `.github/` ·
`scripts/` · all of `docs/` · the root contracts (`README.md`, `SETUP.md`, `CLAUDE.md`,
`AGENTS.md`, `mkdocs.yml`, `pytest.ini`, `ruff.toml`, `environment.yml`, the compose files,
`Makefile`, `backend/requirements*.txt`, `backend/Dockerfile`).

**Out, and this is a ceiling you must state rather than work around:** `backend/tests/`,
`ui/test/`, `analytics-engine/tests/`, `auth/`, `cli/`, `wallet-setup/`, `company-site/`,
`nginx/`, `reports/`, `skills/`, root `tests/`, `submodules/`, `data/`, `docs/archive/`,
generated ABIs and Solidity, build output, bulk data, and every secret spelling. Each
exclusion has a case in `scripts/check_openwiki_ignore.mjs` asserting it is denied today.

Measured at the boundary with OpenWiki's own matcher on **2026-08-31**: **825 files,
199,396 lines, 10.0 MB** — against 8 files, 2,006 lines and 113 KB for the bootstrap slice,
so roughly 90× by volume. Re-measure rather than trusting that figure; the walk is a dozen
lines against `OpenWikiIgnore.load()` and the tree moves daily. Budget accordingly, and see
*Operational obligations* below on working in waves.

### What the excluded test suites mean for what you may claim

**The test suites are outside the boundary. Never assert that behaviour is tested, covered,
guarded, or verified.** You cannot see the tests. You may say that a function validates an
input, because the validating line is in front of you; you may not say the validation is
enforced in CI. Where a claim would benefit from test evidence, write the gap as an explicit
unknown — "no test evidence available inside this boundary" — and move on. That sentence is
useful. A guess dressed as coverage is not.

---

## Rule 1 — every claim is grounded in code

**Cite `repo://path#Lx-Ly` for every substantive claim, and name the file and line in the
prose itself** so a reader on the rendered site can follow it without opening the claims
sidecar. OpenWiki refuses to persist a claim whose citation does not resolve; treat that
refusal as a finding about your reading, not an obstacle.

Four rules that decide *which* line to cite:

- **A claim about behaviour cites the line that implements it, never the line that documents
  it.** `docs/quant/admission-criteria.md` stating a threshold is evidence about that
  document. The threshold's value is whatever `backend/archimedes/services/` computes.
- **A code comment is documentation that happens to live in a `.py` file.** It carries no
  more authority than a Markdown file and is subject to the same cross-check. A comment
  that contradicts the code beneath it is a conflict, and one of the more valuable kinds —
  record it.
- **A name is not evidence.** `verify_*`, `_validate`, `strict`, `secure`, `dry_run` — read
  the body. This repository's defect history is almost entirely functions that were trusted
  for what they were called (`AGENT_DRY_RUN=1` silently meant LIVE; a rigor badge that read
  a cached boolean; a leaderboard serving fixture columns as measured statistics).
- **A default is not the deployed value.** A flag's default in `feature_flags.py` and the
  value the production task actually runs under are different facts. Cite the default as a
  default, and look for the override in `infra/ecs.tf`, the compose files, or
  `.github/workflows/` before saying what production does. Where you cannot establish the
  live value from the tree, say so — the live system is the authority and it is not in the
  boundary.

If a claim cannot be cited, it does not go on the page as prose. It goes on the page as a
named unknown, or it does not go on the page.

---

## Rule 2 — cross-check the docs against the code, and record every contradiction

For each subsystem you write about, read what `docs/` says about it and compare, line
against line. The comparison is the deliverable, not a side effect.

**Every code↔docs contradiction goes on the conflicts page** —
`alignment/conflicts.md`, generalising the bootstrap run's `rigor/documented-conflicts.md`,
which is the model for tone and structure and should be read before writing the new one.
Each entry carries, in this order:

| Field | Content |
|---|---|
| **The disagreement** | What the doc says, cited. What the code does, cited. One sentence each. |
| **Which is right** | The code, unless a ratified ADR or a frozen spec in `docs/specs/` says otherwise. Say which, and cite it. |
| **Why it matters** | The wrong decision a reader would make holding the doc's version. If there isn't one, it is a typo, not a conflict — leave it out. |
| **The fix** | Which file changes, and to what. One line. You propose; you never edit `docs/`. |

Resolution order when sources disagree, and it is not negotiable:
**the live system > the code > a ratified ADR (`docs/adr/`) or frozen spec (`docs/specs/`) >
a newer doc > an older doc.** The live system is outside the boundary, so where a question
turns on it, the honest answer is "unresolvable from the repository — check `/health`, `GET
/api/config/contracts`, or the deploy history," and that answer belongs on the page.

Three shapes worth hunting specifically, because all three have bitten this repo:

- **A doc that is internally consistent and uniformly wrong.** Every page reproduces the
  same formula, the same threshold, the same count — and the code does something else.
  Consistency across docs is not corroboration; it is one error copied.
- **A retired name still in the docs.** A superseded pipeline, a replaced registry, a
  renamed flag. Check whether the symbol still exists in the tree before calling it dead.
- **Present-tense copy for roadmap surfaces.** See the standing overrides below.

---

## Rule 3 — make the complexity enumerable

This is the half the bootstrap run did not do at all, and it is the half the maintainer
asked for. **Enumerable means: a ranked table, with counts, with file:line anchors, that
someone could work down.** "The generation pipeline is complex" is not a finding. "Nine
modules exceed 800 lines; `agents/generation_pipeline.py` is the largest at N lines with M
top-level definitions" is.

Measure and rank at least these, each on the complexity pages:

| Signal | What to report |
|---|---|
| **Module size** | Longest files by line count, with the top-level definition count for each. Size alone is not a defect; size with many unrelated responsibilities is. |
| **Import chains** | The longest chain from an entry point (`backend/archimedes/main.py`, `ui/src/main.jsx`) to a leaf that does the work. Name every hop. Modules with the highest fan-in — the ones a change ripples from. Any import cycle, which is a defect on sight. |
| **Inheritance depth** | Classes more than two levels from `object`. Base classes, `ABC`s, `Protocol`s and interfaces in `interfaces/` with **exactly one** implementation — an abstraction with one implementation is a layer, not a design. Name the single implementer. |
| **Pass-through layers** | Route → service → repository hops where a layer only forwards its arguments. Cite the forwarding line; that is the whole evidence. |
| **Duplication** | Two or more implementations of the same computation in different modules, and constants (thresholds, addresses, chain ids, model ids) written down in more than one place. For each duplicate, name which copy the production path reads. |
| **Apparently unused definitions** | Symbols with zero callers **inside the boundary** — always with that qualifier attached, in the same sentence. Tests, `scripts/`, and `cli/` are outside; zero callers here is a lead, not a verdict. The bootstrap run got this exactly right about the Benjamini–Hochberg helpers; keep that standard. |
| **Permanently-one-valued branches** | Flags, parameters, and conditionals that only ever take one value on the reachable paths in the boundary. Say which value, and where the other branch would come from. |

Then a **reduction-candidates page**: the above, re-sorted by what the change would buy,
each entry saying what exists, what it would become, what depends on it, and what would have
to be re-verified. Bias every recommendation toward *flatter and more functional*: collapse
the layer, delete the one-implementation abstraction, replace the object holding a single
method with the function, lift the branch to the caller, delete rather than deprecate.

Two things this page must never do: rank by a number without saying what the number means,
and recommend a change whose blast radius you have not tried to establish. If you cannot
tell what depends on something, that is the finding — write it as one.

---

## The pages to write

A contract, so a run does not wander. Adjust the leaves to what the code actually shows;
keep the three sections.

- `quickstart.md` — routing map. Which page answers which question, what the wiki does not
  cover, and the claims never to make. First page a reader hits; keep it under a screen.
- **`system/`** — one page per subsystem, each grounded in implementation:
  request path (app → routers → services), the rigor gate and the analytics-engine math
  behind it, generation, persistence (models + migrations), chain and contracts, the
  frontend, deployment (`infra/` + `.github/workflows/`). Each page opens with **what it
  lets you decide** and closes with **what it could not establish**.
- **`alignment/conflicts.md`** — Rule 2's output. If a run finds no contradictions, say so
  in one line and say what you compared; do not pad it.
- **`complexity/`** — Rule 3's output: bloat and duplication, the import graph, inheritance
  and abstraction, and the ranked reduction candidates.

---

## The output bar

**Write for the maintainer who has to govern this system, not for a visitor.** The test for
every page: would someone deciding whether to merge, delete, or rewrite something open this
page, and be better off for it? If the honest answer is no, the page is padding.

- **Open with the decision the page supports.** Not with a definition of the subsystem.
- **State the ceiling on every page.** What this page could not verify, and why —
  the test suites are outside the boundary, the live system is outside the boundary, a value
  is set at deploy time. An unstated ceiling is the defect; a stated one is a service.
- **Short and accurate beats long and complete-looking.** A filled-in section with nothing
  in it costs a reader more than a missing section.
- **No pass counts. Anywhere.** Never state how many strategies pass the rigor gate. Three
  strategies once reported as passing were grading equity-like series through a data-feed
  fallback; the corrected count is **unestablished**, and "unestablished" is the word to
  use. This is `CLAUDE.md`'s hard rule and it outranks anything a doc or a comment in the
  tree says.
- **Banned as unearned:** "comprehensive", "robust", "seamless", "fully", "production-grade",
  "battle-tested", "statistically proven". If a property is real, cite the line that makes
  it real; if it is not, the adjective is the whole claim.
- **Numbers get a provenance and a vintage.** Where a figure came from, and as of when. A
  measured result with no date is a rumour.
- **Never soften a finding to be diplomatic.** If a layer does nothing, say it does nothing.
  The maintainer is the audience and the audience wants the list.

---

## Standing accuracy overrides

These outrank anything you read in the tree, including code comments.

- **Vaults and on-chain execution are ROADMAP, not shipped product.** The
  `Vault`/`VaultFactory` contracts are real and deployed, and the deploy-a-vault journey is
  gated off every public surface behind `ROADMAP_SURFACES_ENABLED`
  (`ui/src/featureFlags.js`, off by default). Write it in the future tense. Never claim
  vaults are live, non-custodial in production, or executing capital.
- **Never state a rigor-gate pass count.** See above. Not a number, not a range, not "a
  handful".
- **`num_trials = 1` on the curated path** means DSR runs undeflated there. Do not describe
  the curated path as multiple-testing corrected. The generated path differs — cite the
  line, do not generalise from one to the other.
- **Board-level selection bias is disclosed, not corrected.** Do not present the
  Benjamini–Hochberg / FDR helpers as implemented until you can cite a non-test caller
  inside the boundary.
- **Say "deflated-Sharpe evidence at the 95% one-sided level"** — real evidence on a short
  return history, never "statistically proven". Quote the bar from
  `rigor_profiles.DSR_P_BADGE_MIN`, which is the only place it is written down (#1794);
  a second copy in a wiki page is how the two-bars defect spread in the first place.
- **Claims must be true** is this repository's first rule. A guarantee the wiki repeats
  must be backed by the live path, not by a fixture, a cached boolean, or a hard-coded
  `true`. When you find one that is not, that is a conflicts-page entry and one of the more
  valuable things a run can produce.

---

## Operational obligations

Things a run breaks if it does not know them.

1. **Every page needs a hand-added nav row in `mkdocs.yml`.** The `Agent-generated wiki`
   section is maintained by hand on purpose, and `backend/tests/test_docs_site.py::
   test_every_openwiki_page_is_in_the_nav` fails the build when an `openwiki/**.md` file has
   no entry. A run that adds pages without nav rows lands red. Add the rows in the same
   change, under the existing section, and nowhere else — a second test asserts only
   `openwiki/` pages live there.
2. **Links must resolve.** `mkdocs build --strict` fails on a link into `docs/` or
   `openwiki/` naming a file that does not exist. Run `python -m mkdocs build --strict`
   before handing the run over.
3. **Work in waves, and record the cost of each.** The slice is roughly 90× the bootstrap by
   volume. Take it in passes — system pages first (they are what the other two sections cite
   into), then conflicts, then complexity — and add a row per wave to
   [`../docs/decisions/tooling-adoptions-2026-08.md`](../docs/decisions/tooling-adoptions-2026-08.md)
   with wall clock, what was read, and what
   came out, including a negative verdict if that is the honest one. "Installed is not a
   resting state" is the standard; an unmeasured run is the thing it exists to prevent.
4. **Do not hand-edit generated pages**, and do not edit `docs/`, code, or
   `.openwikiignore` from inside a wiki run. Findings are output; fixes are someone else's
   PR. The one exception already in the tree — the resolved-banner block on
   `rigor/documented-conflicts.md` — is annotated as a human edit precisely because it is
   an exception.
5. **`.openwikiignore` changes are guarded.** Any edit requires
   `node scripts/check_openwiki_ignore.mjs` to pass, and a widening requires moving the new
   slice's path from `DENIED` to `ALLOWED` in that script — watch it go red first. Ordering
   is last-match-wins, so a deny placed above the allow-list is silently undone by it.

---

## Where these pages get published

**The wiki is served from `docs.archimedes-arc.com`, on our own infrastructure — not
GitHub Pages.**

The build already exists and stays: `mkdocs.yml` mounts this tree via
`.github/scripts/mkdocs_hooks.py`, and `.github/workflows/docs-site.yml` runs
`mkdocs build --strict` on every docs-path push and PR. That wiring, landed by
[#1624](https://github.com/aprin-labs/archimedes/pull/1624), becomes the **build step** and is
not being replaced.

What changes is the **serving**. The GitHub Pages deploy job — gated on
`vars.DOCS_SITE_ENABLED` behind three manual console steps, and dark since 2026-08-20 —
is replaced by S3 + CloudFront + Route 53 in our own account, on the pattern already applied
for `aprin.ai` in `company-site/infra/main.tf`. That is separate infrastructure work,
tracked as [#1634](https://github.com/aprin-labs/archimedes/issues/1634), which also adds the
docs link to the landing footer (`ui/src/components/Landing.jsx`) and the public header
(`ui/src/components/PublicLayout.jsx`).

Why it matters for how you write: **these pages are a published site with a public URL, not
a folder of notes.** A reader arrives from a search engine on a leaf page, with none of the
context of the page above it. Every page carries a rendered banner saying it was written by
an agent and not line-reviewed. Write so that banner is a courtesy rather than a warning.
