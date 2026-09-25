"""The portfolio LLM prompt must carry LIVE rigor verdicts, never the sentinel.

``Strategy.passes_rigor_gate`` on in-memory provider objects is a fail-closed
sentinel (always ``False``, #821). Before the M2 fix, ``_format_strategies``
rendered it as a verdict — ``rigor=candidate`` for every curated strategy — so
the portfolio-construction model was systematically told the whole library had
failed the gate.

Adversarial construction: every stub strategy here carries a POISONED
``passes_rigor_gate`` attribute that CONTRADICTS the live status passed in. A
mutant that reads the attribute (the pre-fix behavior) renders the wrong label
and fails these tests; the pre-fix code itself fails them on the ``candidate``
string it can't stop emitting.
"""

from __future__ import annotations

from archimedes.agents.portfolio_agent import (
    _build_user_prompt,
    _format_strategies,
)


class _Strategy:
    def __init__(self, sid: str, poisoned_gate: bool):
        self.id = sid
        self.paper_title = f"Paper {sid}"
        self.real_sharpe = 1.1
        self.real_cagr = 0.12
        self.strategy_code_path = "strategies/faber_taa.py"
        # POISON: contradicts the live status the test passes in. Reading this
        # attribute (instead of the status map) flips the rendered label.
        self.passes_rigor_gate = poisoned_gate


def _block_for(out: str, sid: str) -> str:
    """The 3-line block rendered for one strategy id."""
    blocks = out.split("  - id=")
    for block in blocks:
        if block.startswith(sid[:8]):
            return block
    raise AssertionError(f"no block rendered for {sid}: {out!r}")


def test_format_strategies_renders_live_statuses_not_the_attribute():
    # aaa: live says PASS but the attribute is poisoned False (attribute-reader → candidate).
    # bbb: live says FAIL but the attribute is poisoned True (attribute-reader → PASS).
    strategies = [_Strategy("aaaa1111", poisoned_gate=False), _Strategy("bbbb2222", poisoned_gate=True)]
    out = _format_strategies(strategies, {"aaaa1111": "pass", "bbbb2222": "fail"})

    assert "rigor=PASS (live gate)" in _block_for(out, "aaaa1111")
    assert "rigor=FAIL (live gate)" in _block_for(out, "bbbb2222")
    assert "candidate" not in out


def test_format_strategies_renders_degenerate_not_pending():
    """#1184: a zero-variance persisted series ("degenerate") must render its
    own label — falling through to "pending (no live verdict)" would tell the
    LLM the strategy is merely unmeasured when it was actually evaluated and
    found broken."""
    strategies = [_Strategy("eeee5555", poisoned_gate=True)]
    out = _format_strategies(strategies, {"eeee5555": "degenerate"})

    assert "rigor=DEGENERATE (zero-variance data, not a real evaluation)" in out
    assert "rigor=pending (no live verdict)" not in out


def test_format_strategies_without_statuses_says_pending_never_candidate():
    # Poisoned True: the pre-fix code renders "rigor=PASS" from the attribute —
    # an unearned pass — and "candidate" for a False one. Both are forbidden.
    out = _format_strategies([_Strategy("cccc3333", poisoned_gate=True)])
    assert "rigor=pending (no live verdict)" in out
    assert "candidate" not in out
    assert "PASS" not in out


def test_prompt_builder_threads_the_status_map():
    """``_build_user_prompt`` is the ONLY prompt builder in the module.

    This test used to loop over ``(_build_user_prompt, _build_tool_user_prompt)``.
    The tool-use prompt builder was deleted with the rest of the caller-less
    tool loop (2026-08-31); one builder remains, and it must still thread the
    live status map rather than the fail-closed attribute.
    """
    strategies = [_Strategy("dddd4444", poisoned_gate=True)]
    statuses = {"dddd4444": "fail"}
    prompt = _build_user_prompt(
        regime="risk_on",
        regime_confidence=0.8,
        risk_profile="moderate",
        usdc_floor=0.2,
        synth_budget=0.8,
        market_ranking=[],
        strategies=strategies,
        scan_universe_synths=set(),
        rigor_statuses=statuses,
    )
    assert "rigor=FAIL (live gate)" in prompt
    # The exact pre-fix label.
    assert "rigor=candidate" not in prompt
