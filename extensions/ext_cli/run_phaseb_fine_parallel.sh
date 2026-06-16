#!/usr/bin/env bash
# Phase B 精验：从粗扫合并结果选 Top 组合，最多 3 路并行 A3 / Fine A3 validation
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export PYTHONUNBUFFERED=1
CACHE="extensions/ext_cli/.cache_crypto"
LONG="$CACHE/optimization_results_long.csv"
FINE_COMMON=(
  --synthetic --top50 --max-symbols 20
  --start 2024-01-01 --end 2026-05-01
  --initial-cash 1000 --scan-every 12
)

COMBOS_FILE="$CACHE/_fine_combos.txt"
python3 - "$CACHE" "$COMBOS_FILE" <<'PY'
import sys
from pathlib import Path

import pandas as pd

cache = Path(sys.argv[1])
out = Path(sys.argv[2])
MAX_DD = -0.19
MIN_TRADES = 50
DEFAULTS = {
    "scan_entry_threshold": 5,
    "reward_risk_ratio": 2.0,
    "trail_activation_pct": 3.0,
    "stale_hours": 24,
    "stale_pnl_pct": 3.0,
    "max_positions": 3,
}
shards = sorted(cache.glob("optimization_results_coarse_*.csv"))
if not shards:
    sys.exit("no coarse CSV shards")
frames = [pd.read_csv(p) for p in shards]
raw = pd.concat(frames, ignore_index=True)
raw = raw[raw["trade_count"] >= MIN_TRADES]
raw = raw[raw["max_drawdown"] >= MAX_DD]
if raw.empty:
    sys.exit("no eligible coarse rows")

def best_for(param: str) -> float | int | None:
    sub = raw[raw["param"] == param]
    if sub.empty:
        return None
    row = sub.sort_values("sharpe", ascending=False).iloc[0]
    return row["value"]

params = dict(DEFAULTS)
for p in ("reward_risk_ratio", "max_positions", "stale_pnl_pct"):
    v = best_for(p)
    if v is not None:
        params[p] = v

combos: list[dict] = [dict(params)]
for p in ("reward_risk_ratio", "max_positions", "stale_pnl_pct"):
    sub = raw[raw["param"] == p].sort_values("sharpe", ascending=False)
    if sub.empty:
        continue
    alt = dict(params)
    alt[p] = sub.iloc[0]["value"]
    if alt not in combos:
        combos.append(alt)
combos = combos[:3]

lines = []
for i, c in enumerate(combos):
    parts = [f"{k}={v}" for k, v in sorted(c.items())]
    lines.append(f"combo{i}\t" + "\t".join(parts))
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Wrote {len(combos)} fine combos → {out}")
for line in lines:
    print(line)
PY

idx=0
while IFS=$'\t' read -r label rest; do
  [[ -z "$label" ]] && continue
  overrides=()
  for kv in $rest; do
    overrides+=(--override "$kv")
  done
  log="/tmp/phaseb_fine_${label}.log"
  echo "START fine $label → $log"
  nohup python3 -u extensions/ext_cli/optimize_crypto.py \
    "${FINE_COMMON[@]}" \
    "${overrides[@]}" \
    --results-file "$LONG" \
    >"$log" 2>&1 &
  echo "fine $label PID=$!"
  idx=$((idx + 1))
  [[ $idx -ge 3 ]] && break
done < "$COMBOS_FILE"
echo "=== fine workers launched ==="
