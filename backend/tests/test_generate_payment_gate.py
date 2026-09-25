"""Route-level contract of the generation payment gate (Dan's directive).

Hermetic — same harness as test_generate_model_gating.py (mocked job store,
pipeline task patched out, signed auth cookies). The paywall module's own laws
live in services/test_generation_payment.py; THIS file pins the route wiring:

  * flag off (the shipped default) → /start behaves exactly as before;
  * flag on + no linked wallet → 409 wallet_link_required (with the faucet
    guidance — #1294's human-only step now bites agents here);
  * flag on + wallet + no payment → 402 quoting via PAYMENT-REQUIRED;
  * dry-run + payment header → 202, the request proceeds to enqueue;
  * the QUOTA runs BEFORE the paywall — a quota-blocked caller is 429'd
    without ever being asked to pay;
  * GET /api/generate/quote is public and matches the 402's quote.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tests.auth_helpers import auth_cookies

RECIPIENT = "0x00000000000000000000000000000000000000a1"

_BODY = {"brief": {"intent": "low-vol treasury alternative", "risk_appetite": "moderate"}}


@pytest.fixture(autouse=True)
def _paid_tier_only(monkeypatch):
    """Switch the #1643 free allowance OFF for this whole file.

    Every assertion here is about the PAID tier — the wallet gate, the 402 —
    which #1643 explicitly leaves untouched from generation #4 onward. Under
    the default allowance of 3 the first three calls in each test would be
    served free, and a ``202`` from the free path is indistinguishable from a
    ``202`` a payment bought: the tests would not fail loudly so much as stop
    measuring the thing they name. ``FREE_GENERATIONS_PER_ACCOUNT=0`` is the
    documented switch that restores the pre-#1643 gate exactly (it also
    short-circuits before the ledger is touched, so this file stays as
    DB-free as it was). The free path has its own file:
    ``test_free_generation_gate.py``.
    """
    monkeypatch.setenv("FREE_GENERATIONS_PER_ACCOUNT", "0")


def _client() -> TestClient:
    from archimedes.main import app

    return TestClient(app)


def _mock_store() -> MagicMock:
    store = MagicMock()
    store.enqueue = AsyncMock(return_value="job-pay")
    return store


def _close_background_coroutine(coro):
    coro.close()
    return MagicMock()


def _harness(store):
    return (
        patch("archimedes.api.generate_routes.get_job_store", return_value=store),
        patch("archimedes.api.generate_routes.asyncio.create_task", side_effect=_close_background_coroutine),
    )


def _payment_header(payer: str) -> str:
    payload = {"payload": {"authorization": {"from": payer, "value": "150000"}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_flag_off_start_behaves_exactly_as_before(monkeypatch) -> None:
    monkeypatch.delenv("GENERATION_PAYMENT_REQUIRED", raising=False)
    store = _mock_store()
    p1, p2 = _harness(store)
    with p1, p2:
        resp = _client().post("/api/generate/start", json=_BODY, cookies=auth_cookies())
    assert resp.status_code == 202
    store.enqueue.assert_awaited_once()


def test_flag_on_without_linked_wallet_is_409_with_faucet_guidance(monkeypatch) -> None:
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    store = _mock_store()
    p1, p2 = _harness(store)
    with (
        p1,
        p2,
        patch("archimedes.api.generate_routes.get_linked_wallet_address", return_value=None),
    ):
        resp = _client().post("/api/generate/start", json=_BODY, cookies=auth_cookies())
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "wallet_link_required"
    assert "faucet" in detail["message"].lower()
    store.enqueue.assert_not_called()


def test_flag_on_without_payment_is_402_with_requirements(monkeypatch) -> None:
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
    store = _mock_store()
    p1, p2 = _harness(store)
    with p1, p2:
        resp = _client().post("/api/generate/start", json=_BODY, cookies=auth_cookies())
    assert resp.status_code == 402
    assert "PAYMENT-REQUIRED" in resp.headers
    detail = resp.json()["detail"]
    assert detail["reason"] == "payment_required"
    # The 402's quote and the public quote endpoint can never disagree.
    quote = _client().get("/api/generate/quote").json()
    assert detail["quote"]["price"] == quote["price"]
    store.enqueue.assert_not_called()


def test_dry_run_payment_header_proceeds_to_enqueue(monkeypatch) -> None:
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "true")
    store = _mock_store()
    p1, p2 = _harness(store)
    cookies = auth_cookies()
    with p1, p2:
        # The conftest legacy adapter links the cookie's wallet; any payer works
        # in dry-run because verify/settle are skipped (module tests pin that).
        resp = _client().post(
            "/api/generate/start",
            json=_BODY,
            cookies=cookies,
            headers={"Payment-Signature": _payment_header("0x" + "ab" * 20)},
        )
    assert resp.status_code == 202
    store.enqueue.assert_awaited_once()


def test_quota_runs_before_the_paywall(monkeypatch) -> None:
    """A quota-blocked caller must be refused 429 BEFORE being asked to pay —
    never pay-then-blocked. TESTING is unset so the quota branch runs; the
    quota itself is stubbed to raise, and the paywall is spied."""
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)

    async def _blocked(request, user_id):
        raise HTTPException(status_code=429, detail="daily cap reached")

    paywall_spy = AsyncMock(side_effect=AssertionError("paywall must not run after a quota block"))
    store = _mock_store()
    p1, p2 = _harness(store)
    with (
        p1,
        p2,
        patch("archimedes.api.generate_routes.enforce_generation_quota", _blocked),
        patch("archimedes.api.generate_routes.generation_payment.enforce_generation_payment", paywall_spy),
    ):
        resp = _client().post("/api/generate/start", json=_BODY, cookies=auth_cookies())
    assert resp.status_code == 429
    paywall_spy.assert_not_called()
    store.enqueue.assert_not_called()


def test_quote_endpoint_is_public_and_reports_the_flag(monkeypatch) -> None:
    monkeypatch.delenv("GENERATION_PAYMENT_REQUIRED", raising=False)
    resp = _client().get("/api/generate/quote")  # no cookies — public by design
    assert resp.status_code == 200
    body = resp.json()
    assert body["payment_required"] is False
    assert body["price"] == "$2.000000"
    assert body["pricing_model"] == "flat_v1"


# ── G8: the paywall × entitlement matrix (test-audit Tranche 4) ──────────────
# The two 402s on /start are distinguishable by SHAPE: the paywall's carries
# the PAYMENT-REQUIRED header and a dict detail (reason="payment_required");
# the entitlement's is a plain-string detail with no payment header. That
# shape difference is what makes the gate ORDER testable end to end:
# quota → wallet link → payment → entitlement → enqueue.

_PREMIUM_MODEL = "us.anthropic.claude-sonnet-4-6"  # mirrors test_model_gating


def _paywalled(monkeypatch, *, dry_run: bool) -> None:
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", RECIPIENT)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "true" if dry_run else "false")


def test_unpaid_entitled_premium_still_gets_the_paywall_402(monkeypatch) -> None:
    """Entitlement does not buy past payment: an ENTITLED premium request with
    no payment is refused by the PAYWALL (dict detail + PAYMENT-REQUIRED
    header), never the entitlement 402."""
    _paywalled(monkeypatch, dry_run=False)
    monkeypatch.setenv("PREMIUM_MODELS_ENABLED", "true")
    store = _mock_store()
    p1, p2 = _harness(store)
    with p1, p2:
        resp = _client().post("/api/generate/start", json={**_BODY, "model": _PREMIUM_MODEL}, cookies=auth_cookies())
    assert resp.status_code == 402
    assert "PAYMENT-REQUIRED" in resp.headers
    assert resp.json()["detail"]["reason"] == "payment_required"
    store.enqueue.assert_not_called()


def test_unpaid_non_entitled_premium_gets_the_paywall_402_not_entitlements(monkeypatch) -> None:
    """Same request minus the entitlement: STILL the paywall's 402. This is the
    ordering detector — if entitlement ran before payment, this response would
    be the plain-string entitlement 402 with no PAYMENT-REQUIRED header."""
    _paywalled(monkeypatch, dry_run=False)
    monkeypatch.delenv("PREMIUM_MODELS_ENABLED", raising=False)
    store = _mock_store()
    p1, p2 = _harness(store)
    with p1, p2:
        resp = _client().post("/api/generate/start", json={**_BODY, "model": _PREMIUM_MODEL}, cookies=auth_cookies())
    assert resp.status_code == 402
    assert "PAYMENT-REQUIRED" in resp.headers
    assert resp.json()["detail"]["reason"] == "payment_required"
    store.enqueue.assert_not_called()


def test_paid_non_entitled_premium_is_refused_by_the_entitlement_gate(monkeypatch) -> None:
    """Payment satisfied, entitlement absent: the ENTITLEMENT 402 — plain-string
    detail, NO payment header, explicit no-downgrade wording. Paying does not
    buy a premium model, and the request is not silently downgraded."""
    _paywalled(monkeypatch, dry_run=True)
    monkeypatch.delenv("PREMIUM_MODELS_ENABLED", raising=False)
    store = _mock_store()
    p1, p2 = _harness(store)
    with p1, p2:
        resp = _client().post(
            "/api/generate/start",
            json={**_BODY, "model": _PREMIUM_MODEL},
            cookies=auth_cookies(),
            headers={"Payment-Signature": _payment_header("0x" + "ab" * 20)},
        )
    assert resp.status_code == 402
    assert "PAYMENT-REQUIRED" not in resp.headers
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "entitlement" in detail
    assert "not downgraded" in detail
    store.enqueue.assert_not_called()


def test_paid_entitled_premium_runs_the_full_gauntlet_to_enqueue(monkeypatch) -> None:
    """The entitled-payer SUCCESS case: payment + entitlement + premium model →
    202, enqueued — and the payload's model provenance is HONEST: premium ids
    are not in the free-tier allowlist, so the recorded model is None (env
    default) until Bedrock activation can actually serve them (B5). A silent
    claim that premium ran would be a false provenance record."""
    _paywalled(monkeypatch, dry_run=True)
    monkeypatch.setenv("PREMIUM_MODELS_ENABLED", "true")
    store = _mock_store()
    p1, p2 = _harness(store)
    with p1, p2:
        resp = _client().post(
            "/api/generate/start",
            json={**_BODY, "model": _PREMIUM_MODEL},
            cookies=auth_cookies(),
            headers={"Payment-Signature": _payment_header("0x" + "ab" * 20)},
        )
    assert resp.status_code == 202
    store.enqueue.assert_awaited_once()
    assert store.enqueue.await_args.kwargs["payload"]["model"] is None
