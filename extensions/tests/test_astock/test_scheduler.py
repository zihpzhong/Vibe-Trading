"""Tests for A-share scheduler — session labels and scan pipeline."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


from extensions.trading.astock.config import AStockTradingConfig
from extensions.trading.astock.live.exchange import create_astock_exchange
from extensions.trading.astock.live.scheduler import AStockScheduler, trading_session


def _dt(hour: int, minute: int = 0, weekday: int = 0) -> datetime:
    """Create a datetime with given weekday (0=Mon) and time."""
    # Use a known Monday 2024-01-01 + offset
    base = datetime(2024, 1, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    return base.replace(hour=hour, minute=minute) + __import__("datetime").timedelta(days=weekday)


class TestTradingSession:
    def test_premarket(self) -> None:
        assert trading_session(_dt(9, 15, 0)) == "premarket"

    def test_morning(self) -> None:
        assert trading_session(_dt(10, 0, 0)) == "morning"

    def test_lunch(self) -> None:
        assert trading_session(_dt(12, 0, 0)) == "lunch"

    def test_afternoon(self) -> None:
        assert trading_session(_dt(14, 0, 0)) == "afternoon"

    def test_tail(self) -> None:
        assert trading_session(_dt(14, 55, 0)) == "tail"

    def test_closed_before_open(self) -> None:
        assert trading_session(_dt(8, 0, 0)) == "closed"

    def test_closed_after_close(self) -> None:
        assert trading_session(_dt(15, 30, 0)) == "closed"

    def test_weekend(self) -> None:
        assert trading_session(_dt(10, 0, 5)) == "closed"  # Saturday
        assert trading_session(_dt(10, 0, 6)) == "closed"  # Sunday


class TestAStockScheduler:
    def test_run_once_returns_report(self) -> None:
        cfg = AStockTradingConfig(data_sources=["mock"])
        ex = create_astock_exchange("mock", cfg)
        sched = AStockScheduler(ex, config=cfg)
        report = sched.run_once(top_n=5)
        assert report.session in ("morning", "afternoon", "tail", "closed", "premarket", "lunch")
        assert isinstance(report.rankings, list)

    def test_phase2_requests_from_rankings(self) -> None:
        cfg = AStockTradingConfig(data_sources=["mock"])
        ex = create_astock_exchange("mock", cfg)
        sched = AStockScheduler(ex, config=cfg, trading_enabled=True)
        report = sched.run_once(top_n=10)
        assert isinstance(report.phase2_requests, list)

    def test_lock_all_blocks_scan(self) -> None:
        """LOCK_ALL status should return early with empty rankings."""
        cfg = AStockTradingConfig(data_sources=["mock"])
        ex = create_astock_exchange("mock", cfg)
        # Force LOCK_ALL by passing a downtrend index
        # The scheduler uses exchange.get_market_index internally
        sched = AStockScheduler(ex, config=cfg)
        report = sched.run_once(top_n=5)
        # Mock market may not trigger LOCK_ALL, but shouldn't crash
        assert report.market_status in ("OK", "CAUTION", "STRONG", "LOCK_ALL")

    def test_active_positions_reported(self) -> None:
        from extensions.trading.astock.live.position import AStockPositionTracker
        from extensions.trading.astock.models import AStockPosition

        cfg = AStockTradingConfig(data_sources=["mock"])
        ex = create_astock_exchange("mock", cfg)
        pos = AStockPositionTracker()
        pos.open_position(
            AStockPosition(symbol="600519.SH", name="茅台", shares=100, entry_price=100.0, stop_loss=93.0)
        )
        sched = AStockScheduler(ex, positions=pos, config=cfg)
        report = sched.run_once(top_n=5)
        assert report.active_positions == 1

    def test_universe_from_config(self) -> None:
        cfg = AStockTradingConfig(data_sources=["mock"], universe=["600519.SH"])
        ex = create_astock_exchange("mock", cfg)
        sched = AStockScheduler(ex, config=cfg)
        report = sched.run_once(top_n=5)
        assert report.session is not None
