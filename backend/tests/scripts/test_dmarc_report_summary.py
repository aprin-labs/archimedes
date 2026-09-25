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

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    path = _REPO_ROOT / "scripts" / "dmarc_report_summary.py"
    spec = importlib.util.spec_from_file_location("dmarc_report_summary", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dmarc_report_summary"] = module
    spec.loader.exec_module(module)
    return module


drs = _load_module()


# ── Report builders ──────────────────────────────────────────────────────────
#
# Shaped after a real Google aggregate report: same element order, same
# two-verdict structure (policy_evaluated vs auth_results) that the parser has
# to tell apart.


def _record(
    *,
    source_ip: str,
    count: int,
    disposition: str = "none",
    dkim_aligned: str = "pass",
    spf_aligned: str = "pass",
    dkim_auth: str = "pass",
    spf_auth: str = "pass",
    header_from: str = "archimedes-arc.com",
    spf_auth_domain: str = "archimedes-arc.com",
) -> str:
    return f"""
  <record>
    <row>
      <source_ip>{source_ip}</source_ip>
      <count>{count}</count>
      <policy_evaluated>
        <disposition>{disposition}</disposition>
        <dkim>{dkim_aligned}</dkim>
        <spf>{spf_aligned}</spf>
      </policy_evaluated>
    </row>
    <identifiers>
      <header_from>{header_from}</header_from>
    </identifiers>
    <auth_results>
      <dkim>
        <domain>archimedes-arc.com</domain>
        <result>{dkim_auth}</result>
        <selector>abc123</selector>
      </dkim>
      <spf>
        <domain>{spf_auth_domain}</domain>
        <result>{spf_auth}</result>
      </spf>
    </auth_results>
  </record>"""


def _report(
    *,
    org: str = "google.com",
    report_id: str = "1234567890",
    begin: int = 1_756_000_000,
    end: int = 1_756_086_400,
    policy: str = "none",
    records: str = "",
) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<feedback>
  <report_metadata>
    <org_name>{org}</org_name>
    <email>noreply-dmarc-support@{org}</email>
    <report_id>{report_id}</report_id>
    <date_range>
      <begin>{begin}</begin>
      <end>{end}</end>
    </date_range>
  </report_metadata>
  <policy_published>
    <domain>archimedes-arc.com</domain>
    <adkim>r</adkim>
    <aspf>r</aspf>
    <p>{policy}</p>
    <sp>{policy}</sp>
    <pct>100</pct>
  </policy_published>{records}
</feedback>
""".encode()


def _zipped(xml: bytes, name: str = "google.com!archimedes-arc.com!1756000000!1756086400.xml") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, xml)
    return buf.getvalue()


def _as_ses_object(attachment: bytes, filename: str) -> bytes:
    """The bytes an SES receipt rule actually puts in S3: the whole MIME message."""
    msg = EmailMessage()
    msg["From"] = "noreply-dmarc-support@google.com"
    msg["To"] = "dmarc-reports@archimedes-arc.com"
    msg["Subject"] = "Report domain: archimedes-arc.com Submitter: google.com"
    msg.set_content("This is a DMARC aggregate report.")
    subtype = "zip" if filename.endswith(".zip") else "gzip"
    msg.add_attachment(attachment, maintype="application", subtype=subtype, filename=filename)
    return msg.as_bytes()


SAMPLE_XML = _report(
    records=_record(source_ip="54.240.8.1", count=42)
    + _record(source_ip="203.0.113.77", count=3, disposition="none", dkim_aligned="fail", spf_aligned="fail")
)


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
    monkeypatch.setattr(drs, "MAX_UNCOMPRESSED_BYTES", 4096)
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
    monkeypatch.setattr(drs, "MAX_UNCOMPRESSED_BYTES", 4096)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(4):
            zf.writestr(f"part{i}.xml", b"A" * 2000)  # 2000 < 4096 each, 8000 > 4096 total
    summary = drs.build_summary([("many.zip", buf.getvalue())])

    assert summary.reports_parsed == 0
    assert len(summary.unreadable) == 1
    assert "zip expands past" in summary.unreadable[0]


def test_member_count_limit_refuses_rather_than_truncating(monkeypatch):
    """An over-full archive is refused, never silently read down to the limit.

    Truncating would drop reports out of the denominator without saying so —
    the same quiet undercount the NO REPORTS PARSED guard exists to stop, just
    one level down.
    """
    monkeypatch.setattr(drs, "MAX_ARCHIVE_MEMBERS", 3)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(5):
            zf.writestr(f"report{i}.xml", SAMPLE_XML)
    summary = drs.build_summary([("crowded.zip", buf.getvalue())])

    assert summary.reports_parsed == 0, "must refuse outright, not parse the first 3"
    assert "zip holds 5 members" in summary.unreadable[0]
