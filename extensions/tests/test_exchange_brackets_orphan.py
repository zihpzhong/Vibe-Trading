"""Tests for orphan bracket cleanup and symbol-wide cancel."""

from __future__ import annotations

from unittest.mock import MagicMock

from extensions.trading.crypto.live.exchange import MockExchange
from extensions.trading.crypto.live.exchange_brackets import (
    cancel_bracket_orders,
    cancel_orphan_exchange_brackets,
    cancel_symbol_bracket_algos,
    place_bracket_orders,
)
from extensions.trading.crypto.live.position_tracker import Position, PositionTracker


def test_cancel_symbol_bracket_algos_removes_orphans_keeps_tracked() -> None:
    ex = MockExchange()
    pos = Position(
        symbol="ETHUSDT",
        direction="SHORT",
        entry_price=3000.0,
        quantity=0.1,
        stop_loss=3100.0,
        take_profit=2800.0,
    )
    sl1, tp1 = place_bracket_orders(ex, pos)
    pos.sl_order_id = sl1
    pos.tp_order_id = tp1
    # Orphan duplicate SL not in DB
    ex.create_stop_loss_order("ETHUSDT", "buy", 0.1, 3200.0)
    assert len(ex.fetch_open_algo_orders("ETHUSDT")) == 3

    keep = frozenset(x for x in (sl1, tp1) if x)
    removed = cancel_symbol_bracket_algos(ex, "ETHUSDT", keep_ids=keep)
    assert removed == 1
    remaining = ex.fetch_open_algo_orders("ETHUSDT")
    assert len(remaining) == 2
    assert {o["algoId"] for o in remaining} == keep


def test_cancel_bracket_orders_clears_all_symbol_brackets() -> None:
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
        {"algoId": "999", "symbol": "ETHUSDT", "orderType": "STOP_MARKET", "algoStatus": "NEW"},
    ]
    cancel_bracket_orders(ex, pos)
    assert ex.fetch_open_algo_orders("ETHUSDT") == []


def test_cancel_orphan_exchange_brackets() -> None:
    ex = MockExchange()
    ex._open_algo_orders = [
        {"algoId": "o1", "symbol": "TONUSDT", "orderType": "STOP_MARKET", "algoStatus": "NEW"},
        {"algoId": "o2", "symbol": "INJUSDT", "orderType": "STOP_MARKET", "algoStatus": "NEW"},
    ]
    cancelled = cancel_orphan_exchange_brackets(ex, {"INJUSDT"})
    assert ("TONUSDT", "o1") in cancelled
    assert len(ex.fetch_open_algo_orders("TONUSDT")) == 0
    assert len(ex.fetch_open_algo_orders("INJUSDT")) == 1


def test_place_bracket_orders_dedupes_before_place() -> None:
    ex = MockExchange()
    ex._open_algo_orders = [
        {"algoId": "old-sl", "symbol": "BTCUSDT", "orderType": "STOP_MARKET", "algoStatus": "NEW"},
    ]
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
    open_orders = ex.fetch_open_algo_orders("BTCUSDT")
    assert len(open_orders) == 2
    assert "old-sl" not in {o["algoId"] for o in open_orders}


def test_reconcile_gone_cancels_brackets() -> None:
    from extensions.trading.crypto.live.reconcile import (
        EMPTY_EXCHANGE_CONFIRM_REQUIRED,
        reconcile_positions,
    )

    mock_exchange = MagicMock()
    mock_exchange.fetch_open_algo_orders.return_value = [
        {"algoId": "sl1", "symbol": "BTCUSDT", "orderType": "STOP_MARKET", "algoStatus": "NEW"},
    ]
    mock_exchange.create_stop_loss_order = MagicMock(return_value={"order_id": "x"})
    mock_exchange.create_take_profit_order = MagicMock(return_value={"order_id": "y"})

    import shutil
    import tempfile

    tmp = tempfile.mkdtemp()
    try:
        tracker = PositionTracker(account_balance=10_000.0, max_positions=3, persist_dir=tmp)
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.01, 63000.0)
        tracker.set_bracket_order_ids("BTCUSDT", "sl1", None)
        reconcile_positions(
            tracker, [], exchange=mock_exchange,
            empty_exchange_streak=EMPTY_EXCHANGE_CONFIRM_REQUIRED,
        )
        mock_exchange.cancel_order.assert_called()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
