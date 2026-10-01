"""One DSR bar, one definition (#1794).

Before this, the bar was written down in three places with two values. The badge
path — every surface that decides "Archimedes Verified" — read
``rigor_profiles.get_profile(STRICTEST_LEVEL).dsr_p_min`` (0.90 since PR #901),
while ``generation_pipeline`` carried two hardcoded ``0.95`` comparisons
(``_rigor_verdict_for`` and ``_patch_dsr_with_pool_correlation``) and every public
rigor page said 0.95 too.

Traced rather than assumed: ``_rigor_verdict_for`` has no production caller today,
and ``_patch_dsr_with_pool_correlation`` — which ``run_generation`` does call —
skips every ``has_real_rigor=True`` candidate, which is all of them on the Phase-3
debate-only pipeline. So the shipped inconsistency was gate-vs-docs, not
gate-vs-generator; the literals were the split waiting to happen the moment a
buy-and-hold path returned. A strategy in [0.90, 0.95) still earned the badge
while the page describing the badge said it should not.

The owner's call (2026-09-03) was 0.95 everywhere and the 0.90 path retired. That
is only durable if the number cannot be written down twice, so this module holds
two guards:

* ``TestOneLiteral`` — a source scan over the modules that compute or serve the
  gate. Outside ``DSR_P_BADGE_MIN``'s own definition line, no ``0.9x`` literal may
  sit within two lines of DSR code — in a comparison, a default, OR a comment. A
  stale comment quoting a retired bar is how #1794's docs drift started, so prose
  is in scope, not exempt from it.

* ``TestGateBehaviourAtTheBar`` — the bar is actually 0.95 at every call site that
  used to hold its own literal: a candidate at DSR p = 0.92 fails and one at 0.96
  passes, on the badge gate, on ``_rigor_verdict_for``, and on the pool-correlation
  re-patch that re-derives ``passing`` after re-deflating.

Hermetic: reads committed source off disk and imports pure Python. No DB, no net.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from archimedes.services.rigor_evaluator import RigorGateResult
from archimedes.services.rigor_profiles import (
    DSR_P_BADGE_MIN,
    DSR_P_FLOOR,
    STRICTEST_LEVEL,
    get_profile,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where the scan looks. Every Python file under these roots that so much as
# mentions DSR is scanned — the list below is a FLOOR, not the scope.
#
# It was a hand-maintained list of nine backend modules until review found the
# hole that predicts: `analytics-engine/scripts/consolidation_candidate1_verify.py`
# carried "but the badge threshold is 0.90" in a live comment, unguarded, because
# it was not on the list. #1794 asked for a scan of the repo, not of a list
# somebody remembers to extend, so the module set is DERIVED and the old list is
# kept only as an anti-shrink check.
# backend and not backend/archimedes: the singleton check below walks the
# same roots WITH tests included, and a second `DSR_P_BADGE_MIN = 0.90` parked in
# backend/tests would otherwise be invisible to it.
SCAN_ROOTS = ("backend", "analytics-engine", "scripts")
# Directory names that never hold gate code.
_SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".venv", "venv", "build", "dist", ".git"})
# Test files are excluded from the LITERAL scan and only from that: pinning a
# number is a test's job (this module asserts `DSR_P_BADGE_MIN == 0.95` itself).
# They are NOT excluded from the singleton scan below — a second definition of
# the constant is a second bar no matter which directory it hides in.
_TEST_DIRS = frozenset({"tests", "test"})

# The floor: modules that compute, patch, or document the admission decision and
# must never drop out of the derived set. A rename that quietly narrows the scan
# fails `test_the_guarded_floor_is_still_covered` rather than passing silently.
GUARDED_MODULES = (
    "backend/archimedes/services/rigor_profiles.py",
    "backend/archimedes/services/rigor_evaluator.py",
    "backend/archimedes/services/_rigor_helpers.py",
    "backend/archimedes/services/live_rigor_gate.py",
    "backend/archimedes/services/fusion_evaluator.py",
    "backend/archimedes/agents/generation_pipeline.py",
    "backend/archimedes/api/selection_bias_routes.py",
    "backend/archimedes/api/strategies_routes.py",
    "backend/archimedes/models/backtest.py",
)

# 0.9, 0.90, 0.95, 0.9xx — the shape a DSR bar takes. Not preceded by a digit or a
# dot, so "28.9%" and "1.0.95" are not matches.
_BAR_LITERAL = re.compile(r"(?<![\d.])0\.9\d*")
# "DSR", "deflated", "deflation", "deflates" — the tokens that make a nearby
# literal a threshold claim rather than an unrelated number.
_DSR_TOKEN = re.compile(r"dsr|deflat", re.IGNORECASE)
# The ONE line allowed to write the number down. Anchored at column 0, not
# searched anywhere in the line: an unanchored test would let
# `x = 0.90  # DSR_P_BADGE_MIN = the real one` name its way out of the scan.
_DEFINITION = "DSR_P_BADGE_MIN = "
# A *definition* assigns a numeric literal. `DSR_P_BADGE_MIN = _load_badge_bar()`
# in analytics-engine's fixture regenerator is a RE-EXPORT — it reads the value
# out of rigor_profiles.py at import time and writes no number down — so it is not
# a second bar and must not be counted as one. Anything whose right-hand side
# starts with a digit or a dot is a literal and IS counted; a value laundered
# through `float("0.90")` still trips the literal scan above, which does not care
# what syntax the number is wearing.
_LITERAL_DEFINITION = re.compile(r"^DSR_P_BADGE_MIN\s*=\s*[\d.]")
# Narrow, documented exemption: the pool-correlation prose quotes ρ ranges like
# "ρ ∈ [0.3, 0.9]" near DSR code. It applies ONLY to a line that mentions ρ or
# correlation and does NOT itself name DSR — so `dsr_p >= 0.95  # correlation`
# is still caught.
_RHO_PROSE = re.compile(r"ρ|correlation", re.IGNORECASE)
_WINDOW = 2


def _python_files(include_tests: bool) -> list[str]:
    """Repo-relative paths of every Python file under ``SCAN_ROOTS``."""
    out: list[str] = []
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for f in base.rglob("*.py"):
            parts = set(f.relative_to(REPO_ROOT).parts)
            if parts & _SKIP_DIRS:
                continue
            if not include_tests and parts & _TEST_DIRS:
                continue
            out.append(f.relative_to(REPO_ROOT).as_posix())
    return sorted(out)


def _scanned_modules() -> list[str]:
    """Every non-test Python file under ``SCAN_ROOTS`` that mentions DSR at all.

    The DSR-token filter is what keeps the scan cheap and its failures readable;
    a file that never says "DSR" or "deflat" cannot hold a DSR bar in a form this
    guard could recognise anyway, since ``_offenders`` requires that token within
    two lines of the literal.
    """
    return [
        path
        for path in _python_files(include_tests=False)
        if _DSR_TOKEN.search((REPO_ROOT / path).read_text(encoding="utf-8", errors="replace"))
    ]


def _offenders(path: str) -> list[str]:
    lines = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if not _BAR_LITERAL.search(line):
            continue
        if line.startswith(_DEFINITION):
            continue  # the one definition
        window = lines[max(0, i - _WINDOW) : i + _WINDOW + 1]
        if not any(_DSR_TOKEN.search(w) for w in window):
            continue  # a number that has nothing to do with the DSR bar
        if _RHO_PROSE.search(line) and not _DSR_TOKEN.search(line):
            continue  # the ρ̄ pool-correlation prose
        out.append(f"{path}:{i + 1}: {line.strip()}")
    return out


class TestOneLiteral:
    """The bar is written down exactly once, in ``rigor_profiles``."""

    def test_the_guarded_floor_is_still_covered(self):
        """A renamed/deleted module must not silently shrink the guard's scope."""
        missing = [m for m in GUARDED_MODULES if not (REPO_ROOT / m).is_file()]
        assert not missing, (
            "GUARDED_MODULES names files that no longer exist, so the #1794 "
            f"single-bar scan silently stopped covering them: {missing}"
        )
        scanned = set(_scanned_modules())
        dropped = [m for m in GUARDED_MODULES if m not in scanned]
        assert not dropped, (
            "Modules that grade or serve the DSR bar fell out of the derived scan "
            "set — either they moved outside SCAN_ROOTS or they stopped matching "
            f"the DSR token filter. The guard is now blind to them: {dropped}"
        )

    def test_the_scan_is_wider_than_the_backend(self):
        """Anti-vacuity, and the specific hole review found.

        The pre-review version of this guard scanned nine backend modules, so a
        stale `0.90` in an analytics-engine script sailed through. Assert the scan
        actually reaches outside `backend/` rather than trusting that it does.
        """
        scanned = _scanned_modules()
        assert len(scanned) > len(GUARDED_MODULES), (
            f"the derived scan set ({len(scanned)}) is no wider than the hand-written "
            "floor — the walk is finding nothing"
        )
        outside = [m for m in scanned if not m.startswith("backend/")]
        assert outside, "the scan no longer reaches analytics-engine/ or scripts/"

    def test_no_dsr_bar_literal_outside_its_definition(self):
        found = [o for m in _scanned_modules() for o in _offenders(m)]
        assert not found, (
            "A DSR-bar literal (0.9 / 0.90 / 0.95) appears next to DSR code outside "
            "rigor_profiles.DSR_P_BADGE_MIN. That is the #1794 defect: two bars in "
            "the tree, and which one graded you depended on which code path you "
            "reached. Import DSR_P_BADGE_MIN (or read profile.dsr_p_min); in a "
            "comment, name the constant instead of quoting its value:\n  " + "\n  ".join(found)
        )

    def test_the_definition_is_singular_and_is_the_badge_bar(self):
        """One assignment in the tree, and the ladder's badge rung IS it."""
        assignments = [
            f"{m}:{i + 1}"
            for m in _python_files(include_tests=True)
            for i, line in enumerate((REPO_ROOT / m).read_text(encoding="utf-8", errors="replace").splitlines())
            if _LITERAL_DEFINITION.match(line)
        ]
        assert len(assignments) == 1 and assignments[0].startswith("backend/archimedes/services/rigor_profiles.py:"), (
            "DSR_P_BADGE_MIN is given a literal value in more than one place — that "
            f"is two bars, which is #1794. Import it instead. Found: {assignments}"
        )
        # The number itself, pinned: 0.95 is the owner's call on #1794. Changing it
        # is a product decision, so it fails here rather than sliding through.
        assert DSR_P_BADGE_MIN == 0.95
        # ...and the ladder's badge rung IS the constant, not a copy of its value.
        assert get_profile(STRICTEST_LEVEL).dsr_p_min is DSR_P_BADGE_MIN


# ── Behaviour: the bar really is 0.95 at every call site that held a literal ──

# Straddles the bar, and also straddles the retired 0.90 one from below/above so a
# regression to it is visible rather than silently equivalent.
BELOW = 0.92
ABOVE = 0.96

# A return series whose OOS Sharpe is positive with no in-/out-of-sample cliff, so
# every non-DSR admission leg is clean and only the bar under test decides.
_CLEAN_SERIES = [0.01, -0.005, 0.008, 0.003] * 20


def _result(dsr_p: float) -> RigorGateResult:
    """A candidate that clears every leg except (possibly) the DSR bar."""
    return RigorGateResult(
        "cand",
        dsr_p_value=dsr_p,
        pbo_score=0.10,
        oos_sharpe=1.00,
        in_sample_sharpe=1.00,
        look_ahead_passed=True,
    )


class TestGateBehaviourAtTheBar:
    def test_badge_gate_fails_below_and_passes_above(self):
        assert _result(BELOW).passes_all is False, (
            f"DSR p={BELOW} cleared the badge gate — the bar is not {DSR_P_BADGE_MIN}."
        )
        assert _result(ABOVE).passes_all is True

    def test_badge_gate_detail_quotes_the_one_bar(self):
        detail = _result(BELOW).gate_details["dsr"]
        assert detail == f"FAIL (p={BELOW:.4f}, need ≥ {DSR_P_BADGE_MIN:.2f})", detail

    @pytest.mark.parametrize(("dsr_p", "expected"), [(BELOW, False), (ABOVE, True)])
    def test_generation_verdict_uses_the_same_bar(self, monkeypatch, dsr_p, expected):
        """``_rigor_verdict_for`` — the buy-and-hold verdict helper (one former literal)."""
        from archimedes.agents import generation_pipeline
        from archimedes.services import rigor_evaluator

        monkeypatch.setattr(rigor_evaluator, "compute_dsr", lambda *a, **k: (1.0, dsr_p))
        # 80 bars: a positive OOS Sharpe with no IS/OOS cliff, so the DSR leg is
        # the only thing left that can decide `passing`.
        verdict = generation_pipeline._rigor_verdict_for(_CLEAN_SERIES, num_trials=4, lookahead_passed=True)
        assert verdict["dsr_p_value"] == pytest.approx(dsr_p)
        assert verdict["passing"] is expected

    @pytest.mark.parametrize(("dsr_p", "expected"), [(BELOW, False), (ABOVE, True)])
    def test_pool_correlation_repatch_uses_the_same_bar(self, monkeypatch, dsr_p, expected):
        """``_patch_dsr_with_pool_correlation`` re-derives ``passing`` from scratch."""
        from archimedes.agents import generation_pipeline
        from archimedes.services import rigor_evaluator

        monkeypatch.setattr(rigor_evaluator, "compute_dsr", lambda *a, **k: (1.0, dsr_p))
        monkeypatch.setattr(rigor_evaluator, "compute_average_pairwise_correlation", lambda *a, **k: 0.5)

        def _cand(cid: str) -> generation_pipeline._CandidateResult:
            c = object.__new__(generation_pipeline._CandidateResult)
            c.candidate_id = cid
            c.has_real_rigor = False
            c.return_series = list(_CLEAN_SERIES)
            c.dsr_num_trials = 2
            c.rigor_verdict = {
                "dsr": 1.0,
                "dsr_p_value": 0.5,
                "pbo": 0.1,
                "oos_sharpe": 1.0,
                "in_sample_sharpe": 1.0,
                "lookahead_audit_passed": True,
                "passing": False,
            }
            return c

        cands = [_cand("a"), _cand("b")]
        generation_pipeline._patch_dsr_with_pool_correlation(cands)
        assert cands[0].rigor_verdict["dsr_p_value"] == pytest.approx(dsr_p)
        assert cands[0].rigor_verdict["passing"] is expected


# ── The UI quotes the bar too, and nothing bound it until now ────────────────
#
# `TestOneLiteral` scans Python. The bar's most consequential statements are not
# in Python: they are the sentences the app shows a visitor. #1794 was *filed* on
# one of them — the Architecture page said 90% while the gate graded at the badge
# bar — and review found that page still said 90% after the first pass at this
# fix, because the docs got a positive binding (`test_quant_docs_conflicts.
# TestThresholdsMatchLiveCode`) and the UI got hand-copies.
#
# These patterns read the number OUT of the shipped copy and compare it to the
# constant, rather than asserting a hardcoded string is present. So a reworded
# sentence keeps its binding, a new hand-copy in any recognised shape is checked
# automatically, and moving `DSR_P_BADGE_MIN` fails here until the app agrees.
_UI_FILES = (
    "ui/src/components/Architecture.jsx",
    "ui/src/components/Landing.jsx",
    "ui/src/components/RigorExplainer.jsx",
    "ui/src/components/RigorStrictnessControl.jsx",
)

# Claims about the BADGE bar. Every alternative captures the number itself.
_UI_BADGE_CLAIM = re.compile(
    r"""(?x)
      (?:positive\ at|evidence\ at\ the)\s*(?P<pct_a>\d{2})\s*%\s*one-sided
    | (?:≥|>=)\s*(?P<pct_b>\d{2})\s*%\s+confidence
    | Badge[^\n]*?thresholds:[^\n]*?conf\s*(?:≥|>=)\s*(?P<dec_a>0\.\d+)
    | confidence\s*(?:≥|>=)\s*(?P<dec_b>0\.\d+)\s*\(badge\)
    | confidence\s*(?:≥|>=)\s*(?P<dec_c>0\.\d+)\s*→
    | level:\s*1\b[^\n]*?dsr_p_min:\s*(?P<dec_d>0\.\d+)
    """
)
# Claims about the always-on FLOOR. Included so the 0.50s in the same sentences
# are checked rather than merely exempted — an exemption is a hole; a second
# binding is not.
_UI_FLOOR_CLAIM = re.compile(
    r"""(?x)
      floors?[^\n]*?conf\s*(?:≥|>=)\s*(?P<f_a>0\.\d+)
    | confidence\s*(?:≥|>=)\s*0\.\d+\s*→\s*(?P<f_b>0\.\d+)
    | floor\s+of\s+(?P<f_c>0\.\d+)
    """
)
# Anti-vacuity floor per file: how many badge claims the copy carries today. A
# rewrite that drops below this fails rather than silently unbinding the page.
_UI_MIN_BADGE_CLAIMS = {
    "ui/src/components/Architecture.jsx": 1,
    "ui/src/components/Landing.jsx": 2,
    "ui/src/components/RigorExplainer.jsx": 4,
    "ui/src/components/RigorStrictnessControl.jsx": 1,
}


def _ui_claims(path: str, pattern: re.Pattern[str]) -> list[tuple[int, str, str]]:
    """``(lineno, matched_text, captured_number)`` for each claim in ``path``."""
    text = (REPO_ROOT / path).read_text(encoding="utf-8")
    out: list[tuple[int, str, str]] = []
    for m in pattern.finditer(text):
        value = next(g for g in m.groups() if g)
        out.append((text[: m.start()].count("\n") + 1, " ".join(m.group(0).split()), value))
    return out


def _as_number(captured: str) -> float:
    """`"95"` (a percent) and `"0.95"` (a probability) both become 0.95."""
    return float(captured) / 100 if "." not in captured else float(captured)


class TestTheAppQuotesTheLiveBar:
    """Every badge bar the UI states equals ``DSR_P_BADGE_MIN``."""

    def test_the_ui_files_exist(self):
        missing = [f for f in _UI_FILES if not (REPO_ROOT / f).is_file()]
        assert not missing, f"the UI guard points at files that no longer exist: {missing}"

    @pytest.mark.parametrize("path", _UI_FILES)
    def test_the_page_still_states_the_bar(self, path):
        """Anti-vacuity: a page that stops quoting the bar unbinds itself silently."""
        found = _ui_claims(path, _UI_BADGE_CLAIM)
        expected = _UI_MIN_BADGE_CLAIMS[path]
        assert len(found) >= expected, (
            f"{path} states the badge bar {len(found)} time(s); it carried {expected}. "
            "Either the copy was reworded into a shape _UI_BADGE_CLAIM does not read — "
            "in which case teach the pattern the new shape, do not lower this count — "
            "or the statement was dropped."
        )

    @pytest.mark.parametrize("path", _UI_FILES)
    def test_every_badge_bar_in_the_ui_is_the_live_bar(self, path):
        wrong = [
            f"{path}:{ln}: {txt!r} states {val}, live bar is {DSR_P_BADGE_MIN}"
            for ln, txt, val in _ui_claims(path, _UI_BADGE_CLAIM)
            if _as_number(val) != DSR_P_BADGE_MIN
        ]
        assert not wrong, (
            "The app states a badge bar that is not the live one. That is #1794 "
            "verbatim: the public rigor surface quoting a threshold the gate does "
            "not use. The bar has ONE definition — "
            "rigor_profiles.DSR_P_BADGE_MIN:\n  " + "\n  ".join(wrong)
        )

    @pytest.mark.parametrize("path", _UI_FILES)
    def test_every_always_on_floor_in_the_ui_is_the_live_floor(self, path):
        wrong = [
            f"{path}:{ln}: {txt!r} states {val}, live floor is {DSR_P_FLOOR}"
            for ln, txt, val in _ui_claims(path, _UI_FLOOR_CLAIM)
            if _as_number(val) != DSR_P_FLOOR
        ]
        assert not wrong, (
            "The app states a DSR always-on floor that is not rigor_profiles.DSR_P_FLOOR:\n  " + "\n  ".join(wrong)
        )

    @pytest.mark.parametrize(
        "line",
        [
            "a strategy's excess Sharpe must be positive at 90% one-sided confidence",
            "deflated-Sharpe evidence at the 90% one-sided level",
            "The Verified badge needs ≥90% confidence the Sharpe is genuine.",
            "Badge (Conservative/level-1) thresholds: DSR conf ≥ 0.90 · PBO < 50%",
            "<span>confidence ≥ 0.90 (badge)</span>",
            "<div>confidence ≥ 0.90 → 0.50</div>",
            "{ level: 1, label: 'Conservative', dsr_p_min: 0.90, pbo_max: 0.5 },",
        ],
    )
    def test_the_ui_guard_rejects_the_retired_bar(self, line: str):
        """Shown red on the input it must reject, not assumed to be (CLAUDE.md rule 4)."""
        found = _UI_BADGE_CLAIM.search(line)
        assert found is not None, f"_UI_BADGE_CLAIM misses a badge-bar claim: {line!r}"
        value = next(g for g in found.groups() if g)
        assert _as_number(value) != DSR_P_BADGE_MIN, f"the retired bar read as live: {line!r}"

    @pytest.mark.parametrize(
        "line",
        [
            # The ladder's other rungs are a personal risk knob, not the badge.
            "{ level: 3, label: 'Moderate', dsr_p_min: 0.7, pbo_max: 0.6 },",
            # An example p-value in a worked figure is not a threshold claim.
            "Moreira-Muir: DSR = 0.55, p = 0.995",
            # A CSS length that happens to look like a bar.
            'style={{ fontSize: "0.92rem" }}',
            # PBO and OOS percentages are different thresholds entirely.
            "PBO &lt; 50% · OOS ≥ 50% of in-sample Sharpe",
        ],
    )
    def test_the_ui_guard_ignores_numbers_that_are_not_the_badge_bar(self, line: str):
        assert _UI_BADGE_CLAIM.search(line) is None, f"_UI_BADGE_CLAIM false-positives on: {line!r}"
