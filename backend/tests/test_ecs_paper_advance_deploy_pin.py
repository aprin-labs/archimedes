"""CI deploy must pin ``PAPER_ADVANCE_ENABLED`` explicitly on the cloned task-def.

#1725 put the pin in ``infra/ecs.tf``. That is not the path that ships:
``deploy.yml`` clones the currently registered task definition and retags
images. It does not apply terraform, so an ecs.tf pin is not live until
somebody applies, and a clone that never heard of the name ships whatever the
code default happens to be — which is how task-def :211 ticked itself to death.

The invariant these tests hold is therefore direction-independent: the value
that ships is the one ``ecs_rewrite_task_def.PAPER_ADVANCE_VALUE`` states, on
every deploy, no matter what the clone carried. That value is ``"true"`` as of
2026-09-01 (#1778, the #1632 lift): the tick is armed and runs in an isolated
child (#1728). Flipping it back means editing that constant and these tests
together — the point of hard-coding it is that turning the tick on or off
cannot happen by accident or by CLI flag.

These tests run the same function ``deploy.yml`` invokes. They are hermetic —
no AWS, no terraform, no network. Every assertion is paired with the mutation
it would catch.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REWRITE_PY = REPO_ROOT / ".github" / "scripts" / "ecs_rewrite_task_def.py"
DEPLOY_YML = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

BACKEND_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-backend:deadbeef"
NGINX_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-nginx:deadbeef"
AUTH_IMAGE = "037613907429.dkr.ecr.us-east-1.amazonaws.com/archimedes-auth:deadbeef"


def _load_rewrite():
    spec = importlib.util.spec_from_file_location("ecs_rewrite_task_def", REWRITE_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _last_good_task_def(*, paper_advance: str | None = None) -> dict:
    """Shape of a cloned live revision that predates the #1725 terraform pin.

    ``paper_advance is None`` is last-good ``011b6bfc``: the name is absent, so
    the container falls through to whatever the code default is — ON when :211
    died, OFF today, and in neither case a decision anybody made for that
    revision. That is precisely what the rewrite exists to stop.
    """
    env = [
        {"name": "APP_ENV", "value": "production"},
        {"name": "PAPER_TRADING", "value": "true"},
    ]
    if paper_advance is not None:
        env.append({"name": "PAPER_ADVANCE_ENABLED", "value": paper_advance})
    return {
        "family": "archimedes-backend",
        "taskDefinitionArn": "arn:aws:ecs:us-east-1:037613907429:task-definition/archimedes-backend:177",
        "revision": 177,
        "status": "ACTIVE",
        "requiresAttributes": [{"name": "ecs.capability.execution-role-ecr-pull"}],
        "compatibilities": ["FARGATE"],
        "registeredAt": "2026-09-01T12:00:00Z",
        "registeredBy": "arn:aws:sts::037613907429:assumed-role/archimedes-github-deploy/x",
        "cpu": "1024",
        "memory": "3072",
        "containerDefinitions": [
            {"name": "backend", "image": "old-backend:011b6bfc", "environment": env},
            {"name": "nginx", "image": "old-nginx:011b6bfc", "environment": [{"name": "FOO", "value": "1"}]},
            {"name": "auth", "image": "old-auth:011b6bfc"},
        ],
    }


def _rewrite(task_def: dict) -> dict:
    mod = _load_rewrite()
    return mod.rewrite_registered_task_definition(
        task_def,
        backend_image=BACKEND_IMAGE,
        nginx_image=NGINX_IMAGE,
        auth_image=AUTH_IMAGE,
    )


def _backend_env(task_def: dict) -> dict[str, str]:
    backend = next(c for c in task_def["containerDefinitions"] if c["name"] == "backend")
    return {e["name"]: e["value"] for e in backend.get("environment") or []}


def _deploy_ecs_job() -> str:
    text = DEPLOY_YML.read_text(encoding="utf-8")
    match = re.search(r"^  deploy-ecs:.*?(?=^  [a-z0-9-]+:|\Z)", text, re.M | re.S)
    assert match, "deploy.yml no longer has a deploy-ecs job"
    return match.group(0)


class TestRewritePinsTheKillSwitch:
    def test_last_good_clone_without_the_name_ships_the_pinned_value(self):
        """The incident shape: live last-good never heard of the flag."""
        out = _rewrite(_last_good_task_def(paper_advance=None))
        env = _backend_env(out)
        assert env["PAPER_ADVANCE_ENABLED"] == "true"
        assert env["APP_ENV"] == "production"
        assert env["PAPER_TRADING"] == "true"

    def test_an_existing_false_is_overwritten_not_duplicated(self):
        """The exact production input on the first deploy after the lift.

        The live task-def family says ``false`` today, so this — not the
        already-``true`` case below — is the shape the very next CI deploy
        rewrites. If the rewrite appended instead of replacing, ECS would take
        the LAST entry and the flip would look like it worked for the wrong
        reason; if it left the clone alone, the flip would not ship at all.
        """
        out = _rewrite(_last_good_task_def(paper_advance="false"))
        env = _backend_env(out)
        assert env["PAPER_ADVANCE_ENABLED"] == "true"
        names = [
            e["name"] for e in next(c for c in out["containerDefinitions"] if c["name"] == "backend")["environment"]
        ]
        assert names.count("PAPER_ADVANCE_ENABLED") == 1

    def test_an_existing_true_is_not_duplicated(self):
        """Every deploy after the first: the clone already carries the pin."""
        out = _rewrite(_last_good_task_def(paper_advance="true"))
        names = [
            e["name"] for e in next(c for c in out["containerDefinitions"] if c["name"] == "backend")["environment"]
        ]
        assert names.count("PAPER_ADVANCE_ENABLED") == 1
        assert _backend_env(out)["PAPER_ADVANCE_ENABLED"] == "true"

    def test_nginx_and_auth_are_not_given_the_flag(self):
        out = _rewrite(_last_good_task_def())
        nginx = next(c for c in out["containerDefinitions"] if c["name"] == "nginx")
        auth = next(c for c in out["containerDefinitions"] if c["name"] == "auth")
        nginx_names = {e["name"] for e in nginx.get("environment") or []}
        assert "PAPER_ADVANCE_ENABLED" not in nginx_names
        assert nginx["environment"] == [{"name": "FOO", "value": "1"}]
        assert "environment" not in auth or not any(
            e.get("name") == "PAPER_ADVANCE_ENABLED" for e in auth.get("environment") or []
        )

    def test_images_are_retagged_and_describe_fields_are_dropped(self):
        out = _rewrite(_last_good_task_def())
        by_name = {c["name"]: c["image"] for c in out["containerDefinitions"]}
        assert by_name == {"backend": BACKEND_IMAGE, "nginx": NGINX_IMAGE, "auth": AUTH_IMAGE}
        for field in (
            "taskDefinitionArn",
            "revision",
            "status",
            "requiresAttributes",
            "compatibilities",
            "registeredAt",
            "registeredBy",
        ):
            assert field not in out, f"{field} must be dropped before register-task-definition"
        assert out["cpu"] == "1024"
        assert out["memory"] == "3072"
        assert out["family"] == "archimedes-backend"

    def test_missing_backend_container_is_a_hard_error(self):
        mod = _load_rewrite()
        task_def = _last_good_task_def()
        task_def["containerDefinitions"] = [c for c in task_def["containerDefinitions"] if c["name"] != "backend"]
        with pytest.raises(mod.RewriteError, match="backend"):
            mod.rewrite_registered_task_definition(
                task_def,
                backend_image=BACKEND_IMAGE,
                nginx_image=NGINX_IMAGE,
                auth_image=AUTH_IMAGE,
            )

    def test_empty_container_definitions_is_a_hard_error(self):
        mod = _load_rewrite()
        with pytest.raises(mod.RewriteError, match="containerDefinitions"):
            mod.rewrite_registered_task_definition(
                {"containerDefinitions": []},
                backend_image=BACKEND_IMAGE,
                nginx_image=NGINX_IMAGE,
                auth_image=AUTH_IMAGE,
            )

    def test_cli_writes_the_pinned_json(self, tmp_path):
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
        assert _backend_env(out)["PAPER_ADVANCE_ENABLED"] == "true"
        backend = next(c for c in out["containerDefinitions"] if c["name"] == "backend")
        assert backend["healthCheck"]["startPeriod"] == 90


class TestRewritePinsBackendStartPeriod:
    """CI clone of the live task-def still carries startPeriod=30.

    The #1713 warmup budget is 60s. Without this pin, ECS starts counting
    /health/ready failures at 30s while uvicorn is still not listening — or, if
    warmup fail-softs, the task joins the ALB cold. Same path as the
    PAPER_ADVANCE pin: deploy.yml never terraform-applies. This PR does not
    flip PAPER_ADVANCE; the assertions below read the live pin value.
    """

    def test_last_good_clone_without_healthcheck_gets_start_period_90(self):
        """Incident-adjacent shape: last-good 011b6bfc has no healthCheck."""
        out = _rewrite(_last_good_task_def())
        backend = next(c for c in out["containerDefinitions"] if c["name"] == "backend")
        hc = backend["healthCheck"]
        assert hc["startPeriod"] == 90
        assert hc["command"][0] == "CMD-SHELL"
        assert "/health/ready" in hc["command"][1]
        assert _backend_env(out)["PAPER_ADVANCE_ENABLED"] == _load_rewrite().PAPER_ADVANCE_VALUE

    def test_live_start_period_30_is_overwritten_command_becomes_readiness(self):
        """The live cloned shape: healthCheck exists, startPeriod is 30.

        Command is rewritten to /health/ready (#1818 P3). startPeriod is
        pinned to 90 (#1713). interval / timeout / retries stay from the clone.
        """
        task_def = _last_good_task_def()
        custom_cmd = [
            "CMD-SHELL",
            "python -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:8000/health\")' || exit 1",
        ]
        for container in task_def["containerDefinitions"]:
            if container["name"] == "backend":
                container["healthCheck"] = {
                    "command": custom_cmd,
                    "interval": 30,
                    "timeout": 5,
                    "retries": 3,
                    "startPeriod": 30,
                }
        out = _rewrite(task_def)
        backend = next(c for c in out["containerDefinitions"] if c["name"] == "backend")
        hc = backend["healthCheck"]
        mod = _load_rewrite()
        assert hc["startPeriod"] == 90
        assert hc["command"] == list(mod.READINESS_HEALTH_CHECK_COMMAND)
        assert "/health/ready" in hc["command"][1]
        assert hc["interval"] == 30
        assert hc["timeout"] == 5
        assert hc["retries"] == 3
        nginx = next(c for c in out["containerDefinitions"] if c["name"] == "nginx")
        assert "healthCheck" not in nginx

    def test_warmup_budget_fits_inside_the_pinned_start_period(self):
        """Source pin: budget 60 vs startPeriod 30 is the review finding.

        MUTATION: raise WARMUP_BUDGET_SECONDS above the rewrite pin, or
        lower BACKEND_HEALTHCHECK_START_PERIOD below 60. Either way this
        fails. The rewrite script must not import archimedes (deploy.yml
        python3, stdlib only); the coupling is asserted from the test.
        """
        from archimedes.services.request_path_warmup import WARMUP_BUDGET_SECONDS

        mod = _load_rewrite()
        assert mod.BACKEND_HEALTHCHECK_START_PERIOD >= WARMUP_BUDGET_SECONDS
        assert mod.BACKEND_HEALTHCHECK_START_PERIOD == 90
        source = REWRITE_PY.read_text(encoding="utf-8")
        assert "BACKEND_HEALTHCHECK_START_PERIOD = 90" in source
        # This PR does not flip paper-advance; the live pin stays whatever
        # main currently ships.
        assert f'PAPER_ADVANCE_VALUE = "{mod.PAPER_ADVANCE_VALUE}"' in source

    def test_nginx_and_auth_healthchecks_are_not_rewritten(self):
        task_def = _last_good_task_def()
        for container in task_def["containerDefinitions"]:
            if container["name"] == "nginx":
                container["healthCheck"] = {"command": ["CMD-SHELL", "true"], "startPeriod": 15}
        out = _rewrite(task_def)
        nginx = next(c for c in out["containerDefinitions"] if c["name"] == "nginx")
        assert nginx["healthCheck"]["startPeriod"] == 15
        auth = next(c for c in out["containerDefinitions"] if c["name"] == "auth")
        assert "healthCheck" not in auth


class TestDeployYmlIsThePathThatShips:
    def test_deploy_ecs_checks_out_the_repo_and_invokes_the_rewrite_script(self):
        job = _deploy_ecs_job()
        assert "actions/checkout@v7" in job, (
            "deploy-ecs has no checkout — the rewrite script is in the tree and "
            "cannot run unless this job checks it out"
        )
        assert "ecs_rewrite_task_def.py" in job, (
            "deploy-ecs no longer invokes ecs_rewrite_task_def.py — that is the "
            "path that actually ships PAPER_ADVANCE_ENABLED"
        )
        # The previous inline jq only swapped images. A revert to that shape
        # is the outage still running on last-good 011b6bfc.
        assert 'if .name == "backend" then .image' not in job

    def test_rewrite_script_hard_codes_the_value_not_a_parameter(self):
        """Turning the tick on or off cannot be a quietly-defaulted CLI flag.

        The value that ships has to be the literal in the module, so editing
        that constant — and this test with it — is the only way to move the
        tick in either direction via CI. A ``--paper-advance`` argument with a
        default would make the deployed value a property of whoever wrote the
        workflow line, which is how a pin becomes untraceable.
        """
        source = REWRITE_PY.read_text(encoding="utf-8")
        assert 'PAPER_ADVANCE_VALUE = "true"' in source
        assert "#1632" in source
        mod = _load_rewrite()
        assert mod.PAPER_ADVANCE_VALUE == "true"
        # The name must stay out of argparse: an option here is an option
        # someone can forget to pass, and its default would quietly decide
        # production. The CLI takes images and a file, nothing else.
        assert 'add_argument("--paper' not in source
        assert "PAPER_ADVANCE_VALUE" not in source.split("def main(", 1)[1]


class TestRewriteStripsRetiredBackendEnv:
    """The deploy must delete retired env names, not carry them forever.

    #1766 retired ``services/backtest_scheduler.py``. The owner's mitigation
    pin ``BACKTEST_REFRESH_ENABLED=false`` (hand-set as revision 216 during
    the 2026-09-01 #1760 storm) now names a flag nothing reads, and the deploy
    clones the live task-def forward, so it rides along on every revision
    until something takes it out. Cleanups ship with the deploy — no operator
    ritual — so the first deploy after this merge registers a revision without
    them.

    These tests say nothing about which way ``PAPER_ADVANCE_ENABLED`` points:
    the strip is orthogonal to the tick, and every assertion about the tick
    here reads :data:`ecs_rewrite_task_def.PAPER_ADVANCE_VALUE` so that a
    future flip moves one constant, not this class.
    """

    def test_the_live_pin_is_stripped_and_nothing_else_moves(self):
        """A clone carrying the pin comes out identical minus that entry.

        Mutation this catches: delete the ``strip_retired_backend_env`` call
        from ``rewrite_registered_task_definition`` and the retired name is
        still in the registered revision.
        """
        clean = _last_good_task_def()
        pinned = _last_good_task_def()
        backend = next(c for c in pinned["containerDefinitions"] if c["name"] == "backend")
        # Insert mid-list, as revision 216 did: stripping must not reorder.
        backend["environment"].insert(1, {"name": "BACKTEST_REFRESH_ENABLED", "value": "false"})

        out_pinned = _rewrite(pinned)
        out_clean = _rewrite(clean)

        assert "BACKTEST_REFRESH_ENABLED" not in _backend_env(out_pinned)
        assert json.dumps(out_pinned, sort_keys=True) == json.dumps(out_clean, sort_keys=True), (
            "stripping the retired pin changed something other than that entry"
        )

    def test_every_retired_name_is_stripped(self):
        mod = _load_rewrite()
        task_def = _last_good_task_def()
        backend = next(c for c in task_def["containerDefinitions"] if c["name"] == "backend")
        for name in mod.RETIRED_BACKEND_ENV:
            backend["environment"].append({"name": name, "value": "whatever"})

        env = _backend_env(_rewrite(task_def))
        assert not (set(mod.RETIRED_BACKEND_ENV) & set(env)), env
        assert env["APP_ENV"] == "production"
        assert env["PAPER_TRADING"] == "true"
        # The strip runs after the pins, so it must not have eaten one.
        assert env["PAPER_ADVANCE_ENABLED"] == mod.PAPER_ADVANCE_VALUE

    def test_no_pinned_name_is_also_retired(self):
        """The strip runs LAST, so a name on both lists would lose its pin.

        ``rewrite_registered_task_definition`` applies ``strip_retired_backend_env``
        to the already-pinned environment on purpose: the retired tuple is the
        final word on what the backend container may carry. That ordering is
        only safe while the two lists are disjoint — otherwise this file would
        pin a value and then silently delete it, and the deploy log would say
        it dropped a name the same file had just asked for.
        """
        mod = _load_rewrite()
        pinned_names = {
            mod.PAPER_ADVANCE_NAME,
            mod.FREE_GENERATIONS_NAME,
            mod.HEALTH_STALE_UNREADY_NAME,
        }
        overlap = pinned_names & set(mod.RETIRED_BACKEND_ENV)
        assert not overlap, (
            f"{sorted(overlap)} is both pinned and retired — the strip runs last, "
            "so the pin would never reach the registered revision. Take the name "
            "off RETIRED_BACKEND_ENV or stop pinning it."
        )

    def test_stripping_is_idempotent(self, capsys):
        """The deploy after the first one has nothing left to remove."""
        source = _last_good_task_def(paper_advance="true")
        backend = next(c for c in source["containerDefinitions"] if c["name"] == "backend")
        backend["environment"].append({"name": "BACKTEST_REFRESH_ENABLED", "value": "false"})

        once = _rewrite(source)
        assert "BACKTEST_REFRESH_ENABLED" in capsys.readouterr().err

        # Feed the registered revision back the way the next deploy clones it:
        # ``describe-task-definition`` returns the describe-only fields this
        # script drops, and ``taskDefinitionArn`` is the one it reads back.
        clone = json.loads(json.dumps(once))
        clone["taskDefinitionArn"] = source["taskDefinitionArn"]
        twice = _rewrite(clone)

        assert "BACKTEST_REFRESH" not in capsys.readouterr().err
        assert _backend_env(twice) == _backend_env(once)

    def test_it_says_what_it_removed(self, capsys):
        task_def = _last_good_task_def()
        backend = next(c for c in task_def["containerDefinitions"] if c["name"] == "backend")
        backend["environment"].append({"name": "BACKTEST_REFRESH_ENABLED", "value": "false"})
        _rewrite(task_def)
        assert "BACKTEST_REFRESH_ENABLED" in capsys.readouterr().err

    def test_a_clone_without_them_logs_nothing(self, capsys):
        _rewrite(_last_good_task_def())
        assert "BACKTEST_REFRESH" not in capsys.readouterr().err

    def test_other_containers_keep_the_names(self):
        """Only the backend container is rewritten; nginx/auth are untouched."""
        task_def = _last_good_task_def()
        nginx = next(c for c in task_def["containerDefinitions"] if c["name"] == "nginx")
        nginx["environment"].append({"name": "BACKTEST_REFRESH_ENABLED", "value": "false"})
        out_nginx = next(c for c in _rewrite(task_def)["containerDefinitions"] if c["name"] == "nginx")
        assert {e["name"] for e in out_nginx["environment"]} == {"FOO", "BACKTEST_REFRESH_ENABLED"}

    def test_no_retired_name_has_a_reader_left_in_the_backend(self):
        """Re-adding a reader without un-retiring the name must go red.

        The deploy now *deletes* these names from the shipped task
        definition. If someone lands ``os.getenv("BACKTEST_MAX_AGE_HOURS")``
        under ``backend/archimedes`` while the name is still on
        ``RETIRED_BACKEND_ENV``, their flag is silently unsettable in
        production. Either the reader goes or the name comes off the tuple.

        Scans committed sources rather than importing: a reader added to a
        module the test suite never imports still trips this.
        """
        mod = _load_rewrite()
        package_root = REPO_ROOT / "backend" / "archimedes"
        assert package_root.is_dir(), package_root

        offenders: list[str] = []
        scanned = 0
        control_hits = 0
        for path in sorted(package_root.rglob("*.py")):
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
            # String constants only: comments are stripped by the parser, so a
            # tombstone comment naming the retired knob does not trip this,
            # but any getenv/os.environ lookup (whose key is a string literal)
            # does.
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in mod.RETIRED_BACKEND_ENV:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {node.value}")
                if isinstance(node, ast.Constant) and node.value == mod.PAPER_ADVANCE_NAME:
                    control_hits += 1

        # Not vacuous: an empty or broken walk (a glob typo, a moved package)
        # would leave ``offenders`` empty and pass with the guard disarmed. The
        # package has 200+ modules and exactly one string constant naming the
        # live kill switch; both sentinels must be seen for the verdict to count.
        assert scanned >= 20, f"walk saw only {scanned} modules under {package_root}"
        assert control_hits == 1, (
            f"expected exactly one {mod.PAPER_ADVANCE_NAME!r} string constant under "
            f"backend/archimedes, saw {control_hits}"
        )

        assert not offenders, (
            "retired env names are read again under backend/archimedes: "
            f"{offenders}. The CI deploy strips every name in "
            "RETIRED_BACKEND_ENV from the backend container, so this reader "
            "can never be set in production. Take the name off "
            "RETIRED_BACKEND_ENV (and re-add it to the task definition) or "
            "delete the reader. Policy: docs/adr/backtests-are-frozen-evidence.md."
        )
