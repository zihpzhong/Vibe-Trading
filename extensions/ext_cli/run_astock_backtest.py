"""Run AStockBacktestEngine with real A-share historical data via akshare.

Usage:
    cd /Users/uncless/workspace/python-code/Vibe-Trading && .venv/bin/python extensions/ext_cli/run_astock_backtest.py

Demonstrates the full scanner-based backtest pipeline on actual market data:
  akshare fetch → BacktestExchange → per-bar scanner evaluation → gate → TPSL → metrics

Data sources (tried in order):
  1. CSV cache on disk (fastest)
  2. akshare (free, no token)
  3. Built-in synthetic data (when APIs are unavailable)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path so 'extensions' package resolves
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import tempfile

import numpy as np
import pandas as pd

from extensions.trading.astock.backtest.engine import (
    AStockBacktestEngine,
    _NullSignalEngine,
)


# ── Config ──────────────────────────────────────────────────────────────────────

CODES = [
    # 消费 (Consumer staples)
    "600519.SH",  # 贵州茅台 — baijiu
    "000858.SZ",  # 五粮液 — baijiu
    "000568.SZ",  # 泸州老窖 — baijiu
    "002304.SZ",  # 洋河股份 — baijiu
    "600887.SH",  # 伊利股份 — dairy
    # 金融 (Financials)
    "601318.SH",  # 中国平安 — insurance
    "600036.SH",  # 招商银行 — banking
    "000001.SZ",  # 平安银行 — banking
    "601398.SH",  # 工商银行 — banking
    "600030.SH",  # 中信证券 — brokerage
    # 科技 (Tech)
    "000725.SZ",  # 京东方A — display
    "002415.SZ",  # 海康威视 — security cameras
    "688981.SH",  # 中芯国际 — semiconductors
    # 新能源 (New Energy)
    "300750.SZ",  # 宁德时代 — EV batteries
    "002594.SZ",  # 比亚迪 — EV / auto
    # 医药 (Healthcare)
    "600276.SH",  # 恒瑞医药 — pharma
    "300760.SZ",  # 迈瑞医疗 — medical devices
    # 家电 (Home Appliances)
    "000333.SZ",  # 美的集团 — appliances
    "000651.SZ",  # 格力电器 — AC
    # 地产 (Real Estate)
    "000002.SZ",  # 万科A — property
    # 通信 (Telecom)
    "600941.SH",  # 中国移动 — telecom
]

START_DATE = "2023-06-01"
END_DATE = "2026-05-21"
INITIAL_CASH = 1_000_000

CACHE_DIR = Path(__file__).resolve().parent / ".cache_astock"


def build_config() -> dict[str, Any]:
    return {
        "codes": CODES,
        "start_date": START_DATE,
        "end_date": END_DATE,
        "source": "akshare",
        "interval": "1D",
        "engine": "astock",
        "initial_cash": INITIAL_CASH,
        # Optimized parameters (from grid search, 2024-06 → 2025-04):
        # Baseline: +8.04% → Optimized B2: +18.64%
        "scan_top_n": 20,
        "position_size_pct": 0.15,
        "hard_stop_loss_pct": 10.0,
        "default_reward_risk": 3.0,
        "atr_multiplier": 2.0,
        "commission_rate": 0.00025,
        "commission_min": 5.0,
        "stamp_tax": 0.0005,
        "transfer_fee": 0.00001,
        "slippage": 0.001,
        "market_index": "000300.SH",
        "lot_size": 100,
    }


# ── Synthetic fallback data ─────────────────────────────────────────────────────


def _synthetic_kline(
    days: int,
    start: float,
    trend: float,
    vol: int,
    seed: int,
) -> pd.DataFrame:
    """Generate realistic OHLCV for a single A-share stock."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-06-03", periods=days)
    prices = np.zeros(days)
    prices[0] = start
    for i in range(1, days):
        prices[i] = prices[i - 1] * (1 + trend + rng.normal(0, 0.025))
    prices = np.maximum(prices, start * 0.3)

    volume = rng.poisson(vol, days).astype(int)

    return pd.DataFrame({
        "open": prices * 0.995,
        "high": prices * np.maximum(1.015, 1 + rng.uniform(0, 0.03, days)),
        "low": prices * np.minimum(0.985, 1 - rng.uniform(0, 0.03, days)),
        "close": prices,
        "volume": volume,
        "amount": prices * volume.astype(float) * 100,
    }, index=dates)


# ── Cached fetch ────────────────────────────────────────────────────────────────


def _fetch_data(use_cache_only: bool = False) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Load data: CSV cache → akshare → synthetic fallback.

    Returns:
        (data_map, source_map) — data_map: code->DataFrame, source_map: code->"real"/"synthetic"
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data_map: dict[str, pd.DataFrame] = {}
    source_map: dict[str, str] = {}
    cache_hits = 0

    # 1) Load from CSV cache
    for code in CODES:
        cache_path = CACHE_DIR / f"{code.replace('.', '_')}.csv"
        if cache_path.exists():
            df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
            df.index = pd.DatetimeIndex(df.index)
            data_map[code] = df
            cache_hits += 1
            source_map[code] = "real"

    if len(data_map) == len(CODES):
        print(f"  (loaded {cache_hits} stocks from CSV cache)")
        return data_map, source_map

    if use_cache_only:
        print(f"  (cache-only: {len(data_map)}/{len(CODES)} stocks cached, missing: {[c for c in CODES if c not in data_map]})")
        return data_map, source_map

    # 2) Try tushare first (token may provide access)
    missing = [c for c in CODES if c not in data_map]
    if missing:
        try:
            from backtest.loaders.tushare import DataLoader as TuShareLoader
            tu_loader = TuShareLoader()
            if tu_loader.is_available():
                print(f"  fetching {len(missing)} stocks from tushare...")
                for code in missing:
                    try:
                        raw = tu_loader.fetch([code], START_DATE, END_DATE, interval="1D")
                        if raw and code in raw:
                            df = raw[code]
                            df.to_csv(CACHE_DIR / f"{code.replace('.', '_')}.csv")
                            data_map[code] = df
                            source_map[code] = "real"
                            print(f"    {code}: {len(df)} bars")
                            continue
                    except Exception as exc:
                        print(f"    {code}: tushare failed ({exc})")
                    time.sleep(0.5)
        except Exception as exc:
            print(f"  tushare not available: {exc}")

    # 3) Try akshare (Sina-based API for A-shares)
    missing = [c for c in CODES if c not in data_map]
    if missing:
        print(f"  fetching {len(missing)} stocks via akshare...")
        import akshare as ak
        for code in missing:
            sym = code.split(".")[0]
            sh_sz = "sh" if code.endswith(".SH") else "sz"
            symbol = f"{sh_sz}{sym}"
            for attempt in range(2):
                try:
                    df = ak.stock_zh_a_daily(
                        symbol=symbol,
                        start_date=START_DATE,
                        end_date=END_DATE,
                        adjust="qfq",
                    )
                    if df is not None and not df.empty:
                        df = df.rename(columns={"date": "trade_date"})
                        df["trade_date"] = pd.to_datetime(df["trade_date"])
                        df = df.set_index("trade_date").sort_index()
                        df = df[["open", "high", "low", "close", "volume"]]
                        df.to_csv(CACHE_DIR / f"{code.replace('.', '_')}.csv")
                        data_map[code] = df
                        source_map[code] = "real"
                        print(f"    {code}: {len(df)} bars (Sina)")
                        break
                except Exception as exc:
                    if attempt > 0:
                        print(f"    {code}: Sina API unavailable ({exc})")
                    time.sleep(2)
            time.sleep(0.5)

    # 4) Fill remaining with synthetic data
    missing = [c for c in CODES if c not in data_map]
    if missing:
        print(f"  generating synthetic data for {len(missing)} stocks (API unavailable)...")
        start_prices = {
            "600519.SH": 1500.0, "000858.SZ": 120.0, "000568.SZ": 130.0, "002304.SZ": 80.0,
            "600887.SH": 25.0,
            "601318.SH": 40.0, "600036.SH": 30.0, "000001.SZ": 10.0, "601398.SH": 5.0,
            "600030.SH": 20.0,
            "000725.SZ": 4.0, "002415.SZ": 30.0, "688981.SH": 50.0,
            "300750.SZ": 180.0, "002594.SZ": 200.0,
            "600276.SH": 40.0, "300760.SZ": 280.0,
            "000333.SZ": 60.0, "000651.SZ": 35.0,
            "000002.SZ": 8.0,
            "600941.SH": 100.0,
        }
        trends = {
            "600519.SH": -0.0005, "000858.SZ": -0.0003, "000568.SZ": -0.0002, "002304.SZ": 0.0,
            "600887.SH": 0.0008,
            "601318.SH": 0.0002, "600036.SH": 0.0005, "000001.SZ": 0.001, "601398.SH": 0.001,
            "600030.SH": 0.0015,
            "000725.SZ": 0.002, "002415.SZ": 0.001, "688981.SH": 0.003,
            "300750.SZ": 0.002, "002594.SZ": 0.004,
            "600276.SH": 0.0015, "300760.SZ": 0.002,
            "000333.SZ": 0.001, "000651.SZ": 0.0005,
            "000002.SZ": -0.002,
            "600941.SH": 0.001,
        }

        for i, code in enumerate(missing):
            df = _synthetic_kline(
                days=222,
                start=start_prices.get(code, 50.0),
                trend=trends.get(code, 0.0),
                vol=int(2_000_000 * (1 + i * 0.1)),
                seed=i + 100,
            )
            df.to_csv(CACHE_DIR / f"{code.replace('.', '_')}.csv")
            data_map[code] = df
            source_map[code] = "synthetic"
            print(f"    {code}: {len(df)} bars (synthetic)")

    return data_map, source_map


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AStockBacktestEngine — Real Data Demo")
    parser.add_argument("--refresh", action="store_true", help="Force re-fetch from akshare")
    args = parser.parse_args()

    cfg = build_config()

    print("AStockBacktestEngine — Real Data Demo")
    print(f"{'=' * 60}")
    print(f"Stocks: {', '.join(CODES)}")
    print(f"Period: {START_DATE} → {END_DATE}")
    print(f"Capital: ¥{INITIAL_CASH:,.0f}")
    print(f"{'=' * 60}\n")

    # Step 1 — Load data
    print("[1/5] Loading data...")
    if args.refresh:
        for p in CACHE_DIR.glob("*.csv"):
            p.unlink()
        print("  cache cleared")
    data_map, source_map = _fetch_data()

    if not data_map:
        print("ERROR: No data available.")
        return

    for code, df in data_map.items():
        src = source_map.get(code, "?")
        print(f"  {code}: {len(df)} bars, {df.index[0].date()} → {df.index[-1].date()} [{src}]")

    # Step 2 — Preloaded loader
    print("\n[2/5] Initializing AStockBacktestEngine...")

    class _PreloadedLoader:
        def __init__(self, data: dict[str, pd.DataFrame]) -> None:
            self._data = data
        def fetch(self, codes, start_date="", end_date="", fields=None, interval="1D"):
            return {c: self._data[c] for c in codes if c in self._data}

    engine = AStockBacktestEngine(cfg)
    signal_engine = _NullSignalEngine()
    loader = _PreloadedLoader(data_map)

    # Step 3 — Run backtest
    print("[3/5] Running backtest (scanner-based pipeline)...")
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        metrics = engine.run_backtest(cfg, loader, signal_engine, run_dir)

    # Step 4 — Results
    print("\n[4/5] Results:")
    print(f"{'─' * 60}")
    print(f"  Final Portfolio Value:  ¥{metrics['final_value']:>12,.2f}")
    print(f"  Total Return:           {metrics['total_return']:>11.2%}")
    print(f"  Annual Return:          {metrics['annual_return']:>11.2%}")
    print(f"  Max Drawdown:           {metrics['max_drawdown']:>11.2%}")
    print(f"  Sharpe Ratio:           {metrics['sharpe']:>11.4f}")
    print(f"  Sortino Ratio:          {metrics['sortino']:>11.4f}")
    print(f"  Win Rate:               {metrics['win_rate']:>11.2%}")
    print(f"  Trade Count:            {metrics['trade_count']:>11d}")
    print(f"  Benchmark Return:       {metrics.get('benchmark_return', 0):>11.2%}")
    print(f"  Excess Return:          {metrics.get('excess_return', 0):>11.2%}")
    print(f"  Profit Factor:          {metrics.get('profit_factor', 0):>11.4f}")
    print(f"  Avg Holding Days:       {metrics.get('avg_holding_days', 0):>11.1f}")
    print(f"{'─' * 60}")

    # Step 5 — Per-symbol breakdown
    from backtest.metrics import by_symbol_stats, by_exit_reason_stats

    print("\n[5/5] Detailed breakdown:")
    print(f"{'─' * 60}")

    sym_stats = by_symbol_stats(engine.trades)
    if sym_stats:
        print("  By symbol:")
        for sym, stats in sorted(sym_stats.items()):
            print(f"    {sym:12s}  trades={stats['count']:>2d}  "
                  f"win={stats['win_rate']:>5.1%}  "
                  f"pnl=¥{stats['total_pnl']:>+9,.0f}  "
                  f"avg=¥{stats['avg_pnl']:>+8,.0f}")
    else:
        print("  By symbol: (no closed trades)")

    if engine.trades:
        print("\n  By exit reason:")
        exit_stats = by_exit_reason_stats(engine.trades)
        for reason, stats in sorted(exit_stats.items()):
            print(f"    {reason:20s}  {stats['count']:>2d} trades  "
                  f"win={stats.get('win_rate', 0):>5.1%}  "
                  f"pnl=¥{stats.get('total_pnl', 0):>+8,.0f}")

    print(f"\n{'=' * 60}")
    print("Backtest complete.")


if __name__ == "__main__":
    main()
