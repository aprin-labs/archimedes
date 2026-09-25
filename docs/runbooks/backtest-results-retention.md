# `backtest_results` retention — archive, then prune

> **status:** current
> **owner:** Dan Browne
> **updated:** 2026-08-30
> **superseded-by:** —

**Scope:** running `backend/archimedes/scripts/archive_backtest_results.py` to shrink the
`backtest_results` table without losing gate-relevant or passport-referenced history.

**Why this exists (verified 2026-08-30):** `backtest_results` is ~6.3GB across 14,857 rows
for 96 strategies (~155 runs/strategy). `artifact_json` averages ~349KB/row and
`equity_curve_json` averages ~63KB/row — those two `TEXT` columns are almost the entire
table's size. `deflated_sharpe_ratio` (a proxy for "the rigor gate scored this exact row")
is `NULL` on 93% of rows; the other ~7% are gate-relevant. v8 Lane 3.1's owner-chosen policy
(2026-08-30) is **archive to S3, then prune** — never delete without an archived,
verified copy first.

## 0. Before you run anything

The script is **read-only against the database by default**. `--plan` (also what running
with no flags does) only reports; `--archive` and `--prune` are the only modes that touch
anything, and both require `ARCHIVE_BUCKET` to be set explicitly — there is no default
bucket, so a missing env var refuses rather than guessing.

```bash
cd backend  # or set PYTHONPATH=backend from the repo root
python -m archimedes.scripts.archive_backtest_results --plan
```

Read the plan output before doing anything else. It reports, without touching S3 or the
DB's contents:

- total rows
- how many rows each keep-rule protects (recent-N, gate-relevant, passport-referenced) and
  their de-duplicated union
- the doomed count (rows with no keep-rule covering them)
- an **estimated** space freed, from the 2026-08-19 column-size averages above — not a
  per-row measurement, and never used to decide what gets archived/pruned (that decision is
  always row-count-exact against the live schema)

## 1. The keep policy

Per `strategy_id`, a row survives archive-then-prune if **any** of:

| # | Rule | Why |
|---|---|---|
| 1 | Among the `--keep-recent-n` (default **5**) most recent rows for that `strategy_id` | Recent history for debugging/comparison, even for strategies nothing has gated yet |
| 2 | `deflated_sharpe_ratio IS NOT NULL` | The rigor gate graded this exact row — gate-relevant, keep regardless of age |
| 3 | It is the **latest** row for a `strategy_id` that has a `strategy_passports` row | See § 2 — the closest available proxy for "referenced by a passport" |

`--keep-recent-n` uses the same `ORDER BY created_at DESC, id DESC` tie-break as
`backend/archimedes/services/backtest_repository.py`'s `get_daily_returns` /
`latest_backtests_by_strategy` — "recent" here means the same thing it means everywhere
else in the codebase that reads this table.

## 2. Why rule 3 exists, and its one honest limitation

`strategy_passports` has **no foreign key** onto `backtest_results.id`. It denormalizes a
*snapshot* of one backtest run onto the passport row instead (`sharpe_ratio`, `cagr`,
`deflated_sharpe_ratio`, `backtest_start`/`_end`, ... — see
`models/strategy_passport_record.py`'s "Backtest results (denormalized for query speed)"
section). The only real linkage between the two tables is
**`strategy_passports.id == backtest_results.strategy_id`** — confirmed by
`passport_loader.get_passport()` (`filter_by(id=strategy_id)`) and every
`backtest_repository` function filtering the same table by the same `strategy_id` string.

Because the denormalized snapshot is always written from whatever `backtest_repository`
treated as canonical for that `strategy_id` at ingest/refresh time — and every function
there defines "canonical" as the latest row by the same tie-break rule 1 uses — this script
operationalizes "referenced by a passport" as **the latest `backtest_results` row for every
`strategy_id` that has a `strategy_passports` row**. That is the conservative reading: if a
passport's displayed numbers came from any `backtest_results` row, it was this one.

**Known limitation, by design, not a bug:** rule 3 protects only the *current* latest row.
If a strategy has been re-run many times since its passport's snapshot was last refreshed,
an older row that the passport once denormalized (and that a stale UI view might still be
showing) is **not** specially protected by rule 3 once superseded — it survives only if
rule 1 or rule 2 also cover it. `backend/tests/test_archive_backtest_results.py::TestKeepPolicy::test_passport_rule_does_not_reach_back_to_a_superseded_snapshot_row`
pins this down explicitly so it stays a documented tradeoff, not a silent regression.
Rule 3 is a strict subset of rule 1 whenever `--keep-recent-n >= 1` — its one case of doing
independent work is `--keep-recent-n 0`.

## 3. Archive

Requires `ARCHIVE_BUCKET` (and standard AWS credential resolution — same boto3 credential
chain the script builds at `archimedes/scripts/archive_backtest_results.py:321`; `AWS_REGION`
defaults to `us-east-1`).

```bash
ARCHIVE_BUCKET=archimedes-backtest-archive-prod \
    PYTHONPATH=backend python -m archimedes.scripts.archive_backtest_results --archive
```

What it does:

1. Recomputes the doomed-id set (same query `--plan` used, so a `--plan` run immediately
   before `--archive` is a reliable preview — barring a concurrent write to the table).
2. Streams doomed rows in batches of `--batch-size` (default 100; `--keep-recent-n` still
   applies), each batch
   serialized as gzipped JSONL (every column, `datetime`/`date` as ISO strings) to
   `s3://$ARCHIVE_BUCKET/backtest-results-archive/YYYY-MM-DD/batch-NNNNN.jsonl.gz`.
3. Writes a `manifest.json` alongside at
   `s3://$ARCHIVE_BUCKET/backtest-results-archive/YYYY-MM-DD/manifest.json` recording, per
   batch: the S3 key, row count, sha256 of the uploaded (gzipped) bytes, and the exact row
   ids in that batch. The manifest's `total_rows` is the sum of every batch's `row_count`.
4. **Deletes nothing.** Archive is always safe to re-run; each run is dated and
   independent.

If a batch comes back short (a row vanished between the id-listing query and the batch
fetch — e.g. a concurrent prune), the script raises rather than silently writing an
incomplete batch as if it were whole.

**`--batch-size` is the memory knob.** A whole batch's rows are held in memory with their
~412KB/row `artifact_json` + `equity_curve_json` payloads, serialized to one JSONL string,
then gzipped — several copies of the batch coexist at the peak, so RSS grows several times
faster than the raw row bytes. Measured 2026-08-30 on production-sized rows (349KB
`artifact_json` + 63KB `equity_curve_json`), peak process RSS during one `run_archive`:

| `--batch-size` | raw row bytes | peak RSS | over baseline |
| --- | --- | --- | --- |
| 100 (default) | 41MB | **283MB** | +128MB |
| 500 (previous default) | 206MB | **942MB** | +595MB |

The default is 100 because a ~1GB spike is a real OOM risk at the 512MB–1GB task sizes this
runs under. Raise it only with known headroom, and re-measure rather than trusting these
figures if the per-row payload changes.

**Pass `--run-date YYYY-MM-DD` to both halves of one operator run.** The S3 prefix defaults
to today in UTC, so a `--archive` that starts at 23:58Z and a `--prune` that starts at
00:03Z resolve *different* prefixes and the prune refuses — after a full archive has already
been paid for. Pinning both to the same date removes the race:

```bash
RUN_DATE=$(date -u +%F)
ARCHIVE_BUCKET=... PYTHONPATH=backend python -m archimedes.scripts.archive_backtest_results \
    --archive --run-date "$RUN_DATE"
ARCHIVE_BUCKET=... PYTHONPATH=backend python -m archimedes.scripts.archive_backtest_results \
    --prune --run-date "$RUN_DATE"
```

## 4. Prune

Requires the **run date's** manifest to already exist and verify clean in S3 — this is the
load-bearing guard, and it has no override flag:

```bash
ARCHIVE_BUCKET=archimedes-backtest-archive-prod \
    PYTHONPATH=backend python -m archimedes.scripts.archive_backtest_results --prune
```

What it does, in order:

1. **Reads back the run date's manifest** (`--run-date`, default today in UTC). Missing →
   refuses (`ManifestNotFound`). No flag bypasses this — run `--archive` first.
2. **Verifies it, bytes then content.** Recomputes `sum(batch.row_count)` against the
   manifest's own `total_rows` (internal consistency), re-downloads every batch object and
   compares its sha256 against the manifest's recorded value, and then **decompresses each
   batch and compares the row ids actually inside it against the `row_ids` the manifest
   claims for it.** Any mismatch — a corrupted upload, a partial re-upload, tampering —
   refuses (`ManifestVerificationFailed`) and deletes nothing.

   The content step is not redundant with the sha256 step. A matching sha256 proves the
   object is byte-identical to whatever the manifest was written against; it says nothing
   about *which rows* are inside. `row_ids` is what step 4 deletes off, so a manifest whose
   `row_ids` name a row the batch does not contain — a hand-edited manifest, or one
   regenerated after a partial re-run — is a licence to delete an unarchived row, and it
   passes a bytes-only check cleanly.
3. **Recomputes the CURRENT keep-set fresh** (not trusted from archive time) and only
   deletes `archived_ids − current_keep_ids`. This is defense in depth: if the keep policy's
   view of the world changed since archiving (e.g. a `strategy_passports` row now points at
   an already-archived id), that row is skipped and logged, never deleted — even though it
   is sitting in a verified manifest. Having an extra archived copy of a kept row is
   harmless; deleting a kept row is not.
4. **Deletes in batches of `--batch-size`**, committing per batch, and reports the exact deleted count
   against the manifest's total and the skipped-as-now-kept count. A batch that deletes
   fewer rows than requested (some id already gone — e.g. a double-run) is logged as a
   warning, not silently swallowed.

## 5. After pruning — VACUUM

The script prints this guidance and **does not run it**:

```sql
VACUUM (VERBOSE, ANALYZE) backtest_results;
```

Deleting rows does not shrink the table on disk or reclaim space for anything other than
future inserts into the same table — Postgres/Aurora needs a `VACUUM` pass for that. This is
left to an operator watching the primary during a low-traffic window rather than run
automatically, because `VACUUM FULL` (or a plain `VACUUM` under heavy concurrent write load)
can hold locks and generate significant I/O. If bloat is severe enough that a plain
`VACUUM`'s dead-tuple reclaim isn't sufficient, prefer `pg_repack` over `VACUUM FULL` — it
rebuilds the table without holding an exclusive lock for the whole operation.

## 6. Recovering an archived row

Nothing automates this yet (no restore subcommand). To manually recover a row: find its id
in the relevant day's `manifest.json` (`row_ids` per batch), download that batch's
`.jsonl.gz`, `gzip -d` it, and `grep`/`jq` the JSON line whose `"id"` matches — every column
is present as a plain JSON value (datetimes as ISO strings), so it can be re-inserted with a
one-off script if ever needed. Filing a proper `--restore <id>` subcommand is a reasonable
follow-up if recovery turns out to be a repeated need.

## 7. Testing

`backend/tests/test_archive_backtest_results.py` is hermetic (tmp-file SQLite via
`tests.db_isolation.redirect_to_tmp_sqlite`, an in-process fake S3 client — no network, no
moto, no real AWS credentials required):

```bash
env -i HOME=$HOME PATH=$PATH PYTHONPATH=backend python -m pytest \
    backend/tests/test_archive_backtest_results.py -q
```

Covers keep-policy correctness (per-strategy recent-N, gate-relevant rows surviving
regardless of age, the passport safety-net case and its documented limitation), the
archive-before-prune guard (missing manifest refuses; a tampered/corrupted batch refuses;
a *byte-valid* batch whose contents disagree with the manifest's `row_ids` refuses; a batch
that is not readable as archived rows refuses), batch accounting (archive splits into the
requested batch size with a manifest whose row counts sum correctly; prune deletes exactly
the archived non-kept rows and never a row that became kept after archiving), and CLI
wiring (`--batch-size` / `--run-date` reach both `run_archive` and `run_prune`;
`--batch-size` rejects non-positive values; a run pinned to one `--run-date` prunes against
its own manifest while the next day's date refuses).
