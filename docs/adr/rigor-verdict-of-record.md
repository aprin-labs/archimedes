# ADR: The passport is the rigor verdict of record

> **Audience:** Archimedes team
> **Status:** **Accepted, pending quant sign-off**
> **Date:** 2026-09-01
> **Owner:** Dan Browne (quant reviewer of record: Önder Akkaya)
> **Supersedes:** the read-time-derivation premise of [#868](https://github.com/aprin-labs/archimedes/issues/868) — that a strategy's rigor verdict is computed when it is read. It does **not** supersede [#821](https://github.com/aprin-labs/archimedes/issues/821), whose principle survives tightened; see "What #821 said, and what still holds".
> **Superseded-by:** —
> **Question being decided:** When is a strategy's rigor verdict decided — every time somebody looks at it, or once, at backtest time?
> **Related:** [#1746](https://github.com/aprin-labs/archimedes/issues/1746), [#1747](https://github.com/aprin-labs/archimedes/issues/1747), [#1654](https://github.com/aprin-labs/archimedes/issues/1654) (board FDR, option 1), [#1760](https://github.com/aprin-labs/archimedes/issues/1760) / PR #1766 (backtests are frozen evidence), [`num-trials-self-containment.md`](num-trials-self-containment.md), [`rigor-gate-unification.md`](rigor-gate-unification.md), [`backend/archimedes/services/passport_loader.py`](../../backend/archimedes/services/passport_loader.py), [`backend/archimedes/services/rigor_gate_version.py`](../../backend/archimedes/services/rigor_gate_version.py).

## TL;DR

**Generation, backtesting and grading are one-time events.** A strategy is graded
ONCE, at backtest time, by the real gate. That verdict is persisted on the
strategy passport together with its inputs and its provenance, and **every
surface that shows a badge reads the stored verdict**. A re-grade is an explicit,
versioned event — never a silent overwrite, and never a recompute on read.

One named exception, stated up front rather than buried: the **deploy ladder**
(`GET /api/selection-bias/gate/{id}`, and the vault deploy check through it)
still grades live, so a badge and a deploy answer for one id can differ in
vintage. See decision 5.

The stored verdict must be written by the **real gate**, never by a fixture and
never by the generation-time synthesis verdict, and its provenance
(`graded_at`, `gate_version`, `cohort_n`) is what proves it.

Board-level FDR stays outside this: it is a live **relational** signal on the
leaderboard, off the passport ([#1654](https://github.com/aprin-labs/archimedes/issues/1654) option 1).

## The problem this decides

`strategy_passports.passes_rigor_gate` was **mixed-vintage**. Two different
things wrote it, and which one won depended on whether a background step
happened to run:

| Writer | When | What it means |
|---|---|---|
| `generation_pipeline._persist_candidate` | at synthesis, before any backtest exists | the **fusion/debate** gate's opinion (`c.rigor_verdict["passing"]`) |
| `_refresh_passport_real_metrics` / `_backtest_and_persist` | after a real backtest, if it ran and did not throw | the **real** gate's verdict over the real return series |

Both write the same column. Nothing recorded which one you were looking at. Both
post-backtest paths swallow their exceptions, so "the fusion verdict is still
sitting there" was an invisible, common state.

On top of that, the read surfaces did not agree about where the answer even
lived:

- `GET /api/strategies/{id}` **derived** a four-state badge at read time from the
  stored aggregate plus a freshly-loaded return series.
- `GET /api/strategies/passports/{id}` served the raw row, with no
  `rigor_gate_status` key at all.
- `GET /api/strategies/generated` — the Library's Generated tab — never looked at
  `strategy_passports`. It served `StrategyRecord.status` and
  `StrategyRecord.rigor_verdict`, **both written from the same synthesis blob**,
  so the tab's own demotion rule ("status says live, the gate says fail") was
  structurally unreachable.

The user-visible result (#1747): twenty-one strategies read **"Live ✓"** in the
Library while their own passports read **"Reference only — gate failed"**, with
no shared field between the two answers.

## The decision

1. **The passport row is the verdict of record.** `rigor_gate_status`
   (`pass` | `fail` | `pending` | `degenerate`), `passes_rigor_gate`,
   `graded_at`, `gate_version` and `cohort_n` are the verdict; the rigor numbers
   the same run produced (DSR, DSR p, PBO, OOS Sharpe) sit beside it.

2. **One writer.** Only the post-backtest grade writes them, and it writes all
   five together. Structurally, not by convention:
   `ingest_passport` reads `passport.passes_rigor_gate` **nowhere**; the verdict
   arrives only as an explicit `rigor_verdict=RigorVerdictWrite(...)`, whose
   `passes` is a **derived property** (`status == "pass"`), so the boolean and
   the four-state cannot be set apart.

3. **`pending` is the honest default.** A row nobody has graded says `pending`
   with `passes_rigor_gate = false` — fail-closed on admission, honest on the
   surface. A refresh that carries no grade leaves an existing verdict alone: it
   neither erases nor silently rewrites one.

4. **Provenance is mandatory.** `RigorVerdictWrite` fills `graded_at` and
   `gate_version` in `__post_init__`, so a verdict without provenance cannot be
   constructed. `gate_version` is a digest of everything that can move a verdict
   without the strategy's own returns moving — the strictness ladder, the
   always-on floors, the DSR/rf convention, the pending boundary, and a
   hand-bumped code revision. Its module docstring lists what is in and, just as
   importantly, what is deliberately out (the git SHA; board FDR).

5. **Readers read.** No surface recomputes the **badge** verdict — the
   pass/fail/pending/degenerate a user or an agent is shown for a strategy.
   `_passport_rigor_status` — the old read-time derivation — is retained off the
   request path as the oracle the migration's backfill rule is tested against,
   and as documentation of what the four states meant before they were stored.

   One thing still computes live, and this ADR does **not** close it: the
   **deploy ladder**. `_generated_strategy_rigor`
   (`backend/archimedes/api/selection_bias_routes.py`) runs `run_rigor_gate` over
   a generated strategy's persisted returns to answer
   `GET /api/selection-bias/gate/{id}`, which backs the Strategy Passport's
   Deploy button, the strictness slider, and — through
   `vaults_routes._strategy_rigor_status` — the server-side vault deploy check.
   So a passport badge and a deploy answer for the same id can differ in
   vintage: the badge is what the gate said when it graded, the ladder is what
   today's gate says now. That is a **named seam, not a closed one**. Whether
   deploy admission may read a stored answer at all is a separate decision —
   admission is the one place where recomputing against the *current* gate is
   arguably the safer behaviour, and it is deliberately left alone here.

6. **A re-grade is an event.** Re-grading calls the same single writer with a
   fresh `RigorVerdictWrite`, which rewrites all five fields and stamps a new
   `graded_at` / `gate_version`. There is no path that changes one of them alone.

## What #821 said, and what still holds

#821's rule was: **the badge must never be a stored value the gate never
derived** — no fixture booleans, no cached passes. That rule survives, tightened
rather than reversed. What #868 added on top — that the derivation should happen
*at read time* — is the part this ADR supersedes.

The distinction that makes both true at once: #821 forbids a verdict **no gate
produced**. It does not require the gate to run on every request. A verdict the
real gate produced, over the real persisted series, stamped with which gate
produced it, is exactly what #821 asked for — and a stamped, dated verdict is
*more* auditable than one recomputed invisibly on each read, because a recompute
that starts disagreeing with yesterday's answer leaves no trace that it did.

The tightening: #821's own escape hatch, the fail-closed `False` placeholder on
curated rows, was itself being served as a verdict by one surface. A placeholder
now has its own word — `pending` — so "the gate ran and it lost" and "no gate has
looked at this" can no longer render identically.

## Consequences

**Good.**
- One field, one meaning, one writer. For a **generated** id the Library row,
  the detail route and the passport route serve the same four-state, by
  construction. Curated ids are not there yet — see "Curated strategies go from
  a false `fail` to an honest `pending`" below; that is what PR-B is for.
- A verdict is now dated and attributable. "Which gate said that?" has an answer.
- The passport list route stopped paying a whole-cohort `get_all_daily_returns`
  per page — a query that projected and deserialized every winning row's
  `artifact_json` on an unbounded route. That is a cost saving, but it is a
  consequence, not the motive.
- The generation-time fusion verdict is no longer demoted OR promoted: it stays
  on `StrategyRecord.rigor_verdict` as the **debate record** — what the synthesis
  gate thought, worth keeping precisely because the real gate can disagree.

**Costs, stated plainly.**
- **Staleness is now visible instead of accidental.** A stored verdict can be
  older than the gate that would be applied today. That is the trade: the old
  behaviour was not fresher, it was undated. `gate_version` is how a reader
  sees it, and PR-C is how it gets closed.
- **One state gets less precise for legacy rows, in the fail-closed direction.**
  The old read path could see a row's return series and report `degenerate` for a
  zero-variance one. The backfill migration cannot (the series lives inside
  `backtest_results.artifact_json`), so a legacy generated row that today reads
  "Unevaluable — flat returns" reads "Reference only — gate failed" until PR-C
  re-grades it. Both are non-pass; neither renders green. Going forward the
  writer stores `degenerate` as itself, so this is a one-time cost on existing
  rows only.
- **Curated strategies went from a false `fail` to an honest `pending` (PR-A),
  and from `pending` to a real graded verdict (PR-B).** Every curated passport
  row's `passes_rigor_gate` used to be the #821 placeholder, not a gate result.
  PR-A replaced it with `pending` — true, but it meant
  `GET /api/strategies/passports/{id}` answered `pending` for a curated id while
  `GET /api/strategies/{id}` answered that id's live verdict, so the two routes
  still disagreed. PR-B closes it: the curated grade is produced by
  `services/curated_grading.py` when a curated backtest runs, stored on the same
  row, and the curated detail / list / leaderboard paths read it. **A curated row
  reads `pending` until the grading job has run against it — see the deploy step
  below.**
- **Two curated pill labels change wording, on a tab PR-A does not otherwise
  touch.** The Library's pill helpers now read the four-state, and they read it
  for curated rows too. A curated row whose live verdict is `pending` or
  `degenerate` used to render its store status ("Validated", "Candidate") and now
  renders "Not yet graded" / "Unevaluable — flat returns"; a `validated` row also
  drops from `tag-accent` to `tag-muted` in those two states. A curated row with
  a real `pass` is unchanged. This is intended — a `validated` store status
  beside an ungraded gate is the same false-confidence shape as the generated
  side — but it is movement on the curated tab and is called out here rather than
  discovered.
- **Two passport pill colours move, for the same reason.** The Strategy Passport
  header kept its own two-argument `statusTag` / `statusLabel` — they read
  `passes_rigor_gate` and never `rigor_gate_status` — so it imported the shared
  demotion *label* while keeping an unshared *decision*, and a live row with a
  `pending` or `degenerate` gate painted "Reference only — gate failed" beside
  that same header's "rigor gate pending" chip. The passport now calls
  `ui/src/libraryStatus.js` like the Library does. Consequence: `validated` was
  green on the passport and is `tag-accent` in the shared helper, and an
  unknown/`candidate` status was `tag-accent` and is `tag-muted`. Green is
  reachable from one place only — a `live` row with a literal `true` verdict.
  No graded state's wording changes.
- **A row can be published with no verdict.** `pending` is a real, reachable,
  non-green state; product copy has to have something to say for it ("Not yet
  graded").

## The three-PR program

| PR | Scope | State |
|---|---|---|
| **PR-A** | Generated strategies: the columns, the migration + backfill, the single writer, every generated read surface, the UI pill, the ADR. | landed (#1792) |
| **PR-B** | Curated grading: a real stored grade from `services/curated_grading.py`, written when a curated backtest runs; the curated detail / list / leaderboard read-time gate run retired. | this PR (#1746) |
| **PR-C** | The explicit re-grade of existing rows: a versioned event that replaces every `gate_version = 'legacy-derived'` verdict (and every `pending` the backfill could not resolve) with a real gate run. | after B |

The order is deliberate. PR-A makes the column mean one thing; PR-B makes it mean
that thing for curated rows too; PR-C fills it in for history. Doing C before A
would re-grade rows into a column that still had two meanings.

## PR-B: how a curated strategy gets graded, and the deploy step

**The grading job.** `services/curated_grading.py::grade_curated_library` runs
the real gate over the full curated library — the same cohort computation the
read path used to do per request, moved verbatim to the write side — and writes
each strategy's verdict, provenance and the four numbers that run produced onto
its passport row, through the single writer. Two entry points, both operator-run:

- `python -m archimedes.scripts.run_backtests` grades at the end of its run. New
  evidence, new grade, one job.
- `python -m archimedes.scripts.grade_curated` grades on its own. This is the
  re-grade, and the one-time backfill.

`backend/tests/test_curated_grading_is_write_side_only.py` is the choke point:
nothing under `backend/archimedes/` but those two scripts may reach the job, so a
recompute on read cannot come back under a new name.

**THE DEPLOY STEP.** Merging PR-B grades nothing. Every curated passport row is
`pending` until the job runs against the production database, and the curated
badge reads "Not yet graded" until then. After deploying, run the one-off Fargate
task exactly as `docs/runbooks/curated-backtests.md` § "Grading on its own" describes:

```
--overrides '{"containerOverrides":[{"name":"migrate",
  "command":["python","-m","archimedes.scripts.grade_curated"]}]}'
```

It reads persisted returns and writes verdicts; it runs no backtest and fetches
no market data, so it is minutes, not the ~15 of a library backtest run.

PR-B carries one schema change, `a4d7e1b93c2f` (`strategy_passports.display_metrics_source`
— which link of the curated display chain supplied a row's headline numbers), so
the normal `alembic upgrade head` step has to land before the grading task. The
column is nullable with no backfill and the read path falls back to its previous
per-request derivation while it is NULL, so a deploy that ran the migration but
not yet the grading job is a correct, if less informative, state.

**What a curated row can and cannot get from it.** A strategy with fewer than ten
persisted daily returns grades `pending` and stays there — the pairs family has
no persisted row at all, by design (`run_backtests` refuses to persist an
implausible artifact), so re-running the job does not move it. That is
fail-closed working, not a bug to automate around.

**Where the numbers come from now.** The four gate numbers
(`deflated_sharpe_ratio`, `dsr_p_value`, `pbo_score`, `out_of_sample_sharpe`)
moved out of `_update_record` and into the verdict write, so they can only be
written by a gate run. A row with no `graded_at` serves `None` for all four —
which is what keeps #1187's fixture snapshot, still sitting in those columns on
un-regraded rows, off the wire. `metrics_source` says `stored_grade` when a grade
produced them and `unavailable` when nothing did.

**The display metrics moved too, and this is the other half of #1746.** A curated
card's Sharpe came from a per-request `real_* → persisted backtest → stub` chain
while the passport row stored only the first link — `null` for a strategy with no
fixture row, which is why `1f9cfe96…` served `0.406` on one route and `null` on
the other, and why the number moved between two reads 37 s apart (the provider
memoises its backtest map per process; prod runs two tasks). The chain now runs
once, in `services/curated_metrics.py`, when the passport sync writes the row.
Same precedence, same numbers — decided by a writer instead of re-decided per
request per process. Whether a fixture snapshot *should* outrank a real persisted
backtest is a separate, open owner call; PR-B deliberately preserves today's
answer rather than changing a displayed number while fixing a disagreement.

**`served_status`.** `GET /api/strategies/passports/{id}` now publishes both the
persisted `status` column (which `?status=` filters on) and `served_status` — the
card status derived from the stored verdict by one shared helper, which is what
the detail route serves. For every id, curated or generated,
`detail.status == passport.served_status`. The curated CANDIDATE → VALIDATED
promotion is that derivation; it used to be driven by a live gate run made during
the request.

## Alternatives considered

**Read-time overlay (recompute on every request).** The shape the triage
originally proposed: leave the column alone and overlay a live verdict on each
read. Rejected by the owner. It keeps the answer undated and unattributable, it
makes the badge a function of *when you looked* (cohort-dependent inputs like PBO
move with the rest of the library), and it puts a ~6-second cohort gate run
behind a free, unauthenticated, documented agent route. It also would not have
fixed the underlying defect — the column would still be mixed-vintage; it would
just have stopped being read.

**Write the live re-grade back to `StrategyRecord.status` / `rigor_verdict`.**
Rejected: that destroys the record of what the synthesis gate thought, which is
the one thing that blob is good for, and `status == "live"` has downstream
readers (`marketplace_service.trending`) that are not about rigor at all.

**Add a `verdict_source` enum instead of a `gate_version` digest.** A source
label ("fusion" | "live_gate") separates the two vintages but does not separate
two live-gate runs under different thresholds — which is the harder and more
persistent problem. `gate_version` answers both: a fusion verdict simply never
gets one, because it never becomes a verdict at all.
