"""The HTTP boundary. One client factory, one request function, nothing else.

This module is the entire reach of the MCP server: an ``httpx.Client`` pointed at one
base URL. There is no database session, no Redis handle, no chain RPC, no import of
``archimedes.*``. If a capability is not in the public HTTP API, this server does not have
it — that is the anti-goal the design was scoped around, and keeping the surface to one
file is what makes it checkable by reading rather than by trusting.

``_http_client`` is the single construction point, copied in spirit from
``cli/src/archimedes_cli/cli.py``: tests monkeypatch this factory to return a client wired
to an ``httpx.MockTransport``, so every HTTP test in this distribution is hermetic without
patching the internals of each tool.

``follow_redirects=False``, deliberately and for the same reason the CLI gives: the API
never redirects, and a compromised or misconfigured endpoint must not be able to bounce a
request carrying a bearer token or a session cookie to another host.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import errors
from .credentials import Credential

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30.0
"""Longer than the CLI's 10s: ``POST /api/generate/start`` does admission control, quota
reads and a payment check before answering, and a client timeout there is indistinguishable
to the caller from a refusal — the one ambiguity this server exists to remove."""

USER_AGENT = "archimedes-mcp"
"""A non-browser User-Agent, sent on every call. Costs nothing and makes the traffic
legible to the telemetry classifier, per ``docs/agent-quickstart.md``'s conventions. Note
what it does NOT do: once a credential resolves to an account, the classifier scores the
caller ``human`` regardless of this string (``api/telemetry_middleware.py``, rule 2 before
rule 3), so this is honesty about the client, not an attribution mechanism."""


def _http_client(api_url: str, credential: Credential | None) -> httpx.Client:
    """The one place an ``httpx.Client`` is constructed. Mock this, not the tools.

    Exactly one credential goes on the wire. ``auth_headers()`` and ``auth_cookies()`` are
    mutually exclusive by construction, so an API-key call sends no ``Cookie`` and a
    cookie call sends no ``Authorization`` — the client never presents two identities and
    lets the server pick.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    cookies: dict[str, str] = {}
    if credential is not None:
        headers.update(credential.auth_headers())
        cookies.update(credential.auth_cookies())
    return httpx.Client(
        base_url=api_url,
        headers=headers,
        cookies=cookies,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
    )


def request(
    method: str,
    path: str,
    *,
    api_url: str,
    credential: Credential | None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Make one call and return either ``{"ok": True, ...}`` or a structured failure.

    Never raises and never logs the request. The log line below carries the method, the
    path and the status — the three things an operator needs to correlate a tool call with
    a server access log — and deliberately not the params, the body, or any header, because
    a credential lives in the headers and a brief may be private.
    """
    try:
        with _http_client(api_url, credential) as client:
            response = client.request(method, path, params=params, json=json_body)
    except httpx.HTTPError as exc:
        logger.debug("archimedes-mcp %s %s -> transport error", method, path)
        return errors.from_transport_error(exc, api_url)

    logger.debug("archimedes-mcp %s %s -> %s", method, path, response.status_code)

    if not response.is_success:
        return errors.from_response(response, credential_kind=credential.kind if credential else None)

    try:
        body = response.json()
    except ValueError:
        return errors.failure(
            "malformed_response",
            f"{method} {path} returned {response.status_code} with a body that is not JSON.",
            "Not caller-fixable. Report it with the route and status.",
            http_status=response.status_code,
        )

    if isinstance(body, dict):
        return errors.ok(body)
    # A list response (the corpus catalog's older shape, say) still has to arrive under a
    # key rather than being reshaped into a dict that pretends to be the API's own object.
    return errors.ok({"result": body})


__all__ = ["TIMEOUT_SECONDS", "USER_AGENT", "request"]
