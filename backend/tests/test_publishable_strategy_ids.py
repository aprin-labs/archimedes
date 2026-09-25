"""#1663 — the Library's publish-rights read is O(1) queries, not one per row.

``strategies_routes`` called ``wallet_can_publish`` once per response row on
both Library listings. Each call is a single ``.first()``
(``models/strategy_generators.py``), so a 34-row curated page issued 34 extra
sequential round trips, every one of them paying a ``pool_pre_ping``
``SELECT 1`` first — and because the per-row call short-circuits on an
anonymous caller, a visitor paid nothing while the signed-in owner paid all 34.

``_publishable_strategy_ids`` replaces the loop with one ``IN`` query. These
tests pin two independent properties:

  1. **Query count is constant, not linear.** Measured at the engine boundary
     (``before_cursor_execute``), with the pre-change per-row shape run through
     the same counter as the adversarial control — if the counter could not see
     the N+1 there, the "constant" assertions would pass against the unfixed
     code too.
  2. **The answers are unchanged**, including the anonymous short-circuit and
     the ``PLATFORM_ADMIN_WALLETS`` override, compared row-for-row against
     ``wallet_can_publish`` itself.

Hermetic: in-memory SQLite, no Docker, no env file. ``PLATFORM_ADMIN_WALLETS``
is set via monkeypatch only where an admin is under test.
"""

from __future__ import annotations

import pytest
from archimedes.api.strategies_routes import _publishable_strategy_ids
from archimedes.models.chat import Base
from archimedes.models.identity import WalletIdentity
from archimedes.models.strategy_generators import StrategyGenerator, wallet_can_publish
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

# 42-char addresses; the DB stores and matches them lower-cased. They carry hex
# LETTERS on purpose — an all-digit address is unchanged by .upper() and would
# make the casing test below vacuous.
_OWNER = "0x" + "a1" * 20
_ADMIN = "0x" + "b2" * 20
_STRANGER = "0x" + "c3" * 20


@pytest.fixture(autouse=True)
def _no_ambient_admin_wallets(monkeypatch):
    """Pin ``PLATFORM_ADMIN_WALLETS`` empty unless a test sets it.

    Hermetic-test mandate: a developer's ``.env`` (or a leaked earlier import)
    setting this would silently flip the curated-page query count from 2 to 1
    and turn the non-admin assertions into a different test than CI runs. The
    admin tests below override it explicitly with their own ``monkeypatch``.
    """
    monkeypatch.delenv("PLATFORM_ADMIN_WALLETS", raising=False)


def _capture_sql(engine) -> tuple[list[str], object]:
    """Attach a before_cursor_execute listener; return (statements, detach)."""
    statements: list[str] = []

    def _on_exec(_conn, _cursor, statement, _params, _context, _executemany) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _on_exec)

    def _detach() -> None:
        event.remove(engine, "before_cursor_execute", _on_exec)

    return statements, _detach


def _generator_selects(statements: list[str]) -> list[str]:
    """The captured statements that read the publish-rights table."""
    return [s for s in statements if s.lstrip().upper().startswith("SELECT") and "strategy_generators" in s]


def _seeded(n_rows: int, generated_by_owner: int = 0):
    """A session over ``n_rows`` strategy ids, the first ``generated_by_owner``
    of which _OWNER generated. _ADMIN and _STRANGER generated nothing."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine, tables=[WalletIdentity.__table__, StrategyGenerator.__table__])
    session = SessionLocal()

    for wallet in (_OWNER, _ADMIN, _STRANGER):
        session.add(WalletIdentity(wallet_address=wallet, actor_class="human"))

    ids = [f"strat_{i:03d}" for i in range(n_rows)]
    for sid in ids[:generated_by_owner]:
        session.add(StrategyGenerator(strategy_id=sid, wallet_address=_OWNER))
    session.commit()
    return session, ids


def _legacy_publishable(session, ids: list[str], wallet: str | None, *, is_example: bool) -> set[str]:
    """The PRE-#1663 per-row shape, verbatim, as parity oracle AND control.

    ``bool(caller) and wallet_can_publish(...)`` is exactly what both route
    loops evaluated per row.
    """
    return {
        sid
        for sid in ids
        if bool(wallet) and wallet_can_publish(session, strategy_id=sid, wallet_address=wallet, is_example=is_example)
    }


# ── Query count: constant, not linear ───────────────────────────────────────


def test_generated_window_issues_exactly_one_query() -> None:
    """THE GUARD: 20 rows on GET /api/strategies/generated → one query."""
    session, ids = _seeded(n_rows=20, generated_by_owner=7)
    try:
        assert len(ids) == 20
        statements, detach = _capture_sql(session.get_bind())
        try:
            publishable = _publishable_strategy_ids(session, ids, _OWNER, is_example=False)
        finally:
            detach()

        selects = _generator_selects(statements)
        # Non-vacuity: an empty capture would make "== 1" a tautology.
        assert selects, "captured no SELECT against strategy_generators — the listener did not fire"
        assert len(selects) == 1, (
            f"issued {len(selects)} queries for {len(ids)} rows; still looping per row. statements={selects!r}"
        )
        assert publishable == set(ids[:7])
    finally:
        session.close()


def test_per_row_gate_issues_twenty_queries_for_a_twenty_row_window() -> None:
    """ADVERSARIAL CONTROL for the guard above: the pre-#1663 shape, measured.

    If the counter could not see the N+1 here, "exactly 1" above would pass
    against the unfixed code too and guard nothing.
    """
    session, ids = _seeded(n_rows=20, generated_by_owner=7)
    try:
        statements, detach = _capture_sql(session.get_bind())
        try:
            _legacy_publishable(session, ids, _OWNER, is_example=False)
        finally:
            detach()

        selects = _generator_selects(statements)
        assert len(selects) == 20, f"expected the old per-row gate to emit one SELECT per row; got {len(selects)}"
    finally:
        session.close()


def test_curated_window_query_count_is_constant_not_linear() -> None:
    """The curated page (is_example=True) costs a CONSTANT two queries: one IN
    query plus one PLATFORM_ADMIN_WALLETS probe.

    The probe is deliberate, not an oversight. The admin override lives inside
    ``wallet_can_publish``; re-parsing ``PLATFORM_ADMIN_WALLETS`` in the route
    would create a third copy of that parsing (``models/strategy_generators.py``
    and ``api/metrics_private_routes.py`` already hold two), and a copy that
    drifts silently changes who may publish. One extra indexed lookup that
    returns nothing is the price of keeping the semantics single-sourced — and
    it is a constant, which is the whole point of the issue.
    """
    for n_rows in (5, 20, 60):
        session, ids = _seeded(n_rows=n_rows, generated_by_owner=0)
        try:
            statements, detach = _capture_sql(session.get_bind())
            try:
                _publishable_strategy_ids(session, ids, _STRANGER, is_example=True)
            finally:
                detach()

            selects = _generator_selects(statements)
            assert len(selects) == 2, f"{n_rows} rows → {len(selects)} queries; expected a constant 2. {selects!r}"
        finally:
            session.close()


def test_curated_window_for_an_admin_wallet_issues_one_query(monkeypatch) -> None:
    """An actual admin costs ZERO probe queries — the override returns before
    ``wallet_can_publish`` ever reaches the row lookup."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN)
    session, ids = _seeded(n_rows=20, generated_by_owner=0)
    try:
        statements, detach = _capture_sql(session.get_bind())
        try:
            publishable = _publishable_strategy_ids(session, ids, _ADMIN, is_example=True)
        finally:
            detach()

        assert len(_generator_selects(statements)) == 1
        assert publishable == set(ids)
    finally:
        session.close()


def test_anonymous_caller_issues_no_query_at_all() -> None:
    """The anonymous short-circuit is NOT relaxed: a visitor issues no query and
    is told they can publish nothing."""
    session, ids = _seeded(n_rows=20, generated_by_owner=7)
    try:
        statements, detach = _capture_sql(session.get_bind())
        try:
            assert _publishable_strategy_ids(session, ids, None, is_example=True) == set()
            assert _publishable_strategy_ids(session, ids, "", is_example=False) == set()
        finally:
            detach()

        assert _generator_selects(statements) == []
    finally:
        session.close()


def test_empty_window_issues_no_query() -> None:
    session, _ids = _seeded(n_rows=5, generated_by_owner=2)
    try:
        statements, detach = _capture_sql(session.get_bind())
        try:
            assert _publishable_strategy_ids(session, [], _OWNER, is_example=True) == set()
        finally:
            detach()
        assert _generator_selects(statements) == []
    finally:
        session.close()


# ── Answers: byte-identical to the per-row gate ─────────────────────────────


@pytest.mark.parametrize("is_example", [True, False])
@pytest.mark.parametrize(
    ("wallet", "generated_by_owner"),
    [
        (None, 0),  # anonymous visitor
        (_OWNER, 0),  # signed in, generated nothing
        (_OWNER, 4),  # signed in, generated some of the window
        (_OWNER, 12),  # signed in, generated the whole window
        (_STRANGER, 4),  # signed in, generated none of these
    ],
)
def test_matches_the_per_row_gate_exactly(wallet, generated_by_owner, is_example) -> None:
    """Same set as ``wallet_can_publish`` row by row, over every ownership mix."""
    session, ids = _seeded(n_rows=12, generated_by_owner=generated_by_owner)
    try:
        expected = _legacy_publishable(session, ids, wallet, is_example=is_example)
        assert _publishable_strategy_ids(session, ids, wallet, is_example=is_example) == expected
    finally:
        session.close()


@pytest.mark.parametrize("is_example", [True, False])
def test_matches_the_per_row_gate_for_an_admin_wallet(monkeypatch, is_example) -> None:
    """The admin override is delegated, so it must still agree row for row —
    including the fact that it applies ONLY to curated (is_example) rows."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", f"{_ADMIN}, {_STRANGER}")
    session, ids = _seeded(n_rows=12, generated_by_owner=3)
    try:
        expected = _legacy_publishable(session, ids, _ADMIN, is_example=is_example)
        assert _publishable_strategy_ids(session, ids, _ADMIN, is_example=is_example) == expected
        # Non-vacuity: the two cases must actually differ, or this test says
        # nothing about the override.
        assert expected == (set(ids) if is_example else set())
    finally:
        session.close()


def test_admin_may_publish_a_curated_row_it_did_not_generate(monkeypatch) -> None:
    """The behavioural statement of the override, independent of the oracle:
    an admin publishes curated rows it never generated. Batching must not
    quietly drop this — it is the one branch a naive IN query loses."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN)
    session, ids = _seeded(n_rows=6, generated_by_owner=0)
    try:
        assert _publishable_strategy_ids(session, ids, _ADMIN, is_example=True) == set(ids)
    finally:
        session.close()


def test_non_admin_may_not_publish_a_curated_row_it_did_not_generate(monkeypatch) -> None:
    """ADVERSARIAL CONTROL for the test above: the same call with the wallet
    absent from PLATFORM_ADMIN_WALLETS must grant nothing. Without this, an
    implementation that returned every id unconditionally would pass."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN)
    session, ids = _seeded(n_rows=6, generated_by_owner=2)
    try:
        assert _publishable_strategy_ids(session, ids, _STRANGER, is_example=True) == set()
        assert _publishable_strategy_ids(session, ids, _OWNER, is_example=True) == set(ids[:2])
    finally:
        session.close()


def test_admin_override_does_not_leak_onto_generated_rows(monkeypatch) -> None:
    """``is_example=False`` has no admin path in ``wallet_can_publish`` and must
    have none here — an admin cannot publish somebody else's generated
    strategy."""
    monkeypatch.setenv("PLATFORM_ADMIN_WALLETS", _ADMIN)
    session, ids = _seeded(n_rows=6, generated_by_owner=3)
    try:
        assert _publishable_strategy_ids(session, ids, _ADMIN, is_example=False) == set()
    finally:
        session.close()


def test_caller_wallet_casing_does_not_change_the_answer() -> None:
    """``wallet_can_publish`` lower-cases its argument; the batched read must
    too, or a checksum-cased session wallet would silently lose publish rights."""
    session, ids = _seeded(n_rows=8, generated_by_owner=3)
    try:
        mixed = "0x" + _OWNER[2:].upper()
        assert mixed != _OWNER
        assert _publishable_strategy_ids(session, ids, mixed, is_example=False) == set(ids[:3])
    finally:
        session.close()


def test_repeated_ids_do_not_change_the_answer_or_the_query_count() -> None:
    session, ids = _seeded(n_rows=8, generated_by_owner=3)
    try:
        statements, detach = _capture_sql(session.get_bind())
        try:
            actual = _publishable_strategy_ids(session, ids + ids, _OWNER, is_example=False)
        finally:
            detach()
        assert actual == set(ids[:3])
        assert len(_generator_selects(statements)) == 1
    finally:
        session.close()


def test_ids_outside_the_window_are_never_returned() -> None:
    """The IN list bounds the answer: a strategy the wallet generated but which
    is not on this page must not appear."""
    session, ids = _seeded(n_rows=10, generated_by_owner=10)
    try:
        page = ids[:3]
        assert _publishable_strategy_ids(session, page, _OWNER, is_example=False) == set(page)
    finally:
        session.close()
