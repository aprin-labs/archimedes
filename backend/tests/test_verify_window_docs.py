"""No agent-facing doc may describe a sub-window series as gradeable (#1803).

The 250-bar minimum evaluation window changed what a short series *is*. Before
it, a 60-bar series reached the gate and came back with the walk-forward leg
unevaluated — ``legs_evaluated < legs_runnable``, an INCOMPLETE evaluation,
CLI exit ``4``. After it, a 60-bar series never reaches the gate: it is refused,
``422 {"detail": {"reason": "window_too_short"}}``, CLI exit ``2``.

Five documents describe that route to an agent, plus two CLI modules that
document exit code 4 in prose a user reads, and the window landed in some of
their paragraphs and not others. ``docs/agent-quickstart.md`` shipped both rules
forty lines apart — "under 250 bars there is no verdict" at :650 and "below about
70 bars the OOS leg cannot run at all … the CLI exits ``1`` there, not ``4``" at
:694 — so an agent reading the file top to bottom got a contradiction, and the
second half of it was false. Prose has no compiler and a partially-applied edit
is the normal way this happens, which is what this module is for.

The check is a RELATION, not a word list: any claim a document makes about a bar
count *below the enforced window* must sit next to refusal vocabulary. That
holds for the sentences the docs legitimately need ("under 250 bars there is no
verdict: the answer is a refusal") and fails for the ones that describe grading
a series the route will not accept. The window itself is read from
``rigor_verify_routes``, so moving the floor moves the guard with it rather than
silently retiring it.

Deliberately NOT a check that shortness is never mentioned: the docs must keep
explaining *why* the floor is 250 — the walk-forward split needs ~70 bars, the
DSR wants a year — and a guard that forbade the number would delete the
explanation.

Hermetic: reads committed files off disk and one module-level constant. No DB,
no network, no model load.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every surface that tells an agent how to build a body for POST /api/rigor/verify.
VERIFY_DOCS = (
    "docs/agent-quickstart.md",
    "docs/agent-api.md",
    "docs/api/strategies-and-rigor.md",
    "ui/public/llms.txt",
    "skills/archimedes-cli/SKILL.md",
)

# The CLI carries the same claim in prose a user reads — `exits.INCOMPLETE`'s
# docstring is the documentation of exit code 4, and it said the same false thing
# ("typically too few bars for the walk-forward OOS split"). Scanned by the two
# negative checks, but not by the "describes the route" reader below: neither file
# names the URL, and requiring it would only force a fake marker string.
VERIFY_SURFACES = (
    *VERIFY_DOCS,
    "cli/src/archimedes_cli/exits.py",
    "cli/src/archimedes_cli/cli.py",
)

# "Below about 70 bars", "under 60 daily bars", "fewer than ~100 bars".
_SUB_WINDOW_CLAIM = re.compile(
    r"(?i)\b(?:below|under|beneath|fewer\s+than|less\s+than)\s+"
    r"(?:about\s+|roughly\s+|around\s+)?~?\s*(\d{1,4})\s*(?:daily\s+)?bars?\b"
)

# What a correct sentence about a sub-window series has to be doing.
_REFUSAL_VOCABULARY = ("refus", "reject", "window_too_short", "422", "exit `2`", "exit 2")

# The same claim without a number: "too few bars", "a series too short to grade".
_SHORT_CAUSE = re.compile(r"(?i)\btoo[\s-]+(?:few|short)\b|\bnot\s+enough\s+bars\b")

# Tighter than `_REFUSAL_VOCABULARY` on purpose. These sentences sit inside
# paragraphs about verdicts, and "rejected" is ordinary vocabulary there — the
# prose that made exit 4 mean "too few bars" also contains the words "strategy
# rejected". Only naming the refusal itself distinguishes the two.
_REFUSAL_NAMED = ("refus", "window_too_short")

# The unit a claim is judged in is its PARAGRAPH, not a character window. A fixed
# window either splits a markdown paragraph (a correction three lines below the
# claim reads as absent) or spills into the neighbouring docstring (an unrelated
# "refused" reads as present). Blank lines separate paragraphs in both markdown
# and a Python docstring, which is the whole file set.
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")

# The specific stale framing: shortness offered as the thing that "is not an
# excuse", i.e. as a state the gate can be in. Literal and exemption-free, in
# the style of test_quant_docs_conflicts: an annotation about this old claim
# should describe it, not reproduce it.
_SHORTNESS_EXCUSE = re.compile(r"(?i)\bshort(?:er|ness)?\b[^.]{0,80}\bexcuse\b")


def _window() -> int:
    from archimedes.api import rigor_verify_routes

    return rigor_verify_routes._MIN_RETURN_ROWS


def _docs(paths: tuple[str, ...] = VERIFY_SURFACES) -> dict[str, str]:
    texts = {}
    for rel in paths:
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} is listed here but not on disk — fix the list or the path"
        texts[rel] = path.read_text(encoding="utf-8")
    return texts


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """Every paragraph with the line number it starts on."""
    out, offset = [], 0
    for para in _PARAGRAPH_SPLIT.split(text):
        start = text.index(para, offset) if para else offset
        out.append((text.count("\n", 0, start) + 1, para))
        offset = start + len(para)
    return out


def _ungrounded_sub_window_claims(text: str, window: int) -> list[str]:
    """Claims about a count below `window` whose paragraph does NOT call it a refusal."""
    offenders = []
    for line, para in _paragraphs(text):
        lowered = para.lower()
        for match in _SUB_WINDOW_CLAIM.finditer(para):
            if int(match.group(1)) >= window:
                continue  # "under 250 bars" is a statement OF the floor, not below it
            if not any(word in lowered for word in _REFUSAL_VOCABULARY):
                offenders.append(f"paragraph at line {line}: {match.group(0)!r}")
    return offenders


def _ungrounded_short_causes(text: str) -> list[str]:
    """ "Too few bars" offered as a cause, without naming the refusal it actually is."""
    offenders = []
    for line, para in _paragraphs(text):
        lowered = para.lower()
        if any(word in lowered for word in _REFUSAL_NAMED):
            continue
        for match in _SHORT_CAUSE.finditer(para):
            offenders.append(f"paragraph at line {line}: {match.group(0)!r}")
    return offenders


def test_the_doc_set_is_real_and_describes_the_route() -> None:
    """Guard on the guard: an empty or wrong file list passes everything below."""
    texts = _docs(VERIFY_DOCS)
    assert len(texts) >= 5, f"only {len(texts)} documents scanned"
    for rel, text in texts.items():
        assert "rigor/verify" in text, f"{rel} no longer describes POST /api/rigor/verify — fix VERIFY_DOCS"
    # The CLI surfaces are scanned too; they just cannot be identified by the URL.
    assert len(_docs()) == len(VERIFY_DOCS) + 2, "VERIFY_SURFACES lost a file — fix the list or this count"


@pytest.mark.parametrize("rel", VERIFY_SURFACES)
def test_no_doc_describes_a_sub_window_series_as_gradeable(rel: str) -> None:
    offenders = _ungrounded_sub_window_claims(_docs()[rel], _window())
    assert not offenders, (
        f"{rel} makes a claim about a bar count below the {_window()}-bar evaluation window "
        "without saying the route refuses it:\n  "
        + "\n  ".join(offenders)
        + f"\nA series under {_window()} bars never reaches the gate — it answers 422 "
        "window_too_short (CLI exit 2), so it cannot produce legs_evaluated < legs_runnable, "
        "an INCOMPLETE verdict, or exit 1/4. Say refused, or delete the sentence."
    )


@pytest.mark.parametrize("rel", VERIFY_SURFACES)
def test_no_surface_names_shortness_as_a_cause_without_naming_the_refusal(rel: str) -> None:
    """The numberless form of the same claim.

    ``exits.INCOMPLETE`` documented exit 4 as "typically too few bars for the
    walk-forward OOS split (~70)" — no comparison operator for the numeric reader
    above to catch, and it is the line a CI author reads to decide what a 4 means.
    """
    offenders = _ungrounded_short_causes(_docs()[rel])
    assert not offenders, (
        f"{rel} names shortness as a cause without saying it is a refusal:\n  "
        + "\n  ".join(offenders)
        + f"\nUnder {_window()} bars the route answers 422 window_too_short and the CLI exits 2. "
        "Say so in the same breath, or describe the cause that is still real (zero variance)."
    )


@pytest.mark.parametrize("rel", VERIFY_SURFACES)
def test_no_doc_still_frames_shortness_as_a_state_the_gate_can_be_in(rel: str) -> None:
    """The sentence that survived round 3 in agent-quickstart.md, pinned by shape.

    "a short series is not an excuse that erases a FAIL" reads as advice about a
    verdict the route can return on a short series. It cannot return one. The
    same point about a leg that genuinely could not run — a zero-variance series
    with no Sharpe to compute — is true and is what the docs say now.
    """
    hits = [m.group(0) for m in _SHORTNESS_EXCUSE.finditer(_docs()[rel])]
    assert not hits, (
        f"{rel} still offers shortness as something the gate weighs against a FAIL: {hits}. "
        "A short series is refused before the gate runs; the leg that can fail to run on the "
        "numbers is a degenerate one (zero variance)."
    )


def test_the_readers_would_actually_catch_the_regression() -> None:
    """The control. Both checks above assert an ABSENCE, so both pass on an empty
    string; without this, deleting the docs would look like fixing them.

    The two strings below are verbatim from ``docs/agent-quickstart.md`` before
    this guard landed — the exact prose the module exists to keep out.
    """
    stale = (
        "Below about 70 bars the OOS leg cannot run at all, which shows up as "
        "`legs_evaluated < legs_runnable`. When no leg actually failed, that is an "
        "incomplete evaluation."
    )
    assert _ungrounded_sub_window_claims(stale, _window()), (
        "the sub-window reader no longer flags the paragraph it was written for — "
        "the regex or the context window is broken, and the checks above are vacuous"
    )

    excuse = "the failure is a real verdict and stands: a short series is not an excuse that erases a FAIL"
    assert _SHORTNESS_EXCUSE.search(excuse), "the shortness/excuse reader no longer flags its own example"

    cause = (
        "not every runnable leg of the gate could be evaluated — typically too few bars for "
        "the walk-forward OOS split (~70) while DSR only needs 4. A new code rather than "
        "GATE_FAILED: collapsing it into 1 would report it as strategy rejected."
    )
    assert _ungrounded_short_causes(cause), (
        "the short-cause reader no longer flags exits.INCOMPLETE's old docstring — note it "
        "contains the word 'rejected', which is why _REFUSAL_NAMED is narrower than "
        "_REFUSAL_VOCABULARY"
    )
    assert not _ungrounded_short_causes("a series too short to grade is refused outright"), (
        "the reader flags a correctly-framed sentence"
    )

    grounded = (
        "Below 70 bars the walk-forward split cannot run, which is why the floor is 250: "
        'a shorter series is refused, 422 {"detail": {"reason": "window_too_short"}}.'
    )
    assert not _ungrounded_sub_window_claims(grounded, _window()), (
        "the reader flags a correctly-framed refusal sentence — it would force the docs to "
        "stop explaining the floor, which is not the goal"
    )
