#!/usr/bin/env python3
"""A-share live trading entry — scan, gate, mock/dry-run execution.

Usage:
    python extensions/run_astock_trading.py --mock
    python extensions/run_astock_trading.py --dry-run --interval 30
    python extensions/run_astock_trading.py --live --confirm-live I_UNDERSTAND
"""

from __future__ import annotations

import argparse
import logging
import signal as _signal
import sys
import time
from pathlib import Path
from threading import Event

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_SCRIPT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("astock_trading")

LIVE_CONFIRM = "I_UNDERSTAND"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Vibe Trading A-share live system")
    p.add_argument("--mock", action="store_true", help="Mock data + mock broker")
    p.add_argument("--dry-run", action="store_true", help="Scan only, no orders")
    p.add_argument("--live", action="store_true", help="Enable order placement")
    p.add_argument("--confirm-live", default="", help=f"Must be {LIVE_CONFIRM} for --live")
    p.add_argument("--interval", type=int, default=30, help="Scan interval minutes")
    p.add_argument("--balance", type=float, default=500_000.0, help="Initial cash (mock)")
    p.add_argument("--data-source", default="akshare,mock", help="Comma-separated data backends")
    p.add_argument("--broker", default="mock", choices=["mock", "xtquant", "easytrader"])
    p.add_argument("--market-index", default="000300.SH", help="Benchmark index code")
    p.add_argument("--mode", choices=["default", "conservative", "aggressive"], default="default")
    p.add_argument("--no-phase2", action="store_true", help="Skip phase2 stub")
    p.add_argument("--once", action="store_true", help="Single scan cycle then exit")
    p.add_argument("--universe", default="", help="Comma-separated stock codes (default: config or mock universe)")
    p.add_argument("--market", default="astock", help=argparse.SUPPRESS)
    args, _unknown = p.parse_known_args()
    return args


def build_config(args: argparse.Namespace):
    from extensions.live_trading.astock.config import AStockTradingConfig

    if args.mode == "conservative":
        cfg = AStockTradingConfig.conservative()
    elif args.mode == "aggressive":
        cfg = AStockTradingConfig.aggressive()
    else:
        cfg = AStockTradingConfig()
    cfg.data_sources = [s.strip() for s in args.data_source.split(",") if s.strip()]
    cfg.market_index = args.market_index
    cfg.scan_interval_minutes = args.interval
    if args.universe:
        cfg.universe = [s.strip().upper() for s in args.universe.split(",") if s.strip()]
    if args.mock and "mock" not in cfg.data_sources:
        cfg.data_sources.append("mock")
    return cfg


def run_cycle(
    scheduler,
    gate_engine,
    exchange,
    positions,
    phase2,
    config,
    trading_enabled: bool,
    balance: float,
) -> int:
    from extensions.live_trading.astock.atr_stop import calculate_astock_stop
    from extensions.live_trading.astock.models import AStockSignal, GateStatus

    report = scheduler.run_once()
    log.info(
        "session=%s market=%s rankings=%d phase2=%d positions=%d",
        report.session,
        report.market_status,
        len(report.rankings),
        len(report.phase2_requests),
        report.active_positions,
    )

    if report.market_status == "LOCK_ALL":
        return 0

    orders = 0
    for req in report.phase2_requests:
        ranking = next((r for r in report.rankings if r["symbol"] == req.symbol), None)
        if not ranking:
            continue
        kline = exchange.get_daily(req.symbol, 120)
        entry = float(ranking.get("entry_price", 0))
        stop, tp, _atr = calculate_astock_stop(kline, entry, config.stop)
        signal = AStockSignal(
            symbol=req.symbol,
            name=req.name,
            score=req.score,
            entry_price=entry,
            stop_loss=stop,
            take_profit=tp,
            volume_ratio=float(ranking.get("vol_ratio", 1)),
            change_5d_pct=float(ranking.get("change_5d_pct", 0)),
            board=str(ranking.get("board", "主板")),
        )
        if phase2:
            phase2.enrich_signal(signal, req)
        if signal.score < 5:
            continue

        lot = config.lot_size
        max_notional = balance * config.position_size_pct
        shares = max(lot, int(max_notional / entry) // lot * lot) if entry > 0 else lot
        gate_engine.run_gate(signal, exchange, balance, shares)
        if signal.gate_result and signal.gate_result.status == GateStatus.REJECT:
            log.info("REJECT %s: %s", signal.symbol, signal.gate_result.summary)
            continue

        if not trading_enabled:
            log.info("DRY signal %s score=%d entry=%.2f sl=%.2f tp=%.2f", signal.symbol, signal.score, entry, stop, tp)
            continue

        if exchange.is_limit(signal.symbol) == "up":
            log.info("SKIP limit-up %s", signal.symbol)
            continue

        result = exchange.create_buy_order(signal.symbol, entry, shares)
        if result.get("status") == "filled":
            from extensions.live_trading.astock.models import AStockPosition

            positions.open_position(
                AStockPosition(
                    symbol=signal.symbol,
                    name=signal.name,
                    shares=int(result.get("filled", shares)),
                    entry_price=entry,
                    stop_loss=stop,
                    take_profit=tp,
                    is_today_buy=True,
                )
            )
            orders += 1
            log.info("BUY %s shares=%s @ %.2f", signal.symbol, shares, entry)
    return orders


def main() -> int:
    args = parse_args()
    if args.live and args.confirm_live != LIVE_CONFIRM:
        log.error("Live requires --confirm-live %s", LIVE_CONFIRM)
        return 2
    trading_enabled = args.live and not args.dry_run
    if args.mock:
        args.data_source = "mock"

    from extensions.live_trading.astock.config import AStockTradingConfig
    from extensions.live_trading.astock.exchange import create_astock_exchange
    from extensions.live_trading.astock.gate import AStockGateEngine
    from extensions.live_trading.astock.phase2 import AStockPhase2Analyzer
    from extensions.live_trading.astock.position import AStockPositionTracker
    from extensions.live_trading.astock.scheduler import AStockScheduler
    from extensions.live_trading.astock.tpsl_monitor import AStockTPSLMonitor

    config = build_config(args)
    exchange = create_astock_exchange("mock" if args.mock else args.broker, config)
    positions = AStockPositionTracker()
    scheduler = AStockScheduler(exchange, positions, config, trading_enabled=trading_enabled)
    gate = AStockGateEngine(config)
    phase2 = None if args.no_phase2 else AStockPhase2Analyzer(enabled=True)
    monitor = AStockTPSLMonitor(exchange, positions, config)
    monitor.start()

    stop_event = Event()

    def _stop(*_a: object) -> None:
        stop_event.set()

    _signal.signal(_signal.SIGINT, _stop)
    _signal.signal(_signal.SIGTERM, _stop)

    balance = args.balance
    cycle = 0
    try:
        while not stop_event.is_set():
            cycle += 1
            try:
                bal = exchange.get_account_info()
                balance = float(bal.get("total", balance))
            except Exception:
                pass
            n = run_cycle(scheduler, gate, exchange, positions, phase2, config, trading_enabled, balance)
            log.info("cycle #%d orders=%d balance=%.0f", cycle, n, balance)
            if args.once:
                break
            stop_event.wait(timeout=max(60, args.interval * 60))
    finally:
        monitor.stop()
        monitor.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
