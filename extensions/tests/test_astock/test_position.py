"""Tests for A-share position tracker and T+1 broker rules."""

from __future__ import annotations

import tempfile
from pathlib import Path

from extensions.trading.astock.live.broker import MockBroker
from extensions.trading.astock.models import AStockPosition
from extensions.trading.astock.live.position import AStockPositionTracker


def test_t_plus_one_sell_blocked() -> None:
    broker = MockBroker(initial_cash=1_000_000)
    buy = broker.buy("600519.SH", 100.0, 100)
    assert buy["status"] == "filled"
    sell = broker.sell("600519.SH", 101.0, 100)
    assert sell["status"] == "rejected"
    assert sell["reason"] == "t_plus_1"
    broker.clear_today_buy_flags()
    sell2 = broker.sell("600519.SH", 101.0, 100)
    assert sell2["status"] == "filled"


def test_position_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        tracker = AStockPositionTracker(db_path=db)
        tracker.open_position(
            AStockPosition(
                symbol="600519.SH",
                name="茅台",
                shares=100,
                entry_price=1800.0,
                stop_loss=1700.0,
                is_today_buy=True,
            )
        )
        tracker2 = AStockPositionTracker(db_path=db)
        assert tracker2.active_count == 1
        assert tracker2.get_position("600519.SH").shares == 100
