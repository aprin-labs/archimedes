# A5 + A6 — the fetch memo and the re-run

**The re-run is the deliverable of Sprint 1.** Everything in clusters 1–4 exists to make its
output honest.

Read [README](README.md) session rules first.

## Hard abort gate — read this first

The re-run is authorised **only** by a passing cost-parity test
([cluster-1](cluster-1-cost-ssot.md)): both engines charge identical slippage + commission for
the same trade list. **If that test does not exist and pass, skip the re-run entirely**, leave
#1226's banner up, and move it to the Sept 16 gate. Do not run it anyway.

Run against a **production snapshot first** and diff the pass count before touching prod. If the
pass count comes back **zero**, hold publication, keep the banner, escalate as a gate item — a
leaderboard with no passing rows is not shippable.

## A5 — the 15-line fetch memo (0.25d)

Measured across all 34 curated strategies: universe sizes are `{1:5, 2:6, 5:22, 26:1}` — **153
`(strategy, asset)` fetch pairs over only 31 distinct tickers.**

`run_command` already exposes a `fetcher: Callable = fetch_ohlcv` injection seam (`cli.py:156`)
and `run_backtests.py:277` **does not pass it**. Build a per-symbol memo dict once in
`run_backtests()` and thread it in: **153 → 31 downloads**, which is also what makes the daily
scheduler viable.

No new file, no schema, no eviction policy — the seam already exists. The persisted Parquet
cache is **cut**.

Also: cache raw pulls to disk *before* the re-run so retries replay from cache and Yahoo leaves
the critical path. Throttle; run overnight. (Blast radius is narrower than it looks —
`PRICE_SOURCE=cascade` already routes *live* prices through Pyth; only historical is Yahoo.)

## A6 — diagnose first

> **Superseded 2026-09-01 (#1760).** This section describes the in-app refresh scheduler as a
> live mechanism. It is gone: `services/backtest_scheduler.py`, `BACKTEST_REFRESH_ENABLED` and
> `BACKTEST_MAX_AGE_HOURS` were deleted, and a curated backtest is now produced only by an
> explicit operator run ([`../runbooks/curated-backtests.md`](../runbooks/curated-backtests.md),
> [`../adr/backtests-are-frozen-evidence.md`](../adr/backtests-are-frozen-evidence.md)). The
> diagnosis below is kept as the historical record of the frozen-board investigation; "what
> makes the daily scheduler viable" in A5 no longer describes anything that exists.

The diagnostic belongs in [cluster-0](cluster-0-unblock.md), but restate the finding here before
running. #1203 merged 2026-08-03 and the newest row on the board is `backtest_end 2026-07-01`.
The scheduler is armed (`main.py:330-338`), `BACKTEST_REFRESH_ENABLED` defaults on,
`BACKTEST_MAX_AGE_HOURS` = 168h, and `content_hash` hashes the *result artifact*
(`backtest_mapper.py:111`) — so a corrected run produces a new hash and **would** insert.
Something is stopping it. Know which of the three hypotheses is true before you run.

## A4 read-path fix — do it in this session, it is the one moment

`backtest_repository.py:95-100 get_daily_returns` — honour the `operation` column (`row.operation`
is right there), then **delete** `run_backtests.py:209 _payload_with_selected_operation_first`.
Its own comment says it is a band-aid awaiting exactly this. The standing objection — "changing a
shared reader re-grades persisted rows" — is **void during a full re-run**. This is the one
moment in the year to do it.

## Execute

Run `run_backtests()` over all 34 with the `CostModel`. Then extend
`backend/archimedes/scripts/audit_backtest_universe.py` (already read-only, already reports the
cross-store delta) to emit the per-strategy **before/after** table:

> Sharpe · DSR p · PBO · OOS · turnover · cost drag · break-even bps · engine · `cost_model_id` ·
> status transition

**That table is the deliverable.** Publish it with the re-run.

**Run detached. Do not stream the run's output into context** — write to a log file, tail the
last 40 lines when it finishes.

## Set the expectation before you publish

[`second-wave-universe-experiment.md`](../quant/second-wave-universe-experiment.md) already tested
whether a bigger universe rescues failing strategies. **The answer is no** — no verdict flips,
several get worse, 7 of 9 had negative Sharpe after costs.
[`quant-roadmap.md`](../plans/quant-roadmap.md) concludes strategy count is a vanity metric.

**Fixing the backtests will make more strategies fail, not fewer.** The handful of rows that read
as passing today are a T-bill proxy, buy-and-hold, and risk parity — and the count itself is
**unestablished** per [`CLAUDE.md`](../../CLAUDE.md) § the hard constraint, so publish it as
"unestablished", never as a number. Correct routing plus real slippage on Engine C will very
likely push it down further. **That is the gate working, and it is the product: we sell the
verdict, not the alpha.**

Have the copy ready **before** the run, not after. Reframe the headline from scoreboard to
**rejection rate** — "most candidates do not clear the bar" *is* the thesis; a board that passes
everything is marketing.

## No threshold moves. A `FeedArityError` is a loud honest failure, not a regression.

Rollback is free — the store is add-only with content-hash dedup, so old rows survive and the
gate can be pinned to a `run_id`.

## Verify

```bash
# no row may predate 2026-08-03 (#1203's merge); today the newest is 2026-07-01
curl -s https://archimedes-arc.com/api/leaderboard \
| python3 -c "import sys,json;print(sum(1 for e in json.load(sys.stdin)['entries'] if e.get('sharpe_ratio')==0.0))"
# expect 0 — no zero-trade artifact presented as a completed backtest
```

Then **re-scope #1226's banner** to "engine-attributed, cost-parity floor" rather than removing it.
