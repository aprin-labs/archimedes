#!/usr/bin/env python3
"""Staleness report: `status: current` docs whose `updated` date is over 60 days old.

INFORMATIONAL ONLY. This never blocks a merge, and the workflow runs it under
`continue-on-error`. A stale `updated` date means "nobody has confirmed this
against the running system recently" — that is a prompt to go and verify, not
evidence that the doc is wrong. Blocking on it would make every unrelated PR
responsible for someone else's un-refreshed doc, and the predictable response
is a mass rubber-stamp date-bump commit, which destroys the signal the field
exists to carry.

Reads the front-matter schema from docs/CONVENTIONS.md: a blockquote block of
`> **key:** value` lines at the top of the file. YAML front-matter is also
accepted so the check keeps working if the convention moves.

Usage:
    python .github/scripts/docs_staleness.py [--root .] [--days 60] [--format text|markdown]
Always exits 0 — it reports, it does not judge.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path

BQ_FIELD = re.compile(r"^>\s*\*\*(?P<key>[a-z-]+):\*\*\s*(?P<val>.*?)\s*$", re.MULTILINE)
YAML_FIELD = re.compile(r"^(?P<key>[a-z-]+):\s*(?P<val>.*?)\s*$", re.MULTILINE)
DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# Index rows: | [`path`](path) | status | owner | YYYY-MM-DD | what it is |
# Most docs in this tree declare status/verification date in the index table
# rather than in per-file front-matter — front-matter adoption is still six
# files. Reading both means the report is useful today instead of after a
# tree-wide front-matter migration that may never happen.
INDEX_ROW = re.compile(
    r"^\|\s*\[`(?P<path>[^`]+)`\]\([^)]*\)\s*\|\s*(?P<status>[a-z]+)\s*\|"
    r"\s*(?P<owner>[^|]*?)\s*\|\s*(?P<verified>[^|]*?)\s*\|",
    re.MULTILINE,
)


def front_matter(text: str) -> dict[str, str]:
    head = "\n".join(text.splitlines()[:20])
    fields = {m.group("key"): m.group("val") for m in BQ_FIELD.finditer(head)}
    if not fields and head.startswith("---"):
        body = head.split("---", 2)
        if len(body) >= 3:
            fields = {m.group("key"): m.group("val") for m in YAML_FIELD.finditer(body[1])}
    return fields


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--days", type=int, default=60, help="staleness threshold (CONVENTIONS.md: 60)")
    ap.add_argument("--format", choices=("text", "markdown"), default="text")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=args.days)

    stale: list[tuple[str, dt.date, int, str]] = []
    undated: list[str] = []
    current_count = 0

    for dirpath, dirnames, filenames in os.walk(root / "docs"):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        # archive/ is historical by definition; its dates are supposed to be old.
        if "archive" in Path(dirpath).relative_to(root).parts:
            continue
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            f = Path(dirpath) / name
            rel = f.relative_to(root).as_posix()
            fm = front_matter(f.read_text(encoding="utf-8", errors="replace"))
            if fm.get("status", "").strip().lower() != "current":
                continue
            current_count += 1
            m = DATE.search(fm.get("updated", ""))
            if not m:
                undated.append(rel)
                continue
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d < cutoff:
                stale.append((rel, d, (today - d).days, fm.get("owner", "—").strip() or "—"))

    # Second source: the docs/doc-index.md register table and its sub-indexes
    # (the per-directory README.md files it links to). The register moved out of
    # docs/README.md on 2026-09-02 — see .github/scripts/docs_index.py.
    seen = {r[0] for r in stale} | set(undated)
    indexes = [root / "docs" / "doc-index.md", *sorted((root / "docs").rglob("README.md"))]
    for idx in indexes:
        if not idx.is_file() or "archive" in idx.relative_to(root).parts:
            continue
        for m in INDEX_ROW.finditer(idx.read_text(encoding="utf-8", errors="replace")):
            if m.group("status").strip().lower() != "current":
                continue
            try:
                target = (idx.parent / m.group("path")).resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            rel = target.as_posix()
            if rel in seen or not (root / rel).is_file():
                continue
            seen.add(rel)
            current_count += 1
            dm = DATE.search(m.group("verified"))
            if not dm:
                undated.append(rel)
                continue
            d = dt.date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
            if d < cutoff:
                stale.append((rel, d, (today - d).days, m.group("owner").strip() or "—"))

    stale.sort(key=lambda r: r[1])

    if args.format == "markdown":
        print(f"**Staleness** — `status: current` docs not verified in {args.days}+ days.")
        print(f"Checked {current_count} `current` docs on {today}. Informational; does not block.\n")
        if stale:
            print("| Doc | Updated | Days | Owner |")
            print("|---|---|---|---|")
            for rel, d, age, owner in stale:
                print(f"| `{rel}` | {d} | {age} | {owner} |")
        else:
            print(f"No `current` doc is older than {args.days} days.")
        if undated:
            print(f"\nMissing an `updated` field ({len(undated)}): " + ", ".join(f"`{u}`" for u in undated))
    else:
        for rel, d, age, owner in stale:
            print(f"{rel}: updated {d} ({age} days ago), owner {owner}")
        for u in undated:
            print(f"{u}: status current, no `updated` date")
        print(
            f"\nstaleness: {len(stale)} of {current_count} `current` docs older than "
            f"{args.days} days; {len(undated)} missing an `updated` date. Informational."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
