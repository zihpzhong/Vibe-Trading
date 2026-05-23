"""Crypto backtest configuration — maps to LiveTradingConfig for scanner/gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from extensions.live_trading.config import (
    ATRStopConfig,
    BTCConductionConfig,
    DCAConfig,
    DeRiskConfig,
    ExecutionGateConfig,
    FundingRateConfig,
    LiveTradingConfig,
)


@dataclass
class CryptoBacktestConfig:
    """Backtest-specific settings for CryptoLiveBacktestEngine."""

    initial_cash: float = 50.0
    leverage: int = 5
    max_leverage: int = 5
    position_size_pct: float = 0.12
    reward_risk_ratio: float = 2.0
    scan_top_n: int = 0
    scan_entry_threshold: int = 5
    max_positions: int = 5
    min_notional_usdt: float = 20.0
    signal_cooldown_bars: int = 1
    scan_every_n_bars: int = 1
    enforce_whitelist: bool = True
    pair_whitelist: list[str] = field(default_factory=list)
    phase2_enabled: bool = False
    phase2_replay_path: str = ""
    phase2_fast_track_neutral: bool = False
    phase2_replay_swarm: bool = False
    enable_dca: bool = True
    coverage_min_pct: float = 0.90
    maker_rate: float = 0.0002
    taker_rate: float = 0.0005
    slippage: float = 0.0005
    funding_rate: float = 0.0001
    btc_symbol: str = "BTCUSDT"
    trail_activation_pct: float = 3.0
    trail_distance_pct: float = 1.5
    enable_trailing: bool = True
    enable_de_risk: bool = True
    enable_stale: bool = True
    stale_hours: float = 24.0
    stale_pnl_pct: float = 3.0
    entry_grace_bars: int = 1

    atr_stop: ATRStopConfig = field(default_factory=ATRStopConfig)
    execution_gate: ExecutionGateConfig = field(default_factory=ExecutionGateConfig)
    btc_conduction: BTCConductionConfig = field(default_factory=BTCConductionConfig)
    funding: FundingRateConfig = field(default_factory=FundingRateConfig)
    de_risk: DeRiskConfig = field(default_factory=DeRiskConfig)
    dca: DCAConfig = field(default_factory=DCAConfig)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CryptoBacktestConfig:
        atr = ATRStopConfig()
        for k in (
            "multiplier_default",
            "multiplier_conservative",
            "period",
            "min_stop_distance_pct",
            "max_stop_distance_pct",
        ):
            if k in d:
                setattr(atr, k, d[k])

        gate = ExecutionGateConfig()
        for k in (
            "min_liquidity_usdt",
            "max_orderbook_impact_pct",
            "min_risk_reward_ratio",
            "max_position_pct",
            "signal_cooldown_minutes",
        ):
            if k in d:
                setattr(gate, k, d[k])

        wl = d.get("pair_whitelist") or d.get("codes") or []
        if isinstance(wl, str):
            wl = [wl]

        return cls(
            initial_cash=float(d.get("initial_cash", d.get("initial_capital", cls.initial_cash))),
            leverage=int(d.get("leverage", d.get("max_leverage", cls.leverage))),
            max_leverage=int(d.get("max_leverage", d.get("leverage", cls.max_leverage))),
            position_size_pct=float(d.get("position_size_pct", cls.position_size_pct)),
            reward_risk_ratio=float(d.get("reward_risk_ratio", d.get("rr", cls.reward_risk_ratio))),
            scan_top_n=int(d.get("scan_top_n", cls.scan_top_n)),
            scan_entry_threshold=int(d.get("scan_entry_threshold", d.get("min_score", cls.scan_entry_threshold))),
            max_positions=int(d.get("max_positions", cls.max_positions)),
            min_notional_usdt=float(d.get("min_notional_usdt", cls.min_notional_usdt)),
            signal_cooldown_bars=int(d.get("signal_cooldown_bars", cls.signal_cooldown_bars)),
            scan_every_n_bars=int(d.get("scan_every_n_bars", cls.scan_every_n_bars)),
            enforce_whitelist=bool(d.get("enforce_whitelist", cls.enforce_whitelist)),
            pair_whitelist=list(wl),
            phase2_enabled=bool(d.get("phase2_enabled", cls.phase2_enabled)),
            phase2_replay_path=str(d.get("phase2_replay_path", cls.phase2_replay_path) or ""),
            phase2_fast_track_neutral=bool(d.get("phase2_fast_track_neutral", cls.phase2_fast_track_neutral)),
            phase2_replay_swarm=bool(d.get("phase2_replay_swarm", cls.phase2_replay_swarm)),
            enable_dca=bool(d.get("enable_dca", cls.enable_dca)),
            coverage_min_pct=float(d.get("coverage_min_pct", cls.coverage_min_pct)),
            maker_rate=float(d.get("maker_rate", cls.maker_rate)),
            taker_rate=float(d.get("taker_rate", cls.taker_rate)),
            slippage=float(d.get("slippage", cls.slippage)),
            funding_rate=float(d.get("funding_rate", cls.funding_rate)),
            btc_symbol=str(d.get("btc_symbol", cls.btc_symbol)),
            trail_activation_pct=float(d.get("trail_activation_pct", cls.trail_activation_pct)),
            trail_distance_pct=float(d.get("trail_distance_pct", cls.trail_distance_pct)),
            enable_trailing=bool(d.get("enable_trailing", cls.enable_trailing)),
            enable_de_risk=bool(d.get("enable_de_risk", cls.enable_de_risk)),
            enable_stale=bool(d.get("enable_stale", cls.enable_stale)),
            stale_hours=float(d.get("stale_hours", cls.stale_hours)),
            stale_pnl_pct=float(d.get("stale_pnl_pct", cls.stale_pnl_pct)),
            entry_grace_bars=int(d.get("entry_grace_bars", cls.entry_grace_bars)),
            atr_stop=atr,
            execution_gate=gate,
        )

    @classmethod
    def with_top50(cls, **overrides: Any) -> CryptoBacktestConfig:
        """Top50 whitelist preset (mirrors LiveTradingConfig.with_top50_whitelist)."""
        try:
            from extensions.live_trading.whitelist import load_whitelist

            symbols = [f"{b}USDT" for b in load_whitelist().symbols]
        except Exception:
            from extensions.live_trading.whitelist import TOP_50

            symbols = [f"{b}USDT" for b in TOP_50]
        base = cls(pair_whitelist=symbols, scan_top_n=0, enforce_whitelist=True)
        for k, v in overrides.items():
            if hasattr(base, k):
                setattr(base, k, v)
        return base

    def to_live_config(self) -> LiveTradingConfig:
        """Produce LiveTradingConfig for MarketScanner / ExecGateEngine."""
        dca = self.dca
        if not self.enable_dca:
            from dataclasses import replace as dc_replace

            dca = dc_replace(dca, enabled=False)
        return LiveTradingConfig(
            funding_rate=self.funding,
            atr_stop=self.atr_stop,
            btc_conduction=self.btc_conduction,
            execution_gate=self.execution_gate,
            de_risk=self.de_risk,
            dca=dca,
            scan_top_n=self.scan_top_n,
            pair_whitelist=list(self.pair_whitelist),
        )

    def gate_whitelist(self) -> list[str] | None:
        """Whitelist passed to Gate when enforce_whitelist is True."""
        if not self.enforce_whitelist or not self.pair_whitelist:
            return None
        return list(self.pair_whitelist)
