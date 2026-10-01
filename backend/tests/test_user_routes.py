"""Tests for user profile API — WelcomeProfileModal backend.

Auth model (Issue #181 + Issue #402):
  - POST requires a SIWE session matching payload.wallet_address.
    The X-Wallet-Address header fallback was dropped per #402 (forgeable).
  - GET strips PII when caller lacks SIWE session matching the queried wallet.
  - GET with a valid SIWE session for the queried wallet returns full data.
  - Email is encrypted at rest before storage.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import patch

import pytest
from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from archimedes.db import get_session
from archimedes.models.user_profile import UserProfile
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

# Unique wallets per test to avoid slowapi rate-limit collisions.
_W_ALICE = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
_W_BOB = "0x2222222222222222222222222222222222222222"
_W_CHARLIE = "0x3333333333333333333333333333333333333333"
_W_UPDATE = "0x4444444444444444444444444444444444444444"
_W_MINIMAL = "0x0000000000000000000000000000000000000002"
_W_CASE = "0x5555555555555555555555555555555555555555"
_W_ROTATED_KEY = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
_W_CANONICAL_PROFILE = "0x6666666666666666666666666666666666666666"
_W_LEGACY_PROFILE = "0x7777777777777777777777777777777777777777"


def _siwe_cookies(wallet: str) -> dict[str, str]:
    """Build a valid SIWE session cookie for `wallet`.

    PII reads (display_name, email) require a SIWE session — header alone is
    no longer trusted. Tests that previously relied on X-Wallet-Address to
    read owner-view PII must now establish a session via this helper.
    """
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


@pytest.fixture
def client():
    """Create a test client with a fresh in-memory DB.

    Disables rate limiting so tests don't hit 429s from rapid
    sequential requests to the same endpoint from the same IP.
    """
    from archimedes.db import engine
    from archimedes.models.chat import Base

    Base.metadata.create_all(bind=engine)

    # Patch the limiter decorator to be a no-op during tests.
    with patch("archimedes.api.user_routes.limiter") as mock_limiter:
        mock_limiter.limit = lambda *a, **kw: lambda f: f
        from archimedes.main import app

        tc = TestClient(app)
        yield tc


class TestUserProfileRoutes:
    """GET and POST /api/user/profile."""

    def test_get_profile_returns_404_for_unknown_wallet(self, client):
        """Unknown wallet should return 404."""
        res = client.get(
            "/api/user/profile/0x0000000000000000000000000000000000000001",
            cookies=_siwe_cookies(_W_ALICE),
        )
        assert res.status_code == 404

    def test_create_profile_with_display_name(self, client):
        """POST creates a profile and returns it (POST always returns owner view)."""
        res = client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_ALICE,
                "display_name": "Alice",
                "marketing_opt_in": False,
            },
            cookies=_siwe_cookies(_W_ALICE),
        )
        assert res.status_code == 200
        data = res.json()
        assert data["display_name"] == "Alice"
        assert data["wallet_address"] == _W_ALICE.lower()

    def test_get_profile_after_create_owner(self, client):
        """GET with SIWE session returns full profile (PII included) after POST."""
        client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_BOB,
                "display_name": "Bob",
            },
            cookies=_siwe_cookies(_W_BOB),
        )
        # GET with SIWE session cookie → full data including PII
        res = client.get(
            f"/api/user/profile/{_W_BOB}",
            cookies=_siwe_cookies(_W_BOB),
        )
        assert res.status_code == 200
        assert res.json()["display_name"] == "Bob"

    def test_get_profile_without_account_is_rejected(self, client):
        """Profile API is behind canonical account boundary."""
        client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_BOB,
                "display_name": "Bob",
            },
            cookies=_siwe_cookies(_W_BOB),
        )
        res = client.get(f"/api/user/profile/{_W_BOB}")
        assert res.status_code == 401

    def test_create_profile_with_all_fields(self, client):
        """POST with every field populated. POST returns owner view (full data)."""
        res = client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_CHARLIE,
                "display_name": "Charlie",
                "email": "charlie@example.com",
                "interests": ["Equities", "Crypto"],
                "attribution": "Twitter",
                "marketing_opt_in": True,
            },
            cookies=_siwe_cookies(_W_CHARLIE),
        )
        assert res.status_code == 200
        data = res.json()
        assert data["display_name"] == "Charlie"
        assert data["email"] == "charlie@example.com"
        assert data["interests"] == ["Equities", "Crypto"]
        assert data["attribution"] == "Twitter"
        assert data["marketing_opt_in"] is True

    def test_email_stored_encrypted(self, client):
        """Verify email is encrypted in DB, not plaintext (Issue #181)."""
        email = "encrypt-test@example.com"
        client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_CHARLIE,
                "email": email,
            },
            cookies=_siwe_cookies(_W_CHARLIE),
        )
        # Read directly from DB
        session = get_session()
        try:
            profile = session.query(UserProfile).filter(UserProfile.wallet_address == _W_CHARLIE.lower()).first()
            assert profile is not None
            assert profile.email != email, "Email must not be stored as plaintext"
            assert len(profile.email) > len(email), "Encrypted token must be longer"
        finally:
            session.close()

    def test_update_existing_profile(self, client):
        """POST to an existing profile updates fields."""
        client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_UPDATE,
                "display_name": "Original",
            },
            cookies=_siwe_cookies(_W_UPDATE),
        )
        res = client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_UPDATE,
                "display_name": "Updated",
                "marketing_opt_in": True,
            },
            cookies=_siwe_cookies(_W_UPDATE),
        )
        assert res.status_code == 200
        assert res.json()["display_name"] == "Updated"
        assert res.json()["marketing_opt_in"] is True

    def test_create_profile_minimal_fields(self, client):
        """POST with only wallet address (all optional fields null)."""
        res = client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_MINIMAL,
            },
            cookies=_siwe_cookies(_W_MINIMAL),
        )
        assert res.status_code == 200
        data = res.json()
        assert data["display_name"] is None
        assert data["email"] is None
        assert data["marketing_opt_in"] is False

    def test_invalid_wallet_address_rejected(self, client):
        """POST with invalid wallet address returns validation error."""
        res = client.post(
            "/api/user/profile",
            json={
                "wallet_address": "not-a-wallet",
                "display_name": "Test",
            },
            cookies=_siwe_cookies(_W_ALICE),
        )
        assert res.status_code == 422

    def test_get_profile_case_insensitive(self, client):
        """GET finds profile regardless of wallet case (with SIWE session for PII)."""
        client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_CASE,
                "display_name": "CaseTest",
            },
            cookies=_siwe_cookies(_W_CASE),
        )
        # Query with lowercase + SIWE session
        res = client.get(
            f"/api/user/profile/{_W_CASE.lower()}",
            cookies=_siwe_cookies(_W_CASE),
        )
        assert res.status_code == 200
        assert res.json()["display_name"] == "CaseTest"
        # Query with original mixed case + SIWE session
        res2 = client.get(
            f"/api/user/profile/{_W_CASE}",
            cookies=_siwe_cookies(_W_CASE),
        )
        assert res2.status_code == 200
        assert res2.json()["display_name"] == "CaseTest"

    def test_anonymous_get_does_not_reveal_email(self, client):
        """Anonymous GET cannot reach private profile data."""
        client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_CHARLIE,
                "email": "secret@example.com",
            },
            cookies=_siwe_cookies(_W_CHARLIE),
        )
        res = client.get(f"/api/user/profile/{_W_CHARLIE}")
        assert res.status_code == 401

    def test_owner_get_decrypts_email(self, client):
        """Owner GET with SIWE session decrypts email (Issue #181 + SIWE auth)."""
        client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_CHARLIE,
                "email": "owner@example.com",
            },
            cookies=_siwe_cookies(_W_CHARLIE),
        )
        res = client.get(
            f"/api/user/profile/{_W_CHARLIE}",
            cookies=_siwe_cookies(_W_CHARLIE),
        )
        assert res.status_code == 200
        assert res.json()["email"] == "owner@example.com"

    def test_owner_get_survives_undecryptable_email(self, client, caplog):
        """GET /api/user/profile must not 500 when stored ciphertext can't be
        decrypted with the current EMAIL_ENCRYPTION_KEY (e.g. after a key
        rotation leaves old tokens unreadable).

        Asserts: HTTP 200, email field is null, and a warning is logged
        (without leaking the ciphertext or any decrypted value).
        """
        client.post(
            "/api/user/profile",
            json={
                "wallet_address": _W_ROTATED_KEY,
                "display_name": "RotatedKeyUser",
                "email": "rotated@example.com",
            },
            cookies=_siwe_cookies(_W_ROTATED_KEY),
        )

        # Simulate a key rotation: overwrite the stored ciphertext with a
        # token encrypted under a DIFFERENT Fernet key. This is
        # well-formed Fernet ciphertext (so it parses) but raises
        # InvalidToken when decrypted with the app's actual key.
        other_key_token = Fernet(Fernet.generate_key()).encrypt(b"rotated@example.com").decode("utf-8")
        session = get_session()
        try:
            profile = session.query(UserProfile).filter(UserProfile.wallet_address == _W_ROTATED_KEY.lower()).first()
            assert profile is not None
            profile.email = other_key_token
            session.commit()
        finally:
            session.close()

        with caplog.at_level(logging.WARNING, logger="archimedes.api.user_routes"):
            res = client.get(
                f"/api/user/profile/{_W_ROTATED_KEY}",
                cookies=_siwe_cookies(_W_ROTATED_KEY),
            )

        assert res.status_code == 200
        data = res.json()
        assert data["email"] is None, "Undecryptable email must degrade to null, not 500"
        assert data["display_name"] == "RotatedKeyUser", "Non-PII-encrypted fields must still be returned"

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("decrypt_email failed" in r.getMessage() for r in warnings), (
            "Expected a warning log for the failed decrypt"
        )
        for r in warnings:
            msg = r.getMessage()
            assert other_key_token not in msg, "Warning must not leak ciphertext"
            assert "rotated@example.com" not in msg, "Warning must not leak decrypted value"
            assert _W_ROTATED_KEY.lower() in msg, "Warning should identify the affected wallet"

    def test_header_spoof_does_not_reveal_pii(self, client):
        """X-Wallet-Address header alone must NOT reveal PII — SIWE session required."""
        wallet = "0x6666666666666666666666666666666666666666"
        client.post(
            "/api/user/profile",
            json={"wallet_address": wallet, "email": "private@example.com", "display_name": "Secret"},
            cookies=_siwe_cookies(wallet),
        )
        # Spoof with header only (no SIWE cookie)
        res = client.get(f"/api/user/profile/{wallet}", headers={"X-Wallet-Address": wallet})
        assert res.status_code == 401

    def test_wrong_siwe_session_does_not_reveal_pii(self, client):
        """SIWE session for wallet A must not reveal wallet B's PII."""
        wallet_a = "0x7777777777777777777777777777777777777777"
        wallet_b = "0x8888888888888888888888888888888888888888"
        client.post(
            "/api/user/profile",
            json={"wallet_address": wallet_a, "email": "a@example.com"},
            cookies=_siwe_cookies(wallet_a),
        )
        # Session is for wallet_b, trying to read wallet_a: answered like a
        # missing profile (#1908), so nothing of wallet_a's comes back.
        res = client.get(f"/api/user/profile/{wallet_a}", cookies=_siwe_cookies(wallet_b))
        assert res.status_code == 404
        assert "a@example.com" not in res.text, "Wrong wallet session must not reveal PII"

    def test_write_without_any_auth_fails(self, client):
        """POST without account auth returns 401."""
        res = client.post(
            "/api/user/profile",
            json={"wallet_address": "0x9999999999999999999999999999999999999999"},
        )
        assert res.status_code == 401

    def test_write_with_header_only_fails(self, client):
        """Body/header wallet values never create account authentication."""
        wallet = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        res = client.post(
            "/api/user/profile",
            json={"wallet_address": wallet, "display_name": "HeaderOnly"},
            headers={"X-Wallet-Address": wallet},
        )
        assert res.status_code == 401, "Header-only auth must be rejected"

    def test_write_with_wrong_siwe_session_fails(self, client):
        """POST with SIWE session for wallet A claiming to write wallet B returns 403."""
        session_wallet = "0xcccccccccccccccccccccccccccccccccccccccc"
        payload_wallet = "0xdddddddddddddddddddddddddddddddddddddddd"
        res = client.post(
            "/api/user/profile",
            json={"wallet_address": payload_wallet, "display_name": "Imposter"},
            cookies=_siwe_cookies(session_wallet),
        )
        assert res.status_code == 403, "Session wallet must match payload wallet"
        assert "does not match" in res.json().get("detail", "")

    def test_write_with_siwe_session_succeeds(self, client):
        """POST with a valid SIWE session (no header) should succeed."""
        wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        res = client.post(
            "/api/user/profile",
            json={"wallet_address": wallet, "display_name": "SIWEUser"},
            cookies=_siwe_cookies(wallet),
        )
        assert res.status_code == 200
        assert res.json()["display_name"] == "SIWEUser"

    def test_second_legacy_profile_returns_conflict_instead_of_500(self, client):
        user_id = f"legacy-test:{_W_LEGACY_PROFILE.lower()}"
        with get_session() as session:
            session.query(UserProfile).filter(
                (UserProfile.owner_user_id == user_id)
                | (UserProfile.wallet_address.in_([_W_CANONICAL_PROFILE.lower(), _W_LEGACY_PROFILE.lower()]))
            ).delete(synchronize_session=False)
            session.add_all(
                [
                    UserProfile(
                        wallet_address=_W_CANONICAL_PROFILE.lower(),
                        owner_user_id=user_id,
                        display_name="Canonical",
                        interests="[]",
                    ),
                    UserProfile(
                        wallet_address=_W_LEGACY_PROFILE.lower(),
                        display_name="Legacy",
                        interests="[]",
                    ),
                ]
            )
            session.commit()

        res = client.post(
            "/api/user/profile",
            json={"wallet_address": _W_LEGACY_PROFILE, "display_name": "Updated"},
            cookies=_siwe_cookies(_W_LEGACY_PROFILE),
        )
        assert res.status_code == 409
        assert res.json()["detail"] == "Account already has a canonical profile"

        with get_session() as session:
            session.query(UserProfile).filter(UserProfile.wallet_address == _W_LEGACY_PROFILE.lower()).delete()
            session.commit()
        res = client.post(
            "/api/user/profile",
            json={"wallet_address": _W_LEGACY_PROFILE, "display_name": "New second profile"},
            cookies=_siwe_cookies(_W_LEGACY_PROFILE),
        )
        assert res.status_code == 409
        assert res.json()["detail"] == "Account already has a canonical profile"


# ── #1908: profile answers are owner-only ────────────────────────────────────
#
# The profile row holds what a user typed into WelcomeProfileModal: display
# name, email, interests, "how did you hear about us" (attribution) and the
# marketing opt-in. None of it is meant for other accounts, and no caller
# reads another wallet's profile, so a non-owner must learn nothing — not even
# that a profile exists. Non-owner reads therefore answer exactly like a
# missing profile (404), never with a "public subset".

_W_1908_OWNER = "0x1908000000000000000000000000000000000001"
_W_1908_OTHER = "0x1908000000000000000000000000000000000002"
_W_1908_MISSING = "0x1908000000000000000000000000000000000003"
_W_1908_LEGACY = "0x1908000000000000000000000000000000000004"
_PRIVATE_1908 = {
    "display_name": "Owner1908",
    "email": "owner1908@example.com",
    "interests": ["Bonds", "FX"],
    "attribution": "heard-from-a-friend-1908",
    "marketing_opt_in": True,
}


def _private_values_in(body: str) -> list[str]:
    """Every private profile answer that appears anywhere in a response body."""
    values = [
        _PRIVATE_1908["display_name"],
        _PRIVATE_1908["email"],
        _PRIVATE_1908["attribution"],
        *_PRIVATE_1908["interests"],
    ]
    return [v for v in values if v in body]


class TestProfileReadIsOwnerOnly:
    """GET /api/user/profile/{wallet} returns profile answers only to the owner (#1908)."""

    @pytest.fixture
    def owned_profile(self, client):
        res = client.post(
            "/api/user/profile",
            json={"wallet_address": _W_1908_OWNER, **_PRIVATE_1908},
            cookies=_siwe_cookies(_W_1908_OWNER),
        )
        assert res.status_code == 200, res.text

    def test_non_owner_gets_no_private_fields(self, client, owned_profile):
        res = client.get(f"/api/user/profile/{_W_1908_OWNER}", cookies=_siwe_cookies(_W_1908_OTHER))
        assert _private_values_in(res.text) == [], f"non-owner read leaked profile answers: {res.text}"
        assert res.status_code == 404

    def test_non_owner_response_is_indistinguishable_from_missing_profile(self, client, owned_profile):
        """No existence oracle: someone else's profile looks exactly like no profile."""
        other = client.get(f"/api/user/profile/{_W_1908_OWNER}", cookies=_siwe_cookies(_W_1908_OTHER))
        missing = client.get(f"/api/user/profile/{_W_1908_MISSING}", cookies=_siwe_cookies(_W_1908_OTHER))
        assert (other.status_code, other.json()) == (missing.status_code, missing.json())

    def test_owner_still_sees_every_field(self, client, owned_profile):
        res = client.get(f"/api/user/profile/{_W_1908_OWNER}", cookies=_siwe_cookies(_W_1908_OWNER))
        assert res.status_code == 200
        assert res.json() == {"wallet_address": _W_1908_OWNER.lower(), **_PRIVATE_1908}

    def test_unauthenticated_is_still_401(self, client, owned_profile):
        res = client.get(f"/api/user/profile/{_W_1908_OWNER}")
        assert res.status_code == 401
        assert _private_values_in(res.text) == []

    def test_unclaimed_legacy_profile_is_owner_only(self, client):
        """Pre-account rows (owner_user_id NULL) are readable only via the matching linked wallet."""
        with get_session() as session:
            session.query(UserProfile).filter(UserProfile.wallet_address == _W_1908_LEGACY.lower()).delete()
            session.add(
                UserProfile(
                    wallet_address=_W_1908_LEGACY.lower(),
                    owner_user_id=None,
                    display_name=_PRIVATE_1908["display_name"],
                    interests='["Bonds", "FX"]',
                    attribution=_PRIVATE_1908["attribution"],
                )
            )
            session.commit()

        other = client.get(f"/api/user/profile/{_W_1908_LEGACY}", cookies=_siwe_cookies(_W_1908_OTHER))
        assert _private_values_in(other.text) == [], f"non-owner read leaked legacy answers: {other.text}"
        assert other.status_code == 404

        owner = client.get(f"/api/user/profile/{_W_1908_LEGACY}", cookies=_siwe_cookies(_W_1908_LEGACY))
        assert owner.status_code == 200
        assert owner.json()["interests"] == ["Bonds", "FX"]
        assert owner.json()["attribution"] == _PRIVATE_1908["attribution"]
