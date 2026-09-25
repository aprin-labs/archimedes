"""The MCP server itself — registration and stdio transport, and nothing else.

This is the only module that imports the MCP SDK. Everything a tool actually does lives in
:mod:`tools`, :mod:`client` and :mod:`contract`, none of which import ``mcp``. That split
is why ``backend/tests/test_mcp_contract_drift.py`` can read the contract from inside the
backend unit suite without installing this distribution, and why the tool logic is testable
without standing up a protocol session.

SDK note: this targets ``mcp`` 2.x, where ``FastMCP`` was renamed ``MCPServer``
(``mcp.server.mcpserver``). Pinned in ``pyproject.toml`` as ``mcp>=2.1`` rather than left
open, because 1.x code does not import under 2.x and the reverse is also true.
"""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from . import contract
from .tools import HANDLERS

logger = logging.getLogger(__name__)


def build_server() -> MCPServer:
    """Register every tool the contract declares, with the contract's own description.

    The description an agent reads is the one the contract carries — there is no second
    copy in a decorator to fall out of step with it. ``tests/test_contract_sync.py``
    asserts the registered set and the declared set are identical in both directions.
    """
    server = MCPServer(
        name=contract.SERVER_NAME,
        version=_version(),
        instructions=contract.SERVER_INSTRUCTIONS,
    )
    for spec in contract.TOOLS:
        name = spec["name"]
        server.add_tool(
            HANDLERS[name],
            name=name,
            description=spec["description"],
            structured_output=True,
        )
    return server


def _version() -> str:
    from . import __version__

    return __version__


def main() -> None:
    """Console-script entry point: serve MCP over stdio.

    Logging goes to stderr at WARNING. stdout is the protocol transport — anything written
    there that is not a JSON-RPC frame corrupts the session — and stderr is kept quiet by
    default because this process's log is the *client's* log, and a chatty server buries the
    agent's own output. No log line in this distribution carries a credential; see
    ``credentials.py``.
    """
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    build_server().run(transport="stdio")


__all__ = ["build_server", "main"]
