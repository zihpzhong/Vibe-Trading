"""A-share market data backends with fallback chain."""

from __future__ import annotations

import logging
import os
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

MOCK_UNIVERSE: list[dict[str, str]] = [
    {"symbol": "600519.SH", "name": "贵州茅台"},
    {"symbol": "000001.SZ", "name": "平安银行"},
    {"symbol": "300750.SZ", "name": "宁德时代"},
    {"symbol": "601318.SH", "name": "中国平安"},
    {"symbol": "000858.SZ", "name": "五粮液"},
]


def normalize_symbol(symbol: str) -> str:
    """Normalize to ``600519.SH`` style."""
    s = symbol.strip().upper()
    if "." in s:
        return s
    if s.startswith(("SH", "SZ", "BJ")):
        code = s[2:]
        prefix = s[:2]
        return f"{code}.{prefix}"
    if s.startswith("6"):
        return f"{s}.SH"
    if s.startswith(("0", "3")):
        return f"{s}.SZ"
    if s.startswith(("4", "8")):
        return f"{s}.BJ"
    return s


def symbol_to_ak_code(symbol: str) -> str:
    return normalize_symbol(symbol).split(".")[0]


def infer_board(symbol: str) -> str:
    code = symbol_to_ak_code(symbol)
    if code.startswith("688"):
        return "科创板"
    if code.startswith("300"):
        return "创业板"
    if code.startswith(("4", "8")):
        return "北交所"
    return "主板"


def limit_pct(board: str, is_st: bool = False) -> float:
    if is_st:
        return 5.0
    if board in ("创业板", "科创板", "北交所"):
        return 20.0
    return 10.0


class DataBackend(ABC):
    @abstractmethod
    def is_available(self) -> bool:
        ...

    @abstractmethod
    def get_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        ...

    @abstractmethod
    def get_realtime(self, symbols: list[str]) -> list[dict[str, Any]]:
        ...

    def get_index(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self.get_daily(index_code, start_date, end_date)

    def get_market_breadth(self) -> dict[str, Any]:
        return {"up": 0, "down": 0, "flat": 0, "ratio": 0.5}

    def get_suspended_list(self) -> set[str]:
        return set()

    def get_st_list(self) -> set[str]:
        return set()

    def get_financials(self, symbol: str) -> dict[str, Any]:
        """Return pe_ttm, pb, eps, revenue_growth, profit_growth or empty dict."""
        return {}

    def get_money_flow(self, symbol: str) -> dict[str, Any]:
        """Return net_main_inflow, net_retail_inflow, total_amount or empty dict."""
        return {}

    def is_limit(self, symbol: str, price: float, prev_close: float, board: str, is_st: bool = False) -> str:
        """Return ``up`` / ``down`` / ``none``."""
        if prev_close <= 0:
            return "none"
        pct = (price - prev_close) / prev_close * 100
        lim = limit_pct(board, is_st) - 0.1
        if pct >= lim:
            return "up"
        if pct <= -lim:
            return "down"
        return "none"


class MockDataBackend(DataBackend):
    def __init__(self, seed: float = 100.0) -> None:
        self._seed = seed
        self._base_prices: dict[str, float] = {
            u["symbol"]: seed * (1 + i * 0.3) for i, u in enumerate(MOCK_UNIVERSE)
        }

    def is_available(self) -> bool:
        return True

    def get_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        sym = normalize_symbol(symbol)
        base = self._base_prices.get(sym, self._seed)
        start = datetime.strptime(start_date[:10], "%Y-%m-%d")
        end = datetime.strptime(end_date[:10], "%Y-%m-%d")
        rows: list[dict[str, Any]] = []
        price = base
        day = start
        while day <= end:
            if day.weekday() < 5:
                chg = random.gauss(0, 0.015)
                open_p = price
                close_p = price * (1 + chg)
                high_p = max(open_p, close_p) * (1 + abs(random.gauss(0, 0.005)))
                low_p = min(open_p, close_p) * (1 - abs(random.gauss(0, 0.005)))
                vol = random.uniform(1e6, 5e7)
                rows.append(
                    {
                        "date": day.strftime("%Y-%m-%d"),
                        "open": open_p,
                        "high": high_p,
                        "low": low_p,
                        "close": close_p,
                        "volume": vol,
                        "amount": vol * close_p,
                    }
                )
                price = close_p
            day += timedelta(days=1)
        return pd.DataFrame(rows)

    def get_realtime(self, symbols: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for sym in symbols:
            sym = normalize_symbol(sym)
            base = self._base_prices.get(sym, self._seed)
            chg = random.uniform(-3, 3)
            last = base * (1 + chg / 100)
            out.append(
                {
                    "symbol": sym,
                    "name": next((u["name"] for u in MOCK_UNIVERSE if u["symbol"] == sym), sym),
                    "last": last,
                    "prev_close": base,
                    "volume": random.uniform(5e6, 8e7),
                    "amount": random.uniform(5e7, 5e8),
                    "turnover_rate": random.uniform(0.5, 5.0),
                    "change_pct": chg,
                    "market_cap": round(base * random.uniform(5e8, 2e9), 2),
                }
            )
        return out

    def get_market_breadth(self) -> dict[str, Any]:
        up = random.randint(1200, 2800)
        down = random.randint(800, 2200)
        flat = max(0, 5000 - up - down)
        total = up + down + flat
        return {"up": up, "down": down, "flat": flat, "ratio": up / total if total else 0.5}

    def get_financials(self, symbol: str) -> dict[str, Any]:
        return {"pe_ttm": random.uniform(10, 40), "pb": random.uniform(1, 6), "eps": random.uniform(0.5, 5)}

    def get_money_flow(self, symbol: str) -> dict[str, Any]:
        return {"net_main_inflow": random.uniform(-5e7, 5e7), "net_retail_inflow": random.uniform(-2e7, 2e7)}


class AkshareDataBackend(DataBackend):
    def is_available(self) -> bool:
        try:
            import akshare  # noqa: F401

            return True
        except ImportError:
            return False

    def get_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        import akshare as ak

        code = symbol_to_ak_code(symbol)
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start_date.replace("-", "")[:8],
            end_date=end_date.replace("-", "")[:8],
            adjust="qfq",
        )
        if df is None or df.empty:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "date": pd.to_datetime(df["日期"]).dt.strftime("%Y-%m-%d"),
                "open": df["开盘"].astype(float),
                "high": df["最高"].astype(float),
                "low": df["最低"].astype(float),
                "close": df["收盘"].astype(float),
                "volume": df["成交量"].astype(float),
                "amount": df["成交额"].astype(float),
            }
        )

    def get_realtime(self, symbols: list[str]) -> list[dict[str, Any]]:
        import akshare as ak

        try:
            spot = ak.stock_zh_a_spot_em()
        except Exception as exc:
            logger.warning("akshare spot failed: %s", exc)
            return []
        if spot is None or spot.empty:
            return []
        code_set = {symbol_to_ak_code(s) for s in symbols}
        out: list[dict[str, Any]] = []
        for _, row in spot.iterrows():
            code = str(row.get("代码", ""))
            if code not in code_set:
                continue
            suffix = "SH" if code.startswith("6") else "SZ"
            sym = f"{code}.{suffix}"
            last = float(row.get("最新价", 0) or 0)
            prev = float(row.get("昨收", last) or last)
            out.append(
                {
                    "symbol": sym,
                    "name": str(row.get("名称", sym)),
                    "last": last,
                    "prev_close": prev,
                    "volume": float(row.get("成交量", 0) or 0),
                    "amount": float(row.get("成交额", 0) or 0),
                    "turnover_rate": float(row.get("换手率", 0) or 0),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "market_cap": float(row.get("总市值", 0) or 0),
                }
            )
        return out

    def get_financials(self, symbol: str) -> dict[str, Any]:
        import akshare as ak

        try:
            code = symbol_to_ak_code(symbol)
            df = ak.stock_financial_abstract_ths(symbol=code, indicator="按年度")
            if df is None or df.empty:
                return {"pe_ttm": 0, "pb": 0, "eps": 0}
            latest = df.iloc[0]
            return {
                "pe_ttm": float(latest.get("市盈率-动态", latest.get("PE", 0)) or 0),
                "pb": float(latest.get("市净率", latest.get("PB", 0)) or 0),
                "eps": float(latest.get("每股收益", latest.get("EPS", 0)) or 0),
            }
        except Exception as exc:
            logger.debug("akshare financials failed for %s: %s", symbol, exc)
            return {}

    def get_money_flow(self, symbol: str) -> dict[str, Any]:
        import akshare as ak

        try:
            code = symbol_to_ak_code(symbol)
            df = ak.stock_individual_fund_flow(stock=code, market="sh" if code.startswith("6") else "sz")
            if df is None or df.empty:
                return {}
            latest = df.iloc[-1]
            return {
                "net_main_inflow": float(latest.get("主力净流入-净额", 0) or 0),
                "net_retail_inflow": float(latest.get("小单净流入-净额", 0) or 0),
            }
        except Exception as exc:
            logger.debug("akshare money_flow failed for %s: %s", symbol, exc)
            return {}


class TushareDataBackend(DataBackend):
    def __init__(self, token: Optional[str] = None) -> None:
        self._token = token or os.environ.get("TUSHARE_TOKEN", "")

    def is_available(self) -> bool:
        if not self._token:
            return False
        try:
            import tushare  # noqa: F401

            return True
        except ImportError:
            return False

    def _api(self):
        import tushare as ts

        return ts.pro_api(self._token)

    def get_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        sym = normalize_symbol(symbol)
        api = self._api()
        df = api.daily(
            ts_code=sym,
            start_date=start_date.replace("-", "")[:8],
            end_date=end_date.replace("-", "")[:8],
        )
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date")
        return pd.DataFrame(
            {
                "date": pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d"),
                "open": df["open"].astype(float),
                "high": df["high"].astype(float),
                "low": df["low"].astype(float),
                "close": df["close"].astype(float),
                "volume": df["vol"].astype(float) * 100,
                "amount": df["amount"].astype(float) * 1000,
            }
        )

    def get_realtime(self, symbols: list[str]) -> list[dict[str, Any]]:
        return []

    def get_financials(self, symbol: str) -> dict[str, Any]:
        try:
            sym = normalize_symbol(symbol)
            api = self._api()
            df = api.fina_indicator(ts_code=sym, limit=1)
            if df is None or df.empty:
                return {}
            row = df.iloc[0]
            return {
                "pe_ttm": float(row.get("pe", 0) or 0),
                "pb": float(row.get("pb", 0) or 0),
                "eps": float(row.get("eps", 0) or 0),
            }
        except Exception as exc:
            logger.debug("tushare financials failed for %s: %s", symbol, exc)
            return {}

    def get_money_flow(self, symbol: str) -> dict[str, Any]:
        try:
            sym = normalize_symbol(symbol)
            api = self._api()
            df = api.moneyflow(ts_code=sym, limit=1)
            if df is None or df.empty:
                return {}
            row = df.iloc[0]
            return {
                "net_main_inflow": float(row.get("net_amount", 0) or 0),
                "net_retail_inflow": float(row.get("buy_sm_vol", 0) or 0),
            }
        except Exception as exc:
            logger.debug("tushare money_flow failed for %s: %s", symbol, exc)
            return {}


class PytdxDataBackend(DataBackend):
    """通达信数据接口 (pytdx) — last resort before mock."""

    _HOSTS = ["119.147.212.81", "119.147.212.113", "218.75.126.72", "180.153.18.170"]
    _PORT = 7709

    def __init__(self) -> None:
        self._api: Any = None

    def is_available(self) -> bool:
        try:
            import pytdx  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_api(self):
        if self._api is not None:
            return self._api
        from pytdx.hq import TdxHq_API

        for host in self._HOSTS:
            try:
                api = TdxHq_API()
                api.connect(host, self._PORT)
                self._api = api
                return api
            except Exception:
                continue
        return None

    def _code(self, symbol: str) -> tuple[str, int]:
        sym = normalize_symbol(symbol)
        code = sym.split(".")[0]
        market = 1 if sym.endswith(".SH") else 0
        return code, market

    def get_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        api = self._get_api()
        if api is None:
            return pd.DataFrame()
        try:
            code, market = self._code(symbol)
            count = 120
            result = api.get_security_bars(9, market, code, 0, count)
            if not result:
                return pd.DataFrame()
            df = pd.DataFrame(result)
            df["date"] = pd.to_datetime(
                df["year"].astype(str) + df["month"].astype(str).str.zfill(2) + df["day"].astype(str).str.zfill(2)
            ).dt.strftime("%Y-%m-%d")
            return pd.DataFrame(
                {
                    "date": df["date"],
                    "open": df["open"].astype(float),
                    "high": df["high"].astype(float),
                    "low": df["low"].astype(float),
                    "close": df["close"].astype(float),
                    "volume": df["vol"].astype(float),
                    "amount": df["amount"].astype(float),
                }
            )
        except Exception as exc:
            logger.debug("pytdx daily failed for %s: %s", symbol, exc)
            return pd.DataFrame()

    def get_realtime(self, symbols: list[str]) -> list[dict[str, Any]]:
        api = self._get_api()
        if api is None:
            return []
        out: list[dict[str, Any]] = []
        for sym in symbols:
            try:
                code, market = self._code(sym)
                quote = api.get_security_quotes([(market, code)])
                if not quote:
                    continue
                q = quote[0]
                last = float(q.get("price", 0))
                prev = float(q.get("last_close", last))
                out.append(
                    {
                        "symbol": normalize_symbol(sym),
                        "name": sym,
                        "last": last,
                        "prev_close": prev,
                        "volume": float(q.get("vol", 0)),
                        "amount": float(q.get("amount", 0)),
                        "change_pct": ((last / prev - 1) * 100) if prev > 0 else 0.0,
                    }
                )
            except Exception as exc:
                logger.debug("pytdx quote failed for %s: %s", sym, exc)
        return out


class DataChain:
    """Try backends in order until one succeeds."""

    def __init__(self, sources: Optional[list[str]] = None) -> None:
        order = sources or ["akshare", "tushare", "pytdx", "mock"]
        self._backends: list[DataBackend] = []
        for name in order:
            if name == "akshare":
                self._backends.append(AkshareDataBackend())
            elif name == "tushare":
                self._backends.append(TushareDataBackend())
            elif name == "pytdx":
                self._backends.append(PytdxDataBackend())
            elif name == "mock":
                self._backends.append(MockDataBackend())
        if not any(b.is_available() for b in self._backends):
            self._backends.append(MockDataBackend())

    def _pick(self) -> DataBackend:
        for b in self._backends:
            if b.is_available():
                return b
        return MockDataBackend()

    def get_daily(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        for b in self._backends:
            if not b.is_available():
                continue
            try:
                df = b.get_daily(symbol, start_date, end_date)
                if df is not None and not df.empty:
                    return df
            except Exception as exc:
                logger.debug("data %s daily failed for %s: %s", type(b).__name__, symbol, exc)
        return pd.DataFrame()

    def get_realtime(self, symbols: list[str]) -> list[dict[str, Any]]:
        for b in self._backends:
            if not b.is_available():
                continue
            try:
                rows = b.get_realtime(symbols)
                if rows:
                    return rows
            except Exception as exc:
                logger.debug("data realtime failed: %s", exc)
        return MockDataBackend().get_realtime(symbols)

    def get_market_breadth(self) -> dict[str, Any]:
        return self._pick().get_market_breadth()

    def get_index(self, index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        return self.get_daily(index_code, start_date, end_date)

    def is_limit(self, symbol: str, price: float, prev_close: float, board: str, is_st: bool = False) -> str:
        return self._pick().is_limit(symbol, price, prev_close, board, is_st)

    def get_financials(self, symbol: str) -> dict[str, Any]:
        return self._pick().get_financials(symbol)

    def get_money_flow(self, symbol: str) -> dict[str, Any]:
        return self._pick().get_money_flow(symbol)
