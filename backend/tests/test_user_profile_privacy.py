"""Tests for Issue #181 — user-data minimization.

Verifies:
  1. Email is encrypted at rest (Fernet round-trip)
  2. Log scrubber strips PII fields and profile answers
  3. GET /api/user/profile/{wallet} returns the profile only to its owner;
     every other caller gets the same 404 as a missing profile (#1908)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from archimedes.services.email_crypto import decrypt_email, encrypt_email
from archimedes.services.log_scrubber import scrub_profile

# ── Email encryption round-trip ─────────────────────────────────────────────


class TestEmailEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        plain = "user@example.com"
        token = encrypt_email(plain)
        assert token != plain, "Encrypted email must differ from plaintext"
        assert decrypt_email(token) == plain

    def test_encrypt_none_returns_none(self):
        assert encrypt_email(None) is None

    def test_decrypt_none_returns_none(self):
        assert decrypt_email(None) is None

    def test_encrypt_produces_different_tokens(self):
        """Each encryption call produces a different token (Fernet includes IV)."""
        plain = "same@email.com"
        t1 = encrypt_email(plain)
        t2 = encrypt_email(plain)
        assert t1 != t2, "Two encryptions of the same plaintext should differ"
        assert decrypt_email(t1) == plain
        assert decrypt_email(t2) == plain

    def test_decrypt_invalid_token_returns_garbage_or_raises(self):
        """Decrypting a non-token string should fail.

        Catching bare Exception is intentional: cryptography lib raises a
        mix of InvalidToken, ValueError, and TypeError depending on input
        shape; the test only cares that SOMETHING fails.
        """
        with pytest.raises(Exception):  # noqa: B017
            decrypt_email("not-a-valid-fernet-token")


# ── Log scrubber ─────────────────────────────────────────────────────────────


class TestLogScrubber:
    def test_scrubs_pii_fields(self):
        data = {
            "wallet_address": "0xabc",
            "email": "secret@example.com",
            "display_name": "Secret User",
            "marketing_opt_in": True,
            "interests": ["crypto"],
            "attribution": "Twitter",
        }
        scrubbed = scrub_profile(data)
        assert scrubbed["email"] == "<REDACTED>"
        assert scrubbed["display_name"] == "<REDACTED>"
        assert scrubbed["marketing_opt_in"] == "<REDACTED>"
        # Profile answers are owner-only too (#1908), so never logged.
        assert scrubbed["interests"] == "<REDACTED>"
        assert scrubbed["attribution"] == "<REDACTED>"
        # Only the wallet key passes through.
        assert scrubbed["wallet_address"] == "0xabc"

    def test_does_not_mutate_original(self):
        data = {"email": "test@test.com", "wallet_address": "0x1"}
        scrub_profile(data)
        assert data["email"] == "test@test.com", "Original dict must not be mutated"

    def test_scrubs_missing_fields_gracefully(self):
        data = {"wallet_address": "0xabc"}
        scrubbed = scrub_profile(data)
        assert scrubbed["wallet_address"] == "0xabc"
        assert "email" not in scrubbed


# ── Owner-only API echo (unit-level route tests) ────────────────────────────


class TestOwnerOnlyEcho:
    """Test that the GET endpoint hides the whole profile from non-owners
    and returns full data for the owner.
    """

    @pytest.fixture()
    def _mock_profile(self):
        """Create a mock UserProfile ORM object."""
        p = MagicMock()
        p.wallet_address = "0xowner123"
        p.display_name = "Owner Name"
        p.email = encrypt_email("owner@example.com")
        p.interests = json.dumps(["defi", "quant"])
        p.attribution = "Owner"
        p.marketing_opt_in = True
        p.owner_user_id = "owner-user"
        return p

    def test_non_owner_get_is_404_not_a_public_subset(self, _mock_profile):
        """#1908: a signed-in non-owner gets no profile fields at all.

        Interests and attribution used to be returned to any session as
        "public" fields; nothing reads them for another wallet, so the only
        safe answer is the same 404 a missing profile gets.
        """
        import asyncio

        from archimedes.api.user_routes import get_profile
        from fastapi import HTTPException

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = _mock_profile
        with (
            patch("archimedes.api.user_routes.get_session", return_value=mock_session),
            patch("archimedes.api.user_routes.get_current_user", return_value=MagicMock(id="someone-else")),
            patch("archimedes.api.user_routes._extract_linked_wallet", return_value="0xsomeoneelse"),
            pytest.raises(HTTPException) as exc,
        ):
            result = asyncio.run(get_profile("0xowner123", request=MagicMock()))
            pytest.fail(f"non-owner read returned a profile: {result!r}")
        assert exc.value.status_code == 404
        assert exc.value.detail == "Profile not found"

    def test_owner_get_decrypts_email(self, _mock_profile):
        """Owner caller gets decrypted email + full PII."""
        from archimedes.api.user_routes import _profile_to_response

        resp = _profile_to_response(_mock_profile)
        assert resp.email == "owner@example.com", "Email must be decrypted for owner"
        assert resp.display_name == "Owner Name"
        assert resp.marketing_opt_in is True

    def test_no_raw_email_in_logs(self, _mock_profile, caplog):
        """Verify log_scrubber is used in log output."""
        import logging

        with caplog.at_level(logging.INFO, logger="test_scrub"):
            data = {
                "wallet_address": "0xowner123",
                "email": "owner@example.com",
                "display_name": "Owner",
                "marketing_opt_in": True,
            }
            scrubbed = scrub_profile(data)
            logging.getLogger("test_scrub").info("profile=%s", scrubbed)

        assert "owner@example.com" not in caplog.text
        assert "<REDACTED>" in caplog.text


# ── Integration: upsert stores encrypted email ───────────────────────────────


class TestUpsertEncryption:
    def test_upsert_encrypts_email_before_storage(self):
        """POST /profile should encrypt the email field."""
        from archimedes.api.user_routes import upsert_profile
        from archimedes.api.user_schemas import UserProfileCreate

        payload = UserProfileCreate(
            wallet_address="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            email="test@encrypt.com",
            display_name="Test",
        )

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None

        with patch("archimedes.api.user_routes.get_session", return_value=mock_session):
            # Capture the UserProfile object passed to session.add
            added_obj = None

            def capture_add(obj):
                nonlocal added_obj
                added_obj = obj

            mock_session.add = capture_add
            import asyncio
            from unittest.mock import MagicMock as _MagicMock

            from starlette.requests import Request as StarletteRequest
            from starlette.responses import Response as StarletteResponse

            mock_req = _MagicMock(spec=StarletteRequest)
            mock_resp = _MagicMock(spec=StarletteResponse)
            # Profile write requires canonical account plus matching linked wallet.
            with (
                patch("archimedes.api.user_routes.require_current_user", return_value=_MagicMock(id="test-user")),
                patch(
                    "archimedes.api.user_routes._extract_linked_wallet",
                    return_value="0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                ),
            ):
                asyncio.run(upsert_profile(payload, request=mock_req, response=mock_resp))

        # The stored email must be encrypted (not plaintext)
        assert added_obj is not None
        assert added_obj.email != "test@encrypt.com", "Stored email must be encrypted"
        assert decrypt_email(added_obj.email) == "test@encrypt.com", "Must decrypt back to original"
