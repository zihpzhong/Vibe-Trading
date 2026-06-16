"""Tests for A-share scanner — scoring and filtering."""

from __future__ import annotations

from extensions.trading.astock.live.scanner import score_buy, _rsi


def test_score_buy_mid_rsi() -> None:
    score = score_buy({"rsi_daily": 50, "above_ma20": 0, "above_ma60": 0,
                        "vol_ratio": 1.0, "change_5d_pct": 0}, 0.0)
    # RSI 35-65 gives +2, nothing else triggers
    assert 2 <= score <= 4


def test_score_buy_full_setup() -> None:
    score = score_buy({"rsi_daily": 45, "above_ma20": 1, "above_ma60": 1,
                        "vol_ratio": 1.5, "change_5d_pct": 3}, 0.5)
    # rsi=45(+2) + above_ma20(+2) + above_ma60(+1) + vol_ratio>=1.2(+2) + chg5 0-8(+1) + alpha>0.2(+2) = 10
    assert 8 <= score <= 10


def test_score_buy_low_vol_ratio() -> None:
    score = score_buy({"rsi_daily": 50, "above_ma20": 0, "above_ma60": 0,
                        "vol_ratio": 0.8, "change_5d_pct": 0}, 0.0)
    # RSI 35-65 gives +2 only
    assert score == 2


def test_score_buy_oversold_rsi() -> None:
    """RSI between 30-35 should get extra point + vol_ratio >=1.0 add 1."""
    score = score_buy({"rsi_daily": 32, "above_ma20": 0, "above_ma60": 0,
                        "vol_ratio": 1.0, "change_5d_pct": 0}, 0.0)
    # RSI 30-35 gives +3, vol_ratio=1.0 gives +1 = 4
    assert score == 4


def test_score_buy_alpha_boost() -> None:
    score_low = score_buy({"rsi_daily": 50, "above_ma20": 0, "above_ma60": 0,
                            "vol_ratio": 1.0, "change_5d_pct": 0}, 0.1)
    score_high = score_buy({"rsi_daily": 50, "above_ma20": 0, "above_ma60": 0,
                             "vol_ratio": 1.0, "change_5d_pct": 0}, 0.5)
    assert score_high >= score_low


def test_score_buy_capped_at_10() -> None:
    score = score_buy({"rsi_daily": 45, "above_ma20": 1, "above_ma60": 1,
                        "vol_ratio": 2.0, "change_5d_pct": 5}, 0.5)
    assert score == 10


def test_rsi_uptrend() -> None:
    import pandas as pd
    close = pd.Series(range(50, 80))  # Strictly increasing
    rsi = _rsi(close, 14)
    assert rsi == 100.0


def test_rsi_downtrend() -> None:
    import pandas as pd
    close = pd.Series(range(80, 49, -1))  # Strictly decreasing
    rsi = _rsi(close, 14)
    assert rsi == 0.0


def test_rsi_insufficient_data() -> None:
    import pandas as pd
    close = pd.Series([1, 2, 3])
    rsi = _rsi(close, 14)
    assert rsi == 50.0
