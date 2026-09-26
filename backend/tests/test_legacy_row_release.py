"""#1283 — releasing a platform-adopted row back to its real owner.

The adoption migration stamps ``owner_user_id`` onto rows whose wallet nobody
had ever linked. ``claim_legacy_wallet_data`` cannot reach a stamped row (it
filters on ``owner_user_id IS NULL``), so without a release path adoption
would be a one-way door: the real holder of that wallet could link it, prove
control with a fresh signature, and still never get their own rows back.

These tests pin the door open in both directions, and pin it CLOSED for
everyone else.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from archimedes.models.account import PLATFORM_LEGACY_USER_ID, AuthUser
from archimedes.models.chat import Base
from archimedes.models.legacy_adoption import LegacyRowAdoption
from archimedes.models.strategy_passport_record import StrategyPassportRecord
from archimedes.models.strategy_proposal import StrategyProposal
from archimedes.models.strategy_store import StrategyRecord, upsert_strategy
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ADOPTED_WALLET = "0x3333333333333333333333333333333333333333"
OTHER_WALLET = "0x4444444444444444444444444444444444444444"
NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    assert LegacyRowAdoption.__table__.metadata is Base.metadata
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all(
        [
            AuthUser(id=uid, name=uid, email=f"{uid}@example.com", email_verified=True, created_at=NOW, updated_at=NOW)
            for uid in ("user-1", "user-2", PLATFORM_LEGACY_USER_ID)
        ]
    )
    session.commit()
    return session


def _adopt(session: Session, wallet: str) -> str:
    """Create a strategy owned by *wallet*, then adopt it exactly as the
    migration does: stamp the platform id and record the prior wallet."""
    record = upsert_strategy(
        session,
        generation_method="fusion",
        strategy_name="Orphaned",
        thesis="pre-account row",
        source_papers=[],
        asset_universe=["SPY"],
        owner_wallet=wallet,
    )
    session.flush()
    strategy_id = record.id
    session.query(StrategyRecord).filter_by(id=strategy_id).update(
        {StrategyRecord.owner_user_id: PLATFORM_LEGACY_USER_ID}
    )
    session.add(
        LegacyRowAdoption(
            table_name="strategy_store",
            row_pk=strategy_id,
            prior_owner_wallet=wallet.lower(),
            adopted_by_user_id=PLATFORM_LEGACY_USER_ID,
            adopted_at=NOW,
        )
    )
    session.commit()
    return strategy_id


def test_linking_the_adopted_wallet_hands_the_row_back():
    from archimedes.api.wallet_routes import claim_legacy_wallet_data

    with _session() as session:
        strategy_id = _adopt(session, ADOPTED_WALLET)
        assert session.get(StrategyRecord, strategy_id).owner_user_id == PLATFORM_LEGACY_USER_ID

        claim_legacy_wallet_data(session, "user-1", ADOPTED_WALLET)
        session.commit()

        assert session.get(StrategyRecord, strategy_id).owner_user_id == "user-1"
        # The ledger entry is consumed — the row is no longer adopted, so a
        # downgrade must not re-orphan it out from under its new owner.
        assert session.query(LegacyRowAdoption).count() == 0


def test_a_different_wallet_gets_nothing():
    """The release is keyed on the wallet the row actually named. Proving
    control of some OTHER address must not move a platform-adopted row."""
    from archimedes.api.wallet_routes import claim_legacy_wallet_data

    with _session() as session:
        strategy_id = _adopt(session, ADOPTED_WALLET)

        claim_legacy_wallet_data(session, "user-2", OTHER_WALLET)
        session.commit()

        assert session.get(StrategyRecord, strategy_id).owner_user_id == PLATFORM_LEGACY_USER_ID
        assert session.query(LegacyRowAdoption).count() == 1


def test_a_narrowed_claim_cannot_widen_itself_through_the_release():
    """``rename_strategy`` passes the strategy-only trio. A release must obey
    the same narrowing — it may never reach a table the caller excluded."""
    from archimedes.api.wallet_routes import claim_legacy_wallet_data

    with _session() as session:
        strategy_id = _adopt(session, ADOPTED_WALLET)

        claim_legacy_wallet_data(
            session,
            "user-1",
            ADOPTED_WALLET,
            models=(
                (StrategyPassportRecord, StrategyPassportRecord.owner_wallet),
                (StrategyProposal, StrategyProposal.owner_wallet),
            ),
            include_profile=False,
        )
        session.commit()

        # strategy_store was not in the tuple, so its adopted row stays put.
        assert session.get(StrategyRecord, strategy_id).owner_user_id == PLATFORM_LEGACY_USER_ID
        assert session.query(LegacyRowAdoption).count() == 1


def test_release_is_a_no_op_when_nothing_was_ever_adopted():
    """Every database that has never run the adoption migration takes this
    path on every wallet link; it must cost nothing and change nothing."""
    from archimedes.api.wallet_routes import claim_legacy_wallet_data

    with _session() as session:
        upsert_strategy(
            session,
            generation_method="fusion",
            strategy_name="Unclaimed",
            thesis="pre-account row",
            source_papers=[],
            asset_universe=["SPY"],
            owner_wallet=ADOPTED_WALLET,
        )
        session.commit()

        claim_legacy_wallet_data(session, "user-1", ADOPTED_WALLET)
        session.commit()

        # The ordinary claim still worked...
        assert session.query(StrategyRecord).filter(StrategyRecord.owner_user_id == "user-1").count() == 1
        # ...and the release wrote no ledger rows.
        assert session.query(LegacyRowAdoption).count() == 0


def test_release_survives_a_database_that_has_no_ledger_table():
    """A database that has not reached the adoption revision has no
    ``legacy_row_adoptions``. The release runs on EVERY wallet link, so it
    must find that out cheaply and return — not raise, which would poison the
    claim's transaction and break wallet linking outright.
    """
    from archimedes.api.wallet_routes import claim_legacy_wallet_data

    with _session() as session:
        upsert_strategy(
            session,
            generation_method="fusion",
            strategy_name="Pre-migration",
            thesis="pre-account row",
            source_papers=[],
            asset_universe=["SPY"],
            owner_wallet=ADOPTED_WALLET,
        )
        session.commit()

        # Take the ledger away, exactly as a not-yet-migrated database has it.
        LegacyRowAdoption.__table__.drop(session.get_bind())
        assert not sa.inspect(session.connection()).has_table("legacy_row_adoptions")

        claim_legacy_wallet_data(session, "user-1", ADOPTED_WALLET)
        session.commit()

        # The ordinary claim still landed — the missing ledger changed nothing.
        assert session.query(StrategyRecord).filter(StrategyRecord.owner_user_id == "user-1").count() == 1


# ── The release must stay DISCOVERABLE ─────────────────────────────────────
#
# The tests above prove the door opens. These prove the sign on it survives
# adoption: `GET /api/wallet/check` -> `_wallet_has_unclaimed_legacy_data` is
# the sole gate on the relink banner (`ui/src/AuthenticatedApp.jsx`), and that
# banner is the only thing that ever tells a pre-account wallet holder their
# strategies are recoverable. A release path nobody is told about is a one-way
# door in practice, however reversible it is in code.


def test_the_relink_prompt_still_fires_for_an_adopted_wallet():
    """The blocking regression. ``_wallet_has_unclaimed_legacy_data`` mirrors
    the claim loop's ``owner_user_id IS NULL`` filter — which adoption
    defeats by construction, since the stamp is exactly what makes the row
    non-NULL. Without the ledger check the predicate answers False for
    precisely the wallets whose rows the platform is holding, the banner never
    renders, and the real owner is never invited to claim.
    """
    from archimedes.api.wallet_routes import _wallet_has_unclaimed_legacy_data

    with _session() as session:
        # Before adoption: the ordinary unclaimed-row path already says yes.
        upsert_strategy(
            session,
            generation_method="fusion",
            strategy_name="Orphaned",
            thesis="pre-account row",
            source_papers=[],
            asset_universe=["SPY"],
            owner_wallet=ADOPTED_WALLET,
        )
        session.commit()
        assert _wallet_has_unclaimed_legacy_data(session, "user-1", ADOPTED_WALLET) is True

        # Adopt every one of this wallet's rows, exactly as the migration does.
        session.query(StrategyRecord).filter(StrategyRecord.owner_wallet == ADOPTED_WALLET).update(
            {StrategyRecord.owner_user_id: PLATFORM_LEGACY_USER_ID}, synchronize_session=False
        )
        for row in session.query(StrategyRecord).filter(StrategyRecord.owner_wallet == ADOPTED_WALLET).all():
            session.add(
                LegacyRowAdoption(
                    table_name="strategy_store",
                    row_pk=row.id,
                    prior_owner_wallet=ADOPTED_WALLET.lower(),
                    adopted_by_user_id=PLATFORM_LEGACY_USER_ID,
                    adopted_at=NOW,
                )
            )
        session.commit()

        # No row is `owner_user_id IS NULL` any more...
        assert (
            session.query(StrategyRecord)
            .filter(StrategyRecord.owner_wallet == ADOPTED_WALLET, StrategyRecord.owner_user_id.is_(None))
            .count()
            == 0
        )
        # ...and the prompt must STILL fire, because linking would still hand
        # the row back.
        assert _wallet_has_unclaimed_legacy_data(session, "user-1", ADOPTED_WALLET) is True


def test_the_prompt_and_the_release_agree_end_to_end():
    """The invariant the predicate's own docstring states: the prompt fires
    iff linking would actually attach something. Pinned across the adoption
    boundary — True while the platform holds the row, False the moment the
    claim has handed it back, so the banner does not nag a user who has
    already recovered everything."""
    from archimedes.api.wallet_routes import _wallet_has_unclaimed_legacy_data, claim_legacy_wallet_data

    with _session() as session:
        strategy_id = _adopt(session, ADOPTED_WALLET)
        assert _wallet_has_unclaimed_legacy_data(session, "user-1", ADOPTED_WALLET) is True

        claim_legacy_wallet_data(session, "user-1", ADOPTED_WALLET)
        session.commit()

        assert session.get(StrategyRecord, strategy_id).owner_user_id == "user-1"
        assert _wallet_has_unclaimed_legacy_data(session, "user-1", ADOPTED_WALLET) is False


def test_the_prompt_does_not_fire_for_a_wallet_whose_rows_someone_else_holds():
    """Discoverability must not become a leak. The ledger is keyed on the
    prior wallet, so proving some OTHER address must not light the banner —
    it would advertise the existence of a stranger's adopted rows to anyone
    who can name an address."""
    from archimedes.api.wallet_routes import _wallet_has_unclaimed_legacy_data

    with _session() as session:
        _adopt(session, ADOPTED_WALLET)

        assert _wallet_has_unclaimed_legacy_data(session, "user-2", OTHER_WALLET) is False


def test_the_prompt_survives_a_database_that_has_no_ledger_table():
    """``/api/wallet/check`` runs for every account with an unlinked browser
    wallet, including on a database that has not reached the adoption
    revision. The ledger lookup must find that out cheaply and return, not
    raise — a 500 here would break the banner for the users it exists for."""
    from archimedes.api.wallet_routes import _wallet_has_unclaimed_legacy_data

    with _session() as session:
        upsert_strategy(
            session,
            generation_method="fusion",
            strategy_name="Pre-migration",
            thesis="pre-account row",
            source_papers=[],
            asset_universe=["SPY"],
            owner_wallet=ADOPTED_WALLET,
        )
        session.commit()

        LegacyRowAdoption.__table__.drop(session.get_bind())
        assert not sa.inspect(session.connection()).has_table("legacy_row_adoptions")

        assert _wallet_has_unclaimed_legacy_data(session, "user-1", ADOPTED_WALLET) is True
        assert _wallet_has_unclaimed_legacy_data(session, "user-1", OTHER_WALLET) is False
