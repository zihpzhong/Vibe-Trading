"""Tests for A-share market conduction — CSI300 trend and breadth."""

from __future__ import annotations

import pandas as pd
import pytest

from extensions.live_trading.astock.conduction import check_market_conduction
from extensions.live_trading.astock.models import MarketConductionStatus


def _uptrend_kline(length: int = 80) -> pd.DataFrame:
    prices = [4000 + i * 10 + (i % 5) * 5 for i in range(length)]
    return pd.DataFrame({
        "close": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
    })


def _downtrend_kline(length: int = 80) -> pd.DataFrame:
    """Gradual decline ending with sharp 5-day drop to trigger LOCK_ALL."""
    slow = [4500 - i * 8 for i in range(length - 5)]  # Gradual: 4500 → 3900
    last_val = slow[-1]
    sharp = [last_val * (1 - 0.01 * (i + 1)) for i in range(5)]  # -1% per day
    prices = slow + sharp
    return pd.DataFrame({
        "close": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
    })


def _flat_kline(length: int = 80, base: float = 4000) -> pd.DataFrame:
    import numpy as np
    rng = np.random.default_rng(42)
    prices = base + rng.normal(0, 20, length)
    return pd.DataFrame({
        "close": prices,
        "high": prices + abs(rng.normal(0, 10, length)),
        "low": prices - abs(rng.normal(0, 10, length)),
    })


def test_conduction_ok_with_good_breadth() -> None:
    df = _uptrend_kline()
    status = check_market_conduction(df, {"ratio": 0.5})
    assert status == MarketConductionStatus.OK


def test_conduction_strong() -> None:
    df = _uptrend_kline()
    status = check_market_conduction(df, {"ratio": 0.6})
    assert status == MarketConductionStatus.STRONG


def test_conduction_lock_all() -> None:
    df = _downtrend_kline()
    # Close below MA20, 5d return < -3%
    status = check_market_conduction(df, {"ratio": 0.3})
    assert status == MarketConductionStatus.LOCK_ALL


def test_conduction_caution() -> None:
    df = _uptrend_kline()
    status = check_market_conduction(df, {"ratio": 0.3})
    assert status == MarketConductionStatus.CAUTION


def test_conduction_empty_kline() -> None:
    status = check_market_conduction(pd.DataFrame(), {"ratio": 0.5})
    assert status == MarketConductionStatus.OK


def test_conduction_none_breadth_defaults_mid() -> None:
    df = _uptrend_kline()
    status = check_market_conduction(df, None)
    assert status in (MarketConductionStatus.OK, MarketConductionStatus.STRONG)


def test_conduction_short_kline() -> None:
    """Very short kline should not crash."""
    df = pd.DataFrame({"close": [4000, 4010]})
    status = check_market_conduction(df, {"ratio": 0.5})
    assert status == MarketConductionStatus.OK
