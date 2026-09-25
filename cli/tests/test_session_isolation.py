"""One session file per lane, and a lock around it — issue #1752.

The bug this file pins was observed live on 2026-09-01: two agent-CLI lanes on one runner
shared ``~/.config/archimedes/session.json`` because ``HOME`` was the only lever, so the
second lane's ``login`` overwrote the first lane's identity *between* two otherwise clean
``meter`` calls. The first lane kept working, kept returning 200s, and was answering as
somebody else.

Four claims are made here, and each has an input written to break it:

* **Isolation** — two paths, two identities, verified through the real ``login``/``meter``
  commands rather than only through the loader. The adversarial partner
  (``test_adversarial_two_lanes_sharing_one_file_still_clobber``) drives the *same* file
  twice and asserts the clobber still happens, so the isolation tests cannot pass for the
  vacuous reason that a second login never overwrites anything.
* **The lock is really taken** — a *separate process* holds ``flock(LOCK_EX)`` on the
  session file and the tests assert that a write, and a read, both WAIT. Delete either
  ``flock`` call in ``session.py`` and these go red, because the operation completes
  immediately instead. ``test_two_readers_do_not_block_each_other`` is the other side:
  the read lock must be shared, not exclusive, or every concurrent reader serializes.
* **0600 survives the override** — including the case that motivates the second ``chmod``:
  a file that already exists, world-readable, from an older version.
* **A bad path is exit 2, not exit 1** — the flag takes user input, so it can name a
  directory or a read-only file, and this CLI's exit-code contract reserves 1 for "the
  gate returned a failing verdict". Before the guard, that write raised through click and
  exited 1.

The lock tests are POSIX-only by construction (``flock`` is), which is where CI, prod and
every developer machine run; they skip rather than lie on a platform without ``fcntl``.
Only those tests skip — the isolation and permission claims hold with or without it.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import archimedes_cli.cli as cli_module
import httpx
import pytest
from archimedes_cli.cli import main
from archimedes_cli.exits import OK, USAGE
from archimedes_cli.session import (
    SESSION_COOKIE_NAME,
    SESSION_FILE_ENV,
    load_session,
    save_session,
    session_path,
    set_session_file,
)
from click.testing import CliRunner

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

requires_flock = pytest.mark.skipif(fcntl is None, reason="flock is POSIX-only; CI, prod and dev are POSIX")


# ── helpers ──────────────────────────────────────────────────────────


def _seed(path: Path, *, email: str, cookie: str) -> Path:
    """Write a session at ``path`` the way ``login`` would, and leave the override off."""
    set_session_file(path)
    try:
        return save_session(api_url="https://archimedes-arc.com", cookies={SESSION_COOKIE_NAME: cookie}, email=email)
    finally:
        set_session_file(None)


def _install_transport(monkeypatch, handler):
    """Mock HTTP at the CLI's one client-construction point — same boundary as
    ``test_cli.py``, never a command's internals."""

    def factory(api_url, *, cookies=None):
        return httpx.Client(base_url=api_url, cookies=cookies, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli_module, "_http_client", factory)


def _login_handler(*, email: str, cookie: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/sign-in/email":
            return httpx.Response(
                200,
                json={"redirect": False},
                headers=[("set-cookie", f"{SESSION_COOKIE_NAME}={cookie}; Path=/; HttpOnly")],
            )
        if request.url.path == "/api/auth/get-session":
            return httpx.Response(200, json={"user": {"id": "u1", "email": email}, "session": {"id": "s1"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    return handler


# A second PROCESS, so the lock under test is a real inter-process lock and not something
# this process could satisfy from its own bookkeeping. It takes LOCK_EX, announces it on
# stdout, and holds it until the parent writes a line to its stdin — no sleeps, no polling,
# so the handshake is deterministic.
_LOCK_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX if sys.argv[2] == "exclusive" else fcntl.LOCK_SH)
sys.stdout.write("locked\\n")
sys.stdout.flush()
sys.stdin.readline()
os.close(fd)
"""


@contextmanager
def _held_by_another_process(path: Path, mode: str = "exclusive"):
    proc = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(path), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "locked", "the helper process never took the lock"
        yield
    finally:
        try:
            proc.stdin.write("\n")
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            # The helper already exited (it crashed, or the assertion above fired first).
            # Swallowed so the real failure is what the test reports, not a broken pipe.
            pass
        proc.stdout.close()
        proc.wait(timeout=30)


def _in_a_thread(work):
    """Run ``work()`` on a daemon thread; return an Event set when it returns."""
    finished = threading.Event()

    def run():
        work()
        finished.set()

    threading.Thread(target=run, daemon=True).start()
    return finished


@pytest.fixture(autouse=True)
def _no_ambient_session_file(monkeypatch):
    """Clear both levers around every test in this module.

    ``CliRunner`` runs commands **in this process**, so a test that passes
    ``--session-file`` leaves ``set_session_file``'s process-wide override installed for
    whatever runs next; and a developer with ``ARCHIMEDES_SESSION_FILE`` exported would
    otherwise run a different suite than CI does, against their own real session file.

    Deliberately here and not in a ``cli/tests/conftest.py``: a conftest in this directory
    is imported under the bare module name ``conftest``, which is the name
    ``mcp-server/tests`` reaches for (``from conftest import json_response``). Adding one
    breaks ``pytest cli/tests mcp-server/tests`` — a combination that works today, and one
    CI does not run only because it runs the two suites as separate commands. Measured, not
    assumed: with a conftest here that invocation dies with "cannot import name
    'json_response' from 'conftest'".
    """
    monkeypatch.delenv(SESSION_FILE_ENV, raising=False)
    set_session_file(None)
    yield
    set_session_file(None)


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARCHIMEDES_EMAIL", "unused@example.com")
    monkeypatch.setenv("ARCHIMEDES_PASSWORD", "unused")
    monkeypatch.delenv("ARCHIMEDES_API_URL", raising=False)
    monkeypatch.delenv("ARCHIMEDES_API_KEY", raising=False)
    return CliRunner()


# ── where the file lives ─────────────────────────────────────────────


class TestResolvingTheSessionPath:
    def test_the_default_is_still_under_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert session_path() == tmp_path / ".config" / "archimedes" / "session.json"

    def test_the_env_var_moves_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(SESSION_FILE_ENV, str(tmp_path / "lane-a.json"))
        assert session_path() == tmp_path / "lane-a.json"

    def test_the_env_var_expands_a_tilde(self, tmp_path, monkeypatch):
        """A quoted argument or an ``execve`` with no shell in between hands the CLI a
        literal ``~``; treating it as a directory name would silently write the session
        into a folder called ``~`` in the cwd."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(SESSION_FILE_ENV, "~/lane-b.json")
        assert session_path() == tmp_path / "lane-b.json"

    def test_adversarial_a_blank_env_var_is_treated_as_unset(self, tmp_path, monkeypatch):
        """The input that should NOT be honoured: an exported-but-empty variable is a
        shell saying "not configured", and resolving it would put the session at the
        current directory rather than anywhere deliberate."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(SESSION_FILE_ENV, "   ")
        assert session_path() == tmp_path / ".config" / "archimedes" / "session.json"

    def test_the_flag_wins_over_the_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(SESSION_FILE_ENV, str(tmp_path / "from-env.json"))
        set_session_file(tmp_path / "from-flag.json")
        assert session_path() == tmp_path / "from-flag.json"

    def test_an_absent_flag_falls_back_to_the_env_var(self, tmp_path, monkeypatch):
        """``set_session_file(None)`` is what an unpassed ``--session-file`` does, and it
        must not shadow the env var — the commands call it unconditionally."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(SESSION_FILE_ENV, str(tmp_path / "from-env.json"))
        set_session_file(None)
        assert session_path() == tmp_path / "from-env.json"


# ── two lanes ────────────────────────────────────────────────────────


class TestTwoLanesDoNotSeeEachOther:
    def test_two_files_hold_two_identities(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        lane_a = tmp_path / "a.json"
        lane_b = tmp_path / "b.json"
        _seed(lane_a, email="a@example.com", cookie="tok-a")
        _seed(lane_b, email="b@example.com", cookie="tok-b")

        set_session_file(lane_a)
        assert load_session()["email"] == "a@example.com"
        assert load_session()["cookies"] == {SESSION_COOKIE_NAME: "tok-a"}

        set_session_file(lane_b)
        assert load_session()["email"] == "b@example.com"
        assert load_session()["cookies"] == {SESSION_COOKIE_NAME: "tok-b"}

        set_session_file(None)
        assert load_session() is None, "neither lane may leak into the default location"

    def test_a_second_lanes_login_does_not_clobber_the_first(self, runner, tmp_path, monkeypatch):
        """The incident itself: lane A logs in, lane B logs in, and lane A's identity is
        still lane A's afterwards."""
        lane_a = tmp_path / "lane-a.json"
        lane_b = tmp_path / "lane-b.json"

        _install_transport(monkeypatch, _login_handler(email="a@example.com", cookie="tok-a"))
        assert runner.invoke(main, ["login", "--json", "--session-file", str(lane_a)]).exit_code == OK

        _install_transport(monkeypatch, _login_handler(email="b@example.com", cookie="tok-b"))
        assert runner.invoke(main, ["login", "--json", "--session-file", str(lane_b)]).exit_code == OK

        assert json.loads(lane_a.read_text())["email"] == "a@example.com"
        assert json.loads(lane_a.read_text())["cookies"] == {SESSION_COOKIE_NAME: "tok-a"}
        assert json.loads(lane_b.read_text())["email"] == "b@example.com"
        assert not (Path(os.environ["HOME"]) / ".config" / "archimedes" / "session.json").exists()

    def test_adversarial_two_lanes_sharing_one_file_still_clobber(self, runner, tmp_path, monkeypatch):
        """The control. Without the flag the second login DOES overwrite the first — which
        is the reported bug, and is what makes the test above a real assertion rather than
        a tautology about logins never overwriting anything."""
        shared = tmp_path / "shared.json"

        _install_transport(monkeypatch, _login_handler(email="a@example.com", cookie="tok-a"))
        assert runner.invoke(main, ["login", "--json", "--session-file", str(shared)]).exit_code == OK

        _install_transport(monkeypatch, _login_handler(email="b@example.com", cookie="tok-b"))
        assert runner.invoke(main, ["login", "--json", "--session-file", str(shared)]).exit_code == OK

        assert json.loads(shared.read_text())["email"] == "b@example.com"

    def test_adversarial_a_session_file_that_cannot_be_written_exits_2_not_1(self, runner, tmp_path, monkeypatch):
        """``--session-file`` is user input, so it can name something unwritable — here a
        directory. The write fails *after* a successful sign-in, and an uncaught OSError
        would surface as exit 1, which in this CLI means "the gate returned a failing
        verdict" (README exit-code table). A CI job branching on 1 would read a typo in a
        path as a research finding."""
        a_directory = tmp_path / "not-a-file"
        a_directory.mkdir()
        _install_transport(monkeypatch, _login_handler(email="a@example.com", cookie="tok-a"))

        result = runner.invoke(main, ["login", "--json", "--session-file", str(a_directory)])

        assert result.exit_code == USAGE
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"] == "session_file_unwritable"
        assert str(a_directory) in payload["message"]
        assert "tok-a" not in result.stdout, "the cookie must not be echoed in the failure"

    def test_an_empty_flag_value_falls_back_rather_than_writing_to_an_empty_path(self, runner, tmp_path, monkeypatch):
        """``--session-file ''`` is a user saying nothing, not naming a file — it must
        resolve like an absent flag rather than trying to write to ``.``."""
        lane = tmp_path / "from-env.json"
        monkeypatch.setenv(SESSION_FILE_ENV, str(lane))
        _install_transport(monkeypatch, _login_handler(email="env@example.com", cookie="tok-env"))

        assert runner.invoke(main, ["login", "--json", "--session-file", ""]).exit_code == OK

        assert json.loads(lane.read_text())["email"] == "env@example.com"

    def test_meter_sends_the_cookie_from_its_own_lane(self, runner, tmp_path, monkeypatch):
        """Reading is isolated too, not just writing: the flag has to reach ``load_session``
        or a lane would authenticate as whoever wrote the default file."""
        lane_a = tmp_path / "lane-a.json"
        lane_b = tmp_path / "lane-b.json"
        _seed(lane_a, email="a@example.com", cookie="tok-a")
        _seed(lane_b, email="b@example.com", cookie="tok-b")
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("cookie", ""))
            return httpx.Response(200, json={"date": "2026-09-01", "user_id": "u1", "user": {}, "ip": {}, "quote": {}})

        _install_transport(monkeypatch, handler)
        assert runner.invoke(main, ["meter", "--json", "--session-file", str(lane_a)]).exit_code == OK
        assert runner.invoke(main, ["meter", "--json", "--session-file", str(lane_b)]).exit_code == OK

        assert f"{SESSION_COOKIE_NAME}=tok-a" in seen[0]
        assert "tok-b" not in seen[0]
        assert f"{SESSION_COOKIE_NAME}=tok-b" in seen[1]
        assert "tok-a" not in seen[1]

    def test_the_env_var_isolates_a_lane_with_no_flag_at_all(self, runner, tmp_path, monkeypatch):
        """The fleet-operator path: export the variable once per lane and every command in
        that lane — including ones with no flag threaded through — follows it."""
        lane = tmp_path / "lane-env.json"
        monkeypatch.setenv(SESSION_FILE_ENV, str(lane))
        _install_transport(monkeypatch, _login_handler(email="env@example.com", cookie="tok-env"))

        assert runner.invoke(main, ["login", "--json"]).exit_code == OK

        assert json.loads(lane.read_text())["email"] == "env@example.com"
        assert not (Path(os.environ["HOME"]) / ".config" / "archimedes" / "session.json").exists()


# ── permissions ──────────────────────────────────────────────────────


class TestThePermissionBitSurvivesTheOverride:
    def test_an_overridden_path_is_still_written_600(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        path = _seed(tmp_path / "lane.json", email="a@example.com", cookie="tok")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, f"{path} is {oct(path.stat().st_mode)}"

    def test_a_pre_existing_world_readable_file_is_tightened(self, tmp_path, monkeypatch):
        """``O_CREAT``'s mode argument applies only when the file is created, so a session
        file left behind by an older version keeps its own looser mode unless something
        chmods it. This is the input that would leave a credential world-readable."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        path = tmp_path / "loose.json"
        path.write_text("{}\n")
        path.chmod(0o644)

        _seed(path, email="a@example.com", cookie="tok")

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_a_missing_parent_directory_is_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        path = _seed(tmp_path / "deep" / "nested" / "lane.json", email="a@example.com", cookie="tok")
        assert path.exists()
        assert json.loads(path.read_text())["email"] == "a@example.com"


# ── the lock ─────────────────────────────────────────────────────────


@requires_flock
class TestTheAdvisoryLock:
    """Each of these fails if the corresponding ``fcntl.flock`` call is deleted from
    ``session.py`` — the operation would then finish while another process holds the
    lock, which is exactly the interleaving the lock exists to prevent."""

    def test_a_write_waits_for_another_processes_exclusive_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv(SESSION_FILE_ENV, str(tmp_path / "session.json"))
        path = tmp_path / "session.json"

        with _held_by_another_process(path):
            finished = _in_a_thread(
                lambda: save_session(api_url="https://x.test", cookies={SESSION_COOKIE_NAME: "t"}, email="a@b.test")
            )
            assert not finished.wait(0.5), "save_session wrote while another process held LOCK_EX"

        assert finished.wait(30), "save_session never completed after the lock was released"
        assert json.loads(path.read_text())["email"] == "a@b.test"

    def test_a_read_waits_for_another_processes_exclusive_lock(self, tmp_path, monkeypatch):
        """Why the read side needs a lock at all: a writer truncates before it rewrites, so
        an unlocked reader can land in that window and report "not logged in" for a session
        that is perfectly good."""
        path = tmp_path / "session.json"
        _seed(path, email="a@example.com", cookie="tok-a")
        monkeypatch.setenv(SESSION_FILE_ENV, str(path))
        loaded: list[dict | None] = []

        with _held_by_another_process(path):
            finished = _in_a_thread(lambda: loaded.append(load_session()))
            assert not finished.wait(0.5), "load_session read while another process held LOCK_EX"

        assert finished.wait(30), "load_session never completed after the lock was released"
        assert loaded == [
            {
                "api_url": "https://archimedes-arc.com",
                "cookies": {SESSION_COOKIE_NAME: "tok-a"},
                "email": "a@example.com",
            }
        ]

    def test_two_readers_do_not_block_each_other(self, tmp_path, monkeypatch):
        """The other side of the claim: the read lock is SHARED. If it were exclusive,
        every concurrent ``meter`` on a runner would serialize behind every other one for
        no reason."""
        path = tmp_path / "session.json"
        _seed(path, email="a@example.com", cookie="tok-a")
        monkeypatch.setenv(SESSION_FILE_ENV, str(path))

        with _held_by_another_process(path, mode="shared"):
            finished = _in_a_thread(load_session)
            assert finished.wait(30), "load_session blocked behind another reader's shared lock"

    def test_the_lock_is_released_when_the_write_finishes(self, tmp_path, monkeypatch):
        """A lock held past the write would deadlock the next command in the same lane."""
        path = tmp_path / "session.json"
        monkeypatch.setenv(SESSION_FILE_ENV, str(path))

        save_session(api_url="https://x.test", cookies={SESSION_COOKIE_NAME: "t1"}, email="a@b.test")
        save_session(api_url="https://x.test", cookies={SESSION_COOKIE_NAME: "t2"}, email="a@b.test")

        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises BlockingIOError if still held
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        assert json.loads(path.read_text())["cookies"] == {SESSION_COOKIE_NAME: "t2"}
