"""arXiv q-fin corpus scraper + manifest builder (Dan's lane — Stream A).

Builds the recency-biased quantitative-finance reading corpus that grounds
every Tier-1 strategy passport. The Archimedes thesis is that *alpha decays
as novelty wears off*, so the corpus must skew bleeding-edge: we over-fetch
recent submissions across the q-fin categories (plus the q-fin-adjacent
cs.LG / stat.ML / econ.EM cross-listings) sorted by submission date, then
trim to the most recent ``--max`` papers after dedupe.

Lifts the conventions of ``arxiv_pipeline.py``:
  - sha256 content-addressed PDF + text caches (re-runs are free / idempotent)
  - defensive page-by-page ``pypdf`` extraction — one bad page never aborts
  - heavy imports (``arxiv``, ``pypdf``, ``requests``) are lazy so this module
    stays importable in dependency-light environments
  - the search + downloader seams are injectable for offline tests

Honesty rule (the project thesis, enforced here): the manifest is
metadata-complete for the full target N even when a PDF download or text
extraction fails — ``pdf_sha256`` is then ``null`` but the row (title,
authors, abstract, categories, dates) is always present and the cache paths
are still named deterministically. A failed PDF is logged, never fatal.

CLI:
    python -m archimedes.services.arxiv_corpus --max 200 \\
        --out data/corpus/manifest.jsonl

Run from the repo root (the ``data/corpus`` paths in the manifest are
repo-root-relative by design).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from archimedes.services.corpus_categories import QFIN_CATEGORIES

logger = logging.getLogger(__name__)

# ── Corpus definition ───────────────────────────────────────────

# The core q-fin taxonomy is imported, never redeclared: before #1635 this
# module carried its own 7-category tuple (no `q-fin.GN`) while
# `scripts/bulk_ingest_arxiv.py` carried a different 9-category list — and the
# script's list is what actually produced the manifest. `corpus_categories`
# is now the single source of truth for both.

# q-fin-adjacent: a lot of the bleeding-edge ML-for-markets work is
# cross-listed here rather than under a q-fin primary. We still want it,
# but only when it is co-tagged q-fin (see _is_qfin_relevant).
#
# These matter *here* only because `_default_search` issues one query per
# category. They are deliberately NOT part of the bulk-harvest OR-query
# (#1635): arXiv's `cat:` matches any tag on a paper, not just the primary, so
# {cross-list ∧ q-fin} ⊆ {q-fin} — a co-tag-filtered cross-list query is a
# subset of what the q-fin query already returns and adds zero recall. The only
# way it would add anything is by dropping the co-tag filter, which is the
# generic-ML leak we refuse.
QFIN_CROSS_CATEGORIES: tuple[str, ...] = (
    "cs.LG",
    "stat.ML",
    "econ.EM",
)

ALL_CATEGORIES: tuple[str, ...] = QFIN_CATEGORIES + QFIN_CROSS_CATEGORIES

DEFAULT_MAX_PAPERS = 200
# Over-fetch factor: pull this multiple of --max before trimming, so the
# recency trim + dedupe + cross-list filter still leaves a full corpus.
_OVERFETCH_FACTOR = 2.0
_MIN_OVERFETCH = 400

DEFAULT_OUT = Path("data/corpus/manifest.jsonl")
DEFAULT_PDF_DIR = Path("data/corpus/pdfs")
DEFAULT_TEXT_DIR = Path("data/corpus/text")
# Cache paths recorded in the manifest are always repo-root-relative.
_PDF_REL = "data/corpus/pdfs"
_TEXT_REL = "data/corpus/text"

# Polite to the arXiv API: their guidance is a single request every ~3s.
_API_DELAY_SECONDS = 3.0
# Same polite delay between PDF downloads. This is the *production default*
# for ``build_corpus(delay_seconds=...)`` — the seam exists so offline callers
# (tests) can pay 0 without ever changing what a real harvest sends to arXiv.
_PDF_DOWNLOAD_DELAY = 3.0
_API_PAGE_SIZE = 100
_PDF_TIMEOUT_SECONDS = 30
_PDF_MAX_RETRIES = 3
_PDF_BACKOFF_BASE = 10  # seconds; exponential: 10, 20, 40


# ── Records ─────────────────────────────────────────────────────


@dataclass
class CorpusPaper:
    """Normalized arXiv record. Mirrors the frozen manifest schema 1:1."""

    arxiv_id: str  # bare id, no version suffix (e.g. "2401.12345")
    title: str
    authors: list[str]
    primary_category: str
    categories: list[str]
    published: str  # YYYY-MM-DD
    updated: str  # YYYY-MM-DD
    abstract: str
    pdf_url: str
    published_dt: datetime = field(repr=False, default=None)  # type: ignore[assignment]

    def manifest_row(
        self,
        *,
        pdf_sha256: str | None,
        fetched_at: str,
    ) -> dict:
        """Emit exactly the frozen manifest schema (key order is stable)."""
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "authors": self.authors,
            "primary_category": self.primary_category,
            "categories": self.categories,
            "published": self.published,
            "updated": self.updated,
            "abstract": self.abstract,
            "pdf_url": self.pdf_url,
            "pdf_sha256": pdf_sha256,
            "pdf_path": f"{_PDF_REL}/{self.arxiv_id}.pdf",
            "text_path": f"{_TEXT_REL}/{self.arxiv_id}.txt",
            "fetched_at": fetched_at,
        }


# ── Helpers ─────────────────────────────────────────────────────


def _bare_id(short_id: str) -> str:
    """Strip the trailing version (``2401.12345v3`` → ``2401.12345``).

    Old-style ids carry a category prefix (``q-fin.PM/0703001v1``); keep the
    full path but drop the version so dedupe is by paper, not by revision.
    """
    sid = short_id.strip()
    if "v" in sid:
        head, _, tail = sid.rpartition("v")
        if tail.isdigit() and head:
            return head
    return sid


def _ymd(dt: datetime | None) -> str:
    return dt.date().isoformat() if dt is not None else ""


def _is_qfin_relevant(primary: str, categories: Iterable[str]) -> bool:
    """Keep the paper if it is genuinely q-fin.

    Core q-fin primary → always in. A cross-list category (cs.LG/stat.ML/
    econ.EM) only counts when the paper is *also* tagged q-fin somewhere —
    otherwise a generic ML paper with no finance content leaks in.
    """
    cats = set(categories) | {primary}
    return any(c.startswith("q-fin.") for c in cats)


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 1. Search (injectable seam) ─────────────────────────────────


def _default_search(categories: Iterable[str], limit: int) -> Iterator[CorpusPaper]:  # noqa: ARG001 — categories scoped via the per-category loop inside the body; declared for forward-compat filter
    """Live arXiv search, recency-first, across the category union.

    arXiv's API caps a single boolean OR query well below what we need and
    paginates unreliably past a few hundred results, so we query each
    category independently (sorted by submission date, newest first) and let
    the caller dedupe + recency-trim the merged stream. ``limit`` is the
    per-category cap.
    """
    import arxiv  # lazy: keeps the module importable without the dep

    client = arxiv.Client(
        page_size=_API_PAGE_SIZE,
        delay_seconds=_API_DELAY_SECONDS,
        num_retries=5,
    )
    for category in ALL_CATEGORIES:
        search = arxiv.Search(
            query=f"cat:{category}",
            max_results=limit,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        logger.info("arxiv: querying cat:%s (cap %d)", category, limit)
        try:
            results = client.results(search)
            for result in results:
                paper = _result_to_paper(result)
                if paper is not None:
                    yield paper
        except Exception as exc:
            logger.warning("arxiv: category %s failed: %s", category, exc)


def _result_to_paper(result: object) -> CorpusPaper | None:
    """Map an ``arxiv.Result`` to our normalized record. Defensive."""
    try:
        short_id = result.get_short_id()  # type: ignore[attr-defined]
        primary = getattr(result, "primary_category", "") or ""
        categories = list(getattr(result, "categories", []) or [])
        if not _is_qfin_relevant(primary, categories):
            return None
        published = getattr(result, "published", None)
        updated = getattr(result, "updated", None)
        return CorpusPaper(
            arxiv_id=_bare_id(short_id),
            title=" ".join(str(result.title).split()),  # type: ignore[attr-defined]
            authors=[a.name for a in result.authors],  # type: ignore[attr-defined]
            primary_category=primary,
            categories=categories,
            published=_ymd(published),
            updated=_ymd(updated),
            abstract=" ".join(str(result.summary).split()),  # type: ignore[attr-defined]
            pdf_url=str(getattr(result, "pdf_url", "") or ""),
            published_dt=published,
        )
    except Exception as exc:
        logger.debug("arxiv: skipping unparseable result: %s", exc)
        return None


# ── 2. Dedupe + recency trim ────────────────────────────────────


def _dedupe_and_trim(papers: Iterable[CorpusPaper], max_papers: int) -> list[CorpusPaper]:
    """Dedupe by bare arxiv_id, then keep the ``max_papers`` most recent.

    Recency bias is the whole point: sort by submission date descending and
    slice. Papers with no parseable date sort last (they shouldn't survive
    the trim if the corpus is large enough).
    """
    seen: dict[str, CorpusPaper] = {}
    for paper in papers:
        if paper.arxiv_id not in seen:
            seen[paper.arxiv_id] = paper

    ordered = sorted(
        seen.values(),
        key=lambda p: p.published_dt or datetime(1970, 1, 1, tzinfo=UTC),
        reverse=True,
    )
    return ordered[:max_papers]


# ── 3. PDF download + sha256 content-addressed cache ────────────


def _default_pdf_downloader(pdf_url: str) -> bytes:
    """Download a PDF with retry + exponential backoff on 429/503."""
    import requests  # lazy

    for attempt in range(_PDF_MAX_RETRIES):
        resp = requests.get(
            pdf_url,
            timeout=_PDF_TIMEOUT_SECONDS,
            headers={"User-Agent": "archimedes-arxiv-corpus/1.0 (+hackathon)"},
        )
        if resp.status_code in (429, 503) and attempt < _PDF_MAX_RETRIES - 1:
            wait = _PDF_BACKOFF_BASE * (2**attempt)
            logger.warning(
                "arxiv returned %d — backing off %ds (attempt %d/%d)",
                resp.status_code,
                wait,
                attempt + 1,
                _PDF_MAX_RETRIES,
            )
            import time

            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.content
    # Should not reach here, but satisfy type checker
    raise RuntimeError(f"PDF download failed after {_PDF_MAX_RETRIES} retries")


def _cache_pdf(
    paper: CorpusPaper,
    pdf_dir: Path,
    downloader: Callable[[str], bytes],
) -> str | None:
    """Download (or reuse cached) PDF; return its sha256, or ``None``.

    The on-disk filename is the bare arxiv_id (stable, human-readable) while
    the integrity primitive is the sha256 of the bytes — same content-
    addressed contract as ``arxiv_pipeline.extract_text``. A 404 / throttle
    / timeout is logged and yields ``None``; it never aborts the run.
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / f"{paper.arxiv_id}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        try:
            return hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        except Exception as exc:
            logger.debug("re-reading cached pdf %s failed: %s", pdf_path, exc)

    if not paper.pdf_url:
        logger.warning("no pdf_url for %s", paper.arxiv_id)
        return None
    try:
        data = downloader(paper.pdf_url)
    except Exception as exc:
        logger.warning("pdf download failed for %s: %s", paper.arxiv_id, exc)
        return None
    if not data:
        logger.warning("empty pdf body for %s", paper.arxiv_id)
        return None
    pdf_path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


# ── 4. Defensive pypdf text extraction (cached) ─────────────────


def _extract_text(paper: CorpusPaper, pdf_dir: Path, text_dir: Path) -> bool:
    """Page-by-page pypdf extraction into a per-paper text cache.

    Returns True if a non-empty text file now exists. One bad page is logged
    and skipped — never fatal — exactly the ``arxiv_pipeline`` contract.
    """
    text_dir.mkdir(parents=True, exist_ok=True)
    text_path = text_dir / f"{paper.arxiv_id}.txt"
    if text_path.exists() and text_path.stat().st_size > 0:
        return True

    pdf_path = pdf_dir / f"{paper.arxiv_id}.pdf"
    if not (pdf_path.exists() and pdf_path.stat().st_size > 0):
        return False

    import pypdf  # lazy

    parts: list[str] = []
    try:
        reader = pypdf.PdfReader(str(pdf_path))
    except Exception as exc:
        logger.warning("pypdf open failed for %s: %s", paper.arxiv_id, exc)
        return False
    for i, page in enumerate(reader.pages):
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:
            logger.debug("%s page %d extract failed: %s", paper.arxiv_id, i, exc)
    text = "\n".join(parts).strip()
    if not text:
        logger.debug("no extractable text for %s", paper.arxiv_id)
        return False
    text_path.write_text(text, encoding="utf-8")
    return True


# ── 5. Orchestrate ──────────────────────────────────────────────


def build_corpus(
    *,
    max_papers: int = DEFAULT_MAX_PAPERS,
    out_path: Path = DEFAULT_OUT,
    pdf_dir: Path = DEFAULT_PDF_DIR,
    text_dir: Path = DEFAULT_TEXT_DIR,
    search: Callable[[Iterable[str], int], Iterable[CorpusPaper]] | None = None,
    pdf_downloader: Callable[[str], bytes] | None = None,
    fetch_pdfs: bool = True,
    delay_seconds: float = _PDF_DOWNLOAD_DELAY,
) -> list[dict]:
    """Build the manifest and (best-effort) the PDF + text caches.

    The metadata manifest is always written for the full trimmed target N —
    even rows whose PDF/text failed (``pdf_sha256`` then ``null``). ``search``
    and ``pdf_downloader`` are injectable so the parse → schema → cache →
    dedupe → recency path is fully testable offline.

    ``delay_seconds`` is the polite pause between cached PDFs and defaults to
    arXiv's guidance (``_PDF_DOWNLOAD_DELAY`` = 3.0s) — a live harvest is
    unchanged. Offline callers pass 0 to skip the sleep entirely.
    """
    search = search or _default_search
    pdf_downloader = pdf_downloader or _default_pdf_downloader

    over = max(int(max_papers * _OVERFETCH_FACTOR), _MIN_OVERFETCH)
    per_category_cap = max(int(over / len(ALL_CATEGORIES)) + 1, 50)

    logger.info(
        "fetching arxiv q-fin corpus: target=%d, over-fetch≈%d (%d/category)",
        max_papers,
        over,
        per_category_cap,
    )
    raw = list(search(ALL_CATEGORIES, per_category_cap))
    logger.info("fetched %d raw results before dedupe/trim", len(raw))

    papers = _dedupe_and_trim(raw, max_papers)
    logger.info("kept %d papers after dedupe + recency trim", len(papers))

    fetched_at = _utc_now_iso()
    rows: list[dict] = []
    pdf_ok = 0
    text_ok = 0
    for idx, paper in enumerate(papers, 1):
        sha: str | None = None
        if fetch_pdfs:
            sha = _cache_pdf(paper, pdf_dir, pdf_downloader)
            if sha is not None:
                pdf_ok += 1
                if _extract_text(paper, pdf_dir, text_dir):
                    text_ok += 1
                # Polite delay between PDF downloads — respect arXiv rate
                # limits. Injected (default 3.0s), so offline callers can
                # pass 0 and skip the sleep entirely.
                if delay_seconds > 0:
                    import time

                    time.sleep(delay_seconds)
        rows.append(paper.manifest_row(pdf_sha256=sha, fetched_at=fetched_at))
        if idx % 25 == 0:
            logger.info("processed %d/%d papers", idx, len(papers))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    logger.info(
        "manifest written: %s | %d papers, %d PDFs cached, %d text extracted",
        out_path,
        len(rows),
        pdf_ok,
        text_ok,
    )
    return rows


# ── CLI ─────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m archimedes.services.arxiv_corpus",
        description="Scrape a recency-biased arXiv q-fin corpus + manifest.",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_PAPERS,
        dest="max_papers",
        help=f"target paper count after trim (default {DEFAULT_MAX_PAPERS})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"manifest output path (default {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--pdf-dir",
        type=Path,
        default=DEFAULT_PDF_DIR,
        help=f"PDF cache directory (default {DEFAULT_PDF_DIR})",
    )
    parser.add_argument(
        "--text-dir",
        type=Path,
        default=DEFAULT_TEXT_DIR,
        help=f"text cache directory (default {DEFAULT_TEXT_DIR})",
    )
    parser.add_argument(
        "--no-pdfs",
        action="store_true",
        help="metadata-only run (skip PDF download + text extraction)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="DEBUG-level logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    rows = build_corpus(
        max_papers=args.max_papers,
        out_path=args.out,
        pdf_dir=args.pdf_dir,
        text_dir=args.text_dir,
        fetch_pdfs=not args.no_pdfs,
    )
    cached = sum(1 for r in rows if r["pdf_sha256"] is not None)
    print(f"corpus: {len(rows)} papers in {args.out} ({cached} PDFs cached, {len(rows) - cached} metadata-only)")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
