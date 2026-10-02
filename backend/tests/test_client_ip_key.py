"""#1908: IPv6 callers are keyed on their /64 by both backend limiters.

Before #1908 nginx resolved every request to the CloudFront edge, so the per-IP
keys were shared by everyone behind one edge. Now X-Real-IP is the viewer, and
for an IPv6 viewer that is a full /128. A host can use any address in its /64,
so keying on the /128 handed one caller 2**64 fresh buckets for the slowapi
limits and for the daily generation cap. Both now key on one shared function,
``archimedes.services.client_ip.client_ip``: IPv4 by address, IPv6 by /64.

Hermetic: the key functions are called on fake requests, and the generation-cap
path runs against a mocked Redis whose INCR keys are read back.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from archimedes.api.limiter import limiter
from archimedes.services import client_ip as shared
from archimedes.services.generation_quota import GenerationQuota, client_ip, enforce_generation_quota
from starlette.requests import Request

# Two addresses in one /64 (2001:db8:1:2::/64), written differently on purpose:
# compressed, fully expanded, upper case.
SAME_64 = ("2001:db8:1:2::1", "2001:0DB8:0001:0002:ffff:ffff:ffff:fffe")
OTHER_64 = "2001:db8:1:3::1"


def _req(x_real_ip: str | None = None, peer: str | None = "10.0.0.2"):
    headers = {"x-real-ip": x_real_ip} if x_real_ip is not None else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer) if peer else None)


KEYERS = {
    "slowapi limiter (api/limiter.py)": limiter._key_func,
    "daily generation cap (services/generation_quota.py)": client_ip,
}


def test_both_limiters_use_the_one_shared_key_function() -> None:
    assert limiter._key_func is shared.client_ip
    assert client_ip is shared.client_ip


@pytest.mark.parametrize("key", KEYERS.values(), ids=KEYERS.keys())
def test_two_addresses_in_one_ipv6_64_share_a_bucket(key) -> None:
    a, b = (key(_req(x_real_ip=ip)) for ip in SAME_64)
    assert a == b, (
        f"{SAME_64[0]} and {SAME_64[1]} are one /64 but got keys {a!r} and {b!r}: "
        "one IPv6 caller can rotate addresses inside its /64 to dodge the limit"
    )
    assert a == "2001:db8:1:2::/64"


@pytest.mark.parametrize("key", KEYERS.values(), ids=KEYERS.keys())
def test_different_ipv6_64s_get_different_buckets(key) -> None:
    assert key(_req(x_real_ip=SAME_64[0])) != key(_req(x_real_ip=OTHER_64))
    assert key(_req(x_real_ip=OTHER_64)) == "2001:db8:1:3::/64"


@pytest.mark.parametrize("key", KEYERS.values(), ids=KEYERS.keys())
def test_ipv4_is_still_keyed_on_the_address(key) -> None:
    assert key(_req(x_real_ip="203.0.113.7")) == "203.0.113.7"
    assert key(_req(x_real_ip="203.0.113.8")) == "203.0.113.8"
    # Same /24, different callers: IPv4 is never widened.
    assert key(_req(x_real_ip="203.0.113.7")) != key(_req(x_real_ip="203.0.113.8"))


@pytest.mark.parametrize("key", KEYERS.values(), ids=KEYERS.keys())
def test_ipv4_mapped_ipv6_is_the_ipv4_caller(key) -> None:
    # Not ::/64, which would lump every mapped IPv4 caller into one bucket.
    assert key(_req(x_real_ip="::ffff:203.0.113.7")) == "203.0.113.7"
    assert key(_req(x_real_ip="::ffff:203.0.113.7")) != key(_req(x_real_ip="::ffff:203.0.113.8"))


@pytest.mark.parametrize("key", KEYERS.values(), ids=KEYERS.keys())
def test_the_socket_peer_fallback_is_keyed_the_same_way(key) -> None:
    assert key(_req(x_real_ip=None, peer="2001:db8:1:2::99")) == "2001:db8:1:2::/64"
    assert key(_req(x_real_ip=None, peer="127.0.0.1")) == "127.0.0.1"


@pytest.mark.parametrize("key", KEYERS.values(), ids=KEYERS.keys())
def test_unparsable_values_never_become_keys(key) -> None:
    assert key(_req(x_real_ip="evil; DROP", peer="10.0.0.1")) == "10.0.0.1"
    assert key(_req(x_real_ip="2001:db8::/64", peer="10.0.0.1")) == "10.0.0.1"
    assert key(_req(x_real_ip="not-an-ip", peer=None)) == "unknown"
    # A zone id is accepted by ipaddress but must not reach the key.
    assert key(_req(x_real_ip="fe80::1%eth0")) == "fe80::/64"


def _starlette_req(headers: dict[str, str], peer: str = "10.0.0.2") -> Request:
    """A real starlette Request, so header lookups are case-insensitive as in production."""
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "client": (peer, 40000)})


@pytest.mark.parametrize("key", KEYERS.values(), ids=KEYERS.keys())
def test_a_forged_x_forwarded_for_is_never_the_key(key) -> None:
    """X-Forwarded-For is never read: its leftmost hops are whatever the client sent.

    nginx forwards it with ``$proxy_add_x_forwarded_for``, so the backend sees the
    client's own entries first. X-Real-IP (set, not appended, by nginx) is the key;
    without it, the socket peer is, never the forwarded header.
    """
    forged = "198.51.100.66, 2001:db8:dead::1"
    assert key(_starlette_req({"X-Forwarded-For": forged, "X-Real-IP": "203.0.113.7"})) == "203.0.113.7"
    assert key(_starlette_req({"X-Forwarded-For": forged, "X-Real-IP": "2001:db8:1:2::1"})) == "2001:db8:1:2::/64"
    # No X-Real-IP: the socket peer, and a different forged header buys no new bucket.
    assert key(_starlette_req({"X-Forwarded-For": forged}, peer="10.0.0.9")) == "10.0.0.9"
    assert key(_starlette_req({"X-Forwarded-For": "192.0.2.1"}, peer="10.0.0.9")) == "10.0.0.9"


async def test_generation_cap_counts_one_ipv6_64_in_one_redis_bucket(monkeypatch) -> None:
    """End to end through enforce_generation_quota: the INCR key, not just the helper."""
    monkeypatch.setenv("GENERATION_DAILY_CAP_PER_USER", "0")  # isolate the IP layer
    monkeypatch.setenv("GENERATION_DAILY_CAP_PER_IP", "20")
    redis = MagicMock()
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    quota = GenerationQuota()
    quota._get_redis = AsyncMock(return_value=redis)
    monkeypatch.setattr("archimedes.services.generation_quota.GenerationQuota", lambda *a, **k: quota)

    for ip in (*SAME_64, OTHER_64, "203.0.113.7"):
        await enforce_generation_quota(_req(x_real_ip=ip), "user-abc")
    # Key shape: archimedes:genquota:ip:<day>:<identity>, and an IPv6 identity has colons.
    identities = [c.args[0].split(":", 4)[4] for c in redis.incr.await_args_list]
    assert identities == ["2001:db8:1:2::/64", "2001:db8:1:2::/64", "2001:db8:1:3::/64", "203.0.113.7"]
