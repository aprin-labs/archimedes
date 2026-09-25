# Insights analytics — what is really measured, and the one structural gap

> **status:** findings
> **owner:** Dan Browne
> **updated:** 2026-08-31

> **TL;DR:** Seven of the eight numbers on `/app/insights` are real queries against
> tables that exist, with their own caveats already rendered on the page. The eighth,
> `payments.settled_volume_usd`, **cannot be anything but `null` today** — not because
> Insights is missing a tile, but because `settlement_intents.amount_usdc` is declared and
> never written by any code path, upstream of this page and gated behind the unfinished
> custody migration (#975). This document is the decision input for whether unblocking
> that upstream work is worth prioritising. It deliberately proposes **no new tiles**:
> every number the current schema can honestly produce is already on the page.

Point-in-time investigation, written alongside the #1648 admin-gate fix (which is why the
page kept appearing and disappearing across browsers — see
[`api/admin-private.md`](../api/admin-private.md)). Grounded in the code as of
2026-08-31; re-verify before acting on it if the schema has moved.

## Scope and method

Source of truth for the tiles:
[`backend/archimedes/services/engagement_metrics.py`](../../backend/archimedes/services/engagement_metrics.py)
(`get_engagement_snapshot()` composes seven metric groups) and
[`ui/src/components/Insights.jsx`](../../ui/src/components/Insights.jsx) (which renders
all seven, plus `real_users` from `/api/metrics/private/cost`). Every claim below was
checked against those two files and the ORM models they query, not against prose.

Worth stating up front, because the owner's framing ("an analytics-quality pass") suggests
a bigger hole than exists: `engagement_metrics.py` has already been through four
documented rounds of adversarial fail-soft correction. Its degraded state is `None` per
numeric field plus `unavailable: true` — never a `0` — precisely so a database outage
cannot render as "measured, and the answer is zero". `Insights.jsx`'s `Stat` component
renders `None` as an em-dash. That discipline is in place and is not what needs work.

## The seven groups, graded

| Group | Real today? | What it actually counts, and the caveat that ships with it |
| --- | --- | --- |
| `accounts` | **Real** | `count(auth_users)` all-time, plus 7d/30d windows. The 7d window is calendar-day-anchored to match `strategies.new_7d`, so the two "(7d)" tiles rendered side by side cover the same span. |
| `linked_wallets` | **Real** | `count(linked_wallets)` rows. A row count, not a distinct-address or distinct-account count — one account with three wallets contributes three. |
| `strategies` | **Real, proxy semantics** | `strategy_store` rows excluding `is_example`, plus a zero-filled 7-day trend. Counts distinct *stored content*, not generation events: `upsert_strategy`'s content-hash dedup collapses a regeneration (or two users producing identical output) into one row. The tile's own `note` says so on the page. |
| `generation_costs` | **Real, with an explicit completeness flag** | Rows in `generation_costs`, and the LLM token totals inside their `measurement_json`. `measured_count` only counts rows whose usage accounting is complete; `usage_complete: false` triggers a "partial measurement" warning on the page. A job that consumed tokens but errored, was cancelled, or failed the rigor gate before persisting a strategy leaves **no row at all** — so `total_tokens` is honest for measured, strategy-producing jobs and is labelled "LLM tokens (measured jobs)" rather than a platform total. |
| `paper_deployments` | **Real** | `count(paper_deployments)` grouped by `status` (active / stopped). |
| `repeat_generation_users` | **Real, proxy semantics** | Accounts owning `is_example = false` strategy rows whose `created_at` spans more than one calendar day, over accounts owning any. A real join (`strategy_store.owner_user_id` is a genuine FK to `auth_users.id`). Drifts in both directions — dedup hides a second real day; a backfilled legacy row can contribute a pre-account day — and the `note` on the page states the proxy's actual definition rather than the informal "days a user generated" the field name suggests. Pre-account, wallet-only generations are excluded from numerator *and* denominator, and the denominator is shown rather than a bare percentage. |
| `payments` | **`dry_run` real; `settled_volume_usd` structurally impossible** | See below. |

## The one real gap: `payments.settled_volume_usd`

`get_payments_snapshot()`'s own docstring states this, and this section does not
contradict it — it restates the same finding for a reader deciding what to fund:

- `settlement_intents` (`models/marketplace.py`) **is** a real, durable table, and its
  `status` column **does** reach `"settled"` / `"failed"` on the live path via
  `marketplace/service.py`'s `_finalize_settlement_intent`. The settlement *event* is
  recorded. The earlier "no durable record exists" framing was wrong and was retracted.
- What is missing is narrower: the table's **`amount_usdc` column is declared and never
  written by any code path**. `SettlementIntent` is constructed in exactly one place
  (`_claim_settlement_intent`, which passes `strategy_id` / `tick_id` / `sub_id` / `step` /
  `status`), and the only code that mutates an existing row is `_finalize_settlement_intent`,
  which assigns only `status` and `settled_at`. Neither touches `amount_usdc`. So
  `sum(amount_usdc) WHERE status = 'settled'` would be a real query returning a meaningless
  `NULL` even against live rows. (Confirm by reading those two write sites, not by grepping
  the column name — the string also appears as an unrelated dict key in
  `services/revenue_sweep.py`.)
- Independently, `PAYMENTS_DRY_RUN` gates every settlement path before an intent is even
  claimed (`services/generation_payment.py`), so no real value has moved. Either reason
  alone already blocks the metric.
- The field therefore reads `null`, never `$0` — "not yet metered", never "measured at
  zero" — and the page renders an em-dash beside a DRY-RUN badge.

**This is a settlement-layer gap, not an Insights defect, and it was deliberately not
touched by #1648.** The code is money-adjacent and sits behind the unfinished custody
migration (#975) and `PAYMENTS_DRY_RUN`; writing `amount_usdc` is a change to the payment
write path, which is out of an admin-gate PR's blast radius per CLAUDE.md's "when to ask
before acting".

### The decision this document exists to support

Unblocking revenue reporting is a three-step chain, and only the owner can price it:

1. Write `amount_usdc` at settlement time (`marketplace/service.py`) — small, but on the
   money write path, so it needs the same review bar as any payment change.
2. Turn `PAYMENTS_DRY_RUN` off, which is gated on #975's custody resolution, not on this.
3. Then `settled_volume_usd` becomes a one-line `sum()` in `get_payments_snapshot()` — the
   field name and the null-not-zero contract are already the right slot for it.

Step 3 is trivial and depends entirely on 1 and 2. Nothing on the Insights page needs to
change to make revenue appear once they land.

## What was explicitly NOT done, and why

- **No new tiles.** Every number the current schema can honestly compute is already
  rendered. Adding a real-looking figure with no measured source is the exact fail-soft
  violation `engagement_metrics.py`'s four rounds of corrections exist to prevent.
- **No change to `settlement_intents`, `amount_usdc`, or any payment path.** See above.
- **No client-side admin logic.** `ui/src/adminProbe.js` remains the only place that decides
  admin, and it does so by asking the server. #1648 was a server-side fix; the frontend
  probe tests needed no changes, which is the signal that the fix stayed on its own side of
  the boundary.

## Known follow-ups (not defects)

- `ui/src/App.jsx` and `ui/src/components/Layout.jsx` still re-run the admin probe when the
  connected wallet changes (`walletChangeSeq` / `walletAddr` effect deps). After #1648 the
  answer no longer depends on the connected wallet, so that is now wasted work rather than
  a correctness issue. Left in place deliberately: removing it is a UI behaviour change that
  belongs in its own PR, and the issue asked for the frontend to stay untouched.
- `linked_wallets.total` counts rows, not distinct accounts or addresses. If the owner reads
  it as "how many users connected a wallet", it will over-count multi-wallet accounts. A
  distinct-account variant is a real query away, but changing what an existing tile means
  is a product call, not a cleanup.
