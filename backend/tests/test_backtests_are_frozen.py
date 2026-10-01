"""A backtest is frozen evidence: no clock, no boot hook, anywhere (#1760).

``services/backtest_scheduler.py`` armed a long-lived task from the FastAPI
lifespan that, after a 180 s settle delay, re-ran ``run_backtests()`` for the
whole curated library **in the serving process**. Its backoff was a
process-global set, so every new task started with it empty and re-ran the
storm exactly once. On the 2026-09-01 deploy (task-def 215) that landed at
+180 s on a 1-vCPU Fargate task while a visitor's cold ``GET /api/strategies/``
was in flight: ``CpuUtilized`` 972 of 1024, ``/health`` past the 5 s container
health-check timeout, three failures, task killed — and ECS replaced it with a
fresh task that booted into the same storm.

The owner's call (Dan, 2026-09-01) was not to make the refresh cheaper but to
retire it: generation, backtesting and grading are one-time events, a backtest
is an artifact of evidence with a stated data window, and it is never revisited
on a clock. Policy: ``docs/adr/backtests-are-frozen-evidence.md``. The one
remaining way to produce a curated backtest is the manual CLI
(``python -m archimedes.scripts.run_backtests``), run out-of-band —
``docs/runbooks/curated-backtests.md``.

**Why a static-source guard.** The property being guarded is *"this code path
does not exist"*. Booting the whole app and waiting three minutes to observe
the absence of a refresh is both slower and weaker than asserting the module is
gone, the lifespan does not name it, and nothing but the CLI reaches the runner
— the same reasoning as ``test_lifespan_no_rigor_backfill.py``, whose shape
this test follows.

Hermetic: an import, ``inspect.getsource``, and ``ast.parse``. No DB, no
network, no yfinance, no app boot.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import archimedes.main as main_module
import pytest


def _lifespan_source() -> str:
    # __wrapped__: lifespan is decorated with @asynccontextmanager, so the
    # decorated object is not the function whose body we want to inspect.
    fn = getattr(main_module.lifespan, "__wrapped__", main_module.lifespan)
    return inspect.getsource(fn)


def _code_lines(source: str) -> list[str]:
    """Source lines with ``#`` comments stripped.

    Comments are stripped on purpose: the replacement comment in the lifespan
    explains *why* there is no backtest refresh, and naming the retired thing
    is how that explanation stops it being re-added. A guard that fails on its
    own tombstone would force the explanation out.
    """
    out = []
    for raw in source.splitlines():
        line = raw.split("#", 1)[0]
        if line.strip():
            out.append(line)
    return out


def test_backtest_scheduler_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("archimedes.services.backtest_scheduler")


def test_backtest_scheduler_file_is_gone() -> None:
    services = Path(inspect.getfile(main_module)).resolve().parent / "services"
    assert not (services / "backtest_scheduler.py").exists(), (
        "services/backtest_scheduler.py is back. #1760: its boot-time + age-driven "
        "refresh re-ran the curated library in the web process and got ECS tasks "
        "killed by their own container health check. A curated backtest is produced "
        "out-of-band (docs/runbooks/curated-backtests.md), never on a clock."
    )


def test_lifespan_does_not_arm_a_backtest_refresh() -> None:
    # Lower-cased before the scan: the flag spellings are upper-case
    # (``BACKTEST_REFRESH_ENABLED``), so a case-sensitive scan would let a
    # re-added ``os.getenv("BACKTEST_REFRESH_ENABLED")`` reader walk straight
    # past this guard.
    code = "\n".join(_code_lines(_lifespan_source())).lower()
    assert "backtest_refresh" not in code, (
        "the FastAPI lifespan arms a backtest refresh again (#1760). Every new "
        "ECS task boots into that loop, it runs run_backtests() for the whole "
        "curated library on the 1-vCPU serving task, and /health misses its 5 s "
        "container health-check budget. Backtests are frozen evidence — "
        "docs/adr/backtests-are-frozen-evidence.md."
    )
    assert "backtest_scheduler" not in code, (
        "the FastAPI lifespan imports backtest_scheduler again (#1760). The module "
        "was deleted; see docs/adr/backtests-are-frozen-evidence.md."
    )


def test_no_module_schedules_a_backtest_refresh() -> None:
    """No source under ``archimedes/`` may schedule a backtest refresh.

    Deleting one module is not the property; *nothing anywhere puts backtests
    on a clock* is. This scans committed sources rather than importing them, so
    a new scheduler under a different name still trips as soon as it spells
    ``backtest_refresh`` or imports the retired module.
    """
    package_root = Path(inspect.getfile(main_module)).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        code = "\n".join(_code_lines(text)).lower()
        if "backtest_refresh" in code or "backtest_scheduler" in code:
            offenders.append(str(path.relative_to(package_root)))
    assert not offenders, (
        "these modules reference a backtest refresh loop again: "
        f"{offenders}. #1760 retired periodic and boot-time backtest refresh "
        "everywhere — a curated backtest is produced by an explicit operator run "
        "(docs/runbooks/curated-backtests.md), a generated one exactly once at "
        "generation. Policy: docs/adr/backtests-are-frozen-evidence.md."
    )


# ── the rebrand-proof half ───────────────────────────────────────────────
#
# Every scan above is pinned to the retired loop's own spelling. A loop that
# does exactly what #1760 retired, under a name nobody thought to ban —
# ``services/evidence_freshness.py::curated_evidence_tick`` — walks past all
# three. The token bans are still worth keeping (they name the incident, and
# they are what a reader greps for), but the property that actually holds is
# behavioural: producing a curated backtest means reaching ``run_backtests``,
# whatever the caller is called, so that is the choke point to guard.

_RUN_BACKTESTS_OWNER = "scripts/run_backtests.py"
_RUN_BACKTESTS_MODULE = "archimedes.scripts.run_backtests"


def _reaches_run_backtests(tree: ast.Module) -> list[tuple[int, str]]:
    """Sites in one parsed module that import, call, or dynamically name the CLI.

    AST rather than a text scan so that *comments and prose do not trip it* —
    same escape hatch as :func:`_code_lines` above. A tombstone comment
    explaining why there is no refresh is not a call site; a docstring naming
    the dotted module path is treated as one, because that is what a dynamic
    import looks like from here.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _RUN_BACKTESTS_MODULE:
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == _RUN_BACKTESTS_MODULE:
                found.append((node.lineno, f"from {module} import ..."))
            elif any(alias.name == "run_backtests" for alias in node.names):
                found.append((node.lineno, f"from {module or '.'} import run_backtests"))
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if called == "run_backtests":
                found.append((node.lineno, "run_backtests(...)"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _RUN_BACKTESTS_MODULE in node.value:
            found.append((node.lineno, f"{_RUN_BACKTESTS_MODULE!r} in a string literal"))
    return found


def test_only_the_cli_reaches_run_backtests() -> None:
    """``scripts/run_backtests.py`` is the only site that may reach the runner.

    This is the guard the runbook's "no boot hook, on any tier" constraint
    actually rests on. A rebranded loop passes every token scan above, but it
    cannot produce a backtest without arriving here.

    Generated strategies are backtested inside the generation pipeline, which
    reaches the analytics engine directly and never calls ``run_backtests`` —
    so the CLI genuinely is the only legitimate caller, and this test needs no
    allow-list beyond the owner file itself.
    """
    package_root = Path(inspect.getfile(main_module)).resolve().parent
    offenders: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        rel = path.relative_to(package_root).as_posix()
        if rel == _RUN_BACKTESTS_OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        offenders.extend(f"{rel}:{lineno} ({what})" for lineno, what in _reaches_run_backtests(tree))
    assert not offenders, (
        "these sites import or call run_backtests outside the CLI: "
        f"{offenders}. #1760 retired boot-time and periodic backtest refresh under "
        "EVERY name, not just the two the scans above ban — a curated backtest is "
        "produced by an explicit operator run (docs/runbooks/curated-backtests.md), "
        "a generated one exactly once at generation. Policy: "
        "docs/adr/backtests-are-frozen-evidence.md."
    )
