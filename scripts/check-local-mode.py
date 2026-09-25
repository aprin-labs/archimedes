#!/usr/bin/env python3
"""Local-mode contract check — does a fresh `docker compose up` stay local? (issue #1044)

`docs/local-vs-prod.md` states one contract: local mode and production mode are the
same code, separated only by configuration. This script is what keeps that sentence
true. It reads the committed compose files and an env file and answers five questions
that each name a way "local" has quietly reached production:

  profiles          COMPOSE_PROFILES selects `localdb` and does NOT select `runners`.
  runners-off       No funds-adjacent runner (oracle / agent / kb-runner) would start.
  no-ecr-pull       No service that carries `build:` resolves to a remote registry when
                    ECR_REGISTRY / IMAGE_TAG are unset — i.e. a bare `up` without
                    `--build` cannot silently run the last-pushed production image.
  no-prod-secrets   PUBLIC_DOMAIN is blank, so `main.py`'s SSM gate never fires, and
                    AWS_SSM_PATH_PREFIX is blank as belt-and-suspenders.
  local-datastores  With `localdb` active, DATABASE_URL / REDIS_URL address the in-stack
                    containers, not Aurora / ElastiCache.

It runs WITHOUT a docker daemon on purpose — it parses YAML rather than shelling
`docker compose config`, so CI (and a laptop with Docker Desktop closed) can hold it.
The one non-stdlib import is PyYAML, which the backend image already ships.

Usage:
    python3 scripts/check-local-mode.py [--env-file .env.example] [--compose FILE ...]
                                        [--format text|github]

Exit status: 0 = every check passed, 1 = at least one failed, 2 = could not run.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by hand, not in CI
    print(
        "check-local-mode: PyYAML is not importable, so the compose files cannot be read.\n"
        "  Run it from the archimedes conda env, or `pip install pyyaml`.\n"
        "  Exiting 2 (could-not-run) rather than 0 — a check that cannot run has not passed.",
        file=sys.stderr,
    )
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Services whose job is to write to chain / spend money / hold an exactly-once lease.
#: They must be reachable only through the `runners` profile.
RUNNER_SERVICES = ("oracle", "agent", "kb-runner")

#: The profile that starts the in-stack Postgres + Redis.
LOCALDB_PROFILE = "localdb"
RUNNERS_PROFILE = "runners"

#: Hostnames the in-stack datastores answer to on the compose network.
LOCAL_DB_HOSTS = ("postgres", "localhost", "127.0.0.1")
LOCAL_REDIS_HOSTS = ("redis", "localhost", "127.0.0.1")


# ── env files ────────────────────────────────────────────────────────────────────────


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines into a dict. Comments, blanks, and `export ` prefixes ignored.

    Deliberately NOT a full dotenv parser: no interpolation, no multi-line values. The
    file this reads is a template of literal defaults, and pretending to more fidelity
    than that would make the checker's verdict depend on a parser nobody audits.
    """
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


# ── compose ${VAR} interpolation ─────────────────────────────────────────────────────

_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _find_closing_brace(text: str, start: int) -> int:
    """Index of the `}` matching the `{` at ``start``; -1 if unbalanced.

    Nesting matters: `${ECR_REGISTRY:+${ECR_REGISTRY}/}` is the exact shape #1044's
    image fix uses, and a non-nesting scanner stops at the inner brace and mangles it.
    """
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def interpolate(value: str, env: dict[str, str]) -> str:
    """Resolve compose-style `${VAR}` / `$VAR` substitution against ``env``.

    Supports the four operators compose defines, with `:`-prefixed forms treating an
    empty value as unset:

        ${VAR}            ${VAR:-default}   ${VAR-default}
        ${VAR:+alt}       ${VAR+alt}

    `${VAR:?err}` / `${VAR?err}` resolve to the value (or empty) — this checker reports,
    it does not enforce compose's own required-variable error.
    """
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        if i + 1 < len(value) and value[i + 1] == "$":  # `$$` is a literal `$`
            out.append("$")
            i += 2
            continue
        if i + 1 < len(value) and value[i + 1] == "{":
            end = _find_closing_brace(value, i + 1)
            if end == -1:  # unbalanced — emit verbatim, let compose complain
                out.append(value[i:])
                break
            out.append(_resolve_braced(value[i + 2 : end], env))
            i = end + 1
            continue
        m = _VAR_NAME.match(value, i + 1)
        if m:
            out.append(env.get(m.group(0), ""))
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _resolve_braced(body: str, env: dict[str, str]) -> str:
    m = _VAR_NAME.match(body)
    if not m:
        return ""
    name = m.group(0)
    rest = body[m.end() :]
    raw = env.get(name)
    is_set = raw is not None
    is_nonempty = bool(raw)

    if not rest:
        return raw or ""
    colon = rest.startswith(":")
    op_at = 1 if colon else 0
    if op_at >= len(rest):
        return raw or ""
    op = rest[op_at]
    operand = rest[op_at + 1 :]
    present = is_nonempty if colon else is_set

    if op == "-":
        return (raw or "") if present else interpolate(operand, env)
    if op == "+":
        return interpolate(operand, env) if present else ""
    if op == "?":
        return raw or ""
    return raw or ""


# ── image refs ───────────────────────────────────────────────────────────────────────


def registry_host(image_ref: str) -> str | None:
    """The registry hostname an image ref names, or None for a purely local tag.

    Docker's own rule: the first `/`-separated component is a registry only when it
    contains a `.` or a `:`, or is exactly `localhost`. `archimedes-backend:local` has
    no registry; `037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:latest`
    does, and that is the difference between "compose builds it" and "compose pulls it".
    """
    ref = image_ref.strip()
    if not ref or "/" not in ref:
        return None
    head = ref.split("/", 1)[0]
    if head == "localhost" or "." in head or ":" in head:
        return head
    return None


# ── compose loading ──────────────────────────────────────────────────────────────────


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_services(compose_paths: list[Path]) -> dict[str, dict]:
    """Merged `services:` mapping across the compose files, in COMPOSE_FILE order."""
    services: dict[str, dict] = {}
    for path in compose_paths:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, spec in (doc.get("services") or {}).items():
            spec = spec or {}
            services[name] = _deep_merge(services.get(name, {}), spec)
    return services


def active_profiles(env: dict[str, str]) -> set[str]:
    raw = env.get("COMPOSE_PROFILES", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def would_start(services: dict[str, dict], profiles: set[str]) -> list[str]:
    """Service names compose would bring up under ``profiles`` (compose's own rule:
    a service with no `profiles:` key is always started)."""
    started = []
    for name, spec in services.items():
        declared = set(spec.get("profiles") or [])
        if not declared or declared & profiles:
            started.append(name)
    return sorted(started)


# ── checks ───────────────────────────────────────────────────────────────────────────


class Result:
    def __init__(self, name: str, ok: bool, headline: str, details: list[str] | None = None):
        self.name = name
        self.ok = ok
        self.headline = headline
        self.details = details or []


def check_profiles(env: dict[str, str]) -> Result:
    profiles = active_profiles(env)
    problems = []
    if LOCALDB_PROFILE not in profiles:
        problems.append(
            f"COMPOSE_PROFILES={env.get('COMPOSE_PROFILES', '')!r} does not select "
            f"{LOCALDB_PROFILE!r} — the in-stack postgres/redis will not start, so the "
            "app tier will try to reach whatever DATABASE_URL/REDIS_URL point at."
        )
    if RUNNERS_PROFILE in profiles:
        problems.append(
            f"COMPOSE_PROFILES selects {RUNNERS_PROFILE!r}. That starts the funds-adjacent "
            "oracle/agent/kb-runner singletons, which take an exactly-once lease and can "
            "race a copy already running elsewhere. Opt in per-command instead: "
            "`COMPOSE_PROFILES=localdb,runners docker compose up -d --build`."
        )
    return Result(
        "profiles",
        not problems,
        f"COMPOSE_PROFILES={env.get('COMPOSE_PROFILES', '') or '(unset)'}",
        problems,
    )


def check_runners_off(services: dict[str, dict], env: dict[str, str]) -> Result:
    profiles = active_profiles(env)
    started = set(would_start(services, profiles))
    problems = []
    for name in RUNNER_SERVICES:
        if name not in services:
            continue
        declared = set(services[name].get("profiles") or [])
        if RUNNERS_PROFILE not in declared:
            problems.append(
                f"service {name!r} does not declare profiles: [{RUNNERS_PROFILE!r}] — it "
                "starts on a bare `docker compose up`, which is the leak #1043 closed."
            )
        if name in started:
            problems.append(f"service {name!r} would start under the active profiles {sorted(profiles)}.")
    return Result(
        "runners-off",
        not problems,
        f"{len(started)} services would start: {', '.join(sorted(started)) or '(none)'}",
        problems,
    )


def check_no_ecr_pull(services: dict[str, dict], env: dict[str, str]) -> Result:
    """A service compose can BUILD must not also name a remote registry by default.

    The env used here deliberately has ECR_REGISTRY and IMAGE_TAG stripped: that is the
    fresh-clone case, where nobody exported them. If the baked-in default still resolves
    to a registry, `docker compose up` without `--build` pulls it and runs code that is
    not the code in the checkout.
    """
    pull_env = {k: v for k, v in env.items() if k not in ("ECR_REGISTRY", "IMAGE_TAG")}
    problems = []
    checked = 0
    for name in sorted(services):
        spec = services[name]
        if "build" not in spec or "image" not in spec:
            continue
        checked += 1
        resolved = interpolate(str(spec["image"]), pull_env)
        host = registry_host(resolved)
        if host:
            problems.append(
                f"service {name!r} resolves to {resolved!r} (registry {host!r}) with "
                "ECR_REGISTRY/IMAGE_TAG unset, so a bare `up` pulls it instead of using "
                f"the local `build:`. Expected a registry-less local tag."
            )
    return Result(
        "no-ecr-pull",
        not problems,
        f"{checked} build-and-image services checked with ECR_REGISTRY/IMAGE_TAG unset",
        problems,
    )


def check_no_prod_secrets(env: dict[str, str]) -> Result:
    problems = []
    if env.get("PUBLIC_DOMAIN"):
        problems.append(
            f"PUBLIC_DOMAIN={env['PUBLIC_DOMAIN']!r} is set. That is the production signal "
            "backend/archimedes/main.py gates load_ssm_secrets() on — with ambient AWS "
            "credentials this run would fetch real production secrets from SSM."
        )
    if env.get("AWS_SSM_PATH_PREFIX"):
        problems.append(
            f"AWS_SSM_PATH_PREFIX={env['AWS_SSM_PATH_PREFIX']!r} is set. The PUBLIC_DOMAIN "
            "gate is what actually stops the fetch, but a non-blank prefix here means the "
            "belt-and-suspenders layer is gone: one stray PUBLIC_DOMAIN and it resolves."
        )
    return Result("no-prod-secrets", not problems, "PUBLIC_DOMAIN and AWS_SSM_PATH_PREFIX are blank", problems)


def _url_host(url: str) -> str:
    after_scheme = url.split("://", 1)[-1]
    authority = after_scheme.split("/", 1)[0]
    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]
    return authority.split(":", 1)[0]


def check_local_datastores(env: dict[str, str]) -> Result:
    problems = []
    if LOCALDB_PROFILE not in active_profiles(env):
        return Result("local-datastores", True, f"{LOCALDB_PROFILE} not selected — not a local-mode run", [])
    db = env.get("DATABASE_URL", "")
    redis = env.get("REDIS_URL", "")
    if db and _url_host(db) not in LOCAL_DB_HOSTS:
        problems.append(
            f"DATABASE_URL points at {_url_host(db)!r} while the {LOCALDB_PROFILE!r} profile "
            "starts an in-stack postgres. A local run would write to a remote database."
        )
    if redis and _url_host(redis) not in LOCAL_REDIS_HOSTS:
        problems.append(
            f"REDIS_URL points at {_url_host(redis)!r} while the {LOCALDB_PROFILE!r} profile starts an in-stack redis."
        )
    return Result(
        "local-datastores",
        not problems,
        f"DATABASE_URL host={_url_host(db) or '(unset)'}, REDIS_URL host={_url_host(redis) or '(unset)'}",
        problems,
    )


def run_checks(services: dict[str, dict], env: dict[str, str]) -> list[Result]:
    return [
        check_profiles(env),
        check_runners_off(services, env),
        check_no_ecr_pull(services, env),
        check_no_prod_secrets(env),
        check_local_datastores(env),
    ]


# ── cli ──────────────────────────────────────────────────────────────────────────────


def resolve_compose_paths(root: Path, env: dict[str, str], explicit: list[str]) -> list[Path]:
    if explicit:
        names = explicit
    else:
        separator = env.get("COMPOSE_PATH_SEPARATOR", ":")
        raw = env.get("COMPOSE_FILE", "docker-compose.yml")
        names = [n for n in raw.split(separator) if n.strip()]
    return [(root / n) if not Path(n).is_absolute() else Path(n) for n in names]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check that local mode stays local (#1044).")
    ap.add_argument("--root", default=str(REPO_ROOT), help="repository root (default: this script's repo)")
    ap.add_argument("--env-file", default=None, help="env file to read (default: .env if present, else .env.example)")
    ap.add_argument(
        "--compose", action="append", default=[], help="compose file (repeatable; default from COMPOSE_FILE)"
    )
    ap.add_argument("--format", choices=("text", "github"), default="text")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if args.env_file:
        env_path = Path(args.env_file)
        if not env_path.is_absolute():
            env_path = root / env_path
    else:
        env_path = root / ".env"
        if not env_path.exists():
            env_path = root / ".env.example"
    if not env_path.exists():
        print(f"check-local-mode: no env file at {env_path}", file=sys.stderr)
        return 2

    env = parse_env_file(env_path)
    compose_paths = resolve_compose_paths(root, env, args.compose)
    missing = [p for p in compose_paths if not p.exists()]
    if missing:
        print(f"check-local-mode: compose file(s) not found: {', '.join(str(p) for p in missing)}", file=sys.stderr)
        return 2

    services = load_services(compose_paths)
    results = run_checks(services, env)

    print(f"Archimedes local-mode check — {env_path.relative_to(root) if env_path.is_relative_to(root) else env_path}")
    print(f"  compose: {', '.join(p.name for p in compose_paths)}")
    print("")
    for r in results:
        print(f"  [{'PASS' if r.ok else 'FAIL'}] {r.name:<17} {r.headline}")
        for detail in r.details:
            print(f"         → {detail}")
            if args.format == "github":
                print(f"::error file={compose_paths[0].name}::{r.name}: {detail}")
    failed = [r for r in results if not r.ok]
    print("")
    if failed:
        print(
            f"{len(failed)} of {len(results)} checks FAILED — see docs/local-vs-prod.md for the contract each one defends."
        )
        return 1
    print(f"All {len(results)} checks passed — this configuration is local mode.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
