"""The local-vs-production contract, and the checker that defends it (#1044).

``docs/local-vs-prod.md`` claims local mode and production mode are the same code
separated only by configuration. Three things make that claim decay, and each has tests
here:

1. **A selector silently loses its reader.** The doc names one variable per dimension. If
   the code stops reading it, the doc is prose about a switch that no longer exists —
   which is how #1044's own table arrived four rows wrong.
2. **A guard stops guarding.** ``scripts/check-local-mode.py`` is only worth running if it
   still rejects the configurations it was written to reject, so every check is fed an
   input that **should** fail and asserted to reject it, then fed the corrected input and
   asserted to pass. A check exercised in one direction proves nothing: an
   ``assert True``-shaped check passes the happy case forever.
3. **The doc drops out of the tree.** Front matter, the ``docs/doc-index.md`` row, and the
   mkdocs nav entry are what make it findable; all three are asserted.

Hermetic: reads committed YAML, markdown, and Python off disk, plus in-memory fixtures.
No DB, Redis, RPC, network, docker daemon, or ``.env``. ``yaml`` is available in the CI
unit-test image because ``backend/requirements.txt`` pins ``uvicorn[standard]``, whose
``standard`` extra requires ``pyyaml>=5.1`` — the same reasoning ``test_docs_site.py``
records.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "local-vs-prod.md"
DOCS_INDEX = REPO_ROOT / "docs" / "doc-index.md"
MKDOCS_YML = REPO_ROOT / "mkdocs.yml"
MAIN_PY = REPO_ROOT / "backend" / "archimedes" / "main.py"
CHECKER = REPO_ROOT / "scripts" / "check-local-mode.py"


def _load_checker():
    """Import the checker by path — `check-local-mode.py` is not an importable module name."""
    spec = importlib.util.spec_from_file_location("archimedes_check_local_mode", CHECKER)
    assert spec and spec.loader, f"cannot load {CHECKER}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


clm = _load_checker()


# ── fixtures: the smallest compose/env pair that exercises every check ────────────────

#: The image shape #1044 item 3 proposes: the registry prefix appears ONLY when
#: ECR_REGISTRY is explicitly exported, so a fresh clone resolves to a local tag.
GOOD_IMAGE = "${ECR_REGISTRY:+${ECR_REGISTRY}/}archimedes-backend:${IMAGE_TAG:-local}"

#: The shape on `main` today: the production registry is the baked-in default.
BAD_IMAGE = "${ECR_REGISTRY:-037613907429.dkr.ecr.us-east-1.amazonaws.com}/archimedes-backend:${IMAGE_TAG:-latest}"


def _compose(*, image: str = GOOD_IMAGE, gate_oracle: bool = True) -> dict[str, dict]:
    oracle: dict = {"image": image, "build": {"context": "."}}
    if gate_oracle:
        oracle["profiles"] = ["runners"]
    return {
        "postgres": {"image": "postgres:18-alpine", "profiles": ["localdb"]},
        "redis": {"image": "redis:7-alpine", "profiles": ["localdb"]},
        "backend": {"image": image, "build": {"context": "."}},
        "oracle": oracle,
        "agent": {"image": image, "build": {"context": "."}, "profiles": ["runners"]},
        "kb-runner": {"image": image, "build": {"context": "."}, "profiles": ["runners"]},
    }


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "COMPOSE_PROFILES": "localdb",
        "DATABASE_URL": "postgresql://archimedes:pw@postgres:5432/archimedes",
        "REDIS_URL": "redis://redis:6379/0",
        "PUBLIC_DOMAIN": "",
        "AWS_SSM_PATH_PREFIX": "",
    }
    env.update(overrides)
    return env


def _verdict(results: list, name: str):
    for r in results:
        if r.name == name:
            return r
    raise AssertionError(f"no check named {name!r} — the checker's check list changed")


# ── each guard, shown rejecting and then accepting ────────────────────────────────────


def test_profiles_check_rejects_runners_in_the_default_profile_set() -> None:
    """The bad input: someone puts `runners` in COMPOSE_PROFILES and forgets."""
    bad = _verdict(clm.run_checks(_compose(), _env(COMPOSE_PROFILES="localdb,runners")), "profiles")
    assert not bad.ok, "COMPOSE_PROFILES=localdb,runners must NOT read as local mode"
    assert any("runners" in d for d in bad.details)

    good = _verdict(clm.run_checks(_compose(), _env()), "profiles")
    assert good.ok, good.details


def test_profiles_check_rejects_a_missing_localdb_profile() -> None:
    bad = _verdict(clm.run_checks(_compose(), _env(COMPOSE_PROFILES="")), "profiles")
    assert not bad.ok, "an empty COMPOSE_PROFILES starts no postgres/redis — that is not local mode"


def test_runners_check_rejects_an_ungated_runner_service() -> None:
    """The bad input: `profiles: ["runners"]` is dropped from oracle (the #1043 gate)."""
    bad = _verdict(clm.run_checks(_compose(gate_oracle=False), _env()), "runners-off")
    assert not bad.ok, "an oracle with no profiles gate starts on a bare `docker compose up`"
    assert any("oracle" in d for d in bad.details)

    good = _verdict(clm.run_checks(_compose(gate_oracle=True), _env()), "runners-off")
    assert good.ok, good.details


def test_runners_check_rejects_runners_being_selected() -> None:
    bad = _verdict(clm.run_checks(_compose(), _env(COMPOSE_PROFILES="localdb,runners")), "runners-off")
    assert not bad.ok
    assert {"oracle", "agent", "kb-runner"} <= {w for d in bad.details for w in d.replace("'", " ").split()}


def test_ecr_check_rejects_a_registry_default_and_accepts_the_local_tag() -> None:
    """The bad input is `main` as it stands: a production ECR tag as the compose default."""
    bad = _verdict(clm.run_checks(_compose(image=BAD_IMAGE), _env()), "no-ecr-pull")
    assert not bad.ok, "a baked-in ECR default means a bare `up` pulls production images"
    assert any("dkr.ecr" in d for d in bad.details)

    good = _verdict(clm.run_checks(_compose(image=GOOD_IMAGE), _env()), "no-ecr-pull")
    assert good.ok, good.details


def test_ecr_check_still_lets_an_explicit_registry_through() -> None:
    """The over-correction guard: exporting ECR_REGISTRY must still produce a pullable ref.

    The fix must narrow the *default*, not remove the pull path — otherwise anyone who
    wants `docker compose pull` has to edit the compose file to get it back.
    """
    env = _env(ECR_REGISTRY="037613907429.dkr.ecr.us-east-1.amazonaws.com", IMAGE_TAG="abc1234")
    resolved = clm.interpolate(GOOD_IMAGE, env)
    assert resolved == "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:abc1234"
    assert clm.registry_host(resolved) == "037613907429.dkr.ecr.us-east-1.amazonaws.com"


def test_secrets_check_rejects_a_production_signal_in_a_local_env() -> None:
    """The bad input: the exact leak #1044 item 2 describes."""
    bad = _verdict(
        clm.run_checks(_compose(), _env(PUBLIC_DOMAIN="https://archimedes-arc.com")),
        "no-prod-secrets",
    )
    assert not bad.ok, "PUBLIC_DOMAIN set is the production signal main.py gates SSM on"

    bad_prefix = _verdict(
        clm.run_checks(_compose(), _env(AWS_SSM_PATH_PREFIX="/archimedes/prod/")),
        "no-prod-secrets",
    )
    assert not bad_prefix.ok, "a prod-shaped SSM prefix removes the belt-and-suspenders layer"

    good = _verdict(clm.run_checks(_compose(), _env()), "no-prod-secrets")
    assert good.ok, good.details


def test_datastore_check_rejects_managed_endpoints_under_the_localdb_profile() -> None:
    """The bad input: `localdb` is on, but the URLs address Aurora / ElastiCache."""
    bad = _verdict(
        clm.run_checks(
            _compose(),
            _env(
                DATABASE_URL="postgresql://u:p@archimedes-aurora.cluster-x.us-east-1.rds.amazonaws.com:5432/a",
                REDIS_URL="rediss://archimedes-cache.x.use1.cache.amazonaws.com:6379/0",
            ),
        ),
        "local-datastores",
    )
    assert not bad.ok, "a local stack pointed at managed stores is the leak, not a config choice"
    assert len(bad.details) == 2, bad.details

    good = _verdict(clm.run_checks(_compose(), _env()), "local-datastores")
    assert good.ok, good.details


# ── the interpolation the ECR check depends on ────────────────────────────────────────


@pytest.mark.parametrize(
    ("template", "env", "expected"),
    [
        ("${A:-fallback}", {}, "fallback"),
        ("${A:-fallback}", {"A": ""}, "fallback"),
        ("${A:-fallback}", {"A": "set"}, "set"),
        ("${A-fallback}", {"A": ""}, ""),  # no colon: empty counts as set
        ("${A:+${A}/}x", {}, "x"),  # nested, unset -> nothing
        ("${A:+${A}/}x", {"A": "reg"}, "reg/x"),  # nested, set -> prefix appears
        ("$A-tail", {"A": "v"}, "v-tail"),
        ("$$A", {"A": "v"}, "$A"),
    ],
)
def test_interpolation_matches_compose_semantics(template: str, env: dict, expected: str) -> None:
    """`${VAR:+${VAR}/}` is the whole point — a scanner that does not nest mangles it."""
    assert clm.interpolate(template, env) == expected


@pytest.mark.parametrize(
    ("ref", "host"),
    [
        ("archimedes-backend:local", None),
        ("postgres:18-alpine", None),
        ("archimedes/backend:local", None),  # a namespace is not a registry
        ("037613907429.dkr.ecr.us-east-1.amazonaws.com/x:latest", "037613907429.dkr.ecr.us-east-1.amazonaws.com"),
        ("localhost:5000/x:latest", "localhost:5000"),
    ],
)
def test_registry_detection_follows_dockers_own_rule(ref: str, host: str | None) -> None:
    assert clm.registry_host(ref) == host


# ── the committed configuration ───────────────────────────────────────────────────────


def _real_config():
    env = clm.parse_env_file(REPO_ROOT / ".env.example")
    paths = clm.resolve_compose_paths(REPO_ROOT, env, [])
    return clm.load_services(paths), env


@pytest.mark.parametrize("check", ["profiles", "runners-off", "no-prod-secrets", "local-datastores"])
def test_committed_env_example_is_local_mode(check: str) -> None:
    """A fresh clone must be local mode on these four dimensions.

    `no-ecr-pull` is deliberately absent: it fails on `main` today and that failure is
    #1044 item 3, tracked in docs/local-vs-prod.md § 3. Asserting it green here would
    force this PR to also carry the compose change; asserting it red would break the
    suite the moment the compose change lands. The next test pins the *shape* of that
    open item instead, so it cannot be forgotten and cannot go stale silently.
    """
    services, env = _real_config()
    result = _verdict(clm.run_checks(services, env), check)
    assert result.ok, f"{check} failed on the committed config:\n  " + "\n  ".join(result.details)


def test_every_runner_service_is_behind_the_runners_profile() -> None:
    services, _ = _real_config()
    for name in clm.RUNNER_SERVICES:
        assert name in services, f"{name!r} vanished from docker-compose.yml — update RUNNER_SERVICES"
        assert clm.RUNNERS_PROFILE in (services[name].get("profiles") or []), (
            f"{name!r} lost its profiles: ['runners'] gate — it now starts on a bare "
            "`docker compose up`, which is the leak #1043 closed."
        )


def test_the_open_ecr_leak_is_documented_while_it_is_open() -> None:
    """Whichever state the compose file is in, the doc must agree with it.

    Red while the leak is open, red again if someone closes the leak and leaves the doc
    saying it is open. Either way the doc and the tree cannot disagree in silence.
    """
    services, env = _real_config()
    leaking = not _verdict(clm.run_checks(services, env), "no-ecr-pull").ok
    doc = DOC.read_text(encoding="utf-8")
    documented_open = "**Currently failing; that is leak 3.**" in doc
    assert leaking == documented_open, (
        "docs/local-vs-prod.md § 6 and the committed compose file disagree about whether the "
        f"ECR-pull leak is open (compose leaking={leaking}, doc says open={documented_open}). "
        "Update the doc in the same change that closes the leak."
    )


# ── drift: the doc's selectors must still be read by something ────────────────────────

_SECTION_RE = re.compile(r"^## 1\..*?(?=^## 3\.)", re.DOTALL | re.MULTILINE)
_ENV_TOKEN_RE = re.compile(r"`([^`]+)`")
_SHOUTY = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")

#: Where a selector is allowed to be read from. A variable named in the contract table
#: with no reader anywhere here is prose about a switch that does not exist.
_READER_GLOBS = (
    "backend/archimedes/**/*.py",
    "scripts/*.py",
    "scripts/*.sh",
    ".github/workflows/*.yml",
    "docker-compose*.yml",
    "Makefile",
)


def _selectors_named_in_the_contract() -> set[str]:
    section = _SECTION_RE.search(DOC.read_text(encoding="utf-8"))
    assert section, "docs/local-vs-prod.md no longer has the '## 1. The contract' section"
    names: set[str] = set()
    for span in _ENV_TOKEN_RE.findall(section.group(0)):
        names.update(_SHOUTY.findall(span))
    return names


def test_every_selector_in_the_contract_table_has_a_reader() -> None:
    names = _selectors_named_in_the_contract()
    assert len(names) >= 5, f"the contract table names suspiciously few selectors: {sorted(names)}"

    haystack = []
    for pattern in _READER_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path.is_file():
                haystack.append(path.read_text(encoding="utf-8", errors="replace"))
    blob = "\n".join(haystack)

    orphans = sorted(n for n in names if n not in blob)
    assert not orphans, (
        "docs/local-vs-prod.md § 1-2 names selectors nothing in the tree reads:\n  "
        + "\n  ".join(orphans)
        + "\n\nEither the code stopped reading them (fix the doc) or they moved somewhere "
        "outside _READER_GLOBS (fix the globs)."
    )


def test_the_ssm_production_gate_is_intact() -> None:
    """The single line that stops a local run pulling production secrets.

    Deleting it restores #1044 item 2 exactly: `load_ssm_secrets()` would run at import
    on any machine with ambient AWS credentials.
    """
    text = MAIN_PY.read_text(encoding="utf-8")
    assert 'if os.getenv("PUBLIC_DOMAIN"):\n    load_ssm_secrets()' in text, (
        "backend/archimedes/main.py no longer gates load_ssm_secrets() on PUBLIC_DOMAIN. "
        "Without that gate a local `docker compose up` on a machine with ambient AWS "
        "credentials pulls real production secrets into the process (#1044 item 2)."
    )
    assert not re.search(r"^load_ssm_secrets\(\)", text, re.MULTILINE), (
        "load_ssm_secrets() is called unconditionally at module level — that is the "
        "pre-#1280 behaviour #1044 item 2 named as a leak."
    )


def test_generation_auth_has_no_local_bypass() -> None:
    """The § 4 decision, asserted against the code that implements it."""
    text = MAIN_PY.read_text(encoding="utf-8")
    assert "app.include_router(generate_router, dependencies=[Depends(require_current_user)])" in text, (
        "the generation router is no longer registered with an unconditional auth "
        "dependency. docs/local-vs-prod.md § 4 records the decision that local mode gets "
        "no auth bypass — change the doc in the same commit if that decision changed."
    )
    assert "REQUIRE_SIWE_FOR_GENERATION" not in text, (
        "the retired generation-auth opt-out is back in main.py. It was deleted by #1300; "
        "reintroducing it re-opens the local/production divergence § 4 rules out."
    )


# ── the doc is findable ───────────────────────────────────────────────────────────────


def test_doc_carries_the_conventions_front_matter() -> None:
    head = DOC.read_text(encoding="utf-8").split("\n", 8)
    joined = "\n".join(head)
    for field in ("**status:**", "**owner:**", "**updated:**", "**superseded-by:**"):
        assert field in joined, f"docs/local-vs-prod.md front matter is missing {field} (docs/CONVENTIONS.md § 3)"


class _MkdocsLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` that tolerates mkdocs' `!!python/name:` tags.

    `mkdocs.yml` names the mermaid fence formatter the way mkdocs-material
    documents it — `format: !!python/name:pymdownx.superfences.fence_code_format`
    — and `yaml.safe_load` refuses to construct that tag (`ConstructorError`),
    which would make every test in this module fail on a config mkdocs itself
    reads fine. Resolving the tag to its *name* is enough here: nothing in this
    suite calls the formatter, and a SafeLoader that imports arbitrary dotted
    paths would be the unsafe loader wearing a different hat.
    """


_MkdocsLoader.add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)


def test_doc_is_in_the_index_and_the_nav() -> None:
    assert "(local-vs-prod.md)" in DOCS_INDEX.read_text(encoding="utf-8"), (
        "docs/local-vs-prod.md has no row in docs/doc-index.md — 'a doc not listed here does not exist'"
    )
    nav = yaml.load(MKDOCS_YML.read_text(encoding="utf-8"), Loader=_MkdocsLoader)["nav"]

    def targets(node) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, list):
            return [t for item in node for t in targets(item)]
        if isinstance(node, dict):
            return [t for value in node.values() for t in targets(value)]
        return []

    assert "local-vs-prod.md" in targets(nav), "docs/local-vs-prod.md is not published by the docs site"
