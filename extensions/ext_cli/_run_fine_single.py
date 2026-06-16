#!/usr/bin/env python3
"""Run a single fine-validation combo. Called in parallel for each combo."""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_ROOT = _PROJECT_ROOT / "agent"
for _p in (_PROJECT_ROOT, _AGENT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from extensions.ext_cli.optimize_crypto import run_single, _save_results, _set_results_path
from extensions.ext_cli.run_crypto_backtest import _synthetic_ohlcv, build_config, filter_universe_by_coverage
from extensions.trading.crypto.backtest.config import CryptoBacktestConfig

FINE_CFG = {
    "start": "2024-01-01",
    "end": "2026-05-01",
    "max_symbols": 20,
    "scan_every": 12,
    "initial_cash": 1000.0,
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--rr", type=float, required=True)
    p.add_argument("--mp", type=int, required=True)
    p.add_argument("--sp", type=float, required=True)
    p.add_argument("--sh", type=int, required=True)
    p.add_argument("--results-file", required=True)
    args = p.parse_args()

    overrides = {
        "reward_risk_ratio": args.rr,
        "max_positions": args.mp,
        "stale_position_pnl_pct": args.sp,
        "stale_position_hours": args.sh,
    }
    label = f"rr={args.rr}+mp={args.mp}+sp={args.sp}+sh={args.sh}"

    _set_results_path(Path(args.results_file))

    print(f"Loading data: {FINE_CFG['max_symbols']} sym {FINE_CFG['start']}..{FINE_CFG['end']}")
    t0 = time.time()
    bt = CryptoBacktestConfig.with_top50()
    codes = list(bt.pair_whitelist)[:FINE_CFG["max_symbols"]]
    data_map = {c: _synthetic_ohlcv(c, FINE_CFG["start"], FINE_CFG["end"], "1h") for c in codes}
    data_map, dropped = filter_universe_by_coverage(data_map, FINE_CFG["start"], FINE_CFG["end"], "1h", 0.5)
    print(f"Data ready in {time.time() - t0:.1f}s, dropped={dropped}")

    import argparse as ap
    ns = ap.Namespace(
        start=FINE_CFG["start"], end=FINE_CFG["end"], interval="1h",
        initial_cash=FINE_CFG["initial_cash"], no_whitelist=False,
        replay_phase2=False, replay_swarm=False,
    )
    base = build_config(ns, list(data_map.keys()))
    base["scan_every_n_bars"] = FINE_CFG["scan_every"]

    print(f">>> Running {label}")
    t0 = time.time()
    m = run_single(overrides, data_map, base)
    row = {"combo": label, **overrides, **m}
    _save_results([row])
    print(
        f"Done: ret={m['total_return']:+.2%} sharpe={m['sharpe']:.2f} "
        f"dd={m['max_drawdown']:.2%} trades={m['trade_count']} ({time.time() - t0:.1f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
