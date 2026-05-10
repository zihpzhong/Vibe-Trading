"""Execution Gate engine.

Implements the verification gate defined in the crypto-entry-analysis SKILL.md.
Each check is an independent method. The engine aggregates results into
an ExecutionGateResult with PASS / WATCH_ONLY / REJECT verdict.
"""

from __future__ import annotations

from typing import Optional

from extensions.live_trading.config import LiveTradingConfig
from extensions.live_trading.models import (
    ExecutionGateResult,
    GateStatus,
    LiveSignal,
    SignalDirection,
)


class ExecGateEngine:
    """Execution Gate engine.

    Usage:
        engine = ExecGateEngine(config)
        signal = LiveSignal(symbol="SOLUSDT", direction="LONG", score=8)
        result = engine.run_gate(signal, ticker_data, funding_rate, orderbook)
    """

    def __init__(self, config: Optional[LiveTradingConfig] = None) -> None:
        self.config = config or LiveTradingConfig()

    def run_gate(
        self,
        signal: LiveSignal,
        ticker: Optional[dict] = None,
        funding_rate: float = 0.0,
        orderbook: Optional[dict] = None,
    ) -> ExecutionGateResult:
        """Run all gate checks against a signal.

        Args:
            signal: The trading signal to evaluate.
            ticker: Ticker dict with keys: last, volume24h, etc.
            funding_rate: Current perpetual funding rate.
            orderbook: Order book dict with bids/asks.

        Returns:
            ExecutionGateResult with aggregated verdict.
        """
        result = ExecutionGateResult(
            symbol=signal.symbol,
            direction=signal.direction,
            status=GateStatus.PASS,
        )

        self._check_liquidity(result, ticker)
        self._check_funding_rate(result, funding_rate)
        self._check_orderbook_impact(result, orderbook, signal)
        self._check_risk_reward(result, signal)
        self._check_position_cap(result)

        failed = result.failed_checks
        if any(c.name == "funding_rate" for c in failed):
            result.status = GateStatus.REJECT
            result.summary = f"REJECTED: Hard block ({failed[0].detail})"
        elif len(failed) >= 2:
            names = [c.name for c in failed]
            result.summary = f"REJECTED: {len(failed)} checks failed: {', '.join(names)}"
            result.status = GateStatus.REJECT
        elif len(failed) == 1:
            result.status = GateStatus.WATCH_ONLY
            result.summary = f"WATCH_ONLY: {failed[0].name} is marginal"
        else:
            result.status = GateStatus.PASS
            result.summary = "PASS: All checks clear"

        return result

    def _check_liquidity(self, result: ExecutionGateResult, ticker: Optional[dict]) -> None:
        """Check 24h volume meets minimum liquidity."""
        if ticker is None:
            result.add_check("liquidity", False, "No ticker data available")
            return
        volume = ticker.get("volume24h", 0) or 0
        min_vol = self.config.execution_gate.min_liquidity_usdt
        if volume >= min_vol:
            result.add_check("liquidity", True, f"24h vol {volume:,.0f} USDT ≥ {min_vol:,.0f}")
        else:
            result.add_check(
                "liquidity", False,
                f"24h vol {volume:,.0f} USDT < {min_vol:,.0f} (min liquidity)",
            )

    def _check_funding_rate(self, result: ExecutionGateResult, funding_rate: float) -> None:
        """Check funding rate doesn't exceed thresholds."""
        cfg = self.config.funding_rate
        direction = result.direction

        if direction == SignalDirection.LONG and funding_rate > cfg.max_long_funding:
            result.add_check(
                "funding_rate", False,
                f"Funding {funding_rate:.4f} > {cfg.max_long_funding:.4f} → no long",
            )
        elif direction == SignalDirection.SHORT and funding_rate < cfg.min_short_funding:
            result.add_check(
                "funding_rate", False,
                f"Funding {funding_rate:.4f} < {cfg.min_short_funding:.4f} → no short",
            )
        else:
            result.add_check(
                "funding_rate", True,
                f"Funding {funding_rate:.4f} within limits",
            )

    def _check_orderbook_impact(
        self, result: ExecutionGateResult, orderbook: Optional[dict],
        signal: LiveSignal,
    ) -> None:
        """Estimate orderbook impact based on a notional trade size."""
        if orderbook is None:
            result.add_check("orderbook_impact", True, "No orderbook data, skipping")
            return

        max_impact = self.config.execution_gate.max_orderbook_impact_pct
        notional = signal.entry_price or 0
        if notional <= 0:
            result.add_check("orderbook_impact", True, "No entry price, skipping")
            return

        if signal.direction == SignalDirection.LONG:
            levels = orderbook.get("asks", [])
        else:
            levels = orderbook.get("bids", [])

        if not levels:
            result.add_check("orderbook_impact", True, "No orderbook depth, skipping")
            return

        # Single-level impact: price impact of trading 1 unit at the top level
        top_price, top_size = float(levels[0][0]), float(levels[0][1])
        trade_size = notional * 0.01  # assume 1% of notional
        if top_size > 0 and trade_size / top_size > 0.5:
            impact_pct = abs(top_price - notional) / notional * 100
            if impact_pct <= max_impact:
                result.add_check(
                    "orderbook_impact", True,
                    f"Impact ~{impact_pct:.2f}% ≤ {max_impact}%",
                )
            else:
                result.add_check(
                    "orderbook_impact", False,
                    f"Impact ~{impact_pct:.2f}% > {max_impact}% (too high)",
                )
        else:
            result.add_check("orderbook_impact", True, "Top level depth sufficient")

    def _check_risk_reward(self, result: ExecutionGateResult, signal: LiveSignal) -> None:
        """Check R:R ratio meets minimum."""
        if signal.entry_price is None or signal.stop_loss is None or not signal.target_prices:
            result.add_check("risk_reward", True, "R:R not calculable, skipping")
            return

        direction = signal.direction
        entry = signal.entry_price
        stop = signal.stop_loss
        target = signal.target_prices[0]

        if direction == SignalDirection.LONG and stop < entry:
            risk = (entry - stop) / entry
            reward = (target - entry) / entry
        elif direction == SignalDirection.SHORT and stop > entry:
            risk = (stop - entry) / entry
            reward = (entry - target) / entry
        else:
            result.add_check("risk_reward", True, "Invalid stop placement, skipping")
            return

        if risk <= 0:
            result.add_check("risk_reward", True, "No calculable risk, skipping")
            return

        rr = reward / risk
        min_rr = self.config.execution_gate.min_risk_reward_ratio

        if rr >= min_rr:
            result.add_check("risk_reward", True, f"R:R {rr:.1f} ≥ {min_rr}:1")
        else:
            result.add_check("risk_reward", False, f"R:R {rr:.1f} < {min_rr}:1 (too low)")

    def _check_position_cap(self, result: ExecutionGateResult) -> None:
        """Record the configured position cap as a memo check.

        Actual portfolio allocation verification is the caller's responsibility.
        """
        max_pct = self.config.execution_gate.max_position_pct
        result.add_check(
            "position_cap", True,
            f"Position cap: {max_pct}% of portfolio",
        )
