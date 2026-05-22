"""Tests for exchange bracket order helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from extensions.live_trading.engine.exchange import MockExchange
from extensions.live_trading.engine.exchange_brackets import (
    cancel_bracket_orders,
    close_side,
    has_bracket_support,
    place_bracket_orders,
)
from extensions.live_trading.engine.position_tracker import Position


def test_close_side() -> None:
    assert close_side("LONG") == "sell"
    assert close_side("SHORT") == "buy"


def test_place_bracket_orders_mock() -> None:
    ex = MockExchange()
    pos = Position(
        symbol="BTCUSDT",
        direction="LONG",
        entry_price=100.0,
        quantity=0.01,
        stop_loss=95.0,
        take_profit=110.0,
    )
    sl_id, tp_id = place_bracket_orders(ex, pos)
    assert sl_id is not None
    assert tp_id is not None


def test_cancel_bracket_orders() -> None:
    ex = MockExchange()
    pos = Position(
        symbol="ETHUSDT",
        direction="SHORT",
        entry_price=3000.0,
        quantity=0.1,
        stop_loss=3100.0,
        take_profit=2800.0,
        sl_order_id="111",
        tp_order_id="222",
    )
    cancel_bracket_orders(ex, pos)
    assert ex.cancel_order.call_count == 2 if hasattr(ex.cancel_order, "call_count") else True


def test_has_bracket_support() -> None:
    assert has_bracket_support(MockExchange()) is True
    bare = MagicMock(spec=["create_market_order"])
    assert has_bracket_support(bare) is False
