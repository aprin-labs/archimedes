"""Durable per-generation payment receipt (Dan's directive, 2026-08-21: "we
must provide people with their receipts").

The $2/generation x402 paywall (``services/generation_payment.py``) settles
through Circle's facilitator and hands the route a
``circlekit.x402.PaymentInfo`` — ``verified``/``payer``/``amount``/``network``/
``transaction``/``response_headers`` — then forgets it. Nothing survived the
request: a payer had no way to look back at what they were charged. This
table is where the settled fact survives, and ``GET /api/payments/receipts``
(``api/payment_routes.py``) is what reads it back.

**Honesty note, load-bearing:** ``settlement_ref`` is ``PaymentInfo.transaction``
— a Circle facilitator reference id, NOT an on-chain transaction hash. Circle
batches and settles on-chain later; this codebase does not run that
settlement logic (see ``marketplace/payments.py``'s module docstring). Never
render ``settlement_ref`` as a clickable arcscan link — a dead arcscan link
for a non-hash is worse than no link at all.

One row per settled payment, keyed to the account that paid
(``user_id`` — canonical Better Auth id, matching every other owner-scoped
table in this codebase, e.g. ``paper_deployments.owner_user_id``).
``job_id`` is nullable: it names the generation the payment funded, but the
receipt is a record of the CHARGE, not of the job succeeding. The route
writes the row the moment the payment settles, before any job exists, and
fills ``job_id`` in once the job is queued (``link_payment_receipt_job``,
#1908). A settled payment whose job never queued keeps its receipt with
``job_id`` NULL rather than losing it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from archimedes.models.chat import Base

logger = logging.getLogger(__name__)

#: No pagination yet (Dan's spec) — a hard cap keeps one query bounded and
#: keeps a runaway payer's list from becoming an unbounded read.
MAX_RECEIPTS = 200


class PaymentReceiptRecord(Base):
    """One settled x402 generation payment, durably kept for the payer."""

    __tablename__ = "payment_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payer_wallet: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_base_units: Mapped[int] = mapped_column(Integer, nullable=False)
    price_usd: Mapped[str] = mapped_column(String(32), nullable=False)
    network: Mapped[str] = mapped_column(String(64), nullable=False)
    # The Circle facilitator reference id — see module docstring's honesty
    # note. Nullable: a settle can complete without one in principle, and an
    # absent reference must render as absent, never as a fabricated value.
    settlement_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_payment_receipts_user_id", "user_id"),)

    def to_payload(self) -> dict[str, Any]:
        """The API shape — matches the fields ``GET /api/payments/receipts``
        promises, in the same order."""
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "price_usd": self.price_usd,
            "amount_base_units": self.amount_base_units,
            "payer_wallet": self.payer_wallet,
            "settlement_ref": self.settlement_ref,
            "job_id": self.job_id,
            "network": self.network,
        }


def record_payment_receipt(
    session: Session,
    *,
    user_id: str,
    payer_wallet: str,
    amount_base_units: int,
    price_usd: str,
    network: str,
    settlement_ref: str | None,
    job_id: str | None = None,
) -> PaymentReceiptRecord:
    """Insert one receipt row. Raises ``ValueError`` on a missing identity
    field — a receipt with no owner or no payer is not a receipt anyone could
    ever read back.

    Callers (``api/generate_routes.py``) are expected to wrap this in a
    try/except: the payment already settled by the time this runs, so a
    write failure here must never surface to the paying user.
    """
    if not user_id:
        raise ValueError("record_payment_receipt requires a user_id")
    if not payer_wallet:
        raise ValueError("record_payment_receipt requires a payer_wallet")

    record = PaymentReceiptRecord(
        user_id=user_id,
        payer_wallet=payer_wallet,
        amount_base_units=int(amount_base_units),
        price_usd=str(price_usd),
        network=str(network),
        settlement_ref=settlement_ref,
        job_id=job_id,
        created_at=datetime.now(UTC),
    )
    session.add(record)
    session.flush()
    return record


def link_payment_receipt_job(session: Session, receipt_id: int, job_id: str) -> bool:
    """Name the generation an already-written receipt's payment funded.

    The receipt is written the moment the payment settles, before a job id
    exists (#1908), and linked here once the job is queued. One conditional
    UPDATE: only a receipt that names no job yet is touched, so a repeat call
    is a no-op and can never re-point a receipt at a different job. Returns
    whether a row was linked.
    """
    if not job_id:
        return False
    updated = (
        session.query(PaymentReceiptRecord)
        .filter(PaymentReceiptRecord.id == receipt_id, PaymentReceiptRecord.job_id.is_(None))
        .update({PaymentReceiptRecord.job_id: job_id}, synchronize_session=False)
    )
    return bool(updated)


def list_payment_receipts(session: Session, user_id: str, *, limit: int = MAX_RECEIPTS) -> list[dict[str, Any]]:
    """Newest-first receipts owned by *user_id*, capped at *limit*.

    Owner-scoped by construction — a caller can only ever pass their own
    ``user_id`` (see ``api/payment_routes.py``), so this never leaks another
    payer's charges.
    """
    if not user_id:
        return []
    records = (
        session.query(PaymentReceiptRecord)
        .filter_by(user_id=user_id)
        .order_by(PaymentReceiptRecord.created_at.desc(), PaymentReceiptRecord.id.desc())
        .limit(limit)
        .all()
    )
    return [r.to_payload() for r in records]
