"""DMARC aggregate-report parsing, aggregation and rendering (#1504).

WHY THIS IS A LIBRARY AND NOT JUST A SCRIPT. Two things read DMARC aggregate
reports and they run in different places:

  * ``scripts/dmarc_report_summary.py`` — the operator command the runbook
    documents, run from a laptop against a directory of downloaded reports or
    straight against the bucket.
  * ``archimedes.scripts.dmarc_weekly_summary`` — the scheduled Fargate job
    that mails the owner a summary every Monday.

The second one runs inside the BACKEND IMAGE, and ``backend/Dockerfile`` copies
``backend/`` and nothing else — the repo's top-level ``scripts/`` directory is
not in the image at all. So a scheduled job cannot import the operator script,
and duplicating the parser would give the two readers of the same evidence two
different answers about whether the domain is being spoofed. The parsing lives
here; both callers import it.

WHY ``archimedes/scripts/`` AND NOT ``archimedes/services/``.
``archimedes/services/__init__.py`` re-exports the generation pipeline, the
portfolio agent and the redis state module, so importing anything under it
drags the whole application in — and with it a need for ``DATABASE_URL`` and
``REDIS_URL`` at import time. ``archimedes/scripts/__init__.py`` is empty, and
this module imports nothing beyond the standard library (boto3 only reaches it
as an already-constructed client argument). That is what keeps the operator
command hermetic and the scheduled task free of database secrets it has no use
for.

THE MISREADING THIS CODE IS BUILT TO PREVENT. A DMARC aggregate report carries
two different verdicts per row and they disagree constantly:

  * ``<auth_results>`` — did SPF/DKIM pass *at all*, for whatever domain.
  * ``<policy_evaluated>`` — did SPF/DKIM pass *and align with the From: domain*.

Only the second one is DMARC. A forwarder, a bulk-mail vendor, or an attacker
sending from a domain they legitimately control will show ``auth_results`` SPF
= pass while ``policy_evaluated`` SPF = fail. Reading the first column and
concluding "everything passes" is precisely how you arrive at ``p=reject`` with
a sending path that is about to start disappearing. :attr:`Record.dmarc_pass`
counts a message as a DMARC pass if and only if ``policy_evaluated`` says
dkim=pass OR spf=pass, and
``backend/tests/scripts/test_dmarc_report_summary.py`` pins that.

THE SECOND MISREADING. An empty bucket and a clean bucket look the same.
Nothing here renders "no reports" as "no failures": :func:`build_summary`
reports ``reports_parsed == 0`` and every caller is required to treat that as
*no result*, not a clean one. Interpretation: docs/runbooks/dmarc-reports.md.
"""

from __future__ import annotations

import dataclasses
import email
import email.policy
import gzip
import io
import json
import zipfile
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

# ── Unwrapping bounds ────────────────────────────────────────────────────────
#
# Every input here arrives from the public internet, addressed to an address we
# published in DNS, and is decompressed before it is inspected. These bounds are
# not tuning knobs — they are what stops a 4 KB object from becoming an OOM.
MAX_DEPTH = 4  # mime → attachment → zip → xml is 3; 4 leaves one level of slack
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 64

# ── Rendering bounds ─────────────────────────────────────────────────────────
#
# The bounds above stop a hostile object from being DECOMPRESSED into memory.
# These stop a hostile object that is entirely within them from being RENDERED
# into a message nobody can send.
#
# :func:`render_table` pads every row to the widest cell in its column, so one
# oversized cell is multiplied by the row count. Measured: a 219 KB report
# carrying a single 200 KB `<source_ip>` plus 30 ordinary records rendered a
# 6.6 MB table — 30x, and the factor grows with the number of sources. Every
# cell here is attacker-supplied (`dmarc-reports@` is published in DNS and the
# receipt rule stores whatever anyone mails it), and the weekly summary puts
# this table straight into an email: past SES's per-message limit the send is
# refused, no summary arrives, and by that job's deliberate no-alarm design
# the only symptom is a Monday morning with no mail.
#
# 64 characters is longer than any real IPv6 address, org_name or disposition,
# so this truncates hostile input and nothing else.
MAX_CELL_CHARS = 64

#: An `unreadable` entry is longer by nature — it carries the object name, an
#: archive member name chosen by whoever sent the archive, and the parser's own
#: message — so it gets its own, larger bound rather than being cut to a cell.
MAX_NOTE_CHARS = 200

_MAGIC_ZIP = b"PK\x03\x04"
_MAGIC_GZIP = b"\x1f\x8b"


# ── Parsed shapes ────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Record:
    """One ``<record>`` — a source IP's traffic for one report window."""

    source_ip: str
    count: int
    disposition: str
    #: ``<policy_evaluated>`` — pass here means passed AND aligned. This is DMARC.
    dkim_aligned: str
    spf_aligned: str
    #: ``<auth_results>`` — raw pass/fail, alignment NOT considered. Diagnostic only.
    dkim_auth: str
    spf_auth: str
    header_from: str

    @property
    def dmarc_pass(self) -> bool:
        """DMARC passes on EITHER aligned mechanism (RFC 7489 §6.6.2).

        Reads ``policy_evaluated`` only. ``auth_results`` is carried alongside
        so an operator can see *why* an aligned check failed, never so it can
        stand in for one.
        """
        return self.dkim_aligned == "pass" or self.spf_aligned == "pass"


@dataclasses.dataclass(frozen=True)
class AggregateReport:
    org_name: str
    report_id: str
    date_begin: int
    date_end: int
    policy_domain: str
    policy_p: str
    records: tuple[Record, ...]


@dataclasses.dataclass
class SourceRow:
    """Everything the table shows about one source IP, across every report."""

    source_ip: str
    messages: int = 0
    passing: int = 0
    failing: int = 0
    dkim_aligned_pass: int = 0
    spf_aligned_pass: int = 0
    dispositions: Counter = dataclasses.field(default_factory=Counter)
    header_froms: set[str] = dataclasses.field(default_factory=set)
    reporters: set[str] = dataclasses.field(default_factory=set)


@dataclasses.dataclass
class Summary:
    rows: list[SourceRow]
    reports_parsed: int
    unreadable: list[str]
    date_begin: int | None
    date_end: int | None
    policy_domains: set[str]
    policies_seen: set[str]

    @property
    def total_messages(self) -> int:
        return sum(r.messages for r in self.rows)

    @property
    def total_failing(self) -> int:
        return sum(r.failing for r in self.rows)

    @property
    def failing_sources(self) -> list[SourceRow]:
        return [r for r in self.rows if r.failing]


# ── Unwrapping: MIME / zip / gzip / bare XML → XML documents ─────────────────


def _looks_like_xml(blob: bytes) -> bool:
    head = blob.lstrip()[:5]
    return head.startswith(b"<") and not head.startswith(b"<html")


def _read_bounded(fh: io.BufferedIOBase, label: str) -> bytes:
    """Read at most MAX_UNCOMPRESSED_BYTES, refusing rather than truncating.

    Truncating a compressed bomb would hand a half-XML document to the parser
    and surface as "unreadable report" — a confusing symptom for a resource
    limit. Refuse loudly instead.
    """
    data = fh.read(MAX_UNCOMPRESSED_BYTES + 1)
    if len(data) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"{label}: expands past {MAX_UNCOMPRESSED_BYTES} bytes; refusing to decompress")
    return data


def iter_report_xml(blob: bytes, name: str = "<blob>", *, depth: int = 0) -> Iterator[tuple[str, bytes]]:
    """Yield ``(label, xml_bytes)`` for every aggregate report inside ``blob``.

    Handles the four shapes a report actually arrives in:

      1. The raw RFC 822 message, which is what an SES receipt rule stores in
         S3 — the report is a base64 attachment, not the object body. Anything
         that parses the S3 object as XML directly will find nothing.
      2. A ``.zip`` attachment (Google, Microsoft).
      3. A ``.gz`` attachment (Yahoo, Comcast, most others).
      4. A bare ``.xml``, which is what you get after unzipping by hand.

    Recursion is bounded by MAX_DEPTH so a malformed or hostile message cannot
    spin: ``email.message_from_bytes`` never raises on binary input, it just
    hands back a defective message whose payload may be the input again.
    """
    if depth > MAX_DEPTH or not blob:
        return

    if blob.startswith(_MAGIC_ZIP):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            members = zf.infolist()
            # Refuse, do not truncate to the first N. A real aggregate report
            # zip holds exactly one XML; silently ignoring members past a limit
            # would drop reports out of the denominator without saying so, which
            # is the same class of quiet undercount this whole tool exists to
            # stop.
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"{name}: zip holds {len(members)} members (limit {MAX_ARCHIVE_MEMBERS}); refusing")
            if sum(m.file_size for m in members) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError(f"{name}: zip expands past {MAX_UNCOMPRESSED_BYTES} bytes; refusing to decompress")
            for member in members:
                if member.is_dir():
                    continue
                with zf.open(member) as fh:
                    inner = _read_bounded(fh, f"{name}!{member.filename}")
                yield from iter_report_xml(inner, f"{name}!{member.filename}", depth=depth + 1)
        return

    if blob.startswith(_MAGIC_GZIP):
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as fh:
            inner = _read_bounded(fh, name)
        yield from iter_report_xml(inner, name, depth=depth + 1)
        return

    if _looks_like_xml(blob):
        yield (name, blob)
        return

    message = email.message_from_bytes(blob, policy=email.policy.default)
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if not payload or payload == blob:
            # payload == blob means this was not really a message; recursing
            # would loop until MAX_DEPTH for no gain.
            continue
        label = part.get_filename() or part.get_content_type()
        yield from iter_report_xml(payload, f"{name}::{label}", depth=depth + 1)


# ── Parsing one aggregate report ─────────────────────────────────────────────


def _text(node: ElementTree.Element | None, path: str, default: str = "") -> str:
    if node is None:
        return default
    found = node.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def parse_aggregate_report(xml_bytes: bytes) -> AggregateReport:
    """Parse one ``<feedback>`` document (RFC 7489 appendix C schema)."""
    # ElementTree, not defusedxml: the stdlib parser has had entity expansion
    # (billion laughs) and external-entity resolution disabled since 3.8, and the
    # size bounds above cap the input before it reaches here.
    root = ElementTree.fromstring(xml_bytes)
    if root.tag != "feedback":
        raise ValueError(f"not a DMARC aggregate report (root element <{root.tag}>)")

    meta = root.find("report_metadata")
    policy = root.find("policy_published")

    records: list[Record] = []
    for node in root.findall("record"):
        row = node.find("row")
        evaluated = row.find("policy_evaluated") if row is not None else None
        auth = node.find("auth_results")
        identifiers = node.find("identifiers")
        records.append(
            Record(
                source_ip=_text(row, "source_ip", "unknown"),
                count=int(_text(row, "count", "0") or 0),
                disposition=_text(evaluated, "disposition", "none"),
                dkim_aligned=_text(evaluated, "dkim", "fail").lower(),
                spf_aligned=_text(evaluated, "spf", "fail").lower(),
                dkim_auth=_text(auth, "dkim/result", "none").lower(),
                spf_auth=_text(auth, "spf/result", "none").lower(),
                header_from=_text(identifiers, "header_from", ""),
            )
        )

    return AggregateReport(
        org_name=_text(meta, "org_name", "unknown"),
        report_id=_text(meta, "report_id", ""),
        date_begin=int(_text(meta, "date_range/begin", "0") or 0),
        date_end=int(_text(meta, "date_range/end", "0") or 0),
        policy_domain=_text(policy, "domain", ""),
        policy_p=_text(policy, "p", ""),
        records=tuple(records),
    )


# ── Aggregation ──────────────────────────────────────────────────────────────


def summarize(reports: Sequence[AggregateReport], unreadable: Sequence[str] = ()) -> Summary:
    rows: dict[str, SourceRow] = {}
    begins: list[int] = []
    ends: list[int] = []
    domains: set[str] = set()
    policies: set[str] = set()

    for report in reports:
        if report.date_begin:
            begins.append(report.date_begin)
        if report.date_end:
            ends.append(report.date_end)
        if report.policy_domain:
            domains.add(report.policy_domain)
        if report.policy_p:
            policies.add(report.policy_p)

        for record in report.records:
            row = rows.setdefault(record.source_ip, SourceRow(source_ip=record.source_ip))
            row.messages += record.count
            if record.dmarc_pass:
                row.passing += record.count
            else:
                row.failing += record.count
            if record.dkim_aligned == "pass":
                row.dkim_aligned_pass += record.count
            if record.spf_aligned == "pass":
                row.spf_aligned_pass += record.count
            row.dispositions[record.disposition] += record.count
            if record.header_from:
                row.header_froms.add(record.header_from)
            row.reporters.add(report.org_name)

    ordered = sorted(rows.values(), key=lambda r: (-r.failing, -r.messages, r.source_ip))
    return Summary(
        rows=ordered,
        reports_parsed=len(reports),
        unreadable=list(unreadable),
        date_begin=min(begins) if begins else None,
        date_end=max(ends) if ends else None,
        policy_domains=domains,
        policies_seen=policies,
    )


# ── Rendering ────────────────────────────────────────────────────────────────


def format_utc(epoch: int | None) -> str:
    """A report ``date_range`` bound, rendered. Public because both renderers —
    the operator table here and the HTML body in
    ``archimedes.scripts.dmarc_weekly_summary`` — must print the same window."""
    if not epoch:
        return "?"
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def clip_cell(value: object, limit: int = MAX_CELL_CHARS) -> str:
    """One rendered cell, bounded. Public because both renderers need it.

    Truncates rather than drops: an oversized source IP is still evidence that
    something is mailing our report address, and a cell that vanished would be
    a source missing from the table — the quiet undercount everything here is
    built against. What is lost is only the tail of one string, and the whole
    object is still on disk under `scripts/dmarc_report_summary.py --path`.
    """
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def render_table(summary: Summary) -> str:
    lines: list[str] = []
    lines.append(f"DMARC aggregate reports: {summary.reports_parsed} parsed")
    lines.append(f"Window:   {format_utc(summary.date_begin)} → {format_utc(summary.date_end)}")
    lines.append(f"Domain:   {', '.join(sorted(summary.policy_domains)) or '(none reported)'}")
    lines.append(f"Policy:   {', '.join(sorted(summary.policies_seen)) or '(none reported)'} (as the reporters saw it)")
    lines.append("")

    headers = ("SOURCE IP", "MSGS", "PASS", "FAIL", "DKIM-A", "SPF-A", "DISPOSITION", "REPORTERS")
    # Every cell is clipped, including the counts: `<count>` is an integer out
    # of untrusted XML and a four-thousand-digit one renders as a
    # four-thousand-column table.
    body = [
        tuple(
            clip_cell(cell)
            for cell in (
                row.source_ip,
                row.messages,
                row.passing,
                row.failing,
                row.dkim_aligned_pass,
                row.spf_aligned_pass,
                ",".join(f"{k}:{v}" for k, v in sorted(row.dispositions.items())),
                ",".join(sorted(row.reporters)),
            )
        )
        for row in summary.rows
    ]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in body)) if body else len(headers[i]) for i in range(len(headers))
    ]
    lines.append("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row_cells in body:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row_cells)).rstrip())

    lines.append("")
    if summary.total_failing:
        failing = summary.failing_sources
        lines.append(
            f"VERDICT: {summary.total_failing} of {summary.total_messages} messages FAILED DMARC "
            f"alignment, from {len(failing)} source(s): {', '.join(clip_cell(r.source_ip) for r in failing)}."
        )
        lines.append("Every one is either a sending path of ours that is misconfigured, or a forgery.")
        lines.append("Do NOT move the policy past p=none until each is explained. See")
        lines.append("docs/runbooks/dmarc-reports.md § 'When to move to quarantine'.")
    else:
        lines.append(
            f"VERDICT: 0 of {summary.total_messages} messages failed DMARC alignment, "
            f"across {len(summary.rows)} source(s)."
        )
    if summary.unreadable:
        lines.append("")
        lines.append(f"UNREADABLE ({len(summary.unreadable)}) — these were NOT counted above:")
        lines.extend(f"  {clip_cell(item, MAX_NOTE_CHARS)}" for item in summary.unreadable)
    return "\n".join(lines)


def render_json(summary: Summary) -> str:
    return json.dumps(
        {
            "reports_parsed": summary.reports_parsed,
            "window": {"begin": summary.date_begin, "end": summary.date_end},
            "policy_domains": sorted(summary.policy_domains),
            "policies_seen": sorted(summary.policies_seen),
            "total_messages": summary.total_messages,
            "total_failing": summary.total_failing,
            "unreadable": summary.unreadable,
            "sources": [
                {
                    "source_ip": row.source_ip,
                    "messages": row.messages,
                    "passing": row.passing,
                    "failing": row.failing,
                    "dkim_aligned_pass": row.dkim_aligned_pass,
                    "spf_aligned_pass": row.spf_aligned_pass,
                    "dispositions": dict(sorted(row.dispositions.items())),
                    "header_froms": sorted(row.header_froms),
                    "reporters": sorted(row.reporters),
                }
                for row in summary.rows
            ],
        },
        indent=2,
    )


# ── Input collection ─────────────────────────────────────────────────────────


def collect_local(path: Path) -> list[tuple[str, bytes]]:
    if path.is_dir():
        return [(str(p), p.read_bytes()) for p in sorted(path.rglob("*")) if p.is_file()]
    return [(str(path), path.read_bytes())]


@dataclasses.dataclass(frozen=True)
class StoredObject:
    """One object out of the reports bucket, with the timestamp that dates it.

    ``last_modified`` is S3's, not the report's own ``date_range`` — the two
    differ by up to a day because receivers batch a UTC day and send it
    afterwards. It is the right key for *which run should look at this object*
    (a listing filter, cheap, no download) and the wrong one for *what period
    does this report describe* (that is ``AggregateReport.date_begin``, and
    :func:`render_table` prints it). Windowing on it is deliberate and its one
    consequence is stated where it bites, in
    ``archimedes.scripts.dmarc_weekly_summary``.
    """

    name: str
    body: bytes
    last_modified: datetime


def iter_s3_objects(client, bucket: str, prefix: str, *, since: datetime | None = None) -> Iterator[StoredObject]:
    """Yield every object under ``prefix`` modified at or after ``since``.

    Separate from :func:`collect_s3` because the weekly summary needs to sort
    one listing into two windows (this week, the week before) and therefore
    needs each object's timestamp; the operator CLI only needs the bytes.
    Sharing the listing is what keeps "the last 7 days" meaning the same thing
    in both.
    """
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if since is not None and obj["LastModified"] < since:
                continue
            body = client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            yield StoredObject(f"s3://{bucket}/{obj['Key']}", body, obj["LastModified"])


def collect_s3(client, bucket: str, prefix: str, since_days: int | None) -> list[tuple[str, bytes]]:
    since = datetime.now(UTC) - timedelta(days=since_days) if since_days else None
    return [(obj.name, obj.body) for obj in iter_s3_objects(client, bucket, prefix, since=since)]


def build_summary(blobs: Sequence[tuple[str, bytes]]) -> Summary:
    reports: list[AggregateReport] = []
    unreadable: list[str] = []
    for name, blob in blobs:
        found = 0
        try:
            for label, xml in iter_report_xml(blob, name):
                try:
                    reports.append(parse_aggregate_report(xml))
                    found += 1
                except (ElementTree.ParseError, ValueError) as exc:
                    unreadable.append(f"{label}: {exc}")
        except (ValueError, zipfile.BadZipFile, OSError) as exc:
            unreadable.append(f"{name}: {exc}")
            continue
        if not found:
            unreadable.append(f"{name}: no DMARC aggregate report found inside")
    return summarize(reports, unreadable)
