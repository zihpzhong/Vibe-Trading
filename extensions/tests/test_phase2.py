"""Tests for Phase 2 skill prompt routing."""

from __future__ import annotations

from extensions.trading.crypto.live.phase2 import Phase2Analyzer, format_alpha_context_block
from extensions.trading.crypto.models import Phase2Request


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

    def test_prompt_includes_alpha_context(self) -> None:
        analyzer = Phase2Analyzer()
        req = Phase2Request(
            symbol="SOLUSDT",
            direction="LONG",
            score=6,
            tier="enhanced",
            dims=["dim1"],
            entry_price=100.0,
            rsi_1h=32.0,
            change_24h=-2.0,
        )
        prompt = analyzer._build_prompt(
            req,
            ticker={"last": 101.0, "volume24h": 1_000_000},
            skills={"dim1": "skill text"},
            alpha_context={"alpha_signal": 0.55, "alpha_momentum_5": 0.21},
        )
        assert "## Alpha Factor Signals (Phase 1)" in prompt
        assert "alpha_signal (aggregate): 0.550 (bullish)" in prompt
        assert "alpha_momentum_5: 0.210" in prompt


class TestFormatAlphaContextBlock:
    def test_empty_context(self) -> None:
        assert format_alpha_context_block({}) == ""
        assert format_alpha_context_block(None) == ""
