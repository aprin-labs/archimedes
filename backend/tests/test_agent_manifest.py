"""Tests for GET /api/agent/manifest — the agent-discoverability manifest."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_agent_manifest_returns_200_with_expected_top_level_keys():
    """The manifest is a public GET returning the documented top-level contract."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")

    assert resp.status_code == 200
    data = resp.json()
    # "erc8004" joined this set in #1527 — the on-chain identity leg — and
    # "erc8004_verification" joined it with the live registry read that decides what the
    # first one may say. Their shapes, and the honesty invariants tying the claim to the
    # reading, live in test_erc8004_identity.py.
    assert set(data.keys()) == {
        "name",
        "blurb",
        "docs",
        "auth",
        "endpoints",
        "faucet",
        "erc8004",
        "erc8004_verification",
    }
    assert data["name"] == "Archimedes"


@pytest.mark.asyncio
async def test_agent_manifest_auth_scheme_is_canonical_account_plus_wallet_link():
    """Manifest separates Better Auth login from optional EIP-4361 wallet proof."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")

    data = resp.json()
    assert data["auth"]["scheme"] == "Better Auth session"
    assert data["auth"]["wallet_link_spec"] == "EIP-4361"
    assert "emailPassword" in data["auth"]["methods"]
    assert data["auth"]["chain_id"] == 5042002


def _assert_chain_id_is_a_well_shaped_scalar(value: object, expected: int) -> None:
    """The manifest's ``auth.chain_id`` must be a genuine scalar ``int`` equal to
    the configured chain — not a string, not a float, and not a bool (``bool`` is
    an ``int`` subclass in Python, so a bare ``isinstance(value, int)`` would wave
    ``True``/``False`` through). A wrong-shaped or wrong-valued chain_id sends an
    agent's on-chain calls to the wrong network with no signal until they revert.
    """
    if type(value) is not int:
        raise AssertionError(f"chain_id must be a plain int, got {type(value).__name__}: {value!r}")
    if value != expected:
        raise AssertionError(f"chain_id {value!r} does not match the configured chain {expected!r}")


@pytest.mark.asyncio
async def test_agent_manifest_chain_id_is_a_well_shaped_scalar_sourced_from_config():
    """chain_id must come from ARC_CHAIN_ID (the same env var wallet_routes and
    auth_siwe read for the identical check), not a separately hardcoded literal
    that could silently drift from what the rest of the backend enforces."""
    from archimedes.api.agent_manifest_routes import _CHAIN_ID
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")

    _assert_chain_id_is_a_well_shaped_scalar(resp.json()["auth"]["chain_id"], _CHAIN_ID)


def test_chain_id_guard_rejects_a_wrong_shaped_or_wrong_valued_input():
    """The failing inputs for the guard above: a stringly-typed chain_id (the
    literal shape a naive ``str(...)`` cast would produce), a bool masquerading
    as an int, a float, and a plain wrong value must all be rejected."""
    for bad in ("5042002", True, False, 5042002.0, 1):
        with pytest.raises(AssertionError):
            _assert_chain_id_is_a_well_shaped_scalar(bad, 5042002)
    # Sanity: the real shape and value pass.
    _assert_chain_id_is_a_well_shaped_scalar(5042002, 5042002)


def test_chain_id_is_read_from_arc_chain_id_env_not_a_frozen_literal(monkeypatch):
    """Mutation-check for the source-of-truth fix itself: chain_id must track
    ARC_CHAIN_ID (module-level, computed at import time — same pattern as
    ``wallet_routes._SUPPORTED_CHAIN_ID`` / ``auth_siwe._EXPECTED_CHAIN_ID``),
    not a value frozen into the manifest at whatever point someone hardcoded it.
    A bare literal would keep reporting the OLD chain forever after a real
    migration (e.g. the Sept 16 mainnet cutover) — the exact staleness class
    this work package exists to eliminate.
    """
    import importlib

    from archimedes.api import agent_manifest_routes

    monkeypatch.setenv("ARC_CHAIN_ID", "999999")
    importlib.reload(agent_manifest_routes)
    try:
        assert agent_manifest_routes._CHAIN_ID == 999999
    finally:
        monkeypatch.delenv("ARC_CHAIN_ID", raising=False)
        importlib.reload(agent_manifest_routes)  # restore module state for later tests


@pytest.mark.asyncio
async def test_agent_manifest_endpoint_groups_present():
    """All the documented endpoint groups are present, each with a status + routes."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")

    endpoints = resp.json()["endpoints"]
    expected_groups = {
        "read",
        "auth",
        "walletLink",
        "generate",
        "account",
        "rigor",
        "paper",
        "deploy",
        "marketplace",
        "monitor",
    }
    assert set(endpoints.keys()) == expected_groups
    for group in expected_groups:
        assert "status" in endpoints[group]
        assert "routes" in endpoints[group]
        assert "auth_required" in endpoints[group], f"{group} must state whether a session is needed"
        assert endpoints[group]["routes"], f"{group} routes must be non-empty"


@pytest.mark.asyncio
async def test_agent_manifest_shipped_groups_are_live_and_roadmap_groups_are_not():
    """Generate / rigor / paper are live. Deploy, marketplace, and monitor are
    not a public surface: marketplace is not shipped, and no user vault has
    ever been created. They must not still claim to be pending a T3.2 redeploy
    (#588 closed 2026-07-14), and they must not advertise ``live``."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")

    endpoints = resp.json()["endpoints"]
    for group in (
        "read",
        "auth",
        "walletLink",
        "generate",
        "account",
        "rigor",
        "paper",
    ):
        assert endpoints[group]["status"] == "live", f"{group} status is {endpoints[group]['status']!r}, not 'live'"
        assert "#588" not in endpoints[group]["status"]
        assert "pending" not in endpoints[group]["status"].lower()
    for group in ("deploy", "marketplace", "monitor"):
        assert endpoints[group]["status"] == "roadmap", (
            f"{group} status is {endpoints[group]['status']!r}, not 'roadmap' — "
            "marketplace is not a public surface and vault execute/monitor is not shipped"
        )
        assert "#588" not in endpoints[group]["status"]
        assert "pending" not in endpoints[group]["status"].lower()


@pytest.mark.asyncio
async def test_agent_manifest_includes_faucet_url():
    """The Circle faucet URL is surfaced for funding a test wallet."""
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")

    assert resp.json()["faucet"]["url"] == "https://faucet.circle.com/"
