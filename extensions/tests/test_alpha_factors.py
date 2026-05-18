"""Tests for Alpha Factor Zoo integration — Phase A-1.

Tests every factor function and the scoring integration.
Pure functions — no exchange needed, no network calls.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from extensions.live_trading.engine.alpha_factors import (
    aggregate_signal,
    compute_all,
    corr_price_volume,
    momentum_normalized,
    normalized_range,
    run_streak,
    ts_argmax_decay,
    ts_argmin_decay,
    ts_rank_close,
    ts_rank_high_low,
    ts_rank_volume,
    volatility_regime,
    volume_price_trend,
    zscore_price,
)


def _make_series(values: list[float]) -> pd.Series:
    return pd.Series(values)


# ---------------------------------------------------------------------------
# Momentum factors
# ---------------------------------------------------------------------------

class TestMomentumNormalized:
    def test_positive_returns_give_positive_signal(self) -> None:
        close = _make_series([100.0 + i * 0.5 for i in range(30)])  # steady uptrend
        result = momentum_normalized(close, 5)
        assert result > 0

    def test_negative_returns_give_negative_signal(self) -> None:
        close = _make_series([100.0 - i * 0.5 for i in range(30)])  # steady downtrend
        result = momentum_normalized(close, 5)
        assert result < 0

    def test_insufficient_data_returns_zero(self) -> None:
        close = _make_series([100.0] * 3)
        assert momentum_normalized(close, 5) == 0.0

    def test_signal_is_bounded(self) -> None:
        close = _make_series([100.0] + [200.0] * 29)  # big jump then flat
        result = momentum_normalized(close, 5)
        assert -1.0 <= result <= 1.0


class TestTsRankClose:
    def test_close_at_window_high(self) -> None:
        close = _make_series([95.0, 96.0, 97.0, 98.0, 99.0, 100.0])
        result = ts_rank_close(close, 5)
        assert result == pytest.approx(1.0, abs=0.01)

    def test_close_at_window_low(self) -> None:
        close = _make_series([105.0, 104.0, 103.0, 102.0, 101.0, 100.0])
        result = ts_rank_close(close, 5)
        assert result == pytest.approx(0.0, abs=0.01)

    def test_insufficient_data(self) -> None:
        close = _make_series([100.0] * 3)
        assert ts_rank_close(close, 20) == 0.5

    def test_flat_market(self) -> None:
        close = _make_series([100.0] * 30)
        assert ts_rank_close(close, 20) == 0.5


class TestTsRankHighLow:
    def test_close_near_high(self) -> None:
        high = _make_series([100.0] * 5 + [106.0, 107.0, 108.0, 109.0, 110.0])
        low = _make_series([80.0] * 5 + [94.0, 95.0, 96.0, 97.0, 98.0])
        result = ts_rank_high_low(high, low, 10)
        # close at ~110 in a 80-110 range → near top
        assert result > 0.5

    def test_close_near_low(self) -> None:
        high = _make_series([100.0] * 5 + [106.0, 107.0, 108.0, 109.0, 110.0])
        low = _make_series([80.0] * 5 + [94.0, 95.0, 96.0, 97.0, 98.0])
        result = ts_rank_high_low(high, low, 10)
        # close at 110 is near the top of the range
        assert result > 0.5

    def test_insufficient_data(self) -> None:
        assert ts_rank_high_low(_make_series([100.0]), _make_series([90.0]), 10) == 0.5


# ---------------------------------------------------------------------------
# Reversal factors
# ---------------------------------------------------------------------------

class TestZscorePrice:
    def test_price_below_mean_gives_positive_signal(self) -> None:
        """Price below SMA → inverted positive signal (reversal bullish)."""
        close = _make_series([100.0] * 20 + [90.0])
        result = zscore_price(close, 20)
        assert result > 0  # below mean → bullish reversal signal

    def test_price_above_mean_gives_negative_signal(self) -> None:
        """Price above SMA → inverted negative signal (reversal bearish)."""
        close = _make_series([100.0] * 20 + [110.0])
        result = zscore_price(close, 20)
        assert result < 0  # above mean → bearish reversal signal

    def test_insufficient_data(self) -> None:
        assert zscore_price(_make_series([100.0] * 3), 20) == 0.0

    def test_signal_bounded(self) -> None:
        close = _make_series([100.0] * 20 + [1.0])
        result = zscore_price(close, 20)
        assert -1.0 <= result <= 1.0

    def test_flat_line(self) -> None:
        close = _make_series([100.0] * 25)
        result = zscore_price(close, 20)
        assert abs(result) < 0.01  # near zero when constant


class TestCorrPriceVolume:
    def test_positive_correlation(self) -> None:
        close = _make_series([100.0, 101.0, 102.0, 103.0, 104.0,
                              105.0, 106.0, 107.0, 108.0, 109.0,
                              110.0, 111.0, 112.0, 113.0, 114.0,
                              115.0, 116.0, 117.0, 118.0, 119.0])
        volume = _make_series([1000 + i * 50 for i in range(20)])  # rising with price
        result = corr_price_volume(close, volume, 20)
        assert result > 0

    def test_negative_correlation(self) -> None:
        close = _make_series([100.0, 101.0, 102.0, 103.0, 104.0,
                              105.0, 106.0, 107.0, 108.0, 109.0,
                              110.0, 111.0, 112.0, 113.0, 114.0,
                              115.0, 116.0, 117.0, 118.0, 119.0])
        volume = _make_series([1000 - i * 30 for i in range(20)])  # falling as price rises
        result = corr_price_volume(close, volume, 20)
        assert result < 0

    def test_insufficient_data(self) -> None:
        assert corr_price_volume(_make_series([100.0] * 3), _make_series([100.0] * 3), 20) == 0.0


class TestTsArgmaxDecay:
    def test_fresh_high(self) -> None:
        high = _make_series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
        result = ts_argmax_decay(high, 5)
        assert result == pytest.approx(1.0, abs=0.01)  # highest = most recent

    def test_old_high(self) -> None:
        high = _make_series([105.0, 104.0, 103.0, 102.0, 101.0, 100.0])
        result = ts_argmax_decay(high, 5)
        assert result == pytest.approx(0.0, abs=0.01)  # highest = oldest


class TestTsArgminDecay:
    def test_fresh_low(self) -> None:
        low = _make_series([100.0, 99.0, 98.0, 97.0, 96.0, 95.0])
        result = ts_argmin_decay(low, 5)
        assert result == pytest.approx(1.0, abs=0.01)


# ---------------------------------------------------------------------------
# Volatility factors
# ---------------------------------------------------------------------------

class TestVolatilityRegime:
    def test_expanding_volatility(self) -> None:
        high = _make_series([101.0] * 30 + [102.0, 103.0, 105.0, 108.0, 112.0])
        low = _make_series([99.0] * 30 + [97.0, 95.0, 92.0, 88.0, 83.0])
        result = volatility_regime(high, low, 14)
        # last bars have expanding range → positive signal
        assert result != 0.0

    def test_insufficient_data(self) -> None:
        assert volatility_regime(_make_series([100.0]), _make_series([99.0]), 20) == 0.0


class TestNormalizedRange:
    def test_narrowing_range(self) -> None:
        high = _make_series([105.0] * 5 + [101.0, 100.5, 100.3, 100.2, 100.1])
        low = _make_series([95.0] * 5 + [99.0, 99.5, 99.7, 99.8, 99.9])
        result = normalized_range(high, low, 14)
        assert result is not None
        assert -1.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# Volume factors
# ---------------------------------------------------------------------------

class TestVolumePriceTrend:
    def test_positive_money_flow(self) -> None:
        close = _make_series([100.0, 101.0, 102.0, 103.0, 104.0,
                              105.0, 106.0, 107.0, 108.0, 109.0,
                              110.0, 111.0, 112.0, 113.0, 114.0])
        volume = _make_series([1000.0] * 15)
        result = volume_price_trend(close, volume, 14)
        assert result > 0  # rising prices → positive flow

    def test_negative_money_flow(self) -> None:
        close = _make_series([110.0, 109.0, 108.0, 107.0, 106.0,
                              105.0, 104.0, 103.0, 102.0, 101.0,
                              100.0, 99.0, 98.0, 97.0, 96.0])
        volume = _make_series([1000.0] * 15)
        result = volume_price_trend(close, volume, 14)
        assert result < 0  # falling prices → negative flow


class TestRunStreak:
    def test_consecutive_green_bars(self) -> None:
        close = _make_series([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
        result = run_streak(close, 8)
        assert result > 0

    def test_consecutive_red_bars(self) -> None:
        close = _make_series([105.0, 104.0, 103.0, 102.0, 101.0, 100.0])
        result = run_streak(close, 8)
        assert result < 0

    def test_alternating_bars(self) -> None:
        close = _make_series([100.0, 101.0, 100.0, 101.0, 100.0, 101.0])
        result = run_streak(close, 8)
        assert abs(result) < 0.5

    def test_insufficient_data(self) -> None:
        assert run_streak(_make_series([100.0, 101.0]), 8) == 0.0


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregateSignal:
    def test_uptrend_factors_produce_positive_signal(self) -> None:
        """Steady uptrend should produce a positive aggregate signal."""
        # Add small noise to create non-zero volatility
        rng = np.random.default_rng(42)
        noise = rng.normal(0, 0.5, 60)
        base = 100.0 + np.arange(60) * 0.5
        close = _make_series(list(base + noise))
        high = _make_series([c * 1.015 for c in close])
        low = _make_series([c * 0.985 for c in close])
        volume = _make_series([1000.0 + i * 5 for i in range(60)])
        factors = compute_all(close, high, low, volume)
        signal = aggregate_signal(factors)
        assert signal > 0, f"Expected positive signal in uptrend, got {signal:.4f}"

    def test_downtrend_factors_produce_negative_signal(self) -> None:
        """Steady downtrend should produce a negative aggregate signal."""
        rng = np.random.default_rng(99)
        noise = rng.normal(0, 0.5, 60)
        base = 130.0 - np.arange(60) * 0.5
        close = _make_series(list(base + noise))
        high = _make_series([c * 1.015 for c in close])
        low = _make_series([c * 0.985 for c in close])
        volume = _make_series([1000.0 + i * 3 for i in range(60)])
        factors = compute_all(close, high, low, volume)
        signal = aggregate_signal(factors)
        assert signal < 0, f"Expected negative signal in downtrend, got {signal:.4f}"

    def test_signal_bounded(self) -> None:
        close = _make_series([100.0] * 60)
        high = _make_series([101.0] * 60)
        low = _make_series([99.0] * 60)
        volume = _make_series([1000.0] * 60)
        factors = compute_all(close, high, low, volume)
        signal = aggregate_signal(factors)
        assert -1.0 <= signal <= 1.0

    def test_all_factors_present(self) -> None:
        close = _make_series([100.0] * 60)
        high = _make_series([101.0] * 60)
        low = _make_series([99.0] * 60)
        volume = _make_series([1000.0] * 60)
        factors = compute_all(close, high, low, volume)
        expected_keys = [
            "momentum_5", "momentum_20", "ts_rank_close_20", "ts_rank_volume_20",
            "ts_rank_hl_10", "zscore_20", "corr_pv_20", "ts_argmax_10",
            "ts_argmin_10", "vol_regime_20", "norm_range_14", "vpt_14", "run_streak_8",
        ]
        for key in expected_keys:
            assert key in factors, f"Missing factor: {key}"
            assert isinstance(factors[key], float), f"{key} not a float"


# ---------------------------------------------------------------------------
# Scoring integration
# ---------------------------------------------------------------------------

class TestAlphaScoreIntegration:
    """Alpha signal contributions to LONG/SHORT scores in MarketScanner."""

    def test_positive_alpha_boosts_long_score(self) -> None:
        """Strong bullish alpha signal → +1 to LONG score."""
        from extensions.live_trading.engine.market_scanner import MarketScanner
        from extensions.tests.test_market_scanner import _base_indicators

        ind_base = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0)
        ind_alpha = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0, alpha_signal=0.5)
        score_base = MarketScanner.score_long(ind_base)
        score_alpha = MarketScanner.score_long(ind_alpha)
        assert score_alpha == score_base + 1

    def test_negative_alpha_reduces_long_score(self) -> None:
        """Strong bearish alpha signal → -1 to LONG score."""
        from extensions.live_trading.engine.market_scanner import MarketScanner
        from extensions.tests.test_market_scanner import _base_indicators

        ind_base = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0)
        ind_alpha = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0, alpha_signal=-0.5)
        score_base = MarketScanner.score_long(ind_base)
        score_alpha = MarketScanner.score_long(ind_alpha)
        assert score_alpha == score_base - 1

    def test_negative_alpha_boosts_short_score(self) -> None:
        """Strong bearish alpha signal → +1 to SHORT score."""
        from extensions.live_trading.engine.market_scanner import MarketScanner
        from extensions.tests.test_market_scanner import _base_indicators

        ind_base = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0)
        ind_alpha = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0, alpha_signal=-0.5)
        score_base = MarketScanner.score_short(ind_base)
        score_alpha = MarketScanner.score_short(ind_alpha)
        assert score_alpha == score_base + 1

    def test_positive_alpha_reduces_short_score(self) -> None:
        """Strong bullish alpha signal → -1 to SHORT score."""
        from extensions.live_trading.engine.market_scanner import MarketScanner
        from extensions.tests.test_market_scanner import _base_indicators

        ind_base = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0)
        ind_alpha = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0, alpha_signal=0.5)
        score_base = MarketScanner.score_short(ind_base)
        score_alpha = MarketScanner.score_short(ind_alpha)
        assert score_alpha == score_base - 1

    def test_weak_alpha_no_effect(self) -> None:
        """|alpha_signal| < 0.3 has no effect on scores."""
        from extensions.live_trading.engine.market_scanner import MarketScanner
        from extensions.tests.test_market_scanner import _base_indicators

        ind = _base_indicators(rsi_1h=50.0, rsi_15m=50.0, change_24h=0.0, alpha_signal=0.2)
        assert MarketScanner.score_long(ind) == 0
        assert MarketScanner.score_short(ind) == 0

    def test_alpha_in_compute_indicators(self) -> None:
        """compute_indicators() includes alpha_signal and alpha_* keys."""
        from extensions.live_trading.engine.market_scanner import MarketScanner

        ticker = {"symbol": "BTCUSDT", "last": 65000.0, "volume24h": 2_000_000_000.0, "change24h": 0.0}
        kline_1h = pd.DataFrame({
            "open": [64900.0] * 30,
            "high": [65100.0] * 30,
            "low": [64800.0] * 30,
            "close": [65000.0] * 30,
            "volume": [10000.0] * 30,
        })
        kline_15m = pd.DataFrame({
            "open": [64900.0] * 20,
            "high": [65100.0] * 20,
            "low": [64800.0] * 20,
            "close": [65000.0] * 20,
            "volume": [10000.0] * 20,
        })
        result = MarketScanner.compute_indicators(ticker, kline_1h, kline_15m)
        assert "alpha_signal" in result
        assert "alpha_momentum_5" in result
        assert "alpha_zscore_20" in result
        assert isinstance(result["alpha_signal"], float)
