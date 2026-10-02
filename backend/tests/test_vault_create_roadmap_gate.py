"""``POST /api/vaults/create`` answers the same way the pages do: not offered (#1432).

The UI keeps every vault surface behind its build-time roadmap flag
(``ROADMAP_SURFACES_ENABLED`` in ``ui/src/featureFlags.js``, off unless
``VITE_ROADMAP_SURFACES=true``), and the Privacy and Terms pages tell a reader
that vault deployment is not offered. The backend route did not agree. It was
mounted unconditionally and gated only by an account, a linked wallet and the
rigor gate, so a direct API caller could still have the backend signer deploy a
testnet vault and transfer it to their wallet.

``FEATURE_ROADMAP_SURFACES`` is the server-side twin of the UI flag. While it is
off, which it is in every environment unless set to ``true``, the route answers
404 before authentication, before body validation, before the rigor gate and
before any chain call. With it on, the route behaves exactly as it did before.

The discovery surfaces follow the route. A manifest that advertises a route that
404s sends an agent into a retry loop it cannot diagnose, and
``test_agent_discovery.py`` cannot catch that here: a feature-gated route stays
in the OpenAPI document. So the last block finds every roadmap-gated route by
walking the live app's dependency trees, and checks both discovery surfaces
against that set.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

V = "archimedes.api.vaults_routes"
FLAG = "FEATURE_ROADMAP_SURFACES"
CREATE = "POST /api/vaults/create"
WALLET = "0x000000000000000000000000000000000000dEaD"
BODY = {"name": "V", "symbol": "V", "strategy_ids": ["passing"]}
DEPLOYED = "0xVaultDeployedAddress"

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_CARD = REPO_ROOT / "ui" / "public" / ".well-known" / "agent.json"


@contextlib.contextmanager
def _linked_wallet(app, wallet: str = WALLET):
    """Stand in for a signed-in account with a verified linked wallet."""
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


@contextlib.contextmanager
def _create_side_effects():
    """Every effect the create route has after its dependencies resolve.

    All of them are stubbed, so a test can assert which ones ran: the rigor
    gate, the chain deploy (the backend signer spending gas), the funnel write,
    the identity-ledger event and the ownership-memo drop.
    """
    rigor = MagicMock(return_value=None)
    deploy = AsyncMock(return_value=DEPLOYED)
    funnel = AsyncMock()
    identity = MagicMock()
    invalidate = MagicMock()
    with (
        patch(f"{V}._assert_strategies_pass_rigor", rigor),
        patch(f"{V}.chain_executor.create_vault", new=deploy),
        patch("archimedes.api.funnel_middleware.record_funnel", new=funnel),
        patch("archimedes.services.identity_events.emit_identity_event", new=identity),
        patch("archimedes.services.trace_visibility.invalidate_vault_owner", new=invalidate),
    ):
        yield {"rigor": rigor, "deploy": deploy, "funnel": funnel, "identity": identity, "invalidate": invalidate}


async def _post(app, body: dict | None = None, *, raise_app_exceptions: bool = True):
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/vaults/create", json=BODY if body is None else body)


def _set_flag(monkeypatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, value)


# ── flag off: refused, and nothing downstream runs ───────────────────────────

# Unset is the production value: no committed deployment layer sets this name.
# The rest are the spellings an operator might reach for, plus a typo; only the
# word "true" turns the route on, so every one of these must leave it off.
OFF_VALUES = [None, "", "  ", "false", "False", "0", "off", "no", "1", "yes", "on", "ture"]


@pytest.mark.parametrize("value", OFF_VALUES)
async def test_flag_off_refuses_create_before_any_chain_call(monkeypatch, value):
    _set_flag(monkeypatch, value)
    from archimedes.main import app

    with _linked_wallet(app), _create_side_effects() as fx:
        resp = await _post(app)

    assert resp.status_code == 404, resp.text
    assert resp.json() == {"detail": "Not offered: roadmap, not shipped"}
    fx["deploy"].assert_not_called()  # the backend signer spent no gas
    fx["rigor"].assert_not_called()
    fx["funnel"].assert_not_called()
    fx["identity"].assert_not_called()
    fx["invalidate"].assert_not_called()


async def test_flag_off_answers_404_before_auth_and_before_body_validation(monkeypatch):
    """No session and an empty body: still 404, not 401 and not 422.

    A route that is not offered must not tell an anonymous caller what it
    would need to use it.
    """
    _set_flag(monkeypatch, None)
    from archimedes.main import app

    with _create_side_effects() as fx:
        resp = await _post(app, body={})

    assert resp.status_code == 404, resp.text
    fx["deploy"].assert_not_called()


# ── flag on: existing behaviour, unchanged ───────────────────────────────────


@pytest.mark.parametrize("value", ["true", "TRUE", " true "])
async def test_flag_on_create_runs_the_existing_path(monkeypatch, value):
    _set_flag(monkeypatch, value)
    from archimedes.main import app

    with _linked_wallet(app), _create_side_effects() as fx:
        resp = await _post(app)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"vault_address": DEPLOYED, "strategy_ids": ["passing"]}
    fx["rigor"].assert_called_once_with(["passing"], 1, request=ANY)
    fx["deploy"].assert_awaited_once_with(
        name="V",
        symbol="V",
        management_fee_bps=0,
        performance_fee_bps=0,
        agent_assisted=True,
        owner_wallet=WALLET,
    )
    fx["funnel"].assert_awaited_once_with(ANY, "vault_deployed")
    fx["identity"].assert_called_once()
    fx["invalidate"].assert_called_once_with(DEPLOYED)


async def test_flag_on_still_requires_an_account(monkeypatch):
    _set_flag(monkeypatch, "true")
    from archimedes.main import app

    with _create_side_effects() as fx:
        resp = await _post(app)

    assert resp.status_code == 401, resp.text
    fx["deploy"].assert_not_called()


# ── discovery surfaces must not advertise a route that 404s ──────────────────


def _depends_on(dependant, name: str) -> bool:
    return any(getattr(dep.call, "__name__", None) == name or _depends_on(dep, name) for dep in dependant.dependencies)


def _roadmap_gated_routes() -> set[str]:
    """Every ``"METHOD /path"`` on the live app that sits behind the roadmap gate.

    Found by walking each route's dependency tree for the gate function rather
    than from a list kept here, so a route moved behind the gate later is
    covered without editing this file.

    fastapi>=0.139 keeps each ``include_router`` call as an ``_IncludedRouter``
    whose routes are only reachable through ``effective_route_contexts()``; the
    context's ``dependant`` carries the merged router-level and route-level
    dependencies, so a gate applied at either level is found. Same two-shape
    walk as ``test_api_docs_drift._live_pairs``.
    """
    import fastapi.routing as fastapi_routing
    from archimedes.main import app
    from fastapi.routing import APIRoute

    included_router_cls = getattr(fastapi_routing, "_IncludedRouter", None)
    gated: set[str] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            candidates = [(route.methods, route.path, route.dependant)]
        elif included_router_cls is not None and isinstance(route, included_router_cls):
            candidates = [
                (ctx.methods, ctx.path_format, ctx.dependant)
                for ctx in route.effective_route_contexts()
                if isinstance(ctx.original_route, APIRoute)
            ]
        else:
            continue
        for methods, path, dependant in candidates:
            if _depends_on(dependant, "require_roadmap_surfaces"):
                gated.update(f"{method} {path}" for method in methods)
    return gated


def _card_routes() -> set[str]:
    card = json.loads(AGENT_CARD.read_text(encoding="utf-8"))
    return {
        route
        for group in card["endpoints"].values()
        if isinstance(group, dict)
        for route in group.get("routes", {}).values()
    }


async def _manifest_routes() -> set[str]:
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")
    assert resp.status_code == 200, resp.text
    return {route for group in resp.json()["endpoints"].values() for route in group["routes"].values()}


def test_create_route_sits_behind_the_roadmap_gate():
    """Guard on the guards below: an empty gated set would pass them vacuously."""
    assert CREATE in _roadmap_gated_routes()


def test_static_agent_card_advertises_no_roadmap_gated_route():
    """The card is a committed file, so it describes production, where the flag is off."""
    card = _card_routes()
    assert CREATE not in card
    assert not card & _roadmap_gated_routes()


async def test_served_manifest_omits_roadmap_gated_routes_while_the_flag_is_off(monkeypatch):
    _set_flag(monkeypatch, None)
    gated = _roadmap_gated_routes()
    assert gated, "no roadmap-gated route found; the check below would pass vacuously"
    assert not (await _manifest_routes()) & gated


async def test_served_manifest_lists_create_while_the_flag_is_on(monkeypatch):
    """The served manifest follows the server's own flag, so a preview stack that
    turns the route on still finds it, filed under the roadmap group."""
    _set_flag(monkeypatch, "true")
    assert CREATE in await _manifest_routes()
