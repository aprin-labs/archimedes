"""Hermetic tests for the server-side rigor deploy gate (#818).

The guarantee "only rigor-passing strategies are deployed" must hold server-side,
where a direct (non-UI) API call cannot route around it. These pin: the resolver
reads the authoritative verdict across curated + generated sources, fails closed on
a DB error, and `create_vault` returns 422 (without spending gas) for a strategy
that failed the gate or was never validated.
"""

from __future__ import annotations

import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import archimedes.db as db
import pytest
from archimedes.api.vaults_routes import _assert_strategies_pass_rigor, _strategy_rigor_status
from archimedes.services.live_rigor_gate import RigorGateVerdict
from archimedes.services.rigor_profiles import STRICTEST_LEVEL
from fastapi import HTTPException

V = "archimedes.api.vaults_routes"
# The curated deploy-gate branch imports the live gate lazily (inside the function)
# to avoid a route<->service import cycle, so patch the SOURCE module here.
LRG = "archimedes.services.live_rigor_gate"


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Rebind DB globals so this file stays hermetic regardless of import order."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'vault-rigor.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(db, "DATABASE_URL", url)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, autocommit=False, autoflush=False))
    db.init_db()
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _roadmap_surfaces_on(monkeypatch):
    """The create route 404s while the roadmap flag is off (#1432). These tests
    pin what it does once it is on; the off case lives in
    test_vault_create_roadmap_gate.py."""
    monkeypatch.setenv("FEATURE_ROADMAP_SURFACES", "true")


@contextlib.contextmanager
def _override_verified_wallet(app, wallet: str = "0x000000000000000000000000000000000000dEaD"):
    """Override linked-wallet dependency, restoring prior override on exit."""
    from archimedes.api.wallet_routes import require_linked_wallet

    prev = app.dependency_overrides.get(require_linked_wallet)
    app.dependency_overrides[require_linked_wallet] = lambda: wallet
    try:
        yield
    finally:
        if prev is None:
            app.dependency_overrides.pop(require_linked_wallet, None)
        else:
            app.dependency_overrides[require_linked_wallet] = prev


class _Strat:
    def __init__(self, passes, sid="s1"):
        self.passes_rigor_gate = passes
        # The cohort build dedupes on .id, so the stub needs one.
        self.id = sid


# ── _strategy_rigor_status ────────────────────────────────────────────────


def test_status_curated_passing():
    # strategy_provider is now a lazily-cached accessor (strategy_provider());
    # patch the name wholesale and configure the mocked callable's return_value.
    # The curated branch reads the LIVE gate (#1173), so stub that too.
    with (
        patch(f"{V}.strategy_provider") as mock_provider,
        patch(f"{LRG}.verdicts_for_strategies", return_value={"s1": RigorGateVerdict.passed()}),
    ):
        mock_provider.return_value.get_strategy.return_value = _Strat(True)
        assert _strategy_rigor_status("s1") == (True, True)


def test_status_curated_failing():
    with (
        patch(f"{V}.strategy_provider") as mock_provider,
        patch(f"{LRG}.verdicts_for_strategies", return_value={"s1": RigorGateVerdict.failed()}),
    ):
        mock_provider.return_value.get_strategy.return_value = _Strat(False)
        assert _strategy_rigor_status("s1") == (True, False)


def test_status_curated_uses_live_verdict_not_provider_attribute():
    """REGRESSION (#1173): a curated strategy must be deployable when the LIVE gate
    passes it, even though the provider object always carries
    ``passes_rigor_gate = False``.

    ``LocalStrategyProvider`` sets that attribute to False unconditionally
    (fail-closed by construction, 56cc9bde) and the real verdict is overlaid
    downstream. Reading the raw attribute here made EVERY curated strategy
    undeployable at the default strictness with a message asserting it "has not
    passed the rigor gate" — a false statement — while deploying at strictness >= 2
    worked, because that path consults the live gate. This test pins the live
    verdict as the source of truth.
    """
    with (
        patch(f"{V}.strategy_provider") as mock_provider,
        patch(f"{LRG}.verdicts_for_strategies", return_value={"s1": RigorGateVerdict.passed()}),
    ):
        # Provider says False — the historical bug source.
        mock_provider.return_value.get_strategy.return_value = _Strat(False)
        assert _strategy_rigor_status("s1") == (True, True)


def test_status_curated_grades_against_full_library_cohort():
    """REGRESSION (#1173): the deploy gate must grade against the shared FULL-library
    cohort, never a 1-item list.

    The verdict is cohort-dependent (cohort-scoped PBO/CSCV; a cohort under
    MIN_LIBRARY_N_FOR_PBO_GATING skips criterion 4), so grading a strategy alone
    would let the deploy gate disagree with the list badge and the passport.
    """
    strat = _Strat(False, sid="s1")
    library = [_Strat(False, sid=f"other{i}") for i in range(12)] + [strat]
    seen: dict = {}

    def _capture(strategies):
        seen["ids"] = [s.id for s in strategies]
        return {"s1": RigorGateVerdict.passed()}

    with (
        patch(f"{V}.strategy_provider") as mock_provider,
        patch(f"{LRG}.verdicts_for_strategies", side_effect=_capture),
    ):
        mock_provider.return_value.get_strategy.return_value = strat
        mock_provider.return_value.list_strategies.return_value = library
        assert _strategy_rigor_status("s1") == (True, True)

    assert seen["ids"] == [s.id for s in library], (
        "deploy gate must grade against the full library cohort, not the strategy alone; "
        f"got a {len(seen.get('ids', []))}-item cohort"
    )


def test_status_curated_fails_closed_when_live_gate_raises():
    """A live-gate failure must not wave a deploy through."""
    with (
        patch(f"{V}.strategy_provider") as mock_provider,
        patch(f"{LRG}.verdicts_for_strategies", side_effect=RuntimeError("boom")),
    ):
        mock_provider.return_value.get_strategy.return_value = _Strat(True)
        assert _strategy_rigor_status("s1") == (True, False)


def test_status_fails_closed_on_db_error():
    # Not curated → falls to the DB; if the session raises, we must report not-found
    # (False, False) so the caller blocks the deploy rather than waving it through.
    with (
        patch(f"{V}.strategy_provider") as mock_provider,
        patch("archimedes.db.get_session", side_effect=RuntimeError("db down")),
    ):
        mock_provider.return_value.get_strategy.return_value = None
        assert _strategy_rigor_status("missing") == (False, False)


def test_status_generated_delegates_to_generated_rigor():
    request = object()
    result = SimpleNamespace(passes_all=True)
    with (
        patch(f"{V}.strategy_provider") as provider,
        patch(
            "archimedes.api.selection_bias_routes._generated_strategy_rigor",
            return_value=result,
        ) as generated_rigor,
    ):
        provider.return_value.get_strategy.return_value = None
        assert _strategy_rigor_status("generated", request=request) == (True, True)

    generated_rigor.assert_called_once_with("generated", request, STRICTEST_LEVEL)


def test_status_generated_hides_missing_or_invisible():
    request = object()
    with (
        patch(f"{V}.strategy_provider") as provider,
        patch("archimedes.api.selection_bias_routes._generated_strategy_rigor", return_value=None),
    ):
        provider.return_value.get_strategy.return_value = None
        assert _strategy_rigor_status("private-or-missing", request=request) == (False, False)


def test_status_generated_rigor_error_returns_503():
    request = object()
    with (
        patch(f"{V}.strategy_provider") as provider,
        patch(
            "archimedes.api.selection_bias_routes._generated_strategy_rigor",
            side_effect=RuntimeError("db down"),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        provider.return_value.get_strategy.return_value = None
        _strategy_rigor_status("generated", request=request)

    assert exc_info.value.status_code == 503


# ── _assert_strategies_pass_rigor ─────────────────────────────────────────


def test_assert_raises_422_on_failing():
    with patch(f"{V}._strategy_rigor_status", return_value=(True, False)), pytest.raises(HTTPException) as exc:
        _assert_strategies_pass_rigor(["s1"])
    assert exc.value.status_code == 422
    assert "rigor gate" in exc.value.detail


def test_assert_raises_422_on_not_found():
    with patch(f"{V}._strategy_rigor_status", return_value=(False, False)), pytest.raises(HTTPException) as exc:
        _assert_strategies_pass_rigor(["ghost"])
    assert exc.value.status_code == 422
    assert "not found" in exc.value.detail


def test_assert_passes_on_passing():
    with patch(f"{V}._strategy_rigor_status", return_value=(True, True)):
        _assert_strategies_pass_rigor(["s1", "s2"])  # no raise


def test_assert_empty_list_is_noop():
    _assert_strategies_pass_rigor([])  # nothing to validate → no raise


def test_assert_blocks_if_any_one_fails():
    # All must pass; one failing id blocks the whole deploy.
    def _status(sid, cohort_cache=None, request=None):
        return (True, sid != "bad")

    with patch(f"{V}._strategy_rigor_status", side_effect=_status), pytest.raises(HTTPException) as exc:
        _assert_strategies_pass_rigor(["good", "bad", "good2"])
    assert exc.value.status_code == 422


# ── HTTP: the gate fires BEFORE the on-chain deploy ───────────────────────


async def test_create_vault_rejects_failing_strategy_before_deploy():
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    with (
        _override_verified_wallet(app),
        patch(f"{V}._strategy_rigor_status", return_value=(True, False)),
        patch(f"{V}.chain_executor.create_vault") as mock_create,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/vaults/create",
                json={"name": "V", "symbol": "V", "strategy_ids": ["failing"]},
            )

    assert resp.status_code == 422
    assert "rigor gate" in resp.json()["detail"]
    mock_create.assert_not_called()  # never spent gas — gate fired first


async def test_create_vault_proceeds_when_rigor_passes():
    # Complementary happy path: a passing strategy must NOT be blocked by the new
    # precondition — the deploy proceeds and chain_executor.create_vault is called.
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    with (
        _override_verified_wallet(app),
        patch(f"{V}._strategy_rigor_status", return_value=(True, True)),
        patch(f"{V}.chain_executor.create_vault", new=AsyncMock(return_value="0xVaultDeployedAddress")) as mock_create,
        # record_funnel touches Redis; stub it so the test stays hermetic.
        patch("archimedes.api.funnel_middleware.record_funnel", new=AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/vaults/create",
                json={"name": "V", "symbol": "V", "strategy_ids": ["passing"]},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["vault_address"] == "0xVaultDeployedAddress"
    assert body["strategy_ids"] == ["passing"]
    mock_create.assert_awaited_once()  # gate let it through → gas spent


async def test_create_vault_writes_the_identity_ledger_vault_created_event():
    """Issue #1028 (D2): the SIWE-verified deployer's ``vault_created`` event
    lands in the ledger — the bottom-of-funnel identity write, unlike
    Generate's account gate, since ``require_linked_wallet`` always
    identifies the caller here."""
    from archimedes.db import get_session
    from archimedes.main import app
    from archimedes.models.identity import IdentityEvent, WalletIdentity
    from httpx import ASGITransport, AsyncClient

    wallet = "0x" + "de" * 20  # matches _override_verified_wallet's lowercased form

    with (
        _override_verified_wallet(app, wallet=wallet),
        patch(f"{V}._strategy_rigor_status", return_value=(True, True)),
        patch(f"{V}.chain_executor.create_vault", new=AsyncMock(return_value="0xLedgerVaultAddress")),
        patch("archimedes.api.funnel_middleware.record_funnel", new=AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/vaults/create",
                json={"name": "V", "symbol": "V", "strategy_ids": ["passing"]},
            )
    assert resp.status_code == 200

    session = get_session()
    try:
        events = session.query(IdentityEvent).filter(IdentityEvent.wallet == wallet).all()
        shapes = [(e.event_type, e.actor_class, e.meta) for e in events]
    finally:
        session.query(IdentityEvent).filter(IdentityEvent.wallet == wallet).delete(synchronize_session=False)
        session.query(WalletIdentity).filter(WalletIdentity.wallet_address == wallet).delete(synchronize_session=False)
        session.commit()
        session.close()

    matching = [s for s in shapes if s[0] == "vault_created"]
    assert len(matching) == 1
    _, actor_class, meta = matching[0]
    assert actor_class == "human"
    assert meta["vault_address"] == "0xLedgerVaultAddress"
    assert meta["strategy_ids"] == ["passing"]


# ── HTTP: strictest-level generated path stays ownership-gated (#1073) ────

_OWNER = "0x000000000000000000000000000000000000dEaD"
_OTHER = "0x0000000000000000000000000000000000000BAD"


def _siwe_cookies(wallet: str) -> dict[str, str]:
    """Real signed SIWE session cookie (same helper shape as
    test_strategy_ownership.py / test_user_routes.py). Needed here because
    generated-rigor ownership reads the SIWE cookie from the request."""
    from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session

    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


async def test_create_vault_hides_private_strategy_from_non_owner():
    """A non-owner deploying a vault bound to a private generated strategy's
    id gets 422 not-found — even though the (ownership-blind) passport badge
    would say it passes. This is the #1073 regression: the badge alone can't
    be trusted at the strictest-level fast path once a request is present."""
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    sid = "priv-deploy-nonowner"
    with (
        patch("archimedes.api.selection_bias_routes._generated_strategy_rigor", return_value=None),
        patch(f"{V}.chain_executor.create_vault") as mock_create,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", cookies=_siwe_cookies(_OTHER)
        ) as client:
            resp = await client.post(
                "/api/vaults/create",
                json={"name": "V", "symbol": "V", "strategy_ids": [sid]},
            )

    assert resp.status_code == 422
    assert "not found" in resp.json()["detail"]
    mock_create.assert_not_called()  # never spent gas — ownership gate fired first


async def test_create_vault_allows_owner_to_deploy_private_strategy():
    """Complementary happy path: the strategy's OWNER deploying the same
    private strategy id is unaffected by the new ownership check."""
    from archimedes.main import app
    from httpx import ASGITransport, AsyncClient

    sid = "priv-deploy-owner"
    with (
        patch(
            "archimedes.api.selection_bias_routes._generated_strategy_rigor",
            return_value=SimpleNamespace(passes_all=True),
        ),
        patch(f"{V}.chain_executor.create_vault", new=AsyncMock(return_value="0xOwnerDeployedAddress")) as mock_create,
        patch("archimedes.api.funnel_middleware.record_funnel", new=AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", cookies=_siwe_cookies(_OWNER)
        ) as client:
            resp = await client.post(
                "/api/vaults/create",
                json={"name": "V", "symbol": "V", "strategy_ids": [sid]},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["vault_address"] == "0xOwnerDeployedAddress"
    mock_create.assert_awaited_once()  # owner → gate lets it through


# ── Cohort map computed once per deploy (#1172 review follow-up) ───────────
#
# `verdicts_for_strategies` is UNCACHED: every call re-reads
# get_all_daily_returns and re-runs a full cohort CSCV/PBO pass (~6s by the
# route's own measurement). Grading the deploy against the full library — the
# correctness fix — therefore made a vault bound to k curated strategies pay
# that k times inside one request. The map must be built at most once.


def test_deploy_gate_computes_cohort_verdicts_once_for_many_strategies():
    """A 3-strategy deploy triggers exactly ONE verdicts_for_strategies call."""
    ids = ["s1", "s2", "s3"]
    cohort = [_Strat(True, sid=i) for i in ids]
    calls: list[int] = []

    def _verdicts(strategies):
        calls.append(len(strategies))
        return {s.id: RigorGateVerdict.passed() for s in strategies}

    with (
        patch(f"{V}.strategy_provider") as provider,
        patch(f"{LRG}.verdicts_for_strategies", side_effect=_verdicts),
    ):
        provider.return_value.list_strategies.return_value = cohort
        provider.return_value.get_strategy.side_effect = lambda sid: next((s for s in cohort if s.id == sid), None)
        _assert_strategies_pass_rigor(ids)  # must not raise

    assert len(calls) == 1, f"expected 1 cohort computation, got {len(calls)}"
    assert calls[0] == len(cohort), "and it must be graded over the FULL library"


def test_deploy_gate_does_not_cache_a_one_off_cohort():
    """A strategy MISSING from list_strategies() is graded on a one-off cohort
    (itself appended). That map must NOT be cached and reused, or a second such
    strategy would miss the dict, degrade to `pending`, and be wrongly refused."""
    listed = [_Strat(True, sid="listed")]
    missing_a = _Strat(True, sid="missing-a")
    missing_b = _Strat(True, sid="missing-b")
    by_id = {s.id: s for s in (listed[0], missing_a, missing_b)}
    seen: list[list[str]] = []

    def _verdicts(strategies):
        seen.append([s.id for s in strategies])
        return {s.id: RigorGateVerdict.passed() for s in strategies}

    with (
        patch(f"{V}.strategy_provider") as provider,
        patch(f"{LRG}.verdicts_for_strategies", side_effect=_verdicts),
    ):
        provider.return_value.list_strategies.return_value = listed
        provider.return_value.get_strategy.side_effect = by_id.get
        # Neither unlisted strategy may be refused.
        _assert_strategies_pass_rigor(["missing-a", "missing-b"])

    assert len(seen) == 2, "each unlisted strategy needs its own cohort"
    assert seen[0] == ["listed", "missing-a"]
    assert seen[1] == ["listed", "missing-b"]
