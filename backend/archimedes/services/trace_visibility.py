"""Reasoning-trace visibility predicate — issue #1556.

Before this module the four ``/api/traces/*`` READ routes had no auth
dependency at all. ``vault_address`` was a *filter* query param, not a gate:
omit it and you enumerated every trace on the platform, and
``/api/traces/{id}/canonical`` handed back the full hashed body —
``portfolio_before`` / ``portfolio_after`` (holdings) and ``market_context``.
That was survivable only for as long as every trace belonged to the house
agent; ``POST /api/vaults/create`` is user-facing, so the first user-owned
vault to publish a trace would have published its portfolio and reasoning to
the internet.

**Ownership resolution is three-layered, most-authoritative first.**

1. **The stamp on the record.** Every trace persisted through
   ``AgentStateStore.save_trace`` now carries ``owner_user_id`` /
   ``owner_wallet`` (see that method). This is the layer that matters: it is
   written once, at publish time, and read back with no database round-trip,
   so a Postgres outage cannot downgrade a private trace to a public one.
2. **The vault's owner, looked up now.** Legacy rows persisted before the
   stamp existed carry no ownership at all, so ownership is recovered from
   ``vault_metadata`` (``owner_user_id`` / ``creator_address``) and, for a
   vault created through ``POST /api/vaults/create`` whose owner never wrote
   metadata, from the ``vault_created`` rows in ``identity_events``. This is
   the Redis-store equivalent of the data backfill a schema migration would
   ship with — traces live in Redis, not in a table, so there is no column to
   add and no Alembic revision to write. On the READ path this lookup is
   **memoized per vault** (:func:`safe_resolve_vault_owners`) and run off the
   event loop by its callers — re-running it per request cost the anonymous
   ``/reasoning`` page 21.2s (#1573). The write-path resolver
   (:func:`resolve_vault_owners`, which produces the permanent stamp) stays
   uncached.
3. **The house-vault allowlist** (``PUBLIC_TRACE_VAULTS``, falling back to the
   agent runner's own ``AGENT_VAULT_ADDRESSES``). This is the FLOOR, and it
   only ever applies to a row whose ownership could not be established by (1)
   or (2). It cannot make an owned trace public: the ownership branches return
   before it is consulted.

**Anonymous callers see house-public traces and nothing else.** That is not a
convenience: ``/reasoning`` and ``/quant-lab`` are the public proof surface and
render an unfiltered ``GET /api/traces/?limit=50``. Filtering per row (rather
than the issue's interim "require ``vault_address`` on every read") gets the
same enumeration guarantee — a row that is not yours is never in the response —
without blanking the page that the product's central claim rests on.

Deliberately shaped after ``services/strategy_visibility.py``: one predicate,
never re-implemented at a call site. This codebase's characteristic defect is a
rule fixed in the one function the current ticket touches while sibling readers
keep the old behaviour, and a visibility rule that disagrees with itself across
two routes is an authorization bug.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Comma-separated house/demo vault addresses whose traces are the public proof
#: surface. ``AGENT_VAULT_ADDRESSES`` (the agent runner's own explicit vault
#: list) is the fallback because a deployment that pins the runner to specific
#: vaults has already named its house vaults there.
_PUBLIC_VAULTS_ENV = "PUBLIC_TRACE_VAULTS"
_AGENT_VAULTS_ENV = "AGENT_VAULT_ADDRESSES"

#: Upper bound on rows pulled out of the trace index before the visibility
#: filter runs. The filter must run over the whole candidate set — windowing
#: first and filtering second returns short pages and lets the page size leak
#: how many of another user's traces were skipped.
MAX_TRACE_SCAN = 2000

#: How long a resolved ``vault → (owner_user_id, owner_wallet)`` pair stays
#: memoized on the READ path, in seconds (#1573).
#:
#: **Why a cache is safe here, and why 300s is the right number.** The mapping
#: is write-once in practice: a vault gets its owner when it is deployed
#: (``POST /api/vaults/create`` → a ``vault_created`` identity event) or when
#: its on-chain owner first writes metadata, and it does not change again.
#: Both of those writers call :func:`invalidate_vault_owner`, so the in-process
#: entry for a vault that *does* acquire an owner is dropped at the moment it
#: acquires one — the TTL is the backstop for a write that happened in another
#: process (a second Fargate task), not the primary mechanism.
#:
#: **The cache cannot flip a private row public**, which is the only direction
#: that would be a gate regression:
#:
#: - A trace persisted after #1556 carries its owner ON THE RECORD, and a
#:   stamped row never consults this resolver at all (see
#:   :func:`can_read_trace` and the listing route's set comprehension). Every
#:   row written from here on is therefore outside the cache's blast radius.
#: - For a legacy unstamped row, a stale *positive* entry keeps the row private
#:   — to the previous owner rather than the new one. Private-to-someone, never
#:   public.
#: - A stale *negative* entry (vault looked unowned, then acquired an owner) is
#:   the only widening direction. It is bounded by this TTL, closed immediately
#:   in-process by :func:`invalidate_vault_owner`, and floored by
#:   ``PUBLIC_TRACE_VAULTS``: with the allowlist armed an unowned row outside
#:   the list is private, so the armed configuration fails CLOSED.
VAULT_OWNER_CACHE_TTL_SECONDS = 300

#: Hard bound on cached entries so a pathological caller cannot grow the cache
#: without limit. One entry is two short strings; 4096 is far above the number
#: of vaults that exist, and blowing past it clears rather than evicts (the
#: cost of a cleared cache is one extra lookup, so cleverness buys nothing).
VAULT_OWNER_CACHE_MAX_ENTRIES = 4096

#: ``{lowercased vault → (expires_at_monotonic, owner_pair_or_None)}``.
#: A stored ``None`` is a NEGATIVE result — "this vault has no recoverable
#: owner" — and caching it is the point: the unresolvable case is the expensive
#: one, because it is what triggers the ``identity_events`` scan below.
_vault_owner_cache: dict[str, tuple[float, tuple[str | None, str | None] | None]] = {}
_vault_owner_cache_lock = threading.Lock()

_warned_unconfigured_allowlist = False


class TraceOwnerLookupIncomplete(Exception):
    """The ownership lookup did not finish; ``partial`` is what it did get.

    Exists to keep "this vault has no owner" distinguishable from "the database
    did not answer". Only the former may be negative-cached — caching a DB
    outage as a confirmed absence would let the outage outlive itself by a full
    TTL.
    """

    def __init__(self, partial: dict[str, tuple[str | None, str | None]]) -> None:
        super().__init__("trace ownership lookup did not complete")
        self.partial = partial


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def public_trace_vaults() -> frozenset[str]:
    """Lowercased house-vault allowlist, or an empty set when unconfigured."""
    raw = os.getenv(_PUBLIC_VAULTS_ENV) or os.getenv(_AGENT_VAULTS_ENV) or ""
    return frozenset(v.strip().lower() for v in raw.split(",") if v.strip())


def is_public_trace_vault(vault_address: object) -> bool:
    """Is this vault's *unowned* trace part of the public proof surface?

    Only ever reached for a row with no resolvable owner — see
    ``is_trace_visible``. Two hard edges:

    - **A blank ``vault_address`` is never public.** Generation traces
      (``trigger="fusion_generation"``) are written with ``vault_address=""``
      and carry a user's private strategy thesis in ``reasoning``. An ownerless
      body with no vault behind it is not a house artifact, and defaulting it
      public is precisely the leak this module exists to close.
    - **An unconfigured allowlist means "every unowned row is the house
      agent's"**, which is what the platform actually looks like today and what
      keeps ``/reasoning`` rendering. It is a *documented* default, not a
      silent one: the first time it decides a row is public it logs a WARNING
      naming the env var, so an operator sees that the floor is not armed.
      Setting ``PUBLIC_TRACE_VAULTS`` arms it and every unowned row outside the
      list goes private.
    """
    addr = _norm(vault_address)
    if not addr:
        return False

    allowlist = public_trace_vaults()
    if allowlist:
        return addr in allowlist

    global _warned_unconfigured_allowlist
    if not _warned_unconfigured_allowlist:
        _warned_unconfigured_allowlist = True
        logger.warning(
            "%s is not set — traces with no resolvable owner are served publicly (house-agent default). "
            "Set %s to the house vault addresses to arm the allowlist floor (#1556).",
            _PUBLIC_VAULTS_ENV,
            _PUBLIC_VAULTS_ENV,
        )
    return True


def is_trace_visible(
    view: dict[str, Any] | None,
    caller_wallet: str | None,
    *,
    caller_user_id: str | None = None,
) -> bool:
    """Can ``caller`` read this trace?

    ``view`` is ``{"vault_address", "owner_user_id", "owner_wallet"}`` as
    produced by :func:`trace_owner_view`.

    OWNERSHIP IS TWO-TIERED, for the same reason ``is_strategy_visible`` is:
    canonical identity (Better Auth ``auth_users.id``) arrived after rows
    already existed.

    - When an ``owner_user_id`` is known, that is the ONLY thing that grants
      access. A matching wallet must not, or the canonical model is bypassable
      by anyone who controls a wallet the row happens to name.
    - When it is not, fall back to the wallet comparison.
    - When neither is known, the row is unowned and the house-vault floor
      decides.
    """
    if view is None:
        return False

    owner_user_id = view.get("owner_user_id")
    if owner_user_id:
        # Both sides must be non-empty: `None == None` is not ownership.
        return bool(caller_user_id) and str(owner_user_id) == str(caller_user_id)

    owner_wallet = _norm(view.get("owner_wallet"))
    if owner_wallet:
        return bool(caller_wallet) and _norm(caller_wallet) == owner_wallet

    return is_public_trace_vault(view.get("vault_address"))


def _resolve_vault_owners_uncached(wanted: frozenset[str]) -> dict[str, tuple[str | None, str | None]]:
    """The database half of :func:`resolve_vault_owners`. Always hits the DB.

    ``wanted`` is already normalized and non-empty. Raises
    :class:`TraceOwnerLookupIncomplete` (carrying whatever it managed to read)
    when the round trip fails, so callers can tell "no owner" from "no answer".
    """
    owners: dict[str, tuple[str | None, str | None]] = {}
    try:
        from archimedes.db import get_session
        from archimedes.models.chat import VaultMetadata
        from archimedes.models.identity import IdentityEvent

        with get_session() as session:
            rows = session.query(VaultMetadata).filter(VaultMetadata.vault_address.in_(sorted(wanted))).all()
            for row in rows:
                owners[_norm(row.vault_address)] = (row.owner_user_id or None, _norm(row.creator_address) or None)

            missing = set(wanted) - set(owners)
            if missing:
                # `meta` is JSONB on Postgres and plain JSON on SQLite; there is
                # no portable containment operator across both, so this filters
                # on the indexed `event_type` column and matches the address in
                # Python. Bounded by the number of vaults ever created, which
                # is the same order as the vault list itself.
                #
                # This is the expensive branch — up to MAX_TRACE_SCAN rows
                # materialized as ORM objects — and it fires precisely when a
                # vault CANNOT be resolved, which is why the negative result is
                # cached alongside the positive one (#1573).
                events = (
                    session.query(IdentityEvent)
                    .filter(IdentityEvent.event_type == "vault_created")
                    .order_by(IdentityEvent.id.desc())
                    .limit(MAX_TRACE_SCAN)
                    .all()
                )
                for event in events:
                    addr = _norm((event.meta or {}).get("vault_address"))
                    if addr in missing and addr not in owners and _norm(event.wallet):
                        owners[addr] = (None, _norm(event.wallet))
    except Exception as exc:
        logger.warning("trace ownership lookup failed — falling back to on-record stamps only", exc_info=True)
        raise TraceOwnerLookupIncomplete(owners) from exc

    return owners


def resolve_vault_owners(vault_addresses: set[str]) -> dict[str, tuple[str | None, str | None]]:
    """``{lowercased vault → (owner_user_id, owner_wallet)}`` for legacy rows.

    Two sources, in precedence order:

    1. ``vault_metadata`` — written by ``POST /api/vaults/metadata``, which
       proves the writer is the vault's on-chain owner before it commits.
    2. ``identity_events`` rows of type ``vault_created`` — emitted by ``POST
       /api/vaults/create``. This closes the gap where a user deploys a vault
       and never gets as far as writing metadata: without it that vault reads
       as unowned and its traces would fall through to the house floor.

    **Fail-soft, and safely so.** A DB outage returns what it has rather than
    raising, because the read routes must keep serving; the reason that is not
    a fail-open hole is that every trace written after #1556 carries its owner
    *on the record*, so this lookup is a backfill for legacy rows only.
    Callers that care about the distinction should arm ``PUBLIC_TRACE_VAULTS``.

    **Deliberately uncached.** This is the WRITE-path resolver: ``save_trace``
    stamps a trace with what this returns, and that stamp is permanent, so it
    must never be answered from a memo that predates the vault's owner
    (#1573). Read paths use :func:`safe_resolve_vault_owners`, which is
    memoized precisely because its answer is re-derived on every request.
    """
    wanted = frozenset(_norm(a) for a in vault_addresses if _norm(a))
    if not wanted:
        return {}
    try:
        return _resolve_vault_owners_uncached(wanted)
    except TraceOwnerLookupIncomplete as exc:
        return exc.partial


def invalidate_vault_owner(vault_address: object) -> None:
    """Drop one vault's memoized owner (#1573).

    Called from the two writes that can *create* ownership — the
    ``vault_created`` identity event in ``POST /api/vaults/create`` and the
    ``vault_metadata`` commit in ``POST /api/vaults/metadata``. Without this, a
    vault whose "unowned" answer was cached moments before it acquired an owner
    would keep reading as unowned for the rest of the TTL, and an unowned
    legacy row falls to the house-vault floor. This makes the cache correct at
    the instant of the write in this process; the TTL covers the same write
    arriving in a sibling process.
    """
    addr = _norm(vault_address)
    if not addr:
        return
    with _vault_owner_cache_lock:
        _vault_owner_cache.pop(addr, None)


def clear_vault_owner_cache() -> None:
    """Forget every memoized vault owner. For tests and operational reset."""
    with _vault_owner_cache_lock:
        _vault_owner_cache.clear()


def safe_resolve_vault_owners(vault_addresses: set[str]) -> dict[str, tuple[str | None, str | None]]:
    """Memoized, non-raising ownership lookup for the READ routes (#1573).

    Two jobs, both of which the read routes need and the write path does not:

    **1. It cannot raise into a read route.** ``_resolve_vault_owners_uncached``
    already converts database errors into a partial result, so this covers the
    class of failure it cannot anticipate — an import error, a model change, a
    mocked-out dependency. A read route degrading to "no legacy owners
    recovered" is safe (unstamped rows fall to the allowlist floor); a read
    route 500ing because an ownership lookup blew up is not.

    **2. It memoizes, per vault, for ``VAULT_OWNER_CACHE_TTL_SECONDS``.** Before
    this, ``GET /api/traces/?limit=50`` re-ran the whole lookup on every
    request — a synchronous ``vault_metadata`` query plus, for any vault it
    could not resolve, a ``MAX_TRACE_SCAN``-row ``identity_events`` scan —
    inside the async event loop, on the anonymous page that ``/reasoning`` and
    ``/quant-lab`` render. Measured live at 21.2s (#1573). The listing covers
    only a handful of distinct vaults, so almost all of that work was the same
    answer computed again.

    Only the vaults that MISS reach the database; a request whose vaults are
    all cached does no database work at all. Negative results are cached too,
    and they are the ones that matter: an unresolvable vault is what triggers
    the ``identity_events`` scan. A result is negative-cached only when the
    lookup actually completed — see :class:`TraceOwnerLookupIncomplete`.

    Gate semantics are unchanged: this returns the same mapping the uncached
    resolver would, so every row gets the same verdict. See
    :data:`VAULT_OWNER_CACHE_TTL_SECONDS` for why the staleness window cannot
    turn a private trace public.
    """
    try:
        wanted = frozenset(_norm(a) for a in vault_addresses if _norm(a))
        if not wanted:
            return {}

        now = time.monotonic()
        resolved: dict[str, tuple[str | None, str | None]] = {}
        missing: set[str] = set()
        with _vault_owner_cache_lock:
            for addr in wanted:
                entry = _vault_owner_cache.get(addr)
                if entry is None or entry[0] <= now:
                    missing.add(addr)
                elif entry[1] is not None:
                    resolved[addr] = entry[1]
                # A live negative entry contributes nothing to the mapping —
                # "absent" is exactly how the uncached resolver reports an
                # unowned vault, so the verdict is identical either way.

        if not missing:
            return resolved

        complete = True
        try:
            looked_up = _resolve_vault_owners_uncached(frozenset(missing))
        except TraceOwnerLookupIncomplete as exc:
            looked_up, complete = exc.partial, False

        resolved.update(looked_up)
        expires_at = time.monotonic() + VAULT_OWNER_CACHE_TTL_SECONDS
        with _vault_owner_cache_lock:
            for addr in missing:
                value = looked_up.get(addr)
                if value is None and not complete:
                    # "The database did not answer" is not "this vault has no
                    # owner". Leaving it uncached costs one lookup; caching it
                    # would extend an outage's effect past the outage.
                    continue
                _vault_owner_cache[addr] = (expires_at, value)
            if len(_vault_owner_cache) > VAULT_OWNER_CACHE_MAX_ENTRIES:
                for stale in [k for k, (exp, _) in _vault_owner_cache.items() if exp <= now]:
                    del _vault_owner_cache[stale]
                if len(_vault_owner_cache) > VAULT_OWNER_CACHE_MAX_ENTRIES:
                    _vault_owner_cache.clear()
        return resolved
    except Exception:
        logger.warning("trace ownership lookup raised — continuing with on-record stamps only", exc_info=True)
        return {}


def trace_owner_view(trace: dict[str, Any], owners: dict[str, tuple[str | None, str | None]]) -> dict[str, Any]:
    """Merge the on-record ownership stamp with the looked-up vault owner.

    The stamp wins. A row that was persisted with ``owner_user_id`` recorded is
    authoritative about its own owner even if the vault has since been handed
    to someone else — the reasoning body in that row belonged to the account
    that produced it.
    """
    vault_address = trace.get("vault_address", "")
    owner_user_id = trace.get("owner_user_id") or None
    owner_wallet = _norm(trace.get("owner_wallet")) or None

    if owner_user_id is None and owner_wallet is None:
        looked_up = owners.get(_norm(vault_address))
        if looked_up is not None:
            owner_user_id, owner_wallet = looked_up

    return {
        "vault_address": vault_address,
        "owner_user_id": owner_user_id,
        "owner_wallet": owner_wallet,
    }


def can_read_trace(
    trace: dict[str, Any],
    caller_wallet: str | None,
    *,
    caller_user_id: str | None = None,
) -> bool:
    """Single-trace convenience: resolve this trace's owner, then decide.

    Skips the database entirely when the record carries its own stamp — the
    common case for anything published after #1556.
    """
    if trace.get("owner_user_id") or trace.get("owner_wallet"):
        owners: dict[str, tuple[str | None, str | None]] = {}
    else:
        owners = safe_resolve_vault_owners({str(trace.get("vault_address") or "")})
    return is_trace_visible(trace_owner_view(trace, owners), caller_wallet, caller_user_id=caller_user_id)
