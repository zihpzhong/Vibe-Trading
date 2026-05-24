"""Shared SQLite schema for live trading persistence.

Centralized schema used by both migration.py and position_tracker.py
to avoid duplicate definitions. Update here when adding columns or tables.
"""

SCHEMA_SQL = """
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