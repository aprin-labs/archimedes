# Setup

This doc walks you from a fresh clone to a working local Archimedes stack. Works on **macOS, Linux, and Windows**. Once you can run [`docker compose up -d --build`](#step-2--spin-up-the-stack-recommended-path), see the migration exit successfully, and see services pass their health checks, you're done — everything else here is optional polish.

> **Status:** Day-10 (2026-05-22); owner review 2026-07-28. Owner: Dan Browne (infra). Platform coverage is best-effort: macOS is the only routinely exercised path.

## Prerequisites

| Tool                                                                             | Purpose                                            | Required for           |
| -------------------------------------------------------------------------------- | -------------------------------------------------- | ---------------------- |
| [Git](https://git-scm.com/)                                                      | Source control                                     | Everyone               |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/)                | Local stack (Postgres + Redis + 4 services)        | Everyone               |
| [mambaforge / miniconda](https://github.com/conda-forge/miniforge)               | Python environments (for `pytest`, scripts)        | Backend / strategy dev |
| [Node.js 20+](https://nodejs.org/) (via [nvm](https://github.com/nvm-sh/nvm))    | Frontend toolchain                                 | Frontend dev           |
| [Foundry](https://book.getfoundry.sh/getting-started/installation)               | Smart contract compilation + testing               | Contract dev           |

## Recommended path: Docker is the source of truth

The local docker-compose stack runs the **same code** production runs, configured for a single user. It is not the same *topology*: production is ECS Fargate + Aurora + ElastiCache and does not run compose at all. What differs, and the one variable that decides each difference, is the contract table in [`docs/local-vs-prod.md`](docs/local-vs-prod.md). Most contributors don't need conda or Node.js for day-to-day work — the containers ship their own Python + Node. Install the host tools only when you want to:

- Run `pytest` against the live stack (conda env)
- Run the analytics-engine CLI directly (conda env)
- Develop the frontend with Vite hot-reload (Node)
- Compile + test smart contracts (Foundry)

## Step 1 — Clone the repository (with submodules)

```bash
git clone --recurse-submodules https://github.com/aprin-labs/archimedes.git
cd archimedes
```

If you already cloned without `--recurse-submodules`, populate them now:

```bash
git submodule update --init --recursive
```

The `submodules/` directory carries Circle's [`context-arc`](submodules/context-arc/) (Arc + Circle developer docs and 5 sample codebases) plus two reference projects ([`KnowledgeBase`](submodules/KnowledgeBase/) — paper-analysis pipeline; [`Linus`](submodules/Linus/) — AI orchestration project, reference only). Full Arc + Circle reference index in [`docs/arc-integration.md`](docs/arc-integration.md).

## Step 2 — Spin up the stack (recommended path)

```bash
cp .env.example .env
# REQUIRED: generate a local auth secret, then paste it after BETTER_AUTH_SECRET=
python -c "import secrets; print(secrets.token_urlsafe(48))"
# Optional: configure an LLM provider — see .env.example and docs/runbooks/operations.md
# § LLM backends. Without one, generation uses the canned fallback.
docker compose up -d --build
```

**`--build` is load-bearing, not a habit.** The app-tier services carry an `image:` key
pointing at the production ECR tag alongside their `build:`. A bare `docker compose up`
resolves that tag and pulls (or reuses another checkout's build) instead of building your
code — [issue #1044](https://github.com/aprin-labs/archimedes/issues/1044) item 3, still open.
Verify the whole local-mode contract before you start the stack:

```bash
make check-local     # or: python3 scripts/check-local-mode.py
```

Local `.env.example` selects `docker-compose.local.yml` and publishes nginx on unprivileged port 8080, so both rootful and rootless Docker work without host sysctl changes. Production omits the local override and `ARCHIMEDES_HTTP_PORT`, retaining its separate migration task and port 80.

Existing `.env` from an older checkout: merge `COMPOSE_PATH_SEPARATOR`, `COMPOSE_FILE`, `ARCHIMEDES_HTTP_PORT`, `BETTER_AUTH_URL`, and `BETTER_AUTH_TRUSTED_ORIGINS` from `.env.example`; do not replace the file or its secrets wholesale.

The stack runs one migration job, then starts **5 long-running services**:

| Service     | Host port | URL                              | What it is |
| ----------- | --------- | -------------------------------- | ---------- |
| `migrate`   | —         | (exits after success)            | Alembic schema preflight; backend and auth wait for exit 0 |
| `nginx`     | 8080      | <http://localhost:8080>          | React UI and sole reverse-proxy ingress |
| `backend`   | —         | via nginx                        | FastAPI app; intentionally not exposed directly |
| `auth`      | —         | via nginx `/api/auth/*`          | Better Auth accounts and sessions |
| `postgres`  | —         | internal Docker network          | Strategies, backtests, traces, and accounts |
| `redis`     | —         | internal Docker network          | Regime state cache and runtime scratch |

Watch health checks succeed with `docker compose ps`; inspect the completed migration with `docker compose ps -a migrate`. Funds-adjacent `agent`, `oracle`, and `kb-runner` processes stay off locally unless explicitly enabled with `COMPOSE_PROFILES=localdb,runners`.

### Fully local intelligence (ollama — no cloud credential)

The default `.env.example` points at AWS Bedrock, which needs credentials. To run the
intelligence layer entirely on your own machine, pull a model on the **host** and set three
variables in `.env` before bringing the stack up:

```bash
ollama pull llama3.1        # on the HOST, not inside a container
```

```
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
LLM_BASE_URL=http://host.docker.internal:11434
```

From inside the compose network `localhost` is the container, not your machine — hence
`host.docker.internal`. If ollama is unreachable or the model is not pulled, the backend
falls back to canned output and `/health` reports `llm_available: false`; it never claims a
live backend it does not have.

## Step 3 — Verify it works

| Open in your browser | Expect to see |
| -------------------- | ------------- |
| <http://localhost:8080>        | Live React UI (Landing / Generate / Library / Corpus / Portfolio / Reasoning / Learnings) |
| <http://localhost:8080/health> | Backend health response through nginx |
| <http://localhost:8080/docs>   | Swagger UI auto-rendered from the API contract |

**Sign up before you try to generate.** Generation, portfolio, and vault routes are behind
an authenticated session in **every** mode — there is no local bypass, and the flag that
used to provide one (`REQUIRE_SIWE_FOR_GENERATION`) was deleted. Create an account at
<http://localhost:8080> first. Nothing else is needed: `GENERATION_PAYMENT_REQUIRED`
defaults `false` locally, so no wallet and no USDC are involved. The reasoning is recorded
in [`docs/local-vs-prod.md`](docs/local-vs-prod.md) § 4.

Then check the LLM is honest about itself:

```bash
curl -s localhost:8080/health | jq '{llm_provider, llm_backend, llm_model, llm_available}'
```

If `corpus_papers` < 10000 in `/health`, the corpus seed hasn't completed yet — wait a few seconds and retry. Full corpus walkthrough in [`docs/corpus-architecture.md`](docs/corpus-architecture.md).

## Step 4 — Tear down

```bash
docker compose down                 # stop containers; keep data
docker compose down -v              # stop containers; wipe postgres volume (fresh start)
docker compose logs -f backend      # tail backend logs
docker compose logs postgres        # database logs
```

## Common dev commands (`make help`)

The repo ships a [`Makefile`](Makefile) with shortcuts for the daily workflow. Run `make`
(or `make help`) for the authoritative list — it is generated from the Makefile itself, so
it cannot drift. The ones you will reach for most:

```
make up         # migrate schema, then build + start the stack
make down       # stop the stack
make logs       # tail backend logs
make pytest     # run the backend test suite
make lint       # ruff check
make format     # ruff format
make ui-dev     # Vite dev server (ui/)
make routes     # dump the FastAPI route inventory
make check-local # verify this config is local mode (no runners, no prod secrets, no ECR pull)
make docs-check # run the docs gate locally (links + index)
make clean      # remove __pycache__ / .pytest_cache / .ruff_cache
```

Foundry (`compile`, `test`), Circle-wallet (`wallet`, `fund`, `deploy`, `rotate-secret`)
and oracle (`feed`) targets are there too — `make help` lists them with one line each.

---

## Host-tool setup (only when you need it)

### Python environment (for `pytest` + analytics CLI)

The repo ships an [`environment.yml`](environment.yml) defining all Python deps.

```bash
conda env create -f environment.yml
conda activate archimedes
```

If you prefer mamba (faster): `mamba env create -f environment.yml && mamba activate archimedes`.

Verify:

```bash
python --version    # → Python 3.12.x
uv --version        # → uv 0.x
which pytest        # → /.../envs/archimedes/bin/pytest
```

### arc-canteen CLI (every team member, individually)

[arc-canteen](https://github.com/the-canteen-dev/ARC-cli) is Canteen's hackathon CLI — it's both your per-developer RPC proxy for Arc testnet AND the telemetry surface the judging rubric reads. Each team member installs it personally.

```bash
uv tool install git+https://github.com/the-canteen-dev/ARC-cli
arc-canteen login        # GitHub device flow
arc-canteen --help
```

After login, the CLI writes credentials to `~/.arc-canteen/env`. **The token in there is a secret.** See [`docs/runbooks/operations.md` § Security notes](docs/runbooks/operations.md#security-notes) before pasting it anywhere.

To get `$RPC` available in every new shell:

```bash
echo '[ -f ~/.arc-canteen/env ] && . ~/.arc-canteen/env' >> ~/.zshrc   # or ~/.bashrc
```

Full RPC walkthrough: [`docs/runbooks/operations.md` § Understanding the RPC URL](docs/runbooks/operations.md#understanding-the-rpc-url).

### Foundry (for smart contract dev)

```bash
curl -L https://foundry.paradigm.xyz | bash
foundryup
```

Verify against Arc testnet:

```bash
source ~/.arc-canteen/env       # ensures $RPC is set
cast block-number --rpc-url $RPC
cast chain-id --rpc-url $RPC    # → 0x4CEF52 (Arc testnet, chain ID 5042002)
```

Contract sources are in [`contracts/src/`](contracts/src/). The Foundry deps
(`forge-std`, `openzeppelin-contracts`) are tracked as git submodules under
`contracts/lib/` pinned to `contracts/foundry.lock`. Restore them from a clean
checkout (skip if you cloned with `--recurse-submodules`), then build + test:

```bash
git submodule update --init --recursive   # restores contracts/lib/* deps
cd contracts && forge build && forge test
```

See [`contracts/README.md`](contracts/README.md) for the dependency layout and the
import auto-remapping.

### Frontend dev (Vite hot-reload)

The docker stack serves the built React bundle on port 80. For hot-reload during frontend development:

```bash
cd ui && npm ci && npm run dev
```

(Use `npm ci` not `npm install` — uses the locked `package-lock.json` exactly.)

---

## Running the test suite

> **The default suite is hermetic — no docker stack required.** Tests must pass with no
> `.env` and no live Postgres / Redis / RPC; that is a hard convention
> ([`docs/testing-conventions.md`](docs/testing-conventions.md)), and CI runs them exactly
> that way. Only tests marked
> `-m integration` need running services; they are excluded from the default run.
>
> Use `docker compose up -d --build` when you want the *stack* (to click through the app,
> or to run the integration marker) — not as a prerequisite for `pytest`. The `--build`
> flag matters whenever you have changed dependencies (`environment.yml`,
> `requirements.txt`, `package.json`).

The backend suite runs with a single command from the repo root — no flags, no `PYTHONPATH`, no cwd juggling. `pytest.ini` wires `pythonpath`, `testpaths`, and a verbose default.

```bash
conda activate archimedes               # one-time per shell
pytest                                  # backend unit suite, verbose, green
pytest --collect-only -q | tail -1      # how many cases there are, from the suite itself
```

Ask the suite for the case count rather than trusting a number written in a doc — a count
in prose goes stale silently.

`pytest` is configured `-v --tb=short --durations=10` — you see each test name, a short traceback on any failure, and the slowest tests. Filter as usual:

```bash
pytest -k selection_bias                # by name substring
pytest backend/tests/services/          # by directory
pytest --cov=archimedes --cov-report=term-missing  # coverage
```

The **analytics-engine** has its own suite (own `pyproject.toml`):

```bash
cd analytics-engine && uv sync && uv run pytest
```

Other suites:

```bash
cd ui && npm run lint                   # ESLint
cd contracts && forge test              # Foundry
```

### Honest coverage picture

Line coverage on the `archimedes` package: **~32% overall**, intentionally uneven — the intelligence / rigor core is well-covered because that's the defensible part:

| Area | Coverage | Notes |
| ---- | -------- | ----- |
| Selection-bias gate (DSR/PBO/walk-forward), Kelly, statistical regime | high | Önder's math — unit-tested incl. spec sanity cases |
| Strategy provider, fusion, arXiv corpus, backtest mapper/repo | 35–70% | core data path |
| `api/` (FastAPI routes) and `chain/` (web3/oracle/agent) | low | exercised by the live docker stack + integration tests, not unit-mocked (testnet/Circle-SDK bound) |

One integration test (`test_backtest_pipeline_integration.py`) needs a DB row seeded by the deploy pipeline; it runs in CI/deploy and is excluded from the default `pytest` run so a cold clone stays green (tracked for a hermetic fix).

### Lint + format

```bash
ruff format backend/archimedes      # auto-format
ruff check backend/archimedes --fix # auto-fix lint
```

---

## Platform-specific notes

### macOS

mambaforge + everything else works natively. No special setup. If on Apple Silicon, the osx-arm64 conda channels are well-supported; `psycopg2-binary`, `web3.py`, and `backtrader` all have arm64 wheels.

### Linux

Native experience. Identical to macOS for our purposes. Standard `apt`/`dnf` installs for Docker + Node if not already present.

### Windows

**Two options. WSL2 strongly recommended.**

**Option A — WSL2 (recommended):** Get a Linux experience inside Windows. Foundry, conda, Docker, and everything else "just works."

```powershell
# In PowerShell as Administrator
wsl --install
# Restart, open Ubuntu, then follow the Linux instructions above
```

WSL2 docs: [microsoft.com/wsl](https://learn.microsoft.com/en-us/windows/wsl/install).

**Option B — Native Windows:** Conda works on Windows; some pain points:

- **Foundry on native Windows** is unsupported officially. Use Git Bash + the standalone binaries from Foundry's releases, or use WSL2 just for Foundry.
- **psycopg2-binary** wheels exist for Windows but occasionally need a Visual Studio Build Tools install. If `pip install psycopg2-binary` fails, install the [Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) and retry.
- **Docker Desktop on Windows** uses WSL2 under the hood anyway, so you already need WSL2 even on Option B.

Practical take: **set up WSL2.** It removes every Windows-specific pain point and matches the macOS + Linux workflow exactly.
