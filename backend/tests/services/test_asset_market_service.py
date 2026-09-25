"""Tests for asset_market_service — on-chain oracle reads + stale guards.

Per issue #168: the service reads on-chain PriceOracle via chain_client as
the primary price source, falls back to yfinance for change/vol, and marks
stale when oracle data is >5 min old.

Universe alignment (#759 follow-up to PR #842): the listing iterates the
deploy-eligible SSOT universe (``archimedes.universe.ON_CHAIN_SYNTHS`` — the
same set the Generate picker uses), NOT the legacy ``DEFAULT_SCAN_UNIVERSE``
scan subset; card names come from the SSOT display name; and a cold /
budget-exhausted fetch degrades to an honest partial result (all universe
symbols listed, unfetched ones as ``price_source="none"``) instead of a
timeout. All tests are hermetic — yfinance / chain boundaries are mocked.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archimedes.api.explore_schemas import ExploreAssetsResponse
from archimedes.services.asset_market_service import (
    _CACHE_TTL_SECONDS,
    _CRYPTO_TRADING_DAYS_PER_YEAR,
    _EQUITY_TRADING_DAYS_PER_YEAR,
    _HISTORY_FETCH_BUDGET_SECONDS,
    _ORACLE_TOTAL_BUDGET_SECONDS,
    AssetMarketService,
    _bar_timestamp,
    _change_window,
    _explanations_for,
    _explore_universe,
    _is_24_7_asset_class,
    _pct_change,
    _pct_change_with_reason,
    _realized_vol_annual,
    _realized_vol_annual_with_reason,
    _ssot_display_name,
)

# ── Unit tests for stat math ──────────────────────────────────────────────


class TestStatMath:
    def test_pct_change_basic(self):
        assert _pct_change([100, 105], 1) == pytest.approx(5.0)

    def test_pct_change_insufficient_data(self):
        assert _pct_change([100], 1) is None

    def test_pct_change_zero_start(self):
        assert _pct_change([0, 105], 1) is None

    def test_realized_vol_basic(self):
        # Constant prices → zero vol
        prices = [100.0] * 32
        assert _realized_vol_annual(prices, 30) == pytest.approx(0.0)

    def test_realized_vol_insufficient(self):
        assert _realized_vol_annual([100, 101], 30) is None


# ── Plausibility guard (#1322 — sJUP's +1483.08% "24h" move) ───────────────


class TestPctChangePlausibilityGuard:
    """An arithmetically-impossible pct change must come back None, not a
    fabricated number — honest absence per docs/architectural-principles.md
    § fail-soft, not a clamp/winsorize (the issue's anti-goal)."""

    def test_pct_change_rejects_implausible_24h_move(self):
        """The exact defect class from the issue: a tiny prior close (a bad
        tick / decimal-placement error) implies a >100%/day move. This
        mirrors sJUP's real prior close of 0.0003166165."""
        prices = [0.0003166165, 0.005]
        implied_pct = (0.005 - 0.0003166165) / 0.0003166165 * 100.0
        assert implied_pct > 100.0  # sanity: this genuinely trips the bound
        assert _pct_change(prices, 1) is None

    def test_pct_change_accepts_plausible_large_move(self):
        """A legitimate outsized daily move — well inside the bound — is NOT
        rejected. The guard targets impossible values, not merely large
        ones; the anti-goal forbids hiding real moves behind the guard."""
        assert _pct_change([100.0, 180.0], 1) == pytest.approx(80.0)

    def test_pct_change_exactly_at_the_bound_is_kept(self):
        """Exactly doubling in one bar (100% for n=1) sits ON the bound and
        is kept, not rejected — the guard is a strict '>' check."""
        assert _pct_change([100.0, 200.0], 1) == pytest.approx(100.0)

    def test_pct_change_multiday_window_tolerates_a_wider_move(self):
        """A large multi-day move survives when it's actually made of
        individually-plausible daily steps (#1322 review finding 2 —
        the guard scans bar-to-bar, not an endpoint-compounded bound, so
        this must be a genuinely compounding series, not a flat run with
        one terminal spike, which is the bad-tick shape the guard exists
        to catch, not tolerate)."""
        start = 10.0
        end = start * 3.0  # +200% over 21 bars
        daily_factor = (end / start) ** (1.0 / 21)
        prices = [start * (daily_factor**i) for i in range(22)]
        assert _pct_change(prices, 21) == pytest.approx(200.0, rel=1e-6)
        # But the same +200% packed into a single bar is still rejected.
        assert _pct_change([10.0, 30.0], 1) is None

    def test_pct_change_multiday_bound_compounds_not_sqrt_scales(self):
        """Regression for #1322 review: the multi-day bound must compound the
        per-day bound (a legitimate large cumulative move — even a real 10x
        over a month in a volatile crypto name — is many individually-
        plausible daily steps), not scale it by sqrt(n). sqrt(n) applies a
        volatility-scaling argument to a point-to-point cumulative simple
        return, where it doesn't belong, and is strict enough to falsely
        reject genuine moves: sqrt(30)*100% ≈ 548%, well under a real 10x
        (+900%) month."""
        # +900% (a real 10x) over 30 bars, each individual bar only a
        # plausible ~8.1%/day compounded — no single bar is anywhere near
        # the 100%/day bound, so this must NOT be rejected.
        start = 10.0
        end = start * 10.0  # +900%
        daily_factor = (end / start) ** (1.0 / 30)
        prices = [start * (daily_factor**i) for i in range(31)]
        pct = _pct_change(prices, 30)
        assert pct is not None, "a legitimate compounding 10x/month move must not be rejected"
        assert pct == pytest.approx(900.0, rel=1e-6)

    def test_pct_change_with_reason_distinguishes_rejection_from_no_data(self):
        """rejected_fields (#1322 review) needs to tell "actively suppressed
        as implausible" apart from "not enough history yet" — both surface
        as None from `_pct_change`, but only the former is a rejection."""
        # Implausible: was_rejected True.
        value, was_rejected = _pct_change_with_reason([0.0003166165, 0.005], 1)
        assert value is None
        assert was_rejected is True
        # Not enough data: was_rejected False.
        value, was_rejected = _pct_change_with_reason([100.0], 1)
        assert value is None
        assert was_rejected is False
        # Zero prior close: was_rejected True (round-3 fix, PR #1343 review).
        # A non-positive endpoint is a data hole, not a computable return —
        # the SAME classification the n>1 bar-scan and the vol path already
        # give a non-positive bar, so this must not be a separate "no data"
        # bucket. Pinning this as False (the pre-fix behavior) is exactly
        # the bug: it let a zero prior close silently compute a "kept"
        # value on the boundary case (see the negative control below for
        # the case that actually leaked, a zero *last* close at n=1).
        value, was_rejected = _pct_change_with_reason([0.0, 105.0], 1)
        assert value is None
        assert was_rejected is True
        # A kept value: was_rejected False.
        value, was_rejected = _pct_change_with_reason([100.0, 105.0], 1)
        assert value == pytest.approx(5.0)
        assert was_rejected is False
        # Negative control (round-3 fix, PR #1343 review): a zero *last*
        # close at n=1 — the exact shape that leaked. Before the fix, only
        # `start` (the prior close) was checked; `end` (the last close)
        # fell through to `pct = (end - start) / start * 100.0 == -100.0`,
        # which then passed the `abs(pct) > 100.0` bound check (-100.0 is
        # not > 100.0) and was served as a real, non-rejected change_24h_pct
        # — self-contradictory next to the 7d/30d/vol paths, which already
        # reject any non-positive bar they scan. Mutation check: reverting
        # the `end <= 0` half of the endpoint guard back to checking only
        # `start` makes this assertion fail with (-100.0, False) instead of
        # (None, True) — see the PR body for the transcript.
        assert _pct_change_with_reason([10.0] * 39 + [0.0], 1) == (None, True)

    def test_pct_change_bound_is_1_day_tight_but_30_day_generous(self):
        """Sanity-pins the guard's shape: the 1-day bound stays at the
        strict 100% (where the sJUP defect class lives) while a genuinely
        compounding 30-day move — every bar individually plausible — is
        kept no matter how large the cumulative total is. (Not an endpoint-
        compounded bound: see `test_pct_change_multiday_rejects_a_bad_bar_
        anywhere_in_the_window` for why an endpoint-only check can't tell
        this apart from a single bad tick.)"""
        assert _pct_change([100.0, 201.0], 1) is None  # just over the 1-day bound
        start = 10.0
        end = start * 7.0  # +600% over 30 bars, each bar a plausible ~6.6%/day
        daily_factor = (end / start) ** (1.0 / 30)
        prices = [start * (daily_factor**i) for i in range(31)]
        assert _pct_change(prices, 30) == pytest.approx(600.0, rel=1e-6)

    def test_pct_change_multiday_rejects_a_bad_bar_anywhere_in_the_window(self):
        """Negative control (#1322 review finding 1/2): a single implausible
        bar must still sink the whole window at n=7 and n=30, even once
        it's aged past the 1-day lookback — an endpoint-only bound (whether
        compounded or sqrt(n)-scaled) can't distinguish "one bad tick
        inside an otherwise-flat run" from "many small legitimate daily
        moves"; only a bar-to-bar scan can. Mirrors the issue's own
        sJUP-shaped bad bar (a tiny prior close that snaps back to a normal
        price one bar later) aged 8 bars deep into a 40-bar history, so it
        sits inside the 7d window (n=7) and the 30d window (n=30) but has
        already fallen out of the 1d window (n=1).

        Mutation check: deleting the multi-day bar-scan — e.g. reverting to
        `if n_eff > 1: bound = float('inf')`, or any endpoint-only bound —
        makes both assertions below fail (value stops being None)."""
        prices = [10.0] * 32 + [0.0003166165] + [0.005] * 7  # bad bar at index -8
        assert len(prices) == 40
        for n in (7, 30):
            value, was_rejected = _pct_change_with_reason(prices, n)
            assert value is None, f"n={n}: bad bar must not survive as a value"
            assert was_rejected is True, f"n={n}: must be flagged as a rejection, not 'no data'"
        # The 1-day window no longer contains the bad bar at all (it aged
        # out 7 bars ago) — the trailing bars are all plausible 0.5%/0.005
        # steps, so n=1 is unaffected.
        assert _pct_change(prices, 1) == pytest.approx(0.0, abs=1e-9)


# ── Asset-class-aware trading-day convention (#1322) ────────────────────────


class TestIs247AssetClass:
    def test_crypto_is_24_7(self):
        assert _is_24_7_asset_class("crypto") is True

    def test_equity_etf_is_not_24_7(self):
        assert _is_24_7_asset_class("us_equity_etf") is False

    def test_empty_or_none_asset_class_is_not_24_7(self):
        assert _is_24_7_asset_class("") is False
        assert _is_24_7_asset_class(None) is False


class TestAssetClassAwareAnnualization:
    """Crypto trades 365 days/year with no weekend/holiday gaps; equities
    trade ~252. Annualizing crypto's realized vol with the equity constant
    understates it by sqrt(365/252) ≈ 1.20 — about 17% too low."""

    def _series(self, seed: int) -> list[float]:
        rng = random.Random(seed)
        prices = [100.0]
        for _ in range(31):
            prices.append(prices[-1] * (1 + rng.uniform(-0.02, 0.02)))
        return prices

    def test_identical_daily_returns_annualize_differently_by_asset_class(self):
        prices = self._series(1322)
        equity_vol = _realized_vol_annual(prices, 30, _EQUITY_TRADING_DAYS_PER_YEAR)
        crypto_vol = _realized_vol_annual(prices, 30, _CRYPTO_TRADING_DAYS_PER_YEAR)

        assert equity_vol is not None
        assert crypto_vol is not None
        assert crypto_vol != equity_vol
        assert crypto_vol > equity_vol
        assert crypto_vol / equity_vol == pytest.approx(math.sqrt(365 / 252), rel=1e-9)

    def test_realized_vol_annual_defaults_to_equity_252_unspecified(self):
        """Backward-compatible default: a caller that omits
        trading_days_per_year keeps the pre-#1322 (equity) behavior."""
        prices = self._series(7)
        assert _realized_vol_annual(prices, 30) == _realized_vol_annual(prices, 30, 252)

    def test_realized_vol_annual_actually_uses_the_given_param(self):
        """Guards the exact defect class this repo's CLAUDE.md rule 4 warns
        about: a `trading_days_per_year` param that's accepted but silently
        ignored (the return statement hard-coded back to `sqrt(252)`).
        Unlike `test_identical_daily_returns_annualize_differently_by_asset_class`
        and `test_daily_vol_copy_uses_the_matching_annualization_factor`
        (which both derive their expected value from the function's own
        output — see PR #1343 review), this computes the expected annualized
        vol independently from the raw bar-to-bar returns, so a mutation
        that hard-codes sqrt(252) actually makes the assertion fail rather
        than passing on both sides of the mutation."""
        prices = self._series(2024)
        tail = prices[-31:]
        rets = [(tail[i] - tail[i - 1]) / tail[i - 1] for i in range(1, len(tail))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        expected_crypto_vol = math.sqrt(var) * math.sqrt(_CRYPTO_TRADING_DAYS_PER_YEAR)

        crypto_vol = _realized_vol_annual(prices, 30, _CRYPTO_TRADING_DAYS_PER_YEAR)
        assert crypto_vol == pytest.approx(expected_crypto_vol)
        # And it must differ from what the (wrong) hard-coded-252 mutation
        # would produce, so this genuinely distinguishes the two.
        wrong_if_ignored = math.sqrt(var) * math.sqrt(_EQUITY_TRADING_DAYS_PER_YEAR)
        assert crypto_vol != pytest.approx(wrong_if_ignored)


class TestRealizedVolPlausibilityGuard:
    """A bad tick inside the realized-vol window must not survive as a
    fabricated-but-plausible-looking vol number (#1322 review finding: the
    plausibility guard was applied only to `_pct_change`, leaving
    `_realized_vol_annual` unguarded — the same sJUP-style bad tick that
    `_pct_change` rejects for change_24h_pct still flowed into
    realized_vol_30d and produced an internally-impossible 'typical daily
    move' figure via `_explanations_for`)."""

    def test_realized_vol_rejects_window_containing_a_bad_tick(self):
        """Mirrors sJUP's real prior close (0.0003166165) landing inside an
        otherwise-normal 30-bar window: one implausible bar corrupts the
        whole vol estimate, so the honest response is None, not a number."""
        prices = [0.0003166165] + [0.005] * 30
        assert _realized_vol_annual(prices, 30, _CRYPTO_TRADING_DAYS_PER_YEAR) is None

    def test_realized_vol_accepts_a_window_with_no_bad_bars(self):
        """Sanity: a normal (if volatile) window is unaffected by the guard."""
        rng = random.Random(42)
        prices = [100.0]
        for _ in range(31):
            prices.append(prices[-1] * (1 + rng.uniform(-0.05, 0.05)))
        assert _realized_vol_annual(prices, 30, _CRYPTO_TRADING_DAYS_PER_YEAR) is not None

    def test_realized_vol_with_reason_reports_rejection(self):
        prices = [0.0003166165] + [0.005] * 30
        value, was_rejected = _realized_vol_annual_with_reason(prices, 30, _CRYPTO_TRADING_DAYS_PER_YEAR)
        assert value is None
        assert was_rejected is True

    def test_realized_vol_with_reason_insufficient_data_is_not_a_rejection(self):
        """None from too little history is NOT a plausibility rejection —
        callers (rejected_fields) must be able to tell the two apart."""
        value, was_rejected = _realized_vol_annual_with_reason([100.0, 101.0], 30)
        assert value is None
        assert was_rejected is False

    def test_realized_vol_rejects_a_zero_bar_in_the_window(self):
        """#1322 review finding 4: a zero close inside the window is a data
        hole, not a real -100% return — it must not slip through and get
        folded into the vol estimate. Before the fix, `if not prev:
        continue` only skipped the step *out of* the zero; the step *into*
        it computed an exact -100% return that passed the strict `>` bound
        comparison (-100% is not > 100%) and was silently included as real
        data. Mutation check: reverting the `prev <= 0 or curr <= 0` guard
        back to `if not prev: continue` makes this assertion fail (value
        stops being None)."""
        prices = [0.005] * 20 + [0.0] + [0.005] * 10
        value, was_rejected = _realized_vol_annual_with_reason(prices, 30, _CRYPTO_TRADING_DAYS_PER_YEAR)
        assert value is None
        assert was_rejected is True


class TestExplanationsAssetClassAware:
    def test_period_labels_match_asset_class(self):
        stat_dict = {
            "current_price": 100.0,
            "change_24h_pct": 1.0,
            "change_7d_pct": 2.0,
            "change_30d_pct": 3.0,
            "realized_vol_30d": 0.4,
        }
        equity_expl = _explanations_for(stat_dict, is_247=False)
        crypto_expl = _explanations_for(stat_dict, is_247=True)

        assert "5 trading days" in equity_expl["change_7d_pct"]
        assert "≈21 trading days" in equity_expl["change_30d_pct"]
        assert "7 calendar days" in crypto_expl["change_7d_pct"]
        assert "30 calendar days" in crypto_expl["change_30d_pct"]

    def test_daily_vol_copy_uses_the_matching_annualization_factor(self):
        """The de-annualized 'typical daily move' figure in the copy must be
        computed with the SAME trading-day count the vol was actually
        annualized with — otherwise the copy asserts a number the
        computation didn't produce (#1322 honesty requirement: a hard-coded
        wrong-convention explanation is the same defect class as a fabricated
        statistic).

        Scope note (PR #1343 review): this guards `_explanations_for`'s own
        factor choice, NOT `_realized_vol_annual`'s `trading_days_per_year`
        plumbing — `expected_daily_pct` here is derived from `crypto_vol`,
        the same value both sides compare against, so it passes unchanged
        even if `_realized_vol_annual` silently ignored its param and always
        annualized with 252 (a mutation-checked demonstration lives in the
        PR body). `test_realized_vol_annual_actually_uses_the_given_param`
        above is what actually guards the param plumbing, by computing its
        expectation independently."""
        rng = random.Random(7)
        prices = [100.0]
        for _ in range(31):
            prices.append(prices[-1] * (1 + rng.uniform(-0.03, 0.03)))
        crypto_vol = _realized_vol_annual(prices, 30, _CRYPTO_TRADING_DAYS_PER_YEAR)
        assert crypto_vol is not None

        expl = _explanations_for({"realized_vol_30d": crypto_vol}, is_247=True)
        expected_daily_pct = (crypto_vol / math.sqrt(365)) * 100.0
        assert f"{expected_daily_pct:.1f}%" in expl["realized_vol_30d"]

    def test_change_24h_copy_drops_vol_clause_when_vol_was_suppressed(self):
        """#1322 review finding 3: when realized_vol_30d is None (suppressed
        by the plausibility guard, or just not enough history), the
        change_24h_pct copy must not assert a fabricated '0.0%' vol
        threshold — a number the computation explicitly refused to
        produce. It should read as a plain move description with no vol
        clause at all. Mutation check: reverting `vol = item.get(...)` back
        to `item.get(...) or 0.0` makes '0.0%' reappear in the copy."""
        expl = _explanations_for({"change_24h_pct": 5.0, "realized_vol_30d": None}, is_247=True)
        assert "0.0%" not in expl["change_24h_pct"]
        assert expl["change_24h_pct"] == "Percentage move in the last trading day. Positive = up."

    def test_change_24h_copy_keeps_vol_clause_when_vol_present(self):
        """Sanity: the vol clause still renders normally when realized_vol_30d
        is actually present, so the finding-3 fix doesn't drop it always."""
        expl = _explanations_for({"change_24h_pct": 5.0, "realized_vol_30d": 0.5}, is_247=True)
        assert "unusual for this asset" in expl["change_24h_pct"]
        assert "0.0%" not in expl["change_24h_pct"]


# ── Oracle read tests (mocked chain_client) ────────────────────────────────


@pytest.fixture
def mock_chain_client():
    """Mock chain_client with oracle and synth addresses + ABI."""
    from pathlib import Path

    mock_settings = MagicMock()
    mock_settings.oracle_addresses = {
        "sSPY": "0xd8161a8eeab7c7100e2863abe3d5f346b5ff9e52",
        "sBTC": "0x6cc5f621c4e3b46152e69e5c9873689cbb4a85e8",
    }
    mock_settings.synth_addresses = {
        "sSPY": "0x6fea38dedea0c6bb66ce93e5383c34385d8b889f",
        "sBTC": "0x317e82be8f7cba6c162ab968fcf695d88e8e0359",
    }
    # Point to real ABI directory so Path resolution works
    abi_dir = str(Path(__file__).resolve().parents[2] / "contracts" / "abis")
    mock_settings.abi_dir = abi_dir

    mock_client = MagicMock()
    mock_client.settings = mock_settings
    mock_client.to_checksum = lambda addr: addr
    return mock_client


class TestOracleReads:
    @pytest.mark.asyncio
    async def test_read_oracle_prices_success(self, mock_chain_client):
        """On-chain getPrice returns price + timestamp; parsed correctly."""
        now = time.time()
        service = AssetMarketService()

        mock_contract = MagicMock()
        mock_contract.functions.getPrice.return_value.call = AsyncMock(
            return_value=(550_000_000, int(now)),  # $550 in 6 decimals
        )
        mock_client_w3 = MagicMock()
        mock_client_w3.eth.contract.return_value = mock_contract
        mock_chain_client.w3 = mock_client_w3

        with patch("archimedes.chain.client.chain_client", mock_chain_client):
            result = await service._read_oracle_prices(["sSPY"])

        assert "sSPY" in result
        assert result["sSPY"]["price"] == pytest.approx(550.0)
        assert result["sSPY"]["stale"] is False

    @pytest.mark.asyncio
    async def test_read_oracle_prices_stale(self, mock_chain_client):
        """Oracle timestamp >5 min old → stale=True."""
        old_ts = time.time() - 600  # 10 minutes ago
        service = AssetMarketService()

        mock_contract = MagicMock()
        mock_contract.functions.getPrice.return_value.call = AsyncMock(
            return_value=(100_000_000, int(old_ts)),  # $100, stale
        )
        mock_client_w3 = MagicMock()
        mock_client_w3.eth.contract.return_value = mock_contract
        mock_chain_client.w3 = mock_client_w3

        with patch("archimedes.chain.client.chain_client", mock_chain_client):
            result = await service._read_oracle_prices(["sSPY"])

        assert "sSPY" in result
        assert result["sSPY"]["stale"] is True

    @pytest.mark.asyncio
    async def test_read_oracle_prices_future_timestamp_stale(self, mock_chain_client):
        """An updated_at ahead of the host clock reads as stale, not fresh (#934).

        A future block timestamp otherwise leaves (now_ts - updated_at) negative
        forever, so a frozen price could never trip the staleness gate."""
        future_ts = time.time() + 1  # 1 second in the future
        service = AssetMarketService()

        mock_contract = MagicMock()
        mock_contract.functions.getPrice.return_value.call = AsyncMock(
            return_value=(100_000_000, int(future_ts) + 1),  # ceil past now to stay in the future
        )
        mock_client_w3 = MagicMock()
        mock_client_w3.eth.contract.return_value = mock_contract
        mock_chain_client.w3 = mock_client_w3

        with patch("archimedes.chain.client.chain_client", mock_chain_client):
            result = await service._read_oracle_prices(["sSPY"])

        assert "sSPY" in result
        assert result["sSPY"]["stale"] is True

    @pytest.mark.asyncio
    async def test_read_oracle_prices_missing_symbol(self, mock_chain_client):
        """Symbol not in oracle_addresses → skipped, not error."""
        service = AssetMarketService()
        mock_client_w3 = MagicMock()
        mock_chain_client.w3 = mock_client_w3

        with patch("archimedes.chain.client.chain_client", mock_chain_client):
            result = await service._read_oracle_prices(["sUNKNOWN"])

        assert "sUNKNOWN" not in result
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_read_oracle_prices_chain_failure(self, mock_chain_client):
        """Chain read throws → symbol skipped, no crash."""
        service = AssetMarketService()

        mock_contract = MagicMock()
        mock_contract.functions.getPrice.return_value.call = AsyncMock(
            side_effect=Exception("RPC timeout"),
        )
        mock_client_w3 = MagicMock()
        mock_client_w3.eth.contract.return_value = mock_contract
        mock_chain_client.w3 = mock_client_w3

        with patch("archimedes.chain.client.chain_client", mock_chain_client):
            result = await service._read_oracle_prices(["sSPY"])

        assert "sSPY" not in result  # Failed read → omitted


class TestListAssets:
    @pytest.mark.asyncio
    async def test_oracle_primary_price_yfinance_fallback(self, mock_chain_client):
        """When oracle returns a price, it takes priority over yfinance."""
        now = time.time()
        service = AssetMarketService()

        mock_contract = MagicMock()
        mock_contract.functions.getPrice.return_value.call = AsyncMock(
            return_value=(550_000_000, int(now)),  # $550 from oracle
        )
        mock_client_w3 = MagicMock()
        mock_client_w3.eth.contract.return_value = mock_contract
        mock_chain_client.w3 = mock_client_w3

        mock_histories = {
            "sSPY": {
                "close": [540.0, 545.0, 548.0],  # yfinance shows ~$548
                "dates": ["2026-05-22", "2026-05-23", "2026-05-24"],
            }
        }

        with (
            patch("archimedes.chain.client.chain_client", mock_chain_client),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sSPY"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sSPY": ("SPY", "SPY", "us_equity_etf", "NYSE")},
            ),
        ):
            resp = await service.list_assets()

        assert len(resp.assets) >= 1
        spy = next((a for a in resp.assets if a.symbol == "sSPY"), None)
        assert spy is not None
        assert spy.current_price == pytest.approx(550.0)  # Oracle price, not yfinance
        assert spy.is_stale is False

    @pytest.mark.asyncio
    async def test_no_oracle_falls_back_to_yfinance_not_stale(self):
        """When the on-chain oracle has no price, the service should fall back
        to yfinance and surface ``price_source="yfinance"``. The displayed
        price isn't stale just because the unused oracle slot has no value —
        ``is_stale`` reflects the *displayed* price's freshness, not the oracle
        slot. (Semantics changed during the 2026-05-25 Explore-page rebuild —
        see ``asset_market_service.py`` module docstring for full rationale.)"""
        service = AssetMarketService()
        # Use the current UTC date as the latest bar so the staleness check
        # (yfinance window of 4 days) doesn't drift the test as time passes.
        from datetime import UTC, datetime, timedelta

        today = datetime.now(UTC).date()
        mock_histories = {
            "sSPY": {
                "close": [540.0, 545.0],
                "dates": [(today - timedelta(days=1)).isoformat(), today.isoformat()],
            }
        }

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sSPY"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sSPY": ("SPY", "SPY", "us_equity_etf", "NYSE")},
            ),
        ):
            resp = await service.list_assets()

        spy = next((a for a in resp.assets if a.symbol == "sSPY"), None)
        assert spy is not None
        assert spy.current_price == pytest.approx(545.0)  # yfinance fallback
        assert spy.price_source == "yfinance"
        assert spy.is_stale is False  # displayed price is fresh; only the unused oracle slot is empty

    @pytest.mark.asyncio
    async def test_list_assets_survives_none_in_price_series(self):
        """A None in a raw (object-dtype) price series must not abort the whole
        /explore/assets response — the bad element is dropped, the card still
        renders (#928). Uses dtype=object so the None stays a None (a float64
        series would coerce it to NaN, which math.isnan handles fine and would
        not reproduce the TypeError this guards against)."""
        import pandas as pd

        service = AssetMarketService()
        bad_series = pd.Series([540.0, None, 548.0], dtype=object)

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch.object(
                service,
                "_fetch_histories_budgeted",
                AsyncMock(return_value={"sSPY": bad_series}),
            ),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sSPY"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sSPY": ("SPY", "SPY", "us_equity_etf", "NYSE")},
            ),
        ):
            resp = await service.list_assets()  # must not raise TypeError

        spy = next((a for a in resp.assets if a.symbol == "sSPY"), None)
        assert spy is not None  # card served, not dropped by a single bad element

    @pytest.mark.asyncio
    async def test_cache_ttl(self):
        """Second call within TTL returns cached result."""
        service = AssetMarketService()

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value={}),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=[]),
            patch("archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS", {}),
        ):
            resp1 = await service.list_assets()
            resp2 = await service.list_assets()

        assert resp1 is resp2  # Same object (cached)

    @pytest.mark.asyncio
    async def test_stale_cache_served_immediately_not_blocked_on_rebuild(self):
        """Stale-while-revalidate (explore-latency fix): once ANY cache exists,
        a request past the TTL must never await the rebuild inline — it gets
        the last-good (stale) response immediately, and a background task
        does the rebuild. This is what turns the measured 12.7s-42.9s prod
        stall (cache-miss request pays the full oracle+yfinance cost) into a
        sub-millisecond cache read for every request except the very first.
        """
        service = AssetMarketService()

        # Seed a cache directly — skip the cold-start path entirely.
        stale_resp = ExploreAssetsResponse(
            assets=[],
            cache_ttl_seconds=_CACHE_TTL_SECONDS,
            generated_at="t0",
            universe_size=0,
            priced_count=0,
        )
        service._cache = stale_resp
        service._cache_ts = time.time() - (_CACHE_TTL_SECONDS + 1)  # already expired

        # A rebuild that would hang forever if this test ever awaited it inline.
        rebuild_started = asyncio.Event()
        rebuild_may_finish = asyncio.Event()

        async def slow_refresh():
            rebuild_started.set()
            await rebuild_may_finish.wait()
            return ExploreAssetsResponse(
                assets=[],
                cache_ttl_seconds=_CACHE_TTL_SECONDS,
                generated_at="t1",
                universe_size=0,
                priced_count=0,
            )

        with patch.object(service, "_refresh", side_effect=slow_refresh):
            t0 = time.monotonic()
            resp = await asyncio.wait_for(service.list_assets(), timeout=1.0)
            elapsed = time.monotonic() - t0

            assert resp is stale_resp  # served the stale cache, not the rebuild
            assert elapsed < 0.5  # never blocked on the (hung) rebuild
            await asyncio.wait_for(rebuild_started.wait(), timeout=1.0)  # rebuild WAS kicked off

            rebuild_may_finish.set()
            await service._refresh_task  # let the background task finish cleanly

    @pytest.mark.asyncio
    async def test_stale_cache_concurrent_requests_dedupe_background_refresh(self):
        """N requests landing in the same stale window must not each start
        their own oracle+yfinance rebuild — only one refresh in flight."""
        service = AssetMarketService()

        stale_resp = ExploreAssetsResponse(
            assets=[],
            cache_ttl_seconds=_CACHE_TTL_SECONDS,
            generated_at="t0",
            universe_size=0,
            priced_count=0,
        )
        service._cache = stale_resp
        service._cache_ts = time.time() - (_CACHE_TTL_SECONDS + 1)

        call_count = 0
        finish = asyncio.Event()

        async def counting_refresh():
            nonlocal call_count
            call_count += 1
            await finish.wait()
            return stale_resp

        with patch.object(service, "_refresh", side_effect=counting_refresh):
            results = await asyncio.gather(*(service.list_assets() for _ in range(5)))
            assert all(r is stale_resp for r in results)
            finish.set()
            # Bind the result rather than awaiting for the side effect alone:
            # a bare ``await task`` reads to a linter as a statement with no
            # effect, and asserting on what the background refresh actually
            # returned is a strictly stronger check than merely draining it.
            refreshed = await service._refresh_task
            assert refreshed is stale_resp

        assert call_count == 1  # deduplicated, not 5 separate rebuilds


# ── End-to-end #1322 repro: impossible values never reach the API ──────────


class TestExploreAssetsPlausibilityAndAssetClassAwareness:
    @pytest.mark.asyncio
    async def test_list_assets_change_24h_pct_none_for_impossible_move(self):
        """Full repro of the issue: a corrupted history (tiny prior close,
        mirroring sJUP's real 0.0003166165) must surface as
        change_24h_pct=None on the served AssetExploreItem — never as a
        fabricated triple-digit percentage. The price itself still shows."""
        service = AssetMarketService()
        mock_histories = {
            "sJUP": {
                "close": [0.0003166165, 0.005],
                "dates": ["2026-08-19", "2026-08-20"],
            }
        }

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sJUP"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sJUP": ("JUP-USD", "JUP", "crypto", "Coinbase")},
            ),
        ):
            resp = await service.list_assets()

        jup = next(a for a in resp.assets if a.symbol == "sJUP")
        assert jup.change_24h_pct is None  # honest absence, not +1479%
        assert jup.current_price == pytest.approx(0.005)  # price still shown
        # #1322 review: the served item must disclose WHICH field was
        # actively suppressed as implausible, not merely leave the field
        # null (indistinguishable from "not enough history yet" — see the
        # issue's Precedent section on is_stale/price_source as the honest-
        # absence mechanism this payload already carries for the price).
        assert jup.rejected_fields == ["change_24h_pct"]

    @pytest.mark.asyncio
    async def test_list_assets_rejected_fields_empty_when_nothing_suppressed(self):
        """A normal series must NOT carry a spurious rejected_fields entry —
        the disclosure mechanism must not cry wolf on legitimate data."""
        service = AssetMarketService()
        mock_histories = {
            "sSPY": {
                "close": [540.0, 545.0, 548.0],
                "dates": ["2026-05-22", "2026-05-23", "2026-05-24"],
            }
        }

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sSPY"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sSPY": ("SPY", "SPY", "us_equity_etf", "NYSE")},
            ),
        ):
            resp = await service.list_assets()

        spy = next(a for a in resp.assets if a.symbol == "sSPY")
        assert spy.rejected_fields == []

    @pytest.mark.asyncio
    async def test_list_assets_realized_vol_rejected_field_disclosed(self):
        """The realized_vol_30d guard (#1322 review finding: it was
        previously unguarded) must also disclose via rejected_fields when it
        fires, end to end through list_assets."""
        service = AssetMarketService()
        bad_tick_series = [0.0003166165] + [0.005] * 30
        mock_histories = {
            "sJUP": {
                "close": bad_tick_series,
                "dates": [f"d{i}" for i in range(len(bad_tick_series))],
            }
        }

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sJUP"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sJUP": ("JUP-USD", "JUP", "crypto", "Coinbase")},
            ),
        ):
            resp = await service.list_assets()

        jup = next(a for a in resp.assets if a.symbol == "sJUP")
        assert jup.realized_vol_30d is None  # no fabricated "typical daily move ~270%"
        assert "realized_vol_30d" in jup.rejected_fields

    @pytest.mark.asyncio
    async def test_list_assets_period_offsets_are_asset_class_aware(self):
        """Same input series for a crypto symbol and an equity symbol must
        produce DIFFERENT change_7d_pct / change_30d_pct: crypto indexes 7
        and 30 bars back (24/7, one bar per calendar day); equity indexes 5
        and 21 (trading days only)."""
        service = AssetMarketService()
        closes = [100.0 + i for i in range(35)]  # monotonic → offsets diverge
        mock_histories = {
            "sBTC": {"close": closes, "dates": [f"d{i}" for i in range(35)]},
            "sSPY": {"close": closes, "dates": [f"d{i}" for i in range(35)]},
        }

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sBTC", "sSPY"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {
                    "sBTC": ("BTC-USD", "BTC", "crypto", "Coinbase"),
                    "sSPY": ("SPY", "SPY", "us_equity_etf", "NYSE"),
                },
            ),
        ):
            resp = await service.list_assets()

        btc = next(a for a in resp.assets if a.symbol == "sBTC")
        spy = next(a for a in resp.assets if a.symbol == "sSPY")
        assert btc.change_7d_pct != spy.change_7d_pct
        assert btc.change_30d_pct != spy.change_30d_pct
        # Exact expected values pin the offsets down (7/30 vs 5/21 bars back).
        assert btc.change_7d_pct == pytest.approx((closes[-1] - closes[-1 - 7]) / closes[-1 - 7] * 100.0)
        assert spy.change_7d_pct == pytest.approx((closes[-1] - closes[-1 - 5]) / closes[-1 - 5] * 100.0)
        assert btc.change_30d_pct == pytest.approx((closes[-1] - closes[-1 - 30]) / closes[-1 - 30] * 100.0)
        assert spy.change_30d_pct == pytest.approx((closes[-1] - closes[-1 - 21]) / closes[-1 - 21] * 100.0)


# ── Universe alignment (#759 follow-up to #842) ────────────────────────────


class TestExploreUniverseAlignment:
    def test_explore_universe_is_the_deploy_eligible_ssot(self):
        """_explore_universe() must be ON_CHAIN_SYNTHS (the Generate-picker set),
        not DEFAULT_SCAN_UNIVERSE and not the single-stock-carrying GLOBAL_ASSETS."""
        from archimedes.services.strategy_signal_evaluator import DEFAULT_SCAN_UNIVERSE, GLOBAL_ASSETS
        from archimedes.universe import ON_CHAIN_SYNTHS

        universe = _explore_universe()
        assert universe == list(ON_CHAIN_SYNTHS)
        assert set(universe) != set(DEFAULT_SCAN_UNIVERSE)  # not the legacy ~74-name scan subset
        assert len(universe) > len(DEFAULT_SCAN_UNIVERSE)
        assert set(universe) <= set(GLOBAL_ASSETS)  # every card resolvable to a yfinance ticker

    @pytest.mark.asyncio
    async def test_list_assets_iterates_ssot_universe_not_scan_universe(self):
        """Every SSOT-universe symbol gets a card — including ones with no data —
        and the counts disclose data coverage honestly."""
        service = AssetMarketService()
        mock_histories = {
            "sAAA": {"close": [10.0, 11.0], "dates": ["2026-06-29", "2026-06-30"]},
        }

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sAAA", "sBBB"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {
                    "sAAA": ("AAA", "AAA", "us_equity_etf", "NYSE"),
                    "sBBB": ("BBB", "BBB", "us_equity_etf", "NYSE"),
                },
            ),
        ):
            resp = await service.list_assets()

        symbols = {a.symbol for a in resp.assets}
        assert symbols == {"sAAA", "sBBB"}  # full universe listed, not just fetched symbols
        assert resp.universe_size == 2
        assert resp.priced_count == 1
        bbb = next(a for a in resp.assets if a.symbol == "sBBB")
        assert bbb.current_price is None
        assert bbb.price_source == "none"
        assert bbb.is_stale is True  # honestly stale: no source at all

    @pytest.mark.asyncio
    async def test_card_name_uses_ssot_display_name_with_ticker_fallback(self):
        """Card names come from the SSOT (synthetic_universe.json), not a
        blanket f"Synthetic {ticker}"; symbols absent from the SSOT fall back
        to the ticker."""
        service = AssetMarketService()

        fake_spec = MagicMock()
        fake_spec.name = "Synthetic TLT (20+yr)"

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value={}),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sTLT", "sZZZ"]),
            patch("archimedes.universe.SYNTHETIC_UNIVERSE", {"sTLT": fake_spec}),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {
                    "sTLT": ("TLT", "TLT", "us_bond_long", "NASDAQ"),
                    "sZZZ": ("ZZZ", "ZZZ", "us_equity_etf", "NYSE"),
                },
            ),
        ):
            resp = await service.list_assets()

        by_symbol = {a.symbol: a for a in resp.assets}
        assert by_symbol["sTLT"].name == "Synthetic TLT (20+yr)"  # SSOT display name
        assert by_symbol["sZZZ"].name == "ZZZ"  # ticker fallback (not in SSOT)

    def test_ssot_display_name_real_ssot(self):
        """Against the real SSOT: sTLT carries a richer name than 'Synthetic TLT'."""
        assert _ssot_display_name("sTLT", "TLT") == "Synthetic TLT (20+yr)"
        assert _ssot_display_name("sNOT_A_SYNTH", "FALLBACK") == "FALLBACK"


# ── Partial-result degradation (budgeted history fetch) ─────────────────────


class TestBudgetedHistoryFetch:
    @pytest.mark.asyncio
    async def test_budget_exhausted_serves_partial_not_timeout(self):
        """With a zero fetch budget, list_assets still answers: every universe
        symbol is listed, prices are honestly null, priced_count == 0."""
        service = AssetMarketService()
        fetch_mock = MagicMock()

        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", fetch_mock),
            patch("archimedes.services.asset_market_service._HISTORY_FETCH_BUDGET_SECONDS", 0.0),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sAAA", "sBBB"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {
                    "sAAA": ("AAA", "AAA", "us_equity_etf", "NYSE"),
                    "sBBB": ("BBB", "BBB", "us_equity_etf", "NYSE"),
                },
            ),
        ):
            resp = await service.list_assets()

        fetch_mock.assert_not_called()  # budget gone before the first chunk
        assert {a.symbol for a in resp.assets} == {"sAAA", "sBBB"}
        assert resp.universe_size == 2
        assert resp.priced_count == 0
        assert all(a.price_source == "none" for a in resp.assets)

    @pytest.mark.asyncio
    async def test_chunked_fetch_merges_all_chunks(self):
        """Chunk size 1 over 3 symbols → 3 evaluator calls, results merged."""
        service = AssetMarketService()
        per_chunk = {
            "sAAA": {"sAAA": {"close": [1.0, 2.0], "dates": ["d1", "d2"]}},
            "sBBB": {"sBBB": {"close": [3.0, 4.0], "dates": ["d1", "d2"]}},
            "sCCC": {"sCCC": {"close": [5.0, 6.0], "dates": ["d1", "d2"]}},
        }
        calls: list[list[str]] = []

        def fake_fetch(symbols, period):
            calls.append(list(symbols))
            return per_chunk[symbols[0]]

        with (
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", side_effect=fake_fetch),
            patch("archimedes.services.asset_market_service._HISTORY_CHUNK_SIZE", 1),
        ):
            histories = await service._fetch_histories_budgeted(["sAAA", "sBBB", "sCCC"])

        assert calls == [["sAAA"], ["sBBB"], ["sCCC"]]
        assert set(histories) == {"sAAA", "sBBB", "sCCC"}

    @pytest.mark.asyncio
    async def test_slow_chunk_times_out_and_serves_what_it_has(self):
        """A chunk that overruns the remaining budget is cut off; earlier
        chunks' data is still served (honest partial, not an exception)."""
        service = AssetMarketService()
        calls: list[list[str]] = []

        def fake_fetch(symbols, period):
            calls.append(list(symbols))
            if symbols[0] == "sBBB":
                time.sleep(0.5)  # overruns the 0.2s budget
            return {symbols[0]: {"close": [1.0], "dates": ["d1"]}}

        with (
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", side_effect=fake_fetch),
            patch("archimedes.services.asset_market_service._HISTORY_CHUNK_SIZE", 1),
            patch("archimedes.services.asset_market_service._HISTORY_FETCH_BUDGET_SECONDS", 0.2),
        ):
            histories = await service._fetch_histories_budgeted(["sAAA", "sBBB", "sCCC"])

        assert "sAAA" in histories  # fetched before the budget ran out
        assert "sCCC" not in histories  # never attempted after the timeout
        assert [c[0] for c in calls] == ["sAAA", "sBBB"]

    @pytest.mark.asyncio
    async def test_failed_chunk_does_not_kill_later_chunks(self):
        """A chunk raising (upstream hiccup) is logged and skipped; later
        chunks still fetch."""
        service = AssetMarketService()

        def fake_fetch(symbols, period):
            if symbols[0] == "sAAA":
                raise RuntimeError("yfinance hiccup")
            return {symbols[0]: {"close": [1.0], "dates": ["d1"]}}

        with (
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", side_effect=fake_fetch),
            patch("archimedes.services.asset_market_service._HISTORY_CHUNK_SIZE", 1),
        ):
            histories = await service._fetch_histories_budgeted(["sAAA", "sBBB"])

        assert set(histories) == {"sBBB"}


# ── #1378: "24h" was a misnomer across weekends and feed gaps ────────────────
#
# `change_24h_pct` is `_pct_change(prices, 1)` — a ONE-BAR change. On a 24/7
# crypto feed one bar is 24 hours. On an equity feed the Friday-to-Monday pair
# spans 72 hours and a mid-week holiday spans 48, and every one of those was
# labelled "24h" in the UI. These tests pin the measured window, not the label
# text, so they fail if the window stops being derived from the data.
#
# Mutation each test must fail against is named in its own docstring.


class TestChangeWindowMeasurement:
    def test_consecutive_daily_bars_are_a_24h_window(self):
        """Control. Fails if _change_window stops returning "24h" for the
        ordinary case, which would make the weekend assertions vacuous."""
        hours, label = _change_window(["2026-08-27", "2026-08-28"])
        assert hours == 24.0
        assert label == "24h"

    def test_a_weekend_gap_is_reported_as_three_days_not_24h(self):
        """The defect itself. Fails against any implementation that returns a
        constant "24h", including the pre-fix behaviour of having no window at
        all and letting the UI hardcode the string."""
        hours, label = _change_window(["2026-08-28", "2026-08-31"])  # Fri -> Mon
        assert hours == 72.0
        assert label == "3d"

    def test_a_midweek_holiday_gap_is_two_days(self):
        """Fails if the label is derived from a weekday calculation rather than
        elapsed time — a Tue->Thu gap has no weekend in it."""
        hours, label = _change_window(["2026-08-25", "2026-08-27"])
        assert hours == 48.0
        assert label == "2d"

    def test_a_long_weekend_is_four_days(self):
        hours, label = _change_window(["2026-08-28", "2026-09-01"])  # Fri -> Tue
        assert hours == 96.0
        assert label == "4d"

    def test_pandas_timestamps_are_handled_not_just_iso_strings(self):
        """The live path is a pd.Series with a DatetimeIndex; only the dict
        fallback carries ISO strings. Fails if _bar_timestamp only parses str,
        which would make every other test here pass while production got None."""
        pd = pytest.importorskip("pandas")
        idx = list(pd.to_datetime(["2026-08-28", "2026-08-31"]))
        hours, label = _change_window(idx)
        assert hours == 72.0
        assert label == "3d"

    def test_an_unknown_window_is_none_and_never_defaults_to_24h(self):
        """The load-bearing one. A guess of "24h" here would reintroduce the
        exact false claim, so absence must stay absent. Fails if any branch
        returns a default label."""
        assert _change_window([]) == (None, None)
        assert _change_window(["2026-08-30"]) == (None, None)
        assert _change_window(["not-a-date", "also-not"]) == (None, None)

    def test_an_out_of_order_index_reports_nothing_rather_than_a_negative_window(self):
        """Fails if the delta is taken as an absolute value, which would invent
        a plausible-looking window from an index we cannot actually trust."""
        assert _change_window(["2026-08-31", "2026-08-28"]) == (None, None)

    def test_mixed_tz_aware_and_naive_bars_do_not_raise(self):
        """Subtracting an aware from a naive datetime raises TypeError, and the
        two shapes co-occur here (pandas Timestamps carry a tz, bare ISO dates
        do not). Fails if the normalisation is dropped."""
        from datetime import UTC, datetime

        aware = datetime(2026, 8, 31, tzinfo=UTC)
        naive = datetime(2026, 8, 28)
        hours, label = _change_window([naive, aware])
        assert hours == 72.0
        assert label == "3d"

    def test_bar_timestamp_parses_each_shape_the_feed_emits(self):
        from datetime import date as _date
        from datetime import datetime as _dt

        assert _bar_timestamp("2026-08-28") == _dt(2026, 8, 28)
        assert _bar_timestamp(_date(2026, 8, 28)) == _dt(2026, 8, 28)
        assert _bar_timestamp(_dt(2026, 8, 28, 13, 30)) == _dt(2026, 8, 28, 13, 30)
        assert _bar_timestamp(None) is None
        assert _bar_timestamp(object()) is None


class TestServedItemCarriesItsChangeWindow:
    @pytest.mark.asyncio
    async def test_an_equity_over_a_weekend_is_served_as_3d_not_24h(self):
        """End-to-end on the served item. Fails if the service computes the
        window but does not put it on AssetExploreItem, which is the shape the
        UI actually reads."""
        service = AssetMarketService()
        mock_histories = {
            "sSPY": {
                "close": [540.0, 548.0],
                "dates": ["2026-08-28", "2026-08-31"],  # Fri -> Mon
            }
        }
        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sSPY"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sSPY": ("SPY", "SPY", "us_equity_etf", "NYSE")},
            ),
        ):
            resp = await service.list_assets()

        spy = next(a for a in resp.assets if a.symbol == "sSPY")
        # The value is unchanged — it was always a correct one-bar change.
        assert spy.change_24h_pct is not None
        # What changes is that the item now says what window that covers.
        assert spy.change_window_hours == 72.0
        assert spy.change_window_label == "3d"

    @pytest.mark.asyncio
    async def test_a_crypto_feed_still_says_24h(self):
        """Control against over-correction: 24/7 feeds genuinely do have a 24h
        window, and relabelling those would be a new false claim in the other
        direction."""
        service = AssetMarketService()
        mock_histories = {
            "sBTC": {
                "close": [60000.0, 61000.0],
                "dates": ["2026-08-29", "2026-08-30"],
            }
        }
        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sBTC"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sBTC": ("BTC-USD", "BTC", "crypto", "Coinbase")},
            ),
        ):
            resp = await service.list_assets()

        btc = next(a for a in resp.assets if a.symbol == "sBTC")
        assert btc.change_window_hours == 24.0
        assert btc.change_window_label == "24h"

    @pytest.mark.asyncio
    async def test_a_nan_bar_does_not_misalign_the_window_from_the_price(self):
        """The subtle one. The close list is filtered for NaN before the change
        is computed; if the date list is not filtered in step, the window gets
        measured between two bars the change was NOT computed from.

        Here the last close is NaN, so the change is taken across 08-24/08-28
        — a 4-day window. An unfiltered index would measure 08-28 to 08-31 and
        report 3d. Fails against zipping the raw index with the filtered closes."""
        pd = pytest.importorskip("pandas")
        series = pd.Series(
            [100.0, 105.0, float("nan")],
            index=pd.to_datetime(["2026-08-24", "2026-08-28", "2026-08-31"]),
        )
        service = AssetMarketService()
        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch(
                "archimedes.services.strategy_signal_evaluator._fetch_price_histories",
                return_value={"sSPY": series},
            ),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sSPY"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sSPY": ("SPY", "SPY", "us_equity_etf", "NYSE")},
            ),
        ):
            resp = await service.list_assets()

        spy = next(a for a in resp.assets if a.symbol == "sSPY")
        assert spy.change_window_hours == 96.0, "window must follow the bars the change used"
        assert spy.change_window_label == "4d"

    @pytest.mark.asyncio
    async def test_a_history_with_no_dates_serves_a_null_window_not_24h(self):
        """The dict fallback shape carries no dates. The item must say it does
        not know, so the UI renders "prev close" rather than the old "24h"."""
        service = AssetMarketService()
        mock_histories = {"sSPY": {"close": [540.0, 548.0]}}
        with (
            patch.object(service, "_read_oracle_prices", return_value={}),
            patch("archimedes.services.strategy_signal_evaluator._fetch_price_histories", return_value=mock_histories),
            patch("archimedes.services.asset_market_service._explore_universe", return_value=["sSPY"]),
            patch(
                "archimedes.services.strategy_signal_evaluator.GLOBAL_ASSETS",
                {"sSPY": ("SPY", "SPY", "us_equity_etf", "NYSE")},
            ),
        ):
            resp = await service.list_assets()

        spy = next(a for a in resp.assets if a.symbol == "sSPY")
        assert spy.change_24h_pct is not None
        assert spy.change_window_hours is None
        assert spy.change_window_label is None


# ── #1664: oracle fan-out width + TTL-vs-rebuild invariant ────────────────


def _wide_chain_client(symbols: list[str]):
    """A mock chain_client with an oracle AND synth address for every symbol.

    Deliberately configures *all* of them: that makes the address filter alone
    unable to narrow anything, so a passing fan-out test can only be passing
    because of the push-set intersection.
    """
    from pathlib import Path

    mock_settings = MagicMock()
    mock_settings.oracle_addresses = {s: f"0xo{i:039x}" for i, s in enumerate(symbols)}
    mock_settings.synth_addresses = {s: f"0xs{i:039x}" for i, s in enumerate(symbols)}
    mock_settings.abi_dir = str(Path(__file__).resolve().parents[2] / "contracts" / "abis")

    mock_client = MagicMock()
    mock_client.settings = mock_settings
    mock_client.to_checksum = lambda addr: addr
    return mock_client


class TestOracleFanOut:
    """#1664 — Explore must read the pushed oracles, not the whole universe.

    The prod shape walked ~281 SSOT symbols serially on every rebuild, and the
    rebuild ran essentially continuously because the cache TTL was shorter than
    the rebuild budget. Both halves are pinned here.
    """

    @pytest.mark.asyncio
    async def test_reads_only_the_pushed_intersection_not_the_universe(self):
        """A 300-symbol universe with a 3-symbol push set → exactly 3 getPrice calls.

        Adversarial check: drop the ``symbol in pushed`` clause from
        ``_read_oracle_prices``' candidate comprehension and this reports 300.
        """
        universe = [f"sSYM{i:03d}" for i in range(300)]
        pushed_equity = {"sSYM007": "SYM7", "sSYM100": "SYM100", "^GSPC": "^GSPC"}
        pushed_crypto = {"sSYM250": "some-coin"}

        service = AssetMarketService()
        mock_contract = MagicMock()
        mock_contract.functions.getPrice.return_value.call = AsyncMock(
            return_value=(100_000_000, int(time.time())),
        )
        mock_client = _wide_chain_client(universe)
        mock_client.w3 = MagicMock()
        mock_client.w3.eth.contract.return_value = mock_contract

        with (
            patch("archimedes.chain.client.chain_client", mock_client),
            patch("archimedes.chain.oracle_updater.YFINANCE_MAP", pushed_equity),
            patch("archimedes.chain.oracle_updater.CRYPTO_MAP", pushed_crypto),
        ):
            result = await service._read_oracle_prices(universe)

        assert mock_contract.functions.getPrice.call_count == 3, (
            f"expected 3 chain reads (the push set), got "
            f"{mock_contract.functions.getPrice.call_count} for a 300-symbol universe"
        )
        assert set(result) == {"sSYM007", "sSYM100", "sSYM250"}
        # ^GSPC is in YFINANCE_MAP but is a regime-signal index, never a synth
        # and never pushed on-chain — it must not become a read.
        assert "^GSPC" not in result

    @pytest.mark.asyncio
    async def test_pushed_symbol_without_addresses_is_not_read(self):
        """Push-set membership alone is not enough — the addresses must resolve."""
        universe = ["sSPY", "sBTC"]
        service = AssetMarketService()
        mock_contract = MagicMock()
        mock_contract.functions.getPrice.return_value.call = AsyncMock(
            return_value=(100_000_000, int(time.time())),
        )
        mock_client = _wide_chain_client(universe)
        del mock_client.settings.synth_addresses["sBTC"]  # oracle configured, synth not
        mock_client.w3 = MagicMock()
        mock_client.w3.eth.contract.return_value = mock_contract

        with (
            patch("archimedes.chain.client.chain_client", mock_client),
            patch("archimedes.chain.oracle_updater.YFINANCE_MAP", {"sSPY": "SPY"}),
            patch("archimedes.chain.oracle_updater.CRYPTO_MAP", {"sBTC": "bitcoin"}),
        ):
            result = await service._read_oracle_prices(universe)

        assert mock_contract.functions.getPrice.call_count == 1
        assert set(result) == {"sSPY"}

    @pytest.mark.asyncio
    async def test_reads_run_concurrently_not_serially(self):
        """Five 0.1s reads must cost ~one round trip, not five.

        Adversarial check: replace the ``asyncio.gather`` with a ``for`` loop
        that awaits ``_read_one`` per symbol and the elapsed time crosses 0.5s.
        """
        universe = [f"sSYM{i:03d}" for i in range(5)]
        read_delay = 0.1

        async def _slow_read(*_args, **_kwargs):
            await asyncio.sleep(read_delay)
            return (100_000_000, int(time.time()))

        service = AssetMarketService()
        mock_contract = MagicMock()
        mock_contract.functions.getPrice.return_value.call = AsyncMock(side_effect=_slow_read)
        mock_client = _wide_chain_client(universe)
        mock_client.w3 = MagicMock()
        mock_client.w3.eth.contract.return_value = mock_contract

        with (
            patch("archimedes.chain.client.chain_client", mock_client),
            patch("archimedes.chain.oracle_updater.YFINANCE_MAP", {s: s for s in universe}),
            patch("archimedes.chain.oracle_updater.CRYPTO_MAP", {}),
        ):
            started = time.monotonic()
            result = await service._read_oracle_prices(universe)
            elapsed = time.monotonic() - started

        assert len(result) == 5, "all five reads must still land"
        serial_cost = read_delay * len(universe)
        assert elapsed < serial_cost / 2, (
            f"reads took {elapsed:.3f}s; serial would be ~{serial_cost:.1f}s — fan-out is not concurrent"
        )


class TestCacheTtlInvariant:
    def test_cache_ttl_exceeds_rebuild_budget(self):
        """The TTL must outlast the worst-case rebuild it caches (#1664).

        Asserted as an invariant over the two budget constants, not against the
        literal 120, so a future budget increase re-trips this instead of
        silently restoring the always-refreshing state: a TTL shorter than the
        rebuild means every entry is born expired, every request re-kicks a
        background refresh, and the refresher never rests.
        """
        rebuild_budget = _ORACLE_TOTAL_BUDGET_SECONDS + _HISTORY_FETCH_BUDGET_SECONDS
        # Written budget-first only to satisfy ruff's SIM300 (it reads the
        # SCREAMING_CASE side as a literal); the invariant is unchanged —
        # _CACHE_TTL_SECONDS > oracle budget + history budget.
        assert rebuild_budget < _CACHE_TTL_SECONDS, (
            f"cache TTL {_CACHE_TTL_SECONDS}s must exceed the worst-case rebuild "
            f"budget {rebuild_budget}s (oracle {_ORACLE_TOTAL_BUDGET_SECONDS}s + "
            f"history {_HISTORY_FETCH_BUDGET_SECONDS}s)"
        )
