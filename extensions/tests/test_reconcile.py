"""Tests for exchange ↔ tracker position reconciliation."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from extensions.live_trading.engine.position_tracker import PositionTracker
from extensions.live_trading.engine.reconcile import reconcile_positions


@pytest.fixture
def tracker() -> PositionTracker:
    tmp = tempfile.mkdtemp()
    t = PositionTracker(account_balance=10_000.0, max_positions=3, persist_dir=tmp)
    yield t
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class TestReconcilePositions:
    def test_removes_ghost_local_position(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.01, 63000.0)
        summary = reconcile_positions(tracker, [])
        assert "BTCUSDT" in summary["removed"]
        assert tracker.active_count == 0
        assert len(tracker.get_recent_closed(5)) == 1
        assert tracker.get_recent_closed(1)[0].reason == "RECONCILE_GONE"

    def test_adopts_exchange_only_position(self, tracker: PositionTracker) -> None:
        exch = [{
            "symbol": "ETHUSDT",
            "direction": "SHORT",
            "entry_price": 3200.0,
            "quantity": 0.05,
        }]
        summary = reconcile_positions(tracker, exch)
        assert "ETHUSDT" in summary["adopted"]
        assert tracker.active_count == 1
        pos = tracker.get_active_positions()[0]
        assert pos.symbol == "ETHUSDT"
        assert pos.direction == "SHORT"

    def test_persists_closed_beyond_fifty(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            t = PositionTracker(account_balance=10_000.0, persist_dir=tmp)
            for i in range(55):
                sym = f"SYM{i}USDT"
                t.open_position(sym, "LONG", 100.0 + i, 1.0, 90.0 + i)
                t.close_position(sym, exit_price=101.0 + i, reason="TP")
            conn = sqlite3.connect(str(tmp / "trading.db"))
            count = conn.execute("SELECT COUNT(*) FROM closed_trades").fetchone()[0]
            conn.close()
            assert count == 55
            assert len(t.get_recent_closed(100)) == 55
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
