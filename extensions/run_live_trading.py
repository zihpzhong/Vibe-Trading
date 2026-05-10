"""实盘交易系统入口 — 全流程自动扫描+自动交易.

完整链路:
  BTC传导 → Phase 1 扫描 → ATR止损 → Execution Gate → 自动开仓 → TP/SL守护

Usage:
    python extensions/run_live_trading.py              # 实盘全自动
    python extensions/run_live_trading.py --mock       # 模拟测试
    python extensions/run_live_trading.py --dry-run    # 仅扫描不交易
"""

from __future__ import annotations

import argparse
import logging
import os
import signal as _signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# ------------------------------------------------------------
# 确保能找到 extensions/ 和 agent/src 包
# ------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent  # /app (= project root)
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_SCRIPT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live_trading")

console = Console()

# 默认开仓比例: 单次开仓使用资金比例
DEFAULT_POSITION_SIZE_PCT = 0.05  # 5% of account
# 默认R:R = 2:1 计算止盈
DEFAULT_REWARD_RISK_RATIO = 2.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vibe Trading 全流程实盘自动交易系统")
    parser.add_argument("--mock", action="store_true", help="使用模拟交易所（测试用）")
    parser.add_argument("--dry-run", action="store_true", help="扫描但不交易")
    parser.add_argument("--interval", type=int, default=15, help="扫描间隔（分钟）")
    parser.add_argument("--balance", type=float, default=50.0, help="账户 USDT 余额")
    parser.add_argument("--mode", choices=["default", "conservative", "aggressive"],
                        default="default", help="风控模式")
    parser.add_argument("--position-size", type=float, default=0.2,
                        help=f"单次开仓比例 (默认 {0.2:.0%})")
    parser.add_argument("--rr", type=float, default=DEFAULT_REWARD_RISK_RATIO,
                        help=f"目标 R:R 止盈比 (默认 {DEFAULT_REWARD_RISK_RATIO}:1)")
    parser.add_argument("--no-phase2", action="store_true",
                        help="跳过 Phase 2 LLM 深度分析 (仅 Phase 1 + Gate)")
    return parser.parse_args()


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
    table.add_row("[bold]Exposure[/bold]", f"{exposure:.1%}")
    table.add_row("[bold]Last Scan[/bold]", last_scan_time)
    table.add_row("[bold]Rankings[/bold]", str(rankings_count))
    table.add_row("[bold]Orders[/bold]", str(orders_count))
    return table


def main() -> int:
    args = parse_args()

    # ---- 导入 (from extensions, not upstream) ----
    from extensions.live_trading.engine.exchange import create_exchange
    from extensions.live_trading.engine.position_tracker import PositionTracker
    from extensions.live_trading.engine.scheduler import TradingScheduler
    from extensions.live_trading.engine.tpsl_monitor import TPSLMonitor
    from extensions.live_trading.engine.atr_stop import calculate_atr_stop
    from extensions.live_trading.engine.execution_gate import ExecGateEngine
    from extensions.live_trading.engine.phase2 import Phase2Analyzer
    from extensions.live_trading.config import LiveTradingConfig
    from extensions.live_trading.models import GateStatus, LiveSignal, SignalDirection

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
    try:
        actual_balance = exchange.get_account_balance()
        if actual_balance:
            usdt_bal = actual_balance.get("USDT", 0)
            if usdt_bal < args.balance:
                log.warning(
                    "Futures wallet USDT balance: %.2f (--balance=%.2f may be inaccurate)",
                    usdt_bal, args.balance,
                )
                if usdt_bal < 10:
                    log.error(
                        "Insufficient futures wallet balance (%.2f USDT). "
                        "Please transfer USDT from Spot wallet to Futures wallet on Binance.",
                        usdt_bal,
                    )
            else:
                log.info("Futures wallet USDT balance: %.2f", usdt_bal)
    except Exception:
        log.info("Could not query account balance (non-fatal)")

    # ---- 设置合约模式：逐仓 + 单向持仓 ----
    if hasattr(exchange, "set_position_mode") and not args.mock:
        exchange.set_position_mode(dual=False)

    # ---- 持仓管理 ----
    positions = PositionTracker(
        account_balance=args.balance,
        max_positions=3,
        max_exposure_pct=args.position_size * 3 * 1.1,  # 3 仓 + 10% 缓冲
    )

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
    monitor = TPSLMonitor(exchange, positions, poll_interval=5.0)
    monitor.start()
    log.info("TPSL Monitor started")

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
        f"  余额: {args.balance} USDT"
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

        try:
            # ================================================================
            # STEP 1: Scheduler — BTC传导 + Phase 1 扫描 + 分级决策
            # ================================================================
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
                except Exception:
                    pass

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
            if report.phase2_requests:
                console.print(f"[bold]--- 自动交易评估 ({'LIVE' if not args.dry_run else 'DRY RUN'}) ---[/bold]")

                for req in report.phase2_requests:
                    symbol = req.symbol
                    direction = SignalDirection(req.direction)
                    score = req.score
                    entry_price = req.entry_price

                    # 2a. 检查是否可以开新仓
                    if not positions.can_open_new(symbol):
                        console.print(f"  {symbol} [yellow]SKIP[/yellow] 仓位已达上限或已有持仓")
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
                            consensus = phase2_result.get("consensus", "PASS")
                            summary = phase2_result.get("summary", "")
                            if consensus == "FAIL":
                                console.print(
                                    f"  {symbol:12s} [red]PHASE2 FAIL[/red] — {summary}"
                                )
                                console.print(f"           {dims_str}")
                                continue
                            elif consensus == "NEUTRAL":
                                console.print(
                                    f"  {symbol:12s} [yellow]PHASE2 NEUTRAL[/yellow] — {summary}"
                                )
                                console.print(f"           {dims_str}")
                                # 降级为 WATCH_ONLY 处理: 继续计算但标记
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
                        # 计算止盈 (R:R = args.rr:1)
                        if direction == SignalDirection.LONG:
                            tp_price = entry_price + (entry_price - stop_price) * args.rr
                        else:
                            tp_price = entry_price - (stop_price - entry_price) * args.rr
                    except Exception as exc:
                        log.warning("ATR calculation failed for %s: %s", symbol, exc)
                        continue

                    # 2d. 获取资金费率
                    funding_rate = 0.0
                    try:
                        funding_rate = exchange.get_funding_rate(symbol)
                    except Exception:
                        pass  # 非 futures 交易对无资金费率，跳过

                    # 2e. 构建 LiveSignal 并执行 Gate
                    live_signal = LiveSignal(
                        symbol=symbol,
                        direction=direction,
                        score=score,
                        entry_price=entry_price,
                        stop_loss=stop_price,
                        target_prices=[tp_price],
                    )

                    gate_result = gate_engine.run_gate(live_signal, ticker, funding_rate, orderbook)

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
                        notional = args.balance * args.position_size
                        if notional < 5:
                            console.print(
                                f"           [yellow]SKIP — 名义价值 ${notional:.1f} < $5 最小限额[/yellow]"
                            )
                            continue
                        if args.dry_run:
                            console.print(
                                f"           [yellow]DRY RUN — 跳过开仓: {symbol} "
                                f"{direction.value} qty ≈ {notional / entry_price:.6f} "
                                f"@ {entry_price:.2f}[/yellow]"
                            )
                        else:
                            quantity = round(args.balance * args.position_size / entry_price, 6)
                            if quantity <= 0:
                                log.warning("Quantity too small for %s, skipping", symbol)
                                continue

                            log.info(
                                "OPENING %s %s qty=%f entry=%.2f SL=%.2f TP=%.2f",
                                symbol, direction.value, quantity, entry_price, stop_price, tp_price,
                            )

                            try:
                                # Set futures leverage and margin mode before placing order
                                if hasattr(exchange, "set_leverage"):
                                    exchange.set_leverage(symbol, leverage=5)
                                if hasattr(exchange, "set_margin_mode"):
                                    exchange.set_margin_mode(symbol, "ISOLATED")
                                order = exchange.create_market_order(symbol, direction.value.lower(), quantity)
                                log.info("Order placed: %s", order)

                                positions.open_position(
                                    symbol=symbol,
                                    direction=direction.value,
                                    entry_price=entry_price,
                                    quantity=quantity,
                                    stop_loss=stop_price,
                                    take_profit=tp_price,
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
