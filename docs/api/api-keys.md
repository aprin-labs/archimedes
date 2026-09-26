# API Keys

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31

`/api/account/keys/*` — a long-lived bearer credential for machine callers, and the
second way (beside the Better Auth session cookie) to authenticate as an account.

**Why this exists.** Before it, the only credential a CI job or agent runner could hold
was the `better-auth.session_token` cookie, and the only way to obtain or refresh one was
`POST /api/auth/sign-in/email` **with the account password in the body**, every seven days
when the cookie expires. That makes the machine credential *the human's password*: it
cannot be scoped, it cannot be revoked without locking the human out, and it goes over the
wire on a weekly cycle. Owner decision **D3** on
[#1653](https://github.com/aprin-labs/archimedes/pull/1653) closed that gap.

## The credential

```
archim_9f3c1a77b204de51_KtQ8yv…
^^^^^^ ^^^^^^^^^^^^^^^^ ^^^^^^^
|      |                32 random bytes from `secrets`, urlsafe-base64
|      the PUBLIC key id — the database lookup handle. Not a secret.
fixed prefix, so a leaked token is greppable by shape
```

Present it as `Authorization: Bearer archim_…`. `archim_<key_id>` alone is the `prefix`
the list endpoint returns: it identifies a key and cannot be used as one.

## Properties, and where each is enforced

| Property | How |
|---|---|
| **Shown once.** The full token appears only in the `POST` response body. | The server stores a per-key salted SHA-256 of the secret and nothing else, so no one — including operators — can read the token back. Lose it → revoke and mint another. |
| **Constant-time verification**, on the hit path *and* the miss path. | `hmac.compare_digest`, the same primitive the `internal-key` guard uses. An unknown key id still performs a full comparison against a dummy digest, so response timing cannot be used to enumerate key ids. |
| **Immediate revocation.** | `revoked_at` is read from the row on every request. No cache, no TTL, no grace period — the next call with a revoked key is `401`. |
| **Scoped to one account.** | Every query is filtered by `user_id`. Another account's key id answers `404`, never `403`: a `403` would confirm the id exists. |
| **Never a bypass.** | The key is resolved into the same `CurrentUser` a cookie resolves to, at the same chokepoint (`api/account_auth.py`), so daily quotas, per-route rate limits, the x402 paywall and the wallet precondition all apply identically. A keyed `POST /api/generate/start` returns the same `402`, with the same quote, that a cookie call returns. No route knows keys exist. |
| **A key cannot manage keys.** | All three routes below require `account-session` **specifically** and answer `403` to a bearer key. Containment: a leaked key must not be able to mint successors that outlive revoking the one you know about. The cost is real — a fully unattended agent cannot rotate its own key. |
| **Never logged.** | No log line in `api/api_key_auth.py`, `api/api_key_routes.py`, or `models/api_key.py` takes a token, a secret, or a digest. The public key id *is* logged, deliberately: that is how an operator ties an audit line to a row without the line being a credential. |

Auth levels used below are defined in [`README.md`](README.md#auth-model). Note that
`account-session` there now means "a live Better Auth session **or** a valid API key" for
every other route in this directory — these three are the sole exception.

---

### POST /api/account/keys
Mint a key for the calling account. | **Auth**: `account-session` — **cookie only**, an
API key gets `403` | **Flags**: rate-limited 10/hour; max 25 live keys per account

Request: JSON body `{name: str}` (1–64 characters, non-blank after trimming).
Response `201`: `{id, name, prefix, created_at, last_used_at, revoked_at, key}` — `key` is
the full token and **this is the only response in the system that contains one**.
Errors: `401` no credential · `403` presented an API key rather than a session ·
`409 api_key_limit_reached` already holding 25 live keys · `422` blank or over-length name.

```bash
curl -sS -b /tmp/agora.jar -X POST http://localhost:8080/api/account/keys \
  -H 'Content-Type: application/json' \
  -d '{"name":"ci-nightly"}'
```

### GET /api/account/keys
List the calling account's keys, newest first. | **Auth**: `account-session` — **cookie
only**, an API key gets `403` | **Flags**: none

Request: no parameters.
Response `200`: a list of `{id, name, prefix, created_at, last_used_at, revoked_at}`.
**Never contains a token** — the response model has no field for one and the record it is
built from holds no token. `last_used_at` is coarsened to the minute (a write per request
would put a database `UPDATE` on the hot path of every authenticated agent call).
Revoked keys are retained and returned with a `revoked_at`, as an audit trail.
Errors: `401` no credential · `403` presented an API key rather than a session.

```bash
curl -sS -b /tmp/agora.jar http://localhost:8080/api/account/keys
```

### DELETE /api/account/keys/{key_id}
Revoke a key. | **Auth**: `account-session` — **cookie only**, an API key gets `403` |
**Flags**: none

Request: path `key_id` — the `id` from the create or list response.
Response `204`: no body. Idempotent — revoking an already-revoked key is also `204`, so a
retried automation is not punished for it. Effective on the **next** request that presents
the key.
Errors: `401` no credential · `403` presented an API key rather than a session · `404` no
such key **or** it belongs to another account (the two are deliberately the same answer).

```bash
curl -sS -b /tmp/agora.jar -X DELETE http://localhost:8080/api/account/keys/$KEY_ID
```

---

## Telemetry

A keyed caller classifies as `agent_type: "keyed"` in
[`leaderboard-and-metrics.md`](leaderboard-and-metrics.md)'s funnel breakdown — an
*identity*, unlike `external`, which is a User-Agent guess about an unauthenticated
client. This matters for reading the numbers: before this credential existed, the
classifier resolved any account session to `human` before any agent heuristic ran, so an
authenticated agent was indistinguishable from a person. Funnel readings taken before the
key lane cannot be compared with readings taken after it.

See also: [`../agent-quickstart.md`](../agent-quickstart.md) step 4b (the walkthrough),
[`../agent-api.md`](../agent-api.md#api-keys-bearer) (the agent-facing summary), and
[`auth-and-accounts.md`](auth-and-accounts.md) (the cookie credential these sit beside).
