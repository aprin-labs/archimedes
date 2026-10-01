"""StrategyStore — persistent, content-hashed, provenance-anchored strategy substrate.

Every strategy generated (fusion, architect, curated) is persisted here with
a keccak256 content hash for dedup and on-chain anchoring.  Status transitions
(candidate → live, demotions) are tracked.  Source paper provenance links
strategies to their origin arXiv documents for full traceability.

Legacy rows are not rewritten (#1637)
-------------------------------------

``upsert_strategy`` normalizes ``source_papers`` to ``assoc/v1``
(``models/paper_assoc.py``) before it hashes or stores, so every row written
from here on holds one shape. Rows written before it keep their writer's
legacy shape: this PR redefines the write path and its migration adds columns
to ``passport_paper_refs`` only — neither touches a stored ``source_papers``
value. **So the column holds two shapes, and it will until PR-2's pass.**

On read, exactly one path normalizes: ``to_strategy_passport``, via
``paper_assoc.cited``. ``to_dict``, ``strategies_by_paper`` and
``resolve_source_papers`` return the stored JSON verbatim — a legacy row and
an ``assoc/v1`` row do **not** decode to the same dict on those paths, and no
comment here should say they do.

What makes the mixed column survivable is narrower than "everything
normalizes", and worth stating exactly, because it is the property PR-2 must
not silently lose:

* Every verbatim path addresses an entry through ``.get()`` on ``arxiv_id``
  (and, on the ``/generated`` render path, ``title`` and ``year``). Those are
  the keys every historical shape either carries or omits, so an omitted
  legacy key reads ``None`` in the same place ``assoc/v1`` stores ``None``.
  ``api/strategies_routes._resolve_source_papers`` then resolves a blank title
  from the corpus, and the Library card prints ``resolved_title`` — never a
  raw stored key.
* The ``assoc/v1``-only keys — ``role``, ``content_hash``, ``contribution``,
  ``selection_rank``, ``semantic_score``, ``schema`` — are *additive*. No
  verbatim reader requires one, so their absence on a legacy row is not a
  decode failure; it is simply less metadata.
* The one reader that branches on ``role`` normalizes first, and
  ``normalize_assoc`` defaults a role-less legacy entry to ``cited`` — which
  is what every pre-#1637 association was.
* Identity is never exposed to the difference at all: ``_compute_content_hash``
  only ever sees the list ``upsert_strategy`` already normalized, never a
  stored row.

The standing cost, stated rather than implied: a legacy row's associations
cannot gain ``role`` / ``content_hash`` / rank / score until they are
normalized, and any NEW reader that needs an ``assoc/v1`` key must call
``normalize_assocs`` itself rather than assume the column. Deferring the
normalization is a deviation from the owner's PR-1 list and is called out as
one on the PR, with the reason and the sign-off it needs.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from archimedes.models.chat import Base

if TYPE_CHECKING:
    # Import only for the forward-reference type annotation on
    # to_strategy_passport(). Avoids a circular import at runtime (mirrors
    # models/strategy_passport_record.py's identical guard).
    from archimedes.models.strategy import StrategyPassport

logger = logging.getLogger(__name__)


class StrategyRecord(Base):
    """Persistent strategy with content-hash dedup and provenance."""

    __tablename__ = "strategy_store"

    # Width normalized to VARCHAR(128) (issue #1028) to match the
    # marketplace/billing tables' strategy_id columns (already VARCHAR(128))
    # ahead of a future cross-table FK. Actual id values are content_hash[:16]
    # (16 chars) so this is headroom, not a live-data change.
    id = Column(String(128), primary_key=True)
    content_hash = Column(String(66), nullable=False)  # keccak256, 0x-prefixed

    # Generation provenance
    generation_method = Column(String(32), nullable=False)  # fusion|architect|curated
    # JSON list of ``assoc/v1`` records — see ``models/paper_assoc.py`` for the
    # key set and the honesty rules. Normalized on WRITE by ``upsert_strategy``,
    # so every row written from here on holds one shape whichever writer
    # produced it (#1637). Rows written BEFORE that keep their writer's legacy
    # shape and NOTHING in this PR rewrites them — see "Legacy rows are not
    # rewritten" in the module docstring for what the column therefore actually
    # holds, which readers normalize, and which hand the stored JSON back
    # verbatim.
    # This comment previously claimed ``[{arxiv_id, sha256}]``, which was a
    # FOURTH shape nothing actually emitted.
    source_papers = Column(Text, nullable=False, default="[]")
    provenance_hash = Column(String(66), nullable=True)

    # Strategy definition
    strategy_name = Column(String(256), nullable=False, default="")
    thesis = Column(Text, nullable=False, default="")
    # The user's own free-text ask that produced this strategy (v8 Lane 3.3) —
    # distinct from `thesis`, which is the DERIVED methodology. Sourced from
    # `GenerateBrief.intent` at generation time (`_persist_candidate`'s
    # `_do_persist`). NULL for curated/example rows (which have no brief) and
    # for legacy generated rows this column's migration could not resolve
    # (see that migration's docstring for the backfill's honest boundary).
    brief_intent = Column(Text, nullable=True)
    asset_universe = Column(Text, nullable=False, default="[]")  # JSON list
    risk_profile = Column(String(32), nullable=False, default="moderate")
    # Raw validated DSL spec JSON (rebalancer decouple, Part A #1 of
    # docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md). Mirrors
    # ``StrategyPassport.strategy_spec`` — when present, the live signal
    # evaluator (``strategy_signal_evaluator.evaluate_strategies``)
    # interprets it directly via the shared DSL condition tree instead of
    # buy-and-hold/keyword-matching. NULL for curated/example rows (which
    # carry a ``strategy_code_path`` instead) and for generated rows
    # persisted before this column existed — the agent runner simply skips
    # any bound generated strategy_id that has no spec, same as before.
    strategy_spec = Column(Text, nullable=True)

    # Status lifecycle
    status = Column(String(16), nullable=False, default="candidate")  # candidate|live|retired|rejected
    rigor_verdict = Column(Text, nullable=True)  # JSON: DSR/PBO/walk-forward results
    is_example = Column(Boolean, nullable=False, default=False)  # hand-curated static strategies

    # Ownership + visibility (per-user strategies, private-until-published).
    # owner_wallet is optional proof-linked wallet provenance bound server-side (mirrors
    # VaultMetadata.creator_address) — never a client-supplied value. Stored
    # lowercase. NULL = legacy/anonymous row (backfilled to the D7 'system'
    # identity in the issue #1028 migration; the column itself stays nullable
    # so this ORM's own anonymous-row semantics — see upsert_strategy below —
    # are unchanged). is_published is a dormant flag (nothing flips it yet —
    # the publish flow is a future marketplace hop); unpublished non-example
    # rows are visible only to their owner.
    # FK retrofit (issue #1028, D1): every non-NULL value must be a known
    # identity.
    owner_wallet: Mapped[str | None] = mapped_column(
        String(42), ForeignKey("wallet_identities.wallet_address"), nullable=True, index=True
    )
    owner_user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("auth_users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_published = Column(Boolean, nullable=False, default=False)

    # On-chain registration (populated when strategy passes rigor gate)
    on_chain_registration_tx = Column(String(66), nullable=True)  # 0x-prefixed tx hash
    on_chain_registration_block = Column(String(32), nullable=True)  # block number as string

    # Lineage
    parent_id = Column(String(64), nullable=True)

    # Timestamps
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_strategy_content_hash"),
        Index("ix_strategy_status", "status"),
        Index("ix_strategy_generation", "generation_method"),
    )

    def decoded_strategy_spec(self) -> dict | None:
        """Defensively decode ``strategy_spec``.

        Mirrors ``VaultMetadata.get_strategy_ids()`` (models/chat.py): a
        corrupt/non-JSON column value must not raise out of a dict-shaping
        method that's reachable straight from API routes (e.g.
        ``strategies_routes.py`` calling ``record.to_dict()``) — a single bad
        row shouldn't 500 the whole response. Falls back to ``None``,
        matching the ``dict | None`` contract every other reader of this
        field (``upsert_strategy``, ``to_strategy_passport``) already uses.

        PUBLIC (was ``_decode_strategy_spec``, #1646) because the passport
        detail route now reads the spec straight off an already-loaded row to
        render it. The alternative call sites were worse: ``to_dict()`` decodes
        four other JSON columns this route does not need, and
        ``to_strategy_passport()`` calls ``json.loads`` on ``asset_universe``
        with no guard — so borrowing either of them would trade one defensive
        read for a new 500 surface on a route that must not acquire one. Same
        single implementation, no second decoder, no private access from a
        module ruff's SLF rules police.
        """
        if not self.strategy_spec:
            return None
        try:
            return json.loads(self.strategy_spec)
        except (json.JSONDecodeError, TypeError):
            logger.warning("strategy %s: corrupt strategy_spec JSON — returning None", self.id)
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content_hash": self.content_hash,
            "generation_method": self.generation_method,
            "source_papers": json.loads(self.source_papers),
            "provenance_hash": self.provenance_hash,
            "strategy_name": self.strategy_name,
            "thesis": self.thesis,
            "asset_universe": json.loads(self.asset_universe),
            "risk_profile": self.risk_profile,
            "strategy_spec": self.decoded_strategy_spec(),
            "status": self.status,
            "rigor_verdict": json.loads(self.rigor_verdict) if self.rigor_verdict else None,
            "is_example": self.is_example,
            "owner_wallet": self.owner_wallet,
            "is_published": bool(self.is_published),
            "parent_id": self.parent_id,
            "on_chain_registration_tx": self.on_chain_registration_tx,
            "on_chain_registration_block": self.on_chain_registration_block,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_strategy_passport(self) -> StrategyPassport:
        """Adapt to the ``StrategyPassport`` dataclass (mirrors
        ``StrategyPassportRecord.to_strategy_passport`` in
        ``models/strategy_passport_record.py``).

        This is the shape ``strategy_signal_evaluator.evaluate_strategies``
        and ``PortfolioConstructor`` already accept — curated strategies flow
        through the SAME dataclass, so a generated ``StrategyRecord`` needs no
        bespoke duck-typed carrier to be evaluated by the live agent runner
        (rebalancer decouple, Part A #1). Only the fields the signal/
        portfolio-construction path actually reads are populated; this is
        NOT a full passport reconstruction (rigor/backtest columns live on
        ``strategy_passports`` instead — see ``StrategyPassportRecord``).
        """
        from archimedes.models.paper_assoc import assoc_to_paper_ref, cited
        from archimedes.models.paper_ref import PaperRef
        from archimedes.models.strategy import StrategyPassport

        source_papers = json.loads(self.source_papers) if self.source_papers else []
        # Only the CITED subset reaches the passport: a "considered" association
        # records that the selector surfaced a paper, not that this strategy is
        # built on it, and putting one in ``papers`` would claim provenance the
        # strategy never had.
        papers = [
            # ``title or strategy_name`` is a display fallback the legacy
            # keyword evaluator depends on (``_get_evaluator(paper_title, …)``),
            # NOT a provenance claim — the wire projections
            # (``_resolve_source_papers``, ``PassportPaperRef.to_dict``) keep an
            # unresolvable title NULL rather than printing the strategy's name
            # in the cited-paper column.
            _with_display_title(assoc_to_paper_ref(a), self.strategy_name)
            for a in cited(source_papers)
        ] or [PaperRef(title=self.strategy_name or "")]

        return StrategyPassport(
            id=self.id,
            papers=papers,
            methodology_summary=self.thesis or "",
            asset_universe=json.loads(self.asset_universe) if self.asset_universe else [],
            # Same defensive decode as to_dict(): a corrupt spec column must
            # degrade to None (spec-less strategy) rather than raise out of a
            # dataclass adapter that future call sites may not wrap (review).
            strategy_spec=self.decoded_strategy_spec(),
        )


def _with_display_title(ref, strategy_name: str):
    """Fill a blank ``PaperRef.title`` with the strategy name, for DISPLAY only.

    ``to_strategy_passport`` feeds the live signal evaluator, whose legacy
    keyword path selects an evaluator from ``paper_title``; a blank title there
    silently drops the strategy from the scan. This is the one place that
    fallback is legitimate, and it is deliberately NOT applied on any wire
    projection — printing the strategy's own name in a "cited paper" column is
    a fabricated citation (see ``_resolve_source_papers``).
    """
    if ref.title:
        return ref
    return dataclasses.replace(ref, title=strategy_name or "")


def _compute_content_hash(
    generation_method: str,
    strategy_name: str,
    thesis: str,
    source_papers: list[dict],
    asset_universe: list[str],
) -> str:
    """Deterministic keccak256 content hash for dedup.

    **The hash sees paper IDENTITY only** — ``assoc_identity`` reduces
    ``source_papers`` to sorted, de-duplicated ``(handle, role)`` pairs before
    it reaches the canonical JSON (#1637).

    It used to hash the whole association dicts. That made the *shape* part of
    the strategy's identity: the same paper set arriving through the fusion
    job (``{arxiv_id, sha256: ""}``) and through the debate engine
    (``{arxiv_id, title: ""}``) produced two hashes, two ids and two "different"
    strategies, and backfilling a title onto a stored association would have
    forked one strategy into two all over again. A title is a fact *about* a
    paper; it is not what makes the association a different association.

    **Rows stored before this change are NOT re-stamped by this PR** (owner
    call on #1688, 2026-09-03: the re-stamp is irreversible and no dry-run has
    measured how many production rows reproduce their stored hash). Until that
    lands, a legacy row's stored ``content_hash`` is a value this function no
    longer produces, so re-upserting the *same* content inserts a second row
    instead of matching the first. That is the pre-existing split-brain moved,
    not widened — and it is bounded, because nothing downstream joins on
    ``content_hash``: ``id`` is the FK every other table uses and it never
    moves. Deliberately no legacy-hash fallback lookup here: a second, weaker
    definition of identity living beside the real one is exactly what #1637
    exists to delete.
    """
    from web3 import Web3

    from archimedes.models.paper_assoc import assoc_identity

    canonical = json.dumps(
        {
            "generation_method": generation_method,
            "strategy_name": strategy_name,
            "thesis": thesis,
            "source_papers": assoc_identity(source_papers),
            "asset_universe": sorted(asset_universe),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return Web3.keccak(text=canonical).hex()


def upsert_strategy(
    session: Session,
    *,
    generation_method: str,
    strategy_name: str,
    thesis: str,
    source_papers: list[dict],
    asset_universe: list[str],
    risk_profile: str = "moderate",
    rigor_verdict: dict | None = None,
    parent_id: str | None = None,
    provenance_hash: str | None = None,
    is_example: bool = False,
    owner_wallet: str | None = None,
    owner_user_id: str | None = None,
    strategy_spec: dict | None = None,
    brief_intent: str | None = None,
) -> StrategyRecord:
    """Idempotent upsert: same content → same row, no duplicates.

    ``owner_user_id`` is the canonical Better Auth owner. ``owner_wallet`` is
    optional verified-wallet provenance. On a content-hash match, ownership is
    only backfilled onto ownerless rows — an existing owner is never overwritten.

    ``strategy_spec`` is the validated DSL spec dict (rebalancer decouple,
    Part A #1) — JSON-encoded when present, left NULL otherwise. On a
    content-hash match it is backfilled onto a row that lacks one, the same
    never-overwrite rule as ownership; an existing non-null spec is never
    replaced.

    ``brief_intent`` is the user's own free-text ask (v8 Lane 3.3) — plain
    text, left NULL otherwise. Same never-overwrite backfill rule: a
    content-hash match only fills a NULL, never replaces an existing value.
    Normalized once here (stripped; empty-or-whitespace → NULL) so BOTH write
    branches below store the same thing: a blank brief has nothing to show,
    and a row holding ``"   "`` would render an empty "Your brief" card on the
    passport instead of no card at all.

    ``source_papers`` is normalized to ``assoc/v1`` here (#1637) regardless of
    which writer's legacy shape arrived, so every row written from here on
    holds one shape and the hash sees paper identity only. It does NOT make the
    whole column one shape: rows stored before this PR keep their legacy shape
    until PR-2's pass (see "Legacy rows are not rewritten" in the module
    docstring).
    """
    from archimedes.models.paper_assoc import normalize_assocs

    owner_wallet = owner_wallet.lower() if owner_wallet else None
    # Normalize before either branch reads it — see the docstring. Doing it in
    # one place is what keeps the new-row branch and the backfill branch from
    # disagreeing about what "no brief" means.
    brief_intent = (brief_intent or "").strip() or None
    # Same one-place rule for associations: the hash, the stored column and the
    # log line below all read the SAME normalized list.
    source_papers = normalize_assocs(source_papers)
    content_hash = _compute_content_hash(
        generation_method,
        strategy_name,
        thesis,
        source_papers,
        asset_universe,
    )

    existing = session.query(StrategyRecord).filter_by(content_hash=content_hash).first()
    if existing:
        # Backfill ownership on a legacy/anonymous row; never reassign an owner.
        if owner_user_id and not existing.owner_user_id:
            existing.owner_user_id = owner_user_id
            session.flush()
        if owner_wallet and not existing.owner_wallet:
            existing.owner_wallet = owner_wallet
            existing.updated_at = datetime.now(UTC)
            session.flush()
        # Backfill a missing spec onto an existing row; never overwrite one
        # that's already there.
        if strategy_spec is not None and not existing.strategy_spec:
            existing.strategy_spec = json.dumps(strategy_spec)
            existing.updated_at = datetime.now(UTC)
            session.flush()
        # Same never-overwrite backfill rule for the user's brief. `brief_intent`
        # is already stripped-or-None at this point (top of the function), so a
        # whitespace-only ask cannot backfill over a genuine NULL either.
        if brief_intent and not existing.brief_intent:
            existing.brief_intent = brief_intent
            existing.updated_at = datetime.now(UTC)
            session.flush()
        # Update status/verdict if provided, but don't duplicate
        if rigor_verdict is not None:
            existing.rigor_verdict = json.dumps(rigor_verdict)
            existing.updated_at = datetime.now(UTC)
            # Status transition per docs/specs strategy-lifecycle:
            #   passing=True  → "live"     (in-portfolio-eligible, preserves
            #                                marketplace_service.trending logic)
            #   passing=False → "rejected" (visible failure — honesty wedge)
            #   no verdict    → unchanged
            if rigor_verdict.get("passing"):
                existing.status = "live"
            else:
                existing.status = "rejected"
            session.flush()
        return existing

    record = StrategyRecord(
        id=content_hash[:16],
        content_hash=content_hash,
        generation_method=generation_method,
        source_papers=json.dumps(source_papers),
        strategy_name=strategy_name,
        thesis=thesis,
        asset_universe=json.dumps(asset_universe),
        risk_profile=risk_profile,
        status="candidate",
        rigor_verdict=json.dumps(rigor_verdict) if rigor_verdict else None,
        parent_id=parent_id,
        provenance_hash=provenance_hash,
        is_example=is_example,
        owner_wallet=owner_wallet,
        owner_user_id=owner_user_id,
        # `is not None` (not truthiness) — consistent with the backfill branch
        # above, which also treats "present vs None" as the distinction. A
        # bare truthiness check would silently drop an explicitly-provided
        # empty dict ({}) by storing NULL instead of the serialized "{}".
        strategy_spec=json.dumps(strategy_spec) if strategy_spec is not None else None,
        # Already stripped-or-None at the top of the function (unlike
        # strategy_spec above, where {} is a meaningful distinct value from
        # "absent" and truthiness would be wrong): an empty or whitespace-only
        # brief has nothing to show, so it is stored as NULL, not as blanks.
        brief_intent=brief_intent,
    )
    if rigor_verdict:
        # Same transition rule as the upsert-existing branch above
        record.status = "live" if rigor_verdict.get("passing") else "rejected"
    session.add(record)
    session.flush()
    logger.info(
        "store: persisted strategy %s (%s, %d papers)",
        record.id,
        generation_method,
        len(source_papers),
    )
    return record


def resolve_source_papers(session: Session, strategy_id: str) -> list[dict]:
    """One strategy's stored ``source_papers`` list, decoded and **not** normalized.

    Returns the column exactly as it sits on disk, so what comes back is
    ``assoc/v1`` records for rows written since #1637 and the writing agent's
    legacy shape for older ones (see "Legacy rows are not rewritten" in the
    module docstring). A caller that needs one shape must run
    ``paper_assoc.normalize_assocs`` over the result; a caller that only reads
    ``arxiv_id`` / ``title`` / ``year`` with ``.get()`` does not have to.

    The old summary said "arxiv_id + sha256". ``sha256`` was one writer's
    spelling of the corpus hash, it was ``""`` on every row it ever appeared
    on, and ``normalize_assoc`` now folds it (and ``pdf_sha256``) into
    ``content_hash`` — so no writer emits that key any more and nothing here
    ever guaranteed the pair.

    An unknown ``strategy_id`` returns ``[]`` — indistinguishable from a
    strategy that cites nothing, which is why this is a convenience reader and
    not a provenance check. It has no production caller today: the ``/generated``
    route reads the column through ``to_dict`` and the passport path through
    ``to_strategy_passport``. A corrupt column raises out of ``json.loads``
    rather than degrading, the same as ``to_dict``.
    """
    record = session.query(StrategyRecord).filter_by(id=strategy_id).first()
    if not record:
        return []
    return json.loads(record.source_papers)


def strategies_by_paper(session: Session, arxiv_id: str) -> list[StrategyRecord]:
    """Find all strategies citing a given arXiv paper (bidirectional link).

    Pushes a substring prefilter into the DB so we don't load the entire
    StrategyRecord table and JSON-parse every row on each call. The LIKE
    matches any row whose serialized ``source_papers`` text contains the
    arxiv_id; the exact JSON check below then removes substring false
    positives (e.g. ``2401.001`` matching ``2401.0012``). Portable across
    SQLite and Postgres.
    """
    if not arxiv_id:
        return []
    # Escape LIKE wildcards in the id so they're matched literally.
    needle = arxiv_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    candidates = (
        session.query(StrategyRecord).filter(StrategyRecord.source_papers.like(f"%{needle}%", escape="\\")).all()
    )
    return [r for r in candidates if arxiv_id in {p.get("arxiv_id", "") for p in json.loads(r.source_papers)}]
