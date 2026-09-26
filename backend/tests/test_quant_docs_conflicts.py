"""The seven conflicts #1598 reconciled in ``docs/quant/`` must not come back.

PR #1597's OpenWiki pass found seven internal contradictions in the quant slice.
Six were documents disagreeing with each other; the seventh was a rule the slice
states three times and breaks twice. Prose has no compiler, so every one of them
regenerates the moment a partially-applied edit lands — which is exactly how
conflict 4 arose (a Faber sentence stranded under a different strategy) and how
conflict 2 arose (a promotion flow left describing a convention reversed on
2026-07-09).

This module is the compiler for the parts that are mechanically checkable.

Two kinds of check live here:

* **Negative guards** — a forbidden string class must be absent from
  ``docs/quant/``. These are deliberately *literal and exemption-free*. An
  exemption ("unless the line also says 'corrected'") is a hole an edit can walk
  through, and it means a doc can still ship the forbidden number verbatim where
  a grep — or an LLM reading the tree — will find it. The consequence is a
  standing convention: **a correction annotation in these docs describes the old
  claim, it does not reproduce it.** Every annotation added by #1598 is written
  that way, and these guards are what keeps it that way.

* **A positive binding** — the level-1 threshold numbers printed in
  ``admission-criteria.md`` are asserted equal to the live values in
  ``rigor_profiles``. That was conflict 1: the doc presented the numbers as "the
  literal gate" while the gate reads a profile row. Transcription drifts;
  an assertion does not.

What this file deliberately does NOT do: judge whether a *prose* description of a
threshold is current. `test_no_library_sized_num_trials` matches the code-shaped
form only. A doc can still describe the reversed convention in words, which is
correct — the findings notes need to, to explain their own vintage.

Scope is ``docs/quant/`` only for the guards above, matching the issue. The blanket
``openwiki/`` carve-out those guards were written with — "its pages are generated
artifacts, and ``documented-conflicts.md`` is the evidence record of what the run
found, so it *must* keep quoting the strings this module forbids" — was correct
about the *evidence record* and wrong about the rest of the tree. ``mkdocs.yml``
serves every one of those pages publicly. #1794 moved the DSR bar and review found
three served pages still teaching the retired one, one of them a field guide whose
closing note asserted the exact inverse of the truth.

So ``TestServedWikiPagesDoNotTeachARetiredBar`` reinstates the scan over the served
rigor/ and findings/ pages under a rule that keeps the provenance argument intact:
**a served page either states the live bar, or carries a STALE-ON-ONE-NUMBER banner
saying it does not.** The banner is the disclosure a reader actually sees, it is
bound to ``DSR_P_BADGE_MIN`` so a future bar move re-aims every one of them, and it
lets a generated page stay as generated instead of being hand-patched into a lie
about its own vintage.

Hermetic: reads committed files off disk and imports one pure-dataclass module.
No DB, no network, no .env, no model load.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from archimedes.services import rigor_profiles


def _repo_root() -> Path:
    import archimedes  # backend/archimedes/__init__.py → parents: archimedes, backend, <repo>

    return Path(archimedes.__file__).resolve().parents[2]


def _quant_dir() -> Path:
    return _repo_root() / "docs" / "quant"


def _quant_docs() -> list[Path]:
    docs = sorted(_quant_dir().glob("*.md"))
    assert docs, f"no markdown found under {_quant_dir()} — the guard would pass vacuously"
    return docs


def _hits(pattern: re.Pattern[str]) -> list[str]:
    """Every ``path:lineno: line`` in the slice matching ``pattern``."""
    found: list[str] = []
    for doc in _quant_docs():
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                found.append(f"{doc.relative_to(_repo_root())}:{n}: {line.strip()}")
    return found


# ── Conflict 7 — the CLAUDE.md hard rule ────────────────────────────────────
# "Don't quote a curated-library strategy pass count — anywhere. [...] Say
# 'unestablished', not a number." Three strategies reported passing were later
# found to be grading equity-like series through a data-feed fallback, so the
# corrected count is not established. Two findings notes stated one anyway.
#
# Tense and synonym coverage is load-bearing, not incidental. An adversarial pass
# over the first draft of this pattern found it caught "three strategies pass the
# gate" but missed "three strategies passed all four gates" — the past tense, and
# the phrasing CLAUDE.md's rule names most directly. `clear` is covered for the
# same reason: "three strategies clear the gate" states the same forbidden count.
# The `(?<![\d.])` on the numeric alternative keeps a decimal fragment from
# reading as a count — without it, "0.930 clears both thresholds" matches on
# "930 clears" and the guard fires on correct prose about a single strategy.
# The verb suffix stays MANDATORY in the first branch: making it optional matches
# the bare noun in "one pass-count phrasing was retracted", which is exactly the
# annotation style this module's docstring requires.
_PASS_COUNT = re.compile(
    r"""(?ix)
    \b(?:one|two|three|four|five|six|seven|eight|nine|ten|(?<![\d.])\d+)
    [\s\-]+ (?:gate[\s\-]*)? (?: pass(?:es|ed|ers?|ing) | clear(?:s|ed|ing) )\b
    |
    \b(?:one|two|three|four|five|six|seven|eight|nine|ten|(?<![\d.])\d+)
    \s+ (?:\w+\s+){0,3}? (?: pass(?:es|ed)? | clear(?:s|ed)? )
    \s+ (?:all\s+)? (?:the\s+)? (?:four\s+)? (?:gates?|admission)\b
    """
)

# ── Conflict 2 — the reversed `num_trials` convention, in code form ─────────
_LIBRARY_SIZED_NUM_TRIALS = re.compile(r"num_trials\s*=\s*len\s*\(", re.IGNORECASE)

# ── Conflict 3 — a RETIRED bar stated as a gate condition ──────────────────
# This guard has been inverted once already, which is the whole lesson. It was
# written to forbid `p ≥ 0.95` because PR #901 had lowered the bar; #1794 found
# that the Generate path and every public rigor page had gone on saying 0.95 the
# whole time, and the owner's call was 0.95 everywhere. So the forbidden string is
# now `p ≥ 0.90`. To make a second inversion impossible to miss,
# `test_the_retired_bar_guard_is_not_the_live_bar` asserts this pattern does NOT
# match the live constant — move the bar in rigor_profiles and this file fails
# until the guard is re-aimed.
_RETIRED_BAR = re.compile(r"p\s*(?:≥|>=)\s*0\.90?(?![\d])")

# ── Conflict 5 — 0.612 is a DSR p-value; it is not any kind of Sharpe ───────
_SHARPE_0612 = re.compile(r"Sharpe[^\n]{0,40}0\.612", re.IGNORECASE)

# ── Conflict 4 — a heading is not a verdict ────────────────────────────────
_VERDICT_IN_HEADING = re.compile(r"✅|❌|passes the gate|fails the gate", re.IGNORECASE)


class TestForbiddenClaims:
    def test_no_curated_library_pass_count(self):
        """CLAUDE.md's hard rule, enforced over the whole slice.

        Caught before this ran: the retraction notes #1598 first wrote quoted the
        old phrasings verbatim ("the two gate-passers", "the two-passers story").
        The guard flagged them, and it was right to — a retraction that reprints
        the count still puts the count in the document. They were rewritten to
        describe the old claim instead.
        """
        hits = _hits(_PASS_COUNT)
        assert not hits, (
            "A curated-library pass count is quoted in docs/quant/. The corrected "
            "count is UNESTABLISHED (CLAUDE.md) — say so, do not give a number:\n  " + "\n  ".join(hits)
        )

    @pytest.mark.parametrize(
        "line",
        [
            # The two phrasings #1598 actually retracted, verbatim from origin/main.
            "both hold a ~0.77 median OOS rank quantile when selected — the two-passers",
            "The two gate-passers (Moreira-Muir, MOP TSMOM) and LIVE-status Faber",
            # Present tense, spelled and numeric.
            "Two strategies pass the gate today.",
            "2 strategies pass the gate today.",
            # Past tense — MISSED by the first draft of this pattern, and the
            # phrasing CLAUDE.md's rule names most directly.
            "Only three passed the gate on the 2026-06-11 pull.",
            "Three strategies passed all four gates.",
            "Two of the 22 strategies passed admission.",
            # The synonym that states the same count without the word "pass".
            "three strategies clear the gate",
            "Two strategies cleared the gate.",
        ],
    )
    def test_pass_count_guard_rejects_a_forbidden_phrasing(self, line: str):
        """The guard is shown to reject bad input, not assumed to (CLAUDE.md rule 4).

        A negative guard that never fires is indistinguishable from a guard with a
        hole in it, and this one had a real hole: the first draft matched
        "three strategies pass the gate" but not "three strategies passed all four
        gates". These cases are the adversarial probe, kept executable so the next
        edit to the pattern cannot quietly narrow it again.
        """
        assert _PASS_COUNT.search(line), f"_PASS_COUNT does not match a forbidden pass count: {line!r}"

    @pytest.mark.parametrize(
        "line",
        [
            # The retraction style this module's docstring requires: describe the
            # old claim, do not reproduce it. Must NOT trip the guard.
            "One pass-count phrasing was retracted in place on 2026-08-31.",
            "Two pass-count phrasings were retracted; the corrected count is unestablished.",
            "The corrected pass count is unestablished.",
            # A decimal that must not read as a count (conflict 5's own prose).
            "an always-on floor of `> 0` and a cliff of `OOS/IS ≥ 0.5` — and 0.930 clears both",
            "the `num_trials = 22` row at p = 0.941 clears the lower bar",
        ],
    )
    def test_pass_count_guard_admits_correct_prose(self, line: str):
        """The other half of rule 4: a guard that rejects everything is also useless.

        Each line is real prose from this PR's own annotations. If the pattern is
        ever broadened, these are what stop it from swallowing the correction
        style the docstring mandates.
        """
        assert not _PASS_COUNT.search(line), f"_PASS_COUNT false-positives on correct prose: {line!r}"

    def test_no_library_sized_num_trials(self):
        """`num_trials = len(...)` was reversed on 2026-07-09 and is now ADR-forbidden.

        The ADR (`num-trials-self-containment.md`, Accepted) is explicit: a
        library-sized trial count makes a strategy's p-value a function of
        unrelated strategies, so a passport stops being reproducible from its own
        artifacts. The curated path hard-codes 1; the generated path uses the
        debate's own pool size.
        """
        hits = _hits(_LIBRARY_SIZED_NUM_TRIALS)
        assert not hits, (
            "docs/quant/ shows a library-sized num_trials. Curated = 1, generated = "
            "the strategy's own debate pool size — see docs/adr/"
            "num-trials-self-containment.md:\n  " + "\n  ".join(hits)
        )

    def test_no_retired_gate_bar(self):
        """The DSR bar is `DSR_P_BADGE_MIN`; PR #901's lower bar is retired (#1794).

        Historical *narration* of a retired bar is fine and necessary — the findings
        notes need it to explain their own vintage — which is why this matches the
        threshold-expression form and not the digits.
        """
        hits = _hits(_RETIRED_BAR)
        assert not hits, (
            "A retired `p ≥ 0.90` gate condition survives in docs/quant/. The bar is "
            f"{rigor_profiles.DSR_P_BADGE_MIN} (#1794); narrate the old bar, do not "
            "state it as a condition:\n  " + "\n  ".join(hits)
        )

    def test_the_retired_bar_guard_is_not_the_live_bar(self):
        """The guard must forbid a RETIRED bar, never the live one.

        #1794's root cause in miniature: this pattern was aimed at 0.95 while the
        live bar was 0.90, and when the owner moved the bar back the guard would
        have started rejecting correct prose. Bind it to the constant instead.
        """
        live = f"p ≥ {rigor_profiles.DSR_P_BADGE_MIN:.2f}"
        assert not _RETIRED_BAR.search(live), (
            f"_RETIRED_BAR matches the LIVE bar ({live}). The bar moved in "
            "rigor_profiles and this guard was not re-aimed — it now forbids docs "
            "from stating the truth."
        )

    @pytest.mark.parametrize(
        "line",
        [
            "the gate admits a strategy at `p >= 0.90`",
            "Admission is `p ≥ 0.90` at level 1.",
            "DSR `p ≥ 0.9`, PBO `< 0.5`",
        ],
    )
    def test_retired_bar_guard_rejects_the_forbidden_form(self, line: str):
        """Shown red on the input it must reject, not assumed to be (CLAUDE.md rule 4)."""
        assert _RETIRED_BAR.search(line), f"_RETIRED_BAR misses a retired gate condition: {line!r}"

    @pytest.mark.parametrize(
        "line",
        [
            # Narration of the retired bar — the correction style this module requires.
            "PR #901 briefly lowered the bar; #1794 retired that path.",
            "the 0.90 bar PR #901 briefly used",
            # The live bar, stated as a condition. Must NOT trip the guard.
            "Admission requires `dsr_p_value ≥ 0.95`.",
            "DSR `p ≥ 0.95`, PBO `< 0.5`",
            # A p-value that merely starts with 0.90-ish digits is not a bar.
            "the `num_trials = 22` row at p = 0.941",
            "Faber's `dsr_p_value` is p ≥ 0.9012 on that pull",
        ],
    )
    def test_retired_bar_guard_admits_correct_prose(self, line: str):
        assert not _RETIRED_BAR.search(line), f"_RETIRED_BAR false-positives on correct prose: {line!r}"

    def test_0612_is_never_called_a_sharpe(self):
        """0.612 is Faber's DSR p-value. Its OOS Sharpe on the same pull is 0.930.

        Two passages explained a failure as an OOS Sharpe of 0.612 "under the DSR
        gate" — comparing a Sharpe ratio to a probability. The OOS Sharpe's own
        thresholds are an always-on floor of > 0 and a cliff of OOS/IS >= 0.5, and
        0.930 clears both; the failure is on criterion 1.
        """
        hits = _hits(_SHARPE_0612)
        assert not hits, (
            "docs/quant/ attributes 0.612 to a Sharpe ratio. It is a DSR p-value "
            "(docs/analysis/faber-dsr-finding.md); the DSR bar is a probability, not "
            "an OOS-Sharpe bar:\n  " + "\n  ".join(hits)
        )

    def test_strategy_library_headings_carry_no_verdict(self):
        """`strategy-library.md` states twice that pass/fail is not recorded there.

        Three headings broke that rule and all three disagreed with the status line
        directly beneath them — including a ✅ *passes the gate* sitting on top of
        "CANDIDATE — fails admission". A reader skimming headings got the opposite
        answer from a reader reading status lines.
        """
        doc = _quant_dir() / "strategy-library.md"
        offenders = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), start=1)
            if line.startswith("#") and _VERDICT_IN_HEADING.search(line)
        ]
        assert not offenders, (
            "A heading in strategy-library.md carries a pass/fail verdict. The live "
            "rigor gate is the only authority on pass/fail:\n  " + "\n  ".join(offenders)
        )


class TestThresholdsMatchLiveCode:
    """Conflict 1: the doc printed the ladder's level-1 row as "the literal gate".

    `passes_all` reads a `rigor_profiles` row and never compares against a
    hard-coded number. The numbers in the doc are still right — they are level 1,
    and level 1 *is* the Tier-1 badge bar — but nothing bound them to the source.
    These assertions are that binding: change `_PROFILES` and this fails until the
    doc is updated with it.
    """

    @pytest.fixture
    def admission_text(self) -> str:
        return (_quant_dir() / "admission-criteria.md").read_text(encoding="utf-8")

    def test_level_1_is_the_strictest_and_the_badge_bar(self):
        """The premise of the whole page: level 1 is what the badge is graded at."""
        assert rigor_profiles.STRICTEST_LEVEL == 1
        assert rigor_profiles.DEFAULT_LEVEL == rigor_profiles.STRICTEST_LEVEL
        assert rigor_profiles.get_profile(1).label == "Conservative"

    def test_adjustable_ladder_endpoints_are_transcribed_correctly(self, admission_text):
        strictest = rigor_profiles.get_profile(rigor_profiles.STRICTEST_LEVEL)
        loosest = rigor_profiles.get_profile(rigor_profiles.LOOSEST_LEVEL)
        for name, lo, hi in (
            ("dsr_p_min", strictest.dsr_p_min, loosest.dsr_p_min),
            ("pbo_max", strictest.pbo_max, loosest.pbo_max),
            ("oos_is_ratio_min", strictest.oos_is_ratio_min, loosest.oos_is_ratio_min),
        ):
            expected = f"`{lo:.2f} → {hi:.2f}`"
            assert expected in admission_text, (
                f"admission-criteria.md's ladder row for {name} does not show "
                f"{expected}, which is what rigor_profiles._PROFILES holds today."
            )

    def test_always_on_floors_are_transcribed_correctly(self, admission_text):
        for expected in (
            f"`OOS_ABS_FLOOR = {rigor_profiles.OOS_ABS_FLOOR:.1f}`",
            f"`DSR_P_FLOOR = {rigor_profiles.DSR_P_FLOOR:.2f}`",
            f"`CPCV_MIN_POSITIVE_FRACTION = {rigor_profiles.CPCV_MIN_POSITIVE_FRACTION}`",
        ):
            assert expected in admission_text, (
                f"admission-criteria.md does not carry {expected}. The always-on "
                "floors are what make 'you can never fully bypass the rigor gate' "
                "true; a stale copy of them is a false claim."
            )

    def test_the_level_1_dsr_bar_in_the_control_table_is_live(self, admission_text):
        """The single most-quoted number on the page."""
        bar = rigor_profiles.get_profile(rigor_profiles.STRICTEST_LEVEL).dsr_p_min
        assert f"`dsr_p_value ≥ {bar:.2f}`" in admission_text


# ── The served OpenWiki pages (#1794 review) ────────────────────────────────
#
# These live outside docs_dir but mkdocs.yml mounts them, and
# `test_docs_site.py::test_every_openwiki_page_is_in_the_nav` guarantees every one
# is reachable. A visitor cannot tell a "generated artifact" from a hand-written
# page; both are just the site.
#
# Every markdown page under openwiki/, not a hand-named subset: a list of two
# directories is the same shape of mistake as `TestOneLiteral`'s old nine-module
# allowlist, and openwiki/ grows a section whenever the generator runs.

# The banner that makes a stale page honest. Matched on the marker AND on the live
# bar, so a banner left behind after the next bar move fails here instead of
# quietly certifying the wrong number.
_STALE_BANNER = "STALE ON ONE NUMBER"

# A DSR bar written down on a wiki page: `0.9`/`0.90`/`0.95`, or `90%`/`95%`.
# Deliberately broader than `_RETIRED_BAR` above, which only matches the `p ≥ x`
# gate-condition form — review's two pages stated the retired bar as "is it ≥
# 0.90?" and "the gate has since moved to 0.90", and `_RETIRED_BAR` reads neither.
#
# It captures the VALUE rather than hardcoding the retired one, and `_wiki_bar_hits`
# drops anything equal to `DSR_P_BADGE_MIN`. Written the other way round — a pattern
# aimed at the digits `0.90` — this guard would need re-aiming by hand every time
# the bar moves, which is the failure mode `test_the_retired_bar_guard_is_not_the_live_bar`
# exists to catch one class up. Caught by this module's own adversarial cases: the
# first draft matched `0.9\d?` and flagged the live bar, i.e. it would have demanded
# a "this page is stale" banner on a page telling the truth.
_WIKI_BAR_LITERAL = re.compile(r"(?<![\d.])(?P<dec>0\.9\d?)(?![\d])|(?<![\d.])(?P<pct>9\d)\s*%")
# ...but only where it is a claim about the gate. Same window trick as
# test_single_dsr_bar: the token must appear within two lines.
_WIKI_BAR_TOKEN = re.compile(r"dsr|deflat|\bgate\b|\bbar\b|threshold|confidence|admission", re.IGNORECASE)
_WIKI_WINDOW = 2


def _served_wiki_pages() -> list[Path]:
    root = _repo_root() / "openwiki"
    pages = sorted(root.rglob("*.md"))
    assert pages, f"no served wiki pages found under {root} — the guard would pass vacuously"
    return pages


def _non_live_bars(line: str) -> list[str]:
    """Bar-shaped numbers on ``line`` that are NOT the live badge bar."""
    live = rigor_profiles.DSR_P_BADGE_MIN
    found: list[str] = []
    for m in _WIKI_BAR_LITERAL.finditer(line):
        raw = m.group("dec") or m.group("pct")
        value = float(raw) if m.group("dec") else float(raw) / 100
        if value != live:
            found.append(raw)
    return found


def _wiki_bar_hits(page: Path) -> list[str]:
    lines = page.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if not _non_live_bars(line):
            continue
        window = lines[max(0, i - _WIKI_WINDOW) : i + _WIKI_WINDOW + 1]
        if not any(_WIKI_BAR_TOKEN.search(w) for w in window):
            continue
        out.append(f"{page.relative_to(_repo_root())}:{i + 1}: {line.strip()}")
    return out


class TestServedWikiPagesDoNotTeachARetiredBar:
    """Every served wiki page states the live bar, or says it doesn't."""

    def test_a_page_quoting_a_retired_bar_carries_the_stale_banner(self):
        offenders: list[str] = []
        for page in _served_wiki_pages():
            text = page.read_text(encoding="utf-8")
            if _STALE_BANNER in text:
                continue
            offenders.extend(_wiki_bar_hits(page))
        assert not offenders, (
            "A publicly served OpenWiki page states a DSR bar that is not "
            f"{rigor_profiles.DSR_P_BADGE_MIN} and carries no STALE-ON-ONE-NUMBER "
            "banner. That is #1794's headline symptom — a public rigor page quoting "
            "a threshold the gate does not use. Either correct the page or add the "
            "banner:\n  " + "\n  ".join(offenders)
        )

    def test_every_stale_banner_names_the_live_bar(self):
        """A banner is a claim too. Bind it, or the next bar move strands them all."""
        live = f"**{rigor_profiles.DSR_P_BADGE_MIN:.2f}**"
        wrong = [
            str(page.relative_to(_repo_root()))
            for page in _served_wiki_pages()
            if _STALE_BANNER in page.read_text(encoding="utf-8") and live not in page.read_text(encoding="utf-8")
        ]
        assert not wrong, (
            f"A STALE-ON-ONE-NUMBER banner does not name the live bar ({live}). The "
            "banner exists to tell a reader what the number really is; one that omits "
            "it — or names a bar that has since moved again — is worse than no banner "
            f"at all: {wrong}"
        )

    def test_the_banner_exemption_is_not_swallowing_the_whole_tree(self):
        """Anti-vacuity: the guard must still be scanning unbannered pages.

        If every served page ends up bannered, this suite would pass while teaching
        nothing. Assert that most pages are NOT exempt, so the scan has real work.
        """
        pages = _served_wiki_pages()
        bannered = [p for p in pages if _STALE_BANNER in p.read_text(encoding="utf-8")]
        assert len(bannered) < len(pages), (
            "every served wiki page is bannered stale — the scan is now vacuous, and "
            "the wiki needs regenerating rather than more banners"
        )

    @pytest.mark.parametrize(
        "line",
        [
            "2. **DSR p-value** — is it ≥ 0.90? If not, the excess Sharpe is unproven.",
            "the gate has since moved to 0.90 (see documented-conflicts)",
            "**DSR at 0.90** is one-sided 90% confidence that the excess Sharpe is positive",
            "| 1 | Deflated Sharpe Ratio | `dsr_p_value ≥ 0.90`, and not `None` |",
        ],
    )
    def test_the_wiki_scan_rejects_a_retired_bar_claim(self, line: str):
        """Shown red on the input it must reject, not assumed to be (CLAUDE.md rule 4)."""
        assert _non_live_bars(line), f"the wiki scan misses a retired bar: {line!r}"
        assert _WIKI_BAR_TOKEN.search(line), f"_WIKI_BAR_TOKEN misses the gate context: {line!r}"

    def test_the_wiki_scan_never_flags_the_live_bar(self):
        """The `_RETIRED_BAR` inversion lesson, applied one guard up.

        A scan aimed at digits instead of at "not the live value" starts demanding a
        stale banner on pages that tell the truth the moment the bar moves.
        """
        live = rigor_profiles.DSR_P_BADGE_MIN
        for stated in (f"the DSR bar is {live:.2f}", f"{live:.0%} one-sided confidence"):
            assert not _non_live_bars(stated), (
                f"the wiki scan flags the LIVE bar ({stated!r}). It must reject bars "
                "that are not DSR_P_BADGE_MIN, never the one that is."
            )

    @pytest.mark.parametrize(
        "line",
        [
            # The live bar, stated as the gate. Must NOT trip the scan.
            "the badge bar is **0.95**, defined once as `DSR_P_BADGE_MIN`",
            "| 1 | Deflated Sharpe Ratio | `dsr_p_value ≥ 0.95`, and not `None` |",
            # A measured p-value in a worked table is not a threshold claim.
            "DSR p-values of 0.999 → 0.963 for `num_trials` 1–13",
            "the `num_trials = 22` row at p = 0.941 clears the bar",
            # A number with nothing to do with the gate.
            "the strategy drew down 90% of its peak equity in 2008",
        ],
    )
    def test_the_wiki_scan_admits_correct_prose(self, line: str):
        flagged = bool(_non_live_bars(line)) and bool(_WIKI_BAR_TOKEN.search(line))
        assert not flagged, f"the wiki scan false-positives on correct prose: {line!r}"
