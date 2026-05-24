"""CCXT fetch helpers for extension scripts — proxy and futures type stay in extensions/."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

_CCXT_TIMEOUT_MS = int(os.getenv("CCXT_TIMEOUT_MS", "15000"))


def resolve_proxy_url(proxy: str | None = None) -> str | None:
    """Resolve proxy from arg or CCXT_PROXY / HTTP(S)_PROXY / CCXT_PROXY_PORT."""
    raw = (proxy or os.getenv("CCXT_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
    if not raw:
        port = os.getenv("CCXT_PROXY_PORT", "").strip()
        if port.isdigit():
            raw = f"http://127.0.0.1:{port}"
    if not raw:
        return None
    if raw.isdigit():
        return f"http://127.0.0.1:{raw}"
    if not raw.startswith("http"):
        return f"http://{raw}"
    return raw


def apply_proxy_env(proxy: str | None = None) -> str | None:
    """Set HTTP_PROXY / HTTPS_PROXY / CCXT_PROXY for child processes."""
    url = resolve_proxy_url(proxy)
    if not url:
        return None
    os.environ["CCXT_PROXY"] = url
    os.environ["HTTP_PROXY"] = url
    os.environ["HTTPS_PROXY"] = url
    return url


def ccxt_proxies_dict() -> Optional[dict[str, str]]:
    url = resolve_proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}


def create_ccxt_exchange():
    """Binance (or CCXT_EXCHANGE) with optional proxy and futures defaultType."""
    import ccxt

    exchange_id = os.getenv("CCXT_EXCHANGE", "binance").lower()
    exchange_cls = getattr(ccxt, exchange_id, None) or ccxt.binance
    options: dict[str, Any] = {"enableRateLimit": True, "timeout": _CCXT_TIMEOUT_MS}
    market_type = os.getenv("BINANCE_MARKET_TYPE", os.getenv("CCXT_DEFAULT_TYPE", "")).lower()
    if market_type in ("future", "futures", "swap"):
        options.setdefault("options", {})["defaultType"] = "swap"
    proxies = ccxt_proxies_dict()
    if proxies:
        options["proxies"] = proxies
        logger.info("CCXT using proxy %s", proxies.get("https", proxies.get("http")))
    return exchange_cls(options)


_INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "1H": "1h",
    "4h": "4h",
    "4H": "4h",
    "1d": "1d",
    "1D": "1d",
}


def fetch_ohlcv_map(
    codes: list[str],
    start_date: str,
    end_date: str,
    *,
    interval: str = "1h",
) -> dict[str, "pd.DataFrame"]:
    """Fetch OHLCV for multiple symbols via CCXT (extension-side proxy/futures)."""

    import pandas as pd

    from backtest.loaders.base import validate_date_range

    validate_date_range(start_date, end_date)
    exchange = create_ccxt_exchange()
    timeframe = _INTERVAL_MAP.get(interval, "1d")
    since_ms = int(pd.Timestamp(start_date).timestamp() * 1000)
    end_ms = int((pd.Timestamp(end_date) + pd.Timedelta(days=1)).timestamp() * 1000)
    budget_s = float(os.getenv("CCXT_FETCH_BUDGET_S", "60"))

    result: dict[str, pd.DataFrame] = {}
    for code in codes:
        try:
            ccxt_symbol = code.replace("-", "/").upper()
            df = _fetch_one_symbol(exchange, ccxt_symbol, timeframe, since_ms, end_ms, budget_s)
            if df is not None and not df.empty:
                result[code] = df
        except Exception as exc:
            logger.warning("CCXT failed for %s: %s", code, exc)
    return result


def _fetch_one_symbol(exchange, symbol: str, timeframe: str, since_ms: int, end_ms: int, budget_s: float):
    import time

    import ccxt
    import pandas as pd

    from backtest.loaders.base import check_budget, retry_with_budget

    all_rows: list = []
    cursor = since_ms
    limit = 1000
    deadline = time.monotonic() + budget_s
    label = f"ext ccxt fetch for {symbol}"

    for _ in range(200):
        check_budget(deadline, label, budget_s=budget_s)
        ohlcv = retry_with_budget(
            lambda: exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=limit),
            transient=ccxt.NetworkError,
            deadline=deadline,
            label=label,
        )
        if not ohlcv:
            break
        all_rows.extend(ohlcv)
        last_ts = ohlcv[-1][0]
        if last_ts >= end_ms or len(ohlcv) < limit:
            break
        cursor = last_ts + 1

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["trade_date"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.set_index("trade_date").sort_index()
    start_dt = pd.Timestamp(since_ms, unit="ms")
    end_dt = pd.Timestamp(end_ms, unit="ms")
    df = df[(df.index >= start_dt) & (df.index < end_dt)]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["open", "high", "low", "close"])
    return df if not df.empty else None
