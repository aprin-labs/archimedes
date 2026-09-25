"""``archimedes-mcp`` — a thin MCP server over the public Archimedes HTTP API.

Thin is the whole design: no business logic, no database, no Redis, no chain RPC, no
wallet key. If a capability is not in the public HTTP API, this server does not have it.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
