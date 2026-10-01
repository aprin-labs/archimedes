"""No git conflict marker may be tracked, anywhere.

A ``>>>>>>> origin/main`` line sat inside ``strategy_fusion.py``'s ``_SPEC_CONTRACT``
from ``bdb93547`` until #1744 removed it — i.e. inside BOTH proposer prompts, shipped
to the model on every generation call — and nothing blocking noticed, because a
marker inside a string literal is valid Python, and the ``check-merge-conflict``
pre-commit hook is advisory (pre-commit is not run in CI). This test is the guard
that was missing: it scans every tracked text file.

What counts as a marker: a line that BEGINS with seven-or-more ``<`` or seven-or-more
``>``, followed by whitespace or end-of-line. Seven-or-more, not exactly seven,
because git honours the ``conflict-marker-size`` attribute; whitespace-or-EOL, not
"a space then a label", because ``git merge-file`` emits bare unlabeled markers. The
middle ``=======`` line is deliberately NOT policed: a real conflict always carries
the two outer markers, and a bare run of ``=`` is a legitimate Markdown/RST setext
heading underline, so policing it would only add false positives.

The scan reads the WORKING TREE of every tracked path, so it also goes red in the
middle of an unfinished ``git merge`` — the other honest reason to see it fail. There
is no allowlist: this file itself contains no line-start marker, and an allowlist
would be the one place a marker could hide.

Hermetic: ``git ls-files`` plus file reads, no network, no DB. MUTATIONS (run before
pushing): append ``>>>>>>> origin/main`` to any tracked ``.py`` or ``.md`` (or to this
file) — red naming that file and line; a 32-char marker from ``conflict-marker-size``
and a bare unlabeled ``<<<<<<<`` are red too; remove them and it is green again.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_MARKER = re.compile(rb"^(<{7,}|>{7,})(\s|$)")


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [REPO_ROOT / p.decode() for p in out.split(b"\0") if p]


def _looks_binary(head: bytes) -> bool:
    return b"\0" in head


def _marker_hits(path: Path) -> list[str]:
    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):  # gitlinks (submodules) and deleted-but-tracked
        return []
    if _looks_binary(data[:8192]):
        return []
    shown = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path.name
    hits: list[str] = []
    for lineno, line in enumerate(data.splitlines(), start=1):
        if _MARKER.match(line):
            hits.append(f"{shown}:{lineno}: {line[:60].decode('utf-8', 'replace')}")
    return hits


def test_no_tracked_file_contains_a_conflict_marker() -> None:
    files = _tracked_files()
    assert len(files) > 100, "git ls-files returned suspiciously few files — is this the repo root?"
    hits = [h for p in files for h in _marker_hits(p)]
    assert not hits, (
        "git conflict marker(s) found in tracked files — either a merge was committed "
        "unresolved (the bdb93547 class: this once shipped inside a live LLM prompt) or a "
        "merge is still in progress in this working tree:\n  " + "\n  ".join(hits)
    )


def test_the_scan_is_not_vacuous(tmp_path: Path) -> None:
    """Every marker shape git can emit is caught; the setext underline is not."""
    sample = tmp_path / "s.md"
    sample.write_bytes(
        b"Title\n=======\n"  # setext underline: must NOT match
        b"<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\n"  # the default shape
        b"<<<<<<<\nx\n>>>>>>>\n"  # git merge-file's unlabeled shape
        b"<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<< HEAD\ny\n>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>> theirs\n"  # conflict-marker-size=32
        b">>>>>>>text\n"  # not a git marker: no whitespace after the run
    )
    hits = [h.split(": ", 1)[1] for h in _marker_hits(sample)]
    assert hits == [
        "<<<<<<< HEAD",
        ">>>>>>> origin/main",
        "<<<<<<<",
        ">>>>>>>",
        "<" * 32 + " HEAD",
        ">" * 32 + " theirs",
    ]
