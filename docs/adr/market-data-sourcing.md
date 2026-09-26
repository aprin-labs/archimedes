# ADR: Split market-data sourcing — Tiingo for paid analysis, yfinance for the free Explore viewer

> **Audience:** Archimedes team (decision owner: Dan, strategy-engine + architecture owner)
> **Status:** Accepted (amended 2026-09-01 — see § Amendment: per-seam routing)
> **Date:** 2026-08-31
> **Amended:** 2026-09-01 ([#1798](https://github.com/aprin-labs/archimedes/issues/1798)) — one variable per SEAM instead of one global variable, so daily bars can move to Tiingo without taking the live/intraday surfaces with them. The decision above is unchanged; what changed is the mechanism that expresses it.
> **Owner:** Dan Browne
> **Supersedes:** —
> **Superseded-by:** —
> **Question being decided:** Now that [#1218](https://github.com/aprin-labs/archimedes/issues/1218) has priced yfinance as an unlicensed-for-commercial-redistribution dependency sitting on the MVP critical path, which surfaces move to a commercial vendor, which stay on yfinance, and what has to be true before real money touches either?
> **Related issues/PRs:** [#1218](https://github.com/aprin-labs/archimedes/issues/1218) (the costing exercise this decides), [#1282](https://github.com/aprin-labs/archimedes/pull/1282) (the provider seam), [#1455](https://github.com/aprin-labs/archimedes/pull/1455) (the Tiingo adapter), [#1203](https://github.com/aprin-labs/archimedes/issues/1203) (per-strategy universes — the routing fix that put market-data volume on the critical path), [#775](https://github.com/aprin-labs/archimedes/issues/775) (yfinance as an oracle cross-check — different use, same dependency), [#1798](https://github.com/aprin-labs/archimedes/issues/1798) (the per-seam routing amendment below).

## TL;DR

**Source market data by surface, not by system.** Backtesting and any paid analysis run
on **Tiingo**, reached through the existing provider seam's daily half
(`MARKET_DATA_DAILY_PROVIDER`, #1798) — starting on
Tiingo's **Free tier, for testing only**. The free, ungated **Explore** viewer stays on
**yfinance**, because Explore is a free open-source data viewer that sells and
redistributes nothing, which is the posture yfinance's terms actually permit.

Two things are flagged rather than assumed:

1. **A Tiingo commercial (Business) plan is a mainnet prerequisite.** Free-tier data is
   for testing. Before real money moves through anything derived from this data, the paid
   path must be on licensed commercial terms. This is a gate, not a to-do.
2. **The decision is reversible by build.** Both vendors sit behind one adapter
   interface, so replacing yfinance everywhere later — or replacing Tiingo — is a config
   flip plus one adapter class, not surgery across the codebase.

## Context

`yfinance` reaches Yahoo Finance through an unofficial interface. Its own README
disclaims affiliation and points at Yahoo's terms, which do not permit commercial
redistribution of the data. Today that is not a violation: Archimedes is a testnet
product with no revenue. #1218's core observation is that **the posture changes at first
revenue, not at first use** — the cost appears at exactly the moment the business does.

Two things sharpened the problem after the issue was filed:

- **#1203** routed every strategy to its own declared universe rather than a single
  shared SPY feed, so market-data volume now scales with strategies × symbols × re-run
  cadence.
- There is no rate-limit SLA, no uptime guarantee and no support on the yfinance path. A
  silent upstream change breaks every backtest at once.

## Decision

### 1. Backtesting and paid analysis → Tiingo

Every path whose output could become something a user pays for — the generation-path
fusion/debate panel, the portfolio backtester, the universe sweep that feeds them — moves
to Tiingo when `MARKET_DATA_DAILY_PROVIDER=tiingo` (or, for back-compat,
`MARKET_DATA_PROVIDER=tiingo` with the daily variable unset — see § Amendment).

**Starting on the Free tier, deliberately, and only for testing.** The purpose of the
free tier here is to exercise the adapter against real vendor responses, real symbol
routing and real error shapes before committing money. Free-tier accounts are metered;
the adapter therefore (a) paces its own requests behind a politeness floor
(`TIINGO_MIN_REQUEST_INTERVAL_S`, default 1.1 s) and (b) surfaces Tiingo's own HTTP 429
as a distinct `TiingoRateLimitError` carrying the vendor's `Retry-After`.

The published per-hour and per-day ceilings are **not** hardcoded anywhere in the code.
They are account- and plan-dependent and they change; a number copied into a source file
reads as verified when it is not. The vendor's own 429 is the authority.

### 2. Explore → yfinance, with the reason stated on the page

Explore is a **free, open-source data viewer over yfinance streams. Nothing on it is sold
or commercially redistributed.** It exists to let someone look at prices and form an
opinion. Paid analysis runs on separately licensed data, on a different feed, sourced
independently on purpose.

That paragraph is not decoration — it is the whole justification for keeping yfinance on
this surface, so it is rendered on the page itself and pinned by
`ui/test/explore-data-disclosure.test.js`, which fails both when the disclosure is
removed and when the copy drifts into claiming a licence we do not hold.

### 3. Never mix vendors inside one run

Two guarantees, both enforced in code rather than described here:

- **Flag on, no token → refuse loudly.** A seam resolved to `tiingo` (by either
  variable) with no
  `TIINGO_API_TOKEN` raises `TiingoAPIKeyMissingError` at provider construction. It does
  not fall back to yfinance. A silent fallback would attach a "ran on licensed data"
  provenance to a run that did not.
- **A vendor's cache is not another vendor's cache.** `asset_daily_bars` rows record the
  vendor that wrote them, and the cache reads now filter on it. Before this, flipping the
  provider on a system with a warm cache — i.e. production — would have served yfinance
  bars for cached symbols and Tiingo bars for uncached ones *inside a single backtest
  panel*, with no error and no log line. The two vendors do not agree bar-for-bar. The
  cost of the fix is a cold cache on the first run after a flip; that is the intended
  trade.

### 4. Credential naming

`TIINGO_API_TOKEN` is canonical (Tiingo's own docs and its `Authorization: Token …`
header call it a token). `TIINGO_API_KEY` — the name the adapter originally shipped with
— is still read as a legacy alias so existing local `.env` files keep working; the
canonical name wins when both are set. Prod: SSM SecureString at
`/archimedes/prod/TIINGO_API_TOKEN`, owner-seeded.

## Amendment: per-seam routing (2026-09-01, #1798)

**What did not change:** a single **run** never mixes vendors. Every guarantee in § 3
stands — the refuse-loudly-on-missing-token rule, and the per-vendor `asset_daily_bars`
cache that makes a flip start cold rather than blend two vendors' bars inside one
backtest panel.

That cache guarantee is enforced on **both** sides of the row, and #1798 is what closed
the second one. The read side filters on `source`, so a `yfinance` row is never served to
a Tiingo caller. The write side matters just as much, because `asset_daily_bars` is
unique on `(symbol, trade_date)`: the close-only writer
(`get_daily_close_batch` → `_write_cached_series`) lands **on** the previous vendor's row
rather than beside it, and it knows only `close`. Assigning `close` + `source` there and
leaving `open/high/low/volume` alone would produce a bar whose Close is Tiingo's and
whose OHLV is yfinance's, stamped `source='tiingo'` — a blend the read filter cannot see,
because the row now claims to be the vendor being asked for. So a cross-vendor close-only
write **clears** the four columns it does not know and logs which vendor it displaced.
The row becomes the honest partial bar it is, the existing partial-bar guard treats it as
a miss, and the next OHLCV read refetches the whole range from the new vendor.

**What changed:** different **features** may run on different vendors, and the mechanism
now says so. Before this amendment there was one variable, `MARKET_DATA_PROVIDER`, read
by every call site. Since the Tiingo adapter serves daily bars only, flipping that one
variable would have moved the backtests onto licensed data *and simultaneously* pointed
the live oracle push, the paper-marks loop, the VIX/S&P regime reads and the Explore
history modal at three `NotImplementedError`s. The decision this ADR records — paid daily
analysis on Tiingo, the free viewer and the live surfaces on yfinance — was not
expressible in one variable.

So routing is by **seam**. A seam is the set of reads one feature makes inside one run:

| Seam | Env var | Serves | Refuses |
| --- | --- | --- | --- |
| `daily` | `MARKET_DATA_DAILY_PROVIDER`, falling back to `MARKET_DATA_PROVIDER`, then `yfinance` | `get_daily_close_batch`, `get_daily_ohlcv` | the intraday methods — **always**, whatever vendor is configured, so a call site cannot pass review on yfinance and break on the day of the flip |
| `intraday` | `MARKET_DATA_PROVIDER`, then `yfinance` | the whole interface, daily bars included | nothing |

The intraday seam serves daily bars *on purpose*: `oracle_updater.fetch_market_snapshot`
reads ^VIX (intraday) and ^GSPC's 50/200-day moving averages (daily) inside one run, and
"never mix vendors inside one run" is exactly what pins both to the same vendor. ^GSPC is
also an index ticker, which Tiingo does not serve at all.

`get_provider(seam=…)` is keyword-only and required — after the split there is no such
thing as "the active provider", and a call site that did not name its seam would be
picking a vendor by accident. `provider_name(seam)` is likewise required, so the vendor
name stamped on a row is always the vendor that actually served it.

### Which feature reads which seam

| Feature | Call site | Seam | Vendor today | After `MARKET_DATA_DAILY_PROVIDER=tiingo` |
| --- | --- | --- | --- | --- |
| Strategy signal evaluation (marketplace ticks: vault + paper rebalances) | `strategy_signal_evaluator._fetch_price_history(ies)` | `daily` | yfinance | **Tiingo** |
| Generation-path fusion/debate real-data panel | `fusion_market_data._fetch_one` | `daily` | yfinance | **Tiingo** |
| Generation-path portfolio backtester | `portfolio_backtester._fetch_price_panel` | `daily` | yfinance | **Tiingo** |
| Live oracle push — equities | `oracle_updater._fetch_yfinance` | `intraday` | yfinance | yfinance |
| Live oracle push — crypto seam leg (`ORACLE_CRYPTO_SOURCE`) | `oracle_updater._fetch_crypto_provider` | `intraday` | yfinance | yfinance |
| VIX read + the #775 cross-check's secondary reading | `oracle_updater._fetch_yfinance_single` | `intraday` | yfinance | yfinance |
| S&P 50/200-day regime moving averages | `oracle_updater._fetch_sp500_moving_averages` | `intraday` (daily bars, intraday seam — same run as the ^VIX read) | yfinance | yfinance |
| Paper-trading mark loop | `paper_marks.mark_all` | `intraday` | yfinance | yfinance |
| Explore per-asset history modal | `asset_market_service._fetch_yfinance_series` | `intraday` (arbitrary interval) | yfinance | yfinance |
| analytics-engine standalone CLI backtests | `archimedes_analytics_engine.data.fetch_ohlcv` | its own module-level seam, `MARKET_DATA_PROVIDER` only | yfinance | yfinance — **it registers no Tiingo adapter** |

The last row is the one to watch. `analytics-engine` is a separate, DB-less package with
its own one-vendor registry; the daily flip does not reach it, and must not, because a
`tiingo` value there would log "unknown provider" and serve yfinance — a run labelled
licensed that was not. What the flip does instead is stop routing daily OHLCV through
that package at all (backend selects `TiingoProvider`, and only `YFinanceProvider`
delegates to `fetch_ohlcv`), so the two never disagree about which vendor served a bar.
Moving the CLI path onto Tiingo is a follow-up: register an adapter there, then have it
read the daily variable.

### A vendor that cannot serve a seam is substituted, and said so

`_VENDOR_SEAMS` declares which seams each vendor can serve (`yfinance`: both; `tiingo`:
`daily`). Naming a vendor for a seam it cannot serve resolves that seam to yfinance and
**logs the substitution by name**. That is not the silent fallback § 3 forbids: § 3 is
about the missing-token case, which still raises `TiingoAPIKeyMissingError` at
construction, and here the returned vendor name is the vendor that really answered — so
every `source` stamp on an `asset_prices` row, a paper mark or an `asset_daily_bars` row
stays true. The practical effect is that the pre-#1798 global flip
(`MARKET_DATA_PROVIDER=tiingo`) now moves daily bars and leaves the live surfaces alone,
rather than breaking them.

### Proving the flip

The flip is not done when the variable is set; it is done when a pull on the daily seam
has been shown to come from the new vendor.

The packaged version of that proof is
[`../../scripts/verify_market_data.py`](../../scripts/verify_market_data.py), driven by
[`../runbooks/market-data-provider-proof.md`](../runbooks/market-data-provider-proof.md) —
it asks this seam by name over a fixed ten-bar window whose right answer is a constant, so
it can say NO. Prefer it; the recipe below is the same read done by hand, kept here because
the ADR should be readable without the runbook and because it is the form that works from
inside a task (the repo-root `scripts/` tree is not in the backend image).

The backend service runs with
`enable_execute_command = true` ([`../../infra/ecs.tf`](../../infra/ecs.tf)), and a
single-symbol read is exactly the "exec is for reading state" case
([`../runbooks/curated-backtests.md`](../runbooks/curated-backtests.md) § Alternative).

```bash
CLUSTER=archimedes
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name archimedes-backend \
  --desired-status RUNNING --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container backend \
  --interactive --command /bin/sh
```

Then, inside the task — one pull through the real seam, printing vendor + row count +
first/last bar date:

```sh
python -c "from datetime import date,timedelta; from archimedes.services.market_data_provider import get_provider,provider_name; p=get_provider(seam=\"daily\"); e=date.today(); s=e-timedelta(days=30); d=p.get_daily_ohlcv(\"SPY\",s.isoformat(),e.isoformat()); print(\"vendor:\",provider_name(\"daily\"),\"| rows:\",len(d),\"| first:\",d.index[0].date(),\"| last:\",d.index[-1].date(),\"| last close:\",float(d[\"Close\"].iloc[-1]))"
```

and the row it wrote, which is the provenance half of the proof:

```sh
python -c "from archimedes.db import get_session; from archimedes.models.asset_daily_bars import AssetDailyBar as B; s=get_session(); r=s.query(B).filter(B.symbol==\"SPY\").order_by(B.trade_date).all(); print(\"cached rows:\",len(r),\"| sources:\",sorted({x.source for x in r}),\"| first:\",r[0].trade_date,\"| last:\",r[-1].trade_date,\"| newest fetched_at:\",max(x.fetched_at for x in r))"
```

Paste both outputs on #1798. `vendor: tiingo` with a fresh `fetched_at` and
`sources: ['tiingo']` is the flip proven; `sources: ['tiingo', 'yfinance']` means the
sweep has not reached every symbol yet, which is expected mid-rollout and is *not* a
mixed bar — no single row can hold two vendors' columns (see § What did not change).

## The mainnet gate, stated explicitly

> **A Tiingo commercial (Business) plan is a prerequisite for mainnet.** Free-tier data is
> licensed for evaluation, not for a product that charges. Before real money moves —
> before the first paid verdict, not "before general availability" — the paid analysis
> path must be running on commercially licensed terms.

This is deliberately written as a gate rather than a roadmap item, because the failure
mode is silence: nothing breaks on the day we start charging, and the licensing posture
changes with no signal from any system. The trigger is **first revenue**, and it is the
same trigger #1218 identified.

Not decided here, and honestly unresolved: the actual quoted number. #1218 asked for
vendor comparisons (Polygon.io, Databento, Nasdaq Data Link) and volume math from the
post-#1203 reality. **That costing exercise is not complete, and this ADR does not
substitute a guess for it.** What this ADR settles is the *shape* — which surface uses
which vendor, and what has to be true before money moves. The number remains #1218's
open deliverable.

## Reversible by build

The point of doing this behind `MarketDataProvider` rather than by rewriting call sites:

- A new vendor is **one class** implementing the interface plus **one row** in
  `_VENDOR_PROVIDERS` and **one row** in `_VENDOR_SEAMS` declaring which seams it can
  serve (added by #1798).
- Selecting it is **one env var per seam** (`MARKET_DATA_DAILY_PROVIDER`,
  `MARKET_DATA_PROVIDER`), each read in one place.
- Replacing yfinance on Explore too — the full migration this ADR deliberately does *not*
  do today — is therefore a config change plus, at most, implementing the three methods
  the Tiingo adapter currently declines (`get_intraday_quote`,
  `get_intraday_quotes_batch`, `get_series`). No call site changes.

So the split is a **position, not a commitment**. If Yahoo's terms tighten, if Explore
starts monetising, or if maintaining two vendors costs more than one licence, collapsing
to a single vendor is a small, bounded change — and this ADR should then be superseded
rather than quietly ignored.

## Alternatives considered

| Option | Why not (today) |
| --- | --- |
| **Migrate everything to a commercial vendor now** | Pays for the Explore surface's volume — the largest symbol fan-out we have — to serve a page that generates no revenue. #1218's own anti-goal: don't migrate before the number and the revenue exist. |
| **Stay entirely on yfinance until first revenue** | Leaves the adapter unexercised until the exact moment it is load-bearing. Free-tier testing now is how the vendor's real error shapes get discovered cheaply. |
| **Polygon.io / Databento / Nasdaq Data Link instead of Tiingo** | Not evaluated against quoted tiers yet — that is #1218's open deliverable. Tiingo was implemented first because it was drafted (#1455) and has a usable free tier for exactly this testing purpose. The seam means this choice is not load-bearing. |
| **Keep both vendors live and cross-check them** | That is #775's cross-check, a different mechanism for a different purpose (oracle guardrail). Using it as a general data path would double cost and force a reconciliation policy for two vendors that legitimately disagree. |

## Consequences

**Accepted:**

- Two vendors to reason about, and a cache that cannot be shared between them.
- A cold cache on the first run after any provider flip.
- The Tiingo adapter covers daily bars only; intraday quotes and arbitrary-interval
  series are not implemented, so the live oracle push, the VIX/S&P regime reads and the
  Explore history modal are **not** cutover-ready. *(Amended 2026-09-01, #1798: they no
  longer have to be. Those surfaces sit on the `intraday` seam, which keeps its own
  vendor, so the daily-bar cutover does not wait on them and cannot break them. The
  `NotImplementedError`s remain as the belt-and-braces refusal — see the amendment.)*
- Tiingo has no endpoint for index (`^GSPC`, `^VIX`) or futures (`GC=F`, `CL=F`) tickers
  on the families this adapter targets. Those symbols raise
  `TiingoUnsupportedSymbolError` rather than silently falling back.

**Gained:**

- The licensing question has an answer per surface, and the answer is visible to users on
  the surface it applies to.
- The mainnet gate is written down where a future reader will look for it, rather than
  living in one person's head.
- Vendor choice stays cheap to revisit.
