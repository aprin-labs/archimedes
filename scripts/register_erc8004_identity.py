#!/usr/bin/env python3
"""Plan (and, for the owner only, execute) the ERC-8004 identity registration on Arc (#1527).

WHAT THIS DOES
    Builds the ``register(string agentURI)`` call against the ERC-8004 IdentityRegistry
    named in ``ui/public/.well-known/agent.json``. ``--plan`` (the default) is READ-ONLY:
    it prints the calldata, decodes it back, checks the registry and chain over JSON-RPC,
    and estimates gas. Nothing is signed and nothing is sent.

WHAT THIS DOES NOT DO
    It does not make Archimedes an ERC-8004 agent. Registration mints an ERC-721 to the
    platform wallet, and that wallet is a Circle dev-controlled wallet whose key Circle
    holds — this repo does not have it and must not have it. Until a human runs
    ``--execute`` and a live ``ownerOf()`` read confirms the mint, every discovery surface
    keeps saying ``status: registration_pending`` and that is the truth.

WHERE THE WORK ACTUALLY HAPPENS
    ``--execute`` and ``--verify`` are thin wrappers over
    ``backend/archimedes/chain/erc8004_identity.py``, which is the same code path the
    served manifest uses to decide whether it may say "registered". This file does not
    sign anything itself: it hands ``register(string)`` to
    ``chain.circle_signer.CircleSigner.execute_contract`` — the identical
    ``contractExecution`` seam ``oracle_updater.push_prices_on_chain`` pushes prices
    through. **No private key is read, held, or derived here.** (#1552's version signed
    with a raw ``OWNER_PRIVATE_KEY`` from the environment; that path is gone.)

THE STANDARD (https://eips.ethereum.org/EIPS/eip-8004)
    struct MetadataEntry { string metadataKey; bytes metadataValue; }
    function register(string agentURI, MetadataEntry[] calldata metadata) returns (uint256 agentId)
    function register(string agentURI)                                    returns (uint256 agentId)
    function register()                                                   returns (uint256 agentId)
    event    Registered(uint256 indexed agentId, string agentURI, address indexed owner)

    ``agentURI`` must resolve to the agent registration file. Ours is published at
    ``ui/public/.well-known/agent-registration.json`` and typed
    ``https://eips.ethereum.org/EIPS/eip-8004#registration-v1``. Because the endpoint
    domain serves it, it doubles as the EIP's optional domain-control proof.

NO RESTATED CONSTANTS
    The registry address, chain and agentURI are READ from the shipped agent card, which
    is the same block ``GET /api/agent/manifest`` serves. A value changed in one place and
    not the other fails ``backend/tests/test_erc8004_identity.py``, not here at 3am.

USAGE
    # read-only: calldata + registry/chain checks + gas estimate
    python scripts/register_erc8004_identity.py --plan
    python scripts/register_erc8004_identity.py --plan --from 0xOwner...
    python scripts/register_erc8004_identity.py --plan --offline      # no RPC at all

    # read-only: what does the chain say about our identity RIGHT NOW?
    ERC8004_OWNER_ADDRESS=0x... python scripts/register_erc8004_identity.py --verify
    ERC8004_OWNER_ADDRESS=0x... python scripts/register_erc8004_identity.py --verify --agent-id 7

    # owner only — NOT run by CI, agents, or the backend. Credentials are Circle's
    # (CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET / WALLET_ID), the same three the oracle uses:
    ERC8004_OWNER_ADDRESS=0x... python scripts/register_erc8004_identity.py --execute

THE LOG-SCAN WINDOW
    Discovering an existing agentId is an ``eth_getLogs`` scan for the mint. Arc's public
    RPC refuses a range wider than 10,000 blocks (``-32614``), and the chain is ~60,000,000
    blocks tall, so a scan from block 0 — the old default — is that error every time.
    ``--from-block`` now defaults to ``eth_blockNumber - 9,000``, resolved once and used by
    BOTH the pre-flight read and the confirming read. Pass ``--from-block`` explicitly to
    override it; the value is then used verbatim.

    A bounded window can be too NARROW as well as too wide: an identity minted before it
    is invisible to the scan. That is why ``find_agent_id`` RAISES when ``balanceOf`` says
    the wallet holds something the window cannot name, rather than answering "no identity
    found" — ``--verify`` prints ``status: undetermined`` and exits non-zero, and
    ``--execute`` refuses. ``--verify --agent-id <ID>`` reads ``ownerOf`` and scans nothing,
    so it is immune to the window entirely.

AFTER A SUCCESSFUL --execute
    The script prints the minted agentId and the exact follow-up: set ``ERC8004_AGENT_ID``
    and ``ERC8004_OWNER_ADDRESS`` in the deployment environment, and land the
    ``registrations`` entry in both registration files plus the agent card's regenerated
    ``erc8004`` block in one commit carrying the transaction hash. The env vars alone do
    NOT make a surface say "registered" — they only tell the verifier which token to read;
    the claim still comes from ``ownerOf()``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_CARD = REPO_ROOT / "ui" / "public" / ".well-known" / "agent.json"
REGISTRATION_FILE = REPO_ROOT / "ui" / "public" / ".well-known" / "agent-registration.json"

# ERC-8004 IdentityRegistry, from the EIP's interface. Kept as signature strings and
# hashed at runtime rather than pasted selectors, so a typo cannot silently produce
# well-formed calldata for a function that does not exist.
REGISTER_STRING_SIG = "register(string)"
REGISTERED_EVENT_SIG = "Registered(uint256,string,address)"
# The registry is an ERC-721, so the mint is a Transfer from the zero address and the token
# id — the agentId — is its fourth topic. That log, in the receipt of the transaction we
# already have, is the recovery path when the discovery scan cannot name the id.
TRANSFER_EVENT_SIG = "Transfer(address,address,uint256)"

DEFAULT_RPC = "https://rpc.testnet.arc.network"

# Arc's public RPC refuses an eth_getLogs range wider than 10,000 blocks. Verified live
# 2026-09-03 against https://rpc.testnet.arc.network:
#     {"code":-32614,"message":"eth_getLogs is limited to a 10,000 range"}
# The mint-log scan used to default to block 0, which on a ~60,000,000-block chain is that
# error every time — so a real mint would report ``action: submitted`` and print nothing,
# for a transaction that had actually landed. 9,000 rather than 10,000 leaves headroom for
# the blocks mined between resolving the window and running the scan.
LOG_RANGE_LIMIT = 10_000
DEFAULT_LOG_WINDOW = 9_000

# No CIRCLE_API_BASE / GAS_MULTIPLIER here any more: gas and the Circle endpoint are the
# signer's problem, and the signer is chain/circle_signer.py. A second copy of either
# constant in this file would be a second place for them to go stale.


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


# ── inputs, read from the shipped discovery surface ───────────────────────────────────


def load_identity_block() -> dict:
    """The ``erc8004`` block from the static agent card — the SSOT for this script."""
    if not AGENT_CARD.exists():
        raise SystemExit(f"✗ agent card not found: {AGENT_CARD}")
    card = json.loads(AGENT_CARD.read_text(encoding="utf-8"))
    block = card.get("erc8004")
    if not isinstance(block, dict):
        raise SystemExit(f"✗ {AGENT_CARD} has no erc8004 block — nothing to register against.")
    for key in ("chain", "identityRegistry", "registrationUri", "status"):
        if not block.get(key):
            raise SystemExit(f"✗ erc8004 block is missing {key!r}.")
    return block


def chain_id_from_caip2(chain: str) -> int:
    """``eip155:5042002`` → ``5042002``. Anything else is refused, not guessed."""
    namespace, _, reference = chain.partition(":")
    if namespace != "eip155" or not reference.isdigit():
        raise SystemExit(f"✗ erc8004.chain must be an eip155 CAIP-2 id, got {chain!r}.")
    return int(reference)


def load_chain_module():
    """Import the backend's ERC-8004 seam, adding ``backend/`` to the path like siblings do."""
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from archimedes.chain import erc8004_identity

    return erc8004_identity


# ── calldata ──────────────────────────────────────────────────────────────────────────


def build_register_calldata(agent_uri: str) -> bytes:
    """ABI-encoded ``register(string agentURI)``.

    The no-metadata overload on purpose: every field an ERC-8004 consumer needs is in
    the registration file the URI resolves to, so on-chain MetadataEntry rows would be a
    second copy of the same facts with its own way to go stale.
    """
    return selector(REGISTER_STRING_SIG) + abi_encode(["string"], [agent_uri])


def decode_register_calldata(calldata: bytes) -> str:
    """Decode our own calldata back. A plan you cannot read back is not a plan."""
    if calldata[:4] != selector(REGISTER_STRING_SIG):
        raise ValueError("calldata does not start with the register(string) selector")
    (agent_uri,) = abi_decode(["string"], calldata[4:])
    return agent_uri


# ── JSON-RPC (read-only helpers) ──────────────────────────────────────────────────────


def rpc(client: httpx.Client, url: str, method: str, params: list) -> object:
    resp = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"{method} → {body['error']}")
    return body.get("result")


def owner_address_from_env() -> str | None:
    """The platform wallet, from ``ERC8004_OWNER_ADDRESS``.

    An ADDRESS, never a key. The wallet's key lives with Circle; this script only ever
    needs to know which address to estimate gas from and which owner to verify against.
    """
    return os.environ.get("ERC8004_OWNER_ADDRESS", "").strip() or None


def head_block(client: httpx.Client, rpc_url: str) -> int:
    return int(str(rpc(client, rpc_url, "eth_blockNumber", [])), 16)


def resolve_from_block(raw: object, rpc_url: str, *, client: httpx.Client | None = None) -> tuple[int | str, str]:
    """The first block of the mint-log scan, plus the line that explains where it came from.

    ``raw is None`` (no ``--from-block``) means *work it out*: ask the RPC for the head and
    subtract :data:`DEFAULT_LOG_WINDOW`. Block 0 is NOT a safe default on Arc — the public
    RPC caps ``eth_getLogs`` at :data:`LOG_RANGE_LIMIT` blocks and refuses anything wider
    with ``-32614``, so the old default guaranteed that the confirming read after a mint
    would fail on a chain 60 million blocks tall.

    A value the operator passed is used verbatim, including the string block tags web3
    accepts (``earliest``/``latest``/…): an explicit instruction is not second-guessed.

    An unreachable RPC falls back to 0 and says so *loudly* rather than inventing a height.
    A guessed window would silently scan the wrong range and report "no identity found",
    which is the one answer that must never be manufactured — it is the input that decides
    whether a second, un-burnable identity gets minted.
    """
    if raw is not None:
        text = str(raw).strip()
        if text.lower() in {"earliest", "latest", "pending", "safe", "finalized"}:
            return text.lower(), f"scanning from block tag {text.lower()!r} (--from-block, explicit)"
        try:
            value = int(text, 0)
        except ValueError:
            raise SystemExit(f"✗ --from-block must be a block number or a block tag, got {raw!r}") from None
        if value < 0:
            raise SystemExit(f"✗ --from-block must not be negative, got {value}")
        return value, f"scanning from block {value} (--from-block, explicit)"

    try:
        if client is None:
            with httpx.Client(timeout=30.0) as owned:
                head = head_block(owned, rpc_url)
        else:
            head = head_block(client, rpc_url)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return 0, (
            f"! could not read eth_blockNumber from {rpc_url} ({exc}) — falling back to block 0. "
            f"The public Arc RPC refuses eth_getLogs ranges wider than {LOG_RANGE_LIMIT:,} blocks "
            "(-32614), so this scan will probably be REFUSED. Pass --from-block <recent block>."
        )

    start = max(0, head - DEFAULT_LOG_WINDOW)
    return start, (
        f"scanning blocks {start}..latest — a {head - start:,}-block window below head {head:,}, "
        f"because the public Arc RPC refuses eth_getLogs ranges wider than {LOG_RANGE_LIMIT:,} (-32614)"
    )


def preflight(client: httpx.Client, rpc_url: str, registry: str, want_chain_id: int) -> list[str]:
    """Chain-id and code-at-address checks. Returns human problem lines (empty = clean).

    Both are read-only and both are worth a round-trip: registering against an address
    with no code, or on the wrong chain, burns real gas to accomplish nothing.
    """
    problems: list[str] = []
    chain_id = int(str(rpc(client, rpc_url, "eth_chainId", [])), 16)
    print(f"  rpc chain id:      {chain_id}")
    if chain_id != want_chain_id:
        problems.append(f"RPC reports chain {chain_id}, the agent card says {want_chain_id}")

    code = str(rpc(client, rpc_url, "eth_getCode", [registry, "latest"]) or "0x")
    size = max(len(code) - 2, 0) // 2
    print(f"  registry bytecode: {size} bytes")
    if size == 0:
        problems.append(
            f"no contract code at {registry} on chain {chain_id} — the IdentityRegistry is "
            "either not deployed here or the address is wrong; registering would send USDC "
            "gas to an EOA and mint nothing"
        )
    return problems


def estimate_gas(client: httpx.Client, rpc_url: str, sender: str, registry: str, calldata: bytes) -> int:
    return int(
        str(rpc(client, rpc_url, "eth_estimateGas", [{"from": sender, "to": registry, "data": "0x" + calldata.hex()}])),
        16,
    )


# ── plan ──────────────────────────────────────────────────────────────────────────────


def plan(args: argparse.Namespace) -> int:
    block = load_identity_block()
    registry = block["identityRegistry"]
    chain_id = chain_id_from_caip2(block["chain"])
    agent_uri = args.agent_uri or block["registrationUri"]
    calldata = build_register_calldata(agent_uri)

    print("ERC-8004 identity registration — PLAN (read-only, nothing signed, nothing sent)")
    print(f"  status now:        {block['status']}  (agentId={block['agentId']!r}, tokenURI={block['tokenURI']!r})")
    print(f"  chain:             {block['chain']}  (chain id {chain_id})")
    print(f"  identityRegistry:  {registry}")
    print(f"  function:          {REGISTER_STRING_SIG}   selector 0x{selector(REGISTER_STRING_SIG).hex()}")
    print(f"  agentURI:          {agent_uri}")
    print(f"  calldata:          0x{calldata.hex()}")
    print(f"  decodes back to:   {decode_register_calldata(calldata)!r}")

    if not REGISTRATION_FILE.exists():
        print(f"  ! registration file missing at {REGISTRATION_FILE} — agentURI would resolve to nothing.")
    else:
        doc = json.loads(REGISTRATION_FILE.read_text(encoding="utf-8"))
        print(f"  registration file: {REGISTRATION_FILE.relative_to(REPO_ROOT)}  type={doc.get('type')!r}")

    if block.get("agentId") is not None:
        print(
            f"  ! the agent card already publishes agentId {block['agentId']} — run --verify before "
            "--execute; registering again would mint a SECOND identity."
        )

    if args.offline:
        print("  gas estimate:      skipped (--offline)")
        return 0

    sender = args.sender or owner_address_from_env()
    problems: list[str] = []
    try:
        with httpx.Client(timeout=30.0) as client:
            print(f"  rpc:               {args.rpc}")
            problems = preflight(client, args.rpc, registry, chain_id)
            if sender is None:
                print("  gas estimate:      unavailable — no sender (pass --from 0x..., or set ERC8004_OWNER_ADDRESS)")
            elif problems:
                print(f"  gas estimate:      not attempted — preflight failed (sender would be {sender})")
            else:
                gas = estimate_gas(client, args.rpc, sender, registry, calldata)
                gas_price = int(str(rpc(client, args.rpc, "eth_gasPrice", [])), 16)
                # USDC is the native gas token on Arc, 18-decimal at the RPC level.
                cost = gas * gas_price / 10**18
                print(f"  sender:            {sender}")
                print(f"  gas estimate:      {gas}  @ gasPrice {gas_price}  ≈ {cost:.6f} USDC")
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        # An unreachable or unhealthy RPC is a real, reportable condition — it must not
        # masquerade as a clean plan.
        print(f"  ! RPC checks could not complete: {exc}")
        print("  ! calldata above is still correct — it is built locally — but the registry, chain")
        print("    and gas were NOT verified. Re-run with a reachable --rpc before executing.")
        return 1

    for problem in problems:
        print(f"  ✗ {problem}")
    if problems:
        print("Plan is NOT safe to execute. Fix the above first.")
        return 1
    print("Plan looks consistent. Execution is a separate, owner-only act: --execute")
    return 0


# ── verify + execute (owner only) ─────────────────────────────────────────────────────
#
# Both delegate to backend/archimedes/chain/erc8004_identity.py so the script and the
# served manifest reach the same verdict from the same code. A CLI that had its own idea
# of what "registered" means is how the two drift.


def verify(args: argparse.Namespace) -> int:
    """Read the registry and print what it actually says. Signs nothing, sends nothing."""
    import asyncio

    erc8004 = load_chain_module()
    owner = args.sender or owner_address_from_env()
    if not owner:
        print("\u2717 no owner address — set ERC8004_OWNER_ADDRESS or pass --from 0x…")
        print("  Verification compares ownerOf(agentId) against a specific wallet; without one")
        print("  there is nothing to compare and nothing can be confirmed.")
        return 2

    agent_id = args.agent_id if args.agent_id is not None else erc8004.configured_agent_id()
    if agent_id is None:
        # The discovery scan is an eth_getLogs range, so it gets the SAME bounded window
        # --execute uses. Before this, --verify dropped --from-block on the floor and
        # scanned from block 0 — harmless while balanceOf == 0 short-circuits it, and a
        # -32614 traceback the moment there is an identity to find.
        from_block, note = resolve_from_block(args.from_block, args.rpc)
        print(f"scan:       {note}")
        try:
            found, detail = asyncio.run(erc8004.find_agent_id(owner, from_block=from_block))
        except Exception as exc:
            # "I could not look" is not "there is nothing there". find_agent_id raises for
            # a refused range AND for the balanceOf > 0 / nothing-in-this-window case, and
            # neither may be printed as registration_pending: that line is what step 2 of
            # the runbook reads as "step 3 (--execute) is safe", and --execute on a wallet
            # that already holds an identity mints a second one that cannot be un-minted.
            print(f"discovery:  could not look ({type(exc).__name__}: {exc})")
            print("status:     undetermined  \u2014 NOT 'no identity found'. Nothing was proved either way.")
            print("            Re-run as --verify --agent-id <ID> (reads ownerOf directly, scans no logs),")
            print("            or widen the scan with --from-block <block below the mint>.")
            print("            Do NOT run --execute off this answer.")
            return 1
        print(f"discovery:  {detail}")
        if found is None:
            print("status:     registration_pending (no identity found for this wallet)")
            return 0
        agent_id = found

    result = asyncio.run(erc8004.verify_identity(agent_id=agent_id, expected_owner=owner))
    print(f"registry:   {erc8004.registry_address()}")
    print(f"agentId:    {result.agent_id}")
    print(f"owner:      {result.owner}  (expected {result.expected_owner})")
    print(f"tokenURI:   {result.token_uri!r}")
    print(f"status:     {result.status}  [source: {result.source}]")
    print(f"detail:     {result.detail}")
    # A read that did not complete is not a verdict, so it is not a success exit code.
    return 0 if result.source != erc8004.SOURCE_UNAVAILABLE else 1


def execute(args: argparse.Namespace) -> int:
    """Register, idempotently, through the Circle signer. Never touches a private key."""
    import asyncio

    erc8004 = load_chain_module()
    block = load_identity_block()
    agent_uri = args.agent_uri or block["registrationUri"]
    owner = args.sender or owner_address_from_env()
    if not owner:
        print("\u2717 ERC8004_OWNER_ADDRESS is not set (or pass --from 0x…).")
        print("  It is the Circle wallet that will own the minted identity — without it the")
        print("  result cannot be verified afterwards, and an unverifiable mint is not a")
        print("  registration this project is willing to publish.")
        return 2

    # Both chain reads around the mint — the idempotency check before and the confirming
    # scan after — run through this one window, so what proved the wallet was empty is the
    # same range that later finds what it minted.
    from_block, note = resolve_from_block(args.from_block, args.rpc)

    print(f"EXECUTE: {REGISTER_STRING_SIG} \u2192 {erc8004.registry_address()}")
    print(f"  agentURI:  {agent_uri}")
    print(f"  owner:     {owner}")
    print("  signer:    Circle dev-controlled wallet (CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET / WALLET_ID)")
    print(f"  scan:      {note}")

    result = asyncio.run(
        erc8004.register_identity(
            agent_uri=agent_uri,
            expected_owner=owner,
            agent_id=args.agent_id,
            from_block=from_block,
            allow_second_identity=args.allow_second_identity,
        )
    )
    print(f"  action:    {result.action}")
    print(f"  agentId:   {result.agent_id}")
    print(f"  tokenURI:  {result.token_uri!r}")
    print(f"  tx:        {result.tx_hash}")
    print(f"  detail:    {result.detail}")

    if result.action in {"noop", "registered"} and result.agent_id is not None:
        print()
        return print_followup(result.agent_id)

    if result.action == "submitted":
        # The mint LANDED; only the read that names it did not. Printing nothing here is
        # how a real registration gets lost: the operator sees "submitted", has no id, and
        # the tempting next move is --allow-second-identity, which double-mints. The
        # agentId is already in the receipt of the transaction we just printed.
        print()
        print_followup(None, tx_hash=result.tx_hash, owner=owner, rpc_url=args.rpc)

    # "submitted" is not success: a transaction with no confirmed agentId behind it must
    # not be treated as a registration by a caller or a CI step.
    return 0 if result.action == "noop" else (0 if result.action == "registered" else 2)


# ── follow-up ─────────────────────────────────────────────────────────────────────────


def print_recovery(tx_hash: str | None, owner: str | None, rpc_url: str) -> None:
    """How to read the agentId out of a mint receipt when the discovery scan came up empty.

    This exists because the scan and the mint fail independently. ``register()`` can land
    on-chain while the ``eth_getLogs`` scan that would name the token is refused (a range
    cap, a rate limit, an RPC blip) — and in that state the id is not lost, it is sitting in
    the receipt of a transaction whose hash we are holding. No second transaction is needed,
    and minting one would be the expensive mistake.
    """
    print("THE TRANSACTION WENT OUT — the agentId is in its receipt, not in a second mint.")
    print()
    print(f"  tx: {tx_hash}")
    print(f"  cast receipt {tx_hash} --rpc-url {rpc_url}")
    print()
    print(f"  In its logs, find the mint — {TRANSFER_EVENT_SIG} from the registry:")
    print(f"    topics[0] == {topic(TRANSFER_EVENT_SIG)}   ({TRANSFER_EVENT_SIG})")
    print("    topics[1] == 0x0…0 (the zero address — that is what makes it a mint)")
    print(f"    topics[2] == the owner{f' ({owner})' if owner else ''}")
    print("    topics[3] IS THE AGENT ID  (hex — convert to decimal)")
    print()
    print("  Then confirm it against the chain and print the follow-up:")
    print("    python scripts/register_erc8004_identity.py --verify --agent-id <AGENT_ID>")
    print("    python scripts/register_erc8004_identity.py --print-followup <AGENT_ID>")
    print()
    print("  Do NOT re-run --execute and do NOT pass --allow-second-identity: the identity")
    print("  exists, and a second one cannot be un-minted.")
    print()


def print_followup(
    agent_id: int | None,
    *,
    tx_hash: str | None = None,
    owner: str | None = None,
    rpc_url: str = DEFAULT_RPC,
) -> int:
    """The exact edits that turn a real receipt into an honest 'registered' claim.

    ``agent_id=None`` is the post-mint-but-unconfirmed case: the same edits are printed
    with an ``<AGENT_ID>`` placeholder, behind :func:`print_recovery`'s instructions for
    getting the real number out of the receipt. The operator is holding a landed
    transaction either way, and the follow-up they need does not change because our scan
    missed.
    """
    if agent_id is None:
        print_recovery(tx_hash, owner, rpc_url)
    block = load_identity_block()
    shown: object = agent_id if agent_id is not None else "<AGENT_ID>"
    entry = {"agentId": shown, "agentRegistry": f"{block['chain']}:{block['identityRegistry']}"}
    rendered = json.dumps([entry], indent=2)
    if agent_id is None:
        # json.dumps quotes the placeholder; the real field is an integer, and a template
        # that ships the wrong JSON type is a template that gets pasted in wrong.
        rendered = rendered.replace('"<AGENT_ID>"', "<AGENT_ID>")
    print("Land these in ONE commit, with the transaction hash in the message:")
    print()
    print("1. ui/public/.well-known/agent-registration.json AND agent-registration.domain.json —")
    print('   replace "registrations": [] with:')
    print("   " + rendered.replace("\n", "\n   "))
    print()
    print("2. the DEPLOYMENT environment (SSM / .env — NOT a code constant):")
    print(f"   ERC8004_AGENT_ID={shown}")
    print("   ERC8004_OWNER_ADDRESS=<the Circle wallet address that owns it>")
    print("   These only tell the verifier WHICH token to read. The 'registered' claim still")
    print("   comes from a live ownerOf() call on every request — set them wrong and the")
    print("   manifest keeps saying registration_pending, which is the correct answer.")
    print()
    print("3. ui/public/.well-known/agent.json — regenerate the erc8004 block from")
    print("   erc8004_identity() so the card and the served manifest stay identical.")
    print()
    print(f"   Registered event topic0: {topic(REGISTERED_EVENT_SIG)}")
    print("   backend/tests/test_erc8004_identity.py enforces that all of these move together.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="read-only: calldata, registry/chain checks, gas (default)")
    mode.add_argument("--verify", action="store_true", help="read-only: what does the registry say right now?")
    mode.add_argument("--execute", action="store_true", help="OWNER ONLY: send register() via the Circle signer")
    mode.add_argument("--print-followup", type=int, metavar="AGENT_ID", help="print the post-registration edits")
    ap.add_argument("--rpc", default=os.environ.get("RPC") or os.environ.get("ARC_ARC_RPC_URL") or DEFAULT_RPC)
    ap.add_argument(
        "--from",
        dest="sender",
        default=None,
        help="the platform wallet ADDRESS (never a key) — gas estimation and ownerOf comparison",
    )
    ap.add_argument(
        "--agent-uri", default=None, help="override the agentURI (default: the agent card's registrationUri)"
    )
    ap.add_argument("--offline", action="store_true", help="plan only: build calldata, make no RPC calls")
    ap.add_argument("--agent-id", type=int, default=None, help="verify/execute against this agentId specifically")
    ap.add_argument(
        "--from-block",
        default=None,
        help=(
            "first block of the mint-log scan used to discover an existing agentId "
            f"(default: head − {DEFAULT_LOG_WINDOW:,}, because the public Arc RPC refuses "
            f"eth_getLogs ranges wider than {LOG_RANGE_LIMIT:,} blocks)"
        ),
    )
    ap.add_argument(
        "--allow-second-identity", action="store_true", help="permit --execute when an agentId already exists"
    )
    args = ap.parse_args()

    if args.print_followup is not None:
        return print_followup(args.print_followup)
    if args.verify:
        return verify(args)
    if args.execute:
        return execute(args)
    return plan(args)


if __name__ == "__main__":
    sys.exit(main())
