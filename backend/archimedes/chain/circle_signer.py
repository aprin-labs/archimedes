"""Circle Developer-Controlled Wallet signer — executes on-chain txs via Circle API.

Replaces raw private key signing with Circle's managed wallet infrastructure.
Uses the same pattern as oracle_updater.py: encrypt entity secret with Circle's
RSA public key, submit contract execution via REST API, poll until COMPLETE.

Env vars:
  CIRCLE_API_KEY       — Circle API key (TEST_API_KEY:UUID:SECRET)
  CIRCLE_ENTITY_SECRET — 32-byte hex entity secret
  WALLET_ID            — Circle wallet UUID
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid

import aiohttp

logger = logging.getLogger(__name__)

CIRCLE_API_BASE = "https://api.circle.com/v1/w3s"
CIRCLE_BLOCKCHAIN = "ARC-TESTNET"

# Transaction terminal states. Circle's full enum is INITIATED, CLEARED, QUEUED, SENT,
# STUCK, CONFIRMED, COMPLETE, FAILED, DENIED, CANCELLED (get-transaction API reference).
# CONFIRMED is deliberately NOT terminal: Circle's blockchain-confirmations doc is explicit
# that a transaction visible on a block explorer may still not be COMPLETE.
_TERMINAL = {"COMPLETE", "FAILED", "DENIED", "CANCELLED"}

# STUCK is its own case, and it is not in _TERMINAL because Circle does not call it a
# failure: "STUCK is a signal for intervention, not a terminal failure" — and, crucially,
# "if a transaction stays STUCK and you take no action, it can block subsequent
# transactions from the same wallet" (transaction-limits-and-optimizations). For a caller
# it is nonetheless the end of the road: nothing this process can do will unstick it, so
# spinning out the full poll budget and then raising a generic timeout costs two minutes
# and throws away the one fact the operator needs — WHICH transaction to accelerate or
# cancel in the Circle Console, before it wedges the oracle's pushes from the same wallet.
_STUCK = "STUCK"

_POLL_INTERVAL = 2.0  # seconds
_MAX_POLLS = 60  # 2 minutes max

# Circle answers a contract-execution submit with 201 for a newly created transaction and
# 200 when it recognises the idempotencyKey and replays the existing one
# (create-developer-transaction-contract-execution). Our key is deterministic BY DESIGN —
# uuid5 over wallet+contract+function+args (see execute_contract) — so a replay is the
# expected shape of any retry, not a rarity. Treating 200 as a failure told the operator a
# transaction that had succeeded had failed, and the tempting next move after that is to
# force a second one.
#
# KNOWN DIVERGENCE, deliberate: oracle_updater.py does NOT come through this method — it
# POSTs to the same endpoint itself — and it fixed this identical defect first, under #1525
# ("Circle API error for sBTC (200): {'state': 'COMPLETE'}" every cycle for a push that had
# landed). Its fix grades on the PAYLOAD (a usable id whose state is not a failure state);
# this one grades on the STATUS and then requires a usable id below. Same outcome for the
# shapes Circle actually returns, two implementations, one seam apart. Converging them is
# its own change; until then, a fix here is not a fix there.
_SUBMIT_ACCEPTED = frozenset({200, 201})

# Bounded wall-clock ceiling on the wallet-address lookup (#1412). This runs
# inside the agent tick's reveal-reconciliation pass, so an unbounded read
# against a hung Circle endpoint would stall the tick. aiohttp's own default is
# 5 minutes; a lookup that has not answered in 10s is not going to answer
# usefully, and "no answer" has a correct, safe meaning here (None = could not
# confirm).
_WALLET_LOOKUP_TIMEOUT = 10.0  # seconds


def _encrypt_entity_secret(entity_secret_hex: str, public_key_pem: str) -> str:
    """Encrypt entity secret with Circle's RSA public key (OAEP/SHA-256)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    public_key = serialization.load_pem_public_key(public_key_pem.encode())
    plaintext = bytes.fromhex(entity_secret_hex)
    ciphertext = public_key.encrypt(
        plaintext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode()


class CircleSigner:
    """Signs and submits on-chain transactions via Circle Developer-Controlled Wallets.

    Usage:
        signer = CircleSigner()
        tx_hash = await signer.execute_contract(
            contract_address="0x...",
            abi_function="setTargetAllocations(address[],uint256[])",
            abi_params=[tokens, weights],
        )
    """

    def __init__(self) -> None:
        self._api_key: str = os.getenv("CIRCLE_API_KEY", "")
        self._entity_secret: str = os.getenv("CIRCLE_ENTITY_SECRET", "")
        self._wallet_id: str = os.getenv("WALLET_ID", "")
        self._circle_public_key: str | None = None
        self._wallet_address: str | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._entity_secret and self._wallet_id)

    async def get_wallet_address(self) -> str | None:
        """The EVM address WALLET_ID actually signs with, asked of Circle itself.

        ``GET /wallets/{WALLET_ID}`` returns Circle's own record of the wallet
        this signer submits every transaction through, so the answer is derived
        from the same identifier that does the signing — not from
        ``WALLET_ADDRESS``, a separate env var that an operator maintains BY
        HAND and that nothing re-derives from the wallet (see
        ``.env.example`` and ``ChainExecutor.backend_signer_address``). That
        distinction is the whole point of this method: a caller that acts
        irreversibly on a signer mismatch (#1353's reveal-reconciliation
        pre-check) can trust this and must not trust the mirror.

        Returns:
            The wallet's EVM address, or ``None`` when it could not be
            established — unconfigured credentials, a non-200 from Circle, a
            payload without an address, a timeout, or any transport error.

        ``None`` means "could not confirm", never "confirmed absent" and never
        "confirmed unchanged". Callers must treat it as *no information*: the
        pre-check skips itself and falls through to the ordinary (reversible)
        retry path rather than terminaling on a guess. Consequently this method
        must never raise and must never substitute a plausible-looking value
        for a failed lookup — a fail-soft guess here would permanently kill
        recoverable dangling reveals (CLAUDE.md § fail-soft).

        The successful answer is cached for the process lifetime, same as
        ``_get_public_key``: a Circle wallet's address is immutable for a given
        wallet ID, and re-reading it on every reconciliation pass would add a
        network round-trip to each agent tick. Failures are deliberately NOT
        cached, so a transient outage does not pin "unconfirmable" forever.
        """
        if self._wallet_address:
            return self._wallet_address
        if not self.is_configured:
            return None

        try:
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_WALLET_LOOKUP_TIMEOUT)) as session,
                session.get(
                    f"{CIRCLE_API_BASE}/wallets/{self._wallet_id}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as resp,
            ):
                if resp.status != 200:
                    logger.error(
                        "Circle wallet address lookup failed for wallet %s: HTTP %d",
                        self._wallet_id,
                        resp.status,
                    )
                    return None
                body = await resp.json()
        except (TimeoutError, aiohttp.ClientError, ValueError) as e:
            # TimeoutError covers asyncio.TimeoutError (its alias since 3.11) and
            # so aiohttp's ServerTimeoutError; ValueError covers a non-JSON body.
            logger.error(
                "Circle wallet address lookup errored for wallet %s: %r — reporting 'could not confirm'",
                self._wallet_id,
                e,
            )
            return None

        data = body.get("data") or {} if isinstance(body, dict) else {}
        wallet = data.get("wallet") or {} if isinstance(data, dict) else {}
        address = wallet.get("address") if isinstance(wallet, dict) else None
        if not address:
            logger.error(
                "Circle wallet address lookup for wallet %s returned no address field",
                self._wallet_id,
            )
            return None

        self._wallet_address = str(address)
        return self._wallet_address

    async def _get_public_key(self, session: aiohttp.ClientSession) -> str | None:
        """Fetch Circle's RSA public key (cached)."""
        if self._circle_public_key:
            return self._circle_public_key

        async with session.get(
            f"{CIRCLE_API_BASE}/config/entity/publicKey",
            headers={"Authorization": f"Bearer {self._api_key}"},
        ) as resp:
            if resp.status == 200:
                body = await resp.json()
                self._circle_public_key = body["data"]["publicKey"]
                return self._circle_public_key
            logger.error("Failed to fetch Circle public key: %d", resp.status)
        return None

    async def execute_contract(
        self,
        contract_address: str,
        abi_function: str,
        abi_params: list,
        fee_level: str = "MEDIUM",
    ) -> str:
        """Execute a write function on a deployed contract via Circle.

        Args:
            contract_address: The deployed contract address.
            abi_function: ABI function signature, e.g. "setTargetAllocations(address[],uint256[])"
            abi_params: List of ABI-encoded parameters.
            fee_level: Gas fee level — "LOW", "MEDIUM", or "HIGH".

        Returns:
            The on-chain transaction hash.

        Raises:
            RuntimeError: If Circle credentials are missing, the submit is rejected
                (anything outside :data:`_SUBMIT_ACCEPTED`), the transaction reaches a
                failing terminal state, goes ``STUCK``, or the poll budget runs out.
        """
        if not self.is_configured:
            raise RuntimeError("Circle credentials not configured (CIRCLE_API_KEY / CIRCLE_ENTITY_SECRET / WALLET_ID)")

        async with aiohttp.ClientSession() as session:
            public_key = await self._get_public_key(session)
            if not public_key:
                raise RuntimeError("Failed to fetch Circle public key")

            ciphertext = _encrypt_entity_secret(self._entity_secret, public_key)

            # Deterministic idempotency key (#F5): derived from the stable
            # identifying content of this exact call (wallet + contract +
            # function + args), not a fresh random UUID per call. A retry of
            # THIS SAME logical call now produces the same key, so Circle's
            # dedup can actually recognize it as a retry, while a genuinely
            # different call (different contract/function/args) still
            # produces a different key. uuid5 is used so the result is a
            # validly-formatted UUID per Circle's IdempotencyKey schema.
            idempotency_source = json.dumps(
                {
                    "walletId": self._wallet_id,
                    "contractAddress": contract_address,
                    "abiFunctionSignature": abi_function,
                    "abiParameters": abi_params,
                },
                sort_keys=True,
                default=str,
            )
            idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_source))

            payload = {
                "idempotencyKey": idempotency_key,
                "walletId": self._wallet_id,
                "contractAddress": contract_address,
                "abiFunctionSignature": abi_function,
                "abiParameters": abi_params,  # Circle handles ABI encoding
                "feeLevel": fee_level,
                "blockchain": CIRCLE_BLOCKCHAIN,
                "entitySecretCiphertext": ciphertext,
            }

            # Submit transaction
            async with session.post(
                f"{CIRCLE_API_BASE}/developer/transactions/contractExecution",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                body = await resp.json()
                if resp.status not in _SUBMIT_ACCEPTED:
                    raise RuntimeError(f"Circle contract execution failed ({resp.status}): {body}")
                # A 2xx is not automatically a usable answer. ``body["data"]["id"]`` was an
                # unguarded double subscript: a malformed success — an error envelope, a
                # proxy's HTML, a 200 with no ``data`` — surfaced as ``KeyError: 'data'``
                # from inside the signer instead of the RuntimeError every caller here is
                # written to handle. Newly reachable now that 200 is accepted at all.
                data = body.get("data") if isinstance(body, dict) else None
                circle_tx_id = data.get("id") if isinstance(data, dict) else None
                if not circle_tx_id:
                    raise RuntimeError(
                        f"Circle accepted the contract execution ({resp.status}) but the response carries "
                        f"no transaction id to poll, so nothing can be confirmed: {body}"
                    )
                if resp.status == 200:
                    # Not a new transaction — Circle recognised the idempotency key and
                    # handed back the one it already has. Logged as such so a reader of
                    # the logs can tell one submission from two.
                    logger.info("Circle tx replayed (idempotent, HTTP 200): %s", circle_tx_id)
                else:
                    logger.info("Circle tx submitted: %s", circle_tx_id)

            # Poll until terminal state
            return await self._poll_transaction(session, circle_tx_id)

    async def sign_and_broadcast(
        self,
        tx_object: dict,
    ) -> str:
        """Sign a raw EVM transaction and broadcast it.

        Lower-level alternative to execute_contract() when you need full
        control over the transaction object (e.g. custom gas params).

        Args:
            tx_object: EVM transaction dict matching Ethereum JSON-RPC shape.
                       Must include chainId, nonce, to, value, gas,
                       maxFeePerGas, maxPriorityFeePerGas, and optionally data.

        Returns:
            The on-chain transaction hash.
        """
        if not self.is_configured:
            raise RuntimeError("Circle credentials not configured")

        async with aiohttp.ClientSession() as session:
            payload = {
                "walletId": self._wallet_id,
                # Circle's SignTransaction endpoint documents `transaction` as
                # a JSON-encoded string of the tx object (see
                # developer-controlled-wallets.yaml TransactionObject schema).
                # Python's str() produces a single-quoted repr
                # ("{'nonce': '0x5', ...}") which is not valid JSON and is
                # rejected by Circle's parser — json.dumps() is the correct
                # encoding. A caller that already passes a pre-serialized
                # string (e.g. a raw RLP hex string) is passed through as-is.
                "transaction": tx_object if isinstance(tx_object, str) else json.dumps(tx_object),
            }

            # Sign
            async with session.post(
                f"{CIRCLE_API_BASE}/wallets/{self._wallet_id}/transactions/sign",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            ) as resp:
                body = await resp.json()
                if resp.status not in (200, 201):
                    raise RuntimeError(f"Circle sign failed ({resp.status}): {body}")

                signed_tx = body["data"]["signedTransaction"]
                tx_hash = body["data"].get("txHash", "")

            # Broadcast via Arc RPC
            if signed_tx:
                from archimedes.chain.client import chain_client

                tx_hash = await chain_client.w3.eth.send_raw_transaction(bytes.fromhex(signed_tx.removeprefix("0x")))
                return tx_hash.hex()

            return tx_hash

    async def _poll_transaction(self, session: aiohttp.ClientSession, circle_tx_id: str) -> str:
        """Poll Circle transaction until terminal state.

        Returns the on-chain hash only for ``COMPLETE``. Every other end — a failing
        terminal state, ``STUCK``, or exhausting the budget — raises, and the ``STUCK``
        message names the transaction because that is what an operator has to type into
        the Circle Console to clear it.
        """
        for _ in range(_MAX_POLLS):
            # Scope the list to this wallet's transactions via the walletIds
            # filter. An unfiltered GET /transactions returns the newest txs
            # across every wallet, so with >50 txs in flight the one we care
            # about can fall off the page before it reaches a terminal state,
            # producing a false timeout on a tx that actually succeeded (#941).
            async with session.get(
                f"{CIRCLE_API_BASE}/transactions?pageSize=50&walletIds={self._wallet_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    txs = body.get("data", {}).get("transactions", [])
                    for tx in txs:
                        if tx.get("id") == circle_tx_id:
                            state = tx.get("state", "UNKNOWN")
                            tx_hash = tx.get("txHash", "")

                            if state == _STUCK:
                                raise RuntimeError(
                                    f"Circle tx {circle_tx_id} is STUCK "
                                    f"(txHash {tx_hash or 'not yet assigned'}, wallet {self._wallet_id}): "
                                    "Circle accepted and broadcast it but it is not being mined, and a "
                                    "stuck transaction blocks every later transaction from the same "
                                    "wallet — including the oracle's price pushes. Accelerate or cancel "
                                    f"transaction {circle_tx_id} in the Circle Console (Transactions), "
                                    "then re-run. Do NOT submit a replacement first."
                                )

                            if state in _TERMINAL:
                                if state == "COMPLETE":
                                    logger.info(
                                        "Circle tx %s complete: %s",
                                        circle_tx_id,
                                        tx_hash,
                                    )
                                    return tx_hash
                                raise RuntimeError(f"Circle tx {circle_tx_id} ended in {state}: {tx}")
                            # Still processing
                            break
                await asyncio.sleep(_POLL_INTERVAL)

        raise RuntimeError(f"Circle tx {circle_tx_id} timed out after {_MAX_POLLS} polls")


# Singleton
circle_signer = CircleSigner()
