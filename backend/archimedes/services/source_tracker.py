"""Source Tracking — Xia et al. 2026 § 4.3.

Every agent decision trace records the content hashes of papers it consulted,
binding the decision to a specific, verifiable corpus snapshot.

This module provides helpers for:
  - Building ``consulted_paper_hashes`` lists from paper dicts.
  - Resolving corpus content hashes for a set of arXiv ids.
  - Verifying that claimed source papers exist in the corpus.

Reference: Xia et al. 2026 (arxiv 2605.19337), § 4.3.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import sqlalchemy.exc as sa_exc

logger = logging.getLogger(__name__)

#: What "the corpus is unreachable" actually raises on the installed
#: SQLAlchemy 2.0.x — verified against it by
#: ``test_corpus_unavailable_covers_what_sqlalchemy_actually_raises``, not
#: assumed from the class names.
#:
#: ``DBAPIError`` alone is NOT enough, which is the bug this constant exists to
#: fix. It *is* the base of ``OperationalError`` / ``InterfaceError`` /
#: ``DatabaseError`` — a refused connect, a server that went away, an aborted
#: statement — so those three were already covered. But SQLAlchemy raises its
#: OWN exceptions, not the driver's, when the failure happens before a
#: statement ever reaches the driver, and none of those inherit from
#: ``DBAPIError``:
#:
#: * ``TimeoutError`` — the pool could not hand out a connection within
#:   ``pool_timeout`` because every one of them is blocked. That is precisely
#:   what a lock-wedged database looks like from here (the 94-minute
#:   ``AccessShareLock`` wedge on 2026-09-03), and it is the one that escaped:
#:   ``/verify`` returned a 500 instead of reporting ``corpus_unavailable``.
#:   Note this is ``sqlalchemy.exc.TimeoutError``, not the builtin.
#: * ``DisconnectionError`` — the pool decided the connection is dead.
#: * ``ResourceClosedError`` — the connection or result was closed underneath
#:   us mid-read.
#:
#: Deliberately NOT widened to ``SQLAlchemyError``: that would swallow
#: ``ArgumentError`` / ``NoSuchModuleError`` (a misconfigured URL) and
#: ``InvalidRequestError`` (a query this module built wrong), and a bug that
#: reports itself as an outage is the failure mode the docstring below is
#: about.
CORPUS_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    sa_exc.DBAPIError,
    sa_exc.TimeoutError,
    sa_exc.DisconnectionError,
    sa_exc.ResourceClosedError,
)


class CorpusUnavailable(RuntimeError):
    """The corpus could not be read — as distinct from "it has no such paper".

    Raised by :func:`corpus_content_hashes` when the database itself is
    unreachable. Callers must NOT collapse this into an empty result: an
    outage that reads as "none of these papers exist" turns a verification
    endpoint into a machine that reports fabricated provenance whenever
    Postgres blinks (the same failure class #1359 fixed for the on-chain half
    of ``/verify``).
    """


def build_consulted_hashes(papers: list[dict[str, Any]]) -> list[str]:
    """Extract a sorted list of ``{arxiv_id}:{content_hash}`` strings.

    Parameters
    ----------
    papers : list[dict]
        Paper dicts with ``arxiv_id`` and optionally ``content_hash``
        or ``pdf_sha256`` fields.

    Returns
    -------
    list[str]
        Sorted list of ``arxiv_id:hash`` strings for deterministic ordering.
    """
    entries: list[str] = []
    for p in papers:
        arxiv_id = p.get("arxiv_id", "")
        content_hash = p.get("content_hash") or p.get("pdf_sha256") or ""
        if arxiv_id:
            entries.append(f"{arxiv_id}:{content_hash}")
    return sorted(entries)


def split_consulted_entry(entry: str) -> tuple[str, str]:
    """``"2301.1:abc"`` → ``("2301.1", "abc")``; ``"2301.1:"`` → ``("2301.1", "")``."""
    arxiv_id, _, claimed = entry.partition(":")
    return arxiv_id, claimed


def corpus_content_hashes(arxiv_ids: Iterable[str]) -> dict[str, str]:
    """``{arxiv_id: content_hash}`` for the requested ids **found in the corpus**.

    A paper present with no hydrated hash maps to ``""`` — that is what makes
    "the corpus has this paper but no hash for it" distinguishable from "the
    corpus does not have this paper", which is the distinction every caller
    here actually needs. Ids absent from the corpus are absent from the result.

    Raises :class:`CorpusUnavailable` when the database is unreachable — for
    the full set of exception classes that means, and why ``DBAPIError`` alone
    was not it, see :data:`CORPUS_UNAVAILABLE_ERRORS`.

    Bugs in this function (a wrong model class, a malformed query) are
    deliberately NOT converted — the imports sit outside the ``try`` and the
    catch names connection-level failures only, so a typo cannot masquerade as
    an outage. That exact defect shipped once already: ``resolve_paper_hashes``
    imported a class name that did not exist, swallowed the ``ImportError``,
    and returned "nothing resolves" permanently and indistinguishably from the
    honest empty answer.

    **Opens its OWN session, so it must only be called from a caller that
    holds none.** ``/verify`` qualifies (its state lives in Redis). A caller
    already inside a transaction must NOT use this — a second connection taken
    while the first holds ``AccessShareLock`` on ``papers`` is one half of the
    wedge that took production down for 94 minutes on 2026-09-03
    (``docs/incidents/2026-09-03-paper-advance-ddl-wedge.md``). That is why
    ``paper_trace.resolve_paper_hashes`` keeps its own caller-session +
    SAVEPOINT lookup instead of delegating here, and the difference is
    load-bearing. Its catch is still ``DBAPIError`` alone, correctly: it runs
    inside a connection the caller already holds, so the pool-level failures
    listed at :data:`CORPUS_UNAVAILABLE_ERRORS` — a checkout that times out
    above all — cannot arise on that path. This one takes a connection, so they
    can.
    """
    wanted = sorted({i.strip() for i in (arxiv_ids or []) if i and i.strip()})
    if not wanted:
        return {}

    from archimedes.db import get_session
    from archimedes.models.corpus_store import PaperRecord

    try:
        with get_session() as session:
            rows = session.query(PaperRecord).filter(PaperRecord.arxiv_id.in_(wanted)).all()
    except CORPUS_UNAVAILABLE_ERRORS as exc:
        logger.warning("source_tracker: corpus content-hash lookup failed", exc_info=True)
        raise CorpusUnavailable("corpus content-hash lookup failed") from exc

    # Outside the try: an attribute this code is wrong about is a bug, not an
    # outage, and must not be laundered into an empty dict.
    return {row.arxiv_id: (row.content_hash or row.pdf_sha256 or "") for row in rows}


def verify_source_papers(
    consulted_hashes: list[str],
    corpus: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify that all consulted papers exist in the current corpus.

    Parameters
    ----------
    consulted_hashes : list[str]
        ``arxiv_id:hash`` strings from a trace.
    corpus : list[dict]
        Current paper corpus dicts.

    Returns
    -------
    dict
        ``{"verified": bool, "missing": list[str], "hash_mismatch": list[str]}``

    An **empty claimed hash asserts nothing** and is checked for existence
    only. That is the honest reading while the corpus's ``content_hash`` /
    ``pdf_sha256`` columns are NULL (#1091): treating ``""`` as a hash to
    compare would either fail every trace or, worse, invite synthesizing a
    value on the corpus side to make the comparison pass.
    """
    corpus_by_id: dict[str, str] = {}
    for p in corpus:
        aid = p.get("arxiv_id", "")
        ch = p.get("content_hash") or p.get("pdf_sha256") or ""
        if aid:
            corpus_by_id[aid] = ch

    missing: list[str] = []
    hash_mismatch: list[str] = []

    for entry in consulted_hashes:
        arxiv_id, claimed_hash = split_consulted_entry(entry)

        if arxiv_id not in corpus_by_id:
            missing.append(arxiv_id)
        elif claimed_hash and corpus_by_id[arxiv_id] != claimed_hash:
            hash_mismatch.append(arxiv_id)

    return {
        "verified": len(missing) == 0 and len(hash_mismatch) == 0,
        "missing": missing,
        "hash_mismatch": hash_mismatch,
    }
