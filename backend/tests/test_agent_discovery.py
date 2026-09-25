"""The two machine-readable discovery surfaces must describe the API that exists.

``GET /api/agent/manifest`` and the static ``ui/public/.well-known/agent.json`` both
hand an autonomous client a list of routes to call. A stale entry there is worse than
an omission: the agent spends a request, gets a 404, and has no way to tell "wrong path"
from "endpoint down". #1293 was filed after exactly that class of dogfood friction.

So both surfaces are checked against the app's own OpenAPI document — the same source
FastAPI serves at ``/openapi.json`` — rather than against a hand-kept list that would
drift in lockstep with the thing it is supposed to guard.

The ``/api/auth/*`` prefix is the one documented exemption: those routes belong to the
Better Auth Node service (``auth/server.js``), proxied onto the same origin by nginx,
so they are genuinely absent from the FastAPI route table. The exemption is asserted to
stay exactly that narrow — widening it would quietly turn this guard off.

A route resolving is necessary but not sufficient. The second guard here is
**cross-surface consistency**: for every route BOTH surfaces advertise, the auth flag
must agree. A real 200 that the manifest calls anonymous and the card calls gated is a
route an agent cannot plan around — it reads one document, omits the session, gets a
401, and cannot tell a wrong flag from a broken endpoint. That is the #1293 finding this
file grew for: the manifest filed all three ``/api/wallets/*`` routes under the anonymous
``auth`` group while every one of them sits behind ``require_current_user``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# Routes served by the separate Better Auth Node service, not by FastAPI.
_EXTERNALLY_SERVED_PREFIXES = ("/api/auth/",)

_ROUTE_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_CARD = _REPO_ROOT / "ui" / "public" / ".well-known" / "agent.json"


def _openapi_index() -> dict[str, set[str]]:
    """{path -> {METHOD, ...}} straight off the live app, no hand-kept mirror."""
    from archimedes.main import app

    return {path: {m.upper() for m in ops} for path, ops in app.openapi()["paths"].items()}


def unresolved_routes(routes: list[str], index: dict[str, set[str]]) -> list[str]:
    """Which of these ``"METHOD /path"`` strings do NOT exist on the app?

    Malformed strings count as unresolved: a route an agent cannot parse is as
    useless as one that 404s, and silently ignoring them would let a typo'd
    method sail through.
    """
    missing = []
    for route in routes:
        match = _ROUTE_RE.match(route.strip())
        if match is None:
            missing.append(route)
            continue
        method, path = match.group(1), match.group(2)
        if path.startswith(_EXTERNALLY_SERVED_PREFIXES):
            continue
        if method not in index.get(path, set()):
            missing.append(route)
    return missing


def auth_flag_by_route(endpoints: dict, flag_key: str) -> dict[str, bool]:
    """``{"METHOD /path" -> auth flag}`` for one discovery surface.

    ``auth_required`` / ``authRequired`` is a per-GROUP flag on both surfaces, so a
    route inherits the flag of the group it was filed under — which is exactly how
    the three ``/api/wallets/*`` routes came to be advertised as anonymous while the
    app 401s them (#1293). Non-dict members (the card's ``_note``) are skipped.

    A route advertised twice within ONE surface under conflicting flags is itself a
    contradiction, so it raises here rather than silently resolving last-wins.
    """
    flags: dict[str, bool] = {}
    for name, group in endpoints.items():
        if not isinstance(group, dict):
            continue
        required = group[flag_key]
        for route in group.get("routes", {}).values():
            if flags.setdefault(route, required) != required:
                raise AssertionError(f"{route} carries conflicting {flag_key} values (group {name!r})")
    return flags


def auth_flag_disagreements(manifest_endpoints: dict, card_endpoints: dict) -> dict[str, tuple[bool, bool]]:
    """``{route -> (manifest flag, card flag)}`` for every route both surfaces
    advertise and disagree about. Empty dict ⇒ the two surfaces are consistent."""
    manifest_flags = auth_flag_by_route(manifest_endpoints, "auth_required")
    card_flags = auth_flag_by_route(card_endpoints, "authRequired")
    return {
        route: (manifest_flags[route], card_flags[route])
        for route in sorted(manifest_flags.keys() & card_flags.keys())
        if manifest_flags[route] != card_flags[route]
    }


def _card() -> dict:
    return json.loads(_AGENT_CARD.read_text())


def _card_routes(card: dict) -> list[str]:
    return [
        route
        for group in card["endpoints"].values()
        if isinstance(group, dict)
        for route in group.get("routes", {}).values()
    ]


async def _manifest() -> dict:
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")
    assert resp.status_code == 200
    return resp.json()


# ── the exemption is narrow, and stays narrow ────────────────────────────────


def test_only_better_auth_is_exempt_from_route_resolution():
    """One exemption, spelled out. Widening this list disables the guard below."""
    assert _EXTERNALLY_SERVED_PREFIXES == ("/api/auth/",)


# ── manifest ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_manifest_routes_all_resolve_against_the_live_openapi():
    manifest = await _manifest()
    routes = [route for group in manifest["endpoints"].values() for route in group["routes"].values()]
    assert routes, "manifest advertised no routes at all"
    assert unresolved_routes(routes, _openapi_index()) == []


@pytest.mark.asyncio
async def test_manifest_advertises_the_live_paper_trading_group():
    """Paper trading is a simulated deployment path that works TODAY. Vault
    deploy is roadmap, not a second live execution path — an agent that cannot
    find ``paper`` has no shipped execute substitute."""
    manifest = await _manifest()
    paper = manifest["endpoints"]["paper"]
    assert paper["status"] == "live"
    assert paper["auth_required"] is True
    assert paper["routes"]["deploy"] == "POST /api/paper/deployments"
    assert paper["routes"]["stop"] == "POST /api/paper/deployments/{deployment_id}/stop"


@pytest.mark.asyncio
async def test_manifest_advertises_the_public_generation_quote():
    manifest = await _manifest()
    assert manifest["endpoints"]["generate"]["routes"]["quote"] == "GET /api/generate/quote"


@pytest.mark.asyncio
async def test_manifest_wallet_providers_match_the_accepted_literal_exactly():
    """Drift guard: the advertised set IS the set the endpoint accepts.

    Read off ``WalletChallengeRequest``'s Literal rather than restated, so a value
    added to one side and not the other fails here instead of in an agent's retry loop.
    """
    from archimedes.api.wallet_routes import WALLET_PROVIDERS

    manifest = await _manifest()
    assert manifest["auth"]["wallet_link_providers"] == list(WALLET_PROVIDERS)
    assert "headless" in WALLET_PROVIDERS
    assert manifest["auth"]["wallet_link_provider_default_for_agents"] == "headless"


# ── static agent card ────────────────────────────────────────────────────────


def test_agent_card_is_valid_json_with_an_endpoints_block():
    card = _card()
    assert card["name"] == "Archimedes"
    assert set(card["endpoints"]) >= {
        "read",
        "auth",
        "walletLink",
        "generate",
        "account",
        "rigor",
        "paper",
    }


def test_agent_card_routes_all_resolve_against_the_live_openapi():
    """The card is a static file with no CI-visible link to the API — which is
    precisely why it went stale (#1293 dogfood finding #6)."""
    routes = _card_routes(_card())
    assert routes, "agent card advertised no routes at all"
    assert unresolved_routes(routes, _openapi_index()) == []


def test_agent_card_covers_the_full_agent_journey():
    """auth -> wallet link -> quote -> generate -> paper deployment, end to end."""
    routes = set(_card_routes(_card()))
    for required in (
        "POST /api/auth/sign-in/email",
        "POST /api/wallets/challenge",
        "POST /api/wallets/verify",
        "GET /api/generate/quote",
        "POST /api/generate/start",
        "GET /api/generate/jobs/{job_id}/candidates",
        "POST /api/paper/deployments",
        "GET /api/paper/deployments/{deployment_id}",
    ):
        assert required in routes, f"agent card omits {required}"


def test_agent_card_wallet_providers_match_the_accepted_literal_exactly():
    from archimedes.api.wallet_routes import WALLET_PROVIDERS

    auth = _card()["authentication"]
    assert auth["walletLinkProviders"] == list(WALLET_PROVIDERS)
    assert auth["walletLinkProviderForAgents"] == "headless"
    # The worked example an agent will copy verbatim must use the honest value.
    assert _card()["endpoints"]["walletLink"]["challengeBody"]["provider"] == "headless"


# ── the two surfaces must not contradict each other ──────────────────────────


@pytest.mark.asyncio
async def test_shared_routes_carry_the_same_auth_flag_on_both_surfaces():
    """Cross-surface consistency: whether a call needs a session cannot depend on
    which discovery document the agent happened to read.

    An agent that reads "anonymous" on one surface and 401s at the call has no way
    to tell a broken endpoint from a wrong flag — it just retries. This is the
    guard for the manifest's original ``auth`` group, which advertised the three
    ``/api/wallets/*`` routes as unauthenticated while the card (correctly) had
    them behind ``walletLink: authRequired true``.
    """
    manifest = await _manifest()
    disagreements = auth_flag_disagreements(manifest["endpoints"], _card()["endpoints"])
    assert disagreements == {}, (
        f"manifest and agent card disagree on auth for {{route: (manifest, card)}}: {disagreements}"
    )


@pytest.mark.asyncio
async def test_the_two_surfaces_actually_share_routes():
    """Anti-vacuity for the check above: a comparison over an empty intersection
    passes for free, so an accidental rename of every route on one surface must
    not read as agreement."""
    manifest = await _manifest()
    manifest_flags = auth_flag_by_route(manifest["endpoints"], "auth_required")
    card_flags = auth_flag_by_route(_card()["endpoints"], "authRequired")
    shared = manifest_flags.keys() & card_flags.keys()
    assert len(shared) >= 10, f"only {len(shared)} shared routes — the consistency guard is near-vacuous"


@pytest.mark.asyncio
async def test_wallet_link_routes_are_advertised_as_authenticated_on_both_surfaces():
    """The specific #1293 finding, pinned. All three ``/api/wallets/*`` routes sit
    behind ``require_current_user``; neither surface may say otherwise."""
    manifest = await _manifest()
    manifest_flags = auth_flag_by_route(manifest["endpoints"], "auth_required")
    card_flags = auth_flag_by_route(_card()["endpoints"], "authRequired")
    for route in ("POST /api/wallets/challenge", "POST /api/wallets/verify", "GET /api/wallets"):
        assert manifest_flags[route] is True, f"manifest advertises {route} as anonymous"
        assert card_flags[route] is True, f"agent card advertises {route} as anonymous"


@pytest.mark.asyncio
async def test_public_strategy_readback_is_not_advertised_as_authenticated():
    """``GET /api/strategies/{strategy_id}`` takes no auth dependency — visibility is
    enforced per-row (private ⇒ 404, not 401), so an agent can read a public
    strategy's rigor verdict before it holds a session."""
    manifest = await _manifest()
    manifest_flags = auth_flag_by_route(manifest["endpoints"], "auth_required")
    assert manifest_flags["GET /api/strategies/{strategy_id}"] is False
    assert "strategy" in manifest["endpoints"]["read"]["routes"]


@pytest.mark.asyncio
async def test_cli_backing_endpoints_are_discoverable_and_gated():
    """``meter`` and ``verify`` (#1305) are live agent-relevant endpoints; an agent
    that cannot find them cannot check its own quota or pre-screen a returns series."""
    manifest = await _manifest()
    assert manifest["endpoints"]["account"]["routes"]["usage"] == "GET /api/account/usage"
    assert manifest["endpoints"]["account"]["auth_required"] is True
    assert manifest["endpoints"]["rigor"]["routes"]["verify"] == "POST /api/rigor/verify"
    assert manifest["endpoints"]["rigor"]["auth_required"] is True

    card_routes = set(_card_routes(_card()))
    assert "GET /api/account/usage" in card_routes
    assert "POST /api/rigor/verify" in card_routes


# ── guard demonstrations: the inputs that MUST be rejected ───────────────────


def test_route_checker_rejects_a_path_the_app_does_not_serve():
    """The failing input for the resolution guard: a plausible-looking 404."""
    index = _openapi_index()
    assert unresolved_routes(["POST /api/paper/deploy"], index) == ["POST /api/paper/deploy"]
    assert unresolved_routes(["GET /api/wallets/providers"], index) == ["GET /api/wallets/providers"]


def test_route_checker_rejects_a_real_path_under_the_wrong_method():
    """Right path, wrong verb — the failure mode a path-only check would miss."""
    index = _openapi_index()
    assert "/api/wallets/challenge" in index
    assert unresolved_routes(["GET /api/wallets/challenge"], index) == ["GET /api/wallets/challenge"]


def test_route_checker_rejects_a_malformed_route_string():
    index = _openapi_index()
    assert unresolved_routes(["/api/generate/quote"], index) == ["/api/generate/quote"]
    assert unresolved_routes(["FETCH /api/generate/quote"], index) == ["FETCH /api/generate/quote"]


def test_agent_card_with_a_fabricated_endpoint_fails_the_same_check():
    """Prove the card guard bites: inject a dead route into the real card shape."""
    card = _card()
    card["endpoints"]["paper"]["routes"]["bogus"] = "POST /api/paper/does-not-exist"
    assert unresolved_routes(_card_routes(card), _openapi_index()) == ["POST /api/paper/does-not-exist"]


@pytest.mark.asyncio
async def test_cross_surface_check_catches_a_flipped_auth_flag():
    """The failing input for the consistency guard: flip ONE flag on the real card
    and every route in that group must be reported. This is the regression the
    manifest actually shipped, reproduced against the live shapes."""
    manifest = await _manifest()
    card = _card()
    card["endpoints"]["walletLink"]["authRequired"] = False

    disagreements = auth_flag_disagreements(manifest["endpoints"], card["endpoints"])
    assert disagreements == {
        "GET /api/wallets": (True, False),
        "POST /api/wallets/challenge": (True, False),
        "POST /api/wallets/verify": (True, False),
    }


@pytest.mark.asyncio
async def test_cross_surface_check_catches_the_flip_in_the_other_direction():
    """Symmetry: a gated route mis-advertised as public is the dangerous direction,
    but a public route mis-advertised as gated wastes a sign-in too."""
    manifest = await _manifest()
    card = _card()
    card["endpoints"]["read"]["authRequired"] = True

    disagreements = auth_flag_disagreements(manifest["endpoints"], card["endpoints"])
    assert disagreements["GET /api/strategies/{strategy_id}"] == (False, True)
    assert disagreements["GET /api/health"] == (False, True)


def test_auth_flag_map_rejects_a_route_filed_under_two_conflicting_groups():
    """A surface that contradicts ITSELF must not resolve to whichever group was
    serialized last."""
    endpoints = {
        "auth": {"authRequired": False, "routes": {"list": "GET /api/wallets"}},
        "walletLink": {"authRequired": True, "routes": {"list": "GET /api/wallets"}},
    }
    with pytest.raises(AssertionError, match="conflicting authRequired"):
        auth_flag_by_route(endpoints, "authRequired")


def test_exemption_does_not_swallow_a_lookalike_prefix():
    """``/api/authz/...`` is not ``/api/auth/...`` — a prefix check must not slip."""
    assert unresolved_routes(["GET /api/authz/whoami"], _openapi_index()) == ["GET /api/authz/whoami"]
