# Archimedes — Claude Code Context

> Read at the start of every Claude Code session in this repo. Revision history is
> `git log --follow CLAUDE.md`.
>
> **This file holds only what an agent would get wrong by default.** Anything readable from
> the tree in one tool call — stack, contract census, test count, ruff rules, service
> inventory — is deliberately *not* here: a stale copy is worse than none, because agents
> act on `CLAUDE.md` without verifying.
>
> **Where things live:** [`docs/doc-index.md`](docs/doc-index.md) — the doc register (a doc not
> listed there does not exist) · [`docs/user-stories.md`](docs/user-stories.md) — canonical
> product spine · [`docs/architecture.md`](docs/architecture.md) — architecture map ·
> [`docs/adr/`](docs/adr/README.md) — the decision records. Current status comes from
> the live system, not a doc: `/health`, `GET /api/config/contracts`, and the
> deploy history. (The README's dated Status section was retired 2026-08-20.)

## Project

**Archimedes** — an agentic strategy generation and validation system ("portfolio
strategy, under scrutiny"): a single-user agent that turns q-fin research literature into
rigor-gated strategies behind an honest validation layer, then runs the survivors as
paper deployments. **No product analogies on public surfaces** — "X-for-quant-finance"
comparison branding (the retired Linus analogy and anything shaped like it) is banned
(Dan, 2026-09-01) in the README, docs, UI, manifests, and the published CLI/MCP package
pages; competitive comps live in the private docs repo only. Guarded by
[`backend/tests/test_public_branding_guard.py`](backend/tests/test_public_branding_guard.py). **Executing and monitoring
them in non-custodial vaults on Arc with USDC settlement is roadmap, not shipped product —
write it in the future tense.** The `Vault`/`VaultFactory` contracts are real and deployed
([ADR](docs/adr/non-custodial-vault-owner-agent.md)), but the deploy-a-vault journey is
gated off every public surface behind `ROADMAP_SURFACES_ENABLED`
([`ui/src/featureFlags.js`](ui/src/featureFlags.js), off by default, #1266/#1354, guarded by
`ui/test/roadmap-copy.test.js`), and #1469 is open to scrub the remaining present-tense
copy. Spine (generate → rigor-gate → execute → monitor → explore) locked in
[`docs/user-stories.md`](docs/user-stories.md).
Repo [`aprin-labs/archimedes`](https://github.com/aprin-labs/archimedes) · Discord **Archimedes
Arcadia** · live at [`archimedes-arc.com`](https://archimedes-arc.com/) (Arc testnet, chain
`5042002` / `0x4cef52`; `.com` is the sole domain — the `.app` split caused the Circle
passkey rpId bug and was decommissioned) · [Unlicense](docs/adr/unlicense-public-domain.md).
The build roadmap lives **privately** in the `docs` repo (`consolidated/ROADMAP.md`) —
**Dan owns it**, ask him for access. Competitive landscape, pricing, and business model are
**not in this repo** either — that material lives in the private docs repo by policy; this
repo carries code and technical documentation only. Ask Dan. (Do not re-create a competitor
doc here — that has happened twice.)

### The hard constraint, above everything else

***Claims must be true.*** Every guarantee the UI, the pitch, or a grant application makes
— rigor, non-custodial, on-chain provenance — must be backed by the live path, not a
fixture, not a cached boolean, not a hard-coded `true`. This is the #1 rule and the thing
Bogdan's full-tree audit ([PR #710](https://github.com/aprin-labs/archimedes/pull/710)) showed
we were violating. Building flashy work on a fake-strict rigor badge is building on sand.

Two corollaries an agent gets wrong by default:

- **Don't quote a curated-library strategy pass count — anywhere.** Three strategies
  reported "passing" were later found to be grading equity-like series (~18.5% annual vol)
  through a data-feed fallback in the backtest loader. The corrected count is **not
  established**. Say "unestablished", not a number.
- **Numbers come from the live source, not a doc.** Contract census: `GET
  /api/config/contracts`. Test count: `pytest --collect-only -q | tail -1`. Lint rules:
  [`ruff.toml`](ruff.toml). Status: the live system (`/health`, the contract census),
  not a doc — the README's dated Status section was retired 2026-08-20.

## Team

Roster, bios, timezones, sync window, and the 2026-06-24 ownership change (Chuan Bai out;
Dan owns contracts + on-chain + infra + architecture and is the sole required
contract approver): [`docs/team.md`](docs/team.md). Two things stay here because they change how you
behave:

> **Lanes are descriptive of strengths, not prescriptive of boundaries.** The table below
> describes where each teammate has the deepest context, not who is *allowed* to work on
> what. Everyone is a full-stack contributor; we all routinely work across lanes when the
> situation calls for it. The point of marking lanes is to know whose review to seek and
> who carries the longest memory on a given subsystem — not to gate who can drive work
> forward. **This applies equally to AI agents working on our behalf:** an issue assigned
> to you is yours to execute, regardless of whose lane it nominally sits in.

"Lead" = deepest context and default reviewer; "Coverage" = who can step in. **Neither
column is a permission gate. Do not refuse or close a task because it sits outside your
nominal lane.** Flag the cross-lane review need in the PR description instead.

| Component | Lead | Coverage |
| --- | --- | --- |
| Strategy engine + q-fin paper corpus curation | Dan | Önder |
| Backtesting / strategy-passport math + risk pricing | Önder | Dan |
| Backend Python layer (FastAPI, API, services, models) | Daniel R. | Marten |
| On-chain integration layer (`backend/archimedes/chain/`, oracle runner) | Dan | Bogdan / Marten |
| Frontend (React + Vite + viem, wallet UX, trade tab) | Marten (current) / Daniel R. | Dan |
| Smart contracts (Arc, Foundry) | Dan | Bogdan (`mnemonik-dev`) / Marten |
| Infra / ECS Fargate / CI/CD / docker-compose / AWS account | Dan | Daniel R. |
| Architecture + design decisions | Dan (lead) | full team |
| Pitch deck + demo script + judging | Dan | Marten |

## Setup

[`SETUP.md`](SETUP.md) has the full walkthrough (conda env via
[`environment.yml`](environment.yml), Node frontend, Foundry, docker compose, the test
suite). What is not obvious from the tree:

**Local stack (post-#1194):** `cp .env.example .env` (generate `BETTER_AUTH_SECRET`;
LLM/RPC optional) then `docker compose up -d --build`. nginx publishes on rootless-safe
port 8080 (<http://localhost:8080>, `/docs` for the API); compose runs Alembic before
starting auth/backend. Full walkthrough: `SETUP.md`.

**Resolve the toolchain explicitly. Do not trust a bare `pytest` / `ruff` / `node`.**
`environment.yml` declares the whole toolchain — `python`, `pytest`, `ruff`, `nodejs`,
`terraform`, `awscli` — but a bare command name gives you whatever PATH resolves first, and
**which one that is, is a property of the box, not of the repo.** Re-measured 2026-08-31 on
one maintainer's box with `command -v <cmd>` / `which -a <cmd>` and `<cmd> --version`:

| command | resolves to | the env has |
| --- | --- | --- |
| `pytest` | the env's — `.../envs/archimedes/bin/pytest`, **pytest 9.0.3** on Python 3.12.13, fastapi 0.141.1 | same binary |
| `ruff` | the env's — **ruff 0.15.13** | same binary |
| `node` / `npm` | the env's — **node v26.2.0 / npm 11.13.0** (Homebrew ships no `node` here) | installed |
| `terraform` | **Homebrew's `/opt/homebrew/bin/terraform` — Terraform v1.15.8**, which *shadows* the env's | v1.15.3 |
| `python` | the env's — **3.12.13** (`python3` is Homebrew's, and is not the env) | 3.12.13 |
| `tofu` | not installed | not declared |

Two things changed since the 2026-08-30 measurement, and both are worth knowing: the
Python 3.13 framework install that used to win `pytest` is **gone** from this box, and
**`terraform` is now installed** (it was previously absent, with OpenTofu `tofu` standing
in — `tofu` is now absent instead). Do not carry either old fact forward.

**The structural rule survives every re-measurement:** `/opt/homebrew/bin` precedes the env
on this box's PATH, so any tool Homebrew *also* provides wins — today that is `terraform`
and `python3`. The resolution can flip in either direction on any box, at any time, without
touching this repo.

**The dangerous one is `pytest`.** It does not fail when it resolves wrong. It collects and
runs the suite against a different interpreter and an unaligned package set, then reports a
result you will believe. That is the same "works on my machine" defect the
`environment.yml` / `backend/requirements.txt` alignment guard exists to catch, arriving
through PATH instead of through version floors.

`nodejs` / `terraform` / `awscli` were added to `environment.yml` on 2026-05-24 and
2026-06-24. An env created before then and only ever pip-updated will not have them.
`conda env update -f environment.yml` is how you would get them, and it will shadow — or be
shadowed by — whatever Homebrew already provides. Worth knowing before you run it; to pick
up a pip-block change only, install those specs directly.

Check rather than assume, then use absolute paths:

```bash
ENV_BIN=/path/to/miniconda/envs/archimedes/bin   # `conda env list` prints the prefix
"$ENV_BIN/python" -V                             # expect 3.12.x
"$ENV_BIN/pytest" --version
command -v node && node --version                # may be Homebrew's; that is fine for lint
```

`conda info --base` is **not** a reliable way to build that path. conda installs itself as
a shell *function*, so in a non-interactive shell (CI, or an agent's Bash tool) it fails
with `__conda_exe: permission denied`, and the surrounding `export PATH="$(...)/..."`
silently produces a garbage prefix that falls through to whatever was already on PATH —
the command then appears to work, for the wrong reason. Call the real binary at
`<conda-root>/bin/conda`, or hard-code the prefix.

For `ui/` lint, Node only has to resolve for the ESLint shebang, so Homebrew's is fine:

```bash
cd ui && npm run lint     # or scoped: ./node_modules/.bin/eslint src/<file>
```

**Tests:** from the repo root, with the env's `pytest` (see above — a bare `pytest` may
not be it), just `pytest` — `pytest.ini` sets `pythonpath`/`testpaths` and a verbose
default. Ask the suite for the case count rather than trusting a doc: `pytest --collect-only -q | tail -1`. Coverage: `pytest
--cov=archimedes --cov-report=term-missing`. The analytics-engine runs its own suite:
`cd analytics-engine && uv run pytest`.

**AWS:** prod is Dan's account (`037613907429` / `us-east-1`); ask **Dan** for a scoped IAM
user (`SecurityAudit` + `ViewOnlyAccess`, MFA). **Credentials go over a secure channel —
1Password / Bitwarden / Signal — never Discord, never email.** The web tier is Fargate:
no host to SSH into (`aws ecs execute-command` for a task shell); SSM reaches only the
residual EC2 box running the oracle / agent / kb runners.

**Submodules** ([`docs/submodules.md`](docs/submodules.md) — also the canonical pointer for
any Arc/Circle integration question, via `submodules/context-arc/`). Do this once per
clone; without it a fresh clone silently drifts off `main`'s recorded pins:

```bash
git config submodule.recurse true && git config diff.submodule log
git submodule update --init --recursive   # --recursive: Linus has a nested submodule
```

## Stack — only the parts you would guess wrong

Live picture: [`docs/architecture.md`](docs/architecture.md). Deployed contract census: `GET
/api/config/contracts`. Four deltas a reasonable guess gets wrong:

- **Backtesting is [backtrader](https://github.com/mementum/backtrader), not vectorbt.** Archived `design.md` § 6 says "vectorbt / custom numpy engine" — superseded per [ADR](docs/adr/backtrader-backtest-engine.md). vectorbt is a v2 problem, only if parameter-sweep speed becomes a constraint.
- **`StrategyRegistry` and `AssetRegistry` both exist and both are live.** `ecosystem-design-spec.md` describes the former as *replaced by* the latter; they coexist and serve different lookups. The spec-vs-state delta is intentional — don't "fix" it by deleting one.
- **Contract ABIs are cached in [`contracts/abis/`](contracts/abis/)** for backend + UI. Read from there; don't re-derive from Foundry output at runtime.
- **Frontend is React 19 + Vite 8 + UnoCSS + viem** — not Next.js, not Tailwind. LLM is **`bedrock_converse` / `amazon.nova-micro-v1:0`** (BYOK + local-Ollama paths preserved); `response.model` is the provenance of record across the GLM→Bedrock migration. Datastore **Aurora PostgreSQL 18.3** + ElastiCache Redis; deploy **ECS Fargate** behind ALB/WAF ([ADR](docs/adr/ec2-to-ecs-fargate-cutover.md)).

## Engineering conventions

### Branch model (build-on-deploy, main-only)

- **`main` is the only long-lived branch, and it is the deploy branch.** Every merge to
  `main` builds and deploys to the live Fargate stack. No `develop`/integration branch — it
  drifted unused and was retired ([ADR](docs/adr/build-on-deploy-main-only.md)).
- **`main` moves continuously.** Dan and teammates drive the build through parallel Claude
  Code sessions and merge on their own authority, so `main` can take dozens of commits in a
  day. Branch late, rebase right before merging, merge in a tight window. Don't wait for
  `main` to "settle" — it won't.
- Short-lived per-owner branches `<discord-handle>/<short-name>` → PR → merge; delete after.
- **Merge commits only.** Squash- and rebase-merge are disabled in repo settings; use `gh pr
  merge <n> --merge`. Why: merge commits preserve branch topology, so `git log --merges` /
  `--graph` show unit-of-work boundaries. Rebase-merge confuses `git branch --merged`
  (rewritten commits aren't ancestors of `main`) and loses the "this was a single PR" signal
  needed for post-hoc forensics.
- **Close the issue from the PR body with a real keyword.** `Closes #123` / `Fixes #123` /
  `Resolves #123`, the keyword immediately before each `#`, repeated once per issue —
  `Closes #1 and #2` closes only #1. Without one the work merges and the issue stays open,
  which is how a pile of already-fixed-but-still-open issues accumulates that nobody goes
  back to drain. Referencing without closing is correct for partial work; write `Part of
  #123` so the non-closing reference reads as deliberate.
  [`.github/pull_request_template.md`](.github/pull_request_template.md) prompts for it.
- **The few hard rules — universal, and they do not impede speed:** never force-push
  `main`; never commit secrets or `.env`; one logical change per PR. Force-pushing your
  *own* unmerged feature branch is fine and expected (rebase-before-merge).
- **One approving review** is enough for non-contract changes. Contract changes warrant
  **Dan's approval (contract/infra owner — the sole required approver)**, with a second
  review from **Bogdan (`mnemonik-dev`) when he is active** (as of 2026-08 he is not),
  given live-funds risk.
- Commits: imperative mood, atomic, one logical change. Optional scope tags `[strategy]`,
  `[backtest]`, `[contracts]`, `[frontend]`, `[infra]`, `[docs]`.

### CI / quality gates

Definitions live in [`.github/workflows/`](.github/workflows/); what matters here is **what
blocks and what does not**:

| Workflow | Blocks? | |
| --- | --- | --- |
| `quality-gate.yml` | **YES** | `pytest -m "not integration"` (unit suite, no DB/Redis) **and** `ruff-gate`. Everything else it reports — full `ruff check`, `ui/` lint — is `continue-on-error`, posted as a PR comment. A ≥60% coverage gate is wired to PRs whose author is `t2o2`; that account is dormant (see § Spec-driven execution), so the gate currently never fires. |
| `complexity-gate.yml` | no | Complexity/nesting table as a PR comment. **Informational only — never blocks.** Don't restructure code to satisfy it. |
| `main-format-guard.yml` | n/a | On push to `main`, if `ruff format --check` fails it reformats, commits back with `[skip ci]`, and **fails its own run** so the violation stays visible. `main` self-heals, so open PRs aren't stranded with red ruff-gates. |
| `import-guard.yml` | **YES** | Runs on every PR. Catches imports that resolve locally but not in a clean environment. |
| `contracts-test.yml` | no | `forge build` + `forge test`. **Path-filtered to `contracts/**`, so it must not be made a required check** until an always-runs fallback job reports the same check name — see the boxed comment in `scripts/setup-branch-protection.sh`. |
| `docs-gate.yml` | no | Link resolution and index completeness across `docs/` and root markdown; staleness reported but never blocking. **Path-filtered — same constraint as `contracts-test.yml`, and the same warning is boxed at the top of the workflow.** Run it locally with `make docs-check`. |
| `infra-gate.yml` | no | `terraform fmt -check -recursive` + `init -backend=false` + `validate`, as a matrix over the three Terraform roots (`infra/`, `company-site/infra/`, `docs-site/infra/`). No credentials, no `plan`, never reads S3 state. **Path-filtered — same constraint as `contracts-test.yml`/`docs-gate.yml`, same boxed warning at the top of the workflow.** Note that quality-gate's "Infra — user-data size guard" row is a byte-count on `infra/user-data.sh` and parses no `.tf` file; this is the row that does. |
| `deploy.yml` · `release-tag.yml` | n/a | Build → ECR → roll Fargate (superseded runs auto-cancel); semver tag per merged PR. |
| `deploy-runners.yml` | n/a | `workflow_dispatch` only — the `push` trigger is deliberately commented out. Oracle + agent EC2 and the KB scheduled Fargate task. |

**Release tags — two surprises.** (1) Bump markers are read from the PR title with an
**end-of-title anchor**: `!version-release` → major, `!minor` → minor, anything else →
patch. Title-end matching prevents false positives when the marker text appears in body
prose. (2) **Direct pushes to `main` with no associated PR are silently skipped — no tag is
created.** Prefer a PR for anything you'd want to find in `git log` later, including agentic
work. `!minor` = new user-facing capability; `!version-release` = major milestone, used
sparingly; when in doubt, no marker.

### Before you approve a merge — the green check may not mean what you think

Applies to every reviewer, every session, and **every review subagent**. Rules 1–4 come
from defects that shipped past a full row of green checks in a single week; rule 5 from a
merge train three months later. The common shape: **a signal was trusted for something it
does not actually measure.**

**1. A stale PR's green check describes a base that no longer exists.**
GitHub freezes a PR's test-merge ref at its last push. A PR far behind `main` was tested
against a base that may lack validators, columns, or behaviour that exist today — and
"re-run failed jobs" replays the same frozen snapshot, so it can never pick up the fix.
→ **If a PR is materially behind `main`, re-verify against current `main` before merging,
whatever the checkmark says.** Push to the branch (or merge `main` in) to regenerate the
ref, then read the *new* result.
*Cost of learning it:* #1155 failed for exactly this reason; days later #1099 merged green
and broke `main`, because its tests predated a `vault_address` validator.

**2. Verify the merged result, not each PR.**
Every PR being green does not make their union green. Semantic conflicts are textually
clean and CI-invisible.
→ **After merging anything non-trivial, re-run the suite on the merged `main`** — with the
*exact* command CI uses, from the repo root. A local `pytest backend/tests` collects a
different set than `pytest -m "not integration"` from the root, and the gap is where this
hides.

**3. A test that passes against the unfixed code proves nothing.**
The specific trap: **passing the same literal to both sides of the boundary the bug lives
on.** A Redis-key casing test that lower-cased the input before handing it to the reader
made fixed and unfixed code produce identical keys — it passed either way and guarded
nothing.
→ **Before pushing a regression test, revert the fix and confirm the test fails.** If it
passes both ways, it is not a regression guard; say so rather than counting it as coverage.

**4. A guard must be shown to reject something.**
Guards are where this repo's defects cluster: one checked presence but not value
(`AGENT_DRY_RUN=1` silently meant LIVE), one measured the wrong compression state, one
claimed "no network" and "installed package import path" while enforcing neither.
→ **Build the input that *should* fail the guard, run it, confirm it fails — before
pushing** — and put that demonstration in the PR body. This applies to claims made in
**prose** too: a PR description asserting a property the code does not enforce is the same
defect, just harder to grep for.

**5. In a merge train, rule 2 runs *between* merges, not once at the end.**
A train lands PRs seconds apart. Union-testing only the final `main` finds the break after
every intermediate `main` has already been red — and every open PR's test-merge ref
regenerates against a broken base, so the whole board goes red for a reason none of them
caused.
→ **Before the train, pairwise-intersect the members' changed-file lists (`gh pr diff
<n> --name-only`) and union-test every pair that shares a file** — merge the pair locally and
run the CI command. A shared *function* is the dangerous case: both branches are green,
neither diff touches the other's lines, and the collision is invisible until they execute
together. Corollary for test doubles: **a boundary mock must stub the shared function's
full surface, not only the calls the branch under test makes** — a double that covers your
own path silently drops a sibling's.
*Cost of learning it:* 2026-08-31 — #1562's ownership-stamp tests mocked only the Redis
methods `save_trace` used on that branch, while #1403 added `sadd`/`srem`/`hsetnx`/`hdel`
index maintenance to the same `save_trace`. Both green alone; merged, the double's
non-awaitable `srem` failed 5 tests on `main` and poisoned every open PR's merge-ref until
the fix-forward (#1565) landed.

### Testing conventions

Full rules — hermetic gate command, the forbidden `asyncio.get_event_loop()` pattern, the
subprocess-env recipe, boundary-mock precedents, coverage targets, no-skip-marks — moved to
[`docs/testing-conventions.md`](docs/testing-conventions.md) (2026-08-31). **Read it before
writing any new test.** The rule that stays here because agents get it wrong by default:

- **Tests must be hermetic, and `CI green ≠ local green` is itself a bug.** No `.env`
  dependence, no live Redis / Postgres / Anthropic / Arc RPC. A test that passes in CI but
  fails locally (or the reverse) is a real defect to fix, never a flaky test to skip-mark.
- The repo-level `--cov-fail-under=60` gate is conditioned on `t2o2` being the PR author and
  is therefore **dormant** — nothing enforces repo-level coverage today (see § Spec-driven
  execution).

### Linting, formatting, dependencies

Ruff config is [`ruff.toml`](ruff.toml) — read it rather than trusting a rule list here.
The blocking subset in `ruff-gate` is deliberately narrow (formatting + syntax/undefined
names) so the gate doesn't trip on pre-existing style debt; the broader `ruff check .` is
informational. `pip install pre-commit && pre-commit install` mirrors the CI gate exactly,
so pre-commit can't pass while CI fails. `--unsafe-fixes` needs line-by-line human review
and is never auto-applied.

Dependency hygiene — `pip-audit` / `cd ui && npm audit --omit=dev` (prod-only view; drop
the flag to also see dev-tooling CVEs) / `cd wallet-setup && npm audit` (a second, separate
npm project — runtime deps only, no `--omit=dev` needed) before any dep bump;
triage Dependabot promptly. Three rules:

- **Pin transitively-vulnerable deps directly when CVEs warrant it** — e.g. `starlette>=1.0.1` is pinned in both `environment.yml` and `backend/requirements.txt` to close PYSEC-2026-161 even though it would otherwise arrive transitively via FastAPI. Pin to the closest `Fix Versions` so a fresh resolution can't regress.
- **Keep `environment.yml` (local dev) and `backend/requirements.txt` (Docker / CI) aligned.** Drift is the most common source of "works on my machine" + "breaks in CI" — the `slowapi`/`redis` misalignment caused 62 `user_routes` test errors locally on 2026-05-24. Since #1522 the shared floors have **one home, [`backend/requirements-base.txt`](backend/requirements-base.txt)**, which both files pull in with `-r`: a new pip dep needed by backend runtime code goes there **once**, not in two places. Only `torch` / `sentence-transformers` are still written down twice (`environment.yml` cannot include the file carrying the CPU-wheel `--extra-index-url` without losing MPS on Apple Silicon), and [`backend/tests/test_env_requirements_alignment.py`](backend/tests/test_env_requirements_alignment.py) fails the build both on drift between those two and on any *new* duplicate.
- **No new dep without a sentence on what it does and why**, as a comment in the requirements/env file. "added by tooling" is not a sentence. Frontend: always `npm ci`, never `npm install` — `npm ci` verifies `package-lock.json` integrity and won't mutate the lockfile.

### Smoke-test before deploy; don't connect important wallets

Don't push to shared infrastructure without smoke-testing locally first; contract changes
go against Arc testnet first. Use a fresh dev wallet, never one with real assets, and never
paste private keys in this repo — `.env.example` is the template, `.env` is gitignored.

### Security ships with the product, not after

Every visitor to the live site gets the **same** security posture. For a Claude session:
**don't propose deferring security work** for cost or scope reasons, and **don't accept "we
don't have real users yet"** as justification for skipping a security fix. Surface
cost-vs-security tension and lean toward keeping security live; the human calls it (Dan's
stance, 2026-05-28 — he will cover the cost personally). Security-relevant PRs (auth,
secrets, permissions, vault contracts, anything PII-adjacent) get more careful review than
feature work even when the diff looks small.

## When to ask before acting

- Pushing to shared infrastructure
- Adding a top-level dependency (state which package, why, and the license)
- Touching `docker-compose*.yml`, deployment configs, or CI/CD wiring without team alignment
- Any smart contract change (needs **Dan's approval as contract owner — the sole required
  approver; Bogdan is the preferred second reviewer when active**) — contracts hold live
  funds and Dan deploys them himself
- Editing `.env.example` or [`environment.yml`](environment.yml) — both are env contracts
  everyone else rebuilds against
- Anything touching the strategy-passport / reasoning-trace data flow, or the on-chain vault
  contracts (`Vault`, `VaultFactory`, `SyntheticVault`, `ReasoningTraceRegistry` — all live)
- Anything under `~/.arc-canteen/` (individual team-member credentials)
- **Never** reintroduce the `/api/papers/corpus/*` endpoints deleted in issue #201. Any
  knowledge-graph surface must come from real KB pipeline output, never arxiv-metadata
  synthesis — see [`docs/submodules.md`](docs/submodules.md).

## When NOT to ask

- Inside your own feature branch, editing your own files
- Writing tests
- Adding docstrings, type hints, or formatting fixes
- Updating `docs/` to keep specs in sync with shipped code
- Running `pytest`, `ruff`, `prettier --write`, `docker compose up --build` locally

## Working with AI agents on this repo

Most of this team works through AI agents. Read this before dispatching agents or feeding
work to the issue pipeline. The mechanics live in
[`docs/agent-operations.md`](docs/agent-operations.md); two recurring non-repo-specific
traps — character limits (`wc -m`, not `wc -c`) and zsh quoting — are in
[`docs/agent-gotchas.md`](docs/agent-gotchas.md).

### Spec-driven execution (highest-leverage workflow)

Mechanics moved to [`docs/agent-operations.md`](docs/agent-operations.md) § Spec-driven
execution (2026-08-31): the acceptance/anti-goal/precedent checklist, the **pre-close
verification gate**, and the verify-your-own-audit-claims rule. Issue skeleton:
[`docs/prompts/agentic-issue-skeleton.md`](docs/prompts/agentic-issue-skeleton.md). Three
things stay here because a session acts on them without looking anything up:

- **A well-specified issue is executable work.** Humans plan and spec; the session executes;
  humans review the PR. Vague issues produce vague code — spec quality is the throughput
  lever, and acceptance criteria must be machine-checkable (the exact command *and* its
  exact expected output), never prose like "make it robust."
- **A claimed issue is authorized. Do not close on lane grounds.** Execute it regardless of
  whose nominal lane it touches. If you genuinely cannot (missing context, ambiguous spec,
  blocking dependency), comment with the reason and leave the issue **open** for a human to
  triage — do *not* close it.
- **`t2o2` is not an active resource** (as of 2026-08-03; Chuan Bai stepped back
  2026-06-24). Do not assign issues to it, do not plan around it, and do not infer
  availability from older documents. This is what makes the coverage gate in the CI table
  dormant. Today's executor is a Claude Code session run by a human teammate.

### Git safety — every contributor and their agents

Non-negotiable, and load-bearing because the judges read this repo like operators:

- **Never force-push `main`.** Ever. (Force-pushing your own unmerged feature
  branch is fine.)
- Humans: branch + PR → merge to `main`. One logical change per PR; atomic commits.
  The agentic system integrates on `main` directly (build-on-deploy) — that's the
  accepted reality, not a violation; the rule that matters is no force-push + no
  secrets, not "never touch `main`."
- `main` moves continuously — rebase onto it right before merge and merge fast.
- **Never commit secrets or `.env`** — `.env` is gitignored; keep it that way; no
  private keys in the tree. `.gitignore` covers `*.pem`, `*.key`, `*.p12`, `*.pfx`,
  `*.crt`, `*.tfstate*` globally as of 2026-05-27. The
  [`detect-secrets`](https://github.com/Yelp/detect-secrets) pre-commit hook is
  wired in `.pre-commit-config.yaml` with the (currently unaudited) baseline at
  `.secrets.baseline` — regenerating and auditing it is outstanding (all 28 entries are
  `is_verified: false`, dated 2026-05-27). Install with `pip install pre-commit && pre-commit install`
  so commits are scanned locally before push.
- **Rotation alone does not undo a leak.** When a credential is committed and
  later removed, the value remains in `git log -p` forever and on every clone
  anyone has done. Rotation makes the leaked credential *useless going forward*
  (the threat is neutralized) — it does not erase the historical artifact.
  Plan accordingly: don't put a secret in code thinking rotation makes the leak
  fully reversible. The SSH deploy key that lived briefly in
  `infra/archimedes-deploy-key.pem` was rotated 2026-05-26 and the old key
  revoked on the EC2; the leaked bytes are still in git history but useless.
- **Terraform state belongs in S3, never local-committed.** S3 backend: bucket
  versioned + encrypted (SSE-S3) + bucket-policied to deny non-TLS access +
  restrict to the account principal; S3-native locking (`use_lockfile = true`)
  obviates the DynamoDB lock table. See [`infra/README.md`](infra/README.md) for
  bootstrap commands. State can contain real secrets (e.g., the `tls_private_key`
  resource puts a private key into state) — treat the backend as a secrets store
  and scope IAM read access accordingly.
- If an AI agent is uncertain, it **stops and asks** — it does not invent APIs,
  fabricate data, or silently work around a blocker.
- New to the stack: pair on one full branch → push → PR cycle before running
  agents unsupervised. No judgement — the cost of a tangled shared history is
  high; the cost of one paired cycle is low.

### Parallel agent fan-out discipline

Full discipline — worktree isolation, the as-you-go worktree/branch cleanup that one session
paid for with 14 stale dirs and ~24 branches, and structured subagent response formats — is
in [`docs/agent-operations.md`](docs/agent-operations.md) § Parallel agent fan-out
(2026-08-31). The two rules that are cheap to state and expensive to skip:

- **Probe with ONE canary agent before any fan-out.** If the canary is blocked at a step,
  the whole fan-out will be too — you pay the fan-out tax for zero parallelism.
- **The canary must match the fan-out's execution mode.** A foreground canary does *not*
  validate a background fan-out; they run under different sandboxes. **Background subagents
  are filesystem-sandboxed here** — use foreground agents for implementation fan-out.

### Agent-as-proxy authorization

When a teammate is unresponsive >24h and work in their lane is blocked, their agent is
authorized to proxy backend code reviews and merges in that lane. Conditions, the audit-note
convention, and the revert-on-disagreement rule:
[`docs/agent-operations.md`](docs/agent-operations.md) § Agent-as-proxy (2026-08-31). **The
two exceptions never proxy:** Solidity contract changes need **Dan's** explicit approval as
contract owner, and architecture decisions or infrastructure cost commitments (new AWS
services, recurring spend, multi-day migrations) need **Dan's** ack — he owns the account.
Operational fixes inside an already-approved architecture are fine to proxy.

## Architectural decisions — index, not argument

**Decisions live in [`docs/adr/`](docs/adr/README.md) and the ADR is authoritative** — status, date, owner, alternatives, consequences. Do not relitigate an
`Accepted` ADR in a spec, a comment, or a PR description; open a superseding ADR instead.

- **Product shape** — [two-tier marketplace](docs/adr/two-tier-marketplace.md) · [non-custodial vault, owner ≠ agent](docs/adr/non-custodial-vault-owner-agent.md) · [Arc settlement](docs/adr/arc-settlement-chain.md)
- **Rigor gate** — [unified gate](docs/adr/rigor-gate-unification.md) · [`num_trials` self-containment](docs/adr/num-trials-self-containment.md) · [backtrader](docs/adr/backtrader-backtest-engine.md) · [selection-bias spec](docs/specs/selection-bias-corrections-spec.md)
- **Generation** — [debate society is the sole pipeline](docs/adr/debate-society-sole-generation-pipeline.md) (supersedes fusion/architect routing) · [K=1 + external rigor gate](docs/adr/k1-generation-external-rigor-gate.md) · [Bedrock](docs/adr/glm-to-bedrock-llm-migration.md)
- **Provenance** — [passport](docs/specs/strategy-passport-spec.md) · [commit-reveal](docs/specs/commit-reveal-trace-spec.md) · [Xia protocols](docs/specs/xia-2026-protocols.md) · [IPFS pinning is not live](docs/adr/ipfs-pinning-not-live.md)
- **Infra** — [Fargate cutover](docs/adr/ec2-to-ecs-fargate-cutover.md) · [Aurora + Alembic](docs/adr/aurora-postgres-alembic-datastore.md) · [build-on-deploy](docs/adr/build-on-deploy-main-only.md) · [AWS account migration](docs/adr/aws-account-migration.md)
- **Interfaces, principles, risks** — [frozen interfaces](docs/specs/component-interfaces-spec.md) · [principles](docs/architectural-principles.md) · [risk matrix](docs/architecture.md)

### Fail-soft is correct for optional configuration and wrong for anything a claim depends on

For credentials and for measured values, the correct degraded state is a loud, visible
absence — a `NOT_RUN`, an em-dash, a startup abort, a CloudWatch alarm — never a plausible
substitute. A fail-soft default converts an outage into a silence, and silence is
indistinguishable from working. The fix is not "always crash": a function that loads secrets
should know which parameters are load-bearing and be loud about *those* while genuinely
optional ones stay quiet. The rigor gate already gets this right with its four-state
`pass`/`fail`/`pending`/`degenerate`; three other subsystems did not — SSM credentials booted degraded by
design and marketplace publish never worked in production for 19 days with no alarm; the
leaderboard fell back to migrated fixture columns and presented fabricated statistics as
measured on the flagship public page; and a persisted return series bound to the wrong asset
had the top-ranked strategy graded on the null benchmark's returns. Long form:
[`docs/architectural-principles.md`](docs/architectural-principles.md) § fail-soft.


## Documentation conventions

Full rules: [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md). Before you write a doc:

- **Where it goes, first match wins:** closed-off decision → `docs/adr/` · frozen
  interface/schema → `docs/specs/` · procedure run under pressure → `docs/runbooks/` ·
  reusable prompt → `docs/prompts/` · point-in-time investigation →
  `docs/audits|benchmarks|cost-estimates|analysis/` · session context → `docs/handovers/` ·
  how a live subsystem works today → `docs/` root · superseded history → `docs/archive/`.
- **Name it** `lower-kebab-case.md`, stable slug, no dates in filenames except
  point-in-time artifacts. **Front matter:** `status` / `owner` / `updated` /
  `superseded-by`. ADRs add `Supersedes` / `Superseded-by` — set both ends of the chain in
  one commit; never delete or silently rewrite an ADR.
- **Add a row to [`docs/doc-index.md`](docs/doc-index.md) in the same commit** — a doc not in the
  index does not exist.
- **60-day rule:** a `current` doc older than 60 days is presumed stale — re-verify, demote,
  or archive. Anything that decays faster belongs in the live source, not a doc.

---

_Disagree with something here? Discuss in Discord, agree, and update the file — don't let
it silently drift. Anything that decays does not belong in this file; put it where readers
expect decay._

<!-- OPENWIKI:START -->

## OpenWiki

See [AGENTS.md](AGENTS.md) for OpenWiki agent instructions.

<!-- OPENWIKI:END -->
