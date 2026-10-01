"""Log scrubber — strip PII fields before logging.

All logger calls that touch user profile data MUST route through this module
to prevent accidental PII leakage in log output.
"""

from __future__ import annotations

from typing import Any

# Fields that must never appear in logs: personal data and consent (email,
# display_name, marketing_opt_in) and the user's own welcome-question answers
# (interests, attribution), which are owner-only too (#1908).
_PII_FIELDS = frozenset({"email", "display_name", "marketing_opt_in", "interests", "attribution"})


def scrub_profile(profile_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *profile_dict* with PII fields and profile answers redacted.

    Redacted fields are replaced with ``"<REDACTED>"`` so log consumers
    can see that a value *was* present without seeing the value itself.
    """
    return {k: ("<REDACTED>" if k in _PII_FIELDS else v) for k, v in profile_dict.items()}


def sanitize_log_value(value: Any) -> str:
    """Neutralize CR/LF in a user-controlled value before it is logged.

    Guards against log forging (CWE-117): an attacker-supplied value containing
    newline or carriage-return characters could otherwise inject forged log
    lines (fake entries, spoofed levels). Both characters are replaced with a
    single space so the original value stays legible. Any value type is
    accepted and coerced to ``str``.
    """
    return str(value).replace("\r", " ").replace("\n", " ")
