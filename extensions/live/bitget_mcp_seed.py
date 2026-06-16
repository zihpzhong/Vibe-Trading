"""Bitget MCP server seed for ``~/.vibe-trading/agent.json``.

Operator copies or merges this block into ``mcp_servers.bitget``. WRITE tools
(``place_order``, ``cancel_order``) are omitted by default — add them by hand
after a mandate is committed (same model as Robinhood).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

#: Broker key used across live paths, mandate store, and MCP server name.
BITGET_BROKER_KEY = "bitget"

#: READ-only tool allowlist — must match ``BITGET_TOOL_CLASS`` READ entries.
BITGET_READ_TOOLS: tuple[str, ...] = (
    "get_account",
    "get_positions",
    "get_quotes",
    "list_orders",
)

#: WRITE tools — enable only after mandate commit.
BITGET_WRITE_TOOLS: tuple[str, ...] = (
    "place_order",
    "cancel_order",
)


def bitget_mcp_server_script() -> Path:
    """Return the absolute path to ``bitget_mcp_server.py``."""
    return Path(__file__).resolve().parent / "bitget_mcp_server.py"


def build_bitget_mcp_server_seed(*, include_write_tools: bool = False) -> dict[str, Any]:
    """Build a stdio MCP server config dict for ``agent.json``.

    Args:
        include_write_tools: When True, include ``place_order`` and ``cancel_order``.

    Returns:
        A dict suitable for ``mcp_servers["bitget"]`` in operator config.
    """
    tools = list(BITGET_READ_TOOLS)
    if include_write_tools:
        tools.extend(BITGET_WRITE_TOOLS)
    return {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(bitget_mcp_server_script())],
        "enabled_tools": tools,
    }


#: Static seed template (paths resolved at ``build_bitget_mcp_server_seed`` time).
BITGET_MCP_SERVER_SEED: dict[str, Any] = build_bitget_mcp_server_seed()
