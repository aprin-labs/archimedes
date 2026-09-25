"""A settled generation payment always buys something (#1441).

The paywall settled the charge and enqueued the job afterwards, with the
premium-model entitlement gate sitting between them. Anything that failed in
that window — the gate raising 402, the enqueue erroring — and anything that
failed after it — worker crash, LLM failure, container roll, the payer
cancelling — kept the money with nothing delivered and nothing recording that
the payer was owed a generation.

A settled payment now buys a durable *credit*, and only a job that reaches the
queue spends it. A run that does not deliver hands the credit back.

Covers:

  1. the ledger: claim/settle/consume/restore transitions, the idempotency key
     as a unique claim, and oldest-first draining;
  2. the window this issue is about: enqueue failure and entitlement-gate 402
     after a settled charge both leave a spendable credit;
  3. the payoff: the payer's NEXT attempt spends that credit and is not
     charged again;
  4. idempotency: a retry against an in-flight key is refused rather than
     settling a second EIP-3009 authorization;
  5. inertness: under flag-off and under dry-run the ledger writes nothing at
     all, so this is a no-op in production until the payment stack is flipped.

Hermetic: tmp-file SQLite (``redirect_to_tmp_sqlite``), mocked job store and
backgrounded task — the harness from ``test_payment_receipts.py``. No live
Redis, Postgres or Circle facilitator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.api import generate_routes
from archimedes.db import get_session
from archimedes.models.generation_credit import (
    CREDIT_AVAILABLE,
    CREDIT_CONSUMED,
    CREDIT_PENDING,
    CREDIT_VOID,
    GenerationCreditRecord,
    claim_credit,
    consume_credit,
    mark_credit_settled,
    restore_credit_for_job,
    take_available_credit,
    void_credit,
)
from archimedes.services import generation_payment
from fastapi.testclient import TestClient

from tests.auth_helpers import auth_cookies
from tests.db_isolation import redirect_to_tmp_sqlite

RECIPIENT = "0x00000000000000000000000000000000000000a1"
PAYER_A = "0x" + "12" * 20  # matches tests.auth_helpers.TEST_WALLET
USER = "user-a"

_BODY = {"brief": {"intent": "low-vol treasury alternative", "risk_appetite": "moderate"}}


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path):
    yield from redirect_to_tmp_sqlite(tmp_path)


@pytest.fixture(autouse=True)
def _paid_tier_only(monkeypatch):
    """Switch the #1643 free allowance OFF for this whole file.

    Every assertion here is about what happens once money moves, which #1643
    leaves untouched from generation #4 onward. Under the default allowance of
    3 the first three calls in each test would be served free and no credit
    would ever be claimed — the tests would not fail loudly so much as stop
    measuring the ledger they name. ``FREE_GENERATIONS_PER_ACCOUNT=0`` is the
    documented switch that restores the pre-#1643 gate exactly. The free path
    has its own file: ``test_free_generation_gate.py``, which includes the
    paid-tier-after-exhaustion case under the real default allowance.
    """
    monkeypatch.setenv("FREE_GENERATIONS_PER_ACCOUNT", "0")


@dataclass
class _FakePaymentInfo:
    """Mirrors circlekit.x402.PaymentInfo exactly; ``amount`` is raw base units."""

    verified: bool = True
    payer: str = PAYER_A
    amount: str = "2000000"  # $2.00 at USDC's 6 decimals
    network: str = "eip155:5042002"
    transaction: str | None = "831aaaf1-f110-47f7-8faf-c76aa8f841cb"
    response_headers: dict = field(default_factory=dict)


def _client() -> TestClient:
    from archimedes.main import app

    return TestClient(app)


def _mock_store(job_id: str = "job-credit") -> MagicMock:
    store = MagicMock()
    store.enqueue = AsyncMock(return_value=job_id)
    return store


def _close_background_coroutine(coro):
    coro.close()
    return MagicMock()


def _harness(store):
    return (
        patch("archimedes.api.generate_routes.get_job_store", return_value=store),
        patch(
            "archimedes.api.generate_routes.asyncio.create_task",
            side_effect=_close_background_coroutine,
        ),
    )


def _paywall_on(monkeypatch) -> AsyncMock:
    """Flag on, dry-run off, and a settle that succeeds. Returns the mock so a
    test can assert how many times a charge was actually taken."""
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
    monkeypatch.delenv("GENERATION_PAYMENTS_DRY_RUN", raising=False)
    monkeypatch.delenv("PAYMENTS_HALT", raising=False)  # hermetic: #1240 must not leak in from a shell/.env
    settle = AsyncMock(return_value=_FakePaymentInfo())
    monkeypatch.setattr(generation_payment, "enforce_generation_payment", settle)
    return settle


def _route_owner_id(settle) -> str:
    """The owner id ``/api/generate/start`` writes credits under.

    Derived by running one real paid generation and reading the row back,
    rather than hard-coding a literal that would silently stop matching if the
    route's identity source ever changed. The credit it creates is consumed by
    that run, so it does not affect a later ``take_credit``.
    """
    store_patch, task_patch = _harness(_mock_store("job-probe"))
    with store_patch, task_patch, _client() as client:
        resp = client.post("/api/generate/start", json=_BODY, cookies=auth_cookies())
    assert resp.status_code == 202, resp.text
    rows = _credits()
    assert len(rows) == 1 and rows[0].status == CREDIT_CONSUMED
    return rows[0].user_id


def _credits() -> list[GenerationCreditRecord]:
    with get_session() as session:
        return list(session.query(GenerationCreditRecord).order_by(GenerationCreditRecord.id).all())


# ── 1. The ledger ────────────────────────────────────────────────────────


class TestLedgerTransitions:
    def test_a_fresh_claim_is_pending_and_carries_no_payment_yet(self):
        with get_session() as session:
            outcome, credit = claim_credit(session, user_id=USER, idempotency_key="k1")
            session.commit()
            assert outcome == "claimed"
            assert credit.status == CREDIT_PENDING
            assert credit.settlement_ref is None
            assert credit.amount_base_units is None

    def test_settle_then_consume_then_restore(self):
        with get_session() as session:
            _, credit = claim_credit(session, user_id=USER, idempotency_key=None)
            mark_credit_settled(
                session,
                credit.id,
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="eip155:5042002",
                settlement_ref="ref-1",
            )
            assert credit.status == CREDIT_AVAILABLE
            assert credit.settled_at is not None

            consume_credit(session, credit.id, job_id="job-1")
            assert credit.status == CREDIT_CONSUMED
            assert credit.consumed_at is not None

            restore_credit_for_job(session, "job-1")
            session.commit()
            assert credit.status == CREDIT_AVAILABLE
            assert credit.consumed_at is None
            # job_id is deliberately kept — the ledger should show which run
            # burned and handed the credit back, not rewind to a blank row.
            assert credit.job_id == "job-1"

    def test_restoring_twice_cannot_mint_a_second_credit(self):
        """A duplicated terminal-state callback must not double-credit."""
        with get_session() as session:
            _, credit = claim_credit(session, user_id=USER, idempotency_key=None)
            mark_credit_settled(
                session,
                credit.id,
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="net",
                settlement_ref="ref",
            )
            consume_credit(session, credit.id, job_id="job-1")
            assert restore_credit_for_job(session, "job-1") is not None
            assert restore_credit_for_job(session, "job-1") is None
            session.commit()
        assert len(_credits()) == 1

    def test_only_an_available_credit_can_be_spent(self):
        with get_session() as session:
            _, credit = claim_credit(session, user_id=USER, idempotency_key=None)
            session.commit()
            # Still pending — the money has not moved, so there is nothing to spend.
            assert consume_credit(session, credit.id, job_id="job-1") is None

    def test_voiding_cannot_destroy_a_credit_the_payer_already_bought(self):
        """``void`` releases an unstarted claim. It must never erase money taken.

        No current caller reaches ``void_credit`` with a settled credit — both
        call sites hold a ``pending`` one. This pins the guard that keeps that
        true, because the failure it prevents is unrecoverable: the charge
        cleared, and the only record that the payer is owed anything is this row.
        """
        with get_session() as session:
            for terminal, expected in (("settle", CREDIT_AVAILABLE), ("consume", CREDIT_CONSUMED)):
                _, credit = claim_credit(session, user_id=USER, idempotency_key=None)
                mark_credit_settled(
                    session,
                    credit.id,
                    payer_wallet=PAYER_A,
                    amount_base_units=2_000_000,
                    price_usd="$2.00",
                    network="net",
                    settlement_ref=f"ref-{terminal}",
                )
                if terminal == "consume":
                    consume_credit(session, credit.id, job_id=f"job-{terminal}")
                void_credit(session, credit.id)
                session.commit()
                assert credit.status == expected, f"void() destroyed a {expected} credit"

    def test_credits_drain_oldest_first(self):
        with get_session() as session:
            ids = []
            for ref in ("ref-1", "ref-2"):
                _, credit = claim_credit(session, user_id=USER, idempotency_key=None)
                mark_credit_settled(
                    session,
                    credit.id,
                    payer_wallet=PAYER_A,
                    amount_base_units=2_000_000,
                    price_usd="$2.00",
                    network="net",
                    settlement_ref=ref,
                )
                ids.append(credit.id)
            session.commit()
            assert take_available_credit(session, USER).id == ids[0]

    def test_credits_are_owner_scoped(self):
        with get_session() as session:
            _, credit = claim_credit(session, user_id=USER, idempotency_key=None)
            mark_credit_settled(
                session,
                credit.id,
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="net",
                settlement_ref="ref",
            )
            session.commit()
            assert take_available_credit(session, "someone-else") is None


class TestIdempotencyKeyClaim:
    def test_a_second_claim_on_a_live_key_reads_in_flight(self):
        with get_session() as session:
            claim_credit(session, user_id=USER, idempotency_key="k1")
            session.commit()
            outcome, _ = claim_credit(session, user_id=USER, idempotency_key="k1")
            assert outcome == "in_flight"

    def test_a_key_whose_credit_is_unspent_reads_already_settled(self):
        with get_session() as session:
            _, credit = claim_credit(session, user_id=USER, idempotency_key="k1")
            mark_credit_settled(
                session,
                credit.id,
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="net",
                settlement_ref="ref",
            )
            session.commit()
            assert claim_credit(session, user_id=USER, idempotency_key="k1")[0] == "already_settled"

    def test_a_key_whose_credit_was_spent_reads_already_consumed(self):
        with get_session() as session:
            _, credit = claim_credit(session, user_id=USER, idempotency_key="k1")
            mark_credit_settled(
                session,
                credit.id,
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="net",
                settlement_ref="ref",
            )
            consume_credit(session, credit.id, job_id="job-1")
            session.commit()
            assert claim_credit(session, user_id=USER, idempotency_key="k1")[0] == "already_consumed"

    def test_a_voided_key_becomes_reusable(self):
        """A claim that never became a charge must not lock the key forever."""
        with get_session() as session:
            _, credit = claim_credit(session, user_id=USER, idempotency_key="k1")
            void_credit(session, credit.id)
            session.commit()
            assert credit.status == CREDIT_VOID
            outcome, again = claim_credit(session, user_id=USER, idempotency_key="k1")
            session.commit()
            assert outcome == "claimed"
            assert again.id == credit.id
        assert len(_credits()) == 1

    def test_keyless_claims_do_not_collide(self):
        """NULL keys are distinct under UNIQUE, so a client that sends none can
        still hold several credits."""
        with get_session() as session:
            claim_credit(session, user_id=USER, idempotency_key=None)
            claim_credit(session, user_id=USER, idempotency_key=None)
            session.commit()
        assert len(_credits()) == 2


# ── 2. The window the issue is about ─────────────────────────────────────


class TestMoneyTakenIsAlwaysAccountedFor:
    def test_enqueue_failure_leaves_a_spendable_credit(self, monkeypatch):
        """The charge cleared; the job never queued. The payer keeps a credit."""
        _paywall_on(monkeypatch)
        store = _mock_store()
        store.enqueue = AsyncMock(side_effect=RuntimeError("redis is down"))
        store_patch, task_patch = _harness(store)
        with store_patch, task_patch, _client() as client, pytest.raises(RuntimeError):
            client.post("/api/generate/start", json=_BODY, cookies=auth_cookies())

        rows = _credits()
        assert len(rows) == 1
        assert rows[0].status == CREDIT_AVAILABLE
        assert rows[0].settlement_ref == "831aaaf1-f110-47f7-8faf-c76aa8f841cb"
        assert rows[0].job_id is None

    def test_entitlement_gate_402_after_settling_leaves_a_spendable_credit(self, monkeypatch):
        """The sharpest window: pay, then get 402'd for a premium model."""
        from fastapi import HTTPException

        _paywall_on(monkeypatch)
        monkeypatch.setattr(
            generate_routes,
            "enforce_model_entitlement",
            MagicMock(side_effect=HTTPException(status_code=402, detail="premium not entitled")),
        )
        store_patch, task_patch = _harness(_mock_store())
        with store_patch, task_patch, _client() as client:
            resp = client.post("/api/generate/start", json=_BODY, cookies=auth_cookies())
        assert resp.status_code == 402

        rows = _credits()
        assert len(rows) == 1
        assert rows[0].status == CREDIT_AVAILABLE

    def test_the_next_attempt_spends_the_credit_and_is_not_charged_again(self, monkeypatch):
        """The payoff. This is what makes the credit worth anything."""
        settle = _paywall_on(monkeypatch)
        store = _mock_store()
        store.enqueue = AsyncMock(side_effect=RuntimeError("redis is down"))
        store_patch, task_patch = _harness(store)
        with store_patch, task_patch, _client() as client, pytest.raises(RuntimeError):
            client.post("/api/generate/start", json=_BODY, cookies=auth_cookies())
        assert settle.await_count == 1

        # Second attempt, everything healthy.
        store2 = _mock_store("job-second")
        store_patch2, task_patch2 = _harness(store2)
        with store_patch2, task_patch2, _client() as client:
            resp = client.post("/api/generate/start", json=_BODY, cookies=auth_cookies())
        assert resp.status_code == 202

        # No second charge was taken...
        assert settle.await_count == 1
        # ...and the one credit was spent on the run that actually started.
        rows = _credits()
        assert len(rows) == 1
        assert rows[0].status == CREDIT_CONSUMED
        assert rows[0].job_id == "job-second"

    def test_a_delivered_generation_spends_its_credit(self, monkeypatch):
        _paywall_on(monkeypatch)
        store_patch, task_patch = _harness(_mock_store("job-ok"))
        with store_patch, task_patch, _client() as client:
            assert client.post("/api/generate/start", json=_BODY, cookies=auth_cookies()).status_code == 202
        rows = _credits()
        assert len(rows) == 1
        assert rows[0].status == CREDIT_CONSUMED
        assert rows[0].job_id == "job-ok"


class TestIdempotencyOverTheRoute:
    def test_a_retry_against_an_in_flight_key_is_refused_not_charged(self, monkeypatch):
        """x402 is not crash-retry-idempotent: a retry signs a FRESH EIP-3009
        authorization, so an unguarded retry settles a second real payment."""
        settle = _paywall_on(monkeypatch)

        # The route derives its own owner id from the session, so learn it from
        # a real request rather than asserting against a guessed literal.
        owner = _route_owner_id(settle)
        with get_session() as session:
            claim_credit(session, user_id=owner, idempotency_key="retry-me")
            session.commit()
        charges_before = settle.await_count

        store_patch, task_patch = _harness(_mock_store())
        with store_patch, task_patch, _client() as client:
            resp = client.post(
                "/api/generate/start",
                json=_BODY,
                cookies=auth_cookies(),
                headers={"Idempotency-Key": "retry-me"},
            )
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "payment_in_flight"
        assert settle.await_count == charges_before  # no second charge

    def test_a_spent_key_is_refused_without_a_second_charge(self, monkeypatch):
        """End to end: the same key twice buys one generation, not two."""
        settle = _paywall_on(monkeypatch)
        store_patch, task_patch = _harness(_mock_store("job-first"))
        with store_patch, task_patch, _client() as client:
            first = client.post(
                "/api/generate/start",
                json=_BODY,
                cookies=auth_cookies(),
                headers={"Idempotency-Key": "spent"},
            )
        assert first.status_code == 202
        assert settle.await_count == 1

        store_patch2, task_patch2 = _harness(_mock_store("job-second"))
        with store_patch2, task_patch2, _client() as client:
            resp = client.post(
                "/api/generate/start",
                json=_BODY,
                cookies=auth_cookies(),
                headers={"Idempotency-Key": "spent"},
            )
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason"] == "idempotency_key_already_used"
        assert settle.await_count == 1  # the retry took no money
        assert len(_credits()) == 1

    def test_a_402_releases_the_claim_so_the_key_stays_usable(self, monkeypatch):
        """An unpaid first attempt must not lock the caller's key forever."""
        from fastapi import HTTPException

        monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
        monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
        monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
        monkeypatch.delenv("GENERATION_PAYMENTS_DRY_RUN", raising=False)
        monkeypatch.setattr(
            generation_payment,
            "enforce_generation_payment",
            AsyncMock(side_effect=HTTPException(status_code=402, detail="pay up")),
        )
        store_patch, task_patch = _harness(_mock_store())
        with store_patch, task_patch, _client() as client:
            resp = client.post(
                "/api/generate/start",
                json=_BODY,
                cookies=auth_cookies(),
                headers={"Idempotency-Key": "unpaid"},
            )
        assert resp.status_code == 402
        rows = _credits()
        assert len(rows) == 1
        assert rows[0].status == CREDIT_VOID


# ── 3. Job outcomes ──────────────────────────────────────────────────────


class TestCreditFollowsDelivery:
    async def _release(self, job_id: str, status: str | None):
        store = MagicMock()
        store.get = AsyncMock(return_value=None if status is None else {"status": status})
        await generate_routes._release_credit_if_undelivered(job_id, store)

    def _consumed_credit(self, job_id: str) -> int:
        with get_session() as session:
            _, credit = claim_credit(session, user_id=USER, idempotency_key=None)
            mark_credit_settled(
                session,
                credit.id,
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="net",
                settlement_ref="ref",
            )
            consume_credit(session, credit.id, job_id=job_id)
            session.commit()
            return credit.id

    @pytest.mark.parametrize("status", ["error", "failed", "cancelled", "stalled", None])
    async def test_a_run_that_did_not_deliver_hands_the_credit_back(self, status):
        self._consumed_credit("job-x")
        await self._release("job-x", status)
        assert _credits()[0].status == CREDIT_AVAILABLE

    async def test_a_delivered_run_keeps_the_credit_spent(self):
        self._consumed_credit("job-x")
        await self._release("job-x", "done")
        assert _credits()[0].status == CREDIT_CONSUMED


# ── 4. Inert until the payment stack is flipped on ───────────────────────


class TestLedgerIsInertWithoutRealValue:
    def test_dry_run_writes_no_credit(self, monkeypatch):
        monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
        monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
        monkeypatch.setenv("PAYMENTS_DRY_RUN", "true")
        monkeypatch.delenv("GENERATION_PAYMENTS_DRY_RUN", raising=False)
        monkeypatch.setattr(generation_payment, "enforce_generation_payment", AsyncMock(return_value=None))
        store_patch, task_patch = _harness(_mock_store())
        with store_patch, task_patch, _client() as client:
            assert client.post("/api/generate/start", json=_BODY, cookies=auth_cookies()).status_code == 202
        assert _credits() == []

    def test_flag_off_writes_no_credit(self, monkeypatch):
        monkeypatch.delenv("GENERATION_PAYMENT_REQUIRED", raising=False)
        store_patch, task_patch = _harness(_mock_store())
        with store_patch, task_patch, _client() as client:
            assert client.post("/api/generate/start", json=_BODY, cookies=auth_cookies()).status_code == 202
        assert _credits() == []


# ── 5. The credit and the receipt describe one payment ───────────────────


def test_credit_and_receipt_agree_on_the_price(monkeypatch):
    """Two tables render the same settled amount; they must not disagree."""
    from archimedes.models.payment_receipt import PaymentReceiptRecord

    _paywall_on(monkeypatch)
    store_patch, task_patch = _harness(_mock_store("job-price"))
    with store_patch, task_patch, _client() as client:
        assert client.post("/api/generate/start", json=_BODY, cookies=auth_cookies()).status_code == 202

    with get_session() as session:
        receipt = session.query(PaymentReceiptRecord).one()
        credit = session.query(GenerationCreditRecord).one()
        assert credit.price_usd == receipt.price_usd == "$2.00"
        assert credit.amount_base_units == receipt.amount_base_units
        assert credit.settlement_ref == receipt.settlement_ref


# ── 6. The #1240 kill switch meets the ledger ────────────────────────────


class TestPaymentsHaltAndTheLedger:
    """PAYMENTS_HALT (#1240) stops money moving. The ledger records money that
    already moved, so it is deliberately NOT gated — this class is where that
    reasoning is checked rather than asserted in a comment.

    Unlike the rest of this file these tests run the REAL
    ``enforce_generation_payment``, because the interaction under test is
    exactly the one between the paywall's 503 and ``_paywall_with_credit``'s
    claim/void bookkeeping.
    """

    @staticmethod
    def _live(monkeypatch) -> None:
        monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
        monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
        monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
        monkeypatch.delenv("GENERATION_PAYMENTS_DRY_RUN", raising=False)
        monkeypatch.delenv("PAYMENTS_HALT", raising=False)

    def test_halt_refuses_the_paid_start_and_leaves_no_pending_claim(self, monkeypatch):
        """503, and — the part that matters — no PENDING row survives.

        A claim left pending reads as ``in_flight`` forever and locks that
        Idempotency-Key out permanently. ``_paywall_with_credit``'s
        ``except BaseException: void(credit_id)`` catches the 503 the same way
        it catches a 402, so the halt window costs the payer nothing.
        """
        self._live(monkeypatch)
        monkeypatch.setenv("PAYMENTS_HALT", "true")
        store_patch, task_patch = _harness(_mock_store())
        with store_patch, task_patch, _client() as client:
            resp = client.post(
                "/api/generate/start",
                json=_BODY,
                cookies=auth_cookies(),
                headers={"Idempotency-Key": "halted-key", "Payment-Signature": "any-value"},
            )
        assert resp.status_code == 503
        assert resp.json()["detail"]["reason"] == "payments_halted"
        rows = _credits()
        assert [r.status for r in rows] == [CREDIT_VOID]
        assert CREDIT_PENDING not in {r.status for r in rows}

    def test_the_same_key_works_once_the_halt_is_lifted(self, monkeypatch):
        """Adversarial companion AND the payoff of the void above: identical
        request, identical key, switch off — 202. Proves the 503 came from the
        switch and that it burned nothing on the way out."""
        self._live(monkeypatch)
        monkeypatch.setenv("PAYMENTS_HALT", "true")
        store_patch, task_patch = _harness(_mock_store())
        with store_patch, task_patch, _client() as client:
            assert (
                client.post(
                    "/api/generate/start",
                    json=_BODY,
                    cookies=auth_cookies(),
                    headers={"Idempotency-Key": "reused-key", "Payment-Signature": "any-value"},
                ).status_code
                == 503
            )

        monkeypatch.delenv("PAYMENTS_HALT", raising=False)
        settle = AsyncMock(return_value=_FakePaymentInfo())
        monkeypatch.setattr(generation_payment, "enforce_generation_payment", settle)
        store_patch, task_patch = _harness(_mock_store("job-after-halt"))
        with store_patch, task_patch, _client() as client:
            resp = client.post(
                "/api/generate/start",
                json=_BODY,
                cookies=auth_cookies(),
                headers={"Idempotency-Key": "reused-key", "Payment-Signature": "any-value"},
            )
        assert resp.status_code == 202, resp.text
        assert settle.await_count == 1
        assert CREDIT_CONSUMED in {r.status for r in _credits()}

    def test_an_already_bought_credit_still_delivers_while_halted(self, monkeypatch):
        """The reason ``consume`` is not gated. This payer's money moved before
        the switch was flipped; spending the credit they already own moves
        nothing and takes no Circle call. Freezing the ledger would withhold a
        generation that is already paid for — the opposite of what a kill switch
        is for."""
        self._live(monkeypatch)
        settle = AsyncMock(return_value=_FakePaymentInfo())
        monkeypatch.setattr(generation_payment, "enforce_generation_payment", settle)
        owner = _route_owner_id(settle)

        with get_session() as session:
            _, credit = claim_credit(session, user_id=owner, idempotency_key=None)
            mark_credit_settled(
                session,
                credit.id,
                payer_wallet=PAYER_A,
                amount_base_units=2_000_000,
                price_usd="$2.00",
                network="eip155:5042002",
                settlement_ref="paid-before-the-halt",
            )
            session.commit()
            credit_id = credit.id

        # Now halt, and use the REAL paywall — if the run reached it, it would 503.
        monkeypatch.setenv("PAYMENTS_HALT", "true")
        monkeypatch.setattr(generation_payment, "enforce_generation_payment", _refuse_if_reached)
        store_patch, task_patch = _harness(_mock_store("job-on-credit"))
        with store_patch, task_patch, _client() as client:
            resp = client.post("/api/generate/start", json=_BODY, cookies=auth_cookies())
        assert resp.status_code == 202, resp.text

        with get_session() as session:
            spent = session.get(GenerationCreditRecord, credit_id)
            assert spent.status == CREDIT_CONSUMED
            assert spent.job_id == "job-on-credit"


async def _refuse_if_reached(*_args, **_kwargs):
    raise AssertionError("the paywall must not be reached when an unspent credit covers the run")
