"""Tests for the Bitget live-broker bridge.

Verifies that after the bridge patches upstream data structures, the
live-broker pipeline (detection / classification / extraction) works
for the ``"bitget"`` broker without modifying upstream source files.

Each test explicitly calls ``patch_upstream()`` to isolate from any
prior bridge state.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.live.classification import ToolClass
from src.live.extractors import get_extractor
from src.live.halt import halt_flag_set
from src.live import registry as _registry
from src.tools.mcp import MCPRemoteTool, MCPRemoteToolSpec

from extensions.live.bitget_bridge import patch_upstream

# is_live_broker and wrap_live_broker_tools are accessed via _registry
# (not imported directly) because patch_upstream() replaces them on the
# module object, and a direct import would retain the pre-patch reference.
is_live_broker = lambda *a, **kw: _registry.is_live_broker(*a, **kw)  # noqa: E731
wrap_live_broker_tools = lambda *a, **kw: _registry.wrap_live_broker_tools(*a, **kw)  # noqa: E731


# ---------------------------------------------------------------------------
# Bridge patching
# ---------------------------------------------------------------------------


def _make_stub_tool(server_name: str, remote_name: str) -> MCPRemoteTool:
    """Build a lightweight MCPRemoteTool stub with a real spec."""
    fake_adapter = MagicMock()
    fake_adapter.server_name = server_name
    spec = MCPRemoteToolSpec(
        server_name=server_name,
        remote_name=remote_name,
        local_name=f"mcp_{server_name}_{remote_name}",
        description=f"Remote {remote_name}",
        parameters={"type": "object", "properties": {}, "required": []},
    )
    return MCPRemoteTool(fake_adapter, spec)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_bridge_makes_is_live_broker_return_true() -> None:
    """After patching, is_live_broker(\"bitget\") returns True."""
    patch_upstream()
    assert is_live_broker("bitget") is True


# ---------------------------------------------------------------------------
# Classification map
# ---------------------------------------------------------------------------


def test_bridge_registers_curated_map() -> None:
    """After patching, the curated map has bitget entries."""
    patch_upstream()
    from src.live import registry as _reg

    bitget_map = _reg._BROKER_CURATED_MAPS.get("bitget")
    assert bitget_map is not None
    assert bitget_map["place_order"] is ToolClass.WRITE
    assert bitget_map["get_account"] is ToolClass.READ


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------


def test_bridge_registers_extractor() -> None:
    """After patching, get_extractor(\"bitget\") returns a callable."""
    patch_upstream()
    extractor = get_extractor("bitget")
    assert extractor is not None
    assert callable(extractor)


def test_bitget_extractor_is_distinct_from_robinhood() -> None:
    """Bitget extractor is not the same object as the Robinhood one."""
    patch_upstream()
    bitget_ext = get_extractor("bitget")
    rh_ext = get_extractor("robinhood")
    assert bitget_ext is not rh_ext


# ---------------------------------------------------------------------------
# Tool wrapping
# ---------------------------------------------------------------------------


def test_bridge_wrap_live_broker_tools_wraps_write_gates() -> None:
    """WRITE tools for bitget are wrapped (not plain MCPRemoteTool)."""
    patch_upstream()
    from src.live.order_guard import LiveOrderGuardTool

    all_tools = [
        _make_stub_tool("bitget", "get_account"),
        _make_stub_tool("bitget", "get_positions"),
        _make_stub_tool("bitget", "get_quotes"),
        _make_stub_tool("bitget", "list_orders"),
        _make_stub_tool("bitget", "place_order"),
        _make_stub_tool("bitget", "cancel_order"),
    ]
    wrapped = wrap_live_broker_tools("bitget", all_tools)
    assert len(wrapped) == 6

    # READ tools stay as plain MCPRemoteTool.
    assert not isinstance(wrapped[0], LiveOrderGuardTool)  # get_account
    assert isinstance(wrapped[4], LiveOrderGuardTool) is not False  # place_order IS wrapped
    assert isinstance(wrapped[4], LiveOrderGuardTool)  # place_order
    assert isinstance(wrapped[5], LiveOrderGuardTool)  # cancel_order


@patch("src.live.registry.halt_flag_set", return_value=True)
def test_bridge_halt_omits_write_tools(_mock_halt) -> None:
    """When halt is tripped for bitget, WRITE tools are omitted."""
    patch_upstream()
    all_tools = [
        _make_stub_tool("bitget", "get_account"),
        _make_stub_tool("bitget", "place_order"),
        _make_stub_tool("bitget", "cancel_order"),
    ]
    wrapped = wrap_live_broker_tools("bitget", all_tools)
    # get_account stays, place_order and cancel_order are omitted.
    assert len(wrapped) == 1
    assert wrapped[0].name == "mcp_bitget_get_account"


# ---------------------------------------------------------------------------
# Halt namespace
# ---------------------------------------------------------------------------


def test_bridge_halt_flag_path(tmp_path, monkeypatch) -> None:
    """halt_flag_set(\"bitget\") checks a file under the bitget namespace."""
    import src.live.paths as paths

    monkeypatch.setattr(paths, "get_runtime_root", lambda: tmp_path)
    # No halt file → not halted.
    assert halt_flag_set("bitget") is False
    # Touch the halt sentinel.
    (tmp_path / "live" / "bitget").mkdir(parents=True, exist_ok=True)
    (tmp_path / "live" / "bitget" / "HALT").write_text("")
    assert halt_flag_set("bitget") is True


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_bridge_patch_is_idempotent() -> None:
    """Calling patch_upstream() twice is safe."""
    patch_upstream()
    patch_upstream()  # should not raise
    assert is_live_broker("bitget") is True


def test_bridge_patches_schema_is_live_broker_entry() -> None:
    """Config validation treats bitget as a live broker (wildcard rejection)."""
    patch_upstream()
    from src.config import schema as _schema

    class _FakeServer:
        url = ""
        enabled_tools = ["get_account"]

    assert _schema.is_live_broker_entry("bitget", _FakeServer()) is True


def test_bridge_propagates_crypto_asset_classes_in_mandate() -> None:
    """Crypto ceilings produce profiles and universe with asset_classes=['crypto']."""
    import json

    patch_upstream()
    from src.live.mandate.commit import _profile_to_universe
    from src.tools.propose_mandate_tool import ProposeMandateProfilesTool

    raw = ProposeMandateProfilesTool().execute(
        broker="bitget",
        intent="crypto perp test",
        ceilings={
            "account_funding_usd": 45.0,
            "max_order_usd": 6.0,
            "daily_trade_cap": 5,
            "leverage": 10,
            "instruments": ["crypto"],
            "asset_classes": ["crypto"],
        },
    )
    proposal = json.loads(raw)
    profile = next(p for p in proposal["profiles"] if int(p["ordinal"]) == 2)
    assert profile.get("asset_classes") == ["crypto"]
    universe = _profile_to_universe(profile)
    assert universe["asset_classes"] == ["crypto"]


def test_bridge_rejects_wildcard_for_bitget_in_agent_config() -> None:
    """AgentConfig rejects enabled_tools ['*'] on bitget MCP entry."""
    patch_upstream()
    from src.config.schema import AgentConfig

    with pytest.raises(ValueError, match="wildcard"):
        AgentConfig.model_validate(
            {
                "mcp_servers": {
                    "bitget": {
                        "type": "stdio",
                        "command": "python",
                        "args": ["extensions/live/bitget_mcp_server.py"],
                        "enabled_tools": ["*"],
                    }
                }
            }
        )
