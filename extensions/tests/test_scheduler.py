"""Unit tests for TradingScheduler — US-010."""

from __future__ import annotations

import tempfile
from unittest.mock import patch

import pytest

from extensions.live_trading.engine.scheduler import TradingScheduler, FAST_TRACK_DIMS, ENHANCED_DIMS
from extensions.live_trading.engine.exchange import MockExchange
from extensions.live_trading.engine.position_tracker import PositionTracker
from extensions.live_trading.models import ScheduleReport


@pytest.fixture
def exchange() -> MockExchange:
    return MockExchange(seed_price=100.0)


@pytest.fixture
def positions() -> PositionTracker:
    tmp = tempfile.mkdtemp()
    t = PositionTracker(account_balance=10000.0, persist_dir=tmp)
    yield t
    t.clear()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# run_once() — basic
# ---------------------------------------------------------------------------

class TestRunOnceBasic:
    def test_returns_schedule_report(self, exchange: MockExchange, positions: PositionTracker) -> None:
        scheduler = TradingScheduler(exchange, positions)
        report = scheduler.run_once()
        assert isinstance(report, ScheduleReport)
        assert report.btc_status in ("CONDUCTION_OK", "LOCK_LONG", "LOCK_SHORT")

    def test_report_has_all_fields(self, exchange: MockExchange, positions: PositionTracker) -> None:
        scheduler = TradingScheduler(exchange, positions)
        report = scheduler.run_once()
        assert hasattr(report, "rankings")
        assert hasattr(report, "phase2_requests")
        assert hasattr(report, "watchlist")
        assert hasattr(report, "btc_status")
        assert hasattr(report, "active_positions")
        assert hasattr(report, "trading_enabled")

    def test_scan_time_recorded(self, exchange: MockExchange, positions: PositionTracker) -> None:
        scheduler = TradingScheduler(exchange, positions)
        report = scheduler.run_once()
        assert report.scan_time_ms >= 0


# ---------------------------------------------------------------------------
# Trading enabled vs disabled
# ---------------------------------------------------------------------------

class TestTradingEnabled:
    def test_trading_disabled_no_phase2_requests(self, exchange: MockExchange, positions: PositionTracker) -> None:
        scheduler = TradingScheduler(exchange, positions, trading_enabled=False)
        report = scheduler.run_once()
        assert len(report.phase2_requests) == 0

    def test_trading_enabled_by_default_false(self, exchange: MockExchange, positions: PositionTracker) -> None:
        scheduler = TradingScheduler(exchange, positions)
        assert scheduler.trading_enabled is False

    def test_trading_enabled_setter(self, exchange: MockExchange, positions: PositionTracker) -> None:
        scheduler = TradingScheduler(exchange, positions)
        scheduler.trading_enabled = True
        assert scheduler.trading_enabled is True


# ---------------------------------------------------------------------------
# BTC conduction lock
# ---------------------------------------------------------------------------

class TestBTCConduction:
    def test_lock_long_returns_empty(self, exchange: MockExchange, positions: PositionTracker) -> None:
        with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="LOCK_LONG"):
            scheduler = TradingScheduler(exchange, positions, trading_enabled=True)
            report = scheduler.run_once()
            assert report.rankings == []
            assert report.phase2_requests == []
            assert report.btc_status == "LOCK_LONG"

    def test_lock_short_returns_empty(self, exchange: MockExchange, positions: PositionTracker) -> None:
        with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="LOCK_SHORT"):
            scheduler = TradingScheduler(exchange, positions, trading_enabled=True)
            report = scheduler.run_once()
            assert report.btc_status == "LOCK_SHORT"
            assert len(report.phase2_requests) == 0

    def test_conduction_ok_proceeds(self, exchange: MockExchange, positions: PositionTracker) -> None:
        with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="CONDUCTION_OK"):
            scheduler = TradingScheduler(exchange, positions)
            report = scheduler.run_once()
            assert report.btc_status == "CONDUCTION_OK"

    def test_conduction_error_fallback(self, exchange: MockExchange, positions: PositionTracker) -> None:
        with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", side_effect=RuntimeError("fail")):
            scheduler = TradingScheduler(exchange, positions)
            report = scheduler.run_once()
            assert report.btc_status == "CONDUCTION_OK"  # graceful fallback


# ---------------------------------------------------------------------------
# Tiered decision logic
# ---------------------------------------------------------------------------

class TestScoreToRequest:
    def test_score_7_plus_fast_track(self) -> None:
        ranking = {"symbol": "A", "direction": "LONG", "score": 8, "rsi_1h": 25.0, "change_24h": -8.0}
        req = TradingScheduler._score_to_request(ranking)
        assert req is not None
        assert req.tier == "fast_track"
        assert req.dims == FAST_TRACK_DIMS
        assert {"dim5", "dim6", "dim7"}.issubset(req.dims)
        assert req.symbol == "A"

    def test_score_5_to_6_enhanced(self) -> None:
        ranking = {"symbol": "B", "direction": "SHORT", "score": 5, "rsi_1h": 75.0, "change_24h": 6.0}
        req = TradingScheduler._score_to_request(ranking)
        assert req is not None
        assert req.tier == "enhanced"
        assert req.dims == ENHANCED_DIMS
        assert {"dim5", "dim6", "dim7"}.issubset(req.dims)

    def test_score_below_5_returns_none(self) -> None:
        ranking = {"symbol": "C", "direction": "LONG", "score": 4}
        assert TradingScheduler._score_to_request(ranking) is None

    def test_score_3_returns_none(self) -> None:
        ranking = {"symbol": "C", "direction": "LONG", "score": 3}
        assert TradingScheduler._score_to_request(ranking) is None

    def test_score_7_boundary(self) -> None:
        ranking = {"symbol": "D", "direction": "LONG", "score": 7}
        req = TradingScheduler._score_to_request(ranking)
        assert req is not None
        assert req.tier == "fast_track"

    def test_score_5_boundary(self) -> None:
        ranking = {"symbol": "E", "direction": "SHORT", "score": 5}
        req = TradingScheduler._score_to_request(ranking)
        assert req is not None
        assert req.tier == "enhanced"


# ---------------------------------------------------------------------------
# Active positions
# ---------------------------------------------------------------------------

class TestActivePositions:
    def test_active_position_count_zero(self, exchange: MockExchange, positions: PositionTracker) -> None:
        scheduler = TradingScheduler(exchange, positions)
        report = scheduler.run_once()
        assert report.active_positions == 0

    def test_active_position_count_one(self, exchange: MockExchange, positions: PositionTracker) -> None:
        positions.open_position("A", "LONG", 100.0, 1.0, 90.0)
        scheduler = TradingScheduler(exchange, positions)
        report = scheduler.run_once()
        assert report.active_positions == 1


# ---------------------------------------------------------------------------
# Filtered count
# ---------------------------------------------------------------------------

class TestFilteredCount:
    def test_filtered_count_is_recorded(self, exchange: MockExchange, positions: PositionTracker) -> None:
        scheduler = TradingScheduler(exchange, positions)
        report = scheduler.run_once()
        assert report.filtered_count >= 0


# ---------------------------------------------------------------------------
# Integration: full pipeline
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_pipeline_with_conduction_ok(self) -> None:
        """Real pipeline: BTC OK → scan → empty report (random data)."""
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp()
        ex = MockExchange(seed_price=100.0)
        pos = PositionTracker(account_balance=10000.0, persist_dir=tmp)
        scheduler = TradingScheduler(ex, pos, trading_enabled=True)
        with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="CONDUCTION_OK"):
            report = scheduler.run_once()
        assert isinstance(report, ScheduleReport)
        assert report.btc_status == "CONDUCTION_OK"
        pos.clear()
        shutil.rmtree(tmp, ignore_errors=True)
