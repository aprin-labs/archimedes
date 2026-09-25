# `docs/api/` — API Reference

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-20

Per-surface HTTP API reference for the FastAPI backend (and the colocated
Better Auth Node sidecar). Each doc below covers one capability area: what
each route does, its exact auth requirement, request/response shapes, every
error it can raise and why, and a runnable `curl` example. This index is the
entry point [`docs/doc-index.md`](../doc-index.md) links to — every file here must
be reachable from the table below, per the repo's index rule.

## Live interactive docs (Swagger / `/docs`)

FastAPI's own interactive docs (`/docs`, `/openapi.json`) are **disabled in
production** by default: `main.py` gates them off whenever `PUBLIC_DOMAIN` is
set (the app's standard "am I in production" signal) unless
`ENABLE_API_DOCS=1` is also set, in which case they re-enable in any
environment. Local `docker-compose` does not set `PUBLIC_DOMAIN`, so `/docs`
is served by default on a local stack — no flag needed. This written
reference is therefore the canonical API surface for production; treat it as
authoritative over any live `/docs` instance you happen to have access to.

## Auth model

Every route in every doc below states its auth requirement using one of
these five levels:

| Level | Requirement | Failure mode |
|---|---|---|
| `anonymous` | Nothing. No cookie, no header. | N/A — never 401s on auth grounds. |
| `account-session` | An authenticated account, established by **either** credential: the `better-auth.session_token` cookie (verified by FastAPI against the colocated Better Auth sidecar's `GET /api/auth/get-session` on every request), **or** an `Authorization: Bearer archim_…` API key (see [`api-keys.md`](api-keys.md)). Both resolve to the same canonical user at the same chokepoint, so no route distinguishes them — a key is a credential, never a bypass. The three key-management routes are the sole exception and require the cookie specifically. | `401` with no/expired session and no valid key. |
| `linked-wallet` | An `account-session`, **plus** a wallet verified-linked to that account (`require_linked_wallet`). | `401` with no session; `403` with a session but no linked wallet. |
| `platform-admin` | A signed-in account that is a platform admin — listed in `PLATFORM_ADMIN_ACCOUNTS` (canonical `auth_users.id`/email), **or** holding a linked wallet listed in `PLATFORM_ADMIN_WALLETS`. Keyed on the account, never on the request's `X-Wallet-Address` header (#1648). Grants **no fund/custody/treasury authority** — it is a read gate on the internal cost/ops dashboard, nothing more. | `401` no session; `403` signed-in non-admin. |
| `internal-key` | A matching `X-Internal-Agent-Key` header, compared with `hmac.compare_digest` against `INTERNAL_AGENT_API_KEY`. Fails closed (rejects everyone) if that env var is unset. Used only by internal services (the agent runner) — never reachable from the browser UI. | `403` on any missing/wrong key. |

Each level nests into the one above it (`linked-wallet` implies
`account-session`; `platform-admin` implies `linked-wallet`) except
`internal-key`, which is a separate, service-to-service credential.

## Index

### Identity & accounts

| Doc | Covers |
|---|---|
| [`auth-and-accounts.md`](auth-and-accounts.md) | The Better Auth sidecar (`/api/auth/*`): email/password + OAuth sign-up/sign-in, session lookup, email verification. |
| [`api-keys.md`](api-keys.md) | `/api/account/keys/*` — mint, list, and revoke the bearer API keys that let a machine caller authenticate as an account without a cookie jar. |
| [`wallets.md`](wallets.md) | `/api/wallets/*` — EIP-4361 wallet-link challenge/verify, linking a wallet to an already-signed-in account. |

### Generation & rigor

| Doc | Covers |
|---|---|
| [`generation.md`](generation.md) | `/api/generate/*` — the debate-society generation pipeline: job-based start/stream/poll, the x402 payment gate, and daily quotas. |
| [`strategies-and-rigor.md`](strategies-and-rigor.md) | `/api/strategies/*` and `/api/selection-bias/*` — the strategy library, stress testing, and the DSR/PBO/OOS rigor gate. |

### Trading & marketplace

| Doc | Covers |
|---|---|
| [`paper-trading.md`](paper-trading.md) | `/api/paper/*` — deploy a strategy to an append-only, never-rewritten forward-return ledger. |
| [`vaults-and-chain.md`](vaults-and-chain.md) | `/api/vaults/*`, `/api/traces/*`, `/api/swap/*`, `/api/config/contracts`, and the health/root endpoints — vault creation and metadata, reasoning-trace publish/verify, the AMM swap preview, contract addresses. |

### Platform metrics

| Doc | Covers |
|---|---|
| [`leaderboard-and-metrics.md`](leaderboard-and-metrics.md) | `/api/leaderboard` (own-vs-curated strategy ranking) and the public, PII-free `/api/metrics/*` traction surface. |
| [`admin-private.md`](admin-private.md) | `/api/metrics/private/*` — the platform-admin-gated cost/ops dashboard and per-wallet identity roster. |

---

Conventions for this directory follow [`../CONVENTIONS.md`](../CONVENTIONS.md)
— front matter, staleness, and where a new API doc belongs. Adding a route
to any router above without updating its doc in the same commit leaves the
reference silently wrong; adding a new API doc without a row here leaves it
unreachable per the repo's index rule.
