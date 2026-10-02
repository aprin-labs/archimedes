"""The per-client rate-limit key, shared by both backend limiters (#1908).

``api/limiter.py`` (slowapi, per-route rates) and ``services/generation_quota.py``
(the daily per-IP generation cap) key on the SAME value, from this one function,
so the two can never disagree about who a caller is.

Source of the address, trustworthy sources only:
  1. ``X-Real-IP``: set by nginx from its realip-resolved ``$remote_addr``. nginx
     OVERWRITES any client-supplied value, and realip trusts only the ALB's VPC
     CIDR and CloudFront's origin-facing ranges, so behind CloudFront this is the
     viewer's address (nginx/nginx.conf, "Real client IP behind CloudFront").
  2. the socket peer (``request.client.host``), for local and non-proxied runs.

``X-Forwarded-For`` is deliberately NOT read: its first hop is whatever the client
sent. Every candidate must parse as an IP address, so a malformed header cannot
mint arbitrary keys; with nothing usable the key is ``"unknown"``, one shared
bucket (still limited, just coarser).

IPv6 is keyed on the /64, not the address. A /64 is the smallest prefix a
subscriber line is normally given, and a host can use any address in it, so a
/128 key let one caller rotate through 2**64 fresh buckets. IPv4 stays the /32:
one address is what a caller actually gets. An IPv4-mapped IPv6 address
(``::ffff:203.0.113.7``) is that IPv4 caller and is keyed as the IPv4 address.
The /64 is the usual compromise, not a complete answer: a caller delegated a /56
still holds 256 /64s. Better Auth's limiter makes the same choice (its
``ipv6Subnet`` defaults to 64 in @better-auth/core ``normalizeIP``), so the
three per-IP limits agree.
"""

from __future__ import annotations

import ipaddress

from starlette.requests import Request

IPV6_KEY_PREFIX = 64


def rate_limit_key(value: str) -> str | None:
    """The bucket key for one address, or ``None`` if ``value`` is not an IP.

    IPv4 → the address (``"203.0.113.7"``). IPv6 → its /64 in CIDR form
    (``"2001:db8:1:2::/64"``). IPv4-mapped IPv6 → the IPv4 address.
    """
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        # Built from the integer, so a zone id (fe80::1%eth0) cannot leak into the key.
        mask = (2**128 - 1) ^ (2 ** (128 - IPV6_KEY_PREFIX) - 1)
        return str(ipaddress.IPv6Network((int(address) & mask, IPV6_KEY_PREFIX)))
    return str(address)


def client_ip(request: Request) -> str:
    """The caller's rate-limit key: X-Real-IP, else the socket peer, else ``"unknown"``.

    See the module docstring: IPv4 is the address, IPv6 is its /64.
    """
    candidates = (request.headers.get("x-real-ip"), request.client.host if request.client else None)
    for candidate in candidates:
        key = rate_limit_key(candidate or "")
        if key is not None:
            return key
    return "unknown"
