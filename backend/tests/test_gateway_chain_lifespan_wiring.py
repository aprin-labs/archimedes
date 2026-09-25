"""End-to-end wiring proof for #1240's GATEWAY_CHAIN/RPC startup assertion.

``test_startup_assertions.py`` calls ``_assert_gateway_chain_matches_rpc``
directly and proves the function is correct in isolation. Nothing in that
file (or elsewhere) actually drives it through ``main.lifespan``'s real
call site — the exact place two review findings on #1240 landed:

  1. The call site's ``except RuntimeError: raise`` re-raised ANY
     RuntimeError, not just a confirmed chain mismatch — including one
     surfacing from ``chain_client.get_chain_id()`` itself for a purely
     connectivity reason, aborting boot even under PAYMENTS_DRY_RUN=true.
     Fixed by giving the mismatch a dedicated type, ``GatewayChainMismatch``,
     and re-raising only that.
  2. ``payments_dry_run`` used to be assigned INSIDE step 3's try block,
     after two statements that can themselves raise (the MarketService
     import, ``int(os.getenv("AGENT_INTERVAL_SECONDS", ...))``). If either
     raised first, step 3y's reference to ``payments_dry_run`` two blocks
     down raised a bare ``NameError`` — caught by that block's own
     ``except Exception`` and logged as "connectivity issue?", silently
     no-op'ing the whole guard. Fixed by reading it once, before step 3's
     try block, so it is unconditionally bound going into step 3y.

These tests drive ``archimedes.main.lifespan`` directly (not through the
ASGI/TestClient stack — lifespan is a plain ``@asynccontextmanager``, so
``async with main_module.lifespan(app):`` is the real function, no
transport layer needed) against a tmp-sqlite DB (pattern:
``tests/db_isolation.py``, used across the suite) and a fake chain client,
proving the actual module-level wiring aborts/survives correctly — not just
the isolated function.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from archimedes import main as main_module

from tests.db_isolation import redirect_to_tmp_sqlite


class _FakeChainClient:
    """Stand-in for archimedes.chain.client.chain_client at the 3y import
    site only (``from archimedes.chain.client import chain_client`` inside
    the function body re-resolves the module attribute at call time, so
    patching ``archimedes.chain.client.chain_client`` intercepts exactly
    that lookup regardless of what any other module already imported)."""

    def __init__(self, chain_id: int | None = None, raise_exc: BaseException | None = None):
        self._chain_id = chain_id
        self._raise_exc = raise_exc
        # Touched (only if reached) by step 3z's controlled-wallet registration.
        self.settings = SimpleNamespace(agent_account=None)

    async def get_chain_id(self) -> int:
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._chain_id is not None
        return self._chain_id


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path):
    """Real (if empty) sqlite DB so the corpus-seed / rehydration steps that
    run before and after 3y succeed or fail-soft normally instead of hitting
    a missing table — same fixture every DB-touching test file in this suite
    uses (tests/db_isolation.py)."""
    yield from redirect_to_tmp_sqlite(tmp_path)


def _fake_app() -> SimpleNamespace:
    """lifespan() only ever touches `_app.state.*` — a bare object with a
    mutable `.state` namespace is the whole ASGI surface it needs."""
    return SimpleNamespace(state=SimpleNamespace())


@pytest.mark.asyncio
async def test_lifespan_aborts_on_a_real_chain_mismatch(monkeypatch):
    """The guard demonstration CLAUDE.md asks for: build the input that
    SHOULD fail the guard, run it through the real wiring, confirm it fails.
    """
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
    monkeypatch.setenv("GATEWAY_CHAIN", "arcTestnet")  # resolves to chain_id 5042002
    fake = _FakeChainClient(chain_id=1)  # RPC reports a DIFFERENT chain

    with (
        patch("archimedes.chain.client.chain_client", fake),
        pytest.raises(main_module.GatewayChainMismatch, match="resolves to chain_id=5042002"),
    ):
        async with main_module.lifespan(_fake_app()):
            pass  # must never be reached


@pytest.mark.asyncio
async def test_lifespan_survives_a_connectivity_runtimeerror_under_dry_run(monkeypatch):
    """Adversarial companion + the actual review regression: get_chain_id()
    raising a plain RuntimeError (e.g. a closed aiohttp session) is a
    connectivity failure, not a chain mismatch, and must NOT abort boot —
    proves the call site's except clause discriminates by type correctly,
    not just that the isolated function does."""
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "true")
    monkeypatch.setenv("GATEWAY_CHAIN", "arcTestnet")
    fake = _FakeChainClient(raise_exc=RuntimeError("Session is closed"))

    with patch("archimedes.chain.client.chain_client", fake):
        async with main_module.lifespan(_fake_app()):
            pass  # reaching here IS the assertion: startup completed


@pytest.mark.asyncio
async def test_lifespan_survives_a_connectivity_runtimeerror_even_live(monkeypatch):
    """Same connectivity failure, but PAYMENTS_DRY_RUN=false this time — a
    RuntimeError from get_chain_id() must fall through to the warning branch
    regardless of the dry-run flag; only a confirmed mismatch is
    dry-run-conditioned. This is the case the old `except RuntimeError:
    raise` got wrong unconditionally."""
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
    monkeypatch.setenv("GATEWAY_CHAIN", "arcTestnet")
    fake = _FakeChainClient(raise_exc=RuntimeError("Session is closed"))

    with patch("archimedes.chain.client.chain_client", fake):
        async with main_module.lifespan(_fake_app()):
            pass  # reaching here IS the assertion: startup completed


@pytest.mark.asyncio
async def test_lifespan_binds_payments_dry_run_even_if_step3_raises_early(monkeypatch):
    """Regression for the review finding: force step 3 (MarketService
    startup) to raise BEFORE the line that used to assign
    payments_dry_run — a non-numeric AGENT_INTERVAL_SECONDS blows up
    int(os.getenv(...)) first. Before the fix this left payments_dry_run
    unbound, step 3y's NameError got swallowed as "connectivity issue?",
    and the mismatch guard silently no-op'd. After the fix the guard still
    fires because payments_dry_run is read before step 3's try block."""
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
    monkeypatch.setenv("GATEWAY_CHAIN", "arcTestnet")
    monkeypatch.setenv("AGENT_INTERVAL_SECONDS", "not-a-number")  # int() raises ValueError
    fake = _FakeChainClient(chain_id=1)  # mismatched vs arcTestnet's 5042002

    app = _fake_app()
    with (
        patch("archimedes.chain.client.chain_client", fake),
        pytest.raises(main_module.GatewayChainMismatch),
    ):
        async with main_module.lifespan(app):
            pass  # must never be reached

    # Confirms step 3 really did fail early, as the test intends — this
    # isn't accidentally passing because MarketService construction itself
    # already fails in this test environment for an unrelated reason.
    assert app.state.market is None
