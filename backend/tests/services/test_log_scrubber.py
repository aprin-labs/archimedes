"""Unit coverage for the PII log scrubber.

`scrub_profile` is a pure, deterministic function — exhaustive
key-by-key tests cover every branch.
"""

from __future__ import annotations

from archimedes.services.log_scrubber import scrub_profile


def test_pii_fields_are_redacted() -> None:
    cleaned = scrub_profile(
        {
            "wallet_address": "0xabc",
            "email": "alice@example.com",
            "display_name": "Alice",
            "marketing_opt_in": True,
        }
    )
    assert cleaned["wallet_address"] == "0xabc"
    assert cleaned["email"] == "<REDACTED>"
    assert cleaned["display_name"] == "<REDACTED>"
    assert cleaned["marketing_opt_in"] == "<REDACTED>"


def test_profile_answers_are_redacted() -> None:
    """Interests and attribution are the user's own answers, owner-only (#1908)."""
    cleaned = scrub_profile(
        {
            "wallet_address": "0xabc",
            "interests": ["Bonds", "FX"],
            "attribution": "heard-from-a-friend",
        }
    )
    assert cleaned == {"wallet_address": "0xabc", "interests": "<REDACTED>", "attribution": "<REDACTED>"}


def test_non_pii_fields_pass_through_unchanged() -> None:
    cleaned = scrub_profile({"id": 42, "tier": "verified", "created_at": "2026-05-24"})
    assert cleaned == {"id": 42, "tier": "verified", "created_at": "2026-05-24"}


def test_empty_dict_returns_empty_dict() -> None:
    assert scrub_profile({}) == {}


def test_original_dict_is_not_mutated() -> None:
    original = {"email": "alice@example.com", "wallet_address": "0xabc"}
    _ = scrub_profile(original)
    # The scrubber returns a copy; original keeps the real email
    assert original["email"] == "alice@example.com"
