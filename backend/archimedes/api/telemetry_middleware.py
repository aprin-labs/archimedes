"""Telemetry middleware — human-vs-agent request classifier (issue #428).

Classifies every inbound request as HUMAN or AGENT and increments the matching
Redis counter (see ``services/telemetry_store.py``). This is the app-layer MVP
of the hackathon win-condition instrument: a real, live "agents vs humans"
traction number backed by the live request path rather than a claim.

Classifier (deterministic — identity model as of today):
  - AGENT (internal) = a valid ``X-Internal-Agent-Key`` header (HMAC-compared
             against ``INTERNAL_AGENT_API_KEY``), agent_type="internal".
  - AGENT (keyed)    = the account identity was proved by a scoped API key
             (``Authorization: Bearer archim_…``), agent_type="keyed".
  - HUMAN  = Better Auth middleware resolved canonical account state from the
             session cookie.
  - AGENT (external) = no account identity AND a non-browser User-Agent (no
             "Mozilla"; matches curl / python-requests / boto / axios / *bot*),
             agent_type="external".
  - Default (browser UA, no session) = HUMAN — the demo is open, so an
    un-signed-in browser visitor still counts as a human.

Why ``keyed`` had to be added (issue #1653, finding 1)
------------------------------------------------------
Before the API-key lane, rule "session ⇒ human" fired before the User-Agent
heuristic, and every stage of the funnel past ``landed`` sits behind
``require_current_user``. So **no agent generation could ever be attributed to
an agent** — the moment a script signed in, it was counted as a human, and the
live funnel's ``external: 0`` at ``generation_started`` measured the absence of
the question, not the absence of agents.

A key is the first credential that says what the caller *is* rather than
guessing from a header a client chooses. So it is classified ahead of the
session rule, and it gets its own ``agent_type`` rather than being folded into
``external``: ``external`` means "unauthenticated non-browser, inferred from a
UA string", which is a different and much weaker claim than "authenticated by a
credential minted for machine use". Collapsing them would destroy the only
high-confidence agent signal we have. UA heuristics remain a courtesy label.

Adding a value to this vocabulary is a deliberate act with three other sites:
``services/funnel_store.AGENT_TYPES`` (which silently drops an unrecognised
type), ``models/telemetry.py``'s field description, and the docs. All four move
together or the breakdown quietly loses a population.

This module only reads request state resolved by auth middleware; it never changes auth.

Graceful degradation is a hard requirement: any classification or Redis error
is logged at ``debug`` and swallowed. The counter is observability, never a
gate — a telemetry fault must never turn a request into a 5xx.
"""

from __future__ import annotations

import hmac
import logging
import os
import re

from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Non-browser User-Agent markers. A request with no account session whose UA
# matches one of these (or carries no "Mozilla" token at all) is treated as an
# external agent. Browsers always send a "Mozilla/5.0 ..." UA, so its absence
# is a strong external-client signal.
_AGENT_UA_PATTERN = re.compile(
    r"curl|python-requests|httpx|aiohttp|go-http-client|boto|botocore|axios|"
    r"node-fetch|okhttp|java/|wget|libwww|scrapy|bot|spider|crawler",
    re.IGNORECASE,
)

# Paths whose traffic is infra/telemetry polling, not a real visit: the health
# probes (Docker/CI/CloudFront) and the metrics-READ endpoints (the dashboard
# polling *itself*). Excluding them from the counter stops the instrument from
# counting its own reads (issue #830). This changes WHICH requests increment, not
# the INCR semantics of the requests that do — a real page hit still counts once.
#
# NOTE: the exclusion is a READ exclusion. The metrics-read surfaces are only ever
# read via GET/HEAD, so we exclude those methods only. The one WRITE under
# ``/api/metrics`` — the POST ``/api/metrics/funnel/event`` beacon — is a real user
# action (a browser that rendered the page fired it) and MUST still count.
_COUNTER_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "/health",
    "/api/health",
    "/api/metrics",
)

# Methods that a metrics-read/health poll uses. Only these are excluded on the
# telemetry prefixes; a POST (the funnel/event beacon) always counts.
_READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})


def _is_counted_request(request: Request) -> bool:
    """False for infra/telemetry-poll READS + CORS preflights (issue #830).

    OPTIONS is a CORS preflight, never a user action → never counted. The excluded
    prefixes (health probes + metrics-read endpoints) are only skipped for READ
    methods (GET/HEAD); the POST ``/api/metrics/funnel/event`` beacon is a real
    user action and still counts. Everything else counts, exactly as before.
    """
    method = request.method
    if method == "OPTIONS":
        return False
    if method not in _READ_METHODS:
        # A write (e.g. the funnel/event beacon POST) is a real action → count it.
        return True
    path = request.url.path
    return not any(path == p or path.startswith(p + "/") for p in _COUNTER_EXCLUDED_PREFIXES)


def _is_browser_ua(user_agent: str) -> bool:
    """A real browser sends a ``Mozilla/...`` UA. Empty/non-Mozilla → not a browser."""
    return "mozilla" in user_agent.lower()


def _has_valid_internal_key(request: Request) -> bool:
    """True iff a valid ``X-Internal-Agent-Key`` is present (constant-time compare).

    Mirrors ``auth_guard.require_internal_agent_key`` semantics: fail-closed when
    the env key is unset (no key configured → no request can be "internal").
    """
    expected = os.getenv("INTERNAL_AGENT_API_KEY", "")
    provided = request.headers.get("X-Internal-Agent-Key", "")
    return bool(expected) and hmac.compare_digest(provided, expected)


def _has_valid_session(request: Request) -> bool:
    """True iff the identity middleware resolved canonical account state."""
    return getattr(request.state, "current_user", None) is not None


def _has_api_key_credential(request: Request) -> bool:
    """True iff that account state was proved by a scoped API key, not a cookie.

    Reads ``request.state.auth_credential``, set by the one identity chokepoint
    (``api/account_auth.py``). This is a *read* of a resolved fact — the header
    is not re-parsed and no verification is repeated here, so telemetry can
    never disagree with auth about who the caller is.
    """
    from archimedes.api.account_auth import CREDENTIAL_API_KEY

    return getattr(request.state, "auth_credential", None) == CREDENTIAL_API_KEY


def classify_request(request: Request) -> tuple[bool, str]:
    """Classify a request. Returns ``(is_agent, agent_type)``.

    ``agent_type`` is one of ``"internal"``, ``"keyed"``, ``"external"``, or
    ``"human"``. Deterministic and side-effect-free so it is trivially testable.
    """
    # 1. Internal agent — explicit, strongest signal.
    if _has_valid_internal_key(request):
        return True, "internal"

    # 2. Scoped API key — an account identity, proved by a machine credential.
    #    Ahead of rule 3 on purpose: the key is a stronger and more honest
    #    signal than the cookie's silence. See the module docstring.
    if _has_valid_session(request) and _has_api_key_credential(request):
        return True, "keyed"

    # 3. Human — canonical account session (cookie).
    if _has_valid_session(request):
        return False, "human"

    # 4. No account identity: a non-browser UA is an external agent/script.
    user_agent = request.headers.get("user-agent", "")
    if not _is_browser_ua(user_agent) or _AGENT_UA_PATTERN.search(user_agent):
        return True, "external"

    # 5. Default — browser UA, no session: an open-demo human.
    return False, "human"


async def telemetry_middleware(request: Request, call_next):
    """ASGI HTTP middleware: classify, count, tag the response.

    Sets ``request.state.is_agent`` / ``request.state.agent_type`` for any
    downstream consumer, increments the right Redis counter, and adds an
    ``X-Telemetry-Agent: true|false`` response header. Any error in
    classification or counting is swallowed (logged at debug) so telemetry can
    never break the request it is measuring.
    """
    is_agent = False
    try:
        is_agent, agent_type = classify_request(request)
        request.state.is_agent = is_agent
        request.state.agent_type = agent_type

        # Count every real request, but skip infra/telemetry-poll paths and CORS
        # preflights (issue #830) so the counter stops counting its own reads. The
        # request is still classified + tagged above; only the INCR is skipped.
        if _is_counted_request(request):
            # Lazy import keeps the store off the import-time critical path and
            # makes the boundary (the Redis client) easy to mock in tests.
            from archimedes.services.telemetry_store import TelemetryStore

            store = TelemetryStore()
            try:
                if is_agent:
                    await store.increment_agent()
                else:
                    await store.increment_human()
            finally:
                await store.close()

        # Visitor insights (#787, #830): geography + device are NOT recorded here.
        # Recording on every human-classified request leaked browser-UA bots
        # through the open-demo default and inflated the distinct-visitor count
        # (the 17-vs-50 discrepancy). The single source of truth for "distinct
        # visitor" is the JS-gated `landed` beacon (see api/metrics_routes.py
        # record_funnel_event), which shares the same archimedes_vid dedup key as
        # the funnel — so geo/device and the funnel `landed` count agree.
    except Exception as exc:
        # Fail-safe: never let telemetry raise into the request path.
        logger.debug("telemetry middleware classify/count failed: %s", exc)

    response: Response = await call_next(request)

    try:
        response.headers["X-Telemetry-Agent"] = "true" if is_agent else "false"
    except Exception as exc:
        logger.debug("telemetry middleware header set failed: %s", exc)

    return response
