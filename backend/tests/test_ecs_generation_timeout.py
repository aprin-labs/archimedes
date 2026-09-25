"""`GENERATION_TIMEOUT_SECONDS` must be pinned in the prod task definition.

``_generation_timeout_seconds()`` (``backend/archimedes/api/generate_routes.py``)
reads this name off the process environment and falls back to a 600 s code
default when it is missing, non-numeric, or non-positive. That fallback is
correct as a crash guard and wrong as a deployment strategy: the name was
absent from ``infra/ecs.tf`` entirely, so production's hard ceiling on a
generation run was an accident of the code default rather than a decision —
the config-drift class the daily-cap comment in that same file already names.

Nothing catches this at deploy time. A task definition with no
``GENERATION_TIMEOUT_SECONDS`` is valid HCL, passes ``terraform validate``,
starts healthy, and serves ``/health`` — the looser bound only shows up as a
hung job holding a payer's credit and a generation-gate slot for longer than
anyone chose. The failure is silent by construction, so the guard has to be
static.

Two properties are worth pinning, and this file pins both:

  * the name is **present** in the backend container's ``environment`` block; and
  * the literal is a value the real resolver actually **accepts**. This is the
    subtle half. ``"300s"``, ``"5m"``, ``"0"`` and ``"-1"`` are all plausible
    things to type and every one of them is swallowed by the defensive parser
    back to 600 — a task definition that *looks* tightened while running the
    exact default it was written to replace. So the literal is fed through the
    genuine ``_generation_timeout_seconds()``, not through a reimplementation
    of it that could drift from the parser it is meant to mirror.

The ceiling is then required to be *tighter* than the code default, with the
default read out of ``generate_routes.py`` by ``ast`` rather than hard-coded
here: plumbing this name only earns its keep if it buys a tighter bound, and
deriving the comparison keeps that true if either side moves.

Hermetic by construction: the inputs are two files in the repo plus one
``monkeypatch.setenv``. No AWS, no terraform binary, no network, no DB, no
Redis, no ``.env``.

Refs #1605 (2026-08-31 re-grade micro-fix).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from archimedes.api import generate_routes

REPO_ROOT = Path(__file__).resolve().parents[2]
ECS_TF = REPO_ROOT / "infra" / "ecs.tf"
GENERATE_ROUTES_PY = REPO_ROOT / "backend" / "archimedes" / "api" / "generate_routes.py"

ENV_NAME = "GENERATION_TIMEOUT_SECONDS"

# Anti-goal. #1668 plumbs the admission-control knobs and states its own
# anti-goal: "plumbing and tuning are separate changes with separate review
# needs". This change is the tuning half for one name; it must not quietly
# become the plumbing half for three others while the reviewer is looking at a
# one-line diff.
FORBIDDEN_NAMES = ("GENERATION_MAX_CONCURRENT", "GENERATION_MAX_QUEUE", "DEBATE_POOL_MAX")

_CONTAINER_NAME_RE = re.compile(r'^      name\s*=\s*"([^"]+)"', re.MULTILINE)
_ENTRY_RE = re.compile(r'\{\s*name\s*=\s*"([^"]+)"\s*,\s*(valueFrom|value)\s*=\s*"([^"]*)"')


def _tf_source() -> str:
    assert ECS_TF.is_file(), f"missing {ECS_TF}"
    return ECS_TF.read_text(encoding="utf-8")


def _container_block(name: str) -> str:
    """Slice one container out of the `container_definitions` list.

    Same shape as test_ecs_backend_secrets.py: containers are delimited by
    their own 6-space-indented `name = "..."` line, so a block runs from this
    container's name to the next one (or to end-of-file for the last).
    """
    src = _tf_source()
    matches = list(_CONTAINER_NAME_RE.finditer(src))
    assert matches, "no container blocks found in infra/ecs.tf — did the file shape change?"
    names = [m.group(1) for m in matches]
    assert name in names, f"no {name!r} container in infra/ecs.tf; found {names}"
    idx = names.index(name)
    start = matches[idx].start()
    end = matches[idx + 1].start() if idx + 1 < len(matches) else len(src)
    return src[start:end]


def _environment_block(container: str) -> str:
    """The text of `environment = [ ... ]`, bracket-matched.

    Bracket-matched rather than regex-terminated because the backend
    container's list contains nested `[...]`; a naive `\\]` would cut it short
    and silently shrink the set under test — which for a presence assertion
    means a false FAILURE, and for the anti-goal assertion a false PASS.
    """
    match = re.search(r"^      environment\s*=\s*(?:concat\()?\[", container, re.MULTILINE)
    assert match, "no `environment` block found in the backend container"
    depth, i = 0, match.end() - 1
    while i < len(container):
        if container[i] == "[":
            depth += 1
        elif container[i] == "]":
            depth -= 1
            if depth == 0:
                return container[match.start() : i + 1]
        i += 1
    raise AssertionError("unbalanced brackets in the `environment` block")


def _entries(text: str) -> dict[str, str]:
    """{name: value} for every `{ name = ..., value = ... }` entry."""
    return {m.group(1): m.group(3) for m in _ENTRY_RE.finditer(text)}


def _preceding_comment(name: str) -> str:
    """The unbroken run of `#` lines directly above `{ name = "<name>", ... }`.

    Walked backwards line by line rather than sliced between two neighbouring
    entries: an anchor on the entry above would silently start matching the
    *wrong* prose the first time someone reorders the block, and a test that
    reads the wrong text is worse than no test.
    """
    block = _environment_block(_container_block("backend"))
    lines = block.splitlines()
    idx = next((i for i, ln in enumerate(lines) if f'name = "{name}"' in ln), None)
    assert idx is not None, f"no `{name}` entry in the backend `environment` block"
    out: list[str] = []
    i = idx - 1
    while i >= 0 and lines[i].strip().startswith("#"):
        out.append(lines[i])
        i -= 1
    assert out, f"the `{name}` entry has no explanatory comment above it"
    return "\n".join(reversed(out))


def _code_default_timeout() -> float:
    """`_DEFAULT_GENERATION_TIMEOUT_SECONDS`, parsed out of generate_routes.py.

    Read with `ast` instead of off the imported module so the comparison below
    is anchored to the source of truth a reviewer would edit, and so this file
    keeps working if the constant is ever made private-by-convention or moved
    behind a helper. Derived, not hard-coded: the point is "prod is tighter
    than the fallback", whichever number the fallback happens to be.
    """
    tree = ast.parse(GENERATE_ROUTES_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = node.targets if isinstance(node, ast.Assign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_DEFAULT_GENERATION_TIMEOUT_SECONDS":
                assert isinstance(node.value, ast.Constant), "default is no longer a literal constant"
                return float(node.value.value)
    raise AssertionError(f"_DEFAULT_GENERATION_TIMEOUT_SECONDS not found in {GENERATE_ROUTES_PY}")


@pytest.fixture(scope="module")
def backend_environment() -> dict[str, str]:
    return _entries(_environment_block(_container_block("backend")))


class TestTheCeilingIsPinnedInProd:
    def test_the_name_is_declared(self, backend_environment: dict[str, str]) -> None:
        """Fails against `main` before this change — the name was absent."""
        assert ENV_NAME in backend_environment, (
            f"{ENV_NAME} is missing from the backend container's `environment` block. "
            "The task starts healthy and every generation runs on "
            "generate_routes.py's 600s code default — a hard ceiling nobody chose."
        )

    def test_the_literal_survives_the_real_resolver(
        self,
        backend_environment: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The value ECS injects must not be swallowed by the fallback.

        This is the check that catches the shipped-looking-fixed defect:
        `"300s"`, `"5m"`, `"0"`, `"-1"` are all valid HCL, all deploy cleanly,
        and all resolve to the 600s default at runtime. Driven through the
        genuine `_generation_timeout_seconds()` — the parser whose fail-soft
        behaviour is the hazard — so this cannot drift from it.
        """
        literal = backend_environment[ENV_NAME]
        monkeypatch.setenv(ENV_NAME, literal)
        resolved = generate_routes._generation_timeout_seconds()
        default = _code_default_timeout()
        assert resolved != default, (
            f"{ENV_NAME}={literal!r} in ecs.tf resolves to {resolved} — the code "
            f"default. _generation_timeout_seconds() is fail-soft, so a non-numeric "
            "or non-positive literal deploys cleanly and changes nothing. Use a bare "
            "positive number of seconds."
        )

    def test_prod_is_tighter_than_the_code_default(
        self,
        backend_environment: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pinning the name only helps if it buys a tighter bound.

        A value >= the fallback would be plumbing with no effect at best, and
        at worst a ceiling looser than the hang it was set to bound — the exact
        finding this change answers.
        """
        monkeypatch.setenv(ENV_NAME, backend_environment[ENV_NAME])
        resolved = generate_routes._generation_timeout_seconds()
        default = _code_default_timeout()
        assert resolved < default, (
            f"{ENV_NAME} resolves to {resolved}s, which is not tighter than the "
            f"{default}s code default in generate_routes.py."
        )

    def test_the_comment_states_the_value_it_sets(self, backend_environment: dict[str, str]) -> None:
        """Prose that asserts a number is the same defect surface as code that does.

        The comment above the entry explains the ceiling in seconds. If the
        entry is retuned and the prose is not, the next reader trusts a stale
        number — so the prose has to name the value actually set.
        """
        comment = _preceding_comment(ENV_NAME)
        seconds = backend_environment[ENV_NAME]
        assert re.search(rf"\b{re.escape(seconds)}\b", comment), (
            f"the comment above {ENV_NAME} does not mention {seconds}, the value it "
            "sets. Restate the number when retuning, or the explanation goes stale "
            "silently."
        )


class TestAntiGoals:
    """This is the tuning half for one name, not the plumbing half for #1668."""

    @pytest.mark.parametrize("name", FORBIDDEN_NAMES)
    def test_admission_knob_is_not_smuggled_in(self, name: str, backend_environment: dict[str, str]) -> None:
        assert name not in backend_environment, (
            f"{name} was added to the backend container's `environment`. Plumbing the "
            "admission-control knobs is #1668, whose own anti-goal keeps plumbing and "
            "tuning in separate PRs with separate review."
        )
