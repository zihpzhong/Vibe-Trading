"""Unit tests for MarketScanner — US-007.5.

Tests every scoring rule from SKILL.md Step 2 individually.
Uses pure static methods (no exchange needed) for deterministic results.
"""

from __future__ import annotations

import pandas as pd
import pytest

from extensions.live_trading.engine.market_scanner import MarketScanner, ScanResult, _rsi, _ema, _bb_pct
from extensions.live_trading.engine.exchange import MockExchange


# ---------------------------------------------------------------------------
# Helper: build synthetic kline DataFrames
# ---------------------------------------------------------------------------

def _make_kline_1h(
    close_prices: list[float],
    volumes: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    """Build a 1h kline DataFrame from close prices."""
    n = len(close_prices)
    if volumes is None:
        volumes = [1000.0] * n
    if highs is None:
        highs = [c * 1.01 for c in close_prices]
    if lows is None:
        lows = [c * 0.99 for c in close_prices]
    df = pd.DataFrame({
        "open": [c * 0.999 for c in close_prices],
        "high": highs,
        "low": lows,
        "close": close_prices,
        "volume": volumes,
    })
    return df


def _make_kline_15m(close_prices: list[float]) -> pd.DataFrame:
    """Build a 15m kline DataFrame."""
    return _make_kline_1h(close_prices)


def _base_indicators(**overrides: float) -> dict:
    """Return neutral indicators. Override with specific values for each test."""
    defaults: dict = {
        "symbol": "TESTUSDT",
        "price": 100.0,
        "change_24h": 0.0,
        "rsi_1h": 50.0,
        "rsi_15m": 50.0,
        "ema200": 100.0,
        "bb_pct": 0.5,
        "vol_ratio": 1.0,
        "price_in_8h_pct": 0.5,
        "volume_24h": 10_000_000.0,
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------

class TestRSI:
    """RSI(14) calculation."""

    def test_rsi_at_50_with_flat_prices(self) -> None:
        """RSI returns 50 when prices are flat."""
        close = [100.0] * 20
        df = _make_kline_1h(close)
        result = _rsi(df["close"], 14)
        assert result == pytest.approx(50.0, abs=1.0)

    def test_rsi_oversold_on_sustained_decline(self) -> None:
        """RSI < 30 on sustained price decline."""
        close = [100.0 - i * 2.0 for i in range(20)]  # 100 → 62
        df = _make_kline_1h(close)
        result = _rsi(df["close"], 14)
        assert result < 30.0

    def test_rsi_overbought_on_sustained_rise(self) -> None:
        """RSI > 70 on sustained price increase."""
        close = [100.0 + i * 2.0 for i in range(20)]  # 100 → 138
        df = _make_kline_1h(close)
        result = _rsi(df["close"], 14)
        assert result > 70.0

    def test_rsi_insufficient_data(self) -> None:
        """RSI returns 50 when not enough data."""
        close = [100.0] * 5
        df = _make_kline_1h(close)
        result = _rsi(df["close"], 14)
        assert result == 50.0


class TestEMA:
    """EMA calculation."""

    def test_ema200_with_enough_data(self) -> None:
        """EMA200 is computed correctly with 200+ bars."""
        close = [100.0] * 250
        df = _make_kline_1h(close)
        result = _ema(df["close"], 200)
        assert result == pytest.approx(100.0)

    def test_ema_falls_back_to_mean_with_insufficient_data(self) -> None:
        """EMA returns simple mean when fewer bars than period."""
        close = [100.0, 110.0, 120.0]
        df = _make_kline_1h(close)
        result = _ema(df["close"], 200)
        assert result == pytest.approx(110.0)


class TestBBPct:
    """Bollinger Band %b calculation."""

    def test_bb_pct_at_middle(self) -> None:
        """BB% ≈ 0.5 when price is at middle band."""
        close = [100.0] * 30
        df = _make_kline_1h(close)
        result = _bb_pct(df["close"])
        assert result == pytest.approx(0.5, abs=0.1)

    def test_bb_pct_insufficient_data(self) -> None:
        """BB% returns 0.5 when not enough bars."""
        close = [100.0] * 5
        df = _make_kline_1h(close)
        result = _bb_pct(df["close"])
        assert result == 0.5


# ---------------------------------------------------------------------------
# compute_indicators
# ---------------------------------------------------------------------------

class TestComputeIndicators:
    """Indicator computation from ticker + klines."""

    def test_basic_fields_mapped(self) -> None:
        """Ticker fields are correctly mapped to indicator dict."""
        ticker = {
            "symbol": "BTCUSDT", "last": 65000.0, "open24h": 64000.0,
            "volume24h": 2_000_000_000.0, "high24h": 66000.0, "low24h": 63500.0,
            "change24h": 1.56,
        }
        kline_1h = _make_kline_1h([65000.0] * 200)
        kline_15m = _make_kline_15m([65000.0] * 20)
        result = MarketScanner.compute_indicators(ticker, kline_1h, kline_15m)
        assert result["symbol"] == "BTCUSDT"
        assert result["price"] == 65000.0
        assert result["change_24h"] == 1.56
        assert result["volume_24h"] == 2_000_000_000.0

    def test_indicators_have_all_required_keys(self) -> None:
        """All expected keys are present."""
        ticker = {"symbol": "X", "last": 50.0, "volume24h": 5_000_000, "change24h": -3.0}
        kline_1h = _make_kline_1h([50.0] * 200)
        kline_15m = _make_kline_15m([50.0] * 20)
        result = MarketScanner.compute_indicators(ticker, kline_1h, kline_15m)
        required = ["symbol", "price", "change_24h", "rsi_1h", "rsi_15m",
                     "ema200", "bb_pct", "vol_ratio", "price_in_8h_pct", "volume_24h"]
        for key in required:
            assert key in result, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# LONG_Score — per-rule tests (exact SKILL.md Step 2 rules)
# ---------------------------------------------------------------------------

class TestLongScoreOversold:
    """Technical oversold rules for LONG."""

    def test_rsi_1h_below_30_adds_2(self) -> None:
        ind = _base_indicators(rsi_1h=25.0)
        assert MarketScanner.score_long(ind) >= 2

    def test_rsi_1h_30_to_40_adds_1(self) -> None:
        ind = _base_indicators(rsi_1h=35.0)
        score = MarketScanner.score_long(ind)
        # RSI(1h) 30-40 gives +1
        assert score >= 1
        # But below 30 gives +2, so 35 should give exactly +1 from this rule
        ind2 = _base_indicators(rsi_1h=25.0)
        score_25 = MarketScanner.score_long(ind2)
        assert score < score_25  # 25 gets 2, 35 gets 1

    def test_rsi_15m_below_30_adds_1(self) -> None:
        ind = _base_indicators(rsi_15m=28.0, rsi_1h=50.0)
        ind_base = _base_indicators(rsi_15m=50.0, rsi_1h=50.0)
        assert MarketScanner.score_long(ind) > MarketScanner.score_long(ind_base)

    def test_rsi_15m_below_rsi_1h_adds_1(self) -> None:
        ind = _base_indicators(rsi_15m=45.0, rsi_1h=50.0)
        ind_base = _base_indicators(rsi_15m=50.0, rsi_1h=50.0)
        assert MarketScanner.score_long(ind) > MarketScanner.score_long(ind_base)


class TestLongScorePriceDecline:
    """Price decline rules for LONG."""

    def test_24h_down_above_10_pct_adds_2(self) -> None:
        ind = _base_indicators(change_24h=-12.0)
        ind_mild = _base_indicators(change_24h=-6.0)
        assert MarketScanner.score_long(ind) > MarketScanner.score_long(ind_mild)

    def test_24h_down_5_to_10_pct_adds_1(self) -> None:
        ind = _base_indicators(change_24h=-6.0)
        ind_neutral = _base_indicators(change_24h=0.0)
        assert MarketScanner.score_long(ind) > MarketScanner.score_long(ind_neutral)


class TestLongScorePosition:
    """Position confirmation rules for LONG."""

    def test_bb_pct_below_0_2_adds_1(self) -> None:
        ind = _base_indicators(bb_pct=0.1)
        ind_base = _base_indicators(bb_pct=0.5)
        assert MarketScanner.score_long(ind) > MarketScanner.score_long(ind_base)

    def test_price_in_lower_20pct_8h_adds_1(self) -> None:
        ind = _base_indicators(price_in_8h_pct=0.1)
        ind_base = _base_indicators(price_in_8h_pct=0.5)
        assert MarketScanner.score_long(ind) > MarketScanner.score_long(ind_base)


class TestLongScoreTrendFilter:
    """Trend filter rules for LONG — the most critical rule."""

    def test_price_above_ema200_adds_1(self) -> None:
        ind = _base_indicators(price=110.0, ema200=100.0)
        ind_base = _base_indicators(price=100.0, ema200=100.0)
        assert MarketScanner.score_long(ind) > MarketScanner.score_long(ind_base)

    def test_trend_filter_zeroes_score(self) -> None:
        """Score → 0 when price < EMA200 AND RSI(1h) > 40."""
        ind = _base_indicators(
            price=90.0, ema200=100.0, rsi_1h=45.0,
            change_24h=-12.0,  # would give +2
        )
        assert MarketScanner.score_long(ind) == 0

    def test_trend_filter_does_not_zero_when_rsi_low(self) -> None:
        """Score NOT zeroed when price < EMA200 but RSI(1h) ≤ 40."""
        ind = _base_indicators(
            price=90.0, ema200=100.0, rsi_1h=28.0,
            change_24h=-12.0,
        )
        assert MarketScanner.score_long(ind) > 0


class TestLongScoreVolume:
    """Volume confirmation rule for LONG."""

    def test_volume_spike_on_decline_adds_1(self) -> None:
        ind = _base_indicators(vol_ratio=2.0, change_24h=-3.0)
        ind_base = _base_indicators(vol_ratio=2.0, change_24h=0.0)
        assert MarketScanner.score_long(ind) > MarketScanner.score_long(ind_base)

    def test_volume_spike_on_rise_no_bonus(self) -> None:
        """vol_ratio > 1.5 but price is up → no bonus for LONG."""
        ind = _base_indicators(vol_ratio=2.0, change_24h=3.0)
        ind_base = _base_indicators(vol_ratio=1.0, change_24h=3.0)
        assert MarketScanner.score_long(ind) == MarketScanner.score_long(ind_base)


# ---------------------------------------------------------------------------
# SHORT_Score — per-rule tests
# ---------------------------------------------------------------------------

class TestShortScoreOverbought:
    """Technical overbought rules for SHORT."""

    def test_rsi_1h_above_70_adds_2(self) -> None:
        ind = _base_indicators(rsi_1h=75.0)
        assert MarketScanner.score_short(ind) >= 2

    def test_rsi_1h_60_to_70_adds_1(self) -> None:
        ind = _base_indicators(rsi_1h=65.0)
        ind_above = _base_indicators(rsi_1h=75.0)
        assert MarketScanner.score_short(ind) < MarketScanner.score_short(ind_above)

    def test_rsi_15m_above_70_adds_1(self) -> None:
        ind = _base_indicators(rsi_15m=72.0, rsi_1h=50.0)
        ind_base = _base_indicators(rsi_15m=50.0, rsi_1h=50.0)
        assert MarketScanner.score_short(ind) > MarketScanner.score_short(ind_base)

    def test_rsi_15m_above_rsi_1h_adds_1(self) -> None:
        ind = _base_indicators(rsi_15m=55.0, rsi_1h=50.0)
        ind_base = _base_indicators(rsi_15m=50.0, rsi_1h=50.0)
        assert MarketScanner.score_short(ind) > MarketScanner.score_short(ind_base)


class TestShortScorePriceRise:
    """Price rise rules for SHORT."""

    def test_24h_up_above_10_pct_adds_2(self) -> None:
        ind = _base_indicators(change_24h=12.0)
        ind_mild = _base_indicators(change_24h=6.0)
        assert MarketScanner.score_short(ind) > MarketScanner.score_short(ind_mild)

    def test_24h_up_5_to_10_pct_adds_1(self) -> None:
        ind = _base_indicators(change_24h=6.0)
        ind_neutral = _base_indicators(change_24h=0.0)
        assert MarketScanner.score_short(ind) > MarketScanner.score_short(ind_neutral)


class TestShortScorePosition:
    """Position confirmation rules for SHORT."""

    def test_bb_pct_above_0_8_adds_1(self) -> None:
        ind = _base_indicators(bb_pct=0.9)
        ind_base = _base_indicators(bb_pct=0.5)
        assert MarketScanner.score_short(ind) > MarketScanner.score_short(ind_base)

    def test_price_in_upper_20pct_8h_adds_1(self) -> None:
        ind = _base_indicators(price_in_8h_pct=0.9)
        ind_base = _base_indicators(price_in_8h_pct=0.5)
        assert MarketScanner.score_short(ind) > MarketScanner.score_short(ind_base)


class TestShortScoreTrendFilter:
    """Trend filter rules for SHORT."""

    def test_price_below_ema200_adds_1(self) -> None:
        ind = _base_indicators(price=90.0, ema200=100.0)
        ind_base = _base_indicators(price=100.0, ema200=100.0)
        assert MarketScanner.score_short(ind) > MarketScanner.score_short(ind_base)

    def test_trend_filter_zeroes_short_score(self) -> None:
        """Score → 0 when price > EMA200 AND RSI(1h) < 60."""
        ind = _base_indicators(
            price=110.0, ema200=100.0, rsi_1h=55.0,
            change_24h=12.0,  # would give +2
        )
        assert MarketScanner.score_short(ind) == 0

    def test_short_trend_filter_not_zeroed_when_rsi_high(self) -> None:
        """Score NOT zeroed when price > EMA200 but RSI(1h) ≥ 60."""
        ind = _base_indicators(
            price=110.0, ema200=100.0, rsi_1h=65.0,
            change_24h=12.0,
        )
        assert MarketScanner.score_short(ind) > 0


class TestShortScoreVolume:
    """Volume confirmation rule for SHORT."""

    def test_volume_spike_on_rise_adds_1(self) -> None:
        ind = _base_indicators(vol_ratio=2.0, change_24h=3.0)
        ind_base = _base_indicators(vol_ratio=1.0, change_24h=3.0)
        assert MarketScanner.score_short(ind) > MarketScanner.score_short(ind_base)


# ---------------------------------------------------------------------------
# Max score & direction
# ---------------------------------------------------------------------------

class TestMaxScore:
    """Score cap and direction selection."""

    def test_long_max_score_is_10(self) -> None:
        """LONG max score = 10 (all conditions met simultaneously)."""
        ind = _base_indicators(
            rsi_1h=25.0, rsi_15m=22.0,
            change_24h=-12.0, bb_pct=0.1, price_in_8h_pct=0.05,
            price=110.0, ema200=100.0, vol_ratio=2.0,
        )
        assert MarketScanner.score_long(ind) == 10  # max: 2+1+1+2+1+1+1+1

    def test_short_max_score_is_10(self) -> None:
        """SHORT max score = 10 (all conditions met simultaneously)."""
        ind = _base_indicators(
            rsi_1h=75.0, rsi_15m=78.0,
            change_24h=12.0, bb_pct=0.9, price_in_8h_pct=0.95,
            price=90.0, ema200=100.0, vol_ratio=2.0,
        )
        assert MarketScanner.score_short(ind) == 10  # max: 2+1+1+2+1+1+1+1

    def test_direction_long_when_long_higher(self) -> None:
        ind = _base_indicators(rsi_1h=25.0, change_24h=-12.0, bb_pct=0.1)
        long_s = MarketScanner.score_long(ind)
        short_s = MarketScanner.score_short(ind)
        assert long_s > short_s

    def test_direction_short_when_short_higher(self) -> None:
        ind = _base_indicators(rsi_1h=75.0, change_24h=12.0, bb_pct=0.9)
        long_s = MarketScanner.score_long(ind)
        short_s = MarketScanner.score_short(ind)
        assert short_s > long_s


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

class TestRanking:
    """Ranking, filtering, tie-breaking."""

    def test_score_below_3_filtered(self) -> None:
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 2, "rsi_extremity": 10.0, "volume_24h": 5_000_000},
        ]
        rankings, watchlist, filtered = MarketScanner.rank(scored)
        assert len(rankings) == 0
        assert len(watchlist) == 0
        assert filtered == 1

    def test_score_3_to_4_watchlist(self) -> None:
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 3, "rsi_extremity": 10.0, "volume_24h": 5_000_000},
        ]
        rankings, watchlist, filtered = MarketScanner.rank(scored)
        assert len(rankings) == 0
        assert len(watchlist) == 1

    def test_score_5_plus_ranked(self) -> None:
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 5, "rsi_extremity": 10.0, "volume_24h": 5_000_000},
        ]
        rankings, watchlist, filtered = MarketScanner.rank(scored)
        assert len(rankings) == 1
        assert len(watchlist) == 0

    def test_low_volume_filtered(self) -> None:
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 5, "rsi_extremity": 10.0, "volume_24h": 500_000},
        ]
        rankings, watchlist, filtered = MarketScanner.rank(scored)
        assert filtered == 1
        assert len(rankings) == 0

    def test_same_score_tiebreak_by_rsi_extremity(self) -> None:
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 5, "rsi_extremity": 10.0, "volume_24h": 5_000_000},
            {"symbol": "B", "direction": "LONG", "score": 5, "rsi_extremity": 20.0, "volume_24h": 5_000_000},
        ]
        rankings, _, _ = MarketScanner.rank(scored)
        assert rankings[0]["symbol"] == "B"  # RSI more extreme (closer to 0/100) ranks first

    def test_score_desc_primary_sort(self) -> None:
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 5, "rsi_extremity": 25.0, "volume_24h": 5_000_000},
            {"symbol": "B", "direction": "LONG", "score": 7, "rsi_extremity": 5.0, "volume_24h": 5_000_000},
        ]
        rankings, _, _ = MarketScanner.rank(scored)
        assert rankings[0]["symbol"] == "B"  # Higher score wins regardless of RSI


# ---------------------------------------------------------------------------
# Integration: scan() with MockExchange
# ---------------------------------------------------------------------------

class TestScanIntegration:
    """Full scan pipeline with MockExchange."""

    def test_scan_returns_scan_result(self) -> None:
        ex = MockExchange(seed_price=100.0)
        scanner = MarketScanner(ex)
        result = scanner.scan()
        assert isinstance(result, ScanResult)
        assert result.scan_time_ms >= 0

    def test_scan_produces_no_crash(self) -> None:
        """scan() completes without exception."""
        ex = MockExchange()
        scanner = MarketScanner(ex)
        result = scanner.scan()
        assert result.filtered_count >= 0

    def test_scan_with_top_n_5(self) -> None:
        """scan(top_n=5) only processes 5 symbols."""
        ex = MockExchange(seed_price=100.0)
        scanner = MarketScanner(ex)
        result = scanner.scan(top_n=5)
        # With 5 symbols, at most 5 in rankings + watchlist
        assert len(result.rankings) + len(result.watchlist) <= 5

    def test_scan_all_rankings_have_required_fields(self) -> None:
        ex = MockExchange(seed_price=100.0)
        scanner = MarketScanner(ex)
        result = scanner.scan()
        required = ["symbol", "direction", "score", "rsi_1h", "rsi_15m", "change_24h", "vol_ratio"]
        for r in result.rankings:
            for key in required:
                assert key in r, f"Missing {key} in ranking"
        for w in result.watchlist:
            for key in required:
                assert key in w, f"Missing {key} in watchlist"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge case handling."""

    def test_zero_volume_ticker(self) -> None:
        """Ticker with zero volume24h is handled gracefully."""
        ticker = {"symbol": "X", "last": 50.0, "volume24h": 0, "change24h": 0.0}
        kline_1h = _make_kline_1h([50.0] * 200)
        kline_15m = _make_kline_15m([50.0] * 20)
        result = MarketScanner.compute_indicators(ticker, kline_1h, kline_15m)
        assert result["volume_24h"] == 0.0

    def test_nan_in_kline_data(self) -> None:
        """NaN values in kline don't crash indicator computation."""
        close = [float("nan")] * 10 + [100.0] * 20
        df = _make_kline_1h(close)
        # Should not raise
        _rsi(df["close"], 14)

    def test_empty_scored_list(self) -> None:
        """rank() handles empty list."""
        rankings, watchlist, filtered = MarketScanner.rank([])
        assert rankings == []
        assert watchlist == []
        assert filtered == 0


# ---------------------------------------------------------------------------
# MockExchange get_tickers
# ---------------------------------------------------------------------------

class TestMockExchangeGetTickers:
    """MockExchange.get_tickers() contract."""

    def test_returns_list_of_20(self) -> None:
        ex = MockExchange()
        tickers = ex.get_tickers()
        assert len(tickers) == 20

    def test_returns_usdt_pairs_only(self) -> None:
        ex = MockExchange()
        tickers = ex.get_tickers()
        for t in tickers:
            assert t["symbol"].endswith("USDT")

    def test_sorted_by_volume_desc(self) -> None:
        ex = MockExchange()
        tickers = ex.get_tickers()
        vols = [t["volume24h"] for t in tickers]
        assert vols == sorted(vols, reverse=True)
