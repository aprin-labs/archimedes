# Agent API — driving the Archimedes journey programmatically

> Status: canonical Better Auth accounts, optional proof-linked wallets, generation,
> DEPLOY, and MONITOR. `--deploy` remains DRY RUN by default; see DEPLOY below.

Archimedes exposes same journey to humans and HTTP clients. Better Auth user ID is
canonical application identity. Wallet proof is separate and required only for wallet
or on-chain actions. This document gives exact `/api/*` contract. Reference client is
[`scripts/agent_journey.py`](../scripts/agent_journey.py).

Two reasons this matters:
1. **Dogfooding** — running the journey as code surfaces the bugs and rough edges a
   human would hit, fast and repeatably.
2. **The agent-user segment** — an agent that sends a non-browser `User-Agent` is
   classified as an external agent by the telemetry middleware and its
   `generation_started` is attributed in the conversion funnel ([#787](https://github.com/aprin-labs/archimedes/issues/787)).
   An agent holding an **API key** does better: it is classified `keyed` from the
   credential itself rather than from a header it chose.

**READ** needs no authentication. **GENERATE requires an account credential**:
anonymous `POST /api/generate/start` returns 401, and per-job stream/status/jobs/candidates
reads are scoped by canonical user ID. Wallet connection alone never authenticates.

Two credentials establish that account identity, and **they are interchangeable
everywhere except key management**: the Better Auth **session cookie**, and an
`Authorization: Bearer archim_…` **API key** (see [API keys](#api-keys-bearer)). Both
resolve to the same canonical user ID at the same chokepoint
(`api/account_auth.py`), so no route, quota, rate limit, or paywall can tell them
apart — a key is a credential, never a bypass.

## Quick start

```bash
# read-only smoke (no LLM spend, no auth)
python scripts/agent_journey.py --base https://archimedes-arc.com --read-only --no-auth

# full journey; credentials come from env, never CLI
ARCHIMEDES_EMAIL=agent@example.test ARCHIMEDES_PASSWORD='<secret>' \
  python scripts/agent_journey.py --base https://archimedes-arc.com \
  --intent "diversified low-volatility strategy for idle USDC" --risk moderate

# isolated smoke account (creates disposable account)
python scripts/agent_journey.py --base https://archimedes-arc.com --ephemeral
```

The client identifies itself with an agent `User-Agent`, so its traffic shows up
as an external agent in `/api/metrics`. It retains the Better Auth cookie across the
run, streams generation, and prints the rigor verdict. Exit code is nonzero on hard
failure.

### The paper deployment the harness leaves behind

Relocated from `README.md` (2026-08-20); it is the one side effect of a "read-only-ish"
run that surprises people.

After generation the winner is deployed to **free paper trading by default**
(`--no-paper` skips it). That creates a **persistent paper deployment** on whatever
`--base` points at:

- On `--ephemeral` runs the script stops the deployment at the end — the disposable
  account is unreachable afterwards, so leaving it running would strand a ledger nobody
  can read.
- On real-account runs it is left **ACTIVE deliberately** — that running ledger is the
  point. Stop it later with `POST /api/paper/deployments/{id}/stop`.

`--deploy` additionally links a wallet from `AGENT_WALLET_KEY` (or a disposable in-memory
wallet under `--ephemeral`) through EIP-4361. Paper trading needs neither a wallet nor gas;
`--deploy` needs both.

Reference implementation: [`scripts/agent_journey.py`](../scripts/agent_journey.py).
For the raw `/api/*` surface without the harness, see
[`skills/`](../skills/README.md) — grounded, file:line-cited agent skills on the
generate/verdict flow, reading a strategy passport honestly, the `archimedes` CLI, and
the x402 marketplace payment flow.

## The journey

### 1. READ — public surfaces (no auth)

| Call | Returns |
| --- | --- |
| `GET /health` | `{ "status": "ok", ... }` — liveness |
| `GET /api/metrics` | `{ human_count, agent_count, total_requests, timestamp }` — cumulative human/agent **traffic** counters (#428). These are request tallies, **not users** — mostly crawlers. |
| `GET /api/metrics/funnel` | distinct-visitor conversion funnel (`landed → generation_started → wallet_connected → vault_deployed`) with `pct_of_landed` + `step_conversion` per stage (#787). Add `?day=YYYY-MM-DD` for one day. |
| `GET /api/strategies/` | the curated/example strategy library |

### 2. GENERATE — start + stream (account session required)

```http
POST /api/generate/start
Content-Type: application/json

{
  "brief": {
    "intent": "diversified low-volatility strategy for idle USDC",
    "risk_appetite": "moderate",          // fixed_income | conservative | moderate | aggressive | hyper_risky
    "max_papers": 5                        // 2..6
  },
  "n_candidates": 1,                       // 1..5 considered internally (K=1 winner is emitted)
  "model": null                            // optional; allowlisted free model id, else env default. Premium → HTTP 402 without entitlement.
}
```

Response `202`:
```json
{ "job_id": "…", "stream_url": "/api/generate/stream/…", "ttl_seconds": 3600 }
```

Then tail the **SSE** stream:
```http
GET /api/generate/stream/{job_id}
```
Events (`event:` name + `data:` JSON), in rough order:
`job_queued → brief_validated → pipeline_selected → candidates_selected →
agent_iteration → tool_called → tool_result → candidate_drafted →
candidate_evaluated → best_selected → trace_hashed → persisted → done` (or `error`).

**Poll fallback** — for a client that never opened the stream, or whose connection dropped
past the event-log TTL:
```http
GET /api/generate/jobs/{job_id}
```
```json
{
  "job_id": "…",
  "state": "queued",            // queued | running | stalled | done | error | cancelled
  "brief_intent": "…",
  "created_at": "…",             // ISO-8601 UTC
  "updated_at": "…",
  "n_candidates": 1,
  "best_strategy_id": null       // set once a winner is persisted
}
```
Identical record to the matching entry in `GET /api/generate/jobs`, so a client can switch
between the listing and the single-job read without the two disagreeing. `"stalled"`
(#1355) is a READ-TIME derived state — a `"running"` job whose `heartbeat_at` has gone
stale for over 5 minutes — never written to Redis. `state == "done"`
with a non-null `best_strategy_id` is the signal to move on to the candidates read below;
`error` and `cancelled` are terminal. The stored failure string is not exposed — it holds
raw unscrubbed exception text; the `error` state and the SSE `error` event (which carries
`message` / `code` / `recoverable`) are the reporting path. Only `generate` jobs are
readable here; any other job id returns the same 404 as an unknown one.

### 3. RIGOR — the externalized verdict + considered alternatives

```http
GET /api/generate/jobs/{job_id}/candidates
```
```json
{
  "job_id": "…",
  "best_candidate_id": "…",
  "candidates": [
    {
      "candidate_id": "…",
      "strategy_id": "…",
      "strategy_name": "…",
      "rigor_verdict": { "...": "DSR / PBO / walk-forward / look-ahead fields" },
      "passes_rigor": true,
      "selected": true,
      "regime": "neutral"
    }
  ]
}
```
This is the K=1-generation + externalized-rigor-gate shape from the architecture
principles: one winner (`selected: true`) plus the considered-and-rejected
alternatives, each carrying the rigor verdict the user reviews before deploy.

### 4. LIVE GATE — deployability (the SAME verdict a human sees)

The `candidates` payload above carries the *generation-time* rigor verdict. The
**authoritative deployability verdict** — the one the server-side `create_vault`
gate (#829) enforces and the human sees on the passport — is the **live rigor gate**
(#833, compute-on-read from the strategy's real persisted returns). Read it per
candidate from its persisted strategy:

```
GET /api/strategies/{strategy_id}
→ { "rigor_gate_status": "pass" | "fail" | "pending" | "degenerate",
    "passes_rigor_gate": <bool>,          # true ⇒ deployable
    "sharpe_ratio": <float|null>,          # real backtest Sharpe (null while pending)
    "deflated_sharpe_ratio": <float|null>,
    "dsr_p_value": <float|null>,
    "out_of_sample_sharpe": <float|null>,
    "pbo_score": <float|null>, ... }
```

- **`pass`** — real returns exist and the live gate passed → `passes_rigor_gate: true`, deployable.
- **`fail`** — real returns exist and the gate failed ≥1 criterion → not deployable (honest).
- **`pending`** — no real persisted returns yet → the gate cannot run; not deployable.
- **`degenerate`** — real returns exist but are a mathematically constant (zero-variance) series (broken data or a zero-trade backtest) → not a legitimate DSR/OOS input, never deployable (#1184).

> Fusion/debate candidates emit a DSL spec (`weights={}`) scored by
> `evaluate_fusion_spec`, so they skip the static buy-and-hold backtester. Until the
> #788/#818 fix, their real backtest returns weren't persisted → the live gate read
> `pending` forever and nothing was deployable. The fix persists those returns so a
> fusion/debate winner reads `pass`/`fail` like any other strategy. **Never weaken
> the gate to force a `pass` — `pending`/`fail` are honest, deployable-only-on-`pass`.**

### 5. METER + VERIFY — quota readback and standalone rigor (account session required)

Two utility endpoints an agent can call outside the generation journey. Both are
`require_current_user`-gated and both are advertised at `GET /api/agent/manifest`
(groups `account` and `rigor`) and in `/.well-known/agent.json`.

```http
GET /api/account/usage
```
```json
{
  "date": "2026-08-20",
  "user_id": "…",
  "user": { "used": 3, "cap": 10, "unlimited": false, "remaining": 7, "error": null },
  "ip":   { "used": 3, "cap": 25, "unlimited": false, "remaining": 22, "error": null },
  "quote": { "…": "the SAME generation_payment.quote() the enforcement path reads" }
}
```
Read this before `POST /api/generate/start` to avoid spending a request on a 429. It
reads the same two Redis buckets `enforce_generation_quota` enforces (a peek, never a
write), and the same `quote()` the enforcement path prices from, so the displayed number
and the enforced number cannot drift. `used` is honestly `null` — never a fabricated
`0` — when the quota backend is unreachable.

```http
POST /api/rigor/verify        # rate limited 5/minute
{ "returns": [{ "date": "2026-01-02", "daily_return": 0.0012 }, …], "trials": 40 }
```
Runs the gate's admission checks over a **bare returns series** you submit — no strategy
code, no trial matrix — reusing the identical functions and threshold constants the
strategy-passport verdict uses. A bare series can only support two of the four checks:

| Check | From a bare series |
| --- | --- |
| DSR | evaluable — deflated by the **self-attested** `trials` count, gated on `DSR_P_FLOOR` |
| walk-forward OOS | evaluable — single chronological 70/30 holdout, gated on `OOS_ABS_FLOOR` |
| PBO | always `not_evaluable` — overfitting probability is a property of a *selection set* |
| look-ahead audit | always `not_evaluable` — AST analysis of code, and this endpoint takes only numbers |

`passes` is `true` **iff every RUNNABLE leg — DSR *and* walk-forward OOS — actually ran
and passed**: a quorum, not "no evaluable check failed" (#1481). Neither an
all-`not_evaluable` request nor a partially-evaluated one (a degenerate series where DSR
ran and the OOS split could not) may report `passes: true`; the response carries
`legs_evaluated` / `legs_runnable` / `legs_total` / `verdict_capped` so the scalar is
qualifiable without re-deriving the leg statuses. `self_attested: true` is returned to
keep the caller's declared `trials` count visible as the unverified input it is.

**The request body is validated strictly and nothing is repaired (#1803).** Dates must be
strict `YYYY-MM-DD`, unique and ascending; returns must be finite with `abs(r) <= 1.0`
(simple decimals — +1.3% is `0.013`); the series is 250..2,600 rows and `trials` is
1..10,000. **250 daily bars — one trading year — is the minimum evaluation window**: under
it the answer is a typed refusal naming `bars_received` and `bars_required`, never a
verdict and never a verdict with a warning attached. A violation is a 422 carrying
`{"detail": {"error": "input_rejected", "reason": "<code>", "message": "…"}}` where
`<code>` is one of `invalid_date`, `duplicate_date`, `unsorted_dates`, `non_finite`,
`out_of_range`, `window_too_short`, `too_many_rows`, `trials_out_of_range`. The walk-forward split is positional, so an out-of-order series is
REFUSED rather than sorted — sorting it would return a verdict on a series you did not
send. Full table:
[`api/strategies-and-rigor.md`](api/strategies-and-rigor.md). The response also carries
`rf_convention` (`excess_tbill_series` | `excess_flat_fallback`, #1409) — the `date`s
above already resolve against the historical 3-month T-bill series when they fall inside
its vendored coverage, and DSR/OOS are computed against whichever rate that resolution
used; see [`rigor-methods.md` §1a](rigor-methods.md#1a-the-risk-free-rate-behind-excess-issue-1409).

## Account authentication and optional wallet proof

### Better Auth recipe

Keep cookies in one HTTP client. No browser or wallet is required.

1. `GET /api/auth/providers` to discover email/password and configured OAuth providers.
2. Create account when needed:
   `POST /api/auth/sign-up/email` with
   `{ "name": "Agent", "email": "...", "password": "..." }`.
3. Sign in:
   `POST /api/auth/sign-in/email` with `{ "email": "...", "password": "..." }`.
4. Confirm `GET /api/auth/get-session` returns non-null user and session.
5. Sign out with `POST /api/auth/sign-out`.

Use `ARCHIMEDES_EMAIL` and `ARCHIMEDES_PASSWORD` for reference client. Never pass
credentials as command-line arguments or commit them. `--ephemeral` creates disposable
account for smoke testing.

### API keys (Bearer)

A cookie expires in seven days and can only be refreshed by re-sending the account
password. For anything unattended, mint a key once instead.

```bash
# mint (session cookie required — see below)
curl -sS -b /tmp/agora.jar -X POST $BASE/api/account/keys \
  -H 'Content-Type: application/json' -d '{"name":"ci-nightly"}'
```

```json
{ "id": "9f3c1a77b204de51", "name": "ci-nightly", "prefix": "archim_9f3c1a77b204de51",
  "created_at": "…", "last_used_at": null, "revoked_at": null,
  "key": "archim_9f3c1a77b204de51_KtQ8yv…" }
```

Then every account-gated call in this document is one header:

```bash
curl -sS -H "Authorization: Bearer $ARCHIMEDES_KEY" $BASE/api/account/usage
```

| Endpoint | Method | Credential | Notes |
| --- | --- | --- | --- |
| `/api/account/keys` | `POST` | **cookie only** | Body `{"name": "…"}` (1–64 chars). `201`. Returns `key` **once**. `409 api_key_limit_reached` past 25 live keys. |
| `/api/account/keys` | `GET` | **cookie only** | `id`, `name`, `prefix`, `created_at`, `last_used_at`, `revoked_at`. Never the key. |
| `/api/account/keys/{id}` | `DELETE` | **cookie only** | `204`, idempotent. Another account's id → `404`. |
| everything else account-gated | — | cookie **or** key | Identical behaviour either way. |

The contract, precisely:

- **Format** `archim_<key_id>_<secret>`. `secret` is 32 random bytes from `secrets`;
  `archim_<key_id>` is the non-secret `prefix` the list returns.
- **Shown once.** The server stores a per-key salted SHA-256 of the secret and nothing
  else, so the token cannot be read back by anyone, including operators. Lost key →
  revoke and mint another.
- **Verification is constant-time** (`hmac.compare_digest`) on the hit path *and* the
  miss path, so response timing does not tell an attacker whether a key id exists.
- **Revocation is immediate** — read from the row on every request, no cache, no TTL.
  The next call with a revoked key is `401`.
- **Scoped to one account.** A key resolves to its own account and no other; another
  account's key id is `404`, not `403`, so the surface is not an enumeration oracle.
- **Never a bypass.** Same daily caps, same per-route rate limits, same x402 paywall,
  same wallet precondition. A keyed `POST /api/generate/start` returns the same `402`,
  with the same quote, that a cookie call returns.
- **A key cannot manage keys.** The three endpoints above answer `403` to a bearer key.
  Containment: a leaked key must not be able to mint successors that outlive your
  revoking the one you know about.
- **Telemetry.** A keyed caller classifies as `agent_type: "keyed"` — an identity, unlike
  `external`, which is a User-Agent guess about an unauthenticated client.

### Optional EIP-4361 wallet link

Wallet is needed only for wallet/on-chain operations. Account session must exist first.

1. `POST /api/wallets/challenge` with
   `{ "address": "0x...", "chain_id": 5042002, "provider": "headless" }`.
2. Sign exact returned `message`; do not reconstruct it.
3. `POST /api/wallets/verify` with returned message and signature.
4. `GET /api/wallets` confirms link.

`provider` is provenance only — it never widens or narrows what the link may do. Accepted
values are `metamask`, `browser`, `circle`, and `headless`. **An API client sends
`headless`**: the other three name browser wallet software a script does not have, and
recording one of them logs a fact that is not true. `circle_wallet_id` may accompany
`circle` only. The live set is advertised at `GET /api/agent/manifest` under
`auth.wallet_link_providers`.

Challenge is bound to account, normalized address, chain, domain, URI, issue time, and
five-minute expiry. It is consumed atomically and cannot replay. A wallet already linked
to another account returns 409 and is never transferred automatically. EOA and
ERC-6492/EIP-1271 smart-wallet proofs are supported.

### Job endpoint scoping

Jobs are tagged with creator `owner_user_id`:

| Endpoint | Rule |
| --- | --- |
| `GET /api/generate/stream/{job_id}` | account required (401); user mismatch → **404** (no existence oracle) |
| `GET /api/generate/jobs` | account required; listing filtered to caller |
| `GET /api/generate/jobs/{job_id}` | same owner rule as stream; non-`generate` job types → **404** |
| `GET /api/generate/jobs/{job_id}/candidates` | same owner rule as stream |
| `POST /api/generate/jobs/{job_id}/cancel` | same owner rule as stream |

Legacy ownerless jobs remain readable to authenticated callers for migration
compatibility. Browser `EventSource` sends same-origin cookies automatically.

### Funding an agent wallet (USDC is gas on Arc) — a DIFFERENT deploy path

The `POST /api/vaults/create` DEPLOY path below needs **no funding on the
agent wallet** — the backend's own signer pays gas (see "DEPLOY" for why).
`fund_agent_wallet.py` is for a separate, not-yet-built path: an agent
signing `createVault` itself, client-side, the way the browser UI's
`CreateVaultModal` does. That path — if built — would need the agent EOA to
hold native USDC (chain 5042002 uses USDC as gas) before it could submit a
transaction. Two options via
[`scripts/fund_agent_wallet.py`](../scripts/fund_agent_wallet.py) (dry-run by
default; `--execute` to send):

```bash
# a) treasury transfer — native-USDC value transfer from DEV_WALLET_PRIVATE_KEY (.env):
python scripts/fund_agent_wallet.py --to 0x<agent-wallet> --amount 5 --execute

# b) Circle faucet API (Bearer $CIRCLE_API_KEY). The Arc blockchain enum is
#    UNVERIFIED — the raw response is printed so the right value can be learned:
python scripts/fund_agent_wallet.py --to 0x<agent-wallet> --mode faucet --execute
```

## Slice 2 continued — DEPLOY + MONITOR

### DEPLOY — create a vault from the generated strategy

With account session and verified linked wallet established, call `POST
/api/vaults/create`. Reference implementation:
`build_vault_create_payload` / `step_deploy` in
[`scripts/agent_journey.py`](../scripts/agent_journey.py).

```http
POST /api/vaults/create
Content-Type: application/json
Cookie: better-auth.session_token=...

{
  "name": "Agent Journey 2026-07-10 12:00",
  "symbol": "AGTJRN",
  "management_fee_bps": 0,
  "performance_fee_bps": 0,
  "agent_assisted": true,
  "strategy_ids": ["<the winning strategy_id from RIGOR / LIVE GATE above>"],
  "strictness_level": 1
}
```

Response `200`:
```json
{ "vault_address": "0x...", "strategy_ids": ["..."] }
```

**Who pays gas — not the agent wallet.** The backend's own signer creates the
vault on-chain, then transfers `Ownable` ownership to caller's selected linked
wallet and pins backend as rebalance-only agent (`owner == you`,
`agent == backend`; see `create_vault` in
[`vaults_routes.py`](../backend/archimedes/api/vaults_routes.py)). See
"Funding an agent wallet" above for the (different, not-yet-built) path that
would need agent-wallet funding.

**Server-side rigor gate is authoritative (#818, `_assert_strategies_pass_rigor`
in `vaults_routes.py`).** Every `strategy_ids` entry must pass the live rigor
gate at the requested `strictness_level`, checked **before** any gas is
spent, or the call returns **422** — enforced independently of the client, so
no caller (agent or human) can route around it. The reference client mirrors
this client-side: `step_deploy` refuses to call the endpoint at all — no
request sent — unless the winning candidate's LIVE GATE read above is
`deployable: true`. **Never weaken or skip that check to force a deploy.**

`--deploy` defaults to a **DRY RUN**: the harness prints the exact payload
and sends nothing. Pass `--deploy` to actually call the endpoint. The default
stays OFF, but the original reason no longer holds: the T3.2 redeploy landed
2026-07-09 and issue
[#588](https://github.com/aprin-labs/archimedes/issues/588) (whether the repo's
cached ABI matches the live deployed bytecode) closed 2026-07-14. The
`deploy` group has been `live` in the served manifest since
[#1447](https://github.com/aprin-labs/archimedes/pull/1447). It stays OFF now for
the ordinary reason: this call spends gas and creates a real on-chain vault,
so it should be an explicit act, not a default.

### MONITOR — read vault health back

```http
GET /api/vaults/{address}/health
```

No auth required. Safe for any address, including one nothing is deployed
behind yet — it reads Redis-backed snapshot/heartbeat state keyed by address
string, not a chain existence check, so an unknown address returns an
empty-ish snapshot rather than erroring.

```json
{
  "vault_address": "0x...",
  "agent_alive": true,
  "last_heartbeat": "2026-07-10T11:58:00+00:00",
  "last_rebalance": "2026-07-09T12:00:00+00:00",
  "rebalance_age_seconds": 86400.0,
  "aum_trend_pct": 1.23,
  "snapshot_count": 42,
  "latest_snapshot": { "...": "most recent AUM/holdings snapshot, or null" },
  "sharpe_drift": { "available": false, "reason": "baseline_backtest_sharpe_unavailable" },
  "recent_events": [ "...vault-scoped or regime_change/agent_error events, newest 5..." ]
}
```

### CLI flags for DEPLOY + MONITOR

| Flag | Default | Meaning |
| --- | --- | --- |
| `--deploy` | off | actually POST to `/api/vaults/create` (else dry run — see above) |
| `--vault-name` | `Agent Journey <UTC timestamp>` | vault name (≤ 64 chars) |
| `--vault-symbol` | `AGTJRN` | vault symbol (≤ 16 chars) |
| `--management-fee-bps` | `0` | management fee, basis points (0–1000) |
| `--performance-fee-bps` | `0` | performance fee, basis points (0–3000) |
| `--strictness-level` | `1` | rigor strictness for deploy (1 = strictest/safest … 5 = most permissive; the always-on correctness floors hold at every level) |
| `--monitor-address` | none | `GET /api/vaults/{address}/health` for an existing vault, independent of deploying one this run |

```bash
# full journey including a REAL deploy (spends the backend signer's testnet gas):
python scripts/agent_journey.py --ephemeral --deploy

# health check only, no generation, no auth:
python scripts/agent_journey.py --no-auth --read-only --monitor-address 0x<vault-address>
```

### What's NOT covered here

The reference harness (`scripts/agent_journey.py`) does **not** use API keys — it
still drives the cookie recipe above. Teaching it the key lane is follow-up work,
not part of the key lane itself.

**Correction (2026-08-31).** An earlier version of this section said funnel
segmentation by `agent_type` "remains open work". That was true when it was
written and is not true now: [#788](https://github.com/aprin-labs/archimedes/issues/788)
shipped, and `GET /api/metrics/funnel` returns a per-stage `by_agent_type`
breakdown over `internal` / `keyed` / `external` / `human`. What is still open is
**interpretation**: before the API-key lane, an authenticated agent was
indistinguishable from a human (`classify_request` resolved a session to `human`
before any agent heuristic ran), so historical `external: 0` at
`generation_started` measures the absence of the question, not the absence of
agents. `keyed` is the first `agent_type` on that endpoint that reflects a
credential rather than a guess; readings taken before it existed cannot be
compared with readings taken after.
