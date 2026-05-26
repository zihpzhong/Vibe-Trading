#!/usr/bin/env python3
"""合并 sweep CSV → Top3 + 各 sweep 最优（A3 长样本约束）。
Merge Phase B CSVs; pick Top-3 and per-sweep winners (sharpe, dd, trades).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

CACHE = Path(__file__).resolve().parent / ".cache_crypto"
LONG = CACHE / "optimization_results_long.csv"
MAX_DD_FLOOR = -0.19
MIN_TRADES = 100

DEFAULTS = {
    "scan_entry_threshold": 5,
    "reward_risk_ratio": 2.0,
    "trail_activation_pct": 3.0,
    "stale_hours": 24,
    "stale_pnl_pct": 3.0,
    "max_positions": 3,
}

PARAM_COLS = [
    "scan_entry_threshold",
    "reward_risk_ratio",
    "trail_activation_pct",
    "stale_hours",
    "stale_pnl_pct",
    "max_positions",
]

SWEEP_PARAM = {
    "min_score": "scan_entry_threshold",
    "reward_risk": "reward_risk_ratio",
    "stale_pnl_pct": "stale_pnl_pct",
    "stale_hours": "stale_hours",
    "trail_activation": "trail_activation_pct",
    "max_positions": "max_positions",
}


def _csv_sources() -> list[Path]:
    out: list[Path] = []
    if LONG.exists():
        out.append(LONG)
    coarse = CACHE / "optimization_results_coarse.csv"
    if coarse.exists():
        out.append(coarse)
    for path in sorted(CACHE.glob("sweep_w*.csv")):
        out.append(path)
    for path in sorted(CACHE.glob("optimization_results_w*.csv")):
        out.append(path)
    for path in sorted(CACHE.glob("optimization_results_coarse_*.csv")):
        out.append(path)
    for path in sorted(CACHE.glob("fine_combo[0-9].csv")):
        out.append(path)
    combo = CACHE / "optimization_results_combo.csv"
    if combo.exists():
        out.append(combo)
    return out


def load_merged() -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in _csv_sources():
        if path.exists() and path.stat().st_size > 0:
            frames.append(pd.read_csv(path))
    if not frames:
        raise FileNotFoundError("No Phase B CSV shards in .cache_crypto/")
    merged = pd.concat(frames, ignore_index=True)
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
    return merged


def normalize_rows(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict] = []
    for _, r in df.iterrows():
        if int(r.get("trade_count", 0)) < MIN_TRADES:
            continue
        params = dict(DEFAULTS)
        if pd.notna(r.get("combo")):
            # fine_combo*.csv 用 config.json 键名 / fine CSV uses config.json key names
            combo_map = {
                "stale_position_pnl_pct": "stale_pnl_pct",
                "stale_position_hours": "stale_hours",
            }
            for col in PARAM_COLS:
                if col in r and pd.notna(r[col]):
                    params[col] = r[col]
            for src, dst in combo_map.items():
                if src in r and pd.notna(r[src]):
                    params[dst] = r[src]
            label = str(r.get("combo", "combo"))
        elif pd.notna(r.get("param")) and pd.notna(r.get("value")):
            params[str(r["param"])] = r["value"]
            label = f"{r['param']}={r['value']}"
        else:
            continue
        rows.append(
            {
                **params,
                "label": label,
                "sharpe": float(r["sharpe"]),
                "max_drawdown": float(r["max_drawdown"]),
                "total_return": float(r.get("total_return", 0)),
                "trade_count": int(r.get("trade_count", 0)),
                "win_rate": float(r.get("win_rate", 0)),
            }
        )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("sharpe", ascending=False).drop_duplicates(
        subset=PARAM_COLS, keep="first"
    )


def eligible_sweep_rows(raw: pd.DataFrame, param: str) -> pd.DataFrame:
    sub = raw[(raw["param"] == param) & (raw["trade_count"] >= MIN_TRADES)].copy()
    if sub.empty:
        return sub
    sub = sub[sub["max_drawdown"] >= MAX_DD_FLOOR]
    return sub.sort_values("sharpe", ascending=False)


def sweep_bests(raw: pd.DataFrame) -> dict[str, dict]:
    bests: dict[str, dict] = {}
    for sweep_name, param in SWEEP_PARAM.items():
        sub = eligible_sweep_rows(raw, param)
        if sub.empty:
            continue
        row = sub.iloc[0]
        bests[sweep_name] = {
            "value": row["value"],
            "sharpe": float(row["sharpe"]),
            "max_drawdown": float(row["max_drawdown"]),
            "total_return": float(row["total_return"]),
            "trade_count": int(row["trade_count"]),
        }
    if "combo" in raw.columns:
        combo = raw[raw["combo"].notna()].sort_values("sharpe", ascending=False)
        if not combo.empty:
            row = combo.iloc[0]
            if int(row.get("trade_count", 0)) >= MIN_TRADES:
                bests["combo"] = {
                    "combo": str(row["combo"]),
                    "sharpe": float(row["sharpe"]),
                    "max_drawdown": float(row["max_drawdown"]),
                    "total_return": float(row["total_return"]),
                    "trade_count": int(row["trade_count"]),
                }
    return bests


def top3_markdown(norm: pd.DataFrame) -> str:
    eligible = norm[norm["max_drawdown"] >= MAX_DD_FLOOR].sort_values(
        "sharpe", ascending=False
    ).head(3)
    lines = [
        "| rank | min_score | rr | trail_act | stale_h | stale_pnl% | sharpe | max_dd | trades | 备注 |",
        "|------|-----------|-----|-----------|---------|------------|--------|--------|--------|------|",
    ]
    for i, (_, r) in enumerate(eligible.iterrows(), 1):
        note = str(r["label"])
        if note == "combo":
            note = "Phase B+ combo"
        elif "=" in note:
            note = f"单扫 {note.split('=')[0]}"
        lines.append(
            f"| {i} "
            f"| {int(r['scan_entry_threshold'])} "
            f"| {r['reward_risk_ratio']:.1f} "
            f"| {r['trail_activation_pct']:.1f} "
            f"| {int(r['stale_hours'])} "
            f"| {r['stale_pnl_pct']:.1f} "
            f"| {r['sharpe']:.2f} "
            f"| {r['max_drawdown'] * 100:.2f}% "
            f"| {int(r['trade_count'])} "
            f"| {note} |"
        )
    return "\n".join(lines)


def main() -> int:
    raw = load_merged()
    LONG.parent.mkdir(parents=True, exist_ok=True)
    raw.to_csv(LONG, index=False)
    norm = normalize_rows(raw)
    bests = sweep_bests(raw)
    table = top3_markdown(norm) if not norm.empty else "(no eligible rows)"
    out = {
        "rows_raw": len(raw),
        "rows_norm": len(norm),
        "eligible": int((norm["max_drawdown"] >= MAX_DD_FLOOR).sum()) if not norm.empty else 0,
        "sweep_bests": bests,
        "top3_markdown": table,
    }
    out_path = CACHE / "_top3_analysis.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if bests else 1


if __name__ == "__main__":
    sys.exit(main())
