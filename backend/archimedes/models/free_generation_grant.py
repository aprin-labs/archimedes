"""Durable free-generation ledger — the first N runs on a bare account (#1643).

**This table records a policy reversal, so it says what it reverses.** From
2026-08-19 until this shipped, ``services/generation_payment.py`` enforced
"generation REQUIRES wallet connection + payment ... no free path": the very
first ``POST /api/generate/start`` on a wallet-less account was refused
``409 wallet_link_required``. The owner's 2026-08-31 product review replaced
that with *account required, wallet optional at first* — a small **lifetime**
allowance of free generations per account, then the wallet gate exactly as it
behaves today.

**Lifetime, not daily — and that is the whole reason this is a table.**
``services/generation_quota.py`` already caps generation *volume* with rolling
36-hour Redis day-buckets. Reusing that mechanism here would regrant the free
allowance every single day, which is not an allowance at all. The two axes are
orthogonal and both still apply: the quota bounds how fast anyone may generate,
this bounds how many generations an account gets before it must bring a wallet.
A Redis-only counter would also hand the allowance back on any cache flush —
generosity that resets on an infrastructure event is not a policy.

Ledger shape, mirroring ``models/generation_credit.py``'s discipline (one row
per logical event, a status rather than a delete, and the claim taken BEFORE
the thing it authorises):

``used``
    A free slot was spent on a generation. ``job_id`` is stamped once the job
    reaches the queue.
``released``
    Nothing was delivered against the slot, so the allowance is handed back.
    Two moments produce this, and the second is why ``job_id`` is worth
    stamping: the request never reached the queue at all (the premium-model
    entitlement gate refused it, the enqueue errored), or the job DID queue and
    then died inside the pipeline without producing a strategy — the corpus
    yielded too few papers to fuse, the run crashed, the user cancelled. Kept
    as a row rather than deleted so the ledger shows the attempt.

``seq`` is a per-account monotonically increasing ordinal, and
``uq_free_generation_grants_user_seq`` is what makes the claim **atomic**:
two concurrent first-generation requests both compute the same next ``seq``,
one INSERT wins and the other raises ``IntegrityError`` and is treated as
"no free slot available". Without it, a burst of N concurrent calls would each
read ``used_count == 0`` and each be granted a free run — the over-grant this
constraint exists to make impossible. ``seq`` is *not* compared against the
allowance (a released row leaves a permanent gap); the live ``used`` count is.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from archimedes.models.chat import Base

logger = logging.getLogger(__name__)

GRANT_USED = "used"
GRANT_RELEASED = "released"


class FreeGenerationGrantRecord(Base):
    """One free generation spent (or claimed and handed back) by one account."""

    __tablename__ = "free_generation_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    #: Better Auth ``auth_users.id`` — the canonical account identity every
    #: ``/api/generate/start`` caller already carries (``require_current_user``).
    #: Deliberately NOT a wallet: the whole point of this table is the runs that
    #: happen before a wallet exists.
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Per-account ordinal. Unique with ``user_id`` — see the module docstring:
    #: this constraint IS the concurrency guard, not a nicety.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=GRANT_USED)

    #: The generation this slot funded. NULL until the job reaches the queue.
    #: A ``released`` row is NULL only on the ``release_grant`` path — the
    #: request never queued, so there was no job. One released by
    #: ``release_grant_for_job`` KEEPS its job id: that is how the ledger shows
    #: which run burned the slot and handed it back, and it is the key the
    #: terminal-failure release finds the row by.
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "seq", name="uq_free_generation_grants_user_seq"),
        Index("ix_free_generation_grants_user_status", "user_id", "status"),
    )

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "seq": self.seq,
            "status": self.status,
            "job_id": self.job_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "released_at": self.released_at.isoformat() if self.released_at else None,
        }


def used_count(session: Session, user_id: str) -> int:
    """How many free generations *user_id* has actually spent.

    Counts ``used`` rows only — a ``released`` row represents a slot that was
    claimed and handed back because nothing was delivered, and charging the
    allowance for it would take a free generation the caller never received.
    """
    if not user_id:
        return 0
    return int(
        session.query(func.count(FreeGenerationGrantRecord.id))
        .filter(
            FreeGenerationGrantRecord.user_id == user_id,
            FreeGenerationGrantRecord.status == GRANT_USED,
        )
        .scalar()
        or 0
    )


def claim_free_grant(session: Session, *, user_id: str, allowance: int) -> FreeGenerationGrantRecord | None:
    """Take one free slot for *user_id*, or return ``None`` if none is left.

    Claimed BEFORE the generation is authorised, for the same reason
    ``generation_credit.claim_credit`` is claimed before the money moves: a
    check that runs ahead of the work it gates is the only kind that bounds
    concurrent callers. The caller must ``release_grant`` if the request it
    authorised never reaches the queue.

    Raises ``sqlalchemy.exc.IntegrityError`` when a concurrent request won the
    same ``seq``. That is a real "no slot available for you" answer and the
    service layer treats it as such — it is not swallowed here, because the
    session's state after the failed flush is the caller's to unwind.
    """
    if not user_id or allowance <= 0:
        return None

    if used_count(session, user_id) >= allowance:
        return None

    # max(seq)+1, NOT count+1: a released row leaves a permanent gap, and
    # reusing its ordinal would collide with nothing while a *later* claim
    # collided with the surviving row — the unique constraint has to be about
    # "two claims raced", never "an old row is in the way".
    highest = (
        session.query(func.max(FreeGenerationGrantRecord.seq))
        .filter(FreeGenerationGrantRecord.user_id == user_id)
        .scalar()
    )
    record = FreeGenerationGrantRecord(
        user_id=user_id,
        seq=int(highest or 0) + 1,
        status=GRANT_USED,
        created_at=datetime.now(UTC),
    )
    session.add(record)
    session.flush()
    return record


def stamp_grant_job(session: Session, grant_id: int, *, job_id: str) -> FreeGenerationGrantRecord | None:
    """Record which generation the slot funded, once that job is queued."""
    record = session.get(FreeGenerationGrantRecord, grant_id)
    if record is None or record.status != GRANT_USED:
        return None
    record.job_id = job_id
    session.flush()
    return record


def release_grant(session: Session, grant_id: int) -> FreeGenerationGrantRecord | None:
    """Hand the slot back — nothing was delivered against it.

    Idempotent: releasing an already-released grant is a no-op, so a retried
    cleanup path cannot mint allowance out of one claim.
    """
    record = session.get(FreeGenerationGrantRecord, grant_id)
    if record is None or record.status != GRANT_USED:
        return None
    record.status = GRANT_RELEASED
    record.released_at = datetime.now(UTC)
    session.flush()
    return record


def release_grant_for_job(session: Session, job_id: str) -> FreeGenerationGrantRecord | None:
    """Hand back the slot that funded *job_id* — that run delivered nothing.

    The sibling of ``generation_credit.restore_credit_for_job``, and it exists
    for the same reason: the claim is taken at ``/start``, but whether anything
    was DELIVERED is only known when the run ends, in a background task that
    holds a ``job_id`` and no ``grant_id``. Looking the row up by the job it was
    stamped with is what lets the terminal-failure path give the slot back
    without threading state through the task.

    ``job_id`` is left in place: the ledger should show which run burned the
    slot and handed it back, not silently rewind to a blank row.

    Idempotent, because only a ``used`` row matches: a retried or duplicated
    cleanup finds a ``released`` row, matches nothing, and returns ``None``
    rather than minting a second free generation out of one claim.
    """
    if not job_id:
        return None
    record = session.query(FreeGenerationGrantRecord).filter_by(job_id=job_id, status=GRANT_USED).one_or_none()
    if record is None:
        return None
    record.status = GRANT_RELEASED
    record.released_at = datetime.now(UTC)
    session.flush()
    return record
