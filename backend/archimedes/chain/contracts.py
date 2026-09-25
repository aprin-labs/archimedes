"""Contract ABI loader and typed contract wrappers.

Loads ABIs from contracts/abis/ and creates web3 contract instances
pointing at deployed addresses from ChainSettings.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from web3.contract import AsyncContract

from archimedes.chain.client import ChainClient, chain_client


class ContractLoader:
    """Loads contract ABIs and creates web3 AsyncContract instances."""

    def __init__(self, client: ChainClient | None = None):
        self.client = client or chain_client
        self.settings = self.client.settings
        self._abi_cache: dict[str, list[Any]] = {}

    def _load_abi(self, name: str) -> list[Any]:
        if name in self._abi_cache:
            return self._abi_cache[name]

        abi_path = Path(self.settings.abi_dir) / f"{name}.json"
        if not abi_path.exists():
            raise FileNotFoundError(f"ABI not found: {abi_path}")

        with open(abi_path) as f:
            abi = json.load(f)

        self._abi_cache[name] = abi
        return abi

    def _contract(self, address: str, abi_name: str) -> AsyncContract:
        if not address:
            raise ValueError(f"Contract address not configured for {abi_name}")
        return self.client.w3.eth.contract(
            address=self.client.to_checksum(address),
            abi=self._load_abi(abi_name),
        )

    # ─── Core Contracts ──────────────────────────────────────────

    @property
    def amm_router(self) -> AsyncContract:
        return self._contract(self.settings.amm_router_address, "AMMRouter")

    @property
    def vault_factory(self) -> AsyncContract:
        return self._contract(self.settings.vault_factory_address, "VaultFactory")

    @property
    def synthetic_factory(self) -> AsyncContract:
        return self._contract(self.settings.synthetic_factory_address, "SyntheticFactory")

    @property
    def trace_registry(self) -> AsyncContract:
        return self._contract(self.settings.reasoning_trace_registry_address, "ReasoningTraceRegistry")

    @property
    def strategy_registry(self) -> AsyncContract:
        return self._contract(self.settings.strategy_registry_address, "StrategyRegistry")

    @property
    def asset_registry(self) -> AsyncContract:
        return self._contract(self.settings.asset_registry_address, "AssetRegistry")

    # ─── Third-party registries ──────────────────────────────────

    def erc8004_identity_registry(self, address: str) -> AsyncContract:
        """The ERC-8004 IdentityRegistry (#1527) — NOT one of ours.

        Takes the address as an argument rather than reading ``ChainSettings``: this
        contract is deployed and owned by the Arc ecosystem, not by
        ``deploy_contracts.py``, so it has no ``ARC_*`` deploy variable. Its address is
        resolved by ``chain.erc8004_identity.registry_address()``
        (``ERC8004_IDENTITY_REGISTRY``, defaulting to the Arc-testnet deployment). The
        pinned ABI is ``contracts/abis/ERC8004IdentityRegistry.json``.
        """
        return self._contract(address, "ERC8004IdentityRegistry")

    # ─── Per-Asset Oracle Contracts ──────────────────────────────

    def oracle_for(self, symbol: str) -> AsyncContract:
        addresses = self.settings.oracle_addresses
        if symbol not in addresses or not addresses[symbol]:
            raise ValueError(f"No oracle address for {symbol}")
        return self._contract(addresses[symbol], "PriceOracle")

    # ─── Token Contracts ─────────────────────────────────────────

    def token(self, address: str) -> AsyncContract:
        return self._contract(address, "SyntheticToken")

    def usdc(self) -> AsyncContract:
        return self._contract(self.settings.usdc_address, "SyntheticToken")

    # ─── Vault Contract (by address) ─────────────────────────────

    def vault(self, address: str) -> AsyncContract:
        return self._contract(address, "Vault")

    # ─── AMM Pool (by address) ───────────────────────────────────

    def amm_pool(self, address: str) -> AsyncContract:
        return self._contract(address, "AMMPool")


@lru_cache(maxsize=1)
def get_contract_loader() -> ContractLoader:
    return ContractLoader()
