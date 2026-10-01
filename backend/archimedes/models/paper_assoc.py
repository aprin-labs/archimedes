"""``assoc/v1`` — the single shape of a paper↔strategy association (#1637).

**An association is a record, not a string.**

Before this module a "source paper" was a string in three incompatible dict
shapes, and the *shape* was inside the content hash:

===========================================  ==========================================
writer                                       shape it emitted
===========================================  ==========================================
``main.py`` (example seed)                   ``{arxiv_id, title, authors}``
``agents/debate_engine.py``                  ``{arxiv_id, title: ""}`` — since #1739,
                                             ``{arxiv_id, title, mechanism,
                                             spec_elements}``
``api/strategies_routes.py`` (fusion job)    ``{arxiv_id, sha256: ""}`` (route deleted
                                             by #1595)
``agents/generation_pipeline.py`` (fixture)  ``{arxiv_id, title}``
===========================================  ==========================================

``strategy_store._compute_content_hash`` hashed the **whole dicts**, so the
same paper set arriving through two writers produced two content hashes, two
ids, and two "different" strategies. Dedup and the paper→strategy back-index
(``strategies_by_paper``) both degraded, and the split-brain would have
returned the moment anyone backfilled a title onto one writer's output.

The fix is one shape and one identity:

* **Shape** — every association carries the same key set (:data:`ASSOC_KEYS`),
  so ``set(a) == set(b)`` for any two associations from any two writers. The
  guarantee is enforced at ONE choke point rather than at N call sites:
  ``strategy_store.upsert_strategy`` runs :func:`normalize_assocs` over
  whatever a writer handed it, so every row written from here on holds
  ``assoc/v1`` whichever historical shape arrived. (``main.py``'s example seed
  builds a ``StrategyRecord`` directly, bypassing that choke point, so it
  normalizes itself via :func:`paper_ref_to_assoc`.) On write only — rows
  stored earlier keep their legacy shape until PR-2 normalizes them, and
  ``strategy_store``'s module docstring ("Legacy rows are not rewritten") says
  which readers therefore see which shape.
* **Identity** — :func:`assoc_identity` projects an association list down to
  ``(handle, role)`` pairs, where the handle is the arXiv id, or — for the
  curated papers that have none — the DOI or the case-folded title
  (:func:`assoc_handle`). That projection, and nothing else, is what the
  content hash may see. Enrichment (title, authors, year, venue, DOI,
  contribution, rank, score) is *additive metadata about a known association*
  and must never change the identity of the strategy that cites it.

Honesty rules baked into the normalizer, not left to call sites:

* ``None`` means "not recorded". ``""`` never does — a blank title, DOI or
  venue normalizes to ``None`` so a downstream renderer prints "unavailable"
  instead of an empty pair of quotes, and so a merge can tell "no value" apart
  from "value that happens to be blank".
* ``role`` is closed: ``"cited"`` (the strategy is built on this paper) or
  ``"considered"`` (the selector surfaced it and the strategy did not use it).
  An unrecognised role raises rather than being coerced — a wrong role is a
  false provenance claim, and silently rewriting it to ``"cited"`` would
  manufacture one.
* Nothing here fabricates. A missing hash stays ``None``; the corpus's
  ``content_hash`` / ``pdf_sha256`` columns are NULL in production (#1091), so
  ``None`` is the *correct* answer until hydration lands, not a gap to fill.

The ``mechanism`` / ``spec_elements`` pair is #1739's per-paper attribution,
and it is part of the record rather than a passing detail because #1739 made
it **durable on purpose**: ``_resolve_source_papers`` carries both to the API
response, and ``test_persisted_source_papers_carry_title_and_mechanism`` pins
that the link survives the write. Note that ``mechanism`` is NOT
``contribution`` and the two must not be collapsed:

* ``mechanism`` is the model's raw claim about what a paper supplies, and
  ``spec_elements`` are the indicator aliases from the validated spec that
  back it. Either can be present without the other.
* ``contribution`` is the **attributed** statement — the one a reader is
  shown. ``generation_pipeline._passport_paper_refs`` derives it as
  ``mechanism if mechanism and spec_elements else None``, so a mechanism the
  spec never used stays an em-dash on the passport. Writing the unbacked prose
  into ``contribution`` would launder an unverified claim into a cited-paper
  column, which is the defect #1739 removed.

Neither is identity. :func:`assoc_identity` sees the handle and the role and
nothing else, so a mechanism arriving late cannot fork a strategy any more
than a title can.

Ordering note for hashing: :func:`assoc_identity` sorts and de-duplicates, so
two writers listing the same papers in different orders — or listing one twice
— produce the same identity and therefore the same strategy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    from archimedes.models.paper_ref import PaperRef

#: Schema tag stamped on every association. Bump the version, never the shape.
ASSOC_SCHEMA = "assoc/v1"

#: The strategy is built on this paper — it appears on the passport.
ROLE_CITED = "cited"
#: The selector surfaced this paper and the strategy did not use it. This is
#: what makes "considered but not cited" an answerable question.
ROLE_CONSIDERED = "considered"

#: Closed set. An association with any other role is rejected, not coerced.
ASSOC_ROLES: frozenset[str] = frozenset({ROLE_CITED, ROLE_CONSIDERED})

#: The exact key set of an ``assoc/v1`` record. Every writer emits all of them;
#: absent facts are ``None``, never omitted, so ``set(a) == set(b)`` holds
#: across writers and a missing key is a bug rather than a shrug.
ASSOC_KEYS: frozenset[str] = frozenset(
    {
        "arxiv_id",
        "role",
        "content_hash",
        "title",
        "authors",
        "year",
        "venue",
        "doi",
        "contribution",
        "mechanism",
        "spec_elements",
        "selection_rank",
        "semantic_score",
        "schema",
    }
)

#: Legacy hash-bearing keys, in precedence order. ``sha256`` is the fusion
#: job's spelling; ``pdf_sha256`` is the corpus column's. Both meant the same
#: thing and both were always ``""`` in practice.
_LEGACY_HASH_KEYS = ("content_hash", "sha256", "pdf_sha256")


def _text(value: Any) -> str | None:
    """Strip to a non-empty string, or ``None``. ``""`` is never a value."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    """Coerce to ``int`` or ``None`` — never to ``0``, which reads as a year."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    """Coerce to ``float`` or ``None`` — never to ``0.0``, which reads as a score."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _authors(value: Any) -> list[str]:
    """Normalize an author list. A list (possibly empty) — never ``None``.

    An empty list is the honest rendering of "no authors recorded": the field
    is a collection, and an absent collection and an empty one are the same
    fact to every consumer of it. Blank entries are dropped rather than kept
    as ``""`` placeholders.
    """
    if value is None:
        return []
    if isinstance(value, str):
        cleaned = _text(value)
        return [cleaned] if cleaned else []
    if isinstance(value, Iterable):
        return [t for a in value if (t := _text(a))]
    return []


def make_assoc(
    arxiv_id: str | None = None,
    *,
    role: str = ROLE_CITED,
    content_hash: str | None = None,
    title: str | None = None,
    authors: Iterable[str] | None = None,
    year: int | None = None,
    venue: str | None = None,
    doi: str | None = None,
    contribution: str | None = None,
    mechanism: str | None = None,
    spec_elements: Iterable[str] | None = None,
    selection_rank: int | None = None,
    semantic_score: float | None = None,
) -> dict[str, Any]:
    """Build one ``assoc/v1`` record. The only supported constructor.

    Every writer goes through here (or through :func:`normalize_assoc`), which
    is what makes the key sets pairwise equal. Raises ``ValueError`` on a role
    outside :data:`ASSOC_ROLES`.
    """
    role_text = _text(role) or ROLE_CITED
    if role_text not in ASSOC_ROLES:
        raise ValueError(f"assoc role must be one of {sorted(ASSOC_ROLES)}, got {role!r}")
    return {
        "arxiv_id": _text(arxiv_id),
        "role": role_text,
        "content_hash": _text(content_hash),
        "title": _text(title),
        "authors": _authors(authors),
        "year": _int(year),
        "venue": _text(venue),
        "doi": _text(doi),
        "contribution": _text(contribution),
        "mechanism": _text(mechanism),
        "spec_elements": _authors(spec_elements),
        "selection_rank": _int(selection_rank),
        "semantic_score": _float(semantic_score),
        "schema": ASSOC_SCHEMA,
    }


def normalize_assoc(raw: Mapping[str, Any] | str | None) -> dict[str, Any]:
    """Coerce any historical association shape into ``assoc/v1``.

    Accepts all three legacy dict shapes, a bare arXiv id string, and an
    already-normalized record (idempotent). Unknown keys are dropped: a shape
    that carried extra fields carried them *unhashed and unread*, and keeping
    them would reintroduce exactly the per-writer variation this removes.
    """
    if raw is None:
        return make_assoc()
    if isinstance(raw, str):
        return make_assoc(raw)
    if not isinstance(raw, Mapping):
        raise TypeError(f"cannot normalize {type(raw).__name__} into an assoc/v1 record")

    content_hash = next((raw[k] for k in _LEGACY_HASH_KEYS if _text(raw.get(k))), None)
    return make_assoc(
        raw.get("arxiv_id"),
        # A legacy row has no role; every legacy association was a citation.
        role=_text(raw.get("role")) or ROLE_CITED,
        content_hash=content_hash,
        title=raw.get("title"),
        authors=raw.get("authors"),
        year=raw.get("year"),
        venue=raw.get("venue"),
        doi=raw.get("doi"),
        contribution=raw.get("contribution"),
        mechanism=raw.get("mechanism"),
        spec_elements=raw.get("spec_elements"),
        selection_rank=raw.get("selection_rank"),
        semantic_score=raw.get("semantic_score"),
    )


def assoc_handle(assoc: Mapping[str, Any]) -> str | None:
    """The identifier this association is known by, or ``None`` if it has none.

    ``arxiv_id`` is the id space (#1637). ``doi`` and a case-folded ``title``
    are the documented fallbacks, and they are not optional niceties: **every
    curated strategy in this repo declares ``PAPER_ARXIV_ID = None``** and
    identifies its paper by title (and sometimes DOI) alone. An arXiv-only rule
    would silently drop all 34 of them from the passport projection.

    ``passport_loader._ref_key`` delegates here rather than keeping a second
    copy of this precedence, so the store's idea of "the same paper" and the
    passport's cannot drift apart.

    The honest consequence, stated rather than hidden: for a paper with neither
    an arXiv id nor a DOI, the title *is* the identifier, so backfilling a
    better title changes that association's identity. There is no way around
    that without an identifier to hang it on — and it does not touch the
    arXiv-identified papers every generated strategy cites, whose identity is
    unaffected by any enrichment.
    """
    return (
        _text(assoc.get("arxiv_id"))
        or (f"doi:{doi}" if (doi := _text(assoc.get("doi"))) else None)
        or (f"title:{title.casefold()}" if (title := _text(assoc.get("title"))) else None)
    )


def normalize_assocs(raw: Iterable[Mapping[str, Any] | str] | None) -> list[dict[str, Any]]:
    """Normalize a whole association list, dropping entries that name no paper.

    An association with no arXiv id, no DOI and no title is not an association
    — it is the ``PaperRef(title=strategy_name)`` placeholder that used to stand
    in for one, and keeping it would put the strategy's own name in the
    cited-paper column. Anything carrying *any* identifier is kept; see
    :func:`assoc_handle`.
    """
    return [a for item in (raw or []) if assoc_handle(a := normalize_assoc(item))]


def is_assoc(obj: Any) -> bool:
    """Does ``obj`` validate as ``assoc/v1``? Key set + schema tag + role."""
    return (
        isinstance(obj, Mapping)
        and set(obj) == ASSOC_KEYS
        and obj.get("schema") == ASSOC_SCHEMA
        and obj.get("role") in ASSOC_ROLES
    )


def assert_assoc(obj: Any) -> dict[str, Any]:
    """Return ``obj`` if it validates as ``assoc/v1``, else raise ``ValueError``.

    The message names the *difference*, because "not a valid assoc" without
    the offending keys is the kind of error that gets caught and logged rather
    than fixed.
    """
    if not isinstance(obj, Mapping):
        raise ValueError(f"assoc/v1 record must be a mapping, got {type(obj).__name__}")
    missing = sorted(ASSOC_KEYS - set(obj))
    extra = sorted(set(obj) - ASSOC_KEYS)
    if missing or extra:
        raise ValueError(f"assoc/v1 key mismatch — missing={missing} extra={extra}")
    if obj.get("schema") != ASSOC_SCHEMA:
        raise ValueError(f"assoc/v1 schema tag must be {ASSOC_SCHEMA!r}, got {obj.get('schema')!r}")
    if obj.get("role") not in ASSOC_ROLES:
        raise ValueError(f"assoc/v1 role must be one of {sorted(ASSOC_ROLES)}, got {obj.get('role')!r}")
    return dict(obj)


def assoc_identity(assocs: Iterable[Mapping[str, Any] | str] | None) -> list[list[str]]:
    """The **only** projection a content hash may see: ``[[arxiv_id, role], …]``.

    Sorted and de-duplicated, so listing the same papers in a different order —
    or listing one twice — cannot fork a strategy's identity. Enrichment is
    excluded by construction rather than by a call site remembering to strip
    it: backfilling a title onto a stored association must not change the
    strategy's content hash, its id, or its dedup behaviour.

    The pair's first element is :func:`assoc_handle`, not the raw ``arxiv_id`` —
    see there for why a DOI/title fallback is required rather than optional.
    """
    pairs = {(assoc_handle(a) or "", a["role"]) for a in normalize_assocs(assocs)}
    return [list(p) for p in sorted(pairs)]


def cited(assocs: Iterable[Mapping[str, Any] | str] | None) -> list[dict[str, Any]]:
    """The ``cited`` subset — what the passport shows and the trace binds to."""
    return [a for a in normalize_assocs(assocs) if a["role"] == ROLE_CITED]


def assoc_to_paper_ref(assoc: Mapping[str, Any] | str) -> PaperRef:
    """Project one association into the passport's ``PaperRef`` dataclass."""
    from archimedes.models.paper_ref import PaperRef

    a = normalize_assoc(assoc)
    return PaperRef(
        arxiv_id=a["arxiv_id"],
        # PaperRef.title is a plain ``str`` for backwards compatibility with
        # every existing consumer; ``None`` becomes ``""`` only at this
        # boundary, and the association itself keeps the honest ``None``.
        title=a["title"] or "",
        authors=list(a["authors"]),
        doi=a["doi"],
        venue=a["venue"],
        year=a["year"],
        citation_count=None,
        contribution=a["contribution"],
        role=a["role"],
        selection_rank=a["selection_rank"],
        semantic_score=a["semantic_score"],
        content_hash=a["content_hash"],
    )


def paper_ref_to_assoc(ref: Any, *, role: str | None = None) -> dict[str, Any]:
    """Project a ``PaperRef``-shaped object back into ``assoc/v1``."""
    return make_assoc(
        getattr(ref, "arxiv_id", None),
        role=role or getattr(ref, "role", None) or ROLE_CITED,
        content_hash=getattr(ref, "content_hash", None),
        title=getattr(ref, "title", None),
        authors=getattr(ref, "authors", None),
        year=getattr(ref, "year", None),
        venue=getattr(ref, "venue", None),
        doi=getattr(ref, "doi", None),
        contribution=getattr(ref, "contribution", None),
        selection_rank=getattr(ref, "selection_rank", None),
        semantic_score=getattr(ref, "semantic_score", None),
    )
