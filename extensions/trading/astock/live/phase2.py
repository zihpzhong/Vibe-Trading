"""Phase 2 LLM analyzer stub — full 13-dim implementation in P3."""

from __future__ import annotations

import logging
from typing import Any

from extensions.trading.astock.models import AStockPhase2Request, AStockSignal

logger = logging.getLogger(__name__)


class AStockPhase2Analyzer:
    """Pass-through stub: approves fast_track when score >= 7 without LLM."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def analyze(self, request: AStockPhase2Request) -> dict[str, Any]:
        if not self.enabled:
            return {"verdict": "SKIP", "conviction": "LOW", "symbol": request.symbol}
        conviction = "HIGH" if request.score >= 7 else "MEDIUM"
        return {
            "verdict": "APPROVE",
            "conviction": conviction,
            "symbol": request.symbol,
            "tier": request.tier,
            "dims": request.dims,
            "note": "phase2 stub — wire SkillsLoader in P3",
        }

    def enrich_signal(self, signal: AStockSignal, request: AStockPhase2Request) -> AStockSignal:
        result = self.analyze(request)
        if result.get("verdict") == "REJECT":
            signal.score = 0
            signal.note = result.get("note", "phase2 reject")
        return signal
