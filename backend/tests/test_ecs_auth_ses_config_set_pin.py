"""CI deploy must ship ``SES_CONFIGURATION_SET`` on the auth container (#1804).

The bounce/complaint loop in ``infra/ses_events.tf`` is push-side: SES only
publishes a bounce or complaint event for a send that NAMES the configuration
set (``aws_sesv2_configuration_set.mail``). ``auth/mailer.js`` names it from
``env.SES_CONFIGURATION_SET``. ``infra/ecs.tf`` defines that env on the auth
container — and none of that reaches production:

* ``deploy.yml`` clones the *currently registered* task definition and never
  applies terraform, and since #1799 (PR #1833) ``aws_ecs_task_definition.
  backend`` carries ``lifecycle { ignore_changes = [container_definitions] }``
  so terraform stops writing container settings altogether. A value that exists
  only in ``ecs.tf`` is prose. Revision 250 (2026-09-04) carried the name on
  NO container; the event destination would have applied cleanly and heard
  nothing, forever, with every guard in ``test_ses_event_wiring.py`` green.

So the property held here is the ``PAPER_ADVANCE_ENABLED`` one, on the auth
container: what ships is what ``ecs_rewrite_task_def.SES_CONFIGURATION_SET_
VALUE`` says, on every deploy, whatever the clone carried — and that value is
the name terraform gives the configuration set, so the two cannot drift apart.

These tests run the same function ``deploy.yml`` invokes. Hermetic: no AWS, no
terraform binary, no network. Every fixture states the pre-pin shape explicitly
(an auth container that has never heard of the name) so no assertion can pass
by accident of the fixture already agreeing with it.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
REWRITE_PY = REPO_ROOT / ".github" / "scripts" / "ecs_rewrite_task_def.py"
SES_EVENTS_TF = REPO_ROOT / "infra" / "ses_events.tf"
VARIABLES_TF = REPO_ROOT / "infra" / "variables.tf"
MAILER_JS = REPO_ROOT / "auth" / "mailer.js"

BACKEND_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:deadbeef"
NGINX_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-nginx:deadbeef"
AUTH_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-auth:deadbeef"

ENV_NAME = "SES_CONFIGURATION_SET"


def _load_rewrite():
    spec = importlib.util.spec_from_file_location("ecs_rewrite_task_def", REWRITE_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _last_good_task_def(
    *,
    auth_env: list[dict[str, str]] | None | str = "revision-250",
) -> dict[str, Any]:
    """A cloned live revision, shaped like ``describe-task-definition`` returns.

    The default is the production input for the first deploy after this pin
    merges: revision 250's auth container, which carries its mailer settings
    and has never heard of ``SES_CONFIGURATION_SET``. ``auth_env=None`` is the
    harder shape — an auth container with no ``environment`` key at all.
    """
    auth: dict[str, Any] = {"name": "auth", "image": "old-auth:011b6bfc"}
    if auth_env == "revision-250":
        auth["environment"] = [
            {"name": "EMAIL_MAILER", "value": "ses"},
            {"name": "EMAIL_SENDER", "value": "no-reply@archimedes-arc.com"},
        ]
    elif auth_env is not None:
        auth["environment"] = auth_env
    return {
        "family": "archimedes-backend",
        "taskDefinitionArn": "arn:aws:ecs:us-east-1:037613907429:task-definition/archimedes-backend:250",
        "revision": 250,
        "status": "ACTIVE",
        "compatibilities": ["FARGATE"],
        "registeredAt": "2026-09-04T09:00:00Z",
        "cpu": "1024",
        "memory": "3072",
        "containerDefinitions": [
            {
                "name": "backend",
                "image": "old-backend:011b6bfc",
                "environment": [{"name": "APP_ENV", "value": "production"}],
                "healthCheck": {
                    "command": ["CMD-SHELL", "exit 0"],
                    "interval": 30,
                    "timeout": 5,
                    "retries": 3,
                    "startPeriod": 30,
                },
            },
            {"name": "nginx", "image": "old-nginx:011b6bfc", "environment": [{"name": "FOO", "value": "1"}]},
            auth,
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


def _env_list(task_def: dict[str, Any], name: str) -> list[dict[str, str]]:
    return list(_container(task_def, name).get("environment") or [])


def _env(task_def: dict[str, Any], name: str) -> dict[str, str]:
    return {e["name"]: e["value"] for e in _env_list(task_def, name)}


class TestTheFixtureIsTheRevision250Shape:
    """Anti-vacuity. Every rewrite assertion below is only worth something while
    the input it is measured against really is the thing that shipped."""

    def test_the_clone_has_mailer_settings_but_not_the_name(self):
        auth = _container(_last_good_task_def(), "auth")
        names = [e["name"] for e in auth["environment"]]
        assert "EMAIL_MAILER" in names
        assert ENV_NAME not in names

    def test_no_container_in_the_clone_carries_the_name(self):
        td = _last_good_task_def()
        assert all(ENV_NAME not in _env(td, c) for c in ("backend", "nginx", "auth"))


class TestTheRewriteShipsThePin:
    def test_a_clone_without_the_name_ships_the_pinned_value(self):
        mod = _load_rewrite()
        out = _rewrite(_last_good_task_def())
        assert _env(out, "auth")[ENV_NAME] == mod.SES_CONFIGURATION_SET_VALUE

    def test_an_auth_container_with_no_environment_at_all_gets_one(self):
        mod = _load_rewrite()
        out = _rewrite(_last_good_task_def(auth_env=None))
        assert _env_list(out, "auth") == [{"name": ENV_NAME, "value": mod.SES_CONFIGURATION_SET_VALUE}]

    def test_the_clones_other_auth_env_is_preserved_in_order(self):
        out = _rewrite(_last_good_task_def())
        names = [e["name"] for e in _env_list(out, "auth")]
        assert names[:2] == ["EMAIL_MAILER", "EMAIL_SENDER"]
        assert names[-1] == ENV_NAME

    def test_a_disagreeing_value_is_overwritten_not_duplicated(self):
        mod = _load_rewrite()
        stale = [{"name": ENV_NAME, "value": "some-other-set"}, {"name": "EMAIL_MAILER", "value": "ses"}]
        out = _rewrite(_last_good_task_def(auth_env=stale))
        entries = [e for e in _env_list(out, "auth") if e["name"] == ENV_NAME]
        assert entries == [{"name": ENV_NAME, "value": mod.SES_CONFIGURATION_SET_VALUE}]

    def test_an_existing_pin_is_not_duplicated(self):
        mod = _load_rewrite()
        already = [{"name": ENV_NAME, "value": mod.SES_CONFIGURATION_SET_VALUE}]
        out = _rewrite(_last_good_task_def(auth_env=already))
        assert [e["name"] for e in _env_list(out, "auth")].count(ENV_NAME) == 1

    def test_pinning_an_already_pinned_environment_changes_nothing(self):
        mod = _load_rewrite()
        first = mod.pin_auth_ses_configuration_set(_container(_last_good_task_def(), "auth")["environment"])
        second = mod.pin_auth_ses_configuration_set(json.loads(json.dumps(first)))
        assert second == first

    def test_the_auth_image_is_still_retagged(self):
        assert _container(_rewrite(_last_good_task_def()), "auth")["image"] == AUTH_IMAGE


class TestOnlyTheAuthContainerSends:
    """``auth/mailer.js`` only runs in the auth container. A pin on backend or
    nginx would satisfy a file-wide grep while no send named the set — the
    wrong-container shape ``test_ses_event_wiring.py`` refuses in ``ecs.tf``."""

    def test_backend_and_nginx_are_not_given_the_name(self):
        out = _rewrite(_last_good_task_def())
        assert ENV_NAME not in _env(out, "backend")
        assert ENV_NAME not in _env(out, "nginx")

    def test_the_pin_function_is_applied_to_the_auth_container_only(self):
        src = REWRITE_PY.read_text(encoding="utf-8")
        body = src[src.index("def rewrite_registered_task_definition(") :]
        body = body[: body.index("\ndef main(")]
        calls = [m.start() for m in re.finditer(r"pin_auth_ses_configuration_set\(", body)]
        assert len(calls) == 1
        auth_branch = body.index("elif name == AUTH_CONTAINER:")
        nxt = body.index("rewritten.append(next_container)", auth_branch)
        assert auth_branch < calls[0] < nxt


class TestTheValueIsTerraformsName:
    """One name, three files: the constant here, the resource in ses_events.tf,
    the reader in mailer.js. Structural, so a rename in any one goes red."""

    def test_the_pinned_value_is_the_configuration_sets_terraform_name(self):
        mod = _load_rewrite()
        hcl = SES_EVENTS_TF.read_text(encoding="utf-8")
        block = re.search(r'resource "aws_sesv2_configuration_set" "mail" \{(.*?)\n\}', hcl, re.S)
        assert block is not None, "aws_sesv2_configuration_set.mail is gone from infra/ses_events.tf"
        name_expr = re.search(r'configuration_set_name\s*=\s*"([^"]+)"', block.group(1))
        assert name_expr is not None
        project = re.search(
            r'variable "project_name" \{.*?default\s*=\s*"([^"]+)"', VARIABLES_TF.read_text(encoding="utf-8"), re.S
        )
        assert project is not None
        expected = name_expr.group(1).replace("${var.project_name}", project.group(1))
        assert "${" not in expected, f"unresolved interpolation in {name_expr.group(1)!r}"
        assert expected == mod.SES_CONFIGURATION_SET_VALUE

    def test_the_mailer_reads_exactly_this_env_name(self):
        mod = _load_rewrite()
        js = MAILER_JS.read_text(encoding="utf-8")
        assert re.search(rf"env\.{re.escape(mod.SES_CONFIGURATION_SET_NAME)}\b", js), (
            "auth/mailer.js no longer reads the env name the pipeline pins"
        )

    def test_the_value_is_not_blank(self):
        # A blank is the documented pull-back, not a default: shipping it by
        # accident would make the loop deaf with every test here still green.
        mod = _load_rewrite()
        assert mod.SES_CONFIGURATION_SET_VALUE.strip()

    def test_the_pin_is_not_on_the_retired_list(self):
        mod = _load_rewrite()
        assert mod.SES_CONFIGURATION_SET_NAME not in mod.RETIRED_BACKEND_ENV


class TestTheCliPath:
    def test_cli_writes_the_pin(self, tmp_path):
        mod = _load_rewrite()
        src = tmp_path / "td.json"
        src.write_text(json.dumps(_last_good_task_def()), encoding="utf-8")
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.main(
                [
                    str(src),
                    "--backend-image",
                    BACKEND_IMAGE,
                    "--nginx-image",
                    NGINX_IMAGE,
                    "--auth-image",
                    AUTH_IMAGE,
                ]
            )
        assert rc == 0
        out = json.loads(buf.getvalue())
        assert _env(out, "auth")[ENV_NAME] == mod.SES_CONFIGURATION_SET_VALUE
