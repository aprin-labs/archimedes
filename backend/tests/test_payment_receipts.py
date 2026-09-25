"""``payment_receipts`` — persistence + list surface for settled generation
payments (Dan's directive, 2026-08-21: "we must provide people with their
receipts").

Covers:

  1. the model: round-trip write/read, newest-first ordering, owner-scoped
     list (another user's receipts are invisible — their list is empty), and
     refusal on a missing identity field;
  2. the route: a settled payment on ``POST /api/generate/start`` produces a
     receipt the SAME payer can read back via ``GET /api/payments/receipts``
     — and it is invisible to a different payer;
  3. auth: the receipts endpoint requires a Better Auth session, and a
     flag-off/dry-run request (no settled ``PaymentInfo``) writes nothing;
  4. FAIL-SAFE (ADVERSARIAL): a receipt-write failure must never fail or
     delay the paid generation. Demonstrated by patching the exact
     persistence boundary (``generate_routes._write_payment_receipt``) to
     raise — the ``/start`` response is still 202, and no receipt row lands.

Hermetic: tmp-file SQLite (``redirect_to_tmp_sqlite`` — the ``_use_tmp_db``
precedent from ``test_api_routes.py``), mocked job store + backgrounded task
(mirrors ``test_generate_payment_gate.py``'s harness), no live Redis/
Postgres/Circle facilitator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.db import get_session
from archimedes.models.payment_receipt import (
    PaymentReceiptRecord,
    list_payment_receipts,
    record_payment_receipt,
)
from archimedes.services import generation_payment
from fastapi.testclient import TestClient

from tests.auth_helpers import auth_cookies
from tests.db_isolation import redirect_to_tmp_sqlite

RECIPIENT = "0x00000000000000000000000000000000000000a1"
PAYER_A = "0x" + "12" * 20  # matches tests.auth_helpers.TEST_WALLET
PAYER_B = "0x" + "34" * 20

_BODY = {"brief": {"intent": "low-vol treasury alternative", "risk_appetite": "moderate"}}


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture(autouse=True)
def _paid_tier_only(monkeypatch):
    """Switch the #1643 free allowance OFF for this whole file.

    A receipt exists only when a payment settled, and #1643 leaves the paid
    tier untouched from generation #4 onward. Under the default allowance of 3
    the calls below would be served free and write no receipt at all.
    ``FREE_GENERATIONS_PER_ACCOUNT=0`` restores the pre-#1643 gate exactly;
    the free path is covered in ``test_free_generation_gate.py``.
    """
    monkeypatch.setenv("FREE_GENERATIONS_PER_ACCOUNT", "0")


def _client() -> TestClient:
    from archimedes.main import app

    return TestClient(app)


def _mock_store(job_id: str = "job-pay") -> MagicMock:
    store = MagicMock()
    store.enqueue = AsyncMock(return_value=job_id)
    return store


def _close_background_coroutine(coro):
    coro.close()
    return MagicMock()


def _harness(store):
    return (
        patch("archimedes.api.generate_routes.get_job_store", return_value=store),
        patch("archimedes.api.generate_routes.asyncio.create_task", side_effect=_close_background_coroutine),
    )


@dataclass
class _FakePaymentInfo:
    """Mirrors circlekit.x402.PaymentInfo's fields exactly: verified / payer /
    amount / network / transaction / response_headers. ``amount`` is raw base
    units as a string (server.py's ``_build_payment_info``), never a dollar
    figure."""

    verified: bool = True
    payer: str = PAYER_A
    amount: str = "2000000"  # $2.00 at USDC's 6 decimals
    network: str = "eip155:5042002"
    transaction: str | None = "831aaaf1-f110-47f7-8faf-c76aa8f841cb"
    response_headers: dict = field(default_factory=dict)


def _settled(monkeypatch, *, payer: str = PAYER_A) -> None:
    """Configure the paywall as ON and make ``enforce_generation_payment``
    return a real settled ``PaymentInfo`` — the ONLY case a receipt is
    written. Patches the module attribute directly (not via string dotted
    path) so the same object ``generate_routes.py`` calls is affected."""
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
    monkeypatch.setattr(
        generation_payment, "enforce_generation_payment", AsyncMock(return_value=_FakePaymentInfo(payer=payer))
    )


# ── 1. The model ─────────────────────────────────────────────────────────


class TestRecordPaymentReceipt:
    def test_round_trips(self):
        with get_session() as session:
            record_payment_receipt(
                session,
                user_id="user-a",
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="eip155:5042002",
                settlement_ref="ref-123",
                job_id="job-1",
            )
            session.commit()
            rows = list_payment_receipts(session, "user-a")
        assert len(rows) == 1
        row = rows[0]
        assert row["price_usd"] == "$2.00"
        assert row["amount_base_units"] == 2_000_000
        assert row["payer_wallet"] == PAYER_A
        assert row["settlement_ref"] == "ref-123"
        assert row["job_id"] == "job-1"
        assert row["network"] == "eip155:5042002"
        assert row["created_at"]

    def test_owner_scoped_another_users_list_is_empty(self):
        with get_session() as session:
            record_payment_receipt(
                session,
                user_id="user-a",
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="eip155:5042002",
                settlement_ref="ref-a",
                job_id="job-a",
            )
            session.commit()
            assert list_payment_receipts(session, "user-b") == []
            assert len(list_payment_receipts(session, "user-a")) == 1

    def test_newest_first(self):
        with get_session() as session:
            record_payment_receipt(
                session,
                user_id="user-a",
                payer_wallet=PAYER_A,
                amount_base_units=1,
                price_usd="$0.000001",
                network="n",
                settlement_ref="old",
                job_id="job-old",
            )
            record_payment_receipt(
                session,
                user_id="user-a",
                payer_wallet=PAYER_A,
                amount_base_units=2,
                price_usd="$0.000002",
                network="n",
                settlement_ref="new",
                job_id="job-new",
            )
            session.commit()
            rows = list_payment_receipts(session, "user-a")
            capped = list_payment_receipts(session, "user-a", limit=1)
        assert [r["settlement_ref"] for r in rows] == ["new", "old"]
        assert [r["settlement_ref"] for r in capped] == ["new"]

    def test_missing_identity_is_refused(self):
        with get_session() as session:
            with pytest.raises(ValueError, match="user_id"):
                record_payment_receipt(
                    session,
                    user_id="",
                    payer_wallet=PAYER_A,
                    amount_base_units=1,
                    price_usd="$0.00",
                    network="n",
                    settlement_ref=None,
                )
            with pytest.raises(ValueError, match="payer_wallet"):
                record_payment_receipt(
                    session,
                    user_id="user-a",
                    payer_wallet="",
                    amount_base_units=1,
                    price_usd="$0.00",
                    network="n",
                    settlement_ref=None,
                )
            assert session.query(PaymentReceiptRecord).count() == 0


# ── 2 & 3. The route: settle -> persist -> the SAME payer reads it back ──


def test_a_settled_payment_produces_a_receipt_the_payer_can_read_back(monkeypatch):
    _settled(monkeypatch)
    store = _mock_store("job-receipt-1")
    p1, p2 = _harness(store)
    cookies = auth_cookies(PAYER_A)
    with p1, p2:
        resp = _client().post("/api/generate/start", json=_BODY, cookies=cookies)
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job-receipt-1"

    list_resp = _client().get("/api/payments/receipts", cookies=cookies)
    assert list_resp.status_code == 200
    receipts = list_resp.json()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["job_id"] == "job-receipt-1"
    assert receipt["amount_base_units"] == 2_000_000
    assert receipt["price_usd"] == "$2.00"
    assert receipt["payer_wallet"] == PAYER_A
    assert receipt["settlement_ref"] == "831aaaf1-f110-47f7-8faf-c76aa8f841cb"
    assert receipt["network"] == "eip155:5042002"
    assert receipt["created_at"]


def test_another_users_receipts_list_does_not_see_this_payment(monkeypatch):
    _settled(monkeypatch)
    store = _mock_store("job-receipt-2")
    p1, p2 = _harness(store)
    with p1, p2:
        resp = _client().post("/api/generate/start", json=_BODY, cookies=auth_cookies(PAYER_A))
    assert resp.status_code == 202

    other_resp = _client().get("/api/payments/receipts", cookies=auth_cookies(PAYER_B))
    assert other_resp.status_code == 200
    assert other_resp.json() == []


def test_receipts_endpoint_requires_a_session():
    resp = _client().get("/api/payments/receipts")
    assert resp.status_code == 401


def test_flag_off_writes_no_receipt(monkeypatch):
    """No settled PaymentInfo (flag off) -> `payment` stays None -> nothing
    is ever written. Mirrors test_generate_payment_gate's flag-off case."""
    monkeypatch.delenv("GENERATION_PAYMENT_REQUIRED", raising=False)
    store = _mock_store("job-no-pay")
    p1, p2 = _harness(store)
    cookies = auth_cookies(PAYER_A)
    with p1, p2:
        resp = _client().post("/api/generate/start", json=_BODY, cookies=cookies)
    assert resp.status_code == 202

    list_resp = _client().get("/api/payments/receipts", cookies=cookies)
    assert list_resp.json() == []


# ── 4. FAIL-SAFE — the adversarial demonstration ──────────────────────────


def test_a_receipt_write_failure_never_fails_the_paid_generation(monkeypatch):
    """The input that SHOULD break a naive implementation: the persistence
    boundary raises. The user already paid — /start must still succeed, and
    no receipt is written (the failure is swallowed, not silently forged)."""
    _settled(monkeypatch)
    store = _mock_store("job-boom")
    p1, p2 = _harness(store)
    cookies = auth_cookies(PAYER_A)
    with (
        p1,
        p2,
        patch(
            "archimedes.api.generate_routes._write_payment_receipt",
            side_effect=RuntimeError("database is on fire"),
        ),
    ):
        resp = _client().post("/api/generate/start", json=_BODY, cookies=cookies)
    assert resp.status_code == 202
    store.enqueue.assert_awaited_once()

    list_resp = _client().get("/api/payments/receipts", cookies=cookies)
    assert list_resp.status_code == 200
    assert list_resp.json() == []
