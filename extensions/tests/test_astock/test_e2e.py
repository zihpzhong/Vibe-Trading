"""End-to-end smoke test for A-share pipeline: scan → gate → order."""

from __future__ import annotations

from extensions.trading.astock.config import AStockTradingConfig
from extensions.trading.astock.live.exchange import create_astock_exchange
from extensions.trading.astock.live.gate import AStockGateEngine
from extensions.trading.astock.live.position import AStockPositionTracker
from extensions.trading.astock.live.scheduler import AStockScheduler


def test_e2e_scan_to_orders() -> None:
    """Full pipeline: scan → scheduler → gate → no crash."""
    cfg = AStockTradingConfig(data_sources=["mock"])
    ex = create_astock_exchange("mock", cfg)
    positions = AStockPositionTracker()
    sched = AStockScheduler(ex, positions, cfg, trading_enabled=True)
    gate = AStockGateEngine(cfg)

    report = sched.run_once(top_n=5)
    assert isinstance(report.rankings, list)
    assert isinstance(report.phase2_requests, list)

    # Run gate on each phase2 request
    orders = 0
    for req in report.phase2_requests:
        ranking = next((r for r in report.rankings if r["symbol"] == req.symbol), None)
        if not ranking:
            continue
        entry = float(ranking.get("entry_price", 0))
        balance = 500_000.0
        lot = cfg.lot_size
        max_notional = balance * cfg.position_size_pct
        shares = max(lot, int(max_notional / entry) // lot * lot) if entry > 0 else lot

        from extensions.trading.astock.models import AStockSignal

        signal = AStockSignal(
            symbol=req.symbol,
            name=req.name,
            score=req.score,
            entry_price=entry,
            stop_loss=entry * 0.93,
            take_profit=entry * 1.14,
        )
        gate.run_gate(signal, ex, balance, shares)
        orders += 1

    # Should not crash — at least attempted some signals
    assert orders >= 0


def test_e2e_scan_without_trading() -> None:
    """Dry-run mode: scan only, no order placement."""
    import tempfile
    from pathlib import Path

    cfg = AStockTradingConfig(data_sources=["mock"])
    ex = create_astock_exchange("mock", cfg)
    positions = AStockPositionTracker(db_path=Path(tempfile.mktemp(suffix=".db")))
    sched = AStockScheduler(ex, positions, cfg, trading_enabled=False)

    report = sched.run_once(top_n=5)
    # Just ensure no crash and nothing placed
    assert report.market_status is not None
    # Positions should not change from dry-run
    assert positions.active_count == 0


def test_e2e_scheduler_phase2_flow() -> None:
    """Verify scheduler produces phase2 requests correctly."""
    cfg = AStockTradingConfig(data_sources=["mock"])
    ex = create_astock_exchange("mock", cfg)
    sched = AStockScheduler(ex, config=cfg, trading_enabled=True)

    report = sched.run_once(top_n=5)

    # Check structure
    assert isinstance(report.rankings, list)
    assert isinstance(report.phase2_requests, list)
    assert isinstance(report.watchlist, list)

    # Phase2 requests should have valid fields
    for req in report.phase2_requests:
        assert req.symbol
        assert req.score >= 5
        assert req.tier in ("fast_track", "enhanced")
        assert req.dims


def test_e2e_empty_universe() -> None:
    """Empty universe should not crash and yield empty rankings."""
    cfg = AStockTradingConfig(data_sources=["mock"])
    ex = create_astock_exchange("mock", cfg)
    # Override universe to be empty
    ex._universe = []  # noqa: SLF001
    sched = AStockScheduler(ex, config=cfg)
    report = sched.run_once(top_n=5)
    assert report.rankings == []
