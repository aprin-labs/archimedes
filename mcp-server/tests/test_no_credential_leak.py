"""The credential must not appear in a log record, a tool result, or an exception string.

This is the adversarial file. A leak here is not a cosmetic defect: an MCP tool result is
rendered into the calling agent's transcript, which is persisted, often shipped to a model
provider, and sometimes shown on a screen. A bearer key in a tool result is a bearer key in
a chat log.

Every test runs the full matrix — both credential kinds, success and every mapped failure —
and searches *everything the caller can see* for the secret. The final test is the
adversarial control: it proves the search would actually find a leak if one existed, so a
green file means "no secret found", not "the search was looking in the wrong place".
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta

import httpx
import pytest
from archimedes_mcp import contract
from archimedes_mcp.credentials import resolve_credential
from archimedes_mcp.server import build_server
from conftest import json_response

# The verify tool refuses a series shorter than the minimum evaluation window
# BEFORE it builds an HTTP client (#1803), so a short fixture here would make
# every assertion in this file vacuous for that tool: no credential is resolved,
# nothing is attached to a request, and "the secret is not in what the caller
# sees" passes because there was never a request. Every body below must reach
# the wire. `_drive` asserts that it did.
_WINDOW = 250


def _rows(n: int = _WINDOW + 2) -> list[dict]:
    """`n` bars that satisfy every input rule the tool checks locally."""
    base = date(2026, 1, 1)
    return [{"date": (base + timedelta(days=i)).isoformat(), "daily_return": 0.001} for i in range(n)]


ARGUMENTS = {
    "archimedes_quote": {},
    "archimedes_usage": {},
    "archimedes_rigor_verify": {"returns": _rows(), "trials": 2},
    "archimedes_generate_start": {"intent": "low-vol USDC"},
    "archimedes_generate_status": {"job_id": "job-1"},
    "archimedes_strategy": {"strategy_id": "s1"},
    "archimedes_passport": {"strategy_id": "s1"},
    "archimedes_leaderboard": {"scope": "own"},
    "archimedes_corpus_search": {"search": "momentum"},
}

RESPONSES = {
    "200": lambda r: json_response(200, {"payment_required": True, "state": "done"}),
    "401": lambda r: json_response(401, {"detail": "Authentication required"}),
    "402": lambda r: json_response(
        402,
        {"detail": {"reason": "payment_required", "quote": {"price": "$2.000000"}}},
        headers={"PAYMENT-REQUIRED": "x402"},
    ),
    "409": lambda r: json_response(409, {"detail": {"reason": "wallet_link_required"}}),
    "422": lambda r: json_response(422, {"detail": [{"loc": ["body", "brief"], "msg": "bad"}]}),
    "429": lambda r: json_response(429, {"detail": {"reason": "generation_daily_cap", "scope": "user"}}),
    "500": lambda r: json_response(500, {"detail": "boom"}),
}


def _transport_error(request):
    # The URL is in the exception message; the credential must not be.
    raise httpx.ConnectError("connection refused")


def _drive(name, handler, mock_api, caplog):
    """Run one tool against one canned response and return everything the caller can see."""
    recorder = mock_api(handler)
    with caplog.at_level(logging.DEBUG):
        result = asyncio.run(build_server().call_tool(name, ARGUMENTS[name]))
    # Non-vacuity, per call, not once per file: a tool that refuses its arguments
    # locally never resolves a credential, so its leak assertions would pass while
    # proving nothing. This is how ARGUMENTS["archimedes_rigor_verify"] silently
    # stopped being covered when the 250-bar window landed (#1803).
    assert recorder.requests, (
        f"{name} never issued a request — ARGUMENTS[{name!r}] was refused by a local "
        "pre-check, so this run proves nothing about credential handling. Fix the fixture."
    )
    visible = [
        json.dumps(result.structured_content or {}),
        *[getattr(block, "text", "") for block in result.content],
        *[record.getMessage() for record in caplog.records],
        *[str(record.args) for record in caplog.records],
    ]
    return "\n".join(visible)


@pytest.mark.parametrize("name", contract.TOOL_NAMES)
@pytest.mark.parametrize("status", sorted(RESPONSES))
def test_api_key_never_appears_in_anything_the_caller_sees(name, status, mock_api, caplog, api_key):
    assert api_key not in _drive(name, RESPONSES[status], mock_api, caplog)


@pytest.mark.parametrize("name", contract.TOOL_NAMES)
@pytest.mark.parametrize("status", sorted(RESPONSES))
def test_session_token_never_appears_in_anything_the_caller_sees(name, status, mock_api, caplog, cached_session):
    assert cached_session not in _drive(name, RESPONSES[status], mock_api, caplog)


@pytest.mark.parametrize("name", contract.TOOL_NAMES)
def test_a_secure_prefixed_session_token_never_appears_either(name, mock_api, caplog, cached_secure_session):
    """Same guarantee, for a session cached against production (the ``__Secure-``-prefixed
    cookie). Not the full status matrix above — that redaction is already proven
    status-independent; this pins that it is also cookie-name-independent, on the one
    status (200) most likely to echo request state back into a result."""
    assert cached_secure_session not in _drive(name, RESPONSES["200"], mock_api, caplog)


@pytest.mark.parametrize("name", contract.TOOL_NAMES)
def test_no_credential_leaks_through_a_transport_error(name, mock_api, caplog, api_key):
    assert api_key not in _drive(name, _transport_error, mock_api, caplog)


def test_repr_and_str_of_a_credential_are_redacted(api_key, cached_session):
    """The blast radius of an accidental f-string or a traceback rendering locals."""
    credential = resolve_credential()
    for rendered in (repr(credential), str(credential), f"{credential}", f"{credential!r}"):
        assert api_key not in rendered
        assert "<redacted>" in rendered


def test_credential_is_absent_from_a_pytest_assertion_diff(api_key):
    """pytest renders the objects in a failing comparison. That render must be safe too."""
    credential = resolve_credential()
    with pytest.raises(AssertionError) as excinfo:
        assert credential == "something else"
    assert api_key not in str(excinfo.getrepr())


def test_the_leak_search_can_actually_find_a_leak(mock_api, caplog, api_key):
    """The control. Without this, every assertion above could be passing vacuously.

    A server that echoes the Authorization header back in its response body IS a leak the
    caller can see — the tool result carries the API's body verbatim. Proving the search
    catches that case is what makes the green above mean something.
    """

    def echoing_handler(request):
        return json_response(200, {"you_sent": request.headers.get("Authorization")})

    seen = _drive("archimedes_usage", echoing_handler, mock_api, caplog)
    assert api_key in seen, "the search is looking in the wrong place — fix the search, not the assertion"


def test_the_debug_log_carries_the_route_and_status_but_no_payload(mock_api, caplog, api_key):
    """What IS logged, pinned. An operator needs the route and the status to correlate a
    tool call with a server access log; the params, the body and the headers are none of
    the log's business."""
    mock_api(lambda r: json_response(402, {"detail": {"reason": "payment_required"}}))
    with caplog.at_level(logging.DEBUG, logger="archimedes_mcp.client"):
        asyncio.run(build_server().call_tool("archimedes_generate_start", {"intent": "SECRET-BRIEF-TEXT"}))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "POST /api/generate/start" in logged
    assert "402" in logged
    assert "SECRET-BRIEF-TEXT" not in logged
    assert api_key not in logged
