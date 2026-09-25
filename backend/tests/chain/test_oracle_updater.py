"""Tests for OracleUpdater sanity bounds — audit #13 / issue #508.

Target: backend/archimedes/chain/oracle_updater.py
The oracle runner must refuse to push prices that are non-positive, stale
upstream, or deviate beyond the configured cap vs the last known good price —
while preserving the legitimate update path (anti-goal: bound it, don't
remove it).

Hermetic: chain reads and Circle HTTP calls are mocked at the boundary
(contract loader / aiohttp session). No network, no Arc RPC, no Circle.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.chain.oracle_updater import (
    DEFAULT_MAX_DEVIATION_BPS,
    DEFAULT_MAX_RESEED_DEVIATION_BPS,
    DEFAULT_MAX_UPSTREAM_STALENESS_SECONDS,
    DEFAULT_STALE_RESEED_AFTER_SECONDS,
    OracleUpdater,
)
from archimedes.models.asset import AssetPrice

# ── Helpers ───────────────────────────────────────────────────


def _price(symbol: str = "sTSLA", usd: float = 100.0, age_seconds: float = 0.0) -> AssetPrice:
    return AssetPrice(
        symbol=symbol,
        price_usd=usd,
        timestamp=datetime.now(UTC) - timedelta(seconds=age_seconds),
        source="yfinance",
    )


def _int6(usd: float) -> int:
    return int(usd * 1e6)


@pytest.fixture
def updater() -> OracleUpdater:
    return OracleUpdater()


# ── _validate_for_push ────────────────────────────────────────


class TestValidateForPush:
    async def test_accepts_fresh_price_within_deviation_cap(self, updater):
        # +10% vs reference — inside the 20% default cap
        price = _price(usd=110.0)
        with patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))):
            assert await updater._validate_for_push(price, _int6(110.0)) is None

    async def test_rejects_deviation_beyond_cap(self, updater):
        # +30% vs reference — beyond the 2000 bps default cap
        price = _price(usd=130.0)
        with patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))):
            reason = await updater._validate_for_push(price, _int6(130.0))
        assert reason is not None
        assert "deviation" in reason

    async def test_rejects_downward_deviation_beyond_cap(self, updater):
        # -30% is equally out of bounds
        price = _price(usd=70.0)
        with patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))):
            reason = await updater._validate_for_push(price, _int6(70.0))
        assert reason is not None
        assert "deviation" in reason

    async def test_rejects_stale_upstream_data(self, updater):
        # 1 hour old — past the 15-minute default staleness cap.
        # Reference mock would pass the deviation check, proving staleness
        # alone is sufficient to refuse the push.
        price = _price(usd=100.0, age_seconds=3600)
        with patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))):
            reason = await updater._validate_for_push(price, _int6(100.0))
        assert reason is not None
        assert "stale" in reason

    async def test_rejects_zero_int_price(self, updater):
        # Sub-microdollar float truncates to a 0 on-chain int, which the
        # contract rejects — the backend must refuse before spending a tx.
        price = _price(usd=5e-7)
        reason = await updater._validate_for_push(price, int(5e-7 * 1e6))
        assert reason is not None
        assert "non-positive" in reason

    async def test_accepts_first_push_without_reference(self, updater):
        # Reference confirmed absent (None, known=True) → bootstrap is allowed
        price = _price(usd=100.0)
        with patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(None, True))):
            assert await updater._validate_for_push(price, _int6(100.0)) is None

    async def test_rejects_when_reference_unobtainable(self, updater):
        # Reference unobtainable (None, known=False) → fail closed: a non-None
        # rejection that names the fail-closed reason (issue #587, part 2).
        price = _price(usd=100.0)
        with patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(None, False))):
            reason = await updater._validate_for_push(price, _int6(100.0))
        assert reason is not None
        assert "failing closed" in reason

    async def test_naive_timestamp_treated_as_utc(self, updater):
        # A tz-naive timestamp must not crash the age computation
        naive_now = datetime.now(UTC).replace(tzinfo=None)
        price = AssetPrice(symbol="sTSLA", price_usd=100.0, timestamp=naive_now, source="yfinance")
        with patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(None, True))):
            assert await updater._validate_for_push(price, _int6(100.0)) is None

    async def test_env_overrides_respected(self, monkeypatch):
        monkeypatch.setenv("ORACLE_MAX_DEVIATION_BPS", "5000")
        monkeypatch.setenv("ORACLE_MAX_UPSTREAM_STALENESS_SECONDS", "7200")
        updater = OracleUpdater()
        assert updater._max_deviation_bps == 5000
        assert updater._max_upstream_staleness_s == 7200
        # +30% now passes under the widened 50% cap; 1h-old data passes 2h cap
        price = _price(usd=130.0, age_seconds=3600)
        with patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))):
            assert await updater._validate_for_push(price, _int6(130.0)) is None

    async def test_defaults_match_contract_bound(self, updater):
        assert DEFAULT_MAX_DEVIATION_BPS == 2000  # mirrors PriceOracle.maxDeviationBps
        assert updater._max_deviation_bps == DEFAULT_MAX_DEVIATION_BPS
        assert updater._max_upstream_staleness_s == DEFAULT_MAX_UPSTREAM_STALENESS_SECONDS


# ── _get_reference_price_int ─────────────────────────────────


class TestReferencePrice:
    async def test_prefers_onchain_price(self, updater):
        # On-chain read succeeds with a positive price → (price, known=True).
        oracle_contract = MagicMock()
        oracle_contract.functions.price.return_value.call = AsyncMock(return_value=_int6(123.45))
        loader = MagicMock()
        loader.oracle_for.return_value = oracle_contract
        updater._last_pushed_price_int["sTSLA"] = _int6(999.0)
        with patch("archimedes.chain.contracts.get_contract_loader", return_value=loader):
            assert await updater._get_reference_price_int("sTSLA") == (_int6(123.45), True)

    async def test_falls_back_to_last_pushed_when_chain_read_fails(self, updater):
        # On-chain read throws but a cached last-pushed value exists →
        # (last_pushed, known=True): we still have a usable reference.
        updater._last_pushed_price_int["sTSLA"] = _int6(101.0)
        with patch(
            "archimedes.chain.contracts.get_contract_loader",
            side_effect=ConnectionError("RPC down"),
        ):
            assert await updater._get_reference_price_int("sTSLA") == (_int6(101.0), True)

    async def test_confirmed_absent_when_chain_read_succeeds_with_no_price(self, updater):
        # On-chain read SUCCEEDS but reports 0 and nothing is cached →
        # (None, known=True): reference confirmed absent (genuine first push).
        oracle_contract = MagicMock()
        oracle_contract.functions.price.return_value.call = AsyncMock(return_value=0)
        loader = MagicMock()
        loader.oracle_for.return_value = oracle_contract
        with patch("archimedes.chain.contracts.get_contract_loader", return_value=loader):
            assert await updater._get_reference_price_int("sTSLA") == (None, True)

    async def test_unobtainable_when_chain_read_fails_and_no_cache(self, updater):
        # On-chain read THROWS and there is no cached fallback →
        # (None, known=False): reference unobtainable, caller must fail closed.
        with patch(
            "archimedes.chain.contracts.get_contract_loader",
            side_effect=ConnectionError("RPC down"),
        ):
            assert await updater._get_reference_price_int("sTSLA") == (None, False)


# ── push_prices_on_chain refuses bad prices ──────────────────


def _mock_aiohttp_session(post_response: MagicMock | None = None) -> tuple[MagicMock, MagicMock]:
    """Build a mock aiohttp.ClientSession context manager (the HTTP boundary)."""
    session = MagicMock()
    if post_response is not None:
        post_cm = MagicMock()
        post_cm.__aenter__ = AsyncMock(return_value=post_response)
        post_cm.__aexit__ = AsyncMock(return_value=False)
        session.post = MagicMock(return_value=post_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


@pytest.fixture
def circle_creds(monkeypatch):
    monkeypatch.setenv("CIRCLE_API_KEY", "test-api-key")
    monkeypatch.setenv("CIRCLE_ENTITY_SECRET", "ab" * 32)
    monkeypatch.setenv("WALLET_ID", "test-wallet-id")


class TestPushPricesOnChain:
    async def test_refuses_to_push_price_failing_deviation_check(self, circle_creds):
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"  # skip key fetch
        session_cm, session = _mock_aiohttp_session()

        bad = _price(symbol="sTSLA", usd=130.0)  # +30% vs reference → rejected
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
        ):
            result = await updater.push_prices_on_chain([bad])

        assert result is None
        session.post.assert_not_called()

    async def test_refuses_to_push_stale_price(self, circle_creds):
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        session_cm, session = _mock_aiohttp_session()

        stale = _price(symbol="sTSLA", usd=100.0, age_seconds=3600)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
        ):
            result = await updater.push_prices_on_chain([stale])

        assert result is None
        session.post.assert_not_called()

    async def test_legitimate_update_path_preserved(self, circle_creds):
        # Anti-goal check: a fresh, in-bounds price still pushes through and
        # is recorded as the new fallback reference.
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"

        resp = MagicMock(status=201)
        resp.json = AsyncMock(return_value={"data": {"id": "tx-123"}})
        session_cm, session = _mock_aiohttp_session(post_response=resp)

        # sSPY is an on-chain synth in the SSOT (#764); sTSLA is compliance-held
        # off-chain (#725) so it has no oracle address and would be skipped.
        good = _price(symbol="sSPY", usd=110.0)  # +10%, inside the cap
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="COMPLETE")),
        ):
            result = await updater.push_prices_on_chain([good])

        assert result == "tx-123"
        session.post.assert_called_once()
        assert updater._last_pushed_price_int["sSPY"] == _int6(110.0)

    async def test_fails_closed_when_reference_unobtainable(self, circle_creds):
        # Issue #587, part 2: when the deviation reference is unobtainable
        # (on-chain read failed AND no cached fallback), the symbol must NOT be
        # pushed — fail closed, no tx, rather than send it unchecked.
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        session_cm, session = _mock_aiohttp_session()

        price = _price(symbol="sTSLA", usd=100.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(None, False))),
        ):
            result = await updater.push_prices_on_chain([price])

        assert result is None
        session.post.assert_not_called()

    async def test_fails_closed_on_rpc_outage_after_restart(self, circle_creds):
        # End-to-end variant: simulate the real failure mode — the contract
        # loader throws (RPC outage) and _last_pushed_price_int is empty (fresh
        # process restart). No mock of _get_reference_price_int — it must resolve
        # to (None, False) itself and the push must be skipped (no tx).
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        assert updater._last_pushed_price_int == {}  # fresh restart, no fallback
        session_cm, session = _mock_aiohttp_session()

        price = _price(symbol="sTSLA", usd=100.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch(
                "archimedes.chain.contracts.get_contract_loader",
                side_effect=ConnectionError("RPC down"),
            ),
        ):
            result = await updater.push_prices_on_chain([price])

        assert result is None
        session.post.assert_not_called()


# ── push_prices_on_chain confirms txs before trusting them (#905) ──────


@pytest.mark.usefixtures("circle_creds")
class TestPushConfirmation:
    """A 201 from Circle is submission-accepted, not landed-on-chain. The cached
    deviation reference must move ONLY on a COMPLETE terminal state — a reverted
    push that still updates the cache poisons every later deviation check."""

    def _updater_with_submission(self):
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        resp = MagicMock(status=201)
        resp.json = AsyncMock(return_value={"data": {"id": "tx-905"}})
        return updater, _mock_aiohttp_session(post_response=resp)

    async def _push_with_terminal_state(self, state: str):
        updater, (session_cm, session) = self._updater_with_submission()
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value=state)) as poll,
        ):
            result = await updater.push_prices_on_chain([good])
        return updater, session, poll, result

    async def test_complete_tx_updates_cache(self):
        updater, _session, poll, result = await self._push_with_terminal_state("COMPLETE")
        poll.assert_awaited_once()
        assert result == "tx-905"
        assert updater._last_pushed_price_int["sSPY"] == _int6(110.0)

    async def test_failed_tx_leaves_cache_unchanged_and_logs_error(self, caplog):
        with caplog.at_level(logging.ERROR, logger="archimedes.chain.oracle_updater"):
            updater, _session, poll, result = await self._push_with_terminal_state("FAILED")
        poll.assert_awaited_once()
        # The tx was submitted but reverted: no success signal, no cache update.
        assert result is None
        assert "sSPY" not in updater._last_pushed_price_int
        assert any("deviation-guard reference unchanged" in r.message for r in caplog.records)

    async def test_timeout_treated_as_failure(self):
        updater, _session, _poll, result = await self._push_with_terminal_state("TIMEOUT")
        assert result is None
        assert "sSPY" not in updater._last_pushed_price_int

    async def test_poll_returns_terminal_state_from_circle(self):
        # _poll_circle_tx itself: a FAILED state on the list endpoint is returned
        # verbatim (the caller decides what to do with it — never raises).
        updater = OracleUpdater()
        get_resp = MagicMock(status=200)
        get_resp.json = AsyncMock(
            return_value={"data": {"transactions": [{"id": "tx-905", "state": "FAILED", "txHash": "0xdead"}]}}
        )
        session = MagicMock()
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=get_resp)
        get_cm.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=get_cm)

        assert await updater._poll_circle_tx(session, "tx-905") == "FAILED"

    async def test_poll_times_out_on_never_terminal_tx(self, monkeypatch):
        import archimedes.chain.oracle_updater as ou

        monkeypatch.setattr(ou, "_TX_MAX_POLLS", 2)
        monkeypatch.setattr(ou, "_TX_POLL_INTERVAL_S", 0)

        updater = OracleUpdater()
        get_resp = MagicMock(status=200)
        get_resp.json = AsyncMock(return_value={"data": {"transactions": [{"id": "tx-905", "state": "QUEUED"}]}})
        session = MagicMock()
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=get_resp)
        get_cm.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=get_cm)

        assert await updater._poll_circle_tx(session, "tx-905") == "TIMEOUT"


# ── Circle 200-with-terminal-state must not be logged as an error (#1525) ──


@pytest.mark.usefixtures("circle_creds")
class TestSubmissionResponseParsing:
    """Circle's create-transaction call can validly return a 200 (not just
    201) carrying an already-terminal state — e.g. a fast testnet tx, or an
    idempotency-key dedup landing on an already-resolved tx. Before #1525,
    grading solely on `resp.status == 201` sent every one of those into the
    error branch: `Circle API error for sBTC (200): {'data': {'id': ...,
    'state': 'COMPLETE'}}` — logged every cycle for a push that had already
    succeeded, burying genuine failures under bogus ones."""

    def _updater_with_response(self, status: int, body: dict):
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        resp = MagicMock(status=status)
        resp.json = AsyncMock(return_value=body)
        return updater, _mock_aiohttp_session(post_response=resp)

    async def test_200_complete_is_treated_as_success_not_logged_as_error(self, caplog):
        # The exact shape from the issue's live log line, at the exact status.
        updater, (session_cm, session) = self._updater_with_response(
            200, {"data": {"id": "tx-dup", "state": "COMPLETE"}}
        )
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="COMPLETE")),
            caplog.at_level(logging.ERROR, logger="archimedes.chain.oracle_updater"),
        ):
            result = await updater.push_prices_on_chain([good])

        assert result == "tx-dup"
        assert updater._last_pushed_price_int["sSPY"] == _int6(110.0)
        error_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert not any("Circle API error" in m for m in error_messages), error_messages

    async def test_200_confirmed_is_also_treated_as_success(self, caplog):
        updater, (session_cm, session) = self._updater_with_response(
            200, {"data": {"id": "tx-conf", "state": "CONFIRMED"}}
        )
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="CONFIRMED")),
            caplog.at_level(logging.ERROR, logger="archimedes.chain.oracle_updater"),
        ):
            result = await updater.push_prices_on_chain([good])

        assert result == "tx-conf"
        error_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert not any("Circle API error" in m for m in error_messages), error_messages

    async def test_genuine_terminal_failure_state_still_logs_error_named(self, caplog):
        # Adversarial/guard-rejects-something check: a REAL failure — Circle
        # accepted the call but the tx itself is already DENIED — must still
        # be caught, named, and NOT silently treated as submitted.
        updater, (session_cm, session) = self._updater_with_response(200, {"data": {"id": "tx-bad", "state": "DENIED"}})
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            caplog.at_level(logging.ERROR, logger="archimedes.chain.oracle_updater"),
        ):
            result = await updater.push_prices_on_chain([good])

        assert result is None
        assert "sSPY" not in updater._last_pushed_price_int
        error_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("DENIED" in m for m in error_messages), error_messages

    async def test_malformed_response_with_no_data_still_logs_error(self, caplog):
        # A genuinely broken/unrecognized response (no "data"/"id" at all)
        # must still be caught — the guard must not be relaxed into silence.
        updater, (session_cm, session) = self._updater_with_response(400, {"code": 123, "message": "bad request"})
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            caplog.at_level(logging.ERROR, logger="archimedes.chain.oracle_updater"),
        ):
            result = await updater.push_prices_on_chain([good])

        assert result is None
        error_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("Circle API error" in m for m in error_messages), error_messages


# ── Dedup-hit COMPLETE must skip the re-poll entirely (#1711) ──────────────


@pytest.mark.usefixtures("circle_creds")
class TestDedupHitCompleteSkipsRepoll:
    """#1525 fixed the create-transaction response's OWN grading (200/COMPLETE
    is a success, not an error). But the confirmation phase then unconditionally
    re-polled every submission via `_poll_circle_tx`, which only inspects the
    most recent 50 transactions account-wide — so a dedup-hit tx that actually
    completed several cycles ago (unchanged price, e.g. an equity synth over a
    weekend keeps reproducing the same idempotency key) can have scrolled off
    that window by the time the poll runs. The captured production symptom
    (issue #1711): `Circle API error for sSPY (200): {'data': {'id':
    'c94fa26b-...', 'state': 'COMPLETE'}}` re-logged every cycle for a push
    that had already succeeded — and, left unbounded, the false TIMEOUT this
    produced would feed the wedge tracker into abandoning a perfectly good tx
    and submitting a genuinely NEW on-chain transaction, burning gas for a
    price already pushed. The fix: a submit-time terminal-success state is
    already authoritative, so it must never be re-derived from that narrower,
    staler view."""

    def _updater_with_dedup_hit(self, tx_id: str, state: str = "COMPLETE"):
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        resp = MagicMock(status=200)
        resp.json = AsyncMock(return_value={"data": {"id": tx_id, "state": state}})
        return updater, _mock_aiohttp_session(post_response=resp)

    async def test_exact_captured_payload_skips_repoll_and_logs_info(self, caplog):
        # The exact shape from the issue's live log line: HTTP 200, a
        # dedup-hit tx id, state already COMPLETE.
        tx_id = "c94fa26b-1234-5678-9abc-def012345678"
        updater, (session_cm, _session) = self._updater_with_dedup_hit(tx_id)
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            # If the fix regresses and this gets called, make the regression
            # loud: TIMEOUT is exactly what a scrolled-off-the-window poll
            # would return for an already-complete tx in production.
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="TIMEOUT")) as poll,
            caplog.at_level(logging.INFO, logger="archimedes.chain.oracle_updater"),
        ):
            result = await updater.push_prices_on_chain([good])

        assert result == tx_id
        assert updater._last_pushed_price_int["sSPY"] == _int6(110.0)
        poll.assert_not_called()  # submit-time COMPLETE is authoritative — no re-poll
        assert "sSPY" not in updater._wedge_tracking
        error_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_messages, error_messages
        info_messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert any(tx_id in m and "COMPLETE" in m for m in info_messages), info_messages

    async def test_repeated_dedup_hits_never_trigger_wedge_retry(self):
        # Gas-waste guard: an unchanged weekend price reproduces the identical
        # idempotency key every cycle, so Circle keeps returning the SAME
        # dedup-hit 200/COMPLETE for MANY consecutive cycles — well past
        # ORACLE_TX_MAX_REPOLLS. None of that may ever be misread as a wedge:
        # a real retry here is a real on-chain forceless setPrice tx, i.e. real
        # gas spent on a price that was already pushed.
        tx_id = "c94fa26b-1234-5678-9abc-def012345678"
        updater, (session_cm, _session) = self._updater_with_dedup_hit(tx_id)
        updater._tx_max_repolls = 10
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="TIMEOUT")) as poll,
        ):
            for _ in range(25):  # well past the 10-cycle abandonment threshold
                result = await updater.push_prices_on_chain([good])
                assert result == tx_id

        poll.assert_not_called()
        assert updater._wedge_tracking == {}
        assert updater._tx_retry_salt.get("sSPY", 0) == 0  # no retry ever fired

    async def test_non_terminal_submission_still_polls_normally(self, caplog):
        # Regression guard: this fix must not swallow the normal path — a
        # submission that is genuinely still in flight (no terminal state yet)
        # must still go through the real confirmation poll.
        tx_id = "tx-in-flight"
        updater, (session_cm, _session) = self._updater_with_dedup_hit(tx_id, state="PENDING")
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="COMPLETE")) as poll,
        ):
            result = await updater.push_prices_on_chain([good])

        assert result == tx_id
        poll.assert_called_once()
        assert updater._last_pushed_price_int["sSPY"] == _int6(110.0)


# ── Wedged-tx abandonment (#1525) ──────────────────────────────────────────


@pytest.mark.usefixtures("circle_creds")
class TestWedgedTxAbandonment:
    """A tx id Circle keeps handing back unresolved (same id, TIMEOUT every
    cycle) must be abandoned after a bounded number of cycles rather than
    re-polled forever — the observed production symptom: sSPY re-polling the
    same Circle tx id every cycle for days."""

    def _updater_with_response(self, tx_id: str = "tx-wedged"):
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        resp = MagicMock(status=201)
        resp.json = AsyncMock(return_value={"data": {"id": tx_id}})
        return updater, _mock_aiohttp_session(post_response=resp)

    async def _push_once(self, updater, session_cm, tx_id: str, poll_state: str):
        good = _price(symbol="sSPY", usd=110.0)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(100.0), True))),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value=poll_state)),
        ):
            return await updater.push_prices_on_chain([good])

    async def test_below_threshold_keeps_repolling_the_same_id_quietly(self, caplog):
        # Guard-rejects-something's negative control: BELOW the cap, no
        # abandonment fires — proves the test below is actually exercising
        # the threshold, not something that always fires.
        updater, (session_cm, _session) = self._updater_with_response("tx-wedged")
        updater._tx_max_repolls = 10
        for _ in range(9):
            result = await self._push_once(updater, session_cm, "tx-wedged", "TIMEOUT")
        assert result is None
        assert updater._wedge_tracking["sSPY"] == ("tx-wedged", 9)
        assert updater._tx_retry_salt.get("sSPY", 0) == 0

    async def test_exceeding_threshold_abandons_with_warning_and_forces_fresh_key(self, caplog):
        updater, (session_cm, _session) = self._updater_with_response("tx-wedged")
        updater._tx_max_repolls = 10
        with caplog.at_level(logging.WARNING, logger="archimedes.chain.oracle_updater"):
            # 10 cycles land the tracked count at 10 (== cap); the 11th cycle
            # sees count>=cap BEFORE submitting and abandons.
            for _ in range(11):
                await self._push_once(updater, session_cm, "tx-wedged", "TIMEOUT")

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("abandoning" in m and "tx-wedged" in m for m in warnings), warnings
        # The abandonment cleared tracking and bumped the salt so the NEXT
        # idempotency key for this symbol is guaranteed to differ.
        assert updater._tx_retry_salt["sSPY"] == 1

    async def test_a_terminal_success_clears_wedge_tracking(self):
        # Anti-goal: a tx that eventually resolves must not leave stale
        # tracking behind to falsely trigger abandonment later.
        updater, (session_cm, _session) = self._updater_with_response("tx-wedged")
        await self._push_once(updater, session_cm, "tx-wedged", "TIMEOUT")
        assert "sSPY" in updater._wedge_tracking
        await self._push_once(updater, session_cm, "tx-wedged", "COMPLETE")
        assert "sSPY" not in updater._wedge_tracking

    async def test_a_different_tx_id_resets_the_count_instead_of_accumulating(self):
        # A genuinely new submission (different id — e.g. price moved) must
        # not inherit a previous id's repoll count.
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        resp = MagicMock(status=201)
        resp.json = AsyncMock(side_effect=[{"data": {"id": "tx-a"}}, {"data": {"id": "tx-b"}}])
        session_cm, _session = _mock_aiohttp_session(post_response=resp)

        await self._push_once(updater, session_cm, "tx-a", "TIMEOUT")
        assert updater._wedge_tracking["sSPY"] == ("tx-a", 1)

        await self._push_once(updater, session_cm, "tx-b", "TIMEOUT")
        assert updater._wedge_tracking["sSPY"] == ("tx-b", 1)


# ── Staleness-aware re-seed escape (#1341) ────────────────────────────────


_DAY_S = 86_400


@pytest.mark.usefixtures("circle_creds")
class TestStaleReseedEscape:
    """After the 2026-08-07→08-20 runner outage the deviation guard deadlocked:
    every price had moved past the 2000 bps band vs a 13-day-stale on-chain
    baseline, so every push was refused — and only a push could refresh the
    baseline. The escape must unfreeze that case WITHOUT weakening the band for
    a fresh baseline, and must re-tighten as soon as the re-seed lands.

    Numbers are the issue's real ones: sBTC 104500.00 → 69583 is 3341 bps.
    """

    STALE_REF = _int6(104500.0)
    RECOVERY_USD = 69583.0

    def _updater(self, tx_id: str = "tx-reseed"):
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        resp = MagicMock(status=201)
        resp.json = AsyncMock(return_value={"data": {"id": tx_id}})
        return updater, _mock_aiohttp_session(post_response=resp)

    async def _push(self, updater, session_cm, *, usd: float, reference_int: int, age_s: float | None):
        price = _price(symbol="sBTC", usd=usd)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(reference_int, True))),
            patch.object(updater, "_reference_age_seconds", AsyncMock(return_value=age_s)),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="COMPLETE")),
        ):
            return await updater.push_prices_on_chain([price])

    # ── the deadlock itself ────────────────────────────────────────

    async def test_stale_baseline_pushes_exactly_once_via_force_set_price(self, caplog):
        # THE regression test for #1341. 13-day-stale baseline + a 3341 bps gap:
        # the pre-fix code refuses this forever (no POST at all).
        updater, (session_cm, session) = self._updater()
        with caplog.at_level(logging.WARNING, logger="archimedes.chain.oracle_updater"):
            result = await self._push(
                updater, session_cm, usd=self.RECOVERY_USD, reference_int=self.STALE_REF, age_s=13 * _DAY_S
            )

        assert result == "tx-reseed"
        # Exactly one submission — the recovery replaces the normal push, it is
        # not an extra tx alongside it.
        session.post.assert_called_once()
        payload = session.post.call_args.kwargs["json"]
        assert payload["abiFunctionSignature"] == "forceSetPrice(uint256)"
        assert payload["abiParameters"] == [str(_int6(self.RECOVERY_USD))]
        assert updater._last_pushed_price_int["sBTC"] == _int6(self.RECOVERY_USD)

        # LOUD log naming old price, new price, staleness (the issue's ask).
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        reseed = [m for m in warnings if "STALE-RESEED for sBTC" in m]
        assert reseed, warnings
        assert "104500.000000" in reseed[0] and "69583.000000" in reseed[0]
        assert "312.0h old" in reseed[0] and "3341 bps" in reseed[0]
        assert any("STALE-RESEED COMPLETE for sBTC" in m for m in warnings), warnings

    async def test_band_retightens_after_the_reseed(self):
        # Cycle 1 re-seeds off the stale baseline; cycle 2's baseline is the
        # freshly written price (age 0) and the SAME class of move is refused
        # again — the escape does not leave a widened band behind.
        updater, (session_cm, session) = self._updater()
        assert await self._push(
            updater, session_cm, usd=self.RECOVERY_USD, reference_int=self.STALE_REF, age_s=13 * _DAY_S
        )
        assert session.post.call_count == 1
        assert updater._pending_reseed == set()  # mark cleared by the confirmation

        result = await self._push(
            updater, session_cm, usd=self.RECOVERY_USD * 1.3, reference_int=_int6(self.RECOVERY_USD), age_s=0.0
        )
        assert result is None
        assert session.post.call_count == 1  # no second POST — refused

    async def test_in_band_price_still_uses_set_price_after_a_reseed(self):
        # The cycle after a recovery is an ORDINARY cycle: normal selector.
        updater, (session_cm, session) = self._updater()
        await self._push(updater, session_cm, usd=self.RECOVERY_USD, reference_int=self.STALE_REF, age_s=13 * _DAY_S)
        await self._push(
            updater, session_cm, usd=self.RECOVERY_USD * 1.05, reference_int=_int6(self.RECOVERY_USD), age_s=0.0
        )
        assert session.post.call_count == 2
        assert session.post.call_args.kwargs["json"]["abiFunctionSignature"] == "setPrice(uint256)"

    # ── the guard must still reject ────────────────────────────────

    async def test_fresh_baseline_still_rejects_the_same_move(self):
        # Negative control: identical prices, only the baseline age differs.
        # 1h old → an ordinary out-of-band price → refused, no tx.
        updater, (session_cm, session) = self._updater()
        result = await self._push(
            updater, session_cm, usd=self.RECOVERY_USD, reference_int=self.STALE_REF, age_s=3600.0
        )
        assert result is None
        session.post.assert_not_called()

    async def test_reseed_ceiling_rejects_an_implausible_move_even_when_stale(self, caplog):
        # Adversarial input: a ÷10 decimal-shift glitch (9000 bps) during the
        # SAME 13-day stale window that legitimises the 3341 bps recovery. The
        # outage must not become a blank cheque.
        updater, (session_cm, session) = self._updater()
        with caplog.at_level(logging.ERROR, logger="archimedes.chain.oracle_updater"):
            result = await self._push(updater, session_cm, usd=10450.0, reference_int=self.STALE_REF, age_s=13 * _DAY_S)
        assert result is None
        session.post.assert_not_called()
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("STALE-RESEED REFUSED for sBTC" in m and "9000 bps" in m for m in errors), errors

    async def test_ceiling_is_what_rejects_it(self):
        # Proves the previous test is exercising the ceiling and not some other
        # gate: raise ONLY the ceiling and the identical input goes through.
        updater, (session_cm, session) = self._updater()
        updater._max_reseed_deviation_bps = 9500
        result = await self._push(updater, session_cm, usd=10450.0, reference_int=self.STALE_REF, age_s=13 * _DAY_S)
        assert result == "tx-reseed"
        session.post.assert_called_once()

    async def test_threshold_boundary_one_second_short_still_rejects(self):
        # Pins the comparison direction on the staleness threshold itself: at
        # the threshold minus 1s the baseline is NOT yet stale and the identical
        # move is refused; at exactly the threshold it is. Without this an
        # off-by-one (>= vs >) would silently widen or narrow the escape by a
        # full cycle and no other test would notice.
        updater, (session_cm, session) = self._updater()
        just_short = await self._push(
            updater,
            session_cm,
            usd=self.RECOVERY_USD,
            reference_int=self.STALE_REF,
            age_s=float(DEFAULT_STALE_RESEED_AFTER_SECONDS - 1),
        )
        assert just_short is None
        session.post.assert_not_called()

        at_threshold = await self._push(
            updater,
            session_cm,
            usd=self.RECOVERY_USD,
            reference_int=self.STALE_REF,
            age_s=float(DEFAULT_STALE_RESEED_AFTER_SECONDS),
        )
        assert at_threshold == "tx-reseed"
        session.post.assert_called_once()

    async def test_reseed_still_faces_the_secondary_crosscheck(self, caplog):
        # The escape relaxes ONE gate (the deviation band vs a stale on-chain
        # baseline) and nothing else. The #775 independent-second-opinion
        # cross-check runs AFTER _validate_for_push returns None, so a re-seed
        # candidate whose primary diverges from the secondary is still refused
        # and no tx is submitted. Uses sSPY (the only synth in YFINANCE_MAP, so
        # the cross-check is live for it) and the issue's real sSPY numbers:
        # 592.40 → 769.09 is 2983 bps, inside the 7500 bps recovery ceiling.
        updater, (session_cm, session) = self._updater()
        price = AssetPrice(
            symbol="sSPY",
            price_usd=769.09,
            timestamp=datetime.now(UTC),
            # NOT the active market-data provider, so the cross-check is a real
            # second opinion rather than the same source twice.
            source="pyth",
        )
        fresh_bar = datetime.now(UTC)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(_int6(592.40), True))),
            patch.object(updater, "_reference_age_seconds", AsyncMock(return_value=13 * _DAY_S)),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="COMPLETE")),
            # Independent reading disagrees wildly — beyond the 5000 bps band.
            patch.object(updater, "_fetch_yfinance_single", AsyncMock(return_value=(300.0, fresh_bar))),
            caplog.at_level(logging.WARNING, logger="archimedes.chain.oracle_updater"),
        ):
            result = await updater.push_prices_on_chain([price])

        assert result is None
        session.post.assert_not_called()
        messages = [r.getMessage() for r in caplog.records]
        # The escape did fire (proving the cross-check is what stopped it, not
        # the deviation band) and the cross-check then refused the push anyway.
        assert any("STALE-RESEED for sSPY" in m for m in messages), messages
        assert any("cross-check FAIL" in m for m in messages), messages

    async def test_unknown_baseline_age_does_not_unlock_the_escape(self, caplog):
        # An unreadable lastUpdated (RPC down) must never be read as "stale
        # enough" — same confirmed-absent vs unobtainable discipline as #587.
        updater, (session_cm, session) = self._updater()
        with caplog.at_level(logging.WARNING, logger="archimedes.chain.oracle_updater"):
            result = await self._push(
                updater, session_cm, usd=self.RECOVERY_USD, reference_int=self.STALE_REF, age_s=None
            )
        assert result is None
        session.post.assert_not_called()
        assert any("UNREADABLE" in r.getMessage() for r in caplog.records), caplog.records

    async def test_escape_can_be_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("ORACLE_STALE_RESEED_AFTER_SECONDS", "0")
        updater = OracleUpdater()
        updater._circle_public_key = "cached-pem"
        assert updater._stale_reseed_after_s == 0
        resp = MagicMock(status=201)
        resp.json = AsyncMock(return_value={"data": {"id": "tx-reseed"}})
        session_cm, session = _mock_aiohttp_session(post_response=resp)
        result = await self._push(
            updater, session_cm, usd=self.RECOVERY_USD, reference_int=self.STALE_REF, age_s=13 * _DAY_S
        )
        assert result is None
        session.post.assert_not_called()

    async def test_a_failed_reseed_names_the_manual_escape(self, caplog):
        # forceSetPrice is onlyOwner. If the Circle wallet is not the owner the
        # recovery reverts — that must be loud and actionable, never silent.
        updater, (session_cm, _session) = self._updater()
        price = _price(symbol="sBTC", usd=self.RECOVERY_USD)
        with (
            patch("archimedes.chain.oracle_updater.aiohttp.ClientSession", return_value=session_cm),
            patch("archimedes.chain.oracle_updater._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(updater, "_get_reference_price_int", AsyncMock(return_value=(self.STALE_REF, True))),
            patch.object(updater, "_reference_age_seconds", AsyncMock(return_value=13 * _DAY_S)),
            patch.object(updater, "_poll_circle_tx", AsyncMock(return_value="FAILED")),
            caplog.at_level(logging.ERROR, logger="archimedes.chain.oracle_updater"),
        ):
            result = await updater.push_prices_on_chain([price])

        assert result is None
        assert "sBTC" not in updater._last_pushed_price_int  # baseline reference untouched
        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("STALE-RESEED FAILED for sBTC" in m and "forceSetPrice" in m for m in errors), errors

    async def test_defaults(self):
        updater = OracleUpdater()
        assert updater._stale_reseed_after_s == DEFAULT_STALE_RESEED_AFTER_SECONDS == 86_400
        assert updater._max_reseed_deviation_bps == DEFAULT_MAX_RESEED_DEVIATION_BPS == 7500
        # The ceiling must sit above the normal band (otherwise the escape is
        # unreachable) and below the contract's FORCE_MAX_DEVIATION_BPS (90_000).
        assert DEFAULT_MAX_DEVIATION_BPS < DEFAULT_MAX_RESEED_DEVIATION_BPS < 90_000


# ── _reference_age_seconds ────────────────────────────────────────────────


class TestReferenceAgeSeconds:
    """The baseline clock behind the #1341 escape. Every unreadable case must
    return None (unknown), never a number the caller could read as 'stale'."""

    def _loader(self, last_updated):
        oracle_contract = MagicMock()
        oracle_contract.functions.lastUpdated.return_value.call = AsyncMock(return_value=last_updated)
        loader = MagicMock()
        loader.oracle_for.return_value = oracle_contract
        return loader

    async def test_reads_last_updated_as_an_age(self, updater):
        ts = int(datetime.now(UTC).timestamp()) - 3600
        with patch("archimedes.chain.contracts.get_contract_loader", return_value=self._loader(ts)):
            age = await updater._reference_age_seconds("sBTC")
        assert age is not None and 3590 <= age <= 3610

    async def test_chain_read_failure_is_unknown_not_stale(self, updater):
        with patch(
            "archimedes.chain.contracts.get_contract_loader",
            side_effect=ConnectionError("RPC down"),
        ):
            assert await updater._reference_age_seconds("sBTC") is None

    async def test_never_set_timestamp_is_unknown(self, updater):
        with patch("archimedes.chain.contracts.get_contract_loader", return_value=self._loader(0)):
            assert await updater._reference_age_seconds("sBTC") is None

    async def test_future_timestamp_is_unknown(self, updater):
        ts = int(datetime.now(UTC).timestamp()) + 3600
        with patch("archimedes.chain.contracts.get_contract_loader", return_value=self._loader(ts)):
            assert await updater._reference_age_seconds("sBTC") is None
