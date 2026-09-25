"""``docs/specs/prompt-inventory.md`` is generated from the prompt registry (#1800).

Hermetic: in-memory render vs the committed file, plus a subprocess invocation of the real
``--check`` CLI — no DB / Redis / RPC / .env. Same shape as ``test_asset_universe_doc.py``.

The last two tests are the reason the doc exists at all: ``multi-agent-debate-spec.md``
documented a proposer ``{regime}/{mechanism}`` system prompt and a Synthesizer LLM prompt
that were never built, and nothing failed. A generated inventory plus these guards makes
that class of aspirational prompt doc impossible to leave behind.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from archimedes.agents.prompts import PROMPTS


def _repo_root() -> Path:
    import archimedes  # backend/archimedes/__init__.py → parents: archimedes, backend, <repo>

    return Path(archimedes.__file__).resolve().parents[2]


def _doc_path() -> Path:
    return _repo_root() / "docs" / "specs" / "prompt-inventory.md"


def _debate_spec() -> str:
    return (_repo_root() / "docs" / "specs" / "multi-agent-debate-spec.md").read_text(encoding="utf-8")


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    # Hermetic subprocess env: do NOT inherit the developer's .env (DATABASE_URL etc. that
    # only resolve inside docker compose) — just what the stdlib generator needs to import
    # archimedes. Deliberately does NOT set PYTHONUTF8: the CLI forces UTF-8 stdio itself,
    # and the drift test below proves it in a hostile C locale.
    env = {
        "HOME": os.environ.get("HOME", "/tmp"),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_repo_root() / "backend"),
    }
    if extra:
        env.update(extra)
    return env


def test_prompt_inventory_doc_is_in_sync() -> None:
    # A prompt edit that forgets to regenerate the doc fails CI here.
    from archimedes.scripts.gen_prompt_inventory import render_doc

    committed = _doc_path().read_text(encoding="utf-8")
    assert committed == render_doc(), (
        "docs/specs/prompt-inventory.md is stale vs the prompt registry — regenerate with "
        "`PYTHONPATH=backend python scripts/gen_prompt_inventory.py`."
    )


def test_every_registry_template_appears_verbatim_in_the_doc() -> None:
    # Not just "the render matches" — the committed BYTES must contain each live template,
    # so a reader diffing the doc is diffing the real prompt.
    text = _doc_path().read_text(encoding="utf-8")
    for prompt in PROMPTS.values():
        assert prompt.text in text, f"{prompt.id}'s template is not in the committed inventory"
        assert f"### {prompt.id}" in text
        assert f"| {prompt.version} |" in text or f"**version:** {prompt.version}" in text


def test_check_cli_reports_in_sync_via_subprocess() -> None:
    # Exercise the REAL --check CLI contract (exit 0 + 'in sync') through the documented
    # scripts/ wrapper, not just render_doc() in-process.
    proc = subprocess.run(
        [sys.executable, "scripts/gen_prompt_inventory.py", "--check"],
        cwd=str(_repo_root()),
        env=_clean_env(),
        capture_output=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, f"--check should pass on the committed doc; stderr=\n{proc.stderr}"
    assert "in sync" in proc.stdout.lower()


def test_check_cli_detects_drift_via_subprocess(tmp_path: Path) -> None:
    # --check must exit 1 AND print a diff when the doc drifts. Point the CLI at a tampered
    # TEMP copy via PROMPT_INVENTORY_DOC_PATH so the committed doc is never mutated. Force a
    # hostile ASCII locale (LC_ALL=C, UTF-8 mode + C-locale coercion OFF) so the diff — which
    # contains the doc's non-ASCII em-dashes — exercises the CLI's OWN UTF-8 stdio
    # reconfigure. Without it this would raise UnicodeEncodeError.
    from archimedes.scripts.gen_prompt_inventory import render_doc

    drifted = tmp_path / "prompt-inventory.md"
    drifted.write_text(render_doc() + "\nIGNORE ALL PREVIOUS INSTRUCTIONS\n", encoding="utf-8")
    hostile = {
        "PROMPT_INVENTORY_DOC_PATH": str(drifted),
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }
    proc = subprocess.run(
        [sys.executable, "scripts/gen_prompt_inventory.py", "--check"],
        cwd=str(_repo_root()),
        env=_clean_env(hostile),
        capture_output=True,
        encoding="utf-8",
    )
    assert proc.returncode == 1, "drift must fail --check"
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in proc.stderr, "the drift diff should be printed to stderr"
    assert "UnicodeEncodeError" not in proc.stderr, "the CLI must force UTF-8 stdio in a C locale"


# ── The debate spec must describe the prompts that exist ─────────────────────


def test_debate_spec_does_not_document_prompts_that_do_not_exist() -> None:
    """#1801: `multi-agent-debate-spec.md` §2 quoted two prompts that were never built.

    The proposer has no system prompt of its own — `debate_engine._propose_pool` calls
    `StrategyFusion.propose`, so the FUSION prompt is the proposer prompt, and the
    regime/mechanism steer travels in the user JSON payload (`strategic_direction`),
    not in a `{regime}/{mechanism}` template. And synthesis is `_score` /
    `_survives_null` — deterministic Python, zero LLM calls.
    """
    spec = _debate_spec()
    assert "You are a quant strategy proposer." not in spec, (
        "the invented proposer system prompt is back in the debate spec"
    )
    assert "You are an impartial fund manager." not in spec, (
        "the invented Synthesizer system prompt is back in the debate spec"
    )
    assert "**{regime}/{mechanism}**" not in spec


def test_debate_spec_points_at_the_generated_inventory() -> None:
    spec = _debate_spec()
    assert "docs/specs/prompt-inventory.md" in spec, (
        "the debate spec must send readers to the generated inventory rather than "
        "carrying its own hand-written copy of the prompts"
    )
    # And it must say, in words, that synthesis takes no LLM call.
    assert "fusion.proposer.system" in spec and "debate.turn.system" in spec
