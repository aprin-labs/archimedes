"""Assert exactly one OpenSSL is mapped into the backend process (issue #1632).

Why this exists
---------------
Production backend was dying roughly ten minutes after every boot, on the
paper-replay tick, with::

    Fatal Python error: Aborted

and nothing else. No Python traceback. No glibc ``*** buffer overflow
detected ***`` banner, no C++ ``terminate called``, no OpenSSL error string —
a bare C ``abort()`` raised from inside psycopg2's ``do_executemany`` while
committing the OHLCV cache write.

The leading mechanism is two OpenSSL builds sharing one address space. The
image shipped ``psycopg2-binary``, whose wheel vendors its own libpq *and* its
own ``libssl``/``libcrypto`` under ``psycopg2_binary.libs/``. Meanwhile stdlib
``_ssl`` (and therefore aiohttp, httpx, web3, boto3) links the interpreter's
OpenSSL. Two copies of the same soname export the same symbols; the dynamic
linker resolves each call site to whichever landed first, so a struct
allocated by one build can be freed or introspected by the other. The failure
mode of that is precisely a silent ``abort()`` with no diagnostic, far from
the line that caused it. psycopg2's own installation docs warn against the
binary package in production for exactly this reason.

``backend/requirements-base.txt`` now installs source-built ``psycopg2``, which
links the system libpq and therefore the system OpenSSL — one copy, one owner.
This module is the guard that keeps it that way. It is deliberately a *runtime*
check rather than a lockfile assertion: a vendored ``libssl`` can arrive inside
any future wheel, from any dependency, without ``psycopg2`` ever being mentioned
again. Reading ``/proc/self/maps`` catches the class, not the instance.

Usage
-----
Run it inside the built image (this is what ``.github/workflows/deploy.yml``
does, right after the ``/health`` boot validation)::

    docker run --rm archimedes-backend:ci python -m archimedes.scripts.check_single_openssl

Exit code 0 = one OpenSSL. Exit code 1 = the incident condition, with every
mapped path printed.

Linux-only by construction: ``/proc/self/maps`` is the only portable-enough way
to enumerate what the dynamic linker actually mapped, and Linux is what
production runs. On macOS the entry point reports "not applicable" and exits 0;
the parsing logic is still unit-tested there — see
``backend/tests/test_single_openssl.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MAPS = Path("/proc/self/maps")

# Both halves of OpenSSL matter. libcrypto is where the allocator, the error
# stack and the EVP/BIO object headers live, so a duplicated libcrypto is at
# least as dangerous as a duplicated libssl — and a wheel that vendors one
# essentially always vendors the other.
OPENSSL_SONAME_PREFIXES = ("libssl", "libcrypto")

INCIDENT = (
    "Two OpenSSL builds are mapped into one process. This is the #1632 crash class: "
    "prod died with a bare `Fatal Python error: Aborted` (a raw C abort(), no traceback, "
    "no glibc/terminate/SSL message) inside psycopg2 do_executemany on the OHLCV cache "
    "commit, ~10 min after boot. The image at the time shipped psycopg2-binary, whose "
    "wheel vendors its own libssl/libcrypto (including an OpenSSL 1.1.1k) alongside the "
    "interpreter's — the leading but UNPROVEN hypothesis for that abort (#1729's A/B "
    "reproduced the co-residency, not the crash). Proven or not, one process must load "
    "one OpenSSL. Whatever dependency just reintroduced a "
    "second OpenSSL: use the source-built distribution, or a wheel that links the system "
    "OpenSSL. See backend/requirements-base.txt."
)


def parse_openssl_mappings(maps_text: str) -> list[str]:
    """Return the distinct on-disk OpenSSL libraries named in ``/proc/self/maps`` text.

    ``/proc/<pid>/maps`` lines are ``address perms offset dev inode pathname``,
    where ``pathname`` is optional and may itself contain spaces — hence the
    ``maxsplit=5`` rather than a naive ``.split()[-1]``.

    Only *real files* count. Anonymous mappings have no pathname, and the kernel
    writes pseudo-paths in brackets (``[heap]``, ``[stack]``, ``[vvar]``) plus
    suffixes like ``(deleted)`` for unlinked files; none of those are a second
    OpenSSL. Each library is also mapped several times over (r--p, r-xp, rw-p
    segments), so the result is deduplicated — a single library appearing four
    times is one OpenSSL, not four.
    """
    found: list[str] = []
    seen: set[str] = set()

    for line in maps_text.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 6:
            continue  # anonymous mapping — no pathname field at all
        path = fields[5].strip()
        if not path or path.startswith("["):
            continue  # [heap], [stack], [vdso], …
        path = path.removesuffix(" (deleted)").strip()
        name = os.path.basename(path)
        if not name.startswith(OPENSSL_SONAME_PREFIXES):
            continue
        # Resolve symlinks so libssl.so.3 -> libssl.so.3.5.4 is not counted as
        # two separate OpenSSL builds. Distinct *real* files are the signal.
        #
        # Only for paths that exist: this function is also fed captured maps text
        # from another machine (the regression fixtures in
        # backend/tests/test_single_openssl.py), where resolving against the
        # local filesystem would be meaningless and, on a host with different
        # symlinks, actively wrong.
        try:
            resolved = os.path.realpath(path) if os.path.exists(path) else path
        except OSError:  # pragma: no cover — a path we cannot stat is still evidence
            resolved = path
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)

    return sorted(found)


def load_the_openssl_consumers() -> None:
    """Import the modules that actually pull OpenSSL in, before reading the maps.

    Order matters only in that all of them must be loaded before the check runs;
    a mapping that has not happened yet cannot be observed. These are the real
    production consumers:

    * ``ssl``      — stdlib ``_ssl``, linked against the interpreter's OpenSSL.
    * ``psycopg2`` — libpq, which is itself linked against OpenSSL. This is the
      import that used to bring in the *second* copy.
    * ``aiohttp``  — the async HTTP client under web3's provider; it uses stdlib
      ``ssl``, and is imported here because it is the other named party in the
      incident and its presence is what makes the two copies coexist under load.
    """
    import ssl  # noqa: F401

    import aiohttp  # noqa: F401
    import psycopg2  # noqa: F401


def main() -> int:
    if not MAPS.exists():
        print(f"check_single_openssl: {MAPS} not available (not Linux) — nothing to check.")
        return 0

    load_the_openssl_consumers()

    libraries = parse_openssl_mappings(MAPS.read_text())

    # An empty result is NOT a pass. Importing `ssl` must map an OpenSSL; if it
    # mapped none, either the parser stopped matching or the interpreter links
    # OpenSSL statically, and in both cases this guard has silently stopped
    # guarding. Fail rather than pass vacuously (CLAUDE.md rule 4).
    if not libraries:
        print(
            "check_single_openssl: FAIL — no OpenSSL library found in /proc/self/maps at all, "
            "even after importing ssl + psycopg2 + aiohttp. This guard cannot see what it is "
            "supposed to guard; do not read it as a pass.",
            file=sys.stderr,
        )
        return 1

    # Group for reporting: libssl and libcrypto are two libraries, not two builds.
    by_soname: dict[str, list[str]] = {}
    for path in libraries:
        stem = "libssl" if os.path.basename(path).startswith("libssl") else "libcrypto"
        by_soname.setdefault(stem, []).append(path)

    duplicated = {stem: paths for stem, paths in by_soname.items() if len(paths) > 1}

    if duplicated:
        print("check_single_openssl: FAIL", file=sys.stderr)
        for stem, paths in sorted(duplicated.items()):
            print(f"  {len(paths)} distinct {stem} builds mapped:", file=sys.stderr)
            for path in paths:
                print(f"    - {path}", file=sys.stderr)
        print(f"\n{INCIDENT}", file=sys.stderr)
        return 1

    print("check_single_openssl: OK — exactly one OpenSSL build mapped.")
    for path in libraries:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
