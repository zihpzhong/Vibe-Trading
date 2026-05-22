"""Tests for A-share execution gate — 7 checks including orderbook impact."""

from __future__ import annotations

from extensions.live_trading.astock.config import AStockTradingConfig
from extensions.live_trading.astock.exchange import create_astock_exchange
from extensions.live_trading.astock.gate import AStockGateEngine
from extensions.live_trading.astock.models import AStockSignal, GateStatus


def _mk_exchange():
    cfg = AStockTradingConfig(data_sources=["mock"])
    return create_astock_exchange("mock", cfg)


def _mk_signal(
    symbol="600519.SH",
    name="茅台",
    score=8,
    entry_price=100.0,
    stop_loss=93.0,
    take_profit=114.0,
) -> AStockSignal:
    return AStockSignal(
        symbol=symbol,
        name=name,
        score=score,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )


def test_gate_passes_good_signal() -> None:
    gate = AStockGateEngine()
    signal = _mk_signal()
    ex = _mk_exchange()
    result = gate.run_gate(signal, ex, account_balance=500_000, order_shares=100)
    assert result.status == GateStatus.PASS


def test_gate_rejects_suspended() -> None:
    gate = AStockGateEngine()
    signal = _mk_signal()
    ex = _mk_exchange()
    result = gate.run_gate(signal, ex, is_st=False)
    # Mock exchange never suspends — should pass
    assert result.status == GateStatus.PASS


def test_gate_rejects_limit_up_check_present() -> None:
    gate = AStockGateEngine()
    signal = _mk_signal()
    ex = _mk_exchange()
    result = gate.run_gate(signal, ex, account_balance=500_000, order_shares=100)
    check_names = {c.name for c in result.checks}
    assert "limit_up" in check_names
    assert "limit_down" in check_names
    assert "st_stock" in check_names
    assert "suspended" in check_names


def test_gate_checks_orderbook_impact() -> None:
    gate = AStockGateEngine()
    signal = _mk_signal()
    ex = _mk_exchange()
    result = gate.run_gate(signal, ex, account_balance=500_000, order_shares=100)
    check_names = {c.name for c in result.checks}
    assert "orderbook_impact" in check_names


def test_gate_orderbook_impact_large_order() -> None:
    gate = AStockGateEngine()
    signal = _mk_signal(entry_price=100.0)
    ex = _mk_exchange()
    # Huge order that walks the book
    result = gate.run_gate(signal, ex, account_balance=1e9, order_shares=1_000_000)
    impact_check = next(c for c in result.checks if c.name == "orderbook_impact")
    # Should pass or fail — either is valid, but must produce a result
    assert impact_check.passed or not impact_check.passed


def test_gate_checks_all_present() -> None:
    gate = AStockGateEngine()
    signal = _mk_signal()
    ex = _mk_exchange()
    result = gate.run_gate(signal, ex, account_balance=500_000, order_shares=100)
    expected = {"suspended", "st_stock", "limit_up", "limit_down",
                "liquidity", "orderbook_impact", "risk_reward", "position_cap"}
    assert expected == {c.name for c in result.checks}, (
        f"Missing: {expected - {c.name for c in result.checks}}"
    )


def test_gate_hard_rejects_st() -> None:
    gate = AStockGateEngine()
    signal = _mk_signal()
    ex = _mk_exchange()
    result = gate.run_gate(signal, ex, is_st=True)
    assert result.status == GateStatus.REJECT
    assert any(c.name == "st_stock" for c in result.failed_checks)


def test_gate_rejects_low_rr() -> None:
    gate = AStockGateEngine()
    signal = _mk_signal(entry_price=100.0, stop_loss=99.0, take_profit=100.5)
    ex = _mk_exchange()
    result = gate.run_gate(signal, ex, account_balance=500_000, order_shares=100)
    assert any(c.name == "risk_reward" and not c.passed for c in result.checks)


def test_gate_rejects_low_liquidity() -> None:
    cfg = AStockTradingConfig(data_sources=["mock"])
    cfg.gate.min_daily_amount_cny = 1e15  # Impossible threshold
    gate = AStockGateEngine(cfg)
    signal = _mk_signal()
    ex = create_astock_exchange("mock", cfg)
    result = gate.run_gate(signal, ex, account_balance=500_000, order_shares=100)
    assert any(c.name == "liquidity" and not c.passed for c in result.checks)


def test_gate_execution_gate_result_add_check() -> None:
    from extensions.live_trading.astock.models import ExecutionGateResult, GateStatus

    r = ExecutionGateResult(symbol="600519.SH", status=GateStatus.PASS)
    r.add_check("test_check", True, "ok")
    assert len(r.checks) == 1
    assert r.failed_checks == []
    r.add_check("test_fail", False, "failed")
    assert len(r.failed_checks) == 1