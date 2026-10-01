"""Session-dependent nginx responses must carry ``Cache-Control: private, no-store``.

2026-09-01 sign-in outage: CloudFront's ``html`` cache policy (infra/cloudfront.tf)
keys the cache WITHOUT cookies and caches for 60s. The gated ``/app/*`` locations
returned an anonymous visitor's ``302 /sign-in`` with no ``Cache-Control``, so
CloudFront cached it and replayed it to the very next *signed-in* request. The
SPA then ping-ponged: AuthPage saw a valid session and jumped to ``/app/...``,
CloudFront answered with the cached anonymous 302, and so on — 165 get-session
calls in 45 minutes for one user. ``min_ttl = 0`` means an origin ``no-store``
is honoured, which is what this test pins.

Rule: every ``location`` that gates on ``auth_request /_auth_session``, and the
``@sign_in`` redirect they fall back to, must set ``add_header Cache-Control
"private, no-store" always;``. MUTATION: delete the header from any one of
those blocks → this test goes red naming the block.
"""

from __future__ import annotations

import re
from pathlib import Path

NGINX_CONF = Path(__file__).resolve().parents[2] / "nginx" / "nginx.conf"
_NO_STORE = re.compile(r'add_header\s+Cache-Control\s+"private,\s*no-store"\s+always;')


def _location_blocks(conf: str) -> dict[str, str]:
    """Map ``location <selector>`` → block body (brace-balanced, no nesting needed here)."""
    blocks: dict[str, str] = {}
    for m in re.finditer(r"^\s*location\s+([^{]+?)\s*\{", conf, re.M):
        depth, i = 0, m.end() - 1
        while i < len(conf):
            if conf[i] == "{":
                depth += 1
            elif conf[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks[m.group(1).strip()] = conf[m.end() : i]
    return blocks


def test_every_session_gated_location_and_the_sign_in_redirect_are_uncacheable() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    blocks = _location_blocks(conf)
    gated = [sel for sel, body in blocks.items() if "auth_request /_auth_session" in body]
    assert gated, "no auth_request-gated locations found — the parser or the gate moved"
    assert "@sign_in" in blocks, "the @sign_in redirect location is gone"
    missing = [sel for sel in [*gated, "@sign_in"] if not _NO_STORE.search(blocks[sel])]
    assert not missing, (
        "session-dependent nginx responses without Cache-Control private,no-store — "
        "CloudFront will cache an anonymous 302 and replay it to signed-in users: " + ", ".join(missing)
    )


def test_the_parser_sees_the_gate() -> None:
    """Anti-vacuity: the gated set is the two /app locations, not an empty list."""
    conf = NGINX_CONF.read_text(encoding="utf-8")
    gated = sorted(sel for sel, body in _location_blocks(conf).items() if "auth_request /_auth_session" in body)
    assert gated == ["= /app/insights", "^~ /app"], gated
