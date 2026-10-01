"""``CircleSigner.execute_contract`` — the two answers from Circle we used to mishandle.

WHAT IS UNDER TEST
    ``CircleSigner.execute_contract`` in ``archimedes.chain.circle_signer``. Its callers,
    enumerated rather than gestured at, because the blast radius of a change here is the
    list: ``TracePublisher.publish`` / ``.commit`` / ``.reveal``, ``StrategyPublisher.anchor``,
    ``ChainExecutor.execute_trades`` / ``.create_vault`` / ``._send_vault_admin_tx`` /
    ``.set_token_oracles`` / ``.set_target_allocations``, ``scripts/bootstrap_vaults.py``
    (14 sites), ``scripts/deploy_contracts.py``, ``services/amm_bootstrap.py``,
    ``api/marketplace_routes.py``, and ``erc8004_identity.register_identity`` (#1527).

    NOT the oracle. ``oracle_updater`` POSTs to Circle's contract-execution endpoint itself
    (``oracle_updater.py``, in ``push_prices_on_chain``) and polls with its own ``_poll_circle_tx``,
    so nothing in this file constrains it. It also got there first: the identical
    200-vs-201 defect was fixed for the oracle under **#1525**, on the payload rather than
    the status. See the ``_SUBMIT_ACCEPTED`` comment in the module under test — the two
    implementations are a known divergence, not an accident, and ``_poll_circle_tx`` still
    has no ``STUCK`` case.

THE TWO DEFECTS, BOTH GROUNDED IN CIRCLE'S OWN DOCS
    1. **HTTP 200 on submit was raised as a failure.** Circle's contract-execution
       reference documents 200 (an idempotent replay of a key it has already seen) as a
       success alongside 201 (a new transaction). Our idempotency key is deterministic by
       construction — ``uuid5`` over wallet+contract+function+args — so a replay is what a
       retry of the SAME logical call looks like, not an exotic case. Reporting it as
       ``Circle contract execution failed (200)`` tells an operator a landed transaction
       failed, and the next move after that reading is to force a second one. For the
       ERC-8004 mint, a second one is an identity that cannot be un-minted.
    2. **``STUCK`` was not handled.** It is not in ``_TERMINAL``, so the poller spun its
       full 120-second budget and raised a generic timeout — discarding the one operational
       fact that matters. Circle: "if a transaction stays STUCK and you take no action, it
       can block subsequent transactions from the same wallet". The wallet in question also
       runs the oracle.

HERMETIC
    ``aiohttp`` is replaced inside the module under test with a fake whose routes are
    Circle's three real endpoints. Everything above the socket runs for real, including
    the RSA-OAEP entity-secret encryption — against a throwaway key generated in-process.
    No network, and **no real credential**: the "entity secret" here is 32 bytes of 0x11.
"""

from __future__ import annotations

import base64
import types
import uuid

import pytest
from archimedes.chain import circle_signer as cs

WALLET_ID = "11111111-2222-3333-4444-555555555555"
CIRCLE_TX_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
TX_HASH = "0xfeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface"
REGISTRY = "0x8004A818BFB912233c491871b3d84c89A494BD9e"
AGENT_URI = "https://archimedes-arc.com/.well-known/agent-registration.json"
FAKE_ENTITY_SECRET = "11" * 32  # 32 bytes of filler — never a real secret, in any environment


# ── the Circle double ────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def public_pem() -> str:
    """A throwaway RSA public key, so ``_encrypt_entity_secret`` runs for real.

    Generated lazily and once: stubbing the encryption instead would leave the one piece
    of cryptography in this file untested, and it is the piece Circle rejects if it is
    wrong (single-use OAEP ciphertext, MGF1-SHA256).
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


class _Resp:
    """aiohttp's response: ``.status``, awaitable ``.json()``, async context manager."""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCircleAPI:
    """Circle's three endpoints, with the answers under test made settable.

    The whole surface ``execute_contract`` touches is modelled — public key, submit, and
    the poll list — not only the endpoint one test cares about. A double that answers one
    route and 404s its sibling would let a change of endpoint pass unnoticed.
    """

    def __init__(
        self,
        public_pem: str,
        *,
        submit_status: int = 201,
        states: tuple[str, ...] = ("COMPLETE",),
        submit_body: dict | None = None,
    ):
        self._pem = public_pem
        self._submit_status = submit_status
        self._states = states
        # ``submit_body`` overrides the well-formed answer, for the malformed-2xx cases.
        # ``None`` means "use the real shape"; ``{}`` is itself a case under test.
        self._submit_body = submit_body
        self.submits: list[dict] = []
        self.polls = 0
        self.unexpected: list[str] = []

    def get(self, url: str, *_a, **_kw):
        if url.endswith("/config/entity/publicKey"):
            return _Resp(200, {"data": {"publicKey": self._pem}})
        if "/transactions?" in url:
            assert f"walletIds={WALLET_ID}" in url, f"poll dropped the walletIds filter (#941): {url}"
            state = self._states[min(self.polls, len(self._states) - 1)]
            self.polls += 1
            return _Resp(
                200,
                {"data": {"transactions": [{"id": CIRCLE_TX_ID, "state": state, "txHash": TX_HASH}]}},
            )
        self.unexpected.append(f"GET {url}")
        return _Resp(404, {})

    def post(self, url: str, *_a, json: dict | None = None, **_kw):
        if url.endswith("/developer/transactions/contractExecution"):
            self.submits.append(json or {})
            if self._submit_body is not None:
                return _Resp(self._submit_status, self._submit_body)
            body = (
                {"data": {"id": CIRCLE_TX_ID, "state": "INITIATED"}}
                if self._submit_status in (200, 201)
                else {"code": 156000, "message": "no."}
            )
            return _Resp(self._submit_status, body)
        self.unexpected.append(f"POST {url}")
        return _Resp(404, {})


class _FakeSession:
    def __init__(self, api: FakeCircleAPI):
        self._api = api

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, *a, **kw):
        return self._api.get(*a, **kw)

    def post(self, *a, **kw):
        return self._api.post(*a, **kw)


@pytest.fixture
def signer(monkeypatch, public_pem):
    """A configured ``CircleSigner`` wired to a fake Circle, with the poll sleep removed."""

    def _make(**api_kwargs) -> tuple[cs.CircleSigner, FakeCircleAPI]:
        api = FakeCircleAPI(public_pem, **api_kwargs)
        monkeypatch.setattr(
            cs,
            "aiohttp",
            types.SimpleNamespace(
                ClientSession=lambda *a, **kw: _FakeSession(api),
                ClientTimeout=lambda **kw: None,
                ClientError=Exception,
            ),
        )
        monkeypatch.setattr(cs, "_POLL_INTERVAL", 0.0)
        monkeypatch.setenv("CIRCLE_API_KEY", "TEST_API_KEY:not-a-real-key")
        monkeypatch.setenv("CIRCLE_ENTITY_SECRET", FAKE_ENTITY_SECRET)
        monkeypatch.setenv("WALLET_ID", WALLET_ID)
        return cs.CircleSigner(), api

    return _make


async def _register(signer_obj: cs.CircleSigner) -> str:
    return await signer_obj.execute_contract(
        contract_address=REGISTRY,
        abi_function="register(string)",
        abi_params=[AGENT_URI],
    )


# ── the submit status ────────────────────────────────────────────────────────


async def test_a_new_transaction_201_is_accepted(signer):
    """The baseline. Its whole job is to be the sibling of the 200 test below."""
    s, api = signer(submit_status=201)

    assert await _register(s) == TX_HASH
    assert len(api.submits) == 1
    assert api.unexpected == []


async def test_an_idempotent_replay_200_is_accepted_not_raised(signer):
    """THE FIX, shown working: Circle's documented 200 is a success, not a failure.

    Byte-identical to the 201 test except for the status Circle answers with. Before this
    change the call raised ``Circle contract execution failed (200): ...`` and the
    transaction it was reporting on was live on-chain.
    """
    s, api = signer(submit_status=200)

    assert await _register(s) == TX_HASH, "a documented idempotent replay was treated as a failure"
    assert len(api.submits) == 1, "a replay must not be resubmitted"


@pytest.mark.parametrize("status", [400, 401, 403, 409, 429, 500, 202, 204])
async def test_every_other_status_is_still_a_failure(signer, status):
    """THE GUARD, shown rejecting: widening to {200, 201} must not widen to 'any 2xx'.

    202 and 204 are in here deliberately. They are the statuses a lazy fix — ``status <
    300``, or ``status in range(200, 300)`` — would swallow, and neither carries a
    transaction Circle has accepted.
    """
    s, api = signer(submit_status=status)

    with pytest.raises(RuntimeError, match=f"Circle contract execution failed \\({status}\\)"):
        await _register(s)
    assert api.polls == 0, "polled for a transaction the submit never created"


@pytest.mark.parametrize("status", [200, 201])
@pytest.mark.parametrize(
    "body",
    [{}, {"data": None}, {"data": {}}, {"data": {"state": "INITIATED"}}, {"code": 156000, "message": "no."}],
    ids=["empty", "null-data", "data-without-id", "state-but-no-id", "error-envelope-under-2xx"],
)
async def test_an_accepted_status_with_no_transaction_id_is_a_clean_error(signer, status, body):
    """An accepted STATUS is not an accepted ANSWER.

    ``circle_tx_id = body["data"]["id"]`` was an unguarded double subscript. A 2xx without
    a ``data`` envelope — an error body a proxy stamped 200 on, a shape change — came out
    as ``KeyError: 'data'`` from inside the signer, which no caller on this seam catches:
    ``register_identity`` turns a ``RuntimeError`` into ``action: refused`` and would turn
    a ``KeyError`` into a traceback. Widening to 200 made the case newly reachable, so the
    guard ships with the widening.
    """
    s, api = signer(submit_status=status, submit_body=body)

    with pytest.raises(RuntimeError, match="no transaction id to poll"):
        await _register(s)

    assert api.polls == 0, "polled for a transaction id that was never returned"


# ── STUCK ────────────────────────────────────────────────────────────────────


async def test_stuck_is_an_actionable_error_naming_the_transaction(signer):
    """THE GUARD, shown rejecting: STUCK ends the poll and says which tx to go and clear.

    The old behaviour raised ``Circle tx <id> timed out after 60 polls`` — two minutes
    later, and describing the poller rather than the transaction. What an operator needs
    is the id to type into the Circle Console and the fact that leaving it there wedges
    every later transaction from the same wallet, the oracle's price pushes included.
    """
    s, api = signer(states=("STUCK",))

    with pytest.raises(RuntimeError) as excinfo:
        await _register(s)

    message = str(excinfo.value)
    assert CIRCLE_TX_ID in message, f"the STUCK error does not name the transaction: {message}"
    assert "STUCK" in message
    assert "Circle Console" in message
    assert "blocks every later transaction from the same wallet" in message
    assert "timed out" not in message
    assert api.polls == 1, f"STUCK is the end of the road; it was polled {api.polls} times"


async def test_a_transaction_that_settles_after_processing_still_completes(signer):
    """STUCK ending the loop must not make every non-terminal state end it.

    QUEUED and SENT are ordinary in-flight states — the poller has to keep going through
    them, or the fix above turns into a false failure on every slightly slow transaction.
    """
    s, api = signer(states=("QUEUED", "SENT", "CONFIRMED", "COMPLETE"))

    assert await _register(s) == TX_HASH
    assert api.polls == 4, "CONFIRMED is not COMPLETE — Circle documents the difference"


async def test_a_failed_terminal_state_still_raises(signer):
    s, _api = signer(states=("FAILED",))

    with pytest.raises(RuntimeError, match="ended in FAILED"):
        await _register(s)


async def test_the_poll_budget_still_ends_in_a_timeout(signer):
    """The generic timeout survives for what it was always for: a transaction that hangs."""
    s, api = signer(states=("SENT",))

    with pytest.raises(RuntimeError, match=f"Circle tx {CIRCLE_TX_ID} timed out"):
        await _register(s)
    assert api.polls == cs._MAX_POLLS


# ── the idempotency key itself ───────────────────────────────────────────────


async def test_the_idempotency_key_is_deterministic_and_content_addressed(signer):
    """Pinned, because accepting 200 is only correct while the key is deterministic.

    If this key ever became a fresh ``uuid4`` per call, a retry would mint a SECOND
    identity instead of replaying the first — and the 200 branch above, which exists to
    handle that replay, would quietly never fire again.
    """
    s, api = signer()
    await _register(s)
    await _register(s)

    keys = [submit["idempotencyKey"] for submit in api.submits]
    assert keys[0] == keys[1], "the same logical call produced two different idempotency keys"
    assert uuid.UUID(keys[0]).version == 5

    await s.execute_contract(contract_address=REGISTRY, abi_function="register(string)", abi_params=["other"])
    assert api.submits[-1]["idempotencyKey"] != keys[0], "different arguments must not share a key"


async def test_the_submit_payload_is_the_shape_circle_documents(signer):
    """The submitted body, checked whole — including that the entity secret is encrypted."""
    s, api = signer()
    await _register(s)

    payload = api.submits[0]
    assert payload["walletId"] == WALLET_ID
    assert payload["contractAddress"] == REGISTRY
    assert payload["abiFunctionSignature"] == "register(string)"
    assert payload["abiParameters"] == [AGENT_URI]
    assert payload["feeLevel"] == "MEDIUM"
    assert payload["blockchain"] == "ARC-TESTNET"
    ciphertext = payload["entitySecretCiphertext"]
    assert FAKE_ENTITY_SECRET not in ciphertext, "the entity secret was sent in the clear"
    assert len(base64.b64decode(ciphertext)) == 256, "not a 2048-bit RSA-OAEP ciphertext"


async def test_an_unconfigured_signer_sends_nothing(signer, monkeypatch):
    _s, api = signer()
    monkeypatch.delenv("CIRCLE_API_KEY", raising=False)
    unconfigured = cs.CircleSigner()

    with pytest.raises(RuntimeError, match="Circle credentials not configured"):
        await _register(unconfigured)
    assert api.submits == []
