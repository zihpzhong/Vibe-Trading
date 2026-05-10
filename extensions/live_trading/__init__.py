"""Extension live trading package.

This package stays under ``extensions/`` to keep upstream sync conflicts low.
"""

from extensions.live_trading.config import LiveTradingConfig
from extensions.live_trading.models import ExecutionGateResult, LiveSignal

# Re-export engine public API so consumers can import from extensions.live_trading
from extensions.live_trading.engine import (
    ConductionStatus,
    ExchangeBase,
    ExecGateEngine,
    MarketScanner,
    MockExchange,
    Position,
    PositionTracker,
    ScanResult,
    TPSLMonitor,
    TradingScheduler,
    calculate_atr,
    calculate_atr_stop,
    check_btc_conduction,
    create_exchange,
)

__all__ = [
    "ExecutionGateResult", "LiveSignal", "LiveTradingConfig",
    "ExecGateEngine", "ConductionStatus", "check_btc_conduction",
    "calculate_atr", "calculate_atr_stop",
    "ExchangeBase", "MockExchange", "create_exchange",
    "MarketScanner", "ScanResult",
    "Position", "PositionTracker",
    "TPSLMonitor", "TradingScheduler",
]
