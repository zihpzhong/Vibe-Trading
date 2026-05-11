"""Tests for Phase 2 skill prompt routing."""

from __future__ import annotations

from extensions.live_trading.engine.phase2 import Phase2Analyzer
from extensions.live_trading.models import Phase2Request


class TestPhase2Prompt:
    def test_prompt_uses_volume24h_before_legacy_volume(self) -> None:
        analyzer = Phase2Analyzer()
        req = Phase2Request(
            symbol="SOLUSDT",
            direction="LONG",
            score=7,
            tier="fast_track",
            dims=["dim1"],
            entry_price=100.0,
            change_24h=-4.0,
            rsi_1h=31.0,
        )

        prompt = analyzer._build_prompt(
            req,
            ticker={"last": 101.0, "volume24h": 12_345_678, "volume": 1},
            skills={"dim1": "skill text"},
        )

        assert "- 24h Volume: $12345678\n" in prompt

    def test_prompt_falls_back_to_legacy_volume(self) -> None:
        analyzer = Phase2Analyzer()
        req = Phase2Request(symbol="SOLUSDT", direction="LONG", score=7, tier="fast_track", dims=["dim1"])

        prompt = analyzer._build_prompt(req, ticker={"last": 101.0, "volume": 999}, skills={"dim1": "skill text"})

        assert "24h Volume: $999" in prompt
