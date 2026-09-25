"""One decision core, two venues — issue #1410's named acceptance file.

The vault feature's most-MVP piece (signal FSM → aggregated target weights →
diff against current positions → executed trades) had zero vaults to run
against and therefore zero validation. #1410 points that same core at a paper
deployment. What has to be true for that to mean anything:

  G1  the two venues produce IDENTICAL target weights from identical signals
      + the copy-of-the-logic divergence that made this issue necessary
  G2  the two venues produce IDENTICAL trades from an identical portfolio
      + a second drift threshold, which is what a forked copy would drift into
  G3  a paper deployment advanced N ticks shows agent-attributed trades with
      tick provenance
      + a trade with no tick, which the schema must refuse
  G4  re-applying a tick is a no-op
      + the duplicate the unique constraint must reject when the skip is bypassed
  G5  exactly ONE definition of the position-FSM entry symbol
      + a planted second copy, which the same grep must catch
  G6  the paper book is an INDEX and never a dollar figure
  G7  one broken deployment does not stall the others
  G8  a spec that does not validate produces NO decision
      + the confident all-cash liquidation it would produce without the gate

Hermetic: tmp-file SQLite, the chain executor mocked at the chain-client
boundary, the signal evaluator stubbed. No Redis, no Postgres, no web3, no
network, no ``.env``.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from archimedes.execution import core as execution_core
from archimedes.execution.paper_venue import PAPER_INDEX_BASE, PaperVenue
from archimedes.execution.venue import ChainVenue, ExecutionVenue

# Imported for the side effect of registering the paper tables on
# Base.metadata before `redirect_to_tmp_sqlite` runs create_all — without it
# whether `paper_agent_trades` exists depends on which test file imported the
# module first (the import-order roulette db_isolation exists to remove).
from archimedes.models.paper_store import PaperAgentTrade, PaperDeployment
from archimedes.models.portfolio import Portfolio, PortfolioHolding, TradeDirection, TradeOrder
from archimedes.services.portfolio_constructor import PortfolioConstructor
from archimedes.services.strategy_signal_evaluator import (
    AssetSignal,
    Signal,
    StrategySignals,
)

from tests.db_isolation import redirect_to_tmp_sqlite

pytestmark = pytest.mark.anyio

STRATEGY_ID = "1410aabbccddeeff"
DEPLOYMENT_ID = "dep1410"
DEPLOY = date(2026, 8, 20)

#: The vault's real address map, pinned here rather than read from settings so
#: the parity claim does not depend on a developer's ``.env``.
USDC_ADDRESS = "0x3600000000000000000000000000000000000000"
SYNTH_ADDRESSES = {
    "sSPY": "0x1111111111111111111111111111111111111111",
    "sQQQ": "0x2222222222222222222222222222222222222222",
}

_SPEC = {
    "name": "venue parity probe",
    "asset_universe": ["SPY", "QQQ"],
    "rebalance_frequency": "daily",
    "entry": {"gt": ["close", "sma_200"]},
    "exit": {"lt": ["close", "sma_200"]},
    "position_sizing": {"type": "full_invested_when_in_market"},
    "source_arxiv_ids": ["0706.1497"],
    "indicators": ["sma_200"],
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture
def session_factory():
    """``get_session`` resolved AFTER the tmp-DB redirect, never before.

    ``redirect_to_tmp_sqlite`` swaps ``archimedes.db.SessionLocal``; a factory
    captured at import time would hand out sessions on the real engine.
    """
    from archimedes.db import get_session

    return get_session


@pytest.fixture(autouse=True)
def _pin_env(monkeypatch):
    """Pin the two knobs the pass reads so a developer's env cannot move them."""
    monkeypatch.setenv("PAPER_AGENT_EXECUTION", "1")
    monkeypatch.setenv("AGENT_USDC_FLOOR", "0.20")


def _signals(spy: float, qqq: float) -> list[StrategySignals]:
    """One strategy voting ``spy``/``qqq`` — the fixture both venues eat."""

    def sig(asset: str, weight: float) -> AssetSignal:
        return AssetSignal(
            strategy_id=STRATEGY_ID,
            strategy_name="venue parity probe",
            asset=asset,
            signal=Signal.LONG if weight > 0 else Signal.FLAT,
            weight=weight,
            reason=f"probe: {asset} weight {weight}",
        )

    return [
        StrategySignals(
            strategy_id=STRATEGY_ID,
            strategy_name="venue parity probe",
            paper_title="venue parity probe",
            signals=[sig("sSPY", spy), sig("sQQQ", qqq)],
        )
    ]


def _chain_venue() -> ChainVenue:
    """The vault venue, mocked at the chain-client boundary.

    ``executor`` is an AsyncMock so nothing here can reach web3, and the address
    maps are passed explicitly so nothing here reads ``chain_client.settings``.
    """
    return ChainVenue(
        executor=AsyncMock(),
        usdc_address=USDC_ADDRESS,
        synth_addresses=dict(SYNTH_ADDRESSES),
    )


def _targets(venue, signals, *, usdc_floor: float = 0.20):
    _weights, targets = execution_core.targets_from_signals(
        signals,
        venue=venue,
        constructor=PortfolioConstructor(),
        usdc_floor=usdc_floor,
        regime=None,
        ensemble_consensus=None,
    )
    return targets


def _weight_map(targets) -> dict[str, float]:
    return {t.symbol: t.weight for t in targets}


# ═══ G1 — identical signals → identical target weights ═══════════════


class TestTargetWeightParity:
    async def test_both_venues_agree_on_every_weight(self, session_factory) -> None:
        """The #1410 acceptance criterion, stated directly."""
        signals = _signals(spy=1.0, qqq=0.5)
        with session_factory() as session:
            paper = PaperVenue(session, spec_hash="deadbeef")
            chain_targets = _targets(_chain_venue(), signals)
            paper_targets = _targets(paper, signals)

        assert _weight_map(chain_targets) == _weight_map(paper_targets)
        # Non-trivial: a fixture where every weight happened to be 0 would pass
        # the equality above while proving nothing.
        assert any(w > 0 for w in _weight_map(chain_targets).values())
        assert set(_weight_map(chain_targets)) == {"USDC", "sSPY", "sQQQ"}

    async def test_only_the_address_differs(self, session_factory) -> None:
        """Venues may disagree about instrument identity — never about size."""
        signals = _signals(spy=1.0, qqq=0.5)
        with session_factory() as session:
            paper = {t.symbol: t for t in _targets(PaperVenue(session, "deadbeef"), signals)}
        chain = {t.symbol: t for t in _targets(_chain_venue(), signals)}

        assert chain["sSPY"].token_address == SYNTH_ADDRESSES["sSPY"]
        assert paper["sSPY"].token_address == "paper:sSPY"
        assert chain["sSPY"].token_address != paper["sSPY"].token_address
        assert chain["sSPY"].weight == paper["sSPY"].weight

    async def test_the_live_runner_method_agrees_with_the_paper_venue(self, session_factory) -> None:
        """Pins the ACTUAL vault-path method, not just the shared function.

        ``targets_from_signals`` is the composed convenience the paper pass
        calls; ``StrategyRunner`` still composes the same shared steps inline at
        its two call sites (migrating it would mean rewriting
        ``test_agent_runner_tick``'s module-level ``strategy_evaluator`` patch,
        i.e. editing tests to fit a refactor). So the parity above could in
        principle hold while the runner's own method drifted.

        This closes that: it invokes ``StrategyRunner._weights_to_targets`` and
        ``._compute_trades`` — the real, live methods — against the same
        allocations, and compares to the paper venue. Bound to a stand-in
        carrying only ``venue`` because that is genuinely all those two methods
        read from ``self``; constructing a real runner would drag in the
        strategy provider and a DB for no additional coverage.
        """
        from types import SimpleNamespace

        from archimedes.chain.agent_runner import StrategyRunner

        signals = _signals(spy=1.0, qqq=0.5)
        weights = {t.symbol: t.weight for t in _targets(_chain_venue(), signals)}

        runner_like = SimpleNamespace(venue=_chain_venue())
        runner_targets = StrategyRunner._weights_to_targets(runner_like, weights, signals)

        with session_factory() as session:
            paper = PaperVenue(session, "deadbeef")
            paper_targets = execution_core.weights_to_targets(weights, signals, paper)

        assert _weight_map(runner_targets) == _weight_map(paper_targets)
        assert any(w > 0 for w in _weight_map(runner_targets).values())

        # …and the same for the trade diff, from an identical all-cash book.
        book = Portfolio(
            vault_address="0xvault",
            holdings=[PortfolioHolding(symbol="USDC", token_address="USDC", amount=1.0, value_usdc=1.0, weight=1.0)],
            total_value_usdc=1.0,
        )
        runner_trades = StrategyRunner._compute_trades(runner_like, book, runner_targets)
        paper_trades = execution_core.compute_trades(book, paper_targets)
        assert sorted((t.symbol, t.direction, round(t.amount, 9)) for t in runner_trades) == sorted(
            (t.symbol, t.direction, round(t.amount, 9)) for t in paper_trades
        )
        assert runner_trades, "an empty trade list would make this vacuous"

    async def test_a_forked_copy_of_the_core_breaks_parity(self, session_factory) -> None:
        """ADVERSARIAL — the input that SHOULD fail G1, and does.

        This is not a hypothetical divergence. ``marketplace/service.py``
        carries a hand-ported copy of the trade-computation logic that has
        already drifted from the runner's (it is missing the #1080
        unpriced-holding guard). A venue that likewise kept its own copy of the
        weight pipeline would differ in exactly this shape: same signals, a
        different cash floor, and target weights that silently disagree.
        """
        signals = _signals(spy=1.0, qqq=0.5)
        chain_weights = _weight_map(_targets(_chain_venue(), signals))
        with session_factory() as session:
            # A paper venue whose "copy" of the pipeline drifted to a 0.30 floor.
            forked = _weight_map(_targets(PaperVenue(session, "deadbeef"), signals, usdc_floor=0.30))

        assert forked != chain_weights, "the forked floor must NOT agree — the guard would be vacuous"
        # More cash, less risk asset, on the same signals. Exactly the kind of
        # silent disagreement a per-venue copy of the pipeline produces.
        assert forked["USDC"] > chain_weights["USDC"]
        assert forked["sSPY"] < chain_weights["sSPY"]


# ═══ G2 — identical portfolio → identical trades ═════════════════════


class TestTradeParity:
    def _portfolio(self, address_of) -> Portfolio:
        """All-cash book, expressed in whichever venue's identifiers."""
        return Portfolio(
            vault_address="0xvault",
            holdings=[
                PortfolioHolding(
                    symbol="USDC",
                    token_address=address_of("USDC"),
                    amount=1.0,
                    value_usdc=1.0,
                    weight=1.0,
                )
            ],
            total_value_usdc=1.0,
        )

    async def test_same_trades_from_both_venues(self, session_factory) -> None:
        signals = _signals(spy=1.0, qqq=1.0)
        chain_venue = _chain_venue()
        with session_factory() as session:
            paper_venue = PaperVenue(session, "deadbeef")
            paper_trades = execution_core.compute_trades(
                self._portfolio(paper_venue.token_address), _targets(paper_venue, signals)
            )
        chain_trades = execution_core.compute_trades(
            self._portfolio(chain_venue.token_address), _targets(chain_venue, signals)
        )

        def shape(trades):
            return sorted((t.symbol, t.direction, round(t.amount, 9)) for t in trades)

        assert shape(chain_trades) == shape(paper_trades)
        assert shape(chain_trades), "an empty trade list would make this vacuous"

    async def test_a_second_drift_threshold_breaks_parity(self, session_factory) -> None:
        """ADVERSARIAL — the input that SHOULD fail G2, and does.

        A venue that kept its own drift constant would trade where the other
        stood still. Feeding a book already within the shared 15% of target,
        the shared threshold produces nothing and a forked 0.01 threshold
        produces trades.
        """
        chain_venue = _chain_venue()
        targets = _targets(chain_venue, _signals(spy=1.0, qqq=1.0))
        weights = _weight_map(targets)
        # Within the shared threshold of target on every leg.
        near = Portfolio(
            vault_address="0xvault",
            holdings=[
                PortfolioHolding(
                    symbol=sym,
                    token_address=chain_venue.token_address(sym),
                    amount=w,
                    value_usdc=w,
                    weight=w + 0.05,
                )
                for sym, w in weights.items()
            ],
            total_value_usdc=1.0,
        )
        assert execution_core.compute_trades(near, targets) == []
        forked = execution_core.compute_trades(near, targets, drift_threshold=0.01)
        assert forked, "the forked threshold must trade — otherwise the guard proves nothing"


# ═══ G3 — N ticks of agent-attributed paper trades ═══════════════════


def _deploy(session) -> PaperDeployment:
    dep = PaperDeployment(
        id=DEPLOYMENT_ID,
        strategy_id=STRATEGY_ID,
        owner_wallet="0x9999999999999999999999999999999999999999",
        owner_user_id="user-1410",
        spec_json=json.dumps(_SPEC, sort_keys=True),
        deployed_at=DEPLOY,
        status="active",
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    session.add(dep)
    session.flush()
    return dep


def _stub_evaluator(monkeypatch, script):
    """Replace the network-backed evaluator with a scripted signal sequence.

    Patched on the singleton the pass actually calls, so the real
    ``evaluate_strategies`` (and therefore yfinance) is never reached.
    """
    from archimedes.services import strategy_signal_evaluator as sse

    calls = {"n": 0}

    def fake(strategies, synths, *args, **kwargs):
        i = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        return script[i]

    monkeypatch.setattr(sse.strategy_evaluator, "evaluate_strategies", fake)
    return calls


class TestPaperTickProvenance:
    async def test_n_ticks_produce_attributed_trades(self, session_factory, monkeypatch) -> None:
        """Three ticks: enter, hold, rotate. Every trade names its tick+signal."""
        from archimedes.services.paper_agent_execution import advance_agent_execution, spec_hash

        _stub_evaluator(
            monkeypatch,
            [
                _signals(spy=1.0, qqq=0.0),  # tick 1: enter SPY
                _signals(spy=1.0, qqq=0.0),  # tick 2: unchanged → aligned
                _signals(spy=0.0, qqq=1.0),  # tick 3: rotate into QQQ
            ],
        )

        summaries = []
        with session_factory() as session:
            _deploy(session)
            for _ in range(3):
                summaries.append(await advance_agent_execution(session))
            session.commit()

        with session_factory() as session:
            rows = (
                session.query(PaperAgentTrade)
                .filter(PaperAgentTrade.deployment_id == DEPLOYMENT_ID)
                .order_by(PaperAgentTrade.decided_at.asc(), PaperAgentTrade.id.asc())
                .all()
            )

            assert rows, "three ticks must leave a trade record"
            # Tick 2 was aligned, so the ticks that traded are two, not three.
            ticks = {r.tick_id for r in rows}
            assert len(ticks) == 2, f"expected 2 trading ticks, got {len(ticks)}"

            expected_hash = spec_hash(json.loads(json.dumps(_SPEC, sort_keys=True)))
            for r in rows:
                assert r.tick_id, "a trade with no tick is the thing #1410 forbids"
                assert r.decided_at is not None
                assert r.spec_hash == expected_hash
                assert r.direction in ("buy", "sell")
                # Every leg the STRATEGY voted on carries its attribution; the
                # USDC residual carries None, honestly, rather than being
                # attributed to whichever strategy sorted first.
                if r.symbol == "USDC":
                    assert r.signal_strategy_id is None
                else:
                    assert r.signal_strategy_id == STRATEGY_ID
                    assert r.signal_state in ("long", "flat", "scaled")
                    assert r.signal_reason

            # The rotation actually happened: SPY was bought, then sold.
            spy = [r for r in rows if r.symbol == "sSPY"]
            assert [r.direction for r in spy] == ["buy", "sell"]
            assert spy[0].prior_weight == pytest.approx(0.0)
            assert spy[0].target_weight > 0
            assert spy[1].target_weight == pytest.approx(0.0)

        assert [s["ticked"] for s in summaries] == [1, 1, 1]
        assert summaries[1]["trades"] == 0, "the unchanged tick must trade nothing"

    async def test_a_trade_with_no_tick_is_refused(self, session_factory) -> None:
        """ADVERSARIAL — the input that SHOULD fail G3, and does.

        Provenance that is merely a convention is provenance that will be
        skipped. ``tick_id`` is NOT NULL, so a row that cannot name its tick
        cannot be written at all.
        """
        from sqlalchemy.exc import IntegrityError

        with session_factory() as session:
            _deploy(session)
            session.add(
                PaperAgentTrade(
                    deployment_id=DEPLOYMENT_ID,
                    tick_id=None,
                    decided_at=datetime.now(UTC),
                    symbol="sSPY",
                    direction="buy",
                    prior_weight=0.0,
                    target_weight=0.8,
                    weight_delta=0.8,
                    spec_hash="deadbeef",
                )
            )
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()


# ═══ G4 — re-applying a tick is a no-op ══════════════════════════════


class TestIdempotentReapply:
    async def test_same_tick_twice_writes_once(self, session_factory) -> None:
        signals = _signals(spy=1.0, qqq=0.0)
        with session_factory() as session:
            _deploy(session)
            venue = PaperVenue(session, "deadbeef")
            targets = _targets(venue, signals)
            portfolio = await venue.read_portfolio(DEPLOYMENT_ID)
            trades = execution_core.compute_trades(portfolio, targets)
            decision = execution_core.TickDecision(
                tick_id="tick0001",
                portfolio=portfolio,
                targets=targets,
                trades=trades,
                signals=signals,
            )
            first = await venue.execute_trades(DEPLOYMENT_ID, decision)
            second = await venue.execute_trades(DEPLOYMENT_ID, decision)
            session.commit()

            assert first, "the first apply must write something"
            assert second == [], "the second apply must write nothing"
            assert session.query(PaperAgentTrade).count() == len(first)

    async def test_a_duplicate_row_is_rejected_by_the_constraint(self, session_factory) -> None:
        """ADVERSARIAL — the input that SHOULD fail G4, and does.

        The skip in ``execute_trades`` is the cheap path; the unique constraint
        is what makes the guarantee real. Written straight past the venue, a
        second (deployment, tick, symbol) row is refused by the database.
        """
        from sqlalchemy.exc import IntegrityError

        def row():
            return PaperAgentTrade(
                deployment_id=DEPLOYMENT_ID,
                tick_id="tick0001",
                decided_at=datetime.now(UTC),
                symbol="sSPY",
                direction="buy",
                prior_weight=0.0,
                target_weight=0.8,
                weight_delta=0.8,
                spec_hash="deadbeef",
            )

        with session_factory() as session:
            _deploy(session)
            session.add(row())
            session.flush()
            session.add(row())
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()


# ═══ G5 — one definition of the position-FSM entry symbol ════════════


#: The unified F2/F3 replay-derived position FSM (#1320). #1410 requires the
#: paper path and the vault path to share it rather than each carry a copy.
FSM_ENTRY_SYMBOL = "_replay_position_state"


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fsm_definitions(root: Path) -> list[str]:
    """Every ``def <FSM entry>`` under ``root``, via grep -rn as the issue asks."""
    proc = subprocess.run(
        ["grep", "-rn", "--include=*.py", f"def {FSM_ENTRY_SYMBOL}", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in proc.stdout.splitlines() if re.search(rf"def\s+{FSM_ENTRY_SYMBOL}\s*\(", line)]


class TestSingleFsmDefinition:
    def test_exactly_one_definition_exists(self) -> None:
        found = _fsm_definitions(_backend_root())
        assert len(found) == 1, f"expected one FSM definition, found {len(found)}:\n" + "\n".join(found)
        assert "strategy_signal_evaluator.py" in found[0]

    def test_a_planted_second_copy_is_caught(self, tmp_path: Path) -> None:
        """ADVERSARIAL — the input that SHOULD fail G5, and does.

        A guard that has never rejected anything is a guard nobody has tested.
        Two files each defining the symbol, and the same grep the real check
        runs, must report two.
        """
        (tmp_path / "real.py").write_text(f"def {FSM_ENTRY_SYMBOL}(spec, prices, max_period):\n    return None\n")
        (tmp_path / "copy.py").write_text(f"def {FSM_ENTRY_SYMBOL}(spec, prices, max_period):\n    return None\n")
        assert len(_fsm_definitions(tmp_path)) == 2

    def test_the_paper_pass_calls_the_shared_evaluator(self) -> None:
        """The FSM is reached by CALLING the shared evaluator, not by re-implementing it.

        One definition is necessary but not sufficient: the paper path could
        still have grown its own signal interpretation that never reaches the
        FSM at all. This pins that the pass's only route to a signal is
        ``strategy_evaluator.evaluate_strategies``, which is what dispatches
        into ``_spec_signal`` → the FSM.
        """
        src = (_backend_root() / "archimedes" / "services" / "paper_agent_execution.py").read_text()
        assert "strategy_evaluator.evaluate_strategies" in src
        # Definitions, not mentions — the module's docstring names both symbols
        # on purpose, to say which shared code it reaches.
        assert f"def {FSM_ENTRY_SYMBOL}" not in src
        assert "def _spec_signal" not in src


# ═══ G6 — the paper book is an index, never dollars ══════════════════


class TestPaperBookIsAnIndex:
    async def test_total_is_the_index_base_not_a_notional(self, session_factory) -> None:
        """``PaperDeployment`` has no capital column; the book must not invent one."""
        signals = _signals(spy=1.0, qqq=0.0)
        with session_factory() as session:
            _deploy(session)
            venue = PaperVenue(session, "deadbeef")
            empty = await venue.read_portfolio(DEPLOYMENT_ID)
            assert empty.total_value_usdc == PAPER_INDEX_BASE == 1.0
            assert empty.weights_dict == {"USDC": 1.0}

            targets = _targets(venue, signals)
            trades = execution_core.compute_trades(empty, targets)
            await venue.execute_trades(
                DEPLOYMENT_ID,
                execution_core.TickDecision(
                    tick_id="tick0001",
                    portfolio=empty,
                    targets=targets,
                    trades=trades,
                    signals=signals,
                ),
            )
            session.commit()

            after = await venue.read_portfolio(DEPLOYMENT_ID)
            assert after.total_value_usdc == PAPER_INDEX_BASE
            # Every trade size is a portfolio FRACTION, so nothing can exceed 1.
            assert all(t.amount <= 1.0 for t in trades)
            # The book now holds what the tick targeted.
            for symbol, weight in after.weights_dict.items():
                assert weight == pytest.approx(_weight_map(targets)[symbol])

    async def test_a_second_tick_sees_the_first_ticks_positions(self, session_factory) -> None:
        """The fold is the position: tick 2 diffs against tick 1's result."""
        signals = _signals(spy=1.0, qqq=0.0)
        with session_factory() as session:
            _deploy(session)
            venue = PaperVenue(session, "deadbeef")
            targets = _targets(venue, signals)
            first = await venue.read_portfolio(DEPLOYMENT_ID)
            trades = execution_core.compute_trades(first, targets)
            await venue.execute_trades(
                DEPLOYMENT_ID,
                execution_core.TickDecision(
                    tick_id="tick0001", portfolio=first, targets=targets, trades=trades, signals=signals
                ),
            )
            session.commit()

            second = await venue.read_portfolio(DEPLOYMENT_ID)
            assert second.weights_dict != first.weights_dict
            # Same signals again → already at target → nothing to do.
            assert execution_core.compute_trades(second, targets) == []


# ═══ G7 — isolation and the protocol ═════════════════════════════════


class TestPassIsolation:
    async def test_one_broken_deployment_does_not_stall_the_others(self, session_factory, monkeypatch) -> None:
        from archimedes.services.paper_agent_execution import advance_agent_execution

        _stub_evaluator(monkeypatch, [_signals(spy=1.0, qqq=0.0)])

        with session_factory() as session:
            _deploy(session)
            session.add(
                PaperDeployment(
                    id="dep1410bad",
                    strategy_id="badbadbadbadbad0",
                    owner_wallet="0x8888888888888888888888888888888888888888",
                    owner_user_id="user-1410-bad",
                    spec_json="{not json",
                    deployed_at=DEPLOY,
                    status="active",
                )
            )
            # SessionLocal runs with autoflush=False, so an unflushed row is
            # invisible to the pass's own query — flush or the "broken"
            # deployment silently would not exist.
            session.flush()
            summary = await advance_agent_execution(session)
            session.commit()

        assert summary["failed"] == 1
        assert summary["ticked"] == 1, "the healthy deployment must still have ticked"
        assert summary["trades"] > 0

    async def test_the_kill_switch_stops_the_pass(self, session_factory, monkeypatch) -> None:
        from archimedes.services.paper_agent_execution import advance_agent_execution

        monkeypatch.setenv("PAPER_AGENT_EXECUTION", "0")
        _stub_evaluator(monkeypatch, [_signals(spy=1.0, qqq=0.0)])
        with session_factory() as session:
            _deploy(session)
            summary = await advance_agent_execution(session)
            session.commit()
            assert summary == {
                "enabled": False,
                "deployments": 0,
                "ticked": 0,
                "skipped": 0,
                "failed": 0,
                "trades": 0,
            }
            assert session.query(PaperAgentTrade).count() == 0

    async def test_both_venues_satisfy_the_protocol(self, session_factory) -> None:
        with session_factory() as session:
            assert isinstance(PaperVenue(session, "deadbeef"), ExecutionVenue)
        assert isinstance(_chain_venue(), ExecutionVenue)

    async def test_chain_venue_forwards_without_reinterpreting(self) -> None:
        """Anti-goal 1: the chain executor's live behavior is untouched."""
        executor = AsyncMock()
        executor.execute_trades.return_value = ["0xtx"]
        venue = ChainVenue(executor=executor, usdc_address=USDC_ADDRESS, synth_addresses=dict(SYNTH_ADDRESSES))
        trade = TradeOrder(
            symbol="sSPY",
            token_address=SYNTH_ADDRESSES["sSPY"],
            direction=TradeDirection.BUY,
            amount=0.5,
            estimated_usdc_value=0.5,
        )
        decision = execution_core.TickDecision(
            tick_id="tick0001",
            portfolio=Portfolio(vault_address="0xvault", total_value_usdc=1.0),
            targets=[],
            trades=[trade],
        )
        assert await venue.execute_trades("0xvault", decision) == ["0xtx"]
        # The trade list reaches the executor unaltered — no re-sizing, no
        # re-ordering, no extra arguments the live path did not have before.
        executor.execute_trades.assert_awaited_once_with("0xvault", [trade])


# ═══ G8 — a broken spec produces NO decision, never a confident one ══


_BROKEN_SPEC = dict(_SPEC, entry={"gt": ["close", "not_an_indicator"]})


class TestBrokenSpecGate:
    async def test_an_invalid_spec_is_failed_and_trades_nothing(self, session_factory, monkeypatch) -> None:
        from archimedes.services.paper_agent_execution import advance_agent_execution

        _stub_evaluator(monkeypatch, [_signals(spy=1.0, qqq=0.0)])

        with session_factory() as session:
            _deploy(session)
            session.add(
                PaperDeployment(
                    id="dep1410broken",
                    strategy_id="brokenbrokenbro0",
                    owner_wallet="0x7777777777777777777777777777777777777777",
                    owner_user_id="user-1410-broken",
                    spec_json=json.dumps(_BROKEN_SPEC, sort_keys=True),
                    deployed_at=DEPLOY,
                    status="active",
                )
            )
            session.flush()
            summary = await advance_agent_execution(session)
            session.commit()

            assert summary["failed"] == 1
            assert summary["ticked"] == 1, "the healthy deployment must still tick"
            assert (
                session.query(PaperAgentTrade).filter(PaperAgentTrade.deployment_id == "dep1410broken").count() == 0
            ), "a spec that does not validate must produce no trade at all"

    def test_without_the_gate_a_broken_spec_would_liquidate_the_book(self) -> None:
        """ADVERSARIAL — the input that SHOULD fail G8, and what it would do.

        ``_spec_signal`` NEVER RAISES on a broken spec: it logs and returns
        FLAT. So an unvalidated bad spec does not arrive as an error — it
        arrives as a full set of legitimate-looking flat signals, aggregates to
        cash, and has the agent sell the whole book. That is why
        ``_tick_deployment`` validates BEFORE evaluating, rather than relying on
        the evaluator to complain.

        Hermetic: price history is injected, so the evaluator never fetches.
        """
        import numpy as np
        import pandas as pd
        from archimedes.models.paper_ref import PaperRef
        from archimedes.models.strategy import StrategyPassport
        from archimedes.services.strategy_signal_evaluator import strategy_evaluator

        idx = pd.date_range("2024-01-01", periods=400, freq="D")
        prices = pd.Series(100.0 + np.arange(400) * 0.1, index=idx)

        broken = StrategyPassport(
            id=STRATEGY_ID,
            papers=[PaperRef(title="broken probe")],
            asset_universe=["SPY"],
            strategy_spec=_BROKEN_SPEC,
        )
        signals = strategy_evaluator.evaluate_strategies([broken], ["sSPY"], price_histories={"sSPY": prices})

        # It produced signals — confidently, with no error anywhere.
        assert signals, "the evaluator does not refuse a broken spec; that is the hazard"
        assert all(s.signal is Signal.FLAT and s.weight == 0.0 for ss in signals for s in ss.signals)

        # And those flat signals aggregate into an instruction to sell.
        venue = _chain_venue()
        targets = _targets(venue, signals)
        invested = Portfolio(
            vault_address="0xvault",
            holdings=[
                PortfolioHolding(
                    symbol="sSPY",
                    token_address=venue.token_address("sSPY"),
                    amount=0.8,
                    value_usdc=0.8,
                    weight=0.8,
                ),
                PortfolioHolding(
                    symbol="USDC",
                    token_address=venue.token_address("USDC"),
                    amount=0.2,
                    value_usdc=0.2,
                    weight=0.2,
                ),
            ],
            total_value_usdc=1.0,
        )
        trades = execution_core.compute_trades(invested, targets)
        sells = [t for t in trades if t.symbol == "sSPY" and t.direction is TradeDirection.SELL]
        assert sells, f"expected a liquidating SELL, got {trades}"
