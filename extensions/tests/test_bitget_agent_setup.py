"""Tests for Bitget agent.json setup helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from extensions.live.bitget_agent_setup import (
    bitget_credentials_configured,
    merge_bitget_into_agent_json,
    setup_status,
)
from extensions.live.bitget_mcp_seed import BITGET_BROKER_KEY, build_bitget_mcp_server_seed


def test_build_seed_is_stdio_without_auth() -> None:
    seed = build_bitget_mcp_server_seed()
    assert seed["type"] == "stdio"
    assert "auth" not in seed
    assert seed["enabled_tools"] == list(build_bitget_mcp_server_seed()["enabled_tools"])


def test_merge_bitget_writes_agent_json(tmp_path: Path) -> None:
    cfg = tmp_path / "agent.json"
    path = merge_bitget_into_agent_json(cfg)
    assert path == cfg
    data = json.loads(cfg.read_text(encoding="utf-8"))
    entry = data["mcp_servers"][BITGET_BROKER_KEY]
    assert entry["type"] == "stdio"
    assert "place_order" not in entry["enabled_tools"]


def test_merge_bitget_with_write_tools(tmp_path: Path) -> None:
    cfg = tmp_path / "agent.json"
    merge_bitget_into_agent_json(cfg, include_write_tools=True)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    tools = data["mcp_servers"][BITGET_BROKER_KEY]["enabled_tools"]
    assert "place_order" in tools
    assert "cancel_order" in tools


def test_setup_status_reflects_mandate(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.live.paths as paths

    monkeypatch.setattr(paths, "get_runtime_root", lambda: tmp_path)
    cfg = tmp_path / "agent.json"
    merge_bitget_into_agent_json(cfg)
    monkeypatch.delenv("BITGET_API_KEY", raising=False)
    status = setup_status(cfg)
    assert status["mcp_configured"] is True
    assert status["credentials_in_env"] is False
    assert status["mandate_committed"] is False


def test_bitget_credentials_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITGET_API_KEY", "k")
    monkeypatch.setenv("BITGET_SECRET", "s")
    monkeypatch.setenv("BITGET_PASSPHRASE", "p")
    assert bitget_credentials_configured() is True
