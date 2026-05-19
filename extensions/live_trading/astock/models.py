"""A-share live trading data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class GateStatus(str, Enum):
    PASS = "PASS"
    WATCH_ONLY = "WATCH_ONLY"
    REJECT = "REJECT"


class AStockSignalDirection(str, Enum):
    LONG = "LONG"


class MarketConductionStatus(str, Enum):
    OK = "OK"
    CAUTION = "CAUTION"
    STRONG = "STRONG"
    LOCK_ALL = "LOCK_ALL"


@dataclass
class ExecutionGateCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ExecutionGateResult:
    symbol: str
    status: GateStatus
    checks: list[ExecutionGateCheck] = field(default_factory=list)
    summary: str = ""

    def add_check(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(ExecutionGateCheck(name=name, passed=passed, detail=detail))

    @property
    def failed_checks(self) -> list[ExecutionGateCheck]:
        return [c for c in self.checks if not c.passed]


@dataclass
class AStockSignal:
    symbol: str
    name: str
    score: int
    max_score: int = 10
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    volume_ratio: float = 1.0
    turnover_rate: float = 0.0
    market_cap: float = 0.0
    board: str = "主板"
    rsi_daily: float = 50.0
    change_5d_pct: float = 0.0
    alpha_signal: float = 0.0
    gate_result: Optional[ExecutionGateResult] = None
    note: str = ""

    @property
    def direction(self) -> AStockSignalDirection:
        return AStockSignalDirection.LONG


@dataclass
class AStockPosition:
    symbol: str
    name: str
    shares: int
    entry_price: float
    stop_loss: float
    take_profit: Optional[float] = None
    opened_at: str = ""
    is_today_buy: bool = True

    def __post_init__(self) -> None:
        if not self.opened_at:
            self.opened_at = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "shares": self.shares,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "opened_at": self.opened_at,
            "is_today_buy": self.is_today_buy,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AStockPosition:
        return cls(
            symbol=d["symbol"],
            name=d.get("name", d["symbol"]),
            shares=int(d["shares"]),
            entry_price=float(d["entry_price"]),
            stop_loss=float(d.get("stop_loss", 0)),
            take_profit=float(d["take_profit"]) if d.get("take_profit") else None,
            opened_at=d.get("opened_at", ""),
            is_today_buy=bool(d.get("is_today_buy", False)),
        )


@dataclass
class AStockCloseRecord:
    symbol: str
    name: str
    shares: int
    entry_price: float
    exit_price: float
    pnl: float
    pnl_pct: float
    commission: float
    stamp_tax: float
    reason: str
    closed_at: str = ""

    def __post_init__(self) -> None:
        if not self.closed_at:
            self.closed_at = datetime.now().isoformat()


@dataclass
class AStockPhase2Request:
    symbol: str
    name: str
    score: int
    tier: str
    dims: list[str] = field(default_factory=list)
    entry_price: float = 0.0


@dataclass
class AStockScheduleReport:
    rankings: list[dict[str, Any]] = field(default_factory=list)
    phase2_requests: list[AStockPhase2Request] = field(default_factory=list)
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    market_status: str = "OK"
    active_positions: int = 0
    trading_enabled: bool = False
    scan_time_ms: float = 0.0
    filtered_count: int = 0
    session: str = "closed"
