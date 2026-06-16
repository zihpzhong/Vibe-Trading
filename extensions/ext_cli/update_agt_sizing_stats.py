#!/usr/bin/env python3
"""Update AGT Kelly stats from closed-trade PnL samples.

从 trading.db 的 closed_trades（或 JSON PnL 列表）更新
``~/.vibe-trading/live/bitget/sizing_stats.json``，供 AGT 分数凯利使用。

Usage:
    PYTHONPATH=agent:. python3 extensions/ext_cli/update_agt_sizing_stats.py
    PYTHONPATH=agent:. python3 extensions/ext_cli/update_agt_sizing_stats.py \\
        --db ~/.vibe-trading/trading.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ROOT = _PROJECT_ROOT / "agent"
for path in (_PROJECT_ROOT, _AGENT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from extensions.live.agt_position_sizing import save_trade_stats, stats_from_closed_pnls


def _pnls_from_db(db_path: Path) -> list[float]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT pnl_usdt FROM closed_trades ORDER BY closed_at").fetchall()
    finally:
        conn.close()
    return [float(row[0]) for row in rows if row[0] is not None]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=Path.home() / ".vibe-trading" / "trading.db",
        help="SQLite with closed_trades (default: AUT trading.db as Kelly prior)",
    )
    args = parser.parse_args()
    pnls = _pnls_from_db(args.db)
    stats = stats_from_closed_pnls(pnls)
    path = save_trade_stats(stats)
    print(json.dumps({"written": str(path), **stats.__dict__}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
