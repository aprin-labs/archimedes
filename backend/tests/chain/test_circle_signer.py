"""Tests for CircleSigner — Circle Developer-Controlled Wallet signing (#738).

Target: backend/archimedes/chain/circle_signer.py
The signer encrypts the entity secret with Circle's RSA public key, submits a
contract execution, and polls until a terminal state. It holds funds-adjacent
authority (it is the wallet that signs vault rebalance / ownership txs), so the
configured/unconfigured gate, the submit path, and the poll loop must all be
exercised.

Hermetic: the aiohttp HTTP boundary is mocked; the RSA encrypt helper is mocked
where a real key would otherwise be needed. No network, no Circle, no Arc RPC.

See also backend/tests/test_circle_signer.py (#1527): the submit-status contract is
{200, 201}, not 201 alone — 200 is Circle replaying our deterministic idempotency key —
and ``STUCK`` raises immediately instead of burning the poll budget. Both are proven
there, against a fake with real RSA rather than a mocked encrypt.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from archimedes.chain.circle_signer import CircleSigner, _encrypt_entity_secret

# ── Helpers ───────────────────────────────────────────────────


def _mock_session() -> MagicMock:
    """An aiohttp.ClientSession whose `get`/`post` return async context managers.

    Each call to `_set_get`/`_set_post` installs the response object the next
    `async with session.get(...)` / `session.post(...)` should yield.
    """
    session = MagicMock()

    def _cm(resp):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    session._cm = _cm
    return session


def _session_context(session: MagicMock) -> MagicMock:
    """Wrap a session in the `async with aiohttp.ClientSession() as s` CM."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _resp(status: int, body: dict) -> MagicMock:
    resp = MagicMock(status=status)
    resp.json = AsyncMock(return_value=body)
    return resp


def _raising_cm(exc: BaseException) -> MagicMock:
    """An async context manager whose __aenter__ raises — how aiohttp surfaces
    a timeout / transport failure at the `async with session.get(...)` line."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=exc)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.fixture
def configured(monkeypatch) -> CircleSigner:
    monkeypatch.setenv("CIRCLE_API_KEY", "test-api-key")
    monkeypatch.setenv("CIRCLE_ENTITY_SECRET", "ab" * 32)
    monkeypatch.setenv("WALLET_ID", "wallet-uuid")
    return CircleSigner()


# ── is_configured ─────────────────────────────────────────────


class TestIsConfigured:
    def test_true_when_all_creds_present(self, configured):
        assert configured.is_configured is True

    def test_false_when_any_cred_missing(self, monkeypatch):
        monkeypatch.delenv("CIRCLE_API_KEY", raising=False)
        monkeypatch.delenv("CIRCLE_ENTITY_SECRET", raising=False)
        monkeypatch.delenv("WALLET_ID", raising=False)
        assert CircleSigner().is_configured is False


# ── _get_public_key ───────────────────────────────────────────


class TestGetPublicKey:
    async def test_fetches_and_caches(self, configured):
        session = _mock_session()
        resp = _resp(200, {"data": {"publicKey": "PEM-DATA"}})
        session.get = MagicMock(return_value=session._cm(resp))

        key = await configured._get_public_key(session)
        assert key == "PEM-DATA"
        # Second call uses the cache — no second HTTP get.
        session.get.reset_mock()
        key2 = await configured._get_public_key(session)
        assert key2 == "PEM-DATA"
        session.get.assert_not_called()

    async def test_non_200_returns_none(self, configured):
        session = _mock_session()
        session.get = MagicMock(return_value=session._cm(_resp(403, {"error": "denied"})))
        assert await configured._get_public_key(session) is None


# ── get_wallet_address (#1412) ────────────────────────────────


class TestGetWalletAddress:
    """``get_wallet_address`` asks Circle what WALLET_ID actually signs with.

    This is the value the reveal-reconciliation signer pre-check acts on
    IRREVERSIBLY (``ChainExecutor.backend_signer_address_confirmed`` →
    ``agent_runner._reconcile_one_reveal``), so the contract under test is
    two-sided: a real address when Circle answers, and ``None`` — meaning
    "could not confirm", never "confirmed" — for every way the lookup can fail.
    """

    async def test_returns_the_address_circle_reports(self, configured):
        session = _mock_session()
        body = {
            "data": {
                "wallet": {
                    "id": "wallet-uuid",
                    "address": "0xCIRCLE0000000000000000000000000000000001",
                    "blockchain": "ARC-TESTNET",
                }
            }
        }
        session.get = MagicMock(return_value=session._cm(_resp(200, body)))

        with patch(
            "archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)
        ) as mock_session_cls:
            addr = await configured.get_wallet_address()

        assert addr == "0xCIRCLE0000000000000000000000000000000001"
        # The lookup must be keyed on WALLET_ID — the identifier that signs —
        # not on the WALLET_ADDRESS mirror.
        assert session.get.call_args.args[0].endswith("/wallets/wallet-uuid")
        # Bounded (#1412): this runs inside the agent tick, so a hung Circle
        # endpoint must not stall it. Pinned so a future edit can't silently
        # drop the ceiling back to aiohttp's 5-minute default.
        timeout = mock_session_cls.call_args.kwargs["timeout"]
        assert timeout.total is not None
        assert 0 < timeout.total <= 30

    async def test_caches_the_successful_answer(self, configured):
        session = _mock_session()
        body = {"data": {"wallet": {"address": "0xCIRCLE0000000000000000000000000000000001"}}}
        session.get = MagicMock(return_value=session._cm(_resp(200, body)))

        with patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)):
            first = await configured.get_wallet_address()
            session.get.reset_mock()
            second = await configured.get_wallet_address()

        assert first == second == "0xCIRCLE0000000000000000000000000000000001"
        session.get.assert_not_called()  # cached, same pattern as _get_public_key

    async def test_unconfigured_returns_none_without_calling_circle(self, monkeypatch):
        monkeypatch.delenv("CIRCLE_API_KEY", raising=False)
        monkeypatch.delenv("CIRCLE_ENTITY_SECRET", raising=False)
        monkeypatch.delenv("WALLET_ID", raising=False)
        signer = CircleSigner()
        with patch("archimedes.chain.circle_signer.aiohttp.ClientSession") as mock_session_cls:
            assert await signer.get_wallet_address() is None
        mock_session_cls.assert_not_called()

    async def test_non_200_returns_none_and_is_not_cached(self, configured):
        """A 401/500 is "could not confirm", not "confirmed absent" — and a
        transient outage must not pin that state for the process lifetime."""
        session = _mock_session()
        session.get = MagicMock(return_value=session._cm(_resp(401, {"message": "unauthorized"})))
        with patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)):
            assert await configured.get_wallet_address() is None

        # Circle recovers → the next call re-reads rather than serving a
        # cached failure.
        ok = _mock_session()
        ok.get = MagicMock(
            return_value=ok._cm(
                _resp(200, {"data": {"wallet": {"address": "0xLATER00000000000000000000000000000000001"}}})
            )
        )
        with patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(ok)):
            assert await configured.get_wallet_address() == "0xLATER00000000000000000000000000000000001"

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("no data key", {"message": "ok"}),
            ("null data", {"data": None}),
            ("no wallet key", {"data": {}}),
            ("null wallet", {"data": {"wallet": None}}),
            ("no address field", {"data": {"wallet": {"id": "wallet-uuid"}}}),
            ("empty address", {"data": {"wallet": {"address": ""}}}),
        ],
    )
    async def test_payload_without_a_usable_address_returns_none(self, configured, label, body):
        session = _mock_session()
        session.get = MagicMock(return_value=session._cm(_resp(200, body)))
        with patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)):
            assert await configured.get_wallet_address() is None, label

    @pytest.mark.parametrize(
        ("label", "exc"),
        [
            # TimeoutError IS asyncio.TimeoutError on 3.11+ — this is what the
            # bounded _WALLET_LOOKUP_TIMEOUT raises when Circle hangs.
            ("timeout", TimeoutError()),
            ("connection error", aiohttp.ClientConnectionError("circle unreachable")),
        ],
    )
    async def test_transport_failure_returns_none_and_never_raises(self, configured, label, exc):
        """The caller runs inside the agent tick and acts irreversibly on a
        confirmed mismatch — a raised exception here would abort the whole
        reconciliation pass, and a guessed address would terminal a
        recoverable reveal. Both are wrong; None is the honest answer."""
        session = _mock_session()
        session.get = MagicMock(return_value=_raising_cm(exc))
        with patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)):
            assert await configured.get_wallet_address() is None, label

    async def test_non_json_body_returns_none(self, configured):
        """A 200 whose body doesn't parse (proxy error page, truncated
        response) must not propagate out of the tick."""
        session = _mock_session()
        resp = MagicMock(status=200)
        resp.json = AsyncMock(side_effect=ValueError("Expecting value"))
        session.get = MagicMock(return_value=session._cm(resp))
        with patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)):
            assert await configured.get_wallet_address() is None


# ── execute_contract ──────────────────────────────────────────


class TestExecuteContract:
    async def test_raises_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("CIRCLE_API_KEY", raising=False)
        signer = CircleSigner()
        with pytest.raises(RuntimeError, match="not configured"):
            await signer.execute_contract("0xVault", "setAgent(address)", ["0xabc"])

    async def test_happy_path_submits_then_polls_to_complete(self, configured):
        session = _mock_session()
        # 1) public key fetch, 2) POST contractExecution (201), then polling GET.
        key_resp = _resp(200, {"data": {"publicKey": "PEM"}})
        submit_resp = _resp(201, {"data": {"id": "circle-tx-1"}})
        session.get = MagicMock(return_value=session._cm(key_resp))
        session.post = MagicMock(return_value=session._cm(submit_resp))

        with (
            patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)),
            patch("archimedes.chain.circle_signer._encrypt_entity_secret", return_value="ciphertext"),
            patch.object(configured, "_poll_transaction", AsyncMock(return_value="0xONCHAIN")),
        ):
            result = await configured.execute_contract(
                "0xVault", "setTargetAllocations(address[],uint256[])", [["0xT"], [10000]]
            )
        assert result == "0xONCHAIN"
        session.post.assert_called_once()

    async def test_public_key_failure_raises(self, configured):
        session = _mock_session()
        session.get = MagicMock(return_value=session._cm(_resp(500, {})))
        with (
            patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)),
            patch("archimedes.chain.circle_signer._encrypt_entity_secret", return_value="ciphertext"),
            pytest.raises(RuntimeError, match="public key"),
        ):
            await configured.execute_contract("0xVault", "setAgent(address)", ["0xabc"])

    async def test_a_rejected_submit_raises(self, configured):
        """A NON-2xx submit raises. 200 does not — it is Circle replaying our idempotency key.

        Renamed from ``test_non_201_submit_raises``: the old name asserted a rule the code
        no longer follows. {200, 201} are both accepted (``_SUBMIT_ACCEPTED``); the 200 path
        is proven in ``backend/tests/test_circle_signer.py``, alongside ``STUCK``.
        """
        session = _mock_session()
        session.get = MagicMock(return_value=session._cm(_resp(200, {"data": {"publicKey": "PEM"}})))
        session.post = MagicMock(return_value=session._cm(_resp(400, {"error": "bad"})))
        with (
            patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)),
            patch("archimedes.chain.circle_signer._encrypt_entity_secret", return_value="ciphertext"),
            pytest.raises(RuntimeError, match="contract execution failed"),
        ):
            await configured.execute_contract("0xVault", "setAgent(address)", ["0xabc"])


# ── _poll_transaction ─────────────────────────────────────────


class TestPollTransaction:
    async def test_returns_tx_hash_on_complete(self, configured):
        session = _mock_session()
        body = {"data": {"transactions": [{"id": "tx-1", "state": "COMPLETE", "txHash": "0xHASH"}]}}
        session.get = MagicMock(return_value=session._cm(_resp(200, body)))
        result = await configured._poll_transaction(session, "tx-1")
        assert result == "0xHASH"

    async def test_raises_on_failed_terminal_state(self, configured):
        session = _mock_session()
        body = {"data": {"transactions": [{"id": "tx-1", "state": "FAILED", "txHash": ""}]}}
        session.get = MagicMock(return_value=session._cm(_resp(200, body)))
        with pytest.raises(RuntimeError, match="ended in FAILED"):
            await configured._poll_transaction(session, "tx-1")

    async def test_times_out_after_max_polls(self, configured):
        session = _mock_session()
        # Tx never reaches terminal state → loop exhausts and raises.
        body = {"data": {"transactions": [{"id": "tx-1", "state": "PROCESSING", "txHash": ""}]}}
        session.get = MagicMock(return_value=session._cm(_resp(200, body)))
        with (
            patch("archimedes.chain.circle_signer._MAX_POLLS", 2),
            patch("archimedes.chain.circle_signer.asyncio.sleep", AsyncMock()),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            await configured._poll_transaction(session, "tx-1")

    async def test_poll_query_scoped_to_wallet(self, configured):
        """The poll GET must filter by walletIds so the target tx can't fall off
        an unfiltered global page (#941).

        Without the walletIds filter, Circle's GET /transactions returns the
        newest txs across every wallet capped at pageSize; with >50 in flight
        the real tx drops off the window and polling false-times-out on a tx
        that actually completed. This asserts the filter is on the request URL.
        """
        session = _mock_session()
        body = {"data": {"transactions": [{"id": "tx-1", "state": "COMPLETE", "txHash": "0xHASH"}]}}
        session.get = MagicMock(return_value=session._cm(_resp(200, body)))

        result = await configured._poll_transaction(session, "tx-1")

        assert result == "0xHASH"
        called_url = session.get.call_args.args[0]
        assert "walletIds=wallet-uuid" in called_url
        # Audit G13: pageSize=50 *within one wallet* is the accepted, documented
        # bound — a single agent wallet with >50 in-flight txs would still fall
        # off the page. Pinned here so a silent pageSize change is a conscious
        # decision, not an accident.
        assert "pageSize=50" in called_url


# ── sign_and_broadcast ────────────────────────────────────────


class TestSignAndBroadcast:
    async def test_raises_when_unconfigured(self, monkeypatch):
        monkeypatch.delenv("CIRCLE_API_KEY", raising=False)
        signer = CircleSigner()
        with pytest.raises(RuntimeError, match="not configured"):
            await signer.sign_and_broadcast({"to": "0x", "value": 0})

    async def test_signs_and_broadcasts_via_arc_rpc(self, configured):
        session = _mock_session()
        sign_resp = _resp(201, {"data": {"signedTransaction": "0xabcd", "txHash": ""}})
        session.post = MagicMock(return_value=session._cm(sign_resp))

        mock_chain = MagicMock()
        sent_hash = MagicMock()
        sent_hash.hex = MagicMock(return_value="0xBROADCAST")
        mock_chain.w3.eth.send_raw_transaction = AsyncMock(return_value=sent_hash)

        with (
            patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)),
            patch("archimedes.chain.client.chain_client", mock_chain),
        ):
            result = await configured.sign_and_broadcast({"to": "0xabc", "value": 0})
        assert result == "0xBROADCAST"
        mock_chain.w3.eth.send_raw_transaction.assert_awaited_once()

    async def test_sign_failure_raises(self, configured):
        session = _mock_session()
        session.post = MagicMock(return_value=session._cm(_resp(422, {"error": "bad tx"})))
        with (
            patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)),
            pytest.raises(RuntimeError, match="sign failed"),
        ):
            await configured.sign_and_broadcast({"to": "0xabc", "value": 0})

    async def test_dict_transaction_is_json_encoded_not_python_repr(self, configured):
        """A dict tx_object must be sent as valid JSON (Circle's documented
        TransactionObject format — see developer-controlled-wallets.yaml),
        not Python's str() repr. A repr like "{'nonce': '0x5', ...}" (single
        quotes) is not valid JSON and is not a format Circle's sign endpoint
        accepts."""
        session = _mock_session()
        sign_resp = _resp(201, {"data": {"signedTransaction": "0xabcd", "txHash": ""}})
        session.post = MagicMock(return_value=session._cm(sign_resp))

        mock_chain = MagicMock()
        sent_hash = MagicMock()
        sent_hash.hex = MagicMock(return_value="0xBROADCAST")
        mock_chain.w3.eth.send_raw_transaction = AsyncMock(return_value=sent_hash)

        tx = {"nonce": "0x5", "to": "0xabc", "value": "0x0", "gas": "0x5208", "chainId": "0x4cef52"}

        with (
            patch("archimedes.chain.circle_signer.aiohttp.ClientSession", return_value=_session_context(session)),
            patch("archimedes.chain.client.chain_client", mock_chain),
        ):
            await configured.sign_and_broadcast(tx)

        sent_payload = session.post.call_args.kwargs["json"]
        sent_transaction = sent_payload["transaction"]

        # Must be valid JSON (json.loads must not raise) round-tripping to
        # the original dict — the old str(tx_object) repr fails this.
        assert json.loads(sent_transaction) == tx
        # Guard against a Python-repr regression directly: repr() uses
        # single-quoted keys/values, which is never valid JSON syntax.
        assert "'" not in sent_transaction


# ── _encrypt_entity_secret ────────────────────────────────────


class TestEncryptEntitySecret:
    def test_round_trips_with_real_rsa_key(self):
        """The encrypt helper must produce a base64 ciphertext that decrypts back
        to the entity secret with the matching private key — proving the OAEP
        padding + key handling is correct (no network needed)."""
        import base64

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )

        secret_hex = "ab" * 32
        ciphertext_b64 = _encrypt_entity_secret(secret_hex, public_pem)
        decrypted = private_key.decrypt(
            base64.b64decode(ciphertext_b64),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        assert decrypted == bytes.fromhex(secret_hex)
