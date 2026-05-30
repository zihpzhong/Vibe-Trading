"""Smoke tests for the Bitget MCP Live Broker server (mock mode).

Manually handles the MCP initialization handshake (required by protocol
2025-03-26+) using stdio subprocess.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


SERVER_SCRIPT = str(
    Path(__file__).resolve().parents[1] / "live" / "bitget_mcp_server.py"
)


def _mcp_request(requests: list[dict]) -> list[dict]:
    """Send one or more JSON-RPC requests to a fresh MCP server process.

    The server process lives for the full batch of requests, then terminates.
    ``requests[0]`` MUST be the ``initialize`` handshake.

    Returns the response list (one per request, in order).
    """
    payload_lines = [json.dumps(r, ensure_ascii=False) for r in requests]
    payload = "\n".join(payload_lines)

    proc = subprocess.Popen(
        [sys.executable, SERVER_SCRIPT, "--mock"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(input=payload, timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()

    # Parse response lines (one JSON-RPC response per line, in request order).
    responses: list[dict] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict) and "jsonrpc" in parsed:
            responses.append(parsed)

    if not responses:
        pytest.fail(f"No valid JSON-RPC response. stderr: {stderr[:500]}")

    return responses


def _init_and_call(method: str, arguments: dict | None = None, id: int = 1) -> dict:
    """Send initialize handshake followed by one ``tools/call`` request.

    Returns the ``tools/call`` response.
    """
    requests = [
        {
            "jsonrpc": "2.0", "id": 100, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "bitget-mcp-test", "version": "1.0"},
            },
        },
        {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        },
        {
            "jsonrpc": "2.0", "id": id, "method": "tools/call",
            "params": {"name": method, "arguments": arguments or {}},
        },
    ]
    responses = _mcp_request(requests)
    # Return the last response (tools/call).
    return responses[-1] if responses else {}


def _response_text(resp: dict) -> object:
    """Extract the result payload from a ``tools/call`` response.

    FastMCP places structured return values in ``structuredContent.result``
    when the tool returns a dict/list; falls back to text content for
    string-returning tools.
    """
    result = resp.get("result", {})
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and "result" in sc:
        return sc["result"]
    content = result.get("content", [])
    text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return json.loads(text)


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


def test_list_tools() -> None:
    """tools/list returns the 6 expected tools."""
    requests = [
        {
            "jsonrpc": "2.0", "id": 100, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "bitget-mcp-test", "version": "1.0"},
            },
        },
        {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        },
        {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        },
    ]
    responses = _mcp_request(requests)
    resp = responses[-1]
    assert "result" in resp, f"No result in response: {resp}"
    tools = resp["result"].get("tools", [])
    tool_names = {t["name"] for t in tools}
    expected = {"get_account", "get_positions", "get_quotes", "list_orders",
                "place_order", "cancel_order"}
    missing = expected - tool_names
    extra = tool_names - expected
    assert not missing, f"Missing tools: {missing}"
    assert not extra, f"Unexpected tools: {extra}"
    assert len(tools) == 6


# ---------------------------------------------------------------------------
# READ tool smoke tests
# ---------------------------------------------------------------------------


def test_get_account() -> None:
    """get_account returns a balance dict in mock mode."""
    resp = _init_and_call("get_account", id=2)
    assert "result" in resp, f"No result in response: {resp}"
    parsed = _response_text(resp)
    assert isinstance(parsed, dict)
    # Mock exchange returns empty balances (no auth).
    assert "equity" in parsed


def test_get_positions() -> None:
    """get_positions returns a list in mock mode."""
    resp = _init_and_call("get_positions", id=3)
    assert "result" in resp
    assert isinstance(_response_text(resp), list)


def test_get_quotes() -> None:
    """get_quotes returns a ticker for BTCUSDT in mock mode."""
    resp = _init_and_call("get_quotes", {"symbol": "BTCUSDT"}, id=4)
    assert "result" in resp
    parsed = _response_text(resp)
    assert isinstance(parsed, dict)
    assert parsed.get("symbol") == "BTCUSDT"


def test_list_orders() -> None:
    """list_orders returns a list in mock mode."""
    resp = _init_and_call("list_orders", id=5)
    assert "result" in resp
    assert isinstance(_response_text(resp), list)


# ---------------------------------------------------------------------------
# WRITE tool smoke tests (mock mode — no real orders)
# ---------------------------------------------------------------------------


def test_place_order() -> None:
    """place_order returns an order result with an order_id in mock mode."""
    resp = _init_and_call(
        "place_order",
        {"symbol": "BTCUSDT", "side": "buy", "notional_usd": 100.0},
        id=6,
    )
    assert "result" in resp
    parsed = _response_text(resp)
    assert isinstance(parsed, dict)
    # In mock mode, an order has order_id, symbol, side, etc.
    assert "order_id" in parsed or "error" in parsed


def test_cancel_order() -> None:
    """cancel_order returns a cancel result in mock mode."""
    resp = _init_and_call(
        "cancel_order",
        {"order_id": "mock_1", "symbol": "BTCUSDT"},
        id=7,
    )
    assert "result" in resp
    parsed = _response_text(resp)
    assert isinstance(parsed, dict)
    assert "order_id" in parsed or "error" in parsed
