"""A-share backtest configuration — extends live config with backtest-specific fields."""

from __future__ import annotations

from dataclasses import dataclass, field

from extensions.trading.astock.config import (
    AStockDeRiskConfig,
    AStockFeeConfig,
    AStockGateConfig,
    AStockStopConfig,
    AStockTradingConfig,
)


@dataclass
class AStockBacktestConfig:
    """Backtest-specific configuration for AStockBacktestEngine.

    Live engine fields that differ from defaults in backtest mode:
      - scan_top_n: how many top-ranked symbols to process per bar
      - position_size_pct: fraction of capital per position
      - stop/sl/tp: inherited from AStockStopConfig
      - fees/gate: inherited from live config
    """

    initial_cash: float = 1_000_000.0
    lot_size: int = 100
    scan_top_n: int = 20
    scan_entry_threshold: int = 5
    max_positions: int = 5
    position_size_pct: float = 0.15
    slippage: float = 0.001
    commission_rate: float = 0.00025
    commission_min: float = 5.0
    stamp_tax: float = 0.0005
    transfer_fee: float = 0.00001

    trail_activation_pct: float = 3.0
    trail_distance_pct: float = 8.0
    max_holding_days: int = 60

    gate: AStockGateConfig = field(default_factory=AStockGateConfig)
    stop: AStockStopConfig = field(default_factory=AStockStopConfig)
    de_risk: AStockDeRiskConfig = field(default_factory=AStockDeRiskConfig)
    fees: AStockFeeConfig = field(default_factory=AStockFeeConfig)

    market_index: str = "000300.SH"

    @classmethod
    def from_dict(cls, d: dict) -> AStockBacktestConfig:
        # Start with default sub-configs, then override from flat dict keys
        stop_cfg = AStockStopConfig()
        for k in ("atr_multiplier", "atr_period", "hard_stop_loss_pct",
                  "min_stop_distance_pct", "max_stop_distance_pct", "default_reward_risk",
                  "trail_activation_pct", "trail_distance_pct"):
            if k in d:
                setattr(stop_cfg, k, d[k])
        gate_cfg = AStockGateConfig()
        for k in ("min_daily_amount_cny", "max_position_pct", "max_orderbook_impact_pct",
                  "min_risk_reward_ratio", "signal_cooldown_minutes", "min_market_cap_cny",
                  "min_listing_days"):
            if k in d:
                setattr(gate_cfg, k, d[k])

        return cls(
            initial_cash=float(d.get("initial_cash", cls.initial_cash)),
            lot_size=int(d.get("lot_size", cls.lot_size)),
            scan_top_n=int(d.get("scan_top_n", cls.scan_top_n)),
            scan_entry_threshold=int(d.get("scan_entry_threshold", cls.scan_entry_threshold)),
            max_positions=int(d.get("max_positions", cls.max_positions)),
            trail_activation_pct=float(d.get("trail_activation_pct", cls.trail_activation_pct)),
            trail_distance_pct=float(d.get("trail_distance_pct", cls.trail_distance_pct)),
            max_holding_days=int(d.get("max_holding_days", cls.max_holding_days)),
            position_size_pct=float(d.get("position_size_pct", cls.position_size_pct)),
            slippage=float(d.get("slippage", cls.slippage)),
            commission_rate=float(d.get("commission_rate", cls.commission_rate)),
            commission_min=float(d.get("commission_min", cls.commission_min)),
            stamp_tax=float(d.get("stamp_tax", cls.stamp_tax)),
            transfer_fee=float(d.get("transfer_fee", cls.transfer_fee)),
            market_index=str(d.get("market_index", cls.market_index)),
            gate=gate_cfg,
            stop=stop_cfg,
        )

    def to_trading_config(self) -> AStockTradingConfig:
        """Produce a live-trading config compatible with scanner/gate/stop."""
        stop = AStockStopConfig(
            atr_multiplier=self.stop.atr_multiplier,
            atr_period=self.stop.atr_period,
            hard_stop_loss_pct=self.stop.hard_stop_loss_pct,
            min_stop_distance_pct=self.stop.min_stop_distance_pct,
            max_stop_distance_pct=self.stop.max_stop_distance_pct,
            default_reward_risk=self.stop.default_reward_risk,
            trail_activation_pct=self.trail_activation_pct,
            trail_distance_pct=self.trail_distance_pct,
        )
        return AStockTradingConfig(
            gate=self.gate,
            stop=stop,
            fees=self.fees,
            de_risk=self.de_risk,
            scan_top_n=self.scan_top_n,
            scan_entry_threshold=self.scan_entry_threshold,
            max_positions=self.max_positions,
            max_holding_days=self.max_holding_days,
            position_size_pct=self.position_size_pct,
            market_index=self.market_index,
            lot_size=self.lot_size,
            data_sources=[],
        )

    def to_flat_dict(self) -> dict:
        """Export all parameters as a flat dict for result tracking."""
        d = {
            "initial_cash": self.initial_cash,
            "lot_size": self.lot_size,
            "scan_top_n": self.scan_top_n,
            "scan_entry_threshold": self.scan_entry_threshold,
            "max_positions": self.max_positions,
            "position_size_pct": self.position_size_pct,
            "slippage": self.slippage,
            "commission_rate": self.commission_rate,
            "commission_min": self.commission_min,
            "stamp_tax": self.stamp_tax,
            "transfer_fee": self.transfer_fee,
            "market_index": self.market_index,
        }
        d.update({
            "atr_multiplier": self.stop.atr_multiplier,
            "hard_stop_loss_pct": self.stop.hard_stop_loss_pct,
            "default_reward_risk": self.stop.default_reward_risk,
            "trail_activation_pct": self.trail_activation_pct,
            "trail_distance_pct": self.trail_distance_pct,
            "max_holding_days": self.max_holding_days,
        })
        return d
