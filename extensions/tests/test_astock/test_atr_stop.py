"""Tests for A-share ATR stop-loss calculation."""

from __future__ import annotations

import pandas as pd

from extensions.trading.astock.live.atr_stop import calculate_astock_stop
from extensions.trading.astock.config import AStockStopConfig


def _mock_kline(length: int = 30, base: float = 100.0) -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(42)
    prices = base * (1 + rng.normal(0, 0.015, length)).cumprod()
    return pd.DataFrame({
        "high": prices * (1 + abs(rng.normal(0, 0.005, length))),
        "low": prices * (1 - abs(rng.normal(0, 0.005, length))),
        "close": prices,
    })


def test_calculate_stop_returns_tuple() -> None:
    kline = _mock_kline()
    sl, tp, atr = calculate_astock_stop(kline, 100.0)
    assert isinstance(sl, float)
    assert isinstance(tp, float)
    assert isinstance(atr, float)
    assert sl < 100.0
    assert tp > 100.0


def test_calculate_stop_with_insufficient_data() -> None:
    kline = _mock_kline(length=5)
    sl, tp, atr = calculate_astock_stop(kline, 100.0)
    assert atr == 0.0
    # Falls back to hard stop (10%)
    assert sl == 90.0
    assert tp > 100.0


def test_calculate_stop_hard_stop_limits() -> None:
    """ATR stop should be constrained by hard stop."""
    kline = _mock_kline(length=14, base=50.0)
    cfg = AStockStopConfig(hard_stop_loss_pct=5.0)
    sl, tp, atr = calculate_astock_stop(kline, 100.0, cfg)
    # Hard stop at 95, so sl should be <= 95
    assert sl <= 95.0


def test_calculate_stop_default_config() -> None:
    kline = _mock_kline()
    sl, tp, atr = calculate_astock_stop(kline, 100.0)
    # Default: hard_stop_loss_pct=10% → hard floor at 90
    assert sl <= 90.0
    # R:R default_reward_risk=3.0
    expected_min_tp = 100.0 + (100.0 - 90.0) * 3.0
    assert tp >= expected_min_tp - 0.02  # FP tolerance


def test_calculate_stop_min_stop_distance() -> None:
    """When ATR is very small, min_stop_distance_pct should kick in."""
    kline = _mock_kline(length=14, base=100.0)
    cfg = AStockStopConfig(min_stop_distance_pct=10.0, max_stop_distance_pct=15.0)
    sl, tp, atr = calculate_astock_stop(kline, 100.0, cfg)
    # Min distance is 10% = 10.0, so sl <= 90.0
    assert sl <= 90.0
    assert sl >= 85.0  # Max distance is 15% = 15.0, so sl >= 85.0


def test_calculate_stop_empty_kline() -> None:
    sl, tp, atr = calculate_astock_stop(pd.DataFrame(), 100.0)
    assert sl == 90.0
    assert tp == 130.0
    assert atr == 0.0
