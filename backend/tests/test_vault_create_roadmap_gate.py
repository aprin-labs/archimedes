"""Vault deployment through the API answers the same way the pages do: not offered (#1432).

The UI keeps every vault and marketplace surface behind its build-time roadmap
flag (``ROADMAP_SURFACES_ENABLED`` in ``ui/src/featureFlags.js``, off unless
``VITE_ROADMAP_SURFACES=true``), and the Privacy and Terms pages tell a reader
that vault deployment is not offered. The backend did not agree. Three mounted
routes have the backend signer deploy a testnet vault owned by the caller's
wallet: ``POST /api/vaults/create``, ``POST /api/marketplace/publish`` (when no
``vault_address`` is given) and ``POST /api/marketplace/subscribe`` (always).
Each was gated only by an account and a linked wallet (plus the rigor gate on
create), so a direct API caller could still get a vault deployed.

``FEATURE_ROADMAP_SURFACES`` is the server-side twin of the UI flag. While it is
off, which it is in every environment unless set to ``true``, each of the three
answers 404 and nothing downstream of the gate runs: not the route's auth
dependency, not body-schema validation, not the handler. Two things do run
first, the app's middleware and FastAPI's JSON parse of the body, so a body
that is not valid JSON gets FastAPI's 422 ``json_invalid`` instead; that is
pinned here too, because the docs state it. With the flag on, each route
behaves exactly as it did before. The create route's behaviour is pinned in
this file; publish and subscribe in ``tests/api/test_marketplace_routes.py``.

The discovery surfaces follow the routes. A manifest that advertises a route
that 404s sends an agent into a retry loop it cannot diagnose, and
``test_agent_discovery.py`` cannot catch that here: a feature-gated route stays
in the OpenAPI document. So the last block finds every roadmap-gated route by
walking the live app's dependency trees, pins that set to exactly the three
routes above, and checks both discovery surfaces against it.
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
PUBLISH = "POST /api/marketplace/publish"
SUBSCRIBE = "POST /api/marketplace/subscribe"
#: Every mounted route that has the backend signer deploy a vault owned by the
#: caller's wallet, and nothing else. A route added to the gate, or dropped
#: from it, must change this set and say why in the same change.
GATED = {CREATE, PUBLISH, SUBSCRIBE}
GATE_404 = {"detail": "Not offered: roadmap, not shipped"}
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


# A body each route's own schema rejects: create declares VaultCreateRequest
# (an empty object is missing name/symbol), publish and subscribe declare
# ``dict`` (a JSON array is not one).
SCHEMA_INVALID_BODY = {CREATE: {}, PUBLISH: ["not", "an", "object"], SUBSCRIBE: ["not", "an", "object"]}


async def _post_route(app, route: str, **kwargs):
    method, path = route.split(" ", 1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.parametrize("route", sorted(GATED))
async def test_flag_off_answers_404_before_auth_and_before_body_schema_validation(monkeypatch, route):
    """No session and a body the route's schema rejects: still 404, not 401 and
    not 422. With the flag on, the same request is refused by auth (401).

    A route that is not offered must not tell an anonymous caller what it
    would need to use it.
    """
    from archimedes.main import app

    with _create_side_effects() as fx:
        _set_flag(monkeypatch, None)
        off = await _post_route(app, route, json=SCHEMA_INVALID_BODY[route])
        _set_flag(monkeypatch, "true")
        on = await _post_route(app, route, json=SCHEMA_INVALID_BODY[route])

    assert off.status_code == 404, off.text
    assert off.json() == GATE_404
    assert on.status_code == 401, on.text
    fx["deploy"].assert_not_called()


@pytest.mark.parametrize("route", sorted(GATED))
async def test_flag_off_body_that_is_not_json_gets_fastapis_422_first(monkeypatch, route):
    """The one request shape the gate does not answer first, pinned so the docs'
    exact statement of it stays true: FastAPI parses the JSON body before it
    resolves any dependency, so a body that does not parse is a 422
    ``json_invalid``. Nothing downstream runs for it either."""
    _set_flag(monkeypatch, None)
    from archimedes.main import app

    with _linked_wallet(app), _create_side_effects() as fx:
        resp = await _post_route(app, route, content=b'{"name": "V", ', headers={"Content-Type": "application/json"})

    assert resp.status_code == 422, resp.text
    assert [err["type"] for err in resp.json()["detail"]] == ["json_invalid"]
    fx["deploy"].assert_not_called()
    fx["rigor"].assert_not_called()
    fx["identity"].assert_not_called()


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


def test_the_roadmap_gate_covers_exactly_the_vault_deploying_routes():
    """The gated set is exactly the three routes that deploy a vault for the
    caller: not fewer (one left open makes the Privacy page false for a direct
    API caller) and not more (a gated exit route, such as unsubscribe, would
    strand an existing subscriber's refund). Also the guard on the guards
    below: an empty gated set would pass them vacuously."""
    from archimedes import main

    assert main.marketplace_router is not None, (
        "the marketplace router is not mounted (its circlekit import failed), so this walk would see a partial app"
    )
    assert _roadmap_gated_routes() == GATED


def test_static_agent_card_advertises_no_roadmap_gated_route():
    """The card is a committed file, so it describes production, where the flag is off."""
    card = _card_routes()
    assert not card & GATED
    assert not card & _roadmap_gated_routes()


async def test_served_manifest_omits_roadmap_gated_routes_while_the_flag_is_off(monkeypatch):
    _set_flag(monkeypatch, None)
    gated = _roadmap_gated_routes()
    assert gated, "no roadmap-gated route found; the check below would pass vacuously"
    manifest = await _manifest_routes()
    assert not manifest & gated
    assert not manifest & GATED


async def test_served_manifest_lists_the_gated_routes_while_the_flag_is_on(monkeypatch):
    """The served manifest follows the server's own flag, so a preview stack that
    turns the routes on still finds them, filed under their roadmap groups."""
    _set_flag(monkeypatch, "true")
    assert await _manifest_routes() >= GATED
