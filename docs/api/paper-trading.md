# Paper Trading API

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-30

`/api/paper/*` — the verdict → paper-trade path (MVP). Deploying a strategy to
paper trading snapshots its DSL spec and forward-runs the **same graded
engine** one appended bar per day, so a paper track record tracks the graded
backtest semantics by construction rather than a separate simulated broker.
This surface never moves funds — it exists to let a strategy earn a forward
track record before anyone considers a real deployment, and its ownership,
rate limits, and error handling all read that way (an account session is
sufficient proof; there is no fresh-wallet-signature gate here).

**Ledger is append-only, never rewritten.** Each daily advance replays the
strategy's full history and diffs it against what is already written. New
dates are appended; dates the ledger already has are **never overwritten**
even when a fresh replay disagrees with the stored value (an upstream data
restatement — e.g. yfinance revising history) — the deployment is instead
stamped `drift_detected_at` and the disagreement is logged loudly. A paper
track record that could silently rewrite its own past is exactly the failure
this product exists to oppose; see `services/paper_trading.py` module
docstring.

**Auth model.** Every route requires a Better Auth account session (the
`better-auth.session_token` cookie via `require_current_user`) — ownership is
`owner_user_id` only. `owner_wallet` is recorded on deploy as **provenance
only** (the caller's linked wallet at that moment, if any) and never grants
access; an account-only user with no linked wallet can deploy normally.
Examples below assume an authenticated cookie jar at `/tmp/session.jar`.

## Endpoints

### POST /api/paper/deployments
Deploy a strategy to paper trading — snapshots its DSL spec and opens a new
ledger. | **Auth**: account-session | **Flags**: rate limit `10/minute`
(disabled under `TESTING`)

Request: `{strategy_id: str}`.
Response (201): a `deployment_summary` object — see [Deployment summary shape](#deployment-summary-shape) below. The
first daily advance runs synchronously and best-effort: if it fails (e.g. a
transient data-source hiccup) the deployment is still created with zero rows,
and the scheduled `paper_advance_loop` backfills it from `deployed_at` on its
next pass.
Errors:
- `422` `strategy_id is required` — missing/blank body field.
- `404` `Strategy not found` — unknown strategy, or not visible to the caller
  (private-until-published rules, #850).
- `422` `no_strategy_spec` — the strategy has no machine-readable DSL spec to
  paper-trade.
- `422` `invalid_strategy_spec` — the stored spec fails DSL validation.

```bash
curl -s -X POST https://archimedes-arc.com/api/paper/deployments \
  -b /tmp/session.jar -H "Content-Type: application/json" \
  -d '{"strategy_id": "<strategy_id>"}'
```

### GET /api/paper/deployments
List the caller's own paper deployments, newest first. | **Auth**:
account-session

Request: none.
Response: `{deployments: [deployment_summary, ...]}` — every deployment
owned by the caller (`owner_user_id`), most recently created first.
Errors: none beyond the global 401.

```bash
curl -s -b /tmp/session.jar https://archimedes-arc.com/api/paper/deployments
```

### GET /api/paper/deployments/{deployment_id}
Get one owned deployment's current summary. | **Auth**: account-session

Request: path `deployment_id`.
Response: a `deployment_summary` object.
Errors: `404` `Paper deployment not found` — missing, or owned by a different
account (existence is private — a wrong-owner lookup 404s exactly like an
unknown ID, never a 403).

```bash
curl -s -b /tmp/session.jar https://archimedes-arc.com/api/paper/deployments/<deployment_id>
```

### GET /api/paper/deployments/{deployment_id}/marks
Intraday mark-to-market history for one owned deployment, oldest first. |
**Auth**: account-session

Request: path `deployment_id`; optional query `limit` (default 500, clamped to
`[1, 2000]` — the newest `limit` marks are returned, never the oldest).
Response: `{deployment_id: str, marks: [mark, ...], latest: mark | null}`.
`latest` is the last element of `marks`, or `null` when this deployment has no
mark yet.
Errors: `404` `Paper deployment not found` — missing, or owned by a different
account (existence is private, same rule as the routes above).

**Marks are the unsettled view, not the track record.** `series` on the
deployment summary comes from `paper_daily_returns`, which is append-only by
law and is the recorded paper track record — Arc testnet, no real funds. A
mark is a decoration with a TTL: raw 15-minute marks are kept 7 days, rolled
up to one-per-hour for 90, then
**deleted** (there is nothing worth aggregating to past that — the daily close
is already stored permanently in the ledger). A client must render the two
distinguishably and must never present a mark as a settled return.

**An empty `marks` array is a real, normal state**, not an error: a deployment
created between ticks, a deployment on `SPY` before the session opens, or any
deployment whose daily advance has not yet stamped a position set. Render it
as an em-dash with a reason — never as `+0.00%`.

#### Known limitation: a mark cannot see cash

A mark re-prices the strategy's **asset basket** — the sleeve weights the last
daily settle established — by applying each asset's move since that settle. It
**does not know whether the strategy is currently invested or sitting in
cash.** `replay_spec` returns dated portfolio returns, not a per-sleeve
invested/flat vector, so v1 has no position vector to read, and inferring one
from a return series would be a guess dressed as a measurement.

The consequence, stated plainly: **a strategy that is flat (its exit condition
fired, or it never entered) still shows a live value that moves with the assets
it would hold.** A day where the settled ledger records `+0.00%` because the
strategy held cash can carry a mark reading `+10.00%` if the underlying asset
rose 10% — a real, reproducible divergence, pinned by
`test_a_cash_sleeve_is_still_marked_as_if_invested` in
`backend/tests/services/test_paper_marks.py`.

The error is bounded and self-correcting: it never touches
`paper_daily_returns`, and the next daily advance re-settles the anchor
against the ledger, so a mark can be wrong for at most one session and cannot
accumulate. **The settled daily return is the honest number** — marks are
labelled unsettled for exactly this reason, and a client must say so at the
point of render, not only in a tooltip.

Closing the gap needs a per-sleeve position vector out of the graded engine —
tracked as the marks-v2 follow-up, not a v1 constant.

Each `mark`:

| Field | Type | Meaning |
|---|---|---|
| `ts` | str (datetime) | The **upstream observation time** — when the price was seen at the vendor, never when the row was written. A mark written at 14:47 from a 14:32 bar is a 14:32 mark. For a multi-leg universe this is the *oldest* contributing leg's bar time: a mark is only as current as the stalest price inside it. |
| `portfolio_value` | float | An **index**, 1.0 == deploy-time capital — the same basis as `series[].equity_index`. Never dollars: no deployed-capital amount exists anywhere in the system, so a dollar figure would be invented. |
| `source` | str | The market-data provider that produced the prices, at fetch time. Stored per row so a later vendor swap cannot retroactively relabel history. |
| `is_delayed` | bool | Whether the provider's intraday feed is a delayed tape. Set by the fetch path from what the provider declares, not inferred at render time. When `true` the UI must say "delayed" beside the value. |
| `granularity` | str | `raw` (a 15-minute mark) or `hourly` (a rolled-up survivor of the 7-day tier). |
| `prices` | object | The prices **actually observed**, keyed by vendor ticker. A leg too stale to use is **absent** rather than carried at a stale price — so a partially-frozen mixed universe (a closed equity leg beside a live crypto one) stays recoverable instead of collapsing into one opaque number. |

#### Known limitation: mixed-universe cadence, and a non-monotonic knob

`ts` is both the honesty stamp *and* the row's dedupe key
(`uq_paper_marks_dep_ts_gran`). On a MIXED equity+crypto universe those two
jobs disagree: for as long as a frozen equity leg is still inside the staleness
window it pins `ts = min(fresh_bar_times)` to its last bar, and every later
tick dedupes against the row already written at that timestamp. Roughly **an
hour of crypto marks is dropped at each equity close** (at the 60-minute
default), after which the equity leg ages out and the cadence resumes.

A dropped mark is a **gap**, never a wrong number — nothing stale is ever
written — but a client charting a mixed-universe deployment will see the tail
pause after each equity close, and should not read that as a fetch failure.

The knob is therefore **non-monotonic** on a mixed universe: **raising**
`PAPER_MARKS_MAX_STALENESS_MINUTES` produces **fewer** marks, because it widens
the window in which the frozen leg pins the stamp. An operator reaching for the
tolerance dial to fix stalled marks would make it worse. Pinned by
`test_a_mixed_universe_loses_marks_while_a_frozen_leg_pins_the_stamp`.

Splitting the dedupe key from the stamp needs a second timestamp column and a
migration (the constraint is on the stored `ts`); stamping the *newest* leg
instead is rejected — it would buy cadence by letting the portfolio claim to be
as current as its freshest leg, which is the one thing `min` exists to prevent.
Tracked as a marks-v2 follow-up.

```bash
curl -s -b /tmp/session.jar \
  "https://archimedes-arc.com/api/paper/deployments/<deployment_id>/marks?limit=100"
```

```json
{
  "deployment_id": "dep_abc123",
  "marks": [
    {"ts": "2026-08-30T14:30:00+00:00", "portfolio_value": 1.0331, "source": "yfinance",
     "is_delayed": true, "granularity": "raw", "prices": {"SPY": 511.9}},
    {"ts": "2026-08-30T14:45:00+00:00", "portfolio_value": 1.0347, "source": "yfinance",
     "is_delayed": true, "granularity": "raw", "prices": {"SPY": 512.34}}
  ],
  "latest": {"ts": "2026-08-30T14:45:00+00:00", "portfolio_value": 1.0347, "source": "yfinance",
             "is_delayed": true, "granularity": "raw", "prices": {"SPY": 512.34}}
}
```

### POST /api/paper/deployments/{deployment_id}/stop
Stop an owned deployment — sets its status to `stopped`; the ledger already
written is untouched. | **Auth**: account-session

Request: path `deployment_id`.
Response: `{deployment_id: str, status: "stopped"}`.
Errors: `404` `Paper deployment not found` — missing, or not owned by the
caller.

```bash
curl -s -X POST -b /tmp/session.jar https://archimedes-arc.com/api/paper/deployments/<deployment_id>/stop
```

## Deployment summary shape

`POST /deployments`, `GET /deployments`, and `GET /deployments/{id}` all
return this same shape (a bare list of it for `GET /deployments`):

| Field | Type | Meaning |
|---|---|---|
| `deployment_id` | str | The deployment's ID. |
| `strategy_id` | str | The strategy this deployment paper-trades. |
| `deployed_at` | str (date) | The date the snapshot was opened; the ledger only ever holds dates `>= deployed_at`. |
| `status` | str | `active` or `stopped`. |
| `days` | int | Number of daily ledger rows written so far. |
| `total_return` | float | Compounded return over the ledger, `equity_index - 1.0` on the last row (`0.0` with zero rows). |
| `drift_detected_at` | str (datetime) \| null | Set the first time a fresh replay disagreed with an already-written ledger row; never cleared automatically once set. |
| `rigor_gate_status` | str | The **verdict of record** — `pass` \| `fail` \| `pending` \| `degenerate`. Read from `strategy_passports`, never recomputed. `pending` when the strategy has no passport row at all: the read fails closed, so this key never reports a pass it did not find. |
| `passes_rigor_gate` | bool \| null | The fail-closed boolean, DERIVED from `rigor_gate_status` (`== "pass"`), not copied from the stored column. `null` — never `false` — when no gate ever graded the strategy: `false` is a verdict ("the gate ran and this lost") and no gate ran. |
| `graded_at` | str (datetime) \| null | When that verdict was produced. Naive UTC, so it carries **no offset** — read the calendar day it names rather than handing it to a local-time parser. `null` means never graded. |
| `gate_version` | str \| null | WHICH gate produced the verdict (`services/rigor_gate_version.py`). Two deployments with different values were graded by different gates and are not comparable. The literal `legacy-derived` marks a verdict the verdict-of-record migration INFERRED from pre-existing columns rather than one a gate run produced. |
| `series` | array | One entry per ledger day, oldest first — see below. |
| `latest_mark` | object \| null | The most recent intraday mark, same shape as [`GET .../marks`](#get-apipaperdeploymentsdeployment_idmarks) returns, so a list view can render a live value without a second round trip. **`null` is a real state** (no mark yet) and must render as an em-dash with a reason, never `+0.00%`. Always a separate key from `total_return`, never folded into it: `total_return` is the settled track record; a mark is unsettled. |

Each `series` entry:

| Field | Type | Meaning |
|---|---|---|
| `date` | str (date) | The bar's date. |
| `daily_return` | float | That day's portfolio return (dollar-sleeve-weighted across the strategy's asset universe, faithful to the graded backtest path). |
| `equity_index` | float | Cumulative compounded equity starting from `1.0` at deploy — this is the sparkline series a client plots directly. |

```json
{
  "deployment_id": "dep_abc123",
  "strategy_id": "strat_xyz",
  "deployed_at": "2026-08-01",
  "status": "active",
  "days": 3,
  "total_return": 0.0142,
  "drift_detected_at": null,
  "rigor_gate_status": "fail",
  "passes_rigor_gate": false,
  "graded_at": "2026-08-30T11:22:33",
  "gate_version": "gate-v1-4f2a9c1e07b3d5a8",
  "latest_mark": {
    "ts": "2026-08-30T14:45:00+00:00", "portfolio_value": 1.0347, "source": "yfinance",
    "is_delayed": true, "granularity": "raw", "prices": {"SPY": 512.34}
  },
  "series": [
    {"date": "2026-08-01", "daily_return": 0.004, "equity_index": 1.004},
    {"date": "2026-08-02", "daily_return": -0.001, "equity_index": 1.003},
    {"date": "2026-08-03", "daily_return": 0.0112, "equity_index": 1.0142}
  ]
}
```

## Deploy at will, with the verdict beside the record

There is **no rigor precondition on deploying**. `POST /deployments` checks that
the caller owns (or may read the reasoning of) the strategy and that its stored
spec still validates — and nothing else. A strategy the rigor gate REJECTED can
be paper-traded, deliberately: owner decision (Dan, 2026-09-01, issue #1764) is
that a rejected strategy performing poorly forward is evidence about the gate's
call, and a passed one tracking its backtest is evidence too. Vault / on-chain
deployment is a different question and stays gated.

That freedom is only honest if the verdict travels with the numbers, which is
what the four fields above are for. A client rendering `total_return` **must**
render the verdict beside it. Three rules the shipped UI follows and any other
client should:

1. **The verdict is a read, never a re-grade.** It is the grade stored at
   backtest time (`docs/adr/rigor-verdict-of-record.md`). Do not recompute a
   verdict from the DSR/PBO numbers on the passport — a client that did would
   put a different answer on the deployment card than the strategy's own
   passport shows.
2. **All four states get their own answer.** `pending` is "no gate has graded
   this", NOT a failure; `degenerate` is "the persisted series was flat, so the
   gate had nothing it could legitimately score", NOT a failure either. Only a
   literal `pass` may be rendered as one.
3. **A missing verdict is rendered as missing.** If `rigor_gate_status` is
   absent (an old server behind a new client), say so — never fall back to
   silence beside the number, and never to a pass.

**The same verdict is on the live-paper board.** `GET
/api/leaderboard/live-paper` — the "Live paper trading" tab, whose rows are
these same deployments ranked by realised forward return — carries
`rigor_gate_status`, `graded_at` and `gate_version` on every row, read from the
same passport row by the same code. It deliberately does NOT carry
`passes_rigor_gate`: a bare boolean beside a forward return is the field a
consumer would blend or sort on, and that board's whole point is that nothing
backtest-era enters its ranking. See
[`leaderboard-and-metrics.md`](leaderboard-and-metrics.md).

**There is no `outside_window` field, and no deployment window.** #1764's
acceptance asked for one; nothing in this product declares a deployment time
window — not the DSL spec, not `strategy_store`, not `strategy_passports`, not
`paper_deployments`. The only windows in the tree are the vault window
(`docs/specs/vault-semantics-spec.md`, out of scope for the paper-only cut) and
the BACKTEST window (`backtest_start` / `backtest_end`), which every forward
paper deployment is outside of by construction — a flag that is always true is
not a disclosure. What actually carries staleness is `graded_at` beside
`deployed_at`: both are on this payload, and the gap between them is the honest
quantity. If a declared deployment window is ever added to the strategy schema,
this is where the flag belongs.

---

See also: `backend/archimedes/services/paper_trading.py` (replay + ledger
mechanics), `backend/archimedes/services/passport_loader.py`
(`stored_rigor_verdict` — the verdict read) and
`docs/specs/strategy-dsl-spec.md` (the DSL the snapshot spec must validate
against).
