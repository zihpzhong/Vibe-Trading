"""CryptoBacktestExchange — historical OHLCV as ExchangeBase for scanner replay."""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from extensions.trading.crypto.live.exchange import ExchangeBase


def normalize_symbol(symbol: str) -> str:
    """Canonical Binance-style symbol (e.g. BTCUSDT)."""
    s = symbol.upper().replace("/", "").replace(":USDT", "").replace("-", "")
    if not s.endswith("USDT"):
        s = f"{s}USDT"
    return s


class CryptoBacktestExchange(ExchangeBase):
    """ExchangeBase backed by preloaded OHLCV; ``set_current_bar`` caps visibility."""

    def __init__(
        self,
        data_map: dict[str, pd.DataFrame],
        codes: list[str],
        funding_rate: float = 0.0001,
        min_qty: float = 0.001,
    ) -> None:
        self._data_map = self._normalize(data_map)
        self._codes = [normalize_symbol(c) for c in codes]
        self._current_ts: Optional[pd.Timestamp] = None
        self._funding_rate = funding_rate
        self._min_qty = min_qty
        self._cash = 0.0

    def set_current_bar(self, ts: pd.Timestamp) -> None:
        self._current_ts = pd.Timestamp(ts)

    def set_account_cash(self, cash: float) -> None:
        self._cash = cash

    @staticmethod
    def _normalize(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        for sym, df in data_map.items():
            if df is None or df.empty:
                continue
            key = normalize_symbol(sym)
            df = df.copy()
            if "vol" in df.columns and "volume" not in df.columns:
                df.rename(columns={"vol": "volume"}, inplace=True)
            if not isinstance(df.index, pd.DatetimeIndex):
                for col in ("timestamp", "datetime", "date", "trade_date"):
                    if col in df.columns:
                        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
                        df.set_index(col, inplace=True)
                        break
            if not isinstance(df.index, pd.DatetimeIndex):
                continue
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            df.sort_index(inplace=True)
            for col in ("open", "high", "low", "close", "volume"):
                if col not in df.columns:
                    df[col] = 0.0
            out[key] = df
        return out

    def _df(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._data_map.get(normalize_symbol(symbol))

    def _hist(self, symbol: str, limit: int = 200) -> pd.DataFrame:
        df = self._df(symbol)
        if df is None or self._current_ts is None:
            return pd.DataFrame()
        mask = df.index <= self._current_ts
        if not mask.any():
            return pd.DataFrame()
        return df.loc[mask].tail(limit)

    def _bar(self, symbol: str) -> Optional[pd.Series]:
        df = self._df(symbol)
        if df is None or self._current_ts is None:
            return None
        if self._current_ts in df.index:
            return df.loc[self._current_ts]
        before = df.index[df.index <= self._current_ts]
        if len(before) == 0:
            return None
        return df.loc[before[-1]]

    def get_kline(self, symbol: str, timeframe: str = "1h", limit: int = 50) -> pd.DataFrame:
        del timeframe
        hist = self._hist(symbol, limit)
        if hist.empty:
            return hist
        return hist[["open", "high", "low", "close", "volume"]].copy()

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        sym = normalize_symbol(symbol)
        bar = self._bar(sym)
        if bar is None:
            return {
                "symbol": sym,
                "last": 0.0,
                "open24h": 0.0,
                "volume24h": 0.0,
                "high24h": 0.0,
                "low24h": 0.0,
                "change24h": 0.0,
            }
        close = float(bar["close"])
        hist = self._hist(sym, 24)
        if len(hist) >= 2:
            open24 = float(hist["close"].iloc[0])
            change24h = (close / open24 - 1) * 100 if open24 > 0 else 0.0
            vol24 = float(hist["volume"].sum())
            high24 = float(hist["high"].max())
            low24 = float(hist["low"].min())
        else:
            open24 = close
            change24h = 0.0
            vol24 = float(bar.get("volume", 0))
            high24 = float(bar.get("high", close))
            low24 = float(bar.get("low", close))
        return {
            "symbol": sym,
            "last": close,
            "open24h": open24,
            "volume24h": vol24 * close,
            "high24h": high24,
            "low24h": low24,
            "change24h": change24h,
        }

    def get_tickers(self, symbols: Optional[list[str]] = None) -> list[dict[str, Any]]:
        targets = [normalize_symbol(s) for s in (symbols or self._codes)]
        out: list[dict[str, Any]] = []
        for sym in targets:
            if self._df(sym) is None:
                continue
            t = self.get_ticker(sym)
            if t["last"] > 0:
                out.append(t)
        out.sort(key=lambda x: x.get("volume24h", 0), reverse=True)
        return out

    def get_funding_rate(self, symbol: str) -> float:
        del symbol
        return self._funding_rate

    def get_orderbook(self, symbol: str, depth: int = 10) -> dict[str, list]:
        close = float(self.get_ticker(symbol).get("last", 0) or 100.0)
        if close <= 0:
            close = 100.0
        spread = close * 0.0005
        bids = [[round(close - spread * (i + 1), 4), 10.0 * (depth - i)] for i in range(depth)]
        asks = [[round(close + spread * (i + 1), 4), 10.0 * (depth - i)] for i in range(depth)]
        return {"bids": bids, "asks": asks}

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        del reduce_only
        return {
            "order_id": f"bt-{symbol}-{side}",
            "symbol": normalize_symbol(symbol),
            "side": side,
            "type": "MARKET",
            "amount": amount,
            "filled": amount,
            "status": "FILLED",
        }

    def create_stop_loss_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        stop_price: float,
    ) -> dict[str, Any]:
        return {
            "order_id": f"bt-sl-{symbol}",
            "symbol": normalize_symbol(symbol),
            "side": side,
            "amount": amount,
            "stop_price": stop_price,
            "status": "NEW",
        }

    def get_min_qty(self, symbol: str) -> float:
        del symbol
        return self._min_qty

    def is_valid_symbol(self, symbol: str) -> bool:
        return normalize_symbol(symbol) in self._data_map

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def get_account_balance(self) -> dict[str, float]:
        return {"USDT": self._cash}

    def get_available_balance(self) -> dict[str, float]:
        return {"USDT": self._cash}
