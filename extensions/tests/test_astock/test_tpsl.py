"""Tests for A-share TP/SL monitor — T+1 awareness, stop-loss, tail risk."""

from __future__ import annotations

from unittest.mock import MagicMock


from extensions.trading.astock.config import AStockDeRiskConfig, AStockTradingConfig
from extensions.trading.astock.live.exchange import MockAStockExchange
from extensions.trading.astock.models import AStockPosition
from extensions.trading.astock.live.position import AStockPositionTracker
from extensions.trading.astock.live.tpsl_monitor import AStockTPSLMonitor


def _setup(
    shares: int = 100,
    entry: float = 100.0,
    stop_loss: float = 93.0,
    take_profit: float = 114.0,
    is_today_buy: bool = False,
    de_risk: AStockDeRiskConfig | None = None,
) -> tuple[AStockTPSLMonitor, AStockPositionTracker, MockAStockExchange, MagicMock]:
    cfg = AStockTradingConfig(data_sources=["mock"])
    if de_risk:
        cfg.de_risk = de_risk
    ex = MockAStockExchange(cfg)
    positions = AStockPositionTracker()
    on_close = MagicMock()
    monitor = AStockTPSLMonitor(ex, positions, cfg, poll_interval=0.1, on_close=on_close)
    positions.open_position(
        AStockPosition(
            symbol="600519.SH",
            name="茅台",
            shares=shares,
            entry_price=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            is_today_buy=is_today_buy,
        )
    )
    return monitor, positions, ex, on_close


class TestAStockTPSLMonitor:
    def test_skip_today_buy(self) -> None:
        """Positions bought today should be skipped (T+1)."""
        monitor, positions, ex, on_close = _setup(is_today_buy=True)
        # Mock ticker to show price below stop_loss
        ex.get_ticker = MagicMock(return_value={"last": 90.0})
        monitor._check_all("morning")
        assert positions.active_count == 1  # Not closed
        on_close.assert_not_called()

    def test_stop_loss_hit(self) -> None:
        monitor, positions, ex, on_close = _setup()
        ex.get_ticker = MagicMock(return_value={"last": 92.0})  # Below stop_loss=93
        ex.create_sell_order = MagicMock(
            return_value={"status": "filled", "filled": 100}
        )
        monitor._check_all("morning")
        assert positions.active_count == 0
        on_close.assert_called_once()

    def test_take_profit_hit(self) -> None:
        monitor, positions, ex, on_close = _setup(take_profit=115.0)
        ex.get_ticker = MagicMock(return_value={"last": 116.0})  # Above take_profit=115
        ex.create_sell_order = MagicMock(
            return_value={"status": "filled", "filled": 100}
        )
        monitor._check_all("morning")
        assert positions.active_count == 0
        on_close.assert_called_once()

    def test_no_action_in_range(self) -> None:
        monitor, positions, ex, on_close = _setup()
        ex.get_ticker = MagicMock(return_value={"last": 100.0})  # Between SL and TP
        monitor._check_all("morning")
        assert positions.active_count == 1
        on_close.assert_not_called()

    def test_tail_full_exit(self) -> None:
        """Tail session with loss >10% should full exit."""
        de_risk = AStockDeRiskConfig(tail_loss_trim_pct=5.0, tail_loss_full_pct=10.0)
        monitor, positions, ex, on_close = _setup(
            shares=100, entry=100.0, stop_loss=80.0, is_today_buy=False, de_risk=de_risk
        )
        ex.get_ticker = MagicMock(return_value={"last": 85.0})  # -15% loss
        ex.create_sell_order = MagicMock(return_value={"status": "filled", "filled": 100})
        monitor._check_all("tail")
        assert positions.active_count == 0

    def test_tail_trim(self) -> None:
        """Tail session with loss >5% should trim 50%."""
        de_risk = AStockDeRiskConfig(tail_loss_trim_pct=5.0, tail_loss_trim_fraction=0.5, tail_loss_full_pct=10.0)
        monitor, positions, ex, on_close = _setup(
            shares=200, entry=100.0, stop_loss=80.0, is_today_buy=False, de_risk=de_risk
        )
        ex.get_ticker = MagicMock(return_value={"last": 92.0})  # -8% loss
        ex.create_sell_order = MagicMock(return_value={"status": "filled", "filled": 100})
        monitor._check_all("tail")
        assert positions.active_count == 1
        assert positions.get_position("600519.SH").shares == 100

    def test_t_plus_1_sell_attempt_logged(self) -> None:
        monitor, positions, ex, on_close = _setup(is_today_buy=False)
        ex.get_ticker = MagicMock(return_value={"last": 90.0})
        ex.create_sell_order = MagicMock(
            return_value={"status": "rejected", "reason": "t_plus_1"}
        )
        # Should not crash, position stays
        monitor._check_all("morning")
        assert positions.active_count == 1

    def test_stop_mechanism(self) -> None:
        """Monitor stop event should terminate the run loop."""
        import tempfile
        from pathlib import Path

        cfg = AStockTradingConfig(data_sources=["mock"])
        ex = MockAStockExchange(cfg)
        positions = AStockPositionTracker(db_path=Path(tempfile.mktemp(suffix=".db")))
        monitor = AStockTPSLMonitor(ex, positions, cfg, poll_interval=0.05)
        assert not monitor._stop.is_set()  # noqa: SLF001
        monitor.stop()
        assert monitor._stop.is_set()  # noqa: SLF001
