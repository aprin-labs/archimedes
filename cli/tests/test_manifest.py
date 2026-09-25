"""The manifest is a promise; these tests check it against the truth.

House pattern (compare CRUMB_MAP⊂PAGE_TO_PATH, the slowapi AST invariant):
the hand-written contract in ``manifest.py`` must track the real click
command tree, so an agent reading ``archimedes manifest`` can trust it.
"""

from __future__ import annotations

import json

import click
from click.testing import CliRunner

from archimedes_cli import exits
from archimedes_cli.cli import main
from archimedes_cli.manifest import MANIFEST


def test_every_click_command_appears_in_the_manifest():
    assert set(MANIFEST["commands"].keys()) == set(main.commands.keys())


def test_implemented_flags_match_reality():
    """A command marked implemented must not be the NOT_IMPLEMENTED stub, and
    vice versa — checked by actually invoking the two unimplemented paths."""
    runner = CliRunner()
    # backtest is declared unimplemented → must exit NOT_IMPLEMENTED.
    assert MANIFEST["commands"]["backtest"]["implemented"] is False
    result = runner.invoke(main, ["backtest", "--strategy-path", __file__, "--strategy-class", "X"])
    assert result.exit_code == exits.NOT_IMPLEMENTED
    # manifest is declared implemented → exits OK with parseable JSON.
    assert MANIFEST["commands"]["manifest"]["implemented"] is True
    result = runner.invoke(main, ["manifest"])
    assert result.exit_code == exits.OK
    payload = json.loads(result.stdout)
    assert payload["tool"] == "archimedes"


def test_manifest_flags_match_the_real_commands_both_directions():
    """Flag parity is BIDIRECTIONAL (the drift-proofing promise): every --flag
    the manifest declares must exist on the command, AND every real click
    --option must appear in the manifest — a new flag added to a command
    without updating manifest.py must fail this test (review finding: the
    original one-directional check let that drift through silently)."""
    for name, spec in MANIFEST["commands"].items():
        inputs = spec.get("inputs") or {}
        declared_flags = {k for k in inputs if k.startswith("--")}
        cmd = main.commands[name]
        real_flags = {opt for p in cmd.params if isinstance(p, click.Option) for opt in p.opts if opt.startswith("--")}
        assert declared_flags <= real_flags, (
            f"{name}: manifest declares {declared_flags - real_flags} but the command lacks them"
        )
        assert real_flags <= declared_flags, (
            f"{name}: command has {real_flags - declared_flags} undeclared in the manifest"
        )


def test_manifest_exit_codes_match_exits_module():
    """Every code `exits.py` defines must appear in the manifest, and vice versa.

    Rewritten in 0.2.0 from a hardcoded ``{"0","1","2","3"}`` literal, which had
    already drifted: ``INCOMPLETE = 4`` shipped with #1481 and the manifest never
    learned about it, so an agent branching on this contract read 4 as undefined.
    Deriving the expected set from the module makes that drift impossible to
    repeat — adding a code to `exits.py` without documenting it now fails here.
    """
    declared = {
        str(getattr(exits, name))
        for name in exits.__all__
        # AUTH deliberately shares USAGE's value 2; a set collapses them, which
        # is correct — the manifest documents numbers, not names.
    }
    assert MANIFEST["exit_codes"].keys() == declared
    assert (exits.OK, exits.GATE_FAILED, exits.AUTH, exits.NOT_IMPLEMENTED) == (0, 1, 2, 3)
    assert (exits.INCOMPLETE, exits.PAYMENT_REQUIRED) == (4, 5)
    assert (exits.ACCOUNT_ACTION_REQUIRED, exits.JOB_FAILED, exits.STILL_RUNNING) == (6, 7, 8)


def test_manifest_version_matches_package():
    from archimedes_cli import __version__

    assert MANIFEST["version"] == __version__
