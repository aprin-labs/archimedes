"""Alembic environment — wired to archimedes.db.DATABASE_URL + Base.metadata.

Issue #1028 (chunk 1/5, alembic-baseline): Archimedes previously had no
migration tool — schema was created via ``Base.metadata.create_all()`` in
``archimedes.db.init_db()``, patched in-place on Postgres via idempotent
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` statements (see ``db.py``).
Alembic now owns retrofitting an ALREADY-POPULATED Postgres DB (adding
constraints, backfilling columns) since ``create_all()`` cannot ALTER
existing tables. ``init_db()`` / ``create_all()`` remains the fresh-DB path
for SQLite (all tests + local dev) — this file does not change that; see
``archimedes/db.py`` docstrings for the two-path rationale.

DATABASE_URL resolution mirrors ``archimedes.db`` exactly: the same env var,
the same SQLite fallback anchored to ``backend/``. We import
``archimedes.db`` directly (rather than re-deriving the default) so the two
can never drift.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the `archimedes` package importable regardless of CWD — this file
# lives at backend/migrations/env.py, so backend/ (its grandparent) is the
# package root, exactly like pytest.ini's `pythonpath = backend`.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Side-effect imports: every ORM model must be imported so it registers its
# table on `Base.metadata` before we hand that metadata to Alembic — otherwise
# autogenerate would see tables as "missing" and (worse) a real `upgrade`
# would silently skip them. This mirrors `archimedes.db.init_db()`'s import
# list exactly (including the two models init_db() imports only inline for
# the same reason), so the two never drift apart.
from archimedes.db import DATABASE_URL  # noqa: E402
from archimedes.models.account import (  # noqa: E402
    AuthAccount,
    AuthEmailDelivery,
    AuthSession,
    AuthUser,
    AuthVerification,
    LinkedWallet,
    WalletLinkChallenge,
)
from archimedes.models.asset_daily_bars import AssetDailyBar  # noqa: E402
from archimedes.models.backtest_fixtures_store import StrategyBacktestFixture  # noqa: E402
from archimedes.models.backtest_store import BacktestResultRecord  # noqa: E402
from archimedes.models.chat import Base  # noqa: E402
from archimedes.models.corpus_store import CorpusMetaRecord, PaperRecord  # noqa: E402
from archimedes.models.daily_returns_store import StrategyDailyReturn  # noqa: E402
from archimedes.models.debate_transcript import DebateTranscriptRecord  # noqa: E402
from archimedes.models.generation_cost import GenerationCostRecord  # noqa: E402
from archimedes.models.identity import ControlledWallet, IdentityEvent, WalletIdentity  # noqa: E402
from archimedes.models.kg import KGEntity, KGRelation  # noqa: E402
from archimedes.models.marketplace import (  # noqa: E402
    MarketplaceAgent,
    SettlementIntent,
    SubscriberLiability,
    SubscriberTickLog,
)
from archimedes.models.payment_receipt import PaymentReceiptRecord  # noqa: E402
from archimedes.models.request_snapshot import RequestCountSnapshot  # noqa: E402
from archimedes.models.strategy_generators import StrategyGenerator  # noqa: E402
from archimedes.models.strategy_passport_record import (  # noqa: E402
    PassportPaperRef,
    StrategyPassportRecord,
)
from archimedes.models.strategy_proposal import StrategyProposal  # noqa: E402
from archimedes.models.strategy_store import StrategyRecord  # noqa: E402
from archimedes.models.user_profile import UserProfile  # noqa: E402

# The model imports above are side-effect imports: they register every table on
# ``Base.metadata`` so Alembic's ``target_metadata`` (below) sees the full schema
# for autogenerate/verification. ruff ignores them via ``noqa: F401``; listing
# them in ``__all__`` marks them as intentional re-exports so CodeQL's
# unused-import query treats them as used too. (This module is an Alembic
# entrypoint, never ``import *``-ed, so ``__all__`` has no other effect.)
__all__ = [
    "DATABASE_URL",
    "AuthAccount",
    "AuthEmailDelivery",
    "AuthSession",
    "AuthUser",
    "AuthVerification",
    "AssetDailyBar",
    "BacktestResultRecord",
    "Base",
    "ControlledWallet",
    "CorpusMetaRecord",
    "DebateTranscriptRecord",
    "GenerationCostRecord",
    "IdentityEvent",
    "KGEntity",
    "KGRelation",
    "LinkedWallet",
    "MarketplaceAgent",
    "PaperRecord",
    "PassportPaperRef",
    "PaymentReceiptRecord",
    "RequestCountSnapshot",
    "SettlementIntent",
    "StrategyBacktestFixture",
    "StrategyDailyReturn",
    "StrategyGenerator",
    "StrategyPassportRecord",
    "StrategyProposal",
    "StrategyRecord",
    "SubscriberLiability",
    "SubscriberTickLog",
    "UserProfile",
    "WalletIdentity",
    "WalletLinkChallenge",
    "run_migrations_offline",
    "run_migrations_online",
    "target_metadata",
]

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL (env var, else the backend-anchored SQLite default) always
# wins over whatever static `sqlalchemy.url` sits in alembic.ini — same
# precedence rule as archimedes.db.engine so `alembic upgrade head` and the
# app always point at the same database.
config.set_main_option("sqlalchemy.url", DATABASE_URL)

# add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # SQLite can't ALTER most column/constraint properties in place;
        # Alembic's "batch" mode works around this by rebuilding the table
        # (create new + copy + swap) under the hood. Postgres ignores this
        # flag's rebuild behavior for operations it can do natively via
        # ALTER, so enabling it unconditionally is safe on both dialects —
        # render_as_batch only changes codegen for `alembic revision
        # --autogenerate`, and here we force it on for SQLite specifically
        # since that's the dialect that actually needs it (tests + local dev).
        is_sqlite = connection.engine.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=is_sqlite,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
