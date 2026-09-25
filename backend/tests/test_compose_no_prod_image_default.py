"""A local `docker compose up` must never resolve to a production image (#1044).

`docker-compose.yml` is the file a fresh clone runs. Every app-tier service in
it carries a `build:` section, so local code is *supposed* to be authoritative.
The leak this module guards is that those same services also carried an
``image:`` key whose **baked-in default** was the live production ECR ref::

    image: ${ECR_REGISTRY:-037613907429.dkr.ecr.us-east-1.amazonaws.com}/archimedes-backend:${IMAGE_TAG:-latest}

Neither ``ECR_REGISTRY`` nor ``IMAGE_TAG`` was defined anywhere in
``.env.example``, so on a fresh clone the defaults always won and two distinct
things went wrong, both silent:

1. **Reaching production.** ``docker compose up`` *without* ``--build`` — and
   ``docker compose pull`` — resolve that ref and make a real request to the
   production registry. On a machine that is ECR-authenticated (our own
   onboarding tells people to set that up) it succeeds, and the developer runs
   the last image CI pushed to prod while believing they are running their
   working tree.
2. **Cross-checkout tag theft.** ``build:`` tags whatever ``image:`` names, and
   that name is machine-global. Every worktree on the box therefore builds into
   the *same* tag, so the last checkout to run ``--build`` silently owns it, and
   a bare ``up`` in any other checkout adopts that foreign build.

The fix keeps ``build:`` authoritative and makes the pull path opt-in:
``docker-compose.yml`` resolves to a project-scoped, registry-less tag that
cannot exist on any remote, and ``docker-compose.ecr.yml`` is a separate
override that restores the registry-qualified refs and *requires* the two vars
to be set explicitly.

These tests read the YAML directly and do their own POSIX-subset interpolation,
so they need no docker daemon and run in the default unit sweep.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
ECR_COMPOSE = REPO_ROOT / "docker-compose.ecr.yml"
PRODUCTION_COMPOSE = REPO_ROOT / "docker-compose.production.yml"

# Any registry host, not just ours — a default pointing at *someone's* remote
# registry is the defect, and hardcoding the account number would let a
# re-tagged copy sail through.
ECR_HOST_RE = re.compile(r"\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/")

# The env a fresh clone has: `cp .env.example .env` and nothing else. Neither
# var is defined in that template, which is the whole point.
SCRUBBED_ENV: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Compose variable interpolation (the POSIX subset compose implements)
# ---------------------------------------------------------------------------


class MissingRequiredVar(Exception):
    """Raised for ``${VAR:?msg}`` when VAR is unset/empty — compose's own
    fail-loud form. The tests assert on this to prove the ECR override cannot
    silently fall back to a default."""


_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _apply(name: str, op: str, word: str, env: dict[str, str]) -> str:
    present = name in env
    value = env.get(name, "")
    non_empty = present and value != ""

    if op == ":-":
        return value if non_empty else word
    if op == "-":
        return value if present else word
    if op == ":+":
        return word if non_empty else ""
    if op == "+":
        return word if present else ""
    if op == ":?":
        if not non_empty:
            raise MissingRequiredVar(f"{name}: {word}")
        return value
    if op == "?":
        if not present:
            raise MissingRequiredVar(f"{name}: {word}")
        return value
    return value


def interpolate(template: str, env: dict[str, str]) -> str:
    """Resolve ``$VAR`` / ``${VAR}`` / ``${VAR:-d}`` / ``${VAR:+a}`` /
    ``${VAR:?e}`` (and the no-colon variants) the way compose does.

    Handles nesting inside the default word, and ``$$`` as a literal ``$``.
    """
    out: list[str] = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        if i + 1 < n and template[i + 1] == "$":
            out.append("$")
            i += 2
            continue
        if i + 1 < n and template[i + 1] == "{":
            depth = 1
            j = i + 2
            while j < n and depth:
                if template[j] == "{":
                    depth += 1
                elif template[j] == "}":
                    depth -= 1
                j += 1
            if depth:
                raise ValueError(f"unbalanced ${{ in {template!r}")
            body = template[i + 2 : j - 1]
            match = _NAME_RE.match(body)
            if not match:
                raise ValueError(f"unparseable substitution {body!r}")
            name = match.group(0)
            rest = body[match.end() :]
            op, word = "", ""
            for candidate in (":-", ":+", ":?", ":=", "-", "+", "?", "="):
                if rest.startswith(candidate):
                    op, word = candidate, rest[len(candidate) :]
                    break
            out.append(_apply(name, op, interpolate(word, env), env))
            i = j
            continue
        match = _NAME_RE.match(template, i + 1)
        if match:
            out.append(env.get(match.group(0), ""))
            i = match.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _services(path: Path) -> dict[str, dict]:
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    doc = yaml.safe_load(path.read_text()) or {}
    return doc.get("services") or {}


def _raw_images(path: Path) -> dict[str, str]:
    return {
        name: svc["image"]
        for name, svc in _services(path).items()
        if isinstance(svc, dict) and isinstance(svc.get("image"), str)
    }


def _built_services(path: Path) -> set[str]:
    """Services that build from this repo's source — the ones whose image ref
    is a *local build tag*, not a third-party pin like ``postgres:18-alpine``."""
    return {name for name, svc in _services(path).items() if isinstance(svc, dict) and svc.get("build")}


def _has_registry_host(ref: str) -> bool:
    """True when the first path segment is a registry host.

    Docker's own rule: a name is registry-qualified only if the first
    ``/``-separated component contains a ``.`` or ``:`` (or is ``localhost``).
    ``postgres:18-alpine`` → False. ``example.com/x:1`` → True.
    """
    head = ref.split("/")[0]
    return "/" in ref and ("." in head or ":" in head or head == "localhost")


# ---------------------------------------------------------------------------
# The guard: the fresh-clone default must not be a production image
# ---------------------------------------------------------------------------


def test_base_compose_never_defaults_to_a_remote_registry_image():
    """With ECR_REGISTRY / IMAGE_TAG unset, nothing in the file a fresh clone
    runs may resolve to an ECR ref."""
    offenders = {name: interpolate(raw, SCRUBBED_ENV) for name, raw in _raw_images(BASE_COMPOSE).items()}
    leaking = {name: ref for name, ref in offenders.items() if ECR_HOST_RE.search(ref)}
    assert not leaking, (
        "docker-compose.yml resolves to production ECR images on a fresh clone "
        "(ECR_REGISTRY and IMAGE_TAG unset). A bare `docker compose up` — no "
        "--build — pulls and runs the last image CI pushed to prod instead of "
        "the developer's working tree (#1044 leak 3). Offending services:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in sorted(leaking.items()))
        + "\nThe ECR pull path belongs in docker-compose.ecr.yml, opt-in."
    )


def test_base_compose_images_are_unresolvable_on_any_registry():
    """Stronger than the ECR check: the local default must not name *any*
    remote host, so a mis-typed or third-party registry cannot serve it
    either."""
    qualified = {
        name: ref
        for name, ref in ((n, interpolate(raw, SCRUBBED_ENV)) for n, raw in _raw_images(BASE_COMPOSE).items())
        if _has_registry_host(ref)
    }
    assert not qualified, (
        "docker-compose.yml resolves registry-qualified images by default; "
        "local runs must resolve to build-only tags no registry can serve:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in sorted(qualified.items()))
    )


def test_locally_built_images_are_scoped_per_compose_project():
    """`build:` tags whatever `image:` names, and image tags are machine-global.

    An unscoped tag means every checkout on the machine builds into the same
    name, so the last worktree to run `--build` owns it and a bare `up` in any
    other checkout silently adopts that foreign build. Scoping the tag by
    ``COMPOSE_PROJECT_NAME`` (which compose derives from the directory) gives
    each worktree its own tag.
    """
    unscoped = {
        name: raw
        for name, raw in _raw_images(BASE_COMPOSE).items()
        if name in _built_services(BASE_COMPOSE) and "COMPOSE_PROJECT_NAME" not in raw
    }
    assert not unscoped, (
        "these built services use a machine-global image tag, so a build in "
        "one worktree overwrites the tag every other checkout resolves "
        "(#1044). Scope it with ${COMPOSE_PROJECT_NAME:-archimedes} or drop "
        "the `image:` key so compose names it per project:\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in sorted(unscoped.items()))
    )


def test_production_compose_never_defaults_to_a_remote_registry_image():
    """`docker-compose.production.yml` documents a `--build` workflow, so it
    carries the same fresh-clone hazard and gets the same treatment."""
    if not PRODUCTION_COMPOSE.is_file():
        pytest.skip("docker-compose.production.yml has been removed")
    leaking = {
        name: ref
        for name, ref in ((n, interpolate(raw, SCRUBBED_ENV)) for n, raw in _raw_images(PRODUCTION_COMPOSE).items())
        if ECR_HOST_RE.search(ref)
    }
    assert not leaking, (
        "docker-compose.production.yml bakes the prod registry in as a default; "
        "the registry prefix must appear only when ECR_REGISTRY is explicitly "
        "exported:\n  " + "\n  ".join(f"{k}: {v}" for k, v in sorted(leaking.items()))
    )


# ---------------------------------------------------------------------------
# The inverse: the pull path must still exist, behind explicit intent
# ---------------------------------------------------------------------------


def test_ecr_override_restores_registry_qualified_refs():
    """Opting in must actually give back a pullable ref for every service the
    base file builds — otherwise this change just breaks `docker compose pull`
    instead of gating it."""
    env = {
        "ECR_REGISTRY": "037613907429.dkr.ecr.us-east-1.amazonaws.com",
        "IMAGE_TAG": "deadbeef",
    }
    override = _raw_images(ECR_COMPOSE)
    missing = sorted(_built_services(BASE_COMPOSE) - set(override))
    assert not missing, (
        "docker-compose.ecr.yml does not cover every buildable service in "
        f"docker-compose.yml; `docker compose pull` would silently no-op for {missing}"
    )
    for name, raw in override.items():
        resolved = interpolate(raw, env)
        assert ECR_HOST_RE.search(resolved), (
            f"{name}: opting into the ECR override still did not produce a registry-qualified ref (got {resolved!r})"
        )
        assert resolved.endswith(":deadbeef"), f"{name}: IMAGE_TAG must select the tag (got {resolved!r})"


def test_ecr_override_fails_loudly_when_any_single_var_is_unset():
    """No defaults in the override: an operator who forgets **either** var gets
    an error, never a surprise `:latest` from production.

    Checking only "unset everything → raises" is too weak, and the adversarial
    pass caught it: swapping `${ECR_REGISTRY:?…}` back to
    `${ECR_REGISTRY:-037613907429.dkr.ecr.us-east-1.amazonaws.com}` still
    raised — because the *other* variable, IMAGE_TAG, was still required. The
    guard would have gone green on a file that had silently re-armed the
    production registry default. So each referenced variable is withheld on its
    own, with every sibling supplied.
    """
    for service, raw in _raw_images(ECR_COMPOSE).items():
        referenced = sorted(set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", raw)))
        assert referenced, f"{service}: image ref interpolates nothing"
        for withheld in referenced:
            env = {name: "placeholder" for name in referenced if name != withheld}
            with pytest.raises(MissingRequiredVar):
                resolved = interpolate(raw, env)
                pytest.fail(
                    f"{service}: {withheld} is unset yet the image ref still "
                    f"resolved, to {resolved!r}. Every variable in the ECR "
                    f"overlay must use the `:?` (required) form — a `:-` "
                    f"default here is how a forgotten variable turns into a "
                    f"silent production pull."
                )


def test_env_example_documents_the_opt_in_but_does_not_arm_it():
    """`.env.example` must explain ECR_REGISTRY / IMAGE_TAG, and must leave
    both commented out — a template that ships them set would re-arm exactly
    the leak this guards."""
    lines = (REPO_ROOT / ".env.example").read_text().splitlines()
    for var in ("ECR_REGISTRY", "IMAGE_TAG"):
        mentioned = [ln for ln in lines if var in ln]
        assert mentioned, f".env.example never mentions {var}"
        armed = [ln for ln in mentioned if re.match(rf"\s*{var}\s*=", ln)]
        assert not armed, (
            f".env.example sets {var} for real; it must stay commented out so "
            f"a `cp .env.example .env` cannot turn on the registry pull path:\n  " + "\n  ".join(armed)
        )


# ---------------------------------------------------------------------------
# Interpolator self-checks — a guard whose engine is wrong proves nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "env", "expected"),
    [
        ("${A:-d}", {}, "d"),
        ("${A:-d}", {"A": ""}, "d"),
        ("${A:-d}", {"A": "v"}, "v"),
        ("${A-d}", {"A": ""}, ""),
        ("${A:+p}", {}, ""),
        ("${A:+p}", {"A": "v"}, "p"),
        ("${A:+${A}/}x", {"A": "reg"}, "reg/x"),
        ("${A:+${A}/}x", {}, "x"),
        ("$A-b", {"A": "v"}, "v-b"),
        ("$$A", {"A": "v"}, "$A"),
    ],
)
def test_interpolate_matches_compose_semantics(template, env, expected):
    assert interpolate(template, env) == expected


def test_interpolate_raises_on_required_var():
    with pytest.raises(MissingRequiredVar):
        interpolate("${A:?set it}", {})
    assert interpolate("${A:?set it}", {"A": "v"}) == "v"
