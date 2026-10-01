"""Pytest fixtures for backend unit tests.

Uses the real LocalStrategyProvider pointed at the analytics-engine/strategies/
directory so tests exercise the actual strategy files rather than mocks.
"""

# IMPORTANT: set TESTING env var BEFORE any archimedes imports so that
# the rate limiter (api/limiter.py) reads it at module init time.
import atexit
import os
import shutil
import tempfile

os.environ["TESTING"] = "1"

# ── Hermetic default database (issue #1640) ──────────────────────────────────
# Same "before any archimedes import" reason as TESTING above, for a defect with
# much longer teeth.
#
# `archimedes.db` resolves DATABASE_URL exactly ONCE, at import time, and builds
# the process-global `engine` from it. With the env var unset its default is
# `_default_database_url()` → `backend/archimedes_chat.db` — a file INSIDE the
# working tree. That file is untracked, is created by `init_db()` (which runs at
# `archimedes.main` import time), survives every pytest run, and is shared with
# whatever else the developer has done in that directory: `uvicorn
# archimedes.main:app`, a `scripts/` run, an interrupted suite. The FastAPI
# lifespan's step 2 (`seed_from_manifest()`) writes all 10,000 rows of
# `data/corpus/manifest.jsonl` into it.
#
# The consequence is a suite whose result depends on the directory's history:
# `strategy_fusion.load_corpus()` reads the DB before the file, so once that
# table is populated, 12 corpus tests across `test_strategy_fusion.py`,
# `test_debate_engine.py` and `test_papers_routes.py` read the real 10K corpus
# instead of their 4-row fixtures (`assert 10000 == 4`) — permanently, in that
# directory, until someone deletes a file they have no reason to know about.
# The same tests pass in a fresh worktree. `test_corpus_embedding_claims.py`'s
# module docstring records an earlier encounter with the same leak.
#
# Pointing the unset-DATABASE_URL default at a throwaway temp file makes every
# run start from the fresh-worktree state by construction. `setdefault`, not an
# unconditional set: the two `@pytest.mark.integration` tests that want a real
# Postgres pass `DATABASE_URL` explicitly and must keep winning.
#
# `tempfile.mkdtemp` rather than `tmp_path_factory`: this has to happen at
# conftest *import* time, before `archimedes.db` is imported and freezes its
# engine — no fixture, session-scoped or otherwise, runs early enough. It is
# also per-process, so the file is shared across the run exactly as the in-tree
# one was; only its starting contents change (empty, like a fresh worktree).
# `tests/db_isolation.redirect_to_tmp_sqlite` remains the right tool for
# per-test isolation and layers on top of this unchanged.
#
# The remaining leak is that same per-process file: a TestClient / ASGI
# lifespan still runs `seed_from_manifest()` into it (~18k rows). Tests that
# call `load_corpus()` with `path=None` and expect the file fallback
# (`test_loader_env_override`, `test_empty_db_falls_back_to_file_manifest`)
# must isolate with `isolated_empty_sqlite`. Do not "fix" that by making
# `ARCHIMEDES_CORPUS_MANIFEST` a production DB bypass.
if not os.environ.get("DATABASE_URL"):
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="archimedes-test-db-")
    atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_TEST_DB_DIR, 'archimedes_test.db')}"

from pathlib import Path

import pytest
from archimedes.services.strategy_provider import LocalStrategyProvider


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the monorepo root (two levels up from backend/)."""
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def strategies_dir(repo_root: Path) -> Path:
    return repo_root / "analytics-engine" / "strategies"


@pytest.fixture(scope="session")
def provider(strategies_dir: Path) -> LocalStrategyProvider:
    return LocalStrategyProvider(strategies_dir)


# ─── Quant-lane synthetic-data fixtures ──────────────────────────────
# Reusable deterministic factories for rigor / optimizer / fusion / backtester
# tests. The underlying functions live in quant_factories.py and can also be
# imported directly when a test needs custom parameters.

from tests import quant_factories  # noqa: E402


@pytest.fixture
def synthetic_returns():
    """Factory fixture: build a daily return series with a target Sharpe.

    Usage:  rets = synthetic_returns(annual_sharpe=1.2, n=504, seed=7)
    """
    return quant_factories.make_returns


@pytest.fixture
def price_panel():
    """Factory fixture: build (close_panel, volume_panel) DataFrames.

    Usage:  close, vol = price_panel(["SPY", "AGG"], n=300, correlation=0.4)
    """
    return quant_factories.make_price_panel


@pytest.fixture
def returns_matrix():
    """Factory fixture: build a {strategy_id: daily returns} matrix.

    Usage:  m = returns_matrix(n_strategies=8, n=504)
    """
    return quant_factories.make_returns_matrix


@pytest.fixture
def regime_shift_returns():
    """Factory fixture: (market, strategy) returns with a mid-series vol regime shift."""
    return quant_factories.make_regime_shift_returns


@pytest.fixture(autouse=True)
def _legacy_siwe_test_adapter(monkeypatch):
    """Map legacy SIWE test cookies onto canonical test users.

    Production never enables this adapter. Existing route tests keep exercising
    ownership with cryptographically signed fixture cookies while dedicated
    account-auth tests cover Better Auth session resolution directly.
    """
    from datetime import UTC, datetime, timedelta

    from archimedes.api import (
        account_auth,
        auth_siwe,
        generate_routes,
        proposals_routes,
        strategies_routes,
        wallet_routes,
    )

    def legacy_wallet(request):
        token = request.cookies.get(auth_siwe._COOKIE_NAME)
        return auth_siwe._verify_session(token) if token else None

    async def legacy_session(request):
        wallet = legacy_wallet(request)
        if wallet is None:
            return None
        return {
            "user": {
                "id": f"legacy-test:{wallet}",
                "name": "Legacy test user",
                "email": f"{wallet[2:10]}@legacy.test",
                "emailVerified": True,
            },
            "session": {"expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        }

    monkeypatch.setattr(account_auth, "_SESSION_COOKIE_FRAGMENT", "session")
    monkeypatch.setattr(account_auth, "_fetch_session", legacy_session)
    monkeypatch.setattr(wallet_routes, "get_linked_wallet_address", legacy_wallet)
    monkeypatch.setattr(generate_routes, "get_linked_wallet_address", legacy_wallet)
    monkeypatch.setattr(proposals_routes, "get_linked_wallet_address", legacy_wallet)
    monkeypatch.setattr(strategies_routes, "get_linked_wallet_address", legacy_wallet)


@pytest.fixture(autouse=True)
def _clear_cohort_returns_cache():
    """Reset the process-level cohort-returns memo (#1713).

    Same hazard as ``_clear_rigor_cache``: several files reuse the SAME
    strategy ids against DIFFERENT per-test SQLite databases. A cached
    series from test A served to test B would make a live-read assertion
    pass against yesterday's rows. Cleared before and after.
    """
    from archimedes.services.backtest_repository import clear_cohort_returns_cache

    clear_cohort_returns_cache()
    yield
    clear_cohort_returns_cache()


@pytest.fixture(autouse=True)
def _clear_rigor_cache():
    """Reset the process-level live-rigor-gate cache (services/rigor_cache.py)
    around every test.

    That cache is a module-global dict keyed on a data-version token
    (strategy ids + a fingerprint of their returns). Several test files reuse
    the SAME real curated strategy ids (via the session-scoped ``provider`` /
    ``default_provider()``) with the SAME synthetic returns fixtures across
    different test functions — without this reset, a cache entry populated by
    one test could be silently served to a later, unrelated test in the same
    pytest process, making that test's assertions (e.g. call-count spies on
    ``run_rigor_gate``) order-dependent on whatever ran before it. Clearing
    before AND after keeps every test's view of the cache empty regardless of
    suite order, matching the hermetic-test mandate (no hidden environmental
    state leaking between tests).
    """
    from archimedes.services import rigor_cache

    rigor_cache.clear()
    yield
    rigor_cache.clear()


@pytest.fixture(autouse=True)
def _clear_vault_owner_cache():
    """Reset the trace-ownership memo (services/trace_visibility.py) — #1573.

    Same hazard as ``_clear_rigor_cache`` above, one layer down: the vault →
    owner memo is a module-global dict keyed on the vault address, and several
    test files reuse the SAME vault addresses against DIFFERENT per-test
    SQLite databases. Without this reset, "vault X is owned by user Y" (or,
    worse, "vault X is unowned") learned in one test would be served to a
    later test whose database says otherwise, making an ownership *verdict*
    depend on suite order. Cleared before and after, like its sibling.
    """
    from archimedes.services.trace_visibility import clear_vault_owner_cache

    clear_vault_owner_cache()
    yield
    clear_vault_owner_cache()


@pytest.fixture(autouse=True)
def _clear_health_probe_cache():
    """Reset /health's last-known-value memo (services/health_cache.py) — #1592.

    Third instance of the same hazard as the two fixtures above, and the one
    with the sharpest teeth: this memo exists precisely so a timed-out probe can
    serve a previous reading. Without a reset, a test that establishes
    "chain_connected = True" hands that value to a later test whose whole point
    is that the probe times out — the later test would then assert against a
    ``stale_cached`` outcome it never set up, and, worse, a broken fallback path
    could pass by inheriting a neighbour's success. Cleared before and after.
    """
    from archimedes.services.health_cache import clear_health_probe_cache

    clear_health_probe_cache()
    yield
    clear_health_probe_cache()
