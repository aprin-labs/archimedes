"""#1283 — adopting the orphaned legacy strategy rows under a platform account.

Exercises the REAL alembic revision (``d3a71f5c9e28``) as a subprocess against
a throwaway SQLite file, the same hermetic pattern
``test_alembic_migrations.py`` uses and for the same reason: a whitelist-only
environment so a developer's ``.env`` cannot point the migration at a
docker-compose Postgres — or, far worse here, at production.

Six behaviours, one test each:

  1. the happy path adopts every orphan and NOTHING else;
  2. a population outside the [140, 165] band is refused, with nothing written;
  3. each structural check (D1/D2/D3) refuses, with nothing written;
  4. a dry run writes nothing at all — not even the alembic version stamp —
     with or without a population, the zero case included, because that is
     the branch that would otherwise return cleanly and get stamped;
  5. running ``upgrade()`` a second time over adopted data is a true no-op;
  6. ``downgrade()`` re-orphans ownership and leaves everything else alone.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
#: The revision under test. Every ``_run_alembic("upgrade", ...)`` below names
#: it EXPLICITLY rather than "head", and that is load-bearing, not style. It
#: was head when this file was written, so the two were the same string; the
#: moment any branch chains a migration behind this one they stop being, and
#: "head" silently changes what the test runs. Two ways it breaks, both seen:
#: the idempotency test ``stamp``s back to ``_down_revision()`` and upgrades
#: again to re-execute THIS revision's ``upgrade()`` — with "head" that also
#: re-executes every later revision, and an ``add_column`` is not idempotent
#: (``sqlite3.OperationalError: duplicate column name``); and the dry-run
#: tests assert ``alembic_version`` equals this revision afterwards, which
#: "head" makes false. Naming the revision is what makes this file about this
#: migration instead of about whatever merged last.
_REVISION = "d3a71f5c9e28"

#: 60 store rows + 60 passport rows (the same 60 strategies, both mirrors
#: orphaned) + 32 proposals = 152, the population the owner decided about.
_N_PAIRS = 60
_N_PROPOSALS = 32
_N_ORPHANS = _N_PAIRS * 2 + _N_PROPOSALS

_ORPHAN_WALLET = "0x" + "a1" * 20
_LINKED_WALLET = "0x" + "b2" * 20
_NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _clean_env(database_url: str, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DATABASE_URL": database_url,
    }
    env.update(extra)
    return env


def _run_alembic(*args: str, database_url: str, timeout: int = 120, **extra_env: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["alembic", *args],
        cwd=str(_BACKEND_DIR),
        env=_clean_env(database_url, **extra_env),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _down_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(_BACKEND_DIR / "alembic.ini")))
    target = script.get_revision(_REVISION).down_revision
    assert isinstance(target, str), f"expected a single down_revision, got {target!r}"
    return target


def _platform_id() -> str:
    from archimedes.models.account import PLATFORM_LEGACY_USER_ID

    return PLATFORM_LEGACY_USER_ID


# ── Fixture seeding ────────────────────────────────────────────────────────


def _seed(
    db_path: Path,
    *,
    n_pairs: int = _N_PAIRS,
    n_proposals: int = _N_PROPOSALS,
    parentless_passport: bool = False,
    split_mirror: bool = False,
    mixed_case_wallet: bool = False,
) -> None:
    """Seed the schema as it stands at the revision's own ``down_revision``.

    Core inserts against reflected tables (the ``_seed_pre_brief_intent_rows``
    technique) so the fixture tracks real nullability instead of an ORM shape
    that a later revision may have widened.

    Beyond the orphans, five CONTROL rows that must survive untouched — they
    are the whole point of the first test:

      * a store/passport pair whose wallet IS in ``linked_wallets`` — unclaimed
        but claimable, so the existing self-healing reclaim owns it, not us;
      * a curated ``is_example`` row (no ``owner_wallet``);
      * an anonymous row (``owner_wallet`` NULL, not an example);
      * a store/passport pair already owned by a real account;
      * a proposal with no ``owner_wallet``.

    Plus one row in each of five touch-map tables pointing at the first
    orphan, to prove adoption leaves every reference resolvable.
    """
    import sqlalchemy as sa

    engine = sa.create_engine(f"sqlite:///{db_path}")
    meta = sa.MetaData()
    meta.reflect(bind=engine)
    t = meta.tables

    def store_row(sid: str, wallet: str | None, *, owner: str | None = None, example: bool = False) -> dict:
        return {
            "id": sid,
            "content_hash": ("0x" + sid).ljust(66, "0"),
            "generation_method": "curated" if example else "debate",
            "source_papers": "[]",
            "strategy_name": f"Strategy {sid}",
            "thesis": "thesis",
            "asset_universe": "[]",
            "risk_profile": "moderate",
            "status": "candidate",
            "is_example": example,
            "is_published": False,
            "owner_wallet": wallet,
            "owner_user_id": owner,
            "created_at": _NOW,
            "updated_at": _NOW,
        }

    def passport_row(sid: str, wallet: str | None, *, owner: str | None = None) -> dict:
        return {
            "id": sid,
            "generation_method": "debate",
            "methodology_summary": "summary",
            "asset_universe": "[]",
            "position_sizing": "equal_weight",
            "rebalance_frequency": "monthly",
            "status": "candidate",
            "regime_tag": "neutral",
            "passes_rigor_gate": False,
            "owner_wallet": wallet,
            "owner_user_id": owner,
            "created_at": _NOW,
            "updated_at": _NOW,
        }

    def proposal_row(pid: str, wallet: str | None, *, owner: str | None = None) -> dict:
        return {
            "id": pid,
            "generation_id": "gen-1",
            "proposal_id": pid,
            "verdict": "pending",
            "trust_level": "CANDIDATE",
            "content_hash": ("0z" + pid).ljust(66, "0"),
            "agent": "debate",
            "payload": "{}",
            "owner_wallet": wallet,
            "owner_user_id": owner,
            "created_at": _NOW,
            "updated_at": _NOW,
        }

    orphan_wallet = _ORPHAN_WALLET.upper() if mixed_case_wallet else _ORPHAN_WALLET

    with engine.begin() as conn:
        conn.execute(
            t["auth_users"].insert(),
            [
                {
                    "id": "user-real",
                    "name": "Ada",
                    "email": "ada@example.com",
                    "emailVerified": True,
                    "createdAt": _NOW,
                    "updatedAt": _NOW,
                }
            ],
        )
        conn.execute(
            t["wallet_identities"].insert(),
            [
                {"wallet_address": _ORPHAN_WALLET, "actor_class": "human", "first_seen_at": _NOW},
                {"wallet_address": _LINKED_WALLET, "actor_class": "human", "first_seen_at": _NOW},
            ],
        )
        conn.execute(
            t["linked_wallets"].insert(),
            [
                {
                    "id": "lw-1",
                    "user_id": "user-real",
                    "normalized_identity": f"1:{_LINKED_WALLET}",
                    "address": _LINKED_WALLET,
                    "display_address": _LINKED_WALLET,
                    "chain_id": 1,
                    "provider": "siwe",
                    "is_primary": True,
                    "verified_at": _NOW,
                    "created_at": _NOW,
                    "updated_at": _NOW,
                }
            ],
        )

        store_rows = [store_row(f"orph{i:05d}", orphan_wallet) for i in range(n_pairs)]
        passport_rows = [passport_row(f"orph{i:05d}", orphan_wallet) for i in range(n_pairs)]
        if parentless_passport and passport_rows:
            # One candidate passport whose strategy_store parent is missing.
            store_rows.pop()
        if split_mirror and passport_rows:
            # One candidate store row whose passport twin has a real owner.
            passport_rows[0] = passport_row(passport_rows[0]["id"], orphan_wallet, owner="user-real")

        # Controls.
        store_rows += [
            store_row("ctl-linked", _LINKED_WALLET),
            store_row("ctl-curated", None, example=True),
            store_row("ctl-anon", None),
            store_row("ctl-owned", _ORPHAN_WALLET, owner="user-real"),
        ]
        passport_rows += [
            passport_row("ctl-linked", _LINKED_WALLET),
            passport_row("ctl-owned", _ORPHAN_WALLET, owner="user-real"),
        ]
        proposal_rows = [proposal_row(f"prop{i:05d}", orphan_wallet) for i in range(n_proposals)]
        proposal_rows.append(proposal_row("ctl-anon-prop", None))

        conn.execute(t["strategy_store"].insert(), store_rows)
        conn.execute(t["strategy_passports"].insert(), passport_rows)
        if proposal_rows:
            conn.execute(t["strategy_proposals"].insert(), proposal_rows)

        # Touch-map rows pointing at the first orphan strategy / passport.
        sid = "orph00000"
        conn.execute(
            t["backtest_results"].insert(),
            [
                {
                    "strategy_id": sid,
                    "content_hash": "bt-hash",
                    "sharpe_ratio": 1.0,
                    "sortino_ratio": 1.0,
                    "max_drawdown": -0.1,
                    "cagr": 0.1,
                    "calmar_ratio": 1.0,
                    "win_rate": 0.5,
                    "profit_factor": 1.1,
                    "total_trades": 10,
                    "avg_holding_period_days": 5.0,
                    "equity_curve_json": "[]",
                    "monthly_returns_json": "[]",
                    "walk_forward_train_fraction": 0.7,
                    "look_ahead_audit_passed": True,
                    "transaction_cost_bps": 10,
                    "created_at": _NOW,
                    "source_pipeline": "test",
                    "computed_at": _NOW,
                }
            ],
        )
        conn.execute(
            t["debate_transcripts"].insert(),
            [
                {
                    "generation_id": "gen-1",
                    "candidate_id": "cand-1",
                    "strategy_id": sid,
                    "transcript_json": "{}",
                    "created_at": _NOW,
                }
            ],
        )
        conn.execute(
            t["generation_costs"].insert(),
            [
                {
                    "job_id": "job-1",
                    "strategy_id": sid,
                    "schema_version": 1,
                    "measurement_json": "{}",
                    "recorded_at": _NOW,
                }
            ],
        )
        conn.execute(
            t["paper_deployments"].insert(),
            [
                {
                    "id": "pd-1",
                    "strategy_id": sid,
                    "owner_wallet": _ORPHAN_WALLET,
                    "spec_json": "{}",
                    "deployed_at": date(2026, 1, 1),
                    "status": "active",
                    "created_at": _NOW,
                }
            ],
        )
        conn.execute(t["passport_paper_refs"].insert(), [{"passport_id": sid, "title": "A paper"}])

    engine.dispose()


# ── Readers ────────────────────────────────────────────────────────────────


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _scalar(db_path: Path, sql: str, params: tuple = ()) -> int:
    return int(_rows(db_path, sql, params)[0][0])


def _has_table(db_path: Path, table: str) -> bool:
    return bool(_rows(db_path, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def _owned_by_platform(db_path: Path) -> int:
    platform = _platform_id()
    return sum(
        _scalar(db_path, f"SELECT COUNT(*) FROM {table} WHERE owner_user_id = ?", (platform,))
        for table in ("strategy_store", "strategy_passports", "strategy_proposals")
    )


def _prepared(tmp_path: Path, name: str, **seed_kwargs) -> tuple[Path, str]:
    """A SQLite DB upgraded to the revision's down_revision and seeded."""
    db_path = tmp_path / name
    database_url = f"sqlite:///{db_path}"
    pre = _run_alembic("upgrade", _down_revision(), database_url=database_url)
    assert pre.returncode == 0, pre.stderr
    _seed(db_path, **seed_kwargs)
    return db_path, database_url


# ── 1. The happy path ──────────────────────────────────────────────────────


def test_adoption_stamps_every_orphan_and_nothing_else(tmp_path):
    db_path, url = _prepared(tmp_path, "adopt.db")
    platform = _platform_id()

    result = _run_alembic("upgrade", _REVISION, database_url=url)
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

    # Every orphan adopted — 60 + 60 + 32 = 152.
    assert _scalar(db_path, "SELECT COUNT(*) FROM strategy_store WHERE owner_user_id = ?", (platform,)) == _N_PAIRS
    assert _scalar(db_path, "SELECT COUNT(*) FROM strategy_passports WHERE owner_user_id = ?", (platform,)) == _N_PAIRS
    assert (
        _scalar(db_path, "SELECT COUNT(*) FROM strategy_proposals WHERE owner_user_id = ?", (platform,)) == _N_PROPOSALS
    )
    assert _owned_by_platform(db_path) == _N_ORPHANS

    # ...and the ledger records each one, with the wallet preserved.
    assert _scalar(db_path, "SELECT COUNT(*) FROM legacy_row_adoptions") == _N_ORPHANS
    assert (
        _scalar(db_path, "SELECT COUNT(*) FROM legacy_row_adoptions WHERE prior_owner_wallet = ?", (_ORPHAN_WALLET,))
        == _N_ORPHANS
    )

    # The platform account exists, is unverified, and cannot be signed into
    # (no credential row, no session row).
    assert _rows(db_path, "SELECT COUNT(*) FROM auth_users WHERE id = ?", (platform,))[0][0] == 1
    assert _scalar(db_path, 'SELECT COUNT(*) FROM auth_users WHERE id = ? AND "emailVerified" = 1', (platform,)) == 0
    assert _scalar(db_path, 'SELECT COUNT(*) FROM auth_accounts WHERE "userId" = ?', (platform,)) == 0
    assert _scalar(db_path, 'SELECT COUNT(*) FROM auth_sessions WHERE "userId" = ?', (platform,)) == 0

    # CONTROLS — none of these may move.
    for table, row_id in (
        ("strategy_store", "ctl-linked"),
        ("strategy_store", "ctl-curated"),
        ("strategy_store", "ctl-anon"),
        ("strategy_passports", "ctl-linked"),
        ("strategy_proposals", "ctl-anon-prop"),
    ):
        owner = _rows(db_path, f"SELECT owner_user_id FROM {table} WHERE id = ?", (row_id,))[0][0]
        assert owner is None, f"{table}:{row_id} must stay unowned, got {owner!r}"
    for table in ("strategy_store", "strategy_passports"):
        owner = _rows(db_path, f"SELECT owner_user_id FROM {table} WHERE id = 'ctl-owned'")[0][0]
        assert owner == "user-real", "a row with a real owner must never be re-owned"

    # owner_wallet is untouched everywhere — adoption adds an owner, it does
    # not erase provenance.
    assert _scalar(db_path, "SELECT COUNT(*) FROM strategy_store WHERE owner_wallet = ?", (_ORPHAN_WALLET,)) == (
        _N_PAIRS + 1
    )

    # TOUCH MAP — every reference still resolves, and no row was deleted.
    assert _scalar(db_path, "SELECT COUNT(*) FROM backtest_results WHERE strategy_id = 'orph00000'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM debate_transcripts WHERE strategy_id = 'orph00000'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM generation_costs WHERE strategy_id = 'orph00000'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM paper_deployments WHERE strategy_id = 'orph00000'") == 1
    assert _scalar(db_path, "SELECT COUNT(*) FROM passport_paper_refs WHERE passport_id = 'orph00000'") == 1
    assert (
        _scalar(
            db_path,
            "SELECT COUNT(*) FROM paper_deployments pd "
            "LEFT JOIN strategy_store ss ON pd.strategy_id = ss.id WHERE ss.id IS NULL",
        )
        == 0
    )


# ── 2. The band ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("n_pairs", "n_proposals", "label"),
    [(140, 20, "300 rows — far above the band"), (5, 3, "13 rows — far below it")],
)
def test_adoption_refuses_a_population_outside_the_band(tmp_path, n_pairs, n_proposals, label):
    db_path, url = _prepared(tmp_path, "band.db", n_pairs=n_pairs, n_proposals=n_proposals)
    total = n_pairs * 2 + n_proposals

    result = _run_alembic("upgrade", _REVISION, database_url=url)

    assert result.returncode != 0, f"{label}: the migration must refuse, not adopt"
    combined = result.stdout + result.stderr
    assert "REFUSED" in combined
    assert f"found {total} orphaned legacy row" in combined, combined[-2000:]

    # Nothing was written: the transaction rolled back, so there is no
    # platform account, no ledger table, and no stamped revision.
    assert _owned_by_platform(db_path) == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM auth_users WHERE id = ?", (_platform_id(),)) == 0
    assert not _has_table(db_path, "legacy_row_adoptions")
    assert _rows(db_path, "SELECT version_num FROM alembic_version") == [(_down_revision(),)]


# ── 3. The structural checks ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "marker"),
    [
        ({"split_mirror": True}, "D1 split mirror"),
        ({"parentless_passport": True}, "D2 parentless passport"),
        ({"mixed_case_wallet": True}, "D3 non-lowercase owner_wallet"),
    ],
)
def test_adoption_refuses_each_structural_defect(tmp_path, kwargs, marker):
    db_path, url = _prepared(tmp_path, "dangling.db", **kwargs)

    result = _run_alembic("upgrade", _REVISION, database_url=url)

    assert result.returncode != 0, f"{marker} must be refused"
    combined = result.stdout + result.stderr
    assert "REFUSED" in combined and marker in combined, combined[-2000:]
    assert _owned_by_platform(db_path) == 0
    assert not _has_table(db_path, "legacy_row_adoptions")
    assert _rows(db_path, "SELECT version_num FROM alembic_version") == [(_down_revision(),)]


# ── 4. Dry run ─────────────────────────────────────────────────────────────


def test_dry_run_logs_the_plan_and_writes_absolutely_nothing(tmp_path):
    db_path, url = _prepared(tmp_path, "dryrun.db")

    dry = _run_alembic("upgrade", _REVISION, database_url=url, ORPHAN_MIGRATION_DRY_RUN="1")

    # Non-zero by design: aborting the transaction is what guarantees the
    # alembic_version stamp is not written either.
    assert dry.returncode != 0
    combined = dry.stdout + dry.stderr
    assert "DRY RUN" in combined and "would adopt" in combined, combined[-2000:]
    assert "OrphanMigrationDryRun" in combined

    assert _owned_by_platform(db_path) == 0
    assert _scalar(db_path, "SELECT COUNT(*) FROM auth_users WHERE id = ?", (_platform_id(),)) == 0
    assert not _has_table(db_path, "legacy_row_adoptions")
    assert _rows(db_path, "SELECT version_num FROM alembic_version") == [(_down_revision(),)]

    # And the real run afterwards still works — the dry run left no residue.
    wet = _run_alembic("upgrade", _REVISION, database_url=url)
    assert wet.returncode == 0, wet.stderr
    assert _owned_by_platform(db_path) == _N_ORPHANS


def test_dry_run_on_a_zero_population_database_also_writes_absolutely_nothing(tmp_path):
    """The rehearsal branch the owner is most likely to actually hit.

    ``population == 0`` writes the ledger table and returns cleanly — which
    on a dry run means alembic COMMITS and stamps ``alembic_version``,
    marking the revision applied so the real deploy skips it. The rehearsal
    would report success while having silently applied the migration, and the
    PR's promise that a dry run "writes nothing, alembic_version included"
    would be false at the exact moment it matters: concern #1 says prod's
    orphans may be mostly wallet-less, which lands here.

    Mutation check: delete the `_dry_run_requested()` block from the
    `population == 0` branch and this test fails on all three assertions —
    exit 0, the ledger created, and the version stamped to the revision.
    """
    db_path = tmp_path / "dryrun_empty.db"
    url = f"sqlite:///{db_path}"
    pre = _run_alembic("upgrade", _down_revision(), database_url=url)
    assert pre.returncode == 0, pre.stderr
    # Deliberately NOT seeded: zero orphans, the CI / fresh-clone shape.

    dry = _run_alembic("upgrade", _REVISION, database_url=url, ORPHAN_MIGRATION_DRY_RUN="1")

    combined = dry.stdout + dry.stderr
    assert dry.returncode != 0, f"a dry run must abort, not apply:\n{combined[-2000:]}"
    assert "OrphanMigrationDryRun" in combined, combined[-2000:]
    assert "population 0" in combined, combined[-2000:]

    # Nothing written — not the ledger DDL, and above all not the stamp.
    assert not _has_table(db_path, "legacy_row_adoptions")
    assert _rows(db_path, "SELECT version_num FROM alembic_version") == [(_down_revision(),)]
    assert _scalar(db_path, "SELECT COUNT(*) FROM auth_users WHERE id = ?", (_platform_id(),)) == 0

    # The real run afterwards still creates the ledger: the dry run changed
    # the outcome of nothing, it only declined to be that outcome.
    wet = _run_alembic("upgrade", _REVISION, database_url=url)
    assert wet.returncode == 0, wet.stderr
    assert _has_table(db_path, "legacy_row_adoptions")
    assert _rows(db_path, "SELECT version_num FROM alembic_version") == [(_REVISION,)]
    assert _owned_by_platform(db_path) == 0


# ── 5. Idempotency ─────────────────────────────────────────────────────────


def test_running_the_upgrade_a_second_time_is_a_no_op(tmp_path):
    db_path, url = _prepared(tmp_path, "idempotent.db")

    first = _run_alembic("upgrade", _REVISION, database_url=url)
    assert first.returncode == 0, first.stderr
    before = _rows(db_path, "SELECT id, owner_user_id, owner_wallet FROM strategy_store ORDER BY id")

    # `stamp` rewinds the version pointer WITHOUT running downgrade(), so the
    # next upgrade re-executes upgrade() against already-adopted data — the
    # only way to test the second-run path rather than a re-adoption.
    stamp = _run_alembic("stamp", _down_revision(), database_url=url)
    assert stamp.returncode == 0, stamp.stderr

    second = _run_alembic("upgrade", _REVISION, database_url=url)
    assert second.returncode == 0, f"STDOUT:\n{second.stdout}\nSTDERR:\n{second.stderr}"
    assert "second run is a no-op" in (second.stdout + second.stderr)

    assert _rows(db_path, "SELECT id, owner_user_id, owner_wallet FROM strategy_store ORDER BY id") == before
    assert _scalar(db_path, "SELECT COUNT(*) FROM legacy_row_adoptions") == _N_ORPHANS
    assert _scalar(db_path, "SELECT COUNT(*) FROM auth_users WHERE id = ?", (_platform_id(),)) == 1


# ── 6. Downgrade ───────────────────────────────────────────────────────────


def test_downgrade_re_orphans_ownership_and_leaves_everything_else(tmp_path):
    db_path, url = _prepared(tmp_path, "downgrade.db")

    up = _run_alembic("upgrade", _REVISION, database_url=url)
    assert up.returncode == 0, up.stderr
    assert _owned_by_platform(db_path) == _N_ORPHANS

    down = _run_alembic("downgrade", _down_revision(), database_url=url)
    assert down.returncode == 0, f"STDOUT:\n{down.stdout}\nSTDERR:\n{down.stderr}"

    assert _owned_by_platform(db_path) == 0
    assert not _has_table(db_path, "legacy_row_adoptions")
    assert _scalar(db_path, "SELECT COUNT(*) FROM auth_users WHERE id = ?", (_platform_id(),)) == 0
    # The wallets — the thing that makes the rows recoverable — are intact.
    assert _scalar(db_path, "SELECT COUNT(*) FROM strategy_store WHERE owner_wallet = ?", (_ORPHAN_WALLET,)) == (
        _N_PAIRS + 1
    )
    # A real owner set before the migration is still there.
    assert _rows(db_path, "SELECT owner_user_id FROM strategy_store WHERE id = 'ctl-owned'")[0][0] == "user-real"
    # Nothing in the touch map was deleted.
    assert _scalar(db_path, "SELECT COUNT(*) FROM paper_deployments WHERE strategy_id = 'orph00000'") == 1

    # And upgrading again re-adopts cleanly.
    again = _run_alembic("upgrade", _REVISION, database_url=url)
    assert again.returncode == 0, again.stderr
    assert _owned_by_platform(db_path) == _N_ORPHANS
