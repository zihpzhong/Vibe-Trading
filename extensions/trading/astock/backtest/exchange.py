"""BacktestExchange — wraps historical OHLCV data as AStockExchangeBase.

Core mechanism: ``_current_date`` controls the simulated "now". All data-access
methods only return data up to that date, so scanner/gate/stop see only
historically available information.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from extensions.trading.astock.live.broker import MockBroker
from extensions.trading.astock.config import AStockFeeConfig
from extensions.trading.astock.live.data import normalize_symbol
from extensions.trading.astock.live.exchange import AStockExchangeBase
from extensions.trading.astock.models import AStockPosition


def _price_limit(symbol: str) -> float:
    """Board-based price limit (same logic as backtest.engines.china_a)."""
    code = symbol.split(".")[0] if "." in symbol else symbol
    if code.startswith("300") or code.startswith("688"):
        return 0.20
    if code.startswith("8") and len(code) == 6:
        return 0.30
    return 0.10


class BacktestExchange(AStockExchangeBase):
    """An AStockExchangeBase that reads from a preloaded data_map.

    Args:
        data_map: ``{symbol: DataFrame}`` from ``loader.fetch()``.
          DataFrames must have columns ``open, high, low, close, volume``
          (or the loader-native ``vol`` which is auto-renamed) and a
          ``DatetimeIndex``. Optionally also ``pre_close``, ``pct_chg``,
          ``amount``.
        codes: Universe of symbols to consider.
        lot_size: Minimum trade unit (100 for A-shares).
        fees: Fee configuration for the underlying MockBroker.
    """

    def __init__(
        self,
        data_map: dict[str, pd.DataFrame],
        codes: list[str],
        lot_size: int = 100,
        fees: Optional[AStockFeeConfig] = None,
    ) -> None:
        self._data_map = self._normalize(data_map)
        self._codes = sorted(codes)
        self._current_date: Optional[pd.Timestamp] = None
        self._broker = MockBroker(initial_cash=1e12, fees=fees or AStockFeeConfig())
        self._lot_size = lot_size

    # ── date cursor ──

    def set_current_date(self, ts: pd.Timestamp) -> None:
        """Advance the simulated clock. Call once per bar before any query."""
        self._current_date = ts

    # ── data helpers ──

    @staticmethod
    def _normalize(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """Ensure columns and index are in canonical form."""
        out = {}
        for sym, df in data_map.items():
            if df is None or df.empty:
                continue
            df = df.copy()
            # Rename loader-native column names
            renames = {
                "vol": "volume",
                "trade_date": "date",
            }
            for old, new in renames.items():
                if old in df.columns and new not in df.columns:
                    df.rename(columns={old: new}, inplace=True)
            # Ensure DatetimeIndex
            if not isinstance(df.index, pd.DatetimeIndex):
                for col in ("date", "trade_date", "datetime", "timestamp"):
                    if col in df.columns:
                        df[col] = pd.to_datetime(df[col])
                        df.set_index(col, inplace=True)
                        break
            if not isinstance(df.index, pd.DatetimeIndex):
                continue  # skip data with no usable date index
            df.sort_index(inplace=True)
            # Fill missing standard columns
            for col in ("open", "high", "low", "close", "volume"):
                if col not in df.columns:
                    df[col] = 0.0
            out[sym] = df
        return out

    def _get_data(self, symbol: str) -> Optional[pd.DataFrame]:
        """Return the full DataFrame for *symbol*, or None."""
        sym = normalize_symbol(symbol)
        return self._data_map.get(sym)

    def _hist(self, symbol: str, limit: int = 120) -> pd.DataFrame:
        """Return data up to _current_date, limited to *limit* rows."""
        df = self._get_data(symbol)
        if df is None or self._current_date is None:
            return pd.DataFrame()
        mask = df.index <= self._current_date
        if not mask.any():
            return pd.DataFrame()
        return df.loc[mask].tail(limit)

    def _current_bar(self, symbol: str) -> Optional[pd.Series]:
        """Return the bar at _current_date, or the most recent available bar."""
        df = self._get_data(symbol)
        if df is None or self._current_date is None:
            return None
        if self._current_date in df.index:
            return df.loc[self._current_date]
        before = df.index[df.index <= self._current_date]
        if len(before) == 0:
            return None
        return df.loc[before[-1]]

    def _col(self, symbol: str, col: str, default: float = 0.0) -> float:
        bar = self._current_bar(symbol)
        if bar is None:
            return default
        val = bar.get(col, default)
        return float(val) if pd.notna(val) else default

    # ── AStockExchangeBase interface ──

    def get_daily(self, symbol: str, limit: int = 120) -> pd.DataFrame:
        hist = self._hist(symbol, limit)
        if hist.empty:
            return hist
        # Preserve index for scanner (don't reset_index)
        return hist

    def get_kline(self, symbol: str, timeframe: str = "D", limit: int = 120) -> pd.DataFrame:
        return self.get_daily(symbol, limit)

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        bar = self._current_bar(symbol)
        if bar is None:
            return {
                "symbol": symbol, "last": 0.0, "prev_close": 0.0,
                "volume": 0, "amount": 0.0, "change_pct": 0.0,
                "name": symbol, "market_cap": 0.0,
            }
        close = float(bar.get("close", 0))
        prev_close = float(bar.get("pre_close", close))
        volume = float(bar.get("volume", 0))
        # Estimate amount if not available
        amount = float(bar.get("amount", close * volume * 100))
        change_pct = float(bar.get("pct_chg", 0))
        if pd.isna(change_pct) and prev_close > 0:
            change_pct = (close / prev_close - 1) * 100
        return {
            "symbol": symbol,
            "last": close,
            "prev_close": prev_close,
            "volume": int(volume),
            "amount": amount,
            "change_pct": change_pct,
            "name": str(bar.get("name", symbol)),
            "market_cap": float(bar.get("market_cap", 0) or 0),
        }

    def get_realtime_quote(self, symbol: str) -> dict[str, Any]:
        return self.get_ticker(symbol)

    def get_market_index(self, index_code: str, limit: int = 60) -> pd.DataFrame:
        # Try the explicit index code first
        df = self._hist(index_code, limit)
        if not df.empty and "close" in df.columns:
            return df
        # Fallback: equal-weighted composite from the universe
        closes = {}
        for sym in self._codes[: min(len(self._codes), 500)]:  # cap breadth
            hist = self._hist(sym, limit)
            if len(hist) < 20:
                continue
            closes[sym] = hist["close"]
        if not closes:
            return pd.DataFrame()
        avg = pd.concat(closes.values(), axis=1).mean(axis=1)
        return pd.DataFrame({"close": avg}, index=avg.index)

    def get_market_breadth(self) -> dict[str, Any]:
        up = down = 0
        for sym in self._codes[:2000]:
            hist = self._hist(sym, 2)
            if len(hist) < 2:
                continue
            c1 = float(hist["close"].iloc[-2])
            c2 = float(hist["close"].iloc[-1])
            if c2 > c1:
                up += 1
            elif c2 < c1:
                down += 1
        total = up + down
        return {"ratio": round(up / total, 4) if total > 0 else 0.5, "up": up, "down": down}

    def is_suspended(self, symbol: str) -> bool:
        bar = self._current_bar(symbol)
        if bar is None:
            return True  # no data = treat as suspended
        volume = float(bar.get("volume", 0))
        close = float(bar.get("close", 0))
        prev_close = float(bar.get("pre_close", close))
        # Suspended ≈ zero volume and unchanged price
        return volume < 1 and abs(close - prev_close) < 1e-6

    def is_st(self, _symbol: str) -> bool:
        # No ST metadata in historical bars → always False in backtest
        return False

    def get_listing_days(self, symbol: str) -> int:
        df = self._get_data(symbol)
        if df is None or self._current_date is None:
            return 0
        hist = df[df.index <= self._current_date]
        if len(hist) < 2:
            return 0
        return (hist.index[-1] - hist.index[0]).days

    def is_limit(self, symbol: str) -> str:
        bar = self._current_bar(symbol)
        if bar is None:
            return ""
        close = float(bar.get("close", 0))
        prev_close = float(bar.get("pre_close", close))
        if prev_close <= 0:
            return ""
        pct = (close - prev_close) / prev_close
        limit = _price_limit(symbol)
        if pct >= limit - 0.001:
            return "up"
        if pct <= -limit + 0.001:
            return "down"
        return ""

    def get_orderbook(self, symbol: str, depth: int = 5) -> dict[str, Any]:
        close = self._col(symbol, "close", 100.0)
        if close <= 0:
            close = 100.0
        spread = close * 0.001
        bids = [[round(close - spread * (i + 1), 2), int(1e4 * (depth - i))] for i in range(depth)]
        asks = [[round(close + spread * (i + 1), 2), int(1e4 * (depth - i))] for i in range(depth)]
        return {"bids": bids, "asks": asks, "symbol": symbol}

    def create_buy_order(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        shares = max(self._lot_size, (shares // self._lot_size) * self._lot_size)
        result = self._broker.buy(symbol, price, shares)
        if result["status"] == "filled":
            # Advance _current_date so today-buy flag is set correctly
            pass
        return result

    def create_sell_order(self, symbol: str, price: float, shares: int) -> dict[str, Any]:
        shares = max(self._lot_size, (shares // self._lot_size) * self._lot_size)
        return self._broker.sell(symbol, price, shares)

    def get_positions(self) -> list[AStockPosition]:
        return self._broker.get_positions()

    def get_account_info(self) -> dict[str, float]:
        return self._broker.get_balance()
