#!/usr/bin/env python3
"""Phase B A3 串行扫参（单进程复用 OHLCV，跳过已完成长样本 sweep）。
Serial Phase B sweeps on A3 baseline — shared synthetic data, skip complete sweeps.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_ROOT = _PROJECT_ROOT / "agent"
_CACHE_DIR = Path(__file__).resolve().parent / ".cache_crypto"
for _p in (_PROJECT_ROOT, _AGENT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from extensions.ext_cli.optimize_crypto import (  # noqa: E402
    RESULTS_FILE,
    SWEEPS,
    _load_cache,
    _set_results_path,
    run_combo,
    run_sweep,
)
from extensions.ext_cli.run_crypto_backtest import (  # noqa: E402
    _synthetic_ohlcv,
    build_config,
    filter_universe_by_coverage,
)
from extensions.trading.crypto.backtest.config import CryptoBacktestConfig  # noqa: E402

SWEEP_ORDER = [
    "min_score",
    "reward_risk",
    "stale_pnl_pct",
    "stale_hours",
    "trail_activation",
    "max_positions",
]

MIN_TRADES_LONG = 100


def _csv_sources(cache_dir: Path) -> list[Path]:
    """Collect sweep/result CSV shards (exclude short-sample legacy + artifacts).
    Gather result CSV shards for merge.
    """
    out: list[Path] = []
    long_path = cache_dir / "optimization_results_long.csv"
    if long_path.exists():
        out.append(long_path)
    for path in sorted(cache_dir.glob("sweep_w*.csv")):
        out.append(path)
    for path in sorted(cache_dir.glob("optimization_results_w*.csv")):
        out.append(path)
    combo = cache_dir / "optimization_results_combo.csv"
    if combo.exists():
        out.append(combo)
    return out


def sweep_is_complete(sweep_name: str, cache_dir: Path) -> bool:
    """长样本判定：该 sweep 每个候选 value 均有 trade_count >= MIN_TRADES_LONG 行。
    Complete only when every candidate value has a long-sample row.
    """
    spec = SWEEPS.get(sweep_name)
    if not spec:
        return False
    param = spec["param"]
    frames: list[pd.DataFrame] = []
    for path in _csv_sources(cache_dir):
        if not path.exists() or path.stat().st_size == 0:
            continue
        df = pd.read_csv(path)
        if "param" in df.columns:
            frames.append(df)
    if not frames:
        return False
    all_rows = pd.concat(frames, ignore_index=True)
    sub = all_rows[all_rows["param"] == param]
    if sub.empty:
        return False
    long_vals = set(sub.loc[sub["trade_count"] >= MIN_TRADES_LONG, "value"].astype(float))
    expected = {float(v) for v in spec["values"]}
    return expected <= long_vals


def merge_into_long(dest: Path, cache_dir: Path) -> int:
    """合并 cache 下所有 sweep/result CSV → optimization_results_long.csv。
    Merge shard CSVs into optimization_results_long.csv.
    """
    frames: list[pd.DataFrame] = []
    for path in _csv_sources(cache_dir):
        if path.resolve() == dest.resolve():
            continue
        frames.append(pd.read_csv(path))
    if not frames:
        return 0
    merged = pd.concat(frames, ignore_index=True)
    if dest.exists():
        existing = pd.read_csv(dest)
        merged = pd.concat([existing, merged], ignore_index=True)
    has_combo = "combo" in merged.columns
    if has_combo:
        combo_mask = merged["combo"].notna()
        combo_df = merged[combo_mask].drop_duplicates(subset=["combo"], keep="last")
        rest_df = merged[~combo_mask]
    else:
        combo_df = pd.DataFrame()
        rest_df = merged
    if "param" in rest_df.columns and "value" in rest_df.columns:
        rest_df = rest_df.drop_duplicates(subset=["param", "value"], keep="last")
    merged = pd.concat([rest_df, combo_df], ignore_index=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(dest, index=False)
    print(f"Merged → {dest} ({len(merged)} rows)")
    return len(merged)


def seed_long_from_shards(dest: Path, cache_dir: Path) -> None:
    """首次运行前把已有 sweep_w*.csv 写入 long（若 long 尚无该 param 长样本）。
    Copy completed shard rows into long CSV before new sweeps.
    """
    if dest.exists():
        return
    merge_into_long(dest, cache_dir)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--results-file", default=str(RESULTS_FILE))
    p.add_argument("--merge-only", action="store_true", help="Only merge shards, no sweeps")
    p.add_argument("--skip-combo", action="store_true")
    args = p.parse_args()
    results_path = Path(args.results_file)
    cache_dir = results_path.parent
    _set_results_path(results_path)

    if args.merge_only:
        merge_into_long(results_path, cache_dir)
        return 0

    seed_long_from_shards(results_path, cache_dir)

    bt = CryptoBacktestConfig.with_top50()
    codes = list(bt.pair_whitelist)[:20]
    print(f"Phase B serial: {len(codes)} symbols, shared synthetic OHLCV")
    t_data = time.time()
    data_map = {c: _synthetic_ohlcv(c, "2024-01-01", "2026-05-01", "1h") for c in codes}
    data_map, dropped = filter_universe_by_coverage(data_map, "2024-01-01", "2026-05-01", "1h", 0.5)
    print(f"  data ready in {time.time() - t_data:.1f}s, dropped={dropped}")

    import argparse as ap

    ns = ap.Namespace(
        start="2024-01-01",
        end="2026-05-01",
        interval="1h",
        initial_cash=1000.0,
        no_whitelist=False,
        replay_phase2=False,
        replay_swarm=False,
    )
    base = build_config(ns, list(data_map.keys()))
    base["scan_every_n_bars"] = 12

    cache = _load_cache()
    for name in SWEEP_ORDER:
        if sweep_is_complete(name, cache_dir):
            print(f"\n>>> sweep {name}: SKIP (long sample already in cache)")
            continue
        print(f"\n>>> sweep {name}")
        run_sweep(name, data_map, base, cache)
        cache = _load_cache()

    combo_done = False
    if results_path.exists():
        df = pd.read_csv(results_path)
        combo_done = "combo" in df.columns and df["combo"].notna().any()
    if not args.skip_combo and not combo_done:
        print("\n>>> combo stale_pnl_pct + reward_risk")
        run_combo(["stale_pnl_pct", "reward_risk"], data_map, base)
    elif combo_done:
        print("\n>>> combo: SKIP (already in results)")

    merge_into_long(results_path, cache_dir)
    print(f"\nDone → {results_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
