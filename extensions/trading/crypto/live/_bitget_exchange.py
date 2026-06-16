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


def _to_trading_symbol(symbol: str) -> str:
    """Normalize base or pair symbol to internal USDT perpetual form.

    BTC / BTCUSDT → BTCUSDT
    """
    sym = symbol.strip().upper()
    if sym.endswith("USDT"):
        return sym
    return f"{sym}USDT"


def _ccxt_symbol(symbol: str) -> str:
    """Convert internal short format to ccxt Bitget swap format.

    BTCUSDT → BTC/USDT:USDT
    ETHUSD → ETH/USD:USD
    """
    trading_sym = _to_trading_symbol(symbol)
    for suffix in ("USDT", "USD"):
        if trading_sym.endswith(suffix):
            base = trading_sym[: -len(suffix)]
            if base:
                return f"{base}/{suffix}:{suffix}"
    return trading_sym


def _internal_symbol(ccxt_sym: str) -> str:
    """Convert ccxt symbol format to internal short format.

    BTC/USDT:USDT → BTCUSDT
    BTC/USDT → BTCUSDT
    NEARUSDT → NEARUSDT  (Bitget raw / no-slash ccxt symbol)
    """
    if "/" not in ccxt_sym:
        return ccxt_sym.split(":")[0]
    base = ccxt_sym.split("/")[0]
    quote = ccxt_sym.split("/")[-1].split(":")[0]
    return f"{base}{quote}"


# ---------------------------------------------------------------------------
# Retry helper (reusable pattern from _real_exchange.py)
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3
_RETRY_BACKOFF = (1, 2, 4)
# 429 断路器 / 429 circuit breaker — pause all requests for 60s after too many
_RATE_LIMIT_COOLDOWN = 60.0  # 限流后冷却秒数
_RATE_LIMIT_THRESHOLD = 3  # 连续 429 达到此数后触发冷却
_consecutive_429 = 0
_rate_limited_until: float = 0.0
_rl_lock = threading.Lock()


def _env_hedge_mode_default() -> bool | None:
    """Optional BITGET_HEDGE_MODE=1|0 override; None = defer until set_position_mode."""
    raw = os.environ.get("BITGET_HEDGE_MODE", "").strip().lower()
    if raw in ("1", "true", "yes", "hedge", "hedge_mode"):
        return True
    if raw in ("0", "false", "no", "one_way", "one_way_mode", "oneway"):
        return False
    return None


def _bitget_error_text(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


def _is_one_way_mode_error(exc: Exception) -> bool:
    """Bitget 40774/40773: order params disagree with account hold mode.

    40773 = "Closed positions can only occur in two-way positions"
    — 账户实际是 one-way mode 但订单使用了 hedged=True(posSide) / Account is one-way
    40774 = "unilateral position" — one-way mode 与订单参数冲突 / Order params disagree
    """
    text = _bitget_error_text(exc).lower()
    return "40773" in text or "unilateral position" in text


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

    global _consecutive_429, _rate_limited_until  # noqa: PLW0602

    last_err: Optional[Exception] = None

    # 断路器：冷却期内直接略过 / circuit breaker: skip during cooldown
    with _rl_lock:
        if _rate_limited_until > time.time():
            logger.warning(
                "%s skipped — cooldown (%.0fs remaining)",
                op_name, _rate_limited_until - time.time(),
            )
            raise _map_ccxt_error(
                RuntimeError(f"cooldown active for {op_name}"),
                op_name,
            )

    for attempt in range(_MAX_RETRIES):
        try:
            result = fn(*args, **kwargs)
            # 成功 → 重置失败计数 / success resets the counter
            with _rl_lock:
                _consecutive_429 = 0
            return result
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
            delay = 5 << attempt  # 指数退避: 5s, 10s, 20s / exponential backoff
            logger.warning(
                "%s attempt %d/%d rate-limited (%.1fs backoff): %s",
                op_name, attempt + 1, _MAX_RETRIES, delay, exc,
            )
            time.sleep(delay)
            continue
    # All retries exhausted — count as failure toward circuit breaker,
    # then raise the mapped error.
    with _rl_lock:
        _consecutive_429 += 1
        if _consecutive_429 >= _RATE_LIMIT_THRESHOLD:
            _rate_limited_until = time.time() + _RATE_LIMIT_COOLDOWN
            logger.warning(
                "%s — consecutive failure threshold hit (%d), cooling down for %.0fs",
                op_name, _consecutive_429, _RATE_LIMIT_COOLDOWN,
            )
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


def _extract_filled_qty(raw: dict, fallback_qty: float = 0.0) -> float:
    """从 ccxt 订单结构提取成交量（含 Bitget info 字段回退）。
    Extract filled quantity from ccxt order dict with Bitget-specific fallbacks.
    """
    filled = float(raw.get("filled", 0) or 0)
    if filled > 0:
        return filled

    info = raw.get("info")
    if isinstance(info, dict):
        for key in (
            "fillSize",
            "filledSize",
            "filledQty",
            "baseVolume",
            "accBaseVolume",
            "size",
            "fillQty",
            "dealSize",
        ):
            try:
                candidate = float(info.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                return candidate

    status = str(raw.get("status", "")).lower()
    if status in ("closed", "filled", "done", "success", "partially_filled"):
        for key in ("amount", "lastTradeAmount"):
            try:
                candidate = float(raw.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                return candidate
        if fallback_qty > 0:
            return fallback_qty
    return 0.0


def _extract_position_qty(raw: dict) -> float:
    """从 ccxt/Bitget 持仓结构提取数量（contracts 为空时回退 info.total）。
    Extract position size from ccxt dict; Bitget UTA often leaves ``contracts`` null.
    """
    contracts = raw.get("contracts")
    if contracts is not None:
        try:
            amt = float(contracts or 0)
            if amt > 0:
                return abs(amt)
        except (TypeError, ValueError):
            pass

    info = raw.get("info")
    if isinstance(info, dict):
        for key in ("total", "available", "holdVol", "size"):
            try:
                candidate = float(info.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                return abs(candidate)

    notional = float(raw.get("notional", 0) or 0)
    mark = float(raw.get("markPrice", 0) or 0)
    if isinstance(info, dict) and mark <= 0:
        try:
            mark = float(info.get("markPrice", 0) or 0)
        except (TypeError, ValueError):
            mark = 0.0
    if notional > 0 and mark > 0:
        return notional / mark
    return 0.0


def _extract_position_entry(raw: dict) -> float:
    """Entry price with Bitget ``openPriceAvg`` fallback."""
    try:
        entry = float(raw.get("entryPrice", 0) or 0)
    except (TypeError, ValueError):
        entry = 0.0
    if entry > 0:
        return entry
    info = raw.get("info")
    if isinstance(info, dict):
        try:
            return float(info.get("openPriceAvg", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _extract_position_mark(raw: dict) -> float:
    """Mark price with Bitget ``markPrice`` info fallback."""
    try:
        mark = float(raw.get("markPrice", 0) or 0)
    except (TypeError, ValueError):
        mark = 0.0
    if mark > 0:
        return mark
    info = raw.get("info")
    if isinstance(info, dict):
        try:
            return float(info.get("markPrice", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _extract_position_unrealized(raw: dict) -> float:
    """Unrealized PnL with Bitget ``unrealizedPL`` fallback."""
    try:
        pnl = float(raw.get("unrealizedPnl", 0) or 0)
    except (TypeError, ValueError):
        pnl = 0.0
    if pnl != 0:
        return pnl
    info = raw.get("info")
    if isinstance(info, dict):
        try:
            return float(info.get("unrealizedPL", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _to_internal_order(
    raw: dict,
    *,
    symbol: str,
    requested_qty: float,
    order_type: str,
    side: str,
    price: Optional[float] = None,
) -> dict[str, Any]:
    """Map ccxt order response to ExchangeBase internal format."""
    filled = _extract_filled_qty(raw, requested_qty)
    result: dict[str, Any] = {
        "order_id": raw.get("id", ""),
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "amount": requested_qty,
        "filled": filled,
        "status": raw.get("status", "open"),
        "avg_price": float(raw.get("average", 0) or 0),
        "cummulative_quote": float(raw.get("cost", 0) or 0),
    }
    if price is not None:
        result["price"] = price
    if "remaining" in raw:
        result["remaining"] = float(raw.get("remaining", 0) or 0)
    return result


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
    Trading methods implemented.

    Supports exchange-native bracket orders (stop-loss / take-profit conditional
    orders) via ccxt stop_market / take_profit_market order types.
    Software TPSL handles trailing stop, DCA, and de-risk as usual —
    these paths cancel exchange brackets before issuing market closes.
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

        # ccxt's Bitget implementation calls ``self.load_markets()`` at the
        # start of EVERY ``fetch_ticker``/``set_margin_mode``/etc — making a
        # network request to ``/api/v2/spot/public/coins`` that is easily
        # rate-limited. The ``markets``/``markets_by_id`` property guards in the
        # base class do NOT prevent this; the Bitget subclass calls it directly.
        # We replace ``load_markets`` itself with a no-op and provide a
        # ``market()`` fallback that auto-creates minimal swap entries for any
        # symbol the caller needs — keeping all trading methods functional
        # without ever hitting the rate-limited endpoint.
        self._ccxt.load_markets = lambda reload=False: {}  # type: ignore[method-assign]
        self._ccxt._markets = {}  # type: ignore[union-attr]
        self._ccxt._markets_by_id = {}  # type: ignore[union-attr]

        _orig_market = self._ccxt.market

        def _auto_seed_market(symbol: str) -> dict:
            try:
                return _orig_market(symbol)
            except Exception:
                base = symbol.split("/")[0] if "/" in symbol else symbol.replace("USDT", "")
                entry = {
                    "id": f"{base}USDT",
                    "symbol": symbol,
                    "base": base, "quote": "USDT", "settle": "USDT",
                    "settleId": "USDT",
                    "type": "swap", "spot": False, "swap": True,
                    "linear": True, "inverse": False,
                    "active": True, "contract": True, "contractSize": 1,
                    "limits": {"amount": {"min": 0.01}, "price": {"min": 0.01}},
                    "precision": {"amount": 0.01, "price": 0.01},
                }
                return entry

        self._ccxt.market = _auto_seed_market  # type: ignore[method-assign]

        # Cache for symbol precision
        self._min_qtys: dict[str, float] = {}
        self._tick_sizes: dict[str, float] = {}
        self._step_sizes: dict[str, float] = {}
        # None = assume hedge until set_position_mode / 40774 fallback resolves it
        self._hedge_mode: bool | None = _env_hedge_mode_default()
        self._uta_account: bool | None = None  # UTA vs Classic (mix v2)
        # ccxt 下单未传 marginMode 时默认全仓 / ccxt defaults orders to cross margin
        self._margin_mode: str = "isolated"

        # Lazy-load markets on first API call (best-effort)
        self._load_markets()

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def _load_markets(self) -> None:
        """Load markets to cache precision and valid symbols (no-op).

        ``self._ccxt.load_markets`` was replaced with a no-op in ``__init__``
        to avoid hitting the rate-limited ``/spot/public/coins`` endpoint.
        Common precision values are pre-configured here instead.
        """
        if self._markets_loaded:
            return
        for sym in ["BTCUSDT", "ETHUSDT"]:
            self._min_qtys[sym] = 0.001
            self._tick_sizes[sym] = 0.1
            self._step_sizes[sym] = 0.001
        for sym in ["PENDLEUSDT", "NEARUSDT"]:
            self._min_qtys[sym] = 0.1
            self._tick_sizes[sym] = 0.0001
            self._step_sizes[sym] = 0.1
        self._markets_loaded = True

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
            raw = self._ccxt.fetch_ticker(ccxt_sym)
            if raw is None:
                raise ValueError(f"fetch_ticker returned None for {symbol}")
            return raw

        with self._lock:
            raw = _retry_ccxt(f"ticker({symbol})", _fetch)
            return _ccxt_ticker_to_internal(raw)

    def get_tickers(self, symbols: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Fetch tickers for requested symbols, one-by-one to avoid heavy endpoint.

        Uses ``fetch_ticker()`` per symbol (lighter) instead of ``fetch_tickers()``
        (fetches every tradable pair, hits a high-volume public endpoint that is
        easily rate-limited). When ``symbols`` is ``None`` (caller wants everything),
        falls back to a curated list of common USDT perpetual pairs.
        """
        targets = symbols or [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
            "ADAUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
            "PENDLEUSDT", "NEARUSDT", "AAVEUSDT", "UNIUSDT", "LTCUSDT",
        ]
        result: list[dict[str, Any]] = []
        for sym in targets:
            try:
                t = self.get_ticker(sym)
                if t.get("last", 0) and float(t["last"]) > 0:
                    result.append(t)
            except Exception as exc:
                if symbols is not None:
                    logger.warning("get_ticker(%s) skipped: %s", sym, exc)
        return result

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

    def _round_price(self, symbol: str, price: float) -> float:
        """Round price using ccxt tick_size to match exchange precision.
        使用 tick_size 匹配交易所价格精度。
        """
        tick = self._tick_sizes.get(symbol)
        if tick and tick > 0:
            from decimal import Decimal, ROUND_HALF_UP
            tick_d = Decimal(str(tick))
            price_d = Decimal(str(price))
            rounded = (price_d / tick_d).to_integral_value(rounding=ROUND_HALF_UP) * tick_d
            return float(rounded)
        decimals = 2 if price >= 1 else 6
        return round(price, decimals)

    def _resolve_filled_qty(self, raw: dict, symbol: str, requested_qty: float) -> float:
        """Resolve filled qty; Bitget create_order often returns filled=0 initially.
        解析成交量；Bitget 下单响应常延迟填充 filled 字段。
        """
        filled = _extract_filled_qty(raw, requested_qty)
        if filled > 0:
            return filled

        order_id = raw.get("id")
        if not order_id:
            return filled

        ccxt_sym = _ccxt_symbol(symbol)
        for delay in (0.0, 0.2, 0.5):
            if delay:
                time.sleep(delay)
            try:
                with self._lock:
                    fetched = _retry_ccxt(
                        f"fetch_order({symbol})",
                        self._ccxt.fetch_order,
                        order_id,
                        ccxt_sym,
                    )
                filled = _extract_filled_qty(fetched, requested_qty)
                if filled > 0:
                    return filled
            except Exception as exc:
                logger.debug("Bitget fill poll via fetch_order failed: %s", exc)
        return filled

    @staticmethod
    def _pos_side(side: str) -> str:
        """Normalize trade side to Bitget UTA ``posSide`` value."""
        return "long" if side.lower() in ("buy", "long") else "short"

    def _order_params(self, side: str, *, reduce_only: bool = False) -> dict[str, Any]:
        """Build ccxt params aligned with UTA hedge vs one-way hold mode.

        Do not pass raw ``posSide`` — ccxt sets it when ``hedged=True``.
        One-way accounts need ``oneWayMode=True`` and must omit ``posSide``.
        Always pass ``marginMode`` — ccxt Bitget defaults to cross (全仓) otherwise.
        """
        params: dict[str, Any] = {"marginMode": self._margin_mode}
        if reduce_only:
            params["reduceOnly"] = True
        hedge = True if self._hedge_mode is None else self._hedge_mode
        if hedge:
            params["hedged"] = True
        else:
            params["oneWayMode"] = True
        return params

    def _place_with_hold_mode_fallback(self, op_name: str, place_fn) -> Any:
        """Place order; on 40774 retry once after switching to oneWayMode."""
        try:
            return _retry_ccxt(op_name, place_fn)
        except Exception as exc:
            if not _is_one_way_mode_error(exc) or self._hedge_mode is False:
                raise _map_ccxt_error(exc, op_name) from exc
            logger.warning(
                "%s — account appears one-way; retrying with oneWayMode",
                op_name,
            )
            self._hedge_mode = False
            return _retry_ccxt(f"{op_name}(oneWay)", place_fn)

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

        params = self._order_params(side, reduce_only=reduce_only)

        def _place() -> dict:
            return self._ccxt.create_order(ccxt_sym, "market", ccxt_side, qty, None, params)

        with self._lock:
            raw = self._place_with_hold_mode_fallback(f"market({symbol})", _place)

        filled = self._resolve_filled_qty(raw, symbol, qty)
        order = _to_internal_order(
            raw,
            symbol=symbol,
            requested_qty=qty,
            order_type="market",
            side=ccxt_side,
        )
        order["filled"] = filled
        order["amount"] = amount
        return order

    def create_limit_order(self, symbol: str, side: str, amount: float, price: float) -> dict:
        """Place a limit order via ccxt."""
        self._require_auth("limit_order")
        self._require_markets()
        ccxt_sym = _ccxt_symbol(symbol)
        ccxt_side = "buy" if side.upper() in ("LONG", "BUY") else "sell"
        qty = self._round_qty(symbol, amount)

        def _place() -> dict:
            return self._ccxt.create_order(ccxt_sym, "limit", ccxt_side, qty, price, self._order_params(side))

        with self._lock:
            raw = self._place_with_hold_mode_fallback(f"limit({symbol})", _place)

        return _to_internal_order(
            raw,
            symbol=symbol,
            requested_qty=qty,
            order_type="limit",
            side=ccxt_side,
            price=price,
        )

    def create_stop_loss_order(self, symbol: str, side: str, amount: float, stop_price: float) -> dict:
        """Place stop-loss plan order via ccxt dedicated method.
        Bitget 通过 ccxt create_stop_loss_order 创建 plan order（planType=loss_plan）。
        """
        self._require_auth("stop_loss_order")
        self._require_markets()
        ccxt_sym = _ccxt_symbol(symbol)
        ccxt_side = side.lower()
        qty = self._round_qty(symbol, amount)
        if qty <= 0:
            raise RuntimeError(
                f"stop_loss({symbol},{side},{amount},{stop_price}) rounds to {qty} — below minimum tradeable"
            )
        trigger_price = self._round_price(symbol, stop_price)

        def _place() -> dict:
            return self._ccxt.create_stop_loss_order(
                ccxt_sym, "market", ccxt_side, qty, None, trigger_price,
                self._order_params(ccxt_side, reduce_only=True),
            )

        with self._lock:
            raw = self._place_with_hold_mode_fallback(f"stop_loss({symbol})", _place)

        return {
            "order_id": str(raw.get("id", "")),
            "symbol": symbol,
            "side": side,
            "type": "stop_loss",
            "amount": amount,
            "stop_price": stop_price,
            "filled": float(raw.get("filled", 0) or 0),
            "status": raw.get("status", ""),
        }

    def create_take_profit_order(self, symbol: str, side: str, amount: float, tp_price: float) -> dict:
        """Place take-profit plan order via ccxt dedicated method.
        Bitget 通过 ccxt create_take_profit_order 创建 plan order（planType=profit_plan）。
        """
        self._require_auth("take_profit_order")
        self._require_markets()
        ccxt_sym = _ccxt_symbol(symbol)
        ccxt_side = side.lower()
        qty = self._round_qty(symbol, amount)
        if qty <= 0:
            raise RuntimeError(
                f"take_profit({symbol},{side},{amount},{tp_price}) rounds to {qty} — below minimum tradeable"
            )
        trigger_price = self._round_price(symbol, tp_price)

        def _place() -> dict:
            return self._ccxt.create_take_profit_order(
                ccxt_sym, "market", ccxt_side, qty, None, trigger_price,
                self._order_params(ccxt_side, reduce_only=True),
            )

        with self._lock:
            raw = self._place_with_hold_mode_fallback(f"take_profit({symbol})", _place)

        return {
            "order_id": str(raw.get("id", "")),
            "symbol": symbol,
            "side": side,
            "type": "take_profit",
            "amount": amount,
            "tp_price": tp_price,
            "filled": float(raw.get("filled", 0) or 0),
            "status": raw.get("status", ""),
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

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """获取当前未成交订单列表 / Fetch open orders via ccxt.

        Args:
            symbol: Optional symbol filter. ``None`` returns all open orders.

        Returns:
            List of open order dicts with keys: order_id, symbol, side, type,
            amount, filled, status, price, timestamp.
        """
        if not self._has_auth:
            return []

        ccxt_sym = _ccxt_symbol(symbol) if symbol else None

        def _fetch() -> list:
            params: dict[str, Any] = {"productType": "USDT-FUTURES"}
            return self._ccxt.fetch_open_orders(ccxt_sym, params=params)

        with self._lock:
            try:
                raw_list = _retry_ccxt("open_orders", _fetch)
            except Exception as exc:
                logger.warning("Bitget fetch_open_orders failed: %s", exc)
                return []

        orders: list[dict[str, Any]] = []
        for raw in raw_list:
            ccxt_order_sym = raw.get("symbol", "")
            if not ccxt_order_sym:
                continue
            orders.append({
                "order_id": raw.get("id", ""),
                "symbol": _internal_symbol(ccxt_order_sym) if ccxt_order_sym else "",
                "side": str(raw.get("side", "")).lower(),
                "type": str(raw.get("type", "")),
                "amount": float(raw.get("amount", 0) or 0),
                "filled": float(raw.get("filled", 0) or 0),
                "status": raw.get("status", ""),
                "price": float(raw.get("price", 0) or 0),
                "timestamp": raw.get("timestamp"),
            })
        return orders

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        """Query order status via ccxt."""
        self._require_auth("fetch_order")
        ccxt_sym = _ccxt_symbol(symbol)

        def _fetch() -> dict:
            return self._ccxt.fetch_order(order_id, ccxt_sym)

        with self._lock:
            raw = _retry_ccxt(f"fetch_order({symbol})", _fetch)

        ccxt_side = str(raw.get("side", "")).lower() or "buy"
        return _to_internal_order(
            raw,
            symbol=symbol,
            requested_qty=float(raw.get("amount", 0) or 0),
            order_type=str(raw.get("type", "market")),
            side=ccxt_side,
        )

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

    def get_balance_snapshot(self) -> dict[str, dict[str, float]]:
        """Fetch total and available balances in a single ccxt call.

        Avoids the double API round-trip that calling ``get_account_balance``
        and ``get_available_balance`` separately would incur.
        """
        if not self._has_auth:
            return {"total": {}, "free": {}}

        def _fetch() -> dict:
            return self._ccxt.fetch_balance()

        with self._lock:
            try:
                raw = _retry_ccxt("balance_snapshot", _fetch)
            except Exception as exc:
                logger.warning("Bitget balance snapshot failed: %s", exc)
                return {"total": {}, "free": {}}

        def _extract(key: str) -> dict[str, float]:
            result: dict[str, float] = {}
            items = raw.get(key, {})
            if isinstance(items, dict):
                for asset, bal in items.items():
                    if bal and float(bal) > 0:
                        result[asset] = float(bal)
            return result

        return {"total": _extract("total"), "free": _extract("free")}

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
            amt = _extract_position_qty(pos)
            if amt <= 0:
                continue
            ccxt_sym = pos.get("symbol", "")
            if not ccxt_sym:
                continue
            info = pos.get("info")
            if isinstance(info, dict) and info.get("symbol"):
                ccxt_sym = str(info["symbol"])
            side = pos.get("side", "")
            direction = "LONG" if side in ("long", "LONG") else "SHORT"
            mark = _extract_position_mark(pos)
            entry = _extract_position_entry(pos)
            positions.append({
                "symbol": _internal_symbol(ccxt_sym),
                "direction": direction,
                "entry_price": entry,
                "mark_price": mark if mark > 0 else entry,  # 标记价格供 mandate 门控检查 / mark price for mandate enforcement
                "quantity": amt,
                "notional": float(pos.get("notional", 0) or 0),
                "unrealized_pnl": _extract_position_unrealized(pos),
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
        mkt = self._ccxt.markets
        if not mkt:
            return True  # 市场不可用时接受所有 / accept all when markets unavailable
        ccxt_sym = _ccxt_symbol(symbol)
        return ccxt_sym in mkt

    def validate_symbols(self, symbols: list[str]) -> list[str]:
        """Filter out symbols not available on Bitget.

        Whitelist may use base tickers (BTC) or pair tickers (BTCUSDT); both are
        accepted. Called once at startup when switching from Binance to Bitget.
        """
        self._require_markets()
        # ccxt.markets may be None/empty when load_markets was rate-limited;
        # accept all symbols in that case rather than crashing at startup.
        mkt = self._ccxt.markets
        if not mkt:
            logger.warning("Bitget markets unavailable (rate-limited), accepting all whitelist symbols")
            return list(symbols)
        valid: list[str] = []
        for sym in symbols:
            ccxt_sym = _ccxt_symbol(sym)
            if ccxt_sym in mkt:
                valid.append(sym)
            else:
                logger.warning(
                    "Bitget 不支持 %s（已从白名单移除）",
                    _to_trading_symbol(sym),
                )
        return valid

    # ------------------------------------------------------------------
    # Futures configuration
    # ------------------------------------------------------------------

    def set_position_mode(self, dual: bool = False) -> None:
        """Set hold mode: dual=True → hedge_mode (posSide), dual=False → one-way.

        Tries UTA API first; Classic accounts fall back to mix v2 ``productType``.
        """
        if not self._has_auth:
            return
        hedged = dual
        last_exc: Exception | None = None
        param_attempts = (
            (True, {"uta": True}),
            (False, {"productType": "USDT-FUTURES"}),
        )
        for uta, params in param_attempts:
            try:
                with self._lock:
                    self._ccxt.set_position_mode(hedged, None, dict(params))
                self._hedge_mode = hedged
                self._uta_account = uta
                label = "hedge_mode" if hedged else "one_way_mode"
                api_label = "UTA" if uta else "Classic/mix-v2"
                logger.info("Bitget position mode set to %s (%s)", label, api_label)
                return
            except Exception as exc:
                last_exc = exc
                text = _bitget_error_text(exc)
                if uta and ("40084" in text or "Classic Account" in text or "Unified Account" in text):
                    logger.info("Bitget account is Classic — retrying set_position_mode via mix v2")
                    continue
                logger.warning(
                    "Bitget set_position_mode(%s, uta=%s) failed: %s",
                    "hedge" if hedged else "one-way",
                    uta,
                    exc,
                )
        if self._hedge_mode is None:
            self._hedge_mode = hedged
        if last_exc is not None:
            logger.warning("Bitget set_position_mode exhausted attempts: %s", last_exc)

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

    def verify_margin_leverage(
        self,
        symbol: str,
        *,
        margin_mode: str = "ISOLATED",
        leverage: int | None = None,
    ) -> bool:
        """Confirm symbol margin mode (and optional leverage) via fetch_leverage.

        开仓前校验 Bitget 侧是否已切到逐仓/目标杠杆。
        """
        if not self._has_auth:
            return True
        ccxt_sym = _ccxt_symbol(symbol)
        want_mode = "isolated" if margin_mode.upper() == "ISOLATED" else "cross"
        try:
            with self._lock:
                lev = self._ccxt.fetch_leverage(ccxt_sym)
        except Exception as exc:
            logger.warning("Bitget verify_margin_leverage fetch failed for %s: %s", symbol, exc)
            return False
        got_mode = str(lev.get("marginMode") or "").lower()
        if got_mode != want_mode:
            logger.warning(
                "Bitget %s margin mode mismatch: want=%s got=%s",
                symbol,
                want_mode,
                got_mode or "?",
            )
            return False
        if leverage is not None:
            long_lev = int(float(lev.get("longLeverage") or 0))
            short_lev = int(float(lev.get("shortLeverage") or long_lev))
            if long_lev != leverage or short_lev != leverage:
                logger.warning(
                    "Bitget %s leverage mismatch: want=%dx got long=%d short=%d",
                    symbol,
                    leverage,
                    long_lev,
                    short_lev,
                )
                return False
        return True

    def set_margin_mode(self, symbol: str, mode: str = "ISOLATED") -> bool:
        """Set margin mode via ccxt (isolated or cross).

        Returns True if margin mode was successfully set or already correct.
        Returns False if the change failed (e.g. position exists on symbol).
        Bitget 不允许在有持仓时改变保证金模式。
        """
        if not self._has_auth:
            return False
        ccxt_sym = _ccxt_symbol(symbol)
        ccxt_mode = "isolated" if mode.upper() == "ISOLATED" else "cross"
        try:
            with self._lock:
                self._ccxt.set_margin_mode(ccxt_mode, ccxt_sym)
            self._margin_mode = ccxt_mode
            logger.info("Bitget margin mode set to %s for %s", mode.upper(), symbol)
            return True
        except Exception as exc:
            message = str(exc).lower()
            if "position" in message and (("exist" in message) or ("open" in message) or ("holding" in message)):
                logger.info("Bitget margin mode already %s for %s (or position exists)", mode.upper(), symbol)
            else:
                logger.warning("Bitget set_margin_mode failed for %s: %s", symbol, exc)
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def has_auth(self) -> bool:
        return self._has_auth

    @property
    def is_testnet(self) -> bool:
        return self._testnet
