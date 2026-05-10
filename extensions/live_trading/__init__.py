"""Extension live trading package.

This package stays under ``extensions/`` to keep upstream sync conflicts low.
"""

from extensions.live_trading.config import LiveTradingConfig
from extensions.live_trading.models import ExecutionGateResult, LiveSignal

__all__ = ["ExecutionGateResult", "LiveSignal", "LiveTradingConfig"]
