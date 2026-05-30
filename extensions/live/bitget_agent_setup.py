"""Operator setup helpers for Bitget agent + mandate live trading.

Merges the Bitget MCP stdio server into ``~/.vibe-trading/agent.json`` and
reports credential / config readiness. No network calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from extensions.live.bitget_mcp_seed import (
    BITGET_BROKER_KEY,
    build_bitget_mcp_server_seed,
)

_ENV_KEYS = ("BITGET_API_KEY", "BITGET_SECRET", "BITGET_PASSPHRASE")


def bitget_credentials_configured() -> bool:
    """Return whether Bitget API credentials are present in the environment."""
    return all(os.environ.get(key, "").strip() for key in _ENV_KEYS)


def known_live_broker_keys() -> list[str]:
    """Return upstream live broker keys plus ``bitget``."""
    from src.config.schema import LIVE_BROKER_SERVER_KEYS

    return sorted(set(LIVE_BROKER_SERVER_KEYS) | {BITGET_BROKER_KEY})


def _load_agent_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"agent config must be a JSON object: {path}")
    return data


def _normalize_mcp_servers_key(data: dict[str, Any]) -> str:
    if "mcp_servers" in data:
        return "mcp_servers"
    if "mcpServers" in data:
        return "mcpServers"
    return "mcp_servers"


def merge_bitget_into_agent_json(
    config_path: Path | None = None,
    *,
    include_write_tools: bool = False,
    dry_run: bool = False,
) -> Path:
    """Merge or replace the ``bitget`` MCP server block in operator ``agent.json``.

    Args:
        config_path: Explicit config file; default ``~/.vibe-trading/agent.json``.
        include_write_tools: When True, seed WRITE tools in ``enabled_tools``.
        dry_run: If True, do not write the file.

    Returns:
        The config path that was (or would be) written.
    """
    from src.config.paths import get_config_path

    path = get_config_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    data = _load_agent_json(path)
    mcp_key = _normalize_mcp_servers_key(data)
    servers = data.get(mcp_key)
    if not isinstance(servers, dict):
        servers = {}
    servers[BITGET_BROKER_KEY] = build_bitget_mcp_server_seed(
        include_write_tools=include_write_tools,
    )
    data[mcp_key] = servers

    if not dry_run:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def setup_status(config_path: Path | None = None) -> dict[str, Any]:
    """Summarize Bitget live-channel readiness for CLI / docs."""
    from src.config.paths import get_config_path
    from src.live.mandate.store import load_mandate

    path = get_config_path(config_path)
    configured = False
    write_tools_enabled = False
    if path.exists():
        try:
            data = _load_agent_json(path)
            mcp_key = _normalize_mcp_servers_key(data)
            servers = data.get(mcp_key) or {}
            entry = servers.get(BITGET_BROKER_KEY) if isinstance(servers, dict) else None
            if isinstance(entry, dict):
                configured = True
                tools = entry.get("enabled_tools") or entry.get("enabledTools") or []
                if isinstance(tools, list):
                    write_tools_enabled = "place_order" in tools
        except (OSError, ValueError, json.JSONDecodeError):
            configured = False

    mandate = load_mandate(BITGET_BROKER_KEY)
    return {
        "broker": BITGET_BROKER_KEY,
        "agent_json": str(path),
        "mcp_configured": configured,
        "write_tools_in_config": write_tools_enabled,
        "credentials_in_env": bitget_credentials_configured(),
        "mandate_committed": mandate is not None,
    }
