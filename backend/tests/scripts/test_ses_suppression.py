"""SES suppression-list operator tooling (#1748 item 4).

WHAT THIS GUARDS. Dogfooding signup with throwaway addresses bounced them onto
the AWS account's suppression list, and a suppressed address is invisible to
the sender: ``SendEmail`` succeeds, returns a MessageId, and the message is
dropped. The trap is a REAL address that was once suppressed staying blocked
forever. The script exists to clear ONE such address; the danger is that the
same tool, pointed at the whole list, destroys sender reputation for every real
user. So the properties tested here are, in order of importance:

  1. ``remove`` changes nothing without ``--apply``. Demonstrated by asserting
     the stub client recorded ZERO delete calls, not by reading the flag back.
  2. There is no bulk mode. ``remove`` takes exactly one address and the parser
     refuses two; no subcommand or flag deletes more than one entry.
  3. A failed AWS call is never rendered as "not suppressed". Silence looking
     like success is the entire failure mode this tool addresses.

Hermetic: a hand-written stub client with the three SESv2 methods the script
calls. No boto3, no credentials, no network — ``main(client=...)`` is the seam.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from archimedes.scripts import ses_suppression


class NotFoundException(Exception):
    """Same CLASS NAME botocore generates for SESv2's not-found error.

    ``ses_suppression._not_found`` classifies by class name precisely so this
    stub travels the same code path production does without importing
    botocore's exception factory.
    """


class ThrottlingException(Exception):
    """An AWS failure that is emphatically NOT "the address is clean"."""


class StubSes:
    def __init__(self, suppressed: dict[str, dict] | None = None, raises: Exception | None = None):
        self.suppressed = dict(suppressed or {})
        self.raises = raises
        self.deleted: list[str] = []
        self.listed: list[dict] = []

    def list_suppressed_destinations(self, **request):
        if self.raises:
            raise self.raises
        self.listed.append(request)
        reasons = request.get("Reasons")
        summaries = [
            {
                "EmailAddress": email,
                "Reason": entry["Reason"],
                "LastUpdateTime": entry["LastUpdateTime"],
            }
            for email, entry in sorted(self.suppressed.items())
            if not reasons or entry["Reason"] in reasons
        ]
        page_size = request.get("PageSize", 100)
        start = int(request.get("NextToken") or 0)
        page = summaries[start : start + page_size]
        next_token = str(start + page_size) if start + page_size < len(summaries) else None
        response = {"SuppressedDestinationSummaries": page}
        if next_token:
            response["NextToken"] = next_token
        return response

    def get_suppressed_destination(self, EmailAddress: str):  # boto3's own kwarg name
        if self.raises:
            raise self.raises
        entry = self.suppressed.get(EmailAddress)
        if entry is None:
            raise NotFoundException(EmailAddress)
        return {"SuppressedDestination": {"EmailAddress": EmailAddress, **entry}}

    def delete_suppressed_destination(self, EmailAddress: str):  # boto3's own kwarg name
        if self.raises:
            raise self.raises
        if EmailAddress not in self.suppressed:
            raise NotFoundException(EmailAddress)
        self.deleted.append(EmailAddress)
        del self.suppressed[EmailAddress]
        return {}


def _entry(reason: str = "BOUNCE") -> dict:
    return {"Reason": reason, "LastUpdateTime": datetime(2026, 8, 30, 10, 0, tzinfo=UTC)}


# ── 1. the dry-run default is the guard ──────────────────────────────────


def test_remove_without_apply_deletes_nothing_and_says_what_it_would_do(capsys):
    """Demonstrated to reject: this test is the reason ``--apply`` cannot
    become a no-op. It asserts on the stub's recorded calls, so making
    ``remove_suppressed`` delete unconditionally fails it immediately."""
    client = StubSes({"real@example.com": _entry("BOUNCE")})

    exit_code = ses_suppression.main(["remove", "real@example.com"], client=client)

    assert exit_code == 0
    assert client.deleted == [], "a dry run issued a DeleteSuppressedDestination"
    assert "real@example.com" in client.suppressed, "the entry was removed during a dry run"
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "--apply" in out
    assert "Nothing was changed" in out
    # The dry run must not claim the removal happened.
    assert "removed real@example.com" not in out


def test_apply_is_the_only_thing_that_deletes(capsys):
    client = StubSes({"real@example.com": _entry("BOUNCE")})

    exit_code = ses_suppression.main(["remove", "real@example.com", "--apply"], client=client)

    assert exit_code == 0
    assert client.deleted == ["real@example.com"]
    assert "real@example.com" not in client.suppressed
    out = capsys.readouterr().out
    assert out.startswith("removed real@example.com")
    assert "BOUNCE" in out


def test_removing_an_address_that_is_not_suppressed_reports_it_and_exits_3(capsys):
    client = StubSes({"other@example.com": _entry()})

    exit_code = ses_suppression.main(["remove", "absent@example.com", "--apply"], client=client)

    assert exit_code == 3
    assert client.deleted == []
    assert "not on the suppression list" in capsys.readouterr().out


# ── 2. there is no bulk mode ─────────────────────────────────────────────


def test_remove_takes_exactly_one_address_and_no_bulk_flag_exists():
    """The absence of a bulk clear is a design decision, not an oversight —
    re-sending to every address that ever bounced is how sender reputation
    (and delivery for every real user) is destroyed. Pinned so a later
    convenience flag has to delete this test on purpose."""
    parser = ses_suppression.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["remove", "a@example.com", "b@example.com"])
    for forbidden in (["remove", "--all"], ["remove", "--all", "--apply"], ["clear"], ["purge"]):
        with pytest.raises(SystemExit):
            parser.parse_args(forbidden)

    help_text = parser.format_help()
    assert "--all" not in help_text, "a bulk flag appeared in the CLI surface"
    assert "no bulk-clear" in help_text, "the CLI must state that the absence of a bulk mode is deliberate"


def test_one_apply_run_deletes_exactly_one_entry_even_with_a_long_list():
    client = StubSes({f"bounced-{index}@example.invalid": _entry() for index in range(10)})

    ses_suppression.main(["remove", "bounced-3@example.invalid", "--apply"], client=client)

    assert client.deleted == ["bounced-3@example.invalid"]
    assert len(client.suppressed) == 9


# ── 3. a failed lookup is never "clean" ──────────────────────────────────


def test_a_failed_aws_call_exits_nonzero_and_never_reports_the_address_as_clean(capsys):
    client = StubSes({"real@example.com": _entry()}, raises=ThrottlingException("slow down"))

    exit_code = ses_suppression.main(["check", "real@example.com"], client=client)

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "ThrottlingException" in captured.err
    assert "not on the suppression list" not in captured.out
    assert client.deleted == []


def test_a_failed_lookup_during_remove_aborts_before_any_delete():
    client = StubSes({"real@example.com": _entry()}, raises=ThrottlingException("slow down"))

    assert ses_suppression.main(["remove", "real@example.com", "--apply"], client=client) == 2
    assert client.deleted == []


def test_check_distinguishes_suppressed_from_absent_by_exit_code(capsys):
    client = StubSes({"bounced@example.invalid": _entry("COMPLAINT")})

    assert ses_suppression.main(["check", "bounced@example.invalid"], client=client) == 0
    assert "COMPLAINT" in capsys.readouterr().out

    assert ses_suppression.main(["check", "clean@example.com"], client=client) == 3
    assert "not on the suppression list" in capsys.readouterr().out


# ── the read side ────────────────────────────────────────────────────────


def test_list_is_read_only_and_paginates_past_the_first_page():
    client = StubSes({f"bounced-{index:03d}@example.invalid": _entry() for index in range(150)})

    entries = ses_suppression.list_suppressed(client, limit=150)

    assert len(entries) == 150
    assert len(client.listed) > 1, "150 entries must have taken more than one page"
    assert client.deleted == []
    assert entries[0]["reason"] == "BOUNCE"
    assert entries[0]["last_update"] == "2026-08-30T10:00:00+00:00"


def test_list_respects_the_limit_and_the_reason_filter():
    client = StubSes(
        {
            "b1@example.invalid": _entry("BOUNCE"),
            "b2@example.invalid": _entry("BOUNCE"),
            "c1@example.invalid": _entry("COMPLAINT"),
        }
    )

    assert len(ses_suppression.list_suppressed(client, limit=1)) == 1
    complaints = ses_suppression.list_suppressed(client, reasons=("COMPLAINT",))
    assert [entry["email"] for entry in complaints] == ["c1@example.invalid"]
    assert client.deleted == []


def test_json_output_is_machine_readable_for_every_command(capsys):
    client = StubSes({"bounced@example.invalid": _entry()})

    ses_suppression.main(["--json", "list"], client=client)
    listed = json.loads(capsys.readouterr().out)
    assert listed == [
        {"email": "bounced@example.invalid", "reason": "BOUNCE", "last_update": "2026-08-30T10:00:00+00:00"}
    ]

    ses_suppression.main(["--json", "remove", "bounced@example.invalid"], client=client)
    dry = json.loads(capsys.readouterr().out)
    assert dry["found"] is True and dry["applied"] is False
    assert client.deleted == []

    ses_suppression.main(["--json", "check", "nobody@example.com"], client=client)
    assert json.loads(capsys.readouterr().out) == {"email": "nobody@example.com", "suppressed": False}


def test_an_empty_list_says_so_rather_than_printing_nothing(capsys):
    assert ses_suppression.main(["list"], client=StubSes()) == 0
    assert "suppression list is empty" in capsys.readouterr().out


def test_a_delete_that_races_another_removal_does_not_claim_success(capsys):
    """The entry is there when we look and gone when we delete. Reporting
    "removed" would be a claim this run cannot back."""

    class RacingStub(StubSes):
        def delete_suppressed_destination(self, EmailAddress: str):  # boto3's own kwarg name
            raise NotFoundException(EmailAddress)

    client = RacingStub({"real@example.com": _entry()})

    exit_code = ses_suppression.main(["remove", "real@example.com", "--apply"], client=client)

    assert exit_code == 3
    assert "not on the suppression list" in capsys.readouterr().out


def test_the_runbook_this_script_points_at_exists_and_names_the_apply_flag():
    """The dry-run output tells the operator to read a runbook before using
    --apply. A pointer at a file that does not exist is worse than none."""
    from pathlib import Path

    repo_root = Path(ses_suppression.__file__).resolve().parents[3]
    runbook = repo_root / "docs" / "runbooks" / "ses-suppression.md"
    assert runbook.exists(), f"missing {runbook}"
    text = runbook.read_text(encoding="utf-8")
    assert "--apply" in text
    assert "archimedes.scripts.ses_suppression" in text
    # The rule the whole runbook exists to state.
    assert "never bulk-clear" in text.lower()
