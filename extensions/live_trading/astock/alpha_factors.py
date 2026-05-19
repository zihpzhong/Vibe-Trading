"""Daily alpha factors for A-share Phase 1 — reuses crypto formulas with A-share weights."""

from __future__ import annotations

import numpy as np
import pandas as pd

from extensions.live_trading.engine import alpha_factors as crypto_alpha

_MIN_PERIODS = 20

# A-shares: higher reversal weight, lower momentum
_A_SHARE_WEIGHTS: dict[str, float] = {
    "momentum_5": 0.8,
    "momentum_20": 0.6,
    "ts_rank_close_20": 0.8,
    "ts_rank_hl_10": 0.8,
    "zscore_20": 1.8,
    "corr_pv_20": 1.0,
    "ts_argmax_10": 0.5,
    "ts_argmin_10": 0.5,
    "vol_regime_20": 0.6,
    "norm_range_14": 0.5,
    "vpt_14": 1.0,
    "run_streak_8": 0.5,
}


def compute_all(close: pd.Series, high: pd.Series, low: pd.Series, volume: pd.Series) -> dict[str, float]:
    if len(close) < _MIN_PERIODS:
        return {k: 0.0 for k in _A_SHARE_WEIGHTS}
    return crypto_alpha.compute_all(close, high, low, volume)


def aggregate_signal(factors: dict[str, float]) -> float:
    total_weight = 0.0
    weighted_sum = 0.0
    for name, weight in _A_SHARE_WEIGHTS.items():
        value = factors.get(name, 0.0)
        if isinstance(value, float) and not np.isnan(value):
            weighted_sum += value * weight
            total_weight += weight
    if total_weight < 1e-10:
        return 0.0
    return float(np.clip(weighted_sum / total_weight, -1.0, 1.0))
