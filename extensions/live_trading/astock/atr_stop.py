"""Daily ATR stop-loss for A-share LONG positions."""

from __future__ import annotations

import pandas as pd

from extensions.live_trading.astock.config import AStockStopConfig
from extensions.live_trading.engine.atr_stop import calculate_atr


def calculate_astock_stop(
    kline: pd.DataFrame,
    entry_price: float,
    config: AStockStopConfig | None = None,
) -> tuple[float, float, float]:
    """Return (stop_loss, take_profit, atr_value) for LONG only."""
    cfg = config or AStockStopConfig()
    if kline is None or kline.empty or len(kline) < cfg.atr_period:
        hard = entry_price * (1 - cfg.hard_stop_loss_pct / 100)
        tp = entry_price + (entry_price - hard) * cfg.default_reward_risk
        return round(hard, 2), round(tp, 2), 0.0

    atr_series = calculate_atr(kline["high"], kline["low"], kline["close"], cfg.atr_period)
    atr_value = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
    stop_distance = atr_value * cfg.atr_multiplier if atr_value > 0 else entry_price * (cfg.hard_stop_loss_pct / 100)

    min_d = entry_price * (cfg.min_stop_distance_pct / 100)
    max_d = entry_price * (cfg.max_stop_distance_pct / 100)
    stop_distance = max(min_d, min(stop_distance, max_d))

    atr_stop = entry_price - stop_distance
    hard_stop = entry_price * (1 - cfg.hard_stop_loss_pct / 100)
    stop_loss = min(atr_stop, hard_stop)
    risk = entry_price - stop_loss
    take_profit = entry_price + risk * cfg.default_reward_risk
    return round(stop_loss, 2), round(take_profit, 2), round(atr_value, 4)
