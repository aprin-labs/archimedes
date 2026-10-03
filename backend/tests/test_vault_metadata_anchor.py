"""Tests for strategy passport on-chain anchoring via POST /api/vaults/metadata.

Validates that store_vault_metadata triggers strategy_publisher.anchor()
for each strategy_id, handles missing passports/hashes gracefully, and
survives anchor failures without breaking the DB write.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from archimedes.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_WALLET = "0x" + "a1" * 20


def _siwe_cookies(wallet: str = _WALLET) -> dict[str, str]:
    """A valid SIWE session cookie for `wallet` (the endpoints are now gated)."""
    return {_COOKIE_NAME: _sign_session(wallet.lower(), time.time())}


def _make_passport(sid: str, methodology_hash: str | None = "abcd1234" * 8):
    """Build a mock passport with the fields anchor() needs."""
    mock = MagicMock()
    mock.id = sid
    mock.methodology_hash = methodology_hash
    mock.regime_tag = "bull"
    paper = MagicMock()
    paper.arxiv_id = f"2301.{sid}"
    mock.papers = [paper]
    return mock


@contextlib.contextmanager
def _rigor_passes():
    """Stub the deploy gate to "this strategy is deployable".

    These tests exercise metadata anchoring, not the rigor gate. They previously
    passed only incidentally: the curated branch of ``_strategy_rigor_status`` read
    ``getattr(strat, "passes_rigor_gate", False)`` off a ``MagicMock`` passport,
    which is truthy for ANY attribute. Since #1173 that branch consults the live
    gate (a MagicMock passport has no persisted returns → ``pending`` → 422), so the
    intent has to be stated explicitly rather than ridden in on mock truthiness.
    """
    with patch("archimedes.api.vaults_routes._strategy_rigor_status", return_value=(True, True)):
        yield


class TestVaultMetadataAnchor:
    @patch("archimedes.api.vaults_routes.chain_executor.get_vault_owner", new_callable=AsyncMock)
    @patch("archimedes.api.vaults_routes.strategy_publisher")
    @patch("archimedes.api.vaults_routes.strategy_provider")
    @_rigor_passes()
    def test_metadata_post_calls_anchor_once_per_strategy_id(self, mock_provider, mock_publisher, mock_owner):
        # strategy_provider is now a lazily-cached accessor (strategy_provider()),
        # so the mocked callable's return_value stands in for the provider instance.
        mock_provider.return_value.get_strategy.side_effect = _make_passport
        mock_publisher.anchor = AsyncMock()
        mock_owner.return_value = _WALLET  # caller is the on-chain owner

        resp = client.post(
            "/api/vaults/metadata",
            json={
                "vault_address": "0x" + "ab" * 20,
                "name": "Test Vault",
                "symbol": "tVLT",
                "strategy_ids": ["s1", "s2", "s3"],
            },
            cookies=_siwe_cookies(),
        )
        assert resp.status_code == 200

        # Give the fire-and-forget task time to execute
        asyncio.run(asyncio.sleep(0.1))

        assert mock_publisher.anchor.call_count == 3

    @patch("archimedes.api.vaults_routes.chain_executor.get_vault_owner", new_callable=AsyncMock)
    @patch("archimedes.api.vaults_routes.strategy_publisher")
    @patch("archimedes.api.vaults_routes.strategy_provider")
    @_rigor_passes()
    def test_metadata_post_skips_passports_without_methodology_hash(self, mock_provider, mock_publisher, mock_owner):
        def get_strat(sid):
            if sid == "s2":
                return _make_passport(sid, methodology_hash=None)
            return _make_passport(sid)

        mock_provider.return_value.get_strategy.side_effect = get_strat
        mock_publisher.anchor = AsyncMock()
        mock_owner.return_value = _WALLET

        resp = client.post(
            "/api/vaults/metadata",
            json={
                "vault_address": "0x" + "cd" * 20,
                "name": "Test Vault 2",
                "symbol": "tVL2",
                "strategy_ids": ["s1", "s2", "s3"],
            },
            cookies=_siwe_cookies(),
        )
        assert resp.status_code == 200

        asyncio.run(asyncio.sleep(0.1))
        # s2 skipped due to no methodology_hash
        assert mock_publisher.anchor.call_count == 2

    @patch("archimedes.api.vaults_routes.chain_executor.get_vault_owner", new_callable=AsyncMock)
    @patch("archimedes.api.vaults_routes.strategy_publisher")
    @patch("archimedes.api.vaults_routes.strategy_provider")
    @_rigor_passes()
    def test_metadata_post_succeeds_when_anchor_raises(self, mock_provider, mock_publisher, mock_owner):
        mock_provider.return_value.get_strategy.side_effect = _make_passport
        mock_publisher.anchor = AsyncMock(side_effect=RuntimeError("simulated chain failure"))
        mock_owner.return_value = _WALLET

        resp = client.post(
            "/api/vaults/metadata",
            json={
                "vault_address": "0x" + "ef" * 20,
                "name": "Test Vault 3",
                "symbol": "tVL3",
                "strategy_ids": ["s1"],
            },
            cookies=_siwe_cookies(),
        )
        # Handler still returns 200 — anchor failure is non-fatal
        assert resp.status_code == 200

    @patch("archimedes.api.vaults_routes.chain_executor.get_vault_owner", new_callable=AsyncMock)
    @patch("archimedes.api.vaults_routes.strategy_publisher")
    @patch("archimedes.api.vaults_routes.strategy_provider")
    def test_metadata_post_with_unknown_strategy_id_is_refused(self, mock_provider, mock_publisher, mock_owner):
        # The metadata route is the client-signed deploy path's rigor choke point:
        # an unknown/unverified strategy id can no longer be bound to a vault. It is
        # cleanly refused with 422 (not a crash) BEFORE any anchoring is attempted —
        # this is the server-side "you can never fully bypass the rigor gate" guarantee.
        mock_provider.return_value.get_strategy.return_value = None
        mock_publisher.anchor = AsyncMock()
        mock_owner.return_value = _WALLET

        resp = client.post(
            "/api/vaults/metadata",
            json={
                "vault_address": "0x" + "11" * 20,
                "name": "Test Vault 4",
                "symbol": "tVL4",
                "strategy_ids": ["unknown_id"],
            },
            cookies=_siwe_cookies(),
        )
        assert resp.status_code == 422

        asyncio.run(asyncio.sleep(0.1))
        # Refused before persisting/anchoring — the unknown strategy is never anchored.
        mock_publisher.anchor.assert_not_called()


class TestVaultMetadataAuth:
    """Audit finding #8: /api/vaults/metadata triggers a backend-signed on-chain
    tx and was unauthenticated. It must require SIWE and be owner-scoped."""

    def test_metadata_post_requires_auth(self):
        resp = client.post(
            "/api/vaults/metadata",
            json={
                "vault_address": "0x" + "22" * 20,
                "name": "Unauthed",
                "symbol": "tUNA",
                "strategy_ids": [],
            },
        )
        assert resp.status_code == 401

    @patch("archimedes.api.vaults_routes.chain_executor.get_vault_owner", new_callable=AsyncMock)
    def test_metadata_post_rejects_non_owner_overwrite(self, mock_owner):
        vault = "0x" + "33" * 20
        owner = "0x" + "aa" * 20
        attacker = "0x" + "bb" * 20
        mock_owner.return_value = owner  # the vault's on-chain owner

        # Owner claims the vault metadata first.
        first = client.post(
            "/api/vaults/metadata",
            json={"vault_address": vault, "name": "Owned", "symbol": "tOWN", "strategy_ids": []},
            cookies=_siwe_cookies(owner),
        )
        assert first.status_code == 200

        # A different authenticated wallet cannot overwrite it.
        attack = client.post(
            "/api/vaults/metadata",
            json={"vault_address": vault, "name": "Hijacked", "symbol": "tHJK", "strategy_ids": []},
            cookies=_siwe_cookies(attacker),
        )
        assert attack.status_code == 403

        # The owner can still edit their own metadata.
        again = client.post(
            "/api/vaults/metadata",
            json={"vault_address": vault, "name": "Owned v2", "symbol": "tOWN", "strategy_ids": []},
            cookies=_siwe_cookies(owner),
        )
        assert again.status_code == 200

    @patch("archimedes.api.vaults_routes.chain_executor.get_vault_owner", new_callable=AsyncMock)
    def test_metadata_post_rejects_non_onchain_owner_on_first_write(self, mock_owner):
        """#916 IDOR: a vault created directly on-chain (no metadata row yet)
        must not have its metadata claimed by a wallet that isn't its on-chain
        owner. Before the fix, the first authenticated writer won."""
        vault = "0x" + "44" * 20
        real_owner = "0x" + "cc" * 20
        attacker = "0x" + "dd" * 20
        mock_owner.return_value = real_owner

        resp = client.post(
            "/api/vaults/metadata",
            json={"vault_address": vault, "name": "Claimed", "symbol": "tCLM", "strategy_ids": []},
            cookies=_siwe_cookies(attacker),
        )
        assert resp.status_code == 403

        # The real on-chain owner can claim it.
        ok = client.post(
            "/api/vaults/metadata",
            json={"vault_address": vault, "name": "Mine", "symbol": "tMIN", "strategy_ids": []},
            cookies=_siwe_cookies(real_owner),
        )
        assert ok.status_code == 200

    @patch("archimedes.api.vaults_routes.chain_executor.get_vault_owner", new_callable=AsyncMock)
    def test_metadata_post_fails_closed_when_owner_unreadable(self, mock_owner):
        """If the on-chain owner can't be read, refuse (503) rather than let an
        unverifiable caller write — never fail open on the ownership gate."""
        mock_owner.return_value = None
        resp = client.post(
            "/api/vaults/metadata",
            json={"vault_address": "0x" + "55" * 20, "name": "X", "symbol": "tX", "strategy_ids": []},
            cookies=_siwe_cookies("0x" + "ee" * 20),
        )
        assert resp.status_code == 503


def test_create_vault_requires_auth(monkeypatch):
    """Vault creation spends the backend signer's gas → must be SIWE-gated.

    With the roadmap flag on: while it is off, the route's auth check never
    runs. The gate answers 404 first, unless FastAPI refuses a body sent with
    a JSON content type before the gate (422 ``json_invalid`` for malformed
    JSON, 400 for bytes that do not decode as text); a body with any other
    content type, or none, gets the 404 (#1432,
    test_vault_create_roadmap_gate.py)."""
    monkeypatch.setenv("FEATURE_ROADMAP_SURFACES", "true")
    resp = client.post(
        "/api/vaults/create",
        json={
            "name": "Unauthed Vault",
            "symbol": "tUAV",
            "management_fee_bps": 100,
            "performance_fee_bps": 1000,
            "agent_assisted": True,
            "strategy_ids": [],
        },
    )
    assert resp.status_code == 401
