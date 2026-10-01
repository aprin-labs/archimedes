#!/usr/bin/env python3
"""Index-completeness check: every doc must be reachable from docs/doc-index.md.

Blocking check for .github/workflows/docs-gate.yml. Self-contained.

docs/doc-index.md opens with "A doc not listed here does not exist." This script
is what makes that sentence true instead of aspirational. Without it the index
is correct only until the next person adds a file, which is exactly how the
tree got into the state phases 1-5 dug it out of — eleven files landed with no
structure in the four weeks before the cleanup started.

The register moved out of docs/README.md on 2026-09-02: docs/README.md and
docs/index.md cannot coexist in an mkdocs build (`--strict` aborts with
"Excluding README.md from the site because it conflicts with index.md"), and
the site needed index.md to be a front door rather than a register. The rule is
unchanged; only the file holding it moved.

Coverage rule: a `docs/**/*.md` file is covered if it is linked from
docs/doc-index.md, or from a sub-index that docs/doc-index.md itself links to
(adr/README.md, archive/README.md, quant/README.md, and any other
`*/README.md` the root index points at — discovered, not hardcoded, so a new
section only has to be linked once).

Usage:
    python .github/scripts/docs_index.py [--root .] [--format text|github]
Exit status: 0 = every doc is indexed, 1 = at least one orphan.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docs_links import INLINE_LINK, REF_DEF, is_external, strip_code  # noqa: E402

# Files that are structural rather than content: they are the index itself, or
# they are conventions the index links to as prose. They never need a row.
ALWAYS_COVERED = {"docs/doc-index.md"}


def link_targets(path: Path, root: Path) -> set[str]:
    """Repo-relative posix paths this markdown file links to."""
    text = strip_code(path.read_text(encoding="utf-8", errors="replace"))
    out: set[str] = set()
    for m in list(INLINE_LINK.finditer(text)) + list(REF_DEF.finditer(text)):
        target = m.group(1).strip()
        if not target or target.startswith(("#", "/")) or is_external(target):
            continue
        path_part = target.split("#", 1)[0].split("?", 1)[0]
        if not path_part:
            continue
        try:
            resolved = (path.parent / path_part).resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        out.add(resolved.as_posix())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--format", choices=("text", "github"), default="text")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    docs = root / "docs"
    index = docs / "doc-index.md"
    if not index.is_file():
        print("::error::docs/doc-index.md is missing — the register is the contract; restore it.")
        return 1

    root_targets = link_targets(index, root)

    # Sub-indexes: any README.md under docs/ that the root index links to.
    sub_indexes = sorted(
        root / t for t in root_targets if t.startswith("docs/") and t.endswith("README.md") and (root / t).is_file()
    )

    covered: set[str] = set(root_targets) | set(ALWAYS_COVERED)
    for sub in sub_indexes:
        covered.add(sub.relative_to(root).as_posix())
        covered |= link_targets(sub, root)

    all_docs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(docs):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(".md"):
                all_docs.append((Path(dirpath) / name).relative_to(root).as_posix())

    orphans = sorted(d for d in all_docs if d not in covered)

    for o in orphans:
        msg = (
            "not listed in docs/doc-index.md or any sub-index it links to. "
            "Add a row to the register, or move the file under a directory whose "
            "sub-index lists it, or delete it."
        )
        if args.format == "github":
            print(f"::error file={o}::{msg}")
        else:
            print(f"{o}: {msg}")

    print(
        f"\nindex: {len(all_docs)} docs under docs/; "
        f"{len(all_docs) - len(orphans)} indexed, {len(orphans)} orphaned. "
        f"Sub-indexes honoured: {', '.join(s.relative_to(root).as_posix() for s in sub_indexes) or 'none'}"
    )
    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
