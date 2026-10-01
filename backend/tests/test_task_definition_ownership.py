"""The backend task definition has exactly one registrar per attribute (#1799).

Terraform's state held ``archimedes-backend`` revision 213 while the deploy
pipeline had walked the live family to 233, because ``deploy.yml`` clones the
family's LATEST revision on every merge. Every attribute of
``aws_ecs_task_definition`` is force-new, so an edit to ``container_definitions``
in ``infra/ecs.tf`` made a plain ``terraform plan`` say ``must be replaced`` — and
an untargeted apply then registered a revision from that file, which the NEXT
merge cloned forward, silently rolling twenty revisions of accumulated container
state (every commit-SHA image tag) back into production.

The owner's 2026-09-03 ruling is that the PIPELINE owns container settings, so
``ecs.tf`` ignores ``container_definitions`` — and nothing else. These tests hold
the three halves of that:

1. The ignore list is exactly ``[container_definitions]``. Widening it to
   ``cpu``/``memory``/``volume`` would hide drift on attributes NOTHING else
   writes, which is strictly worse than the bug: real drift would go silent.
2. ``ecs_rewrite_task_def.py`` — the thing that actually registers revisions —
   still writes only inside ``containerDefinitions``. The moment it starts
   pinning a top-level field, the ignore list above is wrong and the two
   registrars are fighting again somewhere new. This is a behavioural check
   against the real function, not a grep for a comment.
3. ``aws_ecs_service.backend`` keeps ``ignore_changes = [task_definition,
   desired_count]``. That predates #1799 and is what keeps an apply from
   yanking the running service onto a Terraform-built revision.

Plus a guard on ``terraform-drift.yml``, the workflow that would have caught
#1799 four weeks earlier, and unit tests for its verdict script.

Hermetic: reads files from the repo, imports one stdlib-only script. No AWS, no
terraform binary, no network.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ECS_TF = REPO_ROOT / "infra" / "ecs.tf"
REWRITE_PY = REPO_ROOT / ".github" / "scripts" / "ecs_rewrite_task_def.py"
DRIFT_GATE_PY = REPO_ROOT / ".github" / "scripts" / "tf_drift_gate.py"
DRIFT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "terraform-drift.yml"
INFRA_GATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "infra-gate.yml"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "terraform-apply-and-task-definition-ownership.md"
PLAN_ROLE_SH = REPO_ROOT / "infra" / "scripts" / "setup-github-plan-role.sh"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resource_block(source: str, kind: str, name: str) -> str:
    """Slice one top-level ``resource "kind" "name" { ... }`` out of a .tf file.

    Terraform formats top-level resources with the closing brace in column 0
    (``terraform fmt`` guarantees it, and infra-gate.yml blocks on ``fmt
    -check``), so the block runs to the first line that is exactly ``}``.
    """
    opener = f'resource "{kind}" "{name}" {{'
    start = source.find(opener)
    assert start != -1, f"no {opener!r} in the source"
    end = source.find("\n}\n", start)
    assert end != -1, f"unterminated {opener!r}"
    return source[start : end + 2]


def _without_comments(source: str) -> str:
    """Drop whole-line ``#`` comments.

    Not cosmetic: ``infra/ecs.tf`` discusses ``ignore_changes = [task_definition]``
    and ``ignore_changes = [ami, user_data]`` in prose, several hundred lines
    above the real ``lifecycle`` blocks. A regex over the raw text reads the
    documentation instead of the code and passes on a file that has neither.
    """
    return "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))


def _ignore_changes(block: str) -> list[str]:
    """The identifiers in this block's ``lifecycle { ignore_changes = [...] }``."""
    match = re.search(r"ignore_changes\s*=\s*\[([^\]]*)\]", _without_comments(block))
    assert match is not None, "no ignore_changes list in the block"
    return [item.strip() for item in match.group(1).split(",") if item.strip()]


def _plan_command(workflow: str) -> str:
    """The `terraform ... plan ...` invocation itself, flags and all."""
    executable = _without_comments(workflow)
    start = executable.find("terraform -chdir=infra plan")
    assert start != -1, "the drift workflow no longer runs `terraform -chdir=infra plan`"
    end = executable.find("\n", executable.find("|", start))
    return executable[start : end if end != -1 else len(executable)]


def _step(workflow: str, name_fragment: str) -> str:
    """One ``- name: ...`` step block, up to the next step at the same indent."""
    start = workflow.find(f"- name: {name_fragment}")
    assert start != -1, f"the drift workflow has no step named {name_fragment!r}"
    rest = workflow[start:]
    end = rest.find("\n      - ")
    return rest if end == -1 else rest[:end]


def _concurrency(workflow: str) -> dict[str, str]:
    """The workflow-level ``concurrency:`` mapping, values kept as raw strings."""
    executable = _without_comments(workflow)
    marker = "\nconcurrency:\n"
    start = executable.find(marker)
    assert start != -1, "the drift workflow lost its concurrency block"
    out: dict[str, str] = {}
    for line in executable[start + len(marker) :].splitlines():
        if not line.startswith("  "):
            break
        key, _, value = line.strip().partition(":")
        out[key.strip()] = value.strip()
    return out


def _group_for(workflow: str, event_name: str) -> str:
    """Render the concurrency group key GitHub would compute for one trigger.

    Substitutes the two contexts that actually vary. ``push`` and ``schedule``
    both run on ``refs/heads/main``, which is the whole point: if the group
    expression does not distinguish them, they collide.
    """
    group = _concurrency(workflow)["group"]
    ref = "7" if event_name == "pull_request" else "refs/heads/main"
    return (
        group.replace("${{ github.event_name }}", event_name)
        .replace("${{ github.event.pull_request.number || github.ref }}", ref)
        .replace("${{ github.workflow }}", "terraform-drift")
    )


def _plan_role_policy() -> dict[str, Any]:
    """The inline IAM policy ``setup-github-plan-role.sh`` writes, parsed.

    Reads the real heredoc rather than grepping for statement names, so a test
    below can compare a ``Deny``'s ``NotResource`` against the matching
    ``Allow``'s ``Resource``. Shell interpolations become ``<NAME>`` sentinels;
    the same variable renders to the same sentinel on both sides, so the
    comparison holds exactly as it would after substitution.
    """
    source = PLAN_ROLE_SH.read_text(encoding="utf-8")
    anchor = source.find('cat > "$TMP/perms.json" <<JSON')
    assert anchor != -1, "setup-github-plan-role.sh no longer writes an inline policy heredoc"
    start = source.index("<<JSON\n", anchor) + len("<<JSON\n")
    end = source.index("\nJSON\n", start)
    return json.loads(re.sub(r"\$\{(\w+)\}", r"<\1>", source[start:end]))


def _as_list(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


@pytest.fixture(scope="module")
def ecs_tf() -> str:
    assert ECS_TF.is_file(), f"missing {ECS_TF}"
    return ECS_TF.read_text(encoding="utf-8")


class TestTaskDefinitionIgnoresExactlyContainerDefinitions:
    def test_the_task_definition_has_a_lifecycle_block(self, ecs_tf: str) -> None:
        block = _resource_block(ecs_tf, "aws_ecs_task_definition", "backend")
        assert "lifecycle {" in block, (
            "aws_ecs_task_definition.backend lost its lifecycle block — every untargeted "
            "`terraform apply` will register a revision from ecs.tf again, and the next "
            "merge will clone it forward into production (#1799)."
        )

    def test_it_ignores_container_definitions(self, ecs_tf: str) -> None:
        block = _resource_block(ecs_tf, "aws_ecs_task_definition", "backend")
        assert "container_definitions" in _ignore_changes(block)

    def test_it_ignores_nothing_else(self, ecs_tf: str) -> None:
        """The mutation this catches: someone silencing a real diff.

        ``cpu``, ``memory``, the IAM roles, ``runtime_platform`` and the
        corpus-artifact ``volume`` are copied forward by the pipeline's clone
        and authored by nobody but Terraform. Adding one here would turn a
        legitimate "you have unapplied infrastructure" plan line into silence.
        """
        block = _resource_block(ecs_tf, "aws_ecs_task_definition", "backend")
        assert _ignore_changes(block) == ["container_definitions"], (
            "aws_ecs_task_definition.backend must ignore container_definitions and nothing "
            "else — every other attribute has exactly one writer (terraform), so ignoring "
            "it hides drift instead of resolving an ownership conflict."
        )

    def test_the_service_still_ignores_task_definition_and_desired_count(self, ecs_tf: str) -> None:
        block = _resource_block(ecs_tf, "aws_ecs_service", "backend")
        assert _ignore_changes(block) == ["task_definition", "desired_count"], (
            "aws_ecs_service.backend's pre-existing ignore_changes is load-bearing: without "
            "it an apply pulls the live service onto whatever revision terraform just "
            "registered, and the CPU autoscaler's desired_count is reverted on every apply."
        )


class TestThePipelineWritesOnlyContainerDefinitions:
    """The ignore list is only correct while this stays true."""

    @staticmethod
    def _task_def() -> dict[str, Any]:
        """A registered revision, shaped like `describe-task-definition` returns."""
        return {
            "family": "archimedes-backend",
            "taskDefinitionArn": "arn:aws:ecs:us-east-1:037613907429:task-definition/archimedes-backend:233",
            "revision": 233,
            "status": "ACTIVE",
            "cpu": "1024",
            "memory": "3072",
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "executionRoleArn": "arn:aws:iam::037613907429:role/archimedes-ecs-task-execution",
            "taskRoleArn": "arn:aws:iam::037613907429:role/archimedes-ecs-task",
            "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "X86_64"},
            "volumes": [{"name": "corpus-artifact"}],
            "containerDefinitions": [
                {"name": "backend", "image": "old-backend", "environment": []},
                {"name": "nginx", "image": "old-nginx"},
                {"name": "auth", "image": "old-auth"},
            ],
        }

    def test_no_top_level_field_is_rewritten(self) -> None:
        mod = _load(REWRITE_PY, "ecs_rewrite_task_def")
        original = self._task_def()
        out = mod.rewrite_registered_task_definition(
            copy.deepcopy(original),
            backend_image="new-backend",
            nginx_image="new-nginx",
            auth_image="new-auth",
        )

        dropped = set(mod._DROP_FIELDS)
        for key, value in original.items():
            if key in dropped or key == "containerDefinitions":
                continue
            assert out[key] == value, (
                f"ecs_rewrite_task_def now rewrites the top-level field {key!r}. "
                "infra/ecs.tf's ignore_changes covers container_definitions ONLY, so this "
                "field now has two registrars and #1799 has reopened on it. Either stop "
                "writing it in the pipeline, or extend the ignore list and this test "
                "together."
            )

    def test_it_does_rewrite_the_containers(self) -> None:
        """Paired with the assertion above: prove the check is not vacuous."""
        mod = _load(REWRITE_PY, "ecs_rewrite_task_def")
        out = mod.rewrite_registered_task_definition(
            self._task_def(),
            backend_image="new-backend",
            nginx_image="new-nginx",
            auth_image="new-auth",
        )
        images = {c["name"]: c["image"] for c in out["containerDefinitions"]}
        assert images == {
            "backend": "new-backend",
            "nginx": "new-nginx",
            "auth": "new-auth",
        }


class TestDriftWorkflow:
    @pytest.fixture(scope="class")
    def workflow(self) -> str:
        assert DRIFT_WORKFLOW.is_file(), f"missing {DRIFT_WORKFLOW}"
        return DRIFT_WORKFLOW.read_text(encoding="utf-8")

    def test_it_watches_infra_and_runs_on_a_schedule(self, workflow: str) -> None:
        assert '"infra/**"' in workflow, (
            "terraform-drift.yml stopped watching infra/** — it would stay green by never "
            "running, the silent-downgrade shape infra-gate.yml's own guard exists for."
        )
        assert re.search(r"^\s*schedule:", workflow, re.MULTILINE), (
            "drift is mostly not caused by commits (the pipeline, console edits, AWS "
            "defaults). Without the schedule this gate cannot see any of that."
        )
        assert re.search(r"^\s*- cron:", workflow, re.MULTILINE)

    def test_it_uses_detailed_exitcode_and_never_locks_state(self, workflow: str) -> None:
        """Both flags must be on the COMMAND, not merely discussed.

        Two mutations caught this test being wrong while it was written: the
        header prose names both flags (so a comment-blind check passes on a
        workflow whose plan step has neither), and the step is literally
        ``name: terraform plan (-detailed-exitcode)`` (so a comment-stripped
        check still reads the label). Hence the window on the command itself.
        """
        command = _plan_command(workflow)
        assert "-detailed-exitcode" in command, (
            "without it the step cannot distinguish 'no changes' from 'changes' and the gate never classifies anything."
        )
        assert "-lock=false" in command, (
            "the plan role is granted no s3:Put*/s3:Delete* on the state bucket "
            "(infra/scripts/setup-github-plan-role.sh); acquiring the S3-native lock would "
            "fail, and granting the write would make a read-only role writable."
        )

    def test_it_never_uploads_the_planfile_or_the_json(self, workflow: str) -> None:
        """A `-out` planfile embeds the full prior state, unredacted.

        The state holds ``tls_private_key.deploy``'s private key, so uploading
        the binary (or the `show -json` rendering) as a build artifact would
        hand it to anyone with repo read access — the exact trade
        ``infra-gate.yml`` refuses to make for syntax checking.
        """
        upload = workflow.find("upload-artifact")
        assert upload != -1, "the workflow no longer uploads the plan at all"
        # Only this step's own `with:` block: everything up to the next step.
        rest = workflow[upload:]
        end = rest.find("\n      - ")
        window = _without_comments(rest if end == -1 else rest[:end])
        assert "tfplan.binary" not in window, "the binary planfile must never be uploaded"
        assert "plan.json" not in window, "the JSON plan rendering must never be uploaded"
        assert "plan.txt" in window

    def test_it_is_gated_off_until_the_operator_arms_it(self, workflow: str) -> None:
        assert "vars.TF_DRIFT_ENABLED == 'true'" in workflow, (
            "without the self-activating gate this job is red on every infra PR from merge "
            "until the plan role exists, which teaches everyone to ignore it."
        )

    def test_no_credential_hardcoded(self, workflow: str) -> None:
        assert "TF_PLAN_ROLE_ARN" in workflow
        assert "secrets.TF_VAR_ALARM_EMAIL" in workflow
        # The header documents the expected ARN in prose, which is useful; what
        # must not happen is a step actually using a literal one.
        executable = _without_comments(workflow)
        assert not re.search(r"arn:aws:iam::\d+:role/", executable), (
            "the role ARN belongs in the TF_PLAN_ROLE_ARN repository variable, not in a "
            "workflow step — a fork or a re-created role should not need a code change."
        )

    def test_infra_gate_stays_credential_free(self) -> None:
        """#1799 must not have quietly turned the cheap gate into an expensive one."""
        source = INFRA_GATE_WORKFLOW.read_text(encoding="utf-8")
        assert "configure-aws-credentials" not in source
        assert "-backend=false" in source

    def test_the_runbook_exists_and_ecs_tf_points_at_it(self, ecs_tf: str) -> None:
        assert RUNBOOK.is_file(), f"missing {RUNBOOK}"
        rel = "docs/runbooks/terraform-apply-and-task-definition-ownership.md"
        assert rel in ecs_tf, (
            "infra/ecs.tf's ownership comment cites the runbook by path; keep them in step "
            "or the pointer rots into a dead reference."
        )

    def test_the_plan_role_script_is_dry_run_by_default(self) -> None:
        source = PLAN_ROLE_SH.read_text(encoding="utf-8")
        assert "APPLY=false" in source
        assert "--apply" in source
        # The whole point of a second role: the deploy role's trust must not be
        # widened to pull requests.
        assert "archimedes-github-plan" in source
        assert "update-assume-role-policy --role-name $ROLE_NAME" in source
        assert "archimedes-github-deploy" not in source.split("set -euo pipefail", 1)[1], (
            "setup-github-plan-role.sh must not touch archimedes-github-deploy — widening "
            "that role's trust to pull_request would let any PR branch push to ECR and "
            "SendCommand into production."
        )


class TestTheGateIsNotRedByConstruction:
    """A gate nobody can keep green is a gate nobody reads.

    ``terraform-drift.yml``'s own header invokes that failure mode to justify
    the ``TF_DRIFT_ENABLED`` switch. Two ways it could have reappeared anyway,
    both fixed and both pinned here.
    """

    @pytest.fixture(scope="class")
    def workflow(self) -> str:
        return DRIFT_WORKFLOW.read_text(encoding="utf-8")

    def test_a_push_to_main_cannot_cancel_the_weekly_scheduled_run(self, workflow: str) -> None:
        """`push` and `schedule` both run on refs/heads/main.

        With the ref alone as the group key and an unconditional
        ``cancel-in-progress``, a push touching ``infra/**`` silently cancels
        the Monday run — the only trigger that sees drift nobody committed
        (the deploy pipeline, console edits, AWS changing defaults). This
        renders the key GitHub would actually compute for each trigger rather
        than grepping for a fix, so either escape counts: distinct group keys,
        or cancellation that does not apply to them.
        """
        cancels = _concurrency(workflow)["cancel-in-progress"]
        push = _group_for(workflow, "push")
        schedule = _group_for(workflow, "schedule")
        assert push != schedule or cancels != "true", (
            f"a push and the weekly schedule both resolve to concurrency group {push!r} "
            f"with cancel-in-progress={cancels!r}, so a push to main kills the scheduled "
            "drift run. Put github.event_name in the group key, or scope "
            "cancel-in-progress to pull requests."
        )

    def test_a_pull_request_still_cancels_its_own_superseded_run(self, workflow: str) -> None:
        """Paired with the above: prove the fix was not 'turn concurrency off'.

        A force-push to a PR branch makes the older plan worthless, and that is
        the one trigger that genuinely races itself.
        """
        cancels = _concurrency(workflow)["cancel-in-progress"]
        assert cancels == "true" or "pull_request" in cancels, (
            "cancel-in-progress no longer applies to pull requests, so every force-push to "
            "a PR branch leaves a stale plan running."
        )
        assert _group_for(workflow, "pull_request") != _group_for(workflow, "push"), (
            "a pull request and a push to main share a concurrency group."
        )

    def test_an_intentional_infra_pr_is_not_red_by_construction(self, workflow: str) -> None:
        """A PR that CHANGES infra/** plans its own resources as `create`.

        ``tf_drift_gate.py`` fails on every planned change outside the single
        ``local_sensitive_file.private_key`` exemption, so left blocking, the
        ``pull_request`` arm would be red on exactly the PRs doing the right
        thing. plan.txt still uploads and the verdict is still logged, so
        nothing is hidden by making that one arm advisory.
        """
        step = _without_comments(_step(workflow, "Classify the plan"))
        match = re.search(r"continue-on-error:\s*(.+)", step)
        assert match is not None, (
            "the classify step is blocking on the pull_request arm, where a deliberate "
            "infra/** change plans as `create` and fails the gate by construction."
        )
        assert match.group(1).strip() == "${{ github.event_name == 'pull_request' }}", (
            "continue-on-error must be scoped to pull requests and nothing else — a bare "
            "`true` would also silence real drift on pushes to main and on the weekly "
            "schedule, which is the entire signal this workflow exists to produce."
        )

    def test_a_broken_plan_still_fails_on_every_arm(self, workflow: str) -> None:
        """The advisory arm is the CLASSIFIER, never the plan.

        Exit codes other than 0 and 2 mean the plan itself is broken — bad
        credentials, an unparseable root, a missing variable. That is never
        drift and must never be advisory, on any trigger.
        """
        step = _without_comments(_step(workflow, "terraform plan"))
        assert "continue-on-error" not in step, (
            "the terraform plan step became advisory. A broken plan would then report as a "
            "passing drift check on every trigger, which is worse than having no gate."
        )


class TestThePlanRoleCannotReadEveryProdSecret:
    """The Denys are the load-bearing half of that role's policy.

    Read from the live AWS-managed document on 2026-09-03: ``ReadOnlyAccess``
    is three ``Allow`` statements on ``Resource: "*"`` with no ``Deny`` of any
    kind, and it grants ``ssm:Get*`` on every parameter in the account. The
    script additionally grants ``kms:Decrypt`` on ``alias/aws/ssm`` — the one
    thing ReadOnlyAccess withholds — and all 19 of the account's SecureStrings
    are encrypted under that key. Without the Denys below, the pairing is
    permission to read CIRCLE_ENTITY_SECRET, BETTER_AUTH_SECRET, DATABASE_URL
    and the rest in cleartext, from a role assumable by any in-repo
    pull-request branch. A Deny beats an Allow from any policy, so a
    ``NotResource`` Deny re-scopes the managed attachment without enumerating
    it.
    """

    @staticmethod
    def _denies() -> list[dict[str, Any]]:
        return [st for st in _plan_role_policy()["Statement"] if st["Effect"] == "Deny"]

    @staticmethod
    def _allow(sid: str) -> dict[str, Any]:
        for st in _plan_role_policy()["Statement"]:
            if st.get("Sid") == sid:
                return st
        raise AssertionError(f"the inline policy lost its {sid!r} statement")

    def test_every_value_returning_ssm_action_is_denied_elsewhere(self) -> None:
        ssm = [d for d in self._denies() if any(a.startswith("ssm:") for a in _as_list(d["Action"]))]
        assert len(ssm) == 1, (
            "expected exactly one ssm Deny in setup-github-plan-role.sh's inline policy; "
            "without it ReadOnlyAccess's `ssm:Get*` on `*` plus this role's kms:Decrypt "
            "reads every SecureString in the account."
        )
        assert set(_as_list(ssm[0]["Action"])) >= {
            "ssm:GetParameter",
            "ssm:GetParameters",
            "ssm:GetParametersByPath",
            "ssm:GetParameterHistory",
        }, (
            "the ssm Deny must cover every action that RETURNS a parameter value. Leaving "
            "one out leaves that one call able to read all 19 SecureStrings."
        )

    def test_object_reads_outside_the_state_bucket_are_denied(self) -> None:
        s3 = [d for d in self._denies() if any(a.startswith("s3:") for a in _as_list(d["Action"]))]
        assert len(s3) == 1, "expected exactly one s3 Deny in the inline policy"
        assert set(_as_list(s3[0]["Action"])) >= {"s3:GetObject", "s3:GetObjectVersion"}

    def test_the_denys_are_scoped_with_notresource_never_resource(self) -> None:
        """The inversion that would look right and be catastrophic.

        ``{"Effect":"Deny","Resource": <the Aurora parameter>}`` denies the one
        thing the workflow needs and permits all eighteen other secrets — the
        exact opposite of the intent, and it still reads as "there is a Deny
        here" to anyone skimming.
        """
        for deny in self._denies():
            assert "NotResource" in deny, (
                f"Deny {deny.get('Sid')!r} has no NotResource. A Deny scoped with `Resource` "
                "denies ONLY that resource and leaves everything else allowed by "
                "ReadOnlyAccess — it inverts the protection."
            )
            assert "Resource" not in deny, (
                f"Deny {deny.get('Sid')!r} carries both Resource and NotResource; IAM rejects that statement outright."
            )

    def test_each_deny_carves_out_exactly_what_its_allow_grants(self) -> None:
        """A widened Allow with an unwidened Deny is dead permission; the
        reverse is a silent hole. Keep the pair in lockstep."""
        ssm = [d for d in self._denies() if any(a.startswith("ssm:") for a in _as_list(d["Action"]))][0]
        assert ssm["NotResource"] == self._allow("AuroraPasswordParameter")["Resource"], (
            "the ssm Deny exempts a different parameter from the one the Allow grants."
        )
        s3 = [d for d in self._denies() if any(a.startswith("s3:") for a in _as_list(d["Action"]))][0]
        assert s3["NotResource"] == self._allow("TerraformStateObjectRead")["Resource"], (
            "the s3 Deny exempts a different prefix from the one the Allow grants — "
            "`terraform init` would break, or objects outside state stay readable."
        )

    def test_the_decrypt_grant_is_still_pinned_to_ssm(self) -> None:
        """The Denys block the SSM path; kms:Decrypt must stay unreachable by
        any other route (an encrypted S3 object, say)."""
        decrypt = self._allow("AuroraPasswordDecrypt")
        assert _as_list(decrypt["Action"]) == ["kms:Decrypt"]
        via = decrypt.get("Condition", {}).get("StringEquals", {}).get("kms:ViaService", "")
        assert via.startswith("ssm."), (
            "kms:Decrypt lost its `kms:ViaService` pin to SSM, so the SSM key becomes usable "
            "through every other service that encrypts with it — and the ssm Deny, which "
            "only closes the SSM path, no longer bounds this grant."
        )


class TestDriftGateScript:
    @staticmethod
    def _gate():
        return _load(DRIFT_GATE_PY, "tf_drift_gate")

    @staticmethod
    def _plan(*resource_changes: dict[str, Any], outputs: dict[str, Any] | None = None) -> dict:
        return {
            "format_version": "1.2",
            "resource_changes": list(resource_changes),
            "output_changes": outputs or {},
        }

    @staticmethod
    def _change(address: str, actions: list[str]) -> dict[str, Any]:
        return {"address": address, "change": {"actions": actions}}

    def test_a_no_op_plan_is_clean(self) -> None:
        gate = self._gate()
        drift, exempt = gate.classify(self._plan(self._change("aws_ecs_service.backend", ["no-op"])))
        assert (drift, exempt) == ([], [])

    def test_the_local_key_file_is_exempt(self) -> None:
        """The one resource an ephemeral runner can never settle."""
        gate = self._gate()
        drift, exempt = gate.classify(self._plan(self._change("local_sensitive_file.private_key", ["create"])))
        assert drift == []
        assert len(exempt) == 1

    def test_the_exemption_is_pinned_to_one_action(self) -> None:
        """Mutation: the exempt resource planning a DELETE is real drift."""
        gate = self._gate()
        drift, _ = gate.classify(self._plan(self._change("local_sensitive_file.private_key", ["delete"])))
        assert len(drift) == 1
        assert "NOT exempt" in drift[0]

    def test_a_task_definition_replacement_is_drift(self) -> None:
        """The #1799 plan itself must fail this gate."""
        gate = self._gate()
        drift, _ = gate.classify(
            self._plan(
                self._change("aws_ecs_task_definition.backend", ["delete", "create"]),
                self._change("local_sensitive_file.private_key", ["create"]),
            )
        )
        assert drift == ["aws_ecs_task_definition.backend: delete+create"]

    def test_an_output_only_change_is_drift(self) -> None:
        """`database_url` interpolates the Aurora password — a wrong CI value
        shows up here and nowhere else."""
        gate = self._gate()
        drift, _ = gate.classify(self._plan(outputs={"database_url": {"actions": ["update"]}}))
        assert drift == ["output.database_url: update"]

    def test_a_read_action_is_not_drift(self) -> None:
        gate = self._gate()
        drift, exempt = gate.classify(self._plan(self._change("data.aws_lb_target_group.backend", ["read"])))
        assert (drift, exempt) == ([], [])

    def test_a_document_that_is_not_a_plan_fails_loudly(self) -> None:
        """An empty or truncated file must never read as 'no drift'."""
        gate = self._gate()
        with pytest.raises(gate.PlanError):
            gate.classify({})

    def test_main_exits_nonzero_on_drift(self, tmp_path: Path) -> None:
        gate = self._gate()
        path = tmp_path / "plan.json"
        path.write_text(
            json.dumps(self._plan(self._change("aws_s3_bucket.x", ["delete"]))),
            encoding="utf-8",
        )
        assert gate.main([str(path)]) == 1

    def test_main_exits_zero_on_an_exempt_only_plan(self, tmp_path: Path) -> None:
        gate = self._gate()
        path = tmp_path / "plan.json"
        path.write_text(
            json.dumps(self._plan(self._change("local_sensitive_file.private_key", ["create"]))),
            encoding="utf-8",
        )
        assert gate.main([str(path)]) == 0

    def test_main_exits_nonzero_on_an_unreadable_file(self, tmp_path: Path) -> None:
        gate = self._gate()
        path = tmp_path / "plan.json"
        path.write_text("not json", encoding="utf-8")
        assert gate.main([str(path)]) == 1
