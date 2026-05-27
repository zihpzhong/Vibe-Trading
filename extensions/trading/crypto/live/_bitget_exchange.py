"""Bitget exchange adapter via ccxt.

Implements ExchangeBase for live market data and order execution.
Uses ccxt.bitget() instead of direct HTTP (unlike Binance's RealExchange)
because Bitget's API signature scheme (API key + secret + passphrase)
and REST endpoints differ significantly from Binance.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

import pandas as pd

from .exchange import ExchangeBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol format conversion
# ---------------------------------------------------------------------------


def _ccxt_symbol(symbol: str) -> str:
    """Convert internal short format to ccxt Bitget swap format.

    BTCUSDT → BTC/USDT:USDT
    ETHUSD → ETH/USD:USD
    """
    for suffix in ("USDT", "USD", "BTC", "ETH"):
        if symbol.endswith(suffix) and suffix:
            base = symbol[: -len(suffix)]
            if base:
                return f"{base}/{suffix}:{suffix}"
    return symbol


def _internal_symbol(ccxt_sym: str) -> str:
    """Convert ccxt symbol format to internal short format.

    BTC/USDT:USDT → BTCUSDT
    BTC/USDT → BTCUSDT
    """
    base = ccxt_sym.split("/")[0]
    quote = ccxt_sym.split("/")[-1].split(":")[0]
    return f"{base}{quote}"


# ---------------------------------------------------------------------------
# Retry helper (reusable pattern from _real_exchange.py)
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BACKOFF = (1, 2, 4)


def _retry(op_name: str, fn, *args: Any, **kwargs: Any) -> Any:
    """Call fn with exponential backoff retry."""
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_err = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BACKOFF[attempt]
                logger.warning(
                    "%s attempt %d/%d failed (%.1fs retry): %s",
                    op_name, attempt + 1, _MAX_RETRIES, delay, exc,
                )
                time.sleep(delay)
    raise RuntimeError(f"{op_name} failed after {_MAX_RETRIES} attempts") from last_err


# ---------------------------------------------------------------------------
# ccxt error mapping
# ---------------------------------------------------------------------------

def _map_ccxt_error(exc: Exception, context: str = "") -> RuntimeError:
    """Map ccxt exceptions to uniform RuntimeError."""
    import ccxt

    msg = str(exc) or type(exc).__name__
    if isinstance(exc, ccxt.BadSymbol):
        return RuntimeError(f"Bitget 不支持的交易对: {context or msg}")
    if isinstance(exc, ccxt.AuthenticationError):
        return RuntimeError(f"Bitget API 认证失败: {msg}")
    if isinstance(exc, ccxt.InsufficientFunds):
        return RuntimeError(f"Bitget 余额不足: {msg}")
    if isinstance(exc, ccxt.InvalidOrder):
        return RuntimeError(f"Bitget 无效订单参数: {msg}")
    if isinstance(exc, ccxt.RateLimitExceeded):
        return RuntimeError(f"Bitget 速率限制超限: {msg}")
    if isinstance(exc, (ccxt.NetworkError, ccxt.RequestTimeout)):
        return RuntimeError(f"Bitget 网络错误: {msg}")
    return RuntimeError(f"Bitget {context}: {msg}")


def _retry_ccxt(op_name: str, fn, *args: Any, **kwargs: Any) -> Any:
    """Call ccxt method with retry and error mapping."""
    import ccxt

    last_err: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            last_err = exc
            if attempt < _MAX_RETRIES - 1:
                delay = _RETRY_BACKOFF[attempt]
                logger.warning(
                    "%s attempt %d/%d failed (%.1fs retry): %s",
                    op_name, attempt + 1, _MAX_RETRIES, delay, exc,
                )
                time.sleep(delay)
        except ccxt.RateLimitExceeded as exc:
            last_err = exc
            time.sleep(2)
            continue
        except Exception as exc:
            raise _map_ccxt_error(exc, op_name) from exc
    raise _map_ccxt_error(last_err or RuntimeError("unknown"), op_name)


# ---------------------------------------------------------------------------
# Ticker format conversion
# ---------------------------------------------------------------------------

def _ccxt_ticker_to_internal(raw: dict) -> dict[str, Any]:
    """Map ccxt unified ticker to internal format."""
    sym = raw.get("symbol", "")
    return {
        "symbol": _internal_symbol(sym) if sym else "",
        "last": float(raw.get("last", 0) or 0),
        "open24h": float(raw.get("open", 0) or 0),
        "high24h": float(raw.get("high", 0) or 0),
        "low24h": float(raw.get("low", 0) or 0),
        "volume24h": float(raw.get("quoteVolume", 0) or 0),
        "change24h": float(raw.get("percentage", 0) or 0),
    }


# ---------------------------------------------------------------------------
# BitgetExchange
# ---------------------------------------------------------------------------

class BitgetExchange(ExchangeBase):
    """Live Bitget exchange via ccxt.

    API keys read from env:
      - BITGET_API_KEY
      - BITGET_SECRET
      - BITGET_PASSPHRASE
      - BITGET_TESTNET=true  (optional, defaults to false)

    Uses ccxt.bitget() with defaultType='swap' for perpetual futures.
    Without API keys, market-data methods still work (public endpoints).
    Trading methods raise RuntimeError with a clear message.

    Note: Bitget does NOT support exchange-native bracket orders
    (stop-loss / take-profit conditional orders) matching Binance's
    Algo Order API. Both create_stop_loss_order() and
    create_take_profit_order() raise NotImplementedError, causing
    has_bracket_support() to return False. Software TPSL handles
    all risk management.
    """

    def __init__(self) -> None:
        import ccxt  # lazy import so MockExchange works without ccxt

        api_key = os.environ.get("BITGET_API_KEY", "")
        secret = os.environ.get("BITGET_SECRET", "")
        passphrase = os.environ.get("BITGET_PASSPHRASE", "")
        testnet = os.environ.get("BITGET_TESTNET", "").lower() == "true"

        self._has_auth = bool(api_key and secret and passphrase)
        self._testnet = testnet
        self._lock = threading.RLock()
        self._markets_loaded = False

        exchange_config: dict[str, Any] = {
            "apiKey": api_key,
            "secret": secret,
            "password": passphrase,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",  # 永续合约 / perpetual swap
            },
        }
        try:
            from extensions.backtest.ccxt_helpers import ccxt_proxies_dict

            proxies = ccxt_proxies_dict()
            if proxies:
                exchange_config["proxies"] = proxies
                logger.info("Bitget ccxt proxy enabled")
        except ImportError:
            pass

        self._ccxt = ccxt.bitget(exchange_config)
        self._ccxt.markets = {}

        # Cache for symbol precision
        self._min_qtys: dict[str, float] = {}
        self._tick_sizes: dict[str, float] = {}
        self._step_sizes: dict[str, float] = {}

        # Lazy-load markets on first API call
        self._load_markets()

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def _load_markets(self) -> None:
        """Load markets to cache precision and valid symbols."""
        try:
            self._ccxt.load_markets()
            self._markets_loaded = True
            for ccxt_sym, m in self._ccxt.markets.items():
                sym = _internal_symbol(ccxt_sym)
                limits = m.get("limits", {})
                if "amount" in limits:
                    self._min_qtys[sym] = float(limits["amount"].get("min", 0) or 0)
                if "price" in limits:
                    self._tick_sizes[sym] = float(limits["price"].get("min", 0.01) or 0.01)
                prec = m.get("precision", {})
                if "amount" in prec:
                    self._step_sizes[sym] = float(prec["amount"] or 1)
        except Exception as exc:
            logger.warning("Bitget load_markets failed: %s", exc)

    def _require_markets(self) -> None:
        """Ensure markets are loaded before trading operations."""
        if not self._markets_loaded:
            self._load_markets()

    def get_kline(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> pd.DataFrame:
        """Fetch OHLCV candles via ccxt.

        Returns DataFrame with columns: timestamp (index), open, high, low, close, volume.
        """
        ccxt_sym = _ccxt_symbol(symbol)
        tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
        tf = tf_map.get(timeframe, timeframe)

        def _fetch() -> list:
            return self._ccxt.fetch_ohlcv(ccxt_sym, tf, limit=limit)

        with self._lock:
            raw = _retry_ccxt(f"kline({symbol})", _fetch)

        if not raw:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df.set_index("timestamp", inplace=True)
        return df[["open", "high", "low", "close", "volume"]]

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch current ticker via ccxt."""
        ccxt_sym = _ccxt_symbol(symbol)

        def _fetch() -> dict:
            return self._ccxt.fetch_ticker(ccxt_sym)

        with self._lock:
            raw = _retry_ccxt(f"ticker({symbol})", _fetch)
            return _ccxt_ticker_to_internal(raw)

    def get_tickers(self, symbols: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Batch fetch tickers filtered to USDT pairs, sorted by volume."""
        def _fetch() -> dict:
            return self._ccxt.fetch_tickers()

        with self._lock:
            raw_all = _retry_ccxt("tickers", _fetch)

        tickers: list[dict[str, Any]] = []
        for ccxt_sym, raw in raw_all.items():
            sym = _internal_symbol(ccxt_sym)
            if not sym.endswith("USDT"):
                continue
            if symbols and sym not in symbols:
                continue
            t = _ccxt_ticker_to_internal(raw)
            if t["volume24h"] > 0:
                tickers.append(t)

        tickers.sort(key=lambda x: x["volume24h"], reverse=True)
        return tickers

    def get_funding_rate(self, symbol: str) -> float:
        """Fetch current perpetual funding rate via ccxt."""
        ccxt_sym = _ccxt_symbol(symbol)

        def _fetch() -> dict:
            return self._ccxt.fetch_funding_rate(ccxt_sym)

        with self._lock:
            raw = _retry_ccxt(f"funding({symbol})", _fetch)
            fr = raw.get("lastFundingRate") or raw.get("fundingRate")
            return float(fr) if fr is not None else 0.0

    def get_orderbook(self, symbol: str, depth: int = 10) -> dict[str, list]:
        """Fetch order book via ccxt."""
        ccxt_sym = _ccxt_symbol(symbol)

        def _fetch() -> dict:
            return self._ccxt.fetch_order_book(ccxt_sym, limit=depth)

        with self._lock:
            raw = _retry_ccxt(f"orderbook({symbol})", _fetch)

        return {
            "bids": raw.get("bids", []),
            "asks": raw.get("asks", []),
        }

    # ------------------------------------------------------------------
    # Trading — requires API key + secret + passphrase
    # ------------------------------------------------------------------

    def _require_auth(self, op: str) -> None:
        if not self._has_auth:
            raise RuntimeError(
                f"{op} requires BITGET_API_KEY, BITGET_SECRET, and BITGET_PASSPHRASE env vars. "
                f"Set BITGET_TESTNET=true for testnet."
            )

    def _round_qty(self, symbol: str, qty: float) -> float:
        """Round quantity using ccxt amount_to_precision or step-based rounding."""
        step = self._step_sizes.get(symbol)
        if step and step > 0:
            from decimal import Decimal, ROUND_FLOOR
            step_d = Decimal(str(step))
            qty_d = Decimal(str(qty))
            rounded = (qty_d / step_d).to_integral_value(rounding=ROUND_FLOOR) * step_d
            return float(rounded)
        return round(qty, 6)

    def create_market_order(self, symbol: str, side: str, amount: float, reduce_only: bool = False) -> dict:
        """Place a market order via ccxt."""
        self._require_auth("market_order")
        self._require_markets()
        ccxt_sym = _ccxt_symbol(symbol)
        ccxt_side = "buy" if side.upper() in ("LONG", "BUY") else "sell"
        qty = self._round_qty(symbol, amount)
        if qty <= 0:
            raise RuntimeError(
                f"market({symbol},{side},{amount}) rounds to {qty} — below minimum tradeable"
            )

        params: dict[str, Any] = {}
        if reduce_only:
            params["reduceOnly"] = True

        def _place() -> dict:
            return self._ccxt.create_order(ccxt_sym, "market", ccxt_side, qty, None, params)

        with self._lock:
            raw = _retry_ccxt(f"market({symbol})", _place)

        return {
            "order_id": raw.get("id", ""),
            "symbol": symbol,
            "side": ccxt_side,
            "type": "market",
            "amount": amount,
            "filled": float(raw.get("filled", 0) or 0),
            "status": raw.get("status", "closed"),
            "avg_price": float(raw.get("average", 0) or 0),
            "cummulative_quote": float(raw.get("cost", 0) or 0),
        }

    def create_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        """Place a limit order via ccxt."""
        self._require_auth("limit_order")
        self._require_markets()
        ccxt_sym = _ccxt_symbol(symbol)
        ccxt_side = "buy" if side.upper() in ("LONG", "BUY") else "sell"
        qty = self._round_qty(symbol, amount)

        def _place() -> dict:
            return self._ccxt.create_order(ccxt_sym, "limit", ccxt_side, qty, price)

        with self._lock:
            raw = _retry_ccxt(f"limit({symbol})", _place)

        return {
            "order_id": raw.get("id", ""),
            "symbol": symbol,
            "side": ccxt_side,
            "type": "limit",
            "amount": amount,
            "price": price,
            "filled": float(raw.get("filled", 0) or 0),
            "status": raw.get("status", "open"),
        }

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open order via ccxt."""
        self._require_auth("cancel_order")
        ccxt_sym = _ccxt_symbol(symbol)

        def _cancel() -> dict:
            return self._ccxt.cancel_order(order_id, ccxt_sym)

        with self._lock:
            raw = _retry_ccxt(f"cancel({symbol})", _cancel)

        return {
            "order_id": raw.get("id", order_id),
            "status": raw.get("status", "canceled"),
        }

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        """Query order status via ccxt."""
        self._require_auth("fetch_order")
        ccxt_sym = _ccxt_symbol(symbol)

        def _fetch() -> dict:
            return self._ccxt.fetch_order(order_id, ccxt_sym)

        with self._lock:
            raw = _retry_ccxt(f"fetch_order({symbol})", _fetch)

        return {
            "order_id": raw.get("id", order_id),
            "symbol": symbol,
            "filled": float(raw.get("filled", 0) or 0),
            "status": raw.get("status", "open"),
            "remaining": float(raw.get("remaining", 0) or 0),
        }

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account_balance(self) -> dict[str, float]:
        """Fetch wallet balances via ccxt.

        For swap mode, returns total wallet balances (available + position margin).
        """
        if not self._has_auth:
            return {}

        def _fetch() -> dict:
            return self._ccxt.fetch_balance()

        with self._lock:
            try:
                raw = _retry_ccxt("balance", _fetch)
            except Exception as exc:
                logger.warning("Bitget balance fetch failed: %s", exc)
                return {}

        result: dict[str, float] = {}
        total = raw.get("total", {})
        if isinstance(total, dict):
            for asset, bal in total.items():
                if bal and float(bal) > 0:
                    result[asset] = float(bal)
        return result

    def get_available_balance(self) -> dict[str, float]:
        """Fetch available (free) balances via ccxt."""
        if not self._has_auth:
            return {}

        def _fetch() -> dict:
            return self._ccxt.fetch_balance()

        with self._lock:
            try:
                raw = _retry_ccxt("available_balance", _fetch)
            except Exception as exc:
                logger.warning("Bitget available balance fetch failed: %s", exc)
                return {}

        result: dict[str, float] = {}
        free = raw.get("free", {})
        if isinstance(free, dict):
            for asset, bal in free.items():
                if bal and float(bal) > 0:
                    result[asset] = float(bal)
        return result

    def get_positions(self) -> list[dict[str, Any]]:
        """Fetch open positions via ccxt."""
        if not self._has_auth:
            return []

        def _fetch() -> list:
            return self._ccxt.fetch_positions()

        with self._lock:
            try:
                raw_list = _retry_ccxt("positions", _fetch)
            except Exception as exc:
                logger.warning("Bitget position fetch failed: %s", exc)
                return []

        positions: list[dict[str, Any]] = []
        for pos in raw_list if isinstance(raw_list, list) else []:
            amt = float(pos.get("contracts", 0) or 0)
            if amt == 0:
                continue
            ccxt_sym = pos.get("symbol", "")
            if not ccxt_sym:
                continue
            side = pos.get("side", "")
            direction = "LONG" if side in ("long", "LONG") else "SHORT"
            positions.append({
                "symbol": _internal_symbol(ccxt_sym),
                "direction": direction,
                "entry_price": float(pos.get("entryPrice", 0) or 0),
                "quantity": abs(amt),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
            })
        return positions

    # ------------------------------------------------------------------
    # Precision & validation
    # ------------------------------------------------------------------

    def get_min_qty(self, symbol: str) -> float:
        """Minimum tradeable quantity for a symbol."""
        return self._min_qtys.get(symbol, 0.0)

    def is_valid_symbol(self, symbol: str) -> bool:
        """Check if symbol exists on Bitget swap markets."""
        self._require_markets()
        ccxt_sym = _ccxt_symbol(symbol)
        return ccxt_sym in self._ccxt.markets

    def validate_symbols(self, symbols: list[str]) -> list[str]:
        """Filter out symbols not available on Bitget.

        Called once at startup when switching from Binance to Bitget.
        """
        self._require_markets()
        valid: list[str] = []
        for sym in symbols:
            ccxt_sym = _ccxt_symbol(sym)
            if ccxt_sym in self._ccxt.markets:
                valid.append(sym)
            else:
                logger.warning("Bitget 不支持 %s（已从白名单移除）", sym)
        return valid

    # ------------------------------------------------------------------
    # Futures configuration
    # ------------------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: int = 5) -> None:
        """Set futures leverage via ccxt."""
        if not self._has_auth:
            return
        ccxt_sym = _ccxt_symbol(symbol)
        try:
            with self._lock:
                self._ccxt.set_leverage(leverage, ccxt_sym)
            logger.info("Bitget leverage set to %dx for %s", leverage, symbol)
        except Exception as exc:
            logger.warning("Bitget set_leverage failed for %s: %s", symbol, exc)

    def set_margin_mode(self, symbol: str, mode: str = "ISOLATED") -> None:
        """Set margin mode via ccxt (isolated or cross)."""
        if not self._has_auth:
            return
        ccxt_sym = _ccxt_symbol(symbol)
        ccxt_mode = "isolated" if mode.upper() == "ISOLATED" else "cross"
        try:
            with self._lock:
                self._ccxt.set_margin_mode(ccxt_mode, ccxt_sym)
            logger.info("Bitget margin mode set to %s for %s", mode.upper(), symbol)
        except Exception as exc:
            message = str(exc).lower()
            if "position" in message and (("exist" in message) or ("open" in message)):
                logger.info("Bitget margin mode already %s for %s (or position exists)", mode.upper(), symbol)
            else:
                logger.warning("Bitget set_margin_mode failed for %s: %s", symbol, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def has_auth(self) -> bool:
        return self._has_auth

    @property
    def is_testnet(self) -> bool:
        return self._testnet
