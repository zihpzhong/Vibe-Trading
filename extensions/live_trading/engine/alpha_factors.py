"""Curated alpha factors for Phase 1 scoring — single-symbol time-series versions.

Adapted from WorldQuant Alpha101, GTJA191, and Qlib158 formulaic alphas.
Each function takes OHLCV pandas Series data and returns a normalized signal
in [-1, +1] where positive ≈ bullish (LONG-friendly) and negative ≈ bearish
(SHORT-friendly).

Design principles:
  - Pure functions: no side effects, no state, deterministic.
  - NaN-safe: any function returns 0.0 when data is insufficient.
  - O(1) memory: only the latest window is stored; no growing history.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_MIN_PERIODS = 5  # minimum bars needed before any factor emits a non-zero signal


# ---------------------------------------------------------------------------
# Momentum factors
# ---------------------------------------------------------------------------


def momentum_normalized(close: pd.Series, period: int = 5) -> float:
    """Period return normalized by volatility (z-score-like).

    >0 when returns are positive relative to recent volatility (bullish).
    <0 when negative relative to recent volatility (bearish).

    Formula: (close[t]/close[t-p] - 1) / (std(returns, 20) + eps)
    Range: typically [-3, +3], clipped to [-1, +1].
    """
    if len(close) < max(period + 1, _MIN_PERIODS):
        return 0.0
    returns = close.pct_change().dropna()
    if len(returns) < period + 5:
        return 0.0
    period_return = float(close.iloc[-1] / close.iloc[-period - 1] - 1)
    vol = float(returns.tail(20).std())
    if vol < 1e-10:
        return 0.0
    raw = period_return / vol
    return float(np.clip(raw, -1.0, 1.0))


def ts_rank_close(close: pd.Series, period: int = 20) -> float:
    """Time-series rank: where close price ranks in its own [period]-bar window.

    1.0 = close at window high (strong uptrend), 0.0 = at window low (downtrend).
    Useful as trend filter without needing EMA crossover.
    """
    if len(close) < period:
        return 0.5
    window = close.iloc[-period:]
    lo, hi = float(window.min()), float(window.max())
    if hi - lo < 1e-10:
        return 0.5
    return float((close.iloc[-1] - lo) / (hi - lo))


def ts_rank_volume(volume: pd.Series, period: int = 20) -> float:
    """Where volume ranks in its own [period]-bar window.

    1.0 = current volume is the highest in the window (spike).
    0.5 = median volume.
    0.0 = lowest volume in the window.
    """
    if len(volume) < period:
        return 0.5
    window = volume.iloc[-period:]
    lo, hi = float(window.min()), float(window.max())
    if hi - lo < 1e-10:
        return 0.5
    return float((volume.iloc[-1] - lo) / (hi - lo))


def ts_rank_high_low(high: pd.Series, low: pd.Series, period: int = 10) -> float:
    """Where close ranks within the high-low range of the last [period] bars.

    1.0 = close near the top of the range (bullish pressure).
    0.0 = close near the bottom (bearish pressure).
    """
    if len(high) < period or len(low) < period:
        return 0.5
    hi = float(high.iloc[-period:].max())
    lo = float(low.iloc[-period:].min())
    close = min(hi, max(lo, float(high.iloc[-1])))  # clamp to range
    if hi - lo < 1e-10:
        return 0.5
    return float((close - lo) / (hi - lo))


# ---------------------------------------------------------------------------
# Reversal / mean-reversion factors
# ---------------------------------------------------------------------------


def zscore_price(close: pd.Series, period: int = 20) -> float:
    """Z-score of close vs its [period] simple moving average.

    Positive = price above mean (overextended bullish, potential SHORT opp).
    Negative = price below mean (oversold, potential LONG opp).

    For our scoring, the SIGN is such that positive = bullish for LONG.
    So we INVERT the sign: a negative zscore (price below mean) → positive signal.
    """
    if len(close) < period:
        return 0.0
    window = close.iloc[-period:]
    mu, sigma = float(window.mean()), float(window.std())
    if sigma < 1e-10:
        return 0.0
    raw = (float(close.iloc[-1]) - mu) / sigma
    # Invert: negative z-score (price below mean) → positive (reversal bullish)
    signal = -raw
    return float(np.clip(signal, -1.0, 1.0))


def corr_price_volume(close: pd.Series, volume: pd.Series, period: int = 20) -> float:
    """Rolling Pearson correlation of price and volume over [period] bars.

    Positive → price rising on volume (accumulation, bullish).
    Negative → price rising on declining volume (weak rally, bearish).
    """
    if len(close) < period or len(volume) < period:
        return 0.0
    c = close.iloc[-period:].astype(float)
    v = volume.iloc[-period:].astype(float)
    corr = c.corr(v)
    if np.isnan(corr):
        return 0.0
    return float(np.clip(corr, -1.0, 1.0))


def ts_argmax_decay(high: pd.Series, period: int = 10) -> float:
    """Where the window's highest bar sits, as a fraction of [period].

    1.0 = highest bar was most recent (fresh high, momentum strong).
    0.0 = highest bar was long ago (momentum fading, potential reversal).
    """
    if len(high) < period:
        return 0.5
    window = high.iloc[-period:]
    idx_max = int(window.idxmax() if hasattr(window, 'idxmax') else np.argmax(window.to_numpy()))
    # Convert to age: position from end (0 = most recent, period-1 = oldest)
    age = len(window) - 1 - (window.index.get_loc(idx_max) if hasattr(window.index, 'get_loc') else list(window.index).index(idx_max))
    # Normalize: 1.0 = fresh high, 0.0 = long ago
    return float(1.0 - age / max(period - 1, 1))


def ts_argmin_decay(low: pd.Series, period: int = 10) -> float:
    """Where the window's lowest bar sits, as a fraction of [period].

    1.0 = lowest bar was most recent (fresh low, momentum bearish → negative signal).
    """
    if len(low) < period:
        return 0.5
    window = low.iloc[-period:]
    idx_min = int(window.idxmin() if hasattr(window, 'idxmin') else np.argmin(window.to_numpy()))
    age = len(window) - 1 - (window.index.get_loc(idx_min) if hasattr(window.index, 'get_loc') else list(window.index).index(idx_min))
    return float(1.0 - age / max(period - 1, 1))


# ---------------------------------------------------------------------------
# Volatility / structure factors
# ---------------------------------------------------------------------------


def volatility_regime(high: pd.Series, low: pd.Series, period: int = 20) -> float:
    """Normalized ATR vs its own history: expanding (>0) vs contracting (<0) vol.

    High vol → mean reversion expected (reversal signal).
    Low vol → trend continuation expected (momentum signal).
    """
    if len(high) < period + 1 or len(low) < period + 1:
        return 0.0
    tr = (high - low).abs()  # simplified true range
    atr_series = tr.rolling(window=period, min_periods=period).mean()
    if len(atr_series) < period * 2:
        return 0.0
    atr_window = atr_series.iloc[-period:].dropna()
    if len(atr_window) < period // 2:
        return 0.0
    current_atr = float(atr_window.iloc[-1])
    avg_atr = float(atr_window.mean())
    if avg_atr < 1e-10:
        return 0.0
    raw = (current_atr / avg_atr - 1.0) * 5  # scale to [-5, +5], clip below
    return float(np.clip(raw, -1.0, 1.0))


def normalized_range(high: pd.Series, low: pd.Series, period: int = 14) -> float:
    """(H-L)/Close averaged over [period], relative to its own median.

    >0 → wider bars than normal (volatility expansion, potential breakout).
    <0 → narrower bars (consolidation, potential explosion).
    """
    if len(high) < period or len(low) < period:
        return 0.0
    ranges = (high.iloc[-period:] - low.iloc[-period:]).div(high.iloc[-period:].where(high.iloc[-period:] > 0, np.nan))
    med = float(ranges.median())
    cur = float(ranges.iloc[-1])
    if med < 1e-10:
        return 0.0
    raw = (cur / med - 1.0) * 3
    return float(np.clip(raw, -1.0, 1.0))


# ---------------------------------------------------------------------------
# Volume / money-flow factors
# ---------------------------------------------------------------------------


def volume_price_trend(close: pd.Series, volume: pd.Series, period: int = 14) -> float:
    """Simplified money-flow: volume-weighted price change direction.

    Positive → volume confirms price direction (strong trend).
    Negative → volume diverges from price (weak trend, reversal likely).
    """
    if len(close) < period + 1 or len(volume) < period + 1:
        return 0.0
    money_flow = (close.diff() * volume).iloc[-period:]
    total_flow = float(money_flow.sum())
    abs_flow = float(money_flow.abs().sum())
    if abs_flow < 1e-10:
        return 0.0
    return float(np.clip(total_flow / abs_flow, -1.0, 1.0))


def run_streak(close: pd.Series, period: int = 8) -> float:
    """Consecutive up/down bar streak.

    +1.0 → [period] consecutive green bars (strong bullish momentum).
    -1.0 → [period] consecutive red bars (strong bearish momentum).
    0.0 → alternating or no clear streak.
    """
    if len(close) < 3:
        return 0.0
    direction = np.sign(close.diff().dropna().tail(period).to_numpy())
    # Count streak from the end
    streak = 0
    for d in reversed(direction):
        if np.isnan(d) or d == 0:
            break
        if streak == 0:
            streak = d
        elif d == np.sign(streak):
            streak += d
        else:
            break
    return float(np.clip(streak / max(period, 1), -1.0, 1.0))


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

_FACTOR_REGISTRY: list[tuple[str, float]] = []  # populated at module level


def _register(name: str, weight: float = 1.0) -> None:
    _FACTOR_REGISTRY.append((name, weight))


# NOTE: names must match the keys returned by compute_all()
_register("momentum_5", weight=1.5)
_register("momentum_20", weight=1.0)
_register("ts_rank_close_20", weight=1.0)
_register("ts_rank_hl_10", weight=1.0)
_register("zscore_20", weight=1.2)
_register("corr_pv_20", weight=1.0)
_register("ts_argmax_10", weight=0.8)
_register("ts_argmin_10", weight=0.8)
_register("vol_regime_20", weight=0.6)
_register("norm_range_14", weight=0.5)
_register("vpt_14", weight=1.0)
_register("run_streak_8", weight=0.7)
# ts_rank_volume_20 is informational, not directly used in scoring


def compute_all(close: pd.Series, high: pd.Series, low: pd.Series,
                volume: pd.Series) -> dict[str, float]:
    """Compute all alpha factors and return a dict of {name: value}.

    Args:
        close: Close price series (≥25 bars recommended).
        high: High price series.
        low: Low price series.
        volume: Volume series.

    Returns:
        dict with factor names as keys and values in [-1, +1].
    """
    return {
        "momentum_5": momentum_normalized(close, 5),
        "momentum_20": momentum_normalized(close, 20),
        "ts_rank_close_20": ts_rank_close(close, 20),
        "ts_rank_volume_20": ts_rank_volume(volume, 20),
        "ts_rank_hl_10": ts_rank_high_low(high, low, 10),
        "zscore_20": zscore_price(close, 20),
        "corr_pv_20": corr_price_volume(close, volume, 20),
        "ts_argmax_10": ts_argmax_decay(high, 10),
        "ts_argmin_10": ts_argmin_decay(low, 10),
        "vol_regime_20": volatility_regime(high, low, 20),
        "norm_range_14": normalized_range(high, low, 14),
        "vpt_14": volume_price_trend(close, volume, 14),
        "run_streak_8": run_streak(close, 8),
    }


def aggregate_signal(factors: dict[str, float]) -> float:
    """Weighted aggregation of all factor values into a single signal [-1, +1].

    Positive → bullish (LONG-friendly), Negative → bearish (SHORT-friendly).

    Uses the factor registry weights. Factors that return NaN/0.5 (neutral
    baseline for rank-based ones) contribute minimally.
    """
    total_weight = 0.0
    weighted_sum = 0.0
    for name, weight in _FACTOR_REGISTRY:
        value = factors.get(name, 0.0)
        if isinstance(value, float) and not np.isnan(value):
            weighted_sum += value * weight
            total_weight += weight
    if total_weight < 1e-10:
        return 0.0
    return float(np.clip(weighted_sum / total_weight, -1.0, 1.0))
