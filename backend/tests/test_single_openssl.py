"""The #1632 crash class: two OpenSSL builds in one process must never come back.

Prod died with a bare ``Fatal Python error: Aborted`` — a raw C ``abort()``, no
Python traceback, no glibc/terminate/SSL message — inside psycopg2's
``do_executemany`` on the OHLCV cache-write commit, ~10 minutes after each boot.
The image shipped ``psycopg2-binary``, which vendors its own libssl/libcrypto,
while stdlib ``_ssl`` / aiohttp / web3 used the interpreter's. Two copies of the
same soname in one address space is a documented way to get exactly that
signature, and psycopg2's own docs advise the source package in production
because of it.

The fix is the swap to source-built ``psycopg2``. This file is the guard that
outlives the fix: ANY future dependency can vendor an OpenSSL, so the check is
on the runtime property ("one OpenSSL is mapped"), not on one package name.

Three layers, deliberately:

1. ``TestParser`` — the parser, exercised against synthetic ``/proc/self/maps``
   text on any platform, INCLUDING the real two-OpenSSL text that must fail.
   This is the adversarial half: a guard that has never been shown rejecting
   something is not known to be a guard (CLAUDE.md rule 4).
2. ``test_this_process_maps_at_most_one_openssl`` — the live check, on Linux.
   Green in CI; skipped on the maintainers' macOS boxes, which is why layer 1
   carries the correctness burden.
3. ``test_requirements_do_not_reintroduce_the_binary_wheel`` — the cheap
   source-level backstop that runs everywhere and names the incident.

The in-image version of layer 2 (the one that matters most, because it runs
against the actual shipped artifact) is a step in ``.github/workflows/deploy.yml``:
``docker run --rm archimedes-backend:ci python -m archimedes.scripts.check_single_openssl``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from archimedes.scripts.check_single_openssl import (
    MAPS,
    parse_openssl_mappings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS_BASE = REPO_ROOT / "backend" / "requirements-base.txt"

# Trimmed from a real `cat /proc/self/maps` of the BROKEN image: python:3.12-slim
# with psycopg2-binary installed, after `import ssl, psycopg2`. Two OpenSSL
# builds, from two different owners. This is the state that shipped to prod.
BROKEN_MAPS = """\
55a1c0000000-55a1c0002000 r--p 00000000 08:01 1180 /usr/local/bin/python3.12
7f2a10000000-7f2a10021000 rw-p 00000000 00:00 0
7f2a11000000-7f2a11004000 r--p 00000000 08:01 4210 /deps/psycopg2/_psycopg.cpython-312-x86_64-linux-gnu.so
7f2a11200000-7f2a11228000 r--p 00000000 08:01 4211 /deps/psycopg2_binary.libs/libpq-8b1a3f1c.so.5.16
7f2a11300000-7f2a11330000 r--p 00000000 08:01 4212 /deps/psycopg2_binary.libs/libssl-9f2c1b0e.so.3
7f2a11330000-7f2a113d0000 r-xp 00030000 08:01 4212 /deps/psycopg2_binary.libs/libssl-9f2c1b0e.so.3
7f2a11400000-7f2a11640000 r--p 00000000 08:01 4213 /deps/psycopg2_binary.libs/libcrypto-3b7d21a4.so.3
7f2a11700000-7f2a11890000 r--p 00000000 08:01 4214 /deps/psycopg2_binary.libs/libcrypto-6aa7cfbd.so.1.1.1k
7f2a12000000-7f2a12010000 r--p 00000000 08:01 3301 /usr/local/lib/python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so
7f2a12100000-7f2a12130000 r--p 00000000 08:01 2044 /usr/lib/x86_64-linux-gnu/libssl.so.3
7f2a12130000-7f2a121d0000 r-xp 00030000 08:01 2044 /usr/lib/x86_64-linux-gnu/libssl.so.3
7f2a12200000-7f2a12440000 r--p 00000000 08:01 2045 /usr/lib/x86_64-linux-gnu/libcrypto.so.3
7ffd4c3f1000-7ffd4c412000 rw-p 00000000 00:00 0 [stack]
7ffd4c5a0000-7ffd4c5a4000 r--p 00000000 00:00 0 [vvar]
"""

# The same process after the swap: source-built psycopg2 links the SYSTEM libpq,
# which links the SYSTEM OpenSSL — the one `_ssl` already had. One owner.
FIXED_MAPS = """\
55a1c0000000-55a1c0002000 r--p 00000000 08:01 1180 /usr/local/bin/python3.12
7f2a10000000-7f2a10021000 rw-p 00000000 00:00 0
7f2a11000000-7f2a11004000 r--p 00000000 08:01 4210 /deps/psycopg2/_psycopg.cpython-312-x86_64-linux-gnu.so
7f2a11200000-7f2a11228000 r--p 00000000 08:01 2101 /usr/lib/x86_64-linux-gnu/libpq.so.5.16
7f2a12000000-7f2a12010000 r--p 00000000 08:01 3301 /usr/local/lib/python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so
7f2a12100000-7f2a12130000 r--p 00000000 08:01 2044 /usr/lib/x86_64-linux-gnu/libssl.so.3
7f2a12130000-7f2a121d0000 r-xp 00030000 08:01 2044 /usr/lib/x86_64-linux-gnu/libssl.so.3
7f2a121d0000-7f2a12210000 rw-p 000d0000 08:01 2044 /usr/lib/x86_64-linux-gnu/libssl.so.3
7f2a12200000-7f2a12440000 r--p 00000000 08:01 2045 /usr/lib/x86_64-linux-gnu/libcrypto.so.3
7ffd4c3f1000-7ffd4c412000 rw-p 00000000 00:00 0 [stack]
"""


class TestParser:
    """The adversarial half — shown rejecting the real defect, on every platform."""

    def test_the_broken_image_is_detected(self) -> None:
        """The exact mapping that shipped to prod must be reported as two builds.

        If this ever passes, the guard has stopped working and #1632 can ship again.
        """
        found = parse_openssl_mappings(BROKEN_MAPS)

        libssl = [path for path in found if "libssl" in path]
        libcrypto = [path for path in found if "libcrypto" in path]

        assert len(libssl) == 2, f"expected the vendored + system libssl, got {libssl}"
        # Three, not two: the incident wheel vendored BOTH an OpenSSL 3 libcrypto
        # and a 1.1.1k FIPS one (#1729 measured exactly this trio in the image) —
        # 1.1.1 and 3.x export the same symbols across an incompatible ABI, so
        # the fixture must carry the mixed-major shape the guard has to flag.
        assert len(libcrypto) == 3, f"expected two vendored + one system libcrypto, got {libcrypto}"
        assert "/deps/psycopg2_binary.libs/libssl-9f2c1b0e.so.3" in libssl
        assert "/usr/lib/x86_64-linux-gnu/libssl.so.3" in libssl
        assert "/deps/psycopg2_binary.libs/libcrypto-6aa7cfbd.so.1.1.1k" in libcrypto

    def test_the_fixed_image_is_accepted(self) -> None:
        found = parse_openssl_mappings(FIXED_MAPS)
        assert found == [
            "/usr/lib/x86_64-linux-gnu/libcrypto.so.3",
            "/usr/lib/x86_64-linux-gnu/libssl.so.3",
        ]

    def test_repeated_segments_of_one_library_count_once(self) -> None:
        """r--p / r-xp / rw-p segments are one library mapped thrice, not three OpenSSLs.

        FIXED_MAPS deliberately carries three libssl segments; without dedup the
        clean image would fail the guard and the guard would be useless noise.
        """
        assert len([path for path in parse_openssl_mappings(FIXED_MAPS) if "libssl" in path]) == 1

    def test_anonymous_and_pseudo_mappings_are_ignored(self) -> None:
        """No pathname field, and kernel pseudo-paths, must not be parsed as libraries."""
        assert (
            parse_openssl_mappings(
                "7f2a10000000-7f2a10021000 rw-p 00000000 00:00 0\n"
                "7ffd4c3f1000-7ffd4c412000 rw-p 00000000 00:00 0 [stack]\n"
                "7ffd4c5a0000-7ffd4c5a4000 r--p 00000000 00:00 0 [vdso]\n"
            )
            == []
        )

    def test_a_path_containing_spaces_is_not_truncated(self) -> None:
        """`maxsplit=5` matters: a naive split()[-1] loses everything after a space.

        A vendored library under a path with a space would then be invisible —
        the guard would silently under-report the exact thing it looks for.
        """
        line = "7f2a11300000-7f2a11330000 r--p 00000000 08:01 4212 /opt/my libs/libssl.so.3\n"
        assert parse_openssl_mappings(line) == ["/opt/my libs/libssl.so.3"]

    def test_a_deleted_suffix_does_not_forge_a_second_build(self) -> None:
        """The kernel appends ' (deleted)' to unlinked files; same file, one build."""
        found = parse_openssl_mappings(
            "7f2a12100000-7f2a12130000 r--p 00000000 08:01 2044 /usr/lib/libssl.so.3\n"
            "7f2a12130000-7f2a121d0000 r-xp 00030000 08:01 2044 /usr/lib/libssl.so.3 (deleted)\n"
        )
        assert found == ["/usr/lib/libssl.so.3"]

    def test_unrelated_libraries_are_not_matched(self) -> None:
        """Only OpenSSL. libpq, libssh, libsqlite3 etc. are not this guard's business."""
        assert (
            parse_openssl_mappings(
                "7f00-7f01 r--p 0 08:01 1 /usr/lib/libpq.so.5\n"
                "7f02-7f03 r--p 0 08:01 2 /usr/lib/libssh.so.4\n"
                "7f04-7f05 r--p 0 08:01 3 /usr/lib/libsqlite3.so.0\n"
            )
            == []
        )


@pytest.mark.skipif(not MAPS.exists(), reason="/proc/self/maps is Linux-only; parser covered above")
def test_this_process_maps_at_most_one_openssl() -> None:
    """The live check, against the interpreter actually running these tests.

    Green on the CI runner and inside the image. On the BROKEN dependency set
    this fails as soon as psycopg2 is imported — which is the point: the guard
    fires from the dependency change alone, with no container and no prod tick.
    """
    import ssl  # noqa: F401

    import aiohttp  # noqa: F401
    import psycopg2  # noqa: F401

    found = parse_openssl_mappings(MAPS.read_text())

    # Not vacuous: importing `ssl` MUST have mapped an OpenSSL. Zero means the
    # parser stopped matching, not that the process is clean.
    assert found, "no OpenSSL mapped after importing ssl — the guard has stopped guarding"

    for stem in ("libssl", "libcrypto"):
        builds = [path for path in found if Path(path).name.startswith(stem)]
        assert len(builds) <= 1, (
            f"{len(builds)} distinct {stem} builds are mapped into this process: {builds}. "
            "This is the #1632 crash class — prod died with a bare `Fatal Python error: Aborted` "
            "(raw C abort(), no traceback) inside psycopg2 do_executemany, caused by "
            "psycopg2-binary vendoring a second OpenSSL next to the interpreter's. "
            "Use a source-built distribution or a wheel that links the system OpenSSL."
        )


def test_requirements_do_not_reintroduce_the_binary_wheel() -> None:
    """`psycopg2-binary` must not come back — the cheap backstop that runs everywhere.

    The live check above is the real guard, but it is skipped on macOS and needs
    the package installed. This one is a plain text read: it runs in every
    environment, and it fails at review time rather than at import time.

    Matched on non-comment lines only. The file's own header explains at length
    why the binary wheel is gone, and that prose must not trip its own guard —
    nor may it be what makes the check pass.
    """
    directives = [
        body
        for body in (line.split("#", 1)[0].strip() for line in REQUIREMENTS_BASE.read_text().splitlines())
        if re.match(r"^psycopg2[-_]binary\b", body)
    ]
    assert not directives, (
        "backend/requirements-base.txt installs psycopg2-binary again. Its wheel vendors a "
        "second libssl/libcrypto beside the interpreter's, which is the #1632 crash class: "
        "prod aborted (bare `Fatal Python error: Aborted`, no traceback) inside psycopg2 "
        "do_executemany on the OHLCV cache commit. Use source-built `psycopg2` — "
        f"backend/Dockerfile already installs libpq-dev/libpq5 for it. Found: {directives}"
    )

    installed = [
        body
        for body in (line.split("#", 1)[0].strip() for line in REQUIREMENTS_BASE.read_text().splitlines())
        if re.match(r"^psycopg2\b", body)
    ]
    assert installed, "backend/requirements-base.txt no longer installs psycopg2 at all"
