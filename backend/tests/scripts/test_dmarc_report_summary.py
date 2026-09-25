"""DMARC aggregate-report parser (#1504): unwrapping, alignment, and the two guards.

WHAT THIS GUARDS. `scripts/dmarc_report_summary.py` produces the ONLY evidence
that can justify moving `infra/dns_email.tf`'s DMARC policy off ``p=none``. The
issue's own anti-goal is jumping to enforcement on a misread, because the
failure mode — our sign-in and password-reset mail being dropped at the
receiver — is silent. So the properties pinned here are, in order of importance:

  1. **Alignment is read from ``policy_evaluated``, never ``auth_results``.**
     A source that passes raw SPF for its own domain but fails DMARC alignment
     must be counted as FAILING. This is the single misreading that turns a
     forwarder or a hijacked vendor into "everything looks fine, ship
     ``p=reject``". Test: ``test_unaligned_spf_pass_counts_as_dmarc_failure``.

  2. **No reports parsed is not a clean bill of health.** Exit code 2 and a
     stderr banner, never an empty table under a "0 failing" headline. At
     ``p=none`` with `infra/dmarc_reports.tf` unapplied, zero reports is the
     *expected* state, and it must never render as evidence of alignment.
     Test: ``test_zero_reports_exits_two_and_prints_no_result``.

  3. **``--require-all-aligned`` actually refuses.** It is the gate the runbook
     tells an operator to run before tightening the policy; a gate that exits 0
     on a report containing failures is worse than no gate.
     Test: ``test_require_all_aligned_refuses_when_a_source_fails``.

Plus the plumbing without which none of the above ever sees a byte: an SES
receipt rule stores the RAW MIME MESSAGE in S3, so the report is a base64
attachment rather than the object body, and it arrives zipped (Google,
Microsoft) or gzipped (Yahoo and most others).

WHERE THE CODE UNDER TEST LIVES. The parsing, aggregation and rendering are
`archimedes.scripts.dmarc_reports` (imported below as ``core``); the CLI and its
exit codes are `scripts/dmarc_report_summary.py` (loaded by path as ``drs``).
The split exists because `backend/Dockerfile` copies ``backend/`` and nothing
else, so the scheduled weekly summary — which runs in that image — cannot
import the operator script. One parser, two callers, one answer about whether
the domain is being spoofed.

Hermetic: reports are built in-process from the RFC 7489 appendix-C schema and
a hand-written stub S3 client. No boto3, no credentials, no network, no
fixture files.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
from archimedes.scripts import dmarc_reports as core

from tests import dmarc_fixtures

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    """Load `scripts/dmarc_report_summary.py` the way an operator runs it.

    The parsing moved to `archimedes.scripts.dmarc_reports` (imported above as
    `core`) so the scheduled weekly summary, which runs inside the backend
    image where this directory does not exist, reads reports with the same code
    an operator does. The script is still loaded BY PATH here rather than
    trusted to be importable: it carries the CLI — argument parsing and the
    four exit codes — and it also carries the `sys.path` insert that makes
    `python scripts/dmarc_report_summary.py` work from the repo root at all.
    Exercising it as a file is what keeps that documented invocation covered;
    importing `core` alone would leave the shim untested and free to rot.
    """
    path = _REPO_ROOT / "scripts" / "dmarc_report_summary.py"
    spec = importlib.util.spec_from_file_location("dmarc_report_summary", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dmarc_report_summary"] = module
    spec.loader.exec_module(module)
    return module


drs = _load_module()


# ── Report builders ──────────────────────────────────────────────────────────
#
# Shared with tests/scripts/test_dmarc_weekly_summary.py, which tests the other
# consumer of the same parser. See tests/dmarc_fixtures.py for what is faithful
# about these shapes; the aliases below keep this file reading the way it did
# when the builders were local to it.

_record = dmarc_fixtures.record
_report = dmarc_fixtures.aggregate_report
_zipped = dmarc_fixtures.zipped
_as_ses_object = dmarc_fixtures.as_ses_object
SAMPLE_XML = dmarc_fixtures.SAMPLE_XML


# ── Guard 1: alignment comes from policy_evaluated, not auth_results ─────────


def test_unaligned_spf_pass_counts_as_dmarc_failure():
    """The misreading that would justify a premature p=reject.

    This record is exactly the shape a forwarder or a spoofer using their own
    verified domain produces: raw SPF PASSES (``auth_results/spf/result`` =
    pass, for *their* domain) while alignment against our ``header_from``
    FAILS. DMARC failed. Anything that reports it as a pass is reading the
    wrong column.
    """
    xml = _report(
        records=_record(
            source_ip="198.51.100.9",
            count=17,
            dkim_aligned="fail",
            spf_aligned="fail",
            dkim_auth="none",
            spf_auth="pass",
            spf_auth_domain="mail.some-forwarder.example",
        )
    )
    summary = drs.build_summary([("unaligned.xml", xml)])

    (row,) = summary.rows
    assert row.source_ip == "198.51.100.9"
    assert row.failing == 17, "raw SPF pass without alignment is a DMARC FAILURE"
    assert row.passing == 0
    assert summary.total_failing == 17

    rendered = drs.render_table(summary)
    assert "17 of 17 messages FAILED DMARC alignment" in rendered
    assert "Do NOT move the policy past p=none" in rendered


def test_aligned_dkim_alone_is_a_pass():
    """DMARC passes on EITHER aligned mechanism — SPF failing alone is not a failure.

    The mirror of the test above. Our own SES mail has a bounce-domain of
    ``amazonses.com``, so aligned SPF fails and aligned DKIM carries DMARC —
    the exact situation `docs/runbooks/email-verification-validation.md`
    records. Counting that as a failure would block the policy move forever.
    """
    xml = _report(
        records=_record(
            source_ip="54.240.8.1",
            count=9,
            dkim_aligned="pass",
            spf_aligned="fail",
            spf_auth="pass",
            spf_auth_domain="amazonses.com",
        )
    )
    summary = drs.build_summary([("dkim-only.xml", xml)])

    (row,) = summary.rows
    assert (row.passing, row.failing) == (9, 0)
    assert row.dkim_aligned_pass == 9
    assert row.spf_aligned_pass == 0


# ── The sample report, through every wrapper it really arrives in ────────────


@pytest.mark.parametrize(
    ("label", "blob"),
    [
        ("bare xml", SAMPLE_XML),
        ("gzip", gzip.compress(SAMPLE_XML)),
        ("zip", _zipped(SAMPLE_XML)),
        ("ses mime + zip", _as_ses_object(_zipped(SAMPLE_XML), "report.zip")),
        ("ses mime + gzip", _as_ses_object(gzip.compress(SAMPLE_XML), "report.xml.gz")),
    ],
    # Explicit ids: without them pytest renders the whole MIME message into the
    # test id and the failure line is unreadable.
    ids=["bare-xml", "gzip", "zip", "ses-mime-zip", "ses-mime-gzip"],
)
def test_sample_report_parses_from_every_wrapper(label, blob):
    """One sample report, five containers, identical table.

    ``ses mime + …`` is the one that matters most: it is what the receipt rule
    in `infra/dmarc_reports.tf` writes to S3. A parser that reads the object
    body as XML finds nothing at all there.
    """
    summary = drs.build_summary([(label, blob)])

    assert summary.reports_parsed == 1, f"{label} did not yield a report"
    assert summary.unreadable == []
    assert summary.total_messages == 45
    assert summary.total_failing == 3
    assert summary.policy_domains == {"archimedes-arc.com"}
    assert summary.policies_seen == {"none"}

    # Sorted failures-first, so the source that needs explaining is on top.
    assert [r.source_ip for r in summary.rows] == ["203.0.113.77", "54.240.8.1"]
    assert summary.rows[0].failing == 3
    assert summary.rows[1].passing == 42
    assert summary.rows[1].reporters == {"google.com"}


def test_reports_aggregate_across_files_and_reporters():
    """Two reporters, overlapping source IPs — one row per IP, counts summed."""
    google = _report(org="google.com", records=_record(source_ip="54.240.8.1", count=10))
    yahoo = _report(
        org="yahoo.com",
        begin=1_756_100_000,
        end=1_756_186_400,
        records=_record(source_ip="54.240.8.1", count=5)
        + _record(source_ip="203.0.113.77", count=2, dkim_aligned="fail", spf_aligned="fail"),
    )
    summary = drs.build_summary([("g.xml", google), ("y.xml.gz", gzip.compress(yahoo))])

    assert summary.reports_parsed == 2
    by_ip = {r.source_ip: r for r in summary.rows}
    assert by_ip["54.240.8.1"].messages == 15
    assert by_ip["54.240.8.1"].reporters == {"google.com", "yahoo.com"}
    assert by_ip["203.0.113.77"].failing == 2
    # Window spans both reports, not just the last one read.
    assert summary.date_begin == 1_756_000_000
    assert summary.date_end == 1_756_186_400


def test_non_report_attachments_are_named_not_swallowed():
    """A message with no report inside is listed as unreadable, not ignored.

    Silently skipping it would shrink the denominator without saying so.
    """
    msg = EmailMessage()
    msg["From"] = "someone@example.com"
    msg["To"] = "dmarc-reports@archimedes-arc.com"
    msg.set_content("hello, this is not a report")
    summary = drs.build_summary([("junk", msg.as_bytes()), ("real.xml", SAMPLE_XML)])

    assert summary.reports_parsed == 1
    assert len(summary.unreadable) == 1
    assert "junk" in summary.unreadable[0]
    assert "UNREADABLE (1)" in drs.render_table(summary)


# ── Guard 2: no reports is not a clean result ────────────────────────────────


class _StubS3:
    """The three S3 behaviours the script uses, and nothing else."""

    def __init__(
        self, objects: dict[str, tuple[bytes, datetime]] | None = None, *, list_error: Exception | None = None
    ):
        self.objects = objects or {}
        self.list_error = list_error

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, *, Bucket, Prefix):  # boto3's kwarg names, capitalised
        if self.list_error:
            raise self.list_error
        contents = [
            {"Key": key, "LastModified": modified}
            for key, (_, modified) in sorted(self.objects.items())
            if key.startswith(Prefix)
        ]
        yield {"Contents": contents}

    def get_object(self, *, Bucket, Key):  # boto3's kwarg names, capitalised
        return {"Body": io.BytesIO(self.objects[Key][0])}


def test_zero_reports_exits_two_and_prints_no_result(capsys):
    """An empty bucket must never render as '0 failing sources'.

    Exit 2, on stderr, with the words that stop someone quoting it as evidence.
    """
    client = _StubS3({})
    code = drs.main(["--bucket", "archimedes-dmarc-reports"], s3_client=client)

    assert code == 2
    err = capsys.readouterr().err
    assert "NO REPORTS PARSED" in err
    assert "it is no result" in err
    assert "0 of 0 messages failed" not in err


def test_s3_failure_is_exit_four_not_two(capsys):
    """'Could not look' and 'nothing there' are different facts.

    Collapsing a credentials or endpoint error into the empty-bucket path would
    let a broken operator setup read as "no spoofing observed".
    """
    client = _StubS3(list_error=RuntimeError("Unable to locate credentials"))
    code = drs.main(["--bucket", "archimedes-dmarc-reports"], s3_client=client)

    assert code == 4
    err = capsys.readouterr().err
    assert "could not read s3://archimedes-dmarc-reports/reports/" in err
    assert "NO REPORTS PARSED" not in err


def test_since_days_filters_by_last_modified(capsys):
    """--since-days 14 is the fortnight the issue's acceptance criteria name."""
    now = datetime.now(UTC)
    client = _StubS3(
        {
            "reports/recent": (_as_ses_object(_zipped(SAMPLE_XML), "r.zip"), now - timedelta(days=2)),
            "reports/ancient": (_as_ses_object(_zipped(SAMPLE_XML), "r.zip"), now - timedelta(days=90)),
        }
    )
    code = drs.main(
        ["--bucket", "archimedes-dmarc-reports", "--since-days", "14", "--json"],
        s3_client=client,
    )

    assert code == 0
    assert '"reports_parsed": 1' in capsys.readouterr().out


# ── Guard 3: the gate before tightening the policy ───────────────────────────


def test_require_all_aligned_refuses_when_a_source_fails(tmp_path, capsys):
    """The runbook's pre-flight. SAMPLE_XML has one failing source; exit 3."""
    (tmp_path / "report.xml").write_bytes(SAMPLE_XML)
    code = drs.main(["--path", str(tmp_path), "--require-all-aligned"])

    assert code == 3
    captured = capsys.readouterr()
    assert "not clear to tighten DMARC" in captured.err
    assert "203.0.113.77" in captured.out


def test_require_all_aligned_passes_when_everything_aligns(tmp_path, capsys):
    """The other side of the same gate — it has to be passable, or it is theatre."""
    clean = _report(records=_record(source_ip="54.240.8.1", count=42, spf_aligned="fail"))
    (tmp_path / "report.xml.gz").write_bytes(gzip.compress(clean))
    code = drs.main(["--path", str(tmp_path), "--require-all-aligned"])

    assert code == 0
    assert "0 of 42 messages failed DMARC alignment" in capsys.readouterr().out


# ── Bounds on untrusted input ────────────────────────────────────────────────


def test_zip_bomb_is_refused_and_named(monkeypatch):
    """Reports arrive from the public internet at a DNS-published address.

    A compressed bomb must be refused as an unreadable object, not decompressed
    into memory and not silently truncated into half an XML document.
    """
    monkeypatch.setattr(core, "MAX_UNCOMPRESSED_BYTES", 4096)
    bomb = _zipped(b"A" * 100_000, name="bomb.xml")
    summary = drs.build_summary([("bomb.zip", bomb)])

    assert summary.reports_parsed == 0
    assert len(summary.unreadable) == 1
    assert "refusing to decompress" in summary.unreadable[0]


def test_many_small_members_are_refused_on_declared_total(monkeypatch):
    """The archive's DECLARED total is checked before a single member is read.

    Distinct from the test above, which the per-member read bound already
    catches on its own. This is the shape that slips past it: every member is
    individually under the limit, only the sum is not. Without the pre-check
    the parser would happily walk all of them.
    """
    monkeypatch.setattr(core, "MAX_UNCOMPRESSED_BYTES", 4096)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(4):
            zf.writestr(f"part{i}.xml", b"A" * 2000)  # 2000 < 4096 each, 8000 > 4096 total
    summary = drs.build_summary([("many.zip", buf.getvalue())])

    assert summary.reports_parsed == 0
    assert len(summary.unreadable) == 1
    assert "zip expands past" in summary.unreadable[0]


def test_an_oversized_cell_cannot_inflate_the_rendered_table():
    """The decompression bounds do not bound the RENDER.

    `render_table` pads every row to the widest cell in its column, so a single
    oversized cell is multiplied by the row count — measured before the bound,
    a 219 KB report carrying one 200 KB `<source_ip>` plus 30 ordinary records
    rendered a 6.6 MB table, and the factor grows with the number of sources.
    Every cell arrives from the public internet at a DNS-published address, and
    `archimedes.scripts.dmarc_weekly_summary` puts this table into an email, so
    an unbounded render is a message SES refuses to send.
    """
    huge = "192.0.2.99" + "A" * 200_000
    xml = _report(
        records="".join(_record(source_ip=f"198.51.100.{i}", count=1) for i in range(30))
        + _record(source_ip=huge, count=1)
    )
    summary = drs.build_summary([("hostile.xml", xml)])
    rendered = drs.render_table(summary)

    assert summary.reports_parsed == 1, "the report itself is well-formed; only one cell is hostile"
    assert len(rendered) < 16_000, f"31 bounded rows must not render {len(rendered)} characters"
    assert huge not in rendered
    # Clipped, never dropped — a source missing from the table is the quiet
    # undercount every guard in this file is built against.
    assert "192.0.2.99" in rendered
    assert "198.51.100.7  " in rendered, "ordinary rows keep their exact value and padding"


def test_member_count_limit_refuses_rather_than_truncating(monkeypatch):
    """An over-full archive is refused, never silently read down to the limit.

    Truncating would drop reports out of the denominator without saying so —
    the same quiet undercount the NO REPORTS PARSED guard exists to stop, just
    one level down.
    """
    monkeypatch.setattr(core, "MAX_ARCHIVE_MEMBERS", 3)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(5):
            zf.writestr(f"report{i}.xml", SAMPLE_XML)
    summary = drs.build_summary([("crowded.zip", buf.getvalue())])

    assert summary.reports_parsed == 0, "must refuse outright, not parse the first 3"
    assert "zip holds 5 members" in summary.unreadable[0]
