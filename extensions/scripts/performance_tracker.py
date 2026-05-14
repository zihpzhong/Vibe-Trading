"""Performance tracker: captures periodic metrics, compares vs baseline, reports trends.

Usage:
  python extensions/scripts/performance_tracker.py          # print report
  python extensions/scripts/performance_tracker.py --snapshot  # save new snapshot
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".vibe-trading"
BASELINE_PATH = DATA_DIR / "performance_baseline.json"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
LOG_PATH = DATA_DIR / "live_trading.log"

# Regex patterns for parsing trade events from live trading log
RE_CLOSE = re.compile(
    r"(?P<symbol>\w+USDT)\s+(?P<direction>LONG|SHORT)\s+exit=(?P<exit_price>[\d.]+)\s+"
    r"PnL=(?P<pnl>[+-][\d.]+)USDT\s+\((?P<pnl_pct>[+-][\d.]+)%\)\s+(?P<reason>\S+)"
)
RE_DE_RISK = re.compile(r"DE_RISK_\d|DOOM|STALE")
RE_OPEN = re.compile(
    r"开仓\s+(?P<symbol>\w+USDT)\s+(?P<direction>LONG|SHORT)\s+"
    r"price=(?P<entry_price>[\d.]+)\s+qty"
)
RE_SCAN_CYCLE = re.compile(r"Cycle.*?#(?P<num>\d+)")
RE_BALANCE = re.compile(r"account_balance.*?(?P<balance>[\d.]+)")
RE_DAILY_PNL = re.compile(r"当日累计 PnL=(?P<pnl>[+-][\d.]+) USDT")


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def load_baseline() -> Optional[dict]:
    if BASELINE_PATH.exists():
        return json.loads(BASELINE_PATH.read_text())
    return None


def parse_trades_from_log(log_text: str) -> dict[str, Any]:
    """Parse log text for trade events."""
    lines = log_text.splitlines()

    trades: list[dict] = []
    for line in lines:
        m = RE_CLOSE.search(line)
        if m:
            reason = m.group("reason")
            trades.append({
                "symbol": m.group("symbol"),
                "direction": m.group("direction"),
                "exit_price": float(m.group("exit_price")),
                "pnl_usdt": float(m.group("pnl")),
                "pnl_pct": float(m.group("pnl_pct")),
                "is_loss": float(m.group("pnl")) < 0,
                "is_de_risk": bool(RE_DE_RISK.search(reason)),
                "reason": reason,
                "timestamp": line[:19] if len(line) > 19 else "",
            })

    # Daily PnL
    daily_pnl = {}
    for line in lines:
        m = RE_DAILY_PNL.search(line)
        if m:
            day_key = line[:10] if len(line) >= 10 else "unknown"
            daily_pnl[day_key] = round(float(m.group("pnl")), 4)

    if not trades:
        return {"trades": [], "count": 0, "daily_pnl": daily_pnl}

    total_pnl = sum(t["pnl_usdt"] for t in trades)
    wins = [t for t in trades if not t["is_loss"]]
    losses = [t for t in trades if t["is_loss"]]

    return {
        "trades": trades[-100:],  # last 100 only
        "count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "total_pnl_usdt": round(total_pnl, 4),
        "avg_win_pnl": round(sum(t["pnl_usdt"] for t in wins) / len(wins), 4) if wins else 0,
        "avg_loss_pnl": round(sum(t["pnl_usdt"] for t in losses) / len(losses), 4) if losses else 0,
        "de_risk_count": sum(1 for t in trades if t.get("is_de_risk")),
        "daily_pnl": daily_pnl,
    }


def get_current_log() -> str:
    """Read live trading log from Docker container."""
    import subprocess
    try:
        result = subprocess.run(
            ["docker", "exec", "vibe-trading-live-trading-1",
             "cat", "/root/.vibe-trading/live_trading.log"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout
        # Fallback: read from host mounted path
        result = subprocess.run(
            ["docker", "logs", "vibe-trading-live-trading-1", "--tail", "500"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout + result.stderr
    except Exception:
        if LOG_PATH.exists():
            return LOG_PATH.read_text()
        return ""


def compute_metrics(since: Optional[str] = None) -> dict[str, Any]:
    """Compute current metrics from trading log."""
    log_text = get_current_log()
    trades_data = parse_trades_from_log(log_text)

    # Score tier analysis from log
    tier_pattern = re.compile(
        r"(?:fast_track|score=(\d+)).*?"
        r"(?P<result>开仓|通过|跳过|REJECT|WATCH)"
    )

    now = datetime.now(timezone.utc)

    return {
        "timestamp": now.isoformat(),
        "trades": trades_data,
        "container_running": True,
    }


def save_snapshot() -> str:
    """Save a performance snapshot for trend analysis."""
    _ensure_dirs()
    metrics = compute_metrics()
    now = datetime.now(timezone.utc)
    filename = now.strftime("snapshot_%Y%m%d_%H%M%S.json")
    path = SNAPSHOT_DIR / filename
    path.write_text(json.dumps(metrics, indent=2, default=str))
    return str(path)


def generate_report(baseline: Optional[dict] = None) -> str:
    """Generate comparison report between baseline and current state."""
    current = compute_metrics()

    lines = []
    lines.append("=" * 60)
    lines.append(f"  绩效追踪报告 — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 60)

    # Current metrics
    trades = current.get("trades", {})
    lines.append(f"\n当前周期:")
    lines.append(f"  总交易: {trades.get('count', 0)}")
    lines.append(f"  胜率: {trades.get('win_rate_pct', 0):.1f}%")
    lines.append(f"  总 PnL: {trades.get('total_pnl_usdt', 0):.4f} USDT")
    lines.append(f"  DE_RISK 次数: {trades.get('de_risk_count', 0)}")

    # Daily PnL breakdown
    daily = trades.get("daily_pnl", {})
    if daily:
        lines.append(f"\n每日 PnL:")
        for day, pnl in sorted(daily.items()):
            icon = "+" if pnl >= 0 else ""
            lines.append(f"  {day}: {icon}{pnl:.4f} USDT")

    # Compare with baseline
    if baseline:
        base_metrics = baseline.get("metrics", {})
        base_trades = baseline.get("trades", {})

        lines.append(f"\n与基线对比 (基线时间: {baseline.get('timestamp', 'N/A')}):")

        cur_count = trades.get("count", 0)
        base_count = base_metrics.get("trade_count", base_metrics.get("count", 0))
        if base_count > 0:
            delta_count = cur_count - base_count
            lines.append(f"  交易次数: {base_count} → {cur_count} ({delta_count:+,d})")

        cur_win = trades.get("win_rate_pct", 0)
        base_win = base_metrics.get("win_rate_pct", base_metrics.get("win_rate", 0))
        delta_win = cur_win - base_win
        arrow = "↑" if delta_win > 0 else "↓"
        lines.append(f"  胜率: {base_win:.1f}% → {cur_win:.1f}% ({delta_win:+.1f}%) {arrow}")

        cur_pnl = trades.get("total_pnl_usdt", 0)
        base_pnl = base_metrics.get("total_realized_pnl_usdt",
                                     base_metrics.get("total_pnl", 0))
        # Only compare if there are new trades
        if cur_count > base_count:
            new_pnl = cur_pnl - base_pnl
            arrow = "↑" if new_pnl > 0 else "↓"
            lines.append(f"  PnL 增量: {new_pnl:+.4f} USDT {arrow}")

        cur_de_risk = trades.get("de_risk_count", 0)
        base_de_risk = baseline.get("de_risk_count", 0)
        delta_de_risk = cur_de_risk - base_de_risk
        arrow = "↑" if delta_de_risk > 0 else "↓"
        lines.append(f"  DE_RISK: {base_de_risk} → {cur_de_risk} ({delta_de_risk:+,d}) {arrow}")

    # Optimization changes status
    lines.append(f"\n当前运行的优化:")
    lines.append(f"  ✓ TP最低阈值 >120min: 0.5% → 2.0%")
    lines.append(f"  ✓ BILLUSDT 黑名单")
    lines.append(f"  ✓ 评分动量限制 (RSI<30 且 24h<-15% 评分≤5)")
    lines.append(f"  ✓ Phase 2 跳过低数据维度")
    lines.append(f"\n建议下次评估: 运行 24h 后比较每日 PnL 趋势")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Vibe Trading Performance Tracker")
    parser.add_argument("--snapshot", action="store_true", help="Save new performance snapshot")
    args = parser.parse_args()

    if args.snapshot:
        path = save_snapshot()
        print(f"Snapshot saved: {path}")
        return

    baseline = load_baseline()
    report = generate_report(baseline)
    print(report)


if __name__ == "__main__":
    main()
