from __future__ import annotations

from datetime import UTC, datetime

from archimedes.models.account import AuthUser
from archimedes.models.chat import Base
from archimedes.models.identity import WalletIdentity
from archimedes.models.strategy_passport_record import StrategyPassportRecord
from archimedes.models.strategy_proposal import StrategyProposal
from archimedes.models.strategy_store import StrategyRecord, upsert_strategy
from archimedes.models.user_profile import UserProfile
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session


def _user(user_id: str, email: str) -> AuthUser:
    now = datetime.now(UTC)
    return AuthUser(
        id=user_id,
        name=user_id,
        email=email,
        email_verified=False,
        created_at=now,
        updated_at=now,
    )


def test_application_owned_tables_reference_canonical_user():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)

    assert WalletIdentity.__tablename__ in inspector.get_table_names()
    for table in (
        StrategyRecord.__tablename__,
        StrategyPassportRecord.__tablename__,
        StrategyProposal.__tablename__,
        UserProfile.__tablename__,
        "vault_metadata",
    ):
        assert "owner_user_id" in {column["name"] for column in inspector.get_columns(table)}


def test_real_user_metric_counts_canonical_accounts_without_wallets(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([_user("user-1", "one@example.com"), _user("user-2", "two@example.com")])
        session.commit()

    monkeypatch.setattr("archimedes.db.get_session", lambda: Session(engine))
    from archimedes.services import user_stats

    user_stats._reset_cache()
    try:
        assert user_stats.get_distinct_user_count() == 2
        user_stats._reset_cache()
        assert user_stats.get_distinct_user_count_or_none() == 2
    finally:
        user_stats._reset_cache()


def test_real_user_metric_fails_soft_to_none_not_zero_on_db_error(monkeypatch):
    """Round 4 fix: get_distinct_user_count_or_none() must return None (a
    loud absence) on a query failure, distinguishing it from
    get_distinct_user_count()'s legacy 0-on-error shape, which is still
    correct for /health and /private/cost but dishonest wherever the value is
    DISPLAYED as a measured fact (GET /api/metrics's real_users).

    Mutation-verified: reverting get_distinct_user_count_or_none's
    `if result is None: return None` back to `return 0` makes this
    assertion fail.
    """
    from archimedes.services import user_stats

    def _broken_get_session():
        raise RuntimeError("DB unavailable (simulated)")

    monkeypatch.setattr("archimedes.db.get_session", _broken_get_session)
    user_stats._reset_cache()
    try:
        assert user_stats.get_distinct_user_count_or_none() is None
        # The legacy fail-soft-to-0 variant is unchanged for its own callers.
        assert user_stats.get_distinct_user_count() == 0
    finally:
        user_stats._reset_cache()


def test_strategy_dedup_never_transfers_canonical_owner():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    kwargs = {
        "generation_method": "fusion",
        "strategy_name": "Canonical owner",
        "thesis": "same content",
        "source_papers": [],
        "asset_universe": ["SPY"],
    }
    with Session(engine) as session:
        session.add_all([_user("user-1", "one@example.com"), _user("user-2", "two@example.com")])
        session.flush()
        first = upsert_strategy(session, **kwargs, owner_user_id="user-1")
        second = upsert_strategy(session, **kwargs, owner_user_id="user-2")
        session.commit()

        assert first.id == second.id
        assert session.get(StrategyRecord, first.id).owner_user_id == "user-1"
