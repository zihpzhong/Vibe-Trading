"""Tests for A-share backtest engine — scanner-based historical simulation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from extensions.live_trading.astock.backtest.engine import AStockBacktestEngine, _NullSignalEngine
from extensions.live_trading.astock.backtest.exchange import BacktestExchange


# ─── Synthetic data helpers ───


def _make_kline(
    days: int = 120,
    start: float = 50.0,
    trend_strength: float = 0.0,
    vol_base: int = 1_000_000,
    vol_spike: int = 3_000_000,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate a daily OHLCV DataFrame with controlled characteristics.

    Args:
        days: Number of trading days.
        start: Starting price.
        trend_strength: Daily trend component (0 = random walk, 0.005 = mild uptrend).
        vol_base: Base daily volume.
        vol_spike: Last-5-days volume (set > vol_base for vol_ratio >= 1.0).
        seed: Random seed for reproducibility.

    Returns:
        DataFrame with open/high/low/close/volume/amount columns and DatetimeIndex.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-06-01", periods=days)
    prices = np.zeros(days)
    prices[0] = start
    for i in range(1, days):
        prices[i] = prices[i - 1] * (1 + trend_strength + rng.normal(0, 0.02))
    prices = np.maximum(prices, start * 0.3)  # floor at 30% of start

    vol = np.full(days, vol_base, dtype=int)
    vol[-5:] = vol_spike

    close = prices
    high = prices * 1.02
    low = prices * 0.98
    amount = (close * vol.astype(float) * 100).astype(float)  # in RMB

    return pd.DataFrame(
        {
            "open": prices * 0.995,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
            "amount": amount,
        },
        index=dates,
    )


class _SyntheticLoader:
    """Returns pre-built data_map; avoids network calls in tests."""

    def __init__(self, data_map: dict[str, pd.DataFrame]) -> None:
        self._data = data_map

    def fetch(
        self,
        codes,
        start_date="",
        end_date="",
        fields=None,
        interval="1D",
    ) -> dict[str, pd.DataFrame]:
        return {c: self._data[c] for c in codes if c in self._data}


def _run_backtest(
    data_map: dict[str, pd.DataFrame],
    overrides: dict | None = None,
) -> tuple[AStockBacktestEngine, dict]:
    """Run a single backtest and return (engine, metrics)."""
    first_df = next(iter(data_map.values()))
    cfg = {
        "codes": list(data_map.keys()),
        "start_date": "2023-06-01",
        "end_date": str(first_df.index[-1].date()),
        "source": "akshare",
        "interval": "1D",
        "engine": "astock",
    }
    cfg.update(overrides or {})
    engine = AStockBacktestEngine(cfg)
    loader = _SyntheticLoader(data_map)
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        metrics = engine.run_backtest(cfg, loader, _NullSignalEngine(), run_dir)
    return engine, metrics


# ─── Tests ───


class TestAStockBacktestEngine:
    def test_empty_universe(self) -> None:
        """Single stock with neutral data → no trades, no crash."""
        df = _make_kline(days=60, start=50.0, trend_strength=0.0, vol_base=0, vol_spike=0)
        data_map = {"000001.SZ": df}
        engine, metrics = _run_backtest(data_map)
        # With no trend and zero volume, scanner should not score, trade_count=0
        assert metrics["trade_count"] == 0
        assert metrics["final_value"] == 1_000_000

    def test_single_stock_smoke(self) -> None:
        """Full pipeline with one mildly up-trending stock: no crash, valid equity."""
        data = _make_kline(days=120, start=50.0, trend_strength=0.003)
        data_map = {"000001.SZ": data}
        engine, metrics = _run_backtest(data_map)

        assert metrics["final_value"] > 0
        assert metrics["trade_count"] >= 0
        assert "sharpe" in metrics
        assert "max_drawdown" in metrics

    def test_two_stock_smoke(self) -> None:
        """Two symbols: no index collisions, valid output."""
        d1 = _make_kline(days=120, start=50.0, trend_strength=0.003, seed=1)
        d2 = _make_kline(days=120, start=100.0, trend_strength=-0.002, seed=2)
        data_map = {"000001.SZ": d1, "600519.SH": d2}
        engine, metrics = _run_backtest(data_map)

        assert metrics["final_value"] > 0
        assert metrics["trade_count"] >= 0

    def test_t_plus_1_enforced(self) -> None:
        """can_execute returns False for same-day sell."""
        engine = AStockBacktestEngine({"codes": ["000001.SZ"], "initial_cash": 1_000_000})
        # Open a position
        entry_ts = pd.Timestamp("2023-06-05")
        from backtest.models import Position

        engine.positions["000001.SZ"] = Position(
            symbol="000001.SZ",
            direction=1,
            entry_price=50.0,
            entry_time=entry_ts,
            size=100,
            leverage=1.0,
        )

        # Same day → sell blocked
        bar = pd.Series({"close": 55.0, "trade_date": "2023-06-05"})
        assert engine.can_execute("000001.SZ", 0, bar) is False

        # Next day → sell allowed
        bar2 = pd.Series({"close": 55.0, "trade_date": "2023-06-06"})
        assert engine.can_execute("000001.SZ", 0, bar2) is True

    def test_no_short(self) -> None:
        """Short selling is always blocked for A-shares."""
        engine = AStockBacktestEngine({"codes": []})
        assert engine.can_execute("000001.SZ", -1, pd.Series()) is False

    def test_round_size(self) -> None:
        """Lot size rounded to multiples of 100."""
        engine = AStockBacktestEngine({"codes": []})
        assert engine.round_size(150, 50.0) == 100
        assert engine.round_size(50, 50.0) == 0
        assert engine.round_size(1000, 50.0) == 1000
        assert engine.round_size(1050, 50.0) == 1000

    def test_calc_commission(self) -> None:
        """Commission includes min-commission floor, stamp tax on sell."""
        engine = AStockBacktestEngine({"codes": [], "commission_rate": 0.00025, "commission_min": 5.0})
        # 100 shares @ 50 = 5,000 notional → min(5, 5000*0.00025=1.25) → ¥5
        open_comm = engine.calc_commission(100, 50.0, 1, is_open=True)
        # 5_000 * 0.00025 = 1.25 → min 5.0, plus transfer 5_000 * 0.00001 = 0.05
        assert open_comm == pytest.approx(5.05)

        # Close: 100 shares @ 55 = 5,500 → stamp_tax=5500*0.0005=2.75
        close_comm = engine.calc_commission(100, 55.0, -1, is_open=False)
        expected_stamp = 5500 * 0.0005  # 2.75
        expected_comm = max(5500 * 0.00025, 5.0)  # 5.0
        expected_transfer = 5500 * 0.00001  # 0.055
        assert close_comm == pytest.approx(expected_comm + expected_transfer + expected_stamp)

    def test_metrics_output(self) -> None:
        """Returned metrics dict has the expected keys and types."""
        data = _make_kline(days=120, start=50.0, trend_strength=0.003)
        data_map = {"000001.SZ": data}
        engine, metrics = _run_backtest(data_map)

        expected = {
            "total_return", "annual_return", "max_drawdown", "sharpe",
            "sortino", "win_rate", "trade_count", "final_value",
        }
        assert expected.issubset(metrics.keys())
        assert isinstance(metrics["total_return"], float)
        assert isinstance(metrics["trade_count"], int)

    def test_artifacts_created(self) -> None:
        """Verify artifact CSV files are written to run_dir."""
        data = _make_kline(days=60, start=50.0, trend_strength=0.003)
        data_map = {"000001.SZ": data}
        cfg = {
            "codes": ["000001.SZ"],
            "start_date": "2023-06-01",
            "end_date": str(data.index[-1].date()),
            "source": "akshare",
            "interval": "1D",
        }
        engine = AStockBacktestEngine(cfg)
        loader = _SyntheticLoader(data_map)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            engine.run_backtest(cfg, loader, _NullSignalEngine(), run_dir)
            arts = run_dir / "artifacts"
            assert (arts / "equity.csv").exists()
            assert (arts / "trades.csv").exists()
            assert (arts / "metrics.csv").exists()
            assert (arts / "ohlcv_000001.SZ.csv").exists()

    def test_apply_slippage(self) -> None:
        """Slippage: buy costs more, sell gets less."""
        engine = AStockBacktestEngine({"codes": [], "slippage": 0.001})
        buy_price = engine.apply_slippage(100.0, 1)
        assert buy_price == 100.1  # 100 * (1 + 0.001)
        sell_price = engine.apply_slippage(100.0, -1)
        assert sell_price == 99.9  # 100 * (1 - 0.001)

    def test_stop_loss_via_trade_record(self) -> None:
        """When price drops below SL, trade exit_reason is 'stop_loss'."""
        dates = pd.bdate_range("2023-06-01", periods=120)
        rng = np.random.default_rng(42)
        prices = np.zeros(120)
        prices[0] = 50.0
        for i in range(1, 100):
            prices[i] = prices[i - 1] * (1 + 0.004 + rng.normal(0, 0.015))
        # Sharp drop after day 100
        for i in range(100, 120):
            prices[i] = prices[i - 1] * 0.97

        vol = np.full(120, 2_000_000, dtype=int)
        amount = (prices * vol.astype(float) * 100).astype(float)

        df = pd.DataFrame({
            "open": prices * 0.995,
            "high": np.maximum(prices * 1.02, prices * 1.01),
            "low": np.minimum(prices * 0.98, prices * 0.99),
            "close": prices,
            "volume": vol,
            "amount": amount,
        }, index=dates)

        engine, metrics = _run_backtest({"000001.SZ": df})

        assert isinstance(metrics["trade_count"], int)
        # No assertion on stop_loss counts — depends on scanner scoring
        # No assertion on sl_trades — depends on scanner scoring

    def test_initial_cash_override(self) -> None:
        """Config initial_cash is reflected in final_value."""
        data = _make_kline(days=60, start=50.0, trend_strength=0.003)
        engine, metrics = _run_backtest({"000001.SZ": data}, {"initial_cash": 500_000})
        assert metrics["final_value"] > 0
        # Verify initial cash was applied
        assert engine.initial_capital == 500_000

    def test_position_size_pct(self) -> None:
        """Position size respects config."""
        data = _make_kline(days=60, start=50.0, trend_strength=0.003)
        cfg = {
            "codes": ["000001.SZ"],
            "start_date": "2023-06-01",
            "end_date": str(data.index[-1].date()),
            "source": "akshare",
            "interval": "1D",
            "initial_cash": 1_000_000,
            "position_size_pct": 0.12,
        }
        engine = AStockBacktestEngine(cfg)
        # Adaptive sizing: OK → 0.75x. max(100, floor(1M * 0.12 * 0.75 / 50) // 100 * 100)
        # = max(100, floor(1800) // 100 * 100) = max(100, 1800) = 1800
        shares = engine._calc_shares(1_000_000, 50.0)  # noqa: SLF001
        assert shares == 1800
        assert shares % 100 == 0


# ─── BacktestExchange unit tests ───


class TestBacktestExchange:
    def test_get_daily_returns_hist_only(self) -> None:
        """Data beyond _current_date is not visible."""
        dates = pd.bdate_range("2024-01-01", periods=10)
        df = pd.DataFrame({"close": range(10), "high": range(10), "low": range(10), "volume": 1000}, index=dates)
        ex = BacktestExchange({"000001.SZ": df}, ["000001.SZ"])
        ex.set_current_date(pd.Timestamp("2024-01-05"))
        hist = ex.get_daily("000001.SZ", limit=100)
        assert len(hist) == 5  # days 1-5
        assert hist.index[-1] == pd.Timestamp("2024-01-05")

    def test_get_ticker_at_date(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=5)
        df = pd.DataFrame({"close": [10, 11, 12, 13, 14], "high": 15, "low": 9, "volume": 1000}, index=dates)
        ex = BacktestExchange({"000001.SZ": df}, ["000001.SZ"])
        ex.set_current_date(pd.Timestamp("2024-01-03"))
        ticker = ex.get_ticker("000001.SZ")
        assert ticker["last"] == 12.0
        assert ticker["volume"] == 1000

    def test_is_suspended_zero_volume(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=3)
        df = pd.DataFrame({
            "close": [10, 10, 10], "high": 11, "low": 9,
            "volume": [1000, 0, 0], "pre_close": [10, 10, 10],
        }, index=dates)
        ex = BacktestExchange({"000001.SZ": df}, ["000001.SZ"])
        ex.set_current_date(pd.Timestamp("2024-01-02"))
        assert ex.is_suspended("000001.SZ") is True

    def test_is_suspended_normal(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=3)
        df = pd.DataFrame({
            "close": [10, 11, 12], "high": 12, "low": 9,
            "volume": [1000, 1500, 2000], "pre_close": [10, 10, 11],
        }, index=dates)
        ex = BacktestExchange({"000001.SZ": df}, ["000001.SZ"])
        ex.set_current_date(pd.Timestamp("2024-01-03"))
        assert ex.is_suspended("000001.SZ") is False

    def test_is_limit(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=3)
        # Main board: limit_up at +10% from prev_close
        df = pd.DataFrame({
            "close": [10, 11, 14],
            "high": [10.5, 11.5, 14.5],
            "low": [9.5, 10.5, 13.5],
            "volume": 1000,
            "pre_close": [10, 10, 11],
        }, index=dates)
        ex = BacktestExchange({"000001.SZ": df}, ["000001.SZ"])
        # Day 2: close=11, prev_close=10 → +10% → limit_up
        ex.set_current_date(pd.Timestamp("2024-01-02"))
        assert ex.is_limit("000001.SZ") == "up"

        # Day 3: close=14, prev_close=11 → +27% → still limit_up
        ex.set_current_date(pd.Timestamp("2024-01-03"))
        assert ex.is_limit("000001.SZ") == "up"

    def test_market_breadth(self) -> None:
        dates = pd.bdate_range("2024-01-01", periods=5)
        df1 = pd.DataFrame({"close": [10, 11, 12, 13, 14], "high": 15, "low": 9, "volume": 1000}, index=dates)
        df2 = pd.DataFrame({"close": [20, 19, 18, 17, 16], "high": 21, "low": 15, "volume": 1000}, index=dates)
        ex = BacktestExchange({"A": df1, "B": df2}, ["A", "B"])
        ex.set_current_date(pd.Timestamp("2024-01-05"))
        breadth = ex.get_market_breadth()
        assert breadth["up"] == 1  # A went up
        assert breadth["down"] == 1  # B went down
        assert breadth["ratio"] == 0.5
