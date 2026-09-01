#!/usr/bin/env python3
"""Rewrite the live ECS task definition for a CI deploy.

``deploy.yml`` registers a new revision by cloning the *currently registered*
task definition and retagging images. It does **not** apply terraform, so
pins that exist only in ``infra/ecs.tf`` are not live until someone applies.
Proven on task-def :211 (``fc884113``, #1725 image): the clone carried no
``PAPER_ADVANCE_ENABLED``, the code default was still ON, and ``/health``
502'd at ``PAPER_ADVANCE_STARTUP_DELAY_S`` (240s) after deploy-ecs success.

So this script pins the name EXPLICITLY on every deploy, in whichever
direction :data:`PAPER_ADVANCE_VALUE` points. The property that matters is not
which value ships but that the value ships *because this file says so*, rather
than because a months-old last-good revision happened to carry the name.

This script is the path that actually ships. It:

1. Retags the backend / nginx / auth images to this commit.
2. Pins ``PAPER_ADVANCE_ENABLED`` to :data:`PAPER_ADVANCE_VALUE` on the
   backend container, whether the cloned definition had the name unset,
   ``"true"``, or ``"false"``. This PR does not flip that pin.
3. Pins ``FREE_GENERATIONS_PER_ACCOUNT`` to :data:`FREE_GENERATIONS_VALUE`
   on the backend container, for the same reason (#1643 finding A5): prod was
   giving away three generations per account by accident of a code default,
   which is the only knob on the flip-list that hands out paid product.
4. Ensures the backend container carries the :data:`TIINGO_SECRET_NAME`
   secret, resolving from ``/archimedes/prod/TIINGO_API_TOKEN``.
5. Points the backend container's ``healthCheck`` at ``/health/ready`` and
   pins ``HEALTH_STALE_UNREADY_S`` beside it (#1818 P3), for the same reason:
   this job never terraform-applies, so a ``healthCheck`` that exists only in
   ``infra/ecs.tf`` is not live until somebody does, and a clone registered
   before #1818 P3 goes on shipping the old ``/health`` command forever. If
   #1799 (PR #1833, OPEN as of 2026-09-03) lands, ``ignore_changes =
   [container_definitions]`` makes terraform stop writing container settings at
   ALL and this script becomes not merely the first writer but the only one —
   a strengthening, not a premise.
6. Pins the backend container ``healthCheck.startPeriod`` to 90s so a
   cloned live revision (startPeriod 30) cannot leave the #1713 warmup
   budget (60s) outside ECS's ignored-failure window. Command stays the
   readiness probe; interval / timeout / retries on an existing healthCheck
   are preserved; a clone with no healthCheck gets the default shape with
   startPeriod 90. Do not terraform-noop ``deployment_minimum_healthy_percent``.
7. Strips the retired env names in :data:`RETIRED_BACKEND_ENV` from the
   backend container. This runs AFTER the pins above, so the retired tuple is
   the last word on what ships; a name that ever landed on both lists would
   lose its pin, which ``test_no_pinned_name_is_also_retired`` forbids rather
   than leaves to the reading order. #1766 deleted
   ``services/backtest_scheduler.py``; nothing on main reads
   ``BACKTEST_REFRESH_*``/``BACKTEST_MAX_AGE_HOURS`` any more, but the owner's
   hand-pinned ``BACKTEST_REFRESH_ENABLED=false`` (task-def 216, the #1760
   mitigation) rides forward on every clone. #1811 retired
   ``ARCHIMEDES_FUSION_ENABLED`` on 2026-09-02 and left the same residue for
   the same reason. Cleanups ship with the deploy rather than as an operator
   ritual, so the clone drops them here.
8. Drops the describe-only fields ``register-task-definition`` rejects.

The pinned value is ``"true"`` as of 2026-09-01 (#1778, the #1632 lift): the
paper-advance tick is ARMED. It never runs in the web interpreter —
``arm_paper_advance_for_web_tier`` spawns a child (#1728) — so a C-level abort
on the tick kills that child while ``/health``, in the parent, keeps
answering. That process boundary is what makes arming defensible; it is not a
claim that the tick's own frame is proven clean (#1632's found-and-fixed
mechanism, #1740, was elsewhere and was caught with this flag off).

To pull it back: set :data:`PAPER_ADVANCE_VALUE` to ``"false"`` here — this is
the pin that ships — and set the twin line in ``infra/ecs.tf`` to match, then
deploy. The code default in ``services/paper_trading.py`` stays ``"false"`` on
purpose: unset must still mean OFF, which is the :211 hole.

terraform apply is still required for other ``ecs.tf`` drift. This flag must
not depend on it."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

PAPER_ADVANCE_NAME = "PAPER_ADVANCE_ENABLED"
PAPER_ADVANCE_VALUE = "true"

#: Seconds of continuously ``stale_cached`` DB probes before a task calls
#: itself unready and the container check starts failing (#1818 P3). Kept equal
#: to ``main._DEFAULT_STALE_UNREADY_SECONDS`` so this pin is plumbing, not a
#: policy change, and stated here rather than left to the code default because
#: an unset name in a cloned revision is decided by nobody — the :211 shape.
#:
#: How to pull the rule back. Setting the name to ``"0"`` on the live revision
#: (console or ``register-task-definition``) disables it immediately and holds
#: until the next deploy, which is the right shape for an incident. The
#: DURABLE pull-back is editing this constant to ``"0"`` and deploying: with
#: the pipeline pinning the name on every deploy, a console edit alone would be
#: silently undone by the next merge.
HEALTH_STALE_UNREADY_NAME = "HEALTH_STALE_UNREADY_S"
HEALTH_STALE_UNREADY_VALUE = "900"

#: The readiness endpoint the CONTAINER health check must probe (#1818 P3).
#: ``/health`` answers 200 while a task is alive; it answered 200 for ten hours
#: on 2026-09-03 while neither task could read its database. ``/health/ready``
#: answers 503 once the DB-backed probes have been serving cached readings for
#: longer than :data:`HEALTH_STALE_UNREADY_VALUE` seconds, which is the verdict
#: the ECS scheduler can act on proportionately (``deployment_minimum_healthy_
#: percent = 100`` replaces before it drains). The ALB target group is NOT
#: moved: it acts on every target at once, and a shared cause would pull the
#: whole fleet — the incident's own 13:29Z HealthyHostCount=0 line.
READINESS_PROBE_URL = "http://localhost:8000/health/ready"
READINESS_HEALTH_CHECK_COMMAND = [
    "CMD-SHELL",
    "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready')\" || exit 1",
]

#: Only used when the cloned revision carries no ``healthCheck`` at all. Mirrors
#: the block in ``infra/ecs.tf``; 3 retries x 30s => ~90s of continuous 503
#: before ECS acts. startPeriod is 90 so the #1713 request-path warmup budget
#: (60s) fits inside ECS's ignored-failure window; a clone of the live 30s
#: revision cannot leave that budget outside the window. Do not lower this
#: below the warmup budget. Do not raise desiredCount. Do not flip
#: PAPER_ADVANCE as part of the warmup pin.
BACKEND_HEALTHCHECK_START_PERIOD = 90
_DEFAULT_HEALTH_CHECK_SHAPE: dict[str, Any] = {
    "interval": 30,
    "timeout": 5,
    "retries": 3,
    "startPeriod": BACKEND_HEALTHCHECK_START_PERIOD,
}

#: Free generations per account, lifetime (#1643). Kept equal to
#: ``free_generations.DEFAULT_ALLOWANCE`` so this pin is plumbing, not a policy
#: change — the twin line in ``infra/ecs.tf`` must say the same number.
FREE_GENERATIONS_NAME = "FREE_GENERATIONS_PER_ACCOUNT"
FREE_GENERATIONS_VALUE = "3"

BACKEND_CONTAINER = "backend"

# #1798. The env var name is the canonical one the provider reads first
# (``market_data_provider._TIINGO_TOKEN_ENV_VARS[0]``); the SSM path is the
# owner-seeded SecureString. Both halves are pinned against their real
# sources by backend/tests/test_ecs_backend_secrets.py.
TIINGO_SECRET_NAME = "TIINGO_API_TOKEN"
TIINGO_SSM_PATH = "parameter/archimedes/prod/TIINGO_API_TOKEN"

# Env names the backend container must no longer carry. Every name here has
# zero readers under ``backend/archimedes`` — asserted by
# ``backend/tests/test_ecs_paper_advance_deploy_pin.py``, so re-adding a
# reader without taking the name off this tuple goes red rather than shipping
# a flag the deploy silently deletes.
#
# ``BACKTEST_REFRESH_ENABLED`` is the live pin: the owner set it by hand as
# task-def revision 216 during the 2026-09-01 #1760 storm, #1766 deleted the
# loop it switched, and the deploy has cloned it forward ever since.
#
# ``ARCHIMEDES_FUSION_ENABLED`` is the same shape one retirement later — the
# INERT class of #1824's flag inventory. #1811 retired the flag on 2026-09-02 and
# took the reader, the OFF branch, the ``infra/ecs.tf`` pin, both compose
# defaults and ``infra/spike-1411/function-env.txt`` with it — but the LIVE
# task definition still carries ``ARCHIMEDES_FUSION_ENABLED=true`` on the
# backend container, and this job clones the live revision rather than applying
# terraform, so the name rides forward on every deploy with nothing on the far
# end. The flip-list row named that as the outstanding operator action and
# named this tuple as the fix; taking it here means no ``terraform apply`` and
# no operator ritual. Stripping it is a no-op on behaviour by construction:
# ``backend/tests/test_fusion_flag_retired.py`` fails if any reader comes back.
RETIRED_BACKEND_ENV = (
    "BACKTEST_REFRESH_ENABLED",
    "BACKTEST_REFRESH_INTERVAL_HOURS",
    "BACKTEST_MAX_AGE_HOURS",
    "BACKTEST_REFRESH_STARTUP_DELAY_S",
    "ARCHIMEDES_FUSION_ENABLED",
)

# Same drop-list the previous inline jq used. These fields come back from
# ``describe-task-definition`` and ``register-task-definition`` will not
# accept them.
_DROP_FIELDS = (
    "taskDefinitionArn",
    "revision",
    "status",
    "requiresAttributes",
    "compatibilities",
    "registeredAt",
    "registeredBy",
)


class RewriteError(ValueError):
    """The cloned task definition cannot be turned into a deployable revision."""


def pin_backend_paper_advance(environment: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return a copy of ``environment`` with the tick flag pinned explicitly.

    Entries other than ``PAPER_ADVANCE_ENABLED`` are preserved in order. The
    pin replaces any previous value and is otherwise appended, so a cloned
    last-good revision that never heard of the name still ships
    :data:`PAPER_ADVANCE_VALUE` — exactly once, whichever way it points.
    """
    pinned = [entry for entry in (environment or []) if entry.get("name") != PAPER_ADVANCE_NAME]
    pinned.append({"name": PAPER_ADVANCE_NAME, "value": PAPER_ADVANCE_VALUE})
    return pinned


def pin_backend_health_stale_unready(environment: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return a copy of ``environment`` with the readiness threshold pinned.

    Order-preserving, idempotent, exactly like :func:`pin_backend_paper_advance`:
    the name ends up present exactly once carrying
    :data:`HEALTH_STALE_UNREADY_VALUE`, whatever the clone held.

    The threshold has a code default (``main._DEFAULT_STALE_UNREADY_SECONDS``),
    so an absent name is not an outage the way an absent ``PAPER_ADVANCE_ENABLED``
    was. It is pinned anyway because the value is a safety threshold an operator
    reads off the running task definition when deciding whether a replacement was
    the rule firing correctly; "not stated, therefore 900" is a fact about an
    image, and the image is exactly what an operator does not have in front of
    them at 03:32Z.
    """
    pinned = [entry for entry in (environment or []) if entry.get("name") != HEALTH_STALE_UNREADY_NAME]
    pinned.append({"name": HEALTH_STALE_UNREADY_NAME, "value": HEALTH_STALE_UNREADY_VALUE})
    return pinned


def rewrite_backend_health_check(health_check: dict[str, Any] | None) -> dict[str, Any]:
    """Return the backend container's health check, pointed at ``/health/ready``.

    ``command`` is this file's decision (the #1818 P3 readiness probe).
    ``startPeriod`` is also this file's decision: 90s so the #1713 warmup
    budget (60s) fits inside ECS's ignored-failure window. A cloned live
    revision still carries startPeriod 30; preserving that would leave the
    warmup outside the window — the pin this function exists to close.
    ``interval``, ``timeout``, ``retries`` and any unknown fields the live
    revision carries are preserved.

    A clone with no ``healthCheck`` key at all gets the full block from
    :data:`_DEFAULT_HEALTH_CHECK_SHAPE` instead of being left alone. ECS reads
    the health check off the task definition; with the key absent the image's
    own ``HEALTHCHECK`` is invisible to the agent, so nginx's ``dependsOn
    condition = HEALTHY`` has nothing to wait on and no scheduler ever sees the
    503. That is the same hole as an unset env name and it closes the same way.
    """
    rewritten: dict[str, Any] = dict(health_check or {})
    for key, value in _DEFAULT_HEALTH_CHECK_SHAPE.items():
        rewritten.setdefault(key, value)
    rewritten["command"] = list(READINESS_HEALTH_CHECK_COMMAND)
    rewritten["startPeriod"] = BACKEND_HEALTHCHECK_START_PERIOD
    return rewritten


def pin_backend_free_generations(environment: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return a copy of ``environment`` with the free allowance pinned explicitly.

    Idempotent and order-preserving, exactly like
    :func:`pin_backend_paper_advance`: an entry already carrying
    :data:`FREE_GENERATIONS_VALUE` is left where it is, a different value is
    overwritten in place, and an absent name is appended — so the result holds
    the name exactly once whatever the clone carried.

    The overwrite is the part that earns a ``::notice``. Every other outcome is
    the steady state and says nothing; a clone whose value *disagreed* with
    this file means the registered revision was serving a different allowance
    than the repo declares, and an operator reading the deploy log should see
    the number change rather than infer it from a diff of two task-def
    revisions after the fact. Written to stderr because stdout is the JSON
    ``register-task-definition`` consumes.
    """
    entries = list(environment or [])
    pinned: list[dict[str, Any]] = []
    seen = False
    for entry in entries:
        if entry.get("name") != FREE_GENERATIONS_NAME:
            pinned.append(entry)
            continue
        if seen:
            # A duplicated name: ECS takes the last, so a second entry is a
            # silent override. Drop it — the one kept above is the pin.
            continue
        seen = True
        previous = entry.get("value")
        if previous != FREE_GENERATIONS_VALUE:
            print(
                f"::notice::{FREE_GENERATIONS_NAME} pinned to {FREE_GENERATIONS_VALUE} "
                f"(cloned revision carried {previous!r})",
                file=sys.stderr,
            )
        pinned.append({"name": FREE_GENERATIONS_NAME, "value": FREE_GENERATIONS_VALUE})
    if not seen:
        print(
            f"::notice::{FREE_GENERATIONS_NAME} pinned to {FREE_GENERATIONS_VALUE} "
            "(cloned revision did not carry the name)",
            file=sys.stderr,
        )
        pinned.append({"name": FREE_GENERATIONS_NAME, "value": FREE_GENERATIONS_VALUE})
    return pinned


def tiingo_secret_arn(task_def: dict[str, Any]) -> str:
    """Build the Tiingo SSM ARN from the cloned definition's OWN ARN.

    Region and account are read out of ``taskDefinitionArn``
    (``arn:aws:ecs:<region>:<account>:task-definition/<family>:<rev>``), which
    ``aws ecs describe-task-definition`` always returns, rather than taken from
    a workflow env var or a CLI flag. Same reasoning as
    :data:`PAPER_ADVANCE_VALUE`'s: a parameter someone can forget to pass has a
    default, and that default silently decides production. Deriving it means
    the ARN is correct in whatever account and region this deploy is actually
    talking to, and wrong nowhere.

    Raises :class:`RewriteError` rather than guessing. A guessed account id
    produces a syntactically perfect ARN that fails at task-launch time with
    ``ResourceInitializationError`` — the whole service down, not one feature
    degraded.
    """
    arn = task_def.get("taskDefinitionArn")
    if not isinstance(arn, str):
        raise RewriteError(
            "cloned task definition has no taskDefinitionArn — cannot derive the "
            f"account/region for the {TIINGO_SECRET_NAME} secret ARN"
        )
    parts = arn.split(":")
    # arn : partition : service : region : account : resource[ : qualifier ]
    if len(parts) < 6 or parts[0] != "arn" or not parts[1] or not parts[3] or not parts[4]:
        raise RewriteError(
            f"taskDefinitionArn {arn!r} is not a parseable ARN — cannot derive the "
            f"account/region for the {TIINGO_SECRET_NAME} secret ARN"
        )
    partition, region, account = parts[1], parts[3], parts[4]
    return f"arn:{partition}:ssm:{region}:{account}:{TIINGO_SSM_PATH}"


def ensure_backend_tiingo_secret(secrets: list[dict[str, Any]] | None, arn: str) -> list[dict[str, Any]]:
    """Return a copy of ``secrets`` carrying exactly one Tiingo entry.

    Idempotent in the same shape as :func:`pin_backend_paper_advance`: any
    existing entry under the name is dropped and the canonical one appended, so
    a clone that already has it (every deploy after the first, and every clone
    of a terraform-registered revision) comes out unchanged in content and
    carrying it exactly once, while a clone that predates the wiring gains it.
    Other secrets are preserved in order.
    """
    kept = [entry for entry in (secrets or []) if entry.get("name") != TIINGO_SECRET_NAME]
    kept.append({"name": TIINGO_SECRET_NAME, "valueFrom": arn})
    return kept


def strip_retired_backend_env(
    environment: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return ``environment`` without any :data:`RETIRED_BACKEND_ENV` name.

    Idempotent: a clone that already lost them comes back unchanged with an
    empty removal list, so the deploy after the first one is a no-op. Entries
    that survive keep their order and their objects untouched.

    Returns the filtered list and the names that were actually removed, so
    the caller can say what it deleted instead of deleting silently.
    """
    retired = set(RETIRED_BACKEND_ENV)
    kept: list[dict[str, Any]] = []
    removed: list[str] = []
    for entry in environment or []:
        name = entry.get("name")
        if name in retired:
            removed.append(name)
        else:
            kept.append(entry)
    return kept, removed


def rewrite_registered_task_definition(
    task_def: dict[str, Any],
    *,
    backend_image: str,
    nginx_image: str,
    auth_image: str,
) -> dict[str, Any]:
    """Clone ``task_def``, retag application images, pin the tick + allowance
    flags, the Tiingo secret and the readiness check, and drop retired env.

    Raises :class:`RewriteError` if there is no backend container — a silent
    skip would register a revision whose ``PAPER_ADVANCE_ENABLED`` and whose
    container health check are whatever the clone happened to carry, decided by
    nobody. That is the :211 shape.

    Everything written here is inside ``containerDefinitions``, which is what
    keeps #1799's ``ignore_changes = [container_definitions]`` correct: no
    top-level field gains a second registrar
    (``backend/tests/test_task_definition_ownership.py`` holds that).
    """
    containers = task_def.get("containerDefinitions")
    if not isinstance(containers, list) or not containers:
        raise RewriteError("task definition has no containerDefinitions")

    secret_arn = tiingo_secret_arn(task_def)

    rewritten: list[dict[str, Any]] = []
    saw_backend = False
    for container in containers:
        name = container.get("name")
        next_container = dict(container)
        if name == BACKEND_CONTAINER:
            saw_backend = True
            next_container["image"] = backend_image
            pinned = pin_backend_health_stale_unready(
                pin_backend_free_generations(pin_backend_paper_advance(container.get("environment")))
            )
            # Strip LAST, on the already-pinned list: the retired tuple is the
            # final word on what the backend container may carry, so a name
            # that ever lands on both lists loses rather than shipping a knob
            # nothing reads. The two lists are held disjoint by
            # ``test_no_pinned_name_is_also_retired``, so today the strip
            # cannot reach a pin at all.
            environment, removed = strip_retired_backend_env(pinned)
            if removed:
                print(
                    f"::notice::dropping retired backend env from the cloned task definition: {', '.join(removed)}",
                    file=sys.stderr,
                )
            next_container["environment"] = environment
            next_container["secrets"] = ensure_backend_tiingo_secret(container.get("secrets"), secret_arn)
            next_container["healthCheck"] = rewrite_backend_health_check(container.get("healthCheck"))
        elif name == "nginx":
            next_container["image"] = nginx_image
        elif name == "auth":
            next_container["image"] = auth_image
        rewritten.append(next_container)

    if not saw_backend:
        raise RewriteError(
            f"no {BACKEND_CONTAINER!r} container in the cloned task definition — "
            "refusing to register a revision that cannot pin PAPER_ADVANCE_ENABLED "
            f"or {TIINGO_SECRET_NAME}, "
            "or point the container health check at /health/ready"
        )

    out = {key: value for key, value in task_def.items() if key not in _DROP_FIELDS}
    out["containerDefinitions"] = rewritten
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_def_json", help="describe-task-definition taskDefinition blob")
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--nginx-image", required=True)
    parser.add_argument("--auth-image", required=True)
    args = parser.parse_args(argv)

    with open(args.task_def_json, encoding="utf-8") as fh:
        task_def = json.load(fh)

    try:
        rewritten = rewrite_registered_task_definition(
            task_def,
            backend_image=args.backend_image,
            nginx_image=args.nginx_image,
            auth_image=args.auth_image,
        )
    except RewriteError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    json.dump(rewritten, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
