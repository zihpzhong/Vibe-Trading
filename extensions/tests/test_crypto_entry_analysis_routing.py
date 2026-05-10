"""Regression tests for crypto entry analysis routing instructions."""

from __future__ import annotations

from src.agent.context import ContextBuilder
from src.agent.memory import WorkspaceMemory
from src.agent.tools import ToolRegistry


class TestCryptoEntryAnalysisRouting:
    def test_system_prompt_routes_crypto_entry_requests_to_skill_first(self) -> None:
        builder = ContextBuilder(ToolRegistry(), WorkspaceMemory())

        prompt = builder.build_system_prompt()

        assert 'load_skill("crypto-entry-analysis")' in prompt
        assert "scan_only" in prompt
        assert "confirm" in prompt
        assert "full_review" in prompt
        assert "execution gate" in prompt.lower()
