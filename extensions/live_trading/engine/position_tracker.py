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

    def __post_init__(self) -> None:
        if not self.opened_at:
            self.opened_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "opened_at": self.opened_at,
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
        )


class PositionTracker:
    """Manages active positions with thread-safe state and JSON persistence.

    Usage:
        tracker = PositionTracker(account_balance=10000.0, max_exposure_pct=0.25)
        if tracker.can_open_new("BTCUSDT"):
            tracker.open_position(signal, quantity=0.01, stop_loss=63000, take_profit=67000)
        exposure = tracker.get_exposure()
        tracker.close_position("BTCUSDT")
    """

    def __init__(
        self,
        account_balance: float = 10_000.0,
        max_exposure_pct: float = 0.25,
        max_positions: int = 3,
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
        self._cooldowns: dict[str, float] = {}  # "SYMBOL:DIRECTION" → timestamp
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
            )
            self._positions[symbol] = pos
            # Record cooldown to prevent immediate re-entry
            key = f"{symbol}:{direction}"
            self._cooldowns[key] = time.time()
            self._persist()
            logger.info("Position opened: %s %s @ %.4f qty=%.4f", symbol, direction, entry_price, quantity)
            return pos

    def close_position(self, symbol: str) -> Optional[Position]:
        """Close and remove a position. Returns the closed position or None."""
        with self._lock:
            pos = self._positions.pop(symbol, None)
            if pos:
                self._persist()
                logger.info("Position closed: %s %s @ %.4f", symbol, pos.direction, pos.entry_price)
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

    def get_exposure(self) -> float:
        """Total exposure as a fraction of account balance (0.0–1.0)."""
        with self._lock:
            return self._get_exposure_unlocked()

    def _get_exposure_unlocked(self) -> float:
        total_value = sum(p.entry_price * p.quantity for p in self._positions.values())
        if self._account_balance <= 0:
            return 1.0
        return total_value / self._account_balance

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def can_open_new(self, symbol: str) -> bool:
        """Check if a new position can be opened.

        Rejects if:
        - Symbol already has an active position
        - Maximum number of positions reached
        - Exposure limit exceeded
        """
        with self._lock:
            if symbol in self._positions:
                return False
            if len(self._positions) >= self._max_positions:
                return False
            if self._get_exposure_unlocked() >= self._max_exposure_pct:
                return False
            return True

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

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write positions and cooldowns to JSON file.

        If the filesystem is not writable (e.g. Docker volume permissions), state
        is kept in-memory and a warning is logged — the system continues running.
        """
        # _lock must already be held
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "positions": [p.to_dict() for p in self._positions.values()],
            "cooldowns": {k: v for k, v in self._cooldowns.items()},
            "account_balance": self._account_balance,
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
            self._cooldowns = {k: float(v) for k, v in raw.get("cooldowns", {}).items()}
            if "account_balance" in raw:
                self._account_balance = float(raw["account_balance"])
            logger.info(
                "Loaded %d positions from %s", len(self._positions), self._persist_path,
            )
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Failed to load positions from %s: %s", self._persist_path, exc)

    def clear(self) -> None:
        """Remove all positions and clear persistence (useful for testing)."""
        with self._lock:
            self._positions.clear()
            self._cooldowns.clear()
            if self._persist_path.exists():
                self._persist_path.unlink()
