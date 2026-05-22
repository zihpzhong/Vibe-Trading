"""Tests for A-share alpha factors — reuse crypto formulas with A-share weights."""

from __future__ import annotations

import numpy as np
import pandas as pd

from extensions.live_trading.astock.alpha_factors import _A_SHARE_WEIGHTS, aggregate_signal, compute_all


def _mock_ohlcv(length: int = 60, base: float = 100.0) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    rng = np.random.default_rng(42)
    close = base * (1 + rng.normal(0, 0.015, length)).cumprod()
    high = close * (1 + abs(rng.normal(0, 0.005, length)))
    low = close * (1 - abs(rng.normal(0, 0.005, length)))
    volume = rng.uniform(1e6, 5e7, length)
    return pd.Series(close), pd.Series(high), pd.Series(low), pd.Series(volume)


def test_compute_all_returns_dict() -> None:
    close, high, low, volume = _mock_ohlcv()
    result = compute_all(close, high, low, volume)
    assert isinstance(result, dict)
    for key in _A_SHARE_WEIGHTS:
        assert key in result, f"Missing factor: {key}"


def test_compute_all_short_series() -> None:
    close, high, low, volume = _mock_ohlcv(length=10)
    result = compute_all(close, high, low, volume)
    # With insufficient data, all factors should be 0
    for key in _A_SHARE_WEIGHTS:
        assert result.get(key) == 0.0, f"Expected 0 for {key} with short data"


def test_aggregate_signal() -> None:
    factors = {k: 0.5 for k in _A_SHARE_WEIGHTS}
    signal = aggregate_signal(factors)
    assert -1.0 <= signal <= 1.0


def test_aggregate_signal_returns_zero_on_empty() -> None:
    signal = aggregate_signal({})
    assert signal == 0.0


def test_aggregate_signal_clips() -> None:
    factors = {k: 10.0 for k in _A_SHARE_WEIGHTS}
    signal = aggregate_signal(factors)
    assert signal == 1.0


def test_all_weights_positive() -> None:
    for name, weight in _A_SHARE_WEIGHTS.items():
        assert weight > 0, f"Weight for {name} should be positive"


def test_compute_all_contains_expected_keys() -> None:
    close, high, low, volume = _mock_ohlcv()
    result = compute_all(close, high, low, volume)
    # crypto compute_all may return extra keys beyond A-share subset
    for key in _A_SHARE_WEIGHTS:
        assert key in result, f"Missing expected factor: {key}"