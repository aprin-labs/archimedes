# Generation API

The generation surface turns a natural-language strategy brief into rigor-graded
candidate strategies via the debate-society pipeline (the sole live generation
path — see [debate-society-sole-generation-pipeline ADR](../adr/debate-society-sole-generation-pipeline.md)).
The API is job-based: `POST /api/generate/start` enqueues a job and returns
immediately (202); progress streams over Server-Sent Events at
`GET /api/generate/stream/{job_id}`, with polling fallbacks at `/jobs` and
`/jobs/{job_id}`. Generation sits behind an x402-style USDC paywall
(`GENERATION_PAYMENT_REQUIRED`) plus daily volume quotas, both enforced inside
`/start`; `GET /api/generate/quote` is the public price check whose exact
payload also rides inside every 402 the paywall raises. Paper trading
(`POST /api/paper/deployments`) is a separate, always-free surface — the
payment gate documented here is scoped to `/api/generate/start` only.

**Auth model.** Every route except `/quote` requires a Better Auth account
session (the `better-auth.session_token` cookie, verified server-side against
the colocated Better Auth service on every request — FastAPI never parses it
itself). The payment gate and the premium-model entitlement gate additionally
require a Better-Auth-linked, verified wallet on the account; a session alone
is not sufficient once `GENERATION_PAYMENT_REQUIRED` is on or a premium `model`
is requested. Examples below assume an authenticated session already sits in
`/tmp/session.jar` (obtained via the Better Auth service, not documented
here).

## Endpoints

### GET /api/generate/quote
The upfront generation cost estimate — public, so a human can see the price
before signing in and an agent can plan before paying; this exact payload also
rides inside every 402 from `/start`. | **Auth**: anonymous | **Flags**:
`GENERATION_PAYMENT_REQUIRED` (only the literal `"true"` enables the paywall
elsewhere), `GENERATION_PRICE_USD` (default `$0.15`), `GATEWAY_CHAIN`,
`PAYMENTS_DRY_RUN` (default `true`), `GENERATION_PAYMENT_RECIPIENT`

Request: none.
Response: `{payment_required: bool, pricing_model: "flat_v1", price: "$X.XXXXXX", asset: "USDC", chain: str, recipient: str|null, dry_run: bool, how: str}`.
Errors: none raised directly.

```bash
curl -s https://archimedes-arc.com/api/generate/quote
```

### POST /api/generate/start
Create an account-owned generation job (debate-society pipeline) and start it
in the background; the caller tails progress via the SSE stream. | **Auth**:
account-session | **Flags**: `GENERATION_PAYMENT_REQUIRED` (x402 paywall — see
[the status-code flow](#the-payment-gate-status-code-flow-post-apigeneratestart)
below), `GENERATION_DAILY_CAP_PER_USER` / `GENERATION_DAILY_CAP_PER_IP` (see
[Quotas](#quotas)), `PREMIUM_MODELS_ENABLED` / `PREMIUM_MODELS_ALLOWLIST`
(premium-model entitlement), slowapi `5/minute` (disabled under `TESTING`)

Request (`GenerateStartRequest`): `{brief: {intent: str(1..600 chars), risk_appetite: "fixed_income"|"conservative"|"moderate"|"aggressive"|"hyper_risky"="moderate", asset_classes: [str]|null, capital_usdc: float|null, max_papers: int(2..6)=5, name: str|null(<=80 chars, control-chars rejected)}, n_candidates: int(1..5)=1, mode: str|null (accepted for API compat, ignored post T1.1 Phase-3), model: str|null}`.
Response (202, `GenerateStartResponse`): `{job_id: str, stream_url: str, ttl_seconds: int}`.
Errors: 409 `wallet_link_required` (payment required, caller has no linked wallet); 402 (payment-gate failure, or a non-entitled premium `model`); 422 — `max_papers` outside `[2, 6]`, an `intent` outside `[1, 600]` characters, or any other body-validation failure; 422 `BRIEF_INVALID` (the deterministic brief screen, below); 429 (daily generation-quota cap exceeded, unless `TESTING`; or the 5/min burst limit); 503 `payment_config_missing` (payment flag on, `GENERATION_PAYMENT_RECIPIENT` unset).

**Brief screening (#1801).** `intent` is inserted verbatim into every prompt the
generation pays for, so it is bounded (1–600 characters, enforced by the request schema)
and screened deterministically — no LLM — *before* the payment gate: a brief that is
empty, mash, over-length, or carrying a prompt-injection payload (override directives,
role forgery, forged JSON replies, code fences, links, encoded blobs) is refused **422**
with `{reason: "brief_invalid", code: "BRIEF_INVALID", message, hint, reason_code}` and is
never charged for. `reason_code` is the machine-readable rule that tripped, drawn from a
versioned vocabulary; the full list, what is deliberately still allowed, and the
`BRIEF_UNVALIDATED` outcome (the model validator could not reach a verdict — the run stops
rather than admitting the brief) are documented in
[`brief-guidelines.md`](../brief-guidelines.md). Off-topic-but-grammatical text is not
screened here — it still reaches the model validator, post-payment.

```bash
curl -s -X POST https://archimedes-arc.com/api/generate/start \
  -b /tmp/session.jar -H "Content-Type: application/json" \
  -d '{"brief": {"intent": "momentum strategy on large-cap tech", "risk_appetite": "moderate", "max_papers": 5}, "n_candidates": 1}'
```

### GET /api/generate/stream/{job_id}
Server-Sent Events for one account-owned generation job. | **Auth**:
account-session

Request: path `job_id`; optional `Last-Event-ID` header to resume.
Response: `text/event-stream` `StreamingResponse` of `GenerateEvent` frames — `id: int, event: EventName(job_queued|brief_validated|pipeline_selected|candidates_selected|agent_iteration|tool_called|tool_result|candidate_drafted|candidate_evaluated|best_selected|trace_hashed|persisted|done|error), data: dict`. A ~15s heartbeat comment keeps the connection from going byte-silent past intermediary idle-timeouts; the connection is capped at 300s and the client reconnects with `Last-Event-ID`.
Errors: 404 `job {job_id} not found or expired` (unknown/expired job, or owned by a different account/wallet).

```bash
curl -N -b /tmp/session.jar https://archimedes-arc.com/api/generate/stream/<job_id>
```

### POST /api/generate/jobs/{job_id}/cancel
Cancel a running job. Idempotent — hard-cancels the backing asyncio task when
still live. | **Auth**: account-session

Request: path `job_id`.
Response: `{job_id: str, status: str}`.
Errors: 404 `job {job_id} not found or expired` (unknown/expired, or not owned).

```bash
curl -s -X POST -b /tmp/session.jar https://archimedes-arc.com/api/generate/jobs/<job_id>/cancel
```

### GET /api/generate/jobs
Recent jobs for the GenerationStatus UI, filtered to the canonical owner with
linked-wallet fallback for legacy (pre-account) jobs. | **Auth**:
account-session

Request: query `limit: int(1..100)=20`.
Response (`JobsListResponse`): `{jobs: [JobSummary{job_id, state: "queued"|"running"|"stalled"|"done"|"error"|"cancelled", brief_intent, created_at, updated_at, n_candidates, best_strategy_id: str|null, cost: dict|null}]}`. `"stalled"` (#1355) is a READ-TIME derived state — a `"running"` job whose `heartbeat_at` has gone stale for over 5 minutes — never written to Redis.
Errors: none beyond the global 401.

```bash
curl -s -b /tmp/session.jar "https://archimedes-arc.com/api/generate/jobs?limit=20"
```

### GET /api/generate/jobs/{job_id}
One job's current state — the poll fallback for a client with no live stream.
| **Auth**: account-session

Request: path `job_id`.
Response: `JobSummary` (same shape as the `/jobs` listing item).
Errors: 404 `job {job_id} not found or expired` (unknown/expired, `job.type != "generate"`, or not owned).

```bash
curl -s -b /tmp/session.jar https://archimedes-arc.com/api/generate/jobs/<job_id>
```

### GET /api/generate/jobs/{job_id}/cost
What this generation actually consumed — raw measurement only (Bedrock token
counts, wall/CPU seconds, peak RSS, write tallies), no prices. | **Auth**:
account-session

Request: path `job_id`.
Response (`JobCostResponse`): `{job_id, state: "queued"|"running"|"stalled"|"done"|"error"|"cancelled", cost: dict|null}` — `state` is derived identically to `JobSummary.state` (#1355) so this endpoint can't disagree with `/jobs`/`/jobs/{id}` about a stalled job; `cost` is `null` until the job reaches a terminal state, and for jobs older than the cost meter.
Errors: 404 `job {job_id} not found or expired` (unknown, or not owned).

```bash
curl -s -b /tmp/session.jar https://archimedes-arc.com/api/generate/jobs/<job_id>/cost
```

### GET /api/generate/jobs/{job_id}/candidates
Rejected-candidate viewer. Empty list until the job reaches `done`. |
**Auth**: account-session

Request: path `job_id`.
Response (`CandidatesListResponse`): `{job_id, best_candidate_id: str|null, candidates: [CandidateSummary{candidate_id, strategy_id: str|null, strategy_name, rigor_verdict: dict|null, passes_rigor: bool, selected: bool, regime: str|null, generation_method: str|null}]}`.
Errors: 404 `job {job_id} not found or expired` (unknown, or not owned).

```bash
curl -s -b /tmp/session.jar https://archimedes-arc.com/api/generate/jobs/<job_id>/candidates
```

### GET /api/generate/credits
The caller's own generation-credit ledger, newest-first (v8 Lane 1.3a) — makes
visible what `_paywall_with_credit` already does silently: an `available`
credit from an earlier paid-but-undelivered run pays for the NEXT generation,
with no new charge (#1441). | **Auth**: account-session

Request: none.
Response: `[{id: int, status: "pending"|"available"|"consumed"|"void", created_at: str|null, job_id: str|null, amount_usdc: float|null}]`. Narrower than the full ledger row — `payer_wallet`/`network`/`settlement_ref` are payment-plumbing internals the UI has no use for. `amount_usdc` is `null` until the underlying claim settles (mirrors `amount_base_units`'s own nullability), never a fabricated `0.0`.
Errors: none beyond the global 401.

```bash
curl -s -b /tmp/session.jar https://archimedes-arc.com/api/generate/credits
```

### GET /api/payments/receipts
The caller's own settled generation-payment receipts, newest-first (Dan's
directive, 2026-08-21: "we must provide people with their receipts"). Written
at settle time inside `/start` — fail-safe: a receipt-write failure never
fails or delays the paid generation, so this list can in principle be
incomplete during a genuine DB outage. Owner-scoped server-side; no
pagination yet (capped at 200 rows). | **Auth**: account-session

Request: none.
Response: `[{id: int, created_at: str|null, price_usd: str, amount_base_units: int, payer_wallet: str, settlement_ref: str|null, job_id: str|null, network: str}]`.
`settlement_ref` is `PaymentInfo.transaction` — a **Circle facilitator
reference id, not an on-chain transaction hash** (Circle batches and settles
on-chain later); it must never be rendered as a block-explorer link.
Errors: none beyond the global 401.

```bash
curl -s -b /tmp/session.jar https://archimedes-arc.com/api/payments/receipts
```

## The payment-gate status-code flow (POST /api/generate/start)

The paywall carries no payment fields in the request body — it is entirely a
status-code + header contract, gated end-to-end by
`GENERATION_PAYMENT_REQUIRED` (only the literal `"true"` enables it; while off,
every step below is skipped and `/start` behaves exactly as if the gate did
not exist). Order inside `/start` is deliberate — cheapest checks first, no
work burned on a request that will be refused:

1. **Quota** (`enforce_generation_quota`, runs first, skipped under `TESTING`) —
   429 if either daily cap is exceeded; 503 `generation_quota_unavailable` if
   the quota backend (Redis) itself is unreachable (fails **closed** — the
   response says explicitly that nothing was counted). See [Quotas](#quotas).
2. **Wallet-link precondition** (only when the payment flag is on) — no linked
   wallet on the account → **409** `wallet_link_required`. This is 409, not
   402: the blocker is account state (link a wallet, fund it — the faucet is
   human-only), not a missing payment.
3. **Payment gate** (`generation_payment.enforce_generation_payment`, only
   reached once a wallet is linked):
   - No `Payment-Signature` request header → **402**, body
     `{reason: "payment_required", message, quote}` (the identical payload
     `GET /api/generate/quote` returns) plus a `PAYMENT-REQUIRED` response
     header carrying the machine-readable x402 requirements — built by the
     same circlekit middleware that later settles. This 402 *is* the
     quote-approval flow; there is no separate "authorize" call.
   - Header present but its signed `from` doesn't match the caller's linked
     wallet → **402** `payer_mismatch`; undecodable header → **402**
     `payment_malformed`.
   - `PAYMENTS_DRY_RUN` (default `true`): a well-formed, wallet-matching
     header is accepted **without** verification or settlement — no real
     value moves while the custody migration (#975) is pending; the server
     logs this loudly so it can never be mistaken for revenue.
   - Live mode (`PAYMENTS_DRY_RUN=false`): the middleware verifies then
     settles via Circle's facilitator — **402** `payment_invalid` or
     `payment_settle_failed` on failure; on success the settlement receipt
     headers (`PAYMENT-RESPONSE`) are merged into the eventual 202 response.
   - `GENERATION_PAYMENT_REQUIRED=true` with `GENERATION_PAYMENT_RECIPIENT`
     unset → **503** `payment_config_missing` — fail-closed configuration
     error, never a free pass.
4. **Premium-model entitlement** (`enforce_model_entitlement`, independent of
   the paywall flag, always checked) — a `model` naming an Anthropic
   (`anthropic.` marker) Bedrock id from a wallet that is not entitled
   (entitled = wallet-connected **and** (`PREMIUM_MODELS_ENABLED=true` **or**
   wallet in `PREMIUM_MODELS_ALLOWLIST`)) → **402**, plain-text `detail` (no
   `reason`/`quote` envelope — this is a separate gate from step 3). The
   request is rejected outright, never silently downgraded to a free model.
5. Only after every check above passes is the job enqueued and the background
   pipeline started.

```bash
# 1) payment required but no wallet linked -> 409
curl -s -X POST https://archimedes-arc.com/api/generate/start -b /tmp/session.jar \
  -H "Content-Type: application/json" -d '{"brief": {"intent": "..."}}'
# {"detail": {"reason": "wallet_link_required", "message": "..."}}

# 2) linked wallet, no Payment-Signature -> 402 with PAYMENT-REQUIRED header + quote body
curl -s -i -X POST https://archimedes-arc.com/api/generate/start -b /tmp/session.jar \
  -H "Content-Type: application/json" -d '{"brief": {"intent": "..."}}'

# 3) retry, signed
curl -s -X POST https://archimedes-arc.com/api/generate/start -b /tmp/session.jar \
  -H "Content-Type: application/json" -H "Payment-Signature: <x402 signature>" \
  -d '{"brief": {"intent": "..."}}'
```

## Quotas

Two stacked daily volume caps, both enforced before the payment gate on every
`POST /api/generate/start` (skipped entirely under `TESTING`, same as the
slowapi limiter):

| Layer | Env var | Default | Key |
|---|---|---|---|
| Per-account | `GENERATION_DAILY_CAP_PER_USER` | 10/day | Better Auth `user.id` |
| Per-IP | `GENERATION_DAILY_CAP_PER_IP` | 20/day | `X-Real-IP` (nginx/ALB-resolved; `X-Forwarded-For` is deliberately never trusted) |

Both must pass; the user bucket is checked (and counted) first, so a caller
over their own cap cannot drain the shared IP bucket for others behind the
same NAT/office. `<= 0` disables that layer individually; both disabled means
unlimited. Each day-bucket self-expires after ~36h (covers the UTC-day
boundary + clock skew). The check **fails closed**: a Redis error on the
quota check itself returns 503 `generation_quota_unavailable`, explicitly
saying nothing was counted — never a silent 429 and never a silent pass.
Separately, `/start` also carries a slowapi burst limit of `5/minute`
(disabled under `TESTING`) — that bounds *rate*, this section bounds daily
*volume*.

```bash
# daily cap hit -> 429
# {"detail": {"message": "You've reached today's generation limit (10/day for this account)...",
#             "reason": "generation_daily_cap", "scope": "user", "cap": 10}}
```
