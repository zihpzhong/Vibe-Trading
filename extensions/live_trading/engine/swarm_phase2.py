"""Swarm-enhanced Phase 2 — parallel per-dimension analysis (P0: shadow + consensus).

Phase 0 delivers:
  - Pure-code consensus rules (no LLM consensus worker)
  - Background shadow runs for enhanced tier (no trade impact)
  - JSONL audit log under ~/.vibe-trading/logs/swarm_shadow.jsonl
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from extensions.live_trading.engine.phase2 import DIM_LABELS, DIM_SKILL_MAP
from extensions.live_trading.engine.scheduler import FAST_TRACK_DIMS
from extensions.live_trading.models import Phase2Request

logger = logging.getLogger(__name__)

_DEFAULT_LOG = Path.home() / ".vibe-trading" / "logs" / "swarm_shadow.jsonl"

_VERDICT_RE = re.compile(r"^(PASS|NEUTRAL|FAIL)$", re.IGNORECASE)

# Swarm consensus labels (distinct from mono Phase 2 PASS/NEUTRAL/FAIL)
SWARM_CONFIRMED = "CONFIRMED"
SWARM_CAUTION = "CAUTION"
SWARM_DANGER = "DANGER"
SWARM_UNCLEAR = "UNCLEAR"
SWARM_DEGRADED = "DEGRADED"

DEFAULT_SWARM_DIMS = list(FAST_TRACK_DIMS)


@dataclass
class SwarmPhase2Config:
    """Swarm Phase 2 configuration (shadow / future live modes)."""

    enabled: bool = False
    shadow_only: bool = True
    dims: list[str] = field(default_factory=lambda: list(DEFAULT_SWARM_DIMS))
    worker_timeout: float = 60.0
    worker_max_iterations: int = 1
    result_ttl_seconds: float = 300.0
    consensus_min_workers: int = 3
    max_parallel_workers: int = 5
    shadow_log_path: Path = field(default_factory=lambda: _DEFAULT_LOG)


@dataclass
class DimAnalysisResult:
    """Single-dimension worker output."""

    dim: str
    verdict: str  # PASS | NEUTRAL | FAIL | ERROR
    confidence: float = 0.0
    reasoning: str = ""
    suggested_action: str = ""
    error: str | None = None


def normalize_verdict(raw: str) -> str:
    """Normalize verdict string to PASS/NEUTRAL/FAIL or empty."""
    v = (raw or "").strip().upper()
    if _VERDICT_RE.match(v):
        return v
    return ""


def run_consensus(
    dim_verdicts: dict[str, str],
    *,
    min_workers: int = 3,
) -> tuple[str, str]:
    """Aggregate per-dim PASS/NEUTRAL/FAIL into Swarm consensus.

    Returns:
        (consensus_label, human-readable reason)
    """
    valid = {d: normalize_verdict(v) for d, v in dim_verdicts.items()}
    valid = {d: v for d, v in valid.items() if v}

    if len(valid) < min_workers:
        return SWARM_DEGRADED, f"only {len(valid)}/{min_workers} workers succeeded"

    fails = [d for d, v in valid.items() if v == "FAIL"]
    passes = [d for d, v in valid.items() if v == "PASS"]
    neutrals = [d for d, v in valid.items() if v == "NEUTRAL"]

    if len(fails) >= 2:
        return SWARM_DANGER, f"{len(fails)} FAIL dims: {','.join(fails)}"
    if len(fails) == 1 and len(passes) >= 3:
        return SWARM_CONFIRMED, f"1 FAIL ({fails[0]}) among {len(passes)} PASS"
    if len(fails) == 1 and len(passes) >= 1 and not neutrals:
        return SWARM_CAUTION, f"1 FAIL ({fails[0]}) with {len(passes)} PASS"
    if len(fails) == 1:
        return SWARM_CAUTION, f"1 FAIL ({fails[0]})"
    if len(passes) == len(valid):
        return SWARM_CONFIRMED, f"all {len(passes)} dims PASS"
    if len(passes) >= 3 and not fails:
        return SWARM_CONFIRMED, f"{len(passes)} PASS, {len(neutrals)} NEUTRAL"
    if len(neutrals) == len(valid):
        return SWARM_UNCLEAR, "all dims NEUTRAL"
    return SWARM_UNCLEAR, f"{len(passes)} PASS, {len(neutrals)} NEUTRAL, {len(fails)} FAIL"


def swarm_to_phase2_consensus(swarm_consensus: str) -> str:
    """Map Swarm consensus to mono Phase 2 vocabulary for comparison."""
    return {
        SWARM_CONFIRMED: "PASS",
        SWARM_CAUTION: "NEUTRAL",
        SWARM_DANGER: "FAIL",
        SWARM_UNCLEAR: "NEUTRAL",
        SWARM_DEGRADED: "NEUTRAL",
    }.get(swarm_consensus.upper(), "NEUTRAL")


def extract_alpha_context(ranking: dict[str, Any]) -> dict[str, Any]:
    """Pull alpha fields from a Phase 1 ranking row."""
    if not ranking:
        return {}
    ctx: dict[str, Any] = {}
    if "alpha_signal" in ranking:
        ctx["alpha_signal"] = ranking["alpha_signal"]
    for key, value in ranking.items():
        if key.startswith("alpha_") and key != "alpha_signal":
            ctx[key] = value
    return ctx


class SwarmDimAnalyzer:
    """Lightweight single-dimension LLM call (no full SwarmRuntime)."""

    def __init__(self, llm_factory: Callable[[], Any] | None = None) -> None:
        self._llm_factory = llm_factory
        self._llm: Any = None

    def _get_llm(self) -> Any:
        if self._llm is None:
            if self._llm_factory is not None:
                self._llm = self._llm_factory()
            else:
                from src.providers.chat import ChatLLM

                self._llm = ChatLLM()
        return self._llm

    def _load_dim_skill(self, dim: str) -> str:
        from src.agent.skills import SkillsLoader

        loader = SkillsLoader()
        parts: list[str] = []
        for name in DIM_SKILL_MAP.get(dim, []):
            content = loader.get_content(name)
            if content:
                parts.append(f"=== Skill: {name} ===\n{content}")
        return "\n\n".join(parts)

    def analyze_dim(
        self,
        dim: str,
        req: Phase2Request,
        ticker: dict,
        *,
        funding_rate: float | None = None,
        orderbook: dict | None = None,
        btc_1h_trend: str = "NEUTRAL",
        timeout: float = 60.0,
    ) -> DimAnalysisResult:
        """Run one dimension analysis. Returns ERROR verdict on failure."""
        label = DIM_LABELS.get(dim, dim)
        skill_content = self._load_dim_skill(dim)
        if not skill_content:
            return DimAnalysisResult(
                dim=dim,
                verdict="NEUTRAL",
                reasoning="no skill content",
            )

        fr_line = f"- Funding Rate: {funding_rate:.6f}" if funding_rate is not None else "- Funding Rate: N/A"
        prompt = f"""You are a {label} analyst. Analyze this trading signal from your dimension only.

Signal:
- Symbol: {req.symbol}
- Direction: {req.direction}
- Score: {req.score}
- Entry: ${req.entry_price:.4f}
- RSI(1h): {req.rsi_1h:.1f}
- 24h Change: {req.change_24h:+.2f}%
- Current Price: ${ticker.get("last", "N/A")}
- BTC 1h Trend: {btc_1h_trend}
{fr_line}

{skill_content}

Return JSON only:
{{
  "verdict": "PASS" | "NEUTRAL" | "FAIL",
  "confidence": 0.0-1.0,
  "reasoning": "one-line explanation",
  "key_level": "key price level or null",
  "suggested_action": "proceed" | "reduce_size" | "tighten_sl" | "abort"
}}"""
        system = (
            f"You are a crypto {label} specialist. Output valid JSON only. "
            "PASS supports the signal direction; FAIL opposes or shows danger; NEUTRAL if inconclusive."
        )
        try:
            response = self._get_llm().chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                timeout=timeout,
            )
            parsed = self._parse_json(response.content)
            if not parsed:
                return DimAnalysisResult(dim=dim, verdict="ERROR", error="json parse failed")
            verdict = normalize_verdict(str(parsed.get("verdict", "")))
            if not verdict:
                return DimAnalysisResult(dim=dim, verdict="ERROR", error="missing verdict")
            return DimAnalysisResult(
                dim=dim,
                verdict=verdict,
                confidence=float(parsed.get("confidence", 0) or 0),
                reasoning=str(parsed.get("reasoning", ""))[:200],
                suggested_action=str(parsed.get("suggested_action", "")),
            )
        except Exception as exc:
            logger.warning("Swarm dim %s failed for %s: %s", dim, req.symbol, exc)
            return DimAnalysisResult(dim=dim, verdict="ERROR", error=str(exc)[:200])

    @staticmethod
    def _parse_json(content: str | None) -> dict | None:
        if not content:
            return None
        text = content.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            return None


class SwarmPhase2Engine:
    """Parallel dim analysis + code consensus. P0: shadow mode only."""

    def __init__(
        self,
        config: SwarmPhase2Config | None = None,
        dim_analyzer: SwarmDimAnalyzer | None = None,
    ) -> None:
        self.config = config or SwarmPhase2Config()
        self._dim_analyzer = dim_analyzer or SwarmDimAnalyzer()
        self._log_path = Path(self.config.shadow_log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def analyze_parallel(
        self,
        req: Phase2Request,
        ticker: dict,
        *,
        funding_rate: float | None = None,
        orderbook: dict | None = None,
        btc_1h_trend: str = "NEUTRAL",
        dims: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run all dim workers in parallel and return consensus package."""
        target_dims = dims or self.config.dims
        results: dict[str, DimAnalysisResult] = {}

        def _run_one(dim: str) -> DimAnalysisResult:
            return self._dim_analyzer.analyze_dim(
                dim,
                req,
                ticker,
                funding_rate=funding_rate,
                orderbook=orderbook,
                btc_1h_trend=btc_1h_trend,
                timeout=self.config.worker_timeout,
            )

        max_workers = min(self.config.max_parallel_workers, len(target_dims))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_one, dim): dim for dim in target_dims}
            for fut in as_completed(futures):
                dim = futures[fut]
                try:
                    results[dim] = fut.result()
                except Exception as exc:
                    results[dim] = DimAnalysisResult(dim=dim, verdict="ERROR", error=str(exc)[:200])

        dim_verdicts = {
            d: r.verdict for d, r in results.items() if r.verdict in ("PASS", "NEUTRAL", "FAIL")
        }
        consensus, reason = run_consensus(
            dim_verdicts,
            min_workers=self.config.consensus_min_workers,
        )
        return {
            "symbol": req.symbol,
            "tier": req.tier,
            "score": req.score,
            "direction": req.direction,
            "swarm_consensus": consensus,
            "swarm_reason": reason,
            "phase2_equivalent": swarm_to_phase2_consensus(consensus),
            "dims": {
                d: {
                    "verdict": r.verdict,
                    "confidence": r.confidence,
                    "reasoning": r.reasoning,
                    "suggested_action": r.suggested_action,
                    "error": r.error,
                }
                for d, r in results.items()
            },
            "dim_verdicts": dim_verdicts,
            "worker_success_count": len(dim_verdicts),
        }

    def submit_shadow(
        self,
        req: Phase2Request,
        ticker: dict,
        *,
        funding_rate: float | None = None,
        orderbook: dict | None = None,
        btc_1h_trend: str = "NEUTRAL",
        mono_result: dict | None = None,
    ) -> None:
        """Fire-and-forget background shadow analysis (does not block caller)."""
        if req.tier != "enhanced":
            return

        def _worker() -> None:
            try:
                swarm = self.analyze_parallel(
                    req,
                    ticker,
                    funding_rate=funding_rate,
                    orderbook=orderbook,
                    btc_1h_trend=btc_1h_trend,
                    dims=list(req.dims),
                )
                self._append_shadow_log(req, swarm, mono_result=mono_result)
            except Exception:
                logger.exception("Swarm shadow failed for %s", req.symbol)

        thread = threading.Thread(
            target=_worker,
            name=f"swarm-shadow-{req.symbol}",
            daemon=True,
        )
        thread.start()

    def _append_shadow_log(
        self,
        req: Phase2Request,
        swarm: dict[str, Any],
        *,
        mono_result: dict | None = None,
    ) -> None:
        mono_consensus = (mono_result or {}).get("consensus", "N/A")
        mono_dims = {
            d: (mono_result or {}).get("dimensions", {}).get(d, {}).get("verdict", "?")
            for d in req.dims
        }
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": req.symbol,
            "direction": req.direction,
            "score": req.score,
            "tier": req.tier,
            "mono_consensus": mono_consensus,
            "mono_dims": mono_dims,
            "swarm_consensus": swarm.get("swarm_consensus"),
            "swarm_reason": swarm.get("swarm_reason"),
            "phase2_equivalent": swarm.get("phase2_equivalent"),
            "swarm_dims": swarm.get("dim_verdicts"),
            "swarm_detail": swarm.get("dims"),
            "worker_success_count": swarm.get("worker_success_count"),
            "diverged": mono_consensus != swarm.get("phase2_equivalent"),
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        logger.info(
            "Swarm shadow %s: mono=%s swarm=%s diverged=%s",
            req.symbol,
            mono_consensus,
            swarm.get("swarm_consensus"),
            record["diverged"],
        )
