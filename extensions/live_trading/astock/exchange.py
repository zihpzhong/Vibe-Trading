"""A-share exchange facade — market data + trading."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

from extensions.live_trading.astock.broker import BrokerBase, MockBroker, create_broker
from extensions.live_trading.astock.config import AStockTradingConfig
from extensions.live_trading.astock.data import DataChain, MOCK_UNIVERSE, infer_board, normalize_symbol
from extensions.live_trading.astock.models import AStockPosition


class AStockExchangeBase(ABC):
    @abstractmethod
    def get_daily(self, symbol: str, limit: int = 120) -> pd.DataFrame:
        ...

    @abstractmethod
    def get_kline(self, symbol: str, timeframe: str = "D", limit: int = 120) -> pd.DataFrame:
        ...

    @abstractmethod
    def get_ticker(self, symbol: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_realtime_quote(self, symbol: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_market_index(self, index_code: str, limit: int = 60) -> pd.DataFrame:
        ...

    @abstractmethod
    def get_market_breadth(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def is_suspended(self, symbol: str) -> bool:
        ...

    @abstractmethod
    def is_limit(self, symbol: str) -> str:
        ...

    @abstractmethod
    def create_buy_order(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        ...

    @abstractmethod
    def create_sell_order(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_positions(self) -> list[AStockPosition]:
        ...

    @abstractmethod
    def get_account_info(self) -> dict[str, float]:
        ...


class MockAStockExchange(AStockExchangeBase):
    def __init__(
        self,
        config: Optional[AStockTradingConfig] = None,
        broker: Optional[BrokerBase] = None,
        data: Optional[DataChain] = None,
    ) -> None:
        self.config = config or AStockTradingConfig()
        self._data = data or DataChain(self.config.data_sources)
        self._broker = broker or MockBroker(fees=self.config.fees)
        self._universe = self.config.universe or [u["symbol"] for u in MOCK_UNIVERSE]

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    def _date_range(self, limit: int) -> tuple[str, str]:
        end = datetime.now()
        start = end - timedelta(days=int(limit * 1.8))
        return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

    def get_daily(self, symbol: str, limit: int = 120) -> pd.DataFrame:
        start, end = self._date_range(limit)
        df = self._data.get_daily(symbol, start, end)
        if df.empty:
            return df
        return df.tail(limit).reset_index(drop=True)

    def get_kline(self, symbol: str, timeframe: str = "D", limit: int = 120) -> pd.DataFrame:
        if timeframe.upper() in ("D", "1D", "DAY", "DAILY"):
            return self.get_daily(symbol, limit)
        return self.get_daily(symbol, limit)

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        q = self.get_realtime_quote(symbol)
        return {
            "symbol": q["symbol"],
            "last": q["last"],
            "prev_close": q.get("prev_close", q["last"]),
            "volume": q.get("volume", 0),
            "amount": q.get("amount", 0),
            "change_pct": q.get("change_pct", 0),
        }

    def get_realtime_quote(self, symbol: str) -> dict[str, Any]:
        rows = self._data.get_realtime([symbol])
        if rows:
            return rows[0]
        sym = normalize_symbol(symbol)
        return {
            "symbol": sym,
            "name": sym,
            "last": 100.0,
            "prev_close": 100.0,
            "volume": 1e7,
            "amount": 1e9,
            "turnover_rate": 1.0,
            "change_pct": 0.0,
        }

    def get_market_index(self, index_code: str, limit: int = 60) -> pd.DataFrame:
        return self.get_daily(index_code or self.config.market_index, limit)

    def get_market_breadth(self) -> dict[str, Any]:
        return self._data.get_market_breadth()

    def is_suspended(self, symbol: str) -> bool:
        return False

    def is_limit(self, symbol: str) -> str:
        q = self.get_realtime_quote(symbol)
        board = infer_board(symbol)
        return self._data.is_limit(
            symbol, float(q["last"]), float(q.get("prev_close", q["last"])), board
        )

    def create_buy_order(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        lot = self.config.lot_size
        shares = max(lot, (shares // lot) * lot)
        return self._broker.buy(symbol, price, shares)

    def create_sell_order(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        lot = self.config.lot_size
        shares = max(lot, (shares // lot) * lot)
        return self._broker.sell(symbol, price, shares)

    def get_positions(self) -> list[AStockPosition]:
        return self._broker.get_positions()

    def get_account_info(self) -> dict[str, float]:
        return self._broker.get_balance()


def create_astock_exchange(
    mode: str = "mock",
    config: Optional[AStockTradingConfig] = None,
) -> AStockExchangeBase:
    cfg = config or AStockTradingConfig()
    broker = create_broker(mode, cfg)
    data = DataChain(cfg.data_sources)
    return MockAStockExchange(config=cfg, broker=broker, data=data)
