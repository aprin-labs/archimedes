"""#1908: nginx must resolve the VIEWER's address behind CloudFront → ALB, and only that.

The finding: realip trusted only the VPC CIDR, so its right-to-left walk over
X-Forwarded-For stopped on the CloudFront hop the ALB appends. ``$remote_addr``
(and the X-Real-IP / X-Client-IP every per-IP key reads: slowapi, the daily
generation cap, Better Auth's limiter, the session ipAddress) was the
CloudFront edge, shared by everyone that edge served.

The fix trusts CloudFront's origin-facing ranges too, from a generated,
checked-in include file (``nginx/cloudfront-origin-facing.conf``, written by
``scripts/refresh_cloudfront_origin_ranges.py``). Pinned here:

* the include file parses, is non-empty, holds only public ranges no broader
  than the script's floors, and is byte-identical to what the script renders
  (no hand edits);
* nginx.conf includes it at exactly the path nginx/Dockerfile installs it to,
  at http level, with ``real_ip_header X-Forwarded-For`` + ``real_ip_recursive on``
  (a missing include file is an nginx boot failure, and the image is only
  built post-merge, so this is the PR-time guard for that wiring);
* the security argument, executed against the REAL trust set: a viewer behind
  CloudFront resolves to the viewer; a forged X-Forwarded-For buys nothing via
  CloudFront or direct to the ALB. ``resolve_remote_addr`` mirrors nginx's
  ``ngx_http_get_forwarded_addr_internal``; the same scenarios were run against
  the built nginx image (PR body) and gave the same answers.

Mutation guards are in-file: dropping ``real_ip_recursive`` or trusting an
over-broad range each flips a scenario (see the ``test_guard_*`` tests).
Hermetic: no network, no nginx binary.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = ROOT / "nginx" / "nginx.conf"
DOCKERFILE = ROOT / "nginx" / "Dockerfile"
INCLUDE_FILE = ROOT / "nginx" / "cloudfront-origin-facing.conf"
SCRIPT = ROOT / "scripts" / "refresh_cloudfront_origin_ranges.py"

# Documentation / test addresses (RFC 5737), plus one real origin-facing address.
VIEWER = "203.0.113.7"
FORGED = "6.6.6.6"
ATTACKER = "198.51.100.66"
CLOUDFRONT_HOP = "130.176.88.10"  # inside CLOUDFRONT_ORIGIN_FACING 130.176.88.0/21
ALB_PEER = "10.0.0.37"  # an ALB ENI in a public subnet (10.0.0.0/24)


def _load_script():
    # Loaded lazily (not at import) so the realip scenario tests below still run,
    # and fail on their assertions, against a tree that has no script yet.
    spec = importlib.util.spec_from_file_location("refresh_cloudfront_origin_ranges", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["refresh_cloudfront_origin_ranges"] = module
    spec.loader.exec_module(module)
    return module


def _http_level(conf: str) -> str:
    """nginx.conf with the server{} block removed: what sits at http level."""
    start = re.search(r"^server\s*\{", conf, re.M)
    assert start, "no top-level server{} block found in nginx.conf"
    depth, i = 0, start.end() - 1
    while i < len(conf):
        depth += {"{": 1, "}": -1}.get(conf[i], 0)
        if depth == 0:
            break
        i += 1
    return conf[: start.start()] + conf[i + 1 :]


def _directives(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def _include_path(conf: str) -> str | None:
    found = [d for d in _directives(_http_level(conf)) if d.startswith("include ") and "cloudfront" in d]
    return found[0].removeprefix("include ").rstrip(";").strip() if found else None


def trusted_networks() -> list:
    """The realip trust set as deployed: nginx.conf's set_real_ip_from + the include file, if wired."""
    conf = NGINX_CONF.read_text(encoding="utf-8")
    lines = _directives(_http_level(conf))
    if _include_path(conf) is not None:
        lines += _directives(INCLUDE_FILE.read_text(encoding="utf-8"))
    return [ipaddress.ip_network(d.split()[1].rstrip(";")) for d in lines if d.startswith("set_real_ip_from ")]


def recursive_enabled() -> bool:
    """`real_ip_recursive on` at http level (nginx's default is off)."""
    return "real_ip_recursive on;" in _directives(_http_level(NGINX_CONF.read_text(encoding="utf-8")))


def resolve_remote_addr(peer: str, xff: str, trusted: list, recursive: bool = True) -> str:
    """nginx realip with ``real_ip_header X-Forwarded-For`` (ngx_http_get_forwarded_addr_internal).

    Not a trusted peer → the peer. Otherwise walk X-Forwarded-For right to left:
    the first hop that is not trusted (or any hop at all, when not recursive) is
    the answer; if every hop is trusted, the leftmost one is. An unparsable hop
    stops the walk on the last address already accepted (the peer, if none).
    """

    def is_trusted(addr) -> bool:
        return any(addr in net for net in trusted if net.version == addr.version)

    if not is_trusted(ipaddress.ip_address(peer)):
        return peer
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    answer = peer
    for hop in reversed(hops):
        try:
            addr = ipaddress.ip_address(hop)
        except ValueError:
            return answer
        answer = hop
        if not (recursive and is_trusted(addr)):
            return answer
    return answer


# X-Forwarded-For exactly as the ALB hands it to nginx (append mode: the ALB's own
# TCP peer goes LAST). (name, xff, expected $remote_addr)
SCENARIOS = [
    ("viewer via CloudFront", f"{VIEWER}, {CLOUDFRONT_HOP}", VIEWER),
    ("viewer forging XFF via CloudFront", f"{FORGED}, {VIEWER}, {CLOUDFRONT_HOP}", VIEWER),
    ("direct to the ALB, forged chain", f"{FORGED}, {CLOUDFRONT_HOP}, {ATTACKER}", ATTACKER),
    ("direct to the ALB, forged single hop", FORGED + ", " + ATTACKER, ATTACKER),
]


@pytest.mark.parametrize(("name", "xff", "expected"), SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_realip_resolves_the_viewer_and_never_a_forged_hop(name: str, xff: str, expected: str) -> None:
    got = resolve_remote_addr(ALB_PEER, xff, trusted_networks(), recursive=recursive_enabled())
    assert got == expected, f"{name}: X-Forwarded-For {xff!r} resolved to {got}, expected {expected}. " + (
        "That is the CloudFront hop: realip does not trust CloudFront's origin-facing ranges, so every "
        "viewer behind one edge shares one rate-limit key (#1908)."
        if got == CLOUDFRONT_HOP
        else "A client-controlled hop is being trusted."
    )


def test_guard_without_recursion_the_viewer_is_lost() -> None:
    """Anti-vacuity for the scenarios: real_ip_recursive is what reaches past the CloudFront hop."""
    assert resolve_remote_addr(ALB_PEER, f"{VIEWER}, {CLOUDFRONT_HOP}", trusted_networks(), recursive=False) == (
        CLOUDFRONT_HOP
    )
    assert recursive_enabled()


def test_guard_an_over_broad_trust_set_would_be_caught() -> None:
    """Trusting the internet makes the direct-to-ALB forgery win; the scenario above would go red."""
    too_broad = [*trusted_networks(), ipaddress.ip_network("0.0.0.0/0")]
    assert resolve_remote_addr(ALB_PEER, f"{FORGED}, {CLOUDFRONT_HOP}, {ATTACKER}", too_broad) == FORGED


def test_a_peer_outside_the_trust_set_is_taken_at_face_value() -> None:
    """realip only acts for a trusted socket peer; anything else keeps its own address."""
    assert resolve_remote_addr(ATTACKER, f"{FORGED}", trusted_networks()) == ATTACKER


@pytest.mark.parametrize(
    ("xff", "expected"),
    [
        (f"{VIEWER}, garbage, {CLOUDFRONT_HOP}", CLOUDFRONT_HOP),
        (f"garbage, {CLOUDFRONT_HOP}", CLOUDFRONT_HOP),
        ("garbage", ALB_PEER),
    ],
)
def test_an_unparsable_hop_stops_the_walk_on_the_last_accepted_address(xff: str, expected: str) -> None:
    """Fidelity check for the mirror: these are the answers nginx 1.31.2 itself gave (PR body)."""
    assert resolve_remote_addr(ALB_PEER, xff, trusted_networks(), recursive=recursive_enabled()) == expected


def test_nginx_conf_wires_the_include_where_the_dockerfile_installs_it() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    http = _directives(_http_level(conf))
    assert "set_real_ip_from 10.0.0.0/16;" in http
    assert "real_ip_header X-Forwarded-For;" in http
    assert "real_ip_recursive on;" in http
    include = _include_path(conf)
    assert include, "nginx.conf does not include the CloudFront origin-facing ranges at http level (#1908)"
    # Every set_real_ip_from in nginx.conf itself is the VPC; the rest comes only from the generated file.
    assert [d for d in _directives(conf) if d.startswith("set_real_ip_from ")] == ["set_real_ip_from 10.0.0.0/16;"]

    copies = re.findall(r"^COPY\s+nginx/cloudfront-origin-facing\.conf\s+(\S+)\s*$", DOCKERFILE.read_text(), re.M)
    assert copies == [include], (
        f"nginx.conf includes {include!r} but nginx/Dockerfile installs the file at {copies!r}: "
        "nginx would fail to boot on a missing include, and the image is only built after merge"
    )
    assert "/conf.d/" not in include and "/templates/" not in include, (
        "the include must not live where the base image auto-includes or envsubst-renders files"
    )


def test_generated_include_parses_and_holds_only_public_cloudfront_ranges() -> None:
    script = _load_script()
    networks, sync_token, create_date = script.parse_conf(INCLUDE_FILE.read_text(encoding="utf-8"))
    assert len(networks) >= script.MIN_PREFIXES
    assert any(n.version == 4 for n in networks)
    assert len(set(networks)) == len(networks), "duplicate prefixes"
    for net in networks:
        assert net.is_global, f"{net} is not a public range"
        assert not any(net.overlaps(x) for x in script._NON_PUBLIC if x.version == net.version), net
        assert net.prefixlen >= script.MIN_PREFIXLEN[net.version], f"{net} is broader than the script allows"
    assert sync_token.isdigit() and re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}", create_date)
    # The address the scenarios use really is in the file, so they test the real list.
    assert any(ipaddress.ip_address(CLOUDFRONT_HOP) in n for n in networks)


def test_generated_include_is_exactly_what_the_script_renders() -> None:
    """No hand edits: re-rendering the file's own set + provenance reproduces it byte for byte."""
    script = _load_script()
    text = INCLUDE_FILE.read_text(encoding="utf-8")
    networks, sync_token, create_date = script.parse_conf(text)
    assert script.render(sorted(networks, key=script._order), sync_token, create_date) == text


def _ip_ranges(extra_prefixes=(), extra_v6=()) -> dict:
    v4 = [f"52.84.{i}.0/24" for i in range(12)]
    return {
        "syncToken": "1790000000",
        "createDate": "2026-10-01-00-00-00",
        "prefixes": [{"ip_prefix": p, "service": "CLOUDFRONT_ORIGIN_FACING", "region": "GLOBAL"} for p in v4]
        + [
            {"ip_prefix": "13.32.0.0/15", "service": "CLOUDFRONT", "region": "GLOBAL"},
            {"ip_prefix": "3.5.140.0/22", "service": "EC2", "region": "ap-northeast-2"},
            *extra_prefixes,
        ],
        "ipv6_prefixes": [
            {"ipv6_prefix": "2600:9000:1000::/36", "service": "CLOUDFRONT_ORIGIN_FACING", "region": "GLOBAL"},
            {"ipv6_prefix": "2600:9000::/28", "service": "CLOUDFRONT", "region": "GLOBAL"},
            *extra_v6,
        ],
    }


def test_script_keeps_only_origin_facing_prefixes_sorted() -> None:
    script = _load_script()
    nets = [str(n) for n in script.origin_facing_networks(_ip_ranges())]
    assert "13.32.0.0/15" not in nets, "the viewer-facing CLOUDFRONT service must not be trusted"
    assert "3.5.140.0/22" not in nets and "2600:9000::/28" not in nets
    assert nets == [f"52.84.{i}.0/24" for i in range(12)] + ["2600:9000:1000::/36"]


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        ({"ip_prefix": "10.0.0.0/8", "service": "CLOUDFRONT_ORIGIN_FACING"}, "not a public range"),
        ({"ip_prefix": "0.0.0.0/0", "service": "CLOUDFRONT_ORIGIN_FACING"}, "not a public range"),
        # Public endpoints, private inside: contains 192.168.0.0/16. is_global alone says "global".
        ({"ip_prefix": "192.160.0.0/12", "service": "CLOUDFRONT_ORIGIN_FACING"}, "not a public range"),
        ({"ip_prefix": "52.0.0.0/8", "service": "CLOUDFRONT_ORIGIN_FACING"}, "broader than"),
        ({"ip_prefix": "52.84.0.1/24", "service": "CLOUDFRONT_ORIGIN_FACING"}, "not a network prefix"),
    ],
)
def test_script_refuses_input_it_should_not_trust(bad: dict, why: str) -> None:
    script = _load_script()
    with pytest.raises(script.RangesError, match=why):
        script.origin_facing_networks(_ip_ranges(extra_prefixes=[bad]))


def test_script_refuses_a_truncated_download() -> None:
    script = _load_script()
    data = _ip_ranges()
    data["prefixes"] = data["prefixes"][:3]
    with pytest.raises(script.RangesError, match="truncated"):
        script.origin_facing_networks(data)


def test_script_check_mode_reports_staleness_and_writes_nothing(tmp_path: Path) -> None:
    script = _load_script()
    source = tmp_path / "ip-ranges.json"
    source.write_text(json.dumps(_ip_ranges()))
    out = tmp_path / "cf.conf"

    assert script.main(["--check", "--source", str(source), "--output", str(out)]) == 1
    assert not out.exists(), "--check must never write"
    assert script.main(["--source", str(source), "--output", str(out)]) == 0
    written = out.read_text()
    assert script.main(["--check", "--source", str(source), "--output", str(out)]) == 0

    # A syncToken-only bump (AWS moved some OTHER service) rewrites nothing.
    bumped = _ip_ranges()
    bumped["syncToken"] = "1790009999"
    source.write_text(json.dumps(bumped))
    assert script.main(["--source", str(source), "--output", str(out)]) == 0
    assert out.read_text() == written

    # A real change in the origin-facing set is reported as stale.
    changed = _ip_ranges(extra_prefixes=[{"ip_prefix": "64.252.64.0/18", "service": "CLOUDFRONT_ORIGIN_FACING"}])
    source.write_text(json.dumps(changed))
    assert script.main(["--check", "--source", str(source), "--output", str(out)]) == 1
