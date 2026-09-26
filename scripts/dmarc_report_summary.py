#!/usr/bin/env python3
# Raw docstring: the USAGE block below uses shell line-continuations, and in a
# non-raw string Python would eat every `\` + newline and join the examples
# into one unreadable line.
r"""Summarise DMARC aggregate reports into a per-source-IP pass/fail table (#1504).

WHY THIS EXISTS. `infra/dns_email.tf` publishes DMARC at ``p=none`` with
``rua=mailto:dmarc-reports@archimedes-arc.com``; `infra/dmarc_reports.tf` gives
that address a mailbox (an SES receipt rule writing to a private S3 bucket).
The reports that land there are the ONLY evidence that can justify moving the
policy to ``p=quarantine`` and then ``p=reject``. Reading them by hand does not
scale past the first week: each report is a MIME message wrapping a zip or gzip
wrapping XML, receivers send one per domain per day, and the question being
asked of them ("does every legitimate source align?") is an aggregation across
all of them, not a property of any one.

THE MISREADING THIS TOOL IS BUILT TO PREVENT. A DMARC aggregate report carries
two different verdicts per row and they disagree constantly:

  * ``<auth_results>`` — did SPF/DKIM pass *at all*, for whatever domain.
  * ``<policy_evaluated>`` — did SPF/DKIM pass *and align with the From: domain*.

Only the second one is DMARC. A forwarder, a bulk-mail vendor, or an attacker
sending from a domain they legitimately control will show ``auth_results`` SPF
= pass while ``policy_evaluated`` SPF = fail. Reading the first column and
concluding "everything passes" is precisely how you arrive at ``p=reject`` with
a sending path that is about to start disappearing. This script counts a
message as a DMARC pass if and only if ``policy_evaluated`` says dkim=pass OR
spf=pass, and `backend/tests/scripts/test_dmarc_report_summary.py` pins that.

THE SECOND MISREADING. An empty bucket and a clean bucket look the same. If no
reports were parsed, this script prints "NO REPORTS PARSED" and exits 2 rather
than rendering an empty table under a "0 failing sources" headline. Absence of
evidence is not evidence of alignment, and at ``p=none`` — which is where the
policy sits today — absence of evidence is the *expected* state right up until
`infra/dmarc_reports.tf` is applied.

USAGE

    # Local files or a directory of them (.xml, .xml.gz, .zip, or raw MIME).
    python scripts/dmarc_report_summary.py --path ~/Downloads/dmarc/

    # Straight from the bucket the receipt rule writes to.
    python scripts/dmarc_report_summary.py \
        --bucket "$(terraform -chdir=infra output -raw dmarc_reports_bucket)" \
        --since-days 14

    # The gate before moving the policy: non-zero if ANY source has failures.
    python scripts/dmarc_report_summary.py --bucket ... --since-days 14 \
        --require-all-aligned

Exit codes: 0 = reports parsed, 2 = no reports parsed, 3 = failures present
under ``--require-all-aligned``, 4 = the S3 call itself failed. 2 and 4 are
deliberately distinct from 0: "could not look" must never render as "nothing
found". Procedure and interpretation: docs/runbooks/dmarc-reports.md.

Stdlib only for parsing (no boto3 import unless --bucket is used), so the tests
are hermetic and the script runs anywhere a report can be downloaded to.
"""

from __future__ import annotations

import argparse
import dataclasses
import email
import email.policy
import gzip
import io
import json
import sys
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


def _utc(epoch: int | None) -> str:
    if not epoch:
        return "?"
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def render_table(summary: Summary) -> str:
    lines: list[str] = []
    lines.append(f"DMARC aggregate reports: {summary.reports_parsed} parsed")
    lines.append(f"Window:   {_utc(summary.date_begin)} → {_utc(summary.date_end)}")
    lines.append(f"Domain:   {', '.join(sorted(summary.policy_domains)) or '(none reported)'}")
    lines.append(f"Policy:   {', '.join(sorted(summary.policies_seen)) or '(none reported)'} (as the reporters saw it)")
    lines.append("")

    headers = ("SOURCE IP", "MSGS", "PASS", "FAIL", "DKIM-A", "SPF-A", "DISPOSITION", "REPORTERS")
    body = [
        (
            row.source_ip,
            str(row.messages),
            str(row.passing),
            str(row.failing),
            str(row.dkim_aligned_pass),
            str(row.spf_aligned_pass),
            ",".join(f"{k}:{v}" for k, v in sorted(row.dispositions.items())),
            ",".join(sorted(row.reporters)),
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
            f"alignment, from {len(failing)} source(s): {', '.join(r.source_ip for r in failing)}."
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
        lines.extend(f"  {item}" for item in summary.unreadable)
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


def collect_s3(client, bucket: str, prefix: str, since_days: int | None) -> list[tuple[str, bytes]]:
    cutoff = datetime.now(UTC) - timedelta(days=since_days) if since_days else None
    blobs: list[tuple[str, bytes]] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if cutoff and obj["LastModified"] < cutoff:
                continue
            body = client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            blobs.append((f"s3://{bucket}/{obj['Key']}", body))
    return blobs


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


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dmarc_report_summary.py",
        description="Summarise DMARC aggregate reports into a per-source-IP pass/fail table.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--path", type=Path, help="A local report file, or a directory of them.")
    source.add_argument("--bucket", help="S3 bucket the SES receipt rule writes reports into.")
    parser.add_argument("--prefix", default="reports/", help="S3 key prefix (default: %(default)s).")
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="With --bucket, only objects modified in the last N days. Use 14 for the fortnight #1504 asks for.",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output instead of the table.")
    parser.add_argument(
        "--require-all-aligned",
        action="store_true",
        help="Exit 3 if ANY source has failing messages. The gate before moving DMARC off p=none.",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, s3_client=None) -> int:
    args = build_parser().parse_args(argv)

    if args.bucket:
        client = s3_client
        if client is None:
            import boto3  # imported lazily so --path needs no AWS SDK

            client = boto3.client("s3")
        try:
            blobs = collect_s3(client, args.bucket, args.prefix, args.since_days)
        # Bare Exception on purpose: botocore raises a different class for every
        # failure (NoCredentialsError, ClientError, EndpointConnectionError) and
        # they all mean the same thing to a reader of this table — we could not
        # look. Narrowing it would let one of them escape as a traceback.
        except Exception as exc:
            # Exit 4, never 0 and never 2. "The listing failed" and "there are no
            # reports" are different facts and only one of them is about DMARC.
            print(f"ERROR: could not read s3://{args.bucket}/{args.prefix}: {exc}", file=sys.stderr)
            return 4
    else:
        blobs = collect_local(args.path)

    summary = build_summary(blobs)

    if summary.reports_parsed == 0:
        # The guard. An empty table under a "0 failing" headline reads as
        # evidence of alignment; it is the absence of evidence, and at p=none
        # that is the expected state until infra/dmarc_reports.tf is applied.
        print("NO REPORTS PARSED — this is not a clean result, it is no result.", file=sys.stderr)
        print(f"Inspected {len(blobs)} object(s); none contained a DMARC aggregate report.", file=sys.stderr)
        for item in summary.unreadable:
            print(f"  {item}", file=sys.stderr)
        print("See docs/runbooks/dmarc-reports.md § 'No reports are arriving'.", file=sys.stderr)
        return 2

    print(render_json(summary) if args.json else render_table(summary))

    if args.require_all_aligned and summary.total_failing:
        print(
            f"--require-all-aligned: {summary.total_failing} failing message(s) present; not clear to tighten DMARC.",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
