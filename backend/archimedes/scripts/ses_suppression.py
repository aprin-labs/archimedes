"""Operator tooling for the SES **account-level** suppression list (#1748 item 4).

THE PROBLEM THIS SOLVES. Dogfooding signup with throwaway addresses
(``probe-…@example.invalid`` and friends) bounced, and every bounce puts the
address on the AWS account's suppression list automatically. From then on SES
ACCEPTS a send to that address — ``SendEmail`` returns a MessageId — and then
drops the message. Nothing in the product could see it (that is #1748 item 2,
``GET /api/auth/verification-status``), and nothing could clear it. The trap
this leaves behind is the real one: a REAL address that was once typo'd,
temporarily undeliverable, or used as a dogfood probe stays blocked forever,
and the person retrying it gets silence.

WHAT THIS SCRIPT IS, AND IS NOT. It is a read-first inspector with a
single-address escape hatch:

  * ``list`` — what is on the list, why, and since when. Read-only, always.
  * ``check <address>`` — one address. Read-only, always.
  * ``remove <address>`` — DRY RUN by default: it looks the address up, prints
    exactly what it would delete, and deletes nothing. ``--apply`` is the only
    thing that issues ``DeleteSuppressedDestination``.

It is NOT a bulk cleaner, on purpose, and there is deliberately no flag that
clears the list. A suppression entry is AWS telling us a real mailbox refused
or complained about our mail; removing entries wholesale re-sends to every
address that ever bounced, which is precisely how a sender's reputation — and
with it delivery for every real user — is destroyed. ``remove`` takes exactly
one address, and the runbook
(``docs/runbooks/ses-suppression.md``) says the only address that qualifies is
one whose owner has confirmed it is real and working.

CREDENTIALS. Operator credentials from the ambient AWS profile/role, same as
every other script in this directory — nothing is read from the app's task
role and nothing is stored here. The two IAM actions needed are
``ses:ListSuppressedDestinations`` / ``ses:GetSuppressedDestination`` (read)
and ``ses:DeleteSuppressedDestination`` (only for ``--apply``). The auth
service's own task role has the GET only; it can never delete.

Usage (run from the repo root; ``PYTHONPATH=backend`` makes ``archimedes``
importable)::

    PYTHONPATH=backend python -m archimedes.scripts.ses_suppression list
    PYTHONPATH=backend python -m archimedes.scripts.ses_suppression list --reason BOUNCE --json
    PYTHONPATH=backend python -m archimedes.scripts.ses_suppression check dan@example.com
    PYTHONPATH=backend python -m archimedes.scripts.ses_suppression remove dan@example.com
    PYTHONPATH=backend python -m archimedes.scripts.ses_suppression remove dan@example.com --apply

Exit codes: ``0`` success (a dry run that found something to do is a success),
``2`` a usage/configuration error, ``3`` the address is not on the list at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

#: SESv2's own vocabulary. Passed through rather than translated, so a value
#: AWS adds later still reaches the operator intact.
SUPPRESSION_REASONS = ("BOUNCE", "COMPLAINT")

#: A page of ListSuppressedDestinations. The list is small (a handful of
#: dogfood bounces) — this exists so a surprise does not print forever.
DEFAULT_LIMIT = 200

_AWS_REGION_ENV = ("AWS_REGION", "AWS_DEFAULT_REGION")


class SuppressionLookupFailed(RuntimeError):
    """The AWS call itself failed — never conflated with "not suppressed"."""


def _region() -> str | None:
    for name in _AWS_REGION_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _client():  # pragma: no cover - thin boto3 construction, stubbed in tests
    import boto3

    return boto3.client("sesv2", region_name=_region())


def _isoformat(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def _not_found(error: Exception) -> bool:
    """SESv2 answers "this address is not suppressed" with an exception.

    Matched on the error's CLASS NAME rather than on ``botocore.exceptions``
    imports so this module (and its tests) never need botocore's exception
    factory — the stub client in the tests raises a plain class of the same
    name and travels the same path production does.
    """
    return type(error).__name__ in {"NotFoundException", "ResourceNotFoundException"}


# ────────────────────────────── read side ───────────────────────────────


def list_suppressed(client, *, reasons: tuple[str, ...] | None = None, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Every suppressed destination, newest page first, capped at ``limit``.

    Paginates by hand rather than via ``get_paginator`` so the cap is honoured
    across pages and so the stub client in the tests is a plain object with one
    method, not a paginator factory.
    """
    entries: list[dict] = []
    next_token: str | None = None
    while len(entries) < limit:
        request: dict[str, Any] = {"PageSize": min(100, limit - len(entries))}
        if reasons:
            request["Reasons"] = list(reasons)
        if next_token:
            request["NextToken"] = next_token
        try:
            page = client.list_suppressed_destinations(**request)
        except Exception as exc:  # broad on purpose: any boto/botocore failure is one thing here
            raise SuppressionLookupFailed(f"ListSuppressedDestinations failed: {type(exc).__name__}") from exc
        for summary in page.get("SuppressedDestinationSummaries", []) or []:
            entries.append(
                {
                    "email": summary.get("EmailAddress"),
                    "reason": summary.get("Reason"),
                    "last_update": _isoformat(summary.get("LastUpdateTime")),
                }
            )
        next_token = page.get("NextToken")
        if not next_token:
            break
    return entries[:limit]


def check_suppressed(client, address: str) -> dict | None:
    """One address. ``None`` means "not on the list"; an exception means "we could not look".

    The distinction is the whole point: a failed lookup must never be reported
    as a clean address, which is the same rule ``auth/suppression.js`` follows
    on the product side.
    """
    try:
        response = client.get_suppressed_destination(EmailAddress=address)
    except Exception as exc:  # broad on purpose: classified by _not_found immediately below
        if _not_found(exc):
            return None
        raise SuppressionLookupFailed(f"GetSuppressedDestination failed: {type(exc).__name__}") from exc
    destination = response.get("SuppressedDestination", {}) or {}
    return {
        "email": destination.get("EmailAddress", address),
        "reason": destination.get("Reason"),
        "last_update": _isoformat(destination.get("LastUpdateTime")),
    }


# ────────────────────────────── write side ──────────────────────────────


def remove_suppressed(client, address: str, *, apply: bool = False) -> dict:
    """Remove ONE address from the suppression list.

    ``apply=False`` (the default) is a genuine dry run: the address is looked
    up and the finding is returned, and ``DeleteSuppressedDestination`` is not
    called. That default is the guard — an operator who forgets the flag gets a
    report, not a silent reputation change.

    Returns ``{"found": bool, "applied": bool, "entry": dict|None}``.
    """
    entry = check_suppressed(client, address)
    if entry is None:
        return {"found": False, "applied": False, "entry": None}
    if not apply:
        return {"found": True, "applied": False, "entry": entry}
    try:
        client.delete_suppressed_destination(EmailAddress=address)
    except Exception as exc:  # broad on purpose: surfaced as SuppressionLookupFailed
        if _not_found(exc):
            # Raced with another removal. Nothing to do, and saying "removed"
            # would be a claim this run cannot back.
            return {"found": False, "applied": False, "entry": entry}
        raise SuppressionLookupFailed(f"DeleteSuppressedDestination failed: {type(exc).__name__}") from exc
    return {"found": True, "applied": True, "entry": entry}


# ────────────────────────────── rendering ───────────────────────────────


def render_list(entries: list[dict]) -> str:
    if not entries:
        return "suppression list is empty (for the requested reasons)"
    width = max(len(str(entry["email"])) for entry in entries)
    lines = [f"{len(entries)} suppressed destination(s):"]
    for entry in entries:
        lines.append(f"  {entry['email']!s:<{width}}  {entry['reason'] or '—':<9}  {entry['last_update'] or '—'}")
    return "\n".join(lines)


def render_removal(address: str, result: dict) -> str:
    if not result["found"]:
        return f"{address} is not on the suppression list — nothing to remove"
    entry = result["entry"]
    detail = f"{address} (reason {entry['reason'] or '—'}, since {entry['last_update'] or '—'})"
    if result["applied"]:
        return f"removed {detail}"
    return (
        f"DRY RUN — would remove {detail}\n"
        "Nothing was changed. Re-run with --apply only if the address's owner has confirmed it is\n"
        "real and reachable; see docs/runbooks/ses-suppression.md."
    )


# ──────────────────────────────── CLI ───────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses_suppression",
        description=(
            "Inspect and (one address at a time) clean the SES account-level suppression list. "
            "Read-only by default: `remove` is a DRY RUN unless --apply is passed, and there is "
            "deliberately no bulk-clear — see docs/runbooks/ses-suppression.md."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="Show suppressed destinations. Read-only.")
    listing.add_argument(
        "--reason",
        action="append",
        choices=SUPPRESSION_REASONS,
        help="Filter by SES reason. Repeatable. Default: both.",
    )
    listing.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Max entries (default {DEFAULT_LIMIT}).")

    checking = sub.add_parser("check", help="Is ONE address suppressed, and why? Read-only.")
    checking.add_argument("address")

    # Exactly one positional, and no --all: the absence of a bulk mode is the
    # guard, not an oversight. See the module docstring.
    removal = sub.add_parser("remove", help="Remove ONE address. Dry run unless --apply.")
    removal.add_argument("address")
    removal.add_argument(
        "--apply",
        action="store_true",
        help="Actually call DeleteSuppressedDestination. Without this nothing is changed.",
    )
    return parser


def main(argv: list[str] | None = None, *, client=None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)
    aws = client or _client()

    try:
        if args.command == "list":
            reasons = tuple(args.reason) if args.reason else None
            if args.limit <= 0:
                print("error: --limit must be positive", file=sys.stderr)
                return 2
            entries = list_suppressed(aws, reasons=reasons, limit=args.limit)
            print(json.dumps(entries, indent=2) if args.json else render_list(entries))
            return 0

        if args.command == "check":
            entry = check_suppressed(aws, args.address)
            if entry is None:
                print(
                    json.dumps({"email": args.address, "suppressed": False}, indent=2)
                    if args.json
                    else f"{args.address} is not on the suppression list"
                )
                return 3
            print(json.dumps({**entry, "suppressed": True}, indent=2) if args.json else render_list([entry]))
            return 0

        result = remove_suppressed(aws, args.address, apply=args.apply)
        print(
            json.dumps({"address": args.address, **result}, indent=2)
            if args.json
            else render_removal(args.address, result)
        )
        return 0 if result["found"] or result["applied"] else 3
    except SuppressionLookupFailed as exc:
        # A failed AWS call is never rendered as "not suppressed" — the whole
        # reason this list is dangerous is that silence looks like success.
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
