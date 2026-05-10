"""ATR 动态止损计算.

Calculates stop-loss prices based on Average True Range (ATR)
using Wilder's smoothing method.
"""

from __future__ import annotations

import pandas as pd

from extensions.live_trading.config import ATRStopConfig
from extensions.live_trading.models import SignalDirection


def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Average True Range using Wilder's smoothing.

    Args:
        high: High price series.
        low: Low price series.
        close: Close price series.
        period: ATR period (default 14).

    Returns:
        ATR series aligned with the input index.
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder's smoothed ATR
    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def calculate_atr_stop(
    kline: pd.DataFrame,
    direction: SignalDirection,
    entry_price: float,
    config: ATRStopConfig | None = None,
    conservative: bool = False,
) -> tuple[float, float]:
    """Calculate stop-loss price based on ATR.

    Args:
        kline: OHLCV DataFrame with columns: high, low, close.
        direction: Trade direction (LONG or SHORT).
        entry_price: Entry price for the position.
        config: ATR stop configuration.
        conservative: If True, use conservative multiplier (1.5x vs 2x).

    Returns:
        Tuple of (stop_loss_price, atr_value).
    """
    cfg = config or ATRStopConfig()

    if len(kline) < cfg.period:
        # Not enough data: use a fixed 5% stop as fallback
        if direction == SignalDirection.LONG:
            return entry_price * 0.95, 0.0
        else:
            return entry_price * 1.05, 0.0

    atr_series = calculate_atr(kline["high"], kline["low"], kline["close"], cfg.period)
    atr_value = atr_series.iloc[-1]

    if pd.isna(atr_value) or atr_value == 0:
        if direction == SignalDirection.LONG:
            return entry_price * 0.95, 0.0
        else:
            return entry_price * 1.05, 0.0

    multiplier = cfg.multiplier_conservative if conservative else cfg.multiplier_default
    stop_distance = atr_value * multiplier

    if direction == SignalDirection.LONG:
        stop_price = entry_price - stop_distance
    else:
        stop_price = entry_price + stop_distance

    return round(stop_price, 2), round(atr_value, 2)
