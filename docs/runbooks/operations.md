# Operations

Running, configuring, and operating the Archimedes stack — local and production. For first-time setup, read [`SETUP.md`](../../SETUP.md) first.

> **Status:** Day-10 (2026-05-22); re-verified 2026-07-28. Owner: Dan Browne.

## Contents

1. [The 6-service stack](#the-6-service-stack)
2. [Configuration via `.env`](#configuration-via-env)
3. [LLM backends (4 options)](#llm-backends-4-options)
4. [Connecting to Arc testnet](#connecting-to-arc-testnet)
5. [Understanding the RPC URL](#understanding-the-rpc-url)
6. [Local↔prod parity](#localprod-parity)
7. [Reporting traction (the 30% rubric weight)](#reporting-traction-the-30-rubric-weight)
8. [Security notes](#security-notes)

## The 6-service stack

`docker compose up -d --build` brings up 6 services (see [`SETUP.md`](../../SETUP.md#step-2--spin-up-the-stack-recommended-path) for the table). Beyond what's listed there:

- **Backend** is wired to start the autonomous agent loop, oracle price feeder, statistical regime detector, Kelly/risk-parity portfolio constructor, and the four-control selection-bias gate as in-process services. The `agent` and `oracle` standalone containers run their own loops independently.
- **Postgres** persists strategies, backtests, reasoning traces, the q-fin paper corpus (live count: `GET /health` `corpus_papers` / `corpus_db_count`), and vault metadata. Volume `pgdata` survives `docker compose down`.
- **Redis** holds live regime classifications, agent heartbeat, trace index, and the job queue. Volatile by design — fine to lose on restart.
- **`archimedes-corpus-artifact`** named volume is mounted but currently empty — reserved for the future KB-pipeline artifact build (per [`docs/corpus-architecture.md`](../corpus-architecture.md)).

## Configuration via `.env`

Open `.env` in your editor (created by `cp .env.example .env`) and fill in the values that are blank.

| Variable                     | Where to get it                                       | Required for             |
| ---------------------------- | ----------------------------------------------------- | ------------------------ |
| **LLM** — one of: `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`, *or* `ANTHROPIC_API_KEY` | GLM token via the **Canteen submission** (judges), or your own key at <https://console.anthropic.com> | **Real strategy intelligence** — blank = silent canned fallback |
| `RPC`                        | `~/.arc-canteen/env` (after `arc-canteen login`)      | On-chain features (contracts, trace anchoring) |
| `DEV_WALLET_ADDRESS` / `_PRIVATE_KEY` | `cast wallet new` (generate a fresh dev wallet) | Submitting signed transactions to Arc |
| `*_ADDRESS`                  | Auto-populated after contracts are deployed           | On-chain features        |

You can leave these blank to start — the API will boot, you can hit the routes, and the UI will render. On-chain features just won't work until you populate them.

> **🔑 The one secret that actually matters.** Everything except the LLM credential is optional for the core demo. With **no** LLM credential the stack boots and routes work, but strategy fusion / architect / passport extraction return **canned** output (not real intelligence) — and it does so silently. Set exactly one path in your gitignored `.env`:
>
> - **(A)** `ANTHROPIC_AUTH_TOKEN=<token>` + `ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic` — this project runs on GLM; the token is delivered to **judges privately via the Canteen submission** and is **never** committed to this public repo; or
> - **(B)** your own free `ANTHROPIC_API_KEY` from <https://console.anthropic.com>.
>
> The exact block (with comments) is in `.env.example`.

## LLM backends (4 options, same code)

The `LLM_*` env vars configure the LLM provider. All four use the same code paths via [`backend/archimedes/services/llm_backend.py`](../../backend/archimedes/services/llm_backend.py).

| Provider | Config | Use case |
|----------|--------|----------|
| **GLM via z.ai** (production) | `LLM_PROVIDER=anthropic_compatible` + `LLM_AUTH_TOKEN` + `LLM_BASE_URL` | Default for deployed stack; also works locally |
| **Anthropic direct** | `LLM_PROVIDER=anthropic` + `LLM_API_KEY` | BYOK; free tier works for dev |
| **OpenAI** | `LLM_PROVIDER=openai` + `LLM_API_KEY` | Alternative provider for benchmarking |
| **Ollama local** | `LLM_PROVIDER=ollama` + `LLM_BASE_URL=http://localhost:11434` | Fully offline; no API key needed |

Back-compat: `ANTHROPIC_*` env vars still work (deprecated, WARN logged on startup).

## Connecting to Arc testnet

To make on-chain calls (read contract state, send signed transactions) you need `$RPC` set — the per-developer RPC URL from `arc-canteen`. Three ways to use it:

```bash
# One-off: source the env then call any Eth RPC method
source ~/.arc-canteen/env       # exports $RPC
cast chain-id --rpc-url $RPC    # → 0x4cef52 (Arc testnet)
cast block-number --rpc-url $RPC

# Via the canteen CLI directly
arc-canteen rpc eth_chainId

# In docker — paste the URL into .env as RPC, then restart
docker compose up -d
```

Full Arc testnet reference (chain ID, USDC address, faucet, etc.) is in [`docs/arc-integration.md`](../arc-integration.md).

## Understanding the RPC URL

The single most-used piece of infrastructure on this project. Worth a minute of orientation, especially for anyone newer to EVM tooling.

### What you get from `arc-canteen login`

A URL of the form:

```
https://rpc.testnet.arc-node.thecanteenapp.com/v1/swrm_<64-hex>
```

Three pieces glued together:

1. **`rpc.testnet.arc-node.thecanteenapp.com`** — Canteen's JSON-RPC **proxy** for Arc testnet (not the Arc node itself; a proxy in front of it).
2. **`/v1/`** — proxy API version.
3. **`swrm_<64-hex>`** — your **per-user server token**, embedded in the URL path. Each teammate has a different one.

### What it does

The Arc chain is EVM-compatible, so it speaks the standard Ethereum JSON-RPC protocol — the same HTTP API that geth, Infura, Alchemy etc. expose. The proxy lets you make calls like:

| Method                       | Effect                                                | Read or write |
| ---------------------------- | ----------------------------------------------------- | ------------- |
| `eth_chainId`                | Returns `0x4cef52` for Arc testnet                    | Read          |
| `eth_blockNumber`            | Current head block number                             | Read          |
| `eth_getBalance`             | USDC/native-token balance of an address               | Read          |
| `eth_call`                   | Simulate a contract function call (no state change)   | Read          |
| `eth_getLogs`                | Query event logs by topic / address / block range     | Read          |
| `eth_sendRawTransaction`     | Submit a **pre-signed** transaction to be mined       | Write         |

The proxy enforces a **method allowlist** — most reads plus `eth_sendRawTransaction`. Non-allowlisted methods return `method '<x>' not allowed by the proxy`.

### What the token is NOT

The `swrm_*` token is **not a wallet private key.** It can't sign transactions on its own. The signing flow is:

1. You hold a wallet private key (a separate 64-hex string, generated by `cast wallet new` or Circle's wallet service — **never** stored in `~/.arc-canteen/`).
2. Your code uses the private key to sign a transaction *locally* (`web3.py`, `viem`, `forge create`, etc.).
3. The signed-transaction bytes are submitted to the proxy via `eth_sendRawTransaction`.
4. The proxy forwards the bytes to an Arc node.

If someone steals your `swrm_` token they can eat your rate limit and attribute spurious activity to your handle; **they can't drain a wallet** with it. Rotate (`arc-canteen rotate-rpc-key`) if leaked, but don't panic.

### How you actually use it

**Pattern A — One-off via the canteen CLI:**

```bash
arc-canteen rpc eth_chainId
arc-canteen rpc eth_blockNumber
arc-canteen rpc eth_getBalance '["0xYOUR_ADDRESS", "latest"]'
```

**Pattern B — Set `$RPC` once, then any tool that takes `--rpc-url`:**

```bash
source ~/.arc-canteen/env
cast block-number --rpc-url $RPC
cast chain-id --rpc-url $RPC
forge create --rpc-url $RPC src/MyContract.sol:MyContract
```

**Pattern C — From Python or TypeScript:**

```python
import os
from web3 import Web3
w3 = Web3(Web3.HTTPProvider(os.environ["RPC"]))
print(w3.eth.chain_id, w3.eth.block_number)
```

```typescript
import { createPublicClient, http } from "viem";
const client = createPublicClient({ transport: http(process.env.RPC!) });
const block = await client.getBlockNumber();
```

### Why the proxy exists

1. **Telemetry attribution.** The proxy logs every call. The 30% Traction score in the hackathon rubric reads from this telemetry. Without per-user auth, Canteen has no way to tell which team's agent generated which on-chain activity.
2. **Per-team rate limiting.** Without auth, one runaway loop from any team would degrade the testnet for everyone.
3. **Operational control.** Canteen can spin the testnet up/down and revoke individual tokens without distributing new node URLs.

## Curated backtests are run by hand, on purpose

There is **no automated backtest refresh**. `services/backtest_scheduler.py` and its
`BACKTEST_REFRESH_*` / `BACKTEST_MAX_AGE_HOURS` knobs were deleted 2026-09-01
([#1760](https://github.com/aprin-labs/archimedes/issues/1760)): the loop re-ran the whole
curated library inside the serving process at +180 s on every cold boot, pegged the 1-vCPU
Fargate task, and got tasks killed by their own container health check.

A backtest is a one-time artifact of evidence with a stated data window — never revisited on
a clock. Generated strategies are backtested once, at generation. Curated strategies are
backtested when their code changes, when a data-quality fix lands, or when the owner asks, by
an explicit out-of-band run of `python -m archimedes.scripts.run_backtests`.

**Grading follows the same rule.** A strategy's rigor verdict is produced once, by the real
gate, at backtest time, stored on its passport, and READ by every surface — no route
recomputes one ([#1746](https://github.com/aprin-labs/archimedes/issues/1746)).
`run_backtests` grades at the end of its run; `python -m archimedes.scripts.grade_curated` is
the standalone job for the one-time backfill and for a re-grade after a gate change. A
curated strategy reads `pending` until that job has run against it, which is honest, not
broken.

- Procedure (both jobs): [`curated-backtests.md`](curated-backtests.md)
- Policy and the incident: [`../adr/backtests-are-frozen-evidence.md`](../adr/backtests-are-frozen-evidence.md)
- Why the verdict is stored, not recomputed: [`../adr/rigor-verdict-of-record.md`](../adr/rigor-verdict-of-record.md)

## Local↔prod parity

The local docker-compose stack runs the same code as the EC2 deployment. To verify your local setup matches production expectations:

```bash
# One-command parity check
./scripts/check-parity.sh                              # defaults to http://localhost:8000
./scripts/check-parity.sh https://archimedes-arc.com   # or against prod
```

This checks `/health` and asserts two things: live LLM backend, and a non-empty corpus (`corpus_papers > 0`; do not freeze a floor here). There used to be a third assertion, `fusion_enabled` — it went with the flag when `ARCHIMEDES_FUSION_ENABLED` was retired (deck Q4; see the [flip-list](../operations/feature-flag-fliplist.md)), and `/health` stopped publishing the field on 2026-09-03. Fusion is now the unconditional generation path, so there is nothing left to assert.

Full production infrastructure (ECS Fargate behind an ALB, CloudFront + WAF, Aurora PostgreSQL 18.3, ElastiCache Redis 7.1, CI/CD and Terraform) is documented in [`docs/architecture.md`](../architecture.md) and the Terraform under `infra/`. The old `docs/infra-setup.md` is archived and describes a single EC2 box that no longer exists.

## Reporting traction (the 30% rubric weight)

The `arc-canteen` CLI is not just an RPC proxy — it's also the **traction telemetry surface** that the judging rubric reads. **Until the team starts calling `update-traction` and `update-product` regularly, the rubric scoreboard for Archimedes reads zero, regardless of what's shipped.**

Two commands matter:

```bash
# Log a product / feature update — call after merging anything meaningful
arc-canteen update-product "Live testnet deploy — contracts on Arc + LLM-driven agentic advisor + rigor-gated strategy library"

# Log a traction event — call every time you talk to a potential user or onboard someone
arc-canteen update-traction "Shared live demo URL with two crypto-native users — first external traffic on the EC2 deploy"
```

Run `arc-canteen status` to view your current dashboard — what the judges will see. The judging-rubric assessment in ``docs/judging-rubric-assessment.md`` (routed to the private docs repo, 2026-08-19) breaks down where we currently stand on each weighted criterion.

> **Arc OSS Showcase submissions** use the same CLI with an `"ArcOSS:"` prefix on the product update (per the showcase landing page at <https://arc-oss.thecanteenapp.com/>). See ``docs/archive/agora-2026-05/ARC-OSS-SHOWCASE.md`` (routed to the private docs repo, 2026-08-19).

## Security notes

A short list of hygiene items worth surfacing explicitly.

### arc-canteen credentials are secrets

The `arc-canteen login` flow writes credentials to `~/.arc-canteen/env`. The file is permissioned `0600` (owner read/write only) by the CLI — verify yours is too with `ls -la ~/.arc-canteen/env`.

The file contains your **personal RPC endpoint URL** with an **embedded server token** (format: `swrm_<64-hex>`). **Treat the token like an API key:**

- 🚫 Do NOT commit `~/.arc-canteen/env` (not in this repo and not in any dotfiles repo).
- 🚫 Do NOT paste the full RPC URL into Discord channels, screenshots, pitch decks, GitHub issues, or AI chats. Use `$RPC` in commands so the literal token doesn't appear in shell history.
- 🚫 Do NOT share with teammates — each team member has their own.
- ✅ If you suspect leakage, run `arc-canteen rotate-rpc-key` to mint a fresh token and invalidate the old one. Cheap; takes seconds.

### Wallet hygiene

- Use a **dedicated dev wallet** for all hackathon testing — never connect a wallet that holds real assets.
- Private keys go in `.env` files (gitignored). Never commit secrets.
- The platform's signer key for rebalance calls lives in environment variables, not in the repo.
- Production on-chain writes go through Circle Developer-Controlled Wallets via `chain/circle_signer.py` — **no raw private keys** for vault operations.

### Dependency hygiene

- The Python deps in `environment.yml` are loosely-pinned for v1. We'll tighten when we move to a `pyproject.toml`-driven workflow.
- The `arc-canteen` CLI installs from the official [the-canteen-dev/ARC-cli](https://github.com/the-canteen-dev/ARC-cli) repo. Verify the URL when running `uv tool install` — typosquatting is a real attack pattern.
- All transitive deps of the arc-canteen CLI are standard CLI-tooling packages (typer, click, httpx, rich, pyyaml). The `annotated-doc` dep is from [fastapi/annotated-doc](https://github.com/fastapi/annotated-doc) — legitimate.

### GitHub OAuth scopes

When you ran `arc-canteen login`, the GitHub device flow authorized the Canteen app on your account. Verify the granted scopes at [github.com/settings/applications](https://github.com/settings/applications). For a hackathon-traction tool, expected scopes are minimal (`read:user`, `user:email`). If `repo` or `admin:*` scopes were granted, revoke and re-authenticate.
