"""The ``PAPER_ADVANCE_ENABLED`` switch and the loop's cycle contract (#1632).

Why this switch exists, and why a test file rather than a line in a runbook:
the paper-advance tick is the one scheduled job ever suspected of taking the
*web tier* down rather than merely failing. A backend container was dying with
``Fatal Python error: Aborted``; a C-level abort cannot be caught by the loop's
fail-soft ``except`` arm, so the task died at the first tick —
``+PAPER_ADVANCE_STARTUP_DELAY_S`` after boot — and its replacement died the
same way: a cold-fleet spiral. The only lever that works against an uncatchable
abort is not running the code, which is what made this switch load-bearing.

#1725 defaulted it ON so unset was a no-op; task-def :211 proved that hole
(cloned last-good, name absent, tick started, /health 502 at 240s). Unset is
still OFF and the code default is still ``"false"``.

The DEPLOYED value is now ``"true"`` (#1778, 2026-09-01). #1632's found cause
turned out to be elsewhere — concurrent ``/health`` corpus loads racing in
session teardown, fixed by #1740 and caught with this flag off — so the tick's
own frame is unproven rather than cleared, and what carries the risk is the
#1728 child-interpreter boundary, not a fix. The deployed-config tests at the
bottom of this file therefore assert the ARMED state and the written pull-back
procedure, in the same spirit as before: the value cannot move without moving
the prose that explains it.

The fleet-lock tests cover the other half of arming: more than one task ticks,
so exactly one of them may do the work in any cycle.

The DDL tests cover #1818, the P0 that arming exposed: the cycle used to call
``init_db()`` — hand-rolled ``ALTER TABLE`` patches — on every tick in every
task, which wedged Postgres for 94 minutes on 2026-09-03. The seam is now armed
to raise in ``_drive_one_tick``, so every driven tick in this file is a guard
against it coming back.

Hermetic: no DB, no network, no app import beyond the service module. The
loop's ``asyncio.sleep`` is stubbed to break out after exactly one tick, its DB
seam is a fake session, and the agent-execution module is a stub.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import sys
import types
from typing import NamedTuple

import pytest
from archimedes.services import paper_trading


class _StopLoop(Exception):
    """Breaks ``paper_advance_loop`` out after one tick.

    Raised from the stubbed ``asyncio.sleep``. Both sleeps the loop performs
    sit OUTSIDE its ``try``, so this propagates instead of being swallowed by
    the fail-soft arm — which is also, incidentally, a check that the arm has
    not been widened to cover the whole body.
    """


# ─── advance_enabled(): the value contract ─────────────────────────────


class TestAdvanceEnabledValueContract:
    def test_unset_is_off_so_a_cloned_task_def_cannot_tick(self, monkeypatch):
        """Task-def :211 died because unset meant ON. Unset now means OFF."""
        monkeypatch.delenv("PAPER_ADVANCE_ENABLED", raising=False)
        assert paper_trading.advance_enabled() is False

    def test_the_getenv_default_literal_is_false(self):
        """Flip-back of the default cannot be a docstring-only change."""
        from pathlib import Path

        src = Path(paper_trading.__file__).read_text(encoding="utf-8")
        assert 'os.getenv("PAPER_ADVANCE_ENABLED", "false")' in src
        assert 'os.getenv("PAPER_ADVANCE_ENABLED", "true")' not in src

    @pytest.mark.parametrize("value", ["false", "FALSE", "False", "0", "no", "off", "  off  ", "OFF"])
    def test_falsy_literals_disable_case_and_whitespace_insensitively(self, monkeypatch, value):
        monkeypatch.setenv("PAPER_ADVANCE_ENABLED", value)
        assert paper_trading.advance_enabled() is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", ""])
    def test_explicit_non_falsy_values_still_enable(self, monkeypatch, value):
        """An explicit value that is not a recognised falsy literal still enables.

        Unset is OFF (see above). A present-but-typoed value is a different
        mistake — it is not the :211 cloned-task-def hole.
        """
        monkeypatch.setenv("PAPER_ADVANCE_ENABLED", value)
        assert paper_trading.advance_enabled() is True

    def test_a_typo_does_not_silently_disable(self, monkeypatch):
        monkeypatch.setenv("PAPER_ADVANCE_ENABLED", "flase")
        assert paper_trading.advance_enabled() is True


# ─── The loop actually consults it ─────────────────────────────────────


class _Tick(NamedTuple):
    """What one driven cycle did.

    ``sleeps`` is the load-bearing member for the skip paths: it proves a cycle
    that did no work still reached the interval sleep at the bottom of the
    loop instead of spinning.
    """

    advances: int
    sleeps: list[float]
    agent_calls: int
    statements: list[str]
    init_db_calls: int


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeSession:
    """The DB seam. Records SQL text so the fleet lock can be observed."""

    def __init__(self, *, lock_result=True, statements=None, execute_raises=None):
        self._lock_result = lock_result
        self._execute_raises = execute_raises
        self.statements = statements if statements is not None else []

    def commit(self):
        return None

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        if self._execute_raises is not None:
            raise self._execute_raises
        return _FakeResult(self._lock_result)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _drive_one_tick(
    monkeypatch,
    *,
    enabled_env: str | None,
    lock_result: bool = True,
    lock_raises: BaseException | None = None,
) -> _Tick:
    """Run exactly one iteration of ``paper_advance_loop`` and report what ran.

    ``lock_result=False`` makes every session report the fleet advisory lock as
    held by someone else — the two-tasks-at-+240s case.
    """
    if enabled_env is None:
        monkeypatch.delenv("PAPER_ADVANCE_ENABLED", raising=False)
    else:
        monkeypatch.setenv("PAPER_ADVANCE_ENABLED", enabled_env)
    monkeypatch.setenv("PAPER_ADVANCE_STARTUP_DELAY_S", "0")
    monkeypatch.setenv("PAPER_ADVANCE_INTERVAL_HOURS", "24")

    calls = {"advance_all": 0, "agent": 0, "init_db": 0}
    sleeps: list[float] = []
    statements: list[str] = []

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds, *args, **kwargs):
        sleeps.append(seconds)
        # 1st = startup delay, 2nd = the interval sleep after one tick.
        if len(sleeps) >= 2:
            raise _StopLoop
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # Boundary doubles for the DB seam the loop imports from archimedes.db.
    # Stubbed at the module the loop imports FROM, since it imports inside the
    # function body.
    import archimedes.db as db

    # NOT a no-op stub (#1818). The cycle must never run DDL, so the seam that
    # would run it is armed to blow up: every driven tick in this file is now a
    # standing guard, and a re-introduced `init_db()` call cannot pass silently.
    # It raises rather than merely counting because a counter alone would be
    # satisfied by a call the loop's fail-soft arm swallowed — with this, the
    # arm swallows a poisoned tick and `advances` drops to 0 as well.
    def exploding_init_db(*a, **k):
        calls["init_db"] += 1
        raise AssertionError("paper_advance_loop called init_db() — the #1818 root cause")

    monkeypatch.setattr(db, "init_db", exploding_init_db)
    monkeypatch.setattr(
        db,
        "get_session",
        lambda *a, **k: _FakeSession(lock_result=lock_result, statements=statements, execute_raises=lock_raises),
    )

    def fake_advance_all(session):
        calls["advance_all"] += 1
        return {"advanced": 0}

    monkeypatch.setattr(paper_trading, "advance_all", fake_advance_all)

    # The agent pass is a real module import inside the loop; stub it so this
    # file stays hermetic and so "did the second pass run?" is observable.
    agent_mod = types.ModuleType("archimedes.services.paper_agent_execution")

    async def fake_advance_agent_execution(session):
        calls["agent"] += 1
        return {"agent": "stub"}

    agent_mod.advance_agent_execution = fake_advance_agent_execution
    monkeypatch.setitem(sys.modules, "archimedes.services.paper_agent_execution", agent_mod)

    with pytest.raises(_StopLoop):
        asyncio.run(paper_trading.paper_advance_loop())

    return _Tick(calls["advance_all"], sleeps, calls["agent"], statements, calls["init_db"])


class TestLoopHonoursTheSwitch:
    def test_off_skips_the_work_entirely(self, monkeypatch, caplog):
        """The whole point: no replay runs, so nothing can reach the aborting
        cache write."""
        with caplog.at_level(logging.WARNING, logger=paper_trading.__name__):
            tick = _drive_one_tick(monkeypatch, enabled_env="false")

        assert tick.advances == 0, "advance_all ran with the kill switch pulled"
        assert any("PAPER_ADVANCE_ENABLED is off" in r.message for r in caplog.records)
        assert any("#1632" in r.message for r in caplog.records)

    def test_the_skip_log_is_a_warning_not_an_info(self, monkeypatch, caplog):
        """Off means paper ledgers stop advancing — a product claim suspended.
        That is not INFO-level news, and an operator scanning WARNING+ during
        an incident must see it."""
        with caplog.at_level(logging.DEBUG, logger=paper_trading.__name__):
            _drive_one_tick(monkeypatch, enabled_env="off")

        skips = [r for r in caplog.records if "PAPER_ADVANCE_ENABLED is off" in r.message]
        assert skips, "no skip line was logged at all"
        assert all(r.levelno >= logging.WARNING for r in skips)

    def test_disabled_tick_still_sleeps_the_full_interval(self, monkeypatch):
        """The adversarial one, and the reason the skip is an ``else`` rather
        than a ``continue``.

        A ``continue`` would jump past the interval sleep at the bottom of the
        loop and spin the event loop hot forever — turning a mitigation into a
        CPU incident on a fleet that is already cycling. Two sleeps observed
        (startup + interval) is the proof it fell through normally.
        """
        tick = _drive_one_tick(monkeypatch, enabled_env="false")

        assert len(tick.sleeps) == 2, f"expected startup + interval sleep, saw {tick.sleeps}"
        assert tick.sleeps[0] == 0.0  # startup delay, as set
        assert tick.sleeps[1] == 24 * 3600.0  # the full interval — not a hot spin

    def test_unset_skips_the_work(self, monkeypatch):
        """The :211 hole: cloned task-def, name absent, must not advance."""
        tick = _drive_one_tick(monkeypatch, enabled_env=None)

        assert tick.advances == 0, "unset PAPER_ADVANCE_ENABLED must not start the tick"

    def test_explicit_true_runs_the_work(self, monkeypatch):
        """MUTATION CHECK for every skip assertion above.

        Without this, a gate that disabled the tick *unconditionally* — or a
        loop body accidentally deleted — would pass the whole disabled-path
        suite. This is the test that fails if the flag stops being a flag.
        """
        tick = _drive_one_tick(monkeypatch, enabled_env="true")

        assert tick.advances == 1


# ─── One ticker per fleet (#1778) ──────────────────────────────────────


class TestOneTickerPerFleet:
    """More than one task ticks; exactly one of them may do the work.

    Both web tasks boot together and their children both reach the loop at
    ``+PAPER_ADVANCE_STARTUP_DELAY_S``. ``PaperDailyReturn(deployment_id,
    date)`` is unique and ``advance_all`` commits the cycle in ONE
    transaction, so a second ticker does not lose one duplicate row — it loses
    everything it appended for every other deployment in that pass. The
    advisory lock is what makes "extra runs are harmless" true rather than
    true-by-constraint-violation.
    """

    def test_the_lock_is_taken_before_any_ledger_work(self, monkeypatch):
        tick = _drive_one_tick(monkeypatch, enabled_env="true")

        assert tick.advances == 1
        assert any("pg_try_advisory_xact_lock" in s for s in tick.statements), (
            f"the cycle ran without asking for the fleet lock; SQL seen: {tick.statements}"
        )

    def test_a_held_lock_skips_the_whole_cycle_including_the_agent_pass(self, monkeypatch, caplog):
        """The adversarial one: losing the lock must stop BOTH passes.

        Gating only ``advance_all`` would leave ``advance_agent_execution``
        running in every task at once, which is the same collision one layer
        down — and the lock would be advertising a guarantee it does not give.
        """
        with caplog.at_level(logging.INFO, logger=paper_trading.__name__):
            tick = _drive_one_tick(monkeypatch, enabled_env="true", lock_result=False)

        assert tick.advances == 0, "advance_all ran while another task held the lock"
        assert tick.agent_calls == 0, "the agent pass ran while another task held the lock"
        assert any(paper_trading.LOCK_HELD_LOG in r.message for r in caplog.records), (
            "standing down was silent — indistinguishable from never having run"
        )

    def test_a_losing_cycle_still_sleeps_the_full_interval(self, monkeypatch):
        """Standing down must not become a hot spin, same as the disabled path."""
        tick = _drive_one_tick(monkeypatch, enabled_env="true", lock_result=False)

        assert len(tick.sleeps) == 2, f"expected startup + interval sleep, saw {tick.sleeps}"
        assert tick.sleeps[1] == 24 * 3600.0

    def test_the_winner_runs_both_passes(self, monkeypatch):
        """MUTATION CHECK for the two skip assertions above.

        Without this, a lock helper that returned ``False`` unconditionally —
        i.e. a fleet whose ledgers never advance again — would pass every
        contended-path test in this class.
        """
        tick = _drive_one_tick(monkeypatch, enabled_env="true", lock_result=True)

        assert tick.advances == 1
        assert tick.agent_calls == 1

    def test_a_broken_lock_check_fails_open(self, monkeypatch, caplog):
        """A lock that cannot be asked must not silently freeze every ledger.

        Failing closed here would be a config-shaped way to suspend the product
        claim with no operator having decided anything — the exact failure this
        module's kill switch is written to be the opposite of. The unique
        constraint is still underneath.
        """
        with caplog.at_level(logging.WARNING, logger=paper_trading.__name__):
            tick = _drive_one_tick(
                monkeypatch,
                enabled_env="true",
                lock_raises=RuntimeError("connection reset"),
            )

        assert tick.advances == 1, "a failed lock check stopped the ledger"
        assert any("proceeding UNLOCKED" in r.message for r in caplog.records)

    def test_non_postgres_wins_without_asking(self):
        """SQLite dev and the hermetic suites have no fleet to contend with."""

        class _Bind:
            dialect = type("D", (), {"name": "sqlite"})()

        class _Session:
            def __init__(self):
                self.executed = []

            def get_bind(self):
                return _Bind()

            def execute(self, *a, **k):  # pragma: no cover - must not be reached
                self.executed.append(a)
                raise AssertionError("asked SQLite for a Postgres advisory lock")

        session = _Session()
        assert paper_trading.try_take_paper_advance_lock(session) is True
        assert session.executed == []

    def test_the_lock_is_transaction_scoped_not_session_scoped(self):
        """Load-bearing, and invisible at runtime until it deadlocks the fleet.

        ``pg_advisory_lock``/``pg_try_advisory_lock`` are held by the
        CONNECTION, and SQLAlchemy returns connections to a pool rather than
        closing them — so a missed unlock would make one task the permanent
        ticker and freeze everyone else's ledger with no error anywhere. The
        xact variant is released by the pool's ROLLBACK, so the worst case is
        releasing too eagerly.
        """
        from pathlib import Path

        source = Path(paper_trading.__file__).read_text(encoding="utf-8")
        assert "pg_try_advisory_xact_lock" in source
        assert "pg_try_advisory_lock(" not in source
        assert "pg_advisory_lock(" not in source


# ─── The child never runs DDL (#1818) ──────────────────────────────────


class TestTheCycleNeverRunsDdl:
    """The 2026-09-03 root cause: the tick called ``init_db()`` every cycle.

    ``init_db()`` runs hand-rolled ``ALTER TABLE … ADD COLUMN IF NOT EXISTS``
    patches. Even a no-op one takes AccessExclusiveLock on the table for the
    rest of its transaction, and a *waiting* exclusive-lock request queues every
    later reader behind it. Two ECS tasks → two children → two concurrent DDL
    transactions on a 24-hour clock; on 2026-09-03 they wedged each other in a
    cycle PostgreSQL cannot see (the blocker was not itself waiting), and
    production served 504s for 94 minutes.

    Two guards, because either alone is escapable. The behavioural one would
    pass if someone moved the DDL behind another module's helper; the source one
    would pass if the seam were called through a variable. Together they say:
    this function does not name ``init_db``, and driving it does not reach it.

    Schema belongs to the migrate task (``alembic upgrade head``) and to the web
    process's single boot-time call in ``main.py`` — not to a 24-hourly ticker.
    """

    def test_driving_a_tick_never_reaches_init_db(self, monkeypatch):
        """Behavioural. ``_drive_one_tick`` arms ``archimedes.db.init_db`` to
        raise, so a re-introduced call shows up twice over: the counter moves,
        and the loop's fail-soft arm eats the exception so the ledger never
        advances."""
        tick = _drive_one_tick(monkeypatch, enabled_env="true")

        assert tick.init_db_calls == 0, "the cycle ran schema DDL — the #1818 root cause is back"
        assert tick.advances == 1, "the cycle did not complete"

    def test_a_contended_cycle_also_runs_no_ddl(self, monkeypatch):
        """The loser must not do schema work either.

        This is the half that actually wedged: on 2026-09-03 both children
        reached ``init_db()`` *before* asking for the fleet lock, so standing
        down bought nothing — the DDL had already been issued. The lock is
        asked for first now, and neither side issues DDL at all.
        """
        tick = _drive_one_tick(monkeypatch, enabled_env="true", lock_result=False)

        assert tick.init_db_calls == 0
        assert tick.advances == 0

    def test_the_loop_source_does_not_name_init_db(self):
        """Source-level, via AST so prose cannot satisfy or break it.

        The docstring and the inline comment both *mention* ``init_db`` on
        purpose — that is the record of why it is gone. A substring search would
        therefore be red forever. An AST walk asks the only question that
        matters: does this function import, reference, or call that name?
        """
        tree = ast.parse(inspect.getsource(paper_trading.paper_advance_loop))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                offenders += [
                    f"line {node.lineno}: imports {alias.name}" for alias in node.names if alias.name == "init_db"
                ]
            elif isinstance(node, ast.Name) and node.id == "init_db":
                offenders.append(f"line {node.lineno}: references init_db")
            elif isinstance(node, ast.Attribute) and node.attr == "init_db":
                offenders.append(f"line {node.lineno}: references .init_db")

        assert not offenders, "paper_advance_loop names init_db in executable code (#1818): " + "; ".join(offenders)

    def test_the_docstring_says_where_schema_belongs(self):
        """A deletion with no reason attached is re-added by the next person who
        wants a column. The docstring has to carry the incident and the
        alternative, or this guard is guarding a coincidence."""
        doc = paper_trading.paper_advance_loop.__doc__ or ""

        assert "#1818" in doc
        assert "alembic" in doc.lower(), "the docstring does not say who owns schema instead"

    def test_init_db_is_still_importable_and_still_the_boot_path(self):
        """MUTATION CHECK for the guards above.

        Deleting ``init_db`` outright — or letting the web process stop calling
        it — would make every assertion in this class pass while breaking the
        transitional columns the read paths depend on. The function must still
        exist, and ``main.py`` must still call it once at boot.
        """
        from pathlib import Path

        import archimedes.db as db

        assert callable(db.init_db)
        main_py = Path(paper_trading.__file__).resolve().parents[1] / "main.py"
        assert "init_db()" in main_py.read_text(encoding="utf-8"), (
            "the web process no longer runs the boot schema patches at all"
        )


class TestSwitchIsArmedInTheDeployedConfig:
    """Arming is only real if the deploy path actually sets it — and says why.

    ``infra/ecs.tf`` is the terraform pin; ``deploy.yml`` (via
    ``ecs_rewrite_task_def.py``) is the path that actually ships, because it
    clones the live task-def and never applies terraform. Both must agree.

    These tests are written to fail on a value change in EITHER direction with
    the prose left behind, which is the property that mattered when the switch
    was pulled and matters equally now that it is armed: an unexplained
    ``"true"`` is how an experiment becomes permanent by forgetting, exactly as
    an unexplained ``"false"`` was.
    """

    @staticmethod
    def _paper_advance_comment_block() -> str:
        """The contiguous comment block directly above the pinned line.

        Read as a block, not as a whole-file substring search: ``ecs.tf`` is
        ~900 lines of other people's comments, and asserting a word appears
        *somewhere* in it would let this guard be satisfied by an unrelated
        line — which is how a comment test quietly stops testing anything.
        """
        from pathlib import Path

        ecs_tf = Path(__file__).resolve().parents[3] / "infra" / "ecs.tf"
        lines = ecs_tf.read_text(encoding="utf-8").splitlines()
        pin = next(i for i, line in enumerate(lines) if "PAPER_ADVANCE_ENABLED" in line and "value" in line)
        start = pin
        while start > 0 and lines[start - 1].strip().startswith("#"):
            start -= 1
        assert start < pin, "the PAPER_ADVANCE_ENABLED pin has no comment block above it at all"
        return "\n".join(lines[start:pin])

    def test_ecs_task_definition_arms_the_tick(self):
        from pathlib import Path

        ecs_tf = Path(__file__).resolve().parents[3] / "infra" / "ecs.tf"
        assert '{ name = "PAPER_ADVANCE_ENABLED", value = "true" }' in ecs_tf.read_text(encoding="utf-8"), (
            "the terraform twin no longer arms the tick — it must match the CI rewrite pin"
        )

    def test_the_comment_describes_the_armed_state_and_names_the_boundary(self):
        block = self._paper_advance_comment_block()

        assert "ARMED" in block, "the comment above the pin no longer says what state the pin is in"
        assert "#1632" in block, "the incident this flag came from is no longer named"
        assert "#1728" in block, (
            "the comment no longer names the child-interpreter boundary — that boundary, not a fix, "
            "is the entire argument for arming this"
        )
        assert "deploy.yml" in block, (
            "ecs.tf no longer names deploy.yml as the path that actually ships — a terraform-only pin "
            "is how last-good 011b6bfc kept flapping"
        )

    def test_the_comment_carries_a_pull_back_procedure(self):
        """A break-glass switch with no written way back is not break-glass.

        The word that used to hold this line was TEMPORARY, which described a
        mitigation. The armed state needs the opposite: not a promise to
        revisit, but the two files an operator edits at 3am and the fact that a
        terraform apply alone will not do it.
        """
        block = self._paper_advance_comment_block()

        assert "PULL IT BACK" in block
        assert "PAPER_ADVANCE_VALUE" in block, "the pull-back procedure does not name the pin that ships"
        assert "ecs_rewrite_task_def.py" in block
        assert "TEMPORARY" not in block, (
            "the comment still calls this a temporary mitigation while the pin reads true — "
            "the two halves must not describe different states"
        )

    def test_deploy_yml_rewrite_is_the_shipping_pin(self):
        from pathlib import Path

        deploy = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "deploy.yml"
        assert "ecs_rewrite_task_def.py" in deploy.read_text(encoding="utf-8"), (
            "deploy.yml no longer invokes the rewrite that pins PAPER_ADVANCE_ENABLED"
        )
