"""``gate_version`` — the provenance stamp on a stored rigor verdict.

A verdict is only meaningful if a reader can tell WHICH gate produced it
(``docs/adr/rigor-verdict-of-record.md``). ``rigor_gate_version.gate_version()``
is that answer: a digest over everything that can move a verdict WITHOUT the
strategy's own return series moving.

These tests pin three properties and nothing else:

* it is deterministic (a digest that wobbles is not an identifier);
* it MOVES when a threshold moves (a digest that ignores its inputs is not a
  version — this is the anti-vacuity case, and it is the one that matters);
* the hand-bumped ``GATE_CODE_REVISION`` is a reviewed edit, not a drift.

Deliberately NOT pinned: the literal digest string. Pinning it would make every
threshold change fail here first with a diff that says nothing about what
changed; the moves-with-its-inputs test below proves the same property and
survives a legitimate recalibration.
"""

from __future__ import annotations

import pytest
from archimedes.services import rigor_gate_version as gv


def test_the_digest_is_deterministic():
    """MUTATION: build the digest from an unsorted dict dump (drop
    ``sort_keys=True``), or seed it with anything time- or process-varying."""
    assert gv.gate_version() == gv.gate_version()


def test_the_digest_has_the_documented_shape():
    """``gate-v<schema>-<16 hex>`` — short enough for the String(64) column, and
    greppable in a DB dump."""
    value = gv.gate_version()
    prefix, digest = value.rsplit("-", 1)
    assert prefix == f"gate-v{gv.GATE_VERSION_SCHEMA}"
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)
    assert len(value) <= 64


def test_the_digest_moves_when_a_gate_threshold_moves(monkeypatch):
    """THE anti-vacuity test. MUTATION: hash a constant, or hash only
    ``GATE_CODE_REVISION``.

    A digest that does not react to the ladder would let two rows graded at
    materially different bars claim the same gate — the exact false equivalence
    the column exists to prevent. Recalibrating level 1's ``dsr_p_min`` (real
    precedent: #901 moved it one way, #1794 moved it back) must produce a
    different version.

    The recalibrated value is DERIVED from the live bar rather than written down.
    An earlier draft hardcoded the number it moved to, and the moment #1794 moved
    the badge bar onto exactly that number this test started mutating nothing and
    failed — the right lesson being that a mutation test must not pin the value it
    mutates to.
    """
    from dataclasses import replace

    from archimedes.services import rigor_profiles

    before = gv.gate_version()
    recalibrated = dict(rigor_profiles._PROFILES)
    moved = round(recalibrated[1].dsr_p_min - 0.05, 4)
    assert moved != recalibrated[1].dsr_p_min, "the recalibration must actually move the bar"
    recalibrated[1] = replace(recalibrated[1], dsr_p_min=moved)
    monkeypatch.setattr(rigor_profiles, "_PROFILES", recalibrated)

    assert gv.gate_version() != before


def test_the_digest_moves_when_the_badge_bar_constant_moves(monkeypatch):
    """The #1794 case: the badge's DSR bar is ONE constant, and moving it must
    move the digest.

    ``DSR_P_BADGE_MIN`` is level 1's ``dsr_p_min`` today, so the ladder test above
    already covers it *while that wiring holds*. It is hashed on its own because
    the wiring is a source-level property nothing at runtime can police: equal
    float constants inside one module are the SAME object, so re-writing level 1
    as a bare literal passes an ``is`` assertion against the constant. Hashing the
    constant is what makes the bar unable to move behind the digest's back —
    ``generation_pipeline`` compares against it directly, without a profile.

    MUTATION: drop ``badge_bar`` from ``gate_version_inputs`` and re-write level 1
    of ``_PROFILES`` with a literal — the digest then sits still while the bar
    every gate path reads has moved.
    """
    from archimedes.services import rigor_profiles

    assert gv.gate_version_inputs()["badge_bar"] == rigor_profiles.DSR_P_BADGE_MIN

    before = gv.gate_version()
    moved = round(rigor_profiles.DSR_P_BADGE_MIN - 0.05, 4)
    assert moved != rigor_profiles.DSR_P_BADGE_MIN, "the mutation must actually move the bar"
    monkeypatch.setattr(rigor_profiles, "DSR_P_BADGE_MIN", moved)

    assert gv.gate_version() != before


def test_the_digest_moves_when_an_always_on_floor_moves(monkeypatch):
    """The floors are not on the ladder and are not per-level, so a digest built
    from ``all_profiles()`` alone would miss them — while a change to one can
    flip a pass to a fail at every level."""
    before = gv.gate_version()
    from archimedes.services import rigor_profiles

    monkeypatch.setattr(rigor_profiles, "DSR_P_FLOOR", 0.60)
    assert gv.gate_version() != before


def test_the_digest_moves_when_the_pending_boundary_moves(monkeypatch):
    """``_MIN_RETURNS_FOR_GATE`` decides ``pending`` vs graded. Moving it
    re-labels rows without regrading anything, which is exactly the kind of
    silent change a version has to expose."""
    before = gv.gate_version()
    from archimedes.services import live_rigor_gate

    monkeypatch.setattr(live_rigor_gate, "_MIN_RETURNS_FOR_GATE", 30)
    assert gv.gate_version() != before


def test_the_code_revision_is_a_reviewed_constant():
    """The escape hatch for a LOGIC change a digest over constants cannot see.

    Pinned so bumping it is a deliberate edit in a diff a reviewer reads, per the
    module docstring's stated human obligation. When ``run_rigor_gate``'s
    behaviour changes in a way that can move a verdict with no constant moving,
    bump BOTH the constant and this number, in the same commit.
    """
    assert gv.GATE_CODE_REVISION == 1


def test_the_inputs_name_every_documented_ingredient():
    """MUTATION: silently drop an ingredient from ``gate_version_inputs``.

    The module docstring enumerates what goes in; a reader (and a re-grade
    decision) depends on that list being real. Asserting the keys keeps the prose
    and the code from drifting apart.
    """
    inputs = gv.gate_version_inputs()
    assert set(inputs) == {
        "schema",
        "code_revision",
        "badge_level",
        "badge_bar",
        "profiles",
        "floors",
        "min_returns_for_gate",
        "min_library_n_for_pbo_gating",
        "dsr_convention",
    }
    assert set(inputs["floors"]) == {"dsr_p", "oos_abs", "cpcv_min_positive_fraction"}
    assert {p["level"] for p in inputs["profiles"]} == {1, 2, 3, 4, 5}
    assert inputs["dsr_convention"]["returns"] == "excess"


def test_board_fdr_is_deliberately_absent():
    """#1654 option 1: board-level FDR is a live RELATIONAL signal on the
    leaderboard, never on the passport, and it never flips ``passes_all``. It
    therefore cannot move a stored verdict — so it must not move this digest, or
    every row would look stale every time the board changed.

    MUTATION: fold ``DEFAULT_BOARD_FDR_LEVEL`` into the inputs.
    """
    import json

    assert "fdr" not in json.dumps(gv.gate_version_inputs()).lower()


def test_legacy_derived_is_not_a_gate_version():
    """The marker the verdict-of-record migration writes for a verdict it
    INFERRED from pre-existing columns. It must never be mistaken for a real
    digest — PR-C's job is to replace every one of them with a real grade."""
    assert gv.LEGACY_DERIVED == "legacy-derived"
    assert not gv.LEGACY_DERIVED.startswith("gate-v")
    assert gv.gate_version() != gv.LEGACY_DERIVED


@pytest.mark.parametrize("attr", ["GATE_CODE_REVISION", "GATE_VERSION_SCHEMA"])
def test_the_version_constants_are_plain_ints(attr):
    """Both are hashed into the digest; a non-int would serialize differently
    across builds and break determinism silently."""
    assert isinstance(getattr(gv, attr), int)
