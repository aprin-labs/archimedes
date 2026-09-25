"""#1526 — IPFS pinning is not live. The dead pin-client path is gone.

Issue acceptance for outcome (b): a repo grep of the vendor JWT acronym under
``backend/`` and ``infra/`` returns nothing, the pin modules do not exist,
and the backend Fargate ``secrets`` block does not inject a pin JWT. Public
copy is guarded separately in ``ui/test/ipfs-pinning-copy.test.js``.

The JWT acronym is split below so this file itself does not reintroduce the
string the issue's grep is looking for.

Hermetic: reads committed files off disk. No AWS, no network, no ``.env``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
INFRA = REPO_ROOT / "infra"
CHAIN = BACKEND / "archimedes" / "chain"
ECS_TF = INFRA / "ecs.tf"

# Issue #1526 acceptance grep is the vendor JWT acronym under backend/ and infra/.
_JWT_NEEDLE = "PIN" + "ATA"


def _text_files(root: Path) -> list[Path]:
    skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv"}
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out.append(path)
    return out


class TestPinModulesAreGone:
    def test_pin_client_module_does_not_exist(self) -> None:
        assert not (CHAIN / "pinata_client.py").exists()

    def test_provenance_publisher_module_does_not_exist(self) -> None:
        assert not (CHAIN / "provenance_publisher.py").exists()


class TestJwtNameAbsentFromBackendAndInfra:
    """The issue's acceptance grep, as a test.

    A comment, env example, or SSM seed list that names the JWT is the
    half-wired path #1526 forbids: code that implies pinning we do not do.
    """

    @pytest.mark.parametrize("root", [BACKEND, INFRA], ids=["backend", "infra"])
    def test_vendor_jwt_name_is_absent(self, root: Path) -> None:
        hits: list[str] = []
        for path in _text_files(root):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _JWT_NEEDLE in line:
                    rel = path.relative_to(REPO_ROOT)
                    hits.append(f"{rel}:{n}: {line.strip()}")
        assert hits == [], (
            f"{_JWT_NEEDLE} must not appear under {root.relative_to(REPO_ROOT)}/ "
            f"(#1526 outcome b — pinning is not live). Found: {hits}"
        )


class TestEcsDoesNotInjectAPinJwt:
    def test_backend_secrets_block_does_not_name_the_pin_jwt(self) -> None:
        src = ECS_TF.read_text(encoding="utf-8")
        assert ECS_TF.is_file()
        # The JWT is not a backend secret, an environment entry, or a comment
        # that would read as "prod has this".
        assert _JWT_NEEDLE not in src
