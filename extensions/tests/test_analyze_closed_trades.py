"""Tests for analyze_closed_trades.py."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from extensions.scripts.analyze_closed_trades import (
    build_report,
    format_text_report,
    load_trades,
)
from extensions.trading.crypto.schema import SCHEMA_SQL


def _seed_db(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_SQL)
    conn.executemany(
        """
        INSERT INTO closed_trades
        (symbol, direction, entry_price, exit_price, quantity,
         pnl_usdt, pnl_pct, reason, opened_at, closed_at,
         dca_count, leverage, entry_score)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.execute(
        """
        INSERT INTO equity_history (timestamp, balance, equity, active_positions, total_realized_pnl)
        VALUES ('2026-05-25T12:00:00+00:00', 500.0, 504.17, 0, 4.17)
        """
    )
    conn.commit()
    conn.close()


@pytest.fixture
def sample_db() -> Path:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "trading.db"
    _seed_db(
        db,
        [
            (
                "HYPEUSDT",
                "LONG",
                10.0,
                11.0,
                1.0,
                2.0,
                10.0,
                "TP",
                "2026-05-24T00:00:00+00:00",
                "2026-05-24T06:00:00+00:00",
                0,
                5,
                7,
            ),
            (
                "HYPEUSDT",
                "LONG",
                10.0,
                10.5,
                1.0,
                1.98,
                9.8,
                "TP",
                "2026-05-24T08:00:00+00:00",
                "2026-05-24T14:00:00+00:00",
                0,
                5,
                7,
            ),
            (
                "INJUSDT",
                "LONG",
                20.0,
                19.5,
                1.0,
                -0.5,
                -2.5,
                "SL",
                "2026-05-23T10:00:00+00:00",
                "2026-05-23T15:30:00+00:00",
                0,
                5,
                5,
            ),
            (
                "ETHUSDT",
                "SHORT",
                3000.0,
                2990.0,
                0.01,
                0.1,
                0.3,
                "STALE",
                "2026-05-22T00:00:00+00:00",
                "2026-05-23T00:00:00+00:00",
                0,
                5,
                6,
            ),
        ],
    )
    return db


class TestAnalyzeClosedTrades:
    def test_load_trades_count(self, sample_db: Path) -> None:
        trades = load_trades(sample_db)
        assert len(trades) == 4
        assert trades[0].symbol == "ETHUSDT"  # 最早平仓
        assert trades[-1].symbol == "HYPEUSDT"

    def test_build_report_overall(self, sample_db: Path) -> None:
        report = build_report(sample_db)
        assert report["overall"]["count"] == 4
        assert report["overall"]["total_pnl_usdt"] == pytest.approx(3.58, abs=0.01)
        assert report["concentration"]["top_symbol"] == "HYPEUSDT"
        assert report["latest_balance_usdt"] == pytest.approx(500.0)

    def test_daily_breakdown(self, sample_db: Path) -> None:
        report = build_report(sample_db)
        assert len(report["daily"]) == 2
        assert report["daily"][-1]["cumulative_pnl_usdt"] == pytest.approx(3.58, abs=0.01)

    def test_text_report_contains_sections(self, sample_db: Path) -> None:
        report = build_report(sample_db)
        text = format_text_report(report)
        assert "按品种" in text
        assert "HYPEUSDT" in text
        assert "可行性提示" in text

    def test_json_serializable(self, sample_db: Path) -> None:
        report = build_report(sample_db)
        payload = json.dumps(report, ensure_ascii=False)
        parsed = json.loads(payload)
        assert parsed["trade_count"] == 4
