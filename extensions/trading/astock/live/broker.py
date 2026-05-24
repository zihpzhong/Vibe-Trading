"""A-share broker interfaces — mock and future QMT/easytrader adapters."""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

from extensions.trading.astock.config import AStockFeeConfig, AStockTradingConfig
from extensions.trading.astock.live.data import normalize_symbol
from extensions.trading.astock.models import AStockPosition

logger = logging.getLogger(__name__)


class BrokerBase(ABC):
    @abstractmethod
    def buy(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        ...

    @abstractmethod
    def sell(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        ...

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        ...

    @abstractmethod
    def get_positions(self) -> list[AStockPosition]:
        ...

    @abstractmethod
    def get_balance(self) -> dict[str, float]:
        ...


class MockBroker(BrokerBase):
    """In-memory broker for dry-run and tests."""

    def __init__(
        self,
        initial_cash: float = 500_000.0,
        fees: Optional[AStockFeeConfig] = None,
    ) -> None:
        self._cash = initial_cash
        self._fees = fees or AStockFeeConfig()
        self._positions: dict[str, AStockPosition] = {}
        self._orders: dict[str, dict[str, Any]] = {}

    def buy(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        sym = normalize_symbol(symbol)
        notional = price * shares
        commission = max(notional * self._fees.commission_rate, self._fees.min_commission_cny)
        cost = notional + commission
        if cost > self._cash:
            return {"order_id": "", "status": "rejected", "reason": "insufficient_cash"}
        self._cash -= cost
        existing = self._positions.get(sym)
        if existing:
            total_shares = existing.shares + shares
            avg = (existing.entry_price * existing.shares + price * shares) / total_shares
            self._positions[sym] = AStockPosition(
                symbol=existing.symbol,
                name=existing.name,
                shares=total_shares,
                entry_price=avg,
                stop_loss=existing.stop_loss,
                take_profit=existing.take_profit,
                opened_at=existing.opened_at,
                is_today_buy=True,
            )
        else:
            self._positions[sym] = AStockPosition(
                symbol=sym,
                name=sym,
                shares=shares,
                entry_price=price,
                stop_loss=price * 0.93,
                is_today_buy=True,
            )
        oid = str(uuid.uuid4())[:8]
        self._orders[oid] = {"side": "buy", "symbol": sym, "shares": shares, "price": price}
        return {"order_id": oid, "status": "filled", "filled": shares, "commission": commission}

    def sell(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        sym = normalize_symbol(symbol)
        pos = self._positions.get(sym)
        if not pos or pos.shares < shares:
            return {"order_id": "", "status": "rejected", "reason": "no_position"}
        if pos.is_today_buy:
            return {"order_id": "", "status": "rejected", "reason": "t_plus_1"}
        notional = price * shares
        commission = max(notional * self._fees.commission_rate, self._fees.min_commission_cny)
        stamp = notional * self._fees.stamp_tax_rate
        self._cash += notional - commission - stamp
        remaining = pos.shares - shares
        if remaining <= 0:
            del self._positions[sym]
        else:
            self._positions[sym] = AStockPosition(
                symbol=pos.symbol,
                name=pos.name,
                shares=remaining,
                entry_price=pos.entry_price,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                opened_at=pos.opened_at,
                is_today_buy=pos.is_today_buy,
            )
        oid = str(uuid.uuid4())[:8]
        return {
            "order_id": oid,
            "status": "filled",
            "filled": shares,
            "commission": commission,
            "stamp_tax": stamp,
        }

    def cancel(self, order_id: str) -> bool:
        return order_id in self._orders

    def get_positions(self) -> list[AStockPosition]:
        return list(self._positions.values())

    def get_balance(self) -> dict[str, float]:
        market_value = sum(p.entry_price * p.shares for p in self._positions.values())
        return {"cash": self._cash, "market_value": market_value, "total": self._cash + market_value}

    def clear_today_buy_flags(self) -> None:
        updated = {}
        for sym, p in self._positions.items():
            updated[sym] = AStockPosition(
                symbol=p.symbol,
                name=p.name,
                shares=p.shares,
                entry_price=p.entry_price,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                opened_at=p.opened_at,
                is_today_buy=False,
            )
        self._positions.clear()
        self._positions.update(updated)


def create_broker(name: str, config: Optional[AStockTradingConfig] = None) -> BrokerBase:
    cfg = config or AStockTradingConfig()
    if name in ("mock", "dry-run", ""):
        return MockBroker(fees=cfg.fees)
    if name == "xtquant":
        raise NotImplementedError("XtquantBroker requires QMT client — use --broker mock for now")
    if name == "easytrader":
        raise NotImplementedError("EasytraderBroker requires desktop client — use --broker mock for now")
    return MockBroker(fees=cfg.fees)
