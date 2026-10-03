"""Mail the owner a weekly DMARC aggregate-report summary (#1504).

WHY THIS EXISTS. `infra/dmarc_reports.tf` collects DMARC aggregate reports into
a private bucket, and `scripts/dmarc_report_summary.py` turns a pile of them
into a per-source-IP pass/fail table. Both of those are inert until somebody
*looks*. The owner's call on 2026-09-03 was explicit — "the aggregate-report
parser runs on a schedule and posts a weekly summary (per-source pass/fail
table)" — and the reason is the shape of this whole issue: a bucket nobody
reads and a bucket nothing writes to are the same bucket from the outside.

The decision this feeds is #1504's own: moving `_dmarc.archimedes-arc.com` off
``p=none``. That decision needs a fortnight of enumerated sources, and a
fortnight of evidence only accumulates if somebody is reading it weekly rather
than discovering the backlog on the day they want to change the policy.

THE EMAIL IS THE HEARTBEAT, WHICH IS WHY A QUIET WEEK STILL SENDS ONE. If no
reports landed, this job mails a one-line "no reports received" summary and
exits 0. That is not a formality:

  * an empty bucket and an un-spoofed domain look identical, and only one of
    them is good news;
  * a job that sends nothing on a quiet week is indistinguishable from a job
    that has stopped running, a task role that lost its S3 grant, or a schedule
    somebody disabled — and every one of those is the failure this issue is
    about, arriving silently.

So the arrival of the message is itself the signal that the collection pipeline
is alive, and its ABSENCE is the alarm. That is deliberate, and it is the
reason there is no CloudWatch alarm on this task: the owner reading (or not
reading) a Monday email is the monitor, and adding a second, weaker one that
fires on task exit code would just be another thing to mute. The residual is
stated in docs/runbooks/dmarc-reports.md § 'The weekly summary'.

AND A WINDOW OF UNREADABLE OBJECTS IS A THIRD THING, NOT A QUIET WEEK. Objects
can land and every one of them fail to parse — a zip over MAX_ARCHIVE_MEMBERS,
a bomb, an attachment that is not a report. That is neither "nothing arrived"
nor "everything is clean": it says the collection path WORKS and the contents
do not, which is a different diagnosis with a different runbook section. So
:func:`subject_line` says ``NO READABLE REPORTS (n unreadable)``, and both
bodies name every failure, rather than rendering the window as
``NO REPORTS RECEIVED`` and pointing at an arrival ladder that is not the
fault. The operator CLI has always printed those errors; this is the mail
catching up with it.

WHAT IT SENDS. Two bodies in one message:

  * **text/plain** — the OUTPUT OF ``render_table`` VERBATIM, the same
    rendering `scripts/dmarc_report_summary.py` prints. Not a re-implementation:
    if the emailed table and the operator's table could disagree, the weekly
    mail would be a second opinion rather than a report, and the runbook's
    "run §2 by hand to check" would stop being a check.
  * **text/html** — the same numbers as a table, for reading on a phone. Same
    rows, same changes, and the same UNREADABLE list: the HTML part is what
    nearly every mail client displays, so anything that reaches only the text
    part is something the owner does not see.

Plus, in both, what changed since the week before: source IPs that appear this
week and did not last week. A new source claiming ``From: @archimedes-arc.com``
is the event worth a Monday morning; a steady list is not.

Both bodies are bounded (:data:`MAX_BODY_BYTES`, and
`dmarc_reports.MAX_CELL_CHARS` per cell) because every cell in them arrives
from the public internet — see those constants for the amplification that
makes an unbounded render a message SES refuses to send.

EXIT CODES. ``0`` sent · ``2`` misconfigured (no bucket, no recipient) · ``3``
could not read the bucket · ``4`` could not send. 3 and 4 are deliberately
distinct from 0 AND from each other: "I could not look" and "I looked and found
nothing" are different facts, and only one of them is about DMARC. "I looked, I
found things, and I could not read any of them" is a third, and it lives in the
subject line rather than an exit code — the run itself succeeded. Nothing here
ever exits 0 without a message having been accepted by SES.

WHAT SES ACCEPTING THE MESSAGE DOES NOT MEAN. It does not mean delivered — SES
returns a MessageId for an address on the account suppression list and then
drops the mail (`docs/runbooks/ses-suppression.md`). The address this sends to
is `var.owner_alert_email`, which is also a confirmed SNS subscription on the
alerts topic, so it is an address the owner demonstrably receives at; but a 0
from this job is "SES took it", not "the owner read it".

RUNNING IT BY HAND::

    # In the backend image, on the schedule's own task definition:
    aws ecs run-task --cluster archimedes-cluster \\
        --task-definition archimedes-dmarc-weekly-summary --launch-type FARGATE ...

    # From an operator shell, against the live bucket, sending nothing:
    PYTHONPATH=backend python -m archimedes.scripts.dmarc_weekly_summary \\
        --bucket "$(terraform -chdir=infra output -raw dmarc_reports_bucket)" \\
        --to you@example.com --dry-run

The full invocation, and what a healthy summary looks like, are in
docs/runbooks/dmarc-reports.md § 'The weekly summary'.
"""

from __future__ import annotations

import argparse
import html
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from archimedes.scripts.dmarc_reports import (
    MAX_NOTE_CHARS,
    StoredObject,
    Summary,
    build_summary,
    clip_cell,
    format_utc,
    iter_s3_objects,
    render_table,
)

#: Where the reports are. Set by `infra/dmarc_reports.tf` off the bucket
#: resource itself, never a literal — the same one-name-one-place rule
#: `infra/ses_events.tf` follows for SES_EVENTS_QUEUE_URL.
BUCKET_ENV = "DMARC_REPORTS_BUCKET"

#: The key prefix `aws_ses_receipt_rule.dmarc_reports`'s `s3_action` writes
#: under. Same default as the operator CLI's `--prefix`; a mismatch here reads
#: as an empty bucket, which is the one symptom this whole file is about.
PREFIX_ENV = "DMARC_REPORTS_PREFIX"
DEFAULT_PREFIX = "reports/"

#: Who gets the summary. `infra/dmarc_reports.tf` sets it to
#: `var.owner_alert_email` — the address #1818 P5 established the owner
#: actually reads, rather than the one alarms were already going to.
RECIPIENT_ENV = "DMARC_SUMMARY_TO"

#: Who it is from. Same variable name and same default as `auth/mailer.js`,
#: because it is the same identity: the verified `archimedes-arc.com` domain
#: the verification and password-reset mail already goes out as, which is what
#: `aws_iam_role_policy.ecs_task_ses_send` (infra/ecs.tf) scopes the task role's
#: `ses:SendEmail` to. A different sender here would need a second identity and
#: a second IAM grant, and would be a second thing to keep aligned in DNS.
SENDER_ENV = "EMAIL_SENDER"
DEFAULT_SENDER = "no-reply@archimedes-arc.com"

DEFAULT_WINDOW_DAYS = 7

#: Ceiling on EITHER rendered body, in bytes.
#:
#: `dmarc_reports.MAX_CELL_CHARS` bounds how wide one cell can make the table;
#: this bounds how tall. A single hand-written "report" mailed to the
#: DNS-published `dmarc-reports@` address may declare thousands of distinct
#: source IPs, each a legitimate row, and the parser's own limit allows 64 MB
#: uncompressed. Past SES's per-message size limit the send is REFUSED — exit
#: 4, no summary — and because this job deliberately has no CloudWatch alarm,
#: the only symptom is a Monday with no mail. That is the silence the whole
#: file exists to break, so an over-long body is truncated and says so rather
#: than becoming a message that never arrives.
MAX_BODY_BYTES = 512 * 1024

_TRUNCATED_TEXT = (
    "\n\n(TRUNCATED at {limit} bytes. The full table is on disk — run the operator command in\n"
    "{runbook} § 'Get the table' against the same window.)\n"
)
_TRUNCATED_HTML = (
    '<p style="margin:16px 0 0;font-weight:600;color:#b3261e">(TRUNCATED at {limit} bytes — '
    "run the operator command in <code>{runbook}</code> § 'Get the table' for the whole table.)</p></div>"
)

_AWS_REGION_ENV = ("AWS_REGION", "AWS_DEFAULT_REGION")

RUNBOOK = "docs/runbooks/dmarc-reports.md"


class ReportsUnreadable(RuntimeError):
    """The S3 listing or a download failed — never rendered as "no reports"."""


class SummaryNotSent(RuntimeError):
    """SES refused the message. The one failure that must not exit 0."""


def _region() -> str | None:
    for name in _AWS_REGION_ENV:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _s3_client():  # pragma: no cover - thin boto3 construction, injected in tests
    import boto3

    return boto3.client("s3", region_name=_region())


def _ses_client():  # pragma: no cover - thin boto3 construction, injected in tests
    import boto3

    # SESv2, matching `auth/mailer.js`'s `@aws-sdk/client-sesv2`. The IAM action
    # is `ses:SendEmail` either way, so the existing identity-scoped grant on
    # the task role covers this without a second statement.
    return boto3.client("sesv2", region_name=_region())


# ────────────────────────────── collection ──────────────────────────────


def fetch_windows(
    client,
    bucket: str,
    prefix: str,
    *,
    days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> tuple[list[StoredObject], list[StoredObject]]:
    """Return ``(this_window, previous_window)`` objects, in ONE listing.

    Both windows come from a single ``list_objects_v2`` pass because they have
    to agree: computing "new this week" from two listings taken at different
    instants would report an object that crossed the boundary between them as
    new, every time.

    Windowed on S3's ``LastModified``, not on the report's own ``date_range``.
    Receivers batch a UTC day and send it hours later, so a report can land in
    the window after the one it describes — the consequence is that a source
    first seen on a Sunday may be announced as "new" on the following Monday
    rather than the one before. That is a labelling lag on the *diff*, and it
    is the honest trade for not downloading and parsing the whole bucket every
    week to date each report by its contents. The table's own header prints the
    reports' real ``date_range``, so what a summary covers is never in doubt.
    """
    now = now or datetime.now(UTC)
    window = timedelta(days=days)
    this_start = now - window
    previous_start = now - 2 * window

    try:
        objects = list(iter_s3_objects(client, bucket, prefix, since=previous_start))
    # Bare Exception on purpose, same call as the operator CLI's: botocore
    # raises a different class for every failure (NoCredentialsError,
    # ClientError, EndpointConnectionError) and to a reader of this summary they
    # all mean one thing — we could not look.
    except Exception as exc:
        raise ReportsUnreadable(f"could not read s3://{bucket}/{prefix}: {exc}") from exc

    current = [obj for obj in objects if obj.last_modified >= this_start]
    previous = [obj for obj in objects if obj.last_modified < this_start]
    return current, previous


# ────────────────────────────── rendering ───────────────────────────────


def _source_ips(summary: Summary) -> set[str]:
    return {row.source_ip for row in summary.rows}


def subject_line(summary: Summary, previous: Summary, domain: str, days: int) -> str:
    """The verdict, in the inbox list, before anything is opened.

    A subject that reads the same on a clean week and a spoofed one makes the
    message a ritual instead of a signal.
    """
    if summary.reports_parsed == 0:
        if summary.unreadable:
            # THE THIRD FACT. "I could not look", "I looked and found nothing"
            # and "I looked, I found things, and I could not read any of them"
            # are three different states, and only the first two used to have a
            # subject line. Reporting the third as NO REPORTS RECEIVED is a
            # false all-clear in the inbox list and sends the reader down §5's
            # arrival ladder — MX, receipt rule, active rule set, prefix — none
            # of which is the fault when objects are demonstrably arriving.
            return f"DMARC weekly [{domain}]: NO READABLE REPORTS ({len(summary.unreadable)} unreadable) in {days} days"
        return f"DMARC weekly [{domain}]: NO REPORTS RECEIVED in {days} days"
    # Only a window that actually held reports can make a source "new". The
    # first week of collection has an empty comparison window, and calling
    # every source new then would announce our own SES egress as an unexplained
    # sender — and put the subject line at odds with the body, which says
    # plainly that there is nothing to compare against.
    new_sources = _source_ips(summary) - _source_ips(previous) if previous.reports_parsed else set()
    suffix = f", {len(new_sources)} new source(s)" if new_sources else ""
    if summary.total_failing:
        return (
            f"DMARC weekly [{domain}]: {summary.total_failing} of {summary.total_messages} "
            f"messages FAILED alignment, {len(summary.failing_sources)} source(s){suffix}"
        )
    return (
        f"DMARC weekly [{domain}]: all {summary.total_messages} messages aligned "
        f"across {len(summary.rows)} source(s){suffix}"
    )


def _unreadable_lines(unreadable: Sequence[str], heading: str) -> list[str]:
    """The parse errors, named. Bounded per line — every one of these strings
    embeds a filename chosen by whoever sent the archive."""
    return [heading, *(f"  {clip_cell(item, MAX_NOTE_CHARS)}" for item in unreadable)]


def _no_reports_lines(bucket: str, prefix: str, days: int, inspected: int, unreadable: Sequence[str]) -> list[str]:
    """The body a window with no table gets — and it has TWO shapes.

    An empty window and a window full of objects the parser refused are
    different facts with different diagnoses, and the second one used to be
    rendered as the first while the parse errors this code had already computed
    were thrown away. The only trace was the object count in a parenthesis.
    """
    if unreadable:
        return [
            f"NO READABLE REPORTS in the last {days} days: {inspected} object(s) landed in the window",
            "and NOT ONE of them held a report this parser could read.",
            "",
            "This is neither a quiet week nor a clean one. Something IS arriving, so the",
            "collection path is not the fault and the arrival ladder is not where to look.",
            "An unreadable object is counted nowhere: not in the table, not in the failures.",
            "",
            *_unreadable_lines(unreadable, f"UNREADABLE ({len(unreadable)}) — every object in the window:"),
            "",
            f"Looked at: s3://{bucket}/{prefix}",
            f"Diagnose (read-only): {RUNBOOK} § 'Reports arrive but cannot be parsed'.",
        ]
    return [
        f"NO REPORTS RECEIVED in the last {days} days.",
        "",
        "This is not a clean bill of health — it is the absence of evidence, and it looks",
        "exactly like a domain nobody is forging. Either no receiver had mail from us to",
        "report on (plausible: reports are generated only for domains that actually sent),",
        "or the collection path is broken.",
        "",
        f"Looked at: s3://{bucket}/{prefix}  ({inspected} object(s) in the window)",
        f"Diagnose (read-only, ordered by likelihood): {RUNBOOK} § 'No reports are arriving'.",
    ]


def _changes_lines(summary: Summary, previous: Summary) -> list[str]:
    """What is different from last week — the part worth a Monday morning."""
    if previous.reports_parsed == 0:
        return ["CHANGES: no reports in the previous window, so there is nothing to compare against."]

    current_ips = _source_ips(summary)
    previous_ips = _source_ips(previous)
    new = sorted(clip_cell(ip) for ip in current_ips - previous_ips)
    gone = sorted(clip_cell(ip) for ip in previous_ips - current_ips)
    # Clipped on both sides of the membership test so an over-long source IP is
    # still annotated as FAILING rather than quietly losing the annotation.
    failing_now = {clip_cell(row.source_ip) for row in summary.failing_sources}

    lines = ["CHANGES SINCE THE PREVIOUS WINDOW"]
    if new:
        annotated = [f"{ip} (FAILING)" if ip in failing_now else ip for ip in new]
        lines.append(f"  NEW sources ({len(new)}): {', '.join(annotated)}")
        lines.append("  A source sending as this domain that was not here last week is either a")
        lines.append("  sending path someone added, or a forgery. Name it before the policy moves.")
    else:
        lines.append("  No new source IPs.")
    if gone:
        lines.append(f"  Gone ({len(gone)}): {', '.join(gone)}")
    return lines


def render_text(
    summary: Summary,
    previous: Summary,
    *,
    bucket: str,
    prefix: str,
    days: int,
    inspected: int,
) -> str:
    """Plain-text body. The table is ``render_table`` verbatim — see the docstring."""
    lines = [f"DMARC weekly summary — the last {days} days of aggregate reports.", ""]
    if summary.reports_parsed == 0:
        lines.extend(_no_reports_lines(bucket, prefix, days, inspected, summary.unreadable))
        return "\n".join(lines) + "\n"

    lines.append(render_table(summary))
    lines.append("")
    lines.extend(_changes_lines(summary, previous))
    lines.extend(
        [
            "",
            f"Source: s3://{bucket}/{prefix}",
            "Reproduce by hand:",
            "  python scripts/dmarc_report_summary.py \\",
            '      --bucket "$(terraform -chdir=infra output -raw dmarc_reports_bucket)" \\',
            f"      --since-days {days}",
            f"How to read this, and what has to hold before p=none moves: {RUNBOOK}",
        ]
    )
    return "\n".join(lines) + "\n"


_HTML_TABLE_HEADERS = (
    ("Source IP", "left"),
    ("Msgs", "right"),
    ("Pass", "right"),
    ("Fail", "right"),
    ("Aligned DKIM", "right"),
    ("Aligned SPF", "right"),
    ("Disposition", "left"),
    ("Reporters", "left"),
)


def render_html(
    summary: Summary,
    previous: Summary,
    *,
    bucket: str,
    prefix: str,
    days: int,
    inspected: int,
) -> str:
    """HTML body — the same numbers, legible on a phone.

    EVERY interpolated value is ``html.escape``d. Not defensive habit: source
    IPs, ``org_name``s and dispositions come out of XML that arrived from the
    public internet, addressed to an address we published in DNS. Anyone can
    mail ``dmarc-reports@`` a hand-written "report", and its contents land in
    this message unquestioned. Escaping is what keeps a crafted ``org_name``
    from rewriting the summary the owner reads to decide whether the domain is
    being spoofed.
    """

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    head = (
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:14px;line-height:1.5;color:#111">'
    )
    if summary.reports_parsed == 0:
        body = "".join(
            f'<p style="margin:0 0 8px">{esc(line)}</p>' if line else '<p style="margin:0 0 8px"></p>'
            for line in _no_reports_lines(bucket, prefix, days, inspected, summary.unreadable)
        )
        headline = "no READABLE reports" if summary.unreadable else "no reports"
        return (
            f'{head}<h2 style="margin:0 0 12px;font-size:17px">DMARC weekly summary — '
            f"{headline} in {esc(days)} days</h2>{body}</div>"
        )

    verdict = (
        f"{summary.total_failing} of {summary.total_messages} messages FAILED DMARC alignment"
        if summary.total_failing
        else f"All {summary.total_messages} messages aligned"
    )
    verdict_colour = "#b3261e" if summary.total_failing else "#146c2e"

    header_cells = "".join(
        f'<th style="text-align:{align};padding:6px 10px;border-bottom:2px solid #ddd;'
        f'font-weight:600;white-space:nowrap">{esc(label)}</th>'
        for label, align in _HTML_TABLE_HEADERS
    )

    def cell(value: object) -> str:
        """Clipped, then escaped — the same two bounds `render_table` applies."""
        return esc(clip_cell(value))

    rows = []
    for row in summary.rows:
        cells = (
            (cell(row.source_ip), "left"),
            (cell(row.messages), "right"),
            (cell(row.passing), "right"),
            (cell(row.failing), "right"),
            (cell(row.dkim_aligned_pass), "right"),
            (cell(row.spf_aligned_pass), "right"),
            (cell(", ".join(f"{k}:{v}" for k, v in sorted(row.dispositions.items()))), "left"),
            (cell(", ".join(sorted(row.reporters))), "left"),
        )
        # Failing rows are tinted, not merely sorted first: the table is read on
        # a phone where the sort order is not obvious.
        tint = ' style="background:#fdecea"' if row.failing else ""
        rows.append(
            f"<tr{tint}>"
            + "".join(
                f'<td style="text-align:{align};padding:6px 10px;border-bottom:1px solid #eee">{value}</td>'
                for value, align in cells
            )
            + "</tr>"
        )

    changes = "".join(
        f'<p style="margin:0 0 4px">{esc(line.strip())}</p>' for line in _changes_lines(summary, previous)
    )

    # The unreadable list belongs in the HTML part too. This part is what
    # nearly every mail client actually displays, so a fact that reaches only
    # the text body is a fact the owner does not see — and the text body has
    # carried `render_table`'s UNREADABLE block since the parser was written.
    unreadable_block = ""
    if summary.unreadable:
        items = "".join(
            f'<li style="margin:0 0 2px">{esc(clip_cell(item, MAX_NOTE_CHARS))}</li>' for item in summary.unreadable
        )
        unreadable_block = (
            '<div style="margin:16px 0 0">'
            f'<p style="margin:0 0 4px;font-weight:600;color:#b3261e">UNREADABLE '
            f"({esc(len(summary.unreadable))}) — these were NOT counted above</p>"
            f'<ul style="margin:0;padding-left:20px;color:#555">{items}</ul></div>'
        )

    return (
        f'{head}<h2 style="margin:0 0 4px;font-size:17px">DMARC weekly summary</h2>'
        f'<p style="margin:0 0 12px;color:#555">'
        f"{esc(summary.reports_parsed)} report(s) for "
        f"{esc(', '.join(sorted(summary.policy_domains)) or 'an unknown domain')}, covering "
        f"{esc(format_utc(summary.date_begin))} → {esc(format_utc(summary.date_end))} · "
        f"policy {esc(', '.join(sorted(summary.policies_seen)) or 'unknown')} as the reporters saw it</p>"
        f'<p style="margin:0 0 12px;font-weight:600;color:{verdict_colour}">{esc(verdict)}</p>'
        f'<table style="border-collapse:collapse;font-size:13px"><thead><tr>{header_cells}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
        f'<div style="margin:16px 0 0">{changes}</div>{unreadable_block}'
        f'<p style="margin:16px 0 0;color:#555">Source: <code>s3://{esc(bucket)}/{esc(prefix)}</code><br>'
        f"How to read this, and what has to hold before <code>p=none</code> moves: "
        f"<code>{esc(RUNBOOK)}</code></p></div>"
    )


def bounded_body(body: str, note: str) -> str:
    """Cap a rendered body at :data:`MAX_BODY_BYTES`, saying so when it bites.

    Truncation is the LAST resort and it is deliberately visible. A body that
    grew past what SES accepts is a send that fails, an exit 4, and no Monday
    mail at all — and the absence of the Monday mail is this job's only alarm,
    so an unsent summary costs more than a clipped one. The note names the
    operator command that prints the whole table from the same bucket.

    Cut on a byte boundary and decoded with ``errors="ignore"``, so a multi-byte
    character straddling the limit is dropped rather than corrupting the body.
    The HTML note closes the wrapper div; the tags cut mid-document are left
    unclosed, which every mail client tolerates and which is strictly better
    than a message that is never delivered.
    """
    encoded = body.encode("utf-8")
    if len(encoded) <= MAX_BODY_BYTES:
        return body
    kept = encoded[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")
    return kept + note.format(limit=MAX_BODY_BYTES, runbook=RUNBOOK)


# ──────────────────────────────── sending ───────────────────────────────


def send_summary(client, *, sender: str, recipients: Sequence[str], subject: str, text: str, html_body: str) -> str:
    """SESv2 ``SendEmail``. Returns the MessageId; raises on any failure.

    Deliberately NOT swallowing the error into a log line: an exit 0 from this
    job is the only thing that says the owner was told, and a job that reports
    success after failing to send puts the summary in exactly the silent-failure
    class it exists to break.
    """
    try:
        response = client.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": list(recipients)},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": text, "Charset": "UTF-8"},
                        "Html": {"Data": html_body, "Charset": "UTF-8"},
                    },
                }
            },
        )
    except Exception as exc:  # botocore raises a different class per failure
        raise SummaryNotSent(f"SES refused the summary: {type(exc).__name__}: {exc}") from exc
    message_id = (response or {}).get("MessageId")
    if not message_id:
        # A response with no MessageId is not a send. Treating it as one would
        # be the same silent success as swallowing the exception above.
        raise SummaryNotSent("SES returned no MessageId; the summary was not accepted")
    return message_id


# ──────────────────────────────────  CLI  ───────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m archimedes.scripts.dmarc_weekly_summary",
        description="Mail a weekly DMARC aggregate-report summary to the owner alert address.",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get(BUCKET_ENV, ""),
        help=f"Reports bucket. Defaults to ${BUCKET_ENV} (set on the scheduled task).",
    )
    parser.add_argument(
        "--prefix",
        default=os.environ.get(PREFIX_ENV, DEFAULT_PREFIX),
        help=f"S3 key prefix the receipt rule writes under (default: {DEFAULT_PREFIX}).",
    )
    parser.add_argument(
        "--to",
        default=os.environ.get(RECIPIENT_ENV, ""),
        help=f"Recipient(s), comma-separated. Defaults to ${RECIPIENT_ENV}.",
    )
    parser.add_argument(
        "--from",
        dest="sender",
        default=os.environ.get(SENDER_ENV, DEFAULT_SENDER),
        help=f"Sender address on the verified identity. Defaults to ${SENDER_ENV}, then {DEFAULT_SENDER}.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"Window length in days (default {DEFAULT_WINDOW_DAYS}); the same length again before it is "
        "the comparison window.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read the bucket and print the summary. Sends nothing.",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, s3_client=None, ses_client=None, now: datetime | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.bucket:
        print(
            f"error: no reports bucket. Pass --bucket or set {BUCKET_ENV} (terraform output dmarc_reports_bucket).",
            file=sys.stderr,
        )
        return 2
    recipients = [part.strip() for part in args.to.split(",") if part.strip()]
    if not recipients and not args.dry_run:
        # Exit 2, not 0: a summary addressed to nobody is a job that runs
        # forever and tells no one, which is the state this file exists to end.
        print(
            f"error: no recipient. Pass --to or set {RECIPIENT_ENV} (infra/dmarc_reports.tf sets it "
            "to var.owner_alert_email).",
            file=sys.stderr,
        )
        return 2
    if args.days <= 0:
        print("error: --days must be positive", file=sys.stderr)
        return 2

    try:
        current, previous = fetch_windows(
            s3_client or _s3_client(),
            args.bucket,
            args.prefix,
            days=args.days,
            now=now,
        )
    except ReportsUnreadable as exc:
        # Exit 3. NOT "no reports received": the owner would read that as a
        # quiet week and the collection path would stay broken.
        print(f"error: {exc}", file=sys.stderr)
        return 3

    summary = build_summary([(obj.name, obj.body) for obj in current])
    previous_summary = build_summary([(obj.name, obj.body) for obj in previous])

    domain = args.sender.rpartition("@")[2] or args.sender
    subject = subject_line(summary, previous_summary, domain, args.days)
    kwargs = {
        "bucket": args.bucket,
        "prefix": args.prefix,
        "days": args.days,
        "inspected": len(current),
    }
    text = bounded_body(render_text(summary, previous_summary, **kwargs), _TRUNCATED_TEXT)
    html_body = bounded_body(render_html(summary, previous_summary, **kwargs), _TRUNCATED_HTML)

    if args.dry_run:
        print(f"Subject: {subject}")
        print()
        print(text, end="")
        print(f"(dry run — nothing sent; {len(html_body)} bytes of HTML withheld)")
        return 0

    try:
        message_id = send_summary(
            ses_client or _ses_client(),
            sender=args.sender,
            recipients=recipients,
            subject=subject,
            text=text,
            html_body=html_body,
        )
    except SummaryNotSent as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4

    # A MessageId is SES ACCEPTING the message, not delivering it — see the
    # module docstring.
    print(f"sent: {subject}")
    print(f"  to={', '.join(recipients)} from={args.sender} messageId={message_id}")
    print(f"  reports={summary.reports_parsed} messages={summary.total_messages} failing={summary.total_failing}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
