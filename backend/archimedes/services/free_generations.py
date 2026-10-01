"""Session-owning wrapper around the free-generation ledger (#1643).

``models/free_generation_grant.py`` is pure persistence and takes a ``Session``.
This module owns the sessions and decides what a failure means — the same split
``services/generation_credits.py`` makes, and for the same reason: the answer
changes with what is at stake.

**Every failure here resolves to "no free slot".** That is fail-closed on
*generosity*, and it is deliberate:

- The fallback is not an outage. A caller who cannot be granted a free slot
  falls through to the wallet gate + paywall — the behaviour production served
  for the twelve days before this shipped. Degrading to the previous policy is
  a strictly smaller harm than degrading to unbounded free LLM spend.
- The opposite choice is unrecoverable. Granting on a read error means a
  database blip hands out uncapped free generations, and nothing records that
  it happened, so nothing can be reconciled afterwards.

The one place this module must NOT resolve a failure to a number is
``remaining()``, which feeds ``GET /api/account/usage``. There, "we could not
read the ledger" is reported as ``None`` and rendered as an honest blank —
never as ``0`` (which would tell a fresh account it has nothing left) and never
as the full allowance (which would promise free runs the gate may refuse).

Allowance (``FREE_GENERATIONS_PER_ACCOUNT``, default 3) is read per call, not
cached, so an operator can change it without a deploy. ``<= 0`` disables the
free path entirely and restores the pre-#1643 wallet-gate-on-first-call
behaviour exactly — the kill switch for this policy.

**Owner decision D1 (2026-08-31, recorded on #1653): the allowance unlocks on a
VERIFIED EMAIL, not on account creation alone.** An unverified account is not
refused — it falls through to exactly the path it had before this module
existed (link a wallet, then pay) — it simply cannot spend the free allowance.
Two reasons, both the owner's:

- It prices disposable-account farming. Accounts are free and unlimited; a
  working inbox per three free generations is the cheapest honest throttle
  available, and it binds the free tier to something an abuser has to keep
  paying for.
- Verification becomes a carrot rather than a chore. ``locked_reason`` exists
  so ``GET /api/account/usage`` can say *why* the allowance is unavailable and
  the UI can offer the unlock, instead of a fresh account seeing nothing at all
  and concluding the free tier is a fiction.

``email_verified`` is a REQUIRED keyword argument of :func:`claim` rather than
something this module looks up. Two consequences, both deliberate: the check
cannot be silently forgotten by a future call site (omitting it is a
``TypeError``, pinned by a test), and this module keeps no opinion about how a
session is resolved — ``api/account_auth.CurrentUser.email_verified`` is the
single place Better Auth's ``emailVerified`` is parsed.
"""

from __future__ import annotations

import logging
import os

from archimedes.models.free_generation_grant import (
    claim_free_grant,
    release_grant,
    release_grant_for_job,
    stamp_grant_job,
    used_count,
)

logger = logging.getLogger(__name__)

#: The owner's 2026-08-31 call: "3 generations on a verified account, then wallet-gate."
DEFAULT_ALLOWANCE = 3

#: Why an account that has allowance left still cannot spend it. One value
#: today (owner decision D1); a string rather than a bool so a second lock
#: reason can be added without changing the wire shape of
#: ``GET /api/account/usage`` or of the 409 body.
LOCK_EMAIL_UNVERIFIED = "email_unverified"


def allowance() -> int:
    """Free generations granted per account, lifetime. ``<= 0`` disables the path."""
    raw = os.getenv("FREE_GENERATIONS_PER_ACCOUNT", str(DEFAULT_ALLOWANCE))
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "invalid FREE_GENERATIONS_PER_ACCOUNT=%r; using default %d",
            raw,
            DEFAULT_ALLOWANCE,
        )
        return DEFAULT_ALLOWANCE


def locked_reason(*, email_verified: bool) -> str | None:
    """Why this account cannot spend its free allowance right now, or ``None``.

    ``None`` means "not locked" — which includes the case where the free path
    is switched off entirely (``allowance() <= 0``). That distinction is the
    whole point of this function: a disabled policy must NOT report a lock,
    because the UI turns a lock into a promise ("verify your email and you get
    3 free generations") and there is nothing behind that promise when the
    allowance is 0. Silence is honest there; a carrot is not.
    """
    if allowance() <= 0:
        return None
    return None if email_verified else LOCK_EMAIL_UNVERIFIED


def _session():
    from archimedes.db import get_session

    return get_session()


def claim(user_id: str, *, email_verified: bool) -> int | None:
    """Take one free generation for *user_id*; ``None`` when there is none to take.

    ``None`` covers all five honest answers — the account's email is
    unverified (owner decision D1), allowance exhausted, allowance disabled, a
    concurrent request won the slot, or the ledger could not be read — because
    the caller does the same thing in every one of them: fall through to the
    wallet gate. The log line distinguishes them for an operator, and the
    caller re-asks :func:`locked_reason` when it needs to tell the user which
    of the unlocks applies.

    ``email_verified`` is required and keyword-only so that adding a second
    call site cannot quietly reopen the free tier to unverified accounts.
    """
    if not user_id:
        return None
    limit = allowance()
    if limit <= 0:
        return None
    if locked_reason(email_verified=email_verified) is not None:
        # No ledger read at all: an unverified account has nothing to claim, and
        # the wallet gate below is a complete answer for it.
        return None
    try:
        with _session() as session:
            grant = claim_free_grant(session, user_id=user_id, allowance=limit)
            if grant is None:
                return None
            grant_id = grant.id
            session.commit()
            return grant_id
    except Exception:
        # Includes the IntegrityError a concurrent claim raises on
        # uq_free_generation_grants_user_seq — that is the constraint doing its
        # job, so it is logged at info, not as a defect. Anything else here is
        # a real ledger failure and the caller still falls back to the gate.
        logger.info(
            "free-generation claim did not succeed for user %s — falling through to the wallet gate",
            user_id,
            exc_info=True,
        )
        return None


def stamp_job(grant_id: int, *, job_id: str) -> None:
    """Record which job the slot funded. Quiet: the generation is already queued.

    Not only provenance: :func:`release_for_job` looks the row up BY this stamp,
    so an unstamped slot is one the terminal-failure path can no longer hand
    back. The failure log below says exactly that rather than the reassurance it
    used to carry.
    """
    try:
        with _session() as session:
            stamp_grant_job(session, grant_id, job_id=job_id)
            session.commit()
    except Exception:
        logger.error(
            "free-generation grant %s could not be stamped with job %s — the slot stays counted "
            "as used and, with no job id on the row, cannot be returned automatically if that "
            "run fails. Manual release needed if it does.",
            grant_id,
            job_id,
            exc_info=True,
        )


def release(grant_id: int) -> None:
    """Hand a claimed slot back because nothing was delivered against it.

    Quiet on failure, but logged at ``error``: a slot that fails to be released
    silently costs the account one of its free generations for a run it never
    got. That errs against the user, which is why it is not logged as noise.
    """
    try:
        with _session() as session:
            release_grant(session, grant_id)
            session.commit()
    except Exception:
        logger.error(
            "free-generation grant %s could not be released — the account was charged one "
            "free generation for a run that never queued.",
            grant_id,
            exc_info=True,
        )


def release_for_job(job_id: str) -> bool:
    """Hand back the free slot *job_id* spent, because that run delivered nothing.

    The counterpart of :func:`release` for the OTHER half of the failure
    surface. ``release`` covers the failures ``/api/generate/start`` can see
    itself (the entitlement gate refusing, the enqueue erroring) and takes the
    ``grant_id`` it is still holding. This one covers everything that goes
    wrong AFTER the job is queued — the corpus yielding too few papers to fuse,
    a pipeline crash, a cancel — where the only handle the terminal path has is
    the job id the slot was stamped with.

    Returns ``True`` only when a slot actually moved back, so the caller can
    log the fact rather than claim it unconditionally.

    Quiet on failure, but logged at ``error`` for the same reason ``release``
    is: a slot that fails to come back costs the account one of its free
    generations for a run that produced nothing. That errs against the user.
    """
    if not job_id:
        return False
    try:
        with _session() as session:
            released = release_grant_for_job(session, job_id)
            session.commit()
            return released is not None
    except Exception:
        logger.error(
            "free-generation slot for job %s could not be released — the account was charged "
            "one free generation for a run that delivered nothing.",
            job_id,
            exc_info=True,
        )
        return False


def remaining(user_id: str) -> int | None:
    """Free generations left for *user_id*, or ``None`` if the ledger is unreadable.

    ``None`` is the honest absence ``GET /api/account/usage`` renders as a
    blank. A fabricated number in either direction is a claim the gate does not
    keep, which is exactly the defect the repo's first rule names.

    This is the LEDGER balance — how many slots remain unspent — and it says
    nothing about whether they are spendable today. :func:`locked_reason` is
    the other half, and the route reports both rather than folding a lock into
    a ``0``: an unverified account has 3 free generations waiting for it, and
    telling it "0 left" would be as false as telling it "3 available now".
    """
    limit = allowance()
    if limit <= 0:
        return 0
    try:
        with _session() as session:
            return max(0, limit - used_count(session, user_id))
    except Exception:
        logger.exception("free-generation remaining lookup failed for user %s — reporting unknown", user_id)
        return None
