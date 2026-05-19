"""A-share execution gate — 8 checks, LONG only."""

from __future__ import annotations

from typing import Optional

from extensions.live_trading.astock.config import AStockGateConfig, AStockTradingConfig
from extensions.live_trading.astock.exchange import AStockExchangeBase
from extensions.live_trading.astock.models import AStockSignal, ExecutionGateResult, GateStatus


class AStockGateEngine:
    def __init__(self, config: Optional[AStockTradingConfig] = None) -> None:
        self.config = config or AStockTradingConfig()
        self.gate = self.config.gate

    def run_gate(
        self,
        signal: AStockSignal,
        exchange: AStockExchangeBase,
        account_balance: float = 0.0,
        order_shares: int = 0,
        is_st: bool = False,
    ) -> ExecutionGateResult:
        result = ExecutionGateResult(symbol=signal.symbol, status=GateStatus.PASS)
        ticker = exchange.get_ticker(signal.symbol)

        self._check_suspended(result, exchange, signal.symbol)
        self._check_st(result, is_st)
        self._check_limit(result, exchange, signal.symbol)
        self._check_liquidity(result, ticker)
        self._check_risk_reward(result, signal)
        self._check_position_cap(result, signal, ticker, account_balance, order_shares)

        hard = {"suspended", "st_stock", "limit_up", "limit_down", "risk_reward"}
        hard_failed = [c for c in result.failed_checks if c.name in hard]
        soft_failed = [c for c in result.failed_checks if c.name not in hard]

        if hard_failed:
            result.status = GateStatus.REJECT
            result.summary = "REJECTED: " + ", ".join(c.name for c in hard_failed)
        elif soft_failed:
            result.status = GateStatus.WATCH_ONLY
            result.summary = "WATCH_ONLY: " + ", ".join(c.name for c in soft_failed)
        else:
            result.summary = "PASS"
        signal.gate_result = result
        return result

    def _check_suspended(self, result: ExecutionGateResult, exchange: AStockExchangeBase, symbol: str) -> None:
        ok = not exchange.is_suspended(symbol)
        result.add_check("suspended", ok, "停牌" if not ok else "ok")

    def _check_st(self, result: ExecutionGateResult, is_st: bool) -> None:
        result.add_check("st_stock", not is_st, "ST" if is_st else "ok")

    def _check_limit(self, result: ExecutionGateResult, exchange: AStockExchangeBase, symbol: str) -> None:
        lim = exchange.is_limit(symbol)
        result.add_check("limit_up", lim != "up", lim)
        result.add_check("limit_down", lim != "down", lim)

    def _check_liquidity(self, result: ExecutionGateResult, ticker: dict) -> None:
        amount = float(ticker.get("amount", 0) or 0)
        ok = amount >= self.gate.min_daily_amount_cny
        result.add_check(
            "liquidity",
            ok,
            f"成交额 {amount/1e8:.2f}亿 vs 门槛 {self.gate.min_daily_amount_cny/1e8:.2f}亿",
        )

    def _check_risk_reward(self, result: ExecutionGateResult, signal: AStockSignal) -> None:
        if not signal.entry_price or not signal.stop_loss or not signal.take_profit:
            result.add_check("risk_reward", False, "missing prices")
            return
        risk = signal.entry_price - signal.stop_loss
        reward = signal.take_profit - signal.entry_price
        if risk <= 0:
            result.add_check("risk_reward", False, "invalid stop")
            return
        rr = reward / risk
        ok = rr >= self.gate.min_risk_reward_ratio
        result.add_check("risk_reward", ok, f"R:R={rr:.2f}")

    def _check_position_cap(
        self,
        result: ExecutionGateResult,
        signal: AStockSignal,
        ticker: dict,
        balance: float,
        shares: int,
    ) -> None:
        if balance <= 0 or shares <= 0:
            result.add_check("position_cap", True, "skipped")
            return
        last = float(ticker.get("last", signal.entry_price or 0))
        notional = last * shares
        pct = notional / balance * 100
        ok = pct <= self.gate.max_position_pct
        result.add_check("position_cap", ok, f"仓位 {pct:.1f}% vs 上限 {self.gate.max_position_pct}%")
