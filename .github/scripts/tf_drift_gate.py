#!/usr/bin/env python3
"""Decide whether a ``terraform show -json`` plan counts as production drift.

``terraform plan -detailed-exitcode`` answers "is there anything to do", which
is *almost* the question a drift gate wants. The gap is that this root contains
one resource that can never be settled on an ephemeral runner:

    local_sensitive_file.private_key   (infra/main.tf)

It writes the deploy SSH key to ``./archimedes-deploy-key.pem`` in whatever
directory terraform ran from. The ``local`` provider's read step drops the
resource from state when that file is missing, so a fresh checkout — every CI
run, and every clone that is not the working copy which first applied —
plans it as ``create``. Exit code 2 is therefore permanent, and a permanently
red gate is a gate nobody reads. That is the same "the green check may not mean
what you think" failure mode ``infra-gate.yml`` is boxed about, wearing the
opposite colour.

So the workflow keeps ``-detailed-exitcode`` as the trigger and this script as
the verdict: it re-reads the plan as JSON and fails on **every** planned change
except the narrow, named, action-pinned exemptions in :data:`EPHEMERAL_RUNNER_EXEMPTIONS`.

Rules that keep the exemption list from rotting into a mute button:

* An entry names ONE resource address and the ONE action it may take. The
  exempt resource planning anything else — ``update``, ``delete``, ``replace`` —
  is drift and fails.
* Exemptions cover only resources whose drift is an artifact of *where
  terraform ran*, never a resource whose drift is an artifact of *when someone
  last applied*. Real, apply-able drift must stay visible; that is the product.
* Everything not listed fails, including resources that do not exist yet.

Hermetic: reads one JSON file, writes a report to stdout, exits 0 (clean) or
1 (drift / unreadable plan). No AWS, no network, no terraform.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, NamedTuple

# Actions terraform emits for something it is not going to change.
_INERT_ACTIONS = (["no-op"], ["read"])


class Exemption(NamedTuple):
    """One resource address that may take exactly one action without failing."""

    address: str
    action: list[str]
    reason: str


EPHEMERAL_RUNNER_EXEMPTIONS: tuple[Exemption, ...] = (
    Exemption(
        address="local_sensitive_file.private_key",
        action=["create"],
        reason=(
            "infra/main.tf writes the deploy SSH key to ./archimedes-deploy-key.pem "
            "in terraform's working directory. The local provider forgets the resource "
            "when that file is absent, so any checkout that is not the working copy "
            "which first applied plans it as a create. Nothing in AWS is drifting."
        ),
    ),
)


class PlanError(ValueError):
    """The plan JSON is not a shape this gate understands."""


def _material_resource_changes(plan: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Every (address, actions) terraform intends to act on.

    Raises :class:`PlanError` rather than returning ``[]`` when the document is
    not a terraform plan, so a truncated/empty/wrong file fails loudly instead
    of reporting a clean stack.
    """
    if not isinstance(plan, dict) or "format_version" not in plan:
        raise PlanError(
            "not a `terraform show -json` document (no format_version) — refusing "
            "to report 'no drift' from a file this gate cannot read"
        )

    changes = plan.get("resource_changes", [])
    if not isinstance(changes, list):
        raise PlanError("resource_changes is not a list")

    material: list[tuple[str, list[str]]] = []
    for entry in changes:
        if not isinstance(entry, dict):
            raise PlanError(f"resource_changes entry is not an object: {entry!r}")
        address = entry.get("address")
        actions = (entry.get("change") or {}).get("actions")
        if not isinstance(address, str) or not isinstance(actions, list):
            raise PlanError(f"resource_changes entry has no address/actions: {entry!r}")
        if actions in _INERT_ACTIONS:
            continue
        material.append((address, actions))
    return material


def _material_output_changes(plan: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Outputs terraform intends to change.

    An output-only diff still makes ``-detailed-exitcode`` return 2, and it can
    be the only visible symptom of a variable CI is feeding wrong (this root's
    ``database_url`` output interpolates ``var.aurora_master_password``), so it
    is drift like any other. No output is exempt.
    """
    outputs = plan.get("output_changes", {})
    if not isinstance(outputs, dict):
        raise PlanError("output_changes is not an object")

    material: list[tuple[str, list[str]]] = []
    for name, change in outputs.items():
        actions = (change or {}).get("actions")
        if not isinstance(actions, list):
            raise PlanError(f"output_changes[{name!r}] has no actions")
        if actions in _INERT_ACTIONS:
            continue
        material.append((name, actions))
    return material


def classify(plan: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Split a plan into (drift, exempted) human-readable lines."""
    exempt_by_address = {e.address: e for e in EPHEMERAL_RUNNER_EXEMPTIONS}

    drift: list[str] = []
    exempted: list[str] = []

    for address, actions in _material_resource_changes(plan):
        rendered = f"{address}: {'+'.join(actions)}"
        allowed = exempt_by_address.get(address)
        if allowed is not None and actions == allowed.action:
            exempted.append(f"{rendered}  — exempt: {allowed.reason}")
        elif allowed is not None:
            drift.append(
                f"{rendered}  — NOT exempt: {address} may only plan {'+'.join(allowed.action)} on an ephemeral runner"
            )
        else:
            drift.append(rendered)

    for name, actions in _material_output_changes(plan):
        drift.append(f"output.{name}: {'+'.join(actions)}")

    return drift, exempted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan_json", help="output of `terraform show -json <planfile>`")
    args = parser.parse_args(argv)

    try:
        with open(args.plan_json, encoding="utf-8") as fh:
            plan = json.load(fh)
        drift, exempted = classify(plan)
    except (OSError, json.JSONDecodeError, PlanError) as exc:
        print(f"::error::could not read the terraform plan JSON: {exc}", file=sys.stderr)
        return 1

    for line in exempted:
        print(f"  exempt   {line}")

    if not drift:
        print("terraform plan is clean (no changes outside the ephemeral-runner exemptions).")
        return 0

    for line in drift:
        print(f"  DRIFT    {line}")
    print(
        f"::error::terraform plan wants to change {len(drift)} thing(s) that CI does not "
        "consider environmental. Read the uploaded plan artifact, then either land the "
        "terraform that matches production or run `infra/apply.sh --apply` deliberately "
        "(docs/runbooks/terraform-apply-and-task-definition-ownership.md).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
