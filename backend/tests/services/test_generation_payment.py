"""The generation paywall's laws (Dan's directive 2026-08-19).

Hermetic — the facilitator boundary (``middleware.verify``/``settle``) is
mocked; ``require()`` and header decoding run REAL circlekit code (both are
local computation). Payment-Signature headers are crafted as base64 JSON,
which is exactly circlekit's wire encoding.

Pinned laws:
  1. Flag off → inert (returns None, no exception) — today's behavior.
  2. Flag on + no recipient → 503 outage, NEVER a free pass.
  3. No payment header → 402 whose PAYMENT-REQUIRED header and JSON quote both
     carry the price — the quote endpoint and the 402 can never disagree.
  4. PAYMENTS_DRY_RUN → header accepted WITHOUT verify/settle, loudly.
  5. The payer inside the signed authorization must BE the linked wallet.
  6. verify-invalid and settle-failure are honest 402s, not 500s.
  7. Price parsing fails safe to the default.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.services import generation_payment as gp

RECIPIENT = "0x00000000000000000000000000000000000000a1"
WALLET = "0xAbCd000000000000000000000000000000000001"


def _payment_header(payer: str = WALLET) -> str:
    payload = {
        "accepted": {"network": "eip155:5042002"},
        "payload": {"authorization": {"from": payer, "value": "150000"}},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _request_with(header: str | None):
    request = MagicMock()
    request.headers = {"Payment-Signature": header} if header else {}
    return request


def _arm(monkeypatch, *, dry_run: bool = False, recipient: str = RECIPIENT):
    monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", "true")
    monkeypatch.setenv("GENERATION_PAYMENT_RECIPIENT", recipient)
    monkeypatch.setenv("PAYMENTS_DRY_RUN", "true" if dry_run else "false")
    monkeypatch.delenv("GENERATION_PRICE_USD", raising=False)
    # Hermetic: GENERATION_PAYMENTS_DRY_RUN (the 2026-08-20 split) OVERRIDES
    # the global when set, and PAYMENTS_HALT (#1240) short-circuits the live
    # path entirely — a developer's shell or .env carrying either would move
    # every test in this file off the branch it means to exercise. Tests that
    # want them set do so explicitly, after _arm.
    monkeypatch.delenv("GENERATION_PAYMENTS_DRY_RUN", raising=False)
    monkeypatch.delenv("PAYMENTS_HALT", raising=False)


class TestFlagAndConfig:
    @pytest.mark.asyncio
    async def test_flag_off_is_inert(self, monkeypatch):
        monkeypatch.delenv("GENERATION_PAYMENT_REQUIRED", raising=False)
        assert await gp.enforce_generation_payment(_request_with(None), WALLET) is None
        # Strict parse: near-misses stay off.
        for value in ("1", "TRUE", "yes"):
            monkeypatch.setenv("GENERATION_PAYMENT_REQUIRED", value)
            assert gp.payment_required() is False

    @pytest.mark.asyncio
    async def test_flag_on_without_recipient_is_an_outage_not_a_free_pass(self, monkeypatch):
        _arm(monkeypatch, recipient="")
        with pytest.raises(Exception) as excinfo:
            await gp.enforce_generation_payment(_request_with(_payment_header()), WALLET)
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail["reason"] == "payment_config_missing"


class TestQuoteAnd402:
    @pytest.mark.asyncio
    async def test_missing_header_402_carries_requirements_and_matching_quote(self, monkeypatch):
        _arm(monkeypatch)
        with pytest.raises(Exception) as excinfo:
            await gp.enforce_generation_payment(_request_with(None), WALLET)
        exc = excinfo.value
        assert exc.status_code == 402
        assert exc.detail["reason"] == "payment_required"
        # The machine path: real circlekit requirements in the header.
        assert "PAYMENT-REQUIRED" in exc.headers
        decoded = json.loads(base64.b64decode(exc.headers["PAYMENT-REQUIRED"]))
        assert decoded  # non-empty requirements document
        # The human/agent path: the same quote the public endpoint serves.
        assert exc.detail["quote"]["price"] == "$2.000000"
        assert exc.detail["quote"] == gp.quote()

    def test_price_env_overrides_and_fails_safe(self, monkeypatch):
        monkeypatch.setenv("GENERATION_PRICE_USD", "0.42")
        assert gp.quote()["price"] == "$0.420000"
        monkeypatch.setenv("GENERATION_PRICE_USD", "not-a-number")
        assert gp.quote()["price"] == "$2.000000"  # default, never free/absurd
        monkeypatch.setenv("GENERATION_PRICE_USD", "-3")
        assert gp.quote()["price"] == "$2.000000"


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_accepts_header_without_verify_or_settle(self, monkeypatch, caplog):
        _arm(monkeypatch, dry_run=True)
        middleware = MagicMock()
        middleware.require = MagicMock(return_value={"headers": {"PAYMENT-REQUIRED": "req"}, "body": {}})
        middleware.verify = AsyncMock(side_effect=AssertionError("verify must not run in dry-run"))
        middleware.settle = AsyncMock(side_effect=AssertionError("settle must not run in dry-run"))
        with (
            patch("archimedes.marketplace.payments.get_gateway_middleware", return_value=middleware),
            caplog.at_level("WARNING"),
        ):
            result = await gp.enforce_generation_payment(_request_with(_payment_header()), WALLET)
        assert result is None
        assert "UNVERIFIED" in caplog.text  # loud, never mistakable for revenue
        middleware.verify.assert_not_called()
        middleware.settle.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_still_quotes_when_no_header(self, monkeypatch):
        """The approval UX is exercised end to end even in dry-run."""
        _arm(monkeypatch, dry_run=True)
        with pytest.raises(Exception) as excinfo:
            await gp.enforce_generation_payment(_request_with(None), WALLET)
        assert excinfo.value.status_code == 402


class TestLivePayment:
    def _middleware(self, *, valid: bool = True, settle_error: str | None = None):
        middleware = MagicMock()
        middleware.require = MagicMock(return_value={"headers": {"PAYMENT-REQUIRED": "req"}, "body": {}})
        verify_result = MagicMock()
        verify_result.is_valid = valid
        verify_result.invalid_reason = None if valid else "insufficient balance"
        middleware.verify = AsyncMock(return_value=verify_result)
        if settle_error:
            middleware.settle = AsyncMock(side_effect=ValueError(settle_error))
        else:
            payment = MagicMock()
            payment.payer = WALLET
            payment.amount = "150000"
            payment.transaction = "0xsettled"
            payment.response_headers = {"PAYMENT-RESPONSE": "receipt"}
            middleware.settle = AsyncMock(return_value=payment)
        return middleware

    @pytest.mark.asyncio
    async def test_payer_must_be_the_linked_wallet(self, monkeypatch):
        _arm(monkeypatch)
        middleware = self._middleware()
        with (
            patch("archimedes.marketplace.payments.get_gateway_middleware", return_value=middleware),
            pytest.raises(Exception) as excinfo,
        ):
            await gp.enforce_generation_payment(
                _request_with(_payment_header(payer="0x9999999999999999999999999999999999999999")),
                WALLET,
            )
        assert excinfo.value.status_code == 402
        assert excinfo.value.detail["reason"] == "payer_mismatch"
        middleware.verify.assert_not_called()  # rejected before any facilitator spend

    @pytest.mark.asyncio
    async def test_case_insensitive_payer_match_settles_and_returns_receipt(self, monkeypatch):
        _arm(monkeypatch)
        middleware = self._middleware()
        with patch("archimedes.marketplace.payments.get_gateway_middleware", return_value=middleware):
            result = await gp.enforce_generation_payment(
                _request_with(_payment_header(payer=WALLET.upper().replace("0X", "0x"))),
                WALLET.lower(),
            )
        assert result is not None
        assert result.transaction == "0xsettled"
        middleware.verify.assert_awaited_once()
        middleware.settle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verify_invalid_is_an_honest_402(self, monkeypatch):
        _arm(monkeypatch)
        with (
            patch(
                "archimedes.marketplace.payments.get_gateway_middleware",
                return_value=self._middleware(valid=False),
            ),
            pytest.raises(Exception) as excinfo,
        ):
            await gp.enforce_generation_payment(_request_with(_payment_header()), WALLET)
        assert excinfo.value.status_code == 402
        assert excinfo.value.detail["reason"] == "payment_invalid"
        assert "insufficient balance" in excinfo.value.detail["message"]

    @pytest.mark.asyncio
    async def test_settle_failure_is_an_honest_402_not_a_500(self, monkeypatch):
        _arm(monkeypatch)
        with (
            patch(
                "archimedes.marketplace.payments.get_gateway_middleware",
                return_value=self._middleware(settle_error="facilitator timeout"),
            ),
            pytest.raises(Exception) as excinfo,
        ):
            await gp.enforce_generation_payment(_request_with(_payment_header()), WALLET)
        assert excinfo.value.status_code == 402
        assert excinfo.value.detail["reason"] == "payment_settle_failed"

    @pytest.mark.asyncio
    async def test_malformed_header_is_a_402_not_a_500(self, monkeypatch):
        _arm(monkeypatch)
        with pytest.raises(Exception) as excinfo:
            await gp.enforce_generation_payment(_request_with("not-base64!!"), WALLET)
        assert excinfo.value.status_code == 402
        assert excinfo.value.detail["reason"] == "payment_malformed"


class TestPaymentsHalt:
    """#1240 kill switch. This is the one surface the mainnet scope decision
    allows PAYMENTS_DRY_RUN=false on, so it is the surface that most needs a
    stop lever that doesn't require a redeploy. Distinct from PAYMENTS_DRY_RUN
    (a separate switch, re-read fresh here too) so an operator can flip it
    without touching the money-scope decision recorded in PAYMENTS_DRY_RUN.

    Unlike the marketplace tick-charging rail, a halt here REFUSES service
    (503) rather than accepting the header unverified — this is a metered
    pay-per-call API with no pre-existing subscriber relationship, so
    "no real charge" must not mean "serve it anyway" (review finding on the
    original PR: the prior shape gave the paid product away for free AND
    dropped the payer-binding check for the whole halt window)."""

    @pytest.mark.asyncio
    async def test_halt_refuses_service_without_verify_or_settle(self, monkeypatch, caplog):
        _arm(monkeypatch, dry_run=False)
        monkeypatch.setenv("PAYMENTS_HALT", "true")
        middleware = MagicMock()
        middleware.require = MagicMock(return_value={"headers": {"PAYMENT-REQUIRED": "req"}, "body": {}})
        middleware.verify = AsyncMock(side_effect=AssertionError("verify must not run while halted"))
        middleware.settle = AsyncMock(side_effect=AssertionError("settle must not run while halted"))
        with (
            patch("archimedes.marketplace.payments.get_gateway_middleware", return_value=middleware),
            caplog.at_level("WARNING"),
        ):
            with pytest.raises(Exception) as excinfo:
                await gp.enforce_generation_payment(_request_with(_payment_header()), WALLET)
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail["reason"] == "payments_halted"
        assert "PAYMENTS_HALT" in caplog.text
        middleware.verify.assert_not_called()
        middleware.settle.assert_not_called()

    @pytest.mark.asyncio
    async def test_halt_true_rejects_even_a_malformed_or_foreign_header(self, monkeypatch, caplog):
        """The finding's exact concern: while halted, ANY Payment-Signature
        value must be rejected, not accepted as a free pass — including one
        that would otherwise fail payer-binding or decode entirely."""
        _arm(monkeypatch, dry_run=False)
        monkeypatch.setenv("PAYMENTS_HALT", "true")
        with caplog.at_level("WARNING"):
            with pytest.raises(Exception) as excinfo:
                await gp.enforce_generation_payment(_request_with("not-valid-base64-json!!!"), WALLET)
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail["reason"] == "payments_halted"

    @pytest.mark.asyncio
    async def test_halt_false_reaches_the_facilitator(self, monkeypatch):
        """Adversarial companion: same live config with PAYMENTS_HALT unset
        DOES reach verify/settle — proves the test above exercises a real
        short-circuit rather than some unrelated always-off path."""
        _arm(monkeypatch, dry_run=False)
        monkeypatch.delenv("PAYMENTS_HALT", raising=False)
        middleware = MagicMock()
        middleware.require = MagicMock(return_value={"headers": {"PAYMENT-REQUIRED": "req"}, "body": {}})
        verify_result = MagicMock(is_valid=True)
        middleware.verify = AsyncMock(return_value=verify_result)
        payment = MagicMock(payer=WALLET, amount="150000", transaction="0xsettled")
        middleware.settle = AsyncMock(return_value=payment)
        with patch("archimedes.marketplace.payments.get_gateway_middleware", return_value=middleware):
            result = await gp.enforce_generation_payment(_request_with(_payment_header()), WALLET)
        assert result is not None
        middleware.verify.assert_awaited_once()
        middleware.settle.assert_awaited_once()

    def test_quote_reports_halted_state(self, monkeypatch):
        monkeypatch.delenv("PAYMENTS_HALT", raising=False)
        assert gp.quote()["halted"] is False
        monkeypatch.setenv("PAYMENTS_HALT", "true")
        assert gp.quote()["halted"] is True

    @pytest.mark.asyncio
    async def test_no_header_while_halted_is_still_the_402_quote(self, monkeypatch):
        """Pinning a deliberate ordering choice, so it cannot drift silently.

        The halt check sits AFTER the no-header 402, so a caller who presents
        nothing still gets the quote rather than a 503. That is safe — building
        the PAYMENT-REQUIRED header is local circlekit computation, no Circle
        round-trip and no value moved — and the quote embedded in the body
        carries ``halted: true``, so the response is not lying about the state.
        The alternative (503 before quoting) is arguably friendlier UX; it is a
        reviewer's call, not an accident, and this test is where to change it."""
        _arm(monkeypatch, dry_run=False)
        monkeypatch.setenv("PAYMENTS_HALT", "true")
        with pytest.raises(Exception) as excinfo:
            await gp.enforce_generation_payment(_request_with(None), WALLET)
        assert excinfo.value.status_code == 402
        assert excinfo.value.detail["quote"]["halted"] is True

    @pytest.mark.asyncio
    async def test_dry_run_wins_over_halt(self, monkeypatch):
        """Ordering the other way round: under dry-run nothing can move, so the
        halt branch is unreachable and the dry-run accept stands. Pinned because
        the two switches are adjacent and swapping them would turn every
        dry-run deploy that happens to carry PAYMENTS_HALT into a 503 outage."""
        _arm(monkeypatch, dry_run=True)
        monkeypatch.setenv("PAYMENTS_HALT", "true")
        assert await gp.enforce_generation_payment(_request_with(_payment_header()), WALLET) is None


class TestGenerationDryRunSplit:
    """GENERATION_PAYMENTS_DRY_RUN scopes dry-run to this paywall (2026-08-20).

    The split exists so the generation rail can settle for real while the
    global PAYMENTS_DRY_RUN keeps marketplace sweeps/withdraws dry (#975
    scope decision). Fallback semantics must EXACTLY match the old behavior.
    """

    def test_unset_inherits_global(self, monkeypatch):
        monkeypatch.delenv("GENERATION_PAYMENTS_DRY_RUN", raising=False)
        monkeypatch.setenv("PAYMENTS_DRY_RUN", "true")
        assert gp._payments_dry_run() is True
        monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
        assert gp._payments_dry_run() is False
        monkeypatch.delenv("PAYMENTS_DRY_RUN", raising=False)
        assert gp._payments_dry_run() is True  # fail-safe default unchanged

    def test_split_overrides_global_both_ways(self, monkeypatch):
        # The launch configuration: generation live, global dry.
        monkeypatch.setenv("PAYMENTS_DRY_RUN", "true")
        monkeypatch.setenv("GENERATION_PAYMENTS_DRY_RUN", "false")
        assert gp._payments_dry_run() is False
        # And the reverse (generation dry while global live) also honors the split.
        monkeypatch.setenv("PAYMENTS_DRY_RUN", "false")
        monkeypatch.setenv("GENERATION_PAYMENTS_DRY_RUN", "true")
        assert gp._payments_dry_run() is True

    def test_empty_string_is_not_unset(self, monkeypatch):
        # Explicit empty = explicit value ("" is not truthy-dry) — parses falsy,
        # NOT inherited. Pin it so nobody "clears" the var expecting inheritance.
        monkeypatch.setenv("PAYMENTS_DRY_RUN", "true")
        monkeypatch.setenv("GENERATION_PAYMENTS_DRY_RUN", "")
        assert gp._payments_dry_run() is False
