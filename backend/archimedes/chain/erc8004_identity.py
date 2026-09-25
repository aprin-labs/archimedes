"""ERC-8004 identity: the live on-chain read, and the owner-run registration (#1527).

WHAT THIS MODULE IS FOR
    #1552 published the spec-typed registration file and a manifest block that says
    ``registration_pending``. It said so because a module constant said so — a hand-edited
    ``_ERC8004_AGENT_ID = None``. That is the honest answer today and it is the WRONG
    MECHANISM for tomorrow: the day someone edits that constant to ``42``, every discovery
    surface starts claiming a registration that nothing verified. This module replaces the
    constant with a reading.

THE ONE RULE
    ``status == "registered"`` is returned from EXACTLY ONE place in this file
    (:func:`verify_identity`), and only when a live ``ownerOf(agentId)`` call against the
    ERC-8004 IdentityRegistry returned the address we expect to own it. Every other path —
    nothing configured, the RPC unreachable, the token owned by somebody else — returns
    ``registration_pending``. A configured ``ERC8004_AGENT_ID`` is a *pointer*, never a
    claim: it says which token to go and check, and the chain says whether the check passed.

CONFIGURATION (both required before anything can be verified)
    ``ERC8004_AGENT_ID``        the agentId the registry minted, recorded by the operator
                                from the registration receipt.
    ``ERC8004_OWNER_ADDRESS``   the platform wallet that must own it — the Circle
                                dev-controlled wallet that sent ``register()``.
    ``ERC8004_IDENTITY_REGISTRY`` (optional) the registry address; defaults to the Arc
                                testnet deployment.

    With either of the first two unset there is nothing to verify and nothing is claimed:
    ``source == "unconfigured"``, status pending, zero RPC calls. That is the shipped state
    on ``main`` today, so this module costs the manifest nothing until an owner registers.

SIGNING
    Registration is a state-changing call and goes out through
    :class:`~archimedes.chain.circle_signer.CircleSigner` — the same
    ``contractExecution`` seam ``oracle_updater.push_prices_on_chain`` uses. **No private
    key is read, held, derived or logged anywhere in this module or its tests.** Circle
    holds the key; this code holds a wallet id.

THE STANDARD (https://eips.ethereum.org/EIPS/eip-8004 — still Draft)
    The IdentityRegistry is an ERC-721. Registering mints a token to the sender; the token
    id IS the agentId; ``tokenURI(agentId)`` resolves to the registration file. The pinned
    ABI is ``contracts/abis/ERC8004IdentityRegistry.json`` — every function in it was
    probed against the live Arc deployment before it was written down (see the runbook,
    ``docs/runbooks/erc8004-identity-registration.md``). The EIP is Draft: if the registry
    is upgraded (it sits behind an ERC-1967 proxy) these selectors can change, and the
    failure mode is a read that reverts — which lands in ``source == "unavailable"`` and
    keeps the surface at pending. It cannot turn into a false ``registered``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from eth_utils import keccak

from archimedes.deadline import run_with_deadline

logger = logging.getLogger(__name__)

# The canonical Arc-testnet IdentityRegistry (vanity ``0x8004A8…`` prefix), per
# submodules/context-arc/docs/docs.arc.network/arc/tutorials/register-your-first-ai-agent.md.
DEFAULT_IDENTITY_REGISTRY = "0x8004A818BFB912233c491871b3d84c89A494BD9e"

ABI_NAME = "ERC8004IdentityRegistry"

# Circle's contractExecution takes the signature and the args, and does the ABI encoding.
# Same shape as the oracle's ``setPrice(uint256)`` call.
REGISTER_SIGNATURE = "register(string)"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Statuses. These are the strings the discovery surfaces publish, so they live here rather
# than being spelled out at each call site.
STATUS_REGISTERED = "registered"
STATUS_PENDING = "registration_pending"

# How we know what we know. Not a claim — a provenance label, reported beside the status so
# "the chain says we are not registered" is distinguishable from "we could not ask".
SOURCE_ONCHAIN = "onchain"  # a read completed and answered
SOURCE_UNCONFIGURED = "unconfigured"  # no agentId / owner to check — nothing was asked
SOURCE_UNAVAILABLE = "unavailable"  # the read did not complete; we do not know

# Wall-clock ceiling for the verification read on a request-serving path. The chain client
# already bounds each individual RPC (BoundedAsyncHTTPProvider, 3s), but ownerOf + tokenURI
# is two of them; this bounds the pair. Blowing it is reported as ``unavailable``.
_DEFAULT_VERIFY_BUDGET_SECONDS = 2.0

_TRANSFER_TOPIC = "0x" + keccak(text="Transfer(address,address,uint256)").hex()


# ── configuration ────────────────────────────────────────────────────────────


def registry_address() -> str:
    """The IdentityRegistry this deployment reads and registers against."""
    return os.getenv("ERC8004_IDENTITY_REGISTRY", DEFAULT_IDENTITY_REGISTRY)


def configured_agent_id() -> int | None:
    """The agentId to VERIFY. Never the agentId to publish — that comes from the chain."""
    raw = os.getenv("ERC8004_AGENT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        # Loud, not fatal: a malformed pointer means we verify nothing, which is the
        # pending state. Crashing the manifest over it would be worse.
        logger.warning("ERC8004_AGENT_ID=%r is not an integer — treating the identity as unconfigured", raw)
        return None


def configured_owner() -> str | None:
    """The platform wallet that must own the identity for us to claim it."""
    return os.getenv("ERC8004_OWNER_ADDRESS", "").strip() or None


def verify_budget_seconds() -> float:
    raw = os.getenv("ERC8004_VERIFY_BUDGET_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_VERIFY_BUDGET_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning("ERC8004_VERIFY_BUDGET_SECONDS=%r is not a number — using the default", raw)
        return _DEFAULT_VERIFY_BUDGET_SECONDS


# ── the reading ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IdentityVerification:
    """What a live read of the IdentityRegistry established, and how.

    ``status`` is the claim; ``source`` is the evidence class behind it. They are separate
    fields because "pending because the chain says no token" and "pending because the RPC
    was dark" are the same claim and very different operational facts, and collapsing them
    is how a silent outage gets read as a settled state.
    """

    status: str
    agent_id: int | None
    token_uri: str | None
    owner: str | None  # what ownerOf actually returned, when it returned
    expected_owner: str | None
    source: str
    detail: str

    @property
    def registered(self) -> bool:
        return self.status == STATUS_REGISTERED

    def as_dict(self) -> dict[str, Any]:
        """The shape published beside the manifest's ``erc8004`` block."""
        return {
            "status": self.status,
            "agentId": self.agent_id,
            "tokenURI": self.token_uri,
            "owner": self.owner,
            "expectedOwner": self.expected_owner,
            "source": self.source,
            "detail": self.detail,
        }


def _pending(
    source: str, detail: str, *, owner: str | None = None, expected_owner: str | None = None
) -> IdentityVerification:
    """Every not-registered outcome, built in one place so none of them can drift.

    Note what this constructor makes impossible: a pending verification carrying an
    ``agent_id`` or a ``token_uri``. A consumer that reads those fields without reading
    ``status`` still cannot be told we own something we have not proved we own.
    """
    return IdentityVerification(
        status=STATUS_PENDING,
        agent_id=None,
        token_uri=None,
        owner=owner,
        expected_owner=expected_owner,
        source=source,
        detail=detail,
    )


def _registry_contract(client=None):
    """The registry as a web3 contract, through the repo's existing ABI/address seam."""
    from archimedes.chain.contracts import ContractLoader, get_contract_loader

    loader = ContractLoader(client) if client is not None else get_contract_loader()
    return loader.erc8004_identity_registry(registry_address())


async def _read_owner_and_uri(agent_id: int, client=None) -> tuple[str, str, str]:
    """``ownerOf(agentId)`` and ``tokenURI(agentId)``. Returns (owner, tokenURI, note).

    ``ownerOf`` is load-bearing and is allowed to raise: a revert here means the token does
    not exist, and the caller turns that into ``unavailable`` rather than a claim.
    ``tokenURI`` is metadata — a registry that answers ``ownerOf`` but not ``tokenURI``
    still tells us who owns the identity, so its failure is recorded in the detail string
    instead of discarding a good ownership reading.
    """
    contract = _registry_contract(client)
    owner = await contract.functions.ownerOf(agent_id).call()
    try:
        token_uri = await contract.functions.tokenURI(agent_id).call()
    except Exception as exc:
        logger.warning("erc8004: tokenURI(%s) failed (%s) — ownership reading kept", agent_id, exc)
        return str(owner), "", f"tokenURI({agent_id}) could not be read: {exc}"
    return str(owner), str(token_uri), ""


async def verify_identity(
    *,
    agent_id: int | None = None,
    expected_owner: str | None = None,
    client=None,
    budget_seconds: float | None = None,
) -> IdentityVerification:
    """Establish, from the chain, whether this agent holds an ERC-8004 identity.

    **This is the only function in the codebase permitted to return
    ``status == "registered"``**, and it does so on exactly one condition: a completed
    ``ownerOf(agentId)`` returned ``expected_owner``. Not "the config says so", not "the
    last check said so" — a call that went out and came back this time.
    """
    agent_id = agent_id if agent_id is not None else configured_agent_id()
    expected_owner = expected_owner or configured_owner()

    if agent_id is None or not expected_owner:
        missing = " and ".join(
            n for n, v in (("ERC8004_AGENT_ID", agent_id), ("ERC8004_OWNER_ADDRESS", expected_owner)) if not v
        )
        return _pending(
            SOURCE_UNCONFIGURED,
            f"{missing} not set — no on-chain identity is configured, so none is claimed. "
            "Register with scripts/register_erc8004_identity.py --execute, then set both.",
            expected_owner=expected_owner,
        )

    budget = budget_seconds if budget_seconds is not None else verify_budget_seconds()
    try:
        owner, token_uri, uri_note = await run_with_deadline(
            _read_owner_and_uri(agent_id, client),
            budget,
            label=f"erc8004 ownerOf({agent_id})",
        )
    except Exception as exc:
        # Loud, greppable, and deliberately NOT a claim in either direction. The registry
        # may well hold our identity; we just did not manage to look.
        logger.warning(
            "ERC8004_VERIFY_UNAVAILABLE: could not read ownerOf(%s) on %s (%s: %s) — "
            "surfaces stay registration_pending",
            agent_id,
            registry_address(),
            type(exc).__name__,
            exc,
        )
        return _pending(
            SOURCE_UNAVAILABLE,
            f"the registry read produced no ownership answer ({type(exc).__name__}: {exc}); "
            "registration is neither confirmed nor denied by this response",
            expected_owner=expected_owner,
        )

    if owner.lower() != expected_owner.lower():
        logger.warning(
            "ERC8004_OWNER_MISMATCH: agentId %s on %s is owned by %s, not the configured %s",
            agent_id,
            registry_address(),
            owner,
            expected_owner,
        )
        return _pending(
            SOURCE_ONCHAIN,
            f"agentId {agent_id} exists but is owned by {owner}, not {expected_owner} — "
            "this deployment does not hold that identity",
            owner=owner,
            expected_owner=expected_owner,
        )

    return IdentityVerification(
        status=STATUS_REGISTERED,
        agent_id=agent_id,
        token_uri=token_uri or None,
        owner=owner,
        expected_owner=expected_owner,
        source=SOURCE_ONCHAIN,
        detail=f"ownerOf({agent_id}) == {owner} on {registry_address()}" + (f"; {uri_note}" if uri_note else ""),
    )


# ── discovery (used by the registration runner, not by request paths) ────────


def _address_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def _topic_int(topic: Any) -> int:
    """A log topic as an int, whether web3 handed back ``HexBytes`` or a hex string."""
    if isinstance(topic, (bytes, bytearray)):
        return int.from_bytes(bytes(topic), "big")
    return int(str(topic), 16)


async def find_agent_id(owner: str, client=None, from_block: int | str = 0) -> tuple[int | None, str]:
    """Which agentId (if any) ``owner`` already holds. Returns ``(agent_id, detail)``.

    There is no owner→agentId lookup on this registry (probed: ``getAgentId``,
    ``agentIdOf``, ``resolveByOwner`` and ``tokenOfOwnerByIndex`` all revert), so this is
    ``balanceOf`` followed by a mint-log scan: ``Transfer(0x0, owner, tokenId)``. Every
    candidate is then re-checked with ``ownerOf``, because a mint log proves the token was
    once minted to this wallet, not that it is still held.

    ``(None, detail)`` means "no identity found", and it is returned for exactly ONE fact:
    ``balanceOf == 0``. A raised exception means "could not look" — the RPC refused the
    range, or the wallet holds something this window could not name — and the caller must
    not read it as either. Those two must never share a return value: the caller's next
    move on "no identity found" is to MINT, and a second identity cannot be un-minted.
    """
    from archimedes.chain.client import chain_client

    c = client or chain_client
    contract = _registry_contract(client)
    balance = int(await contract.functions.balanceOf(c.to_checksum(owner)).call())
    if balance == 0:
        return None, f"balanceOf({owner}) == 0 — this wallet holds no ERC-8004 identity"

    logs = await c.w3.eth.get_logs(
        {
            "address": c.to_checksum(registry_address()),
            "fromBlock": from_block,
            "toBlock": "latest",
            "topics": [_TRANSFER_TOPIC, _address_topic(ZERO_ADDRESS), _address_topic(owner)],
        }
    )
    minted = sorted({_topic_int(log["topics"][3]) for log in logs}, reverse=True)
    for candidate in minted:
        current = str(await contract.functions.ownerOf(candidate).call())
        if current.lower() == owner.lower():
            return candidate, f"balanceOf == {balance}; mint log + ownerOf({candidate}) both name {owner}"

    # balanceOf says it holds something the mint logs cannot name — a transferred-in token,
    # or, far more likely, a bounded scan that started AFTER the mint. This is not "no
    # identity"; it is "this window could not see the identity that balanceOf just proved
    # exists". Returning ``None`` here would hand the register path the one answer that
    # makes it mint (``_resolve_existing`` → ``_pending`` → fall through to ``register()``),
    # for a wallet already holding a token — a double mint, from a fail-safe. It is a
    # raise, so the caller's existing "could not read the registry → refused" arm catches
    # it. See the returns contract above.
    raise RuntimeError(
        f"balanceOf({owner}) == {balance} but no mint log in range names a token this wallet still owns "
        f"(scanned from block {from_block}); pass the agentId explicitly"
    )


# ── registration (owner-run, Circle-signed) ──────────────────────────────────


@dataclass(frozen=True)
class RegistrationResult:
    """The outcome of a registration attempt.

    ``action`` is one of:

    ``noop``       already registered — the chain was read first and it said so.
    ``registered`` a ``register()`` transaction landed AND a follow-up read confirmed it.
    ``submitted``  the transaction went out but the confirming read did not complete. The
                   agentId is unknown, so nothing may be published yet.
    ``refused``    nothing was sent. The detail says what stopped it.
    """

    action: str
    agent_id: int | None
    token_uri: str | None
    tx_hash: str | None
    detail: str


async def register_identity(
    *,
    agent_uri: str,
    expected_owner: str | None = None,
    agent_id: int | None = None,
    signer=None,
    client=None,
    from_block: int | str = 0,
    allow_second_identity: bool = False,
) -> RegistrationResult:
    """Register this agent's identity, idempotently, via the Circle signing seam.

    Idempotency is a CHAIN READ, not a file check. ``#1552``'s script asked
    ``ui/public/.well-known/agent.json`` whether we were registered — which answers
    "did anyone commit a JSON edit", a question with no relationship to what the registry
    holds. A fresh clone, a stale branch, or a rolled-back commit all made that check say
    "not registered" while an identity existed on-chain, and the consequence of getting it
    wrong is a second minted identity that permanently splits our reputation surface.

    Order of operations:

    1. Read the chain. Already registered → ``noop`` with the agentId surfaced.
    2. Could not read the chain → ``refused``. Minting blind is the one thing worse than
       not minting (``allow_second_identity=True`` overrides, deliberately awkwardly).
    3. Otherwise submit ``register(agentURI)`` through :class:`CircleSigner`.
    4. Read the chain AGAIN to learn the minted agentId and confirm ownership. Only a
       confirmed read produces ``registered``.
    """
    expected_owner = expected_owner or configured_owner()
    if not expected_owner:
        return RegistrationResult(
            "refused",
            None,
            None,
            None,
            "ERC8004_OWNER_ADDRESS is not set. Registration mints to the Circle wallet, and "
            "without knowing which address that is, nothing can verify the result afterwards.",
        )

    # 1/2 — idempotency, from the chain.
    try:
        existing = await _resolve_existing(agent_id, expected_owner, client, from_block)
    except Exception as exc:
        if not allow_second_identity:
            return RegistrationResult(
                "refused",
                None,
                None,
                None,
                f"could not read the registry ({type(exc).__name__}: {exc}) — refusing to mint blind. "
                "A second identity cannot be un-minted; fix the RPC and re-run, or pass "
                "--allow-second-identity if a second identity is genuinely intended.",
            )
        logger.warning("erc8004: registry unreadable (%s) but --allow-second-identity was passed", exc)
        existing = None

    if existing is not None and existing.registered:
        return RegistrationResult(
            "noop",
            existing.agent_id,
            existing.token_uri,
            None,
            f"already registered as agentId {existing.agent_id} ({existing.detail}) — nothing sent",
        )
    if existing is not None and existing.owner is not None and not allow_second_identity:
        # A token exists under a different owner than the one we were told to expect. That
        # is a configuration error, not a reason to mint another one.
        return RegistrationResult(
            "refused",
            None,
            None,
            None,
            f"{existing.detail}. Refusing to mint a second identity; fix ERC8004_OWNER_ADDRESS "
            "(or --agent-id) so the check names the right token.",
        )

    # 3 — sign and send. No key material passes through this process.
    if signer is None:
        from archimedes.chain.circle_signer import CircleSigner

        signer = CircleSigner()
    if not getattr(signer, "is_configured", False):
        return RegistrationResult(
            "refused",
            None,
            None,
            None,
            "Circle credentials are not configured (CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET / WALLET_ID). "
            "Registration is signed by Circle's dev-controlled wallet — this process never holds a key.",
        )

    tx_hash = await signer.execute_contract(
        contract_address=registry_address(),
        abi_function=REGISTER_SIGNATURE,
        abi_params=[agent_uri],
    )
    logger.info("erc8004: register(%s) sent to %s — tx %s", agent_uri, registry_address(), tx_hash)

    # 4 — confirm. A tx hash is not an identity; ownerOf is.
    try:
        minted, detail = await find_agent_id(expected_owner, client, from_block)
    except Exception as exc:
        return RegistrationResult(
            "submitted",
            None,
            None,
            tx_hash,
            f"register() was submitted (tx {tx_hash}) but the confirming read failed "
            f"({type(exc).__name__}: {exc}). Do NOT publish an agentId until "
            "`--verify --agent-id <id>` confirms one.",
        )
    if minted is None:
        return RegistrationResult(
            "submitted",
            None,
            None,
            tx_hash,
            f"register() was submitted (tx {tx_hash}) but no identity is readable yet: {detail}. "
            "Re-run --verify once the transaction has settled.",
        )

    confirmed = await verify_identity(agent_id=minted, expected_owner=expected_owner, client=client)
    if not confirmed.registered:
        return RegistrationResult(
            "submitted",
            None,
            None,
            tx_hash,
            f"register() was submitted (tx {tx_hash}) and agentId {minted} was found, but the "
            f"confirming ownership read did not pass: {confirmed.detail}",
        )
    return RegistrationResult(
        "registered",
        confirmed.agent_id,
        confirmed.token_uri,
        tx_hash,
        f"minted and confirmed: {confirmed.detail} (tx {tx_hash})",
    )


async def _resolve_existing(
    agent_id: int | None,
    expected_owner: str,
    client,
    from_block: int | str,
) -> IdentityVerification:
    """The pre-flight chain read: is there already an identity for this wallet?

    Raises if the chain could not be read — the caller turns that into a refusal, because
    "I could not check" and "there is nothing there" must not share a return value.
    """
    candidate = agent_id if agent_id is not None else configured_agent_id()
    if candidate is None:
        candidate, detail = await find_agent_id(expected_owner, client, from_block)
        if candidate is None:
            return _pending(SOURCE_ONCHAIN, detail, expected_owner=expected_owner)
    verified = await verify_identity(agent_id=candidate, expected_owner=expected_owner, client=client)
    if verified.source == SOURCE_UNAVAILABLE:
        raise RuntimeError(verified.detail)
    return verified
