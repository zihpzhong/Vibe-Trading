"""Tests for exchange bracket order helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

from extensions.trading.crypto.live.exchange import MockExchange
from extensions.trading.crypto.live.exchange_brackets import (
    cancel_bracket_orders,
    close_side,
    has_bracket_support,
    is_algo_order_active,
    place_bracket_orders,
    sanitize_bracket_order_ids,
)
from extensions.trading.crypto.live.position_tracker import Position


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
    ex._open_algo_orders = [
        {"algoId": "111", "symbol": "ETHUSDT", "orderType": "STOP_MARKET", "algoStatus": "NEW"},
        {"algoId": "222", "symbol": "ETHUSDT", "orderType": "TAKE_PROFIT_MARKET", "algoStatus": "NEW"},
    ]
    cancel_bracket_orders(ex, pos)
    assert ex.fetch_open_algo_orders("ETHUSDT") == []


def test_has_bracket_support() -> None:
    assert has_bracket_support(MockExchange()) is True
    bare = MagicMock(spec=["create_market_order"])
    assert has_bracket_support(bare) is False


def test_sanitize_bracket_order_ids_clears_canceled() -> None:
    ex = MagicMock()
    ex.fetch_algo_order = MagicMock(side_effect=[
        {"status": "CANCELED"},
        {"status": "NEW"},
    ])
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
    sl_id, tp_id = sanitize_bracket_order_ids(ex, pos)
    assert sl_id is None
    assert tp_id == "222"


def test_place_bracket_orders_rollback_sl_on_tp_failure() -> None:
    ex = MagicMock()
    ex.create_stop_loss_order.return_value = {"order_id": "sl-new"}
    ex.create_take_profit_order.side_effect = RuntimeError("tp failed")
    pos = Position(
        symbol="BTCUSDT",
        direction="LONG",
        entry_price=100.0,
        quantity=0.01,
        stop_loss=95.0,
        take_profit=110.0,
    )
    sl_id, tp_id = place_bracket_orders(ex, pos)
    assert sl_id is None
    assert tp_id is None
    ex.cancel_order.assert_called_once_with("sl-new", "BTCUSDT")


def test_is_algo_order_active_false_when_canceled() -> None:
    ex = MagicMock()
    ex.fetch_algo_order.return_value = {"status": "CANCELED"}
    assert is_algo_order_active(ex, "999") is False
