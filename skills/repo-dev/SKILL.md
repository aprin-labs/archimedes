---
name: repo-dev
description: Working on the Archimedes codebase — the conda env that carries both Python and Node, hermetic pytest conventions, merge-commit-only branch policy, CI quality gates, and where things live in the repo. Grounded in CLAUDE.md and the working tree; read this before writing or running code here.
triggers:
  - starting work in the archimedes repo
  - "how do I run the tests, and why does pytest pass in CI but fail locally"
  - "which branch strategy does this repo use, and how do I open a PR here"
  - "node/npm not found, in this repo's shell"
  - looking for where a piece of functionality lives in the tree
---

# Working on this codebase

This is the operational complement to `CLAUDE.md` (repo root) — read `CLAUDE.md`
for the full narrative; this skill is the load-bearing subset an agent needs
before touching code, condensed and re-verified against the working tree.

## The conda env carries Node too — a common trap

**`python`/`pytest`/`ruff` AND `node`/`npm`/the `ui/` ESLint all come from the
single `archimedes` conda env; none of them are on the base shell PATH.**
A bare `command -v node` returning nothing does **not** mean Node is missing —
it means the env isn't activated (`CLAUDE.md`, "Setup" section). For a
non-interactive shell (CI runner, or an agent's Bash tool), prepend the env's
bin directory instead of trying to `conda activate`:

```bash
export PATH="$(conda info --base)/envs/archimedes/bin:$PATH"
node --version            # v26.x ; npm 11.x
cd ui && npm run lint     # or scoped: ./node_modules/.bin/eslint src/<file>
```

`environment.yml` (repo root) is the single source of truth for the env. It is
also a file you should **ask before editing** — every team member rebuilds
their env on a change (`CLAUDE.md`, "When to ask before acting").

## Running the tests

```bash
pytest                     # from repo root, in the archimedes env — pytest.ini
                            #   sets pythonpath=backend, testpaths=backend/tests
pytest --cov=archimedes --cov-report=term-missing
cd analytics-engine && uv run pytest   # separate suite, separate toolchain (uv)
```

`pytest.ini` (repo root) explicitly excludes submodules/analytics-engine/cli/
node_modules/ui/contracts/infra/wallet-setup from collection
(`norecursedirs`, pytest.ini:14) — they have their own toolchains and would
collide (`ImportPathMismatch`) if scooped up here. `-m "not integration"` is
the exact filter the CI unit-test gate uses (needs no DB/Redis); the
`integration` marker is defined in `pytest.ini`:25.

`make pytest` (repo root `Makefile`:108-110) runs the same suite with two
Redis-dependent tests deselected for a bare-metal local run without Redis —
useful when you don't have the docker-compose stack up.

### Hermetic-test discipline (codified 2026-05-27) — read before writing any new test

"CI green ≠ local green" is itself treated as a bug here. The rules
(`docs/testing-conventions.md`, extracted from `CLAUDE.md` on 2026-08-31):

- **No `.env` dependence, no live Redis/Postgres/Anthropic/Arc RPC.** A test
  that only passes with `.env` loaded, or only fails without it, is a real bug
  to fix — never skip-mark it. The literal gate a reviewer runs:
  ```bash
  env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend \
    python -m pytest backend/tests/test_<module>.py -q
  # must end: N passed, 0 failed
  ```
- **`asyncio.get_event_loop().run_until_complete(...)` is forbidden** — Python
  3.12 raises `RuntimeError` for it outside a running loop. Use `asyncio.run(coro)`
  for a sync test calling async code, or a plain `async def test_...` (asyncio
  mode is `auto` in `pytest.ini`:8). CI check: `grep -r "asyncio.get_event_loop" backend/tests/`
  must return nothing.
- **Subprocess tests need `_clean_subprocess_env()` + `_DOTENV_NEUTRALIZE`** —
  inheriting `os.environ` leaks the developer's `.env` (e.g. a
  docker-compose-only `DATABASE_URL` hostname) into the subprocess. Reference
  pattern: `backend/tests/test_security_hardening.py`
  (`_clean_subprocess_env` at line 40, `_DOTENV_NEUTRALIZE` at line 31).
- **Mock at boundaries, not internals** — the HTTP client, DB session, Redis
  client, chain client, Circle signer; never internal dict/helper calls.
  Concrete precedents named in `CLAUDE.md`:255-273: `AgentStateStore` mock for
  Redis-down (`backend/tests/test_api_routes.py`), the SIWE signed-cookie
  helper `_siwe_cookies()` (`backend/tests/test_user_routes.py`),
  `httpx.ASGITransport` for endpoint tests (`backend/tests/test_risk_routes.py`).
- **Coverage:** per-module ≥85% line coverage is the standard for new test
  work; the repo-level `--cov-fail-under=60` gate fires only on agent (`t2o2`)
  PRs and is informational for everything else (`CLAUDE.md`:281-285).
- **No skip-marks on flaky tests.** Flakiness is almost always a missing
  boundary mock or hidden environment state — fix it. A skip-mark should be
  rare and load-bearing (a documented architectural limitation), not a way to
  avoid diagnosing (`CLAUDE.md`:286-290).

## Linting + formatting (ruff)

`ruff.toml` (repo root): `line-length = 120`, `target-version = "py312"`, ruff
defaults plus `I,UP,B,SIM,RUF,ARG,C4,PIE,RET,SLF` (`[lint] extend-select`). Two
checks block every Python PR (`quality-gate.yml` per `CLAUDE.md`'s CI table);
broader `ruff check` and `npm run lint` in `ui/` are informational only
(`continue-on-error`, posted as a PR comment table):

```bash
ruff format --check .                            # hard block
ruff check --select E9,F63,F7,F40,F82 .          # hard block (syntax + undefined-name only, deliberately narrow)
ruff check .                                      # informational (broader rule set)
```

Local cleanup before committing:

```bash
ruff check --select I --fix .      # import organization (safe, mechanical)
ruff check --fix .                 # other safe auto-fixes
ruff format .                      # apply formatting
```

`pip install pre-commit && pre-commit install` wires the same checks
(`.pre-commit-config.yaml`, repo root — its ruff hook args are commented as
"MIRRORS the CI ruff-gate blocking subset") plus `detect-secrets` (secret
scanning against `.secrets.baseline`) and basic YAML/JSON/whitespace hygiene.
Pre-commit is **opt-in**; without it you get the same feedback from CI on push.

## Merge-commit-only — the branch model

(`CLAUDE.md`:140-163, "Branch model")

- **`main` is the only long-lived branch, and it is the deploy branch.** Every
  merge triggers a CI build + deploy. There is no `develop` branch.
- **Merge commits only.** Squash-merge and rebase-merge are **disabled in repo
  settings** — use `gh pr merge <n> --merge`, not the squash/rebase buttons.
  Rationale stated in `CLAUDE.md`:150-154: merge commits keep
  `git log --merges` / `git log --graph` showing unit-of-work boundaries, and
  rebase-merge breaks `git branch --merged` (rewritten commits are no longer
  ancestors of `main`).
- **Never force-push `main`.** Force-pushing your own unmerged feature branch
  is fine and expected (rebase-before-merge, `CLAUDE.md`:155-157 — now folded
  into the "few hard rules" bullet rather than stated standalone).
- **One logical change per PR; atomic commits; imperative-mood commit
  messages** ("Add X" not "Added X"), optional scope tags
  (`[strategy]`, `[backtest]`, `[contracts]`, `[frontend]`, `[infra]`, `[docs]`)
  — `CLAUDE.md`:161-162.
- **Branch naming:** `<discord-handle>/<short-name>` → PR → merge to `main`;
  delete the branch after merge (`CLAUDE.md`:149 — one line covers both today).
- **Contract/on-chain changes need the contract owner's review** — currently
  Dan, with a second reviewer preferred; see `CLAUDE.md`'s "Team" section for
  who that is at any given time. Don't merge a Solidity change on your own
  judgment alone.

## CI quality gates (what actually blocks a PR)

Full table + exact commands in `CLAUDE.md`'s "CI / quality gates" section.
Headline: `quality-gate.yml` hard-blocks on `pytest -m "not integration"` +
the ruff-gate subset above; `complexity-gate.yml` is **informational only,
never blocks merge**; `deploy.yml` fires on push to `main` (build → ECR →
Fargate roll); `main-format-guard.yml` self-heals unformatted pushes to `main`;
`release-tag.yml` tags merged PRs by title marker (`!minor`/`!version-release`/
none — default to none when unsure).

**A docs-link gate exists now (`docs-gate.yml`), but it does not cover
`skills/`.** `.github/workflows/docs-gate.yml` runs
`.github/scripts/docs_links.py` (link resolution, blocking-when-triggered) +
`docs_index.py` (index completeness) + a staleness check (informational) —
but its default scan scope (`markdown_files()` in `docs_links.py`, `--scope
gate`) is `docs/**/*.md` plus **root-level** `*.md` only; `skills/*/SKILL.md`
is neither. The workflow's own `on.pull_request.paths` trigger (`docs/**`,
`*.md`, the two `docs_*.py`/workflow files) also won't fire on a PR that
touches only `skills/`. It's also path-filtered and therefore must not be a
required check (boxed warning at the top of the workflow) — see
`CLAUDE.md`'s CI table. Don't assume `skills/` content is covered by it; if
the scope is widened later, re-check before assuming these files are covered.

## Where things live (top-level map)

Full annotated tree in `docs/reference/file-tree.md` (the README's old
"Repository structure" section was retired 2026-08-20); the parts most
relevant to backend/API work:

```
backend/archimedes/
├── api/            ← FastAPI routers (generate_routes.py, strategies_routes.py, …)
├── agents/          ← generation pipeline, debate engine
├── chain/           ← on-chain integration + oracle runner
├── interfaces/       ← frozen Protocol classes
├── models/           ← SQLAlchemy ORM (StrategyPassportRecord, StrategyRecord, …)
├── services/         ← business logic (rigor_evaluator, model_gate, generation_quota, …)
└── marketplace/       ← x402/Circle Gateway copy-trading money seam

analytics-engine/    ← backtrader-based backtest engine (own uv project, own pytest)
contracts/           ← Solidity, Foundry layout
ui/                  ← React 19 + Vite 8 + viem
scripts/              ← operational scripts, incl. agent_journey.py (reference account-auth + generate client)
cli/                  ← archimedes CLI — 0.0.1 stub, every subcommand exits NOT_IMPLEMENTED (see skills/verdict-api)
docs/                 ← specs, ADRs, design docs; docs/doc-index.md is its own map
```

## Verify (re-run these before trusting this document)

```bash
# Env carries both Python and Node:
grep -n "nodejs" environment.yml

# Hermetic gate command still matches CLAUDE.md:
grep -n "PYTHONPATH=backend" CLAUDE.md

# Merge-commit-only is still the repo policy:
grep -n "Merge commits only" CLAUDE.md

# A docs-link gate exists but its scope + trigger both exclude skills/:
grep -n "paths:" -A5 .github/workflows/docs-gate.yml
python3 .github/scripts/docs_links.py --root . 2>&1 | grep -i "skills/"; echo "(empty output above = skills/ not in the gate's default scan scope)"

# CLI is still an unimplemented stub:
grep -n "NOT_IMPLEMENTED" cli/src/archimedes_cli/cli.py
```

## What this skill deliberately does not cover

- API-level detail for the generate/verdict flow — see `skills/verdict-api/SKILL.md`.
- Passport field semantics — see `skills/strategy-passport/SKILL.md`.
- The marketplace payment protocol — see `skills/x402-payment/SKILL.md`.
- AWS/infra operations (SSM, Terraform, deploy) — see the AWS paragraph in
  `CLAUDE.md`'s "Setup" section and `docs/runbooks/operations.md` (the doc
  formerly at root-level `OPERATIONS.md`); out of scope for a dev-workflow skill.
