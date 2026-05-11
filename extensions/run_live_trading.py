"""实盘交易系统入口 — 全流程自动扫描+自动交易.

完整链路:
  BTC传导 → Phase 1 扫描 → ATR止损 → Execution Gate → 自动开仓 → TP/SL守护

Usage:
    python extensions/run_live_trading.py              # 默认 dry-run，仅扫描不交易
    python extensions/run_live_trading.py --mock       # 模拟测试
    python extensions/run_live_trading.py --live --confirm-live I_UNDERSTAND
"""

from __future__ import annotations

import argparse
import logging
import os
import signal as _signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
except ModuleNotFoundError:
    class Console:  # type: ignore[no-redef]
        def print(self, *args, **kwargs) -> None:
            print(*args)

    class Table:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            self._rows: list[tuple[str, ...]] = []

        def add_column(self, *args, **kwargs) -> None:
            return None

        def add_row(self, *args: str, **kwargs) -> None:
            self._rows.append(tuple(args))

        def __str__(self) -> str:
            return "\n".join(" ".join(row) for row in self._rows)

    class Panel:  # type: ignore[no-redef]
        @staticmethod
        def fit(text: str, *args, **kwargs) -> str:
            return text

# ------------------------------------------------------------
# 确保能找到 extensions/ 和 agent/src 包
# ------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent  # /app (= project root)
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_SCRIPT_DIR))

from logging.handlers import RotatingFileHandler

# 日志目录
_LOG_DIR = Path(os.environ.get("VIBE_TRADING_LOG_DIR", Path.home() / ".vibe-trading" / "logs"))
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    _LOG_DIR = Path(tempfile.gettempdir()) / "vibe-trading" / "logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

# 文件日志（带轮转，保留 7 天）
_file_handler = RotatingFileHandler(
    _LOG_DIR / "live_trading.log", maxBytes=10 * 1024 * 1024, backupCount=7,
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        _file_handler,
    ],
)
log = logging.getLogger("live_trading")

console = Console()

# 默认开仓比例: 单次开仓使用资金比例
DEFAULT_POSITION_SIZE_PCT = 0.05  # 5% of account
# 默认R:R = 2:1 计算止盈
DEFAULT_REWARD_RISK_RATIO = 2.0
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vibe Trading 全流程实盘自动交易系统")
    parser.add_argument("--mock", action="store_true", help="使用模拟交易所（测试用）")
    parser.add_argument("--dry-run", action="store_true", help="扫描但不交易")
    parser.add_argument("--live", action="store_true", help="显式启用真实下单")
    parser.add_argument(
        "--confirm-live",
        default="",
        help=f"实盘确认短语，必须为 {LIVE_CONFIRM_PHRASE}",
    )
    parser.add_argument("--interval", type=int, default=10, help="扫描间隔（分钟）")
    parser.add_argument("--balance", type=float, default=50.0, help="账户 USDT 余额")
    parser.add_argument("--mode", choices=["default", "conservative", "aggressive"],
                        default="default", help="风控模式")
    parser.add_argument("--position-size", type=float, default=DEFAULT_POSITION_SIZE_PCT,
                        help=f"单次开仓保证金比例 (默认 {DEFAULT_POSITION_SIZE_PCT * 100:.0f}%%)")
    parser.add_argument("--rr", type=float, default=DEFAULT_REWARD_RISK_RATIO,
                        help=f"目标 R:R 止盈比 (默认 {DEFAULT_REWARD_RISK_RATIO}:1)")
    parser.add_argument("--max-leverage", type=int, default=5,
                        help="最大杠杆倍数 (默认 5), score≥7 用最大值, score 5-6 用半值")
    parser.add_argument("--no-phase2", action="store_true",
                        help="跳过 Phase 2 LLM 深度分析 (仅 Phase 1 + Gate)")
    args = parser.parse_args()
    if args.live and args.dry_run:
        parser.error("--live and --dry-run cannot be used together")
    args.dry_run = not args.live
    return args


def validate_live_mode(args: argparse.Namespace) -> str:
    """Return an error string if live trading was requested unsafely."""
    if args.live and args.confirm_live != LIVE_CONFIRM_PHRASE:
        return f"Live trading requires --confirm-live {LIVE_CONFIRM_PHRASE}"
    return ""


def estimate_max_order_notional(
    balance: float,
    position_size: float,
    max_leverage: int,
    max_position_pct: float,
) -> float:
    """Estimate the largest initial order notional possible under current knobs."""
    effective_position_size = min(position_size, max_position_pct / 100)
    return min(balance * effective_position_size * max_leverage, balance)


def validate_min_order_notional(max_order_notional: float, min_notional: float = 20.0) -> str:
    """Return an error string when the config cannot meet exchange min notional."""
    if max_order_notional < min_notional:
        return (
            f"当前 balance/position-size/leverage 组合最大名义价值 ${max_order_notional:.2f} "
            f"< ${min_notional:.2f} 最小下单名义价值"
        )
    return ""


def build_status_table(
    cycle_count: int,
    btc_status: str,
    active_positions: int,
    exposure: float,
    last_scan_time: str,
    rankings_count: int,
    orders_count: int,
) -> Table:
    table = Table(show_header=False, show_edge=False, padding=(0, 1), expand=True)
    table.add_column(width=16)
    table.add_column()
    table.add_row("[bold]Cycle[/bold]", f"#{cycle_count}")
    table.add_row("[bold]BTC[/bold]", btc_status)
    table.add_row("[bold]Positions[/bold]", str(active_positions))
    exp_str = f"{exposure:.1%}" if exposure < 1.0 else f"{exposure:.1f}x"
    table.add_row("[bold]Exposure[/bold]", exp_str)
    table.add_row("[bold]Last Scan[/bold]", last_scan_time)
    table.add_row("[bold]Rankings[/bold]", str(rankings_count))
    table.add_row("[bold]Orders[/bold]", str(orders_count))
    return table


def main() -> int:
    args = parse_args()
    live_mode_error = validate_live_mode(args)
    if live_mode_error:
        log.error(live_mode_error)
        console.print(f"[red]{live_mode_error}[/red]")
        return 2

    # ---- 导入 (from extensions, not upstream) ----
    from extensions.live_trading.engine.exchange import create_exchange
    from extensions.live_trading.engine.position_tracker import PositionTracker
    from extensions.live_trading.engine.scheduler import TradingScheduler
    from extensions.live_trading.engine.tpsl_monitor import TPSLMonitor
    from extensions.live_trading.engine.atr_stop import calculate_atr_stop
    from extensions.live_trading.engine.execution_gate import ExecGateEngine
    from extensions.live_trading.engine.phase2 import Phase2Analyzer
    from extensions.live_trading.config import LiveTradingConfig
    from extensions.live_trading.models import GateStatus, LiveSignal, ScheduleReport, SignalDirection

    # ---- 配置 ----
    config = LiveTradingConfig()
    if args.mode == "conservative":
        config = LiveTradingConfig.conservative()
    elif args.mode == "aggressive":
        config = LiveTradingConfig.aggressive()
    config.default_scan_interval_minutes = args.interval

    config_err = config.validate()
    if config_err:
        log.error("Config validation failed: %s", config_err)
        return 1

    mode_label = {"default": "默认", "conservative": "保守", "aggressive": "激进"}
    # ---- 交易所 ----
    exchange = create_exchange(mock=args.mock)
    log.info(
        "Exchange: %s (mock=%s, auth=%s)",
        type(exchange).__name__,
        args.mock,
        getattr(exchange, "has_auth", False),
    )

    # ---- 检查实际账户余额（仅 futures） ----
    actual_usdt_balance = None
    try:
        actual_balance = exchange.get_account_balance()
        if actual_balance:
            actual_usdt_balance = actual_balance.get("USDT", 0)
            if actual_usdt_balance < args.balance:
                log.warning(
                    "Futures wallet USDT balance: %.2f (--balance=%.2f may be inaccurate)",
                    actual_usdt_balance, args.balance,
                )
                if actual_usdt_balance < 10:
                    log.error(
                        "Insufficient futures wallet balance (%.2f USDT). "
                        "Please transfer USDT from Spot wallet to Futures wallet on Binance.",
                        actual_usdt_balance,
                    )
            else:
                log.info("Futures wallet USDT balance: %.2f", actual_usdt_balance)
    except Exception:
        log.info("Could not query account balance (non-fatal)")

    # ---- 设置合约模式：逐仓 + 单向持仓 ----
    if hasattr(exchange, "set_position_mode") and not args.mock:
        try:
            exchange.set_position_mode(dual=False)
        except Exception:
            log.warning("set_position_mode failed (可能 Binance 网络暂时不可用), 继续启动...")

    # ---- 持仓管理 ----
    # 使用实际余额（优先）或 CLI 默认值
    effective_balance = actual_usdt_balance if actual_usdt_balance is not None else args.balance
    positions = PositionTracker(
        account_balance=effective_balance,
        max_positions=5,
        max_exposure_pct=5.0,  # 总敞口上限 500%（含杠杆名义价值, 5 仓 × 100%/仓）
    )
    # 确保 _load() 不覆盖构造函数传入的交易所真实余额
    if actual_usdt_balance is not None:
        positions.account_balance = actual_usdt_balance

    # ---- Phase 2 分析引擎 ----
    phase2_analyzer = Phase2Analyzer() if not args.no_phase2 else None
    if phase2_analyzer:
        log.info("Phase 2 deep analysis enabled (LLM-driven skills)")
    else:
        log.info("Phase 2 disabled (--no-phase2)")

    # ---- Gate 引擎 ----
    gate_engine = ExecGateEngine(config)

    # ---- 调度器（始终生成 Phase2Request，下单由 dry_run 控制） ----
    scheduler = TradingScheduler(exchange, positions, trading_enabled=True)

    # ---- TP/SL 守护 ----
    monitor = TPSLMonitor(
        exchange, positions, poll_interval=5.0,
        de_risk_config=config.de_risk,
        dca_config=config.dca,
        dca_gate_engine=gate_engine,
        max_leverage=args.max_leverage,
        position_size_pct=args.position_size,
    )
    monitor.start()
    log.info("TPSL Monitor started (de-risk: [%.0f%%:%.0f%%, %.0f%%:%.0f%%, %.0f%%:%.0f%%], doom=%.0f%%)",
             config.de_risk.level1_loss_pct, config.de_risk.level1_sell_fraction * 100,
             config.de_risk.level2_loss_pct, config.de_risk.level2_sell_fraction * 100,
             config.de_risk.level3_loss_pct, config.de_risk.level3_sell_fraction * 100,
             config.de_risk.doom_loss_pct)

    # ---- 日亏损熔断 ----
    # ---- 日亏损熔断 ----
    class DailyRiskTracker:
        """跟踪当日已实现盈亏，超过阈值时熔断交易。

        仅在持仓平仓时记录已实现盈亏，不受浮动盈亏波动影响。
        熔断后进入冷却期（默认4小时），冷却结束后可恢复交易。
        """
        def __init__(self, max_daily_loss_pct: float = 0.10, cooldown_hours: float = 4.0):
            self.max_daily_loss_pct = max_daily_loss_pct
            self.cooldown_hours = cooldown_hours
            self._day_key = ""
            self._realized_pnl: float = 0.0  # 当日已实现盈亏
            self._last_closed_count: int = 0  # 已处理的平仓记录数
            self._suspended_until: float = 0.0  # 熔断解除时间戳

        def reset_if_new_day(self) -> None:
            """跨日自动重置。"""
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self._day_key:
                if self._day_key and self._realized_pnl != 0:
                    log.info(
                        "📊 日终业绩: %s realized PnL=%+.2f USDT (%.1f%% 账户)",
                        self._day_key, self._realized_pnl,
                        self._realized_pnl / positions.account_balance * 100 if positions.account_balance else 0,
                    )
                self._day_key = today
                self._realized_pnl = 0.0
                self._last_closed_count = 0
                # 新交易日解除熔断
                if self._suspended_until > 0:
                    log.info("DailyRiskTracker: 新交易日, 解除熔断")
                    self._suspended_until = 0.0
                log.info("DailyRiskTracker reset for %s (threshold %.0f%%)", today, self.max_daily_loss_pct * 100)

        def sync_from_closed(self, closed_positions: list) -> int:
            """从已平仓记录同步当日已实现盈亏。

            只处理上次同步后新增的平仓记录，避免重复计数。

            Returns:
                新处理的平仓记录数。
            """
            new_count = 0
            for i in range(self._last_closed_count, len(closed_positions)):
                rec = closed_positions[i]
                try:
                    # CloseRecord is a dataclass with .closed_at and .pnl_usdt
                    closed_at = getattr(rec, "closed_at", "")
                    if not closed_at:
                        closed_at = datetime.now(timezone.utc).isoformat()
                    closed_day = closed_at[:10]  # YYYY-MM-DD
                    if closed_day == self._day_key:
                        pnl = getattr(rec, "pnl_usdt", 0.0)
                        self._realized_pnl += float(pnl)
                        new_count += 1
                except Exception:
                    pass
            self._last_closed_count = len(closed_positions)
            if new_count > 0:
                log.info(
                    "DailyRiskTracker: 记录 %d 笔平仓, 当日累计 PnL=%+.2f USDT",
                    new_count, self._realized_pnl,
                )
            return new_count

        @property
        def is_blown(self) -> bool:
            """是否触发熔断。

            熔断条件：
            1. 当日已实现亏损超过 max_daily_loss_pct * account_balance
            2. 且冷却时间尚未结束
            """
            now = time.time()
            if self._suspended_until > now:
                return True

            if positions.account_balance <= 0:
                return False

            loss_pct = abs(min(self._realized_pnl, 0.0)) / positions.account_balance
            if loss_pct >= self.max_daily_loss_pct:
                self._suspended_until = now + self.cooldown_hours * 3600
                log.warning(
                    "⚠️ 日亏损熔断触发: 累计亏损 %.2f USDT (%.1f%%), 冷却 %.0f 小时至 %s",
                    abs(self._realized_pnl),
                    loss_pct * 100,
                    self.cooldown_hours,
                    datetime.fromtimestamp(self._suspended_until, tz=timezone.utc).isoformat(),
                )
                return True
            return False

        @property
        def suspension_remaining_str(self) -> str:
            """返回熔断剩余时间的可读字符串。"""
            remaining = self._suspended_until - time.time()
            if remaining <= 0:
                return ""
            hours = int(remaining // 3600)
            minutes = int((remaining % 3600) // 60)
            return f"{hours}h{minutes}m"

        @property
        def day_pnl(self) -> float:
            return self._realized_pnl

    daily_risk = DailyRiskTracker(max_daily_loss_pct=0.10, cooldown_hours=4.0)

    max_initial_notional = estimate_max_order_notional(
        balance=positions.account_balance,
        position_size=args.position_size,
        max_leverage=args.max_leverage,
        max_position_pct=config.execution_gate.max_position_pct,
    )
    min_notional_error = validate_min_order_notional(max_initial_notional, config.dca.dca_min_notional_usdt)
    if min_notional_error:
        if args.live:
            log.error(min_notional_error)
            console.print(f"[red]{min_notional_error}[/red]")
            monitor.stop(timeout=5.0)
            return 1
        log.warning(min_notional_error)
        console.print(f"[yellow]{min_notional_error}；dry-run 继续，仅用于观察信号[/yellow]")

    # ---- 信号处理 ----
    stop_event = Event()

    def _handle_signal(signum: int, _frame) -> None:
        log.info("Received signal %d, shutting down...", signum)
        stop_event.set()

    _signal.signal(_signal.SIGINT, _handle_signal)
    _signal.signal(_signal.SIGTERM, _handle_signal)

    # ---- 运行循环 ----
    interval_seconds = args.interval * 60
    cycle_count = 0
    total_orders = 0

    console.print()
    console.print(Panel.fit(
        f"[bold]Vibe Trading 全流程实盘[/bold]\n"
        f"  余额: {positions.account_balance:.2f} USDT"
        f"  模式: {mode_label[args.mode]}"
        f"  间隔: {args.interval}min"
        f"  仓位: {args.position_size:.0%}"
        f"  R:R: {args.rr}:1"
        f"  {'[yellow]DRY RUN[/yellow]' if args.dry_run else '[green]LIVE[/green]'}"
        f"  {'[dim]MOCK[/dim]' if args.mock else ''}",
        border_style="green",
    ))
    console.print()

    while not stop_event.is_set():
        cycle_start = time.time()
        cycle_count += 1
        cycle_orders = 0

        # ---- 记录权益快照 ----
        try:
            positions.record_equity_snapshot()
        except Exception:
            pass

        try:
            # ================================================================
            # STEP 0: 日亏损检查 & BTC传导 + Phase 1 扫描 + 分级决策
            # ================================================================
            daily_risk.reset_if_new_day()
            if daily_risk.is_blown:
                log.warning(
                    "Daily loss limit reached: %.2f USDT (%.1f%%), skipping this cycle",
                    daily_risk.day_pnl,
                    daily_risk.day_pnl / positions.account_balance * 100,
                )
                cooldown_str = daily_risk.suspension_remaining_str
                console.print(f"[red]⚠️ 日亏损熔断触发 (已实现亏损: {daily_risk.day_pnl:.2f} USDT), 冷却剩余 {cooldown_str}[/red]")
                report = ScheduleReport(
                    rankings=[], phase2_requests=[], watchlist=[],
                    btc_status="CONDUCTION_OK", btc_1h_trend="NEUTRAL",
                    active_positions=positions.active_count,
                    trading_enabled=False,
                )
                # 仍显示持仓，但不交易
            else:
                report = scheduler.run_once(top_n=config.scan_top_n)

            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
            btc_label = report.btc_status
            if btc_label in ("LOCK_LONG", "LOCK_SHORT"):
                btc_label = f"[red]{btc_label}[/red]"
            btc_1h = getattr(report, "btc_1h_trend", "NEUTRAL")
            if btc_1h == "WEAKNESS":
                btc_label += " [yellow]1h↓[/yellow]"
            elif btc_1h == "STRENGTH":
                btc_label += " [green]1h↑[/green]"

            # 状态表
            table = build_status_table(
                cycle_count=cycle_count,
                btc_status=btc_label,
                active_positions=report.active_positions,
                exposure=positions.get_exposure(),
                last_scan_time=now_str,
                rankings_count=len(report.rankings),
                orders_count=total_orders,
            )
            console.print(table)

            # --- 持仓盈亏 ---
            active_pos = positions.get_active_positions()
            if active_pos:
                try:
                    active_symbols = [p.symbol for p in active_pos]
                    tickers_all = exchange.get_tickers(active_symbols)
                    prices = {t["symbol"]: float(t.get("last", 0)) for t in tickers_all}
                    console.print("[bold]持仓盈亏:[/bold]")
                    total_pnl = 0.0
                    for p in active_pos:
                        cur = prices.get(p.symbol, 0) or 0
                        if p.direction == "LONG":
                            pnl_pct = (cur - p.entry_price) / p.entry_price * 100 if p.entry_price else 0
                            pnl_usdt = p.quantity * (cur - p.entry_price)
                        else:
                            pnl_pct = (p.entry_price - cur) / p.entry_price * 100 if p.entry_price else 0
                            pnl_usdt = p.quantity * (p.entry_price - cur)
                        total_pnl += pnl_usdt
                        color = "[green]" if pnl_pct >= 0 else "[red]"
                        sl_dist = abs(cur - p.stop_loss) / cur * 100 if cur and p.stop_loss else 0
                        console.print(
                            f"  {color}{p.symbol:12s} {p.direction:5s} "
                            f"entry={p.entry_price:.6f} cur={cur:.6f} "
                            f"PnL={pnl_pct:+.2f}% ({pnl_usdt:+.3f}USDT) "
                            f"SL距={sl_dist:.1f}%[/]"
                        )
                    console.print(f"  [dim]浮动盈亏合计: {total_pnl:+.3f} USDT[/dim]")
                    # 从已平仓记录同步当日已实现盈亏（仅处理新增记录）
                    daily_risk.sync_from_closed(positions.get_recent_closed(50))
                except Exception:
                    pass

            # --- 已平仓记录 ---
            closed_records = positions.get_recent_closed(5)
            if closed_records:
                console.print("[dim]最近平仓:[/dim]")
                for c in reversed(closed_records):
                    color = "[green]" if c.pnl_usdt >= 0 else "[red]"
                    # 小金额显示更多小数位
                    if abs(c.pnl_usdt) < 0.01:
                        pnl_str = f"{c.pnl_usdt:+.4f}"
                    else:
                        pnl_str = f"{c.pnl_usdt:+.2f}"
                    console.print(
                        f"  {color}{c.symbol:12s} {c.direction:5s} "
                        f"exit={c.exit_price:.4f} "
                        f"PnL={pnl_str}USDT ({c.pnl_pct:+.2f}%) "
                        f"{c.reason}[/]"
                    )

            if report.rankings:
                top = report.rankings[:5]
                console.print("[dim]TOP 排名:[/dim]")
                for r in top:
                    console.print(
                        f"  {r.get('symbol',''):12s} "
                        f"score={r.get('score',0):2d} "
                        f"{r.get('direction','LONG'):5s} "
                        f"RSI_1h={r.get('rsi_1h',50):.1f} "
                        f"24h={r.get('change_24h',0):+.1f}%"
                    )

            # ================================================================
            # STEP 2: 自动交易 — 对每个 Phase2Request 执行 Gate → 开仓
            # ================================================================
            # 2aa. 同步实际余额（每轮一次）
            try:
                bal = exchange.get_account_balance()
                if bal and "USDT" in bal:
                    positions.account_balance = float(bal["USDT"])
                # 同步可用余额（开仓保证金用）
                free_bal = exchange.get_available_balance()
                _available_usdt = float(free_bal.get("USDT", 0)) if free_bal else 0
            except Exception as exc:
                log.warning("余额同步失败: %s", exc)
                _available_usdt = 0

            if report.phase2_requests:
                console.print(f"[bold]--- 自动交易评估 ({'LIVE' if not args.dry_run else 'DRY RUN'}) ---[/bold]")

                for req in report.phase2_requests:
                    symbol = req.symbol
                    direction = SignalDirection(req.direction)
                    score = req.score
                    entry_price = req.entry_price

                    # 2a. 检查是否可以开新仓
                    ok, reason = positions.can_open_new(symbol)
                    if not ok:
                        console.print(f"  {symbol} [yellow]SKIP[/yellow] {reason}")
                        continue
                    if positions.is_in_cooldown(symbol, req.direction):
                        console.print(f"  {symbol} [yellow]SKIP[/yellow] 冷却中")
                        continue

                    # 2ab. BTC 1h trend gate: avoid counter-trend altcoin trades
                    btc_hint = getattr(report, "btc_1h_trend", "NEUTRAL")
                    if btc_hint != "NEUTRAL" and symbol != "BTCUSDT":
                        if btc_hint == "WEAKNESS" and direction == SignalDirection.LONG:
                            console.print(
                                f"  {symbol} [yellow]SKIP[/yellow] BTC 1h 弱势, 跳过山寨币多头"
                            )
                            continue
                        if btc_hint == "STRENGTH" and direction == SignalDirection.SHORT:
                            console.print(
                                f"  {symbol} [yellow]SKIP[/yellow] BTC 1h 强势, 跳过山寨币空头"
                            )
                            continue

                    # 2b. 获取市场数据
                    try:
                        ticker = exchange.get_ticker(symbol)
                        orderbook = exchange.get_orderbook(symbol, 10)
                        # 如果 entry_price 为 0（扫描器未输出价格），使用当前市价
                        if entry_price is None or entry_price == 0:
                            entry_price = float(ticker.get("last", 0))
                            req.entry_price = entry_price  # 同步到 Phase2Request
                        if entry_price is None or entry_price <= 0:
                            console.print(f"  {symbol:12s} [yellow]SKIP[/yellow] 无效入场价格")
                            continue
                    except Exception as exc:
                        log.warning("Market data fetch failed for %s: %s", symbol, exc)
                        continue

                    # 2c. Phase 2: LLM深度分析 — load_skill per dim → 综合评分
                    if phase2_analyzer and req.dims:
                        phase2_result = phase2_analyzer.analyze(req, ticker)
                        if phase2_result:
                            dims_str = "; ".join(
                                f"{d}:{phase2_result.get('dimensions', {}).get(d, {}).get('verdict', '?')}"
                                for d in req.dims
                            )
                            consensus = phase2_result.get("consensus", "NEUTRAL")
                            summary = phase2_result.get("summary", "")
                            if consensus == "FAIL":
                                console.print(
                                    f"  {symbol:12s} [red]PHASE2 FAIL[/red] — {summary}"
                                )
                                console.print(f"           {dims_str}")
                                continue
                            elif consensus == "NEUTRAL":
                                # fast_track: Phase 1 评分已确认技术信号, LLM 保守可放过
                                if req.tier == "fast_track":
                                    console.print(
                                        f"  {symbol:12s} [green]PHASE2 FAST_TRACK PASS[/green] — {summary}"
                                    )
                                    console.print(f"           {dims_str}")
                                    watch_only_flag = False
                                else:
                                    console.print(
                                        f"  {symbol:12s} [yellow]PHASE2 NEUTRAL[/yellow] — {summary}"
                                    )
                                    console.print(f"           {dims_str}")
                                    watch_only_flag = True
                            else:
                                console.print(
                                    f"  {symbol:12s} [green]PHASE2 PASS[/green] — {summary}"
                                )
                                console.print(f"           {dims_str}")
                                watch_only_flag = False
                        else:
                            # Phase 2 分析失败（如 LLM JSON 解析错误），保守降级
                            console.print(
                                f"  {symbol:12s} [yellow]PHASE2 ERROR[/yellow] — LLM analysis failed, defaulting to WATCH_ONLY"
                            )
                            watch_only_flag = True
                    else:
                        watch_only_flag = False

                    # 2d. 计算 ATR 止损
                    try:
                        kline_1h = exchange.get_kline(symbol, "1h", 50)
                        stop_price, atr_value = calculate_atr_stop(
                            kline_1h, direction, entry_price,
                            conservative=(args.mode == "conservative"),
                        )
                        # 动态 R:R: 高分信号放大利润目标
                        active_rr = args.rr
                        if score >= 8:
                            active_rr = max(active_rr, 4.0)  # 极强信号 4:1
                        elif score >= 7:
                            active_rr = max(active_rr, 3.0)  # 强信号 3:1
                        elif score >= 6:
                            active_rr = max(active_rr, 2.5)  # 中等偏强 2.5:1
                        if active_rr != args.rr:
                            log.info("%s: score=%d, R:R 从 %.1f 提升至 %.1f", symbol, score, args.rr, active_rr)
                        if direction == SignalDirection.LONG:
                            tp_price = entry_price + (entry_price - stop_price) * active_rr
                        else:
                            tp_price = entry_price - (stop_price - entry_price) * active_rr
                    except Exception as exc:
                        log.warning("ATR calculation failed for %s: %s", symbol, exc)
                        continue

                    # 2d. 获取资金费率
                    funding_rate = None
                    try:
                        funding_rate = exchange.get_funding_rate(symbol)
                    except Exception as exc:
                        log.warning("Funding rate fetch failed for %s: %s", symbol, exc)

                    # 2e. 计算预期下单量（Gate 需要此值做盘口冲击检查）
                    max_lev = args.max_leverage
                    if score >= 7:
                        leverage = max_lev
                    elif score >= 5:
                        leverage = max(1, max_lev // 2)
                    else:
                        leverage = 1
                    effective_position_size = min(
                        args.position_size,
                        config.execution_gate.max_position_pct / 100,
                    )
                    if effective_position_size < args.position_size:
                        log.info(
                            "%s: position-size %.1f%% capped to gate max_position_pct %.1f%%",
                            symbol, args.position_size * 100, config.execution_gate.max_position_pct,
                        )
                    position_margin = min(
                        positions.account_balance * effective_position_size,
                        _available_usdt,
                    )
                    order_notional = position_margin * leverage
                    order_notional = min(order_notional, positions.account_balance)
                    order_qty = order_notional / entry_price if entry_price > 0 else 0.0

                    # 2f. 构建 LiveSignal 并执行 Gate
                    live_signal = LiveSignal(
                        symbol=symbol,
                        direction=direction,
                        score=score,
                        entry_price=entry_price,
                        stop_loss=stop_price,
                        target_prices=[tp_price],
                    )

                    gate_result = gate_engine.run_gate(
                        live_signal, ticker, funding_rate, orderbook,
                        order_qty=order_qty,
                        account_balance=positions.account_balance,
                        order_margin=position_margin,
                    )

                    # Phase 2 NEUTRAL 降级: 即使 Gate PASS 也降为 WATCH_ONLY
                    if watch_only_flag and gate_result.status.value == "PASS":
                        gate_result.status = GateStatus.WATCH_ONLY

                    # 2f. 输出 Gate 结果
                    status_icon = {
                        "PASS": "[green]PASS[/green]",
                        "WATCH_ONLY": "[yellow]WATCH[/yellow]",
                        "REJECT": "[red]REJECT[/red]",
                    }.get(gate_result.status.value, "[dim]?[/dim]")

                    detail = "; ".join(
                        f"{c.name}: {'✅' if c.passed else '❌'} {c.detail}"
                        for c in gate_result.checks
                    )
                    console.print(
                        f"  {symbol:12s} {status_icon} "
                        f"score={score} "
                        f"entry={entry_price:.2f} "
                        f"SL={stop_price:.2f} "
                        f"TP={tp_price:.2f}"
                    )
                    console.print(f"           {detail}")

                    # 2g. Gate PASS → 开仓
                    if gate_result.status.value == "PASS":
                        # 复用 Gate 前置计算的下单量（见 2e 节）
                        notional = order_notional
                        if notional < 20:
                            console.print(
                                f"           [yellow]SKIP — 名义价值 ${notional:.1f} < $20 Binance 最小限额[/yellow]"
                            )
                            continue
                        if args.dry_run:
                            console.print(
                                f"           [yellow]DRY RUN — 跳过开仓: {symbol} "
                                f"{direction.value} {leverage}x qty ≈ {notional / entry_price:.6f} "
                                f"@ {entry_price:.2f}[/yellow]"
                            )
                        else:
                            # 风控检查: 开仓后总敞口是否超限
                            ok, reason = positions.can_open_new(symbol, additional_notional=notional)
                            if not ok:
                                log.info("Gate PASS but %s rejected by position cap: %s", symbol, reason)
                                console.print(f"           [yellow]SKIP — {reason}[/yellow]")
                                continue
                            quantity = round(notional / entry_price, 6)
                            if quantity <= 0:
                                log.warning("Quantity too small for %s, skipping", symbol)
                                continue
                            min_qty = exchange.get_min_qty(symbol)
                            if min_qty > 0 and quantity < min_qty:
                                log.info(
                                    "%s: qty=%.6f < minQty=%.6f, 跳过 (notional=%.2f 不足以交易该币种)",
                                    symbol, quantity, min_qty, notional,
                                )
                                console.print(
                                    f"           [yellow]SKIP — qty {quantity:.6f} < 最小交易量 {min_qty}, 跳过[/yellow]"
                                )
                                continue

                            log.info(
                                "OPENING %s %s %dx qty=%f entry=%.2f SL=%.2f TP=%.2f",
                                symbol, direction.value, leverage, quantity, entry_price, stop_price, tp_price,
                            )

                            try:
                                # Set futures leverage and margin mode before placing order
                                if hasattr(exchange, "set_leverage"):
                                    exchange.set_leverage(symbol, leverage=leverage)
                                if hasattr(exchange, "set_margin_mode"):
                                    exchange.set_margin_mode(symbol, "ISOLATED")
                                order = exchange.create_market_order(symbol, direction.value.lower(), quantity)
                                log.info("Order placed: %s", order)

                                # 确认订单已成交，超时未成交则取消+跳过
                                if order.get("status") in ("NEW", "PARTIALLY_FILLED"):
                                    time.sleep(2)
                                    try:
                                        status = exchange.fetch_order(order["order_id"], symbol)
                                        if status.get("status") == "NEW":
                                            exchange.cancel_order(order["order_id"], symbol)
                                            log.warning("Order %s cancelled — not filled after 2s", order["order_id"])
                                            console.print(f"           [yellow]⚠️ 订单未成交，已取消[/yellow]")
                                            continue
                                    except Exception:
                                        pass

                                positions.open_position(
                                    symbol=symbol,
                                    direction=direction.value,
                                    entry_price=entry_price,
                                    quantity=quantity,
                                    stop_loss=stop_price,
                                    take_profit=tp_price,
                                    leverage=leverage,
                                    entry_score=score,
                                )
                                cycle_orders += 1
                                total_orders += 1
                                console.print(
                                    f"           [green]✅ 开仓成功: {symbol} "
                                    f"{direction.value} qty={quantity} @ {entry_price:.2f}[/green]"
                                )
                            except Exception as exc:
                                log.error("Order failed for %s: %s", symbol, exc)
                                console.print(f"           [red]❌ 开仓失败: {exc}[/red]")
                    elif gate_result.status.value == "WATCH_ONLY":
                        log.info(
                            "WATCH_ONLY: %s %s score=%d — %s",
                            symbol, direction.value, score, gate_result.summary,
                        )

                if cycle_orders > 0:
                    console.print(f"[bold green]{cycle_orders} 个新订单已执行[/bold green]")
                else:
                    console.print("[dim]本轮无开仓[/dim]")


        except Exception as exc:
            log.exception("Cycle #%d failed: %s", cycle_count, exc)

        # ---- 绩效报告（每 10 轮） ----
        if cycle_count % 10 == 0:
            try:
                perf = positions.get_performance_metrics()
                trade_count = perf.get("trade_count", 0)
                if trade_count >= 3 and "error" not in perf:
                    # 按评分分档统计胜率
                    score_tiers = positions.get_win_rate_by_score_tier()
                    tier_lines = ""
                    for tier_name, tier_data in score_tiers.items():
                        if tier_data["count"] > 0:
                            tp = tier_data['total_pnl']
                            pnl_str = f"{tp:+.2f}" if abs(tp) >= 0.01 else f"{tp:+.4f}"
                            tier_lines += (
                                f"  {tier_name}: {tier_data['count']}笔 "
                                f"胜率={tier_data['win_rate']:.0%} "
                                f"总PnL={pnl_str}\n"
                            )
                    panel_text = (
                        f"[bold]绩效报告 (Cycle #{cycle_count})[/bold]\n"
                        f"  交易数: {trade_count}"
                        f"  胜率: {perf.get('win_rate', 0):.1%}"
                        f"  获利因子: {perf.get('profit_factor', 0):.2f}"
                        f"  夏普: {perf.get('sharpe', 0):.2f}"
                        f"  最大回撤: {perf.get('max_drawdown', 0):.2%}"
                        f"  总收益: {perf.get('total_return', 0):+.2%}\n"
                    )
                    if tier_lines.strip():
                        panel_text += f"\n[dim]按评分分档:[/dim]\n{tier_lines}"
                    console.print(Panel.fit(panel_text, border_style="cyan"))
                elif trade_count > 0:
                    console.print(f"[dim]绩效: 仅 {trade_count} 笔交易, 等待更多数据[/dim]")
            except Exception as exc:
                log.debug("Performance report failed: %s", exc)

        # ---- 极端指标值监控 ----
        if cycle_count % 3 == 0:
            try:
                extreme_warnings = []
                for r in report.rankings:
                    rsi = r.get("rsi_1h", 50)
                    if rsi < 20:
                        extreme_warnings.append(f"{r.get('symbol','')} RSI_1h={rsi:.1f}（超卖）")
                    elif rsi > 80:
                        extreme_warnings.append(f"{r.get('symbol','')} RSI_1h={rsi:.1f}（超买）")
                if extreme_warnings:
                    for w in extreme_warnings[:5]:
                        console.print(f"[yellow]⚠ 极端指标: {w}[/yellow]")
                    if len(extreme_warnings) > 5:
                        console.print(f"[dim]  另有 {len(extreme_warnings)-5} 个...[/dim]")
            except Exception:
                pass

        # ---- 等待到下一个周期 ----
        elapsed = time.time() - cycle_start
        remaining = interval_seconds - elapsed
        if remaining > 0:
            console.print(f"[dim]下一轮扫描在 {int(remaining/60)} 分钟后[/dim]")
            stop_event.wait(timeout=remaining)

    # ---- 清理 ----
    log.info("Stopping TPSL Monitor...")
    monitor.stop()
    log.info("实盘系统已停止。本轮共执行 %d 个订单。", total_orders)
    return 0


if __name__ == "__main__":
    sys.exit(main())
