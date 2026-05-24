"""Crypto live-trading parameter optimization via grid / 1D sweep.

Usage:
    python extensions/cli/optimize_crypto.py --synthetic --sweep position_size_pct
    python extensions/cli/optimize_crypto.py --synthetic --sweep min_score
    python extensions/cli/optimize_crypto.py --synthetic --grid quick
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_ROOT = _PROJECT_ROOT / "agent"
for _p in (_PROJECT_ROOT, _AGENT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from extensions.trading.crypto.backtest.config import CryptoBacktestConfig
from extensions.trading.crypto.backtest.engine import CryptoLiveBacktestEngine, _NullSignalEngine
from extensions.cli.run_crypto_backtest import (
    CACHE_DIR,
    _apply_proxy_env,
    _synthetic_ohlcv,
    build_config,
    fetch_ccxt,
    filter_universe_by_coverage,
    normalize_symbol,
)

RESULTS_FILE = CACHE_DIR / "optimization_results.csv"

SWEEPS = {
    "position_size_pct": {
        "param": "position_size_pct",
        "values": [0.05, 0.08, 0.10, 0.12, 0.15, 0.18],
    },
    "min_score": {
        "param": "scan_entry_threshold",
        "values": [4, 5, 6, 7],
    },
    "reward_risk": {
        "param": "reward_risk_ratio",
        "values": [1.5, 2.0, 2.5, 3.0, 4.0],
    },
    "trail_activation": {
        "param": "trail_activation_pct",
        "values": [2.0, 3.0, 5.0, 8.0],
    },
    "trail_distance": {
        "param": "trail_distance_pct",
        "values": [1.0, 1.5, 2.0, 3.0],
    },
    "scan_every_n_bars": {
        "param": "scan_every_n_bars",
        "values": [1, 4, 12, 24],
    },
}

QUICK_GRID = {
    "position_size_pct": [0.08, 0.12, 0.15],
    "scan_entry_threshold": [5, 6],
    "reward_risk_ratio": [2.0, 3.0],
}


def _param_key(params: dict[str, Any]) -> str:
    return ";".join(f"{k}={v}" for k, v in sorted(params.items()))


def _load_cache() -> list[dict[str, Any]]:
    if not RESULTS_FILE.exists():
        return []
    return pd.read_csv(RESULTS_FILE).to_dict("records")


def _save_results(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = not RESULTS_FILE.exists()
    with open(RESULTS_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if header:
            w.writeheader()
        w.writerows(rows)


def run_single(
    overrides: dict[str, Any],
    data_map: dict[str, pd.DataFrame],
    base: dict[str, Any],
) -> dict[str, Any]:
    cfg = {**base, **overrides}
    engine = CryptoLiveBacktestEngine(cfg)
    engine._funding_applied.clear()
    engine._funding_daily_done.clear()

    class _Loader:
        name = "opt"

        def fetch(self, _c, _s, _e, **_kw):  # noqa: ANN001
            return data_map

    with tempfile.TemporaryDirectory() as tmp:
        metrics = engine.run_backtest(cfg, _Loader(), _NullSignalEngine(), Path(tmp))
    return {
        "total_return": metrics.get("total_return", 0),
        "sharpe": metrics.get("sharpe", 0),
        "max_drawdown": metrics.get("max_drawdown", 0),
        "win_rate": metrics.get("win_rate", 0),
        "trade_count": metrics.get("trade_count", 0),
        "final_value": metrics.get("final_value", 0),
    }


def run_sweep(
    name: str,
    data_map: dict[str, pd.DataFrame],
    base: dict[str, Any],
    cache: list[dict],
) -> None:
    spec = SWEEPS.get(name)
    if not spec:
        print(f"Unknown sweep: {name}. Available: {', '.join(SWEEPS)}")
        return
    param, values = spec["param"], spec["values"]
    cached = {_param_key({param: r.get("value")}) for r in cache if r.get("param") == param}
    new_rows: list[dict] = []
    print(f"\nSweep {name} ({param})")
    for val in values:
        key = _param_key({param: val})
        if key in cached:
            print(f"  {val}: cached")
            continue
        t0 = time.time()
        try:
            m = run_single({param: val}, data_map, base)
            row = {"param": param, "value": val, **m}
            new_rows.append(row)
            print(
                f"  {val}: ret={m['total_return']:+.2%} sharpe={m['sharpe']:.2f} "
                f"dd={m['max_drawdown']:.2%} trades={m['trade_count']} ({time.time() - t0:.1f}s)"
            )
        except Exception as exc:
            print(f"  {val}: ERROR {exc}")
    _save_results(new_rows)


def run_grid(
    grid: dict[str, list],
    data_map: dict[str, pd.DataFrame],
    base: dict[str, Any],
) -> None:
    import itertools

    keys = list(grid.keys())
    combos = list(itertools.product(*(grid[k] for k in keys)))
    print(f"\nGrid search: {len(combos)} combinations")
    rows: list[dict] = []
    for combo in combos:
        overrides = dict(zip(keys, combo))
        try:
            m = run_single(overrides, data_map, base)
            rows.append({**overrides, **m})
        except Exception as exc:
            print(f"  {overrides}: ERROR {exc}")
    if not rows:
        return
    df = pd.DataFrame(rows).sort_values("sharpe", ascending=False)
    print(df.head(10).to_string(index=False))
    _save_results(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2024-04-01")
    p.add_argument("--synthetic", action="store_true")
    p.add_argument("--top50", action="store_true", help="Use Top50 whitelist")
    p.add_argument("--max-symbols", type=int, default=0, help="Top50 cap (0 = full whitelist)")
    p.add_argument("--proxy", default=None, help="HTTP proxy for CCXT (e.g. 7897)")
    p.add_argument("--codes", nargs="*", default=None)
    p.add_argument("--sweep", default=None)
    p.add_argument("--grid", choices=["quick"], default=None)
    p.add_argument("--scan-every", type=int, default=12, help="Default scan_every_n_bars for runs")
    args = p.parse_args()

    _apply_proxy_env(args.proxy or os.environ.get("CCXT_PROXY") or os.environ.get("CCXT_PROXY_PORT"))

    if args.top50:
        bt = CryptoBacktestConfig.with_top50()
        wl = bt.pair_whitelist
        codes = list(wl) if args.max_symbols <= 0 else wl[: max(2, args.max_symbols)]
        print(f"Top50 optimize: {len(codes)} symbols")
    else:
        codes = [normalize_symbol(c) for c in (args.codes or ["BTCUSDT", "ETHUSDT"])]

    if args.synthetic:
        data_map = {c: _synthetic_ohlcv(c, args.start, args.end, "1h") for c in codes}
    else:
        data_map = fetch_ccxt(codes, args.start, args.end, "1h", CACHE_DIR)
        if not data_map:
            data_map = {c: _synthetic_ohlcv(c, args.start, args.end, "1h") for c in codes}

    data_map, dropped = filter_universe_by_coverage(data_map, args.start, args.end, "1h", 0.5)
    for d in dropped:
        print(f"  drop: {d}")

    codes_list = list(data_map.keys())
    ns = argparse.Namespace(
        start=args.start,
        end=args.end,
        interval="1h",
        initial_cash=1000.0,
        no_whitelist=not args.top50,
    )
    base = build_config(ns, codes_list)
    base["scan_every_n_bars"] = args.scan_every

    cache = _load_cache()
    if args.sweep:
        run_sweep(args.sweep, data_map, base, cache)
    elif args.grid == "quick":
        run_grid(QUICK_GRID, data_map, base)
    else:
        print("Specify --sweep NAME or --grid quick")
        print(f"Sweeps: {', '.join(SWEEPS)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
