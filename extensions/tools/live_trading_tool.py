"""Live Trading tool for the ReAct Agent.

Wraps Execution Gate, ATR stop, exchange data, trading scheduler,
position tracker, and TPSL monitor into BaseTool for automatic
registration via ext_bridge.py.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Optional

from src.agent.tools import BaseTool

from extensions.live_trading.config import LiveTradingConfig
from extensions.live_trading.models import LiveSignal, SignalDirection
from src.live_trading.execution_gate import ExecGateEngine
from src.live_trading.atr_stop import calculate_atr_stop
from src.live_trading.exchange import create_exchange
from src.live_trading.scheduler import TradingScheduler
from src.live_trading.position_tracker import PositionTracker
from src.live_trading.tpsl_monitor import TPSLMonitor


class LiveTradingTool(BaseTool):
    """实盘交易执行门禁、ATR 止损、自动调度、持仓管理与 TP/SL 守护."""

    name = "live_trading"
    description = (
        "Live trading execution gate, ATR stop calculation, automated trading scheduler, "
        "position management, and TP/SL monitoring. Supports 8 actions: run_gate (7 checks: "
        "PASS/WATCH_ONLY/REJECT), calculate_stop (ATR stop price), run_once (full scheduler "
        "cycle: BTC check, scan, tiered decisions), get_positions (list active positions), "
        "close_position (close a position), get_account (account info), start_monitor (TP/SL "
        "monitor background thread), stop_monitor (stop TP/SL monitor)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "run_gate",
                    "calculate_stop",
                    "run_once",
                    "get_positions",
                    "close_position",
                    "get_account",
                    "start_monitor",
                    "stop_monitor",
                ],
                "description": (
                    "Action to perform: "
                    "run_gate (execute all 7 gate checks -> PASS/WATCH_ONLY/REJECT), "
                    "calculate_stop (ATR-based stop price), "
                    "run_once (execute automated scheduler cycle: BTC conduction -> Phase 1 scan -> tiered decisions), "
                    "get_positions (list all active positions), "
                    "close_position (close a position by symbol), "
                    "get_account (get account balance, active count, exposure), "
                    "start_monitor (start background TP/SL monitoring thread), "
                    "stop_monitor (stop background TP/SL monitoring thread)"
                ),
            },
            "symbol": {
                "type": "string",
                "description": "Trading symbol, e.g. BTCUSDT",
            },
            "direction": {
                "type": "string",
                "enum": ["LONG", "SHORT"],
                "description": "Trade direction",
            },
            "score": {
                "type": "integer",
                "description": "Signal score from Phase 1 (0-10)",
            },
            "entry_price": {
                "type": "number",
                "description": "Entry price (required for calculate_stop, optional for run_gate)",
            },
            "stop_loss": {
                "type": "number",
                "description": "Optional pre-defined stop loss",
            },
            "target_price": {
                "type": "number",
                "description": "Optional first target price for R:R calculation",
            },
            "funding_rate": {
                "type": "number",
                "description": "Current funding rate (decimal)",
            },
            "mode": {
                "type": "string",
                "enum": ["default", "conservative"],
                "description": "Risk mode for ATR multiplier",
            },
        "mock": {
                "type": "boolean",
                "description": "Use mock exchange (default: true). Set to false for real trading.",
            },
        },
        "required": ["action", "symbol", "direction"],
    }
    is_readonly = False

    def __init__(self, mock: bool = True) -> None:
        self._mock = mock
        self._exchange: Optional[Any] = None
        self._positions: Optional[PositionTracker] = None
        self._monitor: Optional[TPSLMonitor] = None

    def _get_exchange(self):
        """Lazy-init exchange instance (RealExchange or MockExchange based on _mock flag)."""
        if self._exchange is None:
            self._exchange = create_exchange(mock=self._mock)
        return self._exchange

    @classmethod
    def check_available(cls) -> bool:
        return True

    def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        symbol = kwargs.get("symbol", "")

        if action == "run_gate":
            direction = SignalDirection(kwargs["direction"])
            return self._run_gate(symbol, direction, kwargs)
        elif action == "calculate_stop":
            direction = SignalDirection(kwargs["direction"])
            return self._calc_stop(symbol, direction, kwargs)
        elif action == "run_once":
            return self._run_once()
        elif action == "get_positions":
            return self._get_positions_action()
        elif action == "close_position":
            return self._close_position(symbol)
        elif action == "get_account":
            return self._get_account()
        elif action == "start_monitor":
            return self._start_monitor()
        elif action == "stop_monitor":
            return self._stop_monitor()
        else:
            return json.dumps({"status": "error", "message": f"Unknown action: {action}"})

    def _run_gate(self, symbol: str, direction: SignalDirection, kwargs: dict) -> str:
        score = kwargs.get("score", 5)
        entry_price = kwargs.get("entry_price")
        stop_loss = kwargs.get("stop_loss")
        target_price = kwargs.get("target_price")
        funding_rate = kwargs.get("funding_rate", 0.0)

        signal = LiveSignal(
            symbol=symbol,
            direction=direction,
            score=score,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target_prices=[target_price] if target_price else [],
            conviction="MEDIUM" if score >= 5 else "LOW",
        )

        # Use real or mock exchange for data
        exchange = self._get_exchange()
        ticker = exchange.get_ticker(symbol)
        orderbook = exchange.get_orderbook(symbol)
        mode = kwargs.get("mode", "default")
        config = LiveTradingConfig.conservative() if mode == "conservative" else LiveTradingConfig()

        engine = ExecGateEngine(config)
        result = engine.run_gate(signal, ticker=ticker, funding_rate=funding_rate, orderbook=orderbook)

        return json.dumps({
            "status": result.status.value,
            "summary": result.summary,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in result.checks
            ],
            "passed": len(result.passed_checks),
            "failed": len(result.failed_checks),
        }, ensure_ascii=False)

    def _calc_stop(self, symbol: str, direction: SignalDirection, kwargs: dict) -> str:
        entry_price = kwargs.get("entry_price")
        if not entry_price:
            return json.dumps({"status": "error", "message": "entry_price required"})

        exchange = self._get_exchange()
        kline = exchange.get_kline(symbol, "1h", 50)
        conservative = kwargs.get("mode") == "conservative"

        stop_price, atr_value = calculate_atr_stop(kline, direction, entry_price, conservative=conservative)

        result = {
            "stop_price": stop_price,
            "atr_value": atr_value,
            "distance_pct": round(abs(entry_price - stop_price) / entry_price * 100, 2),
        }
        return json.dumps(result, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Lazy-init helpers
    # ------------------------------------------------------------------

    def _get_positions(self) -> PositionTracker:
        """Lazy-init PositionTracker singleton."""
        if self._positions is None:
            self._positions = PositionTracker()
        return self._positions

    # ------------------------------------------------------------------
    # New actions: automated scheduler
    # ------------------------------------------------------------------

    def _run_once(self) -> str:
        """Execute one full scheduler cycle: BTC check, Phase 1 scan, tiered decisions."""
        exchange = self._get_exchange()
        positions = self._get_positions()
        scheduler = TradingScheduler(exchange, positions, trading_enabled=True)
        report = scheduler.run_once()
        return json.dumps({
            "rankings": report.rankings,
            "phase2_requests": [dataclasses.asdict(r) for r in report.phase2_requests],
            "watchlist": report.watchlist,
            "btc_status": report.btc_status,
            "active_positions": report.active_positions,
            "scan_time_ms": report.scan_time_ms,
        }, ensure_ascii=False)

    # ------------------------------------------------------------------
    # New actions: position management
    # ------------------------------------------------------------------

    def _get_positions_action(self) -> str:
        """Return JSON list of active positions."""
        positions = self._get_positions().get_active_positions()
        return json.dumps([p.to_dict() for p in positions], ensure_ascii=False)

    def _close_position(self, symbol: str) -> str:
        """Close a position by symbol."""
        if not symbol:
            return json.dumps({"closed": None, "success": False})
        pos = self._get_positions().close_position(symbol)
        if pos:
            return json.dumps({"closed": symbol, "success": True})
        return json.dumps({"closed": None, "success": False})

    def _get_account(self) -> str:
        """Return account info: balance, active count, exposure."""
        positions = self._get_positions()
        return json.dumps({
            "account_balance": positions.account_balance,
            "active_count": positions.active_count,
            "exposure": positions.get_exposure(),
        })

    # ------------------------------------------------------------------
    # New actions: TP/SL monitor lifecycle
    # ------------------------------------------------------------------

    def _start_monitor(self) -> str:
        """Start background TP/SL monitoring thread."""
        if self._monitor is not None and self._monitor.is_alive():
            return json.dumps({"status": "already_running"})
        exchange = self._get_exchange()
        positions = self._get_positions()
        self._monitor = TPSLMonitor(exchange, positions, poll_interval=5.0)
        self._monitor.start()
        return json.dumps({"status": "started"})

    def _stop_monitor(self) -> str:
        """Stop background TP/SL monitoring thread."""
        if self._monitor is None:
            return json.dumps({"status": "not_running"})
        self._monitor.stop()
        self._monitor = None
        return json.dumps({"status": "stopped"})
