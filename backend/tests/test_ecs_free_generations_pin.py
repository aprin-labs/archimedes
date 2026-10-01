"""``FREE_GENERATIONS_PER_ACCOUNT=3`` must ship on BOTH task-definition paths.

Flip-list finding A5: prod grants three free generations per account because
``services/free_generations.allowance()`` falls back to ``DEFAULT_ALLOWANCE``,
not because anyone put the number in a task definition. The owner's 2026-09-02
call was to pin it explicitly — "pipeline or terraform, whichever aligns with
what we already do" — and what this repo already does is *both*, because
neither alone is sufficient:

- ``infra/ecs.tf`` is the declared baseline, but it is drifted (#1799) and
  ``deploy.yml`` never applies terraform, so a pin that lives only there is not
  live until somebody runs an apply.
- ``.github/scripts/ecs_rewrite_task_def.py`` is the path that actually ships —
  it clones the currently registered revision — but it is not a declaration
  anyone reads when reasoning about infrastructure.

That is the same two-path split ``PAPER_ADVANCE_ENABLED`` already has, and the
:211 incident is what happens when only one side is covered. So the invariant
here is the pair: the name and the value on both paths, and the two agreeing.

Hermetic: the only inputs are two files in the repo. No AWS, no terraform, no
network, no env.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REWRITE_PY = REPO_ROOT / ".github" / "scripts" / "ecs_rewrite_task_def.py"
ECS_TF = REPO_ROOT / "infra" / "ecs.tf"
ALLOWANCE_PY = REPO_ROOT / "backend" / "archimedes" / "services" / "free_generations.py"

NAME = "FREE_GENERATIONS_PER_ACCOUNT"
VALUE = "3"

BACKEND_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:deadbeef"
NGINX_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-nginx:deadbeef"
AUTH_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-auth:deadbeef"


def _load_rewrite():
    spec = importlib.util.spec_from_file_location("ecs_rewrite_task_def", REWRITE_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _task_def(*, free_generations: str | None = None) -> dict:
    """A cloned live revision. ``None`` is today's shape: the name is absent."""
    env = [
        {"name": "APP_ENV", "value": "production"},
        {"name": "GENERATION_PAYMENT_REQUIRED", "value": "true"},
    ]
    if free_generations is not None:
        env.append({"name": NAME, "value": free_generations})
    env.append({"name": "GENERATION_PRICE_USD", "value": "2.00"})
    return {
        "family": "archimedes-backend",
        "taskDefinitionArn": "arn:aws:ecs:us-east-1:037613907429:task-definition/archimedes-backend:212",
        "revision": 212,
        "status": "ACTIVE",
        "containerDefinitions": [
            {"name": "backend", "image": "old-backend:cafe", "environment": env},
            {"name": "nginx", "image": "old-nginx:cafe", "environment": [{"name": "FOO", "value": "1"}]},
            {"name": "auth", "image": "old-auth:cafe"},
        ],
    }


def _rewrite(task_def: dict) -> dict:
    return _load_rewrite().rewrite_registered_task_definition(
        task_def,
        backend_image=BACKEND_IMAGE,
        nginx_image=NGINX_IMAGE,
        auth_image=AUTH_IMAGE,
    )


def _backend(task_def: dict) -> dict:
    return next(c for c in task_def["containerDefinitions"] if c["name"] == "backend")


def _backend_env(task_def: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in _backend(task_def).get("environment") or []}


def _backend_env_names(task_def: dict) -> list[str]:
    return [e["name"] for e in _backend(task_def).get("environment") or []]


class TestThePipelinePathPinsIt:
    """The path that actually registers a revision. Mutation: delete the
    ``pin_backend_free_generations`` call from
    ``rewrite_registered_task_definition`` and every case here goes red."""

    def test_a_clone_without_the_name_ships_the_pin(self) -> None:
        """Today's production input: :212 has never carried the name."""
        env = _backend_env(_rewrite(_task_def(free_generations=None)))
        assert env[NAME] == VALUE
        # ...without eating the neighbours.
        assert env["APP_ENV"] == "production"
        assert env["GENERATION_PAYMENT_REQUIRED"] == "true"
        assert env["GENERATION_PRICE_USD"] == "2.00"

    @pytest.mark.parametrize("carried", ["0", "10", "", "three"])
    def test_a_different_value_is_overwritten_not_appended(self, carried: str) -> None:
        """ECS takes the LAST entry, so appending would look like it worked."""
        out = _rewrite(_task_def(free_generations=carried))
        assert _backend_env(out)[NAME] == VALUE
        assert _backend_env_names(out).count(NAME) == 1

    def test_the_matching_value_is_idempotent_and_keeps_its_position(self) -> None:
        """Every deploy after the first. Order matters only in that a stable
        rewrite produces a stable diff between task-def revisions."""
        out = _rewrite(_task_def(free_generations=VALUE))
        names = _backend_env_names(out)
        assert names.count(NAME) == 1
        assert _backend_env(out)[NAME] == VALUE
        # The pin does not migrate to the end of the list, which would make
        # every deploy's task-def diff churn even when nothing changed. Checked
        # on the helper alone so the paper-advance pin's own append (which
        # legitimately lands last) does not muddy the claim.
        mod = _load_rewrite()
        assert mod.pin_backend_free_generations(
            [
                {"name": "APP_ENV", "value": "production"},
                {"name": NAME, "value": VALUE},
                {"name": "GENERATION_PRICE_USD", "value": "2.00"},
            ]
        ) == [
            {"name": "APP_ENV", "value": "production"},
            {"name": NAME, "value": VALUE},
            {"name": "GENERATION_PRICE_USD", "value": "2.00"},
        ]

    def test_a_duplicated_name_collapses_to_the_pin(self) -> None:
        """Two entries is a silent override; one of them must not survive."""
        task_def = _task_def(free_generations="0")
        _backend(task_def)["environment"].append({"name": NAME, "value": "99"})
        out = _rewrite(task_def)
        assert _backend_env_names(out).count(NAME) == 1
        assert _backend_env(out)[NAME] == VALUE

    def test_the_paper_advance_pin_still_ships_alongside(self) -> None:
        """The two pins compose; adding one must not drop the other."""
        mod = _load_rewrite()
        env = _backend_env(_rewrite(_task_def()))
        assert env[mod.PAPER_ADVANCE_NAME] == mod.PAPER_ADVANCE_VALUE

    def test_other_containers_do_not_get_the_allowance(self) -> None:
        out = _rewrite(_task_def())
        nginx = next(c for c in out["containerDefinitions"] if c["name"] == "nginx")
        assert nginx["environment"] == [{"name": "FOO", "value": "1"}]
        auth = next(c for c in out["containerDefinitions"] if c["name"] == "auth")
        assert NAME not in {e.get("name") for e in auth.get("environment") or []}

    def test_the_helper_is_pure(self) -> None:
        """It must not mutate the caller's list — the clone is read again."""
        mod = _load_rewrite()
        original = [{"name": NAME, "value": "0"}]
        mod.pin_backend_free_generations(original)
        assert original == [{"name": NAME, "value": "0"}]

    def test_none_is_accepted_as_an_absent_environment(self) -> None:
        mod = _load_rewrite()
        assert mod.pin_backend_free_generations(None) == [{"name": NAME, "value": VALUE}]

    def test_the_value_is_hard_coded_not_a_cli_flag(self) -> None:
        """Same reasoning as PAPER_ADVANCE_VALUE: a flag with a default makes
        the deployed number a property of whoever wrote the workflow line."""
        source = REWRITE_PY.read_text(encoding="utf-8")
        assert f'FREE_GENERATIONS_VALUE = "{VALUE}"' in source
        assert 'add_argument("--free' not in source
        assert "FREE_GENERATIONS_VALUE" not in source.split("def main(", 1)[1]

    def test_the_cli_emits_the_pin_and_a_notice(self, tmp_path: Path) -> None:
        """The production invocation shape: JSON on stdout, ``::notice`` on
        stderr so the deploy log records the change rather than leaving an
        operator to diff two task-def revisions after the fact."""
        src = tmp_path / "current-task-def.json"
        src.write_text(json.dumps(_task_def(free_generations="0")), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(REWRITE_PY),
                "--backend-image",
                BACKEND_IMAGE,
                "--nginx-image",
                NGINX_IMAGE,
                "--auth-image",
                AUTH_IMAGE,
                str(src),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert _backend_env(json.loads(result.stdout))[NAME] == VALUE
        assert f"::notice::{NAME} pinned to {VALUE}" in result.stderr
        assert "'0'" in result.stderr, "the notice must say what the clone carried"

    def test_no_notice_when_the_clone_already_agrees(self, capsys) -> None:
        """A ``::notice`` on every deploy is noise an operator learns to skip."""
        mod = _load_rewrite()
        capsys.readouterr()
        mod.pin_backend_free_generations([{"name": NAME, "value": VALUE}])
        assert capsys.readouterr().err == ""


class TestTheTerraformPathPinsIt:
    """The declared baseline. Mutation: delete the line from ``infra/ecs.tf``
    and this goes red (as does test_ecs_backend_secrets.py)."""

    def test_ecs_tf_declares_the_name_and_value(self) -> None:
        src = ECS_TF.read_text(encoding="utf-8")
        matches = re.findall(rf'{{\s*name\s*=\s*"{NAME}"\s*,\s*value\s*=\s*"([^"]*)"\s*}}', src)
        assert matches, f"{NAME} is not declared in infra/ecs.tf"
        assert matches == [VALUE], f"infra/ecs.tf declares {NAME} as {matches}, expected ['{VALUE}']"


class TestTheTwoPathsAndTheCodeAgree:
    """Three declarations of one number is two chances to drift.

    A pipeline pin of 3 against a terraform pin of 10 is not a pin — the number
    prod serves would then depend on whether anybody has run an apply since the
    last deploy, which is exactly the "decided by nobody" state A5 names.
    """

    def test_the_pipeline_and_terraform_pins_are_the_same_number(self) -> None:
        mod = _load_rewrite()
        tf = re.findall(
            rf'{{\s*name\s*=\s*"{NAME}"\s*,\s*value\s*=\s*"([^"]*)"\s*}}',
            ECS_TF.read_text(encoding="utf-8"),
        )
        assert tf == [mod.FREE_GENERATIONS_VALUE], (
            f"infra/ecs.tf says {tf} and ecs_rewrite_task_def.py says "
            f"{mod.FREE_GENERATIONS_VALUE!r}. The pipeline pin wins on every deploy, "
            "so terraform would silently be the lie."
        )

    def test_the_pin_matches_the_code_default_so_the_change_is_plumbing(self) -> None:
        """This PR pins what prod already serves. If someone changes the pinned
        number without changing DEFAULT_ALLOWANCE (or vice versa), that is a
        policy change wearing plumbing's clothes — it should be argued, not
        slipped in, and this is where it gets caught."""
        mod = _load_rewrite()
        default = re.search(r"^DEFAULT_ALLOWANCE = (\d+)$", ALLOWANCE_PY.read_text(encoding="utf-8"), re.M)
        assert default, "DEFAULT_ALLOWANCE moved in services/free_generations.py"
        assert default.group(1) == mod.FREE_GENERATIONS_VALUE == VALUE


class TestTheReaderStillReadsItAtRequestTime:
    """A pin on an env var that is snapshotted at import would be a pin on
    nothing an operator can move without a deploy — and the module's own
    docstring promises the opposite ("read per call, not cached")."""

    def test_allowance_reads_the_environment_on_every_call(self, monkeypatch) -> None:
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from archimedes.services import free_generations

        monkeypatch.setenv(NAME, "7")
        assert free_generations.allowance() == 7
        monkeypatch.setenv(NAME, "1")
        assert free_generations.allowance() == 1, "the allowance was cached at import"
        monkeypatch.delenv(NAME)
        assert free_generations.allowance() == int(VALUE), "the code default is no longer 3"
