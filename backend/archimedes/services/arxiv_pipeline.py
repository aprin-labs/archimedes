"""arXiv → strategy passport pipeline (Dan's lane — Step 5, demo flex).

Implements the `IStrategyProvider.extract_from_paper` path that was a stub.
Lifts the KnowledgeBase `papers_analysis/extract.py` *pattern* — sha256
content-addressed cache + defensive page-by-page extraction — but swaps
PyMuPDF (AGPL) for `pypdf` (BSD-3, already a project dep), so nothing
AGPL enters an Unlicense repo.

Flow:
  1. arxiv API → paper metadata (title, authors, abstract, year, doi)
  2. download PDF → pypdf text, sha256-cached on disk
  3. Claude (the same LLMBackend seam) synthesizes the
     strategy *passport* from abstract + body
  4. render a self-describing strategy module into
     analytics-engine/strategies/ and refresh the provider

Honesty rules (the project thesis, enforced here):
  - The model fills PAPER_CLAIMED_* only if the paper states them; never
    invents Sharpe/CAGR/DD.
  - Output is a CANDIDATE with an explicit non-executable placeholder body.
    Extraction yields a *passport*, not validated alpha — a human must
    curate and the selection-bias gate (DSR/PBO) must pass before
    CANDIDATE → VALIDATED. The generated file says so in its CURATOR_NOTE.
  - EXTRACTION_LLM records which model produced it (provenance).

Heavy imports (`arxiv`, `pypdf`) are lazy so this module stays importable
in dependency-light environments; the fetcher is injectable for offline tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from archimedes.agents.generation_json import (
    default_backend,
    extract_json,
)
from archimedes.agents.prompts import PROMPTS
from archimedes.services.llm_backend import LLMBackend

logger = logging.getLogger(__name__)

# Cap how much paper text we hand the model — abstract carries most of the
# methodology signal; the body is supporting context, not a token sink.
_MAX_BODY_CHARS = 24_000


@dataclass
class PaperMeta:
    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    year: int | None = None
    doi: str | None = None
    categories: list[str] = field(default_factory=list)
    pdf_url: str | None = None


# ── 1. Fetch ────────────────────────────────────────────────────


def fetch_paper(arxiv_id: str) -> PaperMeta:
    """Resolve arxiv metadata via the `arxiv` API client (lazy import)."""
    import arxiv

    search = arxiv.Search(id_list=[arxiv_id])
    result = next(arxiv.Client().results(search))
    return PaperMeta(
        arxiv_id=arxiv_id,
        title=result.title.strip(),
        authors=[a.name for a in result.authors],
        abstract=result.summary.strip(),
        year=result.published.year if result.published else None,
        doi=getattr(result, "doi", None),
        categories=list(getattr(result, "categories", []) or []),
        pdf_url=result.pdf_url,
    )


def _download_pdf(pdf_url: str) -> bytes:
    import requests

    resp = requests.get(pdf_url, timeout=30)
    resp.raise_for_status()
    return resp.content


# ── 2. Extract (KnowledgeBase pattern: sha256-cached, defensive) ──


def extract_text(pdf_bytes: bytes, *, cache_dir: Path) -> str:
    """pypdf text extraction with a content-addressed JSON cache.

    The cache key is sha256(pdf_bytes), so re-running on the same paper is
    free and deterministic — the KnowledgeBase extract.py contract, minus
    its Papers-app coupling.
    """
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{sha}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text())["text"]
        except (ValueError, KeyError):
            logger.debug("corrupt extract cache %s; re-extracting", cache_file)

    import io

    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:
            logger.debug("page %d extract failed: %s", i, exc)
    text = "\n".join(parts).strip()
    cache_file.write_text(json.dumps({"sha256": sha, "text": text}))
    return text


# ── 3. Synthesize (reuses the architect's LLM seam) ─────────────


# The template itself lives in the prompt registry (`agents/prompts.py`), which
# is rendered into `docs/specs/prompt-inventory.md` under a drift test (#1800).
_SYNTH_SYSTEM = PROMPTS["paper_passport.synth.system"].text


def synthesize_passport(meta: PaperMeta, body_text: str, backend: LLMBackend) -> dict:
    """Claude-extracted passport fields. Robust JSON parse; honest defaults."""
    user = json.dumps(
        {
            "title": meta.title,
            "authors": meta.authors,
            "year": meta.year,
            "abstract": meta.abstract,
            "body_excerpt": body_text[:_MAX_BODY_CHARS],
        },
        indent=2,
    )
    raw = backend.complete(_SYNTH_SYSTEM, user)
    try:
        return extract_json(raw)
    except ValueError:
        logger.warning("arxiv synth: unparseable model output for %s", meta.arxiv_id)
        return {}


# ── 4. Render + write a self-describing strategy module ─────────


def _slug(arxiv_id: str, title: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48]
    aid = re.sub(r"[^0-9a-zA-Z]+", "_", arxiv_id)
    return f"arxiv_{aid}_{base or 'strategy'}"


_ALLOWED_SIZING = {"equal_weight", "risk_parity", "kelly", "inverse_vol"}
_ALLOWED_FREQ = {"daily", "weekly", "monthly"}
_ALLOWED_PROFILES = {"conservative", "moderate", "aggressive", "hyper_risky"}


def _doc_safe(v: object) -> str:
    """Escape a value for safe *literal* inclusion in a docstring or ``"..."`` string.

    The rendered module is ``exec_module``'d downstream (analytics-engine
    ``strategy_loader``), so unescaped quotes/backslashes/newlines in
    attacker-controlled arXiv metadata (a paper ``title`` / ``arxiv_id``) could
    break out of the enclosing string and execute arbitrary code (#920). Escape
    backslashes first, then every double-quote (so no run can form or terminate
    a ``\"\"\"`` docstring), and collapse CR/LF to spaces (a raw newline would
    break the single-line ``CURATOR_NOTE`` literal). Legit metadata — which has
    none of these — passes through unchanged. The repr()-based ``lit()`` path
    for the PAPER_* constants is already injection-safe; this guards only the
    human-readable prose that is interpolated as raw text.
    """
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def render_strategy_module(meta: PaperMeta, synth: dict, *, extraction_llm: str) -> str:
    """Emit Python the LocalStrategyProvider AST reader can parse.

    Constants only use literals (lists/strings/numbers) so the provider's
    `ast.literal_eval` path recovers them without importing backtrader. The
    Strategy subclass is an explicit non-executable placeholder — extraction
    produces a passport, not validated signal code.
    """
    sizing = str(synth.get("position_sizing", "equal_weight")).lower()
    if sizing not in _ALLOWED_SIZING:
        sizing = "equal_weight"
    freq = str(synth.get("rebalance_frequency", "weekly")).lower()
    if freq not in _ALLOWED_FREQ:
        freq = "weekly"
    profiles = [p for p in synth.get("risk_profiles", []) if p in _ALLOWED_PROFILES] or ["moderate"]

    def lit(v: object) -> str:
        return repr(v)

    title = _doc_safe(meta.title)
    arxiv_id = _doc_safe(meta.arxiv_id)
    extraction_llm_s = _doc_safe(extraction_llm)

    return f'''"""LLM-extracted strategy — {title}

AUTO-GENERATED from arXiv:{arxiv_id} by {extraction_llm_s}.
This is a CANDIDATE passport, NOT validated alpha. A human curator must
review it and it must pass the selection-bias gate (DSR / PBO / walk-forward
OOS / look-ahead) before promotion to VALIDATED. The strategy body below is
an intentional placeholder — extraction does not synthesize executable
signal code in v1.
"""

from __future__ import annotations

import backtrader as bt

PAPER_ARXIV_ID = {lit(meta.arxiv_id)}
PAPER_TITLE = {lit(meta.title)}
PAPER_AUTHORS = {lit(meta.authors)}
PAPER_YEAR = {lit(meta.year)}
PAPER_DOI = {lit(meta.doi)}

METHODOLOGY_SUMMARY = {lit(str(synth.get("methodology_summary", "")))}
METHODOLOGY_TEXT = {lit(str(synth.get("methodology_text", "")))}

ASSET_UNIVERSE = {lit(list(synth.get("asset_universe", [])))}
POSITION_SIZING = {lit(sizing)}
REBALANCE_FREQUENCY = {lit(freq)}
RISK_PROFILES = {lit(profiles)}

PAPER_CLAIMED_SHARPE = {lit(synth.get("paper_claimed_sharpe"))}
PAPER_CLAIMED_CAGR = {lit(synth.get("paper_claimed_cagr"))}
PAPER_CLAIMED_MAX_DD = {lit(synth.get("paper_claimed_max_dd"))}

EXTRACTION_LLM = {lit(extraction_llm)}
CURATOR_WALLET = None
CURATOR_NOTE = (
    "LLM-extracted from arXiv:{arxiv_id}. Passport only — requires human "
    "curation and the selection-bias admission gate before it can go LIVE."
)


class ExtractedStrategy(bt.Strategy):
    """Placeholder. Extraction yields a passport, not executable signals.

    A curator replaces this body with the paper's actual logic before the
    strategy is backtested or promoted past CANDIDATE.
    """

    def next(self) -> None:  # pragma: no cover - intentional no-op
        pass
'''


def write_strategy_file(source: str, dest_dir: Path, slug: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{slug}.py"
    path.write_text(source, encoding="utf-8")
    return path


# ── 5. Orchestrate ──────────────────────────────────────────────


def extract_strategy(
    arxiv_id: str,
    *,
    strategies_dir: Path,
    backend: LLMBackend | None = None,
    fetcher: Callable[[str], PaperMeta] | None = None,
    pdf_downloader: Callable[[str], bytes] | None = None,
) -> Path | None:
    """Full pipeline. Returns the written strategy file path, or None.

    `fetcher` / `pdf_downloader` are injectable so the deterministic path
    (synthesize → render → write) is testable with no network.
    """
    backend = backend or default_backend()
    fetcher = fetcher or fetch_paper
    pdf_downloader = pdf_downloader or _download_pdf

    try:
        meta = fetcher(arxiv_id)
    except Exception as exc:
        logger.warning("arxiv fetch failed for %s: %s", arxiv_id, exc)
        return None

    body = ""
    if meta.pdf_url:
        try:
            pdf_bytes = pdf_downloader(meta.pdf_url)
            body = extract_text(pdf_bytes, cache_dir=strategies_dir.parent / ".paper_cache")
        except Exception as exc:
            logger.warning("pdf extract failed for %s: %s", arxiv_id, exc)

    synth = synthesize_passport(meta, body, backend)
    if not synth.get("methodology_summary"):
        logger.warning("arxiv synth empty for %s; no strategy written", arxiv_id)
        return None

    source = render_strategy_module(meta, synth, extraction_llm=backend.model_id)
    slug = _slug(meta.arxiv_id, meta.title)
    path = write_strategy_file(source, strategies_dir, slug)
    logger.info("wrote LLM-extracted strategy %s", path)
    return path
