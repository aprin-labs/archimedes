"""Drain the SES bounce/complaint feedback queue onto the user rows (#1804).

THE PROBLEM THIS SOLVES. A hard bounce from Amazon SES used to reach this
system in exactly one way: AWS quietly added the address to the account
suppression list, after which every ``SendEmail`` to it SUCCEEDED, returned a
``MessageId``, and was binned inside AWS. The only in-repo view of that fact
was an operator running ``archimedes.scripts.ses_suppression list``. The
product's own view was ``auth_users.emailVerified = false`` — the same value it
holds for a real person who has not clicked the link yet — and that boolean is
also the free-generation gate (``services/free_generations.py``). A fake or
dead address was therefore indistinguishable from an impatient human, and was
locked out with no explanation.

``infra/ses_events.tf`` is the push half: a SES configuration set publishes
BOUNCE / COMPLAINT / REJECT / DELIVERY events to an SNS topic, which fans out
to a durable SQS queue. This module is what reads that queue and writes the
fact down::

    SES ─▶ configuration set ─▶ SNS ─▶ SQS ─▶ [this] ─▶ auth_users.emailBouncedAt
                                                                 emailBounceKind

and ``auth/auth.js`` is what then refuses signup and the self-service resend
for a stamped address, with a typed reason instead of a cheerful 200.

WHAT COUNTS AS A BOUNCE, AND WHAT DOES NOT.

    SES event                       recorded as        why
    ─────────────────────────────── ────────────────── ─────────────────────────
    Bounce / Permanent              ``bounce``         mailbox does not exist
    Bounce / Transient              nothing            full mailbox, DNS blip —
                                                       a real person, not a
                                                       fake address
    Bounce / Undetermined           nothing            SES itself does not know
    Complaint                       ``complaint``      a human told their
                                                       provider to stop us
    Reject / Delivery               nothing            carried on the same
                                                       stream as the control
                                                       group: they are how you
                                                       tell "no bounces" from
                                                       "no events at all"

Stamping a transient bounce would lock out a real user over a full mailbox,
which is the same defect this issue is about, pointed the other way.

IT IS A BATCH COMMAND, NOT A SERVICE — AND SOMETHING CALLS IT. ``drain`` reads
what is on the queue, writes, and exits; it never polls. What invokes it is
``aws_scheduler_schedule.ses_events_drain`` (``infra/ses_events.tf``), an
EventBridge Scheduler schedule that runs the dedicated single-container
``archimedes-ses-events-drain`` Fargate task every 15 minutes by default — the
same one-off shape as the Alembic migrate task, deliberately NOT a command
override on the three-container service family, whose nginx sidecar
``dependsOn`` the backend being HEALTHY and would be waiting on an HTTP server
this command never starts.

The queue is what licenses that interval: ``infra/ses_events.tf`` gives it the
maximum 14-day retention and a dead-letter queue, so a bounce that arrives
between ticks is LATE, never lost — and two CloudWatch alarms
(queue ``ApproximateAgeOfOldestMessage``, DLQ depth) are what stop a schedule
that quietly stops running from putting the product back where #1804 found it.

The same command is run by hand whenever you want to watch it::

    # in the running backend task (the ECS task role carries the queue grant —
    # aws_iam_role_policy.ecs_task_ses_events_queue), SES_EVENTS_QUEUE_URL is
    # already in its environment:
    python -m archimedes.scripts.ses_events drain

    # from an operator shell, naming the queue explicitly:
    PYTHONPATH=backend python -m archimedes.scripts.ses_events drain \\
        --queue-url "$(terraform output -raw ses_events_queue_url)"

    # look, change nothing, delete nothing:
    PYTHONPATH=backend python -m archimedes.scripts.ses_events drain --dry-run

``drain`` WRITES BY DEFAULT — deliberately unlike ``ses_suppression.py``'s
``remove``, whose ``--apply`` gate exists because deleting suppression entries
damages sender reputation. Consuming a queue is this command's whole job, and a
consumer that no-ops unless someone remembers a flag is a consumer that
silently does nothing forever. ``--dry-run`` is the inspection mode.

``clear`` is the other half of the operator story. ``ses_suppression remove``
takes an address off the AWS suppression list; that alone does NOT let the
person back in, because the refusal in ``auth/auth.js`` reads the row this
module wrote. The two must be run together, and the runbook
(``docs/runbooks/ses-bounce-signal.md``) says so::

    PYTHONPATH=backend python -m archimedes.scripts.ses_events clear dan@example.com
    PYTHONPATH=backend python -m archimedes.scripts.ses_events clear dan@example.com --apply

CREDENTIALS. The ambient AWS profile/role, same as every other script here.
The database comes from ``archimedes.db`` (``DATABASE_URL``), same as the rest
of the backend.

Exit codes: ``0`` success, ``2`` a usage/configuration error, ``3`` ``clear``
found no such address.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Values written to ``auth_users.emailBounceKind``. Closed vocabulary — the
#: refusal in ``auth/auth.js`` maps each to its own error code, so adding one
#: here without adding it there produces a refusal with no reason attached.
KIND_BOUNCE = "bounce"
KIND_COMPLAINT = "complaint"

#: SES event type (``eventType`` on a configuration-set event, or
#: ``notificationType`` on the older identity-notification shape) → what it
#: means for the recipient. ``None`` means "seen and deliberately not recorded"
#: — see the table in the module docstring.
_KIND_BY_EVENT_TYPE: dict[str, str | None] = {
    "Bounce": KIND_BOUNCE,
    "Complaint": KIND_COMPLAINT,
    "Delivery": None,
    "Reject": None,
    "Send": None,
    "DeliveryDelay": None,
    "Subscription": None,
    "Open": None,
    "Click": None,
    "RenderingFailure": None,
}

#: The one bounce type that means "this mailbox does not exist". SES's other
#: two (``Transient``, ``Undetermined``) describe a real address we could not
#: reach right now.
_PERMANENT = "Permanent"

#: SQS hands back at most 10 messages per ReceiveMessage call.
_RECEIVE_BATCH = 10

#: Stop after this many messages in one run unless told otherwise, so an
#: unexpected flood cannot turn a drain into an unbounded job.
DEFAULT_MAX_MESSAGES = 1000

_AWS_REGION_ENV = ("AWS_REGION", "AWS_DEFAULT_REGION")


class QueueDrainFailed(RuntimeError):
    """The AWS call itself failed — never conflated with "nothing to do"."""


def _region() -> str | None:
    for name in _AWS_REGION_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _client():  # pragma: no cover - thin boto3 construction, stubbed in tests
    import boto3

    return boto3.client("sqs", region_name=_region())


def _default_session_factory():  # pragma: no cover - thin, injected in tests
    from archimedes.db import get_session

    return get_session()


# ────────────────────────────── parsing ─────────────────────────────────


@dataclass(frozen=True)
class SesEvent:
    """One SES feedback event, normalised out of whatever envelope carried it."""

    event_type: str
    #: ``bounce`` / ``complaint`` / ``None`` (seen, deliberately not recorded).
    kind: str | None
    recipients: tuple[str, ...]
    message_id: str | None = None
    occurred_at: datetime | None = None
    #: ``Permanent``/``Transient``/``Undetermined`` for a bounce, the feedback
    #: type for a complaint. Carried for the log line, never for a decision
    #: other than the permanence test below.
    detail: str | None = None


class MalformedEvent(ValueError):
    """The message body is not a SES event this module understands."""


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _addresses(entries: Any, key: str) -> tuple[str, ...]:
    if not isinstance(entries, list):
        return ()
    found = []
    for entry in entries:
        # A recipient list is normally [{"emailAddress": "..."}], but the
        # non-bounce envelopes carry bare strings; accept both.
        address = entry.get(key) if isinstance(entry, dict) else entry
        if isinstance(address, str) and address.strip():
            found.append(address.strip())
    return tuple(found)


def parse_message(body: str) -> SesEvent:
    """Normalise one SQS message body into a :class:`SesEvent`.

    Accepts BOTH shapes the queue can carry, because which one arrives is a
    single terraform boolean away (``raw_message_delivery`` on the SNS
    subscription, currently false):

      * the SNS envelope — ``{"Type": "Notification", "Message": "<json>"}`` —
        where the SES event is a JSON *string* inside ``Message``;
      * a bare SES event, which is what raw delivery would put on the queue.

    Raises :class:`MalformedEvent` rather than returning ``None`` for anything
    it cannot read. That distinction is load-bearing: an unparseable message
    must stay on the queue and end up in the dead-letter queue where somebody
    can see it, not be silently deleted as if it had been handled.
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise MalformedEvent(f"body is not JSON: {type(exc).__name__}") from exc

    if not isinstance(payload, dict):
        raise MalformedEvent("body is not a JSON object")

    # SNS envelope: the event is a JSON string under "Message".
    if "Message" in payload and "eventType" not in payload and "notificationType" not in payload:
        inner = payload.get("Message")
        if not isinstance(inner, str):
            raise MalformedEvent("SNS envelope carried no string Message")
        try:
            payload = json.loads(inner)
        except (TypeError, ValueError) as exc:
            raise MalformedEvent(f"SNS Message is not JSON: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise MalformedEvent("SNS Message is not a JSON object")

    # `eventType` is the configuration-set event-publishing name; the older
    # per-identity notification shape says `notificationType`. Same values.
    event_type = payload.get("eventType") or payload.get("notificationType")
    if not isinstance(event_type, str) or not event_type:
        raise MalformedEvent("no eventType/notificationType")

    mail = payload.get("mail") if isinstance(payload.get("mail"), dict) else {}
    message_id = mail.get("messageId") if isinstance(mail.get("messageId"), str) else None
    kind = _KIND_BY_EVENT_TYPE.get(event_type)

    if event_type == "Bounce":
        bounce = payload.get("bounce") if isinstance(payload.get("bounce"), dict) else {}
        detail = bounce.get("bounceType") if isinstance(bounce.get("bounceType"), str) else None
        # A non-permanent bounce is a real address we could not reach today.
        # It is parsed (so the drain can delete it and say what it saw) and
        # then recorded as nothing.
        if detail != _PERMANENT:
            kind = None
        return SesEvent(
            event_type=event_type,
            kind=kind,
            recipients=_addresses(bounce.get("bouncedRecipients"), "emailAddress"),
            message_id=message_id,
            occurred_at=_parse_timestamp(bounce.get("timestamp")) or _parse_timestamp(mail.get("timestamp")),
            detail=detail,
        )

    if event_type == "Complaint":
        complaint = payload.get("complaint") if isinstance(payload.get("complaint"), dict) else {}
        feedback = complaint.get("complaintFeedbackType")
        return SesEvent(
            event_type=event_type,
            kind=kind,
            recipients=_addresses(complaint.get("complainedRecipients"), "emailAddress"),
            message_id=message_id,
            occurred_at=_parse_timestamp(complaint.get("timestamp")) or _parse_timestamp(mail.get("timestamp")),
            detail=feedback if isinstance(feedback, str) else None,
        )

    # Everything else (Delivery, Reject, Send, …) records nothing; the
    # recipients come off the envelope purely so the log line is useful.
    return SesEvent(
        event_type=event_type,
        kind=None,
        recipients=_addresses(mail.get("destination"), "emailAddress"),
        message_id=message_id,
        occurred_at=_parse_timestamp(mail.get("timestamp")),
        detail=None,
    )


# ────────────────────────────── writing ─────────────────────────────────


@dataclass
class DrainSummary:
    """What one drain actually did. Every number is a fact, not an estimate."""

    received: int = 0
    #: Rows stamped for the first time.
    stamped: int = 0
    #: Recordable events whose user was already stamped — a redelivery, or a
    #: second bounce for the same address.
    already_stamped: int = 0
    #: Recordable events for an address with no ``auth_users`` row (a deleted
    #: account, or mail we sent somewhere that was never a user).
    no_user: int = 0
    #: Events deliberately not recorded — deliveries, rejects, transient
    #: bounces. See the table in the module docstring.
    ignored: int = 0
    #: Bodies that could not be parsed. NOT deleted; they go to the DLQ.
    malformed: int = 0
    deleted: int = 0
    dry_run: bool = False
    #: One line per malformed body, for the operator.
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "received": self.received,
            "stamped": self.stamped,
            "already_stamped": self.already_stamped,
            "no_user": self.no_user,
            "ignored": self.ignored,
            "malformed": self.malformed,
            "deleted": self.deleted,
            "dry_run": self.dry_run,
            "problems": list(self.problems),
        }


def _users_for(session, address: str):
    from sqlalchemy import func, select

    from archimedes.models.account import AuthUser

    # Case-insensitive: SES echoes the address as it appeared on the envelope,
    # which is not necessarily the case the row was stored in. Matching
    # case-sensitively would silently no-op on a mixed-case address — a
    # feedback loop that quietly records nothing is the failure mode this
    # module exists to remove.
    stmt = select(AuthUser).where(func.lower(AuthUser.email) == address.strip().lower())
    return list(session.execute(stmt).scalars())


def record_event(session, event: SesEvent, *, dry_run: bool = False) -> dict[str, int]:
    """Apply one parsed event to ``auth_users``. Returns per-outcome counts.

    First stamp wins: an address already carrying ``emailBouncedAt`` is left
    exactly as it was. That makes redelivery (SQS is at-least-once — a message
    CAN be handed out twice) a no-op rather than a rewrite, and it means an
    operator who has just cleared an address is not silently overwritten by an
    old event still sitting on the queue; a genuinely new bounce after a clear
    finds a NULL and stamps again.
    """
    outcome = {"stamped": 0, "already_stamped": 0, "no_user": 0, "ignored": 0}

    if event.kind is None:
        outcome["ignored"] += 1
        return outcome

    stamped_at = event.occurred_at or datetime.now(UTC)

    for address in event.recipients:
        users = _users_for(session, address)
        if not users:
            outcome["no_user"] += 1
            continue
        for user in users:
            if user.email_bounced_at is not None:
                outcome["already_stamped"] += 1
                continue
            outcome["stamped"] += 1
            if dry_run:
                continue
            user.email_bounced_at = stamped_at
            user.email_bounce_kind = event.kind

    if not event.recipients:
        # A Bounce/Complaint with no recipient list is well-formed JSON that
        # says nothing about anyone. Counted as ignored rather than dropped
        # into `problems`: there is no bug to chase and nothing to retry.
        outcome["ignored"] += 1

    return outcome


# ────────────────────────────── draining ────────────────────────────────


def drain(
    client,
    queue_url: str,
    *,
    session,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    dry_run: bool = False,
    wait_time_seconds: int = 0,
) -> DrainSummary:
    """Read the queue until it is empty (or ``max_messages`` is reached).

    A message is deleted only once it has been HANDLED — stamped, found to be
    a duplicate, found to name nobody, or deliberately ignored. A message that
    could not be parsed is left on the queue on purpose, so SQS's own
    ``maxReceiveCount`` eventually routes it to the dead-letter queue where an
    operator can look at it; deleting it would make an AWS schema change look
    exactly like a quiet week.
    """
    summary = DrainSummary(dry_run=dry_run)

    while summary.received < max_messages:
        remaining = max_messages - summary.received
        try:
            response = client.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=min(_RECEIVE_BATCH, remaining),
                WaitTimeSeconds=wait_time_seconds,
                MessageAttributeNames=["All"],
            )
        except Exception as exc:  # boto3 raises botocore exceptions; never guessed at
            raise QueueDrainFailed(f"ReceiveMessage failed: {type(exc).__name__}: {exc}") from exc

        messages = response.get("Messages") or []
        if not messages:
            break

        handled_receipts: list[str] = []
        for message in messages:
            summary.received += 1
            body = message.get("Body", "")
            try:
                event = parse_message(body)
            except MalformedEvent as exc:
                summary.malformed += 1
                summary.problems.append(f"{message.get('MessageId', '<no id>')}: {exc}")
                logger.warning(
                    "SES_EVENT_UNPARSEABLE message_id=%s reason=%s (left on the queue for the DLQ)",
                    message.get("MessageId", "<no id>"),
                    exc,
                )
                continue

            outcome = record_event(session, event, dry_run=dry_run)
            summary.stamped += outcome["stamped"]
            summary.already_stamped += outcome["already_stamped"]
            summary.no_user += outcome["no_user"]
            summary.ignored += outcome["ignored"]
            if outcome["stamped"]:
                logger.info(
                    "SES_EVENT_STAMPED type=%s detail=%s recipients=%d message_id=%s",
                    event.event_type,
                    event.detail or "—",
                    len(event.recipients),
                    event.message_id or "—",
                )
            handled_receipts.append(message["ReceiptHandle"])

        if dry_run:
            # Nothing was written and nothing is deleted, so the same messages
            # would come back on the next loop — stop after one batch rather
            # than spinning on them.
            break

        # Commit BEFORE deleting. If the process dies between the two, the
        # messages come back and `record_event` finds the rows already stamped
        # — a duplicate, which is a no-op. Deleting first would lose the event
        # if the write then failed, which is the one outcome that cannot be
        # recovered.
        session.commit()

        for receipt in handled_receipts:
            try:
                client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            except Exception as exc:
                raise QueueDrainFailed(f"DeleteMessage failed: {type(exc).__name__}: {exc}") from exc
            summary.deleted += 1

    return summary


def clear_address(session, address: str, *, apply: bool = False) -> dict[str, Any]:
    """Un-stamp ONE address so signup and resend stop refusing it.

    Dry run unless ``apply`` — the same shape ``ses_suppression remove`` uses,
    for the same reason: this is the manual override on an automated signal,
    and the legitimate cases are narrow (see the runbook). It does NOT touch
    the SES account suppression list; ``ses_suppression remove`` does that, and
    both are needed for the address to work again.
    """
    users = _users_for(session, address)
    if not users:
        return {"address": address, "found": False, "applied": False, "cleared": 0}

    stamped = [user for user in users if user.email_bounced_at is not None]
    if not apply:
        return {
            "address": address,
            "found": True,
            "applied": False,
            "cleared": len(stamped),
            "kinds": [user.email_bounce_kind for user in stamped],
        }

    for user in stamped:
        user.email_bounced_at = None
        user.email_bounce_kind = None
    session.commit()
    return {"address": address, "found": True, "applied": True, "cleared": len(stamped)}


# ──────────────────────────────── CLI ───────────────────────────────────


def render_summary(summary: DrainSummary) -> str:
    lines = [
        ("DRY RUN — nothing was written and nothing was deleted." if summary.dry_run else "drain complete."),
        f"  received        {summary.received}",
        f"  stamped         {summary.stamped}",
        f"  already stamped {summary.already_stamped}",
        f"  no such user    {summary.no_user}",
        f"  not recorded    {summary.ignored}   (deliveries, rejects, transient bounces)",
        f"  unparseable     {summary.malformed}   (left on the queue -> dead-letter queue)",
        f"  deleted         {summary.deleted}",
    ]
    lines.extend(f"  ! {problem}" for problem in summary.problems)
    return "\n".join(lines)


def render_clear(result: dict[str, Any]) -> str:
    address = result["address"]
    if not result["found"]:
        return f"no account uses {address} — nothing to clear"
    if result["applied"]:
        return (
            f"cleared the bounce stamp on {address} ({result['cleared']} row(s)).\n"
            "This does NOT remove the address from the SES account suppression list — run\n"
            "`python -m archimedes.scripts.ses_suppression remove <address> --apply` too, or\n"
            "mail to it will still be accepted and then dropped inside AWS."
        )
    if not result["cleared"]:
        return f"{address} carries no bounce stamp — nothing to clear"
    return (
        f"DRY RUN — would clear the bounce stamp on {address} "
        f"({result['cleared']} row(s), kind {', '.join(k or '—' for k in result.get('kinds', []))}).\n"
        "Nothing was changed. Re-run with --apply only if the address's owner has confirmed it is\n"
        "real and reachable; see docs/runbooks/ses-bounce-signal.md."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses_events",
        description=(
            "Drain the SES bounce/complaint feedback queue onto auth_users, and clear one "
            "address's stamp by hand. See docs/runbooks/ses-bounce-signal.md."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output.")
    sub = parser.add_subparsers(dest="command", required=True)

    draining = sub.add_parser("drain", help="Read the queue and stamp bounced/complained addresses.")
    draining.add_argument(
        "--queue-url",
        default=os.environ.get("SES_EVENTS_QUEUE_URL", ""),
        help="SQS queue URL. Defaults to $SES_EVENTS_QUEUE_URL (set on the backend task).",
    )
    draining.add_argument(
        "--max-messages",
        type=int,
        default=DEFAULT_MAX_MESSAGES,
        help=f"Stop after this many messages (default {DEFAULT_MAX_MESSAGES}).",
    )
    draining.add_argument(
        "--wait-time-seconds",
        type=int,
        default=0,
        help="SQS long-poll seconds per receive (0-20, default 0).",
    )
    draining.add_argument(
        "--dry-run",
        action="store_true",
        help="Read one batch, report what it would do, write nothing and delete nothing.",
    )

    clearing = sub.add_parser("clear", help="Un-stamp ONE address. Dry run unless --apply.")
    clearing.add_argument("address")
    clearing.add_argument(
        "--apply",
        action="store_true",
        help="Actually clear the stamp. Without this nothing is changed.",
    )
    return parser


def main(argv: list[str] | None = None, *, client=None, session=None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)

    owns_session = session is None
    db = session if session is not None else _default_session_factory()
    try:
        if args.command == "drain":
            if not args.queue_url:
                print(
                    "error: no queue URL. Pass --queue-url or set SES_EVENTS_QUEUE_URL "
                    "(terraform output ses_events_queue_url).",
                    file=sys.stderr,
                )
                return 2
            if args.max_messages <= 0:
                print("error: --max-messages must be positive", file=sys.stderr)
                return 2
            if not 0 <= args.wait_time_seconds <= 20:
                print("error: --wait-time-seconds must be between 0 and 20", file=sys.stderr)
                return 2
            summary = drain(
                client or _client(),
                args.queue_url,
                session=db,
                max_messages=args.max_messages,
                dry_run=args.dry_run,
                wait_time_seconds=args.wait_time_seconds,
            )
            print(json.dumps(summary.as_dict(), indent=2) if args.json else render_summary(summary))
            return 0

        result = clear_address(db, args.address, apply=args.apply)
        print(json.dumps(result, indent=2) if args.json else render_clear(result))
        return 0 if result["found"] else 3
    except QueueDrainFailed as exc:
        # A failed AWS call is never rendered as "the queue was empty" — the
        # whole reason this loop exists is that a silent failure looks like
        # good news.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if owns_session:
            db.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
