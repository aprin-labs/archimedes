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
* every server{} block declares it once at server level, or ``off`` (parsed by
  block, not by position in the file, and each server{} is its own context),
  nothing re-adds an http-level one, and no location logs with any other format
  (``access_log <path>;`` with no format means the unredacted ``combined``);
* limit_req rejections are logged below the stock error_log level, because each
  error-log entry carries the raw request line and Referer;
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


def _directives_in_context(
    conf: str, blocks: list[tuple[str, ...]] | None = None
) -> list[tuple[tuple[str, ...], list[str]]]:
    """Every directive in the fragment with the block path it sits in.

    ``((), ["limit_req_zone", ...])`` is http level (this file is spliced into the
    stock ``http {}``); ``(("server[1]",), [...])`` is server level in the first
    ``server {}`` block; ``(("server[1]", "location /api/auth/"), [...])`` is inside
    that location. Each ``server {}`` is numbered in file order, so two server
    blocks are two contexts: one cannot inherit the other's directives. A small
    nginx tokenizer: quotes, backslash escapes, ``#`` comments, and the
    ``${NGINX_*}`` envsubst placeholders the image renders at boot (a placeholder
    that stands alone, like ``${NGINX_RESOLVER_LINE}``, is dropped). ``blocks``,
    when given, collects the path of every block opened, even one with no
    directives in it.
    """
    out: list[tuple[tuple[str, ...], list[str]]] = []
    stack: list[str] = []
    words: list[str] = []
    servers = 0
    i, n = 0, len(conf)
    while i < n:
        c = conf[i]
        if c.isspace():
            i += 1
        elif c == "#":
            while i < n and conf[i] != "\n":
                i += 1
        elif c == ";":
            out.append((tuple(stack), words))
            words, i = [], i + 1
        elif c == "{":
            header = " ".join(words)
            if header == "server":
                servers += 1
                header = f"server[{servers}]"
            stack.append(header)
            if blocks is not None:
                blocks.append(tuple(stack))
            words, i = [], i + 1
        elif c == "}":
            assert not words, f"unterminated directive before '}}': {words}"
            stack.pop()
            i += 1
        else:
            word = []
            while i < n and not conf[i].isspace() and conf[i] not in ";{}":
                if conf[i] in "\"'":
                    quote, j = conf[i], i + 1
                    while conf[j] != quote:
                        j += 2 if conf[j] == "\\" else 1
                    word.append(conf[i + 1 : j])
                    i = j + 1
                elif conf.startswith("${", i):
                    j = conf.index("}", i)
                    word.append(conf[i : j + 1])
                    i = j + 1
                else:
                    word.append(conf[i])
                    i += 1
            token = "".join(word)
            if not (not words and re.fullmatch(r"\$\{\w+\}", token)):
                words.append(token)
    assert not stack, f"unclosed block(s): {stack}"
    return out


def test_the_tokenizer_sees_the_whole_server_block() -> None:
    """Anti-vacuity for the two tests below: the parse reaches every location."""
    directives = _directives_in_context(_conf())
    locations = {ctx[1] for ctx, _ in directives if len(ctx) >= 2 and ctx[0] == "server[1]"}
    assert {"location /api/auth/", "location = /nginx-health", "location /", "location ^~ /app"} <= locations
    assert ((), ["limit_req_zone", "$binary_remote_addr", "zone=api_write:10m", "rate=20r/m"]) in directives
    assert [ctx for ctx, args in directives if args[0] == "server_name"] == [("server[1]",)]
    # A second server{} is a context of its own, not merged into the first.
    two = _directives_in_context("server { listen 1; } server { listen 2; }")
    assert two == [(("server[1]",), ["listen", "1"]), (("server[2]",), ["listen", "2"])]


def test_server_access_log_uses_the_redacted_format_and_replaces_the_stock_one() -> None:
    """Every server{} declares its own redacted access_log (or ``off``); nothing logs unredacted.

    Server level, not merely "first in the file": an access_log inside one
    location applies to that location only, and every other location would keep
    inheriting the stock http-level ``access_log … main``. The same goes for a
    second ``server {}`` block: one with no access_log of its own inherits the
    stock one, whatever the first server declares. Inside a location the only
    acceptable forms are ``off`` and this same format; ``access_log <path>;`` with
    no format means ``combined``, which prints the raw request and Referer.
    """
    blocks: list[tuple[str, ...]] = []
    directives = _directives_in_context(_conf(), blocks)
    logs = [(ctx, args) for ctx, args in directives if args[0] == "access_log"]
    assert not [args for ctx, args in logs if ctx == ()], (
        "an http-level access_log in this conf.d fragment ADDS to the base image's `access_log … main`, "
        "so every request would also be logged unredacted"
    )
    redacted = ["access_log", "/var/log/nginx/access.log", FORMAT]
    servers = [block[0] for block in blocks if len(block) == 1 and block[0].startswith("server[")]
    assert servers, "no server{} block parsed"
    for server in servers:
        declared = [args for ctx, args in logs if ctx == (server,)]
        assert declared in ([redacted], [["access_log", "off"]]), (
            f"{server} declares {declared or 'no access_log'} at server level: every server{{}} must declare "
            "the redacted access_log once (or `off`), so it replaces the stock one for every location it serves"
        )
    # The production server (server_name archimedes-arc.com) keeps its log, redacted.
    production = [ctx for ctx, args in directives if args[:2] == ["server_name", "archimedes-arc.com"]]
    assert len(production) == 1, production
    assert [args for ctx, args in logs if ctx == production[0]] == [redacted], (
        f"the production server {production[0][0]} must log, redacted, not turn its access log off"
    )
    nested = [(ctx, args) for ctx, args in logs if len(ctx) > 1]
    for ctx, args in nested:
        assert args[1] == "off" or (len(args) >= 3 and args[2] == FORMAT), (
            f"`{' '.join(args)}` in `{ctx[-1]}` logs that location with "
            f"{args[2] if len(args) >= 3 else 'the default `combined`'} format, which prints the raw "
            "request line and Referer"
        )
    assert [ctx[-1] for ctx, _ in nested] == ["location = /nginx-health"]


def test_limit_req_rejections_stay_out_of_the_error_log() -> None:
    """A limit_req rejection is an error-log entry carrying the raw request line and Referer.

    The stock image's error_log is ``notice``; rejections logged at ``info`` fall
    below it (checked in the built 1.31.2 image for the PR). Every location that
    applies ``limit_req`` must therefore resolve ``limit_req_log_level`` to
    ``info`` through location → server → http inheritance, and nothing in this
    fragment may lower an ``error_log`` to ``info``/``debug``, which would bring
    the entries back.
    """
    directives = _directives_in_context(_conf())

    def effective(ctx: tuple[str, ...]) -> str | None:
        for depth in range(len(ctx), -1, -1):
            found = [args[1] for c, args in directives if c == ctx[:depth] and args[0] == "limit_req_log_level"]
            if found:
                return found[-1]
        return None  # nginx default: error

    limited = sorted({ctx for ctx, args in directives if args[0] == "limit_req"})
    assert len(limited) >= 4, limited  # /api/auth/, /api/, the SSE stream, /docs, /openapi.json
    for ctx in limited:
        assert effective(ctx) == "info", (
            f"`{ctx[-1]}` applies limit_req but its rejections are logged at {effective(ctx) or 'error (default)'}, "
            "at or above the stock error_log level `notice`: each one would write the raw request line and "
            "Referer (a verify JWT, a reset token) to CloudWatch"
        )
    lowered = [args for _, args in directives if args[0] == "error_log" and args[-1] in {"info", "debug"}]
    assert not lowered, lowered
