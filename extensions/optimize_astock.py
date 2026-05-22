"""AStock parameter optimization via grid search.

Runs multiple backtests across a parameter grid, caches results,
and reports the top configurations ranked by total_return.

Usage:
    cd /Users/uncless/workspace/python-code/Vibe-Trading && .venv/bin/python extensions/optimize_astock.py

    1D sweeps (recommended first):
        python extensions/optimize_astock.py --sweep position_size_pct
        python extensions/optimize_astock.py --sweep scan_top_n
        python extensions/optimize_astock.py --sweep atr_multiplier
        python extensions/optimize_astock.py --sweep hard_stop_loss_pct
        python extensions/optimize_astock.py --sweep reward_risk
        python extensions/optimize_astock.py --sweep min_score

    Combined grid (small subset):
        python extensions/optimize_astock.py --grid quick

    Full grid search (may take hours):
        python extensions/optimize_astock.py --grid full

    Re-run from cache only:
        python extensions/optimize_astock.py --cache-only
"""

from __future__ import annotations

import sys
import time
import itertools
import csv
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import tempfile

import pandas as pd

from extensions.live_trading.astock.backtest.engine import (
    AStockBacktestEngine,
    _NullSignalEngine,
)
from extensions.run_astock_backtest import (
    CODES,
    START_DATE,
    END_DATE,
    INITIAL_CASH,
    CACHE_DIR,
    _fetch_data,
    build_config as _build_base_config,
)

# ── Result cache ──────────────────────────────────────────────────────────────

RESULTS_FILE = Path(__file__).resolve().parent / ".cache_astock" / "optimization_results.csv"


def _load_cache() -> list[dict[str, Any]]:
    """Load previously cached results."""
    if not RESULTS_FILE.exists():
        return []
    df = pd.read_csv(RESULTS_FILE)
    return df.to_dict("records")


def _save_results(results: list[dict[str, Any]]) -> None:
    """Append new results to cache CSV."""
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if RESULTS_FILE.exists() else "w"
    header = not RESULTS_FILE.exists()
    with open(RESULTS_FILE, mode, newline="") as f:
        if not results:
            return
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        if header:
            writer.writeheader()
        writer.writerows(results)


# ── Parameter sweeps ─────────────────────────────────────────────────────────

SWEEPS = {
    "position_size_pct": {
        "param": "position_size_pct",
        "values": [0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25],
        "label": "Position size (% of capital)",
    },
    "scan_top_n": {
        "param": "scan_top_n",
        "values": [5, 8, 10, 12, 15, 20, 25, 30, 40],
        "label": "Scan top N symbols",
    },
    "atr_multiplier": {
        "param": "atr_multiplier",
        "values": [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
        "label": "ATR multiplier (stop distance)",
    },
    "hard_stop_loss_pct": {
        "param": "hard_stop_loss_pct",
        "values": [3.0, 5.0, 7.0, 10.0, 12.0, 15.0],
        "label": "Hard stop loss %",
    },
    "reward_risk": {
        "param": "default_reward_risk",
        "values": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
        "label": "Reward:risk ratio (take profit)",
    },
    "min_score": {
        "param": "scan_entry_threshold",
        "values": [3, 4, 5, 6, 7],
        "label": "Scanner entry threshold (min score)",
    },
    "slippage": {
        "param": "slippage",
        "values": [0.0005, 0.001, 0.002, 0.003, 0.005],
        "label": "Slippage",
    },
    "trail_activation": {
        "param": "trail_activation_pct",
        "values": [1.0, 2.0, 3.0, 5.0, 8.0],
        "label": "Trailing activation %",
    },
    "trail_distance": {
        "param": "trail_distance_pct",
        "values": [3.0, 5.0, 8.0, 10.0, 12.0],
        "label": "Trailing distance %",
    },
    "max_holding": {
        "param": "max_holding_days",
        "values": [30, 45, 60, 90, 120],
        "label": "Max holding days",
    },
}

# Quick combined grid — best values from each 1D sweep
QUICK_GRID = {
    "position_size_pct": [0.08, 0.10, 0.12, 0.15],
    "scan_top_n": [10, 15, 20, 25],
    "atr_multiplier": [1.5, 2.0, 2.5, 3.0],
    "hard_stop_loss_pct": [5.0, 7.0, 10.0],
    "default_reward_risk": [1.5, 2.0, 3.0],
}

FULL_GRID = {
    "position_size_pct": [0.05, 0.08, 0.10, 0.12, 0.15, 0.20],
    "scan_top_n": [5, 10, 15, 20, 25, 30],
    "atr_multiplier": [1.5, 2.0, 2.5, 3.0, 3.5],
    "hard_stop_loss_pct": [5.0, 7.0, 10.0, 15.0],
    "default_reward_risk": [1.5, 2.0, 2.5, 3.0],
}


# ── Parameter key helpers ────────────────────────────────────────────────────


def _param_key(params: dict[str, Any]) -> str:
    """Deterministic key for dedup."""
    return ";".join(f"{k}={v}" for k, v in sorted(params.items()))


def _cached_keys(results: list[dict]) -> set[str]:
    return {_param_key(r) for r in results}


# ── Run a single backtest ────────────────────────────────────────────────────


def run_single(
    overrides: dict[str, Any],
    data_map: dict[str, pd.DataFrame],
    loader: Any,
    signal_engine: Any,
) -> dict[str, Any]:
    """Run one backtest with parameter overrides and return metrics."""
    cfg = _build_base_config()
    cfg.update(overrides)
    engine = AStockBacktestEngine(cfg)
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        metrics = engine.run_backtest(cfg, loader, signal_engine, run_dir)

    return {
        "total_return": metrics.get("total_return", 0),
        "annual_return": metrics.get("annual_return", 0),
        "max_drawdown": metrics.get("max_drawdown", 0),
        "sharpe": metrics.get("sharpe", 0),
        "sortino": metrics.get("sortino", 0),
        "win_rate": metrics.get("win_rate", 0),
        "trade_count": metrics.get("trade_count", 0),
        "final_value": metrics.get("final_value", 0),
        "profit_factor": metrics.get("profit_factor", 0),
        "avg_holding_days": metrics.get("avg_holding_days", 0),
        "benchmark_return": metrics.get("benchmark_return", 0),
    }


# ── 1D sweep ─────────────────────────────────────────────────────────────────


def run_sweep(
    sweep_name: str,
    data_map: dict[str, pd.DataFrame],
    loader: Any,
    signal_engine: Any,
    cache: list[dict],
) -> list[dict]:
    """Run a 1D parameter sweep."""
    sweep = SWEEPS.get(sweep_name)
    if sweep is None:
        print(f"Unknown sweep: {sweep_name}")
        print(f"Available sweeps: {', '.join(SWEEPS.keys())}")
        return []

    param = sweep["param"]
    values = sweep["values"]
    label = sweep["label"]

    cached_keys = _cached_keys(cache)

    print(f"\n{'=' * 70}")
    print(f"Sweep: {label}")
    print(f"{'=' * 70}")

    new_results = []
    for val in values:
        params = {param: val}
        key = _param_key(params)
        if key in cached_keys:
            print(f"  [{param}={val}] cached, skipping")
            continue

        print(f"  [{param}={val}] running...", end=" ", flush=True)
        t0 = time.time()

        try:
            metrics = run_single(params, data_map, loader, signal_engine)
            elapsed = time.time() - t0
            result = {"param": param, "value": val, **metrics}
            new_results.append(result)
            print(f"ret={metrics['total_return']:+.2%}  "
                  f"sharpe={metrics['sharpe']:.2f}  "
                  f"dd={metrics['max_drawdown']:.2%}  "
                  f"wr={metrics['win_rate']:.1%}  "
                  f"trades={metrics['trade_count']}  "
                  f"({elapsed:.1f}s)")
        except Exception as exc:
            elapsed = time.time() - t0
            print(f"FAILED ({elapsed:.1f}s): {exc}")

    if new_results:
        _save_results(new_results)

    # Display results
    all_results = cache + new_results
    sweep_results = [r for r in all_results if r.get("param") == param]
    if not sweep_results:
        return new_results

    sweep_results.sort(key=lambda r: r.get("total_return", 0), reverse=True)

    # Find baseline for excess return comparison
    baseline_ret = None
    for r in all_results:
        if r.get("param") == "baseline":
            baseline_ret = r.get("total_return", 0)
            break

    print(f"\n  {'─' * 86}")
    print(f"  Top results for {label}:")
    print(f"  {'Value':>10s}  {'Return':>8s}  {'ExcessRet':>10s}  "
          f"{'Sharpe':>8s}  {'MaxDD':>8s}  "
          f"{'WinRate':>8s}  {'Trades':>6s}")
    print(f"  {'─' * 86}")
    for r in sweep_results:
        ret = r.get("total_return", 0)
        excess = ret - baseline_ret if baseline_ret is not None else 0.0
        print(f"  {str(r.get('value', '?')):>10s}  "
              f"{ret:+7.2%}  "
              f"{excess:+9.2%}  "
              f"{r.get('sharpe', 0):>7.2f}  "
              f"{r.get('max_drawdown', 0):>7.2%}  "
              f"{r.get('win_rate', 0):>7.1%}  "
              f"{r.get('trade_count', 0):>5d}")

    best = sweep_results[0]
    print(f"  {'─' * 70}")
    print(f"  Best: {param}={best.get('value')} → "
          f"return={best.get('total_return', 0):+.2%}  "
          f"sharpe={best.get('sharpe', 0):.2f}")

    return new_results


# ── Combined grid ────────────────────────────────────────────────────────────


def run_grid(
    grid_type: str,
    data_map: dict[str, pd.DataFrame],
    loader: Any,
    signal_engine: Any,
    cache: list[dict],
) -> list[dict]:
    """Run a multi-parameter grid search."""
    grid = QUICK_GRID if grid_type == "quick" else FULL_GRID
    param_names = list(grid.keys())
    value_lists = [grid[p] for p in param_names]

    total_combos = 1
    for v in value_lists:
        total_combos *= len(v)

    label = f"{grid_type.upper()} grid ({total_combos} combinations)"

    print(f"\n{'=' * 70}")
    print(f"Grid search: {label}")
    print(f"  Parameters: {param_names}")
    print(f"  Total combos: {total_combos}")
    print(f"{'=' * 70}")

    cached_keys = _cached_keys(cache)
    new_results = []
    completed = 0

    for values in itertools.product(*value_lists):
        params = dict(zip(param_names, values))
        key = _param_key(params)
        if key in cached_keys:
            completed += 1
            continue

        print(f"  [{completed + 1}/{total_combos}] {key} ...", end=" ", flush=True)
        t0 = time.time()

        try:
            metrics = run_single(params, data_map, loader, signal_engine)
            elapsed = time.time() - t0
            result = {"param": "grid", "value": key, **metrics}
            new_results.append(result)
            print(f"ret={metrics['total_return']:+.2%}  "
                  f"sharpe={metrics['sharpe']:.2f}  "
                  f"dd={metrics['max_drawdown']:.2%}  "
                  f"({elapsed:.1f}s)")
        except Exception as exc:
            elapsed = time.time() - t0
            print(f"FAILED ({elapsed:.1f}s): {exc}")

        completed += 1

    if new_results:
        _save_results(new_results)

    # Top-N display
    all_results = cache + new_results
    grid_results = [r for r in all_results if r.get("param") == "grid"]
    if not grid_results:
        # Also check for individual param results that are part of this grid
        # Actually just take all non-sweep results
        pass

    grid_results.sort(key=lambda r: r.get("total_return", 0), reverse=True)
    top_n = min(15, len(grid_results))

    if top_n > 0:
        print(f"\n  {'─' * 70}")
        print(f"  Top {top_n} configurations:")
        print(f"  {'Config':>40s}  {'Return':>8s}  {'Sharpe':>8s}  "
              f"{'MaxDD':>8s}  {'WinRate':>8s}  {'Trades':>6s}")
        print(f"  {'─' * 70}")
        for i, r in enumerate(grid_results[:top_n]):
            val = str(r.get("value", "")).replace(";", ", ")
            print(f"  {i + 1:2d}. {val:40s}  "
                  f"{r.get('total_return', 0):+7.2%}  "
                  f"{r.get('sharpe', 0):>7.2f}  "
                  f"{r.get('max_drawdown', 0):>7.2%}  "
                  f"{r.get('win_rate', 0):>7.1%}  "
                  f"{r.get('trade_count', 0):>5d}")

        best = grid_results[0]
        print(f"\n  ╔══ BEST ═══════════════════════════════════════════════════╗")
        print(f"  ║  {best.get('value', 'N/A')}")
        print(f"  ║")
        print(f"  ║  total_return={best.get('total_return', 0):+.2%}")
        print(f"  ║  sharpe={best.get('sharpe', 0):.2f}")
        print(f"  ║  max_drawdown={best.get('max_drawdown', 0):.2%}")
        print(f"  ║  win_rate={best.get('win_rate', 0):.1%}")
        print(f"  ║  trade_count={best.get('trade_count', 0)}")
        print(f"  ║  profit_factor={best.get('profit_factor', 0):.2f}")
        print(f"  ╚════════════════════════════════════════════════════════════╝")

    return new_results


# ── Show best from cache ────────────────────────────────────────────────────


def show_best(cache: list[dict], top_n: int = 15) -> None:
    """Display top-N results from cache across all params."""
    if not cache:
        print("No cached results.")
        return

    baseline_ret = None
    for r in cache:
        if r.get("param") == "baseline":
            baseline_ret = r.get("total_return", 0)
            break

    by_return = sorted(cache, key=lambda r: r.get("total_return", 0), reverse=True)
    by_sharpe = sorted(cache, key=lambda r: r.get("sharpe", 0), reverse=True)

    print(f"\n{'=' * 86}")
    print(f"All-Time Best — Ranked by Total Return")
    print(f"{'=' * 86}")
    print(f"  {'Param':>18s}  {'Value':>15s}  {'Return':>8s}  {'vsBase':>9s}  "
          f"{'Sharpe':>8s}  {'MaxDD':>8s}  {'Trades':>5s}")
    print(f"  {'─' * 86}")
    for r in by_return[:top_n]:
        ret = r.get("total_return", 0)
        excess = ret - baseline_ret if baseline_ret is not None else 0.0
        print(f"  {str(r.get('param', '?')):>18s}  {str(r.get('value', '')):>15s}  "
              f"{ret:+7.2%}  "
              f"{excess:+8.2%}  "
              f"{r.get('sharpe', 0):>7.2f}  "
              f"{r.get('max_drawdown', 0):>7.2%}  "
              f"{r.get('trade_count', 0):>5d}")

    print(f"\n{'─' * 86}")
    print(f"All-Time Best — Ranked by Sharpe")
    print(f"{'─' * 86}")
    print(f"  {'Param':>18s}  {'Value':>15s}  {'Sharpe':>8s}  {'Return':>8s}  "
          f"{'vsBase':>9s}  {'MaxDD':>8s}  {'Trades':>5s}")
    print(f"  {'─' * 86}")
    for r in by_sharpe[:top_n]:
        ret = r.get("total_return", 0)
        excess = ret - baseline_ret if baseline_ret is not None else 0.0
        print(f"  {str(r.get('param', '?')):>18s}  {str(r.get('value', '')):>15s}  "
              f"{r.get('sharpe', 0):>7.2f}  "
              f"{ret:+7.2%}  "
              f"{excess:+8.2%}  "
              f"{r.get('max_drawdown', 0):>7.2%}  "
              f"{r.get('trade_count', 0):>5d}")

    # Baseline reference
    baseline = [r for r in cache if r.get("param") == "baseline"]
    if baseline:
        b = baseline[0]
        print(f"\n{'─' * 86}")
        print(f"Baseline Reference:")
        print(f"  return={b.get('total_return', 0):+.2%}  "
              f"sharpe={b.get('sharpe', 0):.2f}  "
              f"dd={b.get('max_drawdown', 0):.2%}  "
              f"wr={b.get('win_rate', 0):.1%}  "
              f"trades={b.get('trade_count', 0)}")


# ── Run baseline ─────────────────────────────────────────────────────────────


def run_baseline(
    data_map: dict[str, pd.DataFrame],
    loader: Any,
    signal_engine: Any,
    cache: list[dict],
) -> dict[str, Any]:
    """Run baseline config and cache if not already present."""
    for r in cache:
        if r.get("param") == "baseline":
            return r

    print("  [baseline] running...", end=" ", flush=True)
    t0 = time.time()
    metrics = run_single({}, data_map, loader, signal_engine)
    elapsed = time.time() - t0
    result = {"param": "baseline", "value": "default", **metrics}
    _save_results([result])
    print(f"ret={metrics['total_return']:+.2%}  "
          f"sharpe={metrics['sharpe']:.2f}  ({elapsed:.1f}s)")
    return result


# ── Walk-forward OOS validation ─────────────────────────────────────────────


def _subset_data_by_date(
    data_map: dict[str, pd.DataFrame],
    start_date: str,
    end_date: str,
) -> dict[str, pd.DataFrame]:
    """Filter each DataFrame in data_map to the given date range."""
    subset = {}
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    for code, df in data_map.items():
        mask = (df.index >= start) & (df.index <= end)
        subset[code] = df.loc[mask].copy()
    return subset


def run_walk_forward(
    data_map: dict[str, pd.DataFrame],
    loader: Any,
    signal_engine: Any,
    split_pct: float = 0.7,
) -> None:
    """Run walk-forward OOS validation: train on first 70% dates, test on last 30%."""
    # Find common date range
    all_dates = None
    for df in data_map.values():
        if all_dates is None:
            all_dates = set(df.index)
        else:
            all_dates &= set(df.index)
    if not all_dates:
        print("ERROR: No common dates across all symbols for walk-forward.")
        return
    sorted_dates = sorted(all_dates)
    split_idx = int(len(sorted_dates) * split_pct)
    train_dates = sorted_dates[:split_idx]
    test_dates = sorted_dates[split_idx:]

    train_start = train_dates[0].strftime("%Y-%m-%d")
    train_end = train_dates[-1].strftime("%Y-%m-%d")
    test_start = test_dates[0].strftime("%Y-%m-%d")
    test_end = test_dates[-1].strftime("%Y-%m-%d")

    print(f"\n{'=' * 70}")
    print(f"Walk-Forward OOS Validation")
    print(f"{'=' * 70}")
    print(f"  Total bars: {len(sorted_dates)} ({train_dates[0].date()} → {test_dates[-1].date()})")
    print(f"  Train: {train_start} → {train_end} ({len(train_dates)} bars, {split_pct:.0%})")
    print(f"  Test:  {test_start} → {test_end} ({len(test_dates)} bars, {1-split_pct:.0%})")

    # Subset data
    train_data = _subset_data_by_date(data_map, train_start, train_end)
    test_data = _subset_data_by_date(data_map, test_start, test_end)

    class _SubsetLoader:
        def __init__(self, data: dict[str, pd.DataFrame]) -> None:
            self._data = data
        def fetch(self, codes, start_date="", end_date="", fields=None, interval="1D"):
            return {c: self._data[c] for c in codes if c in self._data}

    train_loader = _SubsetLoader(train_data)
    test_loader = _SubsetLoader(test_data)

    # Run baseline on both periods
    print(f"\n  --- Baseline ---")
    baseline_train = run_single({}, train_data, train_loader, signal_engine)
    baseline_test = run_single({}, test_data, test_loader, signal_engine)
    print(f"  Train: ret={baseline_train['total_return']:+.2%}  sharpe={baseline_train['sharpe']:.2f}")
    print(f"  Test:  ret={baseline_test['total_return']:+.2%}  sharpe={baseline_test['sharpe']:.2f}")

    # Sweep each parameter on train set, then evaluate best value on test set
    print(f"\n  --- Parameter Sweeps (train → test) ---")
    wf_results = []
    for sweep_name, sweep in SWEEPS.items():
        param = sweep["param"]
        values = sweep["values"]
        label = sweep["label"]

        best_val = None
        best_train_ret = -float("inf")
        best_train_sharpe = 0.0

        for val in values:
            params = {param: val}
            train_metrics = run_single(params, train_data, train_loader, signal_engine)
            ret = train_metrics.get("total_return", -float("inf"))
            if ret > best_train_ret:
                best_train_ret = ret
                best_train_sharpe = train_metrics.get("sharpe", 0)
                best_val = val

        # Evaluate best on test set
        test_metrics = run_single({param: best_val}, test_data, test_loader, signal_engine)

        wf_results.append({
            "param": param,
            "best_value": best_val,
            "train_return": best_train_ret,
            "train_sharpe": best_train_sharpe,
            "test_return": test_metrics.get("total_return", 0),
            "test_sharpe": test_metrics.get("sharpe", 0),
        })

        print(f"  {label:>35s}: best={best_val}  "
              f"train_ret={best_train_ret:+.2%}  "
              f"test_ret={test_metrics['total_return']:+.2%}  "
              f"test_sharpe={test_metrics['sharpe']:.2f}")

    # Summary table
    print(f"\n  {'─' * 90}")
    print(f"  Walk-Forward Summary:")
    print(f"  {'Parameter':>30s}  {'BestVal':>8s}  {'TrainRet':>9s}  "
          f"{'TestRet':>9s}  {'TestSharpe':>10s}  {'OOS_Drop':>9s}")
    print(f"  {'─' * 90}")
    for r in wf_results:
        train_r = r["train_return"]
        test_r = r["test_return"]
        drop = train_r - test_r if train_r != 0 else 0.0
        print(f"  {r['param']:>30s}  {str(r['best_value']):>8s}  "
              f"{train_r:+8.2%}  "
              f"{test_r:+8.2%}  "
              f"{r['test_sharpe']:>9.2f}  "
              f"{drop:+8.2%}")

    # Overall
    print(f"\n  {'─' * 90}")
    print(f"  Baseline OOS: baseline_test={baseline_test['total_return']:+.2%}  "
          f"sharpe={baseline_test['sharpe']:.2f}")
    best_wf = max(wf_results, key=lambda r: r["test_return"])
    print(f"  Best OOS:     {best_wf['param']}={best_wf['best_value']}  "
          f"test_ret={best_wf['test_return']:+.2%}  "
          f"test_sharpe={best_wf['test_sharpe']:.2f}")


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="AStock parameter optimizer")
    parser.add_argument("--sweep", type=str, default="", help="1D parameter sweep name")
    parser.add_argument("--grid", type=str, default="", choices=["quick", "full"], help="Combined grid search")
    parser.add_argument("--cache-only", action="store_true", help="Only show cached results, no new runs")
    parser.add_argument("--best", action="store_true", help="Show best from cache")
    parser.add_argument("--refresh", action="store_true", help="Clear data cache before starting")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward OOS validation")

    args = parser.parse_args()

    # Load cache
    cache = _load_cache()
    print(f"Loaded {len(cache)} cached results from {RESULTS_FILE}")

    # Load data
    print(f"\nLoading data for {len(CODES)} stocks, {START_DATE} → {END_DATE}...")
    if args.refresh:
        for p in CACHE_DIR.glob("*.csv"):
            p.unlink()
        print("  data cache cleared")
    data_map, source_map = _fetch_data()

    if not data_map:
        print("ERROR: No data available.")
        return

    for code, df in data_map.items():
        src = source_map.get(code, "?")
        print(f"  {code}: {len(df)} bars, {df.index[0].date()} → {df.index[-1].date()} [{src}]")

    # Preloaded loader
    class _PreloadedLoader:
        def __init__(self, data: dict[str, pd.DataFrame]) -> None:
            self._data = data

        def fetch(self, codes, start_date="", end_date="", fields=None, interval="1D"):
            return {c: self._data[c] for c in codes if c in self._data}

    loader = _PreloadedLoader(data_map)
    signal_engine = _NullSignalEngine()

    # Baseline
    baseline = run_baseline(data_map, loader, signal_engine, cache)
    print(f"\nBaseline: ret={baseline.get('total_return', 0):+.2%}  "
          f"sharpe={baseline.get('sharpe', 0):.2f}  "
          f"dd={baseline.get('max_drawdown', 0):.2%}  "
          f"wr={baseline.get('win_rate', 0):.1%}  "
          f"trades={baseline.get('trade_count', 0)}")

    # Cache-only mode
    if args.cache_only:
        show_best(cache)
        return

    # Show best
    if args.best:
        show_best(cache)
        return

    # Run sweep or grid
    if args.walk_forward:
        run_walk_forward(data_map, loader, signal_engine)
        return

    if args.sweep:
        run_sweep(args.sweep, data_map, loader, signal_engine, cache)
    elif args.grid:
        run_grid(args.grid, data_map, loader, signal_engine, cache)
    else:
        # Default: run all 1D sweeps
        print(f"\nNo --sweep or --grid specified. Running all 1D sweeps.")
        for sweep_name in SWEEPS:
            run_sweep(sweep_name, data_map, loader, signal_engine, _load_cache())
            cache = _load_cache()  # Refresh cache after each sweep

    print(f"\n{'=' * 70}")
    print("Optimization complete.")
    print(f"Results cached at: {RESULTS_FILE}")


if __name__ == "__main__":
    main()
