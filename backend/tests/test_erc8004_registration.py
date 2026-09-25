"""ERC-8004 registration and the live-read honesty path (#1527).

WHAT IS UNDER TEST
    ``archimedes.chain.erc8004_identity`` — the module that decides, from the chain,
    whether this deployment holds an ERC-8004 identity, and the runner that registers one
    through the Circle signing seam.

THE PROPERTY THAT MATTERS
    ``status: "registered"`` may leave this process only when a live ``ownerOf(agentId)``
    call returned the wallet we expect. Three ways to get that wrong, all tested here:

    1. Trust the configuration. ``ERC8004_AGENT_ID=7`` says *check token 7*, never *we own
       token 7* — see ``test_manifest_refuses_to_claim_registered_when_the_chain_names_a_different_owner``.
    2. Trust a failure. A dark RPC is not evidence of registration in either direction —
       see ``test_manifest_stays_pending_when_the_registry_read_fails``.
    3. Trust a file. #1552's script read ``agent.json`` to decide whether to register,
       which answers "did anyone commit a JSON edit" — see the idempotency tests, which
       drive the decision off ``balanceOf``/``ownerOf`` instead.

HERMETIC
    The chain boundary is ``ContractLoader.erc8004_identity_registry`` — the factory that
    hands back a web3 contract object with the RPC behind it. Patching there is the same
    shape as this repo's existing ``chain_client`` / ``chain_executor`` doubles: everything
    above it (calldata, ordering, the ownership comparison, the manifest wiring) runs for
    real, and nothing below it exists. The signer is stubbed at ``CircleSigner``'s public
    surface. **No private key, real or fake, appears anywhere in this file** — the whole
    point of the Circle seam is that this process never holds one.
"""

from __future__ import annotations

import asyncio

import pytest
from archimedes.chain import erc8004_identity as erc8004
from httpx import ASGITransport, AsyncClient

OURS = "0x1111111111111111111111111111111111111111"
THEIRS = "0x2222222222222222222222222222222222222222"
AGENT_URI = "https://archimedes-arc.com/.well-known/agent-registration.json"
TX_HASH = "0xfeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface"


# ── the chain double ─────────────────────────────────────────────────────────


class _Call:
    """web3's ``contract.functions.f(args)`` → object with an awaitable ``.call()``."""

    def __init__(self, fn):
        self._fn = fn

    async def call(self):
        return self._fn()


class FakeRegistry:
    """An ERC-721 IdentityRegistry with the four members this repo actually calls.

    Deliberately stubs the WHOLE surface the module uses (``ownerOf``, ``tokenURI``,
    ``balanceOf``, and the mint log the client reads separately), not only the members one
    test needs — a double that covers one path and silently drops a sibling's is the exact
    failure CLAUDE.md's rule 5 was written for.
    """

    def __init__(self, tokens=None, *, fail=None, delay=0.0):
        self.tokens = dict(tokens or {})  # token_id -> (owner, tokenURI)
        self.fail = fail  # exception to raise from any call
        self.delay = delay  # seconds to stall a call, for deadline tests
        self.calls: list[tuple] = []
        self.functions = self

    # -- mutation, used by the fake signer to model a mint --------------------
    def mint(self, owner: str, token_uri: str) -> int:
        token_id = max(self.tokens, default=0) + 1
        self.tokens[token_id] = (owner, token_uri)
        return token_id

    def mint_logs(self, owner: str) -> list[dict]:
        """``Transfer(0x0, owner, tokenId)`` logs, in the shape ``eth_getLogs`` returns."""
        return [
            {
                "topics": [
                    erc8004._TRANSFER_TOPIC,
                    erc8004._address_topic(erc8004.ZERO_ADDRESS),
                    erc8004._address_topic(holder),
                    hex(token_id),
                ]
            }
            for token_id, (holder, _uri) in sorted(self.tokens.items())
            if holder.lower() == owner.lower()
        ]

    # -- the contract surface -------------------------------------------------
    def _guard(self):
        if self.fail is not None:
            raise self.fail

    def ownerOf(self, token_id):
        self.calls.append(("ownerOf", token_id))

        def _run():
            self._guard()
            if token_id not in self.tokens:
                raise ValueError("execution reverted: ERC721NonexistentToken")
            return self.tokens[token_id][0]

        return _Call(_run)

    def tokenURI(self, token_id):
        self.calls.append(("tokenURI", token_id))

        def _run():
            self._guard()
            if token_id not in self.tokens:
                raise ValueError("execution reverted: ERC721NonexistentToken")
            return self.tokens[token_id][1]

        return _Call(_run)

    def balanceOf(self, owner):
        self.calls.append(("balanceOf", owner))

        def _run():
            self._guard()
            return sum(1 for holder, _ in self.tokens.values() if holder.lower() == owner.lower())

        return _Call(_run)

    async def _stall(self):
        await asyncio.sleep(self.delay)


class _SlowCall(_Call):
    def __init__(self, registry, fn):
        super().__init__(fn)
        self._registry = registry

    async def call(self):
        await self._registry._stall()
        return self._fn()


class StallingRegistry(FakeRegistry):
    """A registry whose calls never answer inside the budget — a dark RPC, not a slow one."""

    def ownerOf(self, token_id):
        inner = super().ownerOf(token_id)
        return _SlowCall(self, inner._fn)


class FakeClient:
    """The ``ChainClient`` surface ``find_agent_id`` touches: ``w3.eth.get_logs`` + checksum."""

    def __init__(self, registry: FakeRegistry, owner: str):
        self._registry = registry
        self._owner = owner
        self.settings = type("S", (), {"abi_dir": "/nonexistent"})()
        outer = self

        class _Eth:
            async def get_logs(self, params):
                outer.log_queries.append(params)
                return outer._registry.mint_logs(outer._owner)

        self.log_queries: list[dict] = []
        self.w3 = type("W3", (), {"eth": _Eth()})()

    @staticmethod
    def to_checksum(address: str) -> str:
        return address


class FakeSigner:
    """``CircleSigner``'s public surface, stubbed whole.

    ``sign_and_broadcast`` raises rather than being omitted: if the registration path ever
    reaches for the raw-transaction API instead of ``contractExecution``, that is a change
    a reviewer must see, not one a permissive double absorbs.
    """

    def __init__(self, registry: FakeRegistry | None = None, *, configured: bool = True, owner: str = OURS):
        self.is_configured = configured
        self.calls: list[dict] = []
        self._registry = registry
        self._owner = owner

    async def execute_contract(self, *, contract_address, abi_function, abi_params, fee_level="MEDIUM"):
        self.calls.append(
            {
                "contract_address": contract_address,
                "abi_function": abi_function,
                "abi_params": abi_params,
                "fee_level": fee_level,
            }
        )
        if self._registry is not None:
            self._registry.mint(self._owner, abi_params[0])
        return TX_HASH

    async def sign_and_broadcast(self, tx_object):
        raise AssertionError("registration must go through contractExecution, not raw signing")


@pytest.fixture
def registry(monkeypatch):
    """Install a registry double at the contract-factory boundary and hand it back."""
    reg = FakeRegistry()

    def _factory(self, address):
        return reg

    from archimedes.chain.contracts import ContractLoader

    monkeypatch.setattr(ContractLoader, "erc8004_identity_registry", _factory)
    return reg


def _install(monkeypatch, reg: FakeRegistry) -> None:
    from archimedes.chain.contracts import ContractLoader

    monkeypatch.setattr(ContractLoader, "erc8004_identity_registry", lambda self, address: reg)


# ── the live read ────────────────────────────────────────────────────────────


async def test_unconfigured_makes_no_claim_and_no_rpc_call(monkeypatch, registry):
    """The shipped state: nothing set, nothing claimed, nothing dialled.

    The "nothing dialled" half matters as much as the claim: this runs on every
    /api/agent/manifest request, and an unregistered deployment must not pay an RPC
    round-trip to be told what it already knows.
    """
    monkeypatch.delenv("ERC8004_AGENT_ID", raising=False)
    monkeypatch.delenv("ERC8004_OWNER_ADDRESS", raising=False)

    result = await erc8004.verify_identity()

    assert result.status == "registration_pending"
    assert result.source == "unconfigured"
    assert result.agent_id is None and result.token_uri is None
    assert registry.calls == [], f"an unconfigured identity dialled the chain: {registry.calls}"


async def test_a_live_ownerof_match_is_what_produces_registered(monkeypatch, registry):
    monkeypatch.setenv("ERC8004_AGENT_ID", "7")
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)
    registry.tokens[7] = (OURS, AGENT_URI)

    result = await erc8004.verify_identity()

    assert result.status == "registered"
    assert result.agent_id == 7
    assert result.token_uri == AGENT_URI
    assert result.source == "onchain"
    assert ("ownerOf", 7) in registry.calls


async def test_a_foreign_owner_is_refused_however_confident_the_config_is(monkeypatch, registry):
    """THE GUARD, shown rejecting: token 7 exists, and it is not ours.

    This is the realistic bad input — the configured id is a real, mintable token, so
    ``ownerOf`` succeeds and returns an address. Nothing about the shape of the response
    says "wrong"; only the comparison does.
    """
    monkeypatch.setenv("ERC8004_AGENT_ID", "7")
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)
    registry.tokens[7] = (THEIRS, AGENT_URI)

    result = await erc8004.verify_identity()

    assert result.status == "registration_pending"
    assert result.source == "onchain"  # we did read the chain; the chain said no
    assert result.agent_id is None, "a refused verification must not leak the id it refused"
    assert result.owner == THEIRS
    assert THEIRS in result.detail


async def test_a_nonexistent_token_id_is_refused(monkeypatch, registry):
    """The other bad pointer: an id nothing minted. ownerOf reverts; we do not claim."""
    monkeypatch.setenv("ERC8004_AGENT_ID", "999")
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)

    result = await erc8004.verify_identity()

    assert result.status == "registration_pending"
    assert result.source == "unavailable"
    assert result.agent_id is None


async def test_a_read_that_never_answers_is_unavailable_not_a_verdict(monkeypatch):
    """A dark RPC produces ``unavailable``, inside the budget, without claiming anything.

    ``unavailable`` and the ``onchain``-refusal above carry the same status on purpose —
    both are "we are not saying registered" — and different sources, because an operator
    reading a pending manifest needs to know whether the chain answered.
    """
    monkeypatch.setenv("ERC8004_AGENT_ID", "7")
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)
    stalling = StallingRegistry({7: (OURS, AGENT_URI)}, delay=5.0)
    _install(monkeypatch, stalling)

    result = await erc8004.verify_identity(budget_seconds=0.05)

    assert result.status == "registration_pending"
    assert result.source == "unavailable"
    assert result.agent_id is None


async def test_a_malformed_agent_id_is_loud_and_claims_nothing(monkeypatch, registry):
    monkeypatch.setenv("ERC8004_AGENT_ID", "seven")
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)

    result = await erc8004.verify_identity()

    assert result.status == "registration_pending"
    assert result.source == "unconfigured"
    assert registry.calls == []


# ── idempotency, from the chain ──────────────────────────────────────────────


async def test_already_registered_is_a_noop_that_surfaces_the_id(monkeypatch, registry):
    """The idempotency requirement: re-running the registration must not mint again."""
    monkeypatch.delenv("ERC8004_AGENT_ID", raising=False)
    registry.tokens[4] = (OURS, AGENT_URI)
    signer = FakeSigner(registry)
    client = FakeClient(registry, OURS)

    result = await erc8004.register_identity(agent_uri=AGENT_URI, expected_owner=OURS, signer=signer, client=client)

    assert result.action == "noop"
    assert result.agent_id == 4
    assert result.token_uri == AGENT_URI
    assert result.tx_hash is None
    assert signer.calls == [], "an already-registered agent sent a second register() transaction"


async def test_idempotency_uses_the_chain_not_the_committed_json(monkeypatch, registry):
    """A configured id short-circuits discovery — and still has to pass ``ownerOf``."""
    monkeypatch.setenv("ERC8004_AGENT_ID", "4")
    registry.tokens[4] = (OURS, AGENT_URI)
    signer = FakeSigner(registry)

    result = await erc8004.register_identity(agent_uri=AGENT_URI, expected_owner=OURS, signer=signer)

    assert result.action == "noop"
    assert result.agent_id == 4
    assert ("ownerOf", 4) in registry.calls
    assert signer.calls == []


async def test_fresh_registration_signs_through_circle_and_confirms_on_chain(monkeypatch, registry):
    """The happy path, end to end: empty wallet → one register(string) → confirmed read."""
    monkeypatch.delenv("ERC8004_AGENT_ID", raising=False)
    signer = FakeSigner(registry)
    client = FakeClient(registry, OURS)

    result = await erc8004.register_identity(agent_uri=AGENT_URI, expected_owner=OURS, signer=signer, client=client)

    assert len(signer.calls) == 1, "exactly one registration transaction"
    call = signer.calls[0]
    assert call["abi_function"] == "register(string)"
    assert call["abi_params"] == [AGENT_URI]
    assert call["contract_address"] == erc8004.registry_address()

    assert result.action == "registered"
    assert result.agent_id == 1
    assert result.token_uri == AGENT_URI
    assert result.tx_hash == TX_HASH


async def test_registration_refuses_to_mint_blind_when_the_registry_is_unreadable(monkeypatch):
    """The expensive mistake this guard exists to prevent: a second, un-burnable identity.

    A read failure must not be read as "nothing there". The wallet may well already hold an
    identity — we just could not see it — and minting a second permanently splits the
    reputation surface the whole standard exists to accumulate.
    """
    dark = FakeRegistry({4: (OURS, AGENT_URI)}, fail=ConnectionError("RPC unreachable"))
    _install(monkeypatch, dark)
    monkeypatch.setenv("ERC8004_AGENT_ID", "4")
    signer = FakeSigner(dark)

    result = await erc8004.register_identity(agent_uri=AGENT_URI, expected_owner=OURS, signer=signer)

    assert result.action == "refused"
    assert signer.calls == [], "minted an identity while blind to the registry"
    assert "refusing to mint blind" in result.detail


async def test_registration_refuses_when_the_named_token_belongs_to_someone_else(monkeypatch, registry):
    """A misconfigured owner is a configuration bug, not a licence to mint another."""
    monkeypatch.setenv("ERC8004_AGENT_ID", "4")
    registry.tokens[4] = (THEIRS, AGENT_URI)
    signer = FakeSigner(registry)

    result = await erc8004.register_identity(agent_uri=AGENT_URI, expected_owner=OURS, signer=signer)

    assert result.action == "refused"
    assert signer.calls == []


async def test_registration_refuses_without_circle_credentials(monkeypatch, registry):
    """No key path exists to fall back to — the refusal is the whole design."""
    monkeypatch.delenv("ERC8004_AGENT_ID", raising=False)
    signer = FakeSigner(registry, configured=False)
    client = FakeClient(registry, OURS)

    result = await erc8004.register_identity(agent_uri=AGENT_URI, expected_owner=OURS, signer=signer, client=client)

    assert result.action == "refused"
    assert "CIRCLE_API_KEY" in result.detail
    assert signer.calls == []


async def test_registration_refuses_without_an_owner_address(monkeypatch, registry):
    monkeypatch.delenv("ERC8004_OWNER_ADDRESS", raising=False)
    signer = FakeSigner(registry)

    result = await erc8004.register_identity(agent_uri=AGENT_URI, signer=signer)

    assert result.action == "refused"
    assert "ERC8004_OWNER_ADDRESS" in result.detail
    assert signer.calls == []


async def test_a_submitted_transaction_with_no_confirmed_id_is_not_a_registration(monkeypatch, registry):
    """A tx hash is not an identity. Until ``ownerOf`` confirms, nothing may be published."""
    monkeypatch.delenv("ERC8004_AGENT_ID", raising=False)
    # The signer succeeds but mints nothing — a reverted-on-chain tx that Circle reported
    # as complete, or a read racing the block.
    signer = FakeSigner(registry=None)
    client = FakeClient(registry, OURS)

    result = await erc8004.register_identity(agent_uri=AGENT_URI, expected_owner=OURS, signer=signer, client=client)

    assert result.action == "submitted"
    assert result.agent_id is None
    assert result.tx_hash == TX_HASH
    assert "verify" in result.detail.lower()


async def test_mint_log_discovery_re_checks_current_ownership(monkeypatch):
    """A mint log proves a token was minted here, not that it is still held.

    The registry is transferable ERC-721. Trusting the log alone would let a sold or moved
    identity keep being claimed, which is the cached-assumption bug in a different costume.
    """
    # Both tokens were minted to us; #2 has since been transferred away. #2 is the higher
    # id, so the newest-first scan reaches it FIRST — a discovery that stopped at the most
    # recent mint log would return it.
    reg = FakeRegistry({1: (OURS, AGENT_URI), 2: (THEIRS, AGENT_URI)})
    _install(monkeypatch, reg)
    client = FakeClient(reg, OURS)
    reg.mint_logs = lambda owner: [
        {
            "topics": [
                erc8004._TRANSFER_TOPIC,
                erc8004._address_topic(erc8004.ZERO_ADDRESS),
                erc8004._address_topic(OURS),
                hex(token_id),
            ]
        }
        for token_id in (1, 2)
    ]

    found, detail = await erc8004.find_agent_id(OURS, client)

    assert found != 2, "returned a token this wallet no longer owns"
    assert found == 1, detail
    assert ("ownerOf", 2) in reg.calls, "the newest mint log was never re-checked"


# ── the surface: /api/agent/manifest ─────────────────────────────────────────


async def _manifest() -> dict:
    from archimedes.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/agent/manifest")
    assert resp.status_code == 200
    return resp.json()


async def test_manifest_reports_registered_from_a_live_read(monkeypatch, registry):
    """The positive half of the pair below: when the chain agrees, we say so."""
    monkeypatch.setenv("ERC8004_AGENT_ID", "7")
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)
    registry.tokens[7] = (OURS, AGENT_URI)

    manifest = await _manifest()

    assert manifest["erc8004"]["status"] == "registered"
    assert manifest["erc8004"]["agentId"] == 7
    assert manifest["erc8004"]["tokenURI"] == AGENT_URI
    assert manifest["erc8004_verification"]["source"] == "onchain"


async def test_manifest_refuses_to_claim_registered_when_the_chain_names_a_different_owner(monkeypatch, registry):
    """REVERT DEMO — the honesty path, at the surface a consumer actually reads.

    Identical configuration to the passing test above: ``ERC8004_AGENT_ID=7`` is set, the
    token exists, the read completes. The ONLY difference is that ``ownerOf(7)`` answers
    with somebody else's address.

    Delete the ownership comparison in ``verify_identity`` — the one line that turns a
    completed read into a verdict — and this test fails while its sibling above keeps
    passing, because the config alone is enough to satisfy every other check. That
    asymmetry is the demonstration: the test is guarding the comparison, not the plumbing.
    """
    monkeypatch.setenv("ERC8004_AGENT_ID", "7")
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)
    registry.tokens[7] = (THEIRS, AGENT_URI)

    manifest = await _manifest()

    assert manifest["erc8004"]["status"] == "registration_pending"
    assert manifest["erc8004"]["agentId"] is None
    assert manifest["erc8004"]["tokenURI"] is None
    verification = manifest["erc8004_verification"]
    assert verification["source"] == "onchain"
    assert verification["owner"] == THEIRS
    assert verification["expectedOwner"] == OURS


async def test_manifest_stays_pending_when_the_registry_read_fails(monkeypatch):
    """Configured, but the RPC is dark: the surface refuses to claim, and says why.

    This is the fail-soft rule from docs/architectural-principles.md applied to a claim: the
    degraded state is a visible absence, never the plausible substitute of "well, the env
    var says 7, so 7 it is".
    """
    dark = FakeRegistry({7: (OURS, AGENT_URI)}, fail=ConnectionError("RPC unreachable"))
    _install(monkeypatch, dark)
    monkeypatch.setenv("ERC8004_AGENT_ID", "7")
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)

    manifest = await _manifest()

    assert manifest["erc8004"]["status"] == "registration_pending"
    assert manifest["erc8004"]["agentId"] is None
    assert manifest["erc8004_verification"]["source"] == "unavailable"
    assert "produced no ownership answer" in manifest["erc8004_verification"]["detail"]
