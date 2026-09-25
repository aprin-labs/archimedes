# arXiv q-fin corpus

The recency-biased quantitative-finance reading corpus that grounds every
Tier-1 strategy passport.

**Two producers, one manifest.** The committed `manifest.jsonl` is written by
[`scripts/bulk_ingest_arxiv.py`](../../scripts/bulk_ingest_arxiv.py) — the bulk
harvester, metadata-only, no PDFs.
[`backend/archimedes/services/arxiv_corpus.py`](../../backend/archimedes/services/arxiv_corpus.py)
builds a small corpus *with* PDF + text caches and is the right tool for a
few-hundred-paper run; it did not produce the file in this directory. Both
import the same category list (see below).

**Why recency-biased?** The Archimedes thesis is that *alpha decays as
novelty wears off*. The corpus skews bleeding-edge: it is sorted by submission
date, newest first, and `arxiv_corpus.py` trims to the most recent N. The bulk
harvester does not trim — it takes everything arXiv will give it.

## Coverage (measured 2026-08-31, re-measure rather than quote)

| | |
| --- | --- |
| Rows | **18,907** — equal to the live `totalResults` for the 8-category query on the harvest date |
| Published range | **1997-02-10 → 2026-08-28** |
| File size | 28.5 MB |
| Primary categories | `q-fin.ST` 2,383 · `q-fin.MF` 1,981 · `q-fin.GN` 1,742 · `q-fin.RM` 1,641 · `q-fin.CP` 1,610 · `q-fin.PM` 1,476 · `q-fin.TR` 1,381 · `q-fin.PR` 1,350 · then `math.PR`, `physics.soc-ph`, `cs.LG` (481) and other cross-listed primaries |
| PDFs cached | **0** |

> **Correcting the record.** Commit `2449f4b7` (2026-05-20, issue #97) described the
> 10,000-row manifest as "spanning 2008–2026". It did not: its actual range was
> **2018-07-31 → 2026-05-18**. The corpus reaches back to 1997 only as of this
> harvest. Don't reuse the older phrasing.

The row count grows as arXiv does. `wc -l data/corpus/manifest.jsonl` is the
authority for what is committed; `/health`'s `corpus_db_count` is the authority
for what is loaded.

## What is tracked in git

| Path                        | Tracked? | Notes                                              |
| --------------------------- | -------- | -------------------------------------------------- |
| `manifest.jsonl`            | ✅ yes    | One JSON object per paper. The corpus index.       |
| `README.md`                 | ✅ yes    | This file.                                         |
| `pdfs/<arxiv_id>.pdf`       | ❌ no     | gitignored — regenerate locally (sha256-cached).   |
| `text/<arxiv_id>.txt`       | ❌ no     | gitignored — pypdf-extracted body text.            |

The PDF + text caches are content-addressed and reproducible from the
manifest, so they are not committed. Only the metadata manifest is. **No PDF
has been fetched for the bulk corpus** — every `pdf_sha256` is `null` and every
`pdf_path` / `text_path` names a file that does not exist yet. Those paths are
deterministic promises, not evidence; full-text hydration is issue #1091.

## Manifest schema (frozen — one object per line)

```json
{
  "arxiv_id": "2401.12345",
  "title": "...",
  "authors": ["..."],
  "primary_category": "q-fin.PM",
  "categories": ["q-fin.PM", "q-fin.TR"],
  "published": "2024-01-22",
  "updated": "2024-02-01",
  "abstract": "...",
  "pdf_url": "https://...",
  "pdf_sha256": "<hex, or null if the PDF download/extract failed>",
  "pdf_path": "data/corpus/pdfs/2401.12345.pdf",
  "text_path": "data/corpus/text/2401.12345.txt",
  "fetched_at": "2026-05-16T...Z"
}
```

The manifest is **metadata-complete for every row**: title / authors /
abstract / categories / dates are always present even when the PDF was never
fetched. In that case `pdf_sha256` is `null` but `pdf_path` / `text_path` are
still named deterministically.

> **"Frozen" is now enforced, not just asserted (#1635).** Between 2026-05-20
> and 2026-08-31 the bulk harvester wrote a **10-key** row — no `pdf_path`,
> `text_path`, or `fetched_at` — so 8,000 of the 10,000 committed rows silently
> violated the schema this file calls frozen. Consumers survived only by
> accident (`corpus_service.py` falls back on a missing key;
> `hydrate_corpus.py` re-derives the paths). Every write now goes through
> `bulk_ingest_arxiv.normalize_row`, which emits all 13 keys and repairs rows
> carried over from an older manifest. Guarded by
> `backend/tests/test_corpus_uncap.py::TestManifestSchema`.

## Categories

The harvest terms are **one tuple, in one place**:
[`backend/archimedes/services/corpus_categories.py`](../../backend/archimedes/services/corpus_categories.py)
`QFIN_CATEGORIES`. Both producers import it.

`q-fin.CP q-fin.GN q-fin.MF q-fin.PM q-fin.PR q-fin.RM q-fin.ST q-fin.TR`

Two things a reader would otherwise get wrong:

- **`q-fin.EC` is not harvested.** arXiv retired it (aliased to `econ.GN`); the
  query returns 0 results. It was in the harvester's list from #97 until #1635,
  contributing nothing but a term. It survives only as a display label, for the
  benefit of any legacy row that still carries the tag.
- **There are no explicit cross-list queries**, and adding them would not help.
  arXiv's `cat:` operator matches *any* category tag on a paper, not just the
  primary — so a `cs.LG`-primary paper cross-listed to `q-fin.ST` is already
  returned by the q-fin query. (428 rows in the pre-#1635 manifest have a
  `cs.LG` primary, harvested by a pure q-fin query.) Formally,
  {cross-list ∧ q-fin} ⊆ {q-fin}: a co-tag-filtered cross-list query is a
  subset of what we already fetch. The only way it adds anything is by dropping
  the co-tag filter, which is the generic-ML leak we refuse.
  `arxiv_corpus.py` still names `cs.LG` / `stat.ML` / `econ.EM` because it
  issues one query *per category* under a per-category cap, a different
  mechanism with a different trade-off.

## Regenerate

Run from the **repo root** (the manifest paths are repo-root-relative), using
the `archimedes` conda env.

**Bulk (what produced the committed manifest) — metadata only, no PDFs:**

```bash
python scripts/bulk_ingest_arxiv.py                     # unbounded: drain every category
python scripts/bulk_ingest_arxiv.py --max 500           # smoke run
python scripts/bulk_ingest_arxiv.py --stop-when-caught-up   # cheap incremental top-up
```

`--max` is **unbounded by default**. It used to default to `10000`, which is
where the old ceiling came from.

> **Why one query per category rather than one OR-query.** arXiv's legacy Atom
> API answers **HTTP 500 for `start >= 10000`** on any single query — measured
> 2026-08-31: `start=9800` → 200 OK, `start=10000` → 500. The 8-category
> OR-query reports 18,907 `totalResults` and will not paginate to them, so the
> harvester pages each category separately (largest is `q-fin.ST` at ~4,300) and
> unions the results. Sum-with-duplicates across the 8 is ~24,200, which dedupes
> to the OR-query's total. If a single category ever crosses 10,000 the
> harvester logs `INCOMPLETE` and stops that category loudly — it does not
> silently truncate.

Re-runs are idempotent: an unbounded second run re-walks the pages and adds 0
rows (~12 minutes at the 5s politeness delay). `--stop-when-caught-up` is the
cheap alternative, at the cost of stopping each category at its first
all-duplicate page — **do not use it for a full harvest.**

**With PDFs + text extraction (small N only):**

```bash
python -m archimedes.services.arxiv_corpus --max 200            # PDFs best-effort
python -m archimedes.services.arxiv_corpus --max 200 --no-pdfs  # metadata-only
```

A full PDF-fetching run of ~200 papers takes several minutes (arXiv rate limits
to roughly one request every 3 seconds — that is expected). Fetching PDFs for
the whole bulk corpus is **~28 GB and ~16 h** and is deliberately not done here.

## What this corpus is *not*

Retrieval over it is **lexical**: keyword selection over title + abstract, with
a query-time MiniLM rerank of the top 150 candidates (`rerank_candidate_cap` on
`/health`; candidates past it are appended at score 0.0 and never scored).
There are **no embeddings at rest**, `corpus_meta` holds 0 rows, and the
knowledge graph is 0 entities / 0 relations. Do not describe this as semantic
search, RAG, or a knowledge graph — see #778.
