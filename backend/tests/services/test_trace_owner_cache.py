"""Memoized trace-ownership resolution — issue #1573.

``GET /api/traces/?limit=50`` was measured at **21.2s** on the live public
proof surface right after #1562 deployed. The listing runs the ownership
predicate over up to ``MAX_TRACE_SCAN`` candidate rows, and every request
re-derived the vault → owner mapping from scratch: a synchronous
``vault_metadata`` query plus — for any vault it could not resolve — a
``MAX_TRACE_SCAN``-row ``identity_events`` scan, executed inside the async
event loop. The rows cover a handful of distinct vaults, so nearly all of that
work was the same answer computed again.

**Every test here is a GUARD test.** Each one either fails outright against
pre-#1573 ``trace_visibility.py`` or is paired with an explicit proof that the
property it asserts is not free. The demonstration is recorded in the PR body:
with ``safe_resolve_vault_owners``'s memo removed, the "once per distinct
vault" tests report one database round trip *per request* instead of one in
total.

The non-goal is as load-bearing as the goal: **no verdict may change.** The
last group below re-runs the gate's own questions against a warm cache and
asserts the answers are byte-identical to the cold ones, including for the
private legacy row that must never appear on an anonymous listing.

Hermetic: per-test tmp SQLite, ``AgentStateStore`` mocked at its boundary. No
Redis, no Postgres, no ``.env``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from archimedes.services import trace_visibility
from archimedes.services.trace_visibility import (
    TraceOwnerLookupIncomplete,
    can_read_trace,
    clear_vault_owner_cache,
    invalidate_vault_owner,
    is_trace_visible,
    safe_resolve_vault_owners,
    trace_owner_view,
)
from httpx import ASGITransport, AsyncClient

from tests.db_isolation import redirect_to_tmp_sqlite

OWNER_USER_ID = "user-owner-1573"
OWNER_WALLET = "0x1111111111111111111111111111111111111111"

HOUSE_VAULT = "0xaaaa000000000000000000000000000000000aaa"
#: Five distinct user vaults — the live shape the issue measured: 50 listed
#: rows spread over a handful of addresses.
USER_VAULTS = [f"0x{str(i) * 40}" for i in range(1, 6)]

SECRET_REASONING = "PRIVATE-1573 the user's undisclosed rotation thesis"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture(autouse=True)
def _unset_allowlist(monkeypatch):
    """Run against the WEAKEST floor, exactly as the #1556 gate tests do.

    With ``PUBLIC_TRACE_VAULTS`` unset an unowned trace defaults to
    house-public, so anything shown hidden here is hidden by *ownership* — the
    thing the cache could in principle corrupt — and not by an allowlist that
    happened to be armed.
    """
    monkeypatch.delenv("PUBLIC_TRACE_VAULTS", raising=False)
    monkeypatch.delenv("AGENT_VAULT_ADDRESSES", raising=False)


def _seed_owner(vault: str, *, user_id: str = OWNER_USER_ID) -> None:
    """A ``vault_metadata`` row making ``vault`` owned by ``user_id``."""
    from archimedes.db import get_session
    from archimedes.models.account import AuthUser
    from archimedes.models.chat import VaultMetadata
    from archimedes.models.identity import WalletIdentity

    now = datetime.now(UTC)
    with get_session() as session:
        session.merge(
            AuthUser(id=user_id, name=user_id, email=f"{user_id}@example.test", created_at=now, updated_at=now)
        )
        session.merge(WalletIdentity(wallet_address=OWNER_WALLET.lower(), actor_class="human", first_seen_at=now))
        session.merge(
            VaultMetadata(
                vault_address=vault.lower(),
                name="v",
                symbol="V",
                creator_address=OWNER_WALLET.lower(),
                owner_user_id=user_id,
                strategy_ids="[]",
            )
        )
        session.commit()


@contextmanager
def _count_db_lookups():
    """Spy on the one function that actually opens a database session.

    Yields a list of the address sets it was asked to resolve, so a test can
    assert both HOW MANY round trips happened and WHICH vaults each one
    covered. Delegates to the real implementation — this counts the database
    path, it does not replace it.
    """
    calls: list[frozenset[str]] = []
    real = trace_visibility._resolve_vault_owners_uncached

    def _spy(wanted):
        calls.append(frozenset(wanted))
        return real(wanted)

    with patch.object(trace_visibility, "_resolve_vault_owners_uncached", _spy):
        yield calls


def _trace(vault: str, idx: int, *, stamped: bool = False) -> dict:
    trace = {
        "id": f"trace-{idx}",
        "vault_address": vault,
        "decision_type": "rebalance",
        "trigger": "drift",
        "timestamp": f"2026-08-30T00:00:{idx:02d}+00:00",
        "reasoning": SECRET_REASONING,
        "confidence": 0.5,
        "trades_executed": [],
        "strategies_referenced": [],
        "trace_hash": f"0xhash{idx}",
        "market_context": {},
        "is_verified": False,
    }
    if stamped:
        trace["owner_user_id"] = OWNER_USER_ID
        trace["owner_wallet"] = OWNER_WALLET.lower()
    return trace


def _fifty_rows(*, stamped: bool = False) -> list[dict]:
    """50 rows over 5 distinct vaults — the listing the issue measured."""
    return [_trace(USER_VAULTS[i % len(USER_VAULTS)], i, stamped=stamped) for i in range(50)]


def _assert_mirrors(fake, real) -> None:
    """The fake store must accept EXACTLY what the route passes the real one.

    ``list_traces``'s call to ``state.list_traces(...)`` sits inside an
    ``except Exception:`` that logs "Redis unavailable" and falls back to the
    on-chain-only listing. So a fake whose signature has drifted does **not**
    raise out of a test here — the ``TypeError`` is swallowed as an outage and
    every listing below is quietly re-routed onto the fallback path, where
    there are no off-chain rows, the ownership resolver is never consulted, and
    the page comes back empty.

    That failure mode is silent in the direction that matters least and loud in
    the direction that matters most: the round-trip test reports ``0`` lookups
    (looking like a cache that got *better*) while the privacy test reports an
    empty page (looking like a gate that got *stricter*). Both are lies about
    code that never ran. It is exactly how #1573 (this file) and #1569 (which
    inserted ``strategy_id`` into that signature) each stayed green alone and
    broke on union.

    Names AND order, because the route is free to call positionally.
    """
    import inspect

    want = [p for p in inspect.signature(real).parameters if p != "self"]
    got = list(inspect.signature(fake).parameters)
    assert got == want, f"the fake store has drifted from AgentStateStore.list_traces: {got} != {want}"


@contextmanager
def _store(traces: list[dict]):
    from archimedes.services.redis_state import AgentStateStore, trace_references_strategy

    by_id = {t["id"]: t for t in traces}
    by_id.update({t["trace_hash"]: t for t in traces})

    async def _list(vault_address=None, decision_type=None, strategy_id=None, limit=20, offset=0):
        rows = [
            t
            for t in traces
            if (not vault_address or t["vault_address"].lower() == vault_address.lower())
            and (not decision_type or t.get("decision_type") == decision_type)
            # Delegates rather than re-implements: a second copy of the
            # provenance rule in a fake is a way to prove the wrong thing.
            and (not strategy_id or trace_references_strategy(t, strategy_id))
        ]
        return rows[offset : offset + limit], len(rows)

    _assert_mirrors(_list, AgentStateStore.list_traces)

    with (
        patch.object(AgentStateStore, "list_traces", AsyncMock(side_effect=_list)),
        patch.object(AgentStateStore, "get_trace", AsyncMock(side_effect=lambda k: by_id.get(k))),
        patch.object(AgentStateStore, "close", AsyncMock()),
    ):
        yield


@contextmanager
def _anonymous():
    with (
        patch("archimedes.api.account_auth.get_current_user", return_value=None),
        patch("archimedes.api.auth_siwe.get_verified_wallet", return_value=None),
    ):
        yield


async def _get(path: str, **params):
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, params=params)


# ── The resolver runs once per distinct vault, not once per row/request ─────


async def test_a_fifty_row_listing_resolves_each_distinct_vault_once():
    """ACCEPTANCE (#1573): 50 rows over 5 vaults, three requests → 5 vaults
    resolved, ONE database round trip.

    Pre-#1573 this was one full round trip *per request* — the batching inside
    a single request was already there, but nothing survived the request
    boundary, so the anonymous proof surface paid the ``vault_metadata`` query
    plus the ``identity_events`` scan on every single page load. Reverting the
    memo makes this assert 3 round trips and fail.
    """
    for vault in USER_VAULTS:
        _seed_owner(vault)

    with _store(_fifty_rows()), _anonymous(), _count_db_lookups() as calls:
        for _ in range(3):
            resp = await _get("/api/traces/", limit=50)
            assert resp.status_code == 200

    assert len(calls) == 1, f"expected one database round trip across three listings, got {len(calls)}"
    assert calls[0] == frozenset(v.lower() for v in USER_VAULTS)


async def test_repeated_detail_reads_of_a_legacy_row_resolve_its_vault_once():
    """The detail routes were the genuinely per-call path: ``can_read_trace``
    opened a session for every unstamped trace read. Ten reads → one lookup."""
    _seed_owner(USER_VAULTS[0])
    row = _trace(USER_VAULTS[0], 0)

    with _store([row]), _anonymous(), _count_db_lookups() as calls:
        for _ in range(10):
            assert (await _get("/api/traces/trace-0")).status_code == 404

    assert len(calls) == 1, f"expected one database round trip across ten detail reads, got {len(calls)}"


def test_the_unresolvable_vault_is_negative_cached():
    """The MISS is the expensive case, so the miss is what must be cached.

    A vault with no ``vault_metadata`` row is what triggers the
    ``MAX_TRACE_SCAN``-row ``identity_events`` scan. Caching only successes
    would leave exactly the slow path uncached.
    """
    unknown = "0xdead" + "0" * 36

    with _count_db_lookups() as calls:
        assert safe_resolve_vault_owners({unknown}) == {}
        assert safe_resolve_vault_owners({unknown}) == {}
        assert safe_resolve_vault_owners({unknown.upper()}) == {}

    assert len(calls) == 1, f"the negative result was not cached: {len(calls)} round trips"


def test_only_the_uncached_vaults_reach_the_database():
    """A partially warm request must not re-resolve what it already knows."""
    for vault in USER_VAULTS:
        _seed_owner(vault)

    with _count_db_lookups() as calls:
        safe_resolve_vault_owners(set(USER_VAULTS[:3]))
        safe_resolve_vault_owners(set(USER_VAULTS))

    assert len(calls) == 2
    assert calls[0] == frozenset(v.lower() for v in USER_VAULTS[:3])
    assert calls[1] == frozenset(v.lower() for v in USER_VAULTS[3:]), "warm vaults were re-resolved"


# ── A stamped row never consults the resolver ───────────────────────────────


async def test_a_stamped_listing_never_touches_the_resolver():
    """The on-record stamp is the layer that matters (#1556): 50 stamped rows
    must produce ZERO database work, cache or no cache."""
    with _store(_fifty_rows(stamped=True)), _anonymous(), _count_db_lookups() as calls:
        resp = await _get("/api/traces/", limit=50)

    assert resp.status_code == 200
    assert resp.json()["traces"] == []  # private, and privately: no leak
    assert calls == [], "a stamped row consulted the ownership resolver"


def test_can_read_trace_skips_the_resolver_for_a_stamped_row():
    """Unit-level companion: the short-circuit is in the predicate itself, so a
    Postgres outage cannot downgrade a stamped private trace."""
    stamped = _trace(USER_VAULTS[0], 0, stamped=True)

    with _count_db_lookups() as calls:
        assert can_read_trace(stamped, None, caller_user_id=OWNER_USER_ID)
        assert not can_read_trace(stamped, None, caller_user_id="someone-else")

    assert calls == []


# ── Zero gate-semantics change ──────────────────────────────────────────────


def test_a_cache_hit_returns_the_same_verdict_as_a_cold_resolve():
    """ACCEPTANCE (#1573): warm answers are identical to cold ones.

    Cold verdicts are computed with the cache empty; the resolver's database
    path is then made to raise, so a second pass can ONLY be answered from the
    memo — and every verdict must match, for owner, non-owner and anonymous
    alike.
    """
    for vault in USER_VAULTS:
        _seed_owner(vault)
    callers = [(None, None), (OWNER_USER_ID, OWNER_WALLET), ("someone-else", "0x9" + "9" * 39)]
    rows = _fifty_rows()

    def _verdicts() -> list[bool]:
        owners = safe_resolve_vault_owners({r["vault_address"] for r in rows})
        return [
            is_trace_visible(trace_owner_view(r, owners), wallet, caller_user_id=uid)
            for r in rows
            for uid, wallet in callers
        ]

    clear_vault_owner_cache()
    cold = _verdicts()

    def _explode(_wanted):
        raise AssertionError("the warm pass must not reach the database")

    with patch.object(trace_visibility, "_resolve_vault_owners_uncached", _explode):
        warm = _verdicts()

    assert warm == cold
    assert any(cold), "sanity: some caller must be able to read something"
    assert not all(cold), "sanity: some caller must be denied, or this proves nothing"


async def test_a_private_legacy_row_stays_private_on_the_warmed_listing():
    """The verdict that matters, at the route: an anonymous caller sees the
    house trace and never the user's, on the cold request and every warm one
    after it."""
    for vault in USER_VAULTS:
        _seed_owner(vault)
    rows = [*_fifty_rows(), _trace(HOUSE_VAULT, 99)]
    rows[-1]["reasoning"] = "house demo reasoning"

    with _store(rows), _anonymous():
        for _ in range(3):
            resp = await _get("/api/traces/", limit=100)
            assert [t["id"] for t in resp.json()["traces"]] == ["trace-99"]
            assert SECRET_REASONING not in resp.text


def test_a_failed_lookup_is_never_negative_cached():
    """ "The database did not answer" is not "this vault has no owner".

    Caching an outage as a confirmed absence would let the outage outlive
    itself by a full TTL — and an unowned legacy row falls to the house-public
    floor, so that is the one direction in which a cache could widen the gate.
    """
    _seed_owner(USER_VAULTS[0])

    def _down(_wanted):
        raise TraceOwnerLookupIncomplete({})

    with patch.object(trace_visibility, "_resolve_vault_owners_uncached", _down):
        assert safe_resolve_vault_owners({USER_VAULTS[0]}) == {}

    # DB back: the owner must be found, i.e. the outage was not memoized.
    assert safe_resolve_vault_owners({USER_VAULTS[0]}) == {
        USER_VAULTS[0].lower(): (OWNER_USER_ID, OWNER_WALLET.lower())
    }


def test_becoming_owned_invalidates_the_memo():
    """The only staleness direction that could widen the gate — a vault that
    looked unowned and then acquires an owner — is closed at the write, not
    left to the TTL. ``POST /api/vaults/create`` and ``POST
    /api/vaults/metadata`` both call ``invalidate_vault_owner``.
    """
    vault = USER_VAULTS[0]
    assert safe_resolve_vault_owners({vault}) == {}  # negative, and now memoized

    _seed_owner(vault)
    assert safe_resolve_vault_owners({vault}) == {}, "sanity: the memo is real, or the next line proves nothing"

    invalidate_vault_owner(vault.upper())
    assert safe_resolve_vault_owners({vault}) == {vault.lower(): (OWNER_USER_ID, OWNER_WALLET.lower())}


def test_the_memo_expires(monkeypatch):
    """The TTL is a real bound, not a decorative constant."""
    monkeypatch.setattr(trace_visibility, "VAULT_OWNER_CACHE_TTL_SECONDS", 0)
    vault = USER_VAULTS[0]

    with _count_db_lookups() as calls:
        safe_resolve_vault_owners({vault})
        safe_resolve_vault_owners({vault})

    assert len(calls) == 2, "an entry written with a zero TTL was still served"


def test_the_cache_is_bounded(monkeypatch):
    """A pathological caller cannot grow the memo without limit."""
    monkeypatch.setattr(trace_visibility, "VAULT_OWNER_CACHE_MAX_ENTRIES", 8)
    for i in range(40):
        safe_resolve_vault_owners({f"0x{i:040x}"})

    assert len(trace_visibility._vault_owner_cache) <= 8


# ── The write-path stamp is never served from the memo ──────────────────────


async def test_save_trace_resolves_ownership_uncached():
    """``save_trace``'s stamp is PERMANENT, so it must never be answered from a
    memo that predates the vault's owner.

    The write path deliberately calls the uncached ``resolve_vault_owners``:
    a stale negative here would not expire in 300s, it would be written into
    the record and make that trace house-public forever.
    """
    from unittest.mock import MagicMock

    from archimedes.services.redis_state import AgentStateStore

    vault = USER_VAULTS[0]
    assert safe_resolve_vault_owners({vault}) == {}  # poison the memo with a negative
    _seed_owner(vault)

    store = AgentStateStore(url="redis://fake/")
    fake = MagicMock()
    fake.get = AsyncMock(return_value=None)
    fake.set = AsyncMock()
    fake.zadd = AsyncMock()
    fake.srem = AsyncMock()
    fake.hdel = AsyncMock()
    store._redis = fake

    await store.save_trace(_trace(vault, 0))

    import json

    written = next(
        json.loads(call.args[1])
        for call in fake.set.await_args_list
        if not call.args[0].startswith("archimedes:trace:id:")
    )
    assert written["owner_user_id"] == OWNER_USER_ID, "the permanent stamp was served from the read-path memo"
