"""The contract, the handlers, and the registered MCP tools must be the same set.

``contract.py`` is what ``backend/tests/test_mcp_contract_drift.py`` reads and what a
reviewer reads. If it can describe a tool that is not registered, or miss one that is, then
it is documentation rather than a contract and the backend guard is checking a fiction.

Same promise ``cli/tests/test_cli.py`` makes about ``cli/src/archimedes_cli/manifest.py``:
the hand-written contract is asserted against the real command tree, both directions.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from archimedes_mcp import contract, tools
from archimedes_mcp.server import build_server


def _registered() -> dict[str, str]:
    server = build_server()
    listed = asyncio.run(server.list_tools())
    return {tool.name: tool.description or "" for tool in listed}


def test_declared_and_implemented_sets_match():
    assert set(contract.TOOL_NAMES) == set(tools.HANDLERS), (
        "contract.TOOLS and tools.HANDLERS disagree — one declares a tool the other does not have"
    )


def test_declared_and_registered_sets_match():
    assert set(contract.TOOL_NAMES) == set(_registered())


def test_registered_descriptions_are_the_contract_descriptions():
    """No second copy of a description. The one an agent reads is the one CI checks."""
    registered = _registered()
    for spec in contract.TOOLS:
        assert registered[spec["name"]] == spec["description"]


def test_every_tool_declares_at_least_one_route():
    for spec in contract.TOOLS:
        assert spec["routes"], f"{spec['name']} declares no route"
        for route in spec["routes"]:
            method, _, path = route.partition(" ")
            assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"}, route
            assert path.startswith("/api/"), route


def test_cost_and_auth_labels_are_from_the_closed_vocabulary():
    for spec in contract.TOOLS:
        assert spec["cost"] in {contract.COST_FREE, contract.COST_METERED}, spec["name"]
        assert spec["auth"] in {contract.AUTH_NONE, contract.AUTH_CREDENTIAL}, spec["name"]


def test_exactly_one_tool_is_metered_and_it_is_generate_start():
    """A cost label that drifts is a lie an agent spends money on.

    Pinned as an exact set rather than 'at least one': the day a second tool starts
    charging, this test fails and forces the description and the docs to be updated in the
    same change, instead of a new metered tool quietly inheriting a 'free' story.
    """
    metered = {s["name"] for s in contract.TOOLS if s["cost"] == contract.COST_METERED}
    assert metered == {"archimedes_generate_start"}


def test_metered_tool_says_so_in_its_description():
    """`claims must be true` applies to a tool description — an agent reads it as ground truth."""
    spec = contract.by_name("archimedes_generate_start")
    lowered = spec["description"].lower()
    assert "can spend money" in lowered
    assert "quote" in lowered


def test_corpus_search_description_does_not_claim_semantic_retrieval():
    """The corpus has no embedding column; retrieval is lexical. Saying otherwise is the
    exact over-claim ``docs/claims-ledger.md`` exists to catch."""
    description = contract.by_name("archimedes_corpus_search")["description"].lower()
    assert "lexical only" in description
    assert "no embeddings" in description
    for forbidden in ("semantic search", "vector search", "embedding-based", "similarity search"):
        assert forbidden not in description


@pytest.mark.parametrize("spec", contract.TOOLS, ids=[s["name"] for s in contract.TOOLS])
def test_handler_signature_has_no_credential_parameter(spec):
    """A tool must never accept a credential as an argument.

    An LLM decides these arguments. A ``token``/``api_key``/``password`` parameter would
    invite a model to invent, echo, or log one, and would put a secret into the tool-call
    transcript the client renders. Credentials come from the environment and the 0600 cache,
    never from the conversation.
    """
    params = set(inspect.signature(tools.HANDLERS[spec["name"]]).parameters)
    for forbidden in ("token", "api_key", "apikey", "password", "cookie", "session", "authorization", "secret"):
        assert forbidden not in params, f"{spec['name']} takes a credential-shaped argument"
