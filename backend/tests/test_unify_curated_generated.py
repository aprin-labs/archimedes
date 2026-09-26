"""Hermetic tests for the curated ∪ generated "unify source" decouples —
leaderboard and risk now include GENERATED strategies
(StrategyRecord + StrategyPassportRecord) alongside the curated fixtures,
never curated-only (docs/CURATED-STRATEGY-DECOUPLE-AND-CONSOLIDATE-2026-07-08.md
Part A, low-pri decouples: leaderboard, risk endpoints).

The third surface that decouple covered — the chat vault-context builder — is
gone: per-vault chat was removed on 2026-08-31 (see the PR that deleted
`services/chat_service.py`), and its two tests here went with it.

Each surface is exercised against a REAL temp-sqlite DB (the `_use_tmp_db`
pattern from test_strategy_ownership.py) so the actual resolver code runs —
not a mock. A private, non-owned generated strategy must stay hidden on the
gated surfaces (leaderboard, risk); a published/live one must appear with
real (never fabricated) values.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import archimedes.db as db
import pytest
from archimedes.api.auth_siwe import _COOKIE_NAME, _sign_session
from archimedes.services.rigor_gate_version import gate_version
from httpx import ASGITransport, AsyncClient

_W_OWNER = "0xAbC0000000000000000000000000000000000001"
_W_OTHER = "0x0000000000000000000000000000000000000002"


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path, monkeypatch):
    """Point the DB at a FRESH temp sqlite (rebinds db.engine + db.SessionLocal),
    same pattern as test_strategy_ownership.py's `_use_tmp_db`.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{tmp_path / 'unify.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    eng = create_engine(url, connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=eng, autocommit=False, autoflush=False))
    db.init_db()
    yield


def _siwe_cookies(wallet: str) -> dict[str, str]:
    return {_COOKIE_NAME: _sign_session(wallet, time.time())}


def _mk_wallet(wallet: str) -> None:
    from archimedes.services.identity_events import ensure_wallet_identity

    ensure_wallet_identity(wallet, "human")


def _mk_strategy(
    sid: str,
    *,
    owner: str | None = None,
    published: bool = False,
    status: str = "candidate",
    name: str = "Test Strategy",
) -> None:
    from archimedes.models.strategy_store import StrategyRecord

    with db.get_session() as session:
        session.add(
            StrategyRecord(
                id=sid,
                content_hash=("0x" + sid).ljust(66, "0"),
                generation_method="fusion",
                source_papers="[]",
                strategy_name=name,
                thesis="test thesis",
                asset_universe="[]",
                risk_profile="moderate",
                status=status,
                is_example=False,
                owner_wallet=owner.lower() if owner else None,
                is_published=published,
            )
        )
        session.commit()


def _mk_passport(
    sid: str,
    *,
    owner: str | None = None,
    status: str = "candidate",
    passes: bool = True,
    sharpe: float | None = 1.1,
    dsr: float | None = 0.95,
    oos: float | None = 1.0,
    pbo: float | None = 0.1,
    title: str = "Gen Strat",
) -> None:
    from archimedes.models.strategy_passport_record import PassportPaperRef, StrategyPassportRecord

    with db.get_session() as session:
        record = StrategyPassportRecord(
            id=sid,
            content_hash=("0x" + sid).ljust(66, "0"),
            generation_method="fusion",
            methodology_summary="test methodology",
            asset_universe='["SPY", "GLD"]',
            status=status,
            owner_wallet=owner.lower() if owner else None,
            sharpe_ratio=sharpe,
            cagr=0.12,
            max_drawdown=-0.10,
            win_rate=0.55,
            calmar_ratio=1.2,
            correlation_to_spy=0.2,
            deflated_sharpe_ratio=dsr,
            dsr_p_value=dsr,
            pbo_score=pbo,
            out_of_sample_sharpe=oos,
            # The verdict of record, seeded COUPLED — the only shape
            # passport_loader can write (docs/adr/rigor-verdict-of-record.md).
            # Seeding the boolean alone would leave rigor_gate_status at its
            # "pending" default, which every read surface now (correctly) serves
            # as "no gate has graded this" regardless of the boolean beside it.
            passes_rigor_gate=passes,
            rigor_gate_status="pass" if passes else "fail",
            graded_at=datetime(2026, 8, 1, tzinfo=UTC),
            gate_version=gate_version(),
            cohort_n=1,
        )
        record.paper_refs = [PassportPaperRef(passport_id=sid, arxiv_id="2401.00001", title=title)]
        session.add(record)
        session.commit()


# ── Leaderboard: /api/leaderboard ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_leaderboard_includes_published_generated_strategy():
    _mk_wallet(_W_OWNER)
    _mk_strategy("gen-pub-1", owner=_W_OWNER, published=True, status="live")
    _mk_passport("gen-pub-1", owner=_W_OWNER, status="live", passes=True, sharpe=1.4, dsr=0.95, oos=1.2, pbo=0.1)

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/leaderboard")
    assert resp.status_code == 200
    entries = {e["id"]: e for e in resp.json()["entries"]}
    assert "gen-pub-1" in entries
    # Real numbers flow through — never fabricated.
    assert entries["gen-pub-1"]["dsr_p_value"] == 0.95
    assert entries["gen-pub-1"]["passes_rigor_gate"] is True


@pytest.mark.asyncio
async def test_leaderboard_hides_unpublished_generated_strategy():
    """A private, UNPUBLISHED generated strategy never leaks onto the public
    board — even after it PASSES rigor. `upsert_strategy` sets status="live" on
    any rigor-passing row, published or not, so a private-but-live strategy is
    the real leak case: publish (not rigor) is the consent signal (#850).
    """
    _mk_wallet(_W_OWNER)
    # Private (published=False) but rigor-passing (status="live") — the exact leak.
    _mk_strategy("gen-priv-1", owner=_W_OWNER, published=False, status="live")
    _mk_passport("gen-priv-1", owner=_W_OWNER, status="live", passes=True, sharpe=1.5, dsr=0.96, oos=1.3, pbo=0.1)

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/leaderboard")
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()["entries"]]
    assert "gen-priv-1" not in ids


# ── Risk: /api/risk/portfolio ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_risk_portfolio_includes_owned_generated_strategy():
    _mk_wallet(_W_OWNER)
    _mk_strategy("gen-risk-1", owner=_W_OWNER, published=False, status="candidate")
    _mk_passport("gen-risk-1", owner=_W_OWNER, status="candidate", passes=False, sharpe=0.8, dsr=0.5, oos=0.4, pbo=0.3)

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/risk/portfolio", cookies=_siwe_cookies(_W_OWNER))
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()["strategies"]]
    assert "gen-risk-1" in ids


@pytest.mark.asyncio
async def test_risk_portfolio_hides_private_non_owned_generated_strategy():
    _mk_wallet(_W_OWNER)
    _mk_wallet(_W_OTHER)
    _mk_strategy("gen-risk-2", owner=_W_OWNER, published=False, status="candidate")
    _mk_passport("gen-risk-2", owner=_W_OWNER, status="candidate")

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        other = await client.get("/api/risk/portfolio", cookies=_siwe_cookies(_W_OTHER))
        anon = await client.get("/api/risk/portfolio")
    assert other.status_code == 200 and anon.status_code == 200
    assert "gen-risk-2" not in [s["id"] for s in other.json()["strategies"]]
    assert "gen-risk-2" not in [s["id"] for s in anon.json()["strategies"]]


@pytest.mark.asyncio
async def test_risk_greeks_and_cvar_do_not_break_on_generated_strategy():
    """/greeks and /cvar must stay 200 (never a fabricated CVaR — a generated
    strategy with no persisted equity curve is honestly skipped there, same as
    an un-backtested curated one)."""
    _mk_wallet(_W_OWNER)
    _mk_strategy("gen-risk-3", owner=_W_OWNER, published=True, status="live")
    _mk_passport("gen-risk-3", owner=_W_OWNER, status="live", sharpe=0.9)

    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        greeks = await client.get("/api/risk/greeks", cookies=_siwe_cookies(_W_OWNER))
        cvar = await client.get("/api/risk/cvar", cookies=_siwe_cookies(_W_OWNER))
    assert greeks.status_code == 200
    assert cvar.status_code == 200
    greek_ids = [g["strategy_id"] for g in greeks.json()["strategies"]]
    assert "gen-risk-3" in greek_ids
