"""实盘交易系统入口 — 全流程自动扫描+自动交易.

完整链路:
  BTC传导 → Phase 1 扫描 → ATR止损 → Execution Gate → 自动开仓 → TP/SL守护

Usage:
    python extensions/ext_cli/run_live_trading.py              # 默认 dry-run，仅扫描不交易
    python extensions/ext_cli/run_live_trading.py --mock       # 模拟测试
    python extensions/ext_cli/run_live_trading.py --live --confirm-live I_UNDERSTAND
"""

from __future__ import annotations

import argparse
import json
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
_EXT_ROOT = _SCRIPT_DIR.parent  # extensions/
_PROJECT_ROOT = _EXT_ROOT.parent  # project root
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_EXT_ROOT))

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

# ---- 配置加载 ----
_CONFIG_JSON_PATH = Path(__file__).resolve().parent.parent / "config" / "config.json"
"""Path to the centralized config.json file."""

_JSON_CONFIG_CACHE: dict | None = None
"""Cached parsed config.json contents (load once)."""


def _load_json_config() -> dict:
    """Load config.json into a dict.

    Returns:
        dict with top-level sections (exchange, trading, execution_gate, etc.)
        Falls back to empty dict if file is missing/invalid.  Results are
        cached after the first read.
    """
    global _JSON_CONFIG_CACHE
    if _JSON_CONFIG_CACHE is not None:
        return _JSON_CONFIG_CACHE
    try:
        import json as _json
        with open(_CONFIG_JSON_PATH, encoding="utf-8") as _f:
            _JSON_CONFIG_CACHE = _json.load(_f)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        _JSON_CONFIG_CACHE = {}
    return _JSON_CONFIG_CACHE


# 默认参数常量（保留作为 config.json 缺失时的 fallback）
DEFAULT_POSITION_SIZE_PCT = 0.12  # 12%
DEFAULT_REWARD_RISK_RATIO = 2.0
DEFAULT_MAX_POSITIONS = 3
DEFAULT_MAX_SAME_DIRECTION = 2
DEFAULT_MIN_ENTRY_SCORE = 5
SCORE_TIER_HALF_SIZE = 5  # score==5 → 50% position size
DEFAULT_MAX_DAILY_LOSS_PCT = 0.05  # 5%
DEFAULT_MAX_ROLLING_DRAWDOWN_PCT = 0.05  # 5%
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND"


def _get_cfg(section: str, key: str, default: object = None) -> object:
    """Read a value from config.json, returning default if missing."""
    cfg = _load_json_config()
    return cfg.get(section, {}).get(key, default)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vibe Trading 全流程实盘自动交易系统")
    parser.add_argument(
        "--market",
        choices=["crypto", "astock"],
        default="crypto",
        help="市场类型: crypto=加密永续 (默认), astock=A股",
    )
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
    parser.add_argument(
        "--swarm-phase2-shadow",
        action="store_true",
        help="enhanced 档后台并行 Swarm 分析 (shadow 模式，不影响开仓，写 swarm_shadow.jsonl)",
    )
    parser.add_argument(
        "--phase2-enhanced-strict",
        action="store_true",
        help="enhanced 档 Phase 2 需至少 2 维 PASS 才继续 (否则 SKIP)",
    )
    parser.add_argument(
        "--pairs", nargs="*", default=None,
        help="交易对白名单 e.g. --pairs BTC ETH SOL (空=Top-N 模式, 也支持 TRADING_PAIRS 环境变量)",
    )
    parser.add_argument(
        "--exchange", choices=["binance", "bitget"], default=None,
        help="交易所 (默认: CRYPTO_EXCHANGE 环境变量, 再 fallback binance)",
    )
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
    if args.market == "astock":
        from extensions import run_astock_trading

        return run_astock_trading.main()
    live_mode_error = validate_live_mode(args)
    if live_mode_error:
        log.error(live_mode_error)
        console.print(f"[red]{live_mode_error}[/red]")
        return 2

    # ---- 导入 (from extensions, not upstream) ----
    from extensions.trading.crypto.live.exchange import create_exchange
    from extensions.trading.crypto.live.position_tracker import PositionTracker
    from extensions.trading.crypto.live.scheduler import TradingScheduler
    from extensions.trading.crypto.live.tpsl_monitor import TPSLMonitor
    from extensions.trading.crypto.live.atr_stop import calculate_atr_stop
    from extensions.trading.crypto.live.execution_gate import ExecGateEngine
    from extensions.trading.crypto.live.phase2 import Phase2Analyzer
    from extensions.trading.crypto.live.swarm_phase2 import (
        SwarmPhase2Config,
        SwarmPhase2Engine,
        extract_alpha_context,
    )
    from extensions.trading.crypto.live.reconcile import reconcile_positions
    from extensions.trading.crypto.config import LiveTradingConfig
    from extensions.trading.crypto.models import GateStatus, LiveSignal, ScheduleReport, SignalDirection

    # ---- 配置 ----
    # 1) 从 config.json 加载（缺失则使用代码默认值）
    config = LiveTradingConfig.load_from_json(_CONFIG_JSON_PATH)
    # 2) 覆盖为 top-50 白名单（保留 JSON 中已加载的风控参数）
    whitelist_cfg = LiveTradingConfig.with_top50_whitelist()
    config.pair_whitelist = whitelist_cfg.pair_whitelist
    config.scan_top_n = whitelist_cfg.scan_top_n
    # 3) CLI 参数覆盖
    config.default_scan_interval_minutes = args.interval
    config.exchange_name = args.exchange or os.environ.get("CRYPTO_EXCHANGE", config.exchange_name)

    # 4) 从 config.json 解析交易循环参数
    _min_entry_score = int(_get_cfg("trading", "min_entry_score", DEFAULT_MIN_ENTRY_SCORE))  # type: ignore[arg-type]
    _score_half = int(_get_cfg("trading", "score_tier_half_size", SCORE_TIER_HALF_SIZE))  # type: ignore[arg-type]
    _trading_interval = args.interval

    # 模式覆盖（仅修改风控阈值，保留 pair_whitelist）
    if args.mode == "conservative":
        from extensions.trading.crypto.config import ExecutionGateConfig as _EGC
        config.execution_gate = _EGC(
            min_liquidity_usdt=2_000_000,
            max_orderbook_impact_pct=0.3,
            min_risk_reward_ratio=1.5,
            max_position_pct=2.0,
            signal_cooldown_minutes=60,
        )
    elif args.mode == "aggressive":
        from extensions.trading.crypto.config import ExecutionGateConfig as _EGC
        config.execution_gate = _EGC(
            min_liquidity_usdt=500_000,
            max_orderbook_impact_pct=1.0,
            min_risk_reward_ratio=0.8,
            max_position_pct=10.0,
            signal_cooldown_minutes=15,
        )

    # 交易对白名单: --pairs CLI > TRADING_PAIRS 环境变量 > top-50 默认
    if args.pairs is not None:
        config.pair_whitelist = list(args.pairs)
    elif os.environ.get("TRADING_PAIRS"):
        config.pair_whitelist = [
            p.strip() for p in os.environ["TRADING_PAIRS"].split(",") if p.strip()
        ]
    # else: 已由 with_top50_whitelist() 设置

    config_err = config.validate()
    if config_err:
        log.error("Config validation failed: %s", config_err)
        return 1

    mode_label = {"default": "默认", "conservative": "保守", "aggressive": "激进"}
    # ---- 交易所 ----
    exchange_name = args.exchange or os.environ.get("CRYPTO_EXCHANGE", "binance")
    exchange = create_exchange(mock=args.mock, exchange_name=exchange_name)
    log.info(
        "Exchange: %s (mock=%s, exchange=%s, auth=%s)",
        type(exchange).__name__,
        args.mock,
        exchange_name,
        getattr(exchange, "has_auth", False),
    )

    # ---- 白名单交易所交叉校验（仅非 Binance 时） ----
    if exchange_name != "binance" and hasattr(exchange, "validate_symbols") and config.pair_whitelist:
        valid = exchange.validate_symbols(config.pair_whitelist)
        removed = set(config.pair_whitelist) - set(valid)
        if removed:
            log.warning(
                "交易所切换检测: %d 个交易对在 %s 上不可用: %s",
                len(removed), exchange_name, sorted(removed)[:10],
            )
            config.pair_whitelist = valid

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
    _max_pos = int(_get_cfg("trading", "max_positions", DEFAULT_MAX_POSITIONS))  # type: ignore[arg-type]
    _max_dir = int(_get_cfg("trading", "max_same_direction", DEFAULT_MAX_SAME_DIRECTION))  # type: ignore[arg-type]
    _max_exp = float(_get_cfg("trading", "max_exposure_pct", 3.0))  # type: ignore[arg-type]
    positions = PositionTracker(
        account_balance=effective_balance,
        max_positions=_max_pos,
        max_same_direction=_max_dir,
        max_exposure_pct=_max_exp,
    )
    # 确保 _load() 不覆盖构造函数传入的交易所真实余额
    if actual_usdt_balance is not None:
        positions.account_balance = actual_usdt_balance
        # 同步 initial_balance，避免 _load() 留下的旧 JSON 值
        # 影响绩效指标准确性
        positions._initial_balance = actual_usdt_balance

    # ---- Phase 2 分析引擎 ----
    phase2_analyzer = Phase2Analyzer() if not args.no_phase2 else None
    if phase2_analyzer:
        log.info("Phase 2 deep analysis enabled (LLM-driven skills)")
    else:
        log.info("Phase 2 disabled (--no-phase2)")

    swarm_shadow_engine: SwarmPhase2Engine | None = None
    if args.swarm_phase2_shadow:
        swarm_shadow_engine = SwarmPhase2Engine(SwarmPhase2Config(enabled=True, shadow_only=True))
        log.info("Swarm Phase 2 shadow enabled (enhanced tier, log=%s)", swarm_shadow_engine._log_path)
    if args.phase2_enhanced_strict:
        log.info("Phase 2 enhanced strict: require >=2 dim PASS")

    # ---- Gate 引擎 ----
    gate_engine = ExecGateEngine(config)

    # ---- 调度器（始终生成 Phase2Request，下单由 dry_run 控制） ----
    scheduler = TradingScheduler(exchange, positions, trading_enabled=True)

    # ---- TP/SL 守护 ----
    _tpsl_poll = float(_get_cfg("tpsl_monitor", "poll_interval_seconds", 5.0))  # type: ignore[arg-type]
    _tpsl_trail_act = float(_get_cfg("tpsl_monitor", "trailing_activation_pct", 3.0))  # type: ignore[arg-type]
    _tpsl_trail_dist = float(_get_cfg("tpsl_monitor", "trail_distance_pct", 1.5))  # type: ignore[arg-type]
    monitor = TPSLMonitor(
        exchange, positions,
        poll_interval=_tpsl_poll,
        trailing_activation_pct=_tpsl_trail_act,
        trail_distance_pct=_tpsl_trail_dist,
        de_risk_config=config.de_risk,
        dca_config=config.dca,
        dca_gate_engine=gate_engine,
        max_leverage=args.max_leverage,
        position_size_pct=args.position_size,
        use_exchange_brackets=config.use_exchange_bracket_orders and not args.mock,
    )
    monitor.start()
    log.info("TPSL Monitor started (de-risk: [%.0f%%:%.0f%%, %.0f%%:%.0f%%, %.0f%%:%.0f%%], doom=%.0f%%)",
             config.de_risk.level1_loss_pct, config.de_risk.level1_sell_fraction * 100,
             config.de_risk.level2_loss_pct, config.de_risk.level2_sell_fraction * 100,
             config.de_risk.level3_loss_pct, config.de_risk.level3_sell_fraction * 100,
             config.de_risk.doom_loss_pct)

    # ---- 回溯补挂交易所止盈止损单 / Retroactive exchange bracket placement ----
    # 重启后 SQLite 恢复的持仓可能缺 sl_order_id/tp_order_id，需补挂条件单。
    # After restart, restored positions may lack sl_order_id/tp_order_id; place missing brackets.
    if config.use_exchange_bracket_orders and not args.mock:
        from extensions.trading.crypto.live.exchange_brackets import (
            cancel_orphan_exchange_brackets,
            cancel_symbol_bracket_algos,
            has_bracket_support,
            place_bracket_orders,
            sanitize_bracket_order_ids,
        )

        if has_bracket_support(exchange):
            active_syms = {p.symbol for p in positions.get_active_positions()}
            cancel_orphan_exchange_brackets(exchange, active_syms)
            for pos in positions.get_active_positions():
                if pos.stop_loss is None or pos.stop_loss <= 0:
                    continue
                clean_sl, clean_tp = sanitize_bracket_order_ids(exchange, pos)
                if clean_sl != pos.sl_order_id or clean_tp != pos.tp_order_id:
                    positions.set_bracket_order_ids(pos.symbol, clean_sl, clean_tp)
                    pos.sl_order_id = clean_sl
                    pos.tp_order_id = clean_tp
                keep_ids = frozenset(x for x in (clean_sl, clean_tp) if x)
                deduped = cancel_symbol_bracket_algos(exchange, pos.symbol, keep_ids=keep_ids)
                if deduped:
                    log.info(
                        "Startup dedupe: removed %d extra bracket order(s) for %s",
                        deduped,
                        pos.symbol,
                    )
                needs_sl = bool(pos.stop_loss and pos.stop_loss > 0)
                needs_tp = bool(pos.take_profit and pos.take_profit > 0)
                sl_ok = not needs_sl or bool(clean_sl)
                tp_ok = not needs_tp or bool(clean_tp)
                if sl_ok and tp_ok:
                    continue
                log.info("Restro bracket for %s %s SL=%.4f TP=%s",
                         pos.symbol, pos.direction, pos.stop_loss,
                         f"{pos.take_profit:.4f}" if pos.take_profit else "N/A")
                sl_id, tp_id = place_bracket_orders(exchange, pos)
                positions.set_bracket_order_ids(pos.symbol, sl_id, tp_id)
                if sl_id or tp_id:
                    log.info("  → SL=%s TP=%s", sl_id or "-", tp_id or "-")
                else:
                    log.warning("  → bracket placement returned no order IDs")
        else:
            log.info("Exchange lacks bracket support — skip retroactive bracket placement")

    # ---- 日亏损熔断 ----
    # ---- 日亏损熔断 ----
    class DailyRiskTracker:
        """跟踪当日已实现盈亏，超过阈值时熔断交易。

        仅在持仓平仓时记录已实现盈亏，不受浮动盈亏波动影响。
        熔断后进入冷却期（默认4小时），冷却结束后可恢复交易。
        """
        def __init__(
            self,
            max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT,
            max_rolling_drawdown_pct: float = DEFAULT_MAX_ROLLING_DRAWDOWN_PCT,
            cooldown_hours: float = 4.0,
        ):
            self.max_daily_loss_pct = max_daily_loss_pct
            self.max_rolling_drawdown_pct = max_rolling_drawdown_pct
            self.cooldown_hours = cooldown_hours
            self._day_key = ""
            self._realized_pnl: float = 0.0  # 当日已实现盈亏
            self._last_closed_count: int = 0  # 已处理的平仓记录数
            self._suspended_until: float = 0.0  # 熔断解除时间戳
            self._rolling_tripped: bool = False

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
                self._rolling_tripped = False
                # 新交易日解除熔断
                if self._suspended_until > 0:
                    log.info("DailyRiskTracker: 新交易日, 解除熔断")
                    self._suspended_until = 0.0
                log.info(
                    "DailyRiskTracker reset for %s (daily %.0f%%, rolling %.0f%%)",
                    today, self.max_daily_loss_pct * 100, self.max_rolling_drawdown_pct * 100,
                )

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

        def check_rolling_drawdown(self) -> bool:
            """Trip circuit breaker if 24h peak-to-current balance drawdown exceeds limit."""
            if positions.account_balance <= 0:
                return False
            dd = positions.get_rolling_drawdown_pct(hours=24)
            if dd >= self.max_rolling_drawdown_pct:
                now = time.time()
                if self._suspended_until <= now:
                    self._suspended_until = now + self.cooldown_hours * 3600
                    log.warning(
                        "⚠️ 24h 滚动回撤熔断: 回撤 %.1f%% (阈值 %.0f%%), 冷却 %.0f 小时",
                        dd * 100, self.max_rolling_drawdown_pct * 100, self.cooldown_hours,
                    )
                self._rolling_tripped = True
                return True
            return False

        @property
        def is_blown(self) -> bool:
            """是否触发熔断。

            熔断条件：
            1. 当日已实现亏损超过 max_daily_loss_pct * account_balance
            2. 或 24h 滚动峰值回撤超过 max_rolling_drawdown_pct
            3. 且冷却时间尚未结束
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
            if self.check_rolling_drawdown():
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

    daily_risk = DailyRiskTracker(
        max_daily_loss_pct=float(_get_cfg("trading", "max_daily_loss_pct", DEFAULT_MAX_DAILY_LOSS_PCT)),  # type: ignore[arg-type]
        max_rolling_drawdown_pct=float(_get_cfg("trading", "max_rolling_drawdown_pct", DEFAULT_MAX_ROLLING_DRAWDOWN_PCT)),  # type: ignore[arg-type]
        cooldown_hours=float(_get_cfg("trading", "cooldown_hours", 4.0)),  # type: ignore[arg-type]
    )

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
                report = scheduler.run_once(top_n=config.scan_top_n, whitelist=config.pair_whitelist or None)

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
                    log.info(
                        "CLOSE %s %s exit=%.4f PnL=%s USDT (%.2f%%) %s",
                        c.symbol, c.direction, c.exit_price, pnl_str, c.pnl_pct, c.reason,
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
                    new_balance = float(bal["USDT"])
                    old_balance = positions.account_balance
                    # 突变保护：余额单周期暴跌超过 50% 时拒绝覆盖
                    # 防止交易所 API 偶发错误值覆盖正确余额（历史事故: 53→3.37）
                    if old_balance > 0 and new_balance < old_balance * 0.5:
                        log.error(
                            "余额暴跌检测: %.2f → %.2f (%.1f%%)，跳过同步以防 API 错误数据",
                            old_balance, new_balance,
                            (1 - new_balance / old_balance) * 100,
                        )
                    else:
                        positions.account_balance = new_balance
                # 同步可用余额（开仓保证金用）
                # 说明：wallet balance（总权益）用作 exposure 计算分母，
                #       available balance（可用余额）用作开仓金额上限。
                #       两者从相同 API 读取不同字段，有持仓时可用 < 总权益，属正常现象。
                free_bal = exchange.get_available_balance()
                _available_usdt = float(free_bal.get("USDT", 0)) if free_bal else 0
            except Exception as exc:
                log.warning("余额同步失败: %s", exc)
                _available_usdt = 0

            # 2aab. 交易所持仓对账（仅实盘 + 有 get_positions）
            if not args.mock and not args.dry_run and hasattr(exchange, "get_positions"):
                try:
                    exch_pos = exchange.get_positions()

                    def _mark_price(sym: str) -> float:
                        try:
                            return float(exchange.get_ticker(sym).get("last", 0))
                        except Exception:
                            return 0.0

                    summary = reconcile_positions(
                        positions, exch_pos,
                        price_lookup=_mark_price,
                        exchange=exchange if config.use_exchange_bracket_orders and not args.mock else None,
                    )
                    if summary.get("removed") or summary.get("adopted"):
                        console.print(
                            f"[yellow]持仓对账: 移除幽灵 {summary.get('removed')} "
                            f"采纳交易所 {summary.get('adopted')}[/yellow]"
                        )
                except Exception as exc:
                    log.warning("持仓对账失败: %s", exc)

            if report.phase2_requests:
                console.print(f"[bold]--- 自动交易评估 ({'LIVE' if not args.dry_run else 'DRY RUN'}) ---[/bold]")
                ranking_by_symbol = {str(r.get("symbol", "")): r for r in report.rankings}

                for req in report.phase2_requests:
                    symbol = req.symbol
                    direction = SignalDirection(req.direction)
                    score = req.score
                    entry_price = req.entry_price

                    # 2a. 评分门槛
                    if score < _min_entry_score:
                        log.info("%s SKIP score=%d < min %d", symbol, score, _min_entry_score)
                        console.print(
                            f"  {symbol} [yellow]SKIP[/yellow] 评分 {score} < 最低 {_min_entry_score}"
                        )
                        continue

                    # 2a2. 检查是否可以开新仓（含同向仓位上限）
                    ok, reason = positions.can_open_new(
                        symbol, direction=req.direction,
                    )
                    if not ok:
                        log.info("%s SKIP can_open_new: %s", symbol, reason)
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

                    # 2bb. 获取资金费率（用于 Phase 2 分析和 Gate）
                    funding_rate = None
                    try:
                        funding_rate = exchange.get_funding_rate(symbol)
                    except Exception as exc:
                        log.warning("Funding rate fetch failed for %s: %s", symbol, exc)

                    # 2c. Phase 2: LLM深度分析 — load_skill per dim → 综合评分
                    watch_only_flag = False
                    phase2_result: dict | None = None
                    alpha_context = extract_alpha_context(ranking_by_symbol.get(symbol, {}))
                    if phase2_analyzer and req.dims:
                        phase2_result = phase2_analyzer.analyze(
                            req, ticker,
                            funding_rate=funding_rate,
                            orderbook=orderbook,
                            btc_1h_trend=btc_hint,
                            alpha_context=alpha_context or None,
                        )
                        if phase2_result:
                            dims_str = "; ".join(
                                f"{d}:{(phase2_result.get('dimensions') or {}).get(d, {}).get('verdict', '?')}"
                                for d in req.dims
                            )
                            consensus = phase2_result.get("consensus", "NEUTRAL")
                            summary = phase2_result.get("summary", "")

                            # Swarm shadow fires for ALL enhanced tier (regardless of gate decision)
                            if swarm_shadow_engine and req.tier == "enhanced":
                                swarm_shadow_engine.submit_shadow(
                                    req,
                                    ticker,
                                    funding_rate=funding_rate,
                                    orderbook=orderbook,
                                    btc_1h_trend=btc_hint,
                                    mono_result=phase2_result,
                                )

                            if (
                                args.phase2_enhanced_strict
                                and req.tier == "enhanced"
                                and consensus != "FAIL"
                            ):
                                pass_count = sum(
                                    1
                                    for d in req.dims
                                    if (phase2_result.get("dimensions") or {}).get(d, {}).get("verdict") == "PASS"
                                )
                                if pass_count < 2:
                                    console.print(
                                        f"  {symbol:12s} [yellow]PHASE2 STRICT SKIP[/yellow] — "
                                        f"enhanced needs >=2 PASS dims (got {pass_count})"
                                    )
                                    console.print(f"           {dims_str}")
                                    continue
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
                                elif all(
                                    (phase2_result.get("dimensions") or {}).get(d, {}).get("verdict") == "NEUTRAL"
                                    for d in req.dims
                                ):
                                    # 所有维度均 NEUTRAL（通常是缺乏实时数据，不是真正风险信号）
                                    # Gate 已覆盖流动性/资金费率/盘口冲击/R:R 等实质性检查
                                    console.print(
                                        f"  {symbol:12s} [green]PHASE2 ALL-NEUTRAL PASS[/green] — "
                                        f"no live data for LLM, Gate checks as safety net"
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

                    # 2d. 资金费率已获取（见 2bb 节），直接进入下单量计算

                    # 2e. 计算预期下单量（Gate 需要此值做盘口冲击检查）
                    max_lev = args.max_leverage
                    if score >= 7:
                        leverage = max_lev
                    elif score >= 5:
                        leverage = max(1, max_lev // 2)
                    else:
                        leverage = 1

                    # price_tier 调整: 低价/微价币缩减仓位
                    price_tier = getattr(req, "price_tier", "standard")
                    tier_factor = {
                        "micro": 0.50,    # < $0.1 → 50% 标准仓位
                        "low": 0.75,      # < $1   → 75%
                        "standard": 1.0,
                        "premium": 1.0,
                    }.get(price_tier, 1.0)
                    if score == _score_half:
                        tier_factor *= 0.5
                        log.info("%s: score=%d → 仓位系数 50%%", symbol, score)
                    if tier_factor < 1.0:
                        log.info(
                            "%s: price_tier=%s, 仓位系数 %.0f%%",
                            symbol, price_tier, tier_factor * 100,
                        )

                    effective_position_size = min(
                        args.position_size * tier_factor,
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
                        whitelist=config.pair_whitelist or None,
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
                    log.info(
                        "Gate %s %s %s score=%d entry=%.2f SL=%.2f TP=%.2f | %s",
                        gate_result.status.value, symbol, direction.value, score,
                        entry_price, stop_price, tp_price, detail,
                    )

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
                            ok, reason = positions.can_open_new(
                                symbol, additional_notional=notional, direction=req.direction,
                            )
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
                                            console.print("           [yellow]⚠️ 订单未成交，已取消[/yellow]")
                                            continue
                                    except Exception:
                                        pass

                                fill_price = float(order.get("avg_price") or 0)
                                if fill_price <= 0 and order.get("filled") and order.get("cummulative_quote"):
                                    filled = float(order.get("filled") or 0)
                                    if filled > 0:
                                        fill_price = float(order["cummulative_quote"]) / filled
                                if fill_price > 0:
                                    entry_price = fill_price
                                    risk = abs(entry_price - stop_price)
                                    if direction.value == "LONG":
                                        tp_price = entry_price + risk * active_rr
                                    else:
                                        tp_price = entry_price - risk * active_rr

                                pos = positions.open_position(
                                    symbol=symbol,
                                    direction=direction.value,
                                    entry_price=entry_price,
                                    quantity=quantity,
                                    stop_loss=stop_price,
                                    take_profit=tp_price,
                                    leverage=leverage,
                                    entry_score=score,
                                )
                                bracket_note = ""
                                if (
                                    config.use_exchange_bracket_orders
                                    and not args.mock
                                ):
                                    from extensions.trading.crypto.live.exchange_brackets import (
                                        has_bracket_support,
                                        place_bracket_orders,
                                    )

                                    if has_bracket_support(exchange):
                                        sl_id, tp_id = place_bracket_orders(exchange, pos)
                                        positions.set_bracket_order_ids(symbol, sl_id, tp_id)
                                        if sl_id or tp_id:
                                            bracket_note = f" SL/TP 条件单: {sl_id or '-'}/{tp_id or '-'}"
                                        else:
                                            log.warning(
                                                "Exchange bracket orders failed for %s — TPSL software fallback",
                                                symbol,
                                            )
                                cycle_orders += 1
                                total_orders += 1
                                console.print(
                                    f"           [green]✅ 开仓成功: {symbol} "
                                    f"{direction.value} qty={quantity} @ {entry_price:.2f}{bracket_note}[/green]"
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
