#!/usr/bin/env python3
"""Bulk ingest q-fin papers from arXiv API.

Pages through the arXiv search API in batches of 200, pulling every paper
matching the canonical q-fin category set (``QFIN_CATEGORIES``, imported —
never redeclared — from ``archimedes.services.corpus_categories``). Writes to
manifest.jsonl (append-safe, dedup by arxiv_id).

**One paged query per category**, unioned and deduped — not a single OR-query.
arXiv's legacy Atom API answers HTTP 500 for ``start >= 10000`` on any one
query, so the 8-category OR-query can report 18,907 ``totalResults`` and still
refuse to paginate past 10,000 of them. That, not just ``--max``, is what
pinned the corpus at exactly 10,000 rows. See ``ARXIV_DEEP_PAGE_LIMIT``.

Usage:
    python scripts/bulk_ingest_arxiv.py [--max N] [--output data/corpus/manifest.jsonl]

**``--max`` is unbounded by default** (#1635). The old ``default=10000`` was a
hard-coded harvest ceiling, not the size of the responsive literature — it left
roughly half the addressable corpus unharvested. With no ``--max`` the loop
walks the whole result set and terminates at arXiv's own ``totalResults`` (or an
empty page that survives ``EMPTY_PAGE_RETRIES`` retries). ``--max`` survives as
an opt-in for smoke runs.

The pre-#1635 "stop at the first page with no new papers" rule was **not** a
correct drain condition and is now opt-in behind ``--stop-when-caught-up``. The
feed is ordered by submission date, so a resumed run walks back into the region
the manifest already covers and hits a zero-new page long before the older tail:
measured on 2026-08-31, an otherwise-uncapped run stopped at **10,674 of 18,907**.

Resumable: reads the existing manifest to get already-ingested IDs and keeps
them, re-fetching only to discover what is missing. A second full run is
idempotent — it re-walks the pages and inserts 0 new rows.

Every row written — including rows carried over from an older manifest — is
normalized to the 13-key frozen schema documented in ``data/corpus/README.md``
(#1635: 8,000 of the 10,000 pre-existing rows were missing ``pdf_path`` /
``text_path`` / ``fetched_at``, and consumers only survived by accident).
"""

import argparse
import json
import logging
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from archimedes.services.corpus_categories import QFIN_CATEGORIES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://export.arxiv.org/api/query"
BATCH_SIZE = 200  # arXiv max per request
POLITE_DELAY = 5  # seconds between requests
BACKOFF_BASE = 60  # seconds, doubled on each 429
EMPTY_PAGE_RETRIES = 3  # transient empty deep-pagination pages to retry through

# arXiv's legacy Atom API answers HTTP 500 for `start >= 10000` on any single
# query — measured 2026-08-31: start=9800 → 200 OK, start=10000 → 500. This is
# the *real* reason the corpus sat at exactly 10,000: raising `--max` alone
# cannot get past it, because the 8-category OR-query reports 18,907
# totalResults and the API refuses to paginate to them.
#
# So the harvest is partitioned: one paged query per category (largest today is
# q-fin.ST at 4,347), unioned and deduped. Sum-with-duplicates is 24,227 across
# the 8 categories, which dedupes to the OR-query's 18,907.
ARXIV_DEEP_PAGE_LIMIT = 10000

# Cache paths recorded in the manifest are always repo-root-relative — same
# contract as ``arxiv_corpus.CorpusPaper.manifest_row``.
_PDF_REL = "data/corpus/pdfs"
_TEXT_REL = "data/corpus/text"

# The frozen manifest schema (data/corpus/README.md), in its canonical order.
MANIFEST_KEYS = (
    "arxiv_id",
    "title",
    "authors",
    "primary_category",
    "categories",
    "published",
    "updated",
    "abstract",
    "pdf_url",
    "pdf_sha256",
    "pdf_path",
    "text_path",
    "fetched_at",
)


def normalize_row(obj: dict, *, fetched_at: str) -> dict:
    """Return ``obj`` as the full 13-key frozen manifest row.

    Rows carried over from an older manifest may be missing ``pdf_path`` /
    ``text_path`` / ``fetched_at``; the two paths are deterministic functions
    of ``arxiv_id`` and are re-derived, while an existing ``fetched_at`` is
    preserved (it is provenance — this run did not fetch that row).
    """
    arxiv_id = str(obj.get("arxiv_id", "")).strip()
    row = {
        "arxiv_id": arxiv_id,
        "title": obj.get("title", ""),
        "authors": obj.get("authors", []),
        "primary_category": obj.get("primary_category", ""),
        "categories": obj.get("categories", []),
        "published": obj.get("published", ""),
        "updated": obj.get("updated", ""),
        "abstract": obj.get("abstract", ""),
        "pdf_url": obj.get("pdf_url"),
        "pdf_sha256": obj.get("pdf_sha256"),
        "pdf_path": obj.get("pdf_path") or f"{_PDF_REL}/{arxiv_id}.pdf",
        "text_path": obj.get("text_path") or f"{_TEXT_REL}/{arxiv_id}.txt",
        "fetched_at": obj.get("fetched_at") or fetched_at,
    }
    return {k: row[k] for k in MANIFEST_KEYS}


def load_existing_ids(path: Path) -> set[str]:
    """Load arxiv_ids from existing manifest."""
    ids = set()
    if not path.exists():
        return ids
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            aid = obj.get("arxiv_id", "").strip()
            if aid:
                ids.add(aid)
        except json.JSONDecodeError:
            continue
    return ids


def fetch_batch(search_query: str, start: int, max_results: int) -> tuple[list[dict], int]:
    """Fetch one page of ``search_query``. Returns ``(papers, total_available)``."""
    url = (
        f"{API_BASE}?search_query=({search_query})"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&start={start}&max_results={max_results}"
    )

    resp = httpx.get(url, timeout=60.0)
    resp.raise_for_status()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(resp.text)

    # Total results
    total_elem = root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    total = int(total_elem.text) if total_elem is not None else 0

    entries = root.findall("atom:entry", ns)
    papers = []

    for entry in entries:
        id_elem = entry.find("atom:id", ns)
        if id_elem is None or not id_elem.text:
            continue

        arxiv_url = id_elem.text.strip()
        arxiv_id = arxiv_url.split("/abs/")[-1]
        # Strip version suffix
        if "v" in arxiv_id and arxiv_id[-1].isdigit():
            parts = arxiv_id.rsplit("v", 1)
            if parts[1].isdigit():
                arxiv_id = parts[0]

        title_elem = entry.find("atom:title", ns)
        summary_elem = entry.find("atom:summary", ns)
        published_elem = entry.find("atom:published", ns)
        updated_elem = entry.find("atom:updated", ns)

        title = (title_elem.text or "").strip().replace("\n", " ") if title_elem is not None else ""
        abstract = (summary_elem.text or "").strip().replace("\n", " ") if summary_elem is not None else ""
        published = published_elem.text.strip()[:10] if published_elem is not None and published_elem.text else ""
        updated = updated_elem.text.strip()[:10] if updated_elem is not None and updated_elem.text else ""

        categories = []
        primary_category = ""
        for cat_elem in entry.findall("atom:category", ns):
            term = cat_elem.get("term", "")
            if term:
                categories.append(term)
        primary_category = categories[0] if categories else ""
        for pc in entry.findall("{http://arxiv.org/schemas/atom}primary_category"):
            primary_category = pc.get("term", primary_category)

        authors = []
        for author_elem in entry.findall("atom:author", ns):
            name_elem = author_elem.find("atom:name", ns)
            if name_elem is not None and name_elem.text:
                authors.append(name_elem.text.strip())

        pdf_url = arxiv_url.replace("/abs/", "/pdf/") + ".pdf"

        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": authors,
                "abstract": abstract,
                "primary_category": primary_category,
                "categories": categories,
                "published": published,
                "updated": updated,
                "pdf_url": pdf_url,
                "pdf_sha256": None,
            }
        )

    return papers, total


def harvest_query(
    all_papers: dict[str, dict],
    *,
    search_query: str,
    label: str,
    max_total: int | None,
    stop_when_caught_up: bool,
) -> int:
    """Page ``search_query`` to exhaustion, merging into ``all_papers``.

    Returns the number of newly added papers. Termination, in order:
      * ``max_total`` reached (only when ``--max`` was given);
      * ``start`` has walked past this query's own ``totalResults``;
      * an empty page that survives ``EMPTY_PAGE_RETRIES`` retries;
      * ``ARXIV_DEEP_PAGE_LIMIT`` — a loud, logged *incomplete* stop, never a
        silent one (see the constant for why the API forces it).
      * a page with no new papers, **only** under ``--stop-when-caught-up``.
    """
    start = 0
    new_count = 0
    backoff = BACKOFF_BASE
    total_available = 0
    empty_page_retries = 0

    while True:
        # No --max means no ceiling: the loop walks the whole result set and
        # terminates at arXiv's own totalResults (or an empty page).
        remaining = float("inf") if max_total is None else max_total - len(all_papers)
        if remaining <= 0:
            logger.info("[%s] Reached target of %d papers", label, max_total)
            break

        if total_available and start >= total_available:
            logger.info("[%s] Walked the full result set (%d of %d), done", label, start, total_available)
            break

        if start >= ARXIV_DEEP_PAGE_LIMIT:
            # Loud absence, not a silent truncation: this query has more results
            # than the API will paginate to, so the harvest for it is INCOMPLETE
            # and the operator has to partition it further (by date window).
            logger.error(
                "[%s] INCOMPLETE: arXiv refuses start>=%d, but %d results exist — %d unreachable",
                label,
                ARXIV_DEEP_PAGE_LIMIT,
                total_available,
                max(total_available - start, 0),
            )
            break

        # `remaining + 200` is `inf` when unbounded and a float whenever
        # `remaining` is one, so coerce: the URL must never render
        # `max_results=inf` or `max_results=200.0` (arXiv answers 400).
        batch_size = int(min(BATCH_SIZE, remaining + 200))  # fetch a bit extra for dups
        logger.info("[%s] Fetching batch start=%d, max_results=%d...", label, start, batch_size)

        try:
            papers, total_available = fetch_batch(search_query, start, batch_size)
            backoff = BACKOFF_BASE  # reset on success
        except Exception as exc:
            logger.error("[%s] arXiv API error: %s", label, exc)
            logger.info("[%s] Waiting %ds before retry...", label, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)  # cap at 5 min
            continue

        if not papers:
            # arXiv intermittently answers a deep-pagination page with zero
            # entries even when more results exist. Retry before concluding the
            # feed is exhausted — otherwise one blip silently truncates the
            # harvest and the manifest looks "done" thousands of papers short.
            if start < total_available and empty_page_retries < EMPTY_PAGE_RETRIES:
                empty_page_retries += 1
                logger.warning(
                    "[%s] Empty page at start=%d with %d available — retry %d/%d",
                    label,
                    start,
                    total_available,
                    empty_page_retries,
                    EMPTY_PAGE_RETRIES,
                )
                time.sleep(POLITE_DELAY * empty_page_retries)
                continue
            logger.info("[%s] No more papers from arXiv API", label)
            break
        empty_page_retries = 0

        batch_new = 0
        for paper in papers:
            aid = paper["arxiv_id"]
            if aid not in all_papers:
                all_papers[aid] = paper
                batch_new += 1

        new_count += batch_new
        logger.info(
            "[%s] Batch: %d fetched, %d new, total unique: %d / %d in this query",
            label,
            len(papers),
            batch_new,
            len(all_papers),
            total_available,
        )

        if batch_new == 0 and start > 0 and stop_when_caught_up:
            # Cheap incremental top-up mode only. This is NOT a safe default:
            # the feed is ordered by submission date, so a resumed run walks
            # back into the region the manifest already covers and hits a
            # zero-new page *thousands of papers before* the older tail. That
            # is exactly how the 10,000-row ceiling survived a re-run (#1635) —
            # measured 2026-08-31: it stopped at 10,674 of 18,907.
            logger.info("[%s] No new papers in batch and --stop-when-caught-up set, stopping", label)
            break

        start += len(papers)
        time.sleep(POLITE_DELAY)

    return new_count


def main():
    parser = argparse.ArgumentParser(description="Bulk ingest q-fin papers from arXiv")
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="Optional target paper count (smoke runs). Default: unbounded — drain every category.",
    )
    parser.add_argument("--output", type=str, default="data/corpus/manifest.jsonl")
    parser.add_argument(
        "--stop-when-caught-up",
        action="store_true",
        help=(
            "Cheap top-up: stop each category at the first page with no new papers instead "
            "of walking its whole result set. Do NOT use for a full harvest — it stops "
            "thousands of papers early on a resumed run (#1635)."
        ),
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    existing_ids = load_existing_ids(output_path)
    logger.info("Existing papers: %d", len(existing_ids))

    # We'll overwrite with deduped set
    all_papers: dict[str, dict] = {}
    if output_path.exists():
        for line in output_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                aid = obj.get("arxiv_id", "").strip()
                if aid:
                    all_papers[aid] = obj
            except json.JSONDecodeError:
                continue

    # One paged query per category, unioned and deduped by arxiv_id. A single
    # OR-query cannot reach the full corpus — see ARXIV_DEEP_PAGE_LIMIT.
    new_count = 0
    for category in QFIN_CATEGORIES:
        if args.max is not None and len(all_papers) >= args.max:
            logger.info("Reached target of %d papers; skipping remaining categories", args.max)
            break
        new_count += harvest_query(
            all_papers,
            search_query=f"cat:{category}",
            label=category,
            max_total=args.max,
            stop_when_caught_up=args.stop_when_caught_up,
        )

    # Write sorted by published date (newest first), every row normalized to
    # the 13-key frozen schema — including rows carried over from an older
    # manifest that predate it (#1635).
    fetched_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sorted_papers = [
        normalize_row(p, fetched_at=fetched_at)
        for p in sorted(
            all_papers.values(),
            key=lambda p: p.get("published", ""),
            reverse=True,
        )
    ]

    # Write to temp then atomic rename
    tmp_path = output_path.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for p in sorted_papers:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    tmp_path.rename(output_path)

    logger.info(
        "Done: %d papers written to %s (%d new)",
        len(sorted_papers),
        output_path,
        new_count,
    )


if __name__ == "__main__":
    main()
