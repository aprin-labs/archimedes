# Cluster 0 — human unblocking + day-0 checks

**Zero code. Bash and `gh` only.** Every item has multi-day human lead time and is already
5 days late. Do this before writing a line.

Read [README](README.md) session rules first.

## Asks to Dan — send all in one message

1. **Contract review, prioritised: #1129 > #1200 > #1153.** One-paragraph exploit statement
   each, explicit Sept-16-gate deadline. Tell him the order rather than leaving him to choose.
   None of the three blocks the payment rail — say that too.
2. **Three decisions needed in writing:**
   - identity key (#1194) — **deadline EOD Aug 17**; default if unsettled: opaque `principal_id`
     derived from wallet today, Better Auth later. Document in an ADR either way.
   - narrow `PAYMENTS_DRY_RUN=false` scope (caller-signed metered-API path only; DCW browser
     subscribe stays dry-run, stays blocked on #975). **Bogdan acknowledges the custody side.**
   - mainnet `PaymentSplitter` owner address (multisig).
3. **Kick off Bedrock Anthropic use-case activation** — multi-day approval. No approval by
   **Aug 24** ⇒ assume it misses, B5 ships against the free model path.
4. **Circle mainnet credentials into SSM** + into the ECS `secrets` block, not just the
   best-effort boot fetch.
5. **`terraform apply` for #1071** (runner relocation). If he can't get to it, **stop the
   runners on the detached EC2 box** — a clean stop beats stale funds-adjacent code running
   unmanaged. Frame as a decision, not a complaint.

## Merges — max 2/day, ≥40 min apart, verify `/health` between

Order: **#1224** (mine, money path) → **#1226** (leaderboard provisional banner — makes the
leaderboard claim honest *today*, highest claim-value-per-token in the sprint) → **#1201**
(stop fabricating vault returns; honesty cover for the re-run window) → **#1095** (commit-reveal
stale-trace; narrows the "anchored on-chain" retraction, so sequence it *before* the claims copy).

Then #1202 (low risk, spare slot). #1096 merge-as-docs or close if #1225 supersedes.
**#1225 lands D1–D2 or after Aug 24 — never mid-sprint.** #1223 lands before or after the
re-run, never during (collides with A7 and B4's `num_trials_source`).

## Day-0 checks (B0)

1. **[BLOCKING] Does the pinned `circlekit` know an Arc *mainnet* chain?** Read the installed
   SDK's chain table. `marketplace/config.py:6` hardcodes `"arcTestnet"`; `payments.py:69`
   passes `GATEWAY_CHAIN` into `create_gateway_middleware(chain=…)`. If there is no mainnet
   entry, **the payment rail cannot reach Arc mainnet and the decision changes.**
2. Arc mainnet chain ID + RPC published? `contracts/foundry.toml` has only `arc-testnet`;
   `ui/src/siwe.js:15` hardcodes `5042002`.
3. **Reserve `archimedes-cli` on PyPI** — 10 minutes, irreversible if lost.
4. Measure real generations/wallet/day from prod before choosing a meter ceiling.
5. Confirm the considered-alternatives modal is broken in prod.

## Two live bug fixes — same PR, ~3 lines total

- [`ui/src/components/RejectedCandidates.jsx:73`](../../ui/src/components/RejectedCandidates.jsx#L73)
  — bare `fetch()` with no `credentials: 'include'`. **Verified still broken 2026-08-16.** Sole
  outlier against a universal convention (`api.js` + seven components). Generation reads
  require an authenticated session unconditionally (the `REQUIRE_SIWE_FOR_GENERATION`
  opt-out was deleted 2026-08-19), so this bare fetch 401/404s and the visible proof of the
  K=1-plus-rejects architecture is dead on the one surface that converts.
- **`PAYMENTS_DRY_RUN` is unset** in `infra/ecs.tf`, both compose files, and
  `setup-ssm-secrets.sh`. Its prod value is an accident of
  [`main.py:203`](../../backend/archimedes/main.py#L203)'s default. Set it explicitly — even to
  `true`. An unset money switch is the bug regardless of which way it points.

## A6 diagnostic — one command, do it here

> **Superseded 2026-09-01 (#1760).** The refresh loop this command greps for no longer exists —
> `services/backtest_scheduler.py` was deleted, so `/ecs/archimedes-backend` will never emit
> another `backtest refresh:` line. Curated backtests are produced by an explicit operator run
> ([`../runbooks/curated-backtests.md`](../runbooks/curated-backtests.md)); the summary line to
> grep for is `backtest run summary`. Kept as the historical record of the frozen-board
> investigation.

```bash
aws logs tail /ecs/archimedes-backend --since 7d \
  --filter-pattern '"backtest refresh"' | tail -40
```

- `backtest refresh: skipped (…)` → staleness/backoff (`_last_unresolved_missing`)
- `backtest refresh: cycle failed (…)` → runner raises (likely yfinance egress from Fargate)
- `REFUSING to persist … VolPlausibilityError` (`run_backtests.py:386`) → corrected results
  tripping the vol guard, rejected per-strategy, fail-closed, stale rows left in place

## File two tracking issues

Mainnet gate checklist (16 items + the 8 visibly-deferred, tagged out of scope) · claims ledger.

## Results — executed 2026-08-16

Recorded here so no later session re-derives any of it.

**Check 1 (BLOCKING) — answered: NO.** `circlekit/constants.py` at pinned SHA `09828f3999` has
23 `CHAIN_CONFIGS` entries. Arc appears once, `arcTestnet` / chain 5042002, in the testnet block.
The 11 mainnet entries are ethereum, base, arbitrum, polygon, optimism, avalanche, sonic,
unichain, worldChain, hyperEvm, sei. **Upstream HEAD is the same commit** (2026-03-24), so
bumping the pin does not help.

Two follow-on facts:
- `CHAIN_ALIASES` maps `"mainnet"` → `"ethereum"`. `GATEWAY_CHAIN=mainnet` resolves to a **valid**
  config and would settle real USDC on Ethereum rather than erroring. This is why
  [cluster-6](cluster-6-boot-paywall.md)'s startup assertion #1 is load-bearing, not hygiene.
- Mitigation without forking: `CHAIN_CONFIGS` is a plain module-level dict and `get_chain_config`
  reads it at call time, so an Arc mainnet `ChainConfig` can be registered at runtime. 8 of 9
  fields are available or derivable (`MAINNET_GATEWAY_WALLET` / `MAINNET_GATEWAY_MINTER` are
  already SDK constants; USDC is likely the same `0x3600…` native sentinel). **The one field we
  cannot invent is `gateway_domain`** — Circle's internal domain ID, `arcTestnet` is `26`.
  → Tracked as the top item on the mainnet-gate issue.

**Check 2 — no.** `contracts/foundry.toml` still has only `arc-testnet`;
`ui/src/siwe.js:15` is `VITE_ARC_CHAIN_ID ?? '5042002'`.

> **Both halves have moved since (2026-08-30).** `siwe.js` was deleted whole by `95c9faf7`
> (2026-07-28) and its env seam went with it, leaving the chain id written as a literal in
> five files. `README.md`'s correction table briefly recorded this file as one that "has
> never existed"; that was a working-tree grep reading a deletion as an absence, and the
> anchor above was accurate when written. The seam is restored in `ui/src/chain-config.js`
> (#1240 UI half), which is now the only place the id is written. `foundry.toml` still has
> only `arc-testnet` and remains Dan's item on #1240.

**Check 4 — meter ceiling.** Funnel on 2026-08-16: landed 301, wallet_connected 35,
generation_started 22, vault_deployed 0. Versus 2026-08-10 (282/35/22/0): **+19 landings, zero
new wallet connections, zero new generations in six days.** `/api/marketplace/published` still
`[]`. Any ceiling is invisible at this volume — set it from observed p99 after
`METER_ENFORCE=false` ships, per [cluster-5](cluster-5-meter.md).

**Check 5 — confirmed** by code inspection; fixed in PR #1239.

**A6 diagnostic — BLOCKED, reassigned.** No AWS credentials on Önder's machine and none
available (Dan holds the account). The `aws logs tail` command is now an ask to Dan, not a
local step. **Do not retry it locally.**

**Environment facts** (each cost a round-trip to discover):
- conda base is `/opt/homebrew/Caskroom/miniconda/base`; env at `.../envs/archimedes`,
  Python 3.12.13, circlekit installed. `conda info --base` returns **empty** non-interactively —
  it is shell-function-initialized in `.zshrc`. **Use the absolute path.**
- `node` is **not** in the conda env despite CLAUDE.md; `/opt/homebrew/bin/node` is what exists.
  `ui/node_modules` is present, so `./node_modules/.bin/eslint` works.
- `tofu` (OpenTofu) is installed; `terraform` is not.
- `aws` CLI is not installed.

**Artifacts:** PR #1238 (sprint cards, CI green) · PR #1239 (both day-0 bug fixes) ·
issue #1240 (mainnet gate) · issue #1241 (claims ledger).

**Still open:** Dan message unsent · PyPI `archimedes-cli` unreserved (needs an account) ·
merges held.

## Done when

Asks sent · 2 merges landed with `/health` verified · circlekit mainnet answer known · PyPI name
held · both bug fixes in one PR · A6 hypothesis identified by log line.
