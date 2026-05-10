"""BTC 联动前置检查模块.

Checks BTCUSDT 4h trend and 24h price movement to determine
whether to lock LONG or SHORT signals across all coins.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import pandas as pd

from extensions.live_trading.config import BTCConductionConfig


class ConductionStatus(str, Enum):
    """BTC conduction lock status."""

    CONDUCTION_OK = "CONDUCTION_OK"
    LOCK_LONG = "LOCK_LONG"
    LOCK_SHORT = "LOCK_SHORT"


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Compute EMA using pandas ewm."""
    return series.ewm(span=period, adjust=False).mean()


def check_btc_conduction(
    kline: pd.DataFrame,
    config: Optional[BTCConductionConfig] = None,
) -> ConductionStatus:
    """Check BTCUSDT 4h trend and 24h price change.

    Args:
        kline: BTCUSDT 4h kline DataFrame with at least 'close' column.
        config: Conduction check configuration.

    Returns:
        ConductionStatus:
          - CONDUCTION_OK: no lock
          - LOCK_LONG: bearish structure, >3% 24h drop → block ALL longs
          - LOCK_SHORT: bullish structure, >3% 24h rise → block ALL shorts
    """
    cfg = config or BTCConductionConfig()

    if len(kline) < cfg.ema_periods_long:
        return ConductionStatus.CONDUCTION_OK

    close = kline["close"]
    ema12 = compute_ema(close, cfg.ema_periods_short)
    ema26 = compute_ema(close, cfg.ema_periods_mid)
    ema50 = compute_ema(close, cfg.ema_periods_long)

    latest_ema12 = ema12.iloc[-1]
    latest_ema26 = ema26.iloc[-1]
    latest_ema50 = ema50.iloc[-1]

    # 24h = 6 bars of 4h kline
    bars_24h = 6
    if len(close) < bars_24h:
        return ConductionStatus.CONDUCTION_OK

    price_24h_ago = close.iloc[-bars_24h]
    current_price = close.iloc[-1]
    change_pct = (current_price - price_24h_ago) / price_24h_ago * 100

    threshold = cfg.price_change_threshold_pct

    # Bearish: EMA12 < EMA26 < EMA50 and drop > threshold
    if latest_ema12 < latest_ema26 < latest_ema50 and change_pct < -threshold:
        return ConductionStatus.LOCK_LONG

    # Bullish: EMA12 > EMA26 > EMA50 and rise > threshold
    if latest_ema12 > latest_ema26 > latest_ema50 and change_pct > threshold:
        return ConductionStatus.LOCK_SHORT

    return ConductionStatus.CONDUCTION_OK


class BTCTrendHint(str, Enum):
    """BTC 1h short-term trend hint."""
    NEUTRAL = "NEUTRAL"
    WEAKNESS = "WEAKNESS"
    STRENGTH = "STRENGTH"


def check_btc_1h_trend(kline_1h: pd.DataFrame) -> BTCTrendHint:
    """Check BTCUSDT 1h short-term trend for altcoin trade gating.

    WEAKNESS: EMA12 < EMA26 on 1h — avoid altcoin LONGs.
    STRENGTH: EMA12 > EMA26 and RSI > 70 — avoid altcoin SHORTs.

    Args:
        kline_1h: BTCUSDT 1h kline DataFrame with at least 'close' column.

    Returns:
        BTCTrendHint: NEUTRAL, WEAKNESS, or STRENGTH.
    """
    if len(kline_1h) < 26:
        return BTCTrendHint.NEUTRAL

    close = kline_1h["close"]
    ema12 = close.ewm(span=12, adjust=False).mean().iloc[-1]
    ema26 = close.ewm(span=26, adjust=False).mean().iloc[-1]

    # Wilder RSI(14) for overbought check
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
    rsi = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0

    if ema12 < ema26:
        return BTCTrendHint.WEAKNESS
    elif rsi > 70:
        return BTCTrendHint.STRENGTH
    return BTCTrendHint.NEUTRAL
