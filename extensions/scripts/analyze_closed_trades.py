"""Analyze 108 (or local) live trading closed_trades from trading.db.

从 trading.db 的 closed_trades 表生成按日 / 品种 / 方向 / 平仓原因的 PnL 报告。
Analyze realized PnL breakdown by day, symbol, direction, and close reason.

Usage:
  # 108 服务器（默认 DB 路径）
  python extensions/scripts/analyze_closed_trades.py

  # 指定 DB 或导出 JSON
  python extensions/scripts/analyze_closed_trades.py --db /home/vibe/.vibe-trading/trading.db
  python extensions/scripts/analyze_closed_trades.py --json > report.json

  # Docker 容器内
  docker exec vibe-trading-live-trading-1 python extensions/scripts/analyze_closed_trades.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path.home() / ".vibe-trading" / "trading.db"


@dataclass(frozen=True)
class TradeRow:
    """One row from closed_trades."""

    symbol: str
    direction: str
    pnl_usdt: float
    pnl_pct: float
    reason: str
    opened_at: str
    closed_at: str
    entry_score: int
    leverage: int
    holding_hours: float


def _parse_ts(raw: str) -> datetime | None:
    """Parse ISO timestamp from DB; returns None if empty/invalid."""
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _holding_hours(opened_at: str, closed_at: str) -> float:
    """Hours between open and close; 0 if timestamps missing."""
    opened = _parse_ts(opened_at)
    closed = _parse_ts(closed_at)
    if opened is None or closed is None:
        return 0.0
    return max(0.0, (closed - opened).total_seconds() / 3600.0)


def load_trades(db_path: Path) -> list[TradeRow]:
    """Load all closed_trades ordered by close time."""
    if not db_path.exists():
        raise FileNotFoundError(f"trading.db not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT symbol, direction, pnl_usdt, pnl_pct, reason,
                   opened_at, closed_at, entry_score, leverage
            FROM closed_trades
            ORDER BY closed_at ASC, id ASC
            """
        )
        rows: list[TradeRow] = []
        for row in cursor.fetchall():
            rows.append(
                TradeRow(
                    symbol=row["symbol"],
                    direction=row["direction"],
                    pnl_usdt=float(row["pnl_usdt"]),
                    pnl_pct=float(row["pnl_pct"]),
                    reason=row["reason"] or "",
                    opened_at=row["opened_at"] or "",
                    closed_at=row["closed_at"] or "",
                    entry_score=int(row["entry_score"] if row["entry_score"] is not None else -1),
                    leverage=int(row["leverage"] if row["leverage"] is not None else 1),
                    holding_hours=_holding_hours(row["opened_at"] or "", row["closed_at"] or ""),
                )
            )
        return rows
    finally:
        conn.close()


def _latest_balance(db_path: Path) -> float | None:
    """Read latest account balance from equity_history if present."""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT balance FROM equity_history ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return float(row[0]) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _group_stats(trades: list[TradeRow]) -> dict[str, Any]:
    """Compute win/loss summary for a trade list."""
    if not trades:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "flats": 0,
            "win_rate_pct": 0.0,
            "total_pnl_usdt": 0.0,
            "avg_pnl_usdt": 0.0,
            "avg_win_usdt": 0.0,
            "avg_loss_usdt": 0.0,
        }

    wins = [t for t in trades if t.pnl_usdt > 0]
    losses = [t for t in trades if t.pnl_usdt < 0]
    flats = [t for t in trades if t.pnl_usdt == 0]
    total = sum(t.pnl_usdt for t in trades)

    return {
        "count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "total_pnl_usdt": round(total, 4),
        "avg_pnl_usdt": round(total / len(trades), 4),
        "avg_win_usdt": round(sum(t.pnl_usdt for t in wins) / len(wins), 4) if wins else 0.0,
        "avg_loss_usdt": round(sum(t.pnl_usdt for t in losses) / len(losses), 4) if losses else 0.0,
    }


def _bucket_key(trade: TradeRow, field: str) -> str:
    if field == "symbol":
        return trade.symbol
    if field == "direction":
        return trade.direction
    if field == "reason":
        return trade.reason or "UNKNOWN"
    if field == "entry_score":
        if trade.entry_score >= 7:
            return "fast_track (score>=7)"
        if trade.entry_score >= 5:
            return "enhanced (score 5-6)"
        if trade.entry_score >= 0:
            return f"score={trade.entry_score}"
        return "score=unknown"
    raise ValueError(f"unknown bucket field: {field}")


def _group_breakdown(trades: list[TradeRow], field: str) -> list[dict[str, Any]]:
    """Aggregate PnL by symbol / direction / reason / entry_score."""
    buckets: dict[str, list[TradeRow]] = defaultdict(list)
    for trade in trades:
        buckets[_bucket_key(trade, field)].append(trade)

    rows: list[dict[str, Any]] = []
    for key, bucket in buckets.items():
        stats = _group_stats(bucket)
        hours = [t.holding_hours for t in bucket if t.holding_hours > 0]
        rows.append(
            {
                "key": key,
                **stats,
                "median_holding_hours": round(statistics.median(hours), 1) if hours else 0.0,
            }
        )
    rows.sort(key=lambda item: item["total_pnl_usdt"], reverse=True)
    return rows


def _daily_breakdown(trades: list[TradeRow]) -> list[dict[str, Any]]:
    """Daily realized PnL keyed by UTC close date."""
    buckets: dict[str, list[TradeRow]] = defaultdict(list)
    for trade in trades:
        closed = _parse_ts(trade.closed_at)
        day = closed.strftime("%Y-%m-%d") if closed else "unknown"
        buckets[day].append(trade)

    rows: list[dict[str, Any]] = []
    cumulative = 0.0
    for day in sorted(buckets):
        bucket = buckets[day]
        day_pnl = round(sum(t.pnl_usdt for t in bucket), 4)
        cumulative = round(cumulative + day_pnl, 4)
        rows.append(
            {
                "date": day,
                "trades": len(bucket),
                "daily_pnl_usdt": day_pnl,
                "cumulative_pnl_usdt": cumulative,
            }
        )
    return rows


def _concentration(trades: list[TradeRow]) -> dict[str, Any]:
    """Top symbol share of total positive PnL."""
    by_symbol = _group_breakdown(trades, "symbol")
    total_pnl = sum(t.pnl_usdt for t in trades)
    positive_pnl = sum(t.pnl_usdt for t in trades if t.pnl_usdt > 0)
    top = by_symbol[0] if by_symbol else None

    top_symbol = top["key"] if top else ""
    top_pnl = top["total_pnl_usdt"] if top else 0.0
    share_of_total = round(top_pnl / total_pnl * 100, 1) if total_pnl else 0.0
    share_of_wins = round(top_pnl / positive_pnl * 100, 1) if positive_pnl > 0 else 0.0

    without_top = [t for t in trades if t.symbol != top_symbol] if top_symbol else trades
    return {
        "top_symbol": top_symbol,
        "top_symbol_pnl_usdt": top_pnl,
        "share_of_total_pnl_pct": share_of_total,
        "share_of_positive_pnl_pct": share_of_wins,
        "stats_without_top_symbol": _group_stats(without_top),
    }


def _reason_holding_median(trades: list[TradeRow]) -> list[dict[str, Any]]:
    """Median holding hours per close reason."""
    by_reason = _group_breakdown(trades, "reason")
    return [
        {
            "reason": row["key"],
            "count": row["count"],
            "median_holding_hours": row["median_holding_hours"],
            "total_pnl_usdt": row["total_pnl_usdt"],
        }
        for row in by_reason
    ]


def build_report(db_path: Path) -> dict[str, Any]:
    """Build full analysis payload."""
    trades = load_trades(db_path)
    overall = _group_stats(trades)
    balance = _latest_balance(db_path)

    roi_pct = None
    if balance and balance > 0:
        roi_pct = round(overall["total_pnl_usdt"] / balance * 100, 2)

    stale_trades = [t for t in trades if t.reason == "STALE"]
    stale_pct = round(len(stale_trades) / len(trades) * 100, 1) if trades else 0.0

    holding_hours = [t.holding_hours for t in trades if t.holding_hours > 0]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "trade_count": len(trades),
        "overall": overall,
        "latest_balance_usdt": balance,
        "realized_roi_pct_of_balance": roi_pct,
        "stale_pct": stale_pct,
        "median_holding_hours_all": round(statistics.median(holding_hours), 1) if holding_hours else 0.0,
        "daily": _daily_breakdown(trades),
        "by_symbol": _group_breakdown(trades, "symbol"),
        "by_direction": _group_breakdown(trades, "direction"),
        "by_reason": _group_breakdown(trades, "reason"),
        "by_entry_score": _group_breakdown(trades, "entry_score"),
        "reason_holding": _reason_holding_median(trades),
        "concentration": _concentration(trades),
        "trades": [asdict(t) for t in trades],
    }


def _fmt_table(headers: list[str], rows: list[list[str]], widths: list[int] | None = None) -> str:
    if not widths:
        widths = []
        for col_idx, header in enumerate(headers):
            col_vals = [header] + [row[col_idx] for row in rows]
            widths.append(max(len(v) for v in col_vals))
    lines = []
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(header_line)
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def format_text_report(report: dict[str, Any]) -> str:
    """Render human-readable Chinese report."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("  108 实盘 · closed_trades PnL 分析报告")
    lines.append("=" * 72)
    lines.append(f"  数据源: {report['db_path']}")
    lines.append(f"  生成时间 (UTC): {report['generated_at']}")
    lines.append("")

    overall = report["overall"]
    lines.append("【总体】")
    lines.append(f"  平仓笔数: {overall['count']}")
    lines.append(
        f"  胜率: {overall['win_rate_pct']}% "
        f"({overall['wins']} 胜 / {overall['losses']} 负 / {overall['flats']} 平)"
    )
    lines.append(f"  总已实现 PnL: {overall['total_pnl_usdt']:+.4f} USDT")
    lines.append(f"  单笔期望: {overall['avg_pnl_usdt']:+.4f} USDT")
    lines.append(f"  平均盈利单: {overall['avg_win_usdt']:+.4f} USDT")
    lines.append(f"  平均亏损单: {overall['avg_loss_usdt']:+.4f} USDT")
    lines.append(f"  STALE 占比: {report['stale_pct']}%")
    lines.append(f"  持仓中位时长: {report['median_holding_hours_all']} h")
    if report.get("latest_balance_usdt") is not None:
        lines.append(f"  最新账户余额: {report['latest_balance_usdt']:.2f} USDT")
    if report.get("realized_roi_pct_of_balance") is not None:
        lines.append(f"  已实现 ROI (占余额): {report['realized_roi_pct_of_balance']:+.2f}%")
    lines.append("")

    conc = report["concentration"]
    wo = conc["stats_without_top_symbol"]
    lines.append("【品种集中度】")
    lines.append(
        f"  Top 品种: {conc['top_symbol']}  PnL={conc['top_symbol_pnl_usdt']:+.4f} USDT "
        f"(占总 PnL {conc['share_of_total_pnl_pct']}%)"
    )
    lines.append(
        f"  剔除 {conc['top_symbol']} 后: {wo['count']} 笔, "
        f"总 PnL {wo['total_pnl_usdt']:+.4f}, 期望 {wo['avg_pnl_usdt']:+.4f}/笔"
    )
    lines.append("")

    if report["daily"]:
        lines.append("【按日 PnL】")
        daily_rows = [
            [
                row["date"],
                str(row["trades"]),
                f"{row['daily_pnl_usdt']:+.4f}",
                f"{row['cumulative_pnl_usdt']:+.4f}",
            ]
            for row in report["daily"]
        ]
        lines.append(_fmt_table(["日期", "笔数", "日PnL", "累计PnL"], daily_rows))
        lines.append("")

    def _section(title: str, bucket_rows: list[dict[str, Any]]) -> None:
        if not bucket_rows:
            return
        lines.append(title)
        table_rows = [
            [
                row["key"],
                str(row["count"]),
                f"{row['total_pnl_usdt']:+.4f}",
                f"{row['win_rate_pct']:.1f}%",
                f"{row['median_holding_hours']:.1f}h",
            ]
            for row in bucket_rows
        ]
        lines.append(_fmt_table(["分组", "笔数", "总PnL", "胜率", "中位持仓"], table_rows))
        lines.append("")

    _section("【按品种】", report["by_symbol"])
    _section("【按方向】", report["by_direction"])
    _section("【按平仓原因】", report["by_reason"])
    _section("【按入场评分档】", report["by_entry_score"])

    lines.append("【最近平仓明细】")
    detail_rows = []
    for trade in report["trades"][-20:]:
        closed = _parse_ts(trade["closed_at"])
        closed_s = closed.strftime("%m-%d %H:%M") if closed else "?"
        detail_rows.append(
            [
                closed_s,
                trade["symbol"],
                trade["direction"],
                f"{trade['pnl_usdt']:+.4f}",
                trade["reason"],
                str(trade["entry_score"]),
            ]
        )
    if detail_rows:
        lines.append(_fmt_table(["平仓时间", "品种", "方向", "PnL", "原因", "评分"], detail_rows))
    lines.append("")
    lines.append(_viability_notes(report))
    return "\n".join(lines)


def _viability_notes(report: dict[str, Any]) -> str:
    """Heuristic feasibility notes based on sample size and concentration."""
    count = report["overall"]["count"]
    conc = report["concentration"]
    wo = conc["stats_without_top_symbol"]
    stale_pct = report["stale_pct"]
    lines = ["【可行性提示（启发式，非投资建议）】"]

    if count < 30:
        lines.append(f"  ⚠ 样本仅 {count} 笔，统计功效不足；建议至少 50–100 笔再评估策略 edge。")
    elif count < 50:
        lines.append(f"  ⚠ 样本 {count} 笔偏少，结论仅供参考。")
    else:
        lines.append(f"  ✓ 样本 {count} 笔，可初步评估。")

    if conc["share_of_total_pnl_pct"] >= 50 and count >= 3:
        lines.append(
            f"  ⚠ Top 品种 {conc['top_symbol']} 贡献 {conc['share_of_total_pnl_pct']}% 总 PnL；"
            f"剔除后期望 {wo['avg_pnl_usdt']:+.4f}/笔。"
        )

    if stale_pct >= 40:
        lines.append(f"  ⚠ STALE 占比 {stale_pct}% 偏高（回滚阈值 40%）。")

    if wo["avg_pnl_usdt"] <= 0 and count >= 5:
        lines.append("  ⚠ 剔除 Top 品种后期望为负，盈利可能依赖个别机会。")

    return "\n".join(lines)


def plot_daily_pnl(report: dict[str, Any], output: Path) -> None:
    """Optional daily PnL chart (requires matplotlib)."""
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise SystemExit("matplotlib required for --plot; pip install matplotlib") from exc

    daily = report["daily"]
    if not daily:
        raise SystemExit("No closed trades to plot")

    dates = [row["date"][5:] for row in daily]
    daily_pnl = [row["daily_pnl_usdt"] for row in daily]
    cumulative = [row["cumulative_pnl_usdt"] for row in daily]
    trade_counts = [row["trades"] for row in daily]

    fig, ax1 = plt.subplots(figsize=(10, 5))
    x_pos = range(len(dates))
    ax1.bar(x_pos, daily_pnl, color="#22c55e", alpha=0.85, label="日已实现 PnL")
    ax1.set_ylabel("日 PnL (USDT)")
    ax1.set_xticks(list(x_pos))
    ax1.set_xticklabels(dates)
    ax1.axhline(0, color="#666", linewidth=0.8, linestyle=":")

    ax2 = ax1.twinx()
    ax2.plot(x_pos, cumulative, color="#2563eb", marker="o", linewidth=2, label="累计 PnL")
    ax2.set_ylabel("累计 PnL (USDT)")

    for idx, (day_pnl, cnt) in enumerate(zip(daily_pnl, trade_counts, strict=True)):
        ax1.text(idx, day_pnl, f"({cnt}笔)", ha="center", va="bottom" if day_pnl >= 0 else "top", fontsize=8)

    fig.suptitle("108 实盘 · 按日已实现 PnL (closed_trades)", fontsize=12, fontweight="bold")
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Chart saved: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze live trading closed_trades from trading.db")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to trading.db (default: {DEFAULT_DB})",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON instead of text report")
    parser.add_argument(
        "--plot",
        type=Path,
        metavar="PNG",
        help="Save daily PnL chart to PNG (requires matplotlib)",
    )
    args = parser.parse_args()

    report = build_report(args.db)
    if args.json:
        # 明细 trades 可选省略以减小 JSON；默认保留便于二次分析
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(report))

    if args.plot:
        plot_daily_pnl(report, args.plot)


if __name__ == "__main__":
    main()
