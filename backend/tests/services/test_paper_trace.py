"""The paper trace BODY — issue #1575, build step 4.

``build_paper_trace`` stops at the hash and touches neither Redis nor the
chain (the same seam the retired ``construction_trace.py`` held), so
everything here is pure. What is pinned is the set of choices that make a
paper trace honest and reachable, each of which is a decision someone could
reasonably undo:

  * ``vault_address=""``, NOT the zero-address sentinel — the sentinel is
    world-readable while ``PUBLIC_TRACE_VAULTS`` is unarmed.
  * ``decision_type="rebalance"``, NOT ``"paper_rebalance"`` — a new value
    fails #1569's frozenset SILENTLY.
  * ``confidence == 0.0`` and ``consulted_paper_hashes == []`` — absences
    stated rather than filled with plausible values.
  * paper-ness and provenance are INSIDE the hash, so neither can be stripped.

Hermetic: no DB, no Redis, no network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from archimedes.models.trace import DecisionType
from archimedes.services.paper_trace import (
    PROVENANCE_BACKFILL,
    PROVENANCE_SETTLE,
    build_paper_trace,
    deployment_portfolio,
    resolve_paper_hashes,
)
from archimedes.services.strategy_dsl import FABER_2007_SPEC, validate_strategy_spec

_SPEC_DICT = dict(FABER_2007_SPEC)
_SPEC = validate_strategy_spec(_SPEC_DICT)
_DEPLOYMENT = "dep-1575"
_STRATEGY = "4f2b91c0aa3e1d55"
_DECIDED = date(2026, 7, 14)

_LEGS = [
    {
        "symbol": "SPY",
        "decided_on": _DECIDED,
        "filled_on": date(2026, 7, 15),
        "side": "buy",
        "size": 182.0,
        "price": 548.21,
        "value": 182.0 * 548.21,
        "commission": 99.77,
        "cash_after": 407.06,
        "cash_before": 407.06 + 182.0 * 548.21 + 99.77,
        "position_after": 182.0,
        "position_before": 0.0,
    }
]

#: The single-sleeve deployment's portfolio, produced the way production does
#: it — through :func:`deployment_portfolio` — so the fixture cannot drift from
#: the shape the builder is handed at settle time.
_SLEEVE_CASH = 100_000.0
_PORTFOLIO_BEFORE = deployment_portfolio(
    decision_date=_DECIDED, side="before", sleeve_legs={"SPY": _LEGS}, sleeve_initial_cash=_SLEEVE_CASH
)
_PORTFOLIO_AFTER = deployment_portfolio(
    decision_date=_DECIDED, side="after", sleeve_legs={"SPY": _LEGS}, sleeve_initial_cash=_SLEEVE_CASH
)


def _build(**overrides):
    kwargs = {
        "deployment_id": _DEPLOYMENT,
        "strategy_id": _STRATEGY,
        "spec": _SPEC,
        "spec_dict": _SPEC_DICT,
        "decision_date": _DECIDED,
        "legs": _LEGS,
        "portfolio_before": _PORTFOLIO_BEFORE,
        "portfolio_after": _PORTFOLIO_AFTER,
    }
    kwargs.update(overrides)
    return build_paper_trace(**kwargs)


# ── The field-by-field contract ─────────────────────────────────────────────


def test_decision_type_is_the_conforming_value_not_a_paper_specific_one():
    """#1569's matcher is a frozenset {rebalance, rotation, regime_change,
    skip} and ``list_traces``' ``decision_type`` regex is the same set. A
    ``"paper_rebalance"`` value would be rejected by BOTH — and the frozenset
    fails silently, so the passport would render "no traces for this strategy"
    while traces existed. Silent unreachability on the provenance surface is
    the worst available outcome, so the design conforms rather than extends."""
    assert _build().decision_type is DecisionType.REBALANCE


def test_the_vault_is_blank_never_the_zero_address_sentinel():
    """The zero-address sentinel would make an unstamped paper trace
    WORLD-READABLE: ``is_public_trace_vault`` returns True for any non-blank
    address while ``PUBLIC_TRACE_VAULTS`` is unarmed, which is the live state.
    A blank vault is fail-closed. This is pinned against the real predicate,
    not asserted.

    The sentinel used to be imported as ``construction_trace.UNBOUND_VAULT``.
    That module was a zero-caller dead surface and this PR deletes it, so the
    literal is inlined here. Nothing about the property changes: what is under
    test is ``is_public_trace_vault``'s behaviour on a non-blank address, and
    the zero address is simply the most tempting non-blank value to reach for.
    """
    from archimedes.services.trace_visibility import is_public_trace_vault

    UNBOUND_VAULT = "0x0000000000000000000000000000000000000000"

    trace = _build()
    assert trace.vault_address == ""
    assert trace.vault_address != UNBOUND_VAULT
    assert is_public_trace_vault(trace.vault_address) is False
    # The control that makes the line above mean something: the sentinel this
    # module deliberately does NOT use IS public with the allowlist unarmed.
    assert is_public_trace_vault(UNBOUND_VAULT) is True


def test_strategies_referenced_is_exactly_the_one_strategy_id():
    """#1569 compares WHOLE strings. One element, no prefixes, no composite
    anchors — the two non-conforming writers the constant documents (arXiv ids
    and paper anchors on construction traces) are the cautionary example."""
    assert _build().strategies_referenced == [_STRATEGY]


def test_confidence_is_zero_and_the_absence_is_stated():
    trace = _build()
    assert trace.confidence == 0.0
    assert "no calibrated source" in trace.expected_outcome
    assert "NOT anchored" in trace.expected_outcome


def test_consulted_paper_hashes_is_empty_when_nothing_resolves():
    """Emitting ``"2301.00001:"`` would be a half-formed value that reads as
    provenance. The bare ids stay visible in market_context instead."""
    trace = _build()
    assert trace.consulted_paper_hashes == []
    assert trace.market_context["source_arxiv_ids"] == sorted(_SPEC.source_arxiv_ids)
    assert trace.market_context["source_arxiv_ids"], "the fixture spec must actually cite something"


def test_timestamp_is_the_decision_bar_not_wall_clock():
    trace = _build()
    assert trace.timestamp == datetime(2026, 7, 14, tzinfo=UTC)
    assert trace.market_context["decided_on"] == "2026-07-14"
    assert trace.market_context["filled_on"] == ["2026-07-15"]


def test_portfolio_sides_bracket_the_legs():
    trace = _build()
    assert trace.portfolio_before["holdings"]["SPY"]["size"] == 0.0
    assert trace.portfolio_after["holdings"]["SPY"]["size"] == 182.0
    assert trace.portfolio_after["cash"] == pytest.approx(407.06)
    # `symbol`/`direction`/`amount` are the shape TradeExecutedResponse
    # requires; a leg keyed on "side" 500s GET /api/traces/, which makes the
    # trace unreachable exactly as a non-conforming decision_type would.
    from archimedes.api.schemas import TradeExecutedResponse

    assert trace.trades_executed == [
        {
            "symbol": "SPY",
            "direction": "buy",
            "amount": 182.0,
            "size": 182.0,
            "price": 548.21,
            "value": 99774.22,
            "commission": 99.77,
        }
    ]
    assert TradeExecutedResponse(**trace.trades_executed[0]).direction == "buy"


def test_a_decision_with_no_legs_is_rejected():
    with pytest.raises(ValueError, match="not a decision"):
        _build(legs=[])


def test_the_portfolio_snapshots_are_required_and_must_be_snapshot_shaped():
    """They are hashed fields and, on the anchoring path, ``reveal()`` writes
    them on-chain. A half-formed one must not get that far."""
    with pytest.raises(TypeError):
        build_paper_trace(
            deployment_id=_DEPLOYMENT,
            strategy_id=_STRATEGY,
            spec=_SPEC,
            spec_dict=_SPEC_DICT,
            decision_date=_DECIDED,
            legs=_LEGS,
        )
    with pytest.raises(ValueError, match="deployment-scoped snapshot"):
        _build(portfolio_before={"cash": 1.0})
    with pytest.raises(ValueError, match="deployment-scoped snapshot"):
        _build(portfolio_after=None)


# ── The portfolio is DEPLOYMENT-scoped, not leg-scoped ─────────────────────
#
# A deployment runs its universe as N independent dollar sleeves, so a 2-sleeve
# deployment holds 2 x the sleeve capital. Deriving the snapshot from the legs
# of the one sleeve that traded reported that deployment as holding half its
# money and left the untraded symbol out of `holdings` altogether — inside a
# hashed, reveal()-bound field.

_TWO_SLEEVE_CASH = 100_000.0
_QQQ_TRADE = {
    "symbol": "QQQ",
    "decided_on": date(2026, 6, 10),
    "filled_on": date(2026, 6, 11),
    "side": "buy",
    "size": 100.0,
    "price": 400.0,
    "value": 40_000.0,
    "commission": 20.0,
    "cash_before": 100_000.0,
    "cash_after": 59_980.0,
    "position_before": 0.0,
    "position_after": 100.0,
}
#: SPY trades on the decision date under test; QQQ traded a month EARLIER and
#: does nothing today. Both sleeves are part of the deployment on both dates.
_TWO_SLEEVE_LEGS = {"SPY": _LEGS, "QQQ": [_QQQ_TRADE]}
_TWO_SLEEVE_CLOSES = {"QQQ": {_DECIDED: 425.0}, "SPY": {_DECIDED: 548.21}}


def _two_sleeve(side: str) -> dict:
    return deployment_portfolio(
        decision_date=_DECIDED,
        side=side,
        sleeve_legs=_TWO_SLEEVE_LEGS,
        sleeve_initial_cash=_TWO_SLEEVE_CASH,
        sleeve_closes=_TWO_SLEEVE_CLOSES,
    )


def test_the_snapshot_covers_every_sleeve_not_just_the_one_that_traded():
    before, after = _two_sleeve("before"), _two_sleeve("after")

    # Both symbols present on both sides. The untraded sleeve's absence was the
    # visible half of the bug: a reader could not tell "held nothing" from
    # "was not looked at".
    assert sorted(before["holdings"]) == ["QQQ", "SPY"] == sorted(after["holdings"])

    # QQQ carries forward from its own earlier fill and is marked at TODAY's
    # close, not at the price it filled at a month ago.
    assert before["holdings"]["QQQ"] == {"size": 100.0, "price": 425.0, "value": 42_500.0}
    assert after["holdings"]["QQQ"] == before["holdings"]["QQQ"], "an untraded sleeve does not move across a decision"

    # SPY is bracketed by its own leg.
    assert before["holdings"]["SPY"]["size"] == 0.0
    assert after["holdings"]["SPY"]["size"] == 182.0


def test_cash_is_the_whole_deployments_cash_not_one_sleeves():
    """The load-bearing number. On this fixture the leg-derived rule reported
    407.06 of post-decision cash for a deployment that actually held 60,387.06
    across its two sleeves — and would report that same figure whether the
    deployment had two sleeves or twenty, because it only ever summed the legs
    it happened to be handed."""
    before, after = _two_sleeve("before"), _two_sleeve("after")

    spy_leg = _LEGS[0]
    assert before["cash"] == pytest.approx(spy_leg["cash_before"] + _QQQ_TRADE["cash_after"])
    assert after["cash"] == pytest.approx(spy_leg["cash_after"] + _QQQ_TRADE["cash_after"])

    # ADVERSARIAL: the leg-derived rule this replaced, run on the same input.
    # It must disagree — otherwise the aggregate is decoration.
    leg_only_cash = sum(float(leg["cash_after"]) for leg in _LEGS)
    assert leg_only_cash != pytest.approx(after["cash"])
    assert "QQQ" not in {leg["symbol"] for leg in _LEGS}


def test_a_sleeve_that_never_traded_holds_its_full_opening_capital():
    """Flat, but funded — and NOT missing. Two sleeves that never traded is a
    deployment holding 2x the sleeve capital in cash, which is the number the
    per-leg version could never produce because there were no legs to sum."""
    snapshot = deployment_portfolio(
        decision_date=_DECIDED,
        side="before",
        sleeve_legs={"SPY": _LEGS, "QQQ": [], "IWM": []},
        sleeve_initial_cash=_TWO_SLEEVE_CASH,
        sleeve_closes={"QQQ": {_DECIDED: 425.0}},
    )
    assert snapshot["holdings"]["QQQ"] == {"size": 0.0, "price": 425.0, "value": 0.0}
    # No leg and no close: the mark is an honest absence, never a zero, which
    # would read as "this position is worthless".
    assert snapshot["holdings"]["IWM"] == {"size": 0.0, "price": None, "value": None}
    assert snapshot["cash"] == pytest.approx(_LEGS[0]["cash_before"] + 2 * _TWO_SLEEVE_CASH)


def test_the_aggregate_snapshot_is_what_lands_in_the_hashed_trace():
    """The wiring, not just the helper: what `deployment_portfolio` produces is
    what the trace carries and what the keccak covers."""
    trace = _build(portfolio_before=_two_sleeve("before"), portfolio_after=_two_sleeve("after"))
    assert trace.portfolio_after["holdings"]["QQQ"]["size"] == 100.0
    assert trace.portfolio_after["cash"] == _two_sleeve("after")["cash"]

    understated = _build(
        portfolio_before=_two_sleeve("before"),
        portfolio_after={**_two_sleeve("after"), "cash": 407.06},
    )
    assert understated.trace_hash != trace.trace_hash, "the portfolio must be inside the hash"


def test_multiple_fills_on_one_bar_bracket_the_whole_date():
    """A sleeve that exits and re-enters on the same decision bar: `before` is
    the first fill's opening state and `after` the last fill's closing state,
    not an arbitrary one of the two."""
    exit_leg = {**_LEGS[0], "side": "sell", "size": -50.0, "position_before": 50.0, "position_after": 0.0}
    entry_leg = {**_LEGS[0], "position_before": 0.0, "position_after": 182.0, "filled_on": date(2026, 7, 16)}
    legs = [exit_leg, entry_leg]
    assert (
        deployment_portfolio(
            decision_date=_DECIDED, side="before", sleeve_legs={"SPY": legs}, sleeve_initial_cash=_TWO_SLEEVE_CASH
        )["holdings"]["SPY"]["size"]
        == 50.0
    )
    assert (
        deployment_portfolio(
            decision_date=_DECIDED, side="after", sleeve_legs={"SPY": legs}, sleeve_initial_cash=_TWO_SLEEVE_CASH
        )["holdings"]["SPY"]["size"]
        == 182.0
    )


def test_an_unknown_side_is_rejected():
    with pytest.raises(ValueError, match="before"):
        deployment_portfolio(
            decision_date=_DECIDED, side="during", sleeve_legs={"SPY": _LEGS}, sleeve_initial_cash=_TWO_SLEEVE_CASH
        )


# ── resolve_paper_hashes: the corpus lookup, and what it may NOT swallow ────


def test_resolve_paper_hashes_returns_nothing_for_no_ids():
    """No DB touched at all — the early return is the reason this is cheap on
    the overwhelmingly common path.

    ``_NoSession`` is the assertion: it has neither ``begin_nested`` nor
    ``query``, so any touch of the session on this path is an AttributeError
    rather than a silently-passing test."""

    class _NoSession:
        pass

    assert resolve_paper_hashes(_NoSession(), []) == []
    assert resolve_paper_hashes(_NoSession(), ["", None]) == []  # type: ignore[list-item]


def test_the_signature_requires_a_session_it_can_never_open_one():
    """#1818 P2, stated as the type: there is no ``session=None`` default, so
    no caller can fall back to a second connection the way this function used
    to open one itself. The incident's wedge needs two connections in the same
    logical unit of work; this signature makes that unrepresentable here."""
    import inspect

    params = inspect.signature(resolve_paper_hashes).parameters
    assert list(params) == ["session", "arxiv_ids"]
    assert params["session"].default is inspect.Parameter.empty


class TestResolvePaperHashesAgainstTheCorpus:
    """The corpus lookup against a real (tmp-sqlite) corpus table.

    This is the regression for the shipped defect: the function imported
    ``Paper`` from ``corpus_store``, where the class is ``PaperRecord``. Every
    call raised ``ImportError``, a bare ``except Exception`` swallowed it, the
    result was ``[]``, and a WARNING was logged on every settle — a permanent
    "nothing resolves" indistinguishable from the honest empty result.
    """

    @pytest.fixture(autouse=True)
    def _tmp_db(self, tmp_path):
        from archimedes.models import corpus_store  # noqa: F401  (table registration)

        from tests.db_isolation import redirect_to_tmp_sqlite

        yield from redirect_to_tmp_sqlite(tmp_path)

    @staticmethod
    def _seed(**columns):
        from archimedes.db import get_session
        from archimedes.models.corpus_store import PaperRecord

        with get_session() as session:
            session.merge(PaperRecord(**columns))
            session.commit()

    @staticmethod
    def _resolve(ids):
        """Call it the way production does: on a session the CALLER owns."""
        from archimedes.db import get_session

        with get_session() as session:
            return resolve_paper_hashes(session, ids)

    def test_the_class_this_function_imports_is_the_one_that_exists(self):
        """The typo, stated directly. ``Paper`` is not a name in that module,
        so the old import could only ever raise."""
        from archimedes.models import corpus_store

        assert hasattr(corpus_store, "PaperRecord")
        assert not hasattr(corpus_store, "Paper")

    def test_a_hash_resolves_when_the_record_carries_one(self):
        self._seed(arxiv_id="0706.1497", title="Faber", content_hash="c" * 64)
        assert self._resolve(["0706.1497"]) == ["0706.1497:" + "c" * 64]

    def test_it_falls_back_to_the_pdf_hash(self):
        self._seed(arxiv_id="1234.5678", title="No content hash", pdf_sha256="d" * 64)
        assert self._resolve(["1234.5678"]) == ["1234.5678:" + "d" * 64]

    def test_it_is_empty_when_no_record_exists(self):
        """The honest empty result — the one the swallowed ImportError was
        impersonating. Same call, real corpus, genuinely nothing there."""
        assert self._resolve(["2301.00001"]) == []

    def test_a_record_with_no_hash_at_all_is_omitted_not_half_formed(self):
        self._seed(arxiv_id="2401.00002", title="Bare id")
        assert self._resolve(["2401.00002"]) == []

    def test_only_the_ids_that_resolve_are_returned(self):
        self._seed(arxiv_id="0706.1497", title="Faber", content_hash="c" * 64)
        assert self._resolve(["0706.1497", "2301.00001"]) == ["0706.1497:" + "c" * 64]

    def test_it_reads_on_the_session_it_was_given_and_opens_none(self, monkeypatch):
        """#1818 P2 at unit scale: the corpus row comes back from the caller's
        session, and ``archimedes.db.get_session`` is never called while it is
        open. Armed to RAISE rather than counted — a second connection on this
        path is the incident, not a metric."""
        import archimedes.db as db_module
        from archimedes.db import get_session

        self._seed(arxiv_id="0706.1497", title="Faber", content_hash="c" * 64)
        with get_session() as session:

            def _forbidden():
                raise AssertionError("resolve_paper_hashes opened a second session (#1818 P2)")

            monkeypatch.setattr(db_module, "get_session", _forbidden)
            assert resolve_paper_hashes(session, ["0706.1497"]) == ["0706.1497:" + "c" * 64]

    def test_a_wrong_model_class_RAISES_instead_of_becoming_an_empty_list(self, monkeypatch):
        """The narrowing, shown to reject something.

        Fail-soft is correct for a corpus OUTAGE and wrong for a bug in this
        function — the distinction the bare ``except Exception`` erased. This
        is also why the catch is ``DBAPIError`` and not the ``SQLAlchemyError``
        base: querying a non-model raises ``ArgumentError``, which the base
        would have swallowed into the same ``[]`` a healthy empty corpus
        returns."""
        from archimedes.models import corpus_store
        from sqlalchemy.exc import ArgumentError

        monkeypatch.setattr(corpus_store, "PaperRecord", object)
        with pytest.raises(ArgumentError):
            self._resolve(["0706.1497"])

    def test_a_row_attribute_this_code_is_wrong_about_RAISES(self):
        """The other programming error: reading a column that is not there.
        The result comprehension therefore sits OUTSIDE the try."""
        from contextlib import contextmanager
        from types import SimpleNamespace

        class _Session:
            def query(self, *args, **kwargs):
                return self

            def filter(self, *args, **kwargs):
                return self

            def all(self):
                return [SimpleNamespace(arxiv_id="0706.1497")]  # no content_hash, no pdf_sha256

            @contextmanager
            def begin_nested(self):
                yield

        with pytest.raises(AttributeError):
            resolve_paper_hashes(_Session(), ["0706.1497"])

    def test_an_import_error_RAISES(self, monkeypatch):
        """The exact shape of the shipped defect, reproduced: the model import
        itself failing. Removing the name is what importing a name that was
        never there always did — and it must now be loud instead of returning
        the same ``[]`` as a healthy lookup."""
        from archimedes.models import corpus_store

        monkeypatch.delattr(corpus_store, "PaperRecord")
        with pytest.raises(ImportError):
            self._resolve(["0706.1497"])

    def test_a_real_database_outage_IS_still_soft(self, caplog):
        """The other half of the narrowing: the case fail-soft was for. A
        DBAPIError is an outage, and the trace must still be written."""
        from contextlib import contextmanager

        from sqlalchemy.exc import OperationalError

        class _Down:
            def query(self, *args, **kwargs):
                raise OperationalError("select 1", {}, Exception("corpus is down"))

            @contextmanager
            def begin_nested(self):
                yield

        with caplog.at_level("WARNING"):
            assert resolve_paper_hashes(_Down(), ["0706.1497"]) == []
        assert any("content-hash lookup failed" in r.getMessage() for r in caplog.records)

    def test_the_outage_is_contained_by_a_savepoint_not_by_luck(self, caplog):
        """The cost of sharing the session, paid by the SAVEPOINT (#1818 P2).

        A real DBAPIError — the ``papers`` table genuinely gone — raised on the
        caller's own connection. On PostgreSQL a failed statement aborts the
        whole transaction, so without ``begin_nested`` the ``[]`` above would
        be a lie: every later statement on the cycle session raises
        ``PendingRollbackError`` and the final ``session.commit()`` blows up,
        i.e. a missing corpus table would cost the whole cycle rather than one
        lookup.

        SQLite — what this test runs on — does NOT abort the transaction on a
        statement error, so "the caller could still write afterwards" would
        pass here with or without the savepoint. That half is asserted anyway
        (it is the behaviour being claimed), but the GUARD is the second half:
        the corpus query really did run inside a savepoint that was rolled
        back, observed through SQLAlchemy's own ``savepoint`` /
        ``rollback_savepoint`` connection events, which fire on every dialect.
        Delete ``begin_nested`` and the events list is empty.
        """
        import archimedes.db as db_module
        from archimedes.db import get_session
        from archimedes.models.paper_store import PaperDeployment
        from sqlalchemy import event, text

        savepoints: list[str] = []
        for name in ("savepoint", "rollback_savepoint", "release_savepoint"):
            event.listen(db_module.engine, name, lambda *a, _n=name: savepoints.append(_n))

        self._seed(arxiv_id="0706.1497", title="Faber", content_hash="c" * 64)
        with get_session() as session:
            session.execute(text("DROP TABLE papers"))
            session.merge(
                PaperDeployment(
                    id="dep-1818-p2",
                    strategy_id="aa11bb22cc33dd44",
                    spec_json="{}",
                    deployed_at=date(2026, 9, 3),
                    status="active",
                )
            )
            # Deliberately NOT flushed. Note what actually happens to it:
            # ``begin_nested()`` flushes pending ORM state BEFORE it opens the
            # savepoint, so this row is written by the lookup's own flush and
            # is never inside the savepoint at all — which is why the failed
            # corpus query below cannot take it with it. (The flush being
            # outside the savepoint is also why that call sits outside
            # ``resolve_paper_hashes``' ``try``; see
            # ``test_a_caller_write_that_fails_is_not_laundered_into_a_corpus_outage``.)
            savepoints.clear()  # the caller's own work is not what is being measured

            with caplog.at_level("WARNING"):
                assert resolve_paper_hashes(session, ["0706.1497"]) == []

            assert savepoints == ["savepoint", "rollback_savepoint"], (
                "the corpus lookup must run inside a savepoint that is rolled back on failure — "
                "without it a corpus outage aborts the caller's whole PostgreSQL transaction"
            )

            # …and the caller's transaction is still usable AFTER the lookup.
            session.merge(
                PaperDeployment(
                    id="dep-1818-p2-after",
                    strategy_id="aa11bb22cc33dd44",
                    spec_json="{}",
                    deployed_at=date(2026, 9, 3),
                    status="active",
                )
            )
            session.commit()

        with get_session() as session:
            assert session.get(PaperDeployment, "dep-1818-p2") is not None, (
                "the caller's earlier work did not survive the corpus outage — the savepoint's flush "
                "wrote it before the savepoint opened, so only the corpus query should have rolled back"
            )
            assert session.get(PaperDeployment, "dep-1818-p2-after") is not None, (
                "the caller could not write after the corpus outage — the transaction was aborted"
            )

    def test_a_caller_write_that_fails_is_not_laundered_into_a_corpus_outage(self, caplog):
        """The savepoint's own FLUSH must not be swallowed as an outage.

        ``session.begin_nested()`` is not free of the caller's state.
        ``SessionTransaction.__init__`` calls ``_take_snapshot``, which runs
        ``self.session.flush()`` for a BEGIN_NESTED origin BEFORE the savepoint
        is installed (sqlalchemy 2.0 ``orm/session.py``), and ``SessionLocal``
        is ``autoflush=False`` — so the caller's pending ORM rows are written
        to the database by this function's own savepoint, outside it.

        With ``begin_nested()`` inside the ``try``, an ``IntegrityError`` from
        THAT flush — a ``DBAPIError`` like any other — was caught by the
        corpus-outage handler. Two lies at once: the trace was stored and
        HASHED with ``consulted_paper_hashes=[]`` while the corpus was
        perfectly healthy, and on PostgreSQL the caller's transaction was
        already aborted with no savepoint to roll back to — precisely the
        wedge (``PendingRollbackError`` for every remaining deployment, a
        raising ``commit()``) the savepoint exists to prevent.

        Reachable in the cycle: ``_publish_decision_traces`` does
        ``session.add(PaperDecisionTrace(...))`` in its loop and can then raise
        ``PaperTraceCoverageError``; ``advance_all`` catches it per deployment
        and moves on WITHOUT a rollback, so the next deployment's
        ``resolve_paper_hashes`` is the thing that flushes the previous one's
        pending rows.

        The corpus here is HEALTHY. Only the caller's own row is bad, so the
        error must reach the caller — never a WARNING that blames the corpus.
        """
        from archimedes.db import get_session
        from archimedes.models.paper_store import PaperDailyReturn, PaperDeployment
        from sqlalchemy.exc import IntegrityError

        self._seed(arxiv_id="0706.1497", title="Faber", content_hash="c" * 64)
        with get_session() as session:
            session.add(
                PaperDeployment(
                    id="dep-flush",
                    strategy_id="aa11bb22cc33dd44",
                    spec_json="{}",
                    deployed_at=date(2026, 9, 3),
                    status="active",
                )
            )
            session.flush()
            session.add(PaperDailyReturn(deployment_id="dep-flush", date=date(2026, 1, 2), daily_return=0.01))
            session.commit()

        with get_session() as session:
            # The caller's pending row duplicates (deployment_id, date) — it
            # cannot flush. Nothing is wrong with the corpus.
            session.add(PaperDailyReturn(deployment_id="dep-flush", date=date(2026, 1, 2), daily_return=0.02))

            with caplog.at_level("WARNING"), pytest.raises(IntegrityError):
                resolve_paper_hashes(session, ["0706.1497"])

            assert not [r for r in caplog.records if "content-hash lookup failed" in r.getMessage()], (
                "a CALLER-side write failure was reported as a corpus outage — the trace would be hashed "
                "with consulted_paper_hashes=[] while the corpus was healthy, and on PostgreSQL the "
                "caller's transaction is aborted with no savepoint to roll back to (#1818 P2)"
            )

    def test_a_healthy_corpus_still_resolves_with_a_pending_caller_row(self):
        """The same shape with a row that flushes CLEANLY still resolves.

        Guards the fix from over-correcting into "any pending state breaks the
        lookup": the flush is the caller's, so when it succeeds the corpus
        query must run normally on top of it.
        """
        from archimedes.db import get_session
        from archimedes.models.paper_store import PaperDeployment

        self._seed(arxiv_id="0706.1497", title="Faber", content_hash="c" * 64)
        with get_session() as session:
            session.add(
                PaperDeployment(
                    id="dep-flush-ok",
                    strategy_id="aa11bb22cc33dd44",
                    spec_json="{}",
                    deployed_at=date(2026, 9, 3),
                    status="active",
                )
            )
            assert resolve_paper_hashes(session, ["0706.1497"]) == ["0706.1497:" + "c" * 64]

    def test_the_savepoint_is_released_not_leaked_on_the_healthy_path(self):
        """The other side of the same mechanism: a lookup that SUCCEEDS must
        release its savepoint rather than leave one open on the caller's
        connection for the rest of the cycle."""
        import archimedes.db as db_module
        from archimedes.db import get_session
        from sqlalchemy import event

        savepoints: list[str] = []
        for name in ("savepoint", "rollback_savepoint", "release_savepoint"):
            event.listen(db_module.engine, name, lambda *a, _n=name: savepoints.append(_n))

        self._seed(arxiv_id="0706.1497", title="Faber", content_hash="c" * 64)
        with get_session() as session:
            savepoints.clear()
            assert resolve_paper_hashes(session, ["0706.1497"]) == ["0706.1497:" + "c" * 64]
        assert savepoints == ["savepoint", "release_savepoint"]


# ── Hash properties ─────────────────────────────────────────────────────────


def test_hash_is_stable_across_two_builds_of_the_same_decision():
    """The id is derived from the decision key, not uuid4 — ``id`` is a HASHED
    field, so a random one would make every re-derivation look like a change
    and make drift detection impossible."""
    first, second = _build(), _build()
    assert first.id == second.id
    assert first.trace_hash == second.trace_hash
    assert len(first.trace_hash.removeprefix("0x")) == 64  # keccak256


def test_a_different_decision_date_is_a_different_trace():
    assert _build().id != _build(decision_date=date(2026, 8, 14)).id


def test_paperness_is_inside_the_hash():
    """``trigger`` and ``market_context`` are both hashed fields, so a paper
    trace cannot be laundered into a live one without breaking /verify."""
    trace = _build()
    assert trace.trigger == "paper_settle"
    assert trace.market_context["venue"] == "paper"

    laundered = _build()
    laundered.trigger = "scheduled_tick"
    laundered.market_context = {**laundered.market_context, "venue": "live"}
    assert laundered.compute_hash() != trace.trace_hash


def test_backfill_provenance_is_inside_the_hash():
    """The commit-reveal threat model is entirely post-hoc trace construction.
    A trace written after the fact must admit it, unstrippably."""
    settled = _build(provenance=PROVENANCE_SETTLE)
    backfilled = _build(provenance=PROVENANCE_BACKFILL)
    assert backfilled.market_context["trace_provenance"] == "backfill"
    assert backfilled.trace_hash != settled.trace_hash


def test_an_unknown_provenance_is_rejected():
    with pytest.raises(ValueError, match="provenance"):
        _build(provenance="realtime")


def test_changing_a_leg_changes_the_hash():
    tampered = _build(legs=[{**_LEGS[0], "size": 9999.0}])
    assert tampered.trace_hash != _build().trace_hash


# ── The no-LLM guard (G8) ───────────────────────────────────────────────────

_LLM_MARKERS = (
    "llm_backend",
    "bedrock",
    "anthropic",
    "openai",
    "ollama",
    "generate_completion",
    "invoke_model",
    "converse",
)


def test_no_llm_reaches_the_paper_trace_builder():
    """G8. There is no LLM in the paper settle path and there must not be one:
    a sentence written at settle time is a post-hoc rationalisation of a
    decision a deterministic engine already made — precisely the attack the
    commit-reveal spec exists to defeat.

    A prose claim ("deterministic, no LLM") that nothing enforces is the same
    defect, harder to grep for. The claim also appears in the rendered
    reasoning, so this test is what keeps that sentence true.
    """
    source = (Path(__file__).resolve().parents[2] / "archimedes/services/paper_trace.py").read_text()
    # The module docstring explains WHY there is no LLM, so scan code lines only.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#") and "LLM" not in line
    ).lower()
    hits = [marker for marker in _LLM_MARKERS if marker in code]
    assert not hits, f"paper_trace.py must not reach an LLM client; found {hits}"
    assert "no LLM produced this text" in _build().reasoning


def test_reasoning_is_derived_from_the_spec_and_the_legs():
    reasoning = _build().reasoning
    assert _STRATEGY in reasoning
    assert _SPEC.name in reasoning
    assert "2026-07-14" in reasoning
    assert "SPY" in reasoning
    assert "sha256=" in reasoning
