"""Market index conduction — replaces BTC conduction for A-shares."""

from __future__ import annotations

import pandas as pd

from extensions.live_trading.astock.models import MarketConductionStatus


def _ma(series: pd.Series, period: int) -> float:
    if len(series) < period:
        return float(series.iloc[-1]) if len(series) else 0.0
    return float(series.tail(period).mean())


def check_market_conduction(
    index_kline: pd.DataFrame,
    breadth: dict | None = None,
) -> MarketConductionStatus:
    """Evaluate CSI300-style index trend and market breadth.

    Rules (simplified):
    - Close below MA20 with 5d return < -3% → LOCK_ALL
    - Close above MA20 with breadth ratio > 0.55 → STRONG
    - breadth ratio < 0.35 → CAUTION
    - else OK
    """
    if index_kline is None or index_kline.empty or "close" not in index_kline.columns:
        return MarketConductionStatus.OK

    close = index_kline["close"].astype(float)
    last = float(close.iloc[-1])
    ma20 = _ma(close, 20)
    ret5 = 0.0
    if len(close) >= 6:
        ret5 = (last / float(close.iloc[-6]) - 1) * 100

    ratio = 0.5
    if breadth:
        ratio = float(breadth.get("ratio", 0.5))

    if last < ma20 and ret5 < -3.0:
        return MarketConductionStatus.LOCK_ALL
    if last > ma20 and ratio > 0.55 and ret5 > 0:
        return MarketConductionStatus.STRONG
    if ratio < 0.35:
        return MarketConductionStatus.CAUTION
    return MarketConductionStatus.OK
