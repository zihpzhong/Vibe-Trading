"""A-share live trading configuration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AStockGateConfig:
    min_daily_amount_cny: float = 50_000_000.0
    max_position_pct: float = 20.0
    max_orderbook_impact_pct: float = 0.5
    min_risk_reward_ratio: float = 1.0
    signal_cooldown_minutes: int = 30
    min_market_cap_cny: float = 5_000_000_000.0
    min_listing_days: int = 60


@dataclass
class AStockStopConfig:
    atr_multiplier: float = 2.0
    atr_period: int = 14
    hard_stop_loss_pct: float = 7.0
    min_stop_distance_pct: float = 3.0
    max_stop_distance_pct: float = 10.0
    default_reward_risk: float = 2.0


@dataclass
class AStockFeeConfig:
    commission_rate: float = 0.00025
    stamp_tax_rate: float = 0.0005
    min_commission_cny: float = 5.0


@dataclass
class AStockDeRiskConfig:
    tail_loss_trim_pct: float = 5.0
    tail_loss_trim_fraction: float = 0.5
    tail_loss_full_pct: float = 10.0


@dataclass
class AStockTradingConfig:
    gate: AStockGateConfig = field(default_factory=AStockGateConfig)
    stop: AStockStopConfig = field(default_factory=AStockStopConfig)
    fees: AStockFeeConfig = field(default_factory=AStockFeeConfig)
    de_risk: AStockDeRiskConfig = field(default_factory=AStockDeRiskConfig)
    scan_top_n: int = 20
    scan_interval_minutes: int = 30
    position_size_pct: float = 0.12
    market_index: str = "000300.SH"
    universe: list[str] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=lambda: ["akshare", "tushare", "mock"])
    lot_size: int = 100

    @classmethod
    def conservative(cls) -> AStockTradingConfig:
        return cls(
            gate=AStockGateConfig(max_position_pct=10.0, min_risk_reward_ratio=1.2),
            stop=AStockStopConfig(atr_multiplier=1.5, hard_stop_loss_pct=5.0),
            position_size_pct=0.08,
        )

    @classmethod
    def aggressive(cls) -> AStockTradingConfig:
        return cls(
            gate=AStockGateConfig(
                min_daily_amount_cny=30_000_000.0,
                max_position_pct=25.0,
                min_risk_reward_ratio=0.8,
            ),
            stop=AStockStopConfig(atr_multiplier=2.5, hard_stop_loss_pct=8.0),
            position_size_pct=0.15,
        )
