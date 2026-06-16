#!/usr/bin/env bash
# Phase B 粗扫：3 路并行（各 param 独立 CSV）/ Coarse parallel sweeps (max 3 workers)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export PYTHONUNBUFFERED=1
CACHE="extensions/ext_cli/.cache_crypto"
COARSE_COMMON=(
  --synthetic --top50 --max-symbols 10
  --start 2024-11-01 --end 2026-05-01
  --initial-cash 1000 --scan-every 24
)

_run_worker() {
  local name="$1" sweep="$2" values="$3" outfile="$4" log="$5"
  if [[ -f "$outfile" ]] && python3 -c "
import pandas as pd
from pathlib import Path
p = Path('$outfile')
df = pd.read_csv(p)
spec = {'reward_risk': 'reward_risk_ratio', 'max_positions': 'max_positions', 'stale_pnl_pct': 'stale_pnl_pct'}
param = spec.get('$sweep', '')
if param and 'param' in df.columns:
    sub = df[df['param'] == param]
    want = [float(x) if '.' in x else int(x) for x in '$values'.split(',')]
    have = set(sub['value'].tolist())
    exit(0 if all(v in have for v in want) else 1)
exit(1)
" 2>/dev/null; then
    echo "SKIP $name ($outfile complete)"
    return 0
  fi
  echo "START $name → $log"
  nohup python3 -u extensions/ext_cli/optimize_crypto.py \
    "${COARSE_COMMON[@]}" \
    --sweep "$sweep" --values "$values" \
    --results-file "$outfile" \
    >"$log" 2>&1 &
  echo "$name PID=$!"
}

echo "=== Phase B coarse parallel $(date -Iseconds) ==="
_run_worker A reward_risk "1.5,2.0" \
  "$CACHE/optimization_results_coarse_rr.csv" /tmp/phaseb_coarse_rr.log
_run_worker B max_positions "3,4" \
  "$CACHE/optimization_results_coarse_maxpos.csv" /tmp/phaseb_coarse_maxpos.log
_run_worker C stale_pnl_pct "2.0,2.5" \
  "$CACHE/optimization_results_coarse_stalepnl.csv" /tmp/phaseb_coarse_stalepnl.log
echo "=== coarse workers launched ==="
