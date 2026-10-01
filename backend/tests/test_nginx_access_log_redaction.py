"""#1908: the nginx access log must not carry auth tokens (one of them is the user's email).

The finding: the base image's stock ``main`` log format prints ``$request`` (the
raw request line, query string included) and ``$http_referer`` to stdout, which
ships to CloudWatch ``/archimedes/nginx``. Better Auth's verification link is
``/api/auth/verify-email?token=<JWT>``, and that JWT is signed, not encrypted:
its payload is the account's email address (better-auth
dist/api/routes/email-verification.mjs ``createEmailVerificationToken``). The
reset link carries its token in the path (``/api/auth/reset-password/<token>``),
then lands on the SPA's ``/reset-password?token=…``, whose URL every follow-up
request sends back as its Referer.

The fix is a ``main_redacted`` log format fed by two maps (request URI, Referer)
with identical rules, declared on the server{} block so it REPLACES the stock
http-level ``access_log``. These tests pin:

* the format reads only the redacted variables, never the raw request/Referer;
* it is the server's access log, and nothing re-adds an http-level one;
* the map rules, executed (PCRE named groups translated to Python), redact
  every real Better Auth/SPA token URL shape and leave ordinary query strings
  (``/api/strategies?limit=5``, ``/api/swap/quote?token_in=…``) alone;
* the two maps stay in lockstep.

The same URLs were run through the built nginx image for the PR (RED on main:
token and email in the log; GREEN here: ``[REDACTED]``). Hermetic: no nginx.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

NGINX_CONF = Path(__file__).resolve().parents[2] / "nginx" / "nginx.conf"
FORMAT = "main_redacted"

EMAIL = "alice@example.com"
JWT = ".".join(
    [
        base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("="),
        base64.urlsafe_b64encode(
            json.dumps({"email": EMAIL, "updateTo": "alice.new@example.com", "exp": 1790003600}).encode()
        )
        .decode()
        .rstrip("="),
        "c2lnbmF0dXJlLXBsYWNlaG9sZGVy",
    ]
)
RESET = "Zk9xT3ZhbHVlLXJlc2V0LXRva2Vu"
SITE = "https://archimedes-arc.com"

# (raw request URI, what the access log must show). Every secret-bearing shape is a
# real one: Better Auth's URL builders (email-verification.mjs, password.mjs,
# update-user.mjs, the OAuth callback) and the SPA reset page AuthPage.jsx reads.
SENSITIVE_URIS = [
    (f"/api/auth/verify-email?token={JWT}&callbackURL=%2Fapp", "/api/auth/verify-email?[REDACTED]"),
    (
        f"/api/auth/reset-password/{RESET}?callbackURL=https%3A%2F%2Farchimedes-arc.com%2Freset-password",
        "/api/auth/reset-password/[REDACTED]",
    ),
    (f"/reset-password?token={RESET}", "/reset-password?[REDACTED]"),
    (f"/api/auth/delete-user/callback?token={RESET}&callbackURL=%2F", "/api/auth/delete-user/callback?[REDACTED]"),
    ("/api/auth/callback/google?code=4%2F0AbCdEfG&state=st8", "/api/auth/callback/google?[REDACTED]"),
    (f"/app?next=%2Fapp&access_token={RESET}", "/app?[REDACTED]"),
    (f"/reset-password?TOKEN={RESET}", "/reset-password?[REDACTED]"),
    (f"//api/auth/verify-email?token={JWT}", "//api/auth/verify-email?[REDACTED]"),
]
UNCHANGED_URIS = [
    "/api/strategies?limit=5&sort=new",
    "/api/swap/quote?token_in=0xabc&token_out=0xdef&amount_in=1",
    "/api/auth/get-session",
    "/api/auth/reset-password",  # the POST that submits the new password: token is in the body
    "/sign-in?next=/app/library",
    "/",
]


def _conf() -> str:
    return NGINX_CONF.read_text(encoding="utf-8")


def _map_entries(conf: str, source: str, target: str) -> list[tuple[str, str]]:
    block = re.search(rf"^map {re.escape(source)} {re.escape(target)} \{{(.*?)^\}}", conf, re.M | re.S)
    assert block, f"no `map {source} {target}` block in nginx.conf"
    entries = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r'("(?:[^"\\]|\\.)*"|\S+)\s+("(?:[^"\\]|\\.)*"|\S+);', line)
        assert m, f"unparsed map line: {line!r}"
        entries.append(tuple(part[1:-1] if part.startswith('"') else part for part in m.groups()))
    return entries


def _evaluate(entries: list[tuple[str, str]], value: str, variables: dict[str, str]) -> str:
    """nginx map semantics: exact keys first, then regexes in order; `$name` interpolated."""

    def interpolate(template: str, scope: dict[str, str]) -> str:
        return re.sub(r"\$([A-Za-z0-9_]+)", lambda m: scope[m.group(1)], template)

    exact = {k: v for k, v in entries if not k.startswith("~") and k != "default"}
    if value in exact:
        return interpolate(exact[value], variables)
    for key, result in entries:
        if not key.startswith("~"):
            continue
        flags = re.I if key.startswith("~*") else 0
        pattern = key[2:] if key.startswith("~*") else key[1:]
        m = re.search(pattern.replace("(?<", "(?P<").replace("(?P<=", "(?<=").replace("(?P<!", "(?<!"), value, flags)
        if m:
            return interpolate(result, {**variables, **m.groupdict()})
    return interpolate(dict(entries)["default"], variables)


def _logged_uri(uri: str) -> str:
    return _evaluate(_map_entries(_conf(), "$request_uri", "$log_request_uri"), uri, {"request_uri": uri})


def _logged_referer(referer: str) -> str:
    return _evaluate(_map_entries(_conf(), "$http_referer", "$log_http_referer"), referer, {"http_referer": referer})


@pytest.mark.parametrize(("uri", "expected"), SENSITIVE_URIS, ids=[u for u, _ in SENSITIVE_URIS])
def test_token_bearing_request_uris_are_redacted(uri: str, expected: str) -> None:
    logged = _logged_uri(uri)
    assert logged == expected
    assert JWT.split(".")[1] not in logged and RESET not in logged and "AbCdEfG" not in logged


@pytest.mark.parametrize(("uri", "expected"), SENSITIVE_URIS, ids=[u for u, _ in SENSITIVE_URIS])
def test_token_bearing_referers_are_redacted(uri: str, expected: str) -> None:
    assert _logged_referer(SITE + uri) == SITE + expected


@pytest.mark.parametrize("uri", UNCHANGED_URIS)
def test_ordinary_urls_are_logged_verbatim(uri: str) -> None:
    assert _logged_uri(uri) == uri
    assert _logged_referer(SITE + uri) == SITE + uri


def test_missing_referer_logs_a_dash_like_the_stock_format() -> None:
    assert _logged_referer("") == "-"


def test_the_jwt_fixture_really_carries_the_email() -> None:
    """Anti-vacuity: the token these tests redact is the shape that leaks the address."""
    payload = JWT.split(".")[1]
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert decoded["email"] == EMAIL
    assert any(JWT in uri for uri, _ in SENSITIVE_URIS)


def test_both_maps_apply_identical_rules() -> None:
    conf = _conf()

    def rules(source: str, target: str) -> list[tuple[str, str]]:
        # Capture names differ per map (nginx variables are global); normalise them away.
        out = []
        for key, value in _map_entries(conf, source, target):
            if key.startswith("~"):
                out.append((re.sub(r"\(\?<\w+>", "(?<_>", key), re.sub(r"\$\w+", "$_", value)))
        return out

    uri_rules = rules("$request_uri", "$log_request_uri")
    assert uri_rules, "no regex rules in the request-URI map"
    assert uri_rules == rules("$http_referer", "$log_http_referer")


def test_log_format_reads_only_the_redacted_variables() -> None:
    conf = _conf()
    m = re.search(rf"^log_format\s+{FORMAT}\s+(.*?);\s*$", conf, re.M | re.S)
    assert m, f"no `log_format {FORMAT}` in nginx.conf"
    body = m.group(1)
    assert "$log_request_uri" in body and "$log_http_referer" in body
    raw = re.findall(r"\$(request|request_uri|http_referer|args|query_string|arg_\w+|uri)(?![A-Za-z0-9_])", body)
    assert not raw, f"{FORMAT} still logs raw ${raw[0]}, which carries the auth tokens"


def test_server_access_log_uses_the_redacted_format_and_replaces_the_stock_one() -> None:
    conf = _conf()
    server = re.search(r"^server\s*\{", conf, re.M)
    assert server
    http_part, server_part = conf[: server.start()], conf[server.start() :]
    directives = lambda text: [ln.strip() for ln in text.splitlines() if ln.strip().startswith("access_log")]  # noqa: E731
    assert not directives(http_part), (
        "an http-level access_log in this conf.d fragment ADDS to the base image's `access_log … main`, "
        "so every request would also be logged unredacted"
    )
    server_logs = directives(server_part)
    assert server_logs[0] == f"access_log /var/log/nginx/access.log {FORMAT};"
    assert all(d == "access_log off;" for d in server_logs[1:]), server_logs
