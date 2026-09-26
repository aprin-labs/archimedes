"""Every Surprise Me brief must clear the backend's own cheap validator (#1642).

The Generate page's "Surprise me" button fills the brief box from
``ui/src/data/surpriseBriefs.js``. That text is submitted verbatim, so an entry
the backend rejects is a button that hands the user an error — and it would be
rejected *before payment*, by
:func:`archimedes.agents.generation_pipeline.cheap_brief_reject`, the
deterministic no-LLM prelude the pre-payment route gate shares with the real
validator.

What passing here does and does not mean. ``cheap_brief_reject`` is
deliberately permissive: it rejects only empty, too-short, letter-free, or
keyboard-mash text, and explicitly does *not* reject unfamiliar vocabulary or
off-topic-but-grammatical prose (read its docstring). So this test proves the
bank contains nothing **broken**. It proves nothing about whether an entry is a
*good* brief, whether it will generate a strategy, or whether that strategy
would clear the rigor gate. Do not cite it for any of those.

Hermetic by construction, following ``test_breadcrumbs.py``: reads the
committed JS source as text and regex-extracts the strings. No node runtime, no
DB / Redis / RPC / ``.env``, no LLM, no network — ``cheap_brief_reject`` is a
pure function over a Pydantic model.
"""

from __future__ import annotations

import re
from pathlib import Path

from archimedes.agents.generation_pipeline import cheap_brief_reject
from archimedes.api.generate_schemas import GenerateBrief

REPO_ROOT = Path(__file__).resolve().parents[2]
BANK = REPO_ROOT / "ui" / "src" / "data" / "surpriseBriefs.js"

# The issue's floor. Asserted so a regex that silently stops matching (a
# formatter rewrapping the file, a switch to double quotes) fails loudly
# instead of passing vacuously over zero extracted briefs.
MIN_ENTRIES = 100

# `brief:` then a single-quoted string, possibly wrapped onto the next line by
# the formatter. Escaped quotes are tolerated; the bank avoids them on purpose.
_BRIEF_RE = re.compile(r"brief:\s*'((?:[^'\\]|\\.)*)'")
_ID_RE = re.compile(r"^\s*id: '([^']+)'", re.MULTILINE)


def _extract_briefs(text: str) -> list[str]:
    return [m.group(1) for m in _BRIEF_RE.finditer(text)]


def _extract_ids(text: str) -> list[str]:
    return [m.group(1) for m in _ID_RE.finditer(text)]


def test_bank_file_exists_and_is_big_enough() -> None:
    assert BANK.is_file(), f"brief bank not found at {BANK}"
    text = BANK.read_text(encoding="utf-8")
    briefs = _extract_briefs(text)
    ids = _extract_ids(text)
    assert len(briefs) >= MIN_ENTRIES, f"extracted only {len(briefs)} briefs, expected >= {MIN_ENTRIES}"
    # One brief per entry: a mismatch means the regex is reading something
    # other than the bank's entries (or an entry is missing its brief).
    assert len(briefs) == len(ids), f"{len(ids)} ids but {len(briefs)} briefs — the two are out of step"


def test_no_surprise_brief_is_cheap_rejected() -> None:
    """Zero entries may be refused by the pre-payment validator."""
    text = BANK.read_text(encoding="utf-8")
    briefs = _extract_briefs(text)
    assert briefs, "no briefs extracted — the regex or the file shape changed"

    rejected: list[tuple[str, str]] = []
    for brief in briefs:
        verdict = cheap_brief_reject(GenerateBrief(intent=brief, risk_appetite="moderate", asset_classes=[]))
        if verdict is not None:
            rejected.append((brief, verdict["reason"]))

    assert not rejected, "briefs rejected before payment:\n" + "\n".join(
        f"  {reason}: {brief!r}" for brief, reason in rejected
    )


def test_the_check_actually_rejects_something() -> None:
    """The guard above is only meaningful if it can fail.

    Without this, a `cheap_brief_reject` that grew an unconditional
    ``return None`` — or an import that silently resolved to a stub — would
    leave the bank test green while checking nothing at all.
    """
    # "" is absent on purpose since #1801: ``intent`` carries ``min_length=1``,
    # so an empty brief is now refused by the request schema and a
    # ``GenerateBrief(intent="")`` cannot be constructed to feed this at all.
    for junk in ("  ", "qq", "asdfgh lkjhgf", "123 456 789"):
        verdict = cheap_brief_reject(GenerateBrief(intent=junk, risk_appetite="moderate", asset_classes=[]))
        assert verdict is not None, f"cheap_brief_reject accepted junk: {junk!r}"
        assert "reason" in verdict and "hint" in verdict
