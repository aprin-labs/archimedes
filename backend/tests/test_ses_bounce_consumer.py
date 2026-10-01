"""The SES feedback consumer: what it records, what it refuses to record (#1804).

``archimedes.scripts.ses_events`` is the read side of the push loop
``infra/ses_events.tf`` builds — SES → configuration set → SNS → SQS → here →
``auth_users.emailBouncedAt``. Everything downstream (the signup and resend
refusals in ``auth/auth.js``) trusts the rows this module writes, so the tests
that matter are the ones about what it must NOT write:

* a **transient** bounce is a real person with a full mailbox — stamping it
  would lock out exactly the user this issue exists to stop locking out;
* a **delivery** or a **reject** is not a bounce at all;
* an **unparseable** body must stay on the queue, so SQS's own redrive policy
  can put it in the dead-letter queue where somebody sees it. Deleting it would
  make an AWS schema change indistinguishable from a quiet week.

Hermetic: an in-memory SQLite database built from the ORM models and a stub SQS
client that hands back canned message batches. No AWS, no network, no boto3.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from archimedes.models.account import AuthUser
from archimedes.models.chat import Base
from archimedes.scripts import ses_events
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/037613907429/archimedes-ses-events"


# ────────────────────────────── fixtures ────────────────────────────────


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def as_utc(value: datetime) -> datetime:
    """SQLite has no timezone type, so a round-tripped value comes back naive.

    Production is Postgres (``timestamptz``) and keeps the offset. The value
    written is always UTC either way — this only re-attaches the label SQLite
    dropped, so the assertion below is about the instant, not the storage
    engine.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def make_user(db, email: str, user_id: str = "user-1") -> AuthUser:
    now = datetime.now(UTC)
    user = AuthUser(
        id=user_id,
        name=email.split("@", 1)[0],
        email=email,
        email_verified=False,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.commit()
    return user


class StubSqs:
    """Hands back canned batches, records every delete. Never touches AWS."""

    def __init__(
        self,
        batches: list[list[dict]] | None = None,
        *,
        receive_error: Exception | None = None,
        delete_error: Exception | None = None,
    ):
        self.batches = list(batches or [])
        self.deleted: list[str] = []
        self.receive_error = receive_error
        self.delete_error = delete_error
        self.receive_calls = 0

    def receive_message(self, **_kwargs):
        self.receive_calls += 1
        if self.receive_error is not None:
            raise self.receive_error
        if not self.batches:
            return {}
        return {"Messages": self.batches.pop(0)}

    def delete_message(self, *, QueueUrl, ReceiptHandle):  # boto3's own kwarg names
        assert QueueUrl == QUEUE_URL
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(ReceiptHandle)


def sns_message(event: dict, *, message_id: str = "m-1", receipt: str = "r-1") -> dict:
    """One SQS message carrying the SNS envelope shape the queue gets today."""
    return {
        "MessageId": message_id,
        "ReceiptHandle": receipt,
        "Body": json.dumps(
            {
                "Type": "Notification",
                "TopicArn": "arn:aws:sns:us-east-1:037613907429:archimedes-ses-events",
                "Message": json.dumps(event),
            }
        ),
    }


def bounce_event(address: str, *, bounce_type: str = "Permanent", timestamp: str = "2026-09-02T10:00:00.000Z") -> dict:
    return {
        "eventType": "Bounce",
        "mail": {"messageId": "0100018f", "destination": [address], "timestamp": timestamp},
        "bounce": {
            "bounceType": bounce_type,
            "bounceSubType": "General",
            "bouncedRecipients": [{"emailAddress": address, "action": "failed"}],
            "timestamp": timestamp,
        },
    }


def complaint_event(address: str, *, timestamp: str = "2026-09-02T11:00:00.000Z") -> dict:
    return {
        "eventType": "Complaint",
        "mail": {"messageId": "0100019a", "destination": [address], "timestamp": timestamp},
        "complaint": {
            "complainedRecipients": [{"emailAddress": address}],
            "complaintFeedbackType": "abuse",
            "timestamp": timestamp,
        },
    }


def delivery_event(address: str) -> dict:
    return {
        "eventType": "Delivery",
        "mail": {"messageId": "010001aa", "destination": [address], "timestamp": "2026-09-02T09:00:00.000Z"},
        "delivery": {"recipients": [address], "timestamp": "2026-09-02T09:00:00.000Z"},
    }


# ───────────────────────────── the loop works ───────────────────────────


def test_permanent_bounce_stamps_the_user_and_deletes_the_message(session):
    make_user(session, "ghost@example.invalid")
    client = StubSqs([[sns_message(bounce_event("ghost@example.invalid"))]])

    summary = ses_events.drain(client, QUEUE_URL, session=session)

    user = session.query(AuthUser).one()
    assert user.email_bounced_at is not None
    assert user.email_bounce_kind == ses_events.KIND_BOUNCE
    # The timestamp is SES's, not "now" — the row says when the mailbox
    # refused us, which is the fact an operator needs.
    assert as_utc(user.email_bounced_at) == datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
    assert (summary.stamped, summary.deleted, summary.malformed) == (1, 1, 0)
    assert client.deleted == ["r-1"]


def test_complaint_stamps_a_complaint_not_a_bounce(session):
    make_user(session, "annoyed@example.com")
    client = StubSqs([[sns_message(complaint_event("annoyed@example.com"))]])

    ses_events.drain(client, QUEUE_URL, session=session)

    user = session.query(AuthUser).one()
    assert user.email_bounce_kind == ses_events.KIND_COMPLAINT


def test_raw_message_delivery_shape_is_accepted_too(session):
    """`raw_message_delivery` on the SNS subscription is one boolean away.

    The queue carries the SNS envelope today; flipping that flag would put the
    bare SES event on the queue instead. The parser reads both, so the flag
    cannot silently become a total outage of the feedback loop.
    """
    make_user(session, "ghost@example.invalid")
    raw = {"MessageId": "m-raw", "ReceiptHandle": "r-raw", "Body": json.dumps(bounce_event("ghost@example.invalid"))}
    client = StubSqs([[raw]])

    ses_events.drain(client, QUEUE_URL, session=session)

    assert session.query(AuthUser).one().email_bounce_kind == ses_events.KIND_BOUNCE


def test_recipient_address_matches_case_insensitively(session):
    """SES echoes the envelope's casing; the row holds whatever signup stored."""
    make_user(session, "ghost@example.invalid")
    client = StubSqs([[sns_message(bounce_event("Ghost@Example.INVALID"))]])

    summary = ses_events.drain(client, QUEUE_URL, session=session)

    assert summary.stamped == 1
    assert session.query(AuthUser).one().email_bounced_at is not None


def test_multiple_batches_are_drained_until_the_queue_is_empty(session):
    make_user(session, "one@example.invalid", user_id="user-1")
    make_user(session, "two@example.invalid", user_id="user-2")
    client = StubSqs(
        [
            [sns_message(bounce_event("one@example.invalid"), message_id="m-1", receipt="r-1")],
            [sns_message(bounce_event("two@example.invalid"), message_id="m-2", receipt="r-2")],
        ]
    )

    summary = ses_events.drain(client, QUEUE_URL, session=session)

    assert summary.stamped == 2
    assert client.deleted == ["r-1", "r-2"]


# ─────────────────────── what it refuses to record ──────────────────────


@pytest.mark.parametrize("bounce_type", ["Transient", "Undetermined"])
def test_non_permanent_bounce_records_nothing(session, bounce_type):
    """A full mailbox is not a fake address.

    This is the case the whole issue is about, pointed the other way: stamping
    a transient bounce would lock a real user out of the free tier over a
    temporary failure, with the same silence and the same dead end.
    """
    make_user(session, "real@example.com")
    client = StubSqs([[sns_message(bounce_event("real@example.com", bounce_type=bounce_type))]])

    summary = ses_events.drain(client, QUEUE_URL, session=session)

    user = session.query(AuthUser).one()
    assert user.email_bounced_at is None
    assert user.email_bounce_kind is None
    assert (summary.stamped, summary.ignored) == (0, 1)
    # Still deleted: it was handled, and leaving it would replay forever.
    assert client.deleted == ["r-1"]


def test_delivery_events_record_nothing(session):
    make_user(session, "fine@example.com")
    client = StubSqs([[sns_message(delivery_event("fine@example.com"))]])

    summary = ses_events.drain(client, QUEUE_URL, session=session)

    assert session.query(AuthUser).one().email_bounced_at is None
    assert (summary.stamped, summary.ignored, summary.deleted) == (0, 1, 1)


def test_an_unparseable_body_is_left_on_the_queue(session):
    """Left for the dead-letter queue, deliberately — see the module docstring."""
    client = StubSqs([[{"MessageId": "m-bad", "ReceiptHandle": "r-bad", "Body": "not json at all"}]])

    summary = ses_events.drain(client, QUEUE_URL, session=session)

    assert summary.malformed == 1
    assert summary.deleted == 0
    assert client.deleted == []
    assert "m-bad" in summary.problems[0]


def test_an_event_with_no_event_type_is_unparseable(session):
    body = json.dumps({"Type": "Notification", "Message": json.dumps({"mail": {"destination": ["a@b.com"]}})})
    client = StubSqs([[{"MessageId": "m-x", "ReceiptHandle": "r-x", "Body": body}]])

    summary = ses_events.drain(client, QUEUE_URL, session=session)

    assert (summary.malformed, summary.deleted) == (1, 0)


def test_bounce_for_an_address_with_no_account_is_handled_not_stamped(session):
    client = StubSqs([[sns_message(bounce_event("stranger@example.invalid"))]])

    summary = ses_events.drain(client, QUEUE_URL, session=session)

    assert (summary.stamped, summary.no_user, summary.deleted) == (0, 1, 1)


def test_redelivery_does_not_rewrite_an_existing_stamp(session):
    """SQS is at-least-once. The second copy must be a no-op, not a rewrite."""
    make_user(session, "ghost@example.invalid")
    first = StubSqs([[sns_message(bounce_event("ghost@example.invalid"))]])
    ses_events.drain(first, QUEUE_URL, session=session)
    stamped_at = session.query(AuthUser).one().email_bounced_at

    later = StubSqs([[sns_message(bounce_event("ghost@example.invalid", timestamp="2026-09-03T10:00:00.000Z"))]])
    summary = ses_events.drain(later, QUEUE_URL, session=session)

    user = session.query(AuthUser).one()
    assert user.email_bounced_at == stamped_at
    assert (summary.stamped, summary.already_stamped) == (0, 1)


def test_a_receive_failure_is_raised_not_reported_as_an_empty_queue(session):
    """The failure mode this whole issue is about is silence that looks like success."""
    client = StubSqs(receive_error=RuntimeError("AccessDenied"))

    with pytest.raises(ses_events.QueueDrainFailed) as excinfo:
        ses_events.drain(client, QUEUE_URL, session=session)

    assert "ReceiveMessage failed" in str(excinfo.value)


# ──────────────────────────── dry run + clear ───────────────────────────


def test_dry_run_writes_nothing_and_deletes_nothing(session):
    make_user(session, "ghost@example.invalid")
    client = StubSqs([[sns_message(bounce_event("ghost@example.invalid"))]])

    summary = ses_events.drain(client, QUEUE_URL, session=session, dry_run=True)

    assert session.query(AuthUser).one().email_bounced_at is None
    assert client.deleted == []
    assert (summary.stamped, summary.deleted, summary.dry_run) == (1, 0, True)


def test_clear_is_a_dry_run_unless_applied(session):
    user = make_user(session, "ghost@example.invalid")
    user.email_bounced_at = datetime.now(UTC)
    user.email_bounce_kind = ses_events.KIND_BOUNCE
    session.commit()

    preview = ses_events.clear_address(session, "ghost@example.invalid")
    assert (preview["found"], preview["applied"], preview["cleared"]) == (True, False, 1)
    assert session.query(AuthUser).one().email_bounced_at is not None

    applied = ses_events.clear_address(session, "ghost@example.invalid", apply=True)
    assert (applied["applied"], applied["cleared"]) == (True, 1)
    cleared = session.query(AuthUser).one()
    assert cleared.email_bounced_at is None
    assert cleared.email_bounce_kind is None


def test_clear_reports_an_unknown_address_rather_than_claiming_success(session):
    result = ses_events.clear_address(session, "nobody@example.com", apply=True)
    assert result == {"address": "nobody@example.com", "found": False, "applied": False, "cleared": 0}


def test_the_write_is_committed_before_the_delete_so_a_crash_loses_nothing(session):
    """The ordering claim in ``drain``, executed rather than asserted in a comment.

    SQS is at-least-once and the delete is a SEPARATE call from the write, so
    one of the two has to go first and the choice is not symmetric:

    * commit → delete (what the code does). A crash in between leaves the row
      stamped and the message on the queue; it comes back, ``record_event``
      finds ``email_bounced_at`` already set, counts it ``already_stamped``,
      and deletes it. A duplicate, which is a no-op.
    * delete → commit. A crash in between loses the event permanently — the
      only outcome nothing can recover, because SES will never resend it and
      the address's death was published nowhere else.

    A failing ``DeleteMessage`` is the observable stand-in for that crash. The
    stamp must already be DURABLE at that point, which is what the rollback
    below tests: a rollback discards everything not yet committed, so a row
    that survives it was committed before the delete was attempted.
    """
    make_user(session, "gone@example.invalid")
    client = StubSqs(
        [[sns_message(bounce_event("gone@example.invalid"))]],
        delete_error=RuntimeError("AWS.SimpleQueueService.NonExistentQueue"),
    )

    with pytest.raises(ses_events.QueueDrainFailed):
        ses_events.drain(client, QUEUE_URL, session=session)

    session.rollback()
    stamped = session.query(AuthUser).one()
    assert stamped.email_bounced_at is not None, (
        "the stamp was still uncommitted when the delete was attempted — a crash there loses the bounce"
    )
    assert stamped.email_bounce_kind == ses_events.KIND_BOUNCE
    assert client.deleted == []


# ──────────────────────────────── the CLI ───────────────────────────────


def test_drain_without_a_queue_url_is_a_usage_error(session, capsys, monkeypatch):
    monkeypatch.delenv("SES_EVENTS_QUEUE_URL", raising=False)
    code = ses_events.main(["drain"], client=StubSqs(), session=session)
    assert code == 2
    assert "no queue URL" in capsys.readouterr().err


def test_clear_exits_3_for_an_unknown_address(session):
    assert ses_events.main(["clear", "nobody@example.com"], client=StubSqs(), session=session) == 3
