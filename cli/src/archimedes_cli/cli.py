"""The ``archimedes`` entry point.

0.0.1 was a name-reservation release: the command tree and flags were fixed, but no
subcommand did any work. 0.1.0 fills in three of them against the hosted API —
``login`` (Better Auth email + password, ``POST /api/auth/sign-in/email``), ``meter``
(``GET /api/account/usage`` — today's generation usage and the live price quote), and
``verify`` (``POST /api/rigor/verify`` — the rigor gate over a returns series).
``backtest`` and ``verify --local`` still exit ``NOT_IMPLEMENTED``: both need the local
execution engine, which is not published yet.

0.2.0 adds ``generate`` — brief in, rigor-gated strategy out, entirely from a terminal
(``POST /api/generate/start`` → SSE progress → the strategy's passport URL). It carries
no private key and signs nothing: at the paywall it prints the x402 requirements the
server sent and a browser URL to pay. See the block comment above the command for the
two constraints that shaped it.

``--json`` on every command, including every error path, and a stable exit code for a
failing gate are what make the tool usable from a CI job — see ``exits.py``.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
from collections.abc import Iterator
from datetime import date as _Date

import click
import httpx

from . import __version__, exits
from .session import (
    DEFAULT_SESSION_FILE,
    SESSION_FILE_ENV,
    load_session,
    pick_session_cookie,
    save_session,
    session_path,
    set_session_file,
)

DEFAULT_API_URL = "https://archimedes-arc.com"

_json_option = click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a single JSON object on stdout instead of human-readable text.",
)

_api_url_option = click.option(
    "--api-url",
    envvar="ARCHIMEDES_API_URL",
    default=DEFAULT_API_URL,
    show_default=True,
    metavar="URL",
    help="Base URL of the Archimedes API.",
)


# Which session file this invocation reads and writes. A CLI process is one lane;
# without this the only lever was $HOME, so two agents sharing a runner shared one
# identity and the second one's `login` clobbered the first's (#1752). Not declared with
# click's `envvar=`, deliberately: `archimedes_cli.session.session_path` reads
# ARCHIMEDES_SESSION_FILE itself — it has to, because `archimedes_mcp.credentials` calls
# it with no click context anywhere in the process — and a second reader of the same
# variable is a second place for the precedence rule to drift.
_session_file_option = click.option(
    "--session-file",
    "session_file",
    default=None,
    metavar="PATH",
    help=(
        f"Read/write the cached session here instead of {DEFAULT_SESSION_FILE}. "
        f"Also settable as ${SESSION_FILE_ENV}; the flag wins. One file per lane keeps "
        f"concurrent agents from clobbering each other's identity."
    ),
)


# Session-aware variant for commands that RUN AGAINST an existing session
# (meter/verify): when --api-url/ARCHIMEDES_API_URL is absent, the URL the
# session was logged into wins over the global default — otherwise a session
# cached against a non-default server would silently send its cookie to the
# wrong host and surface as a confusing 401.
_api_url_session_option = click.option(
    "--api-url",
    envvar="ARCHIMEDES_API_URL",
    default=None,
    metavar="URL",
    help=f"Base URL of the Archimedes API. Defaults to the cached session's URL, else {DEFAULT_API_URL}.",
)


def _resolve_api_url(explicit: str | None, session: dict | None) -> str:
    return explicit or (session or {}).get("api_url") or DEFAULT_API_URL


def _http_client(
    api_url: str,
    *,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> httpx.Client:
    """The one place an ``httpx.Client`` gets constructed.

    Keeping construction behind a single function is what lets tests mock HTTP at the
    boundary: they monkeypatch this factory to return a client wired to an
    ``httpx.MockTransport`` instead of a real socket, rather than patching internals of
    each command.

    ``headers`` carries the ``Authorization: Bearer`` header when ``ARCHIMEDES_API_KEY``
    is set (0.2.0); ``timeout`` is a parameter rather than a constant because the SSE
    progress stream deliberately sits idle between server heartbeats for far longer than
    a request/response call ever should.
    """
    # follow_redirects=False, deliberately: the API never redirects, and a
    # compromised/misconfigured endpoint must not be able to bounce a request
    # carrying credentials (login body, session cookie, bearer token) to
    # another host.
    return httpx.Client(
        base_url=api_url,
        cookies=cookies,
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
    )


def _unavailable(command: str, *, as_json: bool, lands_in: str = "unscheduled") -> None:
    """Report that ``command`` has no implementation yet, then exit.

    ``lands_in`` defaults to ``"unscheduled"`` rather than guessing a version number —
    ``backtest`` and ``verify --local`` both need the not-yet-published local execution
    engine, and no target release for that exists yet. Fabricating one (e.g. hardcoding
    the CURRENT version, which 0.0.1 did — a bug once 0.1.0 became the running version
    and made this same message claim a feature "lands in" the release it's running in)
    would be exactly the kind of false claim this repo's conventions forbid.

    Structured output is honoured even here. A caller that asked for ``--json``
    gets JSON on every code path, so a script never has to parse prose to find
    out what happened.
    """
    message = f"'archimedes {command}' is not implemented in {__version__}."
    if lands_in != "unscheduled":
        message += f" It lands in {lands_in}."
    if as_json:
        payload = {
            "ok": False,
            "command": command,
            "error": "not_implemented",
            "version": __version__,
            "lands_in": lands_in,
            "message": message,
        }
        click.echo(json.dumps(payload))
    else:
        click.echo(message, err=True)
        click.echo("See https://github.com/aprin-labs/archimedes for progress.", err=True)
    sys.exit(exits.NOT_IMPLEMENTED)


def _fail(
    command: str,
    *,
    as_json: bool,
    exit_code: int,
    error: str,
    message: str,
    extra: dict | None = None,
    lines: list[str] | None = None,
) -> None:
    """Report a command-produced failure (bad input, no session, a rejected request)
    and exit. Same ``--json``-on-every-path contract as :func:`_unavailable`.

    ``extra`` merges machine-readable detail into the JSON object (the paywall's x402
    requirements, the price quote, the URLs to act on) and ``lines`` are extra
    human-readable lines printed under ``message``. Both default to nothing, so every
    0.1.0 call site behaves byte-identically.
    """
    if as_json:
        payload = {"ok": False, "command": command, "error": error, "message": message}
        payload.update(extra or {})
        click.echo(json.dumps(payload))
    else:
        click.echo(message, err=True)
        for line in lines or []:
            click.echo(line, err=True)
    sys.exit(exit_code)


def _response_detail(response: httpx.Response) -> str:
    """Best-effort human string for a non-2xx API response body."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] if response.text else f"HTTP {response.status_code}"
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)


# `POST /api/rigor/verify`'s own bounds (#1803), mirrored so an obviously-bad
# invocation is refused before a request is spent. The SERVER is the authority:
# these are pre-flight conveniences, and every limit is enforced there too.
_MAX_TRIALS = 10_000

# The two per-bar shape rules from `ReturnPoint` (rigor_verify_routes.py),
# mirrored for the same reason and with the same values. See the block above
# `_parse_returns_csv` for why the CLI checks these locally at all.
_MAX_ABS_DAILY_RETURN = 1.0
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The minimum evaluation window the endpoint publishes: 250 daily bars, one
# trading year (owner decision, #1803). Mirrored here — unlike the row CEILING,
# which is an infrastructure number — because it is a product promise printed in
# `--help`, and making a caller spend one of five requests a minute to be told it
# helps nobody. The server re-checks it and stays the authority.
_MIN_RETURN_ROWS = 250

# The reason codes `POST /api/rigor/verify` attaches to an input refusal
# (`INPUT_REJECTED_CODES` in backend/archimedes/api/rigor_verify_routes.py,
# #1803). Mirrored here rather than imported for the same reason
# `RISK_APPETITES` is: this package does not depend on the backend, and the
# server stays the authority — an unrecognised code still renders, it just
# renders through the generic path below.
_INPUT_REJECTED_CODES = frozenset(
    {
        "invalid_date",
        "duplicate_date",
        "unsorted_dates",
        "non_finite",
        "out_of_range",
        "window_too_short",
        "too_many_rows",
        "trials_out_of_range",
        # The pre-window spelling of the same refusal, kept because `--api-url`
        # can point at any host: an older server answers `too_short` on a short
        # series, and it should still render through the coded path with a
        # remedy rather than degrading to a generic HTTP error.
        "too_short",
    }
)

# One line of "what to do about it" per code. The server's own sentence is
# always printed first and is never replaced by these — it says what was
# rejected; this says what to change.
_INPUT_REJECTED_REMEDY = {
    "invalid_date": "Write dates as YYYY-MM-DD. A two-column CSV of `2026-01-02,0.0013` rows is the expected shape.",
    "duplicate_date": "One row per trading day. Duplicates are refused, not merged — decide which bar is real.",
    "unsorted_dates": (
        "Sort the CSV by date, oldest first (`sort -t, -k1,1 returns.csv`). The out-of-sample split is "
        "positional, so row order IS the time order it grades; the server refuses to re-sort for you "
        "because that would grade a series you did not send."
    ),
    "non_finite": "Remove NaN/inf bars (or drop those days). A non-finite bar cannot be graded.",
    "out_of_range": (
        "Returns are simple decimals, not percentages: +1.3% is 0.013, not 1.3. |r| > 1.0 in one day is "
        "refused because it silently inflates the Sharpe the whole verdict rests on."
    ),
    "window_too_short": (
        "Send at least one trading year of daily bars (250). Below that the endpoint returns a refusal "
        "rather than a verdict — a short series is not graded and then flagged, it is not graded at all, "
        "because a `passes` field with a warning next to it gets read as a pass."
    ),
    "too_short": "Send a longer series — this host's floor predates the 250-bar evaluation window.",
    "too_many_rows": "Split the series or aggregate to a coarser frequency.",
    "trials_out_of_range": "--trials is the number of variants you actually tried: 1..10000.",
}


def _input_rejection(response: httpx.Response) -> tuple[str, str] | None:
    """``(reason_code, server_message)`` when the API refused the BODY with one
    of the verify endpoint's explicit reason codes (#1803), else ``None``.

    The code is what a CI job branches on; the message is the server's own
    sentence, never one this CLI invented.
    """
    detail = _detail(response)
    if not isinstance(detail, dict) or detail.get("error") != "input_rejected":
        return None
    reason = detail.get("reason")
    if reason not in _INPUT_REJECTED_CODES:
        return None
    message = detail.get("message")
    return reason, message if isinstance(message, str) and message else reason


def _handle_api_error(command: str, *, as_json: bool, response: httpx.Response) -> None:
    """Turn a non-2xx API response into a :func:`_fail` call. Never returns."""
    if response.status_code == 401:
        _fail(
            command,
            as_json=as_json,
            exit_code=exits.AUTH,
            error="session_expired",
            message="session expired or was revoked. Run `archimedes login` again.",
        )
    if response.status_code == 429:
        _fail(
            command,
            as_json=as_json,
            exit_code=exits.USAGE,
            error="rate_limited",
            message="rate limited by the API. Wait a moment and try again.",
        )
    if response.status_code == 422:
        rejection = _input_rejection(response)
        if rejection is not None:
            reason, server_message = rejection
            remedy = _INPUT_REJECTED_REMEDY.get(reason)
            _fail(
                command,
                as_json=as_json,
                exit_code=exits.USAGE,
                error="input_rejected",
                message=f"the API rejected the input ({reason}): {server_message}",
                extra={"reason": reason},
                lines=[remedy] if remedy else None,
            )
        _fail(
            command,
            as_json=as_json,
            exit_code=exits.USAGE,
            error="invalid_request",
            message=f"the API rejected the request: {_response_detail(response)}",
        )
    _fail(
        command,
        as_json=as_json,
        exit_code=exits.USAGE,
        error="http_error",
        message=f"{command} failed: HTTP {response.status_code}: {_response_detail(response)}",
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, "-V", "--version", prog_name="archimedes")
def main() -> None:
    """Run the Archimedes rigor gate over your own strategies and returns.

    The gate is four checks: a deflated Sharpe ratio that prices in how many
    variants were tried, a probability of backtest overfitting, a walk-forward
    out-of-sample pass, and a look-ahead audit. A strategy that clears all four
    is worth deploying capital behind; one that clears none of them is a curve
    fit that happens to have a pretty equity chart.

    A bare returns series (what ``verify`` takes) cannot support all four: PBO
    needs a trial matrix and the look-ahead audit needs strategy source, so those
    two always render ``not_evaluable`` rather than being silently passed.

    Your strategy code is never uploaded or executed on an Archimedes server.
    See the README for exactly where that boundary sits.
    """


@main.command()
@_api_url_option
@_session_file_option
@_json_option
def login(api_url: str, session_file: str | None, as_json: bool) -> None:
    """Sign in and cache the session.

    Archimedes accounts are Better Auth (email + password) — there is no wallet
    signature involved in signing in. Reads ``ARCHIMEDES_EMAIL`` / ``ARCHIMEDES_PASSWORD``
    from the environment when both are set (the CI path, so a pipeline never has to
    answer an interactive prompt); otherwise prompts for them, with the password hidden.

    The password is sent once, over this one request (``POST /api/auth/sign-in/email``),
    and is never stored. What gets cached at ``~/.config/archimedes/session.json`` —
    or at ``--session-file``/``$ARCHIMEDES_SESSION_FILE``, one file per lane — at mode 600
    is the session cookie Better Auth issues back, after confirming it round
    trips against ``GET /api/auth/get-session``. Linking a crypto wallet is a separate,
    optional, later step (for on-chain vault actions) — it does not authenticate you and
    is not part of this command.
    """
    set_session_file(session_file)
    email = os.environ.get("ARCHIMEDES_EMAIL", "").strip()
    password = os.environ.get("ARCHIMEDES_PASSWORD", "")
    if not email:
        email = click.prompt("Email")
    if not password:
        password = click.prompt("Password", hide_input=True)

    try:
        with _http_client(api_url) as client:
            response = client.post("/api/auth/sign-in/email", json={"email": email, "password": password})
    except httpx.HTTPError as exc:
        _fail(
            "login",
            as_json=as_json,
            exit_code=exits.AUTH,
            error="network_error",
            message=f"could not reach {api_url}: {exc}",
        )

    if not response.is_success:
        _fail(
            "login",
            as_json=as_json,
            exit_code=exits.AUTH,
            error="invalid_credentials",
            message=f"sign-in failed: HTTP {response.status_code}: {_response_detail(response)}",
        )

    picked = pick_session_cookie(response.cookies)
    if picked is None:
        _fail(
            "login",
            as_json=as_json,
            exit_code=exits.AUTH,
            error="no_session_cookie",
            message="sign-in succeeded but the server did not return a session cookie",
        )
    cookie_name, token = picked

    # Confirm the cookie actually round-trips, and read back the canonical account email,
    # before trusting it — mirrors scripts/agent_journey.py's step_auth. Sent back under
    # the SAME name it arrived as: a prod cookie is `__Secure-`-prefixed and a local one
    # is bare, and only the name it was actually issued under will round-trip.
    try:
        with _http_client(api_url, cookies={cookie_name: token}) as client:
            session_response = client.get("/api/auth/get-session")
        session_payload = session_response.json() if session_response.is_success else None
    except (httpx.HTTPError, ValueError):
        # ValueError covers json.JSONDecodeError from a 200 with a non-JSON or empty
        # body — treated the same as "the round trip didn't confirm a session", not a
        # crash.
        session_payload = None

    user = session_payload.get("user") if isinstance(session_payload, dict) else None
    confirmed_email = user.get("email") if isinstance(user, dict) else None
    if not confirmed_email:
        _fail(
            "login",
            as_json=as_json,
            exit_code=exits.AUTH,
            error="session_not_confirmed",
            message="signed in, but the session cookie did not round-trip on GET /api/auth/get-session",
        )

    # `--session-file` is user input, so the write can fail on a path that is a
    # directory, is read-only, or has an unwritable parent — and it fails HERE, after a
    # successful sign-in. An uncaught OSError exits 1, which this CLI's exit-code
    # contract reserves for "the gate returned a failing verdict"; a CI job branching on
    # 1 would read a mistyped path as a research finding. It is a usage error (2).
    try:
        path = save_session(api_url=api_url, cookies={cookie_name: token}, email=confirmed_email)
    except OSError as exc:
        _fail(
            "login",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="session_file_unwritable",
            message=f"signed in, but the session could not be written to {session_path()}: {exc}",
        )

    if as_json:
        click.echo(json.dumps({"ok": True, "email": confirmed_email}))
    else:
        click.echo(f"Logged in as {confirmed_email}. Session cached at {path}.")
    sys.exit(exits.OK)


def _render_usage(usage: dict) -> None:
    click.echo(f"Usage for {usage.get('date', '?')} (user {usage.get('user_id', '?')})")
    for label, bucket in (("Per-user", usage.get("user") or {}), ("Per-IP", usage.get("ip") or {})):
        error = bucket.get("error")
        used = bucket.get("used")
        if error:
            click.echo(f"  {label}: unavailable ({error})")
        elif bucket.get("unlimited"):
            click.echo(f"  {label}: {used if used is not None else '?'} used (no cap)")
        else:
            click.echo(f"  {label}: {used}/{bucket.get('cap')} used, {bucket.get('remaining')} remaining")
    quote = usage.get("quote") or {}
    price = quote.get("price")
    if price is not None:
        suffix = " (dry run)" if quote.get("dry_run") else ""
        click.echo(f"  Price per generation: {price} {quote.get('asset', '')}{suffix}".rstrip())


@main.command()
@_api_url_session_option
@_session_file_option
@_json_option
def meter(api_url: str | None, session_file: str | None, as_json: bool) -> None:
    """Show what you have used today and what it costs.

    ``GET /api/account/usage`` — today's generation count against both the per-user and
    per-IP daily caps, plus the live price quote (the same ``generation_payment.quote()``
    call the paywall itself reads, so this number and the invoice can never drift apart).
    Requires a cached session; run ``archimedes login`` first.
    """
    set_session_file(session_file)
    session = load_session()
    if session is None:
        _fail(
            "meter",
            as_json=as_json,
            exit_code=exits.AUTH,
            error="no_session",
            message="not logged in. Run `archimedes login` first.",
        )
    api_url = _resolve_api_url(api_url, session)

    try:
        with _http_client(api_url, cookies=session["cookies"]) as client:
            response = client.get("/api/account/usage")
    except httpx.HTTPError as exc:
        _fail(
            "meter",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="network_error",
            message=f"could not reach {api_url}: {exc}",
        )

    if not response.is_success:
        _handle_api_error("meter", as_json=as_json, response=response)

    usage = response.json()
    if as_json:
        click.echo(json.dumps({"ok": True, **usage}))
    else:
        _render_usage(usage)
    sys.exit(exits.OK)


def _reject_local_input(*, as_json: bool, error: str, reason: str, message: str) -> None:
    """A locally-caught violation of the verify input contract. Never returns.

    ``error`` is this CLI's own stable string and ``reason`` is the SERVER's code for
    the identical refusal, exactly the split ``--trials`` already uses: a CI job
    branches on one ``reason`` whichever side caught the bad row. The exit code is
    ``USAGE``, never ``GATE_FAILED`` — a malformed file is not a verdict about a
    strategy, and ``exits.py`` exists to keep those two apart.
    """
    _fail(
        "verify",
        as_json=as_json,
        exit_code=exits.USAGE,
        error=error,
        message=message,
        extra={"reason": reason},
        lines=[_INPUT_REJECTED_REMEDY[reason]],
    )


# ── The local mirror of the verify input contract (#1803 review round 2) ──
#
# WHY THE CLI CHECKS AT ALL. `httpx` serialises the body with
# `json.dumps(..., allow_nan=False)`, so a CSV carrying `nan`/`inf` did not fail
# at the server with `reason: non_finite` — it raised `ValueError` inside the
# request build, printed a traceback, and exited **1**. Exit 1 is `GATE_FAILED`,
# which `exits.py` reserves for "the gate ran and the answer was no": a
# mistyped column was being reported to CI as a research finding, and `--json`
# emitted nothing parseable at all. The other rules are mirrored alongside it so
# one bad file gets one shape of answer rather than two.
#
# WHAT IT IS NOT. This is a mirror, never a replacement. The server re-checks
# every one of these; a row this misses is still refused there, with the same
# code, and the response's sentence is still the one printed. Nothing is sorted,
# deduplicated, dropped or coerced here either — the CLI refuses exactly what
# the server refuses, and repairs nothing.
#
# THE WINDOW **IS** MIRRORED (owner decision, #1803). 250 daily bars — one
# trading year — is a published product rule, not the gate's shifting arithmetic:
# a caller who sends 200 bars is going to be refused, and making them spend a
# rate-limited round trip (5/minute) to be told a number that is printed in
# `--help` is a worse answer than saying it here. It is checked FIRST, before any
# per-row rule, because that is the order the server's own validators run in, so
# one file gets one code whichever side catches it.
#
# WHAT IS STILL NOT MIRRORED: the ceiling (`too_many_rows`). It tracks the edge's
# payload budget (#1749) rather than a product promise, so a CLI that hard-coded
# it could refuse a series a newer server accepts. `--trials` keeps its local
# bound because it is an argument THIS tool defines, not a property of the
# caller's file. An EMPTY file keeps its own `empty_returns` error rather than
# being reported as a short window: zero rows almost always means the wrong file
# or the wrong columns, and that is the more useful thing to say.
def _parse_returns_csv(source: str, *, as_json: bool) -> list[dict]:
    """Parse ``RETURNS_CSV`` into ``[{"date": ..., "daily_return": ...}, ...]``.

    Two columns: date, then daily return. A header row (or any row whose second column
    does not parse as a float — a blank line, a comment) is skipped rather than rejected,
    so both a ``date,daily_return`` header and a bare headerless file work.

    The series is then held to the endpoint's contract, in the server's own order:
    the row COUNT against the 250-bar evaluation window first, then per-row rules
    (strict ``YYYY-MM-DD`` calendar dates, finite returns inside ``[-1.0, 1.0]``),
    then uniqueness, then ordering. A violation exits ``USAGE`` with the server's
    own ``reason`` code (see the block above). Row numbers in the messages count
    DATA rows — the index the server would see — not physical lines, so a skipped
    header does not shift them.
    """
    if source == "-":
        text = click.get_text_stream("stdin").read()
    else:
        with open(source, newline="", encoding="utf-8") as handle:
            text = handle.read()

    # Collect the data rows first so the window can be checked before anything
    # else, the way the server checks it (`_row_count_bounds` runs `mode="before"`,
    # ahead of per-row parsing). A file that is both short AND malformed therefore
    # gets the SAME code from both sides.
    data_rows: list[tuple[int, str, str, float]] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        date_str = row[0].strip()
        if not date_str:
            continue
        raw_value = row[1].strip()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        data_rows.append((len(data_rows) + 1, date_str, raw_value, value))

    # An empty file keeps its own, more diagnostic `empty_returns` answer (raised
    # by the caller); 1..249 rows is the window refusal.
    if data_rows and len(data_rows) < _MIN_RETURN_ROWS:
        _reject_local_input(
            as_json=as_json,
            error="window_too_short_returns",
            reason="window_too_short",
            message=(
                f"{source!r} has {len(data_rows)} data rows; the minimum evaluation window is "
                f"{_MIN_RETURN_ROWS} daily bars (one trading year). A shorter series is refused, "
                "not graded with a caveat"
            ),
        )

    rows: list[dict] = []
    dates: list[_Date] = []
    for number, date_str, raw_value, value in data_rows:
        # Date before value, the order `ReturnPoint` declares its two fields in,
        # so a row that is wrong in both ways is named the same way on both sides.
        if not _ISO_DATE_RE.match(date_str):
            _reject_local_input(
                as_json=as_json,
                error="invalid_date_format",
                reason="invalid_date",
                message=(
                    f"{source!r} row {number} has date {date_str!r}, which is not a strict "
                    "ISO calendar date (YYYY-MM-DD). Epoch seconds, ISO week dates and "
                    "YYYYMMDD are refused rather than guessed at"
                ),
            )
        try:
            parsed = _Date.fromisoformat(date_str)
        except ValueError:
            _reject_local_input(
                as_json=as_json,
                error="invalid_date_format",
                reason="invalid_date",
                message=(
                    f"{source!r} row {number} has date {date_str!r}, which is well-formed "
                    "YYYY-MM-DD but is not a real calendar date"
                ),
            )
        if not math.isfinite(value):
            _reject_local_input(
                as_json=as_json,
                error="non_finite_returns",
                reason="non_finite",
                message=(
                    f"{source!r} row {number} has daily_return {raw_value!r}, which is not a "
                    "finite number and cannot be sent as JSON"
                ),
            )
        if abs(value) > _MAX_ABS_DAILY_RETURN:
            _reject_local_input(
                as_json=as_json,
                error="out_of_range_returns",
                reason="out_of_range",
                message=(
                    f"{source!r} row {number} has daily_return {value}, outside "
                    f"[-{_MAX_ABS_DAILY_RETURN}, {_MAX_ABS_DAILY_RETURN}] (simple return units, "
                    "0.01 = +1%)"
                ),
            )
        rows.append({"date": date_str, "daily_return": value})
        dates.append(parsed)

    # Whole-series rules, in the server's order: every per-row error is reported
    # before either of these, and a duplicate before an inversion.
    first_seen: dict[_Date, int] = {}
    for number, parsed in enumerate(dates, start=1):
        if parsed in first_seen:
            _reject_local_input(
                as_json=as_json,
                error="duplicate_date_rows",
                reason="duplicate_date",
                message=(
                    f"{source!r} row {number} repeats the date {parsed.isoformat()}, already "
                    f"used by row {first_seen[parsed]}. A daily series has one bar per date"
                ),
            )
        first_seen[parsed] = number
    for index in range(1, len(dates)):
        if dates[index] < dates[index - 1]:
            _reject_local_input(
                as_json=as_json,
                error="unsorted_date_rows",
                reason="unsorted_dates",
                message=(
                    f"{source!r} is not in ascending date order: row {index + 1} "
                    f"({dates[index].isoformat()}) precedes row {index} "
                    f"({dates[index - 1].isoformat()})"
                ),
            )
    return rows


_CHECK_LABELS = (
    ("DSR", "dsr"),
    ("PBO", "pbo"),
    ("OOS consistency", "oos_consistency"),
    ("Look-ahead", "look_ahead"),
)
_STATUS_TAG = {"pass": "PASS", "fail": "FAIL", "not_evaluable": "N/A"}


def _verify_verdict(body: dict) -> str:
    """Classify a verify response as ``"pass"``, ``"fail"`` or ``"incomplete"``.

    Never trusts ``passes`` on its own (#1481). The server computes it as a
    quorum over runnable legs, but ``--api-url`` can point at any host,
    including one predating that fix — which returns ``passes: true`` on a
    4-bar series and carries no ``legs_*`` fields at all. So when the quorum
    fields are absent we re-derive the quorum from the leg statuses rather
    than assuming the evaluation was complete.
    """
    leg_statuses = [(body.get(key) or {}).get("status") for _name, key in _CHECK_LABELS]
    if any(st == "fail" for st in leg_statuses):
        return "fail"

    evaluated = body.get("legs_evaluated")
    runnable = body.get("legs_runnable")
    if isinstance(evaluated, int) and isinstance(runnable, int):
        complete = evaluated == runnable and runnable > 0
    else:
        # Older server: no quorum reported. Fail closed — an unreported
        # quorum is an unproven one, not an assumed-complete one.
        complete = bool(leg_statuses) and all(st == "pass" for st in leg_statuses)

    if not complete:
        return "incomplete"
    return "pass" if body.get("passes") else "fail"


def _render_verify(body: dict) -> None:
    click.echo(f"n_bars={body.get('n_bars')}  trials={body.get('trials')} (self-attested)")
    for name, key in _CHECK_LABELS:
        check = body.get(key) or {}
        status = check.get("status", "?")
        tag = _STATUS_TAG.get(status, status.upper())
        line = f"  [{tag}] {name}"
        reason = check.get("reason")
        if reason:
            line += f" — {reason}"
        click.echo(line)
    # #1409 round-4 review fix: the response has carried `rf_convention` since
    # this endpoint was wired to thread per-bar dates through (round 3), but
    # the default human-readable rendering never printed it — the DSR/OOS
    # numbers above were computed against ONE of two materially different
    # risk-free-rate conventions and the CLI user reading this output had no
    # way to tell which. `--json` always carried it (the raw response body is
    # echoed verbatim); this brings the human-readable path to parity.
    rf_convention = body.get("rf_convention")
    if rf_convention:
        click.echo(f"rf_convention={rf_convention}")

    evaluated, runnable = body.get("legs_evaluated"), body.get("legs_runnable")
    total = body.get("legs_total")
    if isinstance(evaluated, int) and isinstance(runnable, int):
        suffix = f" of {total} in the full gate" if isinstance(total, int) else ""
        click.echo(f"legs evaluated: {evaluated}/{runnable} runnable here{suffix}")

    verdict = _verify_verdict(body)
    if verdict == "pass":
        # Never an unqualified "PASSES": PBO and the look-ahead audit cannot
        # run on a bare returns series, so this is not the passport gate.
        click.echo("PASSES (capped — PBO and look-ahead cannot be evaluated on a returns series)")
    elif verdict == "incomplete":
        click.echo("INCOMPLETE — not every runnable leg could be evaluated; this is not a verdict")
    else:
        click.echo("FAILS")


@main.command()
@click.argument(
    "returns_csv",
    type=click.Path(exists=True, dir_okay=False, allow_dash=True),
    metavar="RETURNS_CSV",
)
@click.option(
    "--local",
    "run_local",
    is_flag=True,
    help="Run the gate on this machine. No network, no account, no charge.",
)
@click.option(
    "--trials",
    type=int,
    default=1,
    show_default=True,
    metavar="N",
    help=f"Self-attested trial/variant count the DSR deflation is computed against (1..{_MAX_TRIALS}).",
)
@_api_url_session_option
@_session_file_option
@_json_option
def verify(
    returns_csv: str,
    run_local: bool,
    trials: int,
    api_url: str | None,
    session_file: str | None,
    as_json: bool,
) -> None:
    """Run the rigor gate over a returns series.

    RETURNS_CSV is a two-column CSV of date and daily return (a header row, if present,
    is skipped automatically), or ``-`` to read the series from stdin.

    Runs against the hosted API: ``POST /api/rigor/verify`` computes the deflated Sharpe
    ratio (DSR, deflated by ``--trials``) and a walk-forward out-of-sample consistency
    check against your real returns — the exact same functions and thresholds the
    strategy-passport verdict uses. A bare returns series cannot support the other two
    gate checks: PBO needs a trial matrix of candidate strategies and the look-ahead audit
    needs strategy source, so both always render ``not_evaluable`` with a reason rather
    than being silently scored as a pass. ``--local`` (this machine, no network, no
    account) is not implemented yet.

    **The input contract is strict, and the server refuses rather than repairs it**
    (#1803). Dates must be ``YYYY-MM-DD``, unique, and in ascending order; returns must
    be finite simple decimals with ``|r| <= 1.0`` (+1.3% is ``0.013``, not ``1.3``); the
    series must be at least **250 rows — one trading year, the minimum evaluation
    window** — and at most 2,600 (~10 years); ``--trials`` is 1..10000. Under the window
    there is no verdict at all: the answer is a refusal naming what it got and what it
    needs, never a pass or a fail with a warning attached. A refusal comes back as a 422
    whose ``reason`` is one of ``invalid_date``, ``duplicate_date``, ``unsorted_dates``,
    ``non_finite``, ``out_of_range``, ``window_too_short``, ``too_many_rows``,
    ``trials_out_of_range`` — printed here, and carried as ``reason`` in ``--json``.
    Note ``unsorted_dates`` in particular: the walk-forward split is positional, so a
    shuffled series could otherwise park its best 30% in the holdout, and the server
    will not silently sort it for you because that would grade a series you did not send.

    A series this CLI can already see is wrong — shorter than the evaluation window, or
    carrying a non-finite or out-of-range return, a date that is not ``YYYY-MM-DD``, a
    duplicate, or a row out of order — is refused HERE, before the request is sent,
    carrying the same ``reason`` code the server would have used. The
    server re-checks all of it and remains the authority; the local pass only spends fewer
    round trips and keeps a malformed file from ever reaching the JSON encoder.

    Requires a cached session; run ``archimedes login`` first. Exits 0 when the gate
    passes and 1 when it fails, which is what makes ``archimedes verify returns.csv``
    usable as a CI check before a deploy. A refused input exits 2 and an incomplete
    evaluation exits 4 — never 1, which means only "the gate ran and said no".
    """
    set_session_file(session_file)
    if run_local:
        _unavailable("verify", as_json=as_json)

    if not 1 <= trials <= _MAX_TRIALS:
        # `error` stays `invalid_trials` — an exit/error string is an API and is
        # never redefined (see exits.py) — while `reason` carries the SERVER's
        # code for the identical refusal, so a caller can branch on one code
        # whether the bound was caught here or at the API (#1803).
        _fail(
            "verify",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="invalid_trials",
            message=f"--trials must be between 1 and {_MAX_TRIALS} (got {trials})",
            extra={"reason": "trials_out_of_range"},
        )

    returns = _parse_returns_csv(returns_csv, as_json=as_json)
    if not returns:
        _fail(
            "verify",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="empty_returns",
            message=f"no (date, daily_return) rows parsed from {returns_csv!r}",
        )

    session = load_session()
    if session is None:
        _fail(
            "verify",
            as_json=as_json,
            exit_code=exits.AUTH,
            error="no_session",
            message="not logged in. Run `archimedes login` first.",
        )

    api_url = _resolve_api_url(api_url, session)
    try:
        with _http_client(api_url, cookies=session["cookies"]) as client:
            response = client.post("/api/rigor/verify", json={"returns": returns, "trials": trials})
    except httpx.HTTPError as exc:
        _fail(
            "verify",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="network_error",
            message=f"could not reach {api_url}: {exc}",
        )

    if not response.is_success:
        _handle_api_error("verify", as_json=as_json, response=response)

    body = response.json()
    if as_json:
        click.echo(json.dumps({"ok": True, **body}))
    else:
        _render_verify(body)
    # Not a bare `passes` read (#1481): an incomplete evaluation exits with its
    # own code so a CI job cannot read "a leg could not run" as "strategy
    # rejected". Shortness is not one of those cases any more — #1803 refuses a
    # sub-window series before this point, with USAGE and `window_too_short`.
    _verdict = _verify_verdict(body)
    sys.exit(exits.OK if _verdict == "pass" else exits.GATE_FAILED if _verdict == "fail" else exits.INCOMPLETE)


# ── generate ──────────────────────────────────────────────────────────────
#
# 0.2.0's headline command, and the first one that reaches the product's actual
# spine (brief → rigor-gated strategy) from a terminal. Two constraints shaped
# every decision below.
#
# 1. NO KEY CUSTODY. The 402 the paywall raises carries a full set of x402
#    payment requirements, and it would be perfectly possible to sign them here.
#    This CLI never will. It prints what the server asked for and hands the user
#    a browser. There is no signing code, no private key, no wallet dependency
#    in this package to lose control of.
#
# 2. WRITE AGAINST RESPONSE CODES, NOT AGAINST A POLICY. Who generates for free
#    is being actively redecided (PR #1658's per-account allowance; the owner's
#    D1 decision moving the unlock to a verified email). This command therefore
#    knows nothing about allowances. It asks, and it renders whatever refusal
#    comes back — 402 vs 409 vs 422 — using the server's own reason string. That
#    is what keeps it correct whichever policy lands first.

API_KEY_ENV = "ARCHIMEDES_API_KEY"
"""Env var holding a non-interactive API key, sent as ``Authorization: Bearer``.

The key-issuing lane is being built in parallel; nothing here depends on it
existing. When the variable is unset (today's normal case) the header is simply
not sent and the cached session cookie authenticates exactly as in 0.1.0.
"""

RISK_APPETITES = ("fixed_income", "conservative", "moderate", "aggressive", "hyper_risky")
"""Mirrors ``GenerateBrief.risk_appetite``'s Literal in the backend schema. A
value outside this set is a 422 from the server; catching it in click turns that
round trip into an instant, clearer error."""

# A single SSE connection is capped at 300s server-side (_STREAM_TIMEOUT_SECONDS
# in generate_routes.py) and heartbeat comments arrive about every 15s. The read
# timeout has to sit comfortably above the heartbeat cadence and below nothing in
# particular — when the stream ends for any reason, polling takes over.
_STREAM_READ_TIMEOUT = 90.0
_POLL_INTERVAL_SECONDS = 3.0

# Terminal job states, from JobSummary.state. "stalled" is a read-time derived
# state (#1355) meaning the server saw no heartbeat from the run for over five
# minutes — it is terminal for our purposes because nothing more is coming.
_TERMINAL_JOB_STATES = {"done", "error", "cancelled", "stalled"}
_TERMINAL_SSE_EVENTS = {"done", "error"}


def _api_key_headers() -> dict[str, str]:
    """``Authorization: Bearer`` from the environment, or nothing at all."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _app_url(api_url: str, path: str) -> str:
    return f"{api_url.rstrip('/')}{path}"


def _passport_url(api_url: str, strategy_id: str) -> str:
    """Where the strategy passport for ``strategy_id`` is readable in a browser.

    Matches ``ui/src/routes.js``'s deep route ``/app/strategy/<id>``. That route
    is GATED as of #1753 (the owner's call, narrowing #1194 rev d): it
    left ``ANON_APP_PAGES`` and nginx dropped its ungated carve-out, so a
    recipient with no session is answered with ``302 /sign-in?next=<this URL>``
    and lands here after signing in. The link is still correct and still worth
    forwarding — it is no longer a link that opens without an account, and this
    docstring said the opposite until #1753.
    """
    return _app_url(api_url, f"/app/strategy/{urllib.parse.quote(strategy_id, safe='')}")


def _detail(response: httpx.Response):
    """The API's ``detail`` field — a dict for this router's own refusals, a list
    for FastAPI request-validation errors, a string for some others, ``None`` if
    the body was not JSON. Callers must handle all four rather than assuming."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("detail") if isinstance(body, dict) else None


def _detail_message(response: httpx.Response) -> str:
    """The server's own human-readable sentence, never one this CLI invented.

    Load-bearing for the refusal paths: the free-generation policy is in motion,
    so the server is the only thing that knows why it said no. Falls back to
    :func:`_response_detail`'s best-effort rendering.
    """
    detail = _detail(response)
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
    return _response_detail(response)


def _payment_requirements(response: httpx.Response) -> dict[str, str]:
    """The x402 requirement headers the paywall attached, verbatim.

    ``generation_payment._quote_402`` builds these with the same circlekit
    middleware the settle path uses, so they are what a payer actually has to
    sign. Passed through untouched and unparsed — the CLI is a courier here, not
    a participant.
    """
    return {name: value for name, value in response.headers.items() if name.lower().startswith("payment")}


def _mentions_email_verification(detail, message: str) -> bool:
    """Does the server's refusal name email verification as the unlock?

    Deliberately a check on what the SERVER said, not on what this CLI believes
    the policy to be. #1658 and the owner's D1 decision are still in flight; if
    the deployed server gates the free tier on a verified email it says so, and
    only then does the CLI lead with verification.
    """
    haystack = message.lower()
    if isinstance(detail, dict):
        haystack += " " + str(detail.get("reason", "")).lower()
    return "email" in haystack and ("verif" in haystack or "confirm" in haystack)


def _iter_sse(lines) -> Iterator[dict]:
    """Parse an SSE byte stream into ``{"id", "event", "data"}`` dicts.

    Hand-rolled on purpose. This package's two-dependency footprint (click +
    httpx) is a documented property, and the subset of the SSE grammar the
    Generate stream actually emits is small enough to read in one screen:
    ``id:`` / ``event:`` / ``data:`` fields, a blank line terminating a frame,
    and ``:``-prefixed comment lines — which is what the server's keep-alive
    heartbeats are (#891), so ignoring them is not an optimisation, it is
    required for the stream to survive a long compute step.
    """
    event_name: str | None = None
    event_id: str | None = None
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data_lines:
                yield {"id": event_id, "event": event_name or "message", "data": "\n".join(data_lines)}
            event_name, event_id, data_lines = None, None, []
            continue
        if line.startswith(":"):
            continue  # comment / heartbeat
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            event_id = value
    if data_lines:  # a stream cut off mid-frame yields nothing for the partial frame
        yield {"id": event_id, "event": event_name or "message", "data": "\n".join(data_lines)}


def _progress_line(event: str, data: dict) -> str:
    """One compact human-readable line for one pipeline event."""
    parts: list[str] = []
    message = data.get("message")
    if isinstance(message, str) and message:
        parts.append(message)
    for key in ("stage", "candidate_id", "strategy_name", "position", "served_model", "code"):
        value = data.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return f"  {event:<20} {' '.join(parts)}".rstrip()


def _stream_progress(client: httpx.Client, job_id: str, *, deadline: float, on_event) -> dict | None:
    """Tail the SSE stream, calling ``on_event(name, data)`` per frame.

    Returns the terminal event's data, or ``None`` if the stream ended without
    one — which is an ordinary outcome, not an error: the server caps a single
    connection at five minutes, and any proxy in between may cut it sooner. The
    caller falls back to polling in exactly the same way for all of those cases,
    so no distinction is drawn between them here.
    """
    try:
        with client.stream("GET", f"/api/generate/stream/{job_id}") as response:
            if response.status_code != 200:
                return None
            for frame in _iter_sse(response.iter_lines()):
                try:
                    data = json.loads(frame["data"])
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                name = frame["event"]
                on_event(name, data)
                if name in _TERMINAL_SSE_EVENTS:
                    return {"event": name, **data}
                if time.monotonic() > deadline:
                    return None
    except httpx.HTTPError:
        # A dropped stream is a transport fact, not a verdict about the job.
        # Polling is authoritative and answers the same question.
        return None
    return None


def _poll_until_terminal(client: httpx.Client, job_id: str, *, deadline: float) -> tuple[dict | None, str]:
    """Poll ``GET /api/generate/jobs/{job_id}`` until terminal or out of time.

    This is the documented fallback surface (#1292) and it is also the final
    authority in the happy path: whatever the stream said, the job record is
    what the server actually believes.

    Returns ``(summary, why)`` where ``why`` is ``"terminal"``, ``"timeout"``, or
    ``"auth"``. The caller needs to tell those apart to stay honest: a session
    that expires mid-run stops the polling in about a second, and reporting that
    as "stopped waiting after 900s" would be a false statement about what the
    command actually did.
    """
    summary: dict | None = None
    while True:
        try:
            response = client.get(f"/api/generate/jobs/{job_id}")
        except httpx.HTTPError:
            response = None
        if response is not None and response.is_success:
            body = response.json()
            if isinstance(body, dict):
                summary = body
                if body.get("state") in _TERMINAL_JOB_STATES:
                    return summary, "terminal"
        elif response is not None and response.status_code in (401, 403):
            return summary, "auth"
        if time.monotonic() > deadline:
            return summary, "timeout"
        time.sleep(_POLL_INTERVAL_SECONDS)


def _handle_generate_refusal(response: httpx.Response, *, api_url: str, as_json: bool, quote: dict | None) -> None:
    """Turn a non-202 from ``POST /api/generate/start`` into a `_fail`. Never returns.

    Every branch renders the SERVER's reason and the SERVER's message. Nothing
    here encodes what the free tier currently is, because that is changing
    underneath this command (#1658, owner decision D1) and a CLI that guessed
    would start lying the day the other policy shipped.
    """
    detail = _detail(response)
    reason = detail.get("reason") if isinstance(detail, dict) else None
    message = _detail_message(response)
    status = response.status_code

    if status == 422:
        # The brief was refused BEFORE the paywall. That ordering is not an
        # assumption: `cheap_brief_reject` runs ahead of the `payment_required()`
        # block in generate_routes.start_generation, and FastAPI's own request
        # validation runs ahead of the handler entirely. Those are the two shapes
        # asserted below — a dict whose reason is `brief_invalid`, and the list
        # FastAPI emits. For any OTHER 422 shape the no-charge sentence is
        # omitted rather than guessed at: an unverified claim about money is
        # exactly the kind this repo does not make.
        pre_payment = reason == "brief_invalid" or isinstance(detail, list)
        hint = detail.get("hint") if isinstance(detail, dict) else None
        lines = []
        if hint:
            lines.append(f"  hint: {hint}")
        if pre_payment:
            lines.append("  Nothing was charged and no credit was spent — the brief is rejected before the paywall.")
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="brief_rejected",
            message=f"the brief was rejected: {message}",
            extra={"reason": reason, "hint": hint, "charged": False if pre_payment else None},
            lines=lines,
        )

    if status == 402:
        requirements = _payment_requirements(response)
        body_quote = detail.get("quote") if isinstance(detail, dict) else None
        pay_url = _app_url(api_url, "/app/generate")
        account_url = _app_url(api_url, "/app/account")
        # A premium-model entitlement refusal is also a 402 but carries a plain
        # string detail and no quote. Calling it "the paywall" and pointing at a
        # payment page would be wrong — it is not payable, it is a model choice.
        if not requirements and body_quote is None:
            _fail(
                "generate",
                as_json=as_json,
                exit_code=exits.PAYMENT_REQUIRED,
                error="model_entitlement_required",
                message=f"the server refused this request with 402: {message}",
                extra={"reason": reason},
                lines=[
                    "  This 402 carries no payment requirements — it is a model entitlement refusal.",
                    "  Omit --model to use the default free-tier model.",
                ],
            )
        lines = ["", "  Payment requirements, exactly as the server sent them:"]
        lines += [f"    {name}: {value}" for name, value in requirements.items()] or ["    (none in the headers)"]
        if isinstance(body_quote, dict):
            price = body_quote.get("price")
            asset = body_quote.get("asset", "")
            if price:
                lines.append(f"    price: {price} {asset}".rstrip())
            if body_quote.get("dry_run"):
                lines.append("    dry_run: true — the server reports no real value moves on this host")
        lines += [
            "",
            f"  Pay in a browser: {pay_url}",
            f"  Free tier / account state: {account_url}",
            "",
            "  Then re-run this command — or verify your email for the free tier, if your account is eligible.",
            "  This CLI holds no keys and will not sign a payment.",
        ]
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.PAYMENT_REQUIRED,
            error="payment_required",
            message=f"payment is required before this generation runs: {message}",
            extra={
                "reason": reason,
                "payment_requirements": requirements,
                "quote": body_quote if isinstance(body_quote, dict) else quote,
                "pay_url": pay_url,
                "account_url": account_url,
                "signing_attempted": False,
            },
            lines=lines,
        )

    if status == 409:
        account_url = _app_url(api_url, "/app/account")
        # Both unlocks are always named; which one leads is decided by the
        # server's own reason. Order matters because the first line is the one a
        # hurried reader acts on, and telling someone to connect a wallet when
        # their actual blocker is an unverified email sends them down a path
        # that cannot succeed.
        email_unlock = f"  Verify your email address: {account_url}"
        wallet_unlock = f"  Connect a wallet in the browser: {_app_url(api_url, '/app/generate')}"
        if _mentions_email_verification(detail, message):
            unlocks = [email_unlock, wallet_unlock]
        else:
            unlocks = [wallet_unlock, email_unlock]
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.ACCOUNT_ACTION_REQUIRED,
            error=reason or "account_action_required",
            message=f"this account cannot generate yet: {message}",
            extra={"reason": reason, "account_url": account_url},
            lines=["", "  Two things can unlock this — the server's message above says which applies:", *unlocks],
        )

    # 401 / 429 / 5xx and anything else keep 0.1.0's shared handling.
    _handle_api_error("generate", as_json=as_json, response=response)


def _read_brief_text(brief: str | None, brief_file: str | None) -> str | None:
    """The brief, from an argument or a file (``-`` is stdin). ``None`` if neither."""
    if brief_file:
        if brief_file == "-":
            return click.get_text_stream("stdin").read()
        with open(brief_file, encoding="utf-8") as handle:
            return handle.read()
    return brief


@main.command()
@click.argument("brief", required=False)
@click.option(
    "--brief-file",
    type=click.Path(exists=True, dir_okay=False, allow_dash=True),
    metavar="PATH",
    help="Read the brief from a file, or '-' for stdin, instead of the BRIEF argument.",
)
@click.option(
    "--risk-appetite",
    type=click.Choice(RISK_APPETITES),
    default="moderate",
    show_default=True,
    help="Risk appetite the generated strategy targets.",
)
@click.option("--name", metavar="NAME", help="Optional name for the winning strategy (<= 80 characters).")
@click.option(
    "--n-candidates",
    type=int,
    default=1,
    show_default=True,
    metavar="N",
    help="How many candidates the pipeline considers internally (>= 1; the server caps it).",
)
@click.option("--model", metavar="ID", help="Optional LLM model id. Omit for the account's default free-tier model.")
@click.option(
    "--no-stream",
    is_flag=True,
    help="Skip the SSE progress stream and poll the job endpoint instead.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=float,
    default=900.0,
    show_default=True,
    metavar="SECONDS",
    help="How long to wait for the job. On expiry the job keeps running server-side and exit is 8.",
)
@_api_url_session_option
@_session_file_option
@_json_option
# One parameter per field of the server's brief schema plus the operational flags —
# the width is the API's, not an accident.
def generate(
    brief: str | None,
    brief_file: str | None,
    risk_appetite: str,
    name: str | None,
    n_candidates: int,
    model: str | None,
    no_stream: bool,
    timeout_seconds: float,
    api_url: str | None,
    session_file: str | None,
    as_json: bool,
) -> None:
    """Generate a rigor-gated strategy from a research brief.

    BRIEF is the free-text request ("momentum on liquid US equities, monthly rebalance"),
    or use ``--brief-file`` to read it from a file or ``-`` for stdin.

    Quotes the price, starts the job (``POST /api/generate/start``), tails its progress
    over Server-Sent Events, and prints the resulting strategy id and passport URL.
    If the stream drops — the server caps a single connection at five minutes, and
    proxies cut them sooner — it falls back to polling ``GET /api/generate/jobs/{id}``,
    which is the authoritative source for the final state either way.

    \b
    THIS COMMAND HOLDS NO KEYS AND SIGNS NOTHING. When the server answers 402 it prints
    the x402 payment requirements the server sent plus a browser URL to pay, and exits 5.
    Pay there, then re-run. If your account's unlock is a verified email rather than a
    payment, the server says so and the message names verification.

    \b
    Authentication is the cached session from ``archimedes login``. Setting
    ARCHIMEDES_API_KEY additionally sends it as an Authorization: Bearer header.

    \b
    Exit codes: 0 done · 2 bad input or no session · 5 payment required · 6 account
    action required (verify email / connect wallet) · 7 the job failed · 8 still running
    when the wait budget ran out.
    """
    set_session_file(session_file)
    if brief and brief_file:
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="ambiguous_brief",
            message="pass either a BRIEF argument or --brief-file, not both",
        )

    text = (_read_brief_text(brief, brief_file) or "").strip()
    if not text:
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="empty_brief",
            message="no brief text: pass a BRIEF argument or --brief-file PATH",
        )

    if n_candidates < 1:
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="invalid_n_candidates",
            message="--n-candidates must be >= 1",
        )

    session = load_session()
    headers = _api_key_headers()
    if session is None and not headers:
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.AUTH,
            error="no_session",
            message=f"not logged in. Run `archimedes login` first, or set {API_KEY_ENV}.",
        )

    api_url = _resolve_api_url(api_url, session)
    cookies = (session or {}).get("cookies")

    # The quote is informational: it tells the user what this will cost before
    # anything starts. A quote we could not fetch is reported as absent and the
    # generation still proceeds — the authoritative price rides inside the 402
    # itself, so nothing downstream depends on this call succeeding.
    quote: dict | None = None
    try:
        with _http_client(api_url, cookies=cookies, headers=headers) as client:
            quote_response = client.get("/api/generate/quote")
        if quote_response.is_success:
            body = quote_response.json()
            quote = body if isinstance(body, dict) else None
    except (httpx.HTTPError, ValueError):
        quote = None

    if not as_json:
        if quote is None:
            click.echo("Price quote unavailable — proceeding; the server's own 402 carries the authoritative price.")
        elif quote.get("payment_required"):
            suffix = " (dry run — the server reports no real value moves)" if quote.get("dry_run") else ""
            click.echo(f"Price: {quote.get('price')} {quote.get('asset', '')}{suffix}".rstrip())
        else:
            click.echo("This host reports payment_required=false for generation.")

    payload = {
        "brief": {"intent": text, "risk_appetite": risk_appetite},
        "n_candidates": n_candidates,
    }
    if name:
        payload["brief"]["name"] = name
    if model:
        payload["model"] = model

    try:
        with _http_client(api_url, cookies=cookies, headers=headers) as client:
            start = client.post("/api/generate/start", json=payload)
    except httpx.HTTPError as exc:
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="network_error",
            message=f"could not reach {api_url}: {exc}",
        )

    if start.status_code != 202:
        _handle_generate_refusal(start, api_url=api_url, as_json=as_json, quote=quote)

    started = start.json()
    job_id = started.get("job_id")
    if not job_id:
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.USAGE,
            error="no_job_id",
            message="the server accepted the request but returned no job id",
        )

    if not as_json:
        click.echo(f"Job {job_id} accepted.")

    events: list[dict] = []

    def _record(event_name: str, data: dict) -> None:
        events.append({"event": event_name, "data": data})
        if not as_json:
            click.echo(_progress_line(event_name, data))

    deadline = time.monotonic() + timeout_seconds
    terminal_event: dict | None = None
    if not no_stream:
        with _http_client(api_url, cookies=cookies, headers=headers, timeout=_STREAM_READ_TIMEOUT) as client:
            terminal_event = _stream_progress(client, job_id, deadline=deadline, on_event=_record)

    # The job record is the authority, always — including after a clean stream.
    with _http_client(api_url, cookies=cookies, headers=headers) as client:
        summary, why_stopped = _poll_until_terminal(client, job_id, deadline=deadline)

    if why_stopped == "auth":
        # The session expired while the job was running. Say that, rather than
        # letting it fall through to the wait-budget message below, which would
        # report a timeout that did not happen. The job is unaffected.
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.AUTH,
            error="session_expired",
            message=(
                f"session expired or was revoked while job {job_id} was running. "
                "Run `archimedes login` again — the job itself is unaffected."
            ),
            extra={"job_id": job_id, "events": events},
        )

    state = (summary or {}).get("state")
    strategy_id = (summary or {}).get("best_strategy_id") or (terminal_event or {}).get("strategy_id")
    passport_url = _passport_url(api_url, strategy_id) if strategy_id else None

    if state == "done":
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "ok": True,
                        "command": "generate",
                        "job_id": job_id,
                        "state": state,
                        "strategy_id": strategy_id,
                        "passport_url": passport_url,
                        "events": events,
                    }
                )
            )
        elif strategy_id:
            click.echo(f"Done. strategy_id={strategy_id}")
            click.echo(f"Passport: {passport_url}")
        else:
            # `done` with no id is what the server reported; say that, do not
            # invent a strategy reference that does not exist.
            click.echo("Done, but the server reported no strategy id for this job.")
            click.echo(f"Inspect it with GET /api/generate/jobs/{job_id}/candidates")
        sys.exit(exits.OK)

    if state in _TERMINAL_JOB_STATES:
        message = (terminal_event or {}).get("message") or f"the generation ended in state {state!r}"
        code = (terminal_event or {}).get("code")
        lines = []
        # The credit-restore fact is the server's to assert. `_release_credit_if_undelivered`
        # does restore a credit on a non-`done` terminal state, but nothing in the
        # SSE frame or the job summary says so, and printing it anyway would be a
        # guarantee this CLI cannot see. Point at the ledger instead of claiming.
        if (quote or {}).get("payment_required"):
            lines.append(f"  Your generation-credit ledger: GET {_app_url(api_url, '/api/generate/credits')}")
        _fail(
            "generate",
            as_json=as_json,
            exit_code=exits.JOB_FAILED,
            error="job_failed",
            message=f"generation {job_id} did not complete: {message}",
            extra={"job_id": job_id, "state": state, "code": code, "events": events},
            lines=lines,
        )

    _fail(
        "generate",
        as_json=as_json,
        exit_code=exits.STILL_RUNNING,
        error="still_running",
        message=(
            f"stopped waiting after {timeout_seconds:g}s. Job {job_id} is still "
            f"{state or 'in an unread state'} on the server — it was not cancelled."
        ),
        extra={"job_id": job_id, "state": state, "events": events},
        lines=[f"  Check on it: archimedes generate is not needed — GET /api/generate/jobs/{job_id}"],
    )


@main.command()
@click.option(
    "--strategy-path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    metavar="PATH",
    help="Python file holding the strategy class.",
)
@click.option(
    "--strategy-class",
    required=True,
    metavar="NAME",
    help="Name of the strategy class inside that file.",
)
@_json_option
def backtest(strategy_path: str, strategy_class: str, as_json: bool) -> None:
    """Backtest a strategy file on this machine and print its returns.

    Runs entirely locally, which is the only way it can work: producing returns
    means importing and executing your Python, and that is not something an
    Archimedes server will ever do with code it received over the wire.

    Pipe the output straight into the gate:

        archimedes backtest --strategy-path mine.py --strategy-class Mine | archimedes verify -
    """
    del strategy_path, strategy_class
    _unavailable("backtest", as_json=as_json)


@main.command()
def manifest() -> None:
    """Emit this CLI's machine-readable contract and exit 0.

    Agentic connectivity is the interface: an agent should discover what this
    tool accepts, returns, costs, and exits with from a declarative contract —
    not by parsing --help prose. Always JSON, no network, no session. A test
    walks the real click command tree and asserts this contract matches it,
    so the promise cannot silently drift from the truth.
    """
    from .manifest import MANIFEST

    click.echo(json.dumps(MANIFEST, indent=2))
    sys.exit(exits.OK)


if __name__ == "__main__":  # pragma: no cover
    main()
