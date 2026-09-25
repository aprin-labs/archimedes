# Leaderboard & Metrics API

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-30

Two surfaces bundled together because they answer the same question — "how
is the platform doing?" — at two different scopes: `/api/leaderboard` ranks
strategies (curated reference set, or the signed-in caller's own generated
strategies), while `/api/metrics/*` reports platform traction. The public
`/api/metrics` router (this doc) is **anonymous and PII-free by design** — no
route on it may carry a wallet-bearing response model, and a test walks the
router to enforce that. The admin-gated wallet roster and cost dashboard live
on a **separate** router, `/api/metrics/private/*`, fully documented in
[`admin-private.md`](admin-private.md) — this doc covers both for
completeness but the detail lives there.

**Auth model.** `GET /api/leaderboard`, `GET /api/metrics`, `GET
/api/metrics/funnel`, `POST /api/metrics/funnel/event`, and `GET
/api/metrics/visitors` are all anonymous — none of them ever 401s, by design
(see each route below for its specific fail-soft behavior). `GET
/api/metrics/private/*` requires platform-admin (see
[`admin-private.md`](admin-private.md)).

## Leaderboard

**Two boards, never blended.** `GET /api/leaderboard` is the RESEARCH board:
its conviction score is built entirely from backtest-era passport fields
(rigor gate, DSR, OOS, PBO), so every row now carries
`performance_basis: "backtest_research"` plus the `backtest_start` /
`backtest_end` window the metrics were measured over.
`GET /api/leaderboard/live-paper` is the FORWARD board: rows only for active
paper deployments that have actually produced ledger observations, ranked on
realised return, each carrying `performance_basis: "live_paper"` and an
inception date. **No endpoint returns a combined score**, and the forward
board never renders a deployment with an empty ledger — see its section
below.

### GET /api/leaderboard
Rank strategies by a transparent, real-data-only conviction score. |
**Auth**: anonymous — never 401s; scope is resolved from whether a session
happens to be present, not required

Request: query `scope: "own"|"curated"|null` (default: `"own"` when signed
in, `"curated"` when anonymous — an anonymous request for `scope=own` is
transparently served `curated` instead; the response's own `scope` field
reports what was actually served, which may differ from what was asked for),
`sort_by: "conviction_score"|"sharpe_ratio"|"cagr"|"sortino_ratio"|"calmar_ratio"|"deflated_sharpe_ratio"|"dsr_p_value"|"out_of_sample_sharpe"|"pbo_score" = "conviction_score"`,
`order: "asc"|"desc" = "desc"`, `regime_tag: "bull"|"bear"|"regime_neutral"|null`,
`min_rigor: bool = false`, `limit: int(1..200) = 50`.
Response (`LeaderboardResponse`): `{entries: [LeaderboardEntry], total: int, performance_basis: "backtest_research", sort_by: str, order: str, scope: str, scoring_engine: LeaderboardScoringEngine, degraded: bool=false, degraded_reason: str=""}` — see [Response shape](#response-shape) below. `degraded` is `true` when the underlying strategy provider raised, or the curated cohort came back empty for a reason other than a legitimate filter (e.g. `"strategy corpus not found in build"`, `"curated strategy cohort is empty"`) — the UI must never render "No strategies match these filters yet." while `degraded` is `true` (#1356).
Errors: none — a data-source failure degrades to an empty board with the
scoring-engine metadata intact, never a 5xx (module docstring: "the page must
never hard-fail, for either scope").

```bash
curl -s "https://archimedes-arc.com/api/leaderboard?scope=curated&sort_by=conviction_score&limit=50"
```

#### `scope=own` vs `scope=curated`

- **`own`** (single-user MVP default when signed in): the caller's own
  generated strategies — published or not — ranked against each other. Never
  includes the curated library (nobody owns it) and never includes another
  user's strategies.
- **`curated`** (default when anonymous): the curated seed library **union**
  every *published* generated strategy across all users — explicitly a
  reference set, never framed as "your competition." Publish status (not
  rigor "live" promotion) is the only qualifying criterion for a generated
  strategy here, because keying off rigor promotion would leak a private
  strategy onto a shared board the moment it passed the gate.

**A global, cross-user ranked cohort is a roadmap concept** (tracked in
#1185) — `scope=curated` is a reference view, not a live competitive
leaderboard; the machinery for a real public board exists server-side but is
not exposed as one yet.

#### Response shape

`LeaderboardEntry` (per entry): `rank, medal: "gold"|"silver"|"bronze"|null,
id, name, creator, conviction_score: float(0-100), score_components:
{gate, dsr_confidence, oos_performance, overfitting_resistance,
data_completeness}` (all `[0,1]`, `None`-backed inputs score `0` and are
reflected in `data_completeness`), `sharpe_ratio, cagr, sortino_ratio,
max_drawdown, calmar_ratio` (validation axis, `null` if not yet evaluated),
`deflated_sharpe_ratio, dsr_p_value` (**a 0–1 confidence — higher is
better — despite the legacy "p_value" name**), `pbo_score,
out_of_sample_sharpe, passes_rigor_gate: bool, is_backtest_placeholder: bool,
forward: {stockbench_status: "pending", stockbench_sortino: float|null,
live_pnl_status: "pending", live_pnl_pct: float|null}` (per-strategy
StockBench and live paper-P&L are not scored INTO this board — always
`"pending"` here; realised forward performance has its own endpoint, see
[live-paper](#get-apileaderboardlive-paper)), `performance_basis:
"backtest_research"`, `backtest_start: str|null, backtest_end: str|null`
(the ISO window these metrics were computed over — `null` on rows that were
never stamped with one, which the UI renders as "window not recorded" rather
than leaving the numbers unqualified), `regime_tag, return_source,
status, papers: [PaperRef]`.

`scoring_engine`: `{weights: dict[str,float], oos_target: float, methodology:
str, validation_axis: "live", forward_axis: "pending", stockbench_global:
{scope: "agent_pipeline_global", sortino, return_pct, max_drawdown_pct, rank,
window, source}, disclaimer: str}` — `stockbench_global` is the one real
StockBench result on hand (the whole agent-pipeline run, Chen et al. 2026),
not a per-strategy number; it is surfaced as honest context, not as evidence
about any individual entry.

**The response's `scope` field is authoritative, not the request's `?scope=`
param** — always read it back rather than assuming the server honored what
was asked for.

### GET /api/leaderboard/live-paper
The FORWARD board: the caller's own active paper deployments, ranked by the
return each has actually realised since it went live. | **Auth**: anonymous —
never 401s, same contract as the conviction board

Request: query `limit: int(1..200) = 50`.
Response (`LivePaperLeaderboardResponse`): `{entries: [LivePaperEntry], total: int, performance_basis: "live_paper", scope: "own"|"anonymous", sort_by: "cumulative_return", order: "desc", as_of: str|null, withheld_no_ledger: int, methodology: str, disclaimer: str, degraded: bool=false, degraded_reason: str=""}`.
`LivePaperEntry`: `{rank, deployment_id, strategy_id, name, performance_basis: "live_paper", cumulative_return: float, days_live: int(≥1), inception_date: str, as_of: str, last_updated: str|null, drift_detected: bool, rigor_gate_status: "pass"|"fail"|"pending"|"degenerate", graded_at: str|null, gate_version: str|null}`.
Errors: none — a DB failure degrades to an empty board with
`degraded_reason: "paper deployments unavailable"`, never a 5xx.

```bash
curl -s --cookie "better-auth.session_token=…" \
  "https://archimedes-arc.com/api/leaderboard/live-paper?limit=50"
```

Five contract points that are load-bearing rather than incidental:

- **A deployment with an empty ledger is never an entry.** Not as a `0.0%`
  row, not as a placeholder holding a rank — it is dropped and counted into
  `withheld_no_ledger`, so the omission is a visible absence rather than a
  silence. `days_live` therefore has a floor of 1 by construction.
- **`cumulative_return` is `∏(1 + daily_return) − 1` over the whole ledger.**
  Never annualised and never extrapolated: over a handful of days an
  annualised figure is a fiction. `as_of` is the LAST ledger observation's
  date — what the number actually reflects — not today.
- **Ownership is `owner_user_id` only** (#850 — a paper track record is
  private). There is no curated or cross-user cohort here; an anonymous
  caller gets `scope: "anonymous"` with `entries: []`, which is an honest
  empty state and distinct from both `degraded` and a signed-in caller with
  nothing deployed.
- **`drift_detected`** mirrors `paper_deployments.drift_detected_at`: a
  replay that disagreed with already-written rows stamps the deployment
  rather than rewriting the append-only ledger, and a drifted track record
  reads as drifted on the board too. See
  [`paper-trading.md`](paper-trading.md).
- **Every row carries the verdict of record** (#1764):
  `rigor_gate_status` — the four-state grade STORED on the strategy's passport
  (`docs/adr/rigor-verdict-of-record.md`) — with `graded_at` and `gate_version`
  as its provenance. Deploying to paper has no rigor precondition, so a
  gate-REJECTED strategy can hold rank 1 here with a real forward return; shown
  bare, that row reads as an endorsement. Three properties keep this from being
  the blended board the split forbids:
    - it is a **label with its date**, not a backtest metric. No DSR, PBO,
      Sharpe or conviction score appears on a forward row, and `sort_by` stays
      `cumulative_return` — the verdict never reorders the board.
    - **`passes_rigor_gate` is deliberately absent** here, though
      `GET /api/paper/deployments` carries it: a bare boolean beside a forward
      return is the field a consumer would blend or sort on, while a dated
      four-state reads as the statement about the BACKTEST that it is.
    - it is a **read, never a recompute** — the same `passport_loader`
      derivation the deployment payload uses, in one batched query per board,
      so this board and the deployment card can never show two verdicts for one
      strategy. A strategy with no passport row, or a passport read that fails,
      reads `pending` with a null `graded_at`; the returns still serve, and no
      row is ever degraded into a `pass`.

## Metrics (public, PII-free)

### GET /api/metrics
Live human-vs-agent traction counters, plus the honest user count. |
**Auth**: anonymous

Request: none.
Response (`MetricsResponse`): `{human_count: int, agent_count: int, total_requests: int, real_users: int|null, epoch_started_at: str|null, epoch_resets: int|null, timestamp: str}`. `human_count`/`agent_count`/`total_requests` are **cumulative per-request tallies (site traffic, not users, not visitors)** since `epoch_started_at`; `real_users` is the canonical Better Auth account count, surfaced alongside so the two can never be conflated (issue #830). `real_users` is `null` (round 4 fix), not a fabricated `0`, when the account-count query itself fails — `services/user_stats.py`'s `get_distinct_user_count_or_none`. Counts are Postgres-snapshotted on every read so a Redis restart does not zero them; `epoch_resets` counts how many Redis resets have been absorbed into the durable total.
Errors: none — always 200. A Redis outage falls back to the last durable Postgres snapshot rather than reporting a false zero.

```bash
curl -s https://archimedes-arc.com/api/metrics
```

### GET /api/metrics/funnel
Conversion funnel — two independently-sourced views selected by `source`. |
**Auth**: anonymous

Request: query `day: str (YYYY-MM-DD)|null` (single-day window; omitted =
all-time; ignored when `source=identity`, which is always queried live),
`source: "visitor"|"identity" = "visitor"`, `human_only: bool = true`
(`source=identity` only).
Response (`FunnelResponse`): `{window: str, source: str, stages: [FunnelStageCount{stage, distinct_visitors, pct_of_landed, step_conversion, by_agent_type: dict[str,int]}], timestamp: str}`.
Errors: none — `source=visitor` fails soft to zeros on a Redis outage; `source=identity` fails soft to an all-zero funnel on any DB error.

Two sources, not two views of the same data:
- **`source=visitor`** (default) — the pre-#1028 anonymous browser-id funnel
  (`landed → wallet_connected → generation_started → vault_deployed`),
  backed by Redis HyperLogLog. Every stage also carries `by_agent_type`
  (`internal`/`keyed`/`external`/`human` breakdown), additive to `distinct_visitors`.
  `keyed` is a caller authenticated by a scoped API key (`Authorization: Bearer
  archim_…`) — an identity, unlike `external`, which is a User-Agent guess about
  an unauthenticated client. Readings that predate the key lane cannot be
  compared with later ones: before it, an authenticated agent classified as
  `human`.
- **`source=identity`** — `wallet_connected → generation_started →
  vault_deployed`, each a live `COUNT(DISTINCT wallet)` over the durable
  `identity_events` ledger, no Redis/HLL involved. `human_only=true`
  (default) excludes `actor_class IN ('operator_dogfood','agent')` so
  dogfooding/agent traffic can't inflate the honest human funnel;
  `by_agent_type` is always empty here (no per-request agent_type on ledger
  rows).

```bash
curl -s "https://archimedes-arc.com/api/metrics/funnel?source=identity&human_only=true"
```

### POST /api/metrics/funnel/event
Client beacon for the top-of-funnel `landed` stage. | **Auth**: anonymous

Request: `{stage: str}` — only stages in `CLIENT_EMITTABLE_STAGES` (today:
`landed`) are accepted; every downstream funnel stage is recorded
server-side at its authoritative transition, so a client can never inflate
it.
Response: `{recorded: bool}` — always HTTP 200; `recorded: false` for a
stage that isn't client-emittable, not an error.
Errors: none.

```bash
curl -s -X POST https://archimedes-arc.com/api/metrics/funnel/event \
  -H "Content-Type: application/json" -d '{"stage": "landed"}'
```

### GET /api/metrics/visitors
Where human traffic comes from, and on what device. | **Auth**: anonymous

Request: none.
Response (`VisitorInsightsResponse`): `{window: "all-time", countries: [{code, distinct_visitors}] (sorted desc), devices: dict[str,int] (mobile/tablet/desktop/tv/unknown), timestamp: str}` — counts are distinct HUMAN visitors only; agents/crawlers are excluded. Country comes from CloudFront viewer-country geolocation, device from CloudFront device headers (UA fallback).
Errors: none — empty maps on a Redis outage, always 200.

```bash
curl -s https://archimedes-arc.com/api/metrics/visitors
```

## Metrics (admin-gated) — summary

`/api/metrics/private/cost`, `/api/metrics/private/wallets`, and
`/api/metrics/private/wallets/connections` require a Better Auth session whose
**account** is a platform admin — listed in `PLATFORM_ADMIN_ACCOUNTS`, or
holding a linked wallet listed in `PLATFORM_ADMIN_WALLETS` (#1648: keyed on the
account, never on the request's wallet header). Anonymous gets `401`, a
signed-in non-admin gets `403`. These moved off the public router entirely in
#1373 (closing #1366) after a full-tree audit found the wallet-roster routes
serving a complete per-identity address list to anonymous callers. Full
endpoint reference, request/response shapes, and the exact gate order live in
[`admin-private.md`](admin-private.md) — this doc does not duplicate them.

---

See also: `docs/api/admin-private.md` (the admin-gated half of this metrics
split) and `docs/architecture.md` (telemetry middleware + identity-ledger
overview).
