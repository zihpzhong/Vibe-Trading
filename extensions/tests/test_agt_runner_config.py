"""Tests for AGT live-runner tool allowlist and prompt constraints."""

from __future__ import annotations

from src.agent.tools import BaseTool, ToolRegistry

from extensions.live.agt_runner_config import (
    AGT_LIVE_PROMPT_ADDENDUM,
    append_agt_live_prompt_constraints,
    filter_registry_for_agt_live,
    is_live_runner_session_title,
)


class _StubTool(BaseTool):
    description = "stub"
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs: object) -> str:
        return "ok"


def _register(full: ToolRegistry, name: str) -> None:
    tool = _StubTool()
    tool.name = name
    full.register(tool)


def test_is_live_runner_session_title() -> None:
    assert is_live_runner_session_title("live-runner:bitget") is True
    assert is_live_runner_session_title("chat") is False
    assert is_live_runner_session_title(None) is False


def test_filter_registry_for_agt_live() -> None:
    full = ToolRegistry()
    for name in (
        "load_skill",
        "live_trading",
        "read_url",
        "run_swarm",
        "mcp_bitget_get_account",
        "mcp_other_foo",
    ):
        _register(full, name)

    filtered = filter_registry_for_agt_live(full)
    names = set(filtered.tool_names)
    assert names == {"load_skill", "live_trading", "mcp_bitget_get_account"}


def test_append_agt_live_prompt_constraints_idempotent() -> None:
    base = "mandate tick"
    once = append_agt_live_prompt_constraints(base)
    assert AGT_LIVE_PROMPT_ADDENDUM.strip() in once
    twice = append_agt_live_prompt_constraints(once)
    assert twice.count("AGT LIVE TICK CONSTRAINTS") == 1
