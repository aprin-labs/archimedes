"""CI deploy must ship the #1818 P3 readiness check, because terraform cannot.

P3 pointed the CONTAINER health check at ``/health/ready`` in
``backend/Dockerfile`` and ``infra/ecs.tf``. Neither of those reaches
production on its own:

* ``deploy.yml`` clones the *currently registered* task definition, retags
  images and registers a new revision. It does not apply terraform, so a
  ``healthCheck`` that exists only in ``ecs.tf`` is not live until somebody
  applies — and a clone registered before P3 goes on shipping the old
  ``/health`` command forever. That is task-def :211's shape applied to a probe
  instead of a flag.
* Since #1799 the apply that would have fixed it is gone too:
  ``aws_ecs_task_definition.backend`` carries ``lifecycle { ignore_changes =
  [container_definitions] }``, so terraform stops writing container settings
  altogether. The pipeline is not merely first — inside
  ``containerDefinitions`` it is the only writer there is.
* ECS reads the health check off the task definition. An image-only
  ``HEALTHCHECK`` is invisible to the agent for container-dependency purposes,
  so the Dockerfile line alone gives nginx's ``dependsOn condition = HEALTHY``
  nothing to wait on and gives the scheduler no 503 to act on.

So the property held here is the same one the ``PAPER_ADVANCE_ENABLED`` pin
holds, on two more names: what ships is what
``ecs_rewrite_task_def.READINESS_HEALTH_CHECK_COMMAND`` and
``HEALTH_STALE_UNREADY_VALUE`` say, on every deploy, whatever the clone carried.

These tests run the same function ``deploy.yml`` invokes. Hermetic — no AWS, no
terraform binary, no network. Every assertion is paired with the mutation it
would catch, and every fixture states the pre-P3 value explicitly so no
assertion can pass by accident of the fixture already agreeing with it.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REWRITE_PY = REPO_ROOT / ".github" / "scripts" / "ecs_rewrite_task_def.py"
ECS_TF = REPO_ROOT / "infra" / "ecs.tf"
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"

BACKEND_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:deadbeef"
NGINX_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-nginx:deadbeef"
AUTH_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-auth:deadbeef"

#: What every registered revision carries today: the LIVENESS command. Written
#: out rather than derived so the rewrite assertions below are measured against
#: the real pre-P3 string.
PRE_P3_COMMAND = [
    "CMD-SHELL",
    "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health')\" || exit 1",
]


def _load_rewrite():
    spec = importlib.util.spec_from_file_location("ecs_rewrite_task_def", REWRITE_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _last_good_task_def(
    *,
    health_check: dict[str, Any] | None | str = "pre-p3",
    stale_unready: str | None = None,
) -> dict[str, Any]:
    """A cloned live revision, shaped like ``describe-task-definition`` returns.

    Defaults are the production input for the first deploy after P3 merges: a
    backend container whose health check still probes ``/health`` and which has
    never heard of ``HEALTH_STALE_UNREADY_S``. ``health_check=None`` is the
    harder shape — a revision with no health check at all, where ECS sees no
    container health and nginx's ``dependsOn: HEALTHY`` has nothing to wait on.
    """
    env = [
        {"name": "APP_ENV", "value": "production"},
        {"name": "PAPER_TRADING", "value": "true"},
    ]
    if stale_unready is not None:
        env.append({"name": "HEALTH_STALE_UNREADY_S", "value": stale_unready})
    backend: dict[str, Any] = {
        "name": "backend",
        "image": "old-backend:011b6bfc",
        "environment": env,
    }
    if health_check == "pre-p3":
        backend["healthCheck"] = {
            "command": list(PRE_P3_COMMAND),
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 30,
        }
    elif health_check is not None:
        backend["healthCheck"] = health_check
    return {
        "family": "archimedes-backend",
        "taskDefinitionArn": "arn:aws:ecs:us-east-1:037613907429:task-definition/archimedes-backend:233",
        "revision": 233,
        "status": "ACTIVE",
        "compatibilities": ["FARGATE"],
        "registeredAt": "2026-09-03T12:00:00Z",
        "cpu": "1024",
        "memory": "3072",
        "containerDefinitions": [
            backend,
            {"name": "nginx", "image": "old-nginx:011b6bfc", "environment": [{"name": "FOO", "value": "1"}]},
            {"name": "auth", "image": "old-auth:011b6bfc"},
        ],
    }


def _rewrite(task_def: dict[str, Any]) -> dict[str, Any]:
    mod = _load_rewrite()
    return mod.rewrite_registered_task_definition(
        task_def,
        backend_image=BACKEND_IMAGE,
        nginx_image=NGINX_IMAGE,
        auth_image=AUTH_IMAGE,
    )


def _container(task_def: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in task_def["containerDefinitions"] if c["name"] == name)


def _backend_env(task_def: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e["value"] for e in _container(task_def, "backend").get("environment") or []}


class TestTheFixtureIsThePreP3Shape:
    """Anti-vacuity. Every rewrite assertion below is only worth something while
    the input it is measured against really is the thing that ships today."""

    def test_the_clone_probes_plain_health_and_lacks_the_threshold(self):
        backend = _container(_last_good_task_def(), "backend")
        assert "/health/ready" not in backend["healthCheck"]["command"][1]
        assert "urlopen('http://localhost:8000/health')" in backend["healthCheck"]["command"][1]
        assert "HEALTH_STALE_UNREADY_S" not in {e["name"] for e in backend["environment"]}


class TestTheRewritePointsTheContainerCheckAtReadiness:
    def test_a_clone_still_probing_plain_health_ships_the_readiness_command(self):
        """The production input for the first deploy after this PR merges."""
        mod = _load_rewrite()
        out = _rewrite(_last_good_task_def())
        command = _container(out, "backend")["healthCheck"]["command"]
        assert command == mod.READINESS_HEALTH_CHECK_COMMAND
        assert "/health/ready" in command[1]
        assert command != PRE_P3_COMMAND

    def test_the_timing_of_the_live_revision_is_preserved_except_start_period(self):
        """What the check ASKS is this file's decision; interval / timeout /
        retries belong to the registered revision. startPeriod is also this
        file's decision: 90s so the #1713 warmup budget fits inside ECS's
        ignored-failure window. A deploy that meant to change a URL must not
        silently re-time interval/timeout/retries, but it MUST overwrite a
        cloned startPeriod 30 (or any other value) to 90.
        """
        out = _rewrite(
            _last_good_task_def(
                health_check={
                    "command": list(PRE_P3_COMMAND),
                    "interval": 45,
                    "timeout": 9,
                    "retries": 5,
                    "startPeriod": 120,
                }
            )
        )
        check = _container(out, "backend")["healthCheck"]
        assert (check["interval"], check["timeout"], check["retries"]) == (45, 9, 5)
        assert check["startPeriod"] == 90
        assert "/health/ready" in check["command"][1]

    def test_a_clone_with_no_health_check_at_all_gets_the_whole_block(self):
        """The hole an env-only pin would leave open.

        ECS reads container health off the task definition. With the key absent
        the image's own HEALTHCHECK is invisible to the agent: nginx's
        ``dependsOn condition = HEALTHY`` has nothing to wait on and no
        scheduler ever sees the 503, so /health/ready would be a JSON field
        nothing polls.
        """
        out = _rewrite(_last_good_task_def(health_check=None))
        check = _container(out, "backend")["healthCheck"]
        assert "/health/ready" in check["command"][1]
        assert check["interval"] == 30
        assert check["timeout"] == 5
        assert check["retries"] == 3
        assert check["startPeriod"] == 90

    def test_rewriting_an_already_readied_clone_still_pins_start_period(self):
        """Every deploy after the first. Command/interval/timeout/retries are
        idempotent; a leftover startPeriod 30 is overwritten to 90.
        """
        mod = _load_rewrite()
        already = {
            "command": list(mod.READINESS_HEALTH_CHECK_COMMAND),
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 30,
        }
        out = _rewrite(_last_good_task_def(health_check=dict(already)))
        check = _container(out, "backend")["healthCheck"]
        assert check["command"] == already["command"]
        assert check["interval"] == 30
        assert check["timeout"] == 5
        assert check["retries"] == 3
        assert check["startPeriod"] == 90

    def test_unknown_health_check_fields_survive(self):
        """A field ECS grows later, or one an operator set on the live revision,
        must not be dropped by a URL change."""
        out = _rewrite(
            _last_good_task_def(health_check={"command": list(PRE_P3_COMMAND), "interval": 30, "someNewKnob": 7})
        )
        assert _container(out, "backend")["healthCheck"]["someNewKnob"] == 7

    def test_nginx_and_auth_get_no_health_check(self):
        """Only the backend serves /health/ready. Installing a check on a
        container that cannot answer it fails that container forever."""
        out = _rewrite(_last_good_task_def())
        assert "healthCheck" not in _container(out, "nginx")
        assert "healthCheck" not in _container(out, "auth")


class TestTheRewritePinsTheThreshold:
    def test_a_clone_without_the_name_ships_the_pinned_value(self):
        mod = _load_rewrite()
        env = _backend_env(_rewrite(_last_good_task_def()))
        assert env["HEALTH_STALE_UNREADY_S"] == mod.HEALTH_STALE_UNREADY_VALUE == "900"
        assert env["APP_ENV"] == "production"
        assert env["PAPER_TRADING"] == "true"

    def test_a_disagreeing_value_is_overwritten_not_duplicated(self):
        """``"0"`` is the documented emergency stop. It holds on the live
        revision until the next deploy and then this pin takes it back — which
        is why the durable pull-back is the constant in the script, and why the
        pin must REPLACE rather than append (ECS takes the last entry, so an
        append would look like it worked for the wrong reason).
        """
        out = _rewrite(_last_good_task_def(stale_unready="0"))
        names = [e["name"] for e in _container(out, "backend")["environment"]]
        assert names.count("HEALTH_STALE_UNREADY_S") == 1
        assert _backend_env(out)["HEALTH_STALE_UNREADY_S"] == "900"

    def test_an_existing_pin_is_not_duplicated(self):
        out = _rewrite(_last_good_task_def(stale_unready="900"))
        names = [e["name"] for e in _container(out, "backend")["environment"]]
        assert names.count("HEALTH_STALE_UNREADY_S") == 1

    def test_the_paper_advance_pin_still_ships_alongside_it(self):
        """The two pins compose. A regression that made the second one replace
        the first would silently unpin the tick kill-switch."""
        env = _backend_env(_rewrite(_last_good_task_def()))
        assert env["PAPER_ADVANCE_ENABLED"] == "true"
        assert env["HEALTH_STALE_UNREADY_S"] == "900"

    def test_nginx_and_auth_are_not_given_the_threshold(self):
        out = _rewrite(_last_good_task_def())
        assert _container(out, "nginx")["environment"] == [{"name": "FOO", "value": "1"}]
        assert "environment" not in _container(out, "auth")


class TestOwnershipStaysInsideContainerDefinitions:
    """#1799's ignore list covers ``container_definitions`` and nothing else, so
    the moment this script writes a top-level field the two registrars are
    fighting again somewhere new."""

    def test_no_top_level_field_is_rewritten(self):
        mod = _load_rewrite()
        original = _last_good_task_def()
        out = _rewrite(json.loads(json.dumps(original)))
        dropped = set(mod._DROP_FIELDS)
        for key, value in original.items():
            if key in dropped or key == "containerDefinitions":
                continue
            assert out[key] == value, f"the readiness pin gave the top-level field {key!r} a second registrar"

    def test_the_describe_only_fields_are_still_dropped(self):
        out = _rewrite(_last_good_task_def())
        for field in ("taskDefinitionArn", "revision", "status", "compatibilities", "registeredAt"):
            assert field not in out


class TestTheCliShipsIt:
    def test_cli_writes_both_the_command_and_the_pin(self, tmp_path):
        """The production invocation shape: file in, JSON out, exit 0."""
        import subprocess
        import sys

        src = tmp_path / "current-task-def.json"
        src.write_text(json.dumps(_last_good_task_def()), encoding="utf-8")
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
        out = json.loads(result.stdout)
        assert "/health/ready" in _container(out, "backend")["healthCheck"]["command"][1]
        assert _backend_env(out)["HEALTH_STALE_UNREADY_S"] == "900"

    def test_the_values_are_literals_not_cli_flags(self):
        """Same argument as PAPER_ADVANCE_VALUE: an option is an option someone
        can forget to pass, and its default would quietly decide production."""
        source = REWRITE_PY.read_text(encoding="utf-8")
        assert 'HEALTH_STALE_UNREADY_VALUE = "900"' in source
        assert "#1818" in source
        assert 'add_argument("--health' not in source
        after_main = source.split("def main(", 1)[1]
        assert "HEALTH_STALE_UNREADY_VALUE" not in after_main
        assert "READINESS_HEALTH_CHECK_COMMAND" not in after_main

    def test_the_command_constant_actually_probes_the_readiness_url(self):
        mod = _load_rewrite()
        assert mod.READINESS_PROBE_URL == "http://localhost:8000/health/ready"
        assert mod.READINESS_HEALTH_CHECK_COMMAND[0] == "CMD-SHELL"
        assert mod.READINESS_PROBE_URL in mod.READINESS_HEALTH_CHECK_COMMAND[1]
        # `|| exit 1` is what turns urlopen's 503 exception into an UNHEALTHY
        # container rather than a step that merely logs a traceback.
        assert mod.READINESS_HEALTH_CHECK_COMMAND[1].endswith("|| exit 1")


class TestTheDocumentedTwinsAgreeWithThePipeline:
    """``infra/ecs.tf`` and the Dockerfile are parity documentation now: #1799's
    ``ignore_changes`` makes terraform's copy inert and the image's own
    HEALTHCHECK is invisible to the ECS agent. Documentation that disagrees with
    the writer is worse than none, so it is pinned to the writer."""

    def test_ecs_tf_states_the_same_command_and_threshold(self):
        mod = _load_rewrite()
        ecs = ECS_TF.read_text(encoding="utf-8")
        assert mod.READINESS_PROBE_URL in ecs
        assert f'{{ name = "{mod.HEALTH_STALE_UNREADY_NAME}", value = "{mod.HEALTH_STALE_UNREADY_VALUE}" }}' in ecs

    def test_ecs_tf_says_the_pipeline_script_is_the_effective_writer(self):
        """Without this note the healthCheck block reads as the thing that
        ships, and the next person edits it and waits for a deploy that will
        never carry it (#1799)."""
        ecs = ECS_TF.read_text(encoding="utf-8")
        block = ecs[ecs.index("healthCheck = {") - 2500 : ecs.index("healthCheck = {")]
        assert ".github/scripts/ecs_rewrite_task_def.py" in block
        assert "#1799" in block

    def test_the_dockerfile_healthcheck_matches_the_pinned_url(self):
        mod = _load_rewrite()
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        healthcheck = re.search(r"^HEALTHCHECK .*?\n(?:.*\n)?", dockerfile, re.MULTILINE)
        assert healthcheck, "backend/Dockerfile has no HEALTHCHECK"
        assert mod.READINESS_PROBE_URL in healthcheck.group(0)


class TestAMissingBackendContainerIsStillFatal:
    def test_rewrite_error_names_the_readiness_check(self):
        mod = _load_rewrite()
        task_def = _last_good_task_def()
        task_def["containerDefinitions"] = [c for c in task_def["containerDefinitions"] if c["name"] != "backend"]
        with pytest.raises(mod.RewriteError, match="/health/ready"):
            mod.rewrite_registered_task_definition(
                task_def,
                backend_image=BACKEND_IMAGE,
                nginx_image=NGINX_IMAGE,
                auth_image=AUTH_IMAGE,
            )
