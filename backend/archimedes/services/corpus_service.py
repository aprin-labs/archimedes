"""Corpus service — DB-backed paper corpus with seed, intake, and reads.

Replaces the static ``manifest.jsonl`` with a Postgres-backed corpus.
The manifest is now a *seed source* only; all reads go through the DB.

Intake pulls new papers from the arXiv API (OAI-PMH-style bulk fetch),
deduplicates by arxiv_id, and enforces CORPUS_MAX.
"""

from __future__ import annotations

import json
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func

from archimedes.db import get_session
from archimedes.models.corpus_store import CorpusMetaRecord, PaperRecord

# arXiv categories to pull during incremental intake. Imported, never
# redeclared: `corpus_categories` is the single source of truth (#1635).
from archimedes.services.corpus_categories import QFIN_CATEGORIES

logger = logging.getLogger(__name__)

# Default lifted 2000 → 25000 (#1635) so a cold environment matches prod's env
# override and has headroom above the ~18.9k responsive q-fin literature.
# NOTE: there is still **no eviction** at the cap (docs/corpus-architecture.md);
# raising it defers retention, it does not solve it.
CORPUS_MAX = int(os.getenv("CORPUS_MAX", "25000"))

_ARXIV_API = "https://export.arxiv.org/api/query"
_INTAKE_PAGE_SIZE = 200  # arXiv's per-request maximum
_INTAKE_PAGE_DELAY_SECONDS = 3.0  # arXiv asks for ~1 request / 3s
# Consecutive all-duplicate pages tolerated before we conclude we are caught up.
# >1 because the newest page can be entirely known while an older page still
# holds a paper we missed (a v2 re-submission reorders the feed).
_INTAKE_MAX_DUPLICATE_PAGES = 2
# arXiv answers HTTP 500 for start >= 10000 on a single query (measured
# 2026-08-31). Intake is a newest-first top-up and catches up within a few
# pages, so it should never approach this; stop loudly if it ever does rather
# than emit a generic API-failure warning. The *bulk* harvester works around
# the wall by partitioning per category — see scripts/bulk_ingest_arxiv.py.
_ARXIV_DEEP_PAGE_LIMIT = 10000

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def intake_query_url(start: int, page_size: int) -> str:
    """Build one page of the incremental-intake arXiv query.

    ``start`` is the pagination offset. Before #1635 it was **absent**, so
    every call re-requested the same newest ``max_results`` papers and intake
    could never insert past the first page — the corpus could not grow at all
    through this path.
    """
    cat_query = "+OR+".join(f"cat:{c}" for c in QFIN_CATEGORIES)
    return (
        f"{_ARXIV_API}"
        f"?search_query=({cat_query})"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&start={start}&max_results={page_size}"
    )


def seed_from_manifest(manifest_path: Path | None = None) -> int:
    """Idempotently upsert manifest.jsonl rows into the papers table.

    Returns the number of new rows inserted (0 if already seeded).
    """
    if manifest_path is None:
        env = os.getenv("ARCHIMEDES_CORPUS_MANIFEST")
        if env:
            manifest_path = Path(env)
        else:
            here = Path(__file__).resolve()
            candidates = [
                here.parents[3] / "data" / "corpus" / "manifest.jsonl",
                Path("/app/data/corpus/manifest.jsonl"),
            ]
            manifest_path = next((c for c in candidates if c.exists()), None)

    if manifest_path is None or not manifest_path.exists():
        logger.info("corpus: no manifest to seed from")
        return 0

    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("corpus: cannot read manifest %s: %s", manifest_path, exc)
        return 0

    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("corpus: skip manifest line %d (bad JSON)", lineno)
            continue
        arxiv_id = str(obj.get("arxiv_id", "")).strip()
        if not arxiv_id:
            continue
        rows.append(obj)

    if not rows:
        return 0

    inserted = 0
    now = datetime.now(UTC)
    with get_session() as session:
        existing = {r[0] for r in session.query(PaperRecord.arxiv_id).all()}
        for obj in rows:
            arxiv_id = str(obj.get("arxiv_id", "")).strip()
            if arxiv_id in existing:
                continue
            authors = obj.get("authors", [])
            categories = obj.get("categories", [])
            record = PaperRecord(
                arxiv_id=arxiv_id,
                title=str(obj.get("title", "")).strip(),
                authors=json.dumps(authors if isinstance(authors, list) else [authors]),
                abstract=str(obj.get("abstract", "")).strip(),
                primary_category=str(obj.get("primary_category", "")).strip(),
                categories=json.dumps(categories if isinstance(categories, list) else [categories]),
                published=str(obj.get("published", "")).strip(),
                updated=str(obj.get("updated", "")).strip(),
                pdf_url=obj.get("pdf_url"),
                pdf_sha256=obj.get("pdf_sha256"),
                full_text_path=obj.get("text_path") or obj.get("full_text_path"),
                source="seed",
                ingested_at=now,
            )
            session.add(record)
            existing.add(arxiv_id)
            inserted += 1
        session.commit()

        _update_meta(session, source="seed")
        logger.info("corpus: seeded %d new papers (total %d)", inserted, len(existing))
    return inserted


def _update_meta(session, *, source: str = "unknown") -> None:
    """Upsert the singleton corpus_meta row."""
    meta = session.query(CorpusMetaRecord).first()
    count = session.query(func.count(PaperRecord.arxiv_id)).scalar() or 0
    if meta is None:
        meta = CorpusMetaRecord(
            last_intake_at=datetime.now(UTC),
            paper_count=count,
            source=source,
        )
        session.add(meta)
    else:
        meta.last_intake_at = datetime.now(UTC)
        meta.paper_count = count
        meta.source = source
    session.flush()


def _paper_from_entry(entry, *, now: datetime) -> tuple[str, PaperRecord] | None:
    """Map one Atom ``<entry>`` to ``(arxiv_id, PaperRecord)``, or None."""
    id_elem = entry.find("atom:id", _ATOM_NS)
    if id_elem is None or not id_elem.text:
        return None
    # Extract arxiv_id from URL like http://arxiv.org/abs/2605.12345v1
    arxiv_url = id_elem.text.strip()
    arxiv_id = arxiv_url.split("/abs/")[-1]
    # Strip version suffix
    if "v" in arxiv_id and arxiv_id[-1].isdigit():
        parts = arxiv_id.rsplit("v", 1)
        if parts[1].isdigit():
            arxiv_id = parts[0]

    title_elem = entry.find("atom:title", _ATOM_NS)
    summary_elem = entry.find("atom:summary", _ATOM_NS)
    published_elem = entry.find("atom:published", _ATOM_NS)
    updated_elem = entry.find("atom:updated", _ATOM_NS)

    title = (title_elem.text or "").strip().replace("\n", " ") if title_elem is not None else ""
    abstract = (summary_elem.text or "").strip().replace("\n", " ") if summary_elem is not None else ""
    published = published_elem.text.strip()[:10] if published_elem is not None and published_elem.text else ""
    updated = updated_elem.text.strip()[:10] if updated_elem is not None and updated_elem.text else ""

    categories = []
    for cat_elem in entry.findall("atom:category", _ATOM_NS):
        term = cat_elem.get("term", "")
        if term:
            categories.append(term)
    primary_category = categories[0] if categories else ""
    # arxiv:primary_category lives in the arXiv extension namespace
    for pc in entry.findall("{http://arxiv.org/schemas/atom}primary_category"):
        primary_category = pc.get("term", primary_category)

    authors = []
    for author_elem in entry.findall("atom:author", _ATOM_NS):
        name_elem = author_elem.find("atom:name", _ATOM_NS)
        if name_elem is not None and name_elem.text:
            authors.append(name_elem.text.strip())

    return arxiv_id, PaperRecord(
        arxiv_id=arxiv_id,
        title=title,
        authors=json.dumps(authors),
        abstract=abstract,
        primary_category=primary_category,
        categories=json.dumps(categories),
        published=published,
        updated=updated,
        pdf_url=arxiv_url.replace("/abs/", "/pdf/") + ".pdf",
        source="arxiv_api",
        ingested_at=now,
    )


def intake_from_arxiv(max_results: int | None = None) -> int:
    """Pull new q-fin papers from the arXiv API, dedup, upsert.

    Uses the arXiv Atom API (no key required, rate-limit polite) and **pages
    with ``start=``**. Before #1635 the URL carried only ``max_results``, so
    every call re-requested the same newest page and intake inserted ~0 after
    the first run — the corpus could not grow through this path at all, and
    raising ``CORPUS_MAX`` silently no-op'd.

    Returns the number of new papers inserted.
    """
    import httpx

    cap = max_results or (CORPUS_MAX - get_paper_count())
    if cap <= 0:
        logger.info("corpus: at CORPUS_MAX (%d), skipping intake", CORPUS_MAX)
        return 0

    with get_session() as session:
        existing = {r[0] for r in session.query(PaperRecord.arxiv_id).all()}

    now = datetime.now(UTC)
    pending: list[PaperRecord] = []
    start = 0
    pages = 0
    duplicate_pages = 0

    # Fetch outside the write session: paging is polite-delayed, and holding a
    # transaction open across those sleeps would pin a connection for minutes.
    while len(pending) < cap:
        if start >= _ARXIV_DEEP_PAGE_LIMIT:
            logger.error(
                "corpus: intake INCOMPLETE — arXiv refuses start>=%d; %d of %d wanted papers unreachable "
                "through this path. Re-seed from a bulk harvest instead.",
                _ARXIV_DEEP_PAGE_LIMIT,
                cap - len(pending),
                cap,
            )
            break
        page_size = min(_INTAKE_PAGE_SIZE, cap - len(pending))
        url = intake_query_url(start, page_size)
        try:
            resp = httpx.get(url, timeout=30.0)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("corpus: arxiv API failed at start=%d: %s", start, exc)
            break

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            logger.warning("corpus: arxiv XML parse failed at start=%d: %s", start, exc)
            break

        entries = root.findall("atom:entry", _ATOM_NS)
        pages += 1
        if not entries:
            logger.info("corpus: arxiv feed exhausted at start=%d", start)
            break

        page_new = 0
        for entry in entries:
            parsed = _paper_from_entry(entry, now=now)
            if parsed is None:
                continue
            arxiv_id, record = parsed
            if arxiv_id in existing:
                continue
            existing.add(arxiv_id)
            pending.append(record)
            page_new += 1
            if len(pending) >= cap:
                break

        start += len(entries)
        if page_new == 0:
            duplicate_pages += 1
            if duplicate_pages >= _INTAKE_MAX_DUPLICATE_PAGES:
                logger.info("corpus: %d consecutive pages with no new papers — caught up", duplicate_pages)
                break
        else:
            duplicate_pages = 0

        if len(pending) < cap:
            time.sleep(_INTAKE_PAGE_DELAY_SECONDS)

    if pages == 0:
        # Nothing was successfully fetched — do not stamp a fresh intake time.
        return 0

    with get_session() as session:
        for record in pending:
            session.add(record)
        session.commit()
        _update_meta(session, source="arxiv_api")
    logger.info(
        "corpus: intake inserted %d new papers over %d page(s)",
        len(pending),
        pages,
    )

    return len(pending)


def get_paper_count() -> int:
    """Return current paper count in the DB."""
    with get_session() as session:
        return session.query(func.count(PaperRecord.arxiv_id)).scalar() or 0


def count_corpus_papers(*, embargo_days: int = 30) -> int:
    """ORM-free count of the papers ``load_corpus`` would load — for /health.

    Mirrors ``apply_outcome_embargo``'s rule in SQL. That rule keeps a paper
    whose date is *at most* ``embargo_days`` old (``pub <= cutoff``), and the
    column holds either ``YYYY-MM-DD`` or a full ISO timestamp, so the SQL
    form is ``published < day-after-cutoff`` (a timestamp on the cutoff day
    sorts after the bare date but before the next day); empty ``published``
    is excluded. The Python filter also drops *unparseable* dates, which SQL
    cannot check — so the only permitted disagreement is this count reading
    high by the unparseable rows, never a fabricated low. Both edges and that
    delta are pinned by ``test_corpus_load_race_1632.py`` against a real table.

    Exists because the /health corpus probe used to call ``load_corpus`` — a
    full 18k-row ORM materialization per uncached check. On a cold task the
    probe blows its budget, is abandoned-but-running, and the next checks pile
    more loads into the executor until two race in SQLAlchemy session teardown
    and abort the interpreter (#1632, prod rev 214). A scalar COUNT holds no
    ORM state to race and returns in milliseconds.
    """
    from datetime import date, timedelta

    # apply_outcome_embargo keeps pub <= today - embargo_days; as a string
    # bound over "YYYY-MM-DD[Thh:mm:ssZ]" values that is `< the following day`.
    first_excluded_day = (date.today() - timedelta(days=embargo_days - 1)).isoformat()
    with get_session() as session:
        return (
            session.query(func.count(PaperRecord.arxiv_id))
            .filter(PaperRecord.published != "", PaperRecord.published < first_excluded_day)
            .scalar()
            or 0
        )


def get_corpus_meta() -> dict | None:
    """Return the singleton corpus_meta row as a dict, or None."""
    with get_session() as session:
        meta = session.query(CorpusMetaRecord).first()
        if meta is None:
            return None
        return {
            "last_intake_at": meta.last_intake_at.isoformat() if meta.last_intake_at else None,
            "paper_count": meta.paper_count,
            "source": meta.source,
            "corpus_hash": meta.corpus_hash,
            "artifact_hash": meta.artifact_hash,
            "artifact_built_at": meta.artifact_built_at.isoformat() if meta.artifact_built_at else None,
        }


def load_papers_from_db(
    *,
    embargo_days: int = 30,
    decay_lambda: float = 0.002,
    regime: str = "risk_on",
    apply_embargo: bool = True,
    apply_decay: bool = True,
) -> list[dict]:
    """Load papers from DB with Xia 2026 protocol enforcement.

    Parameters
    ----------
    embargo_days : int
        Outcome Embargo window (default 30 days).
    decay_lambda : float
        Time-Aware Retrieval base decay rate (default 0.002/day).
    regime : str
        Current regime for regime-aware λ scaling.
    apply_embargo : bool
        Whether to apply Outcome Embargo filtering.
    apply_decay : bool
        Whether to apply Time-Aware Retrieval scoring.

    Returns
    -------
    list[dict]
        Paper dicts, embargo-filtered and time-scored.
    """
    from archimedes.services.embargo_filter import apply_outcome_embargo
    from archimedes.services.time_aware_retrieval import (
        apply_time_aware_retrieval,
        regime_lambda,
    )

    with get_session() as session:
        rows = session.query(PaperRecord).order_by(PaperRecord.published.desc()).all()
        papers = [r.to_dict() for r in rows]

    if apply_embargo:
        papers = apply_outcome_embargo(papers, embargo_days=embargo_days)

    if apply_decay:
        lam = regime_lambda(decay_lambda, regime=regime)
        papers = apply_time_aware_retrieval(papers, lam=lam)

    return papers
