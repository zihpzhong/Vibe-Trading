"""A-share position tracker — shares, T+1, SQLite persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Optional

from extensions.live_trading.astock.config import AStockFeeConfig
from extensions.live_trading.astock.models import AStockCloseRecord, AStockPosition

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".vibe-trading" / "astock_trading.db"


class AStockPositionTracker:
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DEFAULT_DB
        self._lock = RLock()
        self._positions: dict[str, AStockPosition] = {}
        self._init_db()
        self._load()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS astock_positions (
                    symbol TEXT PRIMARY KEY,
                    data TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS astock_closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    data TEXT NOT NULL,
                    closed_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _load(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("SELECT symbol, data FROM astock_positions").fetchall()
        for _sym, raw in rows:
            try:
                pos = AStockPosition.from_dict(json.loads(raw))
                self._positions[pos.symbol] = pos
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("skip bad position row: %s", exc)

    def _persist(self) -> None:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM astock_positions")
            for pos in self._positions.values():
                conn.execute(
                    "INSERT INTO astock_positions (symbol, data) VALUES (?, ?)",
                    (pos.symbol, json.dumps(pos.to_dict())),
                )
            conn.commit()

    @property
    def active_count(self) -> int:
        return len(self._positions)

    def get_active_positions(self) -> list[AStockPosition]:
        with self._lock:
            return list(self._positions.values())

    def get_position(self, symbol: str) -> Optional[AStockPosition]:
        with self._lock:
            return self._positions.get(symbol)

    def open_position(self, position: AStockPosition) -> None:
        with self._lock:
            self._positions[position.symbol] = position
            self._persist()

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        shares: int,
        reason: str,
        fees: Optional[AStockFeeConfig] = None,
    ) -> Optional[AStockCloseRecord]:
        fee_cfg = fees or AStockFeeConfig()
        with self._lock:
            pos = self._positions.get(symbol)
            if not pos or pos.shares < shares:
                return None
            notional = exit_price * shares
            commission = max(notional * fee_cfg.commission_rate, fee_cfg.min_commission_cny)
            stamp = notional * fee_cfg.stamp_tax_rate
            pnl = (exit_price - pos.entry_price) * shares - commission - stamp
            pnl_pct = (exit_price / pos.entry_price - 1) * 100 if pos.entry_price else 0
            record = AStockCloseRecord(
                symbol=pos.symbol,
                name=pos.name,
                shares=shares,
                entry_price=pos.entry_price,
                exit_price=exit_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                commission=commission,
                stamp_tax=stamp,
                reason=reason,
                closed_at=datetime.now(timezone.utc).isoformat(),
            )
            pos.shares -= shares
            if pos.shares <= 0:
                del self._positions[symbol]
            self._persist()
            self._save_close(record)
            return record

    def _save_close(self, record: AStockCloseRecord) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO astock_closed_trades (data, closed_at) VALUES (?, ?)",
                (json.dumps(record.__dict__), record.closed_at),
            )
            conn.commit()

    def rollover_t_plus_one(self) -> None:
        """Clear today-buy flags at start of new trading day."""
        with self._lock:
            for pos in self._positions.values():
                pos.is_today_buy = False
            self._persist()
