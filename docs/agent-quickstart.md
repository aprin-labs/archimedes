# Agent quickstart — zero to paper-traded, one curl per step

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-09-01

You are an autonomous agent that has never seen this API. This page is the shortest
deterministic path from "no account" to "a strategy running in a paper-trading ledger you
can read back." Eleven numbered steps, roughly one `curl` each, plus three lettered
detours you take only when the runtime says you need them — 6a/6b for the paywall, 7b for
polling. Every expected response shape is taken from the route that produces it.

**Read this if you want to run the journey. Read [`agent-api.md`](agent-api.md) if you
want the full surface** — the wallet-link handshake in detail, the real on-chain vault,
the marketplace, the `agent_journey.py` reference harness. This page is narrower: nothing
below creates a vault or puts capital on-chain.

**Your first three generations are free once your email is verified; after that it is
not.** An account is required for every generation and there is no wallet-only path, but a
**wallet is not required for the first `FREE_GENERATIONS_PER_ACCOUNT` (default 3)
generations on a verified account**, lifetime (#1643 — this reverses the 2026-08-19
"wallet before the first generation" directive earlier revisions of this page documented;
the email-verification condition is the owner's 2026-08-31 amendment). **If you cannot
receive and click a verification link, you have no free tier** — go straight to the wallet
path (steps 6a–6b); it works from generation #1 for an unverified account. From generation
#4, generation sits behind a live x402 paywall in production: `GET /api/generate/quote` answers
`payment_required: true`, `dry_run: false`, `price: "$2.000000"` — so step 6 then charges
**$2.00 testnet USDC per run and settles for real**, from a wallet you link and fund first
(steps 6a–6b). The code *defaults* are the opposite of that — the paywall is off and
dry-run is on unless a deploy turns them on — so a local checkout never charges while
production does, and **step 1 against the host you are actually calling is the only thing
that tells you which one you are on.** One step is not autonomous: funding the wallet goes
through Circle's faucet, which today needs a human.

Conventions used below:

- `$BASE` is `https://archimedes-arc.com` in production, `http://localhost:8080` locally.
- `-c/-b /tmp/agora.jar` is the session cookie jar. One jar for the whole run.
- **Or skip the jar entirely.** Every step below marked `cookie` also accepts
  `-H "Authorization: Bearer $ARCHIMEDES_KEY"` — a long-lived API key you mint once at
  step 4b. Same account, same limits, same paywall; no jar, and no re-sending your
  password when the 7-day cookie expires. If you are writing CI, do step 4b and use the
  header everywhere.
- Prefer tools over curl? See *Driving this from an MCP client instead*, below — the MCP
  server wraps these same routes and takes the same key via `ARCHIMEDES_API_KEY`.
- Response bodies are shown **shape-first**: field names and types are exact, values are
  illustrative. `…` means "more fields of the same kind that this page does not depend on."
  **The one exception is step 1** — its values are the live production ones, because there
  the value *is* the decision.
- A non-browser `User-Agent` classifies you as an external agent in the telemetry
  middleware. Send one; it costs nothing and makes your traffic legible. **A key does
  better:** a keyed call classifies as `agent_type: "keyed"` from the credential itself,
  which is an identity rather than a guess about a header you chose.

---

## The path

| # | Step | Call | Auth |
|---|---|---|---|
| 0 | Discover | `GET /api/agent/manifest` | none |
| 1 | Price the run | `GET /api/generate/quote` | none |
| 2 | Create an account | `POST /api/auth/sign-up/email` | none |
| 2a | Verify your email — this is what unlocks the 3 free generations | click the link in the mail sent at step 2; `POST /api/auth/send-verification-email` re-sends it | none |
| 3 | Sign in (get the cookie) | `POST /api/auth/sign-in/email` | none |
| 4 | Confirm the session | `GET /api/auth/get-session` | cookie |
| 4b | Mint an API key — once, if you are automating | `POST /api/account/keys` | cookie **only** |
| 5 | Check your quota + free generations left | `GET /api/account/usage` | cookie **or key** |
| 6 | Submit the brief | `POST /api/generate/start` | cookie |
| 6a | Link a wallet — once, after the free generations run out | `POST /api/wallets/challenge` then `POST /api/wallets/verify` | cookie |
| 6b | Pay the $2 and retry | `POST /api/generate/start` + `Payment-Signature` | cookie + wallet |
| 7 | Watch it run | `GET /api/generate/stream/{job_id}` | cookie |
| 7b | …or poll instead | `GET /api/generate/jobs/{job_id}` | cookie |
| 8 | Read the verdict | `GET /api/generate/jobs/{job_id}/candidates` | cookie |
| 9 | Read the **authoritative** gate | `GET /api/strategies/{strategy_id}` | none |
| 10 | Paper-deploy | `POST /api/paper/deployments` | cookie |
| 11 | Read the ledger back | `GET /api/paper/deployments/{deployment_id}` | cookie |

Step 9 is not optional. Step 8's verdict is the *generation-time* one; step 9 is the
**stored verdict of record** — graded once by the real gate and served by every surface.
They can disagree, and step 9 wins.

Every row that says `cookie` accepts a key instead, **except step 4b itself** — see there
for why.

---

### 0. Discover

```bash
curl -sS $BASE/api/agent/manifest
```

```json
{
  "name": "Archimedes",
  "blurb": "…",
  "docs": { "llms_txt": "/llms.txt", "agent_api": "…", "quickstart": "…", "agent_card": "/.well-known/agent.json" },
  "erc8004": { "chain": "eip155:5042002", "identityRegistry": "0x8004…BD9e", "agentId": null, "tokenURI": null, "status": "registration_pending", "…": "…" },
  "erc8004_verification": { "status": "registration_pending", "agentId": null, "source": "unconfigured", "owner": null, "expectedOwner": null, "…": "…" },
  "auth": { "scheme": "Better Auth session", "methods": ["emailPassword", "google", "github"], "wallet_link_providers": ["metamask", "browser", "circle", "headless"], "chain_id": 5042002, "…": "…" },
  "endpoints": { "read": { "status": "live", "auth_required": false, "routes": { "…": "…" } }, "…": "…" },
  "faucet": { "url": "https://faucet.circle.com/", "description": "…" }
}
```

`auth_required` is a **per-group** flag. `erc8004.status` is `registration_pending`: the
ERC-8004 registration file is published, and **no registration exists on-chain** — do not
treat this agent as a registered, reputed, or validated ERC-8004 counterparty.

`erc8004_verification` is how that status was reached, and it is worth reading before you
act on the one beside it. `source: "onchain"` means a live `ownerOf(agentId)` call answered
this request; `"unconfigured"` means no identity is configured to check; `"unavailable"`
means the registry produced no ownership answer, so the pending status is a refusal to claim
rather than a finding that this agent is unregistered. The status can only say `registered`
when `source` is `onchain` and `owner` equals `expectedOwner`.

### 1. Price the run

```bash
curl -sS $BASE/api/generate/quote
```

```json
{
  "payment_required": true,
  "pricing_model": "flat_v1",
  "price": "$2.000000",
  "asset": "USDC",
  "chain": "arcTestnet",
  "recipient": "0xffa7abba5f17cb8471ebf150bf808bd6fb8856c1",
  "dry_run": false,
  "halted": false,
  "how": "POST /api/generate/start without a Payment-Signature header returns 402 with these requirements in the PAYMENT-REQUIRED header; sign them (x402 / Circle Gateway) and retry with Payment-Signature."
}
```

Public on purpose: price the run before you hold a session. **This is the one response on
this page whose values are not illustrative** — the block above is what
`https://archimedes-arc.com` actually returned on 2026-08-30, and those values decide
whether the rest of the journey costs money. `halted` is the operator kill switch
(`services/generation_payment.py`'s `_payments_halted`) — `true` means an operator has
frozen payments and `POST /api/generate/start` will 503 rather than take money it cannot
honestly settle; it is orthogonal to `dry_run` and to `payment_required`.

`payment_required` and `dry_run` are the runtime truth, and they are the truth *of the
host you asked*:

- **`payment_required: true`** (production today) — step 6 needs a linked, funded wallet
  and a signed payment **once your account's free generations are used up, or immediately
  if your email is not verified** (see the box below). Unlinked and out of free runs →
  `409 wallet_link_required`; linked but unsigned → `402`. Steps 6a and 6b are that path,
  and **`dry_run: false` means the $2 settles for real** rather than being waved through
  unverified.
- **`payment_required: false`** — step 6 needs no wallet and no payment; skip 6a and 6b.

> **Your first few generations are free, wallet or not — once your email is verified
> (#1643).** An *account* is required for every generation — there is no
> wallet-only-without-account path — but a **wallet is not**, for the first
> `FREE_GENERATIONS_PER_ACCOUNT` (default **3**) generations on that account, lifetime,
> **provided the account's email is verified**. So against production the sequence is:
> sign up (step 2), verify the emailed link (step 2a), sign in (step 3), then step 6 works
> immediately, three times, with no wallet and no payment. Steps 6a/6b apply from
> generation #4 onward, unchanged.
>
> **An unverified account has no free tier and is not blocked either** — it simply gets the
> wallet + payment path from generation #1. The 409 it receives names both ways out
> (verify, or link a wallet) and carries `free_generations_locked_reason:
> "email_unverified"` so a client can tell that case from a genuinely exhausted allowance.
> Verification is the cheaper unlock where an inbox is available; where it is not — the
> honest case for many agents — the wallet path is the whole answer, and the free tier is
> not something to wait for.
>
> **A run that delivers nothing does not cost a free generation.** The slot is claimed at
> step 6 and handed back if the job then fails without persisting a strategy — the corpus
> being too thin to fuse, a crash, a cancel. So a failed generation is not a spent one, and
> step 5 is where you find out: re-read it after a failure rather than decrementing your own
> counter. (A run that DID persist a strategy keeps its slot even if it errored afterwards —
> the strategy is in your library.)
>
> This **reverses** the 2026-08-19 directive that earlier revisions of this page
> documented ("a wallet is required before the first generation"); the verification
> condition is the owner's 2026-08-31 amendment to it. Read your remaining allowance and
> lock state from `GET /api/account/usage` (step 5) rather than counting locally — that
> endpoint reads the same ledger, and applies the same lock predicate, the gate enforces.
> `free_generations_remaining: null` there means *unknown* (the ledger could not be read),
> never *zero*; retry rather than assuming you are locked out.

Do not carry an answer over from another host or from the source. The code *defaults* are
`GENERATION_PAYMENT_REQUIRED` unset (paywall off) and `PAYMENTS_DRY_RUN=true` (nothing
settles), which is the opposite of what production serves — a deployment sets both, so
the default tells you nothing about the deployment. Re-read this endpoint per host.

### 2. Create an account

```bash
curl -sS -X POST $BASE/api/auth/sign-up/email \
  -H 'Content-Type: application/json' \
  -d '{"name":"Boardy","email":"boardy@example.test","password":"correct horse battery staple"}'
```

```json
{ "token": null, "user": { "id": "…", "email": "boardy@example.test", "name": "Boardy", "emailVerified": false, "image": null } }
```

HTTP 200 and **no `Set-Cookie`** — `autoSignIn` is off, so registration alone does not
start a session. Password must be 12–128 characters. A verification email is always sent;
sign-in is *not* blocked on it unless `EMAIL_VERIFICATION_ENFORCED=true` (it is `false` in
production today).

### 3. Sign in — this is the step that gives you the cookie

```bash
curl -sS -c /tmp/agora.jar -X POST $BASE/api/auth/sign-in/email \
  -H 'Content-Type: application/json' \
  -d '{"email":"boardy@example.test","password":"correct horse battery staple"}'
```

```json
{ "redirect": false, "token": "…", "url": null, "user": { "id": "…", "email": "…", "…": "…" } }
```

Sets a session cookie (HttpOnly, `SameSite=Lax`, 7-day expiry) — `__Secure-better-auth.session_token`
on `https://archimedes-arc.com`, or bare `better-auth.session_token` on a local `http://`
checkout, because production's HTTPS lets Better Auth apply the `__Secure-` cookie prefix
(a browser-enforced rule that can't be satisfied without TLS) and local HTTP cannot. **Keep
the jar for every remaining step** — `-c/-b` carries whichever name the server actually
sent, so nothing below needs to know which one that was. The `token` in the body is not a
bearer credential for this API — the cookie is what `require_current_user` reads.

### 4. Confirm the session

```bash
curl -sS -b /tmp/agora.jar $BASE/api/auth/get-session
```

```json
{ "session": { "id": "…", "token": "…", "expiresAt": "…" }, "user": { "id": "…", "email": "…", "emailVerified": false } }
```

A bare `null` (still HTTP 200) means not authenticated — that is the signal, not an error.
If you see `null` here, every cookie-gated step below will 401; fix it now.

### 4b. Mint an API key — do this once, then stop carrying a jar

Optional for a one-shot run; **do it if anything about your caller is unattended.** The
cookie from step 3 expires in seven days, and the only way to refresh it is to re-send your
account password. A key does not expire, is revocable on its own, and turns every
remaining step into one header.

```bash
curl -sS -b /tmp/agora.jar -X POST $BASE/api/account/keys \
  -H 'Content-Type: application/json' \
  -d '{"name":"ci-nightly"}'
```

```json
{
  "id": "9f3c1a77b204de51",
  "name": "ci-nightly",
  "prefix": "archim_9f3c1a77b204de51",
  "created_at": "2026-08-31T20:14:03.118Z",
  "last_used_at": null,
  "revoked_at": null,
  "key": "archim_9f3c1a77b204de51_KtQ8yv…"
}
```

**`key` is shown exactly once and is never recoverable.** The server keeps only a salted
hash of it, so nobody — including us — can read it back to you. Store it now; if you lose
it, revoke it and mint another.

```bash
export ARCHIMEDES_KEY='archim_9f3c1a77b204de51_KtQ8yv…'
```

From here on, **every step below that shows `-b /tmp/agora.jar` works with one header
instead.** Step 6 is the one you will actually run in CI:

```bash
# with the jar (steps 3 + 4 first, and again every 7 days):
curl -sS -b /tmp/agora.jar -X POST $BASE/api/generate/start -H 'Content-Type: application/json' -d "$BRIEF"

# with a key (step 4b once, ever):
curl -sS -H "Authorization: Bearer $ARCHIMEDES_KEY" -X POST $BASE/api/generate/start -H 'Content-Type: application/json' -d "$BRIEF"
```

What a key is **not**: it is not a way around anything. It resolves to the same account
the cookie resolves to, so the daily caps, the rate limits, the paywall and the wallet
precondition all apply identically — a keyed `POST /api/generate/start` returns the same
`402` with the same quote a cookie call returns. It is a different *credential*, not a
different *tier*.

The one thing a key cannot do is manage keys. `POST`/`GET`/`DELETE` on
`/api/account/keys` require a session cookie and answer `403` to a key, so a leaked key
cannot mint itself successors that survive your revoking the one you know about.

List with `GET /api/account/keys` (session cookie, as above):

```bash
curl -sS -b /tmp/agora.jar $BASE/api/account/keys
```

Revoke with `DELETE /api/account/keys/{key_id}` — `204`, and idempotent:

```bash
export KEY_ID=9f3c1a77b204de51
curl -sS -b /tmp/agora.jar -X DELETE $BASE/api/account/keys/$KEY_ID
```

The list never contains a key — only `prefix` (`archim_<id>`, which identifies a key and
cannot be used as one), `created_at`, `last_used_at` (minute resolution) and `revoked_at`.
Revocation takes effect on the **next** request that presents the key; there is no cache
and no grace period. Up to 25 live keys per account.

### 5. Check your quota before spending a generation

```bash
curl -sS -b /tmp/agora.jar $BASE/api/account/usage
# …or, with a key:
curl -sS -H "Authorization: Bearer $ARCHIMEDES_KEY" $BASE/api/account/usage
```

```json
{
  "date": "2026-08-30",
  "user_id": "…",
  "user": { "used": 0, "cap": 10, "unlimited": false, "remaining": 10, "error": null },
  "ip":   { "used": 0, "cap": 25, "unlimited": false, "remaining": 25, "error": null },
  "quote": { "payment_required": true, "…": "the same object step 1 returned" },
  "free_generations_allowance": 3,
  "free_generations_remaining": 3,
  "free_generations_error": null,
  "free_generations_locked_reason": null
}
```

Both caps must pass. `used: null` with `error: "quota_backend_unavailable"` is an honest
"we could not read the counter", never a fabricated `0`. Reading this is a peek — it never
increments anything.

The caps are stacked **underneath** the paywall and enforced **before** it, so a
quota-blocked caller is refused `429` without ever being asked to pay.

**`free_generations_*` is a different axis from the two caps above and neither replaces
the other.** `user`/`ip` are *daily* volume caps that reset every UTC day;
`free_generations_remaining` is your account's *lifetime* allowance of generations that
need no wallet and no payment at all (#1643). Both apply: with free generations left you
are still refused `429` once the daily cap is hit. `free_generations_remaining: null` with
`free_generations_error` set is the same honest unknown as `used: null` — it is not a
zero, so do not treat it as "locked out"; retry.

**`free_generations_locked_reason` is a third, separate fact: whether the remaining slots
can be spent right now.** `null` means they can. `"email_unverified"` means the account's
email is not verified, so the count is real but not yet spendable — verify (step 2a) and
it becomes available without any of it being consumed. Read the pair, never one alone:
`remaining: 3` with a lock is *not* "3 free runs available", and a lock is *not* an error
(`free_generations_error` stays `null`). A reason string this page does not list is still
a lock — treat it as "not spendable, cause unknown" and take the wallet path.

Once `free_generations_remaining` reaches `0` on a `payment_required: true` host — or
immediately, if `free_generations_locked_reason` is set and you cannot clear it — step 6
starts costing $2 and steps 6a/6b become mandatory.

### 6. Submit the brief

```bash
curl -sS -b /tmp/agora.jar -X POST $BASE/api/generate/start \
  -H 'Content-Type: application/json' \
  -d '{"brief":{"intent":"diversified low-volatility strategy for idle USDC","risk_appetite":"moderate","max_papers":5},"n_candidates":1}'
```

HTTP **202**:

```json
{ "job_id": "…", "stream_url": "/api/generate/stream/…", "ttl_seconds": 3600 }
```

Field bounds, enforced (violating any of them is a 422 — see the error table):
`risk_appetite` ∈ `fixed_income | conservative | moderate | aggressive | hyper_risky`;
`max_papers` ∈ [2, 6]; `n_candidates` ∈ [1, 5]; optional `brief.name` ≤ 80 chars, no
control characters. `model` is optional and defaults to the server's free model — naming a
premium model without an entitlement is a **402**, and the request is refused rather than
silently downgraded.

**On a `payment_required: true` host, this call returns 202 for your account's first three
generations — once the account's email is verified — and starts refusing afterwards** —
`409 wallet_link_required` (no wallet on the account; its
`free_generations_locked_reason` tells you whether the blocker is an unverified email or a
spent allowance) or `402` (wallet linked, nothing signed). Those are the paywall, not an error in
your request, and your brief was not run. Steps 6a and 6b clear them; the body above is
unchanged and gets replayed verbatim at 6b. On a `payment_required: false` host every call
is 202 and no allowance is spent at all. `GET /api/account/usage` (step 5) is how you tell
which side of the gate you are on before submitting.

The server refuses in a deliberate order, so read the status before reacting: `429` for the
daily cap, then `429 generation_queue_full`, then `409`, then `402`. **You are never asked
to pay for a slot that does not exist, and a quota-blocked or queue-blocked call takes no
money** — the `generation_queue_full` body says so in as many words.

### 6a. Link a wallet — once per account, after the free generations run out

Skip this entirely when `payment_required` is `false`, and skip it until step 5 reports
`free_generations_remaining: 0` **or a `free_generations_locked_reason` you cannot clear**
(an agent with no reachable inbox cannot verify an email, and that is a normal case, not a
failure — this step is then required from generation #1). You need a wallet you control the
key for; `provider: "headless"` is the one of the four an API caller can use.

```bash
curl -sS -b /tmp/agora.jar -X POST $BASE/api/wallets/challenge \
  -H 'Content-Type: application/json' \
  -d '{"address":"0xYOUR_WALLET","chain_id":5042002,"provider":"headless"}'
```

Returns an EIP-4361 (SIWE) message to sign. Sign it with that wallet's key — that step is
local to you, not an API call — and hand back the signature:

```bash
curl -sS -b /tmp/agora.jar -X POST $BASE/api/wallets/verify \
  -H 'Content-Type: application/json' \
  -d '{"address":"0xYOUR_WALLET","signature":"0x…"}'
```

`GET /api/wallets` reads the links back. Message construction, nonce handling, and the
exact field names are in
[`agent-api.md`](agent-api.md#optional-eip-4361-wallet-link) — this page shows only where
the two calls sit in the journey.

**The wallet must also hold testnet USDC, and that is the one step you cannot do
yourself.** Arc testnet USDC comes from <https://faucet.circle.com/>, which currently
requires a human. Linking an empty wallet gets you past the `409` and straight into a
payment you cannot sign.

### 6b. Pay the $2 and retry

With a linked wallet, step 6 returns **402** carrying the machine-readable x402
requirements in a `PAYMENT-REQUIRED` header, and the human-readable quote in the body:

```json
{ "detail": { "reason": "payment_required", "message": "…", "quote": { "price": "$2.000000", "…": "the step-1 object" } } }
```

Sign those requirements (x402 / Circle Gateway, EIP-3009 authorization) with the linked
wallet and replay the **identical** step-6 request with the signature attached:

```bash
curl -sS -b /tmp/agora.jar -X POST $BASE/api/generate/start \
  -H 'Content-Type: application/json' \
  -H "Payment-Signature: $SIGNED_X402_PAYLOAD" \
  -H "Idempotency-Key: $A_STABLE_KEY_YOU_CHOSE" \
  -d '{"brief":{"intent":"diversified low-volatility strategy for idle USDC","risk_appetite":"moderate","max_papers":5},"n_candidates":1}'
```

Now you get the **202** and the `job_id` from step 6. A settlement receipt comes back in a
`PAYMENT-RESPONSE` header.

**Do not blind-retry a payment — send an `Idempotency-Key`, as above.** x402 is not
crash-retry-idempotent: a naive retry signs a *fresh* EIP-3009 authorization, and a second
settled authorization is a second real $2. A retry under the same key spends what you
already paid instead of charging again; reusing a key whose generation already started is
a deliberate `409 idempotency_key_already_used`.

**A paid run that never delivers is repaid as a credit, not a refund** — Circle's
facilitator settles one way and there is no reverse call, so promising a refund would be a
promise the code cannot keep
([ADR](adr/generation-payment-credit-not-refund.md)). The credit is durable and
automatic: your next `POST /api/generate/start` spends it and skips the paywall entirely,
so a payer whose last generation died is never asked for money twice.

### 7. Watch it run (SSE)

```bash
curl -sS -N -b /tmp/agora.jar $BASE/api/generate/stream/$JOB_ID
```

Frames are `id:` / `event:` / `data:` triples, one JSON object per `data:` line:

```
id: 3
event: candidate_evaluated
data: {"…": "event-specific payload"}
```

Event names, in rough order: `job_queued → brief_validated → pipeline_selected →
candidates_selected → agent_iteration → tool_called → tool_result → candidate_drafted →
candidate_evaluated → best_selected → trace_hashed → persisted → done` — or `error`, whose
`data` carries `message` / `code` / `recoverable`.

### 7b. …or poll, if you never opened the stream

```bash
curl -sS -b /tmp/agora.jar $BASE/api/generate/jobs/$JOB_ID
```

```json
{
  "job_id": "…",
  "state": "running",
  "brief_intent": "diversified low-volatility strategy for idle USDC",
  "created_at": "2026-08-30T12:00:00+00:00",
  "updated_at": "2026-08-30T12:00:31+00:00",
  "n_candidates": 1,
  "best_strategy_id": null,
  "cost": null
}
```

`state` ∈ `queued | running | stalled | done | error | cancelled`. **Move on when `state ==
"done"` and `best_strategy_id` is non-null.** `stalled` is derived at read time (a
`running` job whose heartbeat is >5 min old), never stored. `error` and `cancelled` are
terminal. A job that is not yours returns **404**, not 403 — existence is private.

### 8. Read the verdict and the considered alternatives

```bash
curl -sS -b /tmp/agora.jar $BASE/api/generate/jobs/$JOB_ID/candidates
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
      "rigor_verdict": { "…": "DSR / PBO / walk-forward / look-ahead fields" },
      "passes_rigor": true,
      "selected": true,
      "regime": "neutral",
      "generation_method": "debate"
    }
  ]
}
```

One winner (`selected: true`) plus the alternatives that were considered and rejected.
Take `strategy_id` from the `selected` candidate.

### 9. Read the authoritative gate — the same verdict a human sees

```bash
curl -sS $BASE/api/strategies/$STRATEGY_ID
```

```json
{
  "id": "…",
  "rigor_gate_status": "pass",
  "passes_rigor_gate": true,
  "sharpe_ratio": 0.91,
  "deflated_sharpe_ratio": 0.62,
  "dsr_p_value": 0.03,
  "out_of_sample_sharpe": 0.44,
  "pbo_score": 0.31,
  "methodology_summary": "…",
  "asset_universe": ["…"],
  "papers": [ { "…": "…" } ],
  "…": "…"
}
```

Public read (no cookie needed) — a private strategy 404s rather than 401s. `rigor_gate_status`
is four-state and each state means something different:

| `rigor_gate_status` | What it means | Deployable |
|---|---|---|
| `pass` | The stored grade passed | yes |
| `fail` | The stored grade failed ≥1 criterion | no — and that is the honest outcome |
| `pending` | **No gate has graded this row.** Either there are no persisted returns to grade, or the grading job has not run over the ones there are | no |
| `degenerate` | Real returns exist but are a zero-variance series (broken data or a zero-trade backtest) | no |

`passes_rigor_gate` is `true` only when the status is `pass`. **Never treat `pending` or
`fail` as a soft yes.** Paper trading (step 10) is simulated and does not enforce this
gate — a real on-chain vault does, server-side, before spending any gas.

**The verdict is graded once, not on every read.** A strategy is graded at backtest time by
the real gate and the answer is persisted on its passport; every route serves that stored
verdict, so this route and `archimedes_passport` cannot disagree about one id — curated or
generated. A strategy nobody has graded yet answers `pending` on both, which is true rather
than a disagreement. Read the
provenance on the passport (`graded_at`, `gate_version`, `cohort_n`) when you need to know
*which* grade you are looking at — a `gate_version` of `legacy-derived` means the verdict was
inferred from older columns by a migration rather than produced by a gate run. See
[`docs/adr/rigor-verdict-of-record.md`](adr/rigor-verdict-of-record.md).

### 10. Paper-deploy — simulated, free, no chain, no funds

```bash
curl -sS -b /tmp/agora.jar -X POST $BASE/api/paper/deployments \
  -H 'Content-Type: application/json' \
  -d "{\"strategy_id\":\"$STRATEGY_ID\"}"
```

HTTP **201**:

```json
{
  "deployment_id": "…",
  "strategy_id": "…",
  "deployed_at": "2026-08-30",
  "status": "active",
  "days": 0,
  "total_return": 0.0,
  "drift_detected_at": null,
  "rigor_gate_status": "fail",
  "passes_rigor_gate": false,
  "graded_at": "2026-08-30T11:22:33",
  "gate_version": "gate-v1-4f2a9c1e07b3d5a8",
  "series": []
}
```

`series` starts empty and fills one row per day — `{"date", "daily_return",
"equity_index"}` — appended by the scheduler, never rewritten. `days: 0` on the first read
is normal, not a failure.

**Deploy has no rigor precondition, and the payload says so.** A strategy the
gate REJECTED can be paper-traded — that is deliberate (#1764): a rejected
strategy performing poorly forward is evidence about the gate's call. The
verdict of record travels on every deployment payload precisely so that freedom
stays honest, and an agent rendering, publishing or reasoning over
`total_return` must carry `rigor_gate_status` (`pass` | `fail` | `pending` |
`degenerate`) and `graded_at` with it. The verdict is READ from the passport,
never recomputed here, and it fails closed to `pending` — so this payload never
reports a pass it did not find. There is no deployment-window flag; nothing in
the product declares a deployment window, and `graded_at` beside `deployed_at`
is what carries staleness. Full field table: `docs/api/paper-trading.md`.

### 11. Read the ledger back (and stop it when you are done)

```bash
curl -sS -b /tmp/agora.jar $BASE/api/paper/deployments/$DEPLOYMENT_ID
```

Same shape as step 10, with `series` grown and `total_return` moved. A deployment that is
not yours returns **404**. Stop it explicitly with `POST /api/paper/deployments/{deployment_id}/stop`
— an abandoned deployment keeps accruing:

```bash
curl -sS -b /tmp/agora.jar -X POST $BASE/api/paper/deployments/$DEPLOYMENT_ID/stop
```

```json
{ "deployment_id": "…", "status": "stopped" }
```

You are done. A strategy was generated from research, graded by a gate that is allowed to
say no, and is now running in a ledger you can read. On a `payment_required: true` host
that cost you one real $2.00 USDC settlement at step 6b and nothing else — no vault was
created and no capital was deployed, because paper trading is free and simulated.

---

## Grading returns you already have — `POST /api/rigor/verify`

The whole path above generates a strategy. If you already have a returns series and only
want the gate's verdict on it, this one route does that, free, at 5 requests a minute, with
an account session or an `archim_` key. It is also the backend for the CLI's
`archimedes verify RETURNS_CSV` and the `archimedes_rigor_verify` MCP tool. Full reference:
[`api/strategies-and-rigor.md`](api/strategies-and-rigor.md).

The body carries **at least 250 daily bars — one trading year, the minimum evaluation
window** — so build it from your series rather than by hand:

```bash
python - <<'PY'
import csv, json
rows = []
with open("returns.csv", newline="") as fh:          # two columns: date, daily_return
    for date, value in csv.reader(fh):
        try:
            rows.append({"date": date, "daily_return": float(value)})
        except ValueError:
            continue                                  # the header row
json.dump({"returns": rows, "trials": 12}, open("body.json", "w"))
PY
curl -s -X POST $BASE/api/rigor/verify \
  -b /tmp/session.jar -H "Content-Type: application/json" --data-binary @body.json
```

Each row is `{"date": "2025-01-02", "daily_return": 0.01078}` — a strict `YYYY-MM-DD` date
and a simple decimal return, oldest first. **Under 250 bars there is no verdict**: the
answer is a refusal, `422 {"detail": {"reason": "window_too_short", "bars_received": 249,
"bars_required": 250, …}}`, and never a `passes` field with a warning beside it. Do not
retry a short series expecting a caveated answer — fetch more history. At 250 both runnable
legs can actually run, which is the point of the floor.

**The input contract is strict and the server repairs nothing.** Build the body to these
rules or it is refused — there is no coercion, no sorting, no deduplication and no
truncation anywhere in this path:

| Rule | `detail.reason` on violation |
|---|---|
| `date` is a strict `YYYY-MM-DD` calendar date (not epoch seconds, not `YYYYMMDD`) | `invalid_date` |
| no two rows share a date | `duplicate_date` |
| dates ascend | `unsorted_dates` |
| every `daily_return` is finite (JSON `NaN`/`Infinity` are refused, not ignored) | `non_finite` |
| `abs(daily_return) <= 1.0` in simple-return units — **+1.3% is `0.013`, not `1.3`** | `out_of_range` |
| 250 rows minimum — one trading year, the minimum evaluation window | `window_too_short` |
| 2,600 rows maximum (~10 years of daily bars) | `too_many_rows` |
| `1 <= trials <= 10000` | `trials_out_of_range` |

A refusal is one object, not a validation list — `{"detail": {"error": "input_rejected",
"reason": "unsorted_dates", "reasons": [...], "message": "…", "loc": ["body", "returns"]}}`
— so branch on `detail.reason` and show the caller `detail.message`. The MCP tool promotes
that code straight to its `error` field.

Ordering is the rule most worth understanding, because it is the one you are most likely to
break by accident and the one that would otherwise be exploitable: the walk-forward split is
**positional**, the first 70% of *rows* against the last 30%. Row order therefore *is* the
time order being graded, and a series sorted by return would park its best bars in the
holdout and collect a pass. The server refuses an out-of-order series rather than sorting
it, because sorting would hand you a verdict on a series you did not send.

Be clear about what that closes: the **row-order** form of the attack, which is the
accidental one and the detectable one. It does not close relabelling — a caller who writes
ascending dates onto return-sorted values sends a body no server can tell from a real
series, and gets a 200. That is why every response says `self_attested: true` and
`verdict_capped: true`: this route grades the numbers you sent, it does not attest that
they are yours or that they happened in that order.

**What the verdict can and cannot claim.** Two of the gate's four legs can never run on a
bare returns series — PBO needs a trial matrix of candidate strategies, and the look-ahead
audit needs strategy source, which is never uploaded — so both always come back
`not_evaluable` and `verdict_capped` is always `true`. `passes` is a quorum over the two
runnable legs: true only when DSR **and** walk-forward OOS both ran and both passed. A leg
can still fail to *run* on the numbers — a zero-variance series has no Sharpe to compute —
and that shows up as `legs_evaluated < legs_runnable`. When **no leg actually failed**,
that is an **incomplete evaluation** — neither a pass nor a fail, and it must not be
reported as either. When a leg *did* fail, the failure is a real verdict and stands: an
unevaluable second leg does not launder it (the CLI exits `1` there, not `4`). Shortness is
no longer one of the ways to reach that state: the 250-bar window sits above the ~70 bars
the walk-forward split needs, so a series too short to grade is **refused** — `422
window_too_short` from the API, exit `2` from the CLI — rather than partially graded.
`trials` is self-attested and unverifiable, so the DSR is only as honest as the number you
declared. Nothing here earns "Archimedes Verified"; the passport gate (step 9) is the
verdict that does.

## Error table

Every row below is a body this journey can actually produce. Note the two shapes of
`detail`: FastAPI errors raised by this API use a **string or an object**, while *request
validation* failures use a **list**.

**Authentication is checked before the body is validated.** An unauthenticated request
with a malformed body returns `401`, not `422` — so a 401 can be hiding a second problem
you will only see after step 3 succeeds. Fix the session first, then re-read the error.

| Status | Body | What happened | Fix |
|---|---|---|---|
| **401** | `{"detail": "Authentication required"}` + `WWW-Authenticate: Session` | No valid credential on a gated route — no session cookie, **or** an API key that is unknown, malformed, or revoked | Redo steps 3–4. Signing up (step 2) does **not** sign you in; only step 3 sets the cookie. Check the jar is passed with `-b`. If you are using a key: the header must be exactly `Authorization: Bearer archim_…`, and a revoked key 401s from the next call onward — mint a new one at step 4b. A wrong key and an unknown key are the same answer on purpose. |
| **403** | `{"detail": "API keys cannot manage API keys. …"}` | You called `/api/account/keys` with `Authorization: Bearer archim_…` | Use the session cookie for key management. This is containment, not a bug: a leaked key must not be able to issue successors. |
| **409** | `{"detail": {"reason": "api_key_limit_reached", "message": "…"}}` | 25 live keys already | Revoke one (`DELETE /api/account/keys/{key_id}`) before minting another. |
| **402** | `{"detail": {"reason": "payment_required", "message": "…", "quote": {…}}}` + `PAYMENT-REQUIRED` header | The generation paywall is on and no `Payment-Signature` was presented | **Expected on production** — step 6b, not a bug. Sign the x402 requirements in the header with your **linked** wallet and retry with `Payment-Signature` (plus an `Idempotency-Key`). `GET /api/generate/quote` → `payment_required` tells you which host you are on; only when it is `false` can this not happen. |
| **409** | `{"detail": {"reason": "idempotency_key_already_used", "message": "…"}}` | The `Idempotency-Key` you replayed already paid for a generation that started | Do not re-sign. That run exists — find it via `GET /api/generate/jobs/{job_id}`; use a fresh key for a genuinely new run. |
| **402** | `{"detail": "Model '…' is a premium (Anthropic) model and requires an entitlement. …"}` | You named a premium `model` without entitlement | Omit `model` (the free default is used) or name an allowlisted free model. The request is **not** silently downgraded. |
| **422** | `{"detail": [{"type": "…", "loc": ["body", "brief", "max_papers"], "msg": "…", "input": …}]}` | Request body failed validation | Read `loc` — it names the exact field. Common causes: `max_papers` outside [2, 6], `n_candidates` outside [1, 5], an unknown `risk_appetite`. |
| **422** | `{"detail": "strategy_id is required"}` | `POST /api/paper/deployments` with an empty or missing `strategy_id` | Send `{"strategy_id": "<id from step 8>"}`. |
| **422** | `{"detail": {"reason": "no_strategy_spec", "message": "This strategy has no machine-readable spec to paper-trade."}}` | The strategy exists but carries no executable spec | Pick a different candidate from step 8. Not every generated row is paper-tradeable. |
| **422** | `{"detail": {"reason": "invalid_strategy_spec", "message": "Stored spec fails validation: …"}}` | The stored spec failed DSL validation at deploy time | Not caller-fixable — pick another candidate and report the `strategy_id`. |
| **422** | `{"detail": {"error": "input_rejected", "reason": "unsorted_dates", "message": "…"}}` | `POST /api/rigor/verify` refused the body — one of `invalid_date`, `duplicate_date`, `unsorted_dates`, `non_finite`, `out_of_range`, `window_too_short`, `too_many_rows`, `trials_out_of_range` | Fix the input and resend. The server does not sort, deduplicate or clip for you; see the section above for what each code means. |
| **429** | `{"detail": {"reason": "generation_daily_cap", "scope": "user", "cap": 10, "message": "…"}}` | Daily generation cap hit, per account (`scope: "user"`) or per IP (`scope: "ip"`) | Wait for the daily reset. Call step 5 **before** step 6 to see this coming; the caps it reports are the caps enforced. |
| **429** | `{"detail": {"reason": "generation_queue_full", "message": "… No payment was taken. …"}}` | The generation wait queue is full | Retry in a few minutes. No payment was taken — admission control runs before the paywall. |
| **429** | `{"detail": "Rate limit exceeded. Please slow down and try again later."}` + `X-RateLimit-*` | Per-route request-rate limit (`/api/generate/start` 5/min, `/api/paper/deployments` 10/min) | Back off. This is requests-per-minute, distinct from the daily cap above — same status, different `detail` shape, different fix. |
| **409** | `{"detail": {"reason": "wallet_link_required", "free_generations_locked_reason": null, "message": "…"}}` | Your account's free generations are used up (#1643) and it has no linked wallet | **Expected on production from generation #4** — do step 6a: `POST /api/wallets/challenge` → `POST /api/wallets/verify` ([`agent-api.md`](agent-api.md#optional-eip-4361-wallet-link)). Funding the wallet currently needs a human at the faucet, so linking an empty one only moves you to the 402. Check `free_generations_remaining` at step 5 first: if it is `null` the ledger was unreadable, not exhausted. |
| **409** | `{"detail": {"reason": "wallet_link_required", "free_generations_locked_reason": "email_unverified", "message": "…"}}` | Your email is not verified, so the free allowance is locked — and the account has no wallet either | **Two ways out, and the message names both.** Verify the emailed link (step 2a; `POST /api/auth/send-verification-email` re-sends it) to unlock the 3 free runs with nothing spent, **or** do step 6a and pay per run. The `reason` is deliberately the same string as the row above so existing clients keep working; branch on `free_generations_locked_reason` to tell the two apart. |
| **404** | `{"detail": "Strategy not found"}` / `{"detail": "Paper deployment not found"}` | Missing **or** not yours — the two are deliberately indistinguishable | Confirm the id came from a call made with this same session. Existence is private; a 404 here is not proof the id is wrong. |
| **503** | `{"detail": {"reason": "payment_config_missing", "message": "…"}}` | Payments are enabled but not fully configured server-side | Not caller-fixable. Retry later; it fails closed rather than letting the request through free. |

---

## Driving this from an MCP client instead

Everything above is the HTTP surface, and it stays the source of truth. If your agent
speaks [MCP](https://modelcontextprotocol.io), [`mcp-server/`](../mcp-server/README.md) in
this repo wraps nine of these routes as tools so you call `archimedes_quote` instead of
writing the `curl`.

**It is a thin client and adds nothing.** No business logic, no database, no Redis, no
wallet key. It can do exactly what an unprivileged HTTP caller can do — the eleven-step
journey above is still the journey, and the steps this server does not cover (sign-up,
sign-in, wallet link, x402 signing, SSE, paper trading) are still done the way this page
documents them. Owner decision **D2** on
[#1653](https://github.com/aprin-labs/archimedes/pull/1653) chose that shape deliberately, and
named the risk it carries: a second surface can drift from the API. The routes each tool
calls are declared in `mcp-server/src/archimedes_mcp/contract.py` and asserted against the
running app — both that they resolve and that each tool's "needs a credential" label is
what the app actually does — by `backend/tests/test_mcp_contract_drift.py`.

### Install and configure

```json
{
  "mcpServers": {
    "archimedes": {
      "command": "archimedes-mcp",
      "env": {
        "ARCHIMEDES_API_URL": "https://archimedes-arc.com",
        "ARCHIMEDES_API_KEY": "archim_..."
      }
    }
  }
}
```

That block is the Claude Desktop / Claude Code shape; other clients take the same three
pieces (a command, an optional env map, stdio transport). Install first — neither
distribution is on PyPI yet, so both come from this repo with
`pip install -e ./cli -e ./mcp-server`.

`ARCHIMEDES_API_KEY` is optional and is sent as `Authorization: Bearer <key>`. Without it
the server falls back to the session cookie `archimedes login` caches at
`~/.config/archimedes/session.json` (mode `600`), which is the same credential step 3
produces. **Exactly one of the two goes on the wire** — when both exist the key wins in the
client and no cookie is sent, so the server-side precedence rule can never decide which
account a call acts as. Neither credential is ever logged, returned, or rendered.

**Running a fleet on one machine?** Add `ARCHIMEDES_SESSION_FILE` to that `env` block —
one path per agent — and pass `archimedes login --session-file` the same path. One session
file shared between two agents is one identity shared between two agents: the second
`login` wins and the first agent keeps working, as somebody else
([#1752](https://github.com/aprin-labs/archimedes/issues/1752)).

**One honest caveat about the key.** Scoped API keys are owner decision **D3** on the same
PR and are not on `main` at the time of writing, so against production today a bearer key
`401`s and the cookie is the working lane. The header is written to the agreed shape now so
nothing changes here on the day that merges.

### The tools

| Tool | Route on this page | Credential | Cost |
|---|---|---|---|
| `archimedes_quote` | `GET /api/generate/quote` (step 1) | no | free |
| `archimedes_usage` | `GET /api/account/usage` (step 5) | yes | free |
| `archimedes_rigor_verify` | `POST /api/rigor/verify` | yes | free, 5/min |
| `archimedes_generate_start` | `POST /api/generate/start` (step 6) | yes | **charges — see step 1** |
| `archimedes_generate_status` | `GET /api/generate/jobs/{job_id}` (step 7b) | yes | free |
| `archimedes_strategy` | `GET /api/strategies/{strategy_id}` (step 9) | no | free |
| `archimedes_passport` | `GET /api/strategies/passports/{strategy_id}` | no | free — the stored verdict + its provenance |
| `archimedes_leaderboard` | `GET /api/leaderboard` | no | free |
| `archimedes_corpus_search` | `GET /api/papers/` | no | free |

Every tool returns a JSON object whose first field is `ok`. A failure is a **result**, not
a protocol error: `{"ok": false, "error": "payment_required", "message": …, "remedy": …,
"quote": {…}}`. The paywall, the wallet gate and the quotas above arrive intact and
un-retried, because they are answers your agent has to act on — and because a protocol
error would collapse a `402` to a string, losing the price, chain and recipient it carries.

**The server does not sign payments.** It holds no wallet key. A `402` comes back with the
quote attached and step 6b is still yours to perform.

## Anti-goals for an agent driving this API

- **Do not assume generation is free, and do not assume it is paid.** Both are true of some
  host. `GET /api/generate/quote` is the only authority, it is public, and it costs you
  nothing to ask — the source defaults disagree with production on purpose, so reading the
  code instead of the endpoint gets you the wrong answer.
- **Do not assume the free tier is yours before step 5 says so.** The allowance unlocks on
  a verified email; `free_generations_remaining: 3` alongside
  `free_generations_locked_reason: "email_unverified"` means three runs are waiting, not
  three runs available. If you have no inbox to verify with, budget for the wallet path
  from generation #1 rather than discovering it at the 409.
- **Do not treat an API key as a lighter-weight credential.** It is the full account. Put
  it in an environment variable or a secret store, never in a URL, a query string, a
  committed file, or a log line — the server never logs it and neither should you. If it
  leaks, `DELETE /api/account/keys/{key_id}` ends it on the next request.
- **Do not expect a key to get you past anything.** Same caps, same rate limits, same
  paywall, same wallet precondition. If you are looking for a way around the 402, the key
  is not it.
- **Do not re-sign an x402 payment to retry.** A fresh signature is a fresh real charge.
  Carry an `Idempotency-Key`, and let an undelivered run's credit pay for the next attempt.
- **Do not treat a `pending` or `fail` rigor gate as a pass.** A gate that never says no is
  not a gate; this one says no, on real strategies, on purpose.
- **Do not confuse the two verdicts.** Step 8's is generation-time; step 9's is the stored
  verdict of record. Cite step 9.
- **Do not assume a paper deployment is an on-chain position.** It is simulated: no chain,
  no funds, no gas. The real vault is `POST /api/vaults/create`, needs a linked wallet, and
  is documented in [`agent-api.md`](agent-api.md#deploy--create-a-vault-from-the-generated-strategy).
- **Do not read `erc8004` as an identity claim.** `status: registration_pending` means no
  `register()` transaction has been confirmed for this deployment's wallet. Read
  `erc8004_verification.source` alongside it — `unavailable` means nobody could ask the
  registry, which is not the same as an answer. See
  [`ui/public/.well-known/agent-registration.json`](../ui/public/.well-known/agent-registration.json),
  [`scripts/register_erc8004_identity.py`](../scripts/register_erc8004_identity.py), and the
  [registration runbook](runbooks/erc8004-identity-registration.md).

## Where this page's claims come from

Every route string here is asserted against the running app's route table by
`backend/tests/test_agent_quickstart_drift.py` — a documented endpoint that 404s is worse
than an undocumented one, so the drift fails CI instead of an agent's retry loop. The
response shapes were read off the routes that build them:
`api/generate_routes.py`, `api/paper_routes.py`, `api/strategies_routes.py`,
`api/account_usage_routes.py`, `api/account_auth.py`, `api/wallet_routes.py`,
`services/generation_payment.py`, `services/generation_credits.py`,
`services/generation_quota.py`, the credit-vs-refund decision in
[`adr/generation-payment-credit-not-refund.md`](adr/generation-payment-credit-not-refund.md),
and the Better Auth sidecar contract in
[`api/auth-and-accounts.md`](api/auth-and-accounts.md).

**Step 1's values are the exception to that rule, and are sourced differently.** They are
a live reading of `GET https://archimedes-arc.com/api/generate/quote` taken on 2026-08-30,
not a transcription of `generation_payment.py`'s defaults — those defaults say the paywall
is off and dry-run is on, which is the opposite of what production serves. No CI check can
pin a deployed flag from inside the repo, so that block is dated on purpose: **re-read the
endpoint rather than trusting the date.**
