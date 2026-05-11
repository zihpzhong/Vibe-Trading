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
        """Dynamic TP at +8% (held <30min) should not trigger at +6%."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0, 120.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 106.0},  # +6% < +8% dynamic TP
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 1  # still open

    def test_tp_triggered_by_dynamic_threshold(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """Dynamic TP (+8% for <30min) triggers even without fixed TP."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)  # no fixed TP
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 109.0},  # +9% > +8% dynamic TP
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0  # triggered by dynamic TP

    def test_time_decay_tp_can_trigger_before_far_fixed_tp(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """Fixed TP and time-decay TP use the nearer threshold, not fixed-only."""
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0, 130.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 109.0},  # +9% reaches dynamic +8%, fixed TP still far
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0


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
        """价格在 SL 之上时 SL 不触发，且 de-risk notional 足够时不触发 fallback."""
        # qty=2.0 确保 15% 部分减持名义价值 = 0.3 * 91 = $27.3 >= $20
        tracker.open_position("LONGUSDT", "LONG", 100.0, 2.0, 90.0)
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

    def test_stop_loss_has_priority_over_dca(self, exchange: MockExchange, tracker: PositionTracker) -> None:
        """SL 已击穿时应先止损，不能先 DCA 扩大亏损仓位."""
        from extensions.live_trading.config import DCAConfig

        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 95.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 94.0},
        ])
        monitor = TPSLMonitor(
            exchange, tracker, poll_interval=0.05,
            dca_config=DCAConfig(enabled=True),
            max_leverage=5,
            position_size_pct=0.05,
        )
        monitor._poll()
        assert tracker.active_count == 0
        closed = tracker.get_recent_closed(1)
        assert closed and closed[-1].reason == "SL"

    def test_dca_gate_rejects_when_funding_unavailable(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """DCA 前重跑 Gate，funding 缺失时不加仓."""
        from extensions.live_trading.config import DCAConfig
        from extensions.live_trading.engine.execution_gate import ExecGateEngine

        # qty=2.0 确保 15% de-risk 部分减持 notional = 0.3 * 94 = $28.2 >= $20
        tracker.open_position("LONGUSDT", "LONG", 100.0, 2.0, 80.0, take_profit=120.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 94.0},
        ])
        exchange.get_ticker = MagicMock(return_value={"symbol": "LONGUSDT", "last": 94.0, "volume24h": 5_000_000})
        exchange.get_funding_rate = MagicMock(side_effect=RuntimeError("funding down"))
        exchange.get_orderbook = MagicMock(return_value={
            "bids": [["93.9", "100"]],
            "asks": [["94.1", "100"]],
        })
        monitor = TPSLMonitor(
            exchange, tracker, poll_interval=0.05,
            dca_config=DCAConfig(enabled=True),
            dca_gate_engine=ExecGateEngine(),
            max_leverage=5,
            position_size_pct=0.05,
        )

        monitor._poll()

        pos = tracker.get_position("LONGUSDT")
        assert pos is not None
        assert pos.dca_count == 0


# ---------------------------------------------------------------------------
# De-risk 降风险减仓
# ---------------------------------------------------------------------------

class TestDeRisk:
    def test_de_risk_fallback_close_when_notional_too_small(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """部分减持名义价值 < $20 时 fallback 全平，避免仓位无限僵死."""
        # 小仓位: qty=0.3, price=85, 15% 减持 = 0.045 * 85 = $3.825 < $20 min
        tracker.open_position("SMALL", "LONG", 100.0, 0.3, 50.0)  # SL=50，de-risk 优先
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "SMALL", "last": 85.0},  # -15%
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        pos = tracker.get_position("SMALL")
        assert pos is None
        closed = tracker.get_recent_closed(1)
        assert len(closed) == 1
        assert closed[0].reason == "DE_RISK_1"
        assert closed[0].quantity == pytest.approx(0.3)

    def test_de_risk_partial_exit_when_notional_sufficient(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """名义价值足够时正常部分减持."""
        # 大仓位: qty=10, price=93, 15% 减持 = 1.5 * 93 = $139.5 >= $20
        tracker.open_position("BIG", "LONG", 100.0, 10.0, 50.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "BIG", "last": 93.0},  # -7% → de-risk L1
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        pos = tracker.get_position("BIG")
        assert pos is not None
        assert pos.quantity == pytest.approx(8.5)  # 15% 减持
        assert pos.de_risk_level == 1


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
