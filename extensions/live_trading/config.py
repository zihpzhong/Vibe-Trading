"""Live trading configuration.

Default values aligned with crypto-entry-analysis SKILL.md guardrails.
Override via environment variables or by creating a local config instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FundingRateConfig:
    """资金费率过滤阈值."""

    max_long_funding: float = 0.0010  # > 0.10% → 禁止追多
    min_short_funding: float = -0.0005  # < -0.05% → 禁止追空


@dataclass
class DeRiskConfig:
    """NFI 风格分级减仓（De-risk）配置.

    所有亏损阈值参照 first_entry_cost（首次入场成本），而非平均成本。
    """

    level1_loss_pct: float = 5.0  # 亏损 ≥ 5% → 触发 level 1
    level1_sell_fraction: float = 0.15  # 卖出当前仓位的 15%
    level2_loss_pct: float = 8.0
    level2_sell_fraction: float = 0.30
    level3_loss_pct: float = 12.0
    level3_sell_fraction: float = 0.50
    doom_loss_pct: float = 18.0  # 亏损 ≥ 18% → 全平


@dataclass
class ATRStopConfig:
    """ATR 动态止损配置."""

    multiplier_default: float = 2.0  # 默认 2.0 × ATR(14)
    multiplier_conservative: float = 1.5  # 保守 1.5 × ATR(14)
    period: int = 14
    min_stop_distance_pct: float = 8.0  # 最小止损距离 (%) — 确保给 DCA 留出空间


@dataclass
class BTCConductionConfig:
    """BTC 联动前置检查."""

    ema_periods_short: int = 12
    ema_periods_mid: int = 26
    ema_periods_long: int = 50
    price_change_threshold_pct: float = 3.0  # 24h 涨/跌超过此值触发锁定
    lookback_hours: int = 4


@dataclass
class ExecutionGateConfig:
    """Execution Gate 校验配置."""

    min_liquidity_usdt: float = 1_000_000  # 最小 24h 交易量
    max_orderbook_impact_pct: float = 0.5  # 最大盘口冲击
    min_risk_reward_ratio: float = 1.0  # 最低 R:R
    max_position_pct: float = 20.0  # 单币仓位上限 (占资金比例 %)
    signal_cooldown_minutes: int = 30  # 重复信号冷却时间


@dataclass
class DCAConfig:
    """DCA 阶梯加仓配置.

    All loss thresholds reference ``first_entry_cost`` (not average entry_price),
    consistent with de-risk, so that DCA triggers do not drift after the
    first add.
    """

    enabled: bool = True
    max_dca_count: int = 3
    trigger_loss_pct: float = 5.0  # ≥ 5% 亏损触发首次 DCA
    dca_multipliers: tuple = (1.25, 1.5, 1.75)  # 阶梯乘数
    dca_leverage_halved: bool = True  # DCA 使用减半杠杆
    max_account_loss_pct: float = 8.0  # 该仓位累计亏损超过此值则跳过 DCA
    dca_min_notional_usdt: float = 20.0  # Binance 最小名义价值


@dataclass
class LiveTradingConfig:
    """实盘交易综合配置.

    Usage:
        config = LiveTradingConfig()
        # override individual fields as needed
        config.execution_gate.min_liquidity_usdt = 500_000
    """

    funding_rate: FundingRateConfig = field(default_factory=FundingRateConfig)
    atr_stop: ATRStopConfig = field(default_factory=ATRStopConfig)
    btc_conduction: BTCConductionConfig = field(default_factory=BTCConductionConfig)
    execution_gate: ExecutionGateConfig = field(default_factory=ExecutionGateConfig)
    de_risk: DeRiskConfig = field(default_factory=DeRiskConfig)
    dca: DCAConfig = field(default_factory=DCAConfig)
    scan_top_n: int = 20  # Phase 1 扫描数量
    scan_batch_size: int = 5  # 并发批次大小
    default_scan_interval_minutes: int = 5  # 闪电模式默认间隔

    @classmethod
    def aggressive(cls) -> LiveTradingConfig:
        """激进模式：放宽风控阈值."""
        return cls(
            execution_gate=ExecutionGateConfig(
                min_liquidity_usdt=500_000,
                max_orderbook_impact_pct=1.0,
                min_risk_reward_ratio=0.8,
                max_position_pct=10.0,
                signal_cooldown_minutes=15,
            ),
        )

    @classmethod
    def conservative(cls) -> LiveTradingConfig:
        """保守模式：收紧风控阈值."""
        return cls(
            execution_gate=ExecutionGateConfig(
                min_liquidity_usdt=2_000_000,
                max_orderbook_impact_pct=0.3,
                min_risk_reward_ratio=1.5,
                max_position_pct=2.0,
                signal_cooldown_minutes=60,
            ),
        )

    def validate(self) -> Optional[str]:
        """Validate config fields, return error message or None."""
        if self.execution_gate.min_liquidity_usdt < 100_000:
            return f"min_liquidity_usdt ({self.execution_gate.min_liquidity_usdt}) must be >= 100,000"
        if not 0.01 <= self.execution_gate.max_position_pct <= 100.0:
            return f"max_position_pct ({self.execution_gate.max_position_pct}) must be 0.01-100.0"
        if self.execution_gate.signal_cooldown_minutes < 1:
            return f"signal_cooldown_minutes ({self.execution_gate.signal_cooldown_minutes}) must be >= 1"
        if self.default_scan_interval_minutes < 1:
            return f"default_scan_interval_minutes ({self.default_scan_interval_minutes}) must be >= 1"
        if self.scan_top_n < 1:
            return f"scan_top_n ({self.scan_top_n}) must be >= 1"
        return None
