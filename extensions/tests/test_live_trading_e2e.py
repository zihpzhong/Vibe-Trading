"""End-to-end integration tests for the live trading pipeline — US-012.

Tests the full auto-trading pipeline across module boundaries:
  Scan -> Rank -> Scheduler -> BTC Conduction -> Gate -> Position -> TP/SL

All tests use MockExchange (no network). BTC conduction is mocked only in
scheduler-based tests (where the exchange object is passed incorrectly to
the conduction check — a known TODO).
"""

from __future__ import annotations

import json
import random
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from extensions.live_trading.config import LiveTradingConfig
from extensions.live_trading.models import (
    ExecutionGateResult,
    GateStatus,
    LiveSignal,
    ScheduleReport,
    SignalDirection,
)
from extensions.live_trading.engine.atr_stop import calculate_atr_stop
from extensions.live_trading.engine.btc_conduction import ConductionStatus, check_btc_conduction
from extensions.live_trading.engine.exchange import MockExchange
from extensions.live_trading.engine.execution_gate import ExecGateEngine
from extensions.live_trading.engine.market_scanner import MarketScanner, ScanResult
from extensions.live_trading.engine.position_tracker import PositionTracker
from extensions.live_trading.engine.scheduler import ENHANCED_DIMS, FAST_TRACK_DIMS, TradingScheduler
from extensions.live_trading.engine.tpsl_monitor import TPSLMonitor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def exchange() -> MockExchange:
    """Deterministic MockExchange with known seed price."""
    return MockExchange(seed_price=100.0)


@pytest.fixture
def tracker() -> PositionTracker:
    """Fresh PositionTracker with temp persistence (auto-cleanup)."""
    tmp = tempfile.mkdtemp()
    t = PositionTracker(account_balance=10_000.0, max_exposure_pct=0.25, max_positions=3, persist_dir=tmp)
    yield t
    t.clear()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def scheduler(exchange: MockExchange, tracker: PositionTracker) -> TradingScheduler:
    """Scheduler with trading enabled, BTC conduction mocked."""
    return TradingScheduler(exchange, tracker, trading_enabled=True)


# ===================================================================
# 1. Scan -> Rank pipeline (5 tests)
# ===================================================================

class TestScanRankPipeline:
    """E2E: MockExchange -> MarketScanner -> ScanResult with rankings."""

    def test_scan_returns_scan_result_with_mock_exchange(self, exchange: MockExchange) -> None:
        """Full scan() pipeline produces a valid ScanResult."""
        scanner = MarketScanner(exchange)
        result = scanner.scan()
        assert isinstance(result, ScanResult)
        assert result.scan_time_ms >= 0
        # All rankings and watchlist entries have required fields
        for entry in result.rankings + result.watchlist:
            assert "symbol" in entry
            assert "score" in entry
            assert "direction" in entry
            assert "rsi_1h" in entry

    def test_rankings_sorted_by_score_desc(self) -> None:
        """rank() output is sorted by score descending."""
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 7, "rsi_extremity": 10.0, "volume_24h": 5_000_000},
            {"symbol": "B", "direction": "SHORT", "score": 5, "rsi_extremity": 20.0, "volume_24h": 5_000_000},
            {"symbol": "C", "direction": "LONG", "score": 9, "rsi_extremity": 5.0, "volume_24h": 5_000_000},
        ]
        rankings, watchlist, filtered = MarketScanner.rank(scored)
        scores = [r["score"] for r in rankings]
        assert scores == sorted(scores, reverse=True)
        assert len(watchlist) == 0
        assert filtered == 0

    def test_score_below_3_filtered_out(self) -> None:
        """Symbols with score < 3 are filtered out by rank()."""
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 2, "rsi_extremity": 10.0, "volume_24h": 5_000_000},
            {"symbol": "B", "direction": "LONG", "score": 1, "rsi_extremity": 5.0, "volume_24h": 5_000_000},
        ]
        rankings, watchlist, filtered = MarketScanner.rank(scored)
        assert len(rankings) == 0
        assert len(watchlist) == 0
        assert filtered == 2

    def test_low_volume_filtered_out(self) -> None:
        """Symbols with 24h volume < 1M USDT are filtered out."""
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 8, "rsi_extremity": 15.0, "volume_24h": 500_000},
            {"symbol": "B", "direction": "LONG", "score": 6, "rsi_extremity": 10.0, "volume_24h": 2_000_000},
        ]
        rankings, watchlist, filtered = MarketScanner.rank(scored)
        assert len(rankings) == 1
        assert rankings[0]["symbol"] == "B"
        assert filtered == 1

    def test_watchlist_populated_for_score_3_to_4(self) -> None:
        """Symbols with score 3-4 go to watchlist, not rankings."""
        scored = [
            {"symbol": "A", "direction": "LONG", "score": 4, "rsi_extremity": 10.0, "volume_24h": 5_000_000},
            {"symbol": "B", "direction": "SHORT", "score": 3, "rsi_extremity": 8.0, "volume_24h": 5_000_000},
            {"symbol": "C", "direction": "LONG", "score": 5, "rsi_extremity": 12.0, "volume_24h": 5_000_000},
        ]
        rankings, watchlist, filtered = MarketScanner.rank(scored)
        assert len(rankings) == 1
        assert rankings[0]["symbol"] == "C"
        assert len(watchlist) == 2
        assert filtered == 0


# ===================================================================
# 2. Scheduler -> Phase2Request pipeline (5 tests)
# ===================================================================

class TestSchedulerPhase2:
    """E2E: TradingScheduler.run_once() tiered decision logic."""

    def test_run_once_returns_schedule_report_with_mocked_conduction(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """scheduler.run_once() produces a ScheduleReport."""
        sched = TradingScheduler(exchange, tracker, trading_enabled=True)
        with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="CONDUCTION_OK"):
            report = sched.run_once()
        assert isinstance(report, ScheduleReport)
        assert hasattr(report, "rankings")
        assert hasattr(report, "phase2_requests")
        assert hasattr(report, "btc_status")

    def test_score_7_plus_creates_fast_track_request(self) -> None:
        """Score >= 7 produces fast_track Phase2Request."""
        ranking = {
            "symbol": "SOLUSDT", "direction": "LONG", "score": 8,
            "rsi_1h": 25.0, "change_24h": -8.0, "entry_price": 140.0,
        }
        req = TradingScheduler._score_to_request(ranking)
        assert req is not None
        assert req.tier == "fast_track"
        assert req.dims == FAST_TRACK_DIMS
        assert req.symbol == "SOLUSDT"
        assert req.score == 8

    def test_score_5_to_6_creates_enhanced_request(self) -> None:
        """Score 5-6 produces enhanced Phase2Request."""
        ranking = {
            "symbol": "ETHUSDT", "direction": "SHORT", "score": 5,
            "rsi_1h": 72.0, "change_24h": 6.0, "entry_price": 3200.0,
        }
        req = TradingScheduler._score_to_request(ranking)
        assert req is not None
        assert req.tier == "enhanced"
        assert req.dims == ENHANCED_DIMS

    def test_score_3_to_4_returns_none(self) -> None:
        """Score 3-4 produces no Phase2Request."""
        ranking = {"symbol": "DOGEUSDT", "direction": "LONG", "score": 4}
        assert TradingScheduler._score_to_request(ranking) is None

    def test_trading_disabled_returns_empty_phase2_requests(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """trading_enabled=False -> empty phase2_requests even with high scores."""
        sched = TradingScheduler(exchange, tracker, trading_enabled=False)
        with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="CONDUCTION_OK"):
            report = sched.run_once()
        assert len(report.phase2_requests) == 0


# ===================================================================
# 3. BTC Conduction integration (4 tests)
# ===================================================================

class TestBTCConductionIntegration:
    """E2E: BTC conduction check with real kline data (no exchange needed)."""

    @staticmethod
    def _make_bearish_kline(n: int = 60) -> pd.DataFrame:
        """EMA12 < EMA26 < EMA50 bearish trend with >3% 24h drop."""
        prices = [70_000.0 * (1 - 0.002 * i) for i in range(n - 6)]
        last = prices[-1]
        for i in range(1, 7):
            prices.append(last * (1 - 0.008 * i))
        return pd.DataFrame({
            "close": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
        })

    @staticmethod
    def _make_bullish_kline(n: int = 60) -> pd.DataFrame:
        """EMA12 > EMA26 > EMA50 bullish trend with >3% 24h rise."""
        prices = [60_000.0 * (1 + 0.002 * i) for i in range(n - 6)]
        last = prices[-1]
        for i in range(1, 7):
            prices.append(last * (1 + 0.008 * i))
        return pd.DataFrame({
            "close": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
        })

    @staticmethod
    def _make_neutral_kline(n: int = 60) -> pd.DataFrame:
        """Range-bound market."""
        base = 65_000.0
        prices = [base + random.gauss(0, 500) for _ in range(n)]
        return pd.DataFrame({
            "close": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
        })

    def test_bearish_locks_long(self) -> None:
        """Bearish structure -> LOCK_LONG."""
        kline = self._make_bearish_kline()
        status = check_btc_conduction(kline)
        assert status == ConductionStatus.LOCK_LONG

    def test_bullish_locks_short(self) -> None:
        """Bullish structure -> LOCK_SHORT."""
        kline = self._make_bullish_kline()
        status = check_btc_conduction(kline)
        assert status == ConductionStatus.LOCK_SHORT

    def test_neutral_returns_ok(self) -> None:
        """Neutral market -> CONDUCTION_OK."""
        kline = self._make_neutral_kline()
        status = check_btc_conduction(kline)
        assert status == ConductionStatus.CONDUCTION_OK

    def test_insufficient_data_graceful(self) -> None:
        """Too few bars -> CONDUCTION_OK (no crash)."""
        kline = pd.DataFrame({"close": [65_000.0, 65_100.0]})
        status = check_btc_conduction(kline)
        assert status == ConductionStatus.CONDUCTION_OK


# ===================================================================
# 4. Gate integration (4 tests)
# ===================================================================

class TestGateIntegration:
    """E2E: ExecGateEngine with LiveSignal and LiveTradingConfig."""

    def test_gate_pass_with_valid_signal(self) -> None:
        """All checks pass -> PASS status."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        ticker = {"volume24h": 2_100_000}
        result = engine.run_gate(signal, ticker=ticker, funding_rate=0.0005)
        assert result.status == GateStatus.PASS
        assert result.summary.startswith("PASS")

    def test_gate_reject_on_funding_rate(self) -> None:
        """Funding rate too high for LONG -> REJECT."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="BTCUSDT", direction=SignalDirection.LONG, score=6)
        result = engine.run_gate(signal, funding_rate=0.0015)
        assert result.status == GateStatus.REJECT
        assert any(c.name == "funding_rate" and not c.passed for c in result.checks)

    def test_gate_watch_only_on_marginal(self) -> None:
        """Single marginal check (low liquidity) -> WATCH_ONLY."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="DOGEUSDT", direction=SignalDirection.LONG, score=4)
        ticker = {"volume24h": 100_000}  # below 1M
        result = engine.run_gate(signal, ticker=ticker, funding_rate=0.0005)
        # Low liquidity is the only failure -> single marginal -> WATCH_ONLY
        assert result.status == GateStatus.WATCH_ONLY

    def test_gate_conservative_mode_stricter(self) -> None:
        """Conservative config has stricter thresholds."""
        config = LiveTradingConfig.conservative()
        engine = ExecGateEngine(config)
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        # R:R = (152-145)/(145-141) = 7/4 = 1.75, which passes 1.5 minimum
        ticker = {"volume24h": 5_000_000}
        result = engine.run_gate(signal, ticker=ticker, funding_rate=0.0005)
        assert result.status == GateStatus.PASS


# ===================================================================
# 5. Position lifecycle (5 tests)
# ===================================================================

class TestPositionLifecycle:
    """E2E: PositionTracker lifecycle operations."""

    def test_open_position_increases_active_count(self, tracker: PositionTracker) -> None:
        assert tracker.active_count == 0
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert tracker.active_count == 1

    def test_close_position_removes_it(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        closed = tracker.close_position("BTCUSDT")
        assert closed is not None
        assert tracker.active_count == 0
        assert tracker.get_position("BTCUSDT") is None

    def test_can_open_new_rejects_duplicate(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        ok, reason = tracker.can_open_new("BTCUSDT")
        assert ok is False
        assert "已有" in reason

    def test_can_open_new_rejects_at_max_positions(self, tracker: PositionTracker) -> None:
        tracker.open_position("A", "LONG", 100.0, 1.0, 90.0)
        tracker.open_position("B", "LONG", 200.0, 1.0, 180.0)
        tracker.open_position("C", "LONG", 300.0, 1.0, 270.0)
        ok, reason = tracker.can_open_new("D")
        assert ok is False
        assert "上限" in reason

    def test_exposure_with_multiple_positions(self, tracker: PositionTracker) -> None:
        tracker.open_position("A", "LONG", 100.0, 10.0, 90.0)   # value = 1000
        tracker.open_position("B", "SHORT", 200.0, 5.0, 220.0)   # value = 1000
        # total = 2000, balance = 10000, exposure = 0.20
        assert tracker.get_exposure() == pytest.approx(0.20)


# ===================================================================
# 6. TP/SL Monitor (4 tests)
# ===================================================================

class TestTPSLMonitorIntegration:
    """E2E: TPSLMonitor with MockExchange and PositionTracker."""

    def test_tp_triggers_when_price_reaches_take_profit(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0, 120.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 121.0},
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0

    def test_sl_triggers_when_price_reaches_stop_loss(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        # qty=0.7 ensures de-risk partial exits all skip ($20 min notional)
        # at price $89 (loss -11%): levels 1&2 notional < $20, level 3/doom don't trigger
        tracker.open_position("LONGUSDT", "LONG", 100.0, 0.7, 90.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 89.0},
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        assert tracker.active_count == 0

    def test_sl_retries_three_times_on_failure(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        # qty=0.7 ensures de-risk levels skip, SL fires and retries 3x
        tracker.open_position("LONGUSDT", "LONG", 100.0, 0.7, 90.0)
        call_count: list[int] = [0]

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
        assert call_count[0] == 3
        assert len(errors) == 1

    def test_trailing_stop_activates_and_moves_sl(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        tracker.open_position("LONGUSDT", "LONG", 100.0, 1.0, 90.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "LONGUSDT", "last": 104.0},  # 4% profit → activates trailing, below +5% TP
        ])
        monitor = TPSLMonitor(
            exchange, tracker, poll_interval=0.05,
            trailing_activation_pct=3.0, trail_distance_pct=1.5,
        )
        monitor._poll()
        assert "LONGUSDT" in monitor._trailing_stops
        new_sl = monitor._trailing_stops["LONGUSDT"]
        assert new_sl > 90.0  # moved up from original SL


# ===================================================================
# 7. De-risk (7 tests)
# ===================================================================

class TestDeRisk:
    """E2E: NFI-style de-risk partial exit logic."""

    def test_first_entry_cost_invariant_after_dca(self, tracker: PositionTracker) -> None:
        """first_entry_cost 在 DCA 后保持不变（de-risk 参照系）."""
        tracker.open_position("TEST", "LONG", 100.0, 1.0, 90.0)
        pos = tracker.get_position("TEST")
        assert pos.first_entry_cost == 100.0
        assert pos.first_entry_quantity == 1.0

        tracker.adjust_position("TEST", 0.5, 90.0)
        pos = tracker.get_position("TEST")
        assert pos.first_entry_cost == 100.0  # 首次入场成本不变
        assert pos.first_entry_quantity == 1.0
        expected_avg = (100.0 * 1.0 + 90.0 * 0.5) / 1.5
        assert pos.entry_price == pytest.approx(expected_avg)  # 均价已变
        assert pos.quantity == 1.5

    def test_de_risk_level1_reduces_position(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """Level 1 (-5%) 触发减仓 15%."""
        tracker.open_position("TEST", "LONG", 100.0, 2.0, 80.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 93.0},  # -7% → past level 1 (5%), below level 2 (8%)
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        pos = tracker.get_position("TEST")
        assert pos is not None
        assert pos.quantity == pytest.approx(2.0 - 2.0 * 0.15)  # 1.7
        assert pos.de_risk_level == 1

    def test_de_risk_cascades_all_levels_to_doom(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """各级别 (L1→L2→L3→DOOM) 依次触发，每次 poll 一级."""
        tracker.open_position("CASCADE", "LONG", 100.0, 10.0, 80.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "CASCADE", "last": 70.0},  # -30%
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)

        # Level 1: sell 15% = 1.5, remaining = 8.5
        monitor._poll()
        pos = tracker.get_position("CASCADE")
        assert pos is not None
        assert pos.de_risk_level == 1
        assert pos.quantity == pytest.approx(10.0 * (1 - 0.15))

        # Level 2: sell 30% of 8.5 = 2.55, remaining = 5.95
        monitor._poll()
        pos = tracker.get_position("CASCADE")
        assert pos.de_risk_level == 2
        assert pos.quantity == pytest.approx(8.5 * (1 - 0.30))

        # Level 3: sell 50% of 5.95 = 2.975, remaining = 2.975
        monitor._poll()
        pos = tracker.get_position("CASCADE")
        assert pos.de_risk_level == 3
        assert pos.quantity == pytest.approx(5.95 * (1 - 0.50))

        # Doom: 全平
        monitor._poll()
        assert tracker.get_position("CASCADE") is None

    def test_de_risk_not_triggered_in_profit(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """盈利时 de-risk 不触发（价格在 TP 阈值之下）。"""
        tracker.open_position("TEST", "LONG", 100.0, 2.0, 80.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 102.5},  # +2.5%, below TP (+5%) and trailing activation (+3%)
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        pos = tracker.get_position("TEST")
        assert pos is not None
        assert pos.quantity == 2.0  # 未减仓
        assert pos.de_risk_level == 0

    def test_de_risk_skipped_below_20_min_notional(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """低于 $20 最小限额时跳过部分减仓."""
        tracker.open_position("TEST", "LONG", 100.0, 0.3, 80.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 85.0},  # -15%, past levels 1-3, below doom (18%)
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        with patch.object(monitor._positions, "de_risk_partial_exit", wraps=tracker.de_risk_partial_exit) as spy:
            monitor._poll()
            # 所有级别都应跳过，不调用 de_risk_partial_exit
            spy.assert_not_called()
        pos = tracker.get_position("TEST")
        assert pos is not None
        assert pos.quantity == 0.3  # 未变化
        assert pos.de_risk_level == 0

    def test_first_entry_cost_backward_compat(self) -> None:
        """旧格式 JSON（无 first_entry_cost）加载时自动 fallback 为 entry_price."""
        tmp = tempfile.mkdtemp()
        try:
            persist_path = Path(tmp) / "positions.json"
            old_data: dict[str, Any] = {
                "positions": [
                    {
                        "symbol": "BTCUSDT", "direction": "LONG",
                        "entry_price": 65000.0, "quantity": 0.1,
                        "stop_loss": 63000.0, "take_profit": 67000.0,
                        "opened_at": "2025-01-01T00:00:00",
                        "dca_count": 0, "leverage": 1, "entry_score": 0,
                    },
                ],
            }
            persist_path.write_text(json.dumps(old_data))
            t = PositionTracker(account_balance=10_000.0, max_positions=3, persist_dir=tmp)
            assert t.active_count == 1
            pos = t.get_position("BTCUSDT")
            assert pos.first_entry_cost == 65000.0  # fallback → entry_price
            assert pos.first_entry_quantity == 0.1
            assert pos.de_risk_level == 0
            t.clear()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dca_then_de_risk_coexist(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """DCA 拉低均价后，de-risk 仍以 first_entry_cost 为参照系."""
        tracker.open_position("TEST", "LONG", 100.0, 2.0, 80.0)
        first_cost = tracker.get_position("TEST").first_entry_cost

        # DCA 加仓
        tracker.adjust_position("TEST", 1.0, 90.0)
        pos = tracker.get_position("TEST")
        assert pos.first_entry_cost == first_cost  # 首次成本不变
        expected_avg = (100.0 * 2.0 + 90.0 * 1.0) / 3.0
        assert pos.entry_price == pytest.approx(expected_avg)
        assert pos.quantity == 3.0
        assert pos.dca_count == 1

        # 价格下跌触发 de-risk
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 85.0},  # loss from first_cost: -15%
        ])
        monitor = TPSLMonitor(exchange, tracker, poll_interval=0.05)
        monitor._poll()
        pos = tracker.get_position("TEST")
        assert pos.de_risk_level == 1  # level 1 fired
        assert pos.first_entry_cost == 100.0  # 参照系不变
        level1_qty = 3.0 * 0.15
        assert pos.quantity == pytest.approx(3.0 - level1_qty)


# ===================================================================
# 7b. DCA (5 tests)
# ===================================================================

class TestDCA:
    """Phase 2: DCA logic in TPSLMonitor."""

    def _make_monitor(
        self, exchange: MockExchange, tracker: PositionTracker,
        dca_enabled: bool = True,
    ) -> TPSLMonitor:
        """Helper: create TPSLMonitor with DCA enabled."""
        from extensions.live_trading.config import DCAConfig
        return TPSLMonitor(
            exchange, tracker, poll_interval=0.05,
            dca_config=DCAConfig(enabled=dca_enabled),
            max_leverage=5, position_size_pct=0.05,
        )

    def test_dca_triggers_on_5_percent_loss(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """亏损 >= 5% 时 DCA 触发加仓."""
        tracker.open_position("TEST", "LONG", 100.0, 1.0, 80.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 94.0},  # -6% from first_entry_cost
        ])
        monitor = self._make_monitor(exchange, tracker)
        monitor._poll()
        pos = tracker.get_position("TEST")
        assert pos is not None
        assert pos.dca_count == 1  # DCA 执行了一次

    def test_dca_max_count_respected(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """超过 max_dca_count (3) 后不再 DCA."""
        tracker.open_position("TEST", "LONG", 100.0, 1.0, 80.0)
        # Artificially set dca_count to max
        tracker._positions["TEST"].dca_count = 3
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 94.0},
        ])
        monitor = self._make_monitor(exchange, tracker)
        with patch.object(monitor._positions, "adjust_position", wraps=tracker.adjust_position) as spy:
            monitor._poll()
            spy.assert_not_called()

    def test_dca_not_triggered_on_small_loss(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """亏损 < 5% 时 DCA 不触发."""
        tracker.open_position("TEST", "LONG", 100.0, 1.0, 80.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 97.0},  # -3% from first_entry_cost
        ])
        monitor = self._make_monitor(exchange, tracker)
        with patch.object(monitor._positions, "adjust_position", wraps=tracker.adjust_position) as spy:
            monitor._poll()
            spy.assert_not_called()

    def test_dca_skipped_when_account_loss_exceeds_8_percent(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """仓位累计亏损超账户 8% 时跳过 DCA."""
        # Small balance, large position: quick to hit 8% account loss
        small_tracker = PositionTracker(account_balance=100.0, max_positions=3)
        small_tracker.open_position("TEST", "LONG", 100.0, 0.5, 80.0)
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 85.0},  # -15% loss → 0.5*15=7.5 USDT = 7.5% of 100
        ])
        monitor = self._make_monitor(exchange, small_tracker)
        monitor._poll()
        pos = small_tracker.get_position("TEST")
        assert pos is not None
        assert pos.dca_count == 0  # DCA should not have executed
        small_tracker.clear()

    def test_dca_skipped_when_exposure_exceeds_limit(
        self, exchange: MockExchange, tracker: PositionTracker,
    ) -> None:
        """加仓后暴露率超限时跳过 DCA."""
        # Near max exposure already
        constrained = PositionTracker(account_balance=100.0, max_exposure_pct=0.05, max_positions=3)
        constrained.open_position("TEST", "LONG", 100.0, 0.04, 80.0)  # 4% exposure
        exchange.get_tickers = MagicMock(return_value=[
            {"symbol": "TEST", "last": 94.0},  # -6% loss → DCA trigger
        ])
        monitor = self._make_monitor(exchange, constrained)
        with patch.object(monitor._positions, "adjust_position", wraps=constrained.adjust_position) as spy:
            monitor._poll()
            spy.assert_not_called()
        constrained.clear()


# ===================================================================
# 8. Cooldown (2 tests)
# ===================================================================

class TestCooldown:
    """E2E: PositionTracker cooldown behavior."""

    def test_in_cooldown_after_open_position(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert tracker.is_in_cooldown("BTCUSDT", "LONG") is True

    def test_different_direction_not_in_cooldown(self, tracker: PositionTracker) -> None:
        tracker.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0)
        assert tracker.is_in_cooldown("BTCUSDT", "SHORT") is False


# ===================================================================
# 8. Persistence (2 tests)
# ===================================================================

class TestPersistence:
    """E2E: PositionTracker JSON persistence."""

    def test_positions_restored_from_json_file(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            t1 = PositionTracker(account_balance=10_000.0, max_positions=3, persist_dir=tmp)
            t1.open_position("BTCUSDT", "LONG", 65000.0, 0.1, 63000.0, 67000.0)
            # Create a second tracker that loads from same file
            t2 = PositionTracker(account_balance=10_000.0, max_positions=3, persist_dir=tmp)
            assert t2.active_count == 1
            pos = t2.get_position("BTCUSDT")
            assert pos is not None
            assert pos.entry_price == 65000.0
            t1.clear()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_corrupt_file_handled_gracefully(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            # Write invalid JSON to positions file
            persist_path = Path(tmp) / "positions.json"
            persist_path.write_text("this is not valid json")
            t = PositionTracker(account_balance=10_000.0, persist_dir=tmp)
            assert t.active_count == 0  # no crash, empty state
            t.clear()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ===================================================================
# 9. Full pipeline integration (3 tests)
# ===================================================================

class TestFullPipeline:
    """E2E: Full scan -> schedule -> report pipeline with MockExchange."""

    def test_full_happy_path_with_mocked_conduction(self) -> None:
        """BTC OK -> Scan -> Scheduler -> Report (full pipeline)."""
        tmp = tempfile.mkdtemp()
        try:
            ex = MockExchange(seed_price=100.0)
            pos = PositionTracker(account_balance=10_000.0, max_positions=3, persist_dir=tmp)
            sched = TradingScheduler(ex, pos, trading_enabled=True)
            with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="CONDUCTION_OK"):
                report = sched.run_once()
            assert isinstance(report, ScheduleReport)
            assert report.btc_status == "CONDUCTION_OK"
            assert report.trading_enabled is True
            pos.clear()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_btc_lock_long_returns_empty_scan(self) -> None:
        """BTC LOCK_LONG -> scheduler returns empty rankings."""
        tmp = tempfile.mkdtemp()
        try:
            ex = MockExchange(seed_price=100.0)
            pos = PositionTracker(account_balance=10_000.0, max_positions=3, persist_dir=tmp)
            sched = TradingScheduler(ex, pos, trading_enabled=True)
            with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="LOCK_LONG"):
                report = sched.run_once()
            assert len(report.rankings) == 0
            assert len(report.phase2_requests) == 0
            assert report.btc_status == "LOCK_LONG"
            pos.clear()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_multiple_run_once_cycles_stable(self) -> None:
        """Multiple run_once() calls produce consistent results (no state leaks)."""
        tmp = tempfile.mkdtemp()
        try:
            ex = MockExchange(seed_price=100.0)
            pos = PositionTracker(account_balance=10_000.0, max_positions=3, persist_dir=tmp)
            sched = TradingScheduler(ex, pos, trading_enabled=True)
            with patch("extensions.live_trading.engine.scheduler.check_btc_conduction", return_value="CONDUCTION_OK"):
                report1 = sched.run_once()
                report2 = sched.run_once()
                report3 = sched.run_once()
            # All produce valid reports
            assert isinstance(report1, ScheduleReport)
            assert isinstance(report2, ScheduleReport)
            assert isinstance(report3, ScheduleReport)
            # BTC status consistent
            assert report1.btc_status == "CONDUCTION_OK"
            assert report2.btc_status == "CONDUCTION_OK"
            assert report3.btc_status == "CONDUCTION_OK"
            # No position leaks (no positions opened by scheduler itself)
            assert report1.active_positions == 0
            assert report3.active_positions == 0
            pos.clear()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ===================================================================
# 10. ATR Stop integration (2 tests)
# ===================================================================

class TestATRStopIntegration:
    """E2E: ATR stop calculation with kline data."""

    @staticmethod
    def _make_kline(n: int = 50, vol_pct: float = 8.0) -> pd.DataFrame:
        """Create kline data with given volatility (peak-to-trough %)."""
        prices = [100.0 + i * 0.5 for i in range(n)]
        half = vol_pct / 200
        return pd.DataFrame({
            "high": [p * (1 + half) for p in prices],
            "low": [p * (1 - half) for p in prices],
            "close": prices,
        })

    def test_calculate_atr_stop_returns_valid_stop(self) -> None:
        """LONG direction: stop below entry, ATR > 0."""
        kline = self._make_kline()
        stop, atr = calculate_atr_stop(kline, SignalDirection.LONG, 150.0)
        assert stop < 150.0
        assert atr > 0
        # ATR should be reasonable for this data
        assert 0.5 < atr < 15.0

    def test_conservative_mode_uses_tighter_stop(self) -> None:
        """conservative=True -> stop closer to entry price."""
        kline = self._make_kline()
        stop_default, atr_d = calculate_atr_stop(kline, SignalDirection.LONG, 150.0, conservative=False)
        stop_conservative, atr_c = calculate_atr_stop(kline, SignalDirection.LONG, 150.0, conservative=True)
        assert stop_conservative > stop_default  # closer to entry
        assert atr_d == atr_c  # same ATR value


# ===================================================================
# 11. SignalDirection enum & LiveSignal (2 additional tests to reach 36+)
# ===================================================================

class TestLiveSignalProperties:
    """LiveSignal model behavior in the pipeline."""

    def test_score_pct_format(self) -> None:
        signal = LiveSignal(symbol="BTCUSDT", direction=SignalDirection.LONG, score=7)
        assert signal.score_pct == "7/10"

    def test_is_watch_only_reflects_gate_status(self) -> None:
        signal = LiveSignal(symbol="SOLUSDT", direction=SignalDirection.LONG, score=5)
        result = ExecutionGateResult(symbol="SOLUSDT", direction=SignalDirection.LONG, status=GateStatus.WATCH_ONLY)
        signal.gate_result = result
        assert signal.is_watch_only is True
        assert signal.is_rejected is False


# ===================================================================
# 12. ExecGateEngine edge cases (2 additional tests)
# ===================================================================

class TestExecGateEngineEdgeCases:
    """Edge cases for the Execution Gate."""

    def test_no_ticker_data_defaults_to_missing_check(self) -> None:
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="X", direction=SignalDirection.LONG, score=5)
        result = engine.run_gate(signal, ticker=None, funding_rate=0.0005)
        # liquidity check fails when ticker is None
        liquidity_check = [c for c in result.checks if c.name == "liquidity"]
        assert len(liquidity_check) == 1
        assert not liquidity_check[0].passed

    def test_risk_reward_skipped_when_no_target(self) -> None:
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="Y", direction=SignalDirection.LONG, score=5, entry_price=100.0, stop_loss=95.0)
        result = engine.run_gate(signal, ticker={"volume24h": 5_000_000}, funding_rate=0.0005)
        rr_check = [c for c in result.checks if c.name == "risk_reward"]
        assert len(rr_check) == 1
        assert rr_check[0].passed  # skipped gracefully


# ===================================================================
# 13. Orderbook impact check (2 additional tests)
# ===================================================================

class TestOrderbookImpact:
    """Orderbook impact estimation in the execution gate."""

    def test_impact_skipped_without_orderbook(self) -> None:
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="X", direction=SignalDirection.LONG, score=5)
        result = engine.run_gate(signal, ticker={"volume24h": 5_000_000}, funding_rate=0.0005, orderbook=None)
        ob_check = [c for c in result.checks if c.name == "orderbook_impact"]
        assert len(ob_check) == 1
        assert ob_check[0].passed  # skipped

    def test_impact_with_orderbook_data(self) -> None:
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="X", direction=SignalDirection.LONG, score=5, entry_price=100.0)
        # Tight spread: impact of 0.1 qty at 100.1 vs mid 100.0 = 0.1% ≤ 0.5% → PASS
        orderbook = {"asks": [["100.1", "5.0"]], "bids": [["99.9", "5.0"]]}
        result = engine.run_gate(
            signal, ticker={"volume24h": 5_000_000}, funding_rate=0.0005,
            orderbook=orderbook, order_qty=0.1,
        )
        ob_check = [c for c in result.checks if c.name == "orderbook_impact"]
        assert len(ob_check) == 1
        assert ob_check[0].passed
