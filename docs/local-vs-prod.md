# Local vs production — the deployment contract

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31
> **superseded-by:** —

Archimedes runs in two modes off **one codebase and one compose file**. The difference is
configuration, never a code fork. Nothing in the generate → backtest → rigor-gate → explore
path branches on which mode it is in, and adding such a branch is the thing this document
exists to prevent.

The exception, stated so it is not mistaken for one: `main.py` reads `PUBLIC_DOMAIN` as the
production signal in four places — the SSM secret load, the `/docs` Swagger gate and the
`EMAIL_ENCRYPTION_KEY` fail-close (both via `_is_production`), and the CORS origin list.
Those are **hardening decisions about the deployment**, not product behaviour: they change
what the process trusts, never what the product does.

- **Local mode** — single-user, self-contained. `docker compose up -d --build` and go.
  In-stack Postgres and Redis, no AWS account, no Circle keys, no chain keys. Generate →
  backtest → rigor gate → explore all work. Marketplace, on-chain settlement, and payments
  are inert.
- **Production mode** — multi-user, split across AWS. ECS Fargate backend, Aurora,
  ElastiCache, EC2 runners, deployed contracts on Arc testnet, live payment rail.

The core product must be **virtually identical** in both. Everything below is the list of
switches that separate them, and the guards that keep a local run from crossing over.

Origin: [issue #1044](https://github.com/aprin-labs/archimedes/issues/1044). The table there
was written before the Fargate cutover and before #1280 and #1300 landed; four of its rows
were wrong by the time it was picked up. The table below is the corrected one, and
`make check-local` is what stops it drifting again.

---

## 1. The contract

Every row names the **one** variable that decides it. If a dimension needs two variables to
explain it, that is a design smell, not a documentation problem.

| Dimension | Local | Production | Selector | Where it is read |
|---|---|---|---|---|
| DB + cache | in-stack `postgres` / `redis` containers | Aurora PostgreSQL + ElastiCache Redis (TLS) | `COMPOSE_PROFILES=localdb` | [`docker-compose.yml`](../docker-compose.yml) `profiles: ["localdb"]` on `postgres`, `redis`, `migrate` |
| LLM | ollama on the host | Bedrock Converse (Amazon Nova Micro) | `LLM_PROVIDER` | [`llm_backend.py`](../backend/archimedes/services/llm_backend.py) `make_llm_backend()` |
| Runners (oracle / agent / kb) | **off by default** | on, exactly-once lease | `COMPOSE_PROFILES=…,runners` | `profiles: ["runners"]` on `oracle`, `agent`, `kb-runner`; lease in [`runner_lease.py`](../backend/archimedes/services/runner_lease.py) |
| Secrets | `.env` only | AWS SSM Parameter Store | **`PUBLIC_DOMAIN`** — *not* `AWS_SSM_PATH_PREFIX` | [`main.py`](../backend/archimedes/main.py) — `if os.getenv("PUBLIC_DOMAIN"): load_ssm_secrets()` |
| Payments | dry-run / paywall off | live x402 settlement | `GENERATION_PAYMENT_REQUIRED` + `PAYMENTS_DRY_RUN` | [`generation_payment.py`](../backend/archimedes/services/generation_payment.py) `payment_required()` |
| Chain | absent (features inert) | Arc testnet, agent signer | chain keys present / absent | [`chain/`](../backend/archimedes/chain/) |
| Auth / identity | **unconditional** | **unconditional** | **no selector — see § 4** | `main.py` registers the generation, portfolio, and user routers with `dependencies=[Depends(require_current_user)]` |
| Deploy mechanism | `docker compose up -d --build` | ECS task-definition registration ([`deploy.yml`](../.github/workflows/deploy.yml)) + `docker pull` over SSM into systemd units for the runners ([`deploy-runners.yml`](../.github/workflows/deploy-runners.yml)) | — | **compose is local-only** |

### The row that keeps getting missed

**Compose is a local-development tool. Production does not run it.** `deploy.yml` registers
an ECS task definition with explicit image URIs and calls `ecs update-service`;
`deploy-runners.yml` does `docker pull` + `docker tag` + `systemctl restart` over SSM.
Neither runs `docker compose up` or `docker compose pull`, and no workflow step in the repo
does. Every stale claim that "prod deploys this compose file" traces back to the pre-Fargate
era and should be corrected on sight, not copied forward.

The practical consequence is the one in § 3: the `image:` keys on the app-tier services no
longer have a production consumer, so what they cost locally is no longer paid for by
anything.

---

## 2. Selectors that are *not* mode switches

Recording these is as load-bearing as the table, because each one has been mistaken for a
mode switch at least once:

- **`AWS_SSM_PATH_PREFIX` does not select the secrets backend.** `PUBLIC_DOMAIN` does. The
  prefix defaults blank in `.env.example`, and `load_ssm_secrets()` early-returns `0` on a
  blank prefix, but that is belt-and-suspenders — the gate that actually stops the fetch is
  the `PUBLIC_DOMAIN` check at import time in `main.py`, which runs before any service
  module reads `os.environ` for a key.
- **`DATABASE_URL` unset does not mean "local".** It means SQLite, which is for hermetic
  tests. `db.py` falls back to a `backend/`-anchored SQLite file when the variable is unset;
  local mode sets it to the in-stack Postgres, and production must always set it. A
  production deploy that forgot `DATABASE_URL` would silently start on a throwaway SQLite
  rather than failing loudly — an open item from #1044's 2026-07-08 comment, not yet built.
- **`COMPOSE_PROFILES` is not a feature flag.** It decides which *containers* exist. The
  application inside them behaves identically either way.

---

## 3. The four leaks, and where each one stands

#1044 named four ways "local" quietly touches production. Their status as of this doc's
`updated` date:

| # | Leak | Status |
|---|---|---|
| 1 | Ollama recipe broken — `OllamaBackend.available` hardcoded `True`, no `LLM_MODEL` documented | **CLOSED** by [#1280](https://github.com/aprin-labs/archimedes/pull/1280). `available` now probes `GET {base_url}/api/tags` and verifies the configured model is in the tag set; an unreachable or unpulled ollama falls back to `CannedBackend`, whose `available` is `False`, and `/health` reports `llm_available: false`. |
| 2 | `AWS_SSM_PATH_PREFIX` defaulted to `/archimedes/prod/` and `load_ssm_secrets()` ran unconditionally | **CLOSED** by #1280, in two layers: the `PUBLIC_DOMAIN` gate in `main.py`, and the blank default in `.env.example`. Guarded by `backend/tests/test_main_ssm_prod_gate.py`, which asserts no SSM client is even constructed with a prod-shaped prefix and `PUBLIC_DOMAIN` unset. |
| 3 | `image:` alongside `build:` — a bare `docker compose up` resolves to the production ECR tag | **OPEN.** Every app-tier service still carries `image: ${ECR_REGISTRY:-037613907429.dkr.ecr…}/…:${IMAGE_TAG:-latest}`, and `.env.example` defines neither variable, so the baked-in default always wins. `make check-local` fails on `no-ecr-pull` until this lands. **Until then, `--build` is load-bearing** — see § 5. |
| 4 | Identity ledger assumed prod-only | **CLARIFIED — see § 4.** |

Leak 3 has a second failure mode the issue predates, and it is the one that bites on a
machine with several worktrees: because `build:` tags the built image with whatever the
`image:` key names, a `--build` in *any* checkout overwrites that single machine-wide tag.
A later bare `up` in a different checkout then sees a changed image ID, recreates the
container, and runs the *other* worktree's code with no warning, no ECR auth, and no
network access involved.

---

## 4. Identity ledger — the clarification (#1044 item 4)

**The identity ledger is not a mode difference, and local mode has no auth bypass.**

- Vault creation is gated on a **linked wallet** in both modes —
  `vaults_routes.create_vault` depends on `require_linked_wallet`. A local single-user
  session that deploys a vault therefore exercises the same `wallet_identities` /
  `identity_events` write path production does.
- Those writes are **fail-soft by design**: `emit_identity_event` opens its own session,
  never raises, and logs failures at DEBUG. A telemetry write must be invisible to the
  request it measures. So the ledger being active locally costs nothing and breaks nothing.
- **The mental model to hold:** identity activates on the first wallet-link verification,
  local or production. It is not "on in prod, off locally".

### The decision this issue asked for

#1044 asked whether local mode wants a vault-create bypass analogous to
`REQUIRE_SIWE_FOR_GENERATION=false`. **The answer is no, and the analogue no longer exists
to copy.** `REQUIRE_SIWE_FOR_GENERATION`, its `gate_generation` dependency, and
`_generation_auth_required` were **deleted by #1300**; setting the variable today is a
no-op, recorded as such in
[`operations/feature-flag-fliplist.md`](operations/feature-flag-fliplist.md). Generation
auth is now unconditional at router-registration time.

So the contract is *simpler* than the issue assumed: **every route that is authenticated is
authenticated identically in both modes, and local mode will not get an opt-out.**
Generation, portfolio, and user routes require a session; vault-create and chat *writes*
require a linked wallet; chat reads are public by design. None of that varies by mode. The
reason to keep it that way is the whole point of having two modes off one
codebase: an auth bypass that exists only locally is a code path production never
exercises, which is exactly the class of divergence a config-driven split is meant to
avoid.

The cost is a documented one, not a hidden one: a fresh local clone must **sign up through
the Better Auth sidecar before it can generate**. That step is now in
[`SETUP.md`](../SETUP.md) and in § 5 below. Nothing else is required — no wallet, no USDC.

---

## 5. Local smoke path

Each step below is checked against `.env.example` and the route code as committed — the
values, the gates, and the auth model, not a remembered recipe. (The stack was not started
as part of writing this page; treat the flow as *specified*, and `make check-local` as the
part that is *enforced*.) `GENERATION_PAYMENT_REQUIRED` defaults `false`, which keeps
**both** the 409 wallet-link precondition and the 402 paywall in `generate_routes.py`
inert, so a local account can generate with no wallet and no funds.

```bash
git clone --recurse-submodules https://github.com/aprin-labs/archimedes.git
cd archimedes
cp .env.example .env

# REQUIRED: paste the output after BETTER_AUTH_SECRET= in .env
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Fully local intelligence — no cloud credential. Set these three in .env:
#   LLM_PROVIDER=ollama
#   LLM_MODEL=llama3.1
#   LLM_BASE_URL=http://host.docker.internal:11434
ollama pull llama3.1          # on the HOST, not in a container

python3 scripts/check-local-mode.py   # or: make check-local
docker compose up -d --build          # --build is load-bearing — see below
```

Then:

1. Open <http://localhost:8080> and **sign up**. This is not optional: generation is behind
   `require_current_user`, and there is no local bypass (§ 4).
2. Generate → backtest → rigor gate → explore.
3. Confirm the LLM is honest about itself:
   ```bash
   curl -s localhost:8080/health | jq '{llm_provider, llm_backend, llm_model, llm_available}'
   ```
   With ollama up and the model pulled you get `llm_available: true` and the ollama backend.
   With ollama down or the model unpulled you get the canned backend and
   `llm_available: false` — **loudly degraded, never silently wrong.** That honesty is the
   contract; a `true` there with nothing behind it is the bug #1280 fixed.

### Why `--build` is load-bearing

Until leak 3 closes, `docker compose up -d` **without** `--build` resolves the app-tier
`image:` keys to the production ECR tag. On an ECR-authenticated machine that pulls and
runs the last-pushed production image against your local checkout, with no indication your
changes are not running. On a machine with several worktrees it can instead run a *different
worktree's* build. `--build` makes the local Dockerfile authoritative and sidesteps both.

`make check-local` fails on `no-ecr-pull` for exactly this reason. That failure is the open
leak, not a broken checker.

---

## 6. `make check-local` — the guard

```
make check-local                      # the committed .env.example, or your .env if present
python3 scripts/check-local-mode.py --env-file .env.example
```

[`scripts/check-local-mode.py`](../scripts/check-local-mode.py) parses the compose files and
an env file and answers five questions. It runs **without a docker daemon** — it reads YAML
rather than shelling `docker compose config` — so CI and a laptop with Docker Desktop closed
can both hold it.

| Check | What it defends |
|---|---|
| `profiles` | `COMPOSE_PROFILES` selects `localdb` and does **not** select `runners`. |
| `runners-off` | No funds-adjacent runner would start. Fails both if a runner loses its `profiles: ["runners"]` gate and if the active profile set would start one anyway. |
| `no-ecr-pull` | No service carrying `build:` resolves to a remote registry with `ECR_REGISTRY` / `IMAGE_TAG` unset — the fresh-clone case. Closed by #1683: local images resolve to project-scoped `:local` tags; the ECR pull path is the explicit opt-in overlay `docker-compose.ecr.yml`. |
| `no-prod-secrets` | `PUBLIC_DOMAIN` blank (so `main.py`'s SSM gate never fires) and `AWS_SSM_PATH_PREFIX` blank. |
| `local-datastores` | With `localdb` active, `DATABASE_URL` / `REDIS_URL` address the in-stack containers, not Aurora / ElastiCache. |

The checks are exercised in both directions by
`backend/tests/test_local_mode_contract.py`: each one is fed an input that **should** fail
and asserted to reject it, then fed the corrected input and asserted to pass. A check that
has never been seen failing is not a guard. The same file carries a drift test that fails if
a selector named in § 1 loses its reader, or if the `PUBLIC_DOMAIN` gate in `main.py`
disappears.

---

## 7. Related

- [`deployment.md`](deployment.md) — the compose-topology mechanics (profile gating, the
  nginx runtime-DNS fix). Its production column describes the retired EC2 era; this document
  supersedes it on the local-vs-prod question.
- [`adr/ec2-to-ecs-fargate-cutover.md`](adr/ec2-to-ecs-fargate-cutover.md) — why production
  stopped being a compose host.
- [`SETUP.md`](../SETUP.md) — the fresh-clone walkthrough.
- [`operations/feature-flag-fliplist.md`](operations/feature-flag-fliplist.md) — every flag,
  including the retired ones, and what setting them does today.
