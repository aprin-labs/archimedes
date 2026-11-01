"""Internal agent authentication guard.

Endpoints that trigger backend-signed on-chain actions or inject system events
must only be callable by the internal agent runner, not by arbitrary public callers.

Usage::

    from archimedes.api.auth_guard import require_internal_agent_key
    from fastapi import Depends

    @router.post("/protected")
    async def endpoint(_: None = Depends(require_internal_agent_key)):
        ...

Set INTERNAL_AGENT_API_KEY in the environment to a random 32-byte hex string
(generate with `openssl rand -hex 32`).

If the env var is unset, all requests are rejected (fail-closed). This avoids
a deploy-time misconfiguration silently leaving internal endpoints open.

User-facing endpoints (e.g. POST /api/vaults/create, POST /api/vaults/metadata)
are deliberately NOT behind this guard — they're signed-by-system on behalf of
a user who arrived via the UI, and the UI cannot carry the internal key. Use
slowapi rate limits for DoS protection on those endpoints instead. (Both of
those two are roadmap-gated as well: they answer 404 unless the server's
FEATURE_ROADMAP_SURFACES flag is on, #1432; see
``feature_flags.require_roadmap_surfaces``.)
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request


def require_internal_agent_key(request: Request) -> None:
    expected = os.getenv("INTERNAL_AGENT_API_KEY", "")
    provided = request.headers.get("X-Internal-Agent-Key", "")
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Forbidden")
