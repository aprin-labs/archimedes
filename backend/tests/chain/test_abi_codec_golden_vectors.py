"""Golden ABI-codec vectors for every ABI shape the backend encodes or decodes.

Why this exists. The ABI codec is on the funds path: it builds the calldata for
``commit``/``rebalance``/``deposit``/``approve``, the preimage of the commit-reveal
``tradeId``, and the marketplace ``pool_id``, and it decodes every contract read
and every event the backend parses. ``backend/Dockerfile`` resolves
``requirements-base.txt``'s floors on every build with no lockfile, so the codec
in a new image is whatever the resolver picked that day. eth-abi 6.0 (the web3 8
bump, #1907) also changed ``encode``/``decode`` to reuse a cached
``get_tuple_encoder``/``get_tuple_decoder`` per type tuple instead of building a
fresh ``TupleEncoder``/``TupleDecoder`` per call. A codec change that shifts one
byte would not raise; it would sign a different transaction.

What is pinned. ``backend/tests/fixtures/abi_codec_golden.json`` holds hex captured under the
pre-#1907 ``main`` environment (web3 7.16.0 / eth-abi 5.2.0; the versions are
recorded in the file). Every vector must stay byte-identical:

* ``direct`` -- the type lists the code passes to ``eth_abi.encode`` itself
  (found with ``graft grep "eth_abi|abi_encode|abi_decode"``):
  ``chain/executor.py`` ``_REBALANCE_ABI_TYPES`` (``compute_trade_id``),
  ``marketplace/encoding.py`` ``["string", "address"]`` (``derive_pool_id``),
  ``api/_erc6492.py`` ``["address", "bytes32", "bytes"]``,
  ``scripts/deploy_contracts.py``'s constructor-arg lists, and the
  ``["string"]`` encode + decode in ``scripts/register_erc8004_identity.py``.
* ``functions`` -- every contract function the backend calls through web3
  (``graft grep "\\.functions\\.[A-Za-z_]+" --in backend/archimedes``: 54 names,
  59 distinct signature/output shapes across ``contracts/abis``): calldata from
  ``build_transaction``, the raw signed transaction for state-changing calls, and
  the call-result encoding plus its decode through ``w3.codec``.
* ``events`` -- the four events the backend decodes with ``process_log``
  (``TraceCommitted``, ``TracePublished``, ``TraceRevealed``, ``VaultCreated``).

Argument values are not stored: ``_gen`` derives them from a SHA-256 of the
vector's id, so the same id always yields the same values. Each vector is checked
in both directions (values -> golden hex, and golden hex -> values), and the whole
set is run twice in interleaved order so the second pass goes through eth-abi
6's warm encoder/decoder cache with different values for the same type tuple.

Hermetic: no RPC, no chain, no network. ``build_transaction`` gets every field it
would otherwise look up, and the provider raises if anything asks it for one.

Re-capturing. Only when a vector is meant to change (a contract ABI changed, or a
codec change was reviewed and accepted). Run under the environment whose output
is the reference, from the repo root::

    PYTHONPATH=backend python backend/tests/chain/test_abi_codec_golden_vectors.py --capture \\
        > /tmp/abi_codec_golden.json && mv /tmp/abi_codec_golden.json backend/tests/fixtures/

and say in the PR which environment produced it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from archimedes.chain.executor import _REBALANCE_ABI_TYPES, compute_trade_id
from archimedes.marketplace.encoding import derive_pool_id
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import event_abi_to_log_topic, keccak
from hexbytes import HexBytes
from web3 import Web3
from web3.providers.base import BaseProvider

REPO_ROOT = Path(__file__).resolve().parents[3]
ABI_DIR = REPO_ROOT / "contracts" / "abis"
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "abi_codec_golden.json"

# Contract functions the backend calls via ``contract.functions.<name>`` and the
# events it decodes via ``process_log``. Every ABI entry with one of these names
# is pinned (an interface and its implementation can share a name with
# different types, so all shapes are kept, deduplicated by signature + outputs).
BACKEND_FUNCTIONS = frozenset(
    {
        # funds path: writes
        "approve", "commit", "createPool", "createVault", "deposit", "publishTrace", "rebalance",
        "registerStrategy", "reveal", "setTargetAllocations", "setTokenOracles",
        # reads
        "balanceOf", "creator", "decimals", "getAllPools", "getAmountOut", "getCommitment", "getHoldings",
        "getPool", "getPrice", "getStrategy", "getSynthetics", "getTargetAllocations", "getTraceById",
        "getTracesByVault", "getVaults", "highWaterMark", "isAgentAssisted", "isFresh", "isRegistered",
        "lastUpdated", "managementFeeBps", "name", "owner", "ownerOf", "paused", "pendingTradeCommitment",
        "performanceFeeBps", "pools", "price", "reserve0", "reserve1", "strategyCount", "swapFeeBps", "symbol",
        "tier", "token0", "token1", "tokenURI", "tokenVault", "totalAssets", "totalSupply", "traceCount",
        "vaultCount",
    }
)  # fmt: skip
BACKEND_EVENTS = frozenset({"TraceCommitted", "TracePublished", "TraceRevealed", "VaultCreated"})

# Fixed signing context for the raw-transaction vectors: the explicit tx-dict
# shape the backend's executor and trace publisher pass to build_transaction.
_SIGNER = Account.from_key("0x" + hashlib.sha256(b"archimedes-abi-codec-golden").hexdigest())
_CONTRACT = Web3.to_checksum_address("0x" + hashlib.sha256(b"abi-codec-golden-contract").hexdigest()[:40])
_TX_FIELDS = {"from": _SIGNER.address, "nonce": 7, "chainId": 5042002, "gas": 300_000, "gasPrice": 160_000_000_000}


def _addr(n: int) -> str:
    return Web3.to_checksum_address("0x" + hashlib.sha256(f"addr-{n}".encode()).hexdigest()[:40])


A = [_addr(i) for i in range(6)]

# (id, types, values). Values are explicit here because these are the shapes the
# backend builds by hand; edge values (0, 2**40, 2**256 - 1, empty arrays, empty
# and multi-word bytes, multi-byte UTF-8) are where a codec slip would hide.
DIRECT_CASES: list[tuple[str, list[str], list[Any]]] = [
    # The arrays of test_commit_reveal_tradeid.py's "mixed_buy_and_two_sells",
    # whose keccak was computed independently with Foundry (chisel/cast).
    (
        "trade_id/mixed",
        _REBALANCE_ABI_TYPES,
        [
            ["0x1111111111111111111111111111111111111111"],
            [1_000_000],
            ["0x2222222222222222222222222222222222222222", "0x3333333333333333333333333333333333333333"],
            [2 * 10**18, 5 * 10**17],
        ],
    ),
    ("trade_id/empty", _REBALANCE_ABI_TYPES, [[], [], [], []]),
    (
        "trade_id/extremes",
        _REBALANCE_ABI_TYPES,
        [A[:4], [0, 1, 2**40, 2**256 - 1], A[3::-1], [2**255, 2**64 - 1, 10**30, 7]],
    ),
    ("pool_id/ascii", ["string", "address"], ["strat-1", A[0]]),
    ("pool_id/unicode", ["string", "address"], ["ü-unicode-策略-" + "x" * 40, A[3]]),
    ("erc6492/65-byte-sig", ["address", "bytes32", "bytes"], [A[1], keccak(b"msg"), bytes(range(65))]),
    (
        "erc6492/6492-wrapped",
        ["address", "bytes32", "bytes"],
        [A[2], b"\x05" * 32, bytes(range(100)) + bytes.fromhex("6492" * 16)],
    ),
    ("erc6492/empty-sig", ["address", "bytes32", "bytes"], [A[2], b"\x00" * 32, b""]),
    ("deploy_ctor/address", ["address"], [A[0]]),
    ("deploy_ctor/address,address", ["address", "address"], [A[0], A[1]]),
    ("deploy_ctor/5xaddress", ["address"] * 5, A[:5]),
    ("deploy_ctor/oracle", ["string", "uint256", "address"], ["AAPL", 23_145_000_000, A[5]]),
    ("erc8004_register/string", ["string"], ["https://archimedes-arc.com/.well-known/agent-card.json"]),
]


class _NoNetwork(BaseProvider):
    """Fails the test the moment anything tries to reach a node."""

    def make_request(self, method: Any, params: Any) -> Any:
        raise AssertionError(f"hermetic codec test made an RPC: {method}({params!r})")

    def is_connected(self, show_traceback: bool = False) -> bool:
        return False


W3 = Web3(_NoNetwork())


def _type_str(p: dict) -> str:
    t = p["type"]
    if t.startswith("tuple"):
        return "(" + ",".join(_type_str(c) for c in p["components"]) + ")" + t[len("tuple") :]
    return t


def _sig(entry: dict) -> str:
    return entry["name"] + "(" + ",".join(_type_str(i) for i in entry.get("inputs", [])) + ")"


def _gen(p: dict, seed: str) -> Any:
    """Deterministic value of ABI param ``p`` derived from ``seed`` alone."""
    t = p["type"]
    h = hashlib.sha256(f"{seed}|{t}".encode()).digest()
    if t.endswith("]"):
        inner = dict(p, type=t[: t.rindex("[")])
        dim = t[t.rindex("[") + 1 : -1]
        n = int(dim) if dim else h[0] % 4  # dynamic arrays: 0..3 elements, empty included
        return [_gen(inner, f"{seed}/{i}") for i in range(n)]
    if t == "tuple":
        return tuple(_gen(c, f"{seed}/{i}") for i, c in enumerate(p["components"]))
    if t == "address":
        return Web3.to_checksum_address(h[:20])
    if t == "bool":
        return bool(h[0] & 1)
    if t == "string":
        return f"s-{h[:3].hex()}-ü" + "z" * (h[1] % 40)
    if t == "bytes":
        return (h + hashlib.sha256(h).digest())[: h[1] % 65]  # 0..64 bytes: crosses a word boundary
    if t.startswith("bytes"):
        return h[: int(t[5:])]
    if t.startswith("uint"):
        return int.from_bytes(h, "big") % 2 ** int(t[4:] or 256)
    if t.startswith("int"):
        bits = int(t[3:] or 256)
        v = int.from_bytes(h, "big") % 2 ** (bits - 1)
        return -v if h[1] & 1 else v
    raise ValueError(f"no generator for ABI type {t!r}")


def _norm(v: Any) -> Any:
    """Comparable form: bytes as hex, sequences as lists, addresses lower-case."""
    if isinstance(v, bytes | bytearray):
        return "0x" + bytes(v).hex()
    if isinstance(v, str) and v.startswith("0x") and len(v) == 42:
        return v.lower()
    if isinstance(v, list | tuple):
        return [_norm(x) for x in v]
    if hasattr(v, "items"):
        return {str(k): _norm(x) for k, x in v.items()}
    return v


def _load_abis() -> dict[str, list[dict]]:
    out = {}
    for path in sorted(ABI_DIR.glob("*.json")):
        abi = json.loads(path.read_text())
        out[path.name] = abi.get("abi", []) if isinstance(abi, dict) else abi
    return out


def _fragment(entry: dict) -> dict:
    keep = ("type", "name", "inputs", "outputs", "stateMutability", "anonymous")
    return {k: entry[k] for k in keep if k in entry}


def _event_log(entry: dict, values: list[Any]) -> tuple[list[str], str]:
    topics = [] if entry.get("anonymous") else [event_abi_to_log_topic(entry)]
    data_types, data_values = [], []
    for param, value in zip(entry["inputs"], values, strict=True):
        t = _type_str(param)
        if param.get("indexed"):
            dynamic = t in ("string", "bytes") or t.endswith("]") or t.startswith("(")
            topics.append(keccak(abi_encode([t], [value])) if dynamic else abi_encode([t], [value]))
        else:
            data_types.append(t)
            data_values.append(value)
    return ["0x" + bytes(x).hex() for x in topics], "0x" + abi_encode(data_types, data_values).hex()


def _function_vector(fragment: dict) -> dict[str, str]:
    sig = _sig(fragment)
    contract = W3.eth.contract(address=_CONTRACT, abi=[fragment])
    args = [_gen(p, f"{sig}:in{i}") for i, p in enumerate(fragment.get("inputs", []))]
    tx = contract.get_function_by_signature(sig)(*args).build_transaction(dict(_TX_FIELDS))
    out_types = [_type_str(o) for o in fragment.get("outputs", [])]
    out_values = [_gen(o, f"{sig}:out{i}") for i, o in enumerate(fragment.get("outputs", []))]
    vector = {"calldata": tx["data"], "output": "0x" + W3.codec.encode(out_types, out_values).hex()}
    if fragment.get("stateMutability") not in ("view", "pure"):
        vector["signed_raw"] = "0x" + bytes(_SIGNER.sign_transaction(tx).raw_transaction).hex()
    return vector


def _capture() -> dict:
    """Build the fixture from the CURRENT environment (see module docstring)."""
    import eth_abi
    import web3

    golden: dict[str, Any] = {
        "_captured_with": {"web3": web3.__version__, "eth_abi": eth_abi.__version__},
        "direct": {cid: "0x" + abi_encode(types, values).hex() for cid, types, values in DIRECT_CASES},
        "functions": [],
        "events": [],
    }
    seen: set[tuple[str, str]] = set()
    for abi_file, abi in _load_abis().items():
        for entry in abi:
            if entry.get("type") == "function" and entry["name"] in BACKEND_FUNCTIONS:
                key = (_sig(entry), ",".join(_type_str(o) for o in entry.get("outputs", [])))
                if key in seen:
                    continue
                seen.add(key)
                fragment = _fragment(entry)
                golden["functions"].append({"abi": abi_file, "fragment": fragment, **_function_vector(fragment)})
            elif entry.get("type") == "event" and entry["name"] in BACKEND_EVENTS:
                sig = _sig(entry)
                if any(e["fragment"]["name"] == entry["name"] for e in golden["events"]):
                    continue
                values = [_gen(p, f"event {sig}:{i}") for i, p in enumerate(entry["inputs"])]
                topics, data = _event_log(entry, values)
                golden["events"].append({"abi": abi_file, "fragment": _fragment(entry), "topics": topics, "data": data})
    return golden


GOLDEN = json.loads(GOLDEN_PATH.read_text()) if GOLDEN_PATH.exists() else {"direct": {}, "functions": [], "events": []}


def _fn_id(v: dict) -> str:
    return f"{v['abi'].removesuffix('.json')}.{_sig(v['fragment'])}"


# ---------------------------------------------------------------------------
# direct eth_abi call sites
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("case_id", "types", "values"), DIRECT_CASES, ids=[c[0] for c in DIRECT_CASES])
def test_direct_encode_matches_golden(case_id: str, types: list[str], values: list[Any]) -> None:
    assert "0x" + abi_encode(types, values).hex() == GOLDEN["direct"][case_id]


@pytest.mark.parametrize(("case_id", "types", "values"), DIRECT_CASES, ids=[c[0] for c in DIRECT_CASES])
def test_direct_golden_decodes_to_values(case_id: str, types: list[str], values: list[Any]) -> None:
    decoded = abi_decode(types, HexBytes(GOLDEN["direct"][case_id]))
    assert _norm(decoded) == _norm(values)


# Foundry-computed keccak256(abi.encode(...)) of the "trade_id/mixed" arrays
# (test_commit_reveal_tradeid.py). An anchor for the golden that is not eth-abi.
_FOUNDRY_TRADE_ID_MIXED = "0xe28398ae52f239509c556342507d5c165cd56e51732fff6686b92b3a273a9191"


def test_backend_helpers_hash_the_golden_preimages() -> None:
    """compute_trade_id / derive_pool_id are keccak over exactly these encodings."""
    _, _, trade = DIRECT_CASES[0]
    golden_preimage = HexBytes(GOLDEN["direct"]["trade_id/mixed"])
    assert "0x" + keccak(golden_preimage).hex() == _FOUNDRY_TRADE_ID_MIXED
    assert compute_trade_id(*trade) == keccak(golden_preimage)
    assert derive_pool_id("strat-1", A[0].lower()) == "0x" + keccak(HexBytes(GOLDEN["direct"]["pool_id/ascii"])).hex()


def test_warm_codec_cache_stays_byte_identical() -> None:
    """eth-abi 6 caches one tuple encoder/decoder per type tuple. Reuse it across
    different values for the same types (three trade_id vectors share one), in
    interleaved order, and every result must still match its own golden."""
    order = DIRECT_CASES + DIRECT_CASES[::-1] + DIRECT_CASES
    for case_id, types, values in order:
        assert "0x" + abi_encode(types, values).hex() == GOLDEN["direct"][case_id], case_id
        assert _norm(abi_decode(types, HexBytes(GOLDEN["direct"][case_id]))) == _norm(values), case_id


# ---------------------------------------------------------------------------
# contract functions and events, through web3's own encode/decode path
# ---------------------------------------------------------------------------


def test_fixture_covers_every_backend_function_and_event() -> None:
    names = {v["fragment"]["name"] for v in GOLDEN["functions"]}
    events = {v["fragment"]["name"] for v in GOLDEN["events"]}
    assert names == BACKEND_FUNCTIONS, sorted(names ^ BACKEND_FUNCTIONS)
    assert events == BACKEND_EVENTS, sorted(events ^ BACKEND_EVENTS)


@pytest.mark.parametrize("vector", GOLDEN["functions"], ids=_fn_id)
def test_function_vectors_are_byte_identical(vector: dict) -> None:
    fragment = vector["fragment"]
    got = _function_vector(fragment)
    assert got["calldata"] == vector["calldata"], "calldata"
    assert got["output"] == vector["output"], "call-result encoding"
    assert got.get("signed_raw") == vector.get("signed_raw"), "signed raw transaction"

    out_types = [_type_str(o) for o in fragment.get("outputs", [])]
    expected = [_gen(o, f"{_sig(fragment)}:out{i}") for i, o in enumerate(fragment.get("outputs", []))]
    assert _norm(W3.codec.decode(out_types, HexBytes(vector["output"]))) == _norm(expected), "call-result decoding"


@pytest.mark.parametrize("vector", GOLDEN["events"], ids=_fn_id)
def test_event_vectors_round_trip(vector: dict) -> None:
    fragment = vector["fragment"]
    sig = _sig(fragment)
    values = [_gen(p, f"event {sig}:{i}") for i, p in enumerate(fragment["inputs"])]
    assert _event_log(fragment, values) == (vector["topics"], vector["data"]), "event encoding"

    contract = W3.eth.contract(address=_CONTRACT, abi=[fragment])
    log = {
        "address": _CONTRACT,
        "topics": [HexBytes(t) for t in vector["topics"]],
        "data": HexBytes(vector["data"]),
        "blockNumber": 123,
        "transactionHash": HexBytes(b"\x11" * 32),
        "transactionIndex": 0,
        "blockHash": HexBytes(b"\x22" * 32),
        "logIndex": 3,
        "removed": False,
    }
    decoded = getattr(contract.events, fragment["name"])().process_log(log)
    assert _norm(dict(decoded["args"])) == {
        p["name"]: _norm(v) for p, v in zip(fragment["inputs"], values, strict=True)
    }


def test_pinned_fragments_still_match_contracts_abis() -> None:
    """The vectors pin the types the backend uses TODAY. If a checked-in ABI
    changes one of these entries, the vector is testing a stale shape: re-capture
    (module docstring) rather than letting this pass on the old types."""
    abis = _load_abis()
    for vector in GOLDEN["functions"] + GOLDEN["events"]:
        live = [_fragment(e) for e in abis[vector["abi"]] if e.get("name") == vector["fragment"]["name"]]
        assert vector["fragment"] in live, f"{_fn_id(vector)} no longer matches contracts/abis/{vector['abi']}"


if __name__ == "__main__":
    if sys.argv[1:] != ["--capture"]:
        sys.exit("usage: PYTHONPATH=backend python backend/tests/chain/test_abi_codec_golden_vectors.py --capture")
    json.dump(_capture(), sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
