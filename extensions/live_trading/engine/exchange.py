"""Cryptocurrency exchange data abstraction layer.

Provides a unified interface for fetching market data.
Supports real (OKX/CCXT) and mock modes for testing.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd


class ExchangeBase(ABC):
    """Abstract exchange data interface."""

    @abstractmethod
    def get_kline(self, symbol: str, timeframe: str = "1h", limit: int = 50) -> pd.DataFrame:
        """Fetch kline/candlestick data.

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        ...

    @abstractmethod
    def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch current ticker.

        Returns:
            dict with keys: symbol, last, open24h, volume24h, high24h, low24h
        """
        ...

    @abstractmethod
    def get_funding_rate(self, symbol: str) -> float:
        """Fetch current perpetual funding rate.

        Returns:
            Funding rate as decimal (e.g. 0.0001 = 0.01%)
        """
        ...

    @abstractmethod
    def get_orderbook(self, symbol: str, depth: int = 10) -> dict[str, list]:
        """Fetch order book depth.

        Returns:
            dict with keys: bids (list of [price, size]), asks (list of [price, size])
        """
        ...

    @abstractmethod
    def get_tickers(self, symbols: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Batch fetch tickers, filtered to USDT spot pairs.

        Returns:
            list of ticker dicts with keys: symbol, last, open24h, volume24h, high24h, low24h
        """
        ...

    @abstractmethod
    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        """Create a market order.

        Returns:
            dict with keys: order_id, symbol, side, type, amount, filled, status
        """
        ...

    @abstractmethod
    def create_stop_loss_order(self, symbol: str, side: str, amount: float, stop_price: float) -> dict:
        """Create a stop-loss order (STOP_MARKET).

        Returns:
            dict with keys: order_id, symbol, side, type, amount, stop_price, filled, status
        """
        ...

    def get_min_qty(self, symbol: str) -> float:
        """Return minimum tradeable quantity for symbol (0.0 = unknown)."""
        return 0.0


class MockExchange(ExchangeBase):
    """Mock exchange that returns simulated data.

    Used for testing and development without real network calls.
    Produces deterministic-ish data based on a seed price.
    """

    BASE_PRICES = {
        "BTCUSDT": 65_000.0,
        "ETHUSDT": 3_200.0,
        "SOLUSDT": 145.0,
        "DOGEUSDT": 0.155,
        "BNBUSDT": 580.0,
    }

    def __init__(self, seed_price: Optional[float] = None) -> None:
        self._seed_price = seed_price
        self._base_time = datetime.now()

    def get_kline(self, symbol: str = "BTCUSDT", timeframe: str = "1h", limit: int = 50) -> pd.DataFrame:
        base_price = self._seed_price or self.BASE_PRICES.get(symbol, 100.0)
        rows: list[dict] = []
        for i in range(limit):
            ts = self._base_time - timedelta(hours=limit - i)
            volatility = base_price * 0.02
            open_p = base_price + random.gauss(0, volatility)
            close_p = open_p + random.gauss(0, volatility * 0.5)
            high_p = max(open_p, close_p) + abs(random.gauss(0, volatility * 0.3))
            low_p = min(open_p, close_p) - abs(random.gauss(0, volatility * 0.3))
            volume = random.uniform(1_000, 10_000) * 100_000
            rows.append({
                "timestamp": int(ts.timestamp()),
                "open": open_p,
                "high": high_p,
                "low": low_p,
                "close": close_p,
                "volume": volume,
            })
        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)
        return df

    def get_ticker(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        base_price = self._seed_price or self.BASE_PRICES.get(symbol, 100.0)
        change_pct = random.uniform(-5.0, 5.0)
        last = base_price * (1 + change_pct / 100)
        return {
            "symbol": symbol,
            "last": last,
            "open24h": base_price,
            "volume24h": random.uniform(500, 5_000) * 1_000_000,
            "high24h": base_price * 1.03,
            "low24h": base_price * 0.97,
            "change24h": change_pct,
        }

    def get_funding_rate(self, symbol: str = "BTCUSDT") -> float:
        return random.uniform(-0.001, 0.002)

    def get_orderbook(self, symbol: str = "BTCUSDT", depth: int = 10) -> dict[str, list]:
        base_price = self._seed_price or self.BASE_PRICES.get(symbol, 100.0)
        bids = [[base_price * (1 - 0.001 * i), random.uniform(0.5, 5.0)] for i in range(1, depth + 1)]
        asks = [[base_price * (1 + 0.001 * i), random.uniform(0.5, 5.0)] for i in range(1, depth + 1)]
        return {"bids": bids, "asks": asks}

    _MOCK_TOP20_SYMBOLS = [
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
        "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
        "MATICUSDT", "SHIBUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT",
        "ETCUSDT", "XLMUSDT", "FILUSDT", "TRXUSDT", "NEARUSDT",
    ]

    def get_tickers(self, symbols: Optional[list[str]] = None) -> list[dict[str, Any]]:
        target = symbols or self._MOCK_TOP20_SYMBOLS
        result: list[dict[str, Any]] = []
        for sym in target:
            t = self.get_ticker(sym)
            result.append(t)
        result.sort(key=lambda x: x.get("volume24h", 0) or 0, reverse=True)
        return result

    # ------------------------------------------------------------------
    # Mock trading methods (for TPSLMonitor / Scheduler testing)
    # ------------------------------------------------------------------

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        return {
            "order_id": f"mock_market_{random.randint(1000, 9999)}",
            "symbol": symbol, "side": side, "type": "market",
            "amount": amount, "filled": amount, "status": "closed",
        }

    def create_stop_loss_order(self, symbol: str, side: str, amount: float, stop_price: float) -> dict:
        return {
            "order_id": f"mock_sl_{random.randint(1000, 9999)}",
            "symbol": symbol, "side": side, "type": "STOP_MARKET",
            "amount": amount, "stop_price": stop_price,
            "filled": amount, "status": "closed",
        }


def create_exchange(mock: bool = True, seed_price: Optional[float] = None) -> ExchangeBase:
    """Factory: create an exchange instance.

    Args:
        mock: If True, return MockExchange. Otherwise return RealExchange.
        seed_price: Optional base price for mock data.

    Returns:
        ExchangeBase instance.
    """
    if mock:
        return MockExchange(seed_price=seed_price)
    try:
        # Lazy import to avoid hard dependency
        from ._real_exchange import RealExchange  # type: ignore[import-untyped,unused-ignore]
        return RealExchange()
    except ImportError:
        raise ImportError(
            "RealExchange requires ccxt. Install with: pip install ccxt, or use mock=True"
        )
