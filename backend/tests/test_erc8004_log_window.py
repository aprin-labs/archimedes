"""The ERC-8004 register path's pre-flight: the log window, and what happens if it fails.

WHAT IS UNDER TEST
    ``scripts/register_erc8004_identity.py`` (imported by path, like this suite's other
    script tests) together with ``archimedes.chain.erc8004_identity.find_agent_id``.

THE DEFECT
    There is no owner→agentId lookup on the ERC-8004 registry, so discovering our own
    identity is an ``eth_getLogs`` scan for the mint. Arc's public RPC refuses a range
    wider than 10,000 blocks — verified live 2026-09-03 against
    ``https://rpc.testnet.arc.network``:

        {"code":-32614,"message":"eth_getLogs is limited to a 10,000 range"}

    The scan defaulted to block 0. Arc testnet is ~60,294,500 blocks tall. So the
    confirming read after a successful mint was guaranteed to fail, ``--execute`` would
    report ``action: submitted``, and — before this change — print nothing further. The
    identity would exist on-chain, unnamed, with the operator holding a transaction hash
    and no instructions. The next move a person reaches for in that state is
    ``--allow-second-identity``, and a second identity cannot be un-minted.

    ``--verify`` had the same bug from the other side: it never forwarded ``--from-block``
    at all, so it scanned from 0 no matter what was passed. Harmless while ``balanceOf ==
    0`` short-circuits the scan; a ``-32614`` traceback the moment there is something to
    find, which is exactly when it is needed.

HERMETIC
    :class:`RangeCappedRPC` serves both transports the code uses — the script's plain
    ``httpx`` JSON-RPC (``eth_blockNumber``, which is how the window is resolved) and the
    backend's web3 ``get_logs`` (the scan itself). The 10,000-block cap is enforced on the
    ``get_logs`` side, which is the only one that scans; the httpx side answers the height
    and rejects everything else. Every "bounded window works" test in here is paired with a
    sibling proving the opposite input is refused by that same fake — the unbounded scan
    the fix replaced, and the too-narrow window the fix introduced. No network, no
    credentials, no key material.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from archimedes.chain import erc8004_identity as erc8004

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "register_erc8004_identity.py"

OURS = "0x9257460000000000000000000000000000000000"
AGENT_URI = "https://archimedes-arc.com/.well-known/agent-registration.json"
TX_HASH = "0xfeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedfacefeedface"
HEAD = 60_294_500  # the real Arc testnet height on 2026-09-03, to scale


def _load_script():
    spec = importlib.util.spec_from_file_location("register_erc8004_identity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mod = _load_script()


# ── the fake RPC: the 10,000-block cap, enforced ─────────────────────────────


class RangeCappedRPC:
    """An Arc-shaped RPC that refuses an ``eth_getLogs`` range wider than 10,000 blocks.

    Serves both transports on purpose. ``handle`` is an ``httpx.MockTransport`` handler for
    the script's own JSON-RPC helper (``eth_blockNumber``); ``get_logs`` is the web3 method
    ``find_agent_id`` reaches through the chain client. A fake that capped only one of them
    would leave whichever half is not exercised free to regress.
    """

    LIMIT = 10_000
    ERROR: ClassVar[dict] = {"code": -32614, "message": "eth_getLogs is limited to a 10,000 range"}

    def __init__(self, head: int = HEAD, mint_block: int = HEAD - 120, token_id: int = 3):
        self.head = head
        self.mint_block = mint_block
        self.token_id = token_id
        self.ranges: list[tuple[int, int]] = []
        self.rpc_calls: list[str] = []

    # -- the script's transport ------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        self.rpc_calls.append(body["method"])
        if body["method"] == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(self.head)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no"}})

    # -- the web3 transport ----------------------------------------------------
    def _resolve(self, block) -> int:
        if isinstance(block, str):
            if block in {"latest", "pending", "safe", "finalized"}:
                return self.head
            if block == "earliest":
                return 0
            return int(block, 16) if block.startswith("0x") else int(block)
        return int(block)

    def get_logs(self, params: dict) -> list[dict]:
        start = self._resolve(params.get("fromBlock", 0))
        end = self._resolve(params.get("toBlock", "latest"))
        self.ranges.append((start, end))
        if end - start > self.LIMIT:
            # web3 surfaces a JSON-RPC error object as a raised ValueError.
            raise ValueError(self.ERROR)
        if not start <= self.mint_block <= end:
            return []
        return [
            {
                "blockNumber": self.mint_block,
                "topics": [
                    erc8004._TRANSFER_TOPIC,
                    erc8004._address_topic(erc8004.ZERO_ADDRESS),
                    erc8004._address_topic(OURS),
                    hex(self.token_id),
                ],
            }
        ]


class _Call:
    def __init__(self, fn):
        self._fn = fn

    async def call(self):
        return self._fn()


class FakeRegistry:
    """``balanceOf``/``ownerOf``/``tokenURI`` — the whole surface the scan path touches."""

    def __init__(self, tokens: dict[int, tuple[str, str]]):
        self.tokens = dict(tokens)
        self.calls: list[tuple] = []
        self.functions = self

    def balanceOf(self, owner):
        self.calls.append(("balanceOf", owner))
        return _Call(lambda: sum(1 for holder, _ in self.tokens.values() if holder.lower() == owner.lower()))

    def ownerOf(self, token_id):
        self.calls.append(("ownerOf", token_id))

        def _run():
            if token_id not in self.tokens:
                raise ValueError("execution reverted: ERC721NonexistentToken")
            return self.tokens[token_id][0]

        return _Call(_run)

    def tokenURI(self, token_id):
        self.calls.append(("tokenURI", token_id))
        return _Call(lambda: self.tokens[token_id][1])


class FakeChainClient:
    """The ``ChainClient`` surface ``find_agent_id`` uses, backed by the capped RPC."""

    def __init__(self, rpc: RangeCappedRPC):
        outer = rpc
        # ContractLoader(client) reads ``client.settings.abi_dir`` on construction; the
        # registry itself is stubbed at the factory, so the directory is never opened.
        self.settings = types.SimpleNamespace(abi_dir="/nonexistent")

        class _Eth:
            async def get_logs(self, params):
                return outer.get_logs(params)

        self.w3 = types.SimpleNamespace(eth=_Eth())

    @staticmethod
    def to_checksum(address: str) -> str:
        return address


class CountingSigner:
    """``CircleSigner``'s surface, with the submissions counted. The count IS the assertion.

    ``execute_contract`` is the only method: if the register path ever reaches for
    ``sign_and_broadcast`` instead, this fake raises ``AttributeError`` rather than letting
    a permissive double absorb the change. A submit optionally mints into the registry and
    drops the mint log inside the window, so the confirming scan behaves like a real one.
    """

    is_configured = True

    def __init__(
        self,
        registry: FakeRegistry | None = None,
        rpc: RangeCappedRPC | None = None,
        token_id: int = 7,
    ):
        self.submits: list[tuple] = []
        self._registry = registry
        self._rpc = rpc
        self._token_id = token_id

    async def execute_contract(self, *, contract_address, abi_function, abi_params, fee_level="MEDIUM"):
        self.submits.append((contract_address, abi_function, tuple(abi_params)))
        if self._registry is not None and self._rpc is not None:
            self._registry.tokens[self._token_id] = (OURS, abi_params[0])
            self._rpc.token_id = self._token_id
            self._rpc.mint_block = self._rpc.head - 1
        return TX_HASH


def _install(monkeypatch, rpc: RangeCappedRPC) -> tuple[RangeCappedRPC, FakeRegistry]:
    """Install the capped RPC everywhere the register path can reach a chain, and hand it back."""
    registry = FakeRegistry({rpc.token_id: (OURS, AGENT_URI)})

    from archimedes.chain.contracts import ContractLoader

    monkeypatch.setattr(ContractLoader, "erc8004_identity_registry", lambda self, address: registry)

    client = FakeChainClient(rpc)
    import archimedes.chain.client as chain_client_module

    monkeypatch.setattr(chain_client_module, "chain_client", client)
    monkeypatch.setattr(
        mod,
        "httpx",
        types.SimpleNamespace(
            Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(rpc.handle), **kw), HTTPError=httpx.HTTPError
        ),
    )
    monkeypatch.setenv("ERC8004_OWNER_ADDRESS", OURS)
    monkeypatch.delenv("ERC8004_AGENT_ID", raising=False)
    return rpc, registry


@pytest.fixture
def wired(monkeypatch):
    """The mint is 120 blocks back — comfortably inside the default window."""
    return _install(monkeypatch, RangeCappedRPC())


@pytest.fixture
def aged(monkeypatch):
    """The same wiring, but the mint is 50,000 blocks old — OUTSIDE the default window.

    This is what every wallet looks like ~77 minutes after its mint (Arc's block time
    measured live at 0.515 s/block on 2026-09-03, so 9,000 blocks is 77 minutes), and it is
    the state in which the bounded window can lie: the scan succeeds, the cap is never hit,
    and it names nothing — while ``balanceOf`` says an identity is right there.
    """
    return _install(monkeypatch, RangeCappedRPC(mint_block=HEAD - 50_000))


def _run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["register_erc8004_identity.py", *argv])
    return mod.main()


# ── the window, resolved ─────────────────────────────────────────────────────


def test_the_default_window_is_the_head_minus_nine_thousand(wired):
    rpc, _registry = wired

    start, note = mod.resolve_from_block(
        None, "http://rpc.invalid", client=httpx.Client(transport=httpx.MockTransport(rpc.handle))
    )

    assert start == HEAD - 9_000
    assert HEAD - start <= RangeCappedRPC.LIMIT, "the default window is wider than the RPC allows"
    assert "eth_blockNumber" in rpc.rpc_calls
    assert "10,000" in note and str(HEAD) in note.replace(",", "")


def test_a_low_head_clamps_at_zero_instead_of_going_negative(monkeypatch):
    """A fresh chain is not a negative block number."""
    rpc = RangeCappedRPC(head=42)

    start, _note = mod.resolve_from_block(
        None, "http://rpc.invalid", client=httpx.Client(transport=httpx.MockTransport(rpc.handle))
    )

    assert start == 0


def test_an_explicit_from_block_is_used_verbatim(wired):
    rpc, _registry = wired

    start, note = mod.resolve_from_block("12345", "http://rpc.invalid")

    assert start == 12345
    assert "explicit" in note
    assert rpc.rpc_calls == [], "an explicit --from-block still dialled the RPC for a head it does not need"


def test_an_unreachable_rpc_falls_back_to_zero_and_says_the_scan_will_probably_fail():
    """Fail-soft, loudly. The one thing that must not happen is a manufactured height."""

    def _dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    start, note = mod.resolve_from_block(
        None, "http://rpc.invalid", client=httpx.Client(transport=httpx.MockTransport(_dead))
    )

    assert start == 0
    assert note.startswith("!")
    assert "-32614" in note and "--from-block" in note


def test_a_junk_from_block_is_refused_not_coerced():
    with pytest.raises(SystemExit):
        mod.resolve_from_block("banana", "http://rpc.invalid")
    with pytest.raises(SystemExit):
        mod.resolve_from_block("-5", "http://rpc.invalid")


# ── the window, against the cap that motivated it ────────────────────────────


async def test_the_bounded_window_is_a_scan_the_arc_rpc_accepts(wired):
    """THE FIX, end to end: head − 9,000 finds the mint the capped RPC will serve."""
    rpc, _registry = wired

    found, detail = await erc8004.find_agent_id(OURS, FakeChainClient(rpc), from_block=HEAD - 9_000)

    assert found == rpc.token_id, detail
    assert rpc.ranges == [(HEAD - 9_000, HEAD)]


async def test_scanning_from_block_zero_is_what_the_arc_rpc_refuses(wired):
    """THE PAIRED PROOF: the old default, against the same fake, is rejected.

    Without this test the one above proves only that a number was passed through. With it,
    the fake is demonstrably enforcing the live RPC's documented limit — so the window is
    load-bearing, not decorative.
    """
    rpc, _registry = wired

    with pytest.raises(ValueError) as excinfo:
        await erc8004.find_agent_id(OURS, FakeChainClient(rpc), from_block=0)

    assert "-32614" in str(excinfo.value)
    assert rpc.ranges == [(0, HEAD)]


# ── --verify: the forwarding that was missing ────────────────────────────────


def test_verify_forwards_the_resolved_window_into_the_discovery_scan(wired, monkeypatch, capsys):
    """THE FIX: ``--verify`` reaches the chain through the bounded window, and finds the id.

    Recording fake at the seam that was dropping the value: ``find_agent_id``'s
    ``from_block``. The old line called ``find_agent_id(owner)`` — no window at all — so
    the recorder would see the module default of 0.
    """
    rpc, _registry = wired
    seen: list[object] = []
    real = erc8004.find_agent_id

    async def _recording(owner, client=None, from_block=0):
        seen.append(from_block)
        return await real(owner, client or FakeChainClient(rpc), from_block)

    monkeypatch.setattr(erc8004, "find_agent_id", _recording)

    rc = _run(monkeypatch, "--verify")
    out = capsys.readouterr().out

    assert seen == [HEAD - 9_000], f"--verify scanned from {seen} instead of the bounded window"
    assert f"agentId:    {rpc.token_id}" in out
    assert rc == 0


def test_verify_honours_an_explicit_from_block(wired, monkeypatch):
    _rpc, _registry = wired
    seen: list[object] = []

    async def _recording(owner, client=None, from_block=0):
        seen.append(from_block)
        return None, "nothing here"

    monkeypatch.setattr(erc8004, "find_agent_id", _recording)

    _run(monkeypatch, "--verify", "--from-block", "51000")

    assert seen == [51000]


def test_verify_with_an_explicit_agent_id_never_scans(wired, monkeypatch):
    """The runbook's post-mint command must not depend on a log scan at all."""
    rpc, _registry = wired

    async def _must_not_run(*a, **kw):
        raise AssertionError("--verify --agent-id scanned the logs")

    monkeypatch.setattr(erc8004, "find_agent_id", _must_not_run)

    assert _run(monkeypatch, "--verify", "--agent-id", str(rpc.token_id)) == 0


# ── --execute: the same window, and the follow-up that was missing ───────────


def test_execute_passes_the_same_bounded_window_to_the_registration_runner(wired, monkeypatch, capsys):
    rpc, _registry = wired
    seen: dict = {}

    async def _fake_register(**kwargs):
        seen.update(kwargs)
        return erc8004.RegistrationResult("noop", rpc.token_id, AGENT_URI, None, "already registered")

    monkeypatch.setattr(erc8004, "register_identity", _fake_register)

    rc = _run(monkeypatch, "--execute")

    assert seen["from_block"] == HEAD - 9_000
    assert rc == 0
    assert "scan:      scanning blocks" in capsys.readouterr().out


def test_a_submitted_mint_still_prints_the_recovery_and_the_follow_up(wired, monkeypatch, capsys):
    """THE FIX: a landed transaction whose scan failed is not left as a dead end.

    This is the exact state the log-range cap produced in production shape — Circle
    reported the transaction, the confirming scan came back empty — and the old code
    printed the result lines and stopped. The agentId is in the receipt; the operator has
    to be told so, in the same breath as the transaction hash, or the next move is a
    second mint.
    """
    _rpc, _registry = wired

    async def _fake_register(**kwargs):
        return erc8004.RegistrationResult(
            "submitted", None, None, TX_HASH, "register() was submitted but no identity is readable yet"
        )

    monkeypatch.setattr(erc8004, "register_identity", _fake_register)

    rc = _run(monkeypatch, "--execute")
    out = capsys.readouterr().out

    assert rc == 2, "submitted is not success"
    # The transaction, and the recovery that turns it into an agentId.
    assert TX_HASH in out
    assert "cast receipt" in out
    assert "topics[3] IS THE AGENT ID" in out
    assert mod.topic(mod.TRANSFER_EVENT_SIG) in out
    assert "--verify --agent-id <AGENT_ID>" in out
    assert "allow-second-identity" in out
    # And the follow-up itself, templated rather than withheld.
    assert "ERC8004_AGENT_ID=<AGENT_ID>" in out
    assert '"agentId": <AGENT_ID>' in out, "the template ships agentId as a string, not an integer"
    assert "agent-registration.json" in out


def test_the_transfer_topic_the_recovery_prints_is_the_one_the_scanner_matches(wired):
    """Two spellings of the same event is how a recovery instruction goes quietly wrong."""
    assert mod.topic(mod.TRANSFER_EVENT_SIG) == erc8004._TRANSFER_TOPIC


# ── the window that is too NARROW: "I could not look" must not read as "nothing there" ──
#
# The bounded window fixes a scan that was always refused. It also introduces the opposite
# failure, and that one is silent: a mint older than the window leaves the scan SUCCEEDING
# and naming nothing. If that were reported as "no identity found", the register path's
# whole idempotency check would invert — its "refusing to mint blind" fail-safe would
# become a fail-OPEN double mint, on a token that cannot be un-minted. So the scan raises.


async def test_a_balance_the_window_cannot_name_raises_instead_of_answering_no(aged):
    """THE FIX: ``balanceOf > 0`` + nothing in range is an error, not a verdict."""
    rpc, _registry = aged

    with pytest.raises(RuntimeError) as excinfo:
        await erc8004.find_agent_id(OURS, FakeChainClient(rpc), from_block=HEAD - 9_000)

    assert "balanceOf" in str(excinfo.value)
    assert "pass the agentId explicitly" in str(excinfo.value)
    # The cap was never hit: this scan was ACCEPTED and simply could not see far enough
    # back. That is what makes the case reachable in the first place.
    assert rpc.ranges == [(HEAD - 9_000, HEAD)]


async def test_a_zero_balance_is_still_a_plain_no_identity_found(wired):
    """THE PAIRED PROOF: the raise did not swallow the one fact that legitimately means "no".

    ``balanceOf == 0`` is the chain saying this wallet holds nothing — the only input that
    may license a mint. Without this test the fix above could be "raise on everything",
    which would make a first registration impossible.
    """
    rpc, registry = wired
    registry.tokens.clear()

    found, detail = await erc8004.find_agent_id(OURS, FakeChainClient(rpc), from_block=HEAD - 9_000)

    assert found is None
    assert "== 0" in detail
    assert rpc.ranges == [], "balanceOf == 0 short-circuits before the scan"


async def test_register_refuses_rather_than_minting_a_second_identity_it_cannot_see(aged):
    """THE FIX, at the only place it costs money: no submit for a wallet that already holds one.

    ``--execute`` on a wallet whose mint has aged out of the window is the exact production
    sequence — register once, come back 77 minutes later, re-run. The assertion that matters
    is ``signer.submits == []``.
    """
    rpc, registry = aged
    signer = CountingSigner()

    result = await erc8004.register_identity(
        agent_uri=AGENT_URI,
        expected_owner=OURS,
        signer=signer,
        client=FakeChainClient(rpc),
        from_block=HEAD - 9_000,
    )

    assert signer.submits == [], (
        f"DOUBLE MINT: register() was submitted for a wallet already holding agentId {rpc.token_id}"
    )
    assert result.action == "refused"
    assert result.agent_id is None
    assert result.tx_hash is None
    assert "refusing to mint blind" in result.detail
    assert "pass the agentId explicitly" in result.detail, "the refusal does not say how to get unstuck"
    assert registry.tokens == {rpc.token_id: (OURS, AGENT_URI)}, "the registry gained a token"


async def test_an_empty_wallet_still_mints_exactly_once(wired):
    """THE PAIRED PROOF: the refusal above is not "refuse always".

    A wallet the chain says is empty still registers, once, and the confirming scan — same
    bounded window — names what it minted.
    """
    rpc, registry = wired
    registry.tokens.clear()
    signer = CountingSigner(registry, rpc, token_id=7)

    result = await erc8004.register_identity(
        agent_uri=AGENT_URI,
        expected_owner=OURS,
        signer=signer,
        client=FakeChainClient(rpc),
        from_block=HEAD - 9_000,
    )

    assert len(signer.submits) == 1, "an empty wallet did not register"
    assert signer.submits[0][1] == erc8004.REGISTER_SIGNATURE
    assert result.action == "registered"
    assert result.agent_id == 7
    assert result.tx_hash == TX_HASH


def test_verify_says_undetermined_not_pending_when_the_window_could_not_look(aged, monkeypatch, capsys):
    """THE FIX on the read-only surface: exit 1 and a different word, not a cheerful zero.

    ``registration_pending (no identity found for this wallet)`` is what step 2 of the
    runbook reads as "step 3 is safe". Printing it for a wallet that demonstrably holds an
    identity is how an operator is talked into the double mint by hand.
    """
    _rpc, _registry = aged

    rc = _run(monkeypatch, "--verify")
    out = capsys.readouterr().out

    assert rc != 0, "an undetermined read exited 0"
    assert "registration_pending" not in out, "an unlookable wallet was reported as pending"
    assert "status:     undetermined" in out
    assert "could not look" in out
    assert "--verify --agent-id <ID>" in out, "the way out is not printed"
    assert "Do NOT run --execute off this answer." in out


def test_verify_still_reports_pending_for_a_wallet_that_really_holds_nothing(wired, monkeypatch, capsys):
    """THE PAIRED PROOF: ``undetermined`` did not eat the honest ``pending``."""
    _rpc, registry = wired
    registry.tokens.clear()

    rc = _run(monkeypatch, "--verify")
    out = capsys.readouterr().out

    assert rc == 0
    assert "status:     registration_pending (no identity found for this wallet)" in out
    assert "undetermined" not in out
