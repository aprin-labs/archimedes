# API Surface Status

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-20
> **superseded-by:** —

One row per router `backend/archimedes/main.py` actually mounts via
`app.include_router(...)`, derived directly from that block (currently lines
520-555 — read the live file, this doc is not the source of truth for line
numbers). This is a **census**, not a tutorial: it exists so a router can
never be registered without a corresponding row, enforced by
[`backend/tests/test_api_surface_status.py`](../backend/tests/test_api_surface_status.py),
which parses this table and the live `include_router` set and fails the
suite the moment they diverge — add a router here, or the build breaks.

For the *detailed* per-route contract (request/response shapes, every error
code, `curl` examples) see [`docs/api/README.md`](api/README.md), which
covers 14 of the 30 rows below. This doc's job is completeness across the
full surface, not depth on any one router — where a detailed doc exists, the
"Documented in" column links to it; where it says `—`, that router has no
per-surface reference doc yet (a real gap, not an oversight to paper over).

## Legend

**Auth model** — this doc's four buckets map onto the five-level scale in
[`docs/api/README.md`](api/README.md#auth-model) (`anonymous`,
`account-session`, `linked-wallet`, `platform-admin`, `internal-key`):

| This doc | Means |
|---|---|
| `public` | Every route in the router is anonymous — no cookie, no header. |
| `session` | Every route requires an authenticated caller (`account-session` and/or `linked-wallet`) — either via a router-level or `include_router`-level dependency, or because every route in the file individually enforces it. |
| `private-admin` | Router-level `platform-admin` gate: session + linked wallet + an admin-wallet allowlist. |
| `internal` | `internal-key` (`X-Internal-Agent-Key`, service-to-service only) — or the whole router is conditionally registered for internal/test use, never reachable in production. |
| `mixed (…)` | Not every route shares one model. The parenthetical names which models are present; see the row's note below the table for the exact split. **A router landing here that should be uniform is itself worth a second look** — most of Archimedes's actual security boundary lives inside these routers, at the route level, not at the `include_router` call. |

**Status**

| This doc | Means |
|---|---|
| `live` | Registered unconditionally; every route serves real data against real dependencies, no documented degraded path. |
| `gated` | Registration itself is conditional — an env var or an import guard decides whether the router mounts at all. |
| `degraded-capable` | Always registered, but has a documented fail-soft path where some or all routes intentionally return a non-fatal degraded response (503, an honest `degraded` flag, a labeled placeholder) instead of crashing or fabricating a number. |

## Router census

| Prefix | Router | Auth model | Status | Documented in |
|---|---|---|---|---|
| `/api/assets` | `archimedes.api.assets_routes.assets_router` | public | live | — |
| `/api/agent` | `archimedes.api.agent_manifest_routes.agent_manifest_router` | public | live | — |
| `/api/vaults` | `archimedes.api.vaults_routes.vaults_router` | mixed (public / session) | live | [`api/vaults-and-chain.md`](api/vaults-and-chain.md) |
| `/api/strategies` | `archimedes.api.strategies_routes.strategies_router` | mixed (public / session) | live | [`api/strategies-and-rigor.md`](api/strategies-and-rigor.md) |
| `/api/traces` | `archimedes.api.traces_routes.traces_router` | mixed (public / internal) | live | [`api/vaults-and-chain.md`](api/vaults-and-chain.md) |
| `/api/regime` | `archimedes.api.regime_routes.regime_router` | public | live | — |
| `/api/swap` | `archimedes.api.swap_routes.swap_router` | public | live | [`api/vaults-and-chain.md`](api/vaults-and-chain.md) |
| `/api/config` | `archimedes.api.config_routes.config_router` | public | live | [`api/vaults-and-chain.md`](api/vaults-and-chain.md) |
| `/api/agent` | `archimedes.api.agent_routes.agent_router` | mixed (public / internal) | live | — |
| `/api/corpus` | `archimedes.api.corpus_routes.corpus_router` | public | degraded-capable | — |
| `/api/paper` | `archimedes.api.paper_routes.paper_router` | session | live | [`api/paper-trading.md`](api/paper-trading.md) |
| `/api/explore` | `archimedes.api.explore_routes.explore_router` | public | live | — |
| `/api/generate` | `archimedes.api.generate_routes.generate_router` | session | live | [`api/generation.md`](api/generation.md) |
| `/api/generate` | `archimedes.api.generate_routes.generate_public_router` | public | live | [`api/generation.md`](api/generation.md) |
| `/api/marketplace` | `archimedes.api.marketplace_routes.marketplace_router` | mixed (public / session) | gated | — |
| `/api/risk` | `archimedes.api.risk_routes.risk_router` | public | live | — |
| `/api/portfolio` | `archimedes.api.portfolio_routes.portfolio_router` | session | live | — |
| `/api/selection-bias` | `archimedes.api.selection_bias_routes.selection_bias_router` | public | live | [`api/strategies-and-rigor.md`](api/strategies-and-rigor.md) |
| `/api/rigor` | `archimedes.api.rigor_verify_routes.rigor_verify_router` | session | live | — |
| `/api/account` | `archimedes.api.account_usage_routes.account_usage_router` | session | live | — |
| `/api/account/keys` | `archimedes.api.api_key_routes.api_key_router` | session | live | [`api/api-keys.md`](api/api-keys.md) |
| `/api/payments` | `archimedes.api.payment_routes.payment_router` | session | live | [`api/generation.md`](api/generation.md) |
| `/api/papers` | `archimedes.api.papers_routes.papers_router` | public | live | — |
| `/api/user` | `archimedes.api.user_routes.user_router` | session | live | — |
| `/api/wallets` | `archimedes.api.wallet_routes.wallet_router` | session | live | [`api/wallets.md`](api/wallets.md) |
| `/api/auth` | `archimedes.api.auth_siwe.auth_router` | internal | gated | see note — do not confuse with the live surface `api/auth-and-accounts.md` covers |
| `/api/proposals` | `archimedes.api.proposals_routes.proposals_router` | session | live | — |
| `/api/features` | `archimedes.api.features_routes.features_router` | public | live | — |
| `/api` | `archimedes.api.metrics_routes.metrics_router` | public | live | [`api/leaderboard-and-metrics.md`](api/leaderboard-and-metrics.md) |
| `/api/metrics/private` | `archimedes.api.metrics_private_routes.metrics_private_router` | private-admin | degraded-capable | [`api/admin-private.md`](api/admin-private.md) |
| `/api/leaderboard` | `archimedes.api.leaderboard_routes.leaderboard_router` | public | degraded-capable | [`api/leaderboard-and-metrics.md`](api/leaderboard-and-metrics.md) |

31 rows, one per `include_router` call in `main.py` (two calls each mount
`generate_routes.py`'s pair of routers and `auth_siwe.py`'s legacy router
under a shared prefix with a sibling — see notes). 15/31 have a detailed doc
in `docs/api/`; the other 16 are real documentation debt, not an oversight —
tracked here rather than silently absent.

## Notes — mixed auth, degraded paths, and name collisions

- **`vaults_router`** — reads (`GET /`, `/{address}`, `/{address}/health`,
  `/{address}/metadata`) are public. `POST /create`, `POST /metadata`, and
  `POST /{address}/derive-allocations` require `require_linked_wallet`
  (session + a verified linked wallet) — each spends backend-signer gas or
  writes state.
- **`strategies_router`** — curated/generated reads are public, with optional
  personalization via `get_current_user` (never mandatory). `GET /generated`
  and `PATCH /{strategy_id}` require `require_current_user`. **This router
  hosts no generation endpoint.** `POST /generate` and `GET
  /generate/{job_id}` — the second live, LLM-spending generation path flagged in
  [`docs/sprint/cluster-7-ui-surface.md`](sprint/cluster-7-ui-surface.md) — were
  deleted on 2026-08-31; generation is `POST /api/generate/start` only, guarded
  by `backend/tests/test_sole_generation_route_guard.py`. Historic context: it
  never had a UI consumer, and cluster-4 chose to route it through the
  generation quota meter rather than delete it; deletion is the resolution.
- **`api_key_router`** — `session`, and **stricter than every other `session`
  row here.** Since #1653 D3 an `Authorization: Bearer archim_…` API key
  satisfies `require_current_user` everywhere else in this table; these three
  routes require the Better Auth *cookie* specifically
  (`require_session_credential`) and answer `403` to a key. A key that could
  mint keys would issue itself successors, and revoking the token an operator
  knows about would not end the compromise. Shares the `/api/account` prefix
  with `account_usage_router`; the two never collide (`/usage` vs `/keys`).
- **`traces_router`** — reads are public. `POST /publish` requires
  `X-Internal-Agent-Key` (`require_internal_agent_key`) — the agent runner
  only, never the browser.
- **`agent_router`** — `/status`, `/circle-status`, `/health/amm` are public
  system-health reads. `POST /bootstrap-liquidity` requires
  `X-Internal-Agent-Key`. Shares the `/api/agent` prefix with
  `agent_manifest_router` on purpose — same URL space, two different
  "agent" concepts (the on-chain trading agent vs. an external AI agent
  consuming the product); see `agent_manifest_routes.py`'s module docstring.
- **`corpus_router`** — `GET /graph` and the `/kg/*` routes return `503`
  ("pipeline not yet run" / "KG store unavailable") when the knowledge-graph
  pipeline hasn't built, which is the current production state (see
  `corpus_routes.py`'s module docstring for the exact per-route 503 contract).
  This is the backend half of why
  [`docs/sprint/cluster-7-ui-surface.md`](sprint/cluster-7-ui-surface.md)
  hides the Graph/KG tabs client-side — a live `503` inside the product is
  worse than an absent tab.
- **`marketplace_router`** — registration itself is `gated`: `main.py` wraps
  the import in `try`/`except` (fail-soft against `circlekit` failing to
  import in the runtime image, per the PR #958 incident) and sets
  `marketplace_router = None` on failure, so the router — and all nine of
  its routes — can be silently absent in a given deploy. When it is
  registered: `GET /published` and `GET /published/{strategy_id}` are public
  reads; the other seven routes (`publish`, `subscribe`,
  `unsubscribe`, `stop_publish`, `my-published`, `withdraw`,
  `my-subscriptions`) require `require_linked_wallet`.
- **`risk_router`** and **`portfolio_router`** — `GET /risk/cvar`, `GET
  /risk/greeks`, `POST /portfolio/optimize`, and `POST
  /portfolio/parameter-sweep` additionally require the `require_quant_feature`
  entitlement — a feature-flag gate layered on top of (for `portfolio_router`)
  the router-wide session requirement, not a substitute for it.
- **`portfolio_router`, `user_router`, `proposals_router`, `generate_router`**
  — these four are gated **wholesale** at the `app.include_router(...,
  dependencies=[Depends(require_current_user)])` call in `main.py` itself,
  so every route requires a session regardless of what each route's own
  signature does or doesn't declare internally.
- **`metrics_private_router`** — router-level `dependencies=[Depends(require_platform_admin)]`
  (401 anonymous, 403 linked-but-non-admin). `GET /cost`'s fields are
  explicitly labeled `"source": "draft"` — DRAFT placeholders pending live
  Bedrock/infra billing wiring, per the file's own docstring — hence
  `degraded-capable`; `GET /wallets` and `GET /wallets/connections` are live.
- **`leaderboard_router`** — never auth-gated by design (single-user MVP
  pivot; an anonymous caller sees the curated scope, a signed-in caller can
  ask for `scope=own`). `degraded-capable` because it returns an explicit
  `(degraded, degraded_reason)` pair on the wire rather than silently
  rendering an empty board as a real "no results" when the strategy provider
  is unavailable (#1356).
- **`auth_router`** (`archimedes.api.auth_siwe`) — the **legacy SIWE**
  router. `main.py` mounts it only when `os.getenv("TESTING")` is truthy, so
  it is absent in production; it survives so signature-verification
  regression tests can exercise its proof helpers. This is a different
  surface from the live `/api/auth/*` the Better Auth Node sidecar serves
  (documented in [`api/auth-and-accounts.md`](api/auth-and-accounts.md)) —
  same prefix, different process, do not conflate them.
- **`papers_router`** (`archimedes.api.papers_routes`, plural, `/api/papers`)
  is the public arxiv-metadata catalog — not to be confused with
  **`paper_router`** (`archimedes.api.paper_routes`, singular, `/api/paper`),
  the session-gated paper-trading deployment surface. Never reintroduce the
  deleted `/api/papers/corpus/*` endpoints here (issue #201) — any
  knowledge-graph surface belongs to `corpus_router`, backed by real KB
  pipeline output.
