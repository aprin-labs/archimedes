"""The MCP server's tool contract must describe the API that exists.

``mcp-server/`` is a second agent-facing surface over the same HTTP routes, and the design
doc that authorised it named the cost in its own words: *"it is small and adds a second
surface that can drift from the API — the failure mode this repo names by name"*
(``docs/specs/agent-native-onboarding-spec.md`` § 7, decision D2). This file is the answer
to that sentence, and it lives in the **backend** suite on purpose: ``pytest -m "not
integration"`` is the CI-blocking command, so the guard runs on every PR whether or not
anyone remembered to run the MCP distribution's own tests.

Two guards, and the second is the one that catches a lie rather than a typo:

1. **Every declared route resolves.** Reuses ``test_agent_discovery.unresolved_routes`` and
   its single documented ``/api/auth/`` exemption rather than re-deriving either — the
   exemption stays narrow in one place, and widening it there is still the only way to
   disable all three of these guards at once. Same contract
   ``test_agent_quickstart_drift.py`` applies to the quickstart.
2. **Every ``auth`` label is true.** The contract tells an agent which tools need a
   credential; that claim is checked by driving the real app with no credential at all and
   reading what comes back. A route the contract calls gated must ``401``; a route it calls
   public must not. A mislabelled row is worse than a missing one — an agent plans around
   the label, omits the credential, gets a ``401``, and cannot tell a wrong flag from a
   broken endpoint (the #1293 finding).

``contract.py`` is read by path with ``importlib`` and imports nothing itself, so this file
needs neither the ``mcp`` SDK nor the ``archimedes-mcp`` distribution installed. A guard
that required installing the thing it guards would be a guard that gets skipped.

Hermetic: ``TESTING=1`` import of ``archimedes.main`` (conftest sets it) plus in-memory
route matching. No DB / Redis / RPC / network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_agent_discovery import _openapi_index, unresolved_routes

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPO_ROOT / "mcp-server" / "src" / "archimedes_mcp" / "contract.py"


def _contract() -> ModuleType:
    assert CONTRACT_PATH.exists(), f"{CONTRACT_PATH} is missing — the MCP server's tool contract"
    spec = importlib.util.spec_from_file_location("archimedes_mcp_contract_under_test", CONTRACT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = _contract()

# A concrete value for every path parameter, so a template can be exercised as a request.
# The ids are deliberately ones that do not exist: this guard reads the AUTH decision, and
# auth is resolved before ownership or existence — an unauthenticated caller gets 401, an
# authenticated one gets 404. That ordering is what makes a nonexistent id a safe probe.
_PATH_PARAMS = {"{job_id}": "mcp-drift-probe", "{strategy_id}": "mcp-drift-probe"}


def _concrete(path: str) -> str:
    for template, value in _PATH_PARAMS.items():
        path = path.replace(template, value)
    assert "{" not in path, f"{path} has a path parameter with no probe value in _PATH_PARAMS"
    return path


def _tool_requests() -> list[tuple[str, str, str, str]]:
    """``(tool name, auth label, method, concrete path)`` for every route every tool calls."""
    return [
        (tool["name"], tool["auth"], route.split(" ", 1)[0], _concrete(route.split(" ", 1)[1]))
        for tool in CONTRACT.TOOLS
        for route in tool["routes"]
    ]


def test_every_route_the_mcp_server_calls_exists():
    missing = unresolved_routes(list(CONTRACT.routes()), _openapi_index())
    assert missing == [], f"MCP tools call routes this API does not serve: {missing}"


def test_the_contract_declares_at_least_the_tools_it_promises():
    """A smoke check that the module read here is the real one, not an empty stand-in."""
    assert len(CONTRACT.TOOLS) >= 9
    assert set(CONTRACT.TOOL_NAMES) == {tool["name"] for tool in CONTRACT.TOOLS}


@pytest.mark.parametrize(
    ("tool_name", "auth", "method", "path"),
    _tool_requests(),
    ids=[f"{name}:{method} {path}" for name, _auth, method, path in _tool_requests()],
)
async def test_auth_labels_match_what_the_app_actually_does(tool_name, auth, method, path):
    """Drive the real app with NO credential and check the label against the answer.

    Bodies are omitted deliberately. ``docs/agent-quickstart.md`` records that
    authentication is checked *before* the body is validated, so an unauthenticated request
    with a missing body is a ``401`` and not a ``422`` — which is exactly the property being
    read here, and reading it without a body keeps this test independent of every request
    schema.
    """
    from archimedes.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(method, path)

    if auth == CONTRACT.AUTH_CREDENTIAL:
        assert response.status_code == 401, (
            f"{tool_name} is declared auth={auth!r}, but {method} {path} answered "
            f"{response.status_code} without a credential. Either the route stopped being "
            f"gated or the contract is lying to the agent that reads it."
        )
    else:
        assert response.status_code != 401, (
            f"{tool_name} is declared auth={auth!r} (public), but {method} {path} answered 401 "
            f"without a credential. An agent will plan around that label, omit the credential, "
            f"and be unable to tell a wrong flag from a broken endpoint."
        )


def test_exactly_one_tool_is_labelled_metered():
    """The cost label is a money claim, and an agent reads it as ground truth.

    Pinned as an exact set: the day a second route starts charging, this fails and forces
    the tool description, the README table and the quickstart section to be corrected in
    the same change, rather than a newly metered tool inheriting a 'free' story.
    """
    metered = {tool["name"] for tool in CONTRACT.TOOLS if tool["cost"] == CONTRACT.COST_METERED}
    assert metered == {"archimedes_generate_start"}


def test_the_metered_tool_is_the_one_that_calls_the_paywalled_route():
    """The label and the route it labels must agree.

    ``POST /api/generate/start`` is the only route in this contract that sits behind
    ``generation_payment``'s paywall. If some other tool ever calls it, that tool is metered
    too and this fails.
    """
    callers = {tool["name"] for tool in CONTRACT.TOOLS if "POST /api/generate/start" in tool["routes"]}
    assert callers == {"archimedes_generate_start"}
    assert CONTRACT.by_name("archimedes_generate_start")["cost"] == CONTRACT.COST_METERED


def test_no_tool_reaches_a_route_outside_the_public_api():
    """Thin-client anti-goal, checked rather than asserted in prose.

    Every declared route is under ``/api/``, and none is an internal or operator surface.
    ``/api/agent/internal`` style routes and anything guarded by ``X-Internal-Agent-Key``
    are not a public capability, and an MCP tool must never be the thing that reaches one.
    """
    for tool in CONTRACT.TOOLS:
        for route in tool["routes"]:
            _method, _, path = route.partition(" ")
            assert path.startswith("/api/"), f"{tool['name']} declares a non-API route: {route}"
            assert "internal" not in path, f"{tool['name']} declares an internal route: {route}"


def test_the_contract_names_both_credential_lanes():
    """The two documented ways to authenticate, and no third one.

    ``X-Internal-Agent-Key`` is explicitly NOT an external-agent credential — it is a single
    shared secret that grants ``internal`` classification, and reusing it for externals is
    listed in the spec's D3 row only to be rejected.
    """
    sources = " ".join(CONTRACT.CREDENTIAL_SOURCES)
    assert "ARCHIMEDES_API_KEY" in sources
    assert "Authorization: Bearer" in sources
    assert "session.json" in sources
    assert "X-Internal-Agent-Key" not in sources


# ═══════════════════════════════════════════════════════════════════════════
# The verdict this contract describes is the STORED one (#1746 PR-B)
# ═══════════════════════════════════════════════════════════════════════════
#
# `strategies_routes`' published OpenAPI descriptions are guarded by
# `test_curated_verdict_parity.py::test_the_published_description_says_the_
# verdict_is_read_not_computed`. This is the same guard for the OTHER
# agent-facing description of the same routes. It exists because the first fix
# round of #1746 PR-B updated `archimedes_passport` here and left
# `archimedes_strategy` and `archimedes_generate_status` saying that
# `GET /api/strategies/{id}` answers a LIVE gate that "wins" over the stored
# verdict — a direct contradiction of the tool right beside it, on the exact
# question the issue was about.

#: Words for a gate run made DURING a read. No route this contract exposes does
#: that any more: the verdict is graded once, stored, and served
#: (docs/adr/rigor-verdict-of-record.md). `POST /api/rigor/verify` DOES compute
#: on request, but over a returns series the CALLER supplies — it grades no
#: stored strategy, and it describes itself without these words.
_RETIRED_LIVE_GATE_PHRASES = ("live gate", "live verdict", "live rigor gate")


def test_no_tool_promises_a_live_gate():
    """MUTATION: restore "the live gate wins" to ``archimedes_generate_status``."""
    offenders = [
        f"{tool['name']} says {phrase!r}"
        for tool in CONTRACT.TOOLS
        for phrase in _RETIRED_LIVE_GATE_PHRASES
        if phrase in tool["description"].lower()
    ]
    assert not offenders, (
        f"these MCP tool descriptions still promise a gate run made during the read: {offenders}. "
        "Every route this contract exposes serves the STORED verdict of record — "
        "docs/adr/rigor-verdict-of-record.md."
    )


def test_every_tool_that_documents_the_four_state_says_it_is_stored():
    """A tool explaining ``rigor_gate_status`` must say where the answer comes from.

    Anti-vacuity: at least two tools must document the four-state at all, so
    deleting the explanation cannot turn this guard green.

    MUTATION: drop "STORED verdict of record" from ``archimedes_strategy``.
    """
    documenting = [t for t in CONTRACT.TOOLS if "rigor_gate_status" in t["description"]]
    assert len(documenting) >= 2, f"only {len(documenting)} tools document rigor_gate_status"
    for tool in documenting:
        assert "stored" in tool["description"].lower(), (
            f"{tool['name']} documents rigor_gate_status without saying the verdict is STORED — "
            "the one thing an agent comparing two endpoints needs to know."
        )
        assert "no real returns yet" not in tool["description"].lower(), (
            f"{tool['name']} defines 'pending' as 'no real returns yet'. It also means "
            "'the grading job has not run over the returns that ARE there', which is the "
            "state every curated strategy is in until docs/runbooks/curated-backtests.md "
            "§ 'Grading on its own' has been run — #1184's conflation, published to agents."
        )
