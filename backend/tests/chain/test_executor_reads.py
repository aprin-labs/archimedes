"""ChainExecutor read + setter coverage (#738 Tier-A).

Target: backend/archimedes/chain/executor.py
Complements test_chain_executor.py (which covers execute_trades / create_vault /
liquidity) by exercising the on-chain READ surface and the vault setters that the
agent tick depends on: read_portfolio, get_vault_metrics, get_all_vaults,
get_vault_count, set_token_oracles, set_target_allocations, and the token /
NAV helpers (_get_token_symbol, _get_token_decimals, _token_to_usdc,
_safe_total_assets, _parse_vault_created).

Hermetic: the ContractLoader and chain_client are mocked at the boundary — every
contract `.functions.X().call()` is an AsyncMock. No network, no Arc RPC, no Circle.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from archimedes.chain.executor import ChainExecutor
from hexbytes import HexBytes

USDC = "0x3600000000000000000000000000000000000000"
STSLA = "0xE745C07d7d32A1Ca0d6162A1c50e876619CF7388"


class _Awaitable:
    """await-able attribute wrapper (see test_chain_executor.py)."""

    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _coro():
            return self._value

        return _coro().__await__()


@pytest.fixture
def mock_loader():
    loader = MagicMock()
    mock_router = MagicMock()
    type(loader).amm_router = PropertyMock(return_value=mock_router)
    loader.amm_pool.return_value = MagicMock()
    loader.vault.return_value = MagicMock()
    loader.vault_factory = MagicMock()
    loader.oracle_for.return_value = MagicMock()
    loader.token.return_value = MagicMock()
    return loader


@pytest.fixture
def executor(mock_loader):
    with patch("archimedes.chain.executor.chain_client") as mock_cc:
        mock_cc.settings = MagicMock()
        mock_cc.settings.usdc_address = USDC
        mock_cc.settings.synth_addresses = {"sTSLA": STSLA}
        mock_cc.settings.oracle_addresses = {"sTSLA": "0xOracleTSLA"}
        mock_cc.settings.chain_id = 5042002
        mock_cc.settings.agent_account = None
        mock_cc.to_checksum = lambda addr: addr
        mock_cc.w3 = MagicMock()
        mock_cc.w3.eth = MagicMock()
        mock_cc.w3.eth.gas_price = _Awaitable(1_000_000_000)
        mock_cc.w3.eth.get_transaction_count = AsyncMock(return_value=1)
        mock_cc.w3.eth.send_raw_transaction = AsyncMock(return_value=HexBytes(b"\x00" * 32))
        mock_cc.w3.eth.wait_for_transaction_receipt = AsyncMock(return_value={"status": 1})
        ex = ChainExecutor(loader=mock_loader)
        ex._mock_cc = mock_cc
        yield ex


def _vault_fn(vault, name, return_value=None, side_effect=None):
    """Wire vault.functions.<name>().call() to an AsyncMock."""
    fn = getattr(vault.functions, name)
    fn.return_value.call = AsyncMock(return_value=return_value, side_effect=side_effect)


# ── read_portfolio ────────────────────────────────────────────


class TestReadPortfolio:
    def test_builds_portfolio_from_holdings(self, executor, mock_loader):
        vault = mock_loader.vault.return_value
        # getHoldings → ([token addrs], [raw amounts]); one synth + one zero leg.
        _vault_fn(vault, "getHoldings", return_value=[[STSLA, USDC], [2 * 10**18, 0]])
        _vault_fn(vault, "totalAssets", return_value=400_000_000)  # $400 (6 dec)
        # token symbol/decimals for the synth
        token = mock_loader.token.return_value
        _vault_fn(token, "symbol", return_value="sTSLA")
        _vault_fn(token, "decimals", return_value=18)
        # oracle price for value: 200 USDC (6 dec) per token. read_portfolio
        # uses the raw price() getter when totalAssets() succeeded.
        oracle = mock_loader.oracle_for.return_value
        _vault_fn(oracle, "getPrice", return_value=200_000_000)
        _vault_fn(oracle, "price", return_value=200_000_000)

        portfolio = asyncio.run(executor.read_portfolio("0xVault"))
        assert portfolio.vault_address == "0xVault"
        assert portfolio.total_value_usdc == pytest.approx(400.0)
        # The zero-amount USDC leg is skipped; only the synth holding remains.
        assert len(portfolio.holdings) == 1
        h = portfolio.holdings[0]
        assert h.symbol == "sTSLA"
        assert h.amount == pytest.approx(2.0)
        assert h.value_usdc == pytest.approx(400.0)
        assert h.weight == pytest.approx(1.0)

    def test_weights_sum_to_one_when_totalassets_excludes_synths(self, executor, mock_loader):
        """Regression for #1080: the live vault's on-chain totalAssets() counted
        only USDC, so weights computed against it had USDC at 100% and could
        never sum to 100. Weights must be computed against the sum of priced
        holdings instead."""
        vault = mock_loader.vault.return_value
        usdc_raw = 300_000_000  # $300 (6 dec)
        synth_raw = 1 * 10**18  # 1 token (18 dec) @ $100 → $100
        _vault_fn(vault, "getHoldings", return_value=[[USDC, STSLA], [usdc_raw, synth_raw]])
        _vault_fn(vault, "totalAssets", return_value=usdc_raw)  # USDC-only NAV
        token = mock_loader.token.return_value
        _vault_fn(token, "symbol", return_value="sTSLA")
        _vault_fn(token, "decimals", return_value=18)
        oracle = mock_loader.oracle_for.return_value
        _vault_fn(oracle, "getPrice", return_value=100_000_000)
        _vault_fn(oracle, "price", return_value=100_000_000)

        portfolio = asyncio.run(executor.read_portfolio("0xVault"))
        weights = {h.symbol: h.weight for h in portfolio.holdings}
        assert weights["USDC"] == pytest.approx(0.75)
        assert weights["sTSLA"] == pytest.approx(0.25)
        assert sum(weights.values()) == pytest.approx(1.0)
        assert all(0.0 <= w <= 1.0 for w in weights.values())
        # total_value_usdc uses the SAME denominator as the weights (sum of
        # priced holdings = $400), not the USDC-only on-chain totalAssets
        # ($300) — trade sizing and weight diffs must share one NAV.
        assert portfolio.total_value_usdc == pytest.approx(400.0)

    def test_unpriceable_synth_valued_zero_not_raw_amount(self, executor, mock_loader):
        """Regression for #1080: a synth whose oracle can't be read was valued
        at its raw 18-decimal base-unit amount, inflating value_usdc/weight by
        10**12 (live vault showed weight_pct of 3,070,572,228%)."""
        vault = mock_loader.vault.return_value
        usdc_raw = 300_000_000
        synth_raw = 595_357_148_956_678  # the live sGOLD raw amount
        _vault_fn(vault, "getHoldings", return_value=[[USDC, STSLA], [usdc_raw, synth_raw]])
        _vault_fn(vault, "totalAssets", return_value=usdc_raw)
        token = mock_loader.token.return_value
        _vault_fn(token, "symbol", return_value="sTSLA")
        _vault_fn(token, "decimals", return_value=18)
        oracle = mock_loader.oracle_for.return_value
        _vault_fn(oracle, "getPrice", side_effect=RuntimeError("StalePrice"))
        _vault_fn(oracle, "price", side_effect=RuntimeError("revert"))

        portfolio = asyncio.run(executor.read_portfolio("0xVault"))
        by_symbol = {h.symbol: h for h in portfolio.holdings}
        assert by_symbol["sTSLA"].value_usdc == 0.0
        assert by_symbol["sTSLA"].weight == 0.0
        assert by_symbol["USDC"].weight == pytest.approx(1.0)
        assert sum(h.weight for h in portfolio.holdings) == pytest.approx(1.0)
        # The unpriceable holding is flagged so trade sizing can refuse to act
        # on its fake-zero weight (#1080 review follow-up).
        assert by_symbol["sTSLA"].priced is False
        assert by_symbol["USDC"].priced is True

    def test_total_assets_revert_falls_back_to_offchain_nav(self, executor, mock_loader):
        vault = mock_loader.vault.return_value
        _vault_fn(vault, "getHoldings", return_value=[[USDC], [500_000_000]])  # $500 USDC
        _vault_fn(vault, "totalAssets", side_effect=RuntimeError("StalePrice"))
        portfolio = asyncio.run(executor.read_portfolio("0xVault"))
        # NAV recomputed off-chain: USDC leg contributes its raw amount directly.
        assert portfolio.total_value_usdc == pytest.approx(500.0)


# ── get_vault_metrics ─────────────────────────────────────────


class TestGetVaultMetrics:
    def test_reads_all_metric_fields(self, executor, mock_loader):
        vault = mock_loader.vault.return_value
        _vault_fn(vault, "totalAssets", return_value=1_000_000_000)  # $1000
        _vault_fn(vault, "totalSupply", return_value=1_000_000_000)
        _vault_fn(vault, "highWaterMark", return_value=123)
        _vault_fn(vault, "creator", return_value="0xCreator")
        _vault_fn(vault, "tier", return_value=1)
        _vault_fn(vault, "managementFeeBps", return_value=100)
        _vault_fn(vault, "performanceFeeBps", return_value=1500)
        _vault_fn(vault, "isAgentAssisted", return_value=True)
        _vault_fn(vault, "paused", return_value=False)

        m = asyncio.run(executor.get_vault_metrics("0xVault"))
        assert m["vault_address"] == "0xVault"
        assert m["total_aum_usdc"] == pytest.approx(1000.0)
        assert m["tier"] == 1
        assert m["management_fee_bps"] == 100
        assert m["is_agent_assisted"] is True
        # share_price = (totalAssets/totalSupply)/1e6. With equal raw units the
        # ratio is 1, scaled by /1e6 → 1e-6 (the contract's share decimals).
        assert m["share_price_usdc"] == pytest.approx(1e-6)
        assert m["total_supply"] == 1_000_000_000
        assert m["high_water_mark"] == 123
        assert m["creator"] == "0xCreator"

    def test_degrades_when_reads_revert(self, executor, mock_loader):
        vault = mock_loader.vault.return_value
        for fn in (
            "totalAssets",
            "totalSupply",
            "highWaterMark",
            "creator",
            "tier",
            "managementFeeBps",
            "performanceFeeBps",
            "isAgentAssisted",
            "paused",
        ):
            _vault_fn(vault, fn, side_effect=RuntimeError("revert"))
        # getHoldings also reverts so the stale-price fallback yields 0.
        _vault_fn(vault, "getHoldings", side_effect=RuntimeError("revert"))
        m = asyncio.run(executor.get_vault_metrics("0xVault"))
        assert m["total_aum_usdc"] == 0.0
        assert m["total_supply"] == 0
        # No supply → share price defaults to 1 USDC.
        assert m["share_price_usdc"] == pytest.approx(1.0)
        assert m["creator"] == "0x0000000000000000000000000000000000000000"
        assert m["tier"] == 2  # default
        assert m["is_agent_assisted"] is False
        # Fees degrade to None (unknown), NOT 0 — "0% fees" on a trust surface
        # would be a lie, and the #1138 guard fails closed on None (issue #1138).
        assert m["management_fee_bps"] is None
        assert m["performance_fee_bps"] is None


# ── get_vault_fee_bps (issue #1138 fee guard) ─────────────────


class TestGetVaultFeeBps:
    def test_returns_both_fee_bps_as_ints(self, executor, mock_loader):
        vault = mock_loader.vault.return_value
        _vault_fn(vault, "managementFeeBps", return_value=500)
        _vault_fn(vault, "performanceFeeBps", return_value=5000)
        assert asyncio.run(executor.get_vault_fee_bps("0xVault")) == (500, 5000)

    def test_raises_on_read_failure_so_callers_fail_closed(self, executor, mock_loader):
        """No silent zero-default: the #1138 guard must refuse a vault whose
        fees can't be verified, so the read raises instead of degrading."""
        vault = mock_loader.vault.return_value
        _vault_fn(vault, "managementFeeBps", side_effect=RuntimeError("revert"))
        _vault_fn(vault, "performanceFeeBps", return_value=0)
        with pytest.raises(RuntimeError):
            asyncio.run(executor.get_vault_fee_bps("0xVault"))


# ── get_all_vaults / get_vault_count ──────────────────────────


class TestVaultEnumeration:
    def test_get_all_vaults(self, executor, mock_loader):
        _vault_fn(mock_loader.vault_factory, "getVaults", return_value=["0xV1", "0xV2"])
        vaults = asyncio.run(executor.get_all_vaults())
        assert vaults == ["0xV1", "0xV2"]

    def test_get_vault_count(self, executor, mock_loader):
        _vault_fn(mock_loader.vault_factory, "vaultCount", return_value=7)
        assert asyncio.run(executor.get_vault_count()) == 7


# ── set_token_oracles / set_target_allocations (Circle path) ──


class TestVaultSettersCircle:
    def test_set_token_oracles_circle(self, executor, mock_loader):
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = True
            signer.execute_contract = AsyncMock(return_value="0xtx-oracles")
            tx = asyncio.run(executor.set_token_oracles("0xVault", [STSLA], ["0xOracleTSLA"]))
        assert tx == "0xtx-oracles"
        signer.execute_contract.assert_awaited_once()
        assert signer.execute_contract.await_args.kwargs["abi_function"] == "setTokenOracles(address[],address[])"

    def test_set_target_allocations_circle(self, executor, mock_loader):
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = True
            signer.execute_contract = AsyncMock(return_value="0xtx-alloc")
            tx = asyncio.run(executor.set_target_allocations("0xVault", [STSLA], [10000]))
        assert tx == "0xtx-alloc"
        assert signer.execute_contract.await_args.kwargs["abi_function"] == "setTargetAllocations(address[],uint256[])"


# ── set_target_allocations (raw-key path) + no-account guard ───


class TestVaultSettersRawKey:
    def test_set_target_allocations_raw_key(self, executor, mock_loader):
        account = MagicMock()
        account.address = "0xAGENT00000000000000000000000000000000aa"
        account.sign_transaction.return_value = MagicMock(raw_transaction=b"\x01")
        executor._mock_cc.settings.agent_account = account
        vault = mock_loader.vault.return_value
        vault.functions.setTargetAllocations.return_value.build_transaction = AsyncMock(
            return_value={"from": account.address, "nonce": 1}
        )
        sent = HexBytes("0x" + "ab" * 32)
        executor._mock_cc.w3.eth.send_raw_transaction = AsyncMock(return_value=sent)
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = False
            tx = asyncio.run(executor.set_target_allocations("0xVault", [STSLA], [10000]))
        # Unconditional exact assert (G11): the old form
        # `assert tx == sent.hex() if cond else "0x" + ...` parsed as
        # `assert (X if cond else <truthy string>)` — had HexBytes.hex() ever
        # dropped its 0x prefix, the assert would have silently passed on the
        # bare string regardless of tx.
        assert tx == sent.hex()

    def test_set_token_oracles_no_account_raises(self, executor):
        executor._mock_cc.settings.agent_account = None
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = False
            with pytest.raises(RuntimeError, match="No agent account"):
                asyncio.run(executor.set_token_oracles("0xVault", [STSLA], ["0xOracleTSLA"]))


# ── backend_signer_address / backend_signer_address_confirmed (#1403 review) ──


class TestBackendSignerAddressConfirmed:
    """``backend_signer_address`` surfaces WALLET_ADDRESS on the Circle path —
    an operator-maintained mirror of the real signer, not a derivation of it
    (signing itself is keyed on the separate WALLET_ID, circle_signer.py).
    ``backend_signer_address_confirmed`` must never return that mirror: on the
    Circle path it asks Circle what WALLET_ID signs with (#1412), and returns
    None whenever the answer cannot be established — so a stale mirror can
    never be mistaken for a confirmed signer identity by a caller that acts
    irreversibly on it (the reveal-reconciliation signer pre-check,
    agent_runner.py)."""

    def test_raw_key_path_is_confirmed(self, executor):
        account = MagicMock()
        account.address = "0xAGENT00000000000000000000000000000000aa"
        executor._mock_cc.settings.agent_account = account
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = False
            addr = executor.backend_signer_address()
            confirmed = asyncio.run(executor.backend_signer_address_confirmed())
        assert addr == account.address
        assert confirmed == account.address  # raw key IS the signer — always confirmed

    def test_raw_key_path_no_account_is_none_both_ways(self, executor):
        executor._mock_cc.settings.agent_account = None
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = False
            assert executor.backend_signer_address() is None
            assert asyncio.run(executor.backend_signer_address_confirmed()) is None

    def test_circle_path_confirmed_comes_from_circle_not_the_mirror(self, executor, monkeypatch):
        """#1412: the confirmed answer is what Circle says WALLET_ID signs
        with. WALLET_ADDRESS is set to something else entirely here; the
        confirmed variant must ignore it completely."""
        monkeypatch.setenv("WALLET_ADDRESS", "0xPOSSIBLYSTALE00000000000000000000000000")
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = True
            signer.get_wallet_address = AsyncMock(return_value="0xCIRCLETRUTH000000000000000000000000000a")
            addr = executor.backend_signer_address()
            confirmed = asyncio.run(executor.backend_signer_address_confirmed())
        assert addr == "0xPOSSIBLYSTALE00000000000000000000000000"  # the mirror, unchanged
        assert confirmed == "0xCIRCLETRUTH000000000000000000000000000a"
        assert confirmed != addr  # the two are genuinely different sources

    def test_circle_path_confirmed_works_when_the_mirror_is_unset(self, executor, monkeypatch):
        """WALLET_ADDRESS is optional to the confirmed path — it is never read
        there. With the mirror entirely absent the confirmation still lands."""
        monkeypatch.delenv("WALLET_ADDRESS", raising=False)
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = True
            signer.get_wallet_address = AsyncMock(return_value="0xCIRCLETRUTH000000000000000000000000000a")
            assert executor.backend_signer_address() is None
            assert asyncio.run(executor.backend_signer_address_confirmed()) == (
                "0xCIRCLETRUTH000000000000000000000000000a"
            )

    def test_circle_path_returns_the_mismatching_address_not_none(self, executor, monkeypatch):
        """When Circle reports an address that differs from the committer the
        caller is comparing against, the MISMATCHING address must come back —
        None would silently disarm the pre-check, and the reconciler needs the
        actual value to log which key it is now signing with."""
        monkeypatch.setenv("WALLET_ADDRESS", "0xOLDKEY000000000000000000000000000000000")
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = True
            signer.get_wallet_address = AsyncMock(return_value="0xROTATEDKEY0000000000000000000000000000b")
            confirmed = asyncio.run(executor.backend_signer_address_confirmed())
        assert confirmed == "0xROTATEDKEY0000000000000000000000000000b"

    def test_circle_path_lookup_failure_is_none_never_a_guess(self, executor, monkeypatch):
        """Anti-goal guard (#1412): when the Circle lookup itself fails,
        ``get_wallet_address`` returns None and the confirmed answer must stay
        None — NOT fall back to the WALLET_ADDRESS mirror, which is set here
        precisely so a fallback would be visible."""
        monkeypatch.setenv("WALLET_ADDRESS", "0xPOSSIBLYSTALE00000000000000000000000000")
        with patch("archimedes.chain.executor.circle_signer") as signer:
            signer.is_configured = True
            signer.get_wallet_address = AsyncMock(return_value=None)
            confirmed = asyncio.run(executor.backend_signer_address_confirmed())
        assert confirmed is None


# ── helpers: token symbol / decimals / value / parse ──────────


class TestTokenHelpers:
    def test_get_token_symbol_known_usdc(self, executor):
        assert asyncio.run(executor._get_token_symbol(USDC)) == "USDC"

    def test_get_token_symbol_known_synth(self, executor):
        assert asyncio.run(executor._get_token_symbol(STSLA)) == "sTSLA"

    def test_get_token_symbol_unknown_queries_contract(self, executor, mock_loader):
        token = mock_loader.token.return_value
        _vault_fn(token, "symbol", return_value="WHO")
        assert asyncio.run(executor._get_token_symbol("0x0000000000000000000000000000000000009999")) == "WHO"

    def test_get_token_symbol_contract_revert_truncates_address(self, executor, mock_loader):
        token = mock_loader.token.return_value
        _vault_fn(token, "symbol", side_effect=RuntimeError("no symbol"))
        addr = "0x0000000000000000000000000000000000009999"
        assert asyncio.run(executor._get_token_symbol(addr)) == addr[:8]

    def test_get_token_decimals_usdc_is_6(self, executor):
        assert asyncio.run(executor._get_token_decimals(USDC)) == 6

    def test_get_token_decimals_contract(self, executor, mock_loader):
        token = mock_loader.token.return_value
        _vault_fn(token, "decimals", return_value=8)
        assert asyncio.run(executor._get_token_decimals(STSLA)) == 8

    def test_get_token_decimals_revert_defaults_18(self, executor, mock_loader):
        token = mock_loader.token.return_value
        _vault_fn(token, "decimals", side_effect=RuntimeError("boom"))
        assert asyncio.run(executor._get_token_decimals(STSLA)) == 18

    def test_token_to_usdc_for_usdc_is_identity(self, executor):
        assert asyncio.run(executor._token_to_usdc(USDC, 12_345, 6)) == (12_345, True)

    def test_token_to_usdc_synth_with_getprice(self, executor, mock_loader):
        oracle = mock_loader.oracle_for.return_value
        _vault_fn(oracle, "getPrice", return_value=200_000_000)  # $200 (6 dec)
        # 2 tokens (18 dec) * 200_000_000 / 1e18 = 400_000_000 → $400
        result = asyncio.run(executor._token_to_usdc(STSLA, 2 * 10**18, 18))
        assert result == (400_000_000, True)

    def test_token_to_usdc_uses_raw_price_when_requested(self, executor, mock_loader):
        oracle = mock_loader.oracle_for.return_value
        _vault_fn(oracle, "price", return_value=100_000_000)
        result = asyncio.run(executor._token_to_usdc(STSLA, 1 * 10**18, 18, use_raw_price=True))
        assert result == (100_000_000, True)

    def test_token_to_usdc_getprice_revert_falls_back_to_raw_price(self, executor, mock_loader):
        oracle = mock_loader.oracle_for.return_value
        _vault_fn(oracle, "getPrice", side_effect=RuntimeError("StalePrice"))
        _vault_fn(oracle, "price", return_value=100_000_000)
        result = asyncio.run(executor._token_to_usdc(STSLA, 3 * 10**18, 18))
        assert result == (300_000_000, True)

    def test_token_to_usdc_synth_unpriceable_returns_zero_unpriced(self, executor, mock_loader):
        # Both getters revert → (0, priced=False): never the raw base-unit
        # amount (#1080), and the caller can tell "worth 0" from "unknown".
        oracle = mock_loader.oracle_for.return_value
        _vault_fn(oracle, "getPrice", side_effect=RuntimeError("StalePrice"))
        _vault_fn(oracle, "price", side_effect=RuntimeError("revert"))
        assert asyncio.run(executor._token_to_usdc(STSLA, 2 * 10**18, 18)) == (0, False)

    def test_token_to_usdc_unknown_token_returns_zero_unpriced(self, executor):
        # No oracle mapping → (0, priced=False), never the raw base-unit amount (#1080).
        assert asyncio.run(executor._token_to_usdc("0x0000000000000000000000000000000000008888", 42, 18)) == (0, False)


class TestParseVaultCreated:
    def test_extracts_vault_from_event(self):
        factory = MagicMock()
        factory.events.VaultCreated.return_value.process_log.return_value = {"args": {"vault": "0xNew"}}
        receipt = MagicMock()
        receipt.logs = [MagicMock()]
        assert ChainExecutor._parse_vault_created(factory, receipt) == "0xNew"

    def test_returns_none_when_no_matching_log(self):
        factory = MagicMock()
        factory.events.VaultCreated.return_value.process_log.side_effect = ValueError("not this log")
        receipt = MagicMock()
        receipt.logs = [MagicMock(), MagicMock()]
        assert ChainExecutor._parse_vault_created(factory, receipt) is None
