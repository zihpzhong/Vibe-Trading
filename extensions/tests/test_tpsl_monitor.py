"""Unit tests for TPSLMonitor — US-009."""

from __future__ import annotations

import tempfile
import time
from unittest.mock import MagicMock

import pytest

from extensions.live_trading.engine.position_tracker import PositionTracker
from extensions.live_trading.engine.tpsl_monitor import TPSLMonitor
from extensions.live_trading.engine.exchange import MockExchange


@pytest.fixture
def tracker() -> PositionTracker:
    tmp = tempfile.mkdtemp()
    t = PositionTracker(account_balance=10000.0, persist_dir=tmp)
    yield t
    t.clear()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def exchange() -> MockExchange:
    return MockExchange(seed_price=100.0)


@pytest.fixture
def monitor(exchange: MockExchange, tracker: PositionTracker) -> TPSLMonitor:
    return TPSLMonitor(exchange, tracker, poll_interval=0.1)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestInit:
    def test_monitor_is_daemon_thread(self) -> None:
        ex = MockExchange()
        t = PositionTracker.__new__(PositionTracker)  # skip init
        m = TPSLMonitor(ex, t)
        assert m.daemon is True
        assert m.name == "TPSLMonitor"


# ---------------------------------------------------------------------------
# TP execution
# ---------------------------------------------------------------------------

class TestTakeProfit:
    def test_tp_long_triggered_when_price_above(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0, 120.0)
        # Mock get_tickers to return price above TP
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 121.0},
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0  # position closed by TP

    def test_tp_short_triggered_when_price_below(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("SHORTUSDT", "SHORT", 100.0, 1.0, 110.0, 80.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "SHORTUSDT", "last": 79.0},
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0

    def test_tp_not_triggered_below_threshold(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """Dynamic TP at +5% (held <30min) should not trigger at +4%."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0, 120.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 104.0},  # +4% < +5% dynamic TP
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 1  # still open

    def test_tp_triggered_by_dynamic_threshold(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """Dynamic TP (+5% for <30min) triggers even without fixed TP."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)  # no fixed TP
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 106.0},  # +6% > +5% dynamic TP
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0  # triggered by dynamic TP


# ---------------------------------------------------------------------------
# SL execution
# ---------------------------------------------------------------------------

class TestStopLoss:
    def test_sl_long_triggered_when_price_below(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 89.0},
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0

    def test_sl_short_triggered_when_price_above(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("SHORTUSDT", "SHORT", 100.0, 1.0, 110.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "SHORTUSDT", "last": 111.0},
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0

    def test_sl_not_triggered_above_threshold(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 91.0},
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 1

    def test_sl_retry_on_failure(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """SL execution retries 3 times on failure."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        call_count = [0]

        def failing_sl(*args, **kwargs):
            call_count[0] += 1
            raise RuntimeError("API down")

        exchange.create_market_order = failing_sl
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 89.0},
        ])
        errors: list[Exception] = []
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05, on_error=lambda e: errors.append(e))
        monitor._poll()
        assert call_count[0] == 3  # tried 3 times
        assert len(errors) == 1  # on_error called once

    def test_sl_closes_position_on_success(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """SL closes position via STOP_MARKET."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        exchange.create_market_order = MagicMock(return_value={
            "order_id": "mkt123", "status": "closed",
        })
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 89.0},
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0
        exchange.create_market_order.assert_called_once()


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------

class TestTrailingStop:
    def test_trailing_activates_after_profit(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """Trailing stop activates when profit exceeds activation threshold."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        # Price at 104 (4% profit > 3% activation)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 104.0},
        ])
        monitor = TPSLMonitor(
            exchange, tracker, poll_interval=0.05,
            trailing_activation_pct=3.0, trail_distance_pct=1.5,
        )
        monitor._poll()
        # Trailing should be active, SL should have moved up
        assert "LONGUSDT" in monitor._trailing_stops
        new_sl = monitor._trailing_stops["LONGUSDT"]
        assert new_sl > 90.0  # moved up from original SL

    def test_trailing_only_moves_favorably(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """Trailing stop never retreats."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        monitor = TPSLMonitor(
            exchange, tracker, poll_interval=0.05,
            trailing_activation_pct=3.0, trail_distance_pct=1.5,
        )
        # First poll: price at 104 (4% profit) → trailing activates, SL = 104*0.985 = 102.44
        exchange.get_tickers = MagicMock(return_value=[{"symbol": "LONGUSDT", "last": 104.0}])
        monitor._poll()
        sl_after_first = monitor._trailing_stops["LONGUSDT"]

        # Second poll: price drops to 103 (still above SL=102.44, no TP trigger at +3%)
        exchange.get_tickers = MagicMock(return_value=[{"symbol": "LONGUSDT", "last": 103.0}])
        monitor._poll()
        sl_after_second = monitor._trailing_stops["LONGUSDT"]

        # SL should NOT have retreated — peak at 104, SL stays at 104*0.985
        assert sl_after_second == sl_after_first
        # Position still open (103 > SL, no trigger)
        assert tracker.active_count == 1

    def test_trailing_short_only_moves_down(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """Trailing stop for SHORT only moves down."""
        tracker.open_position("SHORTUSDT", "SHORT", 100.0, 1.0, 110.0)
        monitor = TPSLMonitor(
            exchange, tracker, poll_interval=0.05,
            trailing_activation_pct=3.0, trail_distance_pct=1.5,
        )
        # Price drops to 96 (4% profit) → trailing activates, SL = 96*1.015 = 97.44
        exchange.get_tickers = MagicMock(return_value=[{"symbol": "SHORTUSDT", "last": 96.0}])
        monitor._poll()
        sl_after_first = monitor._trailing_stops["SHORTUSDT"]

        # Price rebounds to 97 (still below SL=97.44, so SL doesn't trigger)
        exchange.get_tickers = MagicMock(return_value=[{"symbol": "SHORTUSDT", "last": 97.0}])
        monitor._poll()
        sl_after_second = monitor._trailing_stops["SHORTUSDT"]

        # SL should NOT have retreated (moved up would be retreating for SHORT)
        assert sl_after_second == sl_after_first
        assert tracker.active_count == 1  # 97 < SL, no trigger

    def test_trailing_not_activated_below_threshold(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """Trailing stop not activated when profit below threshold."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        # Price at 102 (2% profit < 3% activation)
        exchange.get_tickers = MagicMock(return_value=[{"symbol": "LONGUSDT", "last": 102.0}])
        monitor = TPSLMonitor(
            exchange, tracker, poll_interval=0.05,
            trailing_activation_pct=3.0, trail_distance_pct=1.5,
        )
        monitor._poll()
        # Trailing should NOT be active
        assert "LONGUSDT" not in monitor._trailing_stops


# ---------------------------------------------------------------------------
# stop() lifecycle
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_graceful_shutdown(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor.start()
        time.sleep(0.1)  # let it run a bit
        assert monitor.is_alive()
        monitor.stop(timeout=1.0)
        assert not monitor.is_alive()

    def test_stop_before_start_is_noop(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor.stop(timeout=0.5)  # should not raise
        assert not monitor.is_alive()


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

class TestCallbacks:
    def test_tp_callback_invoked(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0, 120.0)
        exchange.get_tickers = MagicMock(return_value=[{"symbol": "LONGUSDT", "last": 121.0}])
        calls: list = []
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05, on_take_profit=lambda p: calls.append(p))
        monitor._poll()
        assert len(calls) == 1
        assert calls[0].symbol == "LONGUSDT"

    def test_sl_callback_invoked(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        exchange.get_tickers = MagicMock(return_value=[{"symbol": "LONGUSDT", "last": 89.0}])
        calls: list = []
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05, on_stop_loss=lambda p: calls.append(p))
        monitor._poll()
        assert len(calls) == 1
        assert calls[0].symbol == "LONGUSDT"


# ---------------------------------------------------------------------------
# Empty positions
# ---------------------------------------------------------------------------

class TestEmptyPositions:
    def test_poll_with_no_positions(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        # Should not raise
        monitor._poll()

    def test_poll_with_zero_price(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        exchange.get_tickers = MagicMock(return_value=[{"symbol": "LONGUSDT", "last": 0}])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()  # should skip, not crash
        assert tracker.active_count == 1  # position still open


# ---------------------------------------------------------------------------
# get_tickers failure
# ---------------------------------------------------------------------------

class TestTickersFailure:
    def test_get_tickers_error_handled(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        exchange.get_tickers = MagicMock(side_effect=RuntimeError("network error"))
        errors: list[Exception] = []
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05, on_error=lambda e: errors.append(e))
        # _poll is called from run(), not directly - so on_error from _poll wouldn't fire here
        # But the poll should not raise
        monitor._poll()
        # Position should still be open (we couldn't check prices)
        assert tracker.active_count == 1
