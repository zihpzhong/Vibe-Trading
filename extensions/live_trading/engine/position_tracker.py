"""Position Tracker —持仓生命周期管理与风控.

Manages position state, exposure calculation, cooldown tracking,
and JSON persistence. All mutable state is protected by threading.RLock.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PERSIST_DIR = Path.home() / ".vibe-trading"


@dataclass
class Position:
    """A single open position."""

    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: Optional[float] = None
    opened_at: str = ""  # ISO 8601
    dca_count: int = 0  # 已 DCA 加仓次数
    leverage: int = 1
    entry_score: int = 0  # 开仓时评分, 用于分档胜率统计
    first_entry_cost: float = 0.0  # 首次入场价（永不改变，de-risk 参照系）
    first_entry_quantity: float = 0.0  # 首次数量（永不改变）
    de_risk_level: int = 0  # 已触发的最高 de-risk 级别 (0-4)

    def __post_init__(self) -> None:
        if not self.opened_at:
            self.opened_at = datetime.now(timezone.utc).isoformat()
        # 旧数据兼容：确保 first_entry_cost/quantity 有值
        if self.first_entry_cost == 0.0:
            self.first_entry_cost = self.entry_price
        if self.first_entry_quantity == 0.0:
            self.first_entry_quantity = self.quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "opened_at": self.opened_at,
            "dca_count": self.dca_count,
            "leverage": self.leverage,
            "entry_score": self.entry_score,
            "first_entry_cost": self.first_entry_cost,
            "first_entry_quantity": self.first_entry_quantity,
            "de_risk_level": self.de_risk_level,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Position:
        return cls(
            symbol=d["symbol"],
            direction=d["direction"],
            entry_price=float(d["entry_price"]),
            quantity=float(d["quantity"]),
            stop_loss=float(d.get("stop_loss", 0)),
            take_profit=float(d["take_profit"]) if d.get("take_profit") else None,
            opened_at=d.get("opened_at", ""),
            dca_count=int(d.get("dca_count", 0)),
            leverage=int(d.get("leverage", 1)),
            entry_score=int(d.get("entry_score", 0)),
            first_entry_cost=float(d.get("first_entry_cost", 0.0)),
            first_entry_quantity=float(d.get("first_entry_quantity", 0.0)),
            de_risk_level=int(d.get("de_risk_level", 0)),
        )


@dataclass
class CloseRecord:
    """A closed position record."""

    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    pnl_pct: float
    reason: str  # "TP" | "SL" | "MANUAL"
    opened_at: str = ""
    closed_at: str = ""
    dca_count: int = 0
    leverage: int = 1
    entry_score: int = 0

    def __post_init__(self) -> None:
        if not self.closed_at:
            self.closed_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "quantity": self.quantity,
            "pnl_usdt": self.pnl_usdt,
            "pnl_pct": self.pnl_pct,
            "reason": self.reason,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "dca_count": self.dca_count,
            "leverage": self.leverage,
            "entry_score": self.entry_score,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CloseRecord:
        return cls(
            symbol=d["symbol"],
            direction=d["direction"],
            entry_price=float(d["entry_price"]),
            exit_price=float(d["exit_price"]),
            quantity=float(d["quantity"]),
            pnl_usdt=float(d["pnl_usdt"]),
            pnl_pct=float(d["pnl_pct"]),
            reason=d["reason"],
            opened_at=d.get("opened_at", ""),
            closed_at=d.get("closed_at", ""),
            dca_count=int(d.get("dca_count", 0)),
            leverage=int(d.get("leverage", 1)),
            entry_score=int(d.get("entry_score", 0)),
        )

    def to_trade_record(self):
        """Convert to backtest TradeRecord for performance metrics."""
        from agent.backtest.models import TradeRecord
        import pandas as pd

        direction_int = 1 if self.direction == "LONG" else -1
        entry_time = pd.Timestamp(self.opened_at) if self.opened_at else pd.Timestamp.now()
        exit_time = pd.Timestamp(self.closed_at) if self.closed_at else pd.Timestamp.now()
        # Estimate holding bars (1h bar granularity)
        delta = exit_time - entry_time
        holding_bars = max(1, int(delta.total_seconds() / 3600))

        return TradeRecord(
            symbol=self.symbol,
            direction=direction_int,
            entry_price=self.entry_price,
            exit_price=self.exit_price,
            entry_time=entry_time,
            exit_time=exit_time,
            size=self.quantity,
            leverage=float(self.leverage),
            pnl=self.pnl_usdt,
            pnl_pct=self.pnl_pct,
            exit_reason=self.reason,
            holding_bars=holding_bars,
            commission=0.0,
        )


class PositionTracker:
    """Manages active positions with thread-safe state and JSON persistence.

    Usage:
        tracker = PositionTracker(account_balance=10000.0, max_exposure_pct=0.25)
        if tracker.can_open_new("BTCUSDT")[0]:
            tracker.open_position(signal, quantity=0.01, stop_loss=63000, take_profit=67000)
        exposure = tracker.get_exposure()
        tracker.close_position("BTCUSDT")
    """

    def __init__(
        self,
        account_balance: float = 10_000.0,
        max_exposure_pct: float = 0.25,
        max_positions: int = 5,
        cooldown_minutes: int = 30,
        persist_dir: Optional[str | Path] = None,
    ) -> None:
        self._account_balance = account_balance
        self._max_exposure_pct = max_exposure_pct
        self._max_positions = max_positions
        self._cooldown_minutes = cooldown_minutes
        self._persist_path = (Path(persist_dir) if persist_dir else _DEFAULT_PERSIST_DIR) / "positions.json"
        self._lock = RLock()
        self._positions: dict[str, Position] = {}
        self._closed: list[CloseRecord] = []  # 已平仓历史（最多保留最近 50 条）
        self._cooldowns: dict[str, float] = {}  # "SYMBOL:DIRECTION" → timestamp
        self._trailing_stops: dict[str, float] = {}
        self._peak_prices: dict[str, float] = {}
        self._equity_history: list[dict[str, Any]] = []  # 权益曲线快照
        self._initial_balance: float = account_balance
        self._load()

    # ------------------------------------------------------------------
    # Position lifecycle
    # ------------------------------------------------------------------

    def open_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: float,
        stop_loss: float,
        take_profit: Optional[float] = None,
        leverage: int = 1,
        entry_score: int = 0,
    ) -> Position:
        """Record a new position. Overwrites existing position for the same symbol."""
        with self._lock:
            pos = Position(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=stop_loss,
                take_profit=take_profit,
                leverage=leverage,
                entry_score=entry_score,
            )
            self._positions[symbol] = pos
            # Record cooldown to prevent immediate re-entry
            key = f"{symbol}:{direction}"
            self._cooldowns[key] = time.time()
            self._record_equity_snapshot_unlocked()
            self._persist()
            logger.info("Position opened: %s %s @ %.4f qty=%.4f lev=%dx", symbol, direction, entry_price, quantity, leverage)
            return pos

    def close_position(self, symbol: str, exit_price: Optional[float] = None, reason: str = "MANUAL") -> Optional[Position]:
        """Close and remove a position. Records it in closed history with PnL.

        Args:
            symbol: Position symbol to close.
            exit_price: Price at which the position was closed (for PnL calculation).
            reason: Close reason — "TP", "SL", or "MANUAL".

        Returns:
            The closed Position or None.
        """
        with self._lock:
            pos = self._positions.pop(symbol, None)
            if pos:
                pnl_usdt, pnl_pct = self._calculate_pnl_unlocked(pos, exit_price, pos.quantity)
                self._append_close_record_unlocked(pos, exit_price or pos.entry_price, pos.quantity, pnl_usdt, pnl_pct, reason)

                self._persist()
                self._record_equity_snapshot_unlocked()
                logger.info(
                    "Position closed: %s %s %s PnL=%.2fUSDT (%.2f%%)",
                    symbol, pos.direction, reason, pnl_usdt, pnl_pct,
                )
            return pos

    def _calculate_pnl_unlocked(
        self,
        pos: Position,
        exit_price: Optional[float],
        quantity: float,
    ) -> tuple[float, float]:
        """Calculate realized PnL for a full or partial close. Caller holds _lock."""
        if exit_price is None or exit_price <= 0 or pos.entry_price <= 0 or quantity <= 0:
            return 0.0, 0.0
        if pos.direction == "LONG":
            pnl_usdt = (exit_price - pos.entry_price) * quantity
            pnl_pct = (exit_price - pos.entry_price) / pos.entry_price * 100
        else:
            pnl_usdt = (pos.entry_price - exit_price) * quantity
            pnl_pct = (pos.entry_price - exit_price) / pos.entry_price * 100
        return pnl_usdt, pnl_pct

    def _append_close_record_unlocked(
        self,
        pos: Position,
        exit_price: float,
        quantity: float,
        pnl_usdt: float,
        pnl_pct: float,
        reason: str,
    ) -> None:
        """Append a close record for realized PnL accounting. Caller holds _lock."""
        record = CloseRecord(
            symbol=pos.symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=quantity,
            pnl_usdt=round(pnl_usdt, 4),
            pnl_pct=round(pnl_pct, 2),
            reason=reason,
            opened_at=pos.opened_at,
            dca_count=pos.dca_count,
            leverage=pos.leverage,
            entry_score=pos.entry_score,
        )
        self._closed.append(record)
        if len(self._closed) > 50:
            self._closed = self._closed[-50:]

    def get_recent_closed(self, n: int = 10) -> list[CloseRecord]:
        """Return the most recent N closed position records."""
        with self._lock:
            return list(self._closed[-n:])

    def adjust_position(self, symbol: str, add_quantity: float, new_price: float) -> Optional[Position]:
        """DCA: add quantity to existing position, recalc average entry price.

        Returns updated position or None if symbol not found.
        ``first_entry_cost`` and ``first_entry_quantity`` are intentionally
        preserved — they serve as the invariant reference for de-risk thresholds.
        """
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return None
            total_qty = pos.quantity + add_quantity
            pos.entry_price = (pos.entry_price * pos.quantity + new_price * add_quantity) / total_qty
            pos.quantity = total_qty
            pos.dca_count += 1
            # 显式确保 de-risk 参照系不变
            # (更安全的防御，避免未来某段代码意外修改)
            pos.first_entry_cost = pos.first_entry_cost
            pos.first_entry_quantity = pos.first_entry_quantity
            self._persist()
            logger.info(
                "Position adjusted: %s +%.4f @ %.4f, new avg=%.4f qty=%.4f (dca#%d)",
                symbol, add_quantity, new_price, pos.entry_price, total_qty, pos.dca_count,
            )
            return pos

    def reduce_position(self, symbol: str, reduce_qty: float, current_price: float) -> Optional[Position]:
        """Partially close a position (e.g. partial fill).

        If reduce_qty >= total quantity, fully closes the position.
        Otherwise reduces quantity in-place.
        """
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return None
            if reduce_qty >= pos.quantity:
                return self.close_position(symbol, exit_price=current_price, reason="PARTIAL")
            pos.quantity -= reduce_qty
            self._persist()
            logger.info(
                "Position reduced: %s -%.4f @ %.4f, remaining qty=%.4f",
                symbol, reduce_qty, current_price, pos.quantity,
            )
            return pos

    def de_risk_partial_exit(self, symbol: str, de_risk_level: int, sell_qty: float, current_price: float) -> Optional[Position]:
        """De-risk partial exit: sell a fraction of an active position.

        Unlike ``reduce_position``, this method:
        - Updates ``de_risk_level`` on the position (level only moves upward).
        - Creates a CloseRecord for the realized partial PnL.
        - Is idempotent for a given level (will not re-fire the same level).

        Args:
            symbol: Position symbol.
            de_risk_level: Which de-risk level triggered (1-3).
            sell_qty: Quantity to sell.
            current_price: Current market price (for logging).

        Returns:
            Updated Position, or None if symbol not found.
        """
        if de_risk_level < 1 or de_risk_level > 4:
            logger.warning("de_risk_partial_exit: invalid level %d for %s", de_risk_level, symbol)
            return None
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos:
                return None
            # Level 4 = doom, handled by full close
            if de_risk_level == 4:
                self.close_position(symbol, exit_price=current_price, reason="DOOM")
                return None
            # 防重复：level 只升不降
            if de_risk_level <= pos.de_risk_level:
                logger.debug(
                    "de-risk level %d already fired for %s (current=%d), skipping",
                    de_risk_level, symbol, pos.de_risk_level,
                )
                return pos
            clamped_qty = min(sell_qty, pos.quantity)
            if clamped_qty <= 0:
                return pos
            # 如果卖光 = 全平
            if clamped_qty >= pos.quantity:
                logger.info(
                    "DE-RISK level %d: selling all remaining %.4f %s @ %.4f (DOOM)",
                    de_risk_level, pos.quantity, symbol, current_price,
                )
                return self.close_position(symbol, exit_price=current_price, reason=f"DE_RISK_{de_risk_level}")
            pnl_usdt, pnl_pct = self._calculate_pnl_unlocked(pos, current_price, clamped_qty)
            self._append_close_record_unlocked(
                pos, current_price, clamped_qty, pnl_usdt, pnl_pct, f"DE_RISK_{de_risk_level}",
            )
            pos.quantity -= clamped_qty
            pos.de_risk_level = de_risk_level
            self._persist()
            logger.info(
                "DE-RISK level %d: sold %.4f %s @ %.4f, remaining qty=%.4f",
                de_risk_level, clamped_qty, symbol, current_price, pos.quantity,
            )
            return pos

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_active_positions(self) -> list[Position]:
        """Return a copy of all active positions."""
        with self._lock:
            return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[Position]:
        """Get a specific position by symbol."""
        with self._lock:
            return self._positions.get(symbol)

    def get_exposure(
        self,
        mark_prices: Optional[dict[str, float]] = None,
        include_unrealized: bool = False,
    ) -> float:
        """Total mark/notional exposure as a fraction of balance or equity.

        Args:
            mark_prices: Optional symbol → current mark price map. Missing
                symbols fall back to entry price for backward compatibility.
            include_unrealized: If True, denominator is balance plus floating
                PnL computed from ``mark_prices``.
        """
        with self._lock:
            return self._get_exposure_unlocked(mark_prices, include_unrealized)

    def _get_exposure_unlocked(
        self,
        mark_prices: Optional[dict[str, float]] = None,
        include_unrealized: bool = False,
    ) -> float:
        total_value = 0.0
        floating_pnl = 0.0
        prices = mark_prices or {}
        for pos in self._positions.values():
            mark = float(prices.get(pos.symbol, pos.entry_price) or pos.entry_price)
            total_value += mark * pos.quantity
            if include_unrealized:
                if pos.direction == "LONG":
                    floating_pnl += (mark - pos.entry_price) * pos.quantity
                else:
                    floating_pnl += (pos.entry_price - mark) * pos.quantity
        denominator = self._account_balance + floating_pnl if include_unrealized else self._account_balance
        if denominator <= 0:
            return 1.0
        return total_value / denominator

    # ------------------------------------------------------------------
    # Equity curve tracking
    # ------------------------------------------------------------------

    def _record_equity_snapshot_unlocked(self) -> None:
        """Record an equity snapshot. Caller must hold _lock."""
        # Total equity = account balance + cumulative realized PnL
        total_realized_pnl = sum(c.pnl_usdt for c in self._closed)
        equity = self._account_balance + total_realized_pnl
        self._equity_history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "balance": self._account_balance,
            "equity": round(equity, 4),
            "active_positions": len(self._positions),
            "total_realized_pnl": round(total_realized_pnl, 4),
        })

    def record_equity_snapshot(self) -> None:
        """Thread-safe equity snapshot recording."""
        with self._lock:
            self._record_equity_snapshot_unlocked()

    def get_equity_series(self):
        """Return equity curve as pd.Series (index=timestamp, values=equity).

        Returns None if no data.
        """
        with self._lock:
            if not self._equity_history:
                return None
            import pandas as pd
            timestamps = [pd.Timestamp(e["timestamp"]) for e in self._equity_history]
            values = [e["equity"] for e in self._equity_history]
            return pd.Series(values, index=timestamps)

    def get_performance_metrics(self):
        """Compute full performance metrics from equity curve and closed trades.

        Returns dict from calc_metrics, or empty metrics if < 3 trades.
        """
        closed = self.get_recent_closed(50)
        if len(closed) < 3:
            return {
                "trade_count": len(closed),
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "sharpe": 0.0,
                "max_drawdown": 0.0,
                "total_return": 0.0,
                "message": "数据太少，至少需要 3 笔交易",
            }

        trades = [c.to_trade_record() for c in closed]
        equity = self.get_equity_series()
        if equity is None or len(equity) < 5:
            return {
                "trade_count": len(trades),
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "sharpe": 0.0,
                "max_drawdown": 0.0,
                "total_return": 0.0,
                "message": "权益曲线数据不足",
            }

        try:
            from agent.backtest.metrics import calc_metrics
            return calc_metrics(
                equity_curve=equity,
                trades=trades,
                initial_cash=self._initial_balance,
                bars_per_year=365 * 24,  # 1h bars for crypto
            )
        except Exception as exc:
            logger.warning("Performance metrics failed: %s", exc)
            return {"trade_count": len(trades), "error": str(exc)}

    def get_win_rate_by_score_tier(self) -> dict[str, dict[str, float]]:
        """Win rate grouped by entry score tier.

        Returns:
            {"high": {"count": N, "wins": N, "win_rate": 0.0, "total_pnl": 0.0},
             "medium": {...},
             "low": {...}}
        """
        tiers = {
            "high (7-10)": {"count": 0, "wins": 0, "total_pnl": 0.0},
            "medium (5-6)": {"count": 0, "wins": 0, "total_pnl": 0.0},
            "low (0-4)": {"count": 0, "wins": 0, "total_pnl": 0.0},
            "N/A (旧数据)": {"count": 0, "wins": 0, "total_pnl": 0.0},
        }
        with self._lock:
            for c in self._closed:
                if c.entry_score >= 7:
                    tier = "high (7-10)"
                elif c.entry_score >= 5:
                    tier = "medium (5-6)"
                elif c.entry_score > 0:
                    tier = "low (0-4)"
                else:
                    tier = "N/A (旧数据)"
                tiers[tier]["count"] += 1
                if c.pnl_usdt > 0:
                    tiers[tier]["wins"] += 1
                tiers[tier]["total_pnl"] += c.pnl_usdt

        result = {}
        for tier_name, data in tiers.items():
            result[tier_name] = {
                "count": data["count"],
                "wins": data["wins"],
                "win_rate": round(data["wins"] / data["count"], 2) if data["count"] > 0 else 0.0,
                "total_pnl": round(data["total_pnl"], 4),
            }
        return result

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def can_open_new(self, symbol: str, additional_notional: float = 0.0) -> tuple[bool, str]:
        """Check if a new position can be opened.

        Args:
            symbol: Trading symbol to check.
            additional_notional: Expected notional of the new position (0 = skip).

        Returns:
            (True, "") if allowed, or (False, "reason string") if rejected.

        Rejects if:
        - Symbol already has an active position
        - Maximum number of positions reached
        - Total exposure (current + new) exceeds limit
        """
        with self._lock:
            if symbol in self._positions:
                return False, f"已有 {symbol} 持仓"
            if len(self._positions) >= self._max_positions:
                return False, f"仓位已达上限 ({self._max_positions})"
            current_exp = self._get_exposure_unlocked()
            post_exp = current_exp + (additional_notional / self._account_balance) if additional_notional > 0 else current_exp
            if post_exp >= self._max_exposure_pct - 1e-9:
                if additional_notional > 0:
                    return False, (
                        f"开仓后总敞口 {post_exp:.1%} 超限 (上限 {self._max_exposure_pct:.0%}, "
                        f"当前 {current_exp:.1%}, 新增 ${additional_notional:.2f})"
                    )
                else:
                    return False, (
                        f"总敞口 {current_exp:.1%} 超限 (上限 {self._max_exposure_pct:.0%})"
                    )
            return True, ""

    def is_in_cooldown(self, symbol: str, direction: str) -> bool:
        """Check if symbol+direction pair is in cooldown period."""
        with self._lock:
            key = f"{symbol}:{direction}"
            if key not in self._cooldowns:
                return False
            elapsed = time.time() - self._cooldowns[key]
            return elapsed < self._cooldown_minutes * 60

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._positions)

    @property
    def account_balance(self) -> float:
        return self._account_balance

    @account_balance.setter
    def account_balance(self, value: float) -> None:
        with self._lock:
            self._account_balance = value

    @property
    def max_exposure_pct(self) -> float:
        """Maximum total exposure fraction (e.g. 0.25 = 25%)."""
        return self._max_exposure_pct

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------


    def get_trailing_state(self) -> dict[str, dict[str, float]]:
        """Return current trailing stop state for external persistence.

        Returns:
            {"trailing_stops": {...}, "peak_prices": {...}}
        """
        with self._lock:
            return {
                "trailing_stops": dict(self._trailing_stops),
                "peak_prices": dict(self._peak_prices),
            }

    def set_trailing_state(self, trailing_stops: dict[str, float], peak_prices: dict[str, float]) -> None:
        """Restore trailing stop state from persisted data."""
        with self._lock:
            self._trailing_stops = trailing_stops or {}
            self._peak_prices = peak_prices or {}
            self._persist()
    def _persist(self) -> None:
        """Write positions and cooldowns to JSON file.

        If the filesystem is not writable (e.g. Docker volume permissions), state
        is kept in-memory and a warning is logged — the system continues running.
        """
        # _lock must already be held
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "positions": [p.to_dict() for p in self._positions.values()],
            "closed": [c.to_dict() for c in self._closed],
            "cooldowns": {k: v for k, v in self._cooldowns.items()},
            "trailing_stops": getattr(self, "_trailing_stops", {}),
            "peak_prices": getattr(self, "_peak_prices", {}),
            "account_balance": self._account_balance,
            "initial_balance": self._initial_balance,
            "equity_history": getattr(self, "_equity_history", []),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            tmp.replace(self._persist_path)
        except (PermissionError, OSError) as exc:
            logger.warning("Position persistence unavailable (in-memory only): %s", exc)

    def _load(self) -> None:
        """Restore positions and cooldowns from JSON file."""
        if not self._persist_path.exists():
            return
        try:
            raw = json.loads(self._persist_path.read_text())
            for p in raw.get("positions", []):
                pos = Position.from_dict(p)
                self._positions[pos.symbol] = pos
            for c in raw.get("closed", []):
                try:
                    self._closed.append(CloseRecord.from_dict(c))
                except (KeyError, ValueError) as exc:
                    logger.warning("Skipping invalid close record: %s", exc)
            self._cooldowns = {k: float(v) for k, v in raw.get("cooldowns", {}).items()}
            self._trailing_stops = raw.get("trailing_stops", {})
            self._peak_prices = raw.get("peak_prices", {})
            if "account_balance" in raw:
                self._account_balance = float(raw["account_balance"])
            if "initial_balance" in raw:
                self._initial_balance = float(raw["initial_balance"])
            if "equity_history" in raw:
                self._equity_history = list(raw["equity_history"])
            logger.info(
                "Loaded %d positions, %d closed records, %d equity snapshots from %s",
                len(self._positions), len(self._closed), len(self._equity_history), self._persist_path,
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Failed to load positions from %s: %s", self._persist_path, exc)

    def clear(self) -> None:
        """Remove all positions and clear persistence (useful for testing)."""
        with self._lock:
            self._positions.clear()
            self._closed.clear()
            self._cooldowns.clear()
            self._equity_history.clear()
            if self._persist_path.exists():
                self._persist_path.unlink()
