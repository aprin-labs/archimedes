"""Tests for the REAL look-ahead audit of DSL-interpreted strategies.

The thing under test replaced a boolean the LLM declared about its own output
(``spec.look_ahead_safe``) with a structural proof:

  1. an AST audit of the DSL interpreter showing every bar-indexed read is at
     offset ``<= 0`` (bar t or earlier);
  2. a walk of the validated spec showing it uses nothing outside that audited
     surface;
  3. the broker cheat-on-close/open check charged on the real cerebro.

Every guard here is demonstrated adversarially — each test builds the input that
SHOULD fail the check and asserts it does. A guard nobody has watched reject
something is not a guard.
"""

from __future__ import annotations

import ast
import inspect

import backtrader as bt
import pytest
from archimedes.services import dsl_lookahead_audit as la
from archimedes.services import dsl_to_backtrader, strategy_dsl
from archimedes.services.dsl_lookahead_audit import (
    DEGENERATE,
    FAIL,
    PASS,
    PENDING,
    audit_dsl_strategy,
    broker_cheat_check_passed,
    reset_interpreter_surface_cache,
    verify_interpreter_surface,
)
from archimedes.services.fusion_evaluator import apply_rigor_gate, evaluate_fusion_spec, run_dsl_backtest
from archimedes.services.strategy_dsl import FABER_2007_SPEC, StrategySpec, validate_strategy_spec


@pytest.fixture(autouse=True)
def _clean_surface_cache():
    """The interpreter audit is process-cached; tests that patch it must not leak."""
    reset_interpreter_surface_cache()
    yield
    reset_interpreter_surface_cache()


def _audit_source(source: str) -> la.InterpreterSurfaceAudit:
    """Run the interpreter bar-offset verifier over arbitrary source text.

    Used by the adversarial tests to feed the verifier code it SHOULD reject,
    without mutating the real interpreter on disk.
    """
    visitor = la._InterpreterAccessVisitor()
    visitor.visit(ast.parse(source))
    return la.InterpreterSurfaceAudit(
        bar_access_verified=not visitor.violations,
        violations=tuple(visitor.violations),
        sites_checked=len(visitor.sites),
    )


# ── 1. The interpreter surface audit ──────────────────────────────────


class TestInterpreterSurfaceAudit:
    def test_real_interpreter_passes_and_actually_checked_something(self):
        """The shipped interpreter reads only bar t and earlier.

        ``sites_checked > 0`` is load-bearing: a verifier that matched no data
        access would report "no violations" and be worthless. Assert it found
        the real reads.
        """
        audit = verify_interpreter_surface()
        assert audit.violations == ()
        assert audit.bar_access_verified is True
        assert audit.sites_checked >= 8, f"verifier matched only {audit.sites_checked} bar-access sites"
        assert audit.ok is True

    def test_no_unaudited_enum_drift_on_main(self):
        """The DSL's live enums have not grown past the audited surface."""
        assert verify_interpreter_surface().unverified_extensions == {}

    def test_verifier_finds_every_data_read_in_the_real_interpreter(self):
        """Pin the exact set of bar-indexed reads the audit covers.

        If someone adds a data read the visitor does not model, this count moves
        and the test says so, rather than the audit quietly covering less.
        """
        visitor = la._InterpreterAccessVisitor()
        visitor.visit(ast.parse(inspect.getsource(dsl_to_backtrader)))
        sources = sorted({s.split(": ", 1)[1] for s in visitor.sites})
        assert sources == [
            "data_line(...)",
            "ind[...]",
            "self.data.close[...]",
            "self.data.high[...]",
            "self.data.low[...]",
            "self.data.open[...]",
            "self.data.volume[...]",
            # RealizedVolAnnualized (#1567) reads its own input line directly:
            # inside an Indicator `self.data` IS the line it was constructed
            # with, so the read has no `.close` suffix to render.
            "self.data[...]",
        ]


class TestInterpreterVerifierRejectsFutureReads:
    """ADVERSARIAL: feed the verifier interpreters that DO read the future.

    Bodies that use ``i`` put it inside ``for i in range(20):`` on purpose: that
    is the ONE way this verifier will accept a name inside a negation, so writing
    it that way isolates the specific defect each case is about instead of
    tripping the (correct) "``i`` is not provably non-negative" rejection.
    """

    @pytest.mark.parametrize(
        ("body", "needle"),
        [
            # The canonical leak: bar t+1's close.
            ("        return float(self.data.close[1])", "positive bar offset [1]"),
            # A larger forward jump.
            ("        return float(self.data.high[5])", "positive bar offset [5]"),
            # An offset the verifier cannot prove — refuses to guess.
            ("        return float(self.data.close[shift])", "unprovable bar offset expression"),
            # Arithmetic that moves forward in time.
            ("        for i in range(20):\n            return float(self.data.close[-i + 2])", "arithmetic"),
            # Subtracting a negative == adding.
            ("        for i in range(20):\n            return float(self.data.close[-i - -3])", "FORWARD in time"),
            # backtrader's delayed-line accessor pointed the wrong way.
            ("        return float(self.data.close(1)[0])", "positive bar offset [1]"),
            # A negated name nothing proves non-negative. THIS is the hole the
            # old verifier had: it accepted every USub on sight.
            ("        return float(self.data.close[-shift])", "not provably non-negative"),
            # A negated CALL — likewise unprovable.
            ("        return float(self.data.close[-offset()])", "not provably non-negative"),
            # A subtrahend nothing proves non-negative: `-i - j` with j unknown
            # can move forward.
            ("        for i in range(20):\n            return float(self.data.close[-i - j])", "subtrahend"),
            # A range that can itself yield negatives, so `-i` can be positive.
            ("        for i in range(-5, 5):\n            return float(self.data.close[-i])", "non-negative"),
        ],
    )
    def test_forward_read_is_rejected(self, body: str, needle: str):
        source = "class S:\n    def next(self):\n" + body + "\n"
        audit = _audit_source(source)
        assert audit.ok is False, f"verifier ACCEPTED a future read: {body.strip()}"
        assert any(needle in v for v in audit.violations), audit.violations

    def test_negated_module_constant_that_is_itself_negative_is_rejected(self):
        """ADVERSARIAL, the exact mutation the old verifier waved through.

        ``_SHIFT = -1`` makes ``self.data.close[-_SHIFT]`` literally
        ``self.data.close[1]`` — bar *t+1*. The old ``_classify_offset`` accepted
        any ``UnaryOp(USub)`` whose operand was not itself negated, so this
        interpreter passed the audit clean while reading the future. It must FAIL.
        """
        source = "_SHIFT = -1\n\nclass S:\n    def next(self):\n        return float(self.data.close[-_SHIFT])\n"
        audit = _audit_source(source)
        assert audit.ok is False, "verifier ACCEPTED self.data.close[-_SHIFT] with _SHIFT = -1"
        assert any("not provably non-negative" in v for v in audit.violations), audit.violations

    def test_past_and_present_reads_are_accepted(self):
        """The mirror image: legitimate backtrader offsets must NOT be flagged.

        Without this, a verifier that rejects everything would pass the tests
        above while being useless. ``i`` is ``range()``-bound and ``period`` is
        guarded — the two proofs the verifier accepts.
        """
        source = (
            "class S:\n"
            "    def next(self):\n"
            "        a = float(self.data.close[0])\n"
            "        b = float(self.data.close[-1])\n"
            "        for i in range(20):\n"
            "            c = float(self.data.close[-i])\n"
            "            d = float(self.data.close[-i - 1])\n"
            "        e = [float(self.data.close[-k]) for k in range(5)]\n"
            "        return a + b + c + d + e[0]\n"
        )
        audit = _audit_source(source)
        assert audit.violations == ()
        assert audit.sites_checked == 5

    def test_a_guarded_parameter_is_proven_but_an_unguarded_one_is_not(self):
        """The other accepted proof: ``if period < 1: raise`` dominates the read.

        Both halves matter. Without the guard the same expression is unproven and
        rejected — which is why deleting the guard in ``_make_indicator`` fails
        the audit closed instead of silently weakening it.
        """
        guarded = (
            "def make(data_line: bt.LineSeries, period):\n"
            "    if period < 1:\n"
            "        raise DSLError('bad period')\n"
            "    return data_line(-period)\n"
        )
        assert _audit_source(guarded).violations == ()

        unguarded = "def make(data_line: bt.LineSeries, period):\n    return data_line(-period)\n"
        audit = _audit_source(unguarded)
        assert audit.ok is False
        assert any("not provably non-negative" in v for v in audit.violations), audit.violations

    def test_a_read_above_its_guard_is_not_covered_by_it(self):
        """The guard proves nothing about lines that run before it."""
        source = (
            "def make(data_line: bt.LineSeries, period):\n"
            "    early = data_line(-period)\n"
            "    if period < 1:\n"
            "        raise DSLError('bad period')\n"
            "    return early\n"
        )
        audit = _audit_source(source)
        assert audit.ok is False
        assert any("not provably non-negative" in v for v in audit.violations), audit.violations

    def test_line_aliases_are_tracked_through_assignment(self):
        """A future read hidden behind a local alias is still caught."""
        source = "class S:\n    def next(self):\n        line = self.data.close\n        return float(line[1])\n"
        audit = _audit_source(source)
        assert audit.ok is False
        assert any("positive bar offset [1]" in v for v in audit.violations)

    def test_alias_used_before_its_binding_is_still_checked(self):
        """ADVERSARIAL: source ORDER must not decide whether a read is audited.

        The single-pass visitor bound aliases as it walked, so a leak textually
        ABOVE its ``line = self.data.close`` assignment was never classified at
        all. Statement order is not a security boundary.
        """
        source = (
            "class S:\n"
            "    def early(self):\n"
            "        return float(line[1])\n"
            "\n"
            "    def bind(self):\n"
            "        line = self.data.close\n"
            "        return line\n"
        )
        audit = _audit_source(source)
        assert audit.ok is False, "a leak above its alias binding went unaudited"
        assert any("positive bar offset [1]" in v for v in audit.violations), audit.violations

    def test_line_annotated_parameter_is_tracked(self):
        """``_make_indicator``-shaped code: a line arrives as a typed parameter."""
        source = "def make(data_line: bt.LineSeries):\n    return data_line(2)\n"
        audit = _audit_source(source)
        assert audit.ok is False
        assert any("positive bar offset [2]" in v for v in audit.violations)

    def test_non_line_indexing_is_not_flagged(self):
        """List/string indexing must not trip the verifier (no false positives).

        The generic AST auditor in ``rigor_evaluator`` flags ``args[1]`` and
        ``parts[1]`` in this very module — which is exactly why this verifier is
        root-based rather than a reuse of that one.
        """
        source = (
            "def f(name, args):\n"
            "    parts = name.rsplit('_', 1)\n"
            "    right = args[1]\n"
            "    return parts[0], parts[1], right\n"
        )
        audit = _audit_source(source)
        assert audit.violations == ()

    def test_a_broken_verifier_that_matches_nothing_is_not_a_pass(self):
        """``sites_checked == 0`` must not read as clean."""
        audit = _audit_source("def f():\n    return 1\n")
        assert audit.violations == ()
        assert audit.ok is False, "a verifier that matched no data access must not report OK"


class TestFeedIndexingIsNotABarOffset:
    """``self.datas[i]`` picks a FEED. It is not a read of bar ``i``.

    Both directions matter. Classifying the feed index as a bar offset would fail
    a perfectly causal multi-feed interpreter and take the gate down for every
    strategy at once (a total outage, not a leak). Skipping everything downstream
    of it would be the leak.
    """

    def test_multi_feed_indexing_does_not_fail_the_audit(self):
        """A causal multi-feed interpreter must not take the whole gate down."""
        source = (
            "class S:\n"
            "    def next(self):\n"
            "        a = float(self.datas[0].close[0])\n"
            "        b = float(self.datas[1].close[-1])\n"
            "        return a + b\n"
        )
        audit = _audit_source(source)
        assert audit.violations == (), audit.violations
        assert audit.sites_checked == 2, "the reads DOWNSTREAM of the feed pick must still be audited"

    def test_a_forward_read_on_a_picked_feed_is_still_caught(self):
        """Skipping the feed index must not skip what hangs off it."""
        source = "class S:\n    def next(self):\n        return float(self.datas[1].close[1])\n"
        audit = _audit_source(source)
        assert audit.ok is False, "a forward read behind a feed pick went unaudited"
        assert any("positive bar offset [1]" in v for v in audit.violations), audit.violations

    def test_a_feed_bound_to_a_local_is_still_audited(self):
        source = "class S:\n    def next(self):\n        feed = self.datas[1]\n        return float(feed.close[2])\n"
        audit = _audit_source(source)
        assert audit.ok is False
        assert any("positive bar offset [2]" in v for v in audit.violations), audit.violations

    def test_iterating_the_feeds_binds_each_feed_as_a_line(self):
        source = (
            "class S:\n    def next(self):\n        for feed in self.datas:\n            return float(feed.close[3])\n"
        )
        audit = _audit_source(source)
        assert audit.ok is False
        assert any("positive bar offset [3]" in v for v in audit.violations), audit.violations


class TestInterpreterAuditFailureFailsTheStrategy:
    """ADVERSARIAL, end to end: a leaking interpreter fails every spec."""

    def test_spec_fails_when_the_interpreter_audit_fails(self, monkeypatch):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        assert audit_dsl_strategy(spec, broker_cheat_check=True).status == PASS

        broken = la.InterpreterSurfaceAudit(
            bar_access_verified=False,
            violations=("line 158: self.data.close[...] — positive bar offset [1] reads a FUTURE bar",),
            sites_checked=10,
        )
        monkeypatch.setattr(la, "verify_interpreter_surface", lambda: broken)

        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert audit.passed is False
        assert any("FUTURE bar" in r for r in audit.reasons)


# ── 2. The spec-level structural walk ─────────────────────────────────


class TestVerifiedSurfaceSpecPasses:
    def test_faber_spec_passes_structural(self):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == PASS
        assert audit.passed is True
        assert audit.reasons == ()
        assert audit.interpreter_verified is True
        assert audit.broker_cheat_check is True
        assert "PASS (structural)" in audit.label

    def test_a_richer_in_surface_spec_passes(self):
        """Nested logic, several indicators, a variant grid — all in surface."""
        spec = validate_strategy_spec(
            {
                "name": "Multi-indicator",
                "asset_universe": ["SPY", "QQQ"],
                "rebalance_frequency": "weekly",
                "entry": {
                    "and": [
                        {"gt": ["close", "sma_50"]},
                        {"or": [{"lt": ["rsi_14", 70]}, {"gt": ["momentum_20", 0]}]},
                    ]
                },
                "exit": {"not": {"gte": ["close", "ema_100"]}},
                "position_sizing": {"type": "volatility_target", "annual_pct": 0.15},
                "source_arxiv_ids": ["1234.5678"],
                "look_ahead_safe": True,
                "parameter_variants": {"sma_50": [40, 50, 60]},
            }
        )
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == PASS, audit.reasons


class TestOutOfSurfaceSpecsFail:
    """ADVERSARIAL: constructs the interpreter audit never covered must FAIL.

    These specs are built directly as ``StrategySpec`` rather than through
    ``validate_strategy_spec``, because the DSL validator rejects most of them at
    the door. That is the point: the audit must not depend on the validator
    having run, and it must reject an unknown construct instead of assuming the
    closed enum still holds.
    """

    @staticmethod
    def _spec(**overrides) -> StrategySpec:
        base = {
            "name": "Out of surface",
            "asset_universe": ["SPY"],
            "rebalance_frequency": "monthly",
            "entry": {"gt": ["close", "sma_200"]},
            "exit": {"lt": ["close", "sma_200"]},
            "position_sizing": {"type": "full_invested_when_in_market"},
            "source_arxiv_ids": ["0706.1497"],
            "indicators": ["sma_200"],
        }
        base.update(overrides)
        return StrategySpec(**base)

    def test_unknown_indicator_fails(self):
        """A test-only indicator the interpreter was never audited for."""
        spec = self._spec(
            entry={"gt": ["close", "oracle_5"]},
            exit={"lt": ["close", "oracle_5"]},
            indicators=["oracle_5"],
        )
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert audit.passed is False
        assert any("'oracle'" in r and "outside the audited surface" in r for r in audit.reasons), audit.reasons

    def test_a_spec_cannot_carry_a_declaration_to_rescue_itself(self):
        """The whole point of the change: there is no field left to declare in.

        The old schema let a spec assert ``look_ahead_safe: true`` and be
        admitted on the assertion. ``StrategySpec`` has no such field now, so the
        rescue is not merely ignored — it does not typecheck.
        """
        with pytest.raises(TypeError):
            self._spec(look_ahead_safe=True)

    def test_unknown_operator_fails(self):
        spec = self._spec(entry={"crosses_above": ["close", "sma_200"]})
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert any("crosses_above" in r for r in audit.reasons), audit.reasons

    def test_unknown_price_operand_fails(self):
        """``next_close`` is not a series the interpreter binds at bar t."""
        spec = self._spec(entry={"gt": ["next_close", "sma_200"]})
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert any("next_close" in r for r in audit.reasons), audit.reasons

    def test_indicator_alias_not_declared_by_the_spec_fails(self):
        """An alias the interpreter never binds into ``_bar_values``."""
        spec = self._spec(entry={"gt": ["close", "sma_50"]}, indicators=["sma_200"])
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert any("sma_50" in r for r in audit.reasons), audit.reasons

    def test_non_positive_indicator_period_fails(self):
        """``momentum_0`` compiles to ``data_line(-0)`` — bar t, not a past bar."""
        spec = self._spec(
            entry={"gt": ["momentum_0", 0]},
            exit={"lt": ["momentum_0", 0]},
            indicators=["momentum_0"],
        )
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert any("strictly past bar" in r for r in audit.reasons), audit.reasons

    def test_negative_variant_period_fails(self):
        """A variant grid is compiled surface too — ``momentum_-5`` reads forward."""
        spec = self._spec(
            entry={"gt": ["momentum_20", 0]},
            exit={"lt": ["momentum_20", 0]},
            indicators=["momentum_20"],
            parameter_variants={"momentum_20": [10, -5]},
        )
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert any("strictly past bar" in r for r in audit.reasons), audit.reasons

    def test_unknown_position_sizing_fails(self):
        spec = self._spec(position_sizing={"type": "kelly_optimal"})
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert any("kelly_optimal" in r for r in audit.reasons), audit.reasons

    def test_unknown_rebalance_frequency_fails(self):
        spec = self._spec(rebalance_frequency="intraday")
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert any("intraday" in r for r in audit.reasons), audit.reasons

    def test_unimplemented_indicator_fails(self):
        """``realized_vol`` is in the DSL enum but has no interpreter branch."""
        spec = self._spec(
            entry={"gt": ["realized_vol_20", 0]},
            exit={"lt": ["realized_vol_20", 0]},
            indicators=["realized_vol_20"],
        )
        audit = audit_dsl_strategy(spec, broker_cheat_check=True)
        assert audit.status == FAIL
        assert any("no audited interpreter implementation" in r for r in audit.reasons), audit.reasons


class TestEnumDriftIsNotInheritedAsAPass:
    """ADVERSARIAL: widening the DSL enum without re-auditing must not pass."""

    def test_new_indicator_added_to_the_dsl_enum_still_fails_the_audit(self, monkeypatch):
        monkeypatch.setattr(
            strategy_dsl,
            "INDICATOR_NAMES",
            frozenset(strategy_dsl.INDICATOR_NAMES | {"oracle"}),
        )
        reset_interpreter_surface_cache()

        surface = verify_interpreter_surface()
        assert surface.unverified_extensions["indicators"] == ("oracle",)

        spec = TestOutOfSurfaceSpecsFail._spec(
            entry={"gt": ["close", "oracle_5"]},
            exit={"lt": ["close", "oracle_5"]},
            indicators=["oracle_5"],
        )
        assert audit_dsl_strategy(spec, broker_cheat_check=True).status == FAIL

    def test_a_spec_avoiding_the_new_construct_still_passes(self, monkeypatch):
        """Drift fails the constructs that use it, not everything else."""
        monkeypatch.setattr(
            strategy_dsl,
            "INDICATOR_NAMES",
            frozenset(strategy_dsl.INDICATOR_NAMES | {"oracle"}),
        )
        reset_interpreter_surface_cache()
        spec = validate_strategy_spec(FABER_2007_SPEC)
        assert audit_dsl_strategy(spec, broker_cheat_check=True).status == PASS


# ── 3. The broker execution-timing check ──────────────────────────────


class TestBrokerCheatCheck:
    def test_a_default_cerebro_passes(self):
        assert broker_cheat_check_passed(bt.Cerebro(stdstats=False)) is True

    def test_cheat_on_close_is_caught(self):
        """ADVERSARIAL: a broker that fills on the signal's own bar close."""
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.broker.set_coc(True)
        assert broker_cheat_check_passed(cerebro) is False

    def test_cheat_on_open_is_caught(self):
        """ADVERSARIAL: a broker that fills on the signal bar's open."""
        cerebro = bt.Cerebro(stdstats=False)
        cerebro.broker.set_coo(True)
        assert broker_cheat_check_passed(cerebro) is False

    def test_a_cerebro_without_broker_params_fails_closed(self):
        """An unverifiable broker is not a verified one."""

        class _NoBroker:
            pass

        assert broker_cheat_check_passed(_NoBroker()) is False

    def test_a_cheating_broker_fails_the_whole_audit(self):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        audit = audit_dsl_strategy(spec, broker_cheat_check=False)
        assert audit.status == FAIL
        assert any("cheat on close/open" in r for r in audit.reasons)


class TestBrokerCheckIsWiredIntoTheDslBacktestPath:
    """ADVERSARIAL, end to end through ``run_dsl_backtest``."""

    def test_normal_run_records_a_passing_broker_check(self):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = run_dsl_backtest(spec)
        assert metrics.broker_cheat_check_passed is True

    def test_a_cheating_broker_reaches_the_rigor_verdict_as_failed(self, monkeypatch):
        """Simulate a future misconfiguration of the broker and follow it out.

        Patches ``bt.Cerebro`` (the boundary — the broker's configuration), not
        the audit under test, then asserts the failure survives all the way to
        ``RigorVerdict.look_ahead_audit`` and flips ``passing`` off.
        """

        class _CheatingCerebro(bt.Cerebro):
            """A Cerebro misconfigured to fill on the signal's own bar.

            A subclass, not a factory function: backtrader's ``findowner`` does
            ``isinstance(obj, bt.Cerebro)`` during strategy construction, so the
            replacement has to remain a type.
            """

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.broker.set_coc(True)

        monkeypatch.setattr(bt, "Cerebro", _CheatingCerebro)

        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = run_dsl_backtest(spec)
        assert metrics.broker_cheat_check_passed is False

        verdict = apply_rigor_gate(metrics, spec=spec)
        assert verdict.look_ahead_status == FAIL
        assert verdict.look_ahead_clean is False
        assert verdict.passing is False

    def test_a_broker_that_starts_cheating_DURING_the_run_is_caught(self, monkeypatch):
        """ADVERSARIAL: cheat-on-close is settable from inside the backtest.

        A strategy's ``__init__``/``next`` can call ``self.broker.set_coc(True)``
        and backtrader honours it for the fills that follow. The check used to be
        charged BEFORE ``cerebro.run()``, so a broker that cheated for the entire
        run was recorded clean and the verdict came out ``pass``.

        The Cerebro subclass here flips the flag inside ``run()`` — the same
        observable state a mid-run ``set_coc`` produces — patched at the boundary
        (the broker's configuration), not at the audit under test.
        """

        class _MidRunCheatingCerebro(bt.Cerebro):
            def run(self, *args, **kwargs):
                self.broker.set_coc(True)
                return super().run(*args, **kwargs)

        monkeypatch.setattr(bt, "Cerebro", _MidRunCheatingCerebro)

        spec = validate_strategy_spec(FABER_2007_SPEC)
        metrics = run_dsl_backtest(spec)
        assert metrics.broker_cheat_check_passed is False, "a mid-run cheat-on-close recorded as clean"

        verdict = apply_rigor_gate(metrics, spec=spec)
        assert verdict.look_ahead_status == FAIL
        assert verdict.passing is False

    def test_sleeve_aggregation_is_fail_closed_on_an_unchecked_sleeve(self):
        from archimedes.services.fusion_evaluator import _combine_broker_checks

        assert _combine_broker_checks([True, True]) is True
        assert _combine_broker_checks([True, False]) is False
        assert _combine_broker_checks([True, None]) is None
        assert _combine_broker_checks([False, None]) is False
        assert _combine_broker_checks([]) is None


# ── 4. An audit that reached no verdict is not a pass ─────────────────


class TestANonConclusiveAuditIsNotAPass:
    def test_no_spec_is_pending_and_does_not_pass(self):
        audit = audit_dsl_strategy(None, broker_cheat_check=True)
        assert audit.status == PENDING
        assert audit.passed is False
        assert "NOT AUDITED" in audit.label

    def test_no_broker_check_is_pending(self):
        """An incomplete audit does not get to claim a structural pass."""
        spec = validate_strategy_spec(FABER_2007_SPEC)
        audit = audit_dsl_strategy(spec, broker_cheat_check=None)
        assert audit.status == PENDING
        assert audit.passed is False
        assert audit.interpreter_verified is True

    def test_gate_treats_pending_as_a_leak_failure(self):
        """The LEAK criterion: only ``pass`` clears it.

        This is the regression guard for the whole change. Before it,
        ``look_ahead_clean`` was the literal ``True`` on the next line of
        ``apply_rigor_gate`` — mirroring the LLM's own ``look_ahead_safe``
        declaration — so this verdict passed.
        """
        from tests.services.test_fusion_evaluator import _make_high_sharpe_metrics, _two_variant_set

        metrics = _make_high_sharpe_metrics(data_source="csv:spy.csv")
        variants = _two_variant_set(metrics.equity_curve, data_source="csv:spy.csv")

        unaudited = apply_rigor_gate(metrics, variants_metrics=variants, spec=None)
        assert unaudited.look_ahead_status == PENDING
        assert unaudited.look_ahead_clean is False
        assert unaudited.passing is False, "an audit that never ran must not clear the LEAK criterion"

        # Same numbers, same everything — the ONLY delta is a real audit subject.
        audited = apply_rigor_gate(metrics, variants_metrics=variants, spec=validate_strategy_spec(FABER_2007_SPEC))
        assert audited.look_ahead_status == PASS
        assert audited.passing is True, "the other gate legs must be unchanged"


class TestTheUndecidableCaseIsDegenerateNotFailed:
    """``degenerate``: the audit ran and its own instrument gave no reading.

    ``sites_checked == 0`` means the AST pass matched nothing, so it proved
    nothing AND disproved nothing. Fail-closed on deploy, but the strategy is
    accused of nothing — the same distinction ``RigorGateVerdict`` already draws
    between ``degenerate`` and ``fail``.
    """

    @staticmethod
    def _blind_surface() -> la.InterpreterSurfaceAudit:
        return la.InterpreterSurfaceAudit(bar_access_verified=True, violations=(), sites_checked=0)

    def test_an_audit_that_examined_nothing_is_degenerate(self, monkeypatch):
        monkeypatch.setattr(la, "verify_interpreter_surface", self._blind_surface)
        audit = audit_dsl_strategy(validate_strategy_spec(FABER_2007_SPEC), broker_cheat_check=True)

        assert audit.status == DEGENERATE
        assert audit.passed is False, "an instrument with no reading is not evidence of cleanliness"
        assert audit.blocks_deploy is True, "fail-closed"
        assert audit.conclusive is False
        assert "FAIL" not in audit.label, "nothing about this strategy failed — the verifier did"
        assert audit.not_run_reason

    def test_unreadable_interpreter_source_is_degenerate_too(self, monkeypatch):
        monkeypatch.setattr(
            la,
            "verify_interpreter_surface",
            lambda: la.InterpreterSurfaceAudit(
                bar_access_verified=False,
                violations=("interpreter source unavailable for audit: boom",),
                sites_checked=0,
            ),
        )
        audit = audit_dsl_strategy(validate_strategy_spec(FABER_2007_SPEC), broker_cheat_check=True)
        assert audit.status == DEGENERATE
        assert audit.blocks_deploy is True

    def test_a_real_interpreter_violation_is_still_FAIL_not_degenerate(self, monkeypatch):
        """The other direction: an instrument that DID read must accuse.

        Without this arm the `degenerate` branch could swallow every real
        interpreter violation and the audit would never fail again.
        """
        monkeypatch.setattr(
            la,
            "verify_interpreter_surface",
            lambda: la.InterpreterSurfaceAudit(
                bar_access_verified=False,
                violations=("line 42: self.data.close[...] — positive bar offset [1]",),
                sites_checked=17,
            ),
        )
        audit = audit_dsl_strategy(validate_strategy_spec(FABER_2007_SPEC), broker_cheat_check=True)
        assert audit.status == FAIL
        assert audit.label.startswith("FAIL")

    def test_degenerate_blocks_the_gate(self, monkeypatch):
        from tests.services.test_fusion_evaluator import _make_high_sharpe_metrics, _two_variant_set

        metrics = _make_high_sharpe_metrics(data_source="csv:spy.csv")
        variants = _two_variant_set(metrics.equity_curve, data_source="csv:spy.csv")
        spec = validate_strategy_spec(FABER_2007_SPEC)

        clean = apply_rigor_gate(metrics, variants_metrics=variants, spec=spec)
        assert clean.passing is True, "control arm must genuinely pass or the assertion below is vacuous"

        monkeypatch.setattr(la, "verify_interpreter_surface", self._blind_surface)
        blind = apply_rigor_gate(metrics, variants_metrics=variants, spec=spec)
        assert blind.look_ahead_status == DEGENERATE
        assert blind.passing is False
        # Nothing but the look-ahead leg moved.
        assert (blind.dsr, blind.oos_sharpe, blind.pbo_score) == (clean.dsr, clean.oos_sharpe, clean.pbo_score)


class TestFailClosedDeployButHonestRendering:
    """The owner doctrine: only a structural PASS deploys; nothing renders an
    audit that reached no verdict as a FAIL.

    Two axes, deliberately not the same axis. ``pending``/``degenerate`` must
    block deploy exactly as hard as ``fail`` AND must never be shown to a user as
    "your strategy failed a look-ahead audit" — because nothing looked.
    """

    def test_pending_blocks_deploy_but_never_renders_as_a_failure(self):
        audit = audit_dsl_strategy(None, broker_cheat_check=True)
        assert audit.status == PENDING
        # Deploy: fail-closed.
        assert audit.passed is False
        assert audit.blocks_deploy is True
        # Render: honest.
        assert audit.conclusive is False
        assert "NOT AUDITED" in audit.label
        assert "FAIL" not in audit.label
        assert audit.not_run_reason

    def test_a_real_violation_renders_failed_and_blocks(self):
        """The mirror: a check that DID look and found something must say FAIL."""
        spec = validate_strategy_spec(FABER_2007_SPEC)
        audit = audit_dsl_strategy(spec, broker_cheat_check=False)
        assert audit.status == FAIL
        assert audit.blocks_deploy is True
        assert audit.conclusive is True
        assert audit.label.startswith("FAIL")
        assert audit.not_run_reason is None, "a real failure must never be labelled 'not run'"

    def test_a_structural_pass_deploys(self):
        audit = audit_dsl_strategy(validate_strategy_spec(FABER_2007_SPEC), broker_cheat_check=True)
        assert audit.blocks_deploy is False
        assert audit.conclusive is True
        assert audit.not_run_reason is None

    def test_there_is_exactly_one_four_state_vocabulary(self):
        """No second vocabulary to keep in sync — and the words are the four the
        rest of the rigor stack already uses for the same idea."""
        from archimedes.services import live_rigor_gate

        assert {la.PASS, la.FAIL, la.PENDING, la.DEGENERATE} == la.AUDIT_STATUSES
        assert {la.PASS, la.FAIL} == la.CONCLUSIVE_STATUSES
        assert {
            live_rigor_gate.PASS,
            live_rigor_gate.FAIL,
            live_rigor_gate.PENDING,
            live_rigor_gate.DEGENERATE,
        } == la.AUDIT_STATUSES
        assert not hasattr(la, "_RENDER_STATE_BY_STATUS"), "a second render vocabulary is exactly what was removed"

    def test_no_state_but_pass_is_ever_deployable(self):
        for status in la.AUDIT_STATUSES:
            audit = la.DslLookAheadAudit(status=status, reasons=("why",))
            assert audit.passed is (status == la.PASS)
            assert audit.blocks_deploy is (status != la.PASS)
            if status not in la.CONCLUSIVE_STATUSES:
                assert "FAIL" not in audit.label, f"{status!r} must never render as an accusation"

    def test_the_verdict_carries_the_four_state_to_the_api(self):
        from tests.services.test_fusion_evaluator import _make_high_sharpe_metrics, _two_variant_set

        metrics = _make_high_sharpe_metrics(data_source="csv:spy.csv")
        variants = _two_variant_set(metrics.equity_curve, data_source="csv:spy.csv")

        v = apply_rigor_gate(metrics, variants_metrics=variants, spec=None)
        assert v.look_ahead_status == PENDING
        assert "FAIL" not in v.look_ahead_label
        assert v.look_ahead_reason
        assert v.passing is False, "not-checked must still block"

    def test_gate_details_says_NOT_RUN_not_FAIL_when_the_leg_never_ran(self):
        """ADVERSARIAL on the render: the detail line a user actually reads.

        ``run_rigor_gate`` with no source and no supplied verdict cannot evaluate
        the look-ahead leg at all. It used to render that as the literal string
        "FAIL" — an accusation about a check that never happened. Admission is
        unchanged (the always-on floor still blocks); only the wording is honest.
        """
        import numpy as np
        from archimedes.services.rigor_evaluator import run_rigor_gate

        returns = np.random.default_rng(7).normal(0.001, 0.008, size=300).tolist()
        result = run_rigor_gate(strategy_id="no_code", daily_returns=returns, num_trials=1)

        assert result.look_ahead_passed is False
        assert result.blocked_by_floor is True, "fail-closed: a not-run look-ahead leg still blocks"
        detail = result.gate_details["look_ahead"]
        assert detail.startswith("NOT_RUN ("), detail
        assert "blocks admission" in detail
        assert detail != "FAIL"

    def test_gate_details_still_says_FAIL_for_a_real_look_ahead_failure(self):
        """The guard must still be able to say FAIL, or it guards nothing."""
        from archimedes.services.rigor_evaluator import RigorGateResult

        assert RigorGateResult("s", look_ahead_passed=False).gate_details["look_ahead"] == "FAIL"
        leaking = RigorGateResult(
            "s",
            look_ahead_passed=False,
            look_ahead_not_run_reason=None,
        )
        assert leaking.gate_details["look_ahead"] == "FAIL"

    def test_not_run_reason_from_verdict_only_fires_for_a_non_verdict(self):
        from archimedes.services.dsl_lookahead_audit import not_run_reason_from_verdict

        assert not_run_reason_from_verdict({"look_ahead_status": PASS}) is None
        assert not_run_reason_from_verdict({"look_ahead_status": FAIL}) is None
        # A row written before this landed: its False is not evidence of a
        # not-run, so it keeps the plain FAIL rendering.
        assert not_run_reason_from_verdict({}) is None
        assert (
            not_run_reason_from_verdict({"look_ahead_status": PENDING, "look_ahead_reason": "no validated spec"})
            == "no validated spec"
        )
        assert (
            not_run_reason_from_verdict({"look_ahead_status": DEGENERATE, "look_ahead_reason": "verifier is blind"})
            == "verifier is blind"
        )


class TestThereIsNoDeclaredFlagLeftToRead:
    """The self-declared boolean is GONE, not demoted to a recorded field.

    ``look_ahead_safe`` was removed from ``REQUIRED_FIELDS``, from
    :class:`StrategySpec`, from ``to_dict``, and from the generation prompt. The
    audit therefore cannot consult it even by accident: there is no attribute,
    and nothing in this module names it outside a comment.
    """

    def test_a_validated_spec_has_no_such_attribute(self):
        spec = validate_strategy_spec(FABER_2007_SPEC)
        assert not hasattr(spec, "look_ahead_safe")
        assert "look_ahead_safe" not in spec.to_dict()

    def test_the_audit_never_reads_a_declaration(self):
        """The module must not look the name up, defensively or otherwise.

        A ``getattr(spec, "look_ahead_safe", None)`` would be harmless today and
        would quietly become a live read the moment anyone re-added the field.
        Checked over the AST, not the text, so the docstring's account of the
        history does not count as a read.
        """
        tree = ast.parse(inspect.getsource(la))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "look_ahead_safe", "the audit reads the retired declaration"
            if isinstance(node, ast.Constant) and node.value == "look_ahead_safe":
                raise AssertionError("the audit names the retired declaration in executable code")

    def test_a_leaking_spec_still_FAILS_without_any_declaration(self):
        """Losing the flag must not lose the audit's teeth."""
        leaking = TestOutOfSurfaceSpecsFail._spec(
            entry={"gt": ["close", "oracle_5"]},
            exit={"lt": ["close", "oracle_5"]},
            indicators=["oracle_5"],
        )
        audit = audit_dsl_strategy(leaking, broker_cheat_check=True)
        assert audit.status == FAIL

    def test_the_audit_carries_no_declared_field_at_all(self):
        audit = audit_dsl_strategy(validate_strategy_spec(FABER_2007_SPEC), broker_cheat_check=True)
        assert not hasattr(audit, "declared_intent")
        assert "declared" not in audit.to_dict()
        assert not any("declared" in k for k in audit.to_dict())


def _stub_metrics_for(spec):
    from archimedes.services.fusion_evaluator import BacktestMetrics

    return BacktestMetrics(
        sharpe_ratio=1.0,
        sortino_ratio=1.0,
        max_drawdown=0.1,
        cagr=0.1,
        calmar_ratio=1.0,
        win_rate=0.5,
        total_trades=10,
        avg_holding_period_days=5.0,
        equity_curve=[100_000.0 * (1.001**i) for i in range(300)],
        monthly_returns=[],
        backtest_start=None,
        backtest_end=None,
        data_source="csv:test.csv",
        broker_cheat_check_passed=True,
    )


# ── 5. Threading: verdict → passport blob → API ───────────────────────


class TestTheTwoGatesAgree:
    """One strategy must not pass one gate and fail the other.

    Two gates read the look-ahead leg for the same DSL strategy:
    ``fusion_evaluator.apply_rigor_gate`` (folds it into ``RigorVerdict.passing``)
    and ``live_rigor_gate.verdict_from_returns`` (folds it into the badge's
    always-on floor, via the boolean ``generation_pipeline`` threads through).
    They agree only because both read ``DslLookAheadAudit.passed`` and nothing
    else. If either one grew its own rule — "pending does not veto here", say —
    the same strategy would be admitted by one surface and blocked by the other.
    """

    @staticmethod
    def _both_gates(spec, metrics, variants):
        from archimedes.services.live_rigor_gate import verdict_from_returns

        fusion = apply_rigor_gate(metrics, variants_metrics=variants, spec=spec)
        returns = [
            (metrics.equity_curve[i] - metrics.equity_curve[i - 1]) / metrics.equity_curve[i - 1]
            for i in range(1, len(metrics.equity_curve))
        ]
        badge = verdict_from_returns(
            "agree",
            returns,
            num_trials=1,
            # Exactly what generation_pipeline._persist_real_returns passes.
            look_ahead_audit_passed=fusion.look_ahead_clean,
            look_ahead_status=fusion.look_ahead_status,
        )
        return fusion, badge

    @pytest.mark.parametrize("has_spec", [True, False])
    def test_neither_gate_admits_what_the_other_blocks(self, has_spec):
        from tests.services.test_fusion_evaluator import _make_high_sharpe_metrics, _two_variant_set

        metrics = _make_high_sharpe_metrics(data_source="csv:spy.csv")
        variants = _two_variant_set(metrics.equity_curve, data_source="csv:spy.csv")
        spec = validate_strategy_spec(FABER_2007_SPEC) if has_spec else None

        fusion, badge = self._both_gates(spec, metrics, variants)

        # The look-ahead leg is the same fact on both sides.
        assert fusion.look_ahead_clean is (fusion.look_ahead_status == PASS)
        assert badge.blocked_by_floor is not fusion.look_ahead_clean
        if has_spec:
            assert fusion.passing is True and badge.blocked_by_floor is False
        else:
            # Nothing audited: BOTH gates refuse. Before this change the fusion
            # side hardcoded `look_ahead_clean = True` and would have passed here
            # while the badge floor blocked — the disagreement state.
            assert fusion.look_ahead_status == PENDING
            assert fusion.passing is False
            assert badge.blocked_by_floor is True

    def test_a_failing_audit_blocks_on_both_sides(self):
        from tests.services.test_fusion_evaluator import _make_high_sharpe_metrics, _two_variant_set

        metrics = _make_high_sharpe_metrics(data_source="csv:spy.csv")
        leaking = TestOutOfSurfaceSpecsFail._spec(
            entry={"gt": ["close", "oracle_5"]},
            exit={"lt": ["close", "oracle_5"]},
            indicators=["oracle_5"],
        )
        fusion, badge = self._both_gates(leaking, metrics, _two_variant_set(metrics.equity_curve, "csv:spy.csv"))
        assert fusion.look_ahead_status == FAIL
        assert fusion.passing is False
        assert badge.blocked_by_floor is True


class TestVerdictThreadsThroughToThePassport:
    def test_evaluate_fusion_spec_produces_a_structural_verdict(self):
        result = evaluate_fusion_spec(FABER_2007_SPEC)
        assert result.success, result.error
        assert result.rigor.look_ahead_status == PASS
        assert result.rigor.look_ahead_reason == ""
        assert "self-declared" not in result.rigor.look_ahead_label
        assert "self-attested" not in result.rigor.look_ahead_label

    def test_rigor_verdict_dict_carries_the_four_state_field(self):
        from archimedes.agents.debate_engine import _rigor_verdict_dict

        blob = _rigor_verdict_dict(evaluate_fusion_spec(FABER_2007_SPEC))
        assert blob["look_ahead_status"] == PASS
        assert blob["lookahead_audit_passed"] is True
        assert not any("declared" in k for k in blob), "nothing declared survives into the passport blob"

    def test_audit_source_label_is_derived_not_hardcoded(self):
        """``look_ahead_audit_source`` used to be a flat ``"self_attested"``.

        The axis is "did an audit conclude", not "did it pass" — a ``fail`` has
        the audit as its provenance just as much as a ``pass`` does.
        """
        from archimedes.agents.generation_pipeline import _look_ahead_audit_source

        assert _look_ahead_audit_source({"look_ahead_status": PASS}) == "dsl_structural_audit"
        assert _look_ahead_audit_source({"look_ahead_status": FAIL}) == "dsl_structural_audit"
        assert _look_ahead_audit_source({"look_ahead_status": PENDING}) == "dsl_audit_not_run"
        assert _look_ahead_audit_source({"look_ahead_status": DEGENERATE}) == "dsl_audit_not_run"
        # A verdict blob written before this landed.
        assert _look_ahead_audit_source({}) == "dsl_audit_not_run"

    def test_self_attested_is_never_written_again(self):
        """The retired provenance value names a field that no longer exists."""
        from archimedes.agents.generation_pipeline import _look_ahead_audit_source

        for status in (*sorted(la.AUDIT_STATUSES), None, "nonsense"):
            blob = {} if status is None else {"look_ahead_status": status}
            assert _look_ahead_audit_source(blob) != "self_attested", status

    def test_to_dict_is_json_shaped(self):
        audit = audit_dsl_strategy(validate_strategy_spec(FABER_2007_SPEC), broker_cheat_check=True)
        d = audit.to_dict()
        assert d["status"] == PASS
        assert d["reasons"] == []
        assert d["blocks_deploy"] is False
        assert d["surface_version"] == la.VERIFIED_SURFACE_VERSION
        assert isinstance(d["label"], str)
