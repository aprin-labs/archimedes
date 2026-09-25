"""The admission-control knobs must reach the Fargate task, at the code's own defaults (#1668).

``GENERATION_MAX_CONCURRENT``, ``GENERATION_MAX_QUEUE`` and ``DEBATE_POOL_MAX``
are read off the process environment by code that has shipped for months
(``api/generate_routes.py``, ``agents/debate_engine.py``) and were absent from
``infra/ecs.tf`` — so production ran on the ``os.getenv()`` fallbacks by
accident rather than by decision. That is the config-drift class
``docs/adr/lambda-generation-offload.md`` recorded under § Consequences instead
of patching, and the one the ``GENERATION_DAILY_CAP_*`` comment in ``ecs.tf``
already warns about in prose.

Two ways to get this wrong, and this file guards both:

1. **Unplumbed.** The name never reaches the task definition. Silent: the task
   boots healthy, ``/health`` passes, and the knob is simply not a knob.
   ``terraform validate`` cannot see it — an ``environment`` block missing an
   entry is perfectly valid HCL.
2. **Plumbed at a different value.** Worse than unplumbed, because the file now
   *looks* authoritative. #1668 is explicitly plumbing-only: the Terraform
   defaults must be byte-identical to the code defaults, so applying it changes
   nothing. Retuning is a separate change with separate review.

Both sides are read here — the code default via ``ast`` (no import: hermetic,
no env, no ``archimedes`` package, no DB) and the Terraform default via text.
Neither is hand-copied into this file, so the pairing stays checked as either
side moves. Only the *pairing* is pinned, never a particular number: a
deliberate retune that changes both sides together still passes, which is
exactly the review boundary #1668 draws.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ECS_TF = REPO_ROOT / "infra" / "ecs.tf"
VARIABLES_TF = REPO_ROOT / "infra" / "variables.tf"

# env var name → (source file holding its os.getenv default, terraform variable name)
KNOBS: dict[str, tuple[Path, str]] = {
    "GENERATION_MAX_CONCURRENT": (
        REPO_ROOT / "backend" / "archimedes" / "api" / "generate_routes.py",
        "generation_max_concurrent",
    ),
    "GENERATION_MAX_QUEUE": (
        REPO_ROOT / "backend" / "archimedes" / "api" / "generate_routes.py",
        "generation_max_queue",
    ),
    "DEBATE_POOL_MAX": (
        REPO_ROOT / "backend" / "archimedes" / "agents" / "debate_engine.py",
        "debate_pool_max",
    ),
}

# ECS `environment` entries are string/string pairs. A terraform variable typed
# `number` would make jsonencode emit a bare JSON number and
# RegisterTaskDefinition rejects the whole task definition — at apply time,
# which is the prod deploy path. Same reason ecs_backend_cpu is a string.
REQUIRED_TF_TYPE = "string"

_CONTAINER_NAME_RE = re.compile(r'^      name\s*=\s*"([^"]+)"', re.MULTILINE)

# `{ name = "X", value = "literal" }` or `{ name = "X", value = var.something }`.
# The quoted alternative tolerates interpolation (`"https://${var.domain_name}"`).
_ENTRY_RE = re.compile(
    r'\{\s*name\s*=\s*"([^"]+)"\s*,\s*value\s*=\s*'
    r'(?:"((?:[^"\\]|\\.)*)"|([A-Za-z_][\w.\[\]]*))\s*,?\s*\}'
)


def _backend_environment() -> dict[str, tuple[str | None, str | None]]:
    """{env name: (string literal | None, unquoted reference | None)} for the backend container.

    Sliced the same way ``test_ecs_backend_secrets.py`` slices it: containers are
    delimited by their own 6-space-indented ``name = "..."`` line, and the
    ``environment`` list is bracket-matched rather than regex-terminated (it
    contains nested ``[...]``, and a naive ``\\]`` would silently shrink the set
    under test).
    """
    assert ECS_TF.is_file(), f"missing {ECS_TF}"
    src = ECS_TF.read_text(encoding="utf-8")

    matches = list(_CONTAINER_NAME_RE.finditer(src))
    names = [m.group(1) for m in matches]
    assert "backend" in names, f"no `backend` container in infra/ecs.tf; found {names}"
    idx = names.index("backend")
    end = matches[idx + 1].start() if idx + 1 < len(matches) else len(src)
    container = src[matches[idx].start() : end]

    opener = re.search(r"^      environment\s*=\s*(?:concat\()?\[", container, re.MULTILINE)
    assert opener, "no `environment` block in the backend container"
    depth, i = 0, opener.end() - 1
    block = None
    while i < len(container):
        if container[i] == "[":
            depth += 1
        elif container[i] == "]":
            depth -= 1
            if depth == 0:
                block = container[opener.start() : i + 1]
                break
        i += 1
    assert block is not None, "unbalanced brackets in the `environment` block"

    return {m.group(1): (m.group(2), m.group(3)) for m in _ENTRY_RE.finditer(block)}


def _tf_variable_block(name: str) -> str:
    """The body of ``variable "<name>" { ... }`` in infra/variables.tf, brace-matched."""
    assert VARIABLES_TF.is_file(), f"missing {VARIABLES_TF}"
    src = VARIABLES_TF.read_text(encoding="utf-8")
    opener = re.search(rf'^variable\s+"{re.escape(name)}"\s*\{{', src, re.MULTILINE)
    assert opener, f'infra/ecs.tf references var.{name} but infra/variables.tf declares no variable "{name}"'
    depth, i = 0, opener.end() - 1
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[opener.start() : i + 1]
        i += 1
    raise AssertionError(f'unbalanced braces in variable "{name}"')


def _code_getenv_defaults(path: Path) -> dict[str, str]:
    """{env name: default} for every ``os.getenv("NAME", "DEFAULT")`` in a module.

    Parsed with ``ast`` over the whole module rather than by line number: the
    line the ADR cites moves whenever anything above it is edited, and a guard
    that breaks on an unrelated refactor gets deleted rather than fixed.
    """
    assert path.is_file(), f"missing {path}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "getenv" or len(node.args) != 2:
            continue
        key, default = node.args
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        if not (isinstance(default, ast.Constant) and isinstance(default.value, str)):
            continue
        previous = found.get(key.value)
        assert previous in (None, default.value), (
            f"{path.name} reads {key.value} with two different defaults "
            f"({previous!r} and {default.value!r}) — this guard cannot tell which "
            "one Terraform is supposed to mirror. Collapse them to one."
        )
        found[key.value] = default.value
    return found


@pytest.fixture(scope="module")
def backend_environment() -> dict[str, tuple[str | None, str | None]]:
    return _backend_environment()


@pytest.mark.parametrize("env_name", sorted(KNOBS))
class TestTheKnobReachesTheTask:
    def test_it_is_declared_on_the_backend_container(
        self, env_name: str, backend_environment: dict[str, tuple[str | None, str | None]]
    ) -> None:
        """Fails against `main` before #1668 — all three were absent (verified by grep)."""
        assert env_name in backend_environment, (
            f"{env_name} is missing from the backend container's `environment` block in "
            "infra/ecs.tf. The task boots healthy and the knob silently has no effect: "
            "production runs on the code's os.getenv() fallback, which is a decision "
            "nobody made."
        )

    def test_it_is_wired_from_a_variable_not_hardcoded(
        self, env_name: str, backend_environment: dict[str, tuple[str | None, str | None]]
    ) -> None:
        """A literal here would make every retune a code change plus a full apply."""
        # `.get`, not `[...]`: an absent entry is already reported by the test
        # above, and a bare KeyError here would bury that under a worse message.
        literal, reference = backend_environment.get(env_name, (None, None))
        _source, tf_var = KNOBS[env_name]
        found = "nothing (the entry is absent)"
        if literal is not None:
            found = f"the literal {literal!r}"
        elif reference is not None:
            found = f"`{reference}`"
        assert reference == f"var.{tf_var}", (
            f"{env_name} should be wired from `var.{tf_var}` so it is tunable at apply "
            f"time without a code change; infra/ecs.tf has {found}."
        )

    def test_the_variable_is_typed_string(self, env_name: str) -> None:
        """`type = number` renders a bare JSON number and RegisterTaskDefinition 400s."""
        _source, tf_var = KNOBS[env_name]
        block = _tf_variable_block(tf_var)
        match = re.search(r"^\s*type\s*=\s*(\S+)", block, re.MULTILINE)
        assert match, f'variable "{tf_var}" declares no type'
        assert match.group(1) == REQUIRED_TF_TYPE, (
            f'variable "{tf_var}" is `type = {match.group(1)}`. ECS `environment` values '
            f"must be strings — jsonencode would emit a bare JSON number and the whole "
            "task definition is rejected at apply time, i.e. mid-deploy."
        )

    def test_terraform_default_matches_the_code_default(self, env_name: str) -> None:
        """The byte-identical check. #1668 is plumbing; applying it must change nothing.

        Both sides are derived, not hand-copied: change either one alone and this
        fails naming the other. A deliberate retune that moves both together
        still passes — that is the intended boundary, not a hole.
        """
        source, tf_var = KNOBS[env_name]
        code_defaults = _code_getenv_defaults(source)
        assert env_name in code_defaults, (
            f"{source.relative_to(REPO_ROOT)} no longer reads {env_name} via "
            "os.getenv() with a literal default — if the knob was renamed or "
            "removed, update infra/ecs.tf and this file together."
        )
        code_default = code_defaults[env_name]

        block = _tf_variable_block(tf_var)
        match = re.search(r'^\s*default\s*=\s*"([^"]*)"', block, re.MULTILINE)
        assert match, (
            f'variable "{tf_var}" has no string `default`. An unset default makes '
            "`terraform apply` prompt (or fail in CI) rather than reproducing today's "
            "behaviour."
        )
        tf_default = match.group(1)

        assert tf_default == code_default, (
            f"{env_name} drift: infra/variables.tf defaults var.{tf_var} to "
            f"{tf_default!r} but {source.relative_to(REPO_ROOT)} falls back to "
            f"{code_default!r}. #1668 plumbs the knob at today's value; changing the "
            "value is a separate change with separate review. If the retune is "
            "deliberate, move BOTH sides in the same PR."
        )
