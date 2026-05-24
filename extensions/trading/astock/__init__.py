"""A-share trading package (live + backtest).
A 股交易包（实盘 + 回测）。
"""

from extensions.trading.astock.config import (
    AStockDeRiskConfig,
    AStockFeeConfig,
    AStockGateConfig,
    AStockStopConfig,
    AStockTradingConfig,
)
from extensions.trading.astock.live.exchange import MockAStockExchange, create_astock_exchange
from extensions.trading.astock.models import (
    AStockCloseRecord,
    AStockPhase2Request,
    AStockPosition,
    AStockScheduleReport,
    AStockSignal,
    AStockSignalDirection,
    ExecutionGateResult,
    GateStatus,
    MarketConductionStatus,
)

__all__ = [
    "AStockCloseRecord",
    "AStockDeRiskConfig",
    "AStockFeeConfig",
    "AStockGateConfig",
    "AStockPhase2Request",
    "AStockPosition",
    "AStockScheduleReport",
    "AStockSignal",
    "AStockSignalDirection",
    "AStockStopConfig",
    "AStockTradingConfig",
    "ExecutionGateResult",
    "GateStatus",
    "MarketConductionStatus",
    "MockAStockExchange",
    "create_astock_exchange",
]
