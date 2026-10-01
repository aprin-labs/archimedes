# Generation Streaming Spec

> **Status:** Historical design draft. Live endpoint/auth contract is
> [`docs/agent-api.md`](../agent-api.md): all generation requires a Better Auth
> account; wallet linking remains optional until wallet/on-chain actions.
>
> Drafted 2026-05-22 as Phase 0 of the
> [Spine+ v2 plan](../plans/spine-plus-v2-plan.md). Authoritative for Phase 2
> implementation — the Generate page's SSE protocol.
>
> **Lineage:** Wraps [`portfolio_agent.PortfolioAgent`](../../backend/archimedes/agents/portfolio_agent.py)
> per the decision in
> [`portfolio-constructor-decision-tree.md`](../adr/portfolio-constructor-decision-tree.md).
> Iterations bounded by `MAX_AGENT_ITERATIONS=12` (Day-10 default).

## Why SSE not WebSocket

- Generate is **one-way** — server pushes events, client subscribes.
- SSE is plain HTTP, survives proxies, auto-reconnects in the browser.
- The client-side state machine (idle → streaming → done/error) is simpler
  than a bidirectional channel.

If a future feature requires bidirectional (e.g., live "stop and ask the agent
a question mid-stream"), revisit. For Phase 2 generation, SSE.

## Endpoints

The Phase 2 work lands in a new
[`backend/archimedes/api/generate_routes.py`](../../backend/archimedes/api/generate_routes.py)
(per cross-cutting principle #2 — no new routes go into `routes.py`).

### `POST /api/generate/jobs`

Create a generation job. Returns immediately with `job_id`; actual work runs
in a background task.

**Request body:**
```json
{
  "brief": "I want a 13-week treasury alternative with low volatility",
  "wallet_address": "0x..." 
}
```
`wallet_address` was optional in this draft. Live generation requires account
authentication; optional wallet provenance is resolved server-side from linked wallets.

**Response (201):**
```json
{
  "job_id": "gen_01HXYZ...",
  "stream_url": "/api/generate/stream/gen_01HXYZ...",
  "resume_token": "...",
  "ttl_seconds": 900
}
```

### `GET /api/generate/stream/{job_id}`

Server-sent events. Headers:

- `Content-Type: text/event-stream`
- `Cache-Control: no-cache`
- `Connection: keep-alive`
- `X-Accel-Buffering: no` (nginx/EC2 stack — disables proxy buffering)

Client may send `Last-Event-ID: <event_id>` to resume from a known point.

### `POST /api/generate/jobs/{job_id}/cancel`

Cancel a job. Idempotent. Emits a final `error` event with `recoverable: false`
and `message: "cancelled_by_user"` on the open stream.

## Event schema

Each SSE event is `id: <monotonic_int>\nevent: <name>\ndata: <json>\n\n`.

`<name>` is one of:

| Event | Payload | When emitted |
|---|---|---|
| `job_queued` | `{ job_id, brief, ts }` | Immediately after `POST /api/generate/jobs`. |
| `brief_validated` | `{ job_id, asset_classes, risk_appetite, time_horizon, ts }` | After LLM extracts structured intent from free-text brief. |
| `candidates_selected` | `{ job_id, candidate_count, regimes, ts }` | Before per-candidate work begins. Carries **no papers claim** — paper selection happens inside each candidate's debate run, so no honest count exists at this point (the field previously here sliced the curated library to `max_papers`, a constant from the wrong population). |
| `agent_iteration` | `{ job_id, iteration_n, max_iterations, ts }` | At the top of every `PortfolioAgent.recommend()` loop iteration. |
| `tool_called` | `{ job_id, tool_name, args_summary, ts }` | When the agent invokes a tool (`get_asset_stats`, `get_correlation`, `stress_test`). |
| `tool_result` | `{ job_id, tool_name, result_summary, ts }` | After the tool returns. `result_summary` is a 1-line human-readable summary, not the full payload. |
| `debate_turn` | `{ job_id, candidate_id, role, round, verdict, claims: [...], discard: [...], headline, ts }` | Once per finished bull/bear turn, i.e. 4× per debate run, emitted from `_debate_round` immediately after the turn is appended to the transcript — so the stream is 1:1 with what `debate_transcripts.transcript_json` stores. **Sanitized on this path**: the SSE transport never reaches `record_debate_transcript`, so `_emit_debate_turn` applies `sanitize_transcript` itself; raw model text must never leave the server. `headline` is server-written human copy over the SANITIZED turn. No LLM backend ⇒ no turns ⇒ this event never fires (an empty debate is never rendered as a debate). |
| `debate_attribution` | `{ job_id, candidate_id, role: "attribution", round: null, verdict, paper_verdicts: [...], fusion_reasoning, papers_offered, distinct_mechanism_papers, ts }` | Once per debate run, after the leader is chosen. This is the SAME entry `_persist_debate_transcripts` appends to the transcript row (#1739), built by the one shared helper `generation_pipeline._paper_attribution_entry` — the summary sentence exists in exactly one place, or the stream and the passport drift into two claims about one run. Sanitized on emit for the same reason as `debate_turn`. Gated on a non-empty transcript. |
| `candidate_drafted` | `{ job_id, candidate_id, strategy_name, weights_preview, regime, source_arxiv_ids: [...], ts }` | When the agent produces a candidate portfolio. Multiple may fire per job (per the locked decision, agent considers N internally). `source_arxiv_ids` are the papers the accepted proposal **actually cites**, provenance-checked against the corpus surface by the debate critics — never the `max_papers` depth knob. |
| `candidate_evaluated` | `{ job_id, candidate_id, rigor_verdict: { dsr, pbo, oos_sharpe, lookahead_audit_passed, passes }, ts }` | After rigor gate runs on each candidate. |
| `best_selected` | `{ job_id, best_candidate_id, considered_count, ts }` | When the agent picks the surfaced candidate from the considered set. |
| `trace_hashed` | `{ job_id, trace_hash, ts }` | After the reasoning trace is committed to `AgentStateStore` and its keccak256 computed. |
| `persisted` | `{ job_id, strategy_id, redirect_url, ts }` | After `StrategyRecord` insert. `redirect_url` is `/strategy/:id`. |
| `done` | `{ job_id, strategy_id, ts }` | Terminal success. Stream closes. |
| `error` | `{ job_id, message, recoverable: bool, code, ts }` | Terminal failure. Stream closes. |

All payloads include `ts` (ISO-8601 UTC) so the client can render a true timeline.

Multi-strategy: per the locked decision, the **client surfaces the single
`best_selected` candidate**, but the full event stream including all
`candidate_drafted` / `candidate_evaluated` events is persisted. The strategy
passport at `/strategy/:id` exposes a "see candidates that were rejected" link
that replays the event history.

## Reconnection semantics

If the client disconnects (page reload, network blip, back-button), it can
re-subscribe:

1. Client persists `currentJobId` to `localStorage` on `job_queued`.
2. On Generate page mount, if `localStorage.currentJobId` exists and the job's
   TTL hasn't expired, client re-opens the stream with
   `Last-Event-ID: <last_seen_id>`.
3. Server replays events from Redis (key: `gen:job:{job_id}:events`, list type)
   starting after `Last-Event-ID`.
4. If the job already finished, the stream replays the last `done` or `error`
   event and closes.

**TTL on event log:** 15 minutes after `done`/`error`. After that, the job's
events are gone — the user can still see the resulting strategy in Library but
can't re-watch the stream.

## Job persistence model

Per-job Redis keys:

| Key | Type | Contents |
|---|---|---|
| `gen:job:{job_id}:meta` | hash | `brief`, `wallet_address`, `created_at`, `state`, `current_iteration` |
| `gen:job:{job_id}:events` | list | Serialized SSE events in order |
| `gen:job:{job_id}:lock` | string | Prevents double-execution if API restarts |

Frontend stores `currentJobId` in `localStorage` (cleared on `done`/`error` or
when the user explicitly clicks "Start over").

## Backpressure & ordering

- Agent iterations are seconds apart, not milliseconds — no batching needed.
- Each event flushes immediately (`flush()` after every `yield`).
- The Redis list is the source of truth for ordering. The HTTP response writer
  pulls from the list; the agent process pushes to it.

## Failure modes

| Failure | Behavior |
|---|---|
| Agent hits `MAX_AGENT_ITERATIONS=12` without a valid candidate | Emit `error { message: "max_iterations_exceeded", recoverable: true, code: "MAX_ITER" }`. Client offers "Regenerate" CTA. |
| LLM API unreachable | Emit `error { message: "llm_unavailable", recoverable: true, code: "LLM_DOWN" }`. Client offers "Retry in 30s" CTA. |
| All candidates fail rigor gate | **Not an error (since #818).** Emit `best_selected { deployable: false, validated_count: 0, ... }` (ABSTAIN): the best is surfaced as a *considered* candidate, persisted with `passes_rigor_gate=false`, and the server-side vault gate refuses to deploy it. Client redirects to `/strategy/:id` for the **best Rejected** candidate so the user can see why. (Legacy `code: "RIGOR_FAIL"` is no longer emitted.) |
| No candidates generated at all | Upstream generation produced zero candidates. Emit `error { message: "no candidates generated", recoverable: true, code: "NO_CANDIDATES" }` — distinct from the all-failed-rigor ABSTAIN above, so telemetry doesn't read a generation failure as a rigor-gate failure. |
| Brief is gibberish or out-of-scope | Brief-validation step fails; emit `error { message: "brief_invalid", recoverable: true, code: "BRIEF_INVALID", hint: "Try mentioning an asset class or risk appetite" }`. |
| API process restarts mid-job | **Implemented (#1355).** No restart-time scan: the run's independent heartbeat task (`_JOB_HEARTBEAT_INTERVAL_SECONDS = 30`) touches `heartbeat_at` on its own clock for the life of the job, and `enqueue`/`update_status` both refresh the job's TTL so the record no longer silently expires mid-run. A `running` job whose heartbeat is older than 5 min (`_STALLED_AFTER_SECONDS = 300`) is normalized to `state: "stalled"` on every read (`GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/cost` — all three route through the same `_normalize_state`) — a read-time reinterpretation, never written to Redis. An open SSE stream on such a job gets one synthetic `error` event, `code: "STALLED"`, and closes. If the job record already shows a terminal status with no terminal event in the log (e.g. reconnecting after the 15-minute event log TTL expired, which is shorter than the 1-hour job TTL and so the routine case for any client reconnecting to a since-completed job) the stream instead synthesizes the terminal frame matching that job's REAL status — a real `done` event for a successful run, `code: "CANCELLED"` for a cancelled one, `code: "JOB_FAILED"` for a genuine failure — never a blanket STALLED, so a successful generation is never misreported as an error. |
| Client disconnects + never returns | Server completes the job and writes events to Redis; on TTL expiry, GC removes them. Strategy still ends up in Library. |

## Server-side architecture

```
POST /api/generate/jobs
        │
        ▼
   generate_routes.create_job()
        │
        ├─→ create job_id (ULID)
        ├─→ enqueue in BackgroundTasks: run_generation_job(job_id, brief)
        └─→ return 201 with stream_url

(background)
   run_generation_job(job_id, brief)
        │
        ▼
   PortfolioAgent.recommend(brief, regime, emit=push_event_to_redis)
        │
        ├─→ each iteration calls emit("agent_iteration", ...)
        ├─→ each tool call emits ...
        └─→ final result triggers emit("done", ...) + DB insert

GET /api/generate/stream/{job_id}
        │
        ▼
   StreamingResponse(event_generator(job_id, last_event_id))
        │
        └─→ tail Redis list; yield each new event; close on `done`/`error`
```

## Frontend integration sketch

```jsx
// ui/src/components/Generate.jsx (Phase 2)
const [events, setEvents] = useState([])
const [job, setJob] = useState(null)

async function submitBrief(brief) {
  const res = await fetch('/api/generate/jobs', { method: 'POST', body: JSON.stringify({ brief }) })
  const { job_id, stream_url } = await res.json()
  localStorage.setItem('currentJobId', job_id)
  setJob({ id: job_id })

  const es = new EventSource(stream_url)
  es.addEventListener('agent_iteration', e => setEvents(prev => [...prev, JSON.parse(e.data)]))
  es.addEventListener('best_selected', e => setEvents(prev => [...prev, JSON.parse(e.data)]))
  es.addEventListener('done', e => {
    const { redirect_url } = JSON.parse(e.data)
    localStorage.removeItem('currentJobId')
    navigate(redirect_url)
    es.close()
  })
  es.addEventListener('error', e => { /* surface message, offer retry */ })
}

// On mount: if localStorage.currentJobId exists, re-subscribe.
```

## Acceptance

A frontend dev (Marten / Daniel R.) can build the streaming UI from this doc
alone — no further API design questions. A backend dev (Daniel R. / Chuan)
can implement `generate_routes.py` and the Redis-backed event log without
re-deriving the event schema.

## Open questions

1. **Event log TTL** — 15 minutes after terminal state. Long enough? Should we
   keep all events forever for forensic value (link to `/reasoning` from a
   strategy passport always works)?
2. **`tool_result` payload size** — for `stress_test`, the raw result can be
   ~10 KB. Do we include it inline (the `result_summary` field truncates) or
   surface a `tool_result_full_url`?
3. **Anonymous vs authenticated rate limiting** — anonymous users get N free
   generates? Today: no rate limit. Probably fine for hackathon scope; flag
   for v1.5.
