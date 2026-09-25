#!/usr/bin/env python3
"""Alembic migration-chain fork guard (CI).

The recurring failure this exists to catch: two branches each add a new
migration whose `down_revision` points at the SAME parent — the chain head
each branch saw when it forked from main. Whichever merges first becomes the
new head; the other branch's migration is now parented on a revision that is
no longer the head, which `alembic upgrade head` cannot resolve on its own —
a silent fork. This has bitten three times already (the Aug 3 deploy, and
twice on #1194) before this guard existed.

Two different reference points are used deliberately, not interchangeably:
  - `--base-ref`'s merge-base with HEAD scopes step (a): "which migration
    files did THIS PR add or change" (the standard three-dot-diff idiom used
    elsewhere in this repo, e.g. import-guard.yml).
  - `--base-ref` itself (its CURRENT tip, freshly fetched) is the chain those
    new migrations must extend — the actual thing "re-serialize onto main"
    means. Using the historical merge-base for this instead would silently
    pass a migration that forked against a revision another PR has since
    made obsolete, which is exactly the bug class this guard exists to catch.

Usage: python .github/scripts/migration_chain_guard.py [--base-ref REF]
Exit 0 = pass (including "nothing to check"). Exit 1 = fork / stale parent
detected, or main's own chain isn't a clean single-head chain to begin with.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

VERSIONS = "backend/migrations/versions"

# Matches the shape script.py.mako generates:
#   revision: str = "07e9c1489199"
#   down_revision: str | Sequence[str] | None = "af9c6a9376e4"  (or None)
REV_RE = re.compile(r'^revision(?:\s*:[^=\n]+)?\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
DOWN_RE = re.compile(r'^down_revision(?:\s*:[^=\n]+)?\s*=\s*(None|["\']([^"\']+)["\'])', re.MULTILINE)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"FAIL: `git {' '.join(args)}` failed:\n{result.stderr}")
    return result.stdout


def _parse(text: str, where: str) -> tuple[str, str | None]:
    rev_m, down_m = REV_RE.search(text), DOWN_RE.search(text)
    if not rev_m or not down_m:
        sys.exit(
            f"FAIL: could not find `revision =` / `down_revision =` in {where}.\n"
            "This guard only understands a bare string or None (the standard "
            "script.py.mako shape). A tuple down_revision (merge migration) needs "
            "manual review — teach migration_chain_guard.py that shape if it's intentional."
        )
    return rev_m.group(1), (None if down_m.group(1) == "None" else down_m.group(2))


def _chain_at(repo: Path, ref: str) -> dict[str, str | None]:
    """revision -> down_revision for every migration file as of git ref `ref`."""
    listing = _git(repo, "ls-tree", "-r", "--name-only", ref, "--", VERSIONS)
    chain: dict[str, str | None] = {}
    for path in listing.splitlines():
        if path.endswith(".py"):
            rev, down = _parse(_git(repo, "show", f"{ref}:{path}"), f"{path} @ {ref}")
            chain[rev] = down
    return chain


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-ref", default="origin/main")
    args = ap.parse_args()

    repo = Path(_git(Path("."), "rev-parse", "--show-toplevel").strip())
    diff_base = _git(repo, "merge-base", "HEAD", args.base_ref).strip()

    changed = [
        p
        for p in _git(repo, "diff", "--name-only", "--diff-filter=ACMR", diff_base, "HEAD", "--", VERSIONS).splitlines()
        if p.endswith(".py")
    ]
    if not changed:
        print(f"migration_chain_guard: no migration files changed vs {diff_base[:12]} — nothing to check.")
        return 0

    main_chain = _chain_at(repo, args.base_ref)
    if main_chain:
        heads = [r for r in main_chain if r not in main_chain.values()]
        if len(heads) != 1:
            # main is already forked (two PRs merged whose CI each saw a single
            # head — 2026-09-04, #1844 + #1845). The one PR that must still be
            # able to merge is the repair: it re-points an existing migration
            # so the chain has one head again, and adds nothing. Anything else
            # (a third head, a repair that also grafts a new migration, a
            # "repair" that drops a revision) keeps failing — the guard cannot
            # tell which head a NEW migration should extend until main is fixed.
            pr_chain = _chain_at(repo, "HEAD")
            pr_heads = [r for r in pr_chain if r not in pr_chain.values()]
            adds_nothing = set(pr_chain) == set(main_chain)
            if len(pr_heads) == 1 and adds_nothing:
                print(
                    f"migration_chain_guard: OK — {args.base_ref} has {len(heads)} heads "
                    f"({heads}); this PR re-serialises them into one chain (head {pr_heads[0]}) "
                    "and adds no new migration. Repair accepted."
                )
                return 0
            print(
                f"FAIL: {args.base_ref}'s migration chain doesn't have exactly one head "
                f"(found {heads}) — this guard can't tell which one your migration should "
                "extend. Fix main's chain before this guard can validate new migrations "
                "(a repair PR that only re-points existing migrations onto one head passes)."
            )
            return 1
        main_head = heads[0]
    else:
        main_head = None  # no migrations exist yet on base-ref

    new_revs: dict[str, str | None] = {}
    paths: dict[str, str] = {}
    for p in changed:
        rev, down = _parse((repo / p).read_text(), p)
        if rev not in main_chain:  # skip edits to migrations that already exist on base-ref
            new_revs[rev], paths[rev] = down, p

    if not new_revs:
        print("migration_chain_guard: changed migration files are all edits to existing revisions — nothing to check.")
        return 0

    roots = [rev for rev, down in new_revs.items() if down not in new_revs]
    if not roots:
        print(
            f"FAIL: new migrations {list(paths.values())} form a cycle — no revision roots outside this PR's own set."
        )
        return 1
    if len(roots) > 1:
        print(
            f"FAIL: {len(roots)} new migrations each extend from OUTSIDE this PR's own chain "
            f"({[paths[r] for r in roots]}) instead of one linear line — only one new migration "
            "may graft onto main; chain the rest after it."
        )
        return 1

    root = roots[0]
    down = new_revs[root]
    if down != main_head:
        print(
            f"FAIL: {paths[root]} has down_revision={down!r}, but {args.base_ref}'s migration "
            f"chain head is {main_head!r}.\n"
            "Someone else's migration merged to main after you branched — re-serialize:\n"
            "  1. git fetch origin main\n"
            f'  2. edit down_revision in {paths[root]} to "{main_head}"\n'
            "  3. from backend/: `alembic history` should show one unbroken chain, one head."
        )
        return 1

    print(f"migration_chain_guard: OK — {list(paths.values())} extend {args.base_ref}'s head ({main_head}) cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
