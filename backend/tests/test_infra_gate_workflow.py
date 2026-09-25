"""Guard for ``.github/workflows/infra-gate.yml`` (issue #1483).

Issue #1483: nothing in CI parsed a ``.tf`` file, so a syntax error in
``infra/ecs.tf`` — the file holding ``PAYMENTS_DRY_RUN`` and the generation
price/caps — first surfaced at ``terraform apply``, which is also the prod
deploy path. ``infra-gate.yml`` closes that. The gate itself is only worth
anything while its ``on.pull_request.paths`` filter still selects the ``.tf``
files it claims to cover: delete ``infra/**`` from that list and the workflow
stays present, stays green-by-never-running, and the tree goes unchecked again.
That silent-downgrade is exactly the CLAUDE.md § "the green check may not mean
what you think" failure mode, so it gets a test rather than a convention.

**Stdlib only, on purpose.** PyYAML is not in ``backend/requirements.txt`` and
is not imported anywhere in ``backend/``; the CI unit job installs only from
that file (``quality-gate.yml`` line 135), so ``import yaml`` here would pass
locally and ``ImportError`` in CI — the exact "works on my machine" split the
testing conventions exist to prevent. ``_pull_request_paths`` is a small
indentation scanner instead, and it is fail-loud: if it stops understanding the
file it returns nothing and every assertion below fails. Its output was
cross-checked against ``yaml.safe_load`` when written (both return the same
three patterns; note ``safe_load`` parses the ``on:`` key as the boolean
``True`` under YAML 1.1, which is why a naive ``doc["on"]`` would have been
wrong anyway).

Hermetic: reads two files off disk, no env, no network, no services.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "infra-gate.yml"
_BRANCH_PROTECTION = _REPO_ROOT / "scripts" / "setup-branch-protection.sh"

# Directories that are not ours to lint: submodule checkouts, vendored JS, and
# terraform's own provider cache (gitignored, but present after a local init).
_SKIP_DIRS = {".git", ".terraform", "node_modules", "submodules"}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_body(lines: list[str], start: int, outer_indent: int) -> list[str]:
    """Lines belonging to the block opened at ``start``, i.e. everything more
    indented than ``outer_indent`` until the first line that is not."""
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            body.append(line)
            continue
        if _indent(line) <= outer_indent:
            break
        body.append(line)
    return body


def _find_key(lines: list[str], key: str) -> int | None:
    """Index of the shallowest ``<key>:`` line, or None."""
    for i, line in enumerate(lines):
        if line.strip() in (f"{key}:", key):
            return i
    return None


def _pull_request_paths(text: str) -> list[str]:
    """``on.pull_request.paths`` from a workflow file, stdlib only.

    Returns ``[]`` rather than raising when the shape is not what we expect —
    the callers assert on the contents, so an unparseable file fails loudly
    instead of vacuously passing.
    """
    lines = text.splitlines()

    on_idx = next(
        (i for i, ln in enumerate(lines) if _indent(ln) == 0 and ln.strip() == "on:"),
        None,
    )
    if on_idx is None:
        return []
    on_body = _block_body(lines, on_idx, 0)

    pr_idx = _find_key(on_body, "pull_request")
    if pr_idx is None:
        return []
    pr_body = _block_body(on_body, pr_idx, _indent(on_body[pr_idx]))

    paths_idx = _find_key(pr_body, "paths")
    if paths_idx is None:
        return []
    paths_body = _block_body(pr_body, paths_idx, _indent(pr_body[paths_idx]))

    out: list[str] = []
    for line in paths_body:
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        out.append(stripped[2:].strip().strip("\"'"))
    return out


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """GitHub path-filter glob → regex.

    Per GitHub's filter-pattern cheat sheet: ``**`` crosses ``/``, ``*`` does
    not, ``?`` is a single non-``/`` character. Anchored at both ends.
    """
    out = ["^"]
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _matches_any(patterns: list[str], path: str) -> bool:
    return any(_pattern_to_regex(p).match(path) for p in patterns)


def _tf_files() -> list[str]:
    return sorted(
        p.relative_to(_REPO_ROOT).as_posix() for p in _REPO_ROOT.rglob("*.tf") if not _SKIP_DIRS & set(p.parts)
    )


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert _WORKFLOW.exists(), f"{_WORKFLOW} is missing — issue #1483 is not done"
    return _WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def paths(workflow_text: str) -> list[str]:
    parsed = _pull_request_paths(workflow_text)
    assert parsed, "could not read on.pull_request.paths out of infra-gate.yml"
    return parsed


def test_path_filter_lists_the_infra_tree(paths: list[str]) -> None:
    """The literal the issue asks for. ``infra/**`` is what makes an edit to
    ``infra/ecs.tf`` trigger the gate at all."""
    assert "infra/**" in paths, f"on.pull_request.paths lost 'infra/**': {paths}"


def test_path_filter_selects_every_tf_file_in_the_tree(paths: list[str]) -> None:
    """The claim the workflow's name makes, checked against the tree rather
    than against a hard-coded list — a ``.tf`` file added in a directory nobody
    thought about must either be covered or turn this red."""
    tf_files = _tf_files()
    assert tf_files, "no .tf files found — the walker is broken, not the repo"
    uncovered = [f for f in tf_files if not _matches_any(paths, f)]
    assert not uncovered, (
        f"{len(uncovered)} .tf file(s) would not trigger infra-gate.yml: "
        f"{uncovered}. Either add a pattern to on.pull_request.paths or add the "
        f"directory to the job matrix."
    )


def test_the_workflow_actually_runs_fmt_and_validate(workflow_text: str) -> None:
    """A path filter that triggers a job doing nothing is not a gate."""
    assert "terraform fmt -check -recursive" in workflow_text
    assert "terraform validate" in workflow_text or "validate -no-color" in workflow_text
    assert "-backend=false" in workflow_text, (
        "init must use -backend=false: no credentials, no S3 state read (#1483 anti-goal)"
    )


def test_the_workflow_never_runs_plan(workflow_text: str) -> None:
    """#1483 anti-goal. ``plan`` needs credentials and reads remote state, and
    state holds a ``tls_private_key`` — CLAUDE.md § git safety treats the
    backend as a secrets store."""
    run_lines = [
        ln.strip() for ln in workflow_text.splitlines() if "terraform " in ln and not ln.strip().startswith("#")
    ]
    offenders = [ln for ln in run_lines if re.search(r"\bterraform\b.*\bplan\b", ln)]
    assert not offenders, f"infra-gate must not run terraform plan: {offenders}"


def test_the_workflow_carries_the_do_not_require_warning(workflow_text: str) -> None:
    """Path-filtered + required = every non-infra PR permanently unmergeable.
    docs-gate.yml boxes this warning; so must this one, or the next person
    reading the CI table will add it to branch protection."""
    header = workflow_text.split("name: infra-gate")[0]
    assert "DO NOT MARK THIS A REQUIRED STATUS CHECK." in header
    assert "fallback" in header


def test_the_workflow_is_not_a_required_status_check() -> None:
    """Acceptance criterion 4: it must not appear in the required-check list."""
    script = _BRANCH_PROTECTION.read_text(encoding="utf-8")
    contexts_line = next((ln for ln in script.splitlines() if ln.startswith("CONTEXTS=")), None)
    assert contexts_line, "CONTEXTS= line vanished from setup-branch-protection.sh"
    assert "infra-gate" not in contexts_line.lower()
    assert "terraform" not in contexts_line.lower()
