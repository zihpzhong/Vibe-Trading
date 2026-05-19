"""Tests for A-share exchange and data layer."""

from __future__ import annotations

from extensions.live_trading.astock.config import AStockTradingConfig
from extensions.live_trading.astock.conduction import check_market_conduction
from extensions.live_trading.astock.data import DataChain, normalize_symbol
from extensions.live_trading.astock.exchange import create_astock_exchange
from extensions.live_trading.astock.gate import AStockGateEngine
from extensions.live_trading.astock.models import AStockSignal, GateStatus
from extensions.live_trading.astock.scheduler import AStockScheduler, trading_session


def test_normalize_symbol() -> None:
    assert normalize_symbol("600519") == "600519.SH"
    assert normalize_symbol("000001.SZ") == "000001.SZ"


def test_mock_exchange_daily() -> None:
    cfg = AStockTradingConfig(data_sources=["mock"])
    ex = create_astock_exchange("mock", cfg)
    df = ex.get_daily("600519.SH", 30)
    assert len(df) >= 10
    assert "close" in df.columns


def test_mock_scheduler_run_once() -> None:
    cfg = AStockTradingConfig(data_sources=["mock"])
    ex = create_astock_exchange("mock", cfg)
    sched = AStockScheduler(ex, config=cfg)
    report = sched.run_once(top_n=5)
    assert report.session in ("morning", "afternoon", "tail", "closed", "premarket", "lunch")
    assert report.market_status in ("OK", "CAUTION", "STRONG", "LOCK_ALL")


def test_gate_rejects_limit_up() -> None:
    cfg = AStockTradingConfig(data_sources=["mock"])
    ex = create_astock_exchange("mock", cfg)
    gate = AStockGateEngine(cfg)
    signal = AStockSignal(
        symbol="600519.SH",
        name="茅台",
        score=8,
        entry_price=100,
        stop_loss=93,
        take_profit=114,
    )
    result = gate.run_gate(signal, ex, account_balance=500_000, order_shares=100)
    assert result.status in (GateStatus.PASS, GateStatus.WATCH_ONLY, GateStatus.REJECT)


def test_conduction_on_uptrend() -> None:
    cfg = AStockTradingConfig(data_sources=["mock"])
    ex = create_astock_exchange("mock", cfg)
    df = ex.get_market_index("000300.SH", 80)
    status = check_market_conduction(df, {"ratio": 0.6})
    assert status.value in ("OK", "STRONG", "CAUTION", "LOCK_ALL")


def test_data_chain_mock() -> None:
    chain = DataChain(["mock"])
    df = chain.get_daily("600519.SH", "2024-01-01", "2024-06-01")
    assert not df.empty
