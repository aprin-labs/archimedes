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

THIS FILE IS THE COMMAND; THE PARSING LIVES IN THE BACKEND PACKAGE
(``archimedes.scripts.dmarc_reports``). Not a tidiness move: the scheduled
weekly summary (``archimedes.scripts.dmarc_weekly_summary``,
`infra/dmarc_reports.tf`) runs inside the backend image, and
``backend/Dockerfile`` copies ``backend/`` and nothing else — this directory is
not in that image. Two copies of the parser would be two different answers to
"is the domain being spoofed", from the same reports. Everything about how a
report is unwrapped, what counts as a DMARC pass, and how the table is rendered
is documented there.

The two misreadings the parser is built against — ``auth_results`` is not
DMARC, and no reports is not a clean bill of health — are its module docstring
and are pinned by `backend/tests/scripts/test_dmarc_report_summary.py`.

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

The parser it imports is stdlib-only (boto3 is imported here, lazily, and only
for --bucket), so the tests stay hermetic and this runs anywhere a report can be
downloaded to — it needs the repo on disk, not an AWS account.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

# The backend package is where the parser lives (see the docstring above); this
# script is run as `python scripts/dmarc_report_summary.py` from the repo root,
# which puts `scripts/` on sys.path and not `backend/`. Same one-line insert
# scripts/purge_orphan_generated.py uses for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from archimedes.scripts.dmarc_reports import (
    build_summary,
    collect_local,
    collect_s3,
    render_json,
    render_table,
)

__all__ = ["build_summary", "collect_local", "collect_s3", "main", "render_json", "render_table"]


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
