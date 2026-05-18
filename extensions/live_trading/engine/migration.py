"""One-time migration from positions.json to trading.db.

Usage:
    # Auto-migration (called from PositionTracker.__init__):
    from extensions.live_trading.engine.migration import migrate_from_json
    migrate_from_json()  # defaults to ~/.vibe-trading

    # Standalone:
    python -c "from extensions.live_trading.engine.migration import migrate_from_json; migrate_from_json()"
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional, Union

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path.home() / ".vibe-trading"

_SCHEMA_SQL = """
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;

    CREATE TABLE IF NOT EXISTS positions (
        symbol TEXT PRIMARY KEY, direction TEXT NOT NULL,
        entry_price REAL NOT NULL, quantity REAL NOT NULL,
        stop_loss REAL NOT NULL, take_profit REAL,
        opened_at TEXT NOT NULL, dca_count INTEGER DEFAULT 0,
        leverage INTEGER DEFAULT 1, entry_score INTEGER DEFAULT -1,
        first_entry_cost REAL DEFAULT 0.0,
        first_entry_quantity REAL DEFAULT 0.0,
        de_risk_level INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS closed_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL, direction TEXT NOT NULL,
        entry_price REAL NOT NULL, exit_price REAL NOT NULL,
        quantity REAL NOT NULL, pnl_usdt REAL NOT NULL,
        pnl_pct REAL NOT NULL, reason TEXT NOT NULL,
        opened_at TEXT DEFAULT '', closed_at TEXT DEFAULT '',
        dca_count INTEGER DEFAULT 0, leverage INTEGER DEFAULT 1,
        entry_score INTEGER DEFAULT -1
    );
    CREATE INDEX IF NOT EXISTS idx_closed_at ON closed_trades(closed_at);
    CREATE INDEX IF NOT EXISTS idx_entry_score ON closed_trades(entry_score);

    CREATE TABLE IF NOT EXISTS equity_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL, balance REAL NOT NULL,
        equity REAL NOT NULL,
        active_positions INTEGER DEFAULT 0,
        total_realized_pnl REAL DEFAULT 0.0
    );
    CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_history(timestamp);

    CREATE TABLE IF NOT EXISTS cooldowns (
        key TEXT PRIMARY KEY, expires_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS extended_cooldowns (
        key TEXT PRIMARY KEY, expires_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS trailing_state (
        symbol TEXT PRIMARY KEY,
        trailing_stop REAL NOT NULL,
        peak_price REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    );
"""


def _has_data(conn: sqlite3.Connection) -> bool:
    """Check if the database already has data (idempotency guard)."""
    row = conn.execute("SELECT COUNT(*) as c FROM positions").fetchone()
    return row[0] > 0


def migrate_from_json(persist_dir: Optional[Union[str, Path]] = None) -> bool:
    """Import existing positions.json into trading.db.

    Args:
        persist_dir: Directory containing positions.json (default: ~/.vibe-trading).

    Returns:
        True if migration was performed, False if skipped.
    """
    base = Path(persist_dir) if persist_dir else _DEFAULT_DIR
    json_path = base / "positions.json"
    db_path = base / "trading.db"

    if not json_path.exists():
        logger.info("No positions.json found at %s — nothing to migrate", json_path)
        return False

    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            if _has_data(conn):
                logger.info("trading.db already has data — skipping migration")
                return False
        finally:
            conn.close()

    # Load JSON
    try:
        with open(json_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read positions.json: %s", exc)
        return False

    # Insert into SQLite
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA_SQL)

        # Positions
        for p in data.get("positions", []):
            conn.execute(
                """INSERT OR REPLACE INTO positions
                   (symbol, direction, entry_price, quantity, stop_loss,
                    take_profit, opened_at, dca_count, leverage, entry_score,
                    first_entry_cost, first_entry_quantity, de_risk_level)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p.get("symbol"), p.get("direction"), p.get("entry_price", 0),
                 p.get("quantity", 0), p.get("stop_loss", 0), p.get("take_profit"),
                 p.get("opened_at", ""), p.get("dca_count", 0),
                 p.get("leverage", 1), p.get("entry_score", -1),
                 p.get("first_entry_cost", 0), p.get("first_entry_quantity", 0),
                 p.get("de_risk_level", 0)),
            )

        # Closed trades
        for c in data.get("closed", []):
            conn.execute(
                """INSERT INTO closed_trades
                   (symbol, direction, entry_price, exit_price, quantity,
                    pnl_usdt, pnl_pct, reason, opened_at, closed_at,
                    dca_count, leverage, entry_score)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (c.get("symbol"), c.get("direction"), c.get("entry_price", 0),
                 c.get("exit_price", 0), c.get("quantity", 0),
                 c.get("pnl_usdt", 0), c.get("pnl_pct", 0), c.get("reason", ""),
                 c.get("opened_at", ""), c.get("closed_at", ""),
                 c.get("dca_count", 0), c.get("leverage", 1),
                 c.get("entry_score", -1)),
            )

        # Equity history
        for e in data.get("equity_history", []):
            conn.execute(
                """INSERT INTO equity_history
                   (timestamp, balance, equity, active_positions, total_realized_pnl)
                   VALUES (?,?,?,?,?)""",
                (e.get("timestamp"), e.get("balance", 0), e.get("equity", 0),
                 e.get("active_positions", 0), e.get("total_realized_pnl", 0)),
            )

        # Cooldowns
        for key, val in data.get("cooldowns", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO cooldowns (key, expires_at) VALUES (?,?)",
                (key, float(val)),
            )

        # Extended cooldowns
        for key, val in data.get("extended_cooldowns", {}).items():
            conn.execute(
                "INSERT OR REPLACE INTO extended_cooldowns (key, expires_at) VALUES (?,?)",
                (key, float(val)),
            )

        # Trailing state
        trailing_stops = data.get("trailing_stops", {})
        peak_prices = data.get("peak_prices", {})
        for sym in trailing_stops:
            conn.execute(
                """INSERT OR REPLACE INTO trailing_state
                   (symbol, trailing_stop, peak_price) VALUES (?,?,?)""",
                (sym, trailing_stops[sym], peak_prices.get(sym, 0.0)),
            )

        # Account balance
        if "account_balance" in data:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES ('account_balance', ?)",
                (str(data["account_balance"]),),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        logger.exception("Migration failed")
        return False
    finally:
        conn.close()

    # Rename old JSON as backup
    bak_path = json_path.with_suffix(".json.bak")
    json_path.rename(bak_path)

    positions_count = len(data.get("positions", []))
    closed_count = len(data.get("closed", []))
    equity_count = len(data.get("equity_history", []))
    logger.info(
        "Migrated %d positions, %d closed records, %d equity entries to %s (backup: %s)",
        positions_count, closed_count, equity_count, db_path, bak_path,
    )
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate_from_json()
