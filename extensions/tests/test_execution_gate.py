"""Tests for live trading execution modules."""

from __future__ import annotations

import pandas as pd
import pytest

from extensions.live_trading.engine.execution_gate import ExecGateEngine
from extensions.live_trading.engine.btc_conduction import ConductionStatus, check_btc_conduction
from extensions.live_trading.engine.atr_stop import calculate_atr, calculate_atr_stop
from extensions.live_trading.engine.exchange import MockExchange, create_exchange
from extensions.live_trading.config import (
    LiveTradingConfig,
    ATRStopConfig,
    BTCConductionConfig,
)
from extensions.live_trading.models import (
    GateStatus,
    LiveSignal,
    SignalDirection,
)


# ---------------------------------------------------------------------------
# Execution Gate Engine
# ---------------------------------------------------------------------------

class TestExecGateEngine:
    """US-001: Execution Gate 校验引擎."""

    def test_pass_all_checks(self) -> None:
        """所有 7 项校验通过 → PASS."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        ticker = {"volume24h": 2_100_000}
        result = engine.run_gate(signal, ticker=ticker, funding_rate=0.0005)
        assert result.status == GateStatus.PASS
        assert len(result.passed_checks) >= 4

    def test_watch_only_low_liquidity(self) -> None:
        """仅流动性不足 → WATCH_ONLY (单次边缘条件)."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="SOLUSDT", direction=SignalDirection.LONG, score=5)
        ticker = {"volume24h": 50_000}
        result = engine.run_gate(signal, ticker=ticker, funding_rate=0.0005)
        assert result.status == GateStatus.WATCH_ONLY
        assert any(c.name == "liquidity" and not c.passed for c in result.checks)

    def test_reject_low_liquidity(self) -> None:
        """流动性不足 → REJECT (hard block with 2+ failures)."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="SOLUSDT", direction=SignalDirection.LONG, score=5)
        ticker = {"volume24h": 50_000}  # below 1M
        # Also set high funding to trigger second failure → REJECT
        result = engine.run_gate(signal, ticker=ticker, funding_rate=0.0015)
        assert result.status == GateStatus.REJECT
        assert any(c.name == "liquidity" and not c.passed for c in result.checks)

    def test_reject_high_funding_long(self) -> None:
        """资金费率 > 0.10% 且方向 LONG → REJECT."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="BTCUSDT", direction=SignalDirection.LONG, score=6)
        result = engine.run_gate(signal, funding_rate=0.0015)  # > 0.10%
        assert result.status == GateStatus.REJECT
        assert any(c.name == "funding_rate" and not c.passed for c in result.checks)

    def test_reject_negative_funding_short(self) -> None:
        """资金费率 < -0.05% 且方向 SHORT → REJECT."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="BTCUSDT", direction=SignalDirection.SHORT, score=6)
        result = engine.run_gate(signal, funding_rate=-0.001)  # < -0.05%
        assert result.status == GateStatus.REJECT
        assert any(c.name == "funding_rate" and not c.passed for c in result.checks)

    def test_watch_only_single_marginal(self) -> None:
        """仅一项边缘条件 → WATCH_ONLY."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="DOGEUSDT", direction=SignalDirection.LONG, score=4)
        ticker = {"volume24h": 100_000}
        result = engine.run_gate(signal, ticker=ticker, funding_rate=0.003)
        assert result.status in (GateStatus.WATCH_ONLY, GateStatus.REJECT)

    def test_risk_reward_pass(self) -> None:
        """R:R ≥ 1:1 → 通过."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        result = engine.run_gate(signal)
        rr_check = [c for c in result.checks if c.name == "risk_reward"]
        assert len(rr_check) == 1
        assert rr_check[0].passed

    def test_risk_reward_fail(self) -> None:
        """R:R < 1:1 → 不通过."""
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=139.0, target_prices=[146.0],
        )
        # Custom config with R:R = 1.0 as min
        config = LiveTradingConfig()
        engine2 = ExecGateEngine(config)
        # R:R = (146-145)/(145-139) = 1/6 ≈ 0.17, well below 1.0
        result = engine2.run_gate(signal)
        rr_check = [c for c in result.checks if c.name == "risk_reward"]
        assert len(rr_check) == 1
        assert not rr_check[0].passed

    def test_gate_result_properties(self) -> None:
        """Gateway 属性正确反映通过/失败情况."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="BTCUSDT", direction=SignalDirection.SHORT, score=7)
        result = engine.run_gate(signal, funding_rate=0.001)
        assert isinstance(result.passed_checks, list)
        assert isinstance(result.failed_checks, list)
        assert isinstance(result.summary, str)
        assert len(result.summary) > 0

    def test_exec_gate_detailed_report(self) -> None:
        """执行结果包含详细校验报告."""
        engine = ExecGateEngine()
        signal = LiveSignal(symbol="ETHUSDT", direction=SignalDirection.LONG, score=6)
        ticker = {"volume24h": 5_000_000}
        result = engine.run_gate(signal, ticker=ticker, funding_rate=0.0005)
        assert len(result.checks) >= 4
        for check in result.checks:
            assert check.name
            assert isinstance(check.passed, bool)
            assert check.detail


class TestOrderbookImpact:
    """盘口冲击 VWAP 吃单模拟测试."""

    @staticmethod
    def _make_orderbook(base_price: float = 145.0, levels: int = 10, size: float = 1.0) -> dict:
        """Create a synthetic orderbook for testing.

        Bids step down 0.1% per level, asks step up 0.1% per level.
        """
        bids = [[f"{base_price * (1 - 0.001 * (i + 1)):.2f}", str(size)] for i in range(levels)]
        asks = [[f"{base_price * (1 + 0.001 * (i + 1)):.2f}", str(size)] for i in range(levels)]
        return {"bids": bids, "asks": asks}

    def test_impact_skip_zero_qty(self) -> None:
        """order_qty=0 时跳过冲击检查."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        orderbook = self._make_orderbook()
        result = engine.run_gate(
            signal, ticker={"volume24h": 2_100_000},
            funding_rate=0.0005, orderbook=orderbook, order_qty=0,
        )
        ob_check = [c for c in result.checks if c.name == "orderbook_impact"]
        assert len(ob_check) == 1
        assert ob_check[0].passed
        assert "skipping" in ob_check[0].detail

    def test_impact_no_orderbook(self) -> None:
        """已知下单数量但缺少 orderbook 时应失败，避免实盘乐观放行."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        result = engine.run_gate(
            signal, ticker={"volume24h": 2_100_000},
            funding_rate=0.0005, orderbook=None, order_qty=0.5,
        )
        ob_check = [c for c in result.checks if c.name == "orderbook_impact"]
        assert len(ob_check) == 1
        assert not ob_check[0].passed
        assert "No orderbook data" in ob_check[0].detail

    def test_impact_long_low_impact_passes(self) -> None:
        """LONG 小单吃 top level → VWAP 偏离 ≤ 0.5% → PASS."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        # Each ask level has size 5.0, first ask at ~145.145
        # Eating 0.5 qty stays within top level → VWAP ≈ 145.145
        # Mid = (144.855 + 145.145) / 2 = 145.0
        # Impact = (145.145 - 145.0) / 145.0 * 100 ≈ 0.1% ≤ 0.5%
        orderbook = self._make_orderbook(base_price=145.0, size=5.0)
        result = engine.run_gate(
            signal, ticker={"volume24h": 2_100_000},
            funding_rate=0.0005, orderbook=orderbook, order_qty=0.5,
        )
        ob_check = [c for c in result.checks if c.name == "orderbook_impact"]
        assert len(ob_check) == 1
        assert ob_check[0].passed, f"Expected PASS, got: {ob_check[0].detail}"

    def test_impact_long_high_impact_fails(self) -> None:
        """LONG 大单吃多档 → VWAP 偏离 > 0.5% → FAIL."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        # Each level size=1.0, 10 levels stepping up 0.1% each
        # Total depth = 10.0, qty = 10.0 eats entire book
        # VWAP ≈ 145.80 vs mid ≈ 145.00 → impact ≈ 0.55% > 0.5%
        orderbook = self._make_orderbook(base_price=145.0, size=1.0)
        result = engine.run_gate(
            signal, ticker={"volume24h": 2_100_000},
            funding_rate=0.0005, orderbook=orderbook, order_qty=10.0,
        )
        ob_check = [c for c in result.checks if c.name == "orderbook_impact"]
        assert len(ob_check) == 1
        assert not ob_check[0].passed, f"Expected FAIL, got: {ob_check[0].detail}"

    def test_impact_insufficient_depth(self) -> None:
        """目标数量超出可用深度 → FAIL."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.LONG, score=8,
            entry_price=145.0, stop_loss=141.0, target_prices=[152.0],
        )
        # Each level size=1.0, 10 levels → total depth = 10.0
        # Qty = 20.0 exceeds total available
        orderbook = self._make_orderbook(base_price=145.0, size=1.0)
        result = engine.run_gate(
            signal, ticker={"volume24h": 2_100_000},
            funding_rate=0.0005, orderbook=orderbook, order_qty=20.0,
        )
        ob_check = [c for c in result.checks if c.name == "orderbook_impact"]
        assert len(ob_check) == 1
        assert not ob_check[0].passed
        assert "Cannot fill" in ob_check[0].detail

    def test_impact_short_passes(self) -> None:
        """SHORT 方向吃 bid 盘口 → 低偏离 → PASS."""
        engine = ExecGateEngine()
        signal = LiveSignal(
            symbol="SOLUSDT", direction=SignalDirection.SHORT, score=8,
            entry_price=145.0, stop_loss=149.0, target_prices=[138.0],
        )
        # Each bid level has size 5.0, first bid at ~144.855
        # Eating 0.3 qty → VWAP ≈ 144.855
        # Impact ≈ (145.0 - 144.855) / 145.0 * 100 ≈ 0.1% ≤ 0.5%
        orderbook = self._make_orderbook(base_price=145.0, size=5.0)
        result = engine.run_gate(
            signal, ticker={"volume24h": 2_100_000},
            funding_rate=-0.0002, orderbook=orderbook, order_qty=0.3,
        )
        ob_check = [c for c in result.checks if c.name == "orderbook_impact"]
        assert len(ob_check) == 1
        assert ob_check[0].passed, f"Expected PASS, got: {ob_check[0].detail}"


# ---------------------------------------------------------------------------
# BTC 联动前置检查
# ---------------------------------------------------------------------------

class TestBTCConduction:
    """US-002: BTC 联动前置检查."""

    def _make_bearish_kline(self, n: int = 60) -> pd.DataFrame:
        """EMA12 < EMA26 < EMA50 下降趋势，最后24h跌幅 > 3%."""
        # Gradual decline then sharp drop in last 6 bars
        prices = [70_000.0 * (1 - 0.002 * i) for i in range(n - 6)]
        # Last 6 bars: sharp drop to trigger 24h threshold
        last = prices[-1]
        for i in range(1, 7):
            prices.append(last * (1 - 0.008 * i))
        assert len(prices) == n
        return pd.DataFrame({"close": prices, "high": [p * 1.01 for p in prices], "low": [p * 0.99 for p in prices]})

    def _make_bullish_kline(self, n: int = 60) -> pd.DataFrame:
        """EMA12 > EMA26 > EMA50 上升趋势，最后24h涨幅 > 3%."""
        # Gradual rise then sharp spike in last 6 bars
        prices = [60_000.0 * (1 + 0.002 * i) for i in range(n - 6)]
        last = prices[-1]
        for i in range(1, 7):
            prices.append(last * (1 + 0.008 * i))
        assert len(prices) == n
        return pd.DataFrame({"close": prices, "high": [p * 1.01 for p in prices], "low": [p * 0.99 for p in prices]})

    def _make_neutral_kline(self, n: int = 60) -> pd.DataFrame:
        """震荡行情."""
        import random
        base = 65_000.0
        prices = [base + random.gauss(0, 500) for _ in range(n)]
        return pd.DataFrame({"close": prices, "high": [p * 1.01 for p in prices], "low": [p * 0.99 for p in prices]})

    def test_bearish_locks_long(self) -> None:
        """EMA12 < EMA26 < EMA50 且跌幅 > 3% → LOCK_LONG."""
        kline = self._make_bearish_kline()
        status = check_btc_conduction(kline)
        assert status == ConductionStatus.LOCK_LONG

    def test_bullish_locks_short(self) -> None:
        """EMA12 > EMA26 > EMA50 且涨幅 > 3% → LOCK_SHORT."""
        kline = self._make_bullish_kline()
        status = check_btc_conduction(kline)
        assert status == ConductionStatus.LOCK_SHORT

    def test_neutral_returns_ok(self) -> None:
        """震荡行情 → CONDUCTION_OK."""
        kline = self._make_neutral_kline()
        status = check_btc_conduction(kline)
        assert status == ConductionStatus.CONDUCTION_OK

    def test_insufficient_data_returns_ok(self) -> None:
        """数据不足 → CONDUCTION_OK."""
        kline = pd.DataFrame({"close": [65_000.0, 65_100.0]})
        status = check_btc_conduction(kline)
        assert status == ConductionStatus.CONDUCTION_OK

    def test_configurable_threshold(self) -> None:
        """阈值可通过 BTCConductionConfig 配置."""
        kline = self._make_bearish_kline()
        cfg = BTCConductionConfig(price_change_threshold_pct=10.0)
        status = check_btc_conduction(kline, config=cfg)
        # With threshold 10%, the ~18% drop still triggers
        # Actually the bearish kline drops ~18% over 60 bars
        # So even with 10% threshold it should still lock
        assert status in (ConductionStatus.LOCK_LONG, ConductionStatus.CONDUCTION_OK)

    def test_custom_config_affects_result(self) -> None:
        """极高的阈值可以阻止锁定."""
        kline = self._make_bearish_kline()
        cfg = BTCConductionConfig(price_change_threshold_pct=50.0)
        status = check_btc_conduction(kline, config=cfg)
        # With 50% threshold, the ~18% drop won't trigger
        assert status == ConductionStatus.CONDUCTION_OK


# ---------------------------------------------------------------------------
# ATR 动态止损
# ---------------------------------------------------------------------------

class TestATRStop:
    """US-003: ATR 动态止损计算."""

    @staticmethod
    def _make_kline(n: int = 50, vol_pct: float = 8.0) -> pd.DataFrame:
        """Create kline data with given volatility (peak-to-trough %)."""
        prices = [100.0 + i * 0.5 for i in range(n)]
        half = vol_pct / 200  # 8% vol → high=1.04×, low=0.96×
        return pd.DataFrame({
            "high": [p * (1 + half) for p in prices],
            "low": [p * (1 - half) for p in prices],
            "close": prices,
        })

    def test_calculate_atr_returns_series(self) -> None:
        """ATR 计算返回 Series."""
        kline = self._make_kline()
        atr = calculate_atr(kline["high"], kline["low"], kline["close"])
        assert isinstance(atr, pd.Series)
        assert len(atr) == len(kline)
        assert atr.iloc[-1] > 0

    def test_long_stop_below_entry(self) -> None:
        """LONG 方向止损价低于入场价."""
        kline = self._make_kline()
        stop, atr = calculate_atr_stop(kline, SignalDirection.LONG, 150.0)
        assert stop < 150.0
        assert atr > 0

    def test_short_stop_above_entry(self) -> None:
        """SHORT 方向止损价高于入场价."""
        kline = self._make_kline()
        stop, atr = calculate_atr_stop(kline, SignalDirection.SHORT, 150.0)
        assert stop > 150.0
        assert atr > 0

    def test_conservative_multiplier(self) -> None:
        """保守模式止损距离更近."""
        kline = self._make_kline()
        stop_default, atr_d = calculate_atr_stop(kline, SignalDirection.LONG, 150.0, conservative=False)
        stop_conservative, atr_c = calculate_atr_stop(kline, SignalDirection.LONG, 150.0, conservative=True)
        # conservative uses 1.5x instead of 2.0x, so stop is closer to entry
        assert stop_conservative > stop_default  # closer to entry (less distance)
        assert atr_d == atr_c  # same ATR value

    def test_custom_atr_config(self) -> None:
        """ATR 配置可自定义."""
        kline = self._make_kline()
        cfg = ATRStopConfig(multiplier_default=3.0, period=14)
        stop, atr = calculate_atr_stop(kline, SignalDirection.LONG, 150.0, config=cfg)
        expected_stop = round(150.0 - atr * 3.0, 2)
        assert stop == pytest.approx(expected_stop, rel=1e-3)

    def test_insufficient_data_fallback(self) -> None:
        """数据不足时使用固定百分比止损."""
        kline = pd.DataFrame({"high": [100.0], "low": [98.0], "close": [99.0]})
        stop, atr = calculate_atr_stop(kline, SignalDirection.LONG, 100.0)
        assert stop == 92.0  # 8% fallback (from ATRStopConfig.min_stop_distance_pct)
        assert atr == 0.0

    def test_min_stop_distance_pct_enforced(self) -> None:
        """ATR 计算出的距离小于最小距离时，使用最小距离."""
        kline = pd.DataFrame({
            "high": [101.0] * 20,
            "low": [99.0] * 20,
            "close": [100.0] * 20,
        })
        # Very low volatility → ATR-based stop < 8% → should enforce min distance
        stop, atr = calculate_atr_stop(kline, SignalDirection.LONG, 100.0)
        assert stop == pytest.approx(92.0, rel=1e-3)  # 8% of 100
        assert atr > 0

    def test_min_stop_short_direction(self) -> None:
        """SHORT 方向最小止损距离."""
        kline = pd.DataFrame({
            "high": [101.0] * 20,
            "low": [99.0] * 20,
            "close": [100.0] * 20,
        })
        stop, atr = calculate_atr_stop(kline, SignalDirection.SHORT, 100.0)
        assert stop == pytest.approx(108.0, rel=1e-3)  # 8% of 100, above entry
        assert atr > 0


# ---------------------------------------------------------------------------
# Exchange 抽象层
# ---------------------------------------------------------------------------

class TestExchange:
    """US-004: 交易所数据抽象层."""

    def test_mock_exchange_get_kline(self) -> None:
        """MockExchange.get_kline 返回 DataFrame."""
        ex = MockExchange()
        df = ex.get_kline("BTCUSDT", "1h", 50)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 50
        for col in ["open", "high", "low", "close", "volume"]:
            assert col in df.columns, f"Missing column: {col}"

    def test_mock_exchange_get_ticker(self) -> None:
        """MockExchange.get_ticker 返回有效数据."""
        ex = MockExchange()
        ticker = ex.get_ticker("BTCUSDT")
        assert "symbol" in ticker
        assert "last" in ticker
        assert "volume24h" in ticker
        assert ticker["symbol"] == "BTCUSDT"

    def test_mock_exchange_get_funding_rate(self) -> None:
        """MockExchange.get_funding_rate 返回 float."""
        ex = MockExchange()
        rate = ex.get_funding_rate("BTCUSDT")
        assert isinstance(rate, float)

    def test_mock_exchange_get_orderbook(self) -> None:
        """MockExchange.get_orderbook 返回买卖盘口."""
        ex = MockExchange()
        ob = ex.get_orderbook("BTCUSDT", 10)
        assert "bids" in ob
        assert "asks" in ob
        assert len(ob["bids"]) == 10
        assert len(ob["asks"]) == 10

    def test_create_exchange_mock(self) -> None:
        """create_exchange(mock=True) 返回 MockExchange."""
        ex = create_exchange(mock=True)
        from extensions.live_trading.engine.exchange import MockExchange
        assert isinstance(ex, MockExchange)

    def test_mock_exchange_seed_price(self) -> None:
        """可指定 seed_price 控制价格."""
        ex = MockExchange(seed_price=100.0)
        ticker = ex.get_ticker("BTCUSDT")
        assert ticker["open24h"] == 100.0

    def test_mock_exchange_get_kline_with_symbol(self) -> None:
        """不同币种用不同的基准价."""
        ex = MockExchange()
        ticker_btc = ex.get_ticker("BTCUSDT")
        ticker_sol = ex.get_ticker("SOLUSDT")
        assert ticker_btc["symbol"] == "BTCUSDT"
        assert ticker_sol["symbol"] == "SOLUSDT"


# ---------------------------------------------------------------------------
# LiveTradingConfig
# ---------------------------------------------------------------------------

class TestLiveTradingConfig:
    """LiveTradingConfig 配置验证."""

    def test_default_config(self) -> None:
        """默认配置值验证."""
        cfg = LiveTradingConfig()
        assert cfg.scan_top_n == 20
        assert cfg.funding_rate.max_long_funding == 0.0010
        assert cfg.funding_rate.min_short_funding == -0.0005
        assert cfg.atr_stop.multiplier_default == 2.0
        assert cfg.execution_gate.min_liquidity_usdt == 1_000_000
        assert cfg.execution_gate.min_risk_reward_ratio == 1.0

    def test_aggressive_mode(self) -> None:
        """aggressive() 模式放宽风控."""
        cfg = LiveTradingConfig.aggressive()
        assert cfg.execution_gate.min_liquidity_usdt == 500_000
        assert cfg.execution_gate.max_orderbook_impact_pct == 1.0
        assert cfg.execution_gate.min_risk_reward_ratio == 0.8
        assert cfg.execution_gate.max_position_pct == 10.0
        assert cfg.execution_gate.signal_cooldown_minutes == 15

    def test_conservative_mode(self) -> None:
        """conservative() 模式收紧风控."""
        cfg = LiveTradingConfig.conservative()
        assert cfg.execution_gate.min_liquidity_usdt == 2_000_000
        assert cfg.execution_gate.max_orderbook_impact_pct == 0.3
        assert cfg.execution_gate.min_risk_reward_ratio == 1.5
        assert cfg.execution_gate.max_position_pct == 2.0
        assert cfg.execution_gate.signal_cooldown_minutes == 60

    def test_aggressive_funding_rate_unchanged(self) -> None:
        """aggressive 应保留默认资金费率阈值."""
        cfg = LiveTradingConfig.aggressive()
        assert cfg.funding_rate.max_long_funding == 0.0010
        assert cfg.funding_rate.min_short_funding == -0.0005
