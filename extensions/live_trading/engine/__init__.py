"""Live trading engine modules.

Execution Gate engine, BTC conduction check, ATR stop calculation,
exchange data abstraction, Phase 1 market scanner, scheduler,
position tracker, TP/SL monitor, Phase 2 analysis, and swarm consensus.
"""

from extensions.live_trading.engine.candlestick_patterns import aggregate_candlestick_signal
from extensions.live_trading.engine.execution_gate import ExecGateEngine
from extensions.live_trading.engine.btc_conduction import ConductionStatus, check_btc_conduction
from extensions.live_trading.engine.atr_stop import calculate_atr, calculate_atr_stop
from extensions.live_trading.engine.exchange import ExchangeBase, MockExchange, create_exchange
from extensions.live_trading.engine.market_scanner import MarketScanner, ScanResult
from extensions.live_trading.engine.position_tracker import Position, PositionTracker
from extensions.live_trading.engine.migration import migrate_from_json
from extensions.live_trading.engine.tpsl_monitor import TPSLMonitor
from extensions.live_trading.engine.scheduler import TradingScheduler
from extensions.live_trading.engine.phase2 import Phase2Analyzer
from extensions.live_trading.engine.swarm_phase2 import (
    SwarmPhase2Config,
    SwarmPhase2Engine,
    SwarmDimAnalyzer,
    run_consensus,
    swarm_to_phase2_consensus,
)
from extensions.live_trading.engine.reconcile import reconcile_positions
from extensions.live_trading.engine.exchange_brackets import (
    has_bracket_support,
    place_bracket_orders,
    cancel_bracket_orders,
    cancel_exchange_sl_order,
)
from extensions.live_trading.engine.alpha_factors import compute_all as compute_alpha_factors

__all__ = [
    "ExecGateEngine",
    "check_btc_conduction",
    "ConductionStatus",
    "calculate_atr",
    "calculate_atr_stop",
    "ExchangeBase",
    "MockExchange",
    "create_exchange",
    "MarketScanner",
    "ScanResult",
    "Position",
    "PositionTracker",
    "migrate_from_json",
    "TPSLMonitor",
    "TradingScheduler",
    "Phase2Analyzer",
    "SwarmPhase2Config",
    "SwarmPhase2Engine",
    "SwarmDimAnalyzer",
    "run_consensus",
    "swarm_to_phase2_consensus",
    "reconcile_positions",
    "has_bracket_support",
    "place_bracket_orders",
    "cancel_bracket_orders",
    "cancel_exchange_sl_order",
    "compute_alpha_factors",
]
