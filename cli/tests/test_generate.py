"""Tests for 0.2.0's `archimedes generate`.

Same discipline as ``test_cli.py``: HTTP is mocked at the ``_http_client`` factory
boundary with ``httpx.MockTransport``, never by patching command internals, and no test
opens a socket.

Three things here are guards rather than coverage, and each is written to REJECT
something rather than to confirm a happy path:

* the paywall path never signs and never invents charge language (``TestThePaywall``);
* the 422 path says "nothing was charged" only for the shapes that are provably
  pre-payment (``TestBriefRejection``);
* neither the session cookie nor ``ARCHIMEDES_API_KEY`` ever reaches stdout or stderr
  (``TestCredentialsAreNeverPrinted``).

The SSE-drop → polling fallback is exercised against a stream that is cut mid-frame, not
against a stream that politely ends, because a mid-frame cut is what a real proxy timeout
produces.
"""

from __future__ import annotations

import json

import archimedes_cli.cli as cli_module
import httpx
import pytest
from archimedes_cli.cli import _iter_sse, main
from archimedes_cli.exits import (
    ACCOUNT_ACTION_REQUIRED,
    AUTH,
    JOB_FAILED,
    OK,
    PAYMENT_REQUIRED,
    STILL_RUNNING,
    USAGE,
)
from archimedes_cli.session import SESSION_COOKIE_NAME, SESSION_FILE_ENV, save_session
from click.testing import CliRunner

API = "https://archimedes-arc.com"
COOKIE_VALUE = "s3cr3t-session-token-value"
API_KEY_VALUE = "ak-live-do-not-print-me"

_QUOTE = {
    "payment_required": True,
    "pricing_model": "flat_v1",
    "price": "$2.000000",
    "asset": "USDC",
    "chain": "arc-testnet",
    "recipient": "0xrecipient",
    "dry_run": False,
    "halted": False,
}

_STARTED = {"job_id": "job-abc", "stream_url": "/api/generate/stream/job-abc", "ttl_seconds": 900}


@pytest.fixture
def runner(tmp_path, monkeypatch):
    """CliRunner in a tmp dir with an isolated $HOME and no ambient credentials."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # ARCHIMEDES_SESSION_FILE overrides HOME outright (#1752) — clear it for the same
    # reason HOME is redirected at all.
    monkeypatch.delenv(SESSION_FILE_ENV, raising=False)
    monkeypatch.delenv("ARCHIMEDES_API_URL", raising=False)
    monkeypatch.delenv("ARCHIMEDES_API_KEY", raising=False)
    # Polling must never actually sleep: the fallback tests would otherwise take
    # _POLL_INTERVAL_SECONDS per iteration of real wall-clock time.
    monkeypatch.setattr(cli_module.time, "sleep", lambda _seconds: None)
    return CliRunner()


def _seed_session() -> None:
    save_session(api_url=API, cookies={SESSION_COOKIE_NAME: COOKIE_VALUE}, email="dan@example.com")


def _sse(frames: list[tuple[str, dict]], *, trailing_blank: bool = True) -> str:
    """Encode frames exactly as ``generate_routes._format_sse`` does, heartbeats included."""
    out = ": stream opened\n\n"
    for index, (name, data) in enumerate(frames, start=1):
        out += f"id: {index}\nevent: {name}\ndata: {json.dumps(data)}\n\n"
    out += ": heartbeat\n\n" if trailing_blank else ""
    return out


def _install(monkeypatch, routes, *, stream_body: str | None = None, capture: list | None = None):
    """Patch the client factory. ``routes`` maps ``(method, path)`` to a response or
    a callable; ``stream_body`` answers the SSE path with a text body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        path = request.url.path
        if stream_body is not None and path.startswith("/api/generate/stream/"):
            return httpx.Response(200, text=stream_body, headers={"content-type": "text/event-stream"})
        target = routes.get((request.method, path))
        if target is None:
            raise AssertionError(f"unexpected request: {request.method} {path}")
        return target(request) if callable(target) else target

    def factory(api_url, *, cookies=None, headers=None, timeout=10.0):  # noqa: ARG001
        return httpx.Client(
            base_url=api_url,
            cookies=cookies,
            headers=headers,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(cli_module, "_http_client", factory)


def _forbid_network(monkeypatch):
    def factory(api_url, *, cookies=None, headers=None, timeout=10.0):  # noqa: ARG001
        raise AssertionError("a guard should have stopped this before any HTTP client was built")

    monkeypatch.setattr(cli_module, "_http_client", factory)


def _base_routes(*, start, job):
    return {
        ("GET", "/api/generate/quote"): httpx.Response(200, json=_QUOTE),
        ("POST", "/api/generate/start"): start,
        ("GET", "/api/generate/jobs/job-abc"): job,
    }


def _job(state: str, *, strategy_id: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "job_id": "job-abc",
            "state": state,
            "brief_intent": "momentum",
            "created_at": "2026-08-31T00:00:00Z",
            "updated_at": "2026-08-31T00:01:00Z",
            "n_candidates": 1,
            "best_strategy_id": strategy_id,
        },
    )


# ── Input validation: nothing reaches the network ──────────────────────


class TestInputGuardsRunBeforeAnyRequest:
    def test_no_brief_at_all_is_usage(self, runner, monkeypatch):
        _seed_session()
        _forbid_network(monkeypatch)
        result = runner.invoke(main, ["generate"])
        assert result.exit_code == USAGE
        assert "no brief text" in result.output

    def test_whitespace_only_brief_is_usage(self, runner, monkeypatch):
        _seed_session()
        _forbid_network(monkeypatch)
        result = runner.invoke(main, ["generate", "   \n  "])
        assert result.exit_code == USAGE

    def test_brief_and_brief_file_together_is_usage(self, runner, monkeypatch, tmp_path):
        _seed_session()
        _forbid_network(monkeypatch)
        (tmp_path / "b.txt").write_text("momentum")
        result = runner.invoke(main, ["generate", "momentum", "--brief-file", str(tmp_path / "b.txt")])
        assert result.exit_code == USAGE
        assert "not both" in result.output

    def test_n_candidates_zero_is_rejected_before_the_network(self, runner, monkeypatch):
        _seed_session()
        _forbid_network(monkeypatch)
        result = runner.invoke(main, ["generate", "momentum", "--n-candidates", "0"])
        assert result.exit_code == USAGE
        assert ">= 1" in result.output

    def test_no_session_and_no_api_key_exits_auth_without_touching_the_network(self, runner, monkeypatch):
        _forbid_network(monkeypatch)
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == AUTH
        assert "ARCHIMEDES_API_KEY" in result.output

    def test_brief_file_dash_reads_stdin(self, runner, monkeypatch):
        _seed_session()
        captured: list[httpx.Request] = []
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="strat-1")),
            stream_body=_sse([("done", {"strategy_id": "strat-1"})]),
            capture=captured,
        )
        result = runner.invoke(main, ["generate", "--brief-file", "-"], input="carry trade on FX majors\n")
        assert result.exit_code == OK
        start = next(r for r in captured if r.url.path == "/api/generate/start")
        assert json.loads(start.content)["brief"]["intent"] == "carry trade on FX majors"


# ── The happy path ─────────────────────────────────────────────────────


class TestGenerateCompletes:
    def test_streams_progress_then_prints_the_strategy_and_passport_url(self, runner, monkeypatch):
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="strat-9")),
            stream_body=_sse(
                [
                    ("brief_validated", {"message": "brief accepted"}),
                    ("candidate_drafted", {"candidate_id": "c1", "strategy_name": "Momentum A"}),
                    ("done", {"strategy_id": "strat-9", "served_model": "amazon.nova-micro-v1:0"}),
                ]
            ),
        )
        result = runner.invoke(main, ["generate", "momentum on liquid US equities"])
        assert result.exit_code == OK, result.output
        assert "brief_validated" in result.output
        assert "candidate_drafted" in result.output
        assert "strategy_id=strat-9" in result.output
        assert f"{API}/app/strategy/strat-9" in result.output

    def test_json_is_exactly_one_object_carrying_the_events(self, runner, monkeypatch):
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="strat-9")),
            stream_body=_sse([("brief_validated", {}), ("done", {"strategy_id": "strat-9"})]),
        )
        result = runner.invoke(main, ["generate", "momentum", "--json"])
        assert result.exit_code == OK
        payload = json.loads(result.stdout)  # one object, per the house --json contract
        assert payload["ok"] is True
        assert payload["strategy_id"] == "strat-9"
        assert payload["passport_url"] == f"{API}/app/strategy/strat-9"
        assert [e["event"] for e in payload["events"]] == ["brief_validated", "done"]

    def test_the_brief_fields_are_sent_as_the_server_schema_expects(self, runner, monkeypatch):
        _seed_session()
        captured: list[httpx.Request] = []
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="s")),
            stream_body=_sse([("done", {"strategy_id": "s"})]),
            capture=captured,
        )
        result = runner.invoke(
            main,
            ["generate", "momentum", "--risk-appetite", "aggressive", "--name", "My Strat", "--n-candidates", "3"],
        )
        assert result.exit_code == OK
        body = json.loads(next(r for r in captured if r.url.path == "/api/generate/start").content)
        assert body["brief"] == {"intent": "momentum", "risk_appetite": "aggressive", "name": "My Strat"}
        assert body["n_candidates"] == 3

    def test_done_without_a_strategy_id_says_so_instead_of_inventing_one(self, runner, monkeypatch):
        """An honest absence, per the repo's fail-soft rule: the server said `done`
        and reported no id, so the CLI reports exactly that — it does not fabricate
        a passport URL, and it does not claim the job failed either."""
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id=None)),
            stream_body=_sse([("done", {})]),
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == OK
        assert "no strategy id" in result.output
        assert "/app/strategy/" not in result.output


# ── The paywall: printed, never signed ─────────────────────────────────


class TestThePaywall:
    """The 402 path. The guard being demonstrated is a NEGATIVE one — the CLI must
    surface what the server asked for and stop, never act on it."""

    PAYWALL = httpx.Response(
        402,
        json={
            "detail": {
                "reason": "payment_required",
                "message": (
                    "Generation requires payment. Sign the PAYMENT-REQUIRED requirements "
                    "with your linked wallet and retry with a Payment-Signature header."
                ),
                "quote": _QUOTE,
            }
        },
        headers={"PAYMENT-REQUIRED": "x402 scheme=exact network=arc-testnet amount=2000000 asset=USDC"},
    )

    def test_402_prints_the_requirements_and_exits_its_own_code(self, runner, monkeypatch):
        _seed_session()
        _install(monkeypatch, _base_routes(start=self.PAYWALL, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == PAYMENT_REQUIRED
        assert PAYMENT_REQUIRED == 5 and PAYMENT_REQUIRED not in (OK, USAGE, JOB_FAILED)
        assert "x402 scheme=exact network=arc-testnet amount=2000000 asset=USDC" in result.output
        assert f"{API}/app/generate" in result.output
        assert "re-run this command" in result.output
        assert "verify your email" in result.output.lower()

    def test_402_json_carries_the_requirements_verbatim(self, runner, monkeypatch):
        _seed_session()
        _install(monkeypatch, _base_routes(start=self.PAYWALL, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum", "--json"])
        assert result.exit_code == PAYMENT_REQUIRED
        payload = json.loads(result.stdout)
        assert payload["error"] == "payment_required"
        assert payload["payment_requirements"]["payment-required"].startswith("x402 ")
        assert payload["quote"]["price"] == "$2.000000"
        assert payload["signing_attempted"] is False

    def test_the_cli_never_sends_a_payment_signature_header_anywhere(self, runner, monkeypatch):
        """The anti-goal, checked against real captured requests rather than asserted."""
        _seed_session()
        captured: list[httpx.Request] = []
        _install(monkeypatch, _base_routes(start=self.PAYWALL, job=_job("queued")), capture=captured)
        runner.invoke(main, ["generate", "momentum"])
        assert captured, "no requests were captured — the test would pass vacuously"
        for request in captured:
            assert "payment-signature" not in {k.lower() for k in request.headers}

    def test_no_signing_or_key_handling_code_exists_in_the_package(self):
        """A grep-shaped guard: this package must not grow the ability to sign.

        Checked against the real module source so that adding an eth-account
        import, or building a Payment-Signature header, fails here.
        """
        import pathlib

        import archimedes_cli

        source = "\n".join(path.read_text() for path in pathlib.Path(archimedes_cli.__file__).parent.glob("*.py"))
        for forbidden in ("eth_account", "eth-account", "private_key", "sign_message", "sign_typed_data"):
            assert forbidden not in source, f"the CLI must not reference {forbidden!r}"

    def test_a_premium_model_402_is_not_reported_as_the_paywall(self, runner, monkeypatch):
        """`enforce_model_entitlement` also raises 402, with a plain-string detail and
        no requirements. Calling that "pay in a browser" would be a false instruction."""
        _seed_session()
        entitlement = httpx.Response(
            402,
            json={"detail": "Model 'anthropic.claude' is a premium (Anthropic) model and requires an entitlement."},
        )
        _install(monkeypatch, _base_routes(start=entitlement, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum", "--model", "anthropic.claude", "--json"])
        assert result.exit_code == PAYMENT_REQUIRED
        payload = json.loads(result.stdout)
        assert payload["error"] == "model_entitlement_required"
        assert "pay_url" not in payload


# ── 422: the brief was refused before the paywall ──────────────────────


class TestBriefRejection:
    def test_422_renders_the_validators_reason_and_hint(self, runner, monkeypatch):
        _seed_session()
        rejected = httpx.Response(
            422,
            json={
                "detail": {
                    "reason": "brief_invalid",
                    "code": "BRIEF_INVALID",
                    "message": "That brief does not describe a strategy.",
                    "hint": "Mention an asset class, a goal, or a risk appetite.",
                }
            },
        )
        _install(monkeypatch, _base_routes(start=rejected, job=_job("queued")))
        result = runner.invoke(main, ["generate", "asdf"])
        assert result.exit_code == USAGE
        assert "That brief does not describe a strategy." in result.output
        assert "Mention an asset class" in result.output
        assert "Nothing was charged" in result.output

    def test_the_no_charge_claim_is_omitted_for_an_unrecognised_422_shape(self, runner, monkeypatch):
        """The honesty guard, shown rejecting. `cheap_brief_reject` provably runs
        before the payment gate, which is what licenses the no-charge sentence — for
        any 422 whose shape does not prove that ordering, the sentence must not
        appear rather than be guessed at."""
        _seed_session()
        odd = httpx.Response(422, json={"detail": {"reason": "something_else", "message": "refused"}})
        _install(monkeypatch, _base_routes(start=odd, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum", "--json"])
        assert result.exit_code == USAGE
        payload = json.loads(result.stdout)
        assert "Nothing was charged" not in result.stdout
        assert payload["charged"] is None  # unknown, not asserted False

    def test_fastapi_request_validation_list_detail_is_handled(self, runner, monkeypatch):
        _seed_session()
        pydantic = httpx.Response(
            422,
            json={"detail": [{"loc": ["body", "n_candidates"], "msg": "Input should be less than or equal to 5"}]},
        )
        _install(monkeypatch, _base_routes(start=pydantic, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum", "--n-candidates", "99"])
        assert result.exit_code == USAGE
        assert "less than or equal to 5" in result.output
        assert "Nothing was charged" in result.output


# ── 409: account state, not payment ────────────────────────────────────


class TestAccountActionRequired:
    @staticmethod
    def _unlock_lines(output: str) -> list[str]:
        return [line for line in output.splitlines() if "http" in line and "/app/" in line]

    def test_wallet_link_required_names_both_unlocks_and_leads_with_the_wallet(self, runner, monkeypatch):
        """The ordering assertion here is what makes its email-first sibling a real
        guard rather than a vacuous one. The two tests demand OPPOSITE orders, so no
        single fixed ordering can satisfy both — replacing the branch with either
        constant fails one of them. (Caught by a revert-demo: an earlier version of
        this pair asserted only the email-first order, which the fixed list literal
        satisfied even with the branch deleted.)"""
        _seed_session()
        conflict = httpx.Response(
            409,
            json={
                "detail": {
                    "reason": "wallet_link_required",
                    "message": "Generation requires a linked, funded wallet.",
                }
            },
        )
        _install(monkeypatch, _base_routes(start=conflict, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == ACCOUNT_ACTION_REQUIRED
        assert "Generation requires a linked, funded wallet." in result.output
        lines = self._unlock_lines(result.output)
        assert len(lines) == 2, f"both unlocks must always be named, got {lines}"
        assert "/app/generate" in lines[0], f"the wallet unlock should lead here, got {lines}"
        assert "/app/account" in lines[1]

    def test_an_email_verification_reason_leads_with_verification(self, runner, monkeypatch):
        """Policy-neutrality, demonstrated: the CLI encodes no allowance rule, it
        reads the server's reason. If the deployed server gates the free tier on a
        verified email (#1658 / owner decision D1), verification is named first."""
        _seed_session()
        conflict = httpx.Response(
            409,
            json={
                "detail": {
                    "reason": "email_verification_required",
                    "message": "Verify your email address to use your free generations.",
                }
            },
        )
        _install(monkeypatch, _base_routes(start=conflict, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == ACCOUNT_ACTION_REQUIRED
        lines = self._unlock_lines(result.output)
        assert len(lines) == 2, f"both unlocks must always be named, got {lines}"
        assert "/app/account" in lines[0], f"verification should lead here, got {lines}"
        assert "/app/generate" in lines[1]

    def test_the_reason_is_reported_even_when_the_cli_has_never_seen_it(self, runner, monkeypatch):
        """A reason this release does not know about must still round-trip. This is
        what keeps the command correct when the policy changes underneath it."""
        _seed_session()
        conflict = httpx.Response(
            409,
            json={"detail": {"reason": "some_future_policy_gate", "message": "A future rule refused this."}},
        )
        _install(monkeypatch, _base_routes(start=conflict, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum", "--json"])
        assert result.exit_code == ACCOUNT_ACTION_REQUIRED
        payload = json.loads(result.stdout)
        assert payload["reason"] == "some_future_policy_gate"
        assert "A future rule refused this." in payload["message"]


# ── Shared error handling inherited from 0.1.0 ─────────────────────────


class TestInheritedErrorPaths:
    def test_expired_session_exits_auth(self, runner, monkeypatch):
        _seed_session()
        _install(monkeypatch, _base_routes(start=httpx.Response(401), job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == AUTH

    def test_queue_full_429_exits_usage_not_payment_required(self, runner, monkeypatch):
        _seed_session()
        full = httpx.Response(429, json={"detail": {"reason": "generation_queue_full", "message": "queue is full"}})
        _install(monkeypatch, _base_routes(start=full, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == USAGE
        assert result.exit_code != PAYMENT_REQUIRED


# ── SSE parsing + the polling fallback ─────────────────────────────────


class TestSseParsing:
    def test_comments_and_heartbeats_are_ignored(self):
        raw = ': stream opened\n\n: heartbeat\n\nid: 1\nevent: done\ndata: {"a": 1}\n\n'
        frames = list(_iter_sse(raw.split("\n")))
        assert frames == [{"id": "1", "event": "done", "data": '{"a": 1}'}]

    def test_multiline_data_is_joined(self):
        raw = "event: x\ndata: one\ndata: two\n\n"
        assert next(iter(_iter_sse(raw.split("\n"))))["data"] == "one\ntwo"

    def test_a_frame_with_no_event_field_defaults_to_message(self):
        assert next(iter(_iter_sse(["data: {}", "", ""])))["event"] == "message"


class TestStreamDropFallsBackToPolling:
    def test_a_stream_killed_mid_frame_still_reaches_the_terminal_state(self, runner, monkeypatch):
        """The fallback guard. The stream is cut in the middle of the `done` frame —
        exactly what an intermediary's idle timeout produces — so no terminal event
        is ever parsed from it. The command must still report `done`, from the job
        record, which is the authoritative surface (#1292)."""
        _seed_session()
        truncated = ": stream opened\n\nid: 1\nevent: brief_validated\ndata: {}\n\nid: 2\nevent: do"
        polls: list[httpx.Request] = []

        def job_handler(request: httpx.Request) -> httpx.Response:
            polls.append(request)
            # first poll: still running; second: done. Proves the loop really polls.
            return _job("running") if len(polls) == 1 else _job("done", strategy_id="strat-fallback")

        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=job_handler),
            stream_body=truncated,
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == OK, result.output
        assert len(polls) >= 2, "the polling fallback did not actually poll"
        assert "strategy_id=strat-fallback" in result.output

    def test_a_stream_that_errors_at_the_transport_falls_back_too(self, runner, monkeypatch):
        _seed_session()

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.startswith("/api/generate/stream/"):
                raise httpx.ReadError("connection reset by peer")
            if path == "/api/generate/quote":
                return httpx.Response(200, json=_QUOTE)
            if path == "/api/generate/start":
                return httpx.Response(202, json=_STARTED)
            return _job("done", strategy_id="strat-reset")

        def factory(api_url, *, cookies=None, headers=None, timeout=10.0):  # noqa: ARG001
            return httpx.Client(
                base_url=api_url, cookies=cookies, headers=headers, transport=httpx.MockTransport(handler)
            )

        monkeypatch.setattr(cli_module, "_http_client", factory)
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == OK, result.output
        assert "strat-reset" in result.output

    def test_no_stream_flag_skips_the_stream_entirely(self, runner, monkeypatch):
        _seed_session()
        captured: list[httpx.Request] = []
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="s")),
            capture=captured,
        )
        result = runner.invoke(main, ["generate", "momentum", "--no-stream"])
        assert result.exit_code == OK
        assert not any(r.url.path.startswith("/api/generate/stream/") for r in captured)


# ── Terminal states other than done ────────────────────────────────────


class TestTerminalFailures:
    @pytest.mark.parametrize("state", ["error", "cancelled", "stalled"])
    def test_a_non_done_terminal_state_exits_job_failed(self, runner, monkeypatch, state):
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job(state)),
            stream_body=_sse([("error", {"message": "the run died", "code": "STALLED"})]),
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == JOB_FAILED
        assert "the run died" in result.output

    def test_a_timeout_event_surfaces_the_servers_own_message(self, runner, monkeypatch):
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("error")),
            stream_body=_sse([("error", {"message": "generation exceeded the 600-second limit", "code": "TIMEOUT"})]),
        )
        result = runner.invoke(main, ["generate", "momentum", "--json"])
        assert result.exit_code == JOB_FAILED
        payload = json.loads(result.stdout)
        assert payload["code"] == "TIMEOUT"
        assert "exceeded the 600-second limit" in payload["message"]

    def test_no_credit_restore_is_claimed_when_the_server_did_not_assert_one(self, runner, monkeypatch):
        """The honesty guard, shown rejecting. `_release_credit_if_undelivered` does
        restore a credit on a failed run, but the SSE frame carries no field saying
        so — so the CLI must point at the ledger, never promise a refund."""
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("error")),
            stream_body=_sse([("error", {"message": "boom", "code": "JOB_FAILED"})]),
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == JOB_FAILED
        lowered = result.output.lower()
        for false_claim in ("credit was restored", "credit has been restored", "you were refunded", "refund issued"):
            assert false_claim not in lowered
        assert "/api/generate/credits" in result.output

    def test_a_session_expiring_mid_run_is_not_reported_as_a_wait_timeout(self, runner, monkeypatch):
        """A 401 on the job poll stops the wait in about a second. Reporting that as
        "stopped waiting after 900s" would describe something the command did not do,
        so it exits AUTH with the real reason and says the job is unaffected."""
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=httpx.Response(401)),
            stream_body=_sse([("agent_iteration", {"stage": "debate"})]),
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == AUTH
        assert result.exit_code != STILL_RUNNING
        assert "stopped waiting" not in result.output
        assert "job itself is unaffected" in result.output

    def test_a_client_side_timeout_is_not_reported_as_a_failed_job(self, runner, monkeypatch):
        """Exit 8, not 7. The job is still running server-side; saying it failed
        would be a false claim about work that may yet succeed."""
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("running")),
            stream_body=_sse([("agent_iteration", {"stage": "debate"})]),
        )
        result = runner.invoke(main, ["generate", "momentum", "--timeout", "0"])
        assert result.exit_code == STILL_RUNNING
        assert result.exit_code != JOB_FAILED
        assert "not cancelled" in result.output
        assert "job-abc" in result.output


# ── Auth plumbing ──────────────────────────────────────────────────────


class TestAuthentication:
    def test_the_session_cookie_is_sent(self, runner, monkeypatch):
        _seed_session()
        captured: list[httpx.Request] = []
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="s")),
            stream_body=_sse([("done", {"strategy_id": "s"})]),
            capture=captured,
        )
        runner.invoke(main, ["generate", "momentum"])
        start = next(r for r in captured if r.url.path == "/api/generate/start")
        assert COOKIE_VALUE in start.headers.get("cookie", "")

    def test_an_api_key_is_sent_as_a_bearer_header(self, runner, monkeypatch):
        _seed_session()
        monkeypatch.setenv("ARCHIMEDES_API_KEY", API_KEY_VALUE)
        captured: list[httpx.Request] = []
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="s")),
            stream_body=_sse([("done", {"strategy_id": "s"})]),
            capture=captured,
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == OK
        start = next(r for r in captured if r.url.path == "/api/generate/start")
        assert start.headers["authorization"] == f"Bearer {API_KEY_VALUE}"

    def test_an_api_key_alone_authenticates_with_no_cached_session(self, runner, monkeypatch):
        monkeypatch.setenv("ARCHIMEDES_API_KEY", API_KEY_VALUE)
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="s")),
            stream_body=_sse([("done", {"strategy_id": "s"})]),
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == OK, result.output


class TestCredentialsAreNeverPrinted:
    """Grep-shaped guard over real captured output, on both a success and a failure
    path. Written to fail if a future change ever echoes the request headers — which
    is the realistic way this regresses."""

    def test_neither_the_cookie_nor_the_api_key_reaches_stdout_or_stderr(self, runner, monkeypatch):
        _seed_session()
        monkeypatch.setenv("ARCHIMEDES_API_KEY", API_KEY_VALUE)
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="s")),
            stream_body=_sse([("done", {"strategy_id": "s"})]),
        )
        result = runner.invoke(main, ["generate", "momentum", "--json"])
        assert result.exit_code == OK
        assert COOKIE_VALUE not in result.output
        assert API_KEY_VALUE not in result.output

    def test_credentials_do_not_leak_on_the_402_path_either(self, runner, monkeypatch):
        _seed_session()
        monkeypatch.setenv("ARCHIMEDES_API_KEY", API_KEY_VALUE)
        _install(monkeypatch, _base_routes(start=TestThePaywall.PAYWALL, job=_job("queued")))
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == PAYMENT_REQUIRED
        assert COOKIE_VALUE not in result.output
        assert API_KEY_VALUE not in result.output


# ── The quote is informational, never load-bearing ─────────────────────


class TestQuoteHandling:
    def test_an_unreachable_quote_does_not_block_the_generation(self, runner, monkeypatch):
        _seed_session()
        _install(
            monkeypatch,
            {
                ("GET", "/api/generate/quote"): httpx.Response(503),
                ("POST", "/api/generate/start"): httpx.Response(202, json=_STARTED),
                ("GET", "/api/generate/jobs/job-abc"): _job("done", strategy_id="s"),
            },
            stream_body=_sse([("done", {"strategy_id": "s"})]),
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert result.exit_code == OK
        assert "Price quote unavailable" in result.output

    def test_the_price_is_shown_before_anything_starts(self, runner, monkeypatch):
        _seed_session()
        _install(
            monkeypatch,
            _base_routes(start=httpx.Response(202, json=_STARTED), job=_job("done", strategy_id="s")),
            stream_body=_sse([("done", {"strategy_id": "s"})]),
        )
        result = runner.invoke(main, ["generate", "momentum"])
        assert "$2.000000 USDC" in result.output
        assert result.output.index("$2.000000") < result.output.index("Job job-abc")


class TestHelpAndManifest:
    def test_help_documents_the_no_key_custody_property(self, runner):
        result = runner.invoke(main, ["generate", "--help"])
        assert result.exit_code == OK
        assert "HOLDS NO KEYS" in result.output
        for flag in ("--brief-file", "--risk-appetite", "--n-candidates", "--no-stream", "--timeout", "--json"):
            assert flag in result.output
