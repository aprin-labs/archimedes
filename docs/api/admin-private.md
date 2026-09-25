# Admin Metrics API

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-31 (admin gate keyed on canonical account identity, #1648)

`/api/metrics/private/*` — the internal cost/ops dashboard: Bedrock/infra spend (currently
draft placeholders), the measured per-generation cost (#1217), the current-schema
engagement/adoption dashboard-v2 tiles, the
admin-gate probe the frontend uses, and the per-wallet identity roster that the public,
PII-free `GET /api/metrics` deliberately does not expose. Landed in #1373 (closing #1366)
after a full-tree audit found `GET /api/metrics/wallets` and `GET
/api/metrics/wallets/connections` serving the complete per-wallet address list to
**anonymous** callers in production — a per-identity roster is not aggregate traction data.
Those two routes were removed from the public router entirely (the old paths now `404`,
they do not redirect) and re-mounted here behind an admin gate.

**Owner directive (2026-08-20, supersedes issue #1028 D8 "public Insights page"):**
`/app/insights` moved from the public app surface to ADMIN-ONLY (platform-admin accounts —
see the auth model below). `GET /whoami` below is the server-truth gate the
frontend probes on entry; `GET /engagement` is the new dashboard-v2 content. See
`ui/src/adminProbe.js`, `ui/src/App.jsx`, and `ui/src/components/Insights.jsx`.

**Auth model — the platform-admin gate is keyed on the ACCOUNT (#1648).** Every route in
this router shares one dependency chain, applied at the router level
(`metrics_private_router = APIRouter(..., dependencies=[Depends(require_platform_admin)])`),
so the same check order runs before any handler code executes:

1. **Is there a session at all?** `require_platform_admin` depends on `require_current_user`,
   which resolves the Better Auth session. No session → **`401`** "Authentication required".
2. **Is that ACCOUNT a platform admin?** `services/platform_admin.resolve_platform_admin`
   answers from server-side state only, by either of two paths:
   - the account's canonical id or email is listed in **`PLATFORM_ADMIN_ACCOUNTS`** (ids
     match case-sensitively, emails case-insensitively); **or**
   - any wallet in **the account's own linked-wallet set** — looked up by `user_id`, not
     from any request header — is listed in **`PLATFORM_ADMIN_WALLETS`**
     (comma/whitespace-separated, parsed case-insensitively, empty by default).

   Neither → **`403`** "Admin access required."

So in practice: **anonymous → 401; any signed-in non-admin → 403; only an admin account →
200.** There is now exactly one 403 message. Before #1648 there were two ("A verified
linked wallet is required" as a *precondition*, then "Admin access required."); with the
account as the key, having a linked wallet is no longer a precondition that can fail on its
own — an account listed in `PLATFORM_ADMIN_ACCOUNTS` is an admin with no wallet at all.
One message also discloses less about why a request was refused.

Neither allowlist grants **any fund / custody / treasury authority** — they are a read gate
on this dashboard (plus one narrow publish exception for example strategies,
`wallet_can_publish` in `models/strategy_generators.py`, which still reads
`PLATFORM_ADMIN_WALLETS` per-wallet and is unchanged), nothing more.

The wallet-roster routes were moved here, not merely re-gated, specifically because a
per-wallet address list is individually identity-bearing and permanently linkable to
on-chain activity — the module docstring is explicit that "any linked wallet is not an
appropriate bar for it," which is why the gate is admin membership and not merely "has a
linked wallet."

**The self-lockout this replaced (was: "round 3 note", 2026-08-20 — FIXED by #1648).**
Admin used to be resolved from `X-Wallet-Address`, which `ui/src/api.js` attaches to every
`apiGet` from whatever the browser extension has selected in that tab. The gate therefore
keyed off the browser, not off who was signed in: the same owner account saw Insights in
one browser and the bare not-found page in the next, with no on-page explanation (by
design — the page must never advertise that it exists). The documented recovery was to
switch or disconnect wallets in the extension. That is gone: the header is not read on this
path at all, so it can neither grant admin nor revoke it. Regression coverage:
`backend/tests/test_platform_admin_gate.py`.

**Migration — what to do with `PLATFORM_ADMIN_WALLETS`.** Nothing is required: it keeps
working exactly as before as evidence, so an existing deploy needs no config change. To go
further and pin admin to the account (survives unlinking or rotating a wallet, and is
answered without touching the database — the break-glass during a datastore incident):

```bash
# read-only; prints the PLATFORM_ADMIN_ACCOUNTS line implied by the current wallet list,
# and names any admin wallet that no account has linked
"$ENV_BIN/python" backend/scripts/derive_platform_admin_accounts.py
```

Then set `PLATFORM_ADMIN_ACCOUNTS` (`TF_VAR_platform_admin_accounts` for Fargate —
`infra/variables.tf`, and note the same "re-pass on every apply or it silently empties"
drift gotcha the wallet variable carries). Keeping both set is fine and is the recommended
end state: either path alone grants admin.

To check where you stand, without guessing from the UI:
```bash
curl -sS -b session.jar https://<host>/api/metrics/private/whoami
```
`401` → you are not signed in. `403` + `"Admin access required."` → signed in, but this
account is on neither allowlist (and no wallet it has linked is either). `200` → you're in;
`wallet` names the linked wallet that evidenced the grant, or is `null` when the grant came
from `PLATFORM_ADMIN_ACCOUNTS` with no admin wallet linked.

---

### GET /api/metrics/private/whoami
Admin-gate probe. | **Auth**: platform-admin | **Flags**: `PLATFORM_ADMIN_ACCOUNTS` /
`PLATFORM_ADMIN_WALLETS` env allowlists

Request: none.
Response: `{admin: true, wallet: str|null}` — always `admin: true` on `200`; there is no
`admin: false` 200 response, non-admin is always a `401`/`403` (see below). `wallet` is the
allowlisted linked wallet that evidenced the grant, reported for provenance and never the
address the caller's own header named; it is `null` for a `PLATFORM_ADMIN_ACCOUNTS` grant
with no admin wallet linked (#1648).
Errors: `401` — no session. `403` — signed in, but not an admin account (one message,
`"Admin access required."`).

The frontend calls this on entry to `/app/insights`, before rendering anything
Insights-shaped, and to decide whether the Ops nav item renders at all
(`ui/src/adminProbe.js`, cached ~30s and shared across both call sites). A denied probe
renders the identical "page not found" treatment an unknown route gets
(`ui/src/components/NotFound.jsx`) — the page does not exist for a non-admin/anonymous
visitor, not merely "access denied", so it is never advertised.

```bash
curl -sS -b /tmp/session.jar http://localhost:8080/api/metrics/private/whoami
```

### GET /api/metrics/private/engagement
Dashboard-v2 engagement/adoption tiles. | **Auth**: platform-admin | **Flags**:
`PLATFORM_ADMIN_ACCOUNTS` / `PLATFORM_ADMIN_WALLETS` env allowlists

Request: none.
Response:
```
{
  accounts: {total: int|null, new_7d: int|null, new_30d: int|null, unavailable?: true},
  linked_wallets: {total: int|null, unavailable?: true},
  strategies: {total: int|null, new_7d: int|null,
                daily_new: [{date: "YYYY-MM-DD", count: int}, ...7]|null,
                note: str|null, unavailable?: true},
  generation_costs: {measured_count: int|null, rows_total: int|null,
                      calls_missing_usage: int|null, usage_complete: bool|null,
                      total_input_tokens: int|null, total_output_tokens: int|null,
                      total_tokens: int|null, note: str|null, unavailable?: true},
  paper_deployments: {active: int|null, stopped: int|null, unavailable?: true},
  repeat_generation_users: {generating_users: int|null, repeat_users: int|null,
                             note: str|null, unavailable?: true},
  payments: {dry_run: bool, settled_volume_usd: null, note: str},
  authenticated_wallet: str|null,   # evidence wallet, null for an account-allowlist grant (#1648)
  timestamp: str,
}
```
Errors: `401` / `403` — same admin-gate semantics as `/cost`, above.

Every field is a real query against an existing table (`services/engagement_metrics.py`) —
no sampling, no estimation. `repeat_generation_users` is scoped to `strategy_store` rows
with a non-NULL `owner_user_id` (the real FK to `auth_users.id`) AND `is_example = false`
(round 4 fix — the same platform-seeded-curated-row exclusion `strategies` already applies,
so the two tiles read the same population); pre-account, wallet-only generations are
excluded from both `generating_users` and `repeat_users` rather than silently rounded into
either bucket.

**Fail-soft shape (round 4 correction — this section previously documented the pre-fix
behavior).** A DB error on any ONE sub-metric degrades that tile's fields to `null`
(never a fabricated `0`) plus `unavailable: true`, without blanking the rest of the
response — matching `services/engagement_metrics.py`'s module docstring exactly: "the
degraded shape on failure is `None` per numeric field, never `0`... a count of zero is a
CLAIM..., not an absence." Earlier text here said a failed tile degrades to "its own zero
shape," which was never true after round 2's fix and directly contradicted that docstring.

**`payments.settled_volume_usd` is always `null` today (round 4 correction — this
paragraph previously re-asserted the pre-fix "no durable record" claim).** The narrower,
current truth: `settlement_intents` (`models/marketplace.py`) IS a real, durable table
whose `status` column DOES reach `"settled"`/`"failed"` via
`marketplace/service.py`'s `_finalize_settlement_intent` — the settlement EVENT is
durably recorded. What is actually missing is narrower: its `amount_usdc` column is
declared but never written by any code path (`grep -rn amount_usdc backend/archimedes/`
returns only the declaration), so there is no per-settlement dollar amount to sum even
once `PAYMENTS_DRY_RUN=false`. `settled_volume_usd` stays `null` rather than `0` either
way — "not yet metered," never "measured at zero" — and is the real field name settlement
wiring will populate once `amount_usdc` is written.

**`strategies`/`repeat_generation_users` count distinct stored content, not generation
events (round 3 correction).** `upsert_strategy`'s content-hash dedup means two different
users generating identical output — or one user regenerating — produces ONE
`strategy_store` row, so `strategies.total`/`new_7d`/`daily_new` and
`repeat_generation_users` are both proxies for distinct stored content, not an exact
per-run or per-user generation count; `strategies.note` states this explicitly and is
rendered on the page rather than left implicit.

**`generation_costs`'s completeness fields — TRUE accumulator semantics (round 4
correction — this paragraph previously said `calls_missing_usage` is "summed across every
row," which was never accurate once dedup and corrupt-row `continue` guards are accounted
for).** `measured_count` only counts rows whose LLM usage accounting is complete
(`calls_missing_usage == 0` for that job) — a row where some calls in the job reported no
usage is NOT counted as measured, even though its available tokens still contribute to the
totals. `calls_missing_usage` is the sum of TWO different kinds of "missing," not a plain
per-row sum: (1) each qualifying row's own self-reported `llm.calls_missing_usage` value,
for rows that decoded AND carried an `llm` block; PLUS (2) exactly `+1` for every row whose
JSON failed to decode, or that decoded with no `llm` block at all — a row this broken
cannot even report how many of its calls are missing, so it counts as *at least one*
rather than zero (before round 4, such a row contributed NOTHING, so `usage_complete`
could read `true` — "a complete accounting" — while an entire row's usage was silently
unaccounted for: the single most incomplete case, reported as the least). Both kinds are
de-duplicated by `job_id` before summing — `generation_costs` is K=1 today (one row per
job), so this is currently a no-op, but it prevents a future K>1 from silently
double- (or under-) counting a job's figures once per persisted strategy. `usage_complete`
is `true` iff the resulting total is zero; when `false`, `total_tokens` is a real but
*undercounting* total, the same "partial ≠ absent, but still not a plain measured number"
distinction `payments.settled_volume_usd` makes.

**`generation_costs.total_tokens` is NOT an all-time platform total (round 4 addition —
new `note` field).** A row here exists only for a job that persisted at least one
`strategy_store` row (`agents/generation_pipeline.py`'s `_persist_generation_cost`: "No
strategy row ⇒ nothing to key a durable record to, so nothing is written"). A generation
that consumed real LLM tokens but errored, was cancelled, or failed the rigor gate before
producing a strategy leaves NO row at all — not a corrupt one, not a zero one, simply
absent from this table. `total_tokens` sums every row this table HAS (no LIMIT — a LIMIT
would under-report even that), which is an honest count of tokens for **measured,
strategy-producing jobs**, never a platform-wide total; `generation_costs.note` states this
explicitly and `Insights.jsx` labels the tile "LLM tokens (measured jobs)" rather than
"Total LLM tokens" for the same reason.

```bash
curl -sS -b /tmp/session.jar http://localhost:8080/api/metrics/private/engagement
```

### GET /api/metrics/private/cost
Account + admin cost/ops dashboard. | **Auth**: platform-admin | **Flags**:
`PLATFORM_ADMIN_ACCOUNTS` / `PLATFORM_ADMIN_WALLETS` env allowlists

Request: none.
Response: `{source: "draft", real_users: int, bedrock_monthly_usd: null,
bedrock_daily_usd: null, infra_monthly_usd: null, cost_per_user_usd: null,
cost_per_generation_usd: str|null, generation_cost: {...}, note: str,
authenticated_wallet: str|null, timestamp: str}` (`authenticated_wallet` is the evidence wallet, null for an account-allowlist grant — #1648).
Errors: `401` — no session. `403` — session without a linked wallet, or a linked wallet
not on the admin allowlist (see the two-flavor 403 note above).

**Two provenances on one payload, labelled separately.** The AWS-billing fields
(`bedrock_*`, `infra_monthly_usd`, `cost_per_user_usd`) are explicit `null` placeholders —
**not live-metered spend**; the AWS Cost Explorer + Bedrock token-metering wiring is
roadmap work. That is what the top-level `source: "draft"` describes.

`cost_per_generation_usd` is **not** one of them (#1217). It is the mean of the
`generation_costs` measurements this platform actually recorded, priced against the
`GENERATION_COST_RATE_CARD` environment rate card, with the full distribution, the
LLM-vs-compute split, the per-`n_candidates` scaling breakdown and the unpriceable tally
under `generation_cost`. It is `null` — never `0` — when no rate card is configured or no
run was priceable, and `generation_cost.rate_card_configured` /
`.unpriceable_reasons` / `.unavailable` say which. Shape and refusals:
[`generation-cost-instrumentation.md`](../generation-cost-instrumentation.md) §
Pricing the measurement.

`real_users` (canonical Better Auth account count) is read live, so any per-user math a
consumer does downstream is anchored to an honest denominator rather than the cumulative
request tallies on the public `GET /api/metrics` (issue #830). This endpoint is the only
surface carrying the priced per-generation figure; the public metrics family stays
aggregate and unpriced.

```bash
curl -sS -b /tmp/session.jar http://localhost:8080/api/metrics/private/cost
```

### GET /api/metrics/private/wallets
Enumerate legacy verified human wallets. | **Auth**: platform-admin | **Flags**:
`PLATFORM_ADMIN_WALLETS` env allowlist

Request: none.
Response: `{real_users: int, wallets: [{wallet_address, actor_class, first_seen_at,
last_auth_at}], timestamp: str}` — `real_users` here means the wallet count on *this*
endpoint (kept for issue #1028 AC1 response-shape compatibility), not the canonical
account count; don't conflate it with `/private/cost`'s `real_users`.
Errors: `401` / `403` — same admin-gate semantics as `/cost`, above.

Fail-safe: an empty list / zero on a DB read error, never a `500`.

```bash
curl -sS -b /tmp/session.jar http://localhost:8080/api/metrics/private/wallets
```

### GET /api/metrics/private/wallets/connections
"Which wallets connected, and when." | **Auth**: platform-admin | **Flags**:
`PLATFORM_ADMIN_WALLETS` env allowlist

Request: none.
Response: `{count: int, connections: [{wallet: str, connected_at: str}], timestamp: str}`
— one row per wallet, `connected_at = min(occurred_at)` over that wallet's
`identity_events` rows where `event_type='auth_verified'`, earliest first.
Errors: `401` / `403` — same admin-gate semantics as `/cost`, above.

This query was impossible before the identity ledger existed: the pre-Better-Auth
SIWE-verify path discarded the wallet into a stateless cookie with no durable write
(issue #1028 AC2). Fail-safe: an empty list on a DB read error.

```bash
curl -sS -b /tmp/session.jar http://localhost:8080/api/metrics/private/wallets/connections
```

---

**Admin UI (2026-08-20).** `ui/src/components/Insights.jsx` is now the admin-only
`/app/insights` page's content — but the gate itself lives one level up, in
`ui/src/App.jsx`: it probes `GET /whoami` before Insights.jsx ever mounts, and only
renders the component once that probe resolves `admin === true`. Insights.jsx itself never
imports the probe; once mounted it renders `GET /engagement`'s tiles.
`GET /api/metrics/private/cost` is still deliberately NOT rendered anywhere in that
component — its own header comment explains why: every field is a draft placeholder
pending the live AWS Cost Explorer + Bedrock token-metering wiring (roadmap), not something
this page should present as a real number. `/wallets` and `/wallets/connections` also have
no frontend consumer yet — reach them directly (curl / an API client with a session
cookie).

See also: `.env.example` (`PLATFORM_ADMIN_WALLETS` documentation block) and
`docs/security/auth-model.md`.
