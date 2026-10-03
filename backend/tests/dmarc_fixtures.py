"""DMARC aggregate reports, built in-process, in the shapes receivers send (#1504).

WHY THESE ARE SHARED RATHER THAN COPIED. Two suites read the same reports —
`tests/scripts/test_dmarc_report_summary.py` (the parser and the operator CLI)
and `tests/scripts/test_dmarc_weekly_summary.py` (the scheduled job that mails
the owner a table). They are testing two halves of one claim: that an operator
running the command by hand and a Monday-morning email agree about whether the
domain is being spoofed. Two private copies of these builders could drift into
testing two different report schemas and both stay green, which would take the
one guarantee worth having with them.

Same pattern as `tests/quant_factories.py` and `tests/gateway_fake.py`: a plain
helper module (not a conftest) that tests import, so the shapes are named once.

WHAT IS FAITHFUL HERE, AND WHAT IS NOT. The XML is RFC 7489 appendix-C, shaped
after a real Google report: same element order, and — the part that matters —
the same TWO-VERDICT structure, `<policy_evaluated>` (alignment; this is DMARC)
next to `<auth_results>` (raw pass/fail; this is not). Every builder can set
them independently, because a source whose raw SPF passes while its aligned SPF
fails is the exact record the parser must not read as a pass.

:func:`as_ses_object` is the one that keeps the tests honest about I/O: an SES
receipt rule stores the whole RFC 822 MESSAGE in S3, so the report is a base64
attachment and never the object body. Code that parses an S3 object as XML
directly finds nothing, and finding nothing is indistinguishable from a domain
nobody is forging.

Stdlib only. No boto3, no network, no fixture files on disk.
"""

from __future__ import annotations

import gzip
import io
import zipfile
from email.message import EmailMessage

#: Default report window, chosen only so the rendered header is stable:
#: 2025-08-24 01:46 UTC → 2025-08-25 01:46 UTC.
DEFAULT_BEGIN = 1_756_000_000
DEFAULT_END = 1_756_086_400

#: The domain every builder here reports on, matching `var.domain_name`.
DOMAIN = "archimedes-arc.com"


def record(
    *,
    source_ip: str,
    count: int,
    disposition: str = "none",
    dkim_aligned: str = "pass",
    spf_aligned: str = "pass",
    dkim_auth: str = "pass",
    spf_auth: str = "pass",
    header_from: str = DOMAIN,
    spf_auth_domain: str = DOMAIN,
) -> str:
    """One ``<record>``. ``*_aligned`` is DMARC; ``*_auth`` is diagnostic only."""
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
        <domain>{DOMAIN}</domain>
        <result>{dkim_auth}</result>
        <selector>abc123</selector>
      </dkim>
      <spf>
        <domain>{spf_auth_domain}</domain>
        <result>{spf_auth}</result>
      </spf>
    </auth_results>
  </record>"""


def aggregate_report(
    *,
    org: str = "google.com",
    report_id: str = "1234567890",
    begin: int = DEFAULT_BEGIN,
    end: int = DEFAULT_END,
    policy: str = "none",
    records: str = "",
) -> bytes:
    """A whole ``<feedback>`` document, as bytes."""
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
    <domain>{DOMAIN}</domain>
    <adkim>r</adkim>
    <aspf>r</aspf>
    <p>{policy}</p>
    <sp>{policy}</sp>
    <pct>100</pct>
  </policy_published>{records}
</feedback>
""".encode()


def zipped(xml: bytes, name: str = "google.com!archimedes-arc.com!1756000000!1756086400.xml") -> bytes:
    """A ``.zip`` attachment — what Google and Microsoft send."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, xml)
    return buf.getvalue()


def gzipped(xml: bytes) -> bytes:
    """A ``.gz`` attachment — what Yahoo and most others send."""
    return gzip.compress(xml)


def zipped_over_member_limit(xml: bytes, members: int = 65) -> bytes:
    """A ``.zip`` the parser REFUSES: more members than ``MAX_ARCHIVE_MEMBERS``.

    A real aggregate-report zip holds exactly one XML, so an archive with
    dozens is either broken or hostile and the parser refuses it outright
    rather than reading the first N (see ``iter_report_xml``). The point of
    having this shape named is what it does to a SUMMARY: the object arrived,
    it counts as an object in the window, and it contributes nothing to the
    table. A window made only of these is neither empty nor clean, and the
    default of 65 is one past the shipped limit so the refusal is real rather
    than monkeypatched.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for index in range(members):
            zf.writestr(f"report{index}.xml", xml)
    return buf.getvalue()


def as_ses_object(attachment: bytes, filename: str) -> bytes:
    """The bytes an SES receipt rule actually puts in S3: the whole MIME message."""
    msg = EmailMessage()
    msg["From"] = "noreply-dmarc-support@google.com"
    msg["To"] = f"dmarc-reports@{DOMAIN}"
    msg["Subject"] = f"Report domain: {DOMAIN} Submitter: google.com"
    msg.set_content("This is a DMARC aggregate report.")
    subtype = "zip" if filename.endswith(".zip") else "gzip"
    msg.add_attachment(attachment, maintype="application", subtype=subtype, filename=filename)
    return msg.as_bytes()


#: One healthy SES source (aligned DKIM carries it) and one failing source.
#: Deliberately NOT all-clean: a fixture with nothing failing lets a renderer
#: that drops the failure path pass every test that uses it.
SAMPLE_XML = aggregate_report(
    records=record(source_ip="54.240.8.1", count=42)
    + record(source_ip="203.0.113.77", count=3, disposition="none", dkim_aligned="fail", spf_aligned="fail")
)
