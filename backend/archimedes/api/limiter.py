"""Shared rate limiter instance for the Archimedes API.

Extracted from ``main.py`` to avoid circular imports — route modules need the
``limiter`` object to decorate endpoints, but ``main.py`` imports those same
route modules. By defining the limiter here, both can import it without cycles.

Usage in route files::

    from archimedes.api.limiter import limiter

    @router.post("/heavy-endpoint")
    @limiter.limit("5/minute")
    async def heavy_endpoint(request: Request):
        ...

The limiter is Redis-backed when ``REDIS_URL`` is available (production / ASG),
falling back to in-memory storage for local dev and CI.
"""

import logging
import os

from slowapi import Limiter

from archimedes.services.client_ip import client_ip

logger = logging.getLogger(__name__)


_redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/1")
# Was REDIS_URL explicitly provided? If so, a fallback to memory:// is a
# production misconfiguration worth a loud warning; if not, it's expected local dev.
_redis_explicit = "REDIS_URL" in os.environ

# Try Redis first; fall back to in-memory when unavailable.
try:
    import redis as _redis

    _r = _redis.Redis.from_url(_redis_url)
    _r.ping()
    _storage_uri = _redis_url
except Exception as exc:
    _storage_uri = "memory://"
    if _redis_explicit:
        logger.warning(
            "Rate limiter: REDIS_URL is set but unreachable (%s) — falling back to "
            "per-process memory:// storage. With N Uvicorn/Gunicorn workers the "
            "effective rate limit becomes N× the configured value (each worker counts "
            "independently). Restore Redis connectivity to enforce a shared, correct limit.",
            exc,
        )
    else:
        logger.info("Rate limiter: REDIS_URL not set — using in-memory storage (local/dev/CI).")

# Key on the REAL client IP, not slowapi's get_remote_address. Behind nginx,
# request.client.host is the nginx peer (10.x inside the container), so
# get_remote_address collapsed every caller into one shared bucket — per-IP
# limits were effectively global. client_ip (services/client_ip.py, shared with
# the daily generation cap) reads the nginx-set X-Real-IP header (it overwrites
# any client-supplied value and is bound to the ALB CIDR + CloudFront
# origin-facing ranges, so it is the viewer's address, #1908). It deliberately
# does NOT trust X-Forwarded-For, whose first hop is spoofable. IPv6 callers are
# keyed on their /64, so one cannot dodge the limit by rotating addresses.
limiter = Limiter(
    key_func=client_ip,
    storage_uri=_storage_uri,
    default_limits=["60/minute"],  # default for undecorated routes
    headers_enabled=True,  # X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset
    enabled=not os.getenv("TESTING"),  # disable in pytest
)
