"""Paper decisions produce real, owner-gated, verifiable traces — issue #1575.

This is the issue's named acceptance file. Before this change a user's paper
deployment moved (paper) money and left no hashed, owner-stamped, verifiable
record of *why* — the product's central claim failing on the surface users
actually touch.

Every guard here is paired with the input that SHOULD fail it, per CLAUDE.md
§ "A guard must be shown to reject something":

  G1  publishing disabled is LOUD          + the coverage rule that would hide it
  G2  a tampered trace does not re-derive  + a NON-hashed field that must not move it
  G3  non-owner reads are blocked          + the sentinel vault that leaks without the gate
  G4  idempotency across three advances    + duplicate keys when the key is ignored
  G5  #1569 reachability conformance       + "paper_rebalance" rejected by the live regex
  G6  the ledger keeps advancing on failure, and the gap is durable
  G7  the coverage identity RAISES         + a miscount that must trip it

Hermetic: tmp-file SQLite for the identity/paper tables, the trace store mocked
at the ``AgentStateStore`` boundary. No Redis, no Postgres, no network, no
``.env``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

# Import for the side effect: `redirect_to_tmp_sqlite` runs `create_all` over
# whatever is registered on `Base.metadata` at fixture time, so a model this
# file only imports lazily inside a helper would have no table on the first
# test in the process — import-order roulette, and the exact class of flake
# `db_isolation` exists to remove.
from archimedes.models import paper_store  # noqa: F401  (table registration)
from httpx import ASGITransport, AsyncClient

from tests.db_isolation import redirect_to_tmp_sqlite

pytestmark = pytest.mark.anyio

OWNER_USER_ID = "user-owner-1575"
OTHER_USER_ID = "user-other-1575"
OWNER_WALLET = "0x1111111111111111111111111111111111111111"
OTHER_WALLET = "0x2222222222222222222222222222222222222222"

STRATEGY_ID = "aa11bb22cc33dd44"
DEPLOY = date(2026, 8, 3)
DECIDED = date(2026, 8, 4)
FILLED = date(2026, 8, 5)

#: Distinctive strings that must never reach an unauthorised reader. Asserting
#: on CONTENT, not just a status code, is what makes G3 about the leak rather
#: than about a number.
SECRET_SYMBOL = "SECRETSPY1575"

#: The deployment's SECOND sleeve. It never trades, and that is the point: a
#: 2-sleeve deployment holds 2x the sleeve capital, and the portfolio snapshot
#: inside the hash has to say so. The per-leg snapshot this replaced reported
#: only the traded sleeve's cash and omitted this symbol from `holdings`
#: entirely.
QUIET_SYMBOL = "QUIETIWM1575"

#: What one sleeve opens with — `fusion_evaluator._DEFAULT_CASH`, read rather
#: than re-typed so the fixture cannot drift from the engine.
SLEEVE_CASH = 100_000.0

_SPEC = {
    "name": "paper trace probe",
    "asset_universe": [SECRET_SYMBOL, QUIET_SYMBOL],
    "rebalance_frequency": "monthly",
    "entry": {"gt": ["close", "sma_200"]},
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497"],
    "look_ahead_safe": True,
    "indicators": ["sma_200"],
}

_RETURNS = {DECIDED: 0.01, FILLED: -0.004, date(2026, 8, 6): 0.002}

_LEG = {
    "symbol": SECRET_SYMBOL,
    "decided_on": DECIDED,
    "filled_on": FILLED,
    "side": "buy",
    "size": 182.0,
    "price": 548.21,
    "value": 182.0 * 548.21,
    "commission": 99.77,
    "cash_after": 407.06,
    "cash_before": 407.06 + 182.0 * 548.21 + 99.77,
    "position_after": 182.0,
    "position_before": 0.0,
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture(autouse=True)
def _weakest_floor(monkeypatch):
    """Run against the WEAKEST configuration of the visibility floor.

    With ``PUBLIC_TRACE_VAULTS`` unset an unowned trace defaults to
    house-public, so anything proved hidden here is hidden by *ownership*, not
    by an allowlist that happened to be armed. Also pins publishing ON so a
    developer's ``.env`` cannot silently turn the acceptance tests into
    no-ops.
    """
    monkeypatch.delenv("PUBLIC_TRACE_VAULTS", raising=False)
    monkeypatch.delenv("AGENT_VAULT_ADDRESSES", raising=False)
    monkeypatch.setenv("PAPER_TRACE_PUBLISH", "true")
    monkeypatch.setenv("PAPER_TRACE_ANCHOR", "false")
    monkeypatch.delenv("PAPER_TRACE_BACKFILL_MAX", raising=False)


def _replay(spec_dict, deployed_at):
    return {d: r for d, r in _RETURNS.items() if d >= deployed_at}


def _portfolio(side: str, legs=None) -> dict:
    """The deployment-scoped snapshot, built the way the settle path builds it.

    Both sleeves, whether or not they traded — which is what makes the numbers
    the tests below assert on the DEPLOYMENT's numbers rather than one sleeve's.
    """
    from archimedes.services.paper_trace import deployment_portfolio

    return deployment_portfolio(
        decision_date=DECIDED,
        side=side,
        sleeve_legs={SECRET_SYMBOL: [dict(leg) for leg in (legs or [_LEG])], QUIET_SYMBOL: []},
        sleeve_initial_cash=SLEEVE_CASH,
        sleeve_closes={QUIET_SYMBOL: {DECIDED: 250.0}},
    )


def _decision(legs=None) -> dict:
    legs = [dict(leg) for leg in (legs or [_LEG])]
    return {
        "legs": legs,
        "portfolio_before": _portfolio("before", legs),
        "portfolio_after": _portfolio("after", legs),
    }


def _decisions(spec_dict, deployed_at):
    return {DECIDED: _decision()}


class _Store:
    """A stand-in for the Redis trace store, at the AgentStateStore boundary.

    Records exactly what the production writer hands to ``save_trace``, so the
    ownership stamp under test is the real one rather than something this
    fixture invented.
    """

    def __init__(self, fail: bool = False):
        self.records: dict[str, dict] = {}
        self.fail = fail
        #: How many times the writer CALLED save_trace. Counting distinct ids
        #: instead would be tautological: the trace id is derived from the
        #: decision key, so a writer that republishes on every settle still
        #: yields exactly one id. Republishing is the thing under test.
        self.saves = 0

    async def save(self, payload: dict) -> None:
        self.saves += 1
        if self.fail:
            raise ConnectionError("redis down")
        self.records[payload["trace_hash"]] = payload
        self.records[payload["id"]] = payload

    async def get(self, key: str):
        return self.records.get(key)

    async def list(self, vault_address=None, decision_type=None, strategy_id=None, limit=20, offset=0):
        # `strategy_id` mirrors the parameter #1569 added to the real
        # AgentStateStore.list_traces. The route's call sits inside an
        # `except Exception` that reads a TypeError as "Redis unavailable"
        # and silently serves the empty on-chain fallback — a fake whose
        # signature drifts makes every test downstream assert on a page the
        # gate never produced (the union failure mode from #1573/#1569).
        # Delegates the provenance rule rather than re-implementing it.
        from archimedes.services.redis_state import trace_references_strategy

        rows = list({id(v): v for v in self.records.values()}.values())
        if decision_type:
            rows = [t for t in rows if t.get("decision_type") == decision_type]
        if strategy_id:
            rows = [t for t in rows if trace_references_strategy(t, strategy_id)]
        return rows[offset : offset + limit], len(rows)


@contextmanager
def _store(store: _Store):
    from archimedes.services.redis_state import AgentStateStore

    with (
        patch.object(AgentStateStore, "save_trace", AsyncMock(side_effect=store.save)),
        patch.object(AgentStateStore, "get_trace", AsyncMock(side_effect=store.get)),
        patch.object(AgentStateStore, "list_traces", AsyncMock(side_effect=store.list)),
        patch.object(AgentStateStore, "close", AsyncMock()),
    ):
        yield store


@contextmanager
def _as(user_id: str | None, wallet: str | None = None):
    from archimedes.api.account_auth import CurrentUser

    user = (
        None
        if user_id is None
        else CurrentUser(id=user_id, name=user_id, email=f"{user_id}@example.test", email_verified=True)
    )
    with (
        patch("archimedes.api.account_auth.get_current_user", return_value=user),
        patch("archimedes.api.auth_siwe.get_verified_wallet", return_value=wallet),
    ):
        yield


async def _get(path: str, **params):
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, params=params)


def _seed(*, owner_user_id: str | None = OWNER_USER_ID, owner_wallet: str | None = OWNER_WALLET):
    """One paper deployment plus the identity rows the read gate consults."""
    from datetime import UTC, datetime

    from archimedes.db import get_session
    from archimedes.models.account import AuthUser
    from archimedes.models.identity import WalletIdentity
    from archimedes.models.paper_store import PaperDeployment

    now = datetime.now(UTC)
    with get_session() as session:
        for uid, email in ((OWNER_USER_ID, "owner@example.test"), (OTHER_USER_ID, "other@example.test")):
            session.merge(AuthUser(id=uid, name=uid, email=email, email_verified=True, created_at=now, updated_at=now))
        for wallet in (OWNER_WALLET, OTHER_WALLET):
            session.merge(WalletIdentity(wallet_address=wallet.lower(), actor_class="human", first_seen_at=now))
        dep = PaperDeployment(
            id="dep1575",
            strategy_id=STRATEGY_ID,
            owner_wallet=owner_wallet,
            owner_user_id=owner_user_id,
            spec_json=json.dumps(_SPEC, sort_keys=True),
            deployed_at=DEPLOY,
            status="active",
        )
        session.add(dep)
        session.commit()
    return "dep1575"


def _advance(deployment_id: str, *, decisions=_decisions, replay=_replay):
    """One settle advance, committed, returning the advance result dict."""
    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperDeployment
    from archimedes.services.paper_trading import advance_deployment

    with get_session() as session:
        dep = session.get(PaperDeployment, deployment_id)
        result = advance_deployment(session, dep, replay=replay, decision_replay=decisions)
        session.commit()
    return result


def _summary(deployment_id: str) -> dict:
    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperDeployment
    from archimedes.services.paper_trading import deployment_summary

    with get_session() as session:
        return deployment_summary(session, session.get(PaperDeployment, deployment_id))


def _rows(deployment_id: str) -> list:
    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperDecisionTrace

    with get_session() as session:
        return session.query(PaperDecisionTrace).filter_by(deployment_id=deployment_id).all()


# ── ACCEPTANCE: one settle → exactly one trace, and it round-trips ──────────


async def test_one_settle_advance_produces_exactly_one_trace():
    """ACCEPTANCE #1 (issue): a seeded paper deployment advancing one settle
    produces exactly one trace, keyed on (deployment, decision date) — not one
    per ledger row (a monthly strategy gets ~21 ledger rows per decision) and
    not one per symbol leg."""
    dep_id = _seed()
    with _store(_Store()) as store:
        result = _advance(dep_id)

    assert result["decisions"] == 1
    assert result["published"] == 1
    assert (result["failed"], result["unowned"], result["disabled"]) == (0, 0, 0)
    # Three ledger rows, ONE trace: tracing per ledger row would manufacture
    # fake decisions.
    assert result["appended"] == 3

    traces = {payload["id"]: payload for payload in store.records.values()}
    assert len(traces) == 1
    (trace,) = traces.values()
    assert trace["decision_type"] == "rebalance"
    assert trace["trigger"] == "paper_settle"
    assert trace["market_context"]["venue"] == "paper"
    assert trace["strategies_referenced"] == [STRATEGY_ID]
    # The ownership stamp is copied verbatim from the deployment, so the read
    # gate needs no DB round-trip and a Postgres outage cannot downgrade it.
    assert trace["owner_user_id"] == OWNER_USER_ID
    assert trace["owner_wallet"] == OWNER_WALLET
    # Never anchored by default, and it says so honestly rather than claiming
    # a registry write that was never attempted.
    assert trace["arc_tx_hash"] is None
    assert trace["is_verified"] is False

    assert [row.status for row in _rows(dep_id)] == ["published"]


async def test_the_trace_round_trips_its_hash_through_the_canonical_route():
    """ACCEPTANCE #2 (issue): the trace round-trips ``verify`` — the hash
    re-derives.

    For an UNANCHORED trace the honest verification is off-chain: fetch
    ``/canonical`` (the exact bytes the hash was taken over) and re-run the
    keccak. ``/verify`` is checked too, and must report the absence of an
    anchor rather than a green check — an unanchored paper trace reporting
    ``is_verified`` would be the fabrication this whole issue is against.
    """
    from web3 import Web3

    dep_id = _seed()
    with _store(_Store()) as store:
        _advance(dep_id)
    (trace,) = {payload["id"]: payload for payload in store.records.values()}.values()

    with _store(store), _as(OWNER_USER_ID, OWNER_WALLET):
        canonical = await _get(f"/api/traces/{trace['id']}/canonical")
        verify = await _get(f"/api/traces/{trace['id']}/verify")

    assert canonical.status_code == 200
    assert Web3.keccak(text=canonical.text).hex() == trace["trace_hash"]

    assert verify.status_code == 200
    body = verify.json()
    assert body["trace_hash"] == trace["trace_hash"]
    assert body["is_verified"] is False
    assert "not published on-chain" in body["details"]


# ── G5: reachability conformance (#1569) ───────────────────────────────────


async def test_decision_type_conforms_to_the_live_trace_filter():
    """G5. The produced ``decision_type`` must satisfy the filter that already
    exists on ``list_traces``, read out of the route rather than hard-coded —
    if the allowed set ever changes shape, this fails loudly instead of the
    paper pipeline going quietly unreachable.

    ADVERSARIAL: the value the design rejected (``"paper_rebalance"``) is shown
    to fail the same pattern. That is why conforming is not cosmetic."""
    import inspect
    import re

    from archimedes.api.traces_routes import list_traces

    decision_type_param = inspect.signature(list_traces).parameters["decision_type"]
    pattern = next(
        meta.pattern for meta in getattr(decision_type_param.default, "metadata", []) if getattr(meta, "pattern", None)
    )

    dep_id = _seed()
    with _store(_Store()) as store:
        _advance(dep_id)
    (trace,) = {payload["id"]: payload for payload in store.records.values()}.values()

    assert re.match(pattern, trace["decision_type"])
    assert not re.match(pattern, "paper_rebalance")


async def test_it_conforms_to_1569s_matcher_when_that_lands():
    """G5, second half. ``trace_references_strategy`` lives on #1569's branch,
    not on main. Import the CONSTANT rather than re-typing ``"rebalance"``, so
    the day #1569 merges this becomes a live conformance check with no edit."""
    trace_visibility = pytest.importorskip(
        "archimedes.services.trace_visibility",
        reason="present on main; the matcher below is #1569's",
    )
    matcher = getattr(trace_visibility, "trace_references_strategy", None)
    if matcher is None:
        pytest.skip("trace_references_strategy lands with #1569 (dbrowneup/user-reachable-traces)")

    dep_id = _seed()
    with _store(_Store()) as store:
        _advance(dep_id)
    (trace,) = {payload["id"]: payload for payload in store.records.values()}.values()

    assert matcher(trace, STRATEGY_ID) is True
    assert matcher({**trace, "decision_type": "paper_rebalance"}, STRATEGY_ID) is False


# ── G3: owner-gated reads, with the leaks-without-the-gate control ──────────


async def test_owner_reads_the_paper_trace_and_nobody_else_does():
    """G3. ACCEPTANCE #3 (issue): the owner sees it; anonymous gets the 404
    shape. 404 not 403 — a 403 on someone else's id confirms the id exists,
    which is half of the enumeration the gate prevents."""
    dep_id = _seed()
    with _store(_Store()) as store:
        _advance(dep_id)
    (trace,) = {payload["id"]: payload for payload in store.records.values()}.values()

    with _store(store), _as(OWNER_USER_ID, OWNER_WALLET):
        owner_detail = await _get(f"/api/traces/{trace['id']}")
        owner_list = await _get("/api/traces/", limit=100)
    with _store(store), _as(OTHER_USER_ID, OTHER_WALLET):
        other_detail = await _get(f"/api/traces/{trace['id']}")
        other_list = await _get("/api/traces/", limit=100)
    with _store(store), _as(None):
        anon_detail = await _get(f"/api/traces/{trace['id']}")
        anon_canonical = await _get(f"/api/traces/{trace['id']}/canonical")
        anon_list = await _get("/api/traces/", limit=100)

    assert owner_detail.status_code == 200
    assert [t["id"] for t in owner_list.json()["traces"]] == [trace["id"]]

    for resp in (other_detail, anon_detail, anon_canonical):
        assert resp.status_code == 404
        assert SECRET_SYMBOL not in resp.text
    for resp in (other_list, anon_list):
        assert resp.json()["traces"] == []
        assert resp.json()["total"] == 0
        assert SECRET_SYMBOL not in resp.text


async def test_the_zero_address_sentinel_would_leak_the_same_body():
    """G3's adversarial control — the reason ``vault_address=""`` is not
    cosmetic.

    The design's finding: reusing the zero-address sentinel for "no vault"
    makes an UNSTAMPED paper trace world-readable, because
    ``is_public_trace_vault`` returns True for any non-blank address while
    ``PUBLIC_TRACE_VAULTS`` is unarmed (it is set nowhere in this tree). Here
    the identical record is rewritten onto the sentinel with the stamp
    removed, and it IS readable anonymously — which is what makes the test
    above a demonstration rather than a tautology.

    The sentinel was imported as ``construction_trace.UNBOUND_VAULT`` until
    that zero-caller module was deleted; the literal is inlined here. The
    control is unchanged — any non-blank vault address leaks, and the zero
    address is the one a future author is most likely to reach for.
    """
    UNBOUND_VAULT = "0x0000000000000000000000000000000000000000"

    dep_id = _seed()
    with _store(_Store()) as store:
        _advance(dep_id)
    (trace,) = {payload["id"]: payload for payload in store.records.values()}.values()

    leaky = {**trace, "vault_address": UNBOUND_VAULT}
    leaky.pop("owner_user_id")
    leaky.pop("owner_wallet")
    leaky_store = _Store()
    leaky_store.records = {leaky["id"]: leaky, leaky["trace_hash"]: leaky}

    with _store(leaky_store), _as(None):
        detail = await _get(f"/api/traces/{leaky['id']}")
        canonical = await _get(f"/api/traces/{leaky['id']}/canonical")

    assert detail.status_code == 200, "the sentinel must be shown to leak, or the blank-vault choice proves nothing"
    assert SECRET_SYMBOL in canonical.text


# ── G2: tamper evidence, with a non-hashed control ─────────────────────────


async def test_a_tampered_paper_trace_does_not_re_derive_its_hash():
    """G2. Flip one character of ``reasoning`` in the stored record and the
    canonical bytes no longer keccak to the stored hash.

    ADVERSARIAL CONTROL (non-tautological): mutating a NON-hashed field
    (``arc_tx_hash``) must leave the hash valid — otherwise this test would be
    measuring "any write breaks it" rather than "the hashed set is what is
    protected"."""
    from web3 import Web3

    dep_id = _seed()
    with _store(_Store()) as store:
        _advance(dep_id)
    (trace,) = {payload["id"]: payload for payload in store.records.values()}.values()

    tampered = {**trace, "reasoning": trace["reasoning"].replace("Rebalance", "Rebalanse", 1)}
    tampered_store = _Store()
    tampered_store.records = {tampered["id"]: tampered}
    with _store(tampered_store), _as(OWNER_USER_ID, OWNER_WALLET):
        canonical = await _get(f"/api/traces/{tampered['id']}/canonical")
    assert Web3.keccak(text=canonical.text).hex() != trace["trace_hash"]

    annotated = {**trace, "arc_tx_hash": "0xFORGED", "is_verified": True}
    annotated_store = _Store()
    annotated_store.records = {annotated["id"]: annotated}
    with _store(annotated_store), _as(OWNER_USER_ID, OWNER_WALLET):
        canonical = await _get(f"/api/traces/{annotated['id']}/canonical")
    assert Web3.keccak(text=canonical.text).hex() == trace["trace_hash"]


# ── G1: publishing disabled is LOUD ────────────────────────────────────────


async def test_publishing_disabled_is_loud_and_durable(monkeypatch, caplog):
    """G1 + the issue's third acceptance criterion: with publishing disabled
    the failure is VISIBLE, not a silent zero.

    Four surfaces, all required: a durable Postgres row, a WARNING naming the
    env var, the advance's own counters, and ``trace_coverage`` on the
    deployment payload."""
    monkeypatch.setenv("PAPER_TRACE_PUBLISH", "off")
    dep_id = _seed()
    with _store(_Store()) as store, caplog.at_level("WARNING"):
        result = _advance(dep_id)

    assert store.records == {}, "nothing may be published while the switch is off"
    assert result["decisions"] == 1
    assert result["disabled"] == 1
    assert result["published"] == 0
    # The ledger still advances — freezing every user's paper history behind a
    # switch would trade a visible gap for an invisible stall.
    assert result["appended"] == 3

    assert [(row.status, row.trace_id) for row in _rows(dep_id)] == [("disabled", None)]
    assert any("PAPER_TRACE_PUBLISH" in record.getMessage() for record in caplog.records)

    coverage = _summary(dep_id)["trace_coverage"]
    assert coverage["status"] == "disabled"
    assert (coverage["decisions"], coverage["published"], coverage["disabled"]) == (1, 0, 1)
    assert coverage["first_gap_at"] == DECIDED.isoformat()


async def test_the_naive_coverage_rule_would_hide_the_disabled_gap(monkeypatch):
    """G1's adversarial half: build the rule that SHOULD fail and show it does.

    ``status`` derived from a bare ``published > 0`` — the obvious shortcut —
    reports the disabled deployment as healthy while it has zero traces. The
    real rule reports ``disabled``. If both said the same thing, the coverage
    field would be decoration."""
    monkeypatch.setenv("PAPER_TRACE_PUBLISH", "off")
    dep_id = _seed()
    with _store(_Store()):
        _advance(dep_id)

    coverage = _summary(dep_id)["trace_coverage"]

    def _naive_status(cov: dict) -> str:
        """The shortcut this design deliberately does not take."""
        return "ok" if cov["published"] > 0 or cov["decisions"] == 0 else "gap"

    assert _naive_status(coverage) == "gap"
    # ...but on a deployment that has published something AND has a gap, the
    # shortcut lies outright — which is the shape that hides a partial hole.
    assert _naive_status({"published": 3, "decisions": 5}) == "ok"
    assert coverage["status"] == "disabled", "the shipped rule names the switch instead"


async def test_a_disabled_gap_heals_on_the_next_settle(monkeypatch):
    """A gap should close itself. The decision key makes the retry idempotent,
    so re-attempting a ``disabled`` row cannot double-publish."""
    monkeypatch.setenv("PAPER_TRACE_PUBLISH", "off")
    dep_id = _seed()
    with _store(_Store()):
        _advance(dep_id)
    assert [row.status for row in _rows(dep_id)] == ["disabled"]

    monkeypatch.setenv("PAPER_TRACE_PUBLISH", "true")
    with _store(_Store()) as store:
        result = _advance(dep_id)

    assert result["published"] == 1 and result["disabled"] == 0
    assert [row.status for row in _rows(dep_id)] == ["published"]
    assert len({payload["id"] for payload in store.records.values()}) == 1
    assert _summary(dep_id)["trace_coverage"]["status"] == "ok"


# ── G6: a store outage is a loud, durable, retryable gap ───────────────────


async def test_a_store_outage_is_a_gap_not_a_stall(caplog):
    """The ledger is the honest number of record and must keep advancing; the
    absence is what has to be loud. Postgres carries the gap, so it survives
    the process that caused it."""
    dep_id = _seed()
    with _store(_Store(fail=True)), caplog.at_level("WARNING"):
        result = _advance(dep_id)

    assert result["failed"] == 1 and result["published"] == 0
    assert result["appended"] == 3, "a Redis outage must not freeze the user's paper ledger"
    (row,) = _rows(dep_id)
    assert row.status == "failed"
    assert "ConnectionError" in row.error
    coverage = _summary(dep_id)["trace_coverage"]
    assert coverage["status"] == "gap"
    assert coverage["failed"] == 1
    assert coverage["gap_at"] is not None


async def test_a_deployment_with_no_ownership_fails_closed(caplog):
    """A trace we cannot scope is worse than no trace, and a silent skip is
    worse than both. ERROR, a durable ``unowned`` row, and nothing published."""
    dep_id = _seed(owner_user_id=None, owner_wallet=None)
    with _store(_Store()) as store, caplog.at_level("ERROR"):
        result = _advance(dep_id)

    assert store.records == {}
    assert result["unowned"] == 1
    assert [row.status for row in _rows(dep_id)] == ["unowned"]
    assert any("NEITHER owner_user_id NOR owner_wallet" in r.getMessage() for r in caplog.records)


# ── G4: idempotency ────────────────────────────────────────────────────────


async def test_three_advances_produce_one_trace_per_decision():
    """G4. ``advance_deployment`` re-replays full history every settle, so
    without a durable key every deployment would republish its whole decision
    history daily."""
    dep_id = _seed()
    store = _Store()
    with _store(store):
        first = _advance(dep_id)
        second = _advance(dep_id)
        third = _advance(dep_id)

    assert (first["published"], second["published"], third["published"]) == (1, 1, 1)
    assert len(_rows(dep_id)) == 1
    # The load-bearing assertion: the WRITE happened once. Asserting "one
    # distinct trace id" would pass either way, because the id is derived from
    # the decision key.
    assert store.saves == 1
    assert _summary(dep_id)["trace_coverage"]["decisions"] == 1


async def test_the_decision_key_is_unique_in_the_database():
    """G4's adversarial half: the key is enforced by the DATABASE, not only by
    the code path above. Insert the duplicate the app would never write and
    watch the constraint reject it — that is what makes idempotency survive a
    concurrent settle rather than depending on the read-then-write above."""
    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperDecisionTrace
    from sqlalchemy.exc import IntegrityError

    dep_id = _seed()
    with _store(_Store()):
        _advance(dep_id)

    with pytest.raises(IntegrityError), get_session() as session:
        session.add(PaperDecisionTrace(deployment_id=dep_id, decision_date=DECIDED, status="published"))
        session.commit()


async def test_a_re_replay_that_decides_differently_is_drift_not_a_rewrite():
    """The hash is the point, so a published trace is never rewritten. The
    disagreement is counted and stamped, exactly as the ledger's own drift is."""
    dep_id = _seed()
    store = _Store()
    with _store(store):
        _advance(dep_id)
        original = dict(next(iter(store.records.values())))

        def restated(spec_dict, deployed_at):
            return {DECIDED: _decision([{**_LEG, "size": 900.0, "price": 111.0}])}

        result = _advance(dep_id, decisions=restated)

    assert result["trace_drift"] == 1
    (row,) = _rows(dep_id)
    assert row.trace_hash == original["trace_hash"], "a published trace must never be rewritten"
    assert _summary(dep_id)["trace_coverage"]["drift_at"] is not None


# ── G7: the coverage identity RAISES ───────────────────────────────────────


async def test_the_coverage_identity_holds_across_every_failure_state():
    dep_id = _seed()
    for store in (_Store(), _Store(fail=True)):
        with _store(store):
            result = _advance(dep_id)
        assert result["decisions"] == (result["published"] + result["failed"] + result["unowned"] + result["disabled"])


async def test_a_stored_unknown_status_trips_the_identity_instead_of_a_KeyError(caplog):
    """G7, third half. The publish branch already bucketed by explicit
    membership; the EXISTING-ROW branch did ``counts[row.status] += 1`` and
    died on a bare ``KeyError`` — deep in a loop, naming neither the deployment
    nor the decision, and aborting the settle before the identity could say
    what was lost. A stored row can carry anything (a hand-edited row, a
    half-run migration, an older writer's vocabulary), so this is untrusted
    input, not a local variable.

    ADVERSARIAL: the input is seeded to a status outside all four buckets and
    the LOUD path is asserted by type — ``PaperTraceCoverageError`` with the
    bucket breakdown — with ``KeyError`` explicitly excluded."""
    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperDecisionTrace
    from archimedes.services.paper_trading import PaperTraceCoverageError

    dep_id = _seed()
    with get_session() as session:
        session.add(PaperDecisionTrace(deployment_id=dep_id, decision_date=DECIDED, status="weird"))
        session.commit()

    with (
        _store(_Store()),
        caplog.at_level("ERROR"),
        pytest.raises(PaperTraceCoverageError, match="left the pipeline uncounted") as caught,
    ):
        _advance(dep_id)
    assert not isinstance(caught.value, KeyError)
    assert any("unrecognised trace status 'weird'" in r.getMessage() for r in caplog.records)

    # And the READ side does not blow up either — it reports the row instead of
    # dropping it from the totals, so `decisions` still adds up.
    caplog.clear()
    with caplog.at_level("ERROR"):
        coverage = _summary(dep_id)["trace_coverage"]
    assert coverage["decisions"] == 1
    assert coverage["unknown"] == 1
    assert coverage["status"] == "gap"
    assert sum(coverage[k] for k in ("published", "failed", "unowned", "disabled", "unknown")) == coverage["decisions"]
    assert any("unrecognised status(es) ['weird']" in r.getMessage() for r in caplog.records)


async def test_a_miscount_trips_the_coverage_identity():
    """G7's adversarial half: build the input that SHOULD fail the identity.

    A publisher returning a status outside the four buckets is exactly the
    "decision fell out of the pipeline" shape the identity exists to catch —
    the one that produces a silent zero on a page claiming full coverage. It
    must RAISE, not log."""
    from archimedes.services import paper_trace as pt
    from archimedes.services.paper_trading import PaperTraceCoverageError

    dep_id = _seed()
    with (
        _store(_Store()),
        patch.object(pt, "publish_paper_trace", return_value=("lost", None)),
        pytest.raises(PaperTraceCoverageError, match="left the pipeline uncounted"),
    ):
        _advance(dep_id)


# ── The published portfolio is the DEPLOYMENT's, not one sleeve's ──────────


async def test_the_published_trace_carries_the_whole_deployments_portfolio():
    """A 2-sleeve deployment tracing one sleeve's decision.

    ``portfolio_before``/``portfolio_after`` are hashed and, on the opt-in
    anchoring path, ``reveal()`` writes them on-chain permanently. Derived from
    the traded sleeve's legs alone they stated a deployment holding $200,000 as
    holding $100,000 and left the untraded symbol out of ``holdings``
    altogether — a false statement with a keccak over it.

    ADVERSARIAL: the leg-derived figures are computed alongside and asserted to
    DIFFER, so this measures the aggregate rather than any snapshot at all."""
    dep_id = _seed()
    with _store(_Store()) as store:
        _advance(dep_id)
    (trace,) = {payload["id"]: payload for payload in store.records.values()}.values()

    before, after = trace["portfolio_before"], trace["portfolio_after"]

    # Both sleeves present on both sides — the untraded one included.
    assert sorted(before["holdings"]) == sorted([QUIET_SYMBOL, SECRET_SYMBOL])
    assert sorted(after["holdings"]) == sorted([QUIET_SYMBOL, SECRET_SYMBOL])
    assert after["holdings"][QUIET_SYMBOL] == {"size": 0.0, "price": 250.0, "value": 0.0}

    # The deployment's cash, not the traded sleeve's: the quiet sleeve's full
    # opening capital is in both totals.
    assert before["cash"] == pytest.approx(_LEG["cash_before"] + SLEEVE_CASH)
    assert after["cash"] == pytest.approx(_LEG["cash_after"] + SLEEVE_CASH)

    # ADVERSARIAL CONTROL: what the per-leg rule would have published.
    leg_only_before = sum(float(leg["cash_before"]) for leg in [_LEG])
    leg_only_after = sum(float(leg["cash_after"]) for leg in [_LEG])
    assert before["cash"] != pytest.approx(leg_only_before)
    assert after["cash"] != pytest.approx(leg_only_after)
    assert QUIET_SYMBOL not in {leg["symbol"] for leg in [_LEG]}

    # And the traded sleeve is still bracketed correctly by its own leg.
    assert before["holdings"][SECRET_SYMBOL]["size"] == 0.0
    assert after["holdings"][SECRET_SYMBOL]["size"] == 182.0


async def test_a_decision_payload_without_a_portfolio_is_rejected():
    """The snapshots are required at the seam, not defaulted. A caller that
    supplies only legs cannot produce a deployment-scoped portfolio, and the
    honest answer is to refuse rather than to publish a half-formed one."""
    from archimedes.services.paper_trading import PaperReplayError

    dep_id = _seed()

    def legs_only(spec_dict, deployed_at):
        return {DECIDED: [dict(_LEG)]}

    with _store(_Store()), pytest.raises(PaperReplayError, match="portfolio_before"):
        _advance(dep_id, decisions=legs_only)


# ── G7b: a broken identity is not "advance crashed" ────────────────────────


async def test_a_broken_identity_is_isolated_but_distinct_in_advance_all(caplog):
    """The class docstring says this must NOT be swept in with the ordinary
    per-deployment failure — but ``advance_all``'s bare ``except Exception``
    swept it in anyway and logged "advance crashed", which reads as one
    deployment's bad data. A broken identity is a bug in the trace pipeline and
    the counts users read about their own provenance are wrong.

    Both halves are asserted: the distinct ERROR literal AND the isolation —
    the other deployment's ledger still advances."""
    from archimedes.db import get_session
    from archimedes.models.paper_store import PaperDeployment
    from archimedes.services import paper_trace as pt
    from archimedes.services import paper_trading
    from archimedes.services.paper_trading import COVERAGE_BROKEN_LOG, advance_all

    broken = _seed()
    with get_session() as session:
        healthy = PaperDeployment(
            id="dep1575b",
            strategy_id=STRATEGY_ID,
            owner_wallet=OWNER_WALLET,
            owner_user_id=OWNER_USER_ID,
            spec_json=json.dumps(_SPEC, sort_keys=True),
            deployed_at=DEPLOY,
            status="active",
        )
        session.add(healthy)
        session.commit()

    def one_decision(spec_dict, deployed_at):
        return _replay(spec_dict, deployed_at), {DECIDED: _decision()}

    def publish(dep, trace):
        # Only the first deployment falls out of the buckets, so the isolation
        # claim is measured against a deployment that genuinely succeeds.
        return ("lost", None) if dep.id == broken else ("published", None)

    original = paper_trading.replay_spec_with_decisions
    paper_trading.replay_spec_with_decisions = one_decision
    try:
        with (
            _store(_Store()),
            patch.object(pt, "publish_paper_trace", side_effect=publish),
            caplog.at_level("ERROR"),
            get_session() as session,
        ):
            out = advance_all(session)
            session.commit()
    finally:
        paper_trading.replay_spec_with_decisions = original

    assert out["coverage_broken"] == 1
    assert out["failed"] == 1
    assert out["ok"] == 1, "one deployment's broken accounting must not stall everyone else's ledger"
    messages = [r.getMessage() for r in caplog.records]
    assert any(COVERAGE_BROKEN_LOG in message for message in messages)
    assert not any("advance crashed" in message for message in messages), (
        "the generic handler must not be what reports a broken coverage identity"
    )


async def test_the_create_route_reports_a_broken_identity_as_an_error_not_a_deferral(caplog):
    """The route's other silencer, exercised through the real endpoint.

    "deferred to the scheduler" is a WARNING that promises the next pass fixes
    it, and the bare ``except Exception`` sent a broken coverage identity down
    exactly that path. It will break the same way next pass. Distinct ERROR —
    and the 201 still stands, because the deployment exists, its ledger is
    fine, and ``trace_coverage`` on the returned payload is what carries the
    hole to the user."""
    from archimedes.main import app
    from archimedes.services.paper_trading import COVERAGE_BROKEN_LOG, PaperTraceCoverageError

    def boom(session, dep, **kwargs):
        raise PaperTraceCoverageError("2 decisions detected but 1 accounted for — a decision left the pipeline")

    _seed()
    with (
        patch("archimedes.api.paper_routes._spec_for_strategy", return_value=_SPEC),
        patch("archimedes.api.paper_routes.advance_deployment", side_effect=boom),
        _as(OWNER_USER_ID, OWNER_WALLET),
        caplog.at_level("WARNING"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/paper/deployments", json={"strategy_id": STRATEGY_ID})

    assert resp.status_code == 201, "a broken identity must not take the create route down"
    assert any(COVERAGE_BROKEN_LOG in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert not any("deferred to the scheduler" in r.getMessage() for r in caplog.records), (
        "the deferral path promises the next settle fixes it; this one will break identically"
    )


# ── The ownership stamp really does suppress the vault lookup ──────────────


async def test_the_stamp_suppresses_save_traces_vault_owner_lookup():
    """#1556's documented escape hatch, exercised against the REAL
    ``save_trace`` rather than the fixture: a caller that already knows the
    owner sets the keys itself, and the presence of either suppresses the
    vault lookup. A paper deployment has no vault, so a lookup here could only
    ever produce a wrong answer."""
    from archimedes.services.redis_state import AgentStateStore

    def _must_not_run(_vault):
        raise AssertionError("save_trace resolved the vault owner for a stamped paper trace")

    redis = AsyncMock()
    with (
        patch.object(AgentStateStore, "_get_redis", AsyncMock(return_value=redis)),
        patch.object(AgentStateStore, "_resolve_trace_owner", staticmethod(_must_not_run)),
        patch.object(AgentStateStore, "close", AsyncMock()),
    ):
        dep_id = _seed()
        result = _advance(dep_id)

    assert result["published"] == 1
    (payload,) = [json.loads(call.args[1]) for call in redis.set.call_args_list if call.args[0].endswith(":")] or [
        json.loads(call.args[1]) for call in redis.set.call_args_list if call.args[1].startswith("{")
    ]
    assert payload["owner_user_id"] == OWNER_USER_ID


# ── G8: the advance cycle is ONE connection (#1818 P2) ─────────────────────


async def test_the_advance_cycle_opens_no_second_session(monkeypatch):
    """The wedge shape from the 2026-09-03 incident, made unrepresentable.

    ``docs/incidents/2026-09-03-paper-advance-ddl-wedge.md``, mechanism item 3:
    the cycle transaction held ``AccessShareLock`` on ``papers`` for the whole
    replay while ``resolve_paper_hashes`` opened a SECOND session and queried
    ``papers`` from it. A sibling task's ``ALTER TABLE papers`` sat between the
    two, so session 2 waited on the ALTER, the ALTER waited on session 1, and
    session 1 waited on session 2 — a cycle that lives outside PostgreSQL,
    which is why there was no deadlock detection, no timeout and no log line
    for ten hours.

    So the guard is not about ``papers`` or about DDL, both of which P1 already
    moved off the cycle path: it is that the cycle is ONE connection, whatever
    anyone later adds to it. Exactly one session may be opened for the whole
    advance, the cycle's own, and the failure message names the file and line
    of every extra opener.

    ``archimedes.db.SessionLocal`` is what is instrumented, NOT ``get_session``.
    Patching ``get_session`` only intercepts callers that resolve the name at
    call time; this repo's dominant idiom is a module-level ``from archimedes.db
    import get_session`` (``api/user_routes.py``, ``api/wallet_routes.py``,
    ``marketplace/service.py``, ``services/corpus_service.py``,
    ``scripts/run_backtests.py``, …), and those bindings are invisible to that
    patch — a second session opened through one of them left this guard GREEN.
    ``get_session()`` is ``return SessionLocal()``, resolving the module global
    at call time, so instrumenting the factory counts every route to a session
    including those bindings.

    The corpus row is seeded so the lookup genuinely resolves — a guard that
    passed because ``resolve_paper_hashes`` returned early on an empty corpus
    would be measuring nothing.
    """
    import traceback

    import archimedes.db as db_module
    from archimedes.models.corpus_store import PaperRecord
    from archimedes.services import paper_trading

    _seed()
    with db_module.get_session() as session:
        session.merge(PaperRecord(arxiv_id="0706.1497", title="Faber", content_hash="c" * 64))
        session.commit()

    def one_decision(spec_dict, deployed_at):
        return _replay(spec_dict, deployed_at), {DECIDED: _decision()}

    monkeypatch.setattr(paper_trading, "replay_spec_with_decisions", one_decision)

    opens: list[str] = []
    real_session_local = db_module.SessionLocal
    db_file = db_module.__file__

    def _recording_session_local(*args, **kwargs):
        # Name the REAL opener: drop this wrapper, then any ``archimedes/db.py``
        # frame, since ``get_session`` is only ``return SessionLocal()``. A
        # future direct ``SessionLocal()`` caller is attributed just as
        # precisely, which a fixed stack index would not manage.
        stack = traceback.extract_stack()[:-1]
        caller = next((f for f in reversed(stack) if f.filename != db_file), stack[-1])
        opens.append(f"{caller.filename}:{caller.lineno} in {caller.name}()")
        return real_session_local(*args, **kwargs)

    monkeypatch.setattr(db_module, "SessionLocal", _recording_session_local)

    store = _Store()
    with _store(store):
        # Exactly the shape of `paper_advance_loop`'s `_run`: one session, one
        # commit, the whole fleet advanced in between.
        from archimedes.db import get_session
        from archimedes.services.paper_trading import advance_all

        with get_session() as session:
            out = advance_all(session)
            session.commit()

    assert out["ok"] == 1 and out["traces_published"] == 1, "the cycle has to have actually done the work"
    (published,) = list({id(v): v for v in store.records.values()}.values())
    assert published["consulted_paper_hashes"] == ["0706.1497:" + "c" * 64], (
        "the corpus lookup must have really run on the cycle session"
    )
    assert len(opens) == 1, (
        f"the advance cycle opened {len(opens)} sessions, not 1 — a second connection inside the cycle "
        f"transaction is the 2026-09-03 wedge shape (#1818 P2). Openers: {opens[1:]}"
    )
