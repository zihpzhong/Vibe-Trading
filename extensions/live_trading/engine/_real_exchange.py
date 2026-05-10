"""Binance exchange adapter via ccxt.

Implements ExchangeBase for live market data and order execution.
Filename starts with underscore to match the lazy import in exchange.py:135.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Optional
from urllib.parse import urlencode

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry as UrllibRetry

from .exchange import ExchangeBase

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = (1, 2, 4)

_BINANCE_TOP20_FALLBACK = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "MATICUSDT", "SHIBUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT",
    "ETCUSDT", "XLMUSDT", "FILUSDT", "TRXUSDT", "NEARUSDT",
]


def _to_internal_symbol(ccxt_symbol: str) -> str:
    """Convert ccxt symbol format to internal short format.

    Spot:  BTC/USDT → BTCUSDT
    Futures: BTC/USDT:USDT → BTCUSDT
    """
    base, _, quote = ccxt_symbol.partition("/")
    if ":" in quote:
        quote, _, _ = quote.partition(":")
    return f"{base}{quote}"


def _ccxt_ticker_to_internal(raw: dict) -> dict[str, Any]:
    """Map Binance raw ticker (or ccxt unified ticker) to internal format.

    Works for both raw Binance API responses and ccxt unified tickers.
    Fields differ slightly between the two formats:
      - Binance raw: lastPrice, quoteVolume, priceChangePercent
      - ccxt unified: last, quoteVolume, percentage
    """
    # Binance raw API format (from /api/v3/ticker/24hr)
    sym = raw.get("symbol", "")
    result: dict[str, Any] = {"symbol": _to_internal_symbol(sym) if sym else ""}
    vol = raw.get("quoteVolume") or raw.get("volume24h", 0)
    result["volume24h"] = float(vol) if vol is not None else 0.0
    result["last"] = float(raw.get("lastPrice") or raw.get("last", 0))
    result["open24h"] = float(raw.get("openPrice") or raw.get("open", 0))
    result["high24h"] = float(raw.get("highPrice") or raw.get("high", 0))
    result["low24h"] = float(raw.get("lowPrice") or raw.get("low", 0))
    result["change24h"] = float(raw.get("priceChangePercent") or raw.get("percentage", 0))
    return result


def _retry(op_name: str, fn, *args: Any, **kwargs: Any) -> Any:
    """Call fn with exponential backoff retry."""
    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_err = exc
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BACKOFF[attempt]
                logger.warning(
                    "%s attempt %d/%d failed (%.1fs retry): %s",
                    op_name, attempt + 1, MAX_RETRIES, delay, exc,
                )
                time.sleep(delay)
    raise RuntimeError(f"{op_name} failed after {MAX_RETRIES} attempts") from last_err


class RealExchange(ExchangeBase):
    """Live Binance exchange via ccxt.

    API keys read from env:
      - BINANCE_API_KEY
      - BINANCE_SECRET
      - BINANCE_TESTNET=true  (optional, defaults to false)
      - BINANCE_MARKET_TYPE=spot|future  (optional, defaults to spot)

    Without API keys, market-data methods still work (public endpoints).
    Trading methods raise RuntimeError with a clear message.
    """

    def __init__(self) -> None:
        import ccxt  # lazy import so MockExchange works without ccxt

        testnet = os.environ.get("BINANCE_TESTNET", "").lower() == "true"
        api_key = os.environ.get("BINANCE_API_KEY", "")
        secret = os.environ.get("BINANCE_SECRET", "")
        market_type = os.environ.get("BINANCE_MARKET_TYPE", "spot").lower()

        self._testnet = testnet
        self._has_auth = bool(api_key and secret)
        self._market_type = market_type
        self._lock = threading.RLock()

        exchange_config: dict[str, Any] = {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": market_type,
                "fetchCurrencies": False,
                "loadMarkets": False,  # prevent auto-load on first API call
            },
        }

        self._ccxt = ccxt.binance(exchange_config)
        self._ccxt.markets = {}
        self._ccxt.markets_by_id = {}

        # Shared HTTP session with connection pooling + retry for Binance API
        self._session = requests.Session()
        retry_strategy = UrllibRetry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=retry_strategy,
        )
        self._session.mount("https://api.binance.com", adapter)
        self._session.mount("https://fapi.binance.com", adapter)

        # Pre-fetch LOT_SIZE stepSizes from exchangeInfo for quantity rounding
        self._step_sizes: dict[str, float] = {}
        # Also store min notional for reference
        self._min_notionals: dict[str, float] = {}
        # Valid trading symbols (futures or spot depending on market_type)
        self._valid_symbols: set[str] = set()
        self._load_exchange_info()

    # ------------------------------------------------------------------
    # ExchangeBase — market data (public, no auth required)
    # ------------------------------------------------------------------

    def _get_kline_urls(self) -> tuple[str, str]:
        """Return (base_url, endpoint) for kline fetching.

        Both routes use api.binance.com (proxies fapi too — fapi.binance.com has SSL issues in Docker).
        Falls back to spot api if fapi is unreachable.
        """
        if self._market_type == "future":
            return ("https://api.binance.com", "/fapi/v1/klines")
        return ("https://api.binance.com", "/api/v3/klines")

    def get_kline(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> pd.DataFrame:
        """Fetch OHLCV candles from Binance.

        Returns DataFrame with columns: timestamp (index), open, high, low, close, volume.
        Falls back to spot kline if futures endpoint is unreachable.
        """
        with self._lock:
            base_url, endpoint = self._get_kline_urls()
        # Map timeframe to Binance format (ccxt uses same format for spot and futures)
        tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
        tf = tf_map.get(timeframe, timeframe)

        def _fetch(url: str, ep: str) -> list:
            resp = _retry(f"klines({symbol},{tf},{limit})", lambda: self._session.get(
                f"{url}{ep}",
                params={"symbol": symbol, "interval": tf, "limit": limit},
                timeout=10,
            ))
            if resp.status_code == 404:
                raise RuntimeError(f"{url}{ep} not reachable (404)")
            resp.raise_for_status()
            return resp.json()

        try:
            raw = _fetch(base_url, endpoint)
        except Exception as exc:
            # Futures fapi blocked — try spot fallback
            if self._market_type == "future" and "not reachable" in str(exc):
                raw = _fetch("https://api.binance.com", "/api/v3/klines")
            else:
                raise

        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume",
                                          "close_time", "quote_volume", "trades", "taker_buy_base",
                                          "taker_buy_quote", "ignore"])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df.set_index("timestamp", inplace=True)
        # Keep only the 6 standard columns
        return df[["open", "high", "low", "close", "volume"]]

    @property
    def _data_prefix(self) -> str:
        """Market data prefix — always api.binance.com/api/v3 (spot).

        Futures market data is read from spot endpoints for volume/price screening.
        Klines use a separate endpoint (api.binance.com/fapi/v1/klines works).
        """
        return "https://api.binance.com/api/v3"

    @property
    def _trade_prefix(self) -> str:
        """Trading prefix — fapi.binance.com/fapi/v1 for futures, api for spot.

        Trading (orders, leverage, positions) requires the correct backend.
        """
        if self._market_type == "future":
            return "https://fapi.binance.com/fapi/v1"
        return "https://api.binance.com/api/v3"

    def _api_url(self) -> str:
        """Return base Binance API URL for spot/universal endpoints."""
        return "https://api.binance.com"

    def _fapi_url(self) -> str:
        """Return base Binance Futures API URL for fapi endpoints."""
        return "https://fapi.binance.com"

    def _api_url(self) -> str:
        """Return base Binance API URL for spot/universal endpoints."""
        return "https://api.binance.com"

    def _fapi_url(self) -> str:
        """Return base Binance Futures API URL for fapi endpoints."""
        return "https://fapi.binance.com"

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch current ticker using direct HTTP (avoids ccxt load_markets)."""
        with self._lock:
            resp = _retry(f"ticker({symbol})", lambda: self._session.get(
                f"{self._data_prefix}/ticker/24hr",
                params={"symbol": symbol},
                timeout=10,
            ))
            resp.raise_for_status()
            raw = resp.json()
            return _ccxt_ticker_to_internal({
                "symbol": raw.get("symbol", symbol),
                "last": raw.get("lastPrice", 0),
                "open": raw.get("openPrice", 0),
                "high": raw.get("highPrice", 0),
                "low": raw.get("lowPrice", 0),
                "quoteVolume": raw.get("quoteVolume", 0),
                "percentage": raw.get("priceChangePercent", 0),
            })

    def get_tickers(self, symbols: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Batch fetch all USDT ticker data via direct HTTP."""
        with self._lock:
            resp = _retry("all_tickers", lambda: self._session.get(
                f"{self._data_prefix}/ticker/24hr",
                timeout=10,
            ))
            resp.raise_for_status()
            raw_all = resp.json()
            tickers: list[dict[str, Any]] = []
            for raw in raw_all:
                sym = raw.get("symbol", "")
                if not sym.endswith("USDT") or ":USDT" in sym:
                    continue
                # In futures mode, only return symbols that exist on Binance Futures
                if self._market_type == "future" and self._valid_symbols and sym not in self._valid_symbols:
                    logger.debug("  Filter(合约不可用): %s not on futures, skipped", sym)
                    continue
                if symbols and sym not in symbols:
                    continue
                t = _ccxt_ticker_to_internal(raw)
                if t["volume24h"] > 0:
                    tickers.append(t)
            tickers.sort(key=lambda x: x["volume24h"], reverse=True)
            return tickers

    def get_funding_rate(self, symbol: str) -> float:
        """Fetch current perpetual funding rate via direct HTTP."""
        with self._lock:
            resp = _retry(f"funding({symbol})", lambda: self._session.get(
                f"{self._api_url()}/fapi/v1/premiumIndex",
                params={"symbol": symbol},
                timeout=10,
            ))
            if resp.status_code == 404:
                logger.warning("Funding rate unavailable for %s (not a futures pair?), returning 0.0", symbol)
                return 0.0
            resp.raise_for_status()
            raw = resp.json()
            fr = raw.get("lastFundingRate")
            return float(fr) if fr is not None else 0.0

    def get_orderbook(self, symbol: str, depth: int = 10) -> dict[str, list]:
        """Fetch order book via direct HTTP."""
        with self._lock:
            resp = _retry(f"orderbook({symbol},{depth})", lambda: self._session.get(
                f"{self._data_prefix}/depth",
                params={"symbol": symbol, "limit": depth},
                timeout=10,
            ))
            if resp.status_code == 404:
                return {"bids": [], "asks": []}
            resp.raise_for_status()
            raw = resp.json()
            return {"bids": raw.get("bids", []), "asks": raw.get("asks", [])}

    # ------------------------------------------------------------------
    # Trading — requires API key
    # ------------------------------------------------------------------

    def _require_auth(self, op: str) -> None:
        if not self._has_auth:
            raise RuntimeError(
                f"{op} requires BINANCE_API_KEY and BINANCE_SECRET env vars. "
                f"Set BINANCE_TESTNET=true for testnet."
            )

    def _trade_request(self, endpoint: str, params: dict) -> dict:
        """Send a signed POST request and parse the response.

        Endpoint is auto-mapped to the correct base prefix:
        /api/v3/order + spot   → https://api.binance.com/api/v3/order
        /api/v3/order + future → https://fapi.binance.com/fapi/v1/order
        """
        self._require_auth("trade")
        action = endpoint.rsplit("/", 1)[-1]  # /api/v3/order → "order"
        url = f"{self._trade_prefix}/{action}"
        data = {"timestamp": int(time.time() * 1000), "recvWindow": 10000}
        data.update(params)
        query_string = urlencode(sorted(data.items()))
        signature = hmac.new(
            self._ccxt.secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query_string += f"&signature={signature}"
        resp = requests.post(
            url,
            data=query_string,
            headers={"X-MBX-APIKEY": self._ccxt.apiKey},
            timeout=10,
        )
        if resp.status_code == 401:
            raise RuntimeError(f"Trade auth failed: {resp.text}")
        if not resp.ok:
            raise RuntimeError(f"Trade failed ({resp.status_code}): {resp.text}")
        raw = resp.json()
        if isinstance(raw, dict) and "code" in raw and raw["code"] < 0:
            raise RuntimeError(f"Binance error {raw['code']}: {raw.get('msg', '')}")
        return raw

    @staticmethod
    def _binance_side(side: str) -> str:
        """Map internal direction (LONG/SHORT/long/short/sell/buy) to Binance side (BUY/SELL).

        Spot mapping: LONG → BUY, SHORT → SELL.
        """
        mapping = {"LONG": "BUY", "SHORT": "SELL", "BUY": "BUY", "SELL": "SELL",
                   "long": "BUY", "short": "SELL", "buy": "BUY", "sell": "SELL"}
        return mapping.get(side, side.upper())

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        """Place a market order via direct HTTP.

        Args:
            side: Internal direction — "long"/"short"/"buy"/"sell" or "LONG"/"SHORT".
        """
        qty = self._round_qty(symbol, amount)
        with self._lock:
            raw = _retry(f"market({symbol},{side},{qty})", self._trade_request, "/api/v3/order", {
                "symbol": symbol, "side": self._binance_side(side), "type": "MARKET",
                "quantity": str(qty),
            })
            return {
                "order_id": raw.get("orderId"),
                "symbol": raw.get("symbol"),
                "side": raw.get("side"),
                "type": raw.get("type"),
                "amount": amount,
                "filled": float(raw.get("executedQty", 0)),
                "status": raw.get("status"),
            }

    def create_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        """Place a limit order via direct HTTP."""
        qty = self._round_qty(symbol, amount)
        with self._lock:
            raw = _retry(f"limit({symbol},{side},{qty},{price})", self._trade_request, "/api/v3/order", {
                "symbol": symbol, "side": self._binance_side(side), "type": "LIMIT",
                "quantity": str(qty), "price": str(price), "timeInForce": "GTC",
            })
            return {
                "order_id": raw.get("orderId"),
                "symbol": raw.get("symbol"),
                "side": raw.get("side"),
                "type": raw.get("type"),
                "amount": amount,
                "price": price,
                "filled": float(raw.get("executedQty", 0)),
                "status": raw.get("status"),
            }

    def create_stop_loss_order(self, symbol: str, side: str, amount: float, stop_price: float) -> dict:
        """Place a stop-loss (market on trigger) order via direct HTTP.

        Spot: STOP_LOSS type. Futures: STOP_MARKET type.
        Triggers a market order when stopPrice is reached.
        """
        qty = self._round_qty(symbol, amount)
        order_type = "STOP_MARKET" if self._market_type == "future" else "STOP_LOSS"
        with self._lock:
            raw = _retry(f"stop_loss({symbol},{side},{qty},{stop_price})", self._trade_request, "/api/v3/order", {
                "symbol": symbol, "side": self._binance_side(side), "type": order_type,
                "quantity": str(qty), "stopPrice": str(stop_price),
            })
            return {
                "order_id": raw.get("orderId"),
                "symbol": raw.get("symbol"),
                "side": raw.get("side"),
                "type": raw.get("type"),
                "amount": amount,
                "stop_price": stop_price,
                "filled": float(raw.get("executedQty", 0)),
                "status": raw.get("status"),
            }

    def create_take_profit_order(self, symbol: str, side: str, amount: float, tp_price: float) -> dict:
        """Place a take-profit (market on trigger) order via direct HTTP.

        Spot: TAKE_PROFIT type. Futures: TAKE_PROFIT_MARKET type.
        Triggers a market order when stopPrice is reached.
        """
        qty = self._round_qty(symbol, amount)
        order_type = "TAKE_PROFIT_MARKET" if self._market_type == "future" else "TAKE_PROFIT"
        with self._lock:
            raw = _retry(f"take_profit({symbol},{side},{qty},{tp_price})", self._trade_request, "/api/v3/order", {
                "symbol": symbol, "side": self._binance_side(side), "type": order_type,
                "quantity": str(qty), "stopPrice": str(tp_price),
            })
            return {
                "order_id": raw.get("orderId"),
                "symbol": raw.get("symbol"),
                "side": raw.get("side"),
                "type": raw.get("type"),
                "amount": amount,
                "tp_price": tp_price,
                "filled": float(raw.get("executedQty", 0)),
                "status": raw.get("status"),
            }

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open order via direct HTTP DELETE."""
        with self._lock:
            self._require_auth("cancel_order")
            query_string = urlencode(sorted({
                "symbol": symbol,
                "orderId": order_id,
                "timestamp": int(time.time() * 1000),
                "recvWindow": 10000,
            }.items()))
            signature = hmac.new(
                self._ccxt.secret.encode("utf-8"),
                query_string.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            resp = requests.delete(
                f"{self._trade_prefix}/order?{query_string}&signature={signature}",
                headers={"X-MBX-APIKEY": self._ccxt.apiKey},
                timeout=10,
            )
            if resp.status_code == 401:
                raise RuntimeError(f"Cancel order auth failed: {resp.text}")
            resp.raise_for_status()
            raw = resp.json()
            return {"order_id": raw.get("orderId", order_id), "status": raw.get("status")}

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        """Query order status via direct HTTP GET."""
        with self._lock:
            self._require_auth("fetch_order")
            resp = self._signed_request(self._trade_prefix, "/order", {
                "symbol": symbol, "orderId": order_id,
            })
            resp.raise_for_status()
            raw = resp.json()
            return {
                "order_id": raw.get("orderId", order_id),
                "symbol": symbol,
                "filled": float(raw.get("executedQty", 0)),
                "status": raw.get("status"),
                "remaining": float(raw.get("origQty", 0)) - float(raw.get("executedQty", 0)),
            }

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def _signed_request(self, base_url: str, endpoint: str, params: dict | None = None) -> requests.Response:
        """Make a signed GET request to Binance API."""
        req_params = {"timestamp": int(time.time() * 1000), "recvWindow": 10000}
        if params:
            req_params.update(params)
        query_string = urlencode(sorted(req_params.items()))
        signature = hmac.new(
            self._ccxt.secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query_string += f"&signature={signature}"
        return requests.get(
            f"{base_url}{endpoint}?{query_string}",
            headers={"X-MBX-APIKEY": self._ccxt.apiKey},
            timeout=10,
        )

    def get_account_balance(self) -> dict[str, float]:
        """Fetch spot or futures account balances via direct HTTP."""
        with self._lock:
            if not self._has_auth:
                return {}
            if self._market_type == "future":
                try:
                    resp = self._signed_request(self._fapi_url(), "/fapi/v2/balance")
                    if resp.ok:
                        result: dict[str, float] = {}
                        for entry in (resp.json() if isinstance(resp.json(), list) else []):
                            asset = entry.get("asset", "")
                            balance = float(entry.get("balance", 0))
                            if balance > 0:
                                result[asset] = balance
                        return result
                    logger.warning("Futures balance fetch failed (%d): %s", resp.status_code, resp.text[:200])
                except Exception as exc:
                    logger.warning("Futures balance fetch unavailable: %s", exc)
                return {}
            # Spot: use /api/v3/account
            resp = self._signed_request(self._api_url(), "/api/v3/account")
            if not resp.ok:
                if resp.status_code == 401:
                    logger.warning("get_account_balance auth failed: %s", resp.text)
                else:
                    logger.warning("get_account_balance error %d: %s", resp.status_code, resp.text[:200])
                return {}
            raw = resp.json()
            result: dict[str, float] = {}
            for entry in raw.get("balances", []):
                free = float(entry.get("free", 0))
                if free > 0:
                    result[entry.get("asset", "")] = free
            return result

    def get_positions(self) -> list[dict[str, Any]]:
        """Fetch current positions. Spot: non-zero balances. Futures: open positions."""
        with self._lock:
            if not self._has_auth:
                return []
            if self._market_type == "future":
                resp = self._signed_request(self._fapi_url(), "/fapi/v2/positionRisk")
                if resp.ok:
                    positions: list[dict[str, Any]] = []
                    for p in (resp.json() if isinstance(resp.json(), list) else []):
                        amt = float(p.get("positionAmt", 0))
                        if amt != 0:
                            positions.append({
                                "symbol": p.get("symbol", ""),
                                "direction": "LONG" if amt > 0 else "SHORT",
                                "entry_price": float(p.get("entryPrice", 0)),
                                "quantity": abs(amt),
                                "unrealized_pnl": float(p.get("unrealizedProfit", 0)),
                            })
                    return positions
                logger.warning("Futures position risk fetch failed (%d): %s",
                              resp.status_code, resp.text[:200])
                return []
            # Spot: non-zero balances as positions
            balances = self.get_account_balance()
            positions: list[dict[str, Any]] = []
            for asset, amount in balances.items():
                if asset == "USDT":
                    continue
                ticker_sym = f"{asset}USDT"
                try:
                    t = self.get_ticker(ticker_sym)
                    price = t["last"]
                except Exception:
                    price = 0.0
                positions.append({
                    "symbol": ticker_sym,
                    "asset": asset,
                    "quantity": amount,
                    "current_price": price,
                    "value_usdt": amount * price,
                })
            return positions

    # ------------------------------------------------------------------
    # LOT_SIZE / quantity rounding
    # ------------------------------------------------------------------

    def _load_exchange_info(self) -> None:
        """Fetch exchangeInfo and cache LOT_SIZE stepSizes for quantity precision.

        For futures: uses /fapi/v1/exchangeInfo (futures-specific).
        For spot: uses /api/v3/exchangeInfo.
        Falls back silently — without stepSizes, quantity rounding is skipped.
        """
        try:
            if self._market_type == "future":
                url = f"{self._fapi_url()}/fapi/v1/exchangeInfo"
            else:
                url = f"{self._api_url()}/api/v3/exchangeInfo"
            resp = self._session.get(url, timeout=10)
            if not resp.ok:
                logger.warning("exchangeInfo fetch failed (%d), skipping LOT_SIZE cache", resp.status_code)
                return
            info = resp.json()
            for s in info.get("symbols", []):
                sym = s.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                self._valid_symbols.add(sym)
                for f in s.get("filters", []):
                    if f.get("filterType") == "LOT_SIZE":
                        step = float(f.get("stepSize", 1))
                        self._step_sizes[sym] = step
                        break
        except Exception as exc:
            logger.warning("Failed to load exchangeInfo for LOT_SIZE: %s", exc)

    def _round_qty(self, symbol: str, qty: float) -> float:
        """Round quantity to the symbol's LOT_SIZE stepSize.

        Uses cached stepSize from exchangeInfo.
        Falls back to rounding to 6 decimal places if stepSize unknown.
        """
        step = self._step_sizes.get(symbol)
        if step is None or step <= 0:
            return round(qty, 6)
        # Use Decimal for precise step rounding (avoids float 24.400000000000002 bugs)
        step_d = Decimal(str(step))
        qty_d = Decimal(str(qty))
        rounded = (qty_d / step_d).to_integral_value(rounding=ROUND_FLOOR) * step_d
        return float(rounded)

    def _fapi_post(self, path: str, params: dict) -> dict:
        """Send a signed POST to fapi.binance.com with full path.

        Unlike _trade_request which strips the endpoint to a single segment,
        this preserves the full path (e.g. /fapi/v1/positionSide/dual).
        """
        self._require_auth("fapi_post")
        url = f"{self._fapi_url()}{path}"
        data = {"timestamp": int(time.time() * 1000), "recvWindow": 10000}
        data.update(params)
        query_string = urlencode(sorted(data.items()))
        signature = hmac.new(
            self._ccxt.secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query_string += f"&signature={signature}"
        resp = requests.post(
            url,
            data=query_string,
            headers={"X-MBX-APIKEY": self._ccxt.apiKey},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
        body = resp.text[:200]
        raise RuntimeError(f"FAPI POST {path} failed ({resp.status_code}): {body}")

    def set_margin_mode(self, symbol: str, mode: str = "ISOLATED") -> None:
        """Set margin type for a symbol: ISOLATED (逐仓) or CROSSED (全仓).

        Only works when there are no open positions for the symbol.
        Binance error code -4058 = position exists, which is ignored.
        """
        if self._market_type != "future":
            return
        try:
            self._fapi_post("/fapi/v1/marginType", {"symbol": symbol, "marginType": mode.upper()})
            logger.info("Margin mode set to %s for %s", mode.upper(), symbol)
        except RuntimeError as exc:
            if "-4058" in str(exc) or "-4046" in str(exc):
                logger.info("Margin mode already %s for %s (or position exists)", mode.upper(), symbol)
            else:
                logger.warning("Failed to set margin mode for %s: %s", symbol, exc)

    def set_position_mode(self, dual: bool = False) -> None:
        """Set position mode: dual=False = 单向持仓, dual=True = 双向持仓.

        Global setting — requires no open positions or orders.
        Error code -4059 = mode already set, ignored.
        """
        if self._market_type != "future":
            return
        try:
            val = "true" if dual else "false"
            self._fapi_post("/fapi/v1/positionSide/dual", {"dualSidePosition": val})
            logger.info("Position mode set to %s", "双向" if dual else "单向")
        except RuntimeError as exc:
            if "-4059" in str(exc) or "-4046" in str(exc):
                logger.info("Position mode already set to %s", "双向" if dual else "单向")
            else:
                logger.warning("Failed to set position mode: %s", exc)

    def set_leverage(self, symbol: str, leverage: int = 5) -> None:
        """Set futures leverage for a symbol via POST /fapi/v1/leverage."""
        if self._market_type != "future":
            return
        try:
            self._trade_request("/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})
            logger.info("Leverage set to %dx for %s", leverage, symbol)
        except RuntimeError as exc:
            # Code -4046 means already set to this value — not an error
            if "-4046" not in str(exc):
                logger.warning("Failed to set leverage for %s: %s", symbol, exc)

    def is_valid_symbol(self, symbol: str) -> bool:
        """Check if a symbol is tradeable on the current exchange.

        For futures mode, checks against cached exchangeInfo.
        For spot mode, always returns True (all USDT pairs from ticker are valid).
        """
        if self._market_type != "future" or not self._valid_symbols:
            return True
        return symbol in self._valid_symbols

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def has_auth(self) -> bool:
        return self._has_auth

    @property
    def is_testnet(self) -> bool:
        return self._testnet
