#!/usr/bin/env python3
"""Phase B 粗扫 + 精验（方案 2 + 方案 1 精验步）。
Coarse sweeps on short A3 proxy, then full A3 validation on top combos.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_AGENT_ROOT = _PROJECT_ROOT / "agent"
_CACHE_DIR = Path(__file__).resolve().parent / ".cache_crypto"
for _p in (_PROJECT_ROOT, _AGENT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from extensions.ext_cli.optimize_crypto import (  # noqa: E402
    _load_cache,
    _param_key,
    _save_results,
    _set_results_path,
    run_single,
)
from extensions.ext_cli.run_crypto_backtest import (  # noqa: E402
    _synthetic_ohlcv,
    build_config,
    filter_universe_by_coverage,
)
from extensions.trading.crypto.backtest.config import CryptoBacktestConfig  # noqa: E402

COARSE_RESULTS = _CACHE_DIR / "optimization_results_coarse.csv"
LONG_RESULTS = _CACHE_DIR / "optimization_results_long.csv"
MAX_DD_FLOOR = -0.19
STALE_HOURS_WINNER = 16  # 长样本 stale_position_hours 胜者

# 粗扫：跳过 min_score / trail_activation / stale_position_hours（已定或已完成）
COARSE_SWEEPS: dict[str, dict[str, Any]] = {
    "reward_risk": {"param": "reward_risk_ratio", "values": [1.5, 2.0]},
    "max_positions": {"param": "max_positions", "values": [3, 4]},
    "stale_pnl_pct": {"param": "stale_position_pnl_pct", "values": [2.0, 2.5]},
}

COARSE_CFG = {
    "start": "2024-11-01",
    "end": "2026-05-01",
    "max_symbols": 10,
    "scan_every": 24,
    "initial_cash": 1000.0,
}

FINE_CFG = {
    "start": "2024-01-01",
    "end": "2026-05-01",
    "max_symbols": 20,
    "scan_every": 12,
    "initial_cash": 1000.0,
}


def _build_data_map(
    start: str,
    end: str,
    max_symbols: int,
) -> dict[str, pd.DataFrame]:
    """加载 synthetic OHLCV 并过滤覆盖率。
    Load synthetic OHLCV and filter by coverage.
    """
    bt = CryptoBacktestConfig.with_top50()
    codes = list(bt.pair_whitelist)[:max_symbols]
    print(f"  symbols={len(codes)} window={start}..{end}")
    t0 = time.time()
    data_map = {c: _synthetic_ohlcv(c, start, end, "1h") for c in codes}
    data_map, dropped = filter_universe_by_coverage(data_map, start, end, "1h", 0.5)
    print(f"  data ready in {time.time() - t0:.1f}s, dropped={dropped}")
    return data_map


def _build_base(
    data_map: dict[str, pd.DataFrame],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """构建回测 base config。
    Build backtest base config dict.
    """
    import argparse as ap

    ns = ap.Namespace(
        start=cfg["start"],
        end=cfg["end"],
        interval="1h",
        initial_cash=cfg["initial_cash"],
        no_whitelist=False,
        replay_phase2=False,
        replay_swarm=False,
    )
    base = build_config(ns, list(data_map.keys()))
    base["scan_every_n_bars"] = cfg["scan_every"]
    return base


def run_sweep_values(
    sweep_name: str,
    spec: dict[str, Any],
    data_map: dict[str, pd.DataFrame],
    base: dict[str, Any],
) -> None:
    """单变量 sweep，逐值 checkpoint 写入 CSV。
    1D sweep with per-value checkpoint append to CSV.
    """
    param, values = spec["param"], spec["values"]
    cache = _load_cache()
    cached = {_param_key({param: r.get("value")}) for r in cache if r.get("param") == param}
    print(f"\n>>> coarse sweep {sweep_name} ({param})")
    for val in values:
        key = _param_key({param: val})
        if key in cached:
            print(f"  {val}: cached")
            continue
        t0 = time.time()
        try:
            m = run_single({param: val}, data_map, base)
            row = {"param": param, "value": val, **m}
            _save_results([row])
            cache.append(row)
            print(
                f"  {val}: ret={m['total_return']:+.2%} sharpe={m['sharpe']:.2f} "
                f"dd={m['max_drawdown']:.2%} trades={m['trade_count']} ({time.time() - t0:.1f}s)"
            )
        except Exception as exc:
            print(f"  {val}: ERROR {exc}")


def _eligible_rows(df: pd.DataFrame, param: str) -> pd.DataFrame:
    """Sharpe 排序 + 回撤约束 / Rank by Sharpe with drawdown floor."""
    sub = df[df["param"] == param].copy()
    if sub.empty:
        return sub
    sub = sub[sub["max_drawdown"] >= MAX_DD_FLOOR]
    return sub.sort_values("sharpe", ascending=False)


def rank_fine_combos(coarse_path: Path, top_n: int = 3) -> list[dict[str, Any]]:
    """从粗扫 CSV 推导精验组合（含 stale_hours=16 固定项）。
    Derive fine-validation combos from coarse CSV rankings.
    """
    if not coarse_path.exists():
        print(f"No coarse results at {coarse_path}")
        return []

    df = pd.read_csv(coarse_path)
    rr_rows = _eligible_rows(df, "reward_risk_ratio")
    pos_rows = _eligible_rows(df, "max_positions")
    pnl_rows = _eligible_rows(df, "stale_position_pnl_pct")

    rr_vals = list(rr_rows["value"].head(2)) if not rr_rows.empty else [2.0]
    pos_vals = list(pos_rows["value"].head(2)) if not pos_rows.empty else [3]
    pnl_vals = list(pnl_rows["value"].head(2)) if not pnl_rows.empty else [2.5]

    # 优先确认 stale_pnl=2.5 / Prefer stale_pnl=2.5 when present
    if 2.5 in pnl_vals:
        pnl_vals = [2.5] + [v for v in pnl_vals if v != 2.5]

    candidates: list[dict[str, Any]] = []
    for rr, pos, pnl in itertools.product(rr_vals, pos_vals, pnl_vals):
        candidates.append(
            {
                "reward_risk_ratio": float(rr),
                "max_positions": int(pos),
                "stale_position_pnl_pct": float(pnl),
                "stale_position_hours": STALE_HOURS_WINNER,
            }
        )

    # 去重并保留 mandated combo 在首位 / Dedupe; mandated combo first
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    mandated = {
        "reward_risk_ratio": float(rr_vals[0]),
        "max_positions": int(pos_vals[0]),
        "stale_position_pnl_pct": 2.5,
        "stale_position_hours": STALE_HOURS_WINNER,
    }
    for combo in [mandated, *candidates]:
        key = ";".join(f"{k}={v}" for k, v in sorted(combo.items()))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(combo)
        if len(ordered) >= top_n:
            break

    print(f"\nFine combos ({len(ordered)}):")
    for i, c in enumerate(ordered, 1):
        print(f"  {i}: {c}")
    return ordered


def _combo_label(overrides: dict[str, Any]) -> str:
    parts = [f"rr={overrides['reward_risk_ratio']}", f"mp={overrides['max_positions']}"]
    parts.append(f"spnl={overrides['stale_position_pnl_pct']}")
    parts.append(f"sh={overrides['stale_position_hours']}")
    return "+".join(parts)


def run_fine_combo(
    overrides: dict[str, Any],
    data_map: dict[str, pd.DataFrame],
    base: dict[str, Any],
    cache: list[dict[str, Any]],
) -> None:
    """全 A3 多参数精验，写入 long CSV。
    Full A3 multi-param validation run → long CSV.
    """
    label = _combo_label(overrides)
    if any(r.get("combo") == label for r in cache):
        print(f"  {label}: cached")
        return
    t0 = time.time()
    m = run_single(overrides, data_map, base)
    row = {"combo": label, **overrides, **m}
    _save_results([row])
    print(
        f"  {label}: ret={m['total_return']:+.2%} sharpe={m['sharpe']:.2f} "
        f"dd={m['max_drawdown']:.2%} trades={m['trade_count']} ({time.time() - t0:.1f}s)"
    )


def run_coarse_phase() -> None:
    """粗扫阶段 / Coarse sweep phase."""
    _set_results_path(COARSE_RESULTS)
    COARSE_RESULTS.parent.mkdir(parents=True, exist_ok=True)
    print("=== Phase B coarse ===")
    data_map = _build_data_map(COARSE_CFG["start"], COARSE_CFG["end"], COARSE_CFG["max_symbols"])
    base = _build_base(data_map, COARSE_CFG)
    for name, spec in COARSE_SWEEPS.items():
        run_sweep_values(name, spec, data_map, base)
    print(f"\nCoarse done → {COARSE_RESULTS}")


def run_fine_phase(top_n: int = 3) -> None:
    """精验阶段 / Fine validation phase."""
    _set_results_path(LONG_RESULTS)
    LONG_RESULTS.parent.mkdir(parents=True, exist_ok=True)
    combos = rank_fine_combos(COARSE_RESULTS, top_n=top_n)
    if not combos:
        print("No fine combos — run coarse first")
        return
    print("\n=== Phase B fine (full A3) ===")
    data_map = _build_data_map(FINE_CFG["start"], FINE_CFG["end"], FINE_CFG["max_symbols"])
    base = _build_base(data_map, FINE_CFG)
    cache = _load_cache()
    for combo in combos:
        run_fine_combo(combo, data_map, base, cache)
        cache = _load_cache()
    print(f"\nFine done → {LONG_RESULTS}")


def main() -> int:
    p = argparse.ArgumentParser(description="Phase B coarse + fine sweeps")
    p.add_argument("--coarse-only", action="store_true", help="Run coarse sweeps only")
    p.add_argument("--fine-only", action="store_true", help="Run fine validation only")
    p.add_argument("--top-n", type=int, default=3, help="Fine phase combo count")
    args = p.parse_args()

    if args.fine_only:
        run_fine_phase(top_n=args.top_n)
    elif args.coarse_only:
        run_coarse_phase()
    else:
        run_coarse_phase()
        run_fine_phase(top_n=args.top_n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
