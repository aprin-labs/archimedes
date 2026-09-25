"""Tests for the #1240 PAYMENTS_HALT kill switch on the tick-charging rail.

``MarketService.payments_dry_run`` is read once at construction (main.py
boot) and cached — flipping it off needs a container restart.
``PAYMENTS_HALT`` (``marketplace.config.payments_halted``) is read fresh from
``os.environ`` on every ``_charge_one`` call instead, so an operator can stop
real charges without a redeploy. These tests exercise ``_charge_one``
directly rather than the full ``tick()`` pipeline (see
``test_per_step_charging.py`` / ``test_service_tick.py`` for that) since the
halt check is a narrow, self-contained gate.

The second half of the file is not about the tick rail at all: it is the
structural guard that no NEW circlekit call site can escape the switch, plus
the ``payments.charge`` backstop underneath ``_charge_one``. See the banner
comment above ``_READ_ONLY_CIRCLEKIT`` for why per-site tests were not enough.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import archimedes
import pytest
from archimedes.marketplace.service import MarketService, Publisher, Subscriber
from archimedes.marketplace.tick_registry import TickStep


def _svc() -> MarketService:
    return MarketService(interval_seconds=9999, payments_dry_run=False, paper_trading=True)


def _pub() -> Publisher:
    return Publisher(
        strategy_id="strat_a",
        pool_id="0x" + "aa" * 32,
        vault_address="0xpublisher_vault",
        creator_wallet="0xpublisher",
        gateway_seller_address="0xgateway_seller",
    )


def _sub() -> Subscriber:
    return Subscriber(
        sub_id="0x" + "s1" * 32,
        pool_id="0x" + "bb" * 32,
        vault_address="0xsub_vault",
        ephemeral_wallet="0xephemeral",
        subscriber_wallet="0xsubscriber",
        active=True,
        circle_wallet_id="wallet_circle_id",
    )


@pytest.mark.asyncio
async def test_payments_halt_stops_real_charge(monkeypatch):
    """PAYMENTS_HALT=true short-circuits BEFORE the settlement-intent claim,
    the spend-cap reservation, or payments.charge — none of them run."""
    monkeypatch.setenv("PAYMENTS_HALT", "true")
    svc = _svc()
    pub, sub = _pub(), _sub()

    with (
        patch("archimedes.marketplace.service.payments.charge", new=AsyncMock()) as mock_charge,
        patch("archimedes.marketplace.service.spend_cap.try_reserve_usdc", new=AsyncMock()) as mock_reserve,
        patch.object(svc, "_claim_settlement_intent") as mock_claim,
    ):
        paid, override, charge_suppressed = await svc._charge_one(
            pub, sub, "strat_a", "tick_1", TickStep.LOAD_STRATEGY, action_count=1
        )

    assert paid is True
    assert override is None
    assert charge_suppressed is True, (
        "halted charge must be distinguishable from a real/dry-run charge "
        "so callers persist charged=False instead of the #1240 tick-ledger lie"
    )
    mock_claim.assert_not_called()
    mock_reserve.assert_not_awaited()
    mock_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_payments_halt_true_case_insensitive_and_numeric(monkeypatch):
    svc = _svc()
    pub, sub = _pub(), _sub()
    for value in ("true", "TRUE", "1", "yes", "Yes"):
        monkeypatch.setenv("PAYMENTS_HALT", value)
        with patch("archimedes.marketplace.service.payments.charge", new=AsyncMock()) as mock_charge:
            paid, _, charge_suppressed = await svc._charge_one(
                pub, sub, "strat_a", "tick_1", TickStep.LOAD_STRATEGY, action_count=1
            )
        assert paid is True, f"PAYMENTS_HALT={value!r} should halt"
        assert charge_suppressed is True, f"PAYMENTS_HALT={value!r} must mark the charge suppressed"
        mock_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_payments_halt_false_reaches_the_real_charge_path(monkeypatch):
    """Adversarial companion: PAYMENTS_HALT unset (default false) reaches the
    spend-cap reservation — proves the halted test above exercises a real
    short-circuit rather than some other always-unpaid path. A spend-cap
    refusal downstream is expected and orthogonal to this guard."""
    monkeypatch.delenv("PAYMENTS_HALT", raising=False)
    svc = _svc()
    pub, sub = _pub(), _sub()

    with (
        patch.object(svc, "_claim_settlement_intent", return_value="claimed"),
        patch(
            "archimedes.marketplace.service.spend_cap.try_reserve_usdc",
            new=AsyncMock(return_value=False),
        ) as mock_reserve,
    ):
        paid, override, charge_suppressed = await svc._charge_one(
            pub, sub, "strat_a", "tick_1", TickStep.LOAD_STRATEGY, action_count=1
        )

    mock_reserve.assert_awaited_once()
    assert paid is False
    assert override == "24h spend cap reached"
    assert charge_suppressed is False


# ── Structural guard: no new circlekit call site may escape the switch ──────
#
# The failure this exists to prevent already happened once. #1240 wired the
# switch into five call sites; while the PR sat open, `services/revenue_sweep.py`
# landed on main with a live `GatewayClient.withdraw` and nothing noticed. Two
# per-site tests cannot catch a SIXTH site that does not exist yet, so this
# checks the property structurally: circlekit is the only way USDC moves out of
# this backend, so every module that reaches for it must also reach for the
# switch.
#
# Modelled on main's TestNoFifthCallSite (marketplace/test_gateway_chain.py),
# which polices the GATEWAY_CHAIN accessor the same way.

# Modules that import circlekit for something that cannot move value. Each entry
# needs a reason a reviewer can check, not just a path.
_READ_ONLY_CIRCLEKIT = {
    "main.py": (
        "imports get_chain_config only — a pure chain-name -> chain_id lookup "
        "used by the boot-time GATEWAY_CHAIN/RPC assertion. No client, no "
        "signer, no transfer."
    ),
}

_CIRCLEKIT_IMPORT = re.compile(r"^\s*(?:from\s+circlekit[.\w]*\s+import|import\s+circlekit)", re.MULTILINE)


def _package_root() -> Path:
    return Path(archimedes.__file__).resolve().parent


def _modules_importing_circlekit() -> dict[str, str]:
    root = _package_root()
    return {
        str(path.relative_to(root)): path.read_text()
        for path in root.rglob("*.py")
        if _CIRCLEKIT_IMPORT.search(path.read_text())
    }


def test_every_circlekit_module_reaches_for_the_kill_switch():
    """The property: importing the money SDK obliges you to import the brake."""
    offenders = {
        rel: "imports circlekit but never references payments_halted"
        for rel, source in _modules_importing_circlekit().items()
        if rel not in _READ_ONLY_CIRCLEKIT and "payments_halted" not in source
    }
    assert not offenders, (
        f"These modules import circlekit without honouring PAYMENTS_HALT: {sorted(offenders)}. "
        "Gate the fund-moving call (in the callee, so a caller cannot forget), or — if the "
        "import genuinely cannot move value — add it to _READ_ONLY_CIRCLEKIT with the reason."
    )


def test_the_scan_reaches_the_modules_it_claims_to_police():
    """Guard on the guard. A regex that matched nothing would pass the test
    above forever; prove it sees every module that actually moves money today."""
    scanned = set(_modules_importing_circlekit())
    for expected in (
        "marketplace/settlement.py",
        "marketplace/payments.py",
        "services/revenue_sweep.py",
    ):
        assert expected in scanned, f"the circlekit scan missed {expected} — the guard is not guarding"


def test_the_allowlist_is_not_a_dumping_ground():
    """Every exemption must name a module that still exists and still imports
    circlekit, so a stale entry cannot silently exempt a file that later grew a
    withdraw call under a recycled path."""
    scanned = _modules_importing_circlekit()
    for rel, reason in _READ_ONLY_CIRCLEKIT.items():
        assert rel in scanned, f"_READ_ONLY_CIRCLEKIT entry {rel!r} no longer imports circlekit — drop it"
        assert len(reason) > 40, f"_READ_ONLY_CIRCLEKIT entry {rel!r} needs a real reason, not a shrug"


@pytest.mark.asyncio
async def test_payments_charge_itself_refuses_while_halted(monkeypatch, caplog):
    """Backstop, one level below _charge_one. Today unreachable — _charge_one
    short-circuits first — so this pins the behaviour a future caller that goes
    around that gate would get: no Circle round-trip, False, and an ERROR that
    names the bypass rather than a silent success."""
    from archimedes.marketplace import payments

    monkeypatch.setenv("PAYMENTS_HALT", "true")
    with (
        patch.object(payments, "get_gateway_middleware", side_effect=AssertionError("no middleware while halted")),
        caplog.at_level("ERROR"),
    ):
        paid = await payments.charge(
            sub_id="sub_1",
            wallet_id="wallet_1",
            wallet_address="0xsub",
            seller_address="0xseller",
            strategy_id="strat_a",
            tick_id="tick_1",
            action_count=1,
            flat_fee_raw=150_000,
        )
    assert paid is False
    assert "PAYMENTS_HALT" in caplog.text and "bypassed" in caplog.text


@pytest.mark.asyncio
async def test_payments_charge_unhalted_still_builds_the_middleware(monkeypatch):
    """Adversarial companion: same call with the switch off DOES reach
    get_gateway_middleware, so the refusal above is the switch and not the
    argument shape. (charge() never raises, so the sentinel comes back as
    False through its own except — what matters is that it was reached.)"""
    from archimedes.marketplace import payments

    monkeypatch.delenv("PAYMENTS_HALT", raising=False)
    with patch.object(payments, "get_gateway_middleware", side_effect=RuntimeError("reached")) as mw:
        paid = await payments.charge(
            sub_id="sub_1",
            wallet_id="wallet_1",
            wallet_address="0xsub",
            seller_address="0xseller",
            strategy_id="strat_a",
            tick_id="tick_1",
            action_count=1,
            flat_fee_raw=150_000,
        )
    mw.assert_called_once()
    assert paid is False
