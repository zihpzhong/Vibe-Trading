"""Tests for A-share broker — MockBroker with T+1 rules, fees, and balance."""

from __future__ import annotations

import pytest

from extensions.live_trading.astock.broker import MockBroker, create_broker
from extensions.live_trading.astock.config import AStockFeeConfig, AStockTradingConfig


class TestMockBroker:
    def test_buy_filled(self) -> None:
        broker = MockBroker(initial_cash=500_000)
        result = broker.buy("600519.SH", 100.0, 100)
        assert result["status"] == "filled"
        assert result["filled"] == 100
        assert result["commission"] > 0
        pos = broker.get_positions()
        assert len(pos) == 1
        assert pos[0].symbol == "600519.SH"
        assert pos[0].shares == 100
        assert pos[0].is_today_buy

    def test_buy_insufficient_cash(self) -> None:
        broker = MockBroker(initial_cash=100)
        result = broker.buy("600519.SH", 100.0, 100)
        assert result["status"] == "rejected"
        assert result["reason"] == "insufficient_cash"

    def test_buy_multiple_accumulates(self) -> None:
        broker = MockBroker(initial_cash=500_000)
        broker.buy("600519.SH", 100.0, 100)
        broker.buy("600519.SH", 110.0, 100)
        pos = broker.get_positions()[0]
        assert pos.shares == 200
        assert pos.entry_price == 105.0
        assert pos.is_today_buy

    def test_sell_after_t_plus_one(self) -> None:
        broker = MockBroker(initial_cash=500_000)
        broker.buy("600519.SH", 100.0, 100)
        broker.clear_today_buy_flags()
        result = broker.sell("600519.SH", 110.0, 50)
        assert result["status"] == "filled"
        assert result["commission"] > 0
        assert result["stamp_tax"] > 0
        pos = broker.get_positions()[0]
        assert pos.shares == 50

    def test_sell_t_plus_one_blocked(self) -> None:
        broker = MockBroker(initial_cash=500_000)
        broker.buy("600519.SH", 100.0, 100)
        result = broker.sell("600519.SH", 101.0, 100)
        assert result["status"] == "rejected"
        assert result["reason"] == "t_plus_1"

    def test_sell_no_position(self) -> None:
        broker = MockBroker()
        result = broker.sell("600519.SH", 100.0, 100)
        assert result["status"] == "rejected"
        assert result["reason"] == "no_position"

    def test_sell_full_exit_removes_position(self) -> None:
        broker = MockBroker(initial_cash=500_000)
        broker.buy("600519.SH", 100.0, 100)
        broker.clear_today_buy_flags()
        broker.sell("600519.SH", 110.0, 100)
        assert len(broker.get_positions()) == 0

    def test_balance_reflects_cash_and_market_value(self) -> None:
        broker = MockBroker(initial_cash=1_000_000)
        bal = broker.get_balance()
        assert bal["cash"] == 1_000_000
        assert bal["market_value"] == 0
        assert bal["total"] == 1_000_000

        broker.buy("600519.SH", 100.0, 100)
        bal = broker.get_balance()
        assert bal["cash"] < 1_000_000  # Cash decreased
        assert bal["market_value"] > 0  # Has position

    def test_cancel_known_order(self) -> None:
        broker = MockBroker(initial_cash=500_000)
        result = broker.buy("600519.SH", 100.0, 100)
        oid = result["order_id"]
        assert broker.cancel(oid) is True

    def test_cancel_unknown_order(self) -> None:
        broker = MockBroker()
        assert broker.cancel("nonexistent") is False

    def test_commission_rate_applied(self) -> None:
        fees = AStockFeeConfig(commission_rate=0.001, min_commission_cny=1.0)
        broker = MockBroker(initial_cash=500_000, fees=fees)
        result = broker.buy("600519.SH", 100.0, 100)
        expected_commission = 100.0 * 100 * 0.001
        assert result["commission"] == expected_commission

    def test_min_commission_enforced(self) -> None:
        fees = AStockFeeConfig(commission_rate=0.0001, min_commission_cny=10.0)
        broker = MockBroker(initial_cash=500_000, fees=fees)
        result = broker.buy("600519.SH", 1.0, 100)
        assert result["commission"] == 10.0  # Min commission enforced

    def test_stamp_tax_on_sell(self) -> None:
        broker = MockBroker(initial_cash=500_000)
        broker.buy("600519.SH", 100.0, 100)
        broker.clear_today_buy_flags()
        result = broker.sell("600519.SH", 110.0, 100)
        expected_stamp = 110.0 * 100 * 0.0005
        assert result["stamp_tax"] == expected_stamp

    def test_clear_today_buy_flags_immutable(self) -> None:
        """Verify clear_today_buy_flags creates new objects (immutable pattern)."""
        broker = MockBroker(initial_cash=500_000)
        broker.buy("600519.SH", 100.0, 100)
        pos_before = broker.get_positions()[0]
        broker.clear_today_buy_flags()
        pos_after = broker.get_positions()[0]
        assert pos_after.is_today_buy is False
        # Should be a different object
        assert pos_before is not pos_after


class TestCreateBroker:
    def test_create_mock(self) -> None:
        broker = create_broker("mock")
        assert isinstance(broker, MockBroker)

    def test_create_dry_run(self) -> None:
        broker = create_broker("dry-run")
        assert isinstance(broker, MockBroker)

    def test_create_empty_string(self) -> None:
        broker = create_broker("")
        assert isinstance(broker, MockBroker)

    def test_create_xtquant_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            create_broker("xtquant")

    def test_create_easytrader_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            create_broker("easytrader")

    def test_create_with_config(self) -> None:
        cfg = AStockTradingConfig()
        broker = create_broker("mock", cfg)
        assert isinstance(broker, MockBroker)
