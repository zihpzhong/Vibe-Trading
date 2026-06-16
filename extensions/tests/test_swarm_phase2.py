"""Tests for Swarm Phase 2 consensus and shadow engine."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import time

from extensions.trading.crypto.live.swarm_phase2 import (
    SWARM_CAUTION,
    SWARM_CONFIRMED,
    SWARM_DANGER,
    SWARM_DEGRADED,
    SWARM_UNCLEAR,
    DimAnalysisResult,
    SwarmDimAnalyzer,
    SwarmPhase2Config,
    SwarmPhase2Engine,
    extract_alpha_context,
    run_consensus,
    swarm_to_phase2_consensus,
)
from extensions.trading.crypto.models import Phase2Request


class TestRunConsensus:
    def test_all_pass_confirmed(self) -> None:
        consensus, _ = run_consensus(
            {"dim1": "PASS", "dim3": "PASS", "dim5": "PASS", "dim6": "PASS", "dim7": "PASS"},
            min_workers=3,
        )
        assert consensus == SWARM_CONFIRMED

    def test_two_fail_danger(self) -> None:
        consensus, reason = run_consensus(
            {"dim1": "FAIL", "dim3": "FAIL", "dim5": "PASS"},
            min_workers=3,
        )
        assert consensus == SWARM_DANGER
        assert "FAIL" in reason

    def test_one_fail_caution(self) -> None:
        consensus, _ = run_consensus(
            {"dim1": "FAIL", "dim3": "PASS", "dim5": "NEUTRAL", "dim6": "NEUTRAL", "dim7": "NEUTRAL"},
            min_workers=3,
        )
        assert consensus == SWARM_CAUTION

    def test_three_pass_rest_neutral_confirmed(self) -> None:
        consensus, _ = run_consensus(
            {"dim1": "PASS", "dim3": "PASS", "dim5": "PASS", "dim6": "NEUTRAL", "dim7": "NEUTRAL"},
            min_workers=3,
        )
        assert consensus == SWARM_CONFIRMED

    def test_all_neutral_unclear(self) -> None:
        consensus, _ = run_consensus(
            {"dim1": "NEUTRAL", "dim3": "NEUTRAL", "dim5": "NEUTRAL"},
            min_workers=3,
        )
        assert consensus == SWARM_UNCLEAR

    def test_insufficient_workers_degraded(self) -> None:
        consensus, reason = run_consensus({"dim1": "PASS"}, min_workers=3)
        assert consensus == SWARM_DEGRADED
        assert "only 1/3" in reason


class TestSwarmMapping:
    def test_swarm_to_phase2(self) -> None:
        assert swarm_to_phase2_consensus(SWARM_CONFIRMED) == "PASS"
        assert swarm_to_phase2_consensus(SWARM_DANGER) == "FAIL"
        assert swarm_to_phase2_consensus(SWARM_CAUTION) == "NEUTRAL"


class TestExtractAlphaContext:
    def test_extracts_alpha_fields(self) -> None:
        ctx = extract_alpha_context(
            {
                "symbol": "SOLUSDT",
                "alpha_signal": 0.55,
                "alpha_momentum_5": 0.2,
                "score": 6,
            }
        )
        assert ctx["alpha_signal"] == 0.55
        assert ctx["alpha_momentum_5"] == 0.2
        assert "score" not in ctx


class TestSwarmPhase2Engine:
    def test_analyze_parallel_with_mock_analyzer(self) -> None:
        mock_analyzer = MagicMock()
        mock_analyzer.analyze_dim.side_effect = [
            DimAnalysisResult(dim="dim1", verdict="PASS"),
            DimAnalysisResult(dim="dim3", verdict="FAIL"),
            DimAnalysisResult(dim="dim5", verdict="PASS"),
            DimAnalysisResult(dim="dim6", verdict="NEUTRAL"),
            DimAnalysisResult(dim="dim7", verdict="NEUTRAL"),
        ]
        engine = SwarmPhase2Engine(
            SwarmPhase2Config(dims=["dim1", "dim3", "dim5", "dim6", "dim7"], consensus_min_workers=3),
            dim_analyzer=mock_analyzer,
        )
        req = Phase2Request(
            symbol="SOLUSDT",
            direction="LONG",
            score=6,
            tier="enhanced",
            dims=["dim1", "dim3", "dim5", "dim6", "dim7"],
            entry_price=100.0,
            rsi_1h=35.0,
            change_24h=-3.0,
        )
        result = engine.analyze_parallel(req, {"last": 101.0})
        assert result["swarm_consensus"] == SWARM_CAUTION
        assert result["dim_verdicts"]["dim3"] == "FAIL"
        assert mock_analyzer.analyze_dim.call_count == 5

    def test_submit_shadow_enhanced_tier_triggers(self) -> None:
        """submit_shadow with enhanced tier must write shadow JSONL."""
        mock_analyzer = MagicMock()
        mock_analyzer.analyze_dim.return_value = DimAnalysisResult(dim="dim1", verdict="PASS")
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "shadow.jsonl"
            engine = SwarmPhase2Engine(
                SwarmPhase2Config(
                    dims=["dim1", "dim3", "dim5"],
                    consensus_min_workers=2,
                    shadow_log_path=log_path,
                ),
                dim_analyzer=mock_analyzer,
            )
            req = Phase2Request(
                symbol="ETHUSDT", direction="SHORT", score=5,
                tier="enhanced", dims=["dim1", "dim3", "dim5"],
            )
            engine.submit_shadow(req, {"last": 3000.0})
            for _ in range(100):
                if log_path.exists() and log_path.stat().st_size > 0:
                    break
                time.sleep(0.05)
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            row = json.loads(lines[0])
            assert row["symbol"] == "ETHUSDT"
            assert "swarm_consensus" in row

    def test_submit_shadow_fast_track_skipped(self) -> None:
        """submit_shadow with non-enhanced tier must be silently skipped."""
        mock_analyzer = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "shadow.jsonl"
            engine = SwarmPhase2Engine(
                SwarmPhase2Config(shadow_log_path=log_path),
                dim_analyzer=mock_analyzer,
            )
            req = Phase2Request(
                symbol="BTCUSDT", direction="LONG", score=7,
                tier="fast_track", dims=["dim1"],
            )
            engine.submit_shadow(req, {"last": 50000.0})
            time.sleep(0.3)
            assert not log_path.exists()
            assert mock_analyzer.analyze_dim.call_count == 0

    def test_shadow_log_append(self) -> None:
        mock_analyzer = MagicMock()
        mock_analyzer.analyze_dim.return_value = DimAnalysisResult(dim="dim1", verdict="PASS")
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "shadow.jsonl"
            engine = SwarmPhase2Engine(
                SwarmPhase2Config(
                    dims=["dim1", "dim3", "dim5"],
                    consensus_min_workers=2,
                    shadow_log_path=log_path,
                ),
                dim_analyzer=mock_analyzer,
            )
            req = Phase2Request(
                symbol="ETHUSDT",
                direction="SHORT",
                score=5,
                tier="enhanced",
                dims=["dim1", "dim3", "dim5"],
            )
            engine._append_shadow_log(
                req,
                engine.analyze_parallel(req, {"last": 3000.0}),
                mono_result={"consensus": "PASS", "dimensions": {"dim1": {"verdict": "PASS"}}},
            )
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            assert len(lines) == 1
            row = json.loads(lines[0])
            assert row["symbol"] == "ETHUSDT"
            assert row["mono_consensus"] == "PASS"
            assert "swarm_consensus" in row
            assert "diverged" in row


class TestSwarmDimAnalyzer:
    def test_no_skill_returns_neutral(self) -> None:
        analyzer = SwarmDimAnalyzer(llm_factory=lambda: MagicMock())
        analyzer._load_dim_skill = lambda dim: ""  # type: ignore[method-assign]
        req = Phase2Request(symbol="X", direction="LONG", score=5, tier="enhanced", dims=["dim1"])
        result = analyzer.analyze_dim("dim1", req, {"last": 1.0})
        assert result.verdict == "NEUTRAL"
