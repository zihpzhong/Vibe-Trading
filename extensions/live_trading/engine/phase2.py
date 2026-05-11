"""Phase 2: Agent-driven deep analysis — 8-dimension skill-based signal evaluation.

Executes the full Phase 2 pipeline specified in docs/auto-trading-plan.md:

  1. receive Phase2Request (signal + required dims)
  2. for each dimension: load_skill("skill-name") → skill content
  3. LLM analyzes signal through each skill's framework
  4. output per-dimension verdict + consensus → PASS / NEUTRAL / FAIL

Flow:
  Scheduler.run_once()
    │
    ├─ Phase 1: MarketScanner.scan() → TOP 3 排名
    │
    └─ Phase2Request {dims, tier}
         │
         ▼  ← this module
    Phase 2: load_skill per dim → LLM analysis
         │
         ▼
    ATR → Gate → Position check → Order
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from extensions.live_trading.models import Phase2Request

logger = logging.getLogger(__name__)

# Dimension → skill mapping (per auto-trading-plan.md)
DIM_SKILL_MAP: dict[str, list[str]] = {
    "dim1": ["technical-basic", "candlestick"],
    "dim2": ["onchain-analysis"],
    "dim3": ["perp-funding-basis", "liquidation-heatmap"],
    "dim4": ["sentiment-analysis", "social-media-intelligence"],
    "dim5": ["volatility"],
    "dim6": ["stablecoin-flow", "market-microstructure"],
    "dim7": ["risk-analysis"],
    "dim8": ["correlation-analysis", "sector-rotation"],
}

DIM_LABELS: dict[str, str] = {
    "dim1": "技术面",
    "dim2": "链上数据",
    "dim3": "合约",
    "dim4": "市场情绪",
    "dim5": "波动率",
    "dim6": "稳定币",
    "dim7": "风险评估",
    "dim8": "相关性",
}

class Phase2Analyzer:
    """Phase 2 deep analysis: load skills per dimension → LLM → verdict."""

    def __init__(self) -> None:
        self._llm: Any = None

    def _system_prompt(self, tier: str) -> str:
        """Build tier-aware system prompt with appropriate consensus strictness."""
        base = """You are a professional crypto trading analyst. Analyze each required dimension using the provided skill framework, then output a consensus verdict.

Return valid JSON only:
{
  "dimensions": {
    "dim1": {
      "verdict": "PASS" | "NEUTRAL" | "FAIL",
      "reasoning": "one-line explanation"
    }
  },
  "consensus": "PASS" | "NEUTRAL" | "FAIL",
  "summary": "one-line overall assessment",
  "risk_flag": "key risk or null"
}

Rules:
- PASS = dimension strongly supports the signal direction
- NEUTRAL = inconclusive, mixed signals, or dimension data unavailable
- FAIL = dimension opposes the signal or shows danger
"""
        if tier == "fast_track":
            base += """- consensus = PASS when at least one dimension is PASS and none are FAIL
- consensus = FAIL if any required dim is FAIL
- consensus = NEUTRAL if ALL dimensions are NEUTRAL"""
        else:
            base += """- consensus = PASS when >= 50% of required dims are PASS and none are FAIL
- consensus = FAIL if any required dim is FAIL
- consensus = NEUTRAL otherwise"""
        return base

    def _get_llm(self):
        if self._llm is None:
            from src.providers.chat import ChatLLM
            self._llm = ChatLLM()
        return self._llm

    def _load_skills(self, dims: list[str]) -> dict[str, str]:
        """Load skill content for each dimension — exact per-plan load_skill()."""
        from src.agent.skills import SkillsLoader

        loader = SkillsLoader()
        result: dict[str, str] = {}
        for dim in dims:
            skill_names = DIM_SKILL_MAP.get(dim, [])
            parts: list[str] = []
            for name in skill_names:
                content = loader.get_content(name)
                if content:
                    parts.append(f"=== Skill: {name} ===\n{content}")
            if parts:
                result[dim] = "\n\n".join(parts)
            else:
                logger.warning("Phase2: No skill content found for %s (%s)", dim, skill_names)
        return result

    def analyze(
        self,
        req: Phase2Request,
        ticker: Optional[dict] = None,
    ) -> Optional[dict]:
        """Run Phase 2 on one signal. Returns verdict dict or None on failure."""
        needed = [d for d in req.dims if d in DIM_SKILL_MAP]
        if not needed:
            logger.info("Phase2: No analyzable dims for %s (%s), skipping", req.symbol, req.dims)
            return None

        try:
            skills = self._load_skills(needed)
            if not skills:
                logger.info("Phase2: No skills loaded for %s, skipping", req.symbol)
                return None
            prompt = self._build_prompt(req, ticker or {}, skills)
            llm = self._get_llm()
            response = llm.chat(
                messages=[
                    {"role": "system", "content": self._system_prompt(req.tier)},
                    {"role": "user", "content": prompt},
                ],
                timeout=30,
            )
            return self._parse_response(response.content, req.symbol)
        except Exception as exc:
            logger.warning("Phase2 analysis failed for %s: %s", req.symbol, exc)
            return None

    def _build_prompt(self, req: Phase2Request, ticker: dict, skills: dict[str, str]) -> str:
        dim_list = "\n".join(f"  - {d} ({DIM_LABELS.get(d, d)})" for d in skills.keys())

        skills_block = ""
        for dim, content in skills.items():
            label = DIM_LABELS.get(dim, dim)
            skills_block += f"\n===== {label} ({dim}) =====\n{content}\n"

        return f"""Analyze this trading signal.

## Signal
- Symbol: {req.symbol}
- Direction: {req.direction}
- Score: {req.score}/9
- Tier: {req.tier}
- Entry Price: ${req.entry_price:.4f}
- 24h Change: {req.change_24h:+.2f}%
- RSI(1h): {req.rsi_1h:.1f}
- Current Price: ${ticker.get("last", "N/A")}
- 24h Volume: ${ticker.get("volume24h", ticker.get("volume", "N/A"))}

## Required Dimensions (load_skill per dim)
{dim_list}

## Skill Frameworks
{skills_block}

Analyze EACH dimension. Then output consensus."""

    def _parse_response(self, content: Optional[str], symbol: str) -> Optional[dict]:
        if not content:
            return None
        try:
            text = content.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0]
            elif "```" in text:
                text = text.split("```")[1].split("```")[0]
            result = json.loads(text.strip())
            result["symbol"] = symbol
            return result
        except (json.JSONDecodeError, IndexError) as exc:
            logger.warning("Phase2 JSON parse error for %s: %s", symbol, exc)
            return None
