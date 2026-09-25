"""Traces become reachable per strategy, and readable in full — without
becoming an existence oracle for private strategies.

Six claims are under test, each one a place where a plausible-looking
implementation would have lied:

1. **``?strategy_id=`` is gated exactly like the strategy itself.** Traces are
   public product material — the hashes are already anchored in a public
   registry on a public chain — but *"how many traces reference id X"* is a
   statement about X, and generated strategies are private-until-published
   (#850). An ungated scoped listing turns a 404-on-detail strategy into a
   discoverable one by counting rows. So the scope runs the same
   ``assert_strategy_visible`` gate ``GET /api/strategies/{id}`` runs — the
   card tier, because existence is card content — and returns **404, never
   403** (a 403 confirms the id exists). ``/debate`` and ``/returns`` sit one
   tier tighter on ``is_strategy_reasoning_visible`` (#1557); this scope
   deliberately does not.

2. **The on-chain fallback must not answer a strategy-scoped question.** The
   registry entry is ``(agent, vault, hash, timestamp)`` and records no
   strategy reference whatsoever. If Redis is empty or unreachable and the
   route fell through to the on-chain listing, every returned row would be a
   false positive under a filter the caller explicitly asked for.

3. **The detail route returns the hashed body, not just its summary.** Four of
   the thirteen ``_HASH_FIELDS`` (``market_context``, ``portfolio_before``,
   ``portfolio_after``, ``consulted_paper_hashes``) were reachable only via
   ``/canonical``'s raw bytes. The anchored claim is worth nothing to a user
   who cannot read what was anchored.

4. **An empty ``?strategy_id=`` cannot walk around the gate.** A blank value is
   falsy, so a truthiness check skipped the visibility gate *and* the filter and
   returned the whole platform feed under a filter the caller had asked for.

5. **``total`` describes rows a page can actually contain.** The ``empty_vault``
   drop and the #1556 per-row filter both run before the count, so "3 of 5"
   never promises a row no offset will reach.

6. **The match is exact, and scoped to traces that name strategies at all.**
   ``strategies_referenced`` holds real strategy ids only on the agent's
   DECISION traces; the two construction writers put arXiv ids and paper anchors
   in the same field. Inside that scope the match is element-exact — a bare
   string is not substring-matched and a dict is not key-matched.

The ownership half of the trace gate (#1556 — who may read a row at all) is
proved in ``tests/api/test_traces_ownership_gate.py``, including the guard on
the widened detail body this branch introduced. It is deliberately NOT
re-implemented here.

Hermetic: ``AgentStateStore`` is mocked at the boundary (same shape as
``test_traces_display_anchored_only.py``) and the DB is a per-test tmp sqlite
(same ``_use_tmp_db`` shape as ``test_strategy_ownership.py``). No Redis, no
chain, no ``.env``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import archimedes.db as db
import pytest
from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.anyio

_W_OWNER = "0xAbC0000000000000000000000000000000000011"
_W_OTHER = "0x0000000000000000000000000000000000000012"

_SID = "strat-scoped-1"
_OTHER_SID = "strat-scoped-2"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _unset_trace_allowlist(monkeypatch):
    """Weakest #1556 floor, so these tests measure the STRATEGY scope only.

    With `PUBLIC_TRACE_VAULTS` unset every unowned trace is house-public, so a
    row missing from a scoped listing here is missing because of the strategy
    filter — not because an allowlist happened to be armed in the environment.
    The ownership half of the story is proved in
    `tests/api/test_traces_ownership_gate.py`, against the same weakest floor.
    """
    monkeypatch.delenv("PUBLIC_TRACE_VAULTS", raising=False)
    monkeypatch.delenv("AGENT_VAULT_ADDRESSES", raising=False)


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Fresh per-test sqlite; rebinds db.engine + db.SessionLocal (they are
    built once at import, so setenv alone does not re-point them)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'traces_scope.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()
    yield


def _siwe_cookies(wallet: str) -> dict[str, str]:
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


def _mk_strategy(sid: str, *, owner: str | None = None, published: bool = False, example: bool = False):
    from archimedes.models.strategy_store import StrategyRecord

    with db.get_session() as session:
        session.add(
            StrategyRecord(
                id=sid,
                content_hash=("0x" + sid).ljust(66, "0"),
                generation_method="fusion",
                source_papers="[]",
                strategy_name="Scoped Trace Strategy",
                thesis="test thesis",
                asset_universe="[]",
                risk_profile="moderate",
                status="candidate",
                is_example=example,
                owner_wallet=owner.lower() if owner else None,
                is_published=published,
            )
        )
        session.commit()


def _trace(tid: str, *, strategies: list[str], decision_type: str = "rebalance", **extra) -> dict:
    """One persisted off-chain trace dict, shaped like agent_runner writes it."""
    base = {
        "id": tid,
        "vault_address": "0xVault",
        "decision_type": decision_type,
        "trigger": "scheduled",
        "timestamp": "2026-08-30T12:00:00+00:00",
        "market_context": {"regime": "bull", "vix": 14.2},
        "portfolio_before": {"USDC": 1000.0},
        "portfolio_after": {"USDC": 400.0, "WETH": 0.2},
        "reasoning": "Momentum signal above threshold.",
        "confidence": 0.7,
        "trades_executed": [],
        "strategies_referenced": strategies,
        "consulted_paper_hashes": ["2301.00001:abc"],
        "settlement_tx_hashes": ["0xsettle"],
        "trace_hash": "0x" + tid.encode().hex().ljust(64, "0")[:64],
        "arc_tx_hash": "0xanchor",
        "is_verified": True,
        "ipfs_cid": "bafyfake",
    }
    base.update(extra)
    return base


async def _get(path: str, *, list_traces=None, get_trace=None, cookies=None, on_chain_count: int = 0):
    """Call the API with AgentStateStore + trace_publisher mocked at the boundary."""
    from archimedes.main import app
    from archimedes.services.redis_state import AgentStateStore

    list_mock = list_traces if list_traces is not None else AsyncMock(return_value=([], 0))
    detail_mock = get_trace if get_trace is not None else AsyncMock(return_value=None)

    with (
        patch.object(AgentStateStore, "list_traces", list_mock),
        patch.object(AgentStateStore, "get_trace", detail_mock),
        patch.object(AgentStateStore, "close", AsyncMock()),
        patch("archimedes.chain.trace_publisher.trace_publisher") as pub,
    ):
        pub.get_total_trace_count = AsyncMock(return_value=on_chain_count)
        pub.get_trace_by_id = AsyncMock(
            return_value={
                "vault": "0xVault",
                "trace_hash": "0xdeadbeef",
                "timestamp": 1_700_000_000,
                "agent": "0xAgent",
            }
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path, cookies=cookies or {})


# ── 1. The scoped listing is gated exactly like the strategy ────────────────


async def test_scoped_listing_404s_for_a_private_strategy_and_never_403s():
    """A non-owner must not be able to tell a private strategy exists by asking
    for its traces. 404 is the whole point: a 403 would confirm the id."""
    _mk_strategy(_SID, owner=_W_OWNER, published=False)

    # A store that WOULD happily return a trace, so a missing gate shows up as
    # a 200 with data rather than as an incidental empty list.
    store = AsyncMock(return_value=([_trace("t1", strategies=[_SID])], 1))

    anon = await _get(f"/api/traces/?strategy_id={_SID}", list_traces=store)
    other = await _get(f"/api/traces/?strategy_id={_SID}", list_traces=store, cookies=_siwe_cookies(_W_OTHER))

    assert anon.status_code == 404, anon.text
    assert other.status_code == 404, other.text
    # Never a 403 — that would be an existence oracle with extra steps.
    assert anon.json()["detail"] == "Strategy not found"


async def test_scoped_listing_serves_the_owner_and_the_published_case():
    _mk_strategy(_SID, owner=_W_OWNER, published=False)
    _mk_strategy(_OTHER_SID, owner=_W_OWNER, published=True)

    store = AsyncMock(return_value=([_trace("t1", strategies=[_SID])], 1))

    owner = await _get(f"/api/traces/?strategy_id={_SID}", list_traces=store, cookies=_siwe_cookies(_W_OWNER))
    assert owner.status_code == 200, owner.text
    assert [t["id"] for t in owner.json()["traces"]] == ["t1"]

    public = await _get(f"/api/traces/?strategy_id={_OTHER_SID}", list_traces=store)
    assert public.status_code == 200, public.text


async def test_unscoped_listing_stays_public():
    """No strategy_id, no gate: the unfiltered feed is public product material
    (every hash is already readable from the on-chain registry)."""
    store = AsyncMock(return_value=([_trace("t1", strategies=[_SID])], 1))
    resp = await _get("/api/traces/?limit=10", list_traces=store)
    assert resp.status_code == 200, resp.text
    assert resp.json()["total"] == 1


async def test_strategy_id_reaches_the_store_as_a_filter_not_a_post_filter():
    """`total` must describe the FILTERED universe, so the filter has to be
    applied before windowing — i.e. passed down to the store, not applied to a
    page the store already cut."""
    seen = {}

    async def _list(self=None, **kwargs):
        seen.update(kwargs)
        return ([_trace("t1", strategies=[_SID])], 1)

    _mk_strategy(_SID, published=True)
    resp = await _get(f"/api/traces/?strategy_id={_SID}", list_traces=_list)

    assert resp.status_code == 200, resp.text
    assert seen.get("strategy_id") == _SID


# ── 2. The on-chain fallback cannot answer a strategy-scoped question ───────


async def test_scoped_listing_does_not_fall_through_to_the_unfiltered_registry():
    """Registry entries are (agent, vault, hash, timestamp) — no strategy field
    exists on-chain. Falling through would return the whole registry under the
    caller's filter, making every row a false positive."""
    _mk_strategy(_SID, published=True)

    for store in (
        AsyncMock(return_value=([], 0)),  # store reachable, nothing matched
        AsyncMock(side_effect=ConnectionError("redis down")),  # store unreachable
    ):
        resp = await _get(f"/api/traces/?strategy_id={_SID}", list_traces=store, on_chain_count=5)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["traces"] == []
        assert body["total"] == 0, "an unfilterable registry must report nothing, not everything"


async def test_unscoped_listing_still_uses_the_on_chain_fallback():
    """The guard above must not have disabled the fallback in general — the
    anchored_only projection is the only thing standing between a Redis outage
    and an empty provenance page."""
    resp = await _get("/api/traces/?limit=10", list_traces=AsyncMock(return_value=([], 0)), on_chain_count=2)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert body["traces"], "on-chain fallback returned nothing for an unscoped listing"
    assert body["traces"][0]["verification_mode"] == "anchored_only"


# ── 3. The detail route returns the hashed body ─────────────────────────────


async def test_detail_returns_the_rest_of_the_hashed_body():
    """market_context / portfolio_before / portfolio_after /
    consulted_paper_hashes are four of the thirteen _HASH_FIELDS. Before this
    they were reachable only as raw bytes from /canonical."""
    resp = await _get("/api/traces/t1", get_trace=AsyncMock(return_value=_trace("t1", strategies=[_SID])))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["market_context"] == {"regime": "bull", "vix": 14.2}
    assert body["portfolio_before"] == {"USDC": 1000.0}
    assert body["portfolio_after"] == {"USDC": 400.0, "WETH": 0.2}
    assert body["consulted_paper_hashes"] == ["2301.00001:abc"]
    assert body["settlement_tx_hashes"] == ["0xsettle"]
    assert body["ipfs_cid"] == "bafyfake"
    # The list-row fields are still there — the detail model widens, not replaces.
    assert body["reasoning"] == "Momentum signal above threshold."
    assert body["strategies_referenced"] == [_SID]
    assert body["regime_at_decision"] == "bull"


async def test_detail_on_chain_only_leaves_the_body_empty_and_says_so():
    """The registry stores no body. Empty defaults are the honest rendering of
    that, and verification_mode is what tells the reader why."""
    resp = await _get("/api/traces/7", get_trace=AsyncMock(return_value=None))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verification_mode"] == "anchored_only"
    assert body["market_context"] == {}
    assert body["portfolio_before"] == {}
    assert body["portfolio_after"] == {}
    assert body["consulted_paper_hashes"] == []


async def test_detail_still_404s_for_an_unknown_id():
    from archimedes.main import app
    from archimedes.services.redis_state import AgentStateStore

    with (
        patch.object(AgentStateStore, "get_trace", AsyncMock(return_value=None)),
        patch.object(AgentStateStore, "close", AsyncMock()),
        patch("archimedes.chain.trace_publisher.trace_publisher") as pub,
    ):
        pub.get_trace_by_id = AsyncMock(return_value=None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/traces/not-a-real-id")).status_code == 404
            assert (await client.get("/api/traces/999")).status_code == 404


# ── 4. The store-level filter itself ───────────────────────────────────────
#
# The route tests above mock `AgentStateStore.list_traces`, so they pin the
# wiring but say nothing about the filter's own behaviour. These drive the real
# method against a fake redis client (same `store._get_redis = AsyncMock(...)`
# shape as test_visitor_insights.py).


def _fake_redis(traces: list[dict]):
    """Minimal redis double: a newest-first index plus JSON bodies."""
    import json

    from archimedes.services.redis_state import KEY_TRACE_PREFIX

    bodies = {f"{KEY_TRACE_PREFIX}{t['trace_hash']}": json.dumps(t) for t in traces}
    order = [t["trace_hash"] for t in traces]

    class _R:
        async def zrevrange(self, key, start, end):
            return order

        async def get(self, key):
            return bodies.get(key)

        async def mget(self, keys):
            # `list_traces` batches the body reads (#1577). Mirrors the real
            # contract: one slot per requested key, in request order, `None`
            # where the key is absent. `get` stays for the single-key readers.
            return [bodies.get(k) for k in keys]

    return _R()


async def test_store_filters_on_strategies_referenced_and_totals_the_filtered_set():
    from archimedes.services.redis_state import AgentStateStore

    rows = [
        _trace("t1", strategies=[_SID, "other"]),
        _trace("t2", strategies=["unrelated"]),
        _trace("t3", strategies=[_SID]),
    ]
    store = AgentStateStore()
    store._get_redis = AsyncMock(return_value=_fake_redis(rows))

    window, total = await store.list_traces(strategy_id=_SID)

    assert [t["id"] for t in window] == ["t1", "t3"]
    # `total` must count the FILTERED universe. If the filter ran after
    # windowing, this would be 3 — a paginator promising rows that do not match.
    assert total == 2


async def test_store_filter_is_applied_before_windowing():
    """With limit=1, a post-window filter would return an empty page (t2 cut,
    then filtered away) while still reporting a total. Applied first, page 1 is
    t1 and page 2 is t3."""
    from archimedes.services.redis_state import AgentStateStore

    rows = [
        _trace("t1", strategies=[_SID]),
        _trace("t2", strategies=["unrelated"]),
        _trace("t3", strategies=[_SID]),
    ]
    store = AgentStateStore()
    store._get_redis = AsyncMock(return_value=_fake_redis(rows))

    page1, total1 = await store.list_traces(strategy_id=_SID, limit=1, offset=0)
    page2, total2 = await store.list_traces(strategy_id=_SID, limit=1, offset=1)

    assert [t["id"] for t in page1] == ["t1"]
    assert [t["id"] for t in page2] == ["t3"]
    assert total1 == total2 == 2


async def test_store_strategy_filter_needs_an_exact_id_match():
    """A substring match would attribute a decision to a strategy it never
    consulted — a provenance claim, not a search convenience."""
    from archimedes.services.redis_state import AgentStateStore

    rows = [_trace("t1", strategies=[f"{_SID}-suffix"]), _trace("t2", strategies=["prefix-" + _SID])]
    store = AgentStateStore()
    store._get_redis = AsyncMock(return_value=_fake_redis(rows))

    window, total = await store.list_traces(strategy_id=_SID)
    assert window == []
    assert total == 0


async def test_store_without_strategy_id_is_unchanged():
    from archimedes.services.redis_state import AgentStateStore

    rows = [_trace("t1", strategies=[_SID]), _trace("t2", strategies=["unrelated"])]
    store = AgentStateStore()
    store._get_redis = AsyncMock(return_value=_fake_redis(rows))

    window, total = await store.list_traces()
    assert [t["id"] for t in window] == ["t1", "t2"]
    assert total == 2


async def test_detail_cannot_smuggle_a_temporal_binding_claim_through_the_wider_model():
    """TraceResponse's claim-integrity validator (#714) coerces
    temporal_binding_valid to None unless source == "chain". Widening to
    TraceDetailResponse must not reopen that hole."""
    resp = await _get(
        "/api/traces/t1",
        get_trace=AsyncMock(
            return_value=_trace(
                "t1",
                strategies=[_SID],
                temporal_binding_valid=True,
                temporal_binding_source="none",
            )
        ),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["temporal_binding_valid"] is None


# ── 5. An empty ?strategy_id= must not bypass the gate ──────────────────────


async def test_empty_strategy_id_is_rejected_not_silently_unscoped():
    """`?strategy_id=` — present but empty — must never serve the whole feed.

    The bypass: an empty string is falsy, so a `if strategy_id:` gate skipped
    `assert_strategy_visible` AND skipped passing the filter down, and the
    caller got the platform's entire unfiltered trace feed back under a filter
    they had explicitly asked for. A gate that a blank value walks around is
    not a gate.

    Two independent defences, so relaxing either alone still refuses: FastAPI's
    `min_length=1` rejects the value before the handler runs, and the handler
    branches on `is not None` rather than on truthiness.
    """
    _mk_strategy(_SID, owner=_W_OWNER, published=False)
    store = AsyncMock(return_value=([_trace("t1", strategies=[_SID])], 1))

    resp = await _get(f"/api/traces/?strategy_id=&limit=100&_={_SID}", list_traces=store)

    assert resp.status_code == 422, resp.text
    assert "t1" not in resp.text, "the unfiltered feed came back under an empty filter"


async def test_whitespace_strategy_id_is_gated_like_any_other_id():
    """A value that clears `min_length=1` still meets the visibility gate.

    `?strategy_id=%20` is a real string, so it reaches the handler — and it is
    not a strategy anyone may read, so it is 404, exactly like a made-up id.
    """
    store = AsyncMock(return_value=([_trace("t1", strategies=[_SID])], 1))

    resp = await _get("/api/traces/?strategy_id=%20&limit=100", list_traces=store)

    assert resp.status_code == 404, resp.text
    assert "t1" not in resp.text


async def test_unknown_strategy_id_is_404_not_an_empty_200():
    """Same answer for "no such strategy" as for "not yours" — otherwise the
    difference between the two responses is the existence oracle."""
    resp = await _get(
        "/api/traces/?strategy_id=no-such-strategy",
        list_traces=AsyncMock(return_value=([], 0)),
    )
    assert resp.status_code == 404, resp.text


# ── 6. `total` describes rows the caller can actually reach ────────────────


async def test_total_counts_only_rows_that_survive_every_filter():
    """ "3 of 5" must not include rows the response can never show.

    `empty_vault` traces are dropped by the route (an agent tick against a vault
    with nothing in it is not a decision worth showing), and #1556 drops rows
    the caller may not read. Counting before those drops and windowing after
    them yields a total that promises rows no page will ever contain.
    """
    _mk_strategy(_SID, published=True)
    rows = [
        _trace("t1", strategies=[_SID]),
        _trace("skip-me-1", strategies=[_SID], trigger="empty_vault"),
        _trace("t2", strategies=[_SID]),
        _trace("skip-me-2", strategies=[_SID], trigger="empty_vault"),
        _trace("t3", strategies=[_SID]),
    ]
    # The store reports the pre-drop count, exactly as the real one does.
    store = AsyncMock(return_value=(rows, len(rows)))

    resp = await _get(f"/api/traces/?strategy_id={_SID}&limit=100", list_traces=store)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["id"] for t in body["traces"]] == ["t1", "t2", "t3"]
    assert body["total"] == 3, "total counted rows the route filtered out of every page"


async def test_every_row_the_total_promises_is_reachable_by_paging():
    """The honest form of the claim: paging through `total` yields `total` rows.

    A count taken before a post-window filter fails this even when the first
    page looks right — page 2 comes back short and the reader never learns why.
    """
    _mk_strategy(_SID, published=True)
    rows = [
        _trace("t1", strategies=[_SID]),
        _trace("skip-me", strategies=[_SID], trigger="empty_vault"),
        _trace("t2", strategies=[_SID]),
    ]
    store = AsyncMock(return_value=(rows, len(rows)))

    first = await _get(f"/api/traces/?strategy_id={_SID}&limit=1&offset=0", list_traces=store)
    second = await _get(f"/api/traces/?strategy_id={_SID}&limit=1&offset=1", list_traces=store)

    total = first.json()["total"]
    assert total == 2
    seen = [t["id"] for t in first.json()["traces"]] + [t["id"] for t in second.json()["traces"]]
    assert seen == ["t1", "t2"]
    assert len(seen) == total


# ── 7. What `strategies_referenced` actually matches ───────────────────────
#
# The field is named for strategy ids and does not uniformly hold them:
# chain/agent_runner.py writes real strategy ids on its decision traces, while
# the two CONSTRUCTION writers in api/strategies_routes.py write paper anchors
# and arXiv ids into the same field. These pin the two halves of the honest
# answer — the scope, and the exactness of the match inside it.


def test_construction_traces_are_out_of_scope_because_they_name_papers():
    """A construction trace's `strategies_referenced` is arXiv ids / paper
    anchors, so matching a strategy id against it is a category error. Excluded
    explicitly rather than left to fail silently, which reads as "this strategy
    has no construction trace" instead of "this filter cannot see one"."""
    from archimedes.services.redis_state import trace_references_strategy

    fusion = _trace("f1", strategies=["2301.00001", "2405.09876"], decision_type="construction")
    anchored = _trace("c1", strategies=["arxiv:2301.00001#momentum"], decision_type="construction")

    # Not even its own literal contents match, because the scope is the gate.
    assert trace_references_strategy(fusion, "2301.00001") is False
    assert trace_references_strategy(anchored, "arxiv:2301.00001#momentum") is False
    assert trace_references_strategy(fusion, _SID) is False


def test_every_agent_decision_type_is_in_scope():
    """The runner's four decision types all write real strategy ids, and all
    four belong on a strategy's passport — a skip is a decision about holding
    it, and dropping skips would quietly present a filtered history."""
    from archimedes.services.redis_state import STRATEGY_REFERENCE_DECISION_TYPES, trace_references_strategy

    assert {"rebalance", "rotation", "regime_change", "skip"} == STRATEGY_REFERENCE_DECISION_TYPES
    for dt in STRATEGY_REFERENCE_DECISION_TYPES:
        assert trace_references_strategy(_trace("t", strategies=[_SID], decision_type=dt), _SID) is True


def test_a_bare_string_matches_the_whole_string_never_a_substring():
    """`strategy_id in refs` on a string is a SUBSTRING test. "alpha" would be
    attributed every decision that consulted "alpha-momentum-v2"."""
    from archimedes.services.redis_state import trace_references_strategy

    row = _trace("t1", strategies=[])
    row["strategies_referenced"] = f"{_SID}-momentum-v2"

    assert trace_references_strategy(row, _SID) is False
    row["strategies_referenced"] = _SID
    assert trace_references_strategy(row, _SID) is True


def test_a_dict_shape_records_no_references_rather_than_matching_its_keys():
    """`strategy_id in refs` on a dict is a KEY test. No writer produces a dict
    here, so key-matching would silently promote whatever a future writer keyed
    a mapping by into a provenance claim."""
    from archimedes.services.redis_state import trace_references_strategy

    row = _trace("t1", strategies=[])
    row["strategies_referenced"] = {_SID: {"weight": 0.4}}

    assert trace_references_strategy(row, _SID) is False


@pytest.mark.parametrize(
    "refs",
    [None, 7, [None], [7], [{"id": _SID}], [[_SID]], ()],
    ids=["none", "int", "list-of-none", "list-of-int", "list-of-dict", "nested-list", "empty-tuple"],
)
def test_unrecognised_shapes_match_nothing(refs):
    """ "I cannot establish that this decision consulted that strategy" is the
    honest answer, and the safe one: a false positive puts someone else's trade
    on a strategy's passport."""
    from archimedes.services.redis_state import trace_references_strategy

    row = _trace("t1", strategies=[])
    row["strategies_referenced"] = refs
    assert trace_references_strategy(row, _SID) is False


def test_a_tuple_or_set_of_ids_still_matches_exactly():
    """JSON round-trips lists, but the predicate is also called on in-memory
    records; accepting the sequence types while keeping element equality exact
    is the point."""
    from archimedes.services.redis_state import trace_references_strategy

    for refs in ([_SID, "other"], (_SID,), {_SID, "other"}, frozenset({_SID})):
        row = _trace("t1", strategies=[])
        row["strategies_referenced"] = refs
        assert trace_references_strategy(row, _SID) is True

    row = _trace("t1", strategies=[])
    row["strategies_referenced"] = (f"{_SID}x",)
    assert trace_references_strategy(row, _SID) is False


async def test_store_filter_skips_construction_traces_end_to_end():
    """The scope is enforced by the store, not only by the pure predicate."""
    from archimedes.services.redis_state import AgentStateStore

    rows = [
        _trace("decision", strategies=[_SID]),
        _trace("construction", strategies=[_SID], decision_type="construction"),
    ]
    store = AgentStateStore()
    store._get_redis = AsyncMock(return_value=_fake_redis(rows))

    window, total = await store.list_traces(strategy_id=_SID)

    assert [t["id"] for t in window] == ["decision"]
    assert total == 1
