"""Vectorized candlestick pattern detection for Phase 1 scoring.

Extracted from market_scanner.py. All functions are pure, NaN-safe,
and operate on OHLCV pandas Series. Each returns values in {-1, 0, +1}.

Supports single-candle (hammer, inverted hammer, shooting star),
two-candle (engulfing, harami, piercing line, dark cloud),
and three-candle (morning star, evening star, three soldiers, three crows) patterns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_BODY_PCT = 0.1       # 十字星阈值
_SHADOW_RATIO = 2.0   # 影线实体比阈值


def _body(open_: pd.Series, close: pd.Series) -> pd.Series:
    """实体长度（绝对值）。"""
    return (close - open_).abs()


def _range_hl(high: pd.Series, low: pd.Series) -> pd.Series:
    """K线振幅（最高 - 最低）。"""
    return high - low


def _upper_shadow(open_: pd.Series, close: pd.Series, high: pd.Series) -> pd.Series:
    """上影线长度。"""
    return high - pd.concat([open_, close], axis=1).max(axis=1)


def _lower_shadow(open_: pd.Series, close: pd.Series, low: pd.Series) -> pd.Series:
    """下影线长度。"""
    return pd.concat([open_, close], axis=1).min(axis=1) - low


# -- 单根形态 --

def detect_hammer(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """锤子线 — 看涨。下影线 >= 2*实体，上影线 < 实体。"""
    bd = _body(open_, close)
    rng = _range_hl(high, low)
    cond = (
        (_lower_shadow(open_, close, low) >= _SHADOW_RATIO * bd)
        & (_upper_shadow(open_, close, high) < bd)
        & (bd > 0)
        & (rng > 0)
    )
    return cond.astype(int)


def detect_inverted_hammer(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """倒锤子 — 看涨。上影线 >= 2*实体，下影线 < 实体。"""
    bd = _body(open_, close)
    cond = (_upper_shadow(open_, close, high) >= _SHADOW_RATIO * bd) & (_lower_shadow(open_, close, low) < bd) & (bd > 0)
    return cond.astype(int)


def detect_shooting_star(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """射击之星 — 看跌。形态同倒锤子，需在上涨趋势后。"""
    bd = _body(open_, close)
    us = _upper_shadow(open_, close, high)
    ls = _lower_shadow(open_, close, low)
    uptrend = close.shift(1) > close.shift(2)
    cond = (us >= _SHADOW_RATIO * bd) & (ls < bd) & (bd > 0) & uptrend
    return -(cond.astype(int))


# -- 双根形态 --

def detect_engulfing(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """吞没形态。看涨: 前阴后阳, 当前实体包含前一根实体。看跌: 前阳后阴。"""
    o1, c1 = open_.shift(1), close.shift(1)
    prev_bear, prev_bull = c1 < o1, c1 > o1
    curr_bull, curr_bear = close > open_, close < open_
    bullish = prev_bear & curr_bull & (close >= o1) & (open_ <= c1)
    bearish = prev_bull & curr_bear & (close <= o1) & (open_ >= c1)
    sig = pd.Series(0, index=open_.index)
    sig[bullish] = 1
    sig[bearish] = -1
    return sig


def detect_harami(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """孕线形态。看涨: 前大阴, 当前实体被包含。看跌: 前大阳, 当前实体被包含。"""
    bd, o1, c1 = _body(open_, close), open_.shift(1), close.shift(1)
    bd1 = _body(o1, c1)
    prev_bear, prev_bull = c1 < o1, c1 > o1
    prev_top = pd.concat([o1, c1], axis=1).max(axis=1)
    prev_bot = pd.concat([o1, c1], axis=1).min(axis=1)
    curr_top = pd.concat([open_, close], axis=1).max(axis=1)
    curr_bot = pd.concat([open_, close], axis=1).min(axis=1)
    contained = (curr_top <= prev_top) & (curr_bot >= prev_bot)
    sig = pd.Series(0, index=open_.index)
    sig[prev_bear & (bd1 > bd) & contained] = 1
    sig[prev_bull & (bd1 > bd) & contained] = -1
    return sig


def detect_piercing_line(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """刺穿线 — 看涨。前阴，当前开低于前低，收高于前实体中点。"""
    o1, c1, l1 = open_.shift(1), close.shift(1), low.shift(1)
    cond = (c1 < o1) & (close > open_) & (open_ < l1) & (close > (o1 + c1) / 2)
    return cond.astype(int)


def detect_dark_cloud(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """乌云盖顶 — 看跌。前阳，当前开高于前高，收低于前实体中点。"""
    o1, c1, h1 = open_.shift(1), close.shift(1), high.shift(1)
    cond = (c1 > o1) & (close < open_) & (open_ > h1) & (close < (o1 + c1) / 2)
    return -(cond.astype(int))


# -- 三根形态 --

def detect_morning_star(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """晨星 — 看涨。Day1阴，Day2小实体向下跳空，Day3阳收高于Day1中点。"""
    o1, c1 = open_.shift(2), close.shift(2)
    o2, c2, h2 = open_.shift(1), close.shift(1), high.shift(1)
    bd2 = _body(o2, c2)
    rng2 = _range_hl(high.shift(1), low.shift(1)).replace(0, np.nan)
    cond = (c1 < o1) & (bd2 / rng2 < 0.3) & (h2 < low.shift(2)) & (close > open_) & (close > (o1 + c1) / 2)
    return cond.astype(int).fillna(0).astype(int)


def detect_evening_star(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """暮星 — 看跌。Day1阳，Day2小实体向上跳空，Day3阴收低于Day1中点。"""
    o1, c1 = open_.shift(2), close.shift(2)
    o2, c2, l2 = open_.shift(1), close.shift(1), low.shift(1)
    bd2 = _body(o2, c2)
    rng2 = _range_hl(high.shift(1), low.shift(1)).replace(0, np.nan)
    cond = (c1 > o1) & (bd2 / rng2 < 0.3) & (l2 > high.shift(2)) & (close < open_) & (close < (o1 + c1) / 2)
    return -(cond.astype(int).fillna(0).astype(int))


def detect_three_white_soldiers(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """三白兵 — 看涨。连续3阳，收盘递增，开盘在前一根实体内。"""
    cond = (
        (close.shift(2) > open_.shift(2))
        & (close.shift(1) > open_.shift(1))
        & (close > open_)
        & (close.shift(1) > close.shift(2))
        & (close > close.shift(1))
        & (open_.shift(1) >= open_.shift(2))
        & (open_.shift(1) <= close.shift(2))
        & (open_ >= open_.shift(1))
        & (open_ <= close.shift(1))
    )
    return cond.astype(int).fillna(0).astype(int)


def detect_three_black_crows(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """三乌鸦 — 看跌。连续3阴，收盘递减，开盘在前一根实体内。"""
    cond = (
        (close.shift(2) < open_.shift(2))
        & (close.shift(1) < open_.shift(1))
        & (close < open_)
        & (close.shift(1) < close.shift(2))
        & (close < close.shift(1))
        & (open_.shift(1) <= open_.shift(2))
        & (open_.shift(1) >= close.shift(2))
        & (open_ <= open_.shift(1))
        & (open_ >= close.shift(1))
    )
    return -(cond.astype(int).fillna(0).astype(int))


def aggregate_candlestick_signal(open_: pd.Series, high: pd.Series, low: pd.Series, close: pd.Series) -> int:
    """对所有形态检测函数求和，取符号 {-1, 0, +1}。

    逐个调用各检测函数，对当前最新K线的信号值求和，
    返回综合信号方向。适用于在 Phase 1 扫描中作为额外指标。
    """
    total = 0
    # 单根
    total += int(detect_hammer(open_, high, low, close).iloc[-1])
    total += int(detect_inverted_hammer(open_, high, low, close).iloc[-1])
    total += int(detect_shooting_star(open_, high, low, close).iloc[-1])
    # 双根
    total += int(detect_engulfing(open_, high, low, close).iloc[-1])
    total += int(detect_harami(open_, high, low, close).iloc[-1])
    total += int(detect_piercing_line(open_, high, low, close).iloc[-1])
    total += int(detect_dark_cloud(open_, high, low, close).iloc[-1])
    # 三根
    total += int(detect_morning_star(open_, high, low, close).iloc[-1])
    total += int(detect_evening_star(open_, high, low, close).iloc[-1])
    total += int(detect_three_white_soldiers(open_, high, low, close).iloc[-1])
    total += int(detect_three_black_crows(open_, high, low, close).iloc[-1])

    if total > 0:
        return 1
    if total < 0:
        return -1
    return 0
