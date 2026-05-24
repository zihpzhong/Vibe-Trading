"""Phase 1 A-share daily scanner — single-direction LONG scoring."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from extensions.trading.astock.live import alpha_factors
from extensions.trading.astock.config import AStockTradingConfig
from extensions.trading.astock.live.data import infer_board, normalize_symbol
from extensions.trading.astock.live.exchange import AStockExchangeBase

logger = logging.getLogger(__name__)

MIN_SCORE = 3


@dataclass
class AStockScanResult:
    rankings: list[dict[str, Any]] = field(default_factory=list)
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    filtered_count: int = 0
    scan_time_ms: float = 0.0


def _rsi(close: pd.Series, period: int = 14) -> float:
    if len(close) < period + 1:
        return 50.0
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    if avg_loss.iloc[-1] == 0:
        return 100.0
    rs = avg_gain.iloc[-1] / avg_loss.iloc[-1]
    return float(100.0 - (100.0 / (1.0 + rs)))


def score_buy(indicators: dict[str, float], alpha_signal: float) -> int:
    """0-10 LONG score for A-share daily setup."""
    score = 0
    rsi = indicators.get("rsi_daily", 50)
    if 35 <= rsi <= 65:
        score += 2
    elif 30 <= rsi < 35:
        score += 3
    if indicators.get("above_ma20", 0) > 0:
        score += 2
    if indicators.get("above_ma60", 0) > 0:
        score += 1
    vol_ratio = indicators.get("vol_ratio", 1.0)
    if vol_ratio >= 1.2:
        score += 2
    elif vol_ratio >= 1.0:
        score += 1
    chg5 = indicators.get("change_5d_pct", 0)
    if 0 < chg5 < 8:
        score += 1
    if alpha_signal > 0.2:
        score += 2
    elif alpha_signal > 0:
        score += 1
    return min(10, score)


class AStockScanner:
    def __init__(self, exchange: AStockExchangeBase, config: Optional[AStockTradingConfig] = None) -> None:
        self._exchange = exchange
        self._config = config or AStockTradingConfig()

    def scan(self, top_n: int = 20, universe: Optional[list[str]] = None) -> AStockScanResult:
        t0 = time.time()
        symbols = universe or getattr(self._exchange, "universe", [])
        if not symbols and hasattr(self._exchange, "_universe"):
            symbols = self._exchange._universe  # noqa: SLF001
        rankings: list[dict[str, Any]] = []
        watchlist: list[dict[str, Any]] = []
        filtered = 0

        for sym in symbols[: max(top_n * 3, top_n)]:
            sym = normalize_symbol(sym)
            try:
                row = self._scan_symbol(sym)
            except Exception as exc:
                logger.debug("scan skip %s: %s", sym, exc)
                filtered += 1
                continue
            if row is None:
                filtered += 1
                continue
            if row["score"] >= self._config.scan_entry_threshold:
                rankings.append(row)
            elif row["score"] >= MIN_SCORE:
                watchlist.append(row)
            else:
                filtered += 1

        rankings.sort(key=lambda r: r["score"], reverse=True)
        return AStockScanResult(
            rankings=rankings[:top_n],
            watchlist=watchlist,
            filtered_count=filtered,
            scan_time_ms=(time.time() - t0) * 1000,
        )

    def _scan_symbol(self, symbol: str) -> Optional[dict[str, Any]]:
        if self._exchange.is_suspended(symbol):
            return None
        if self._exchange.is_st(symbol):
            return None
        if self._exchange.get_listing_days(symbol) < self._config.gate.min_listing_days:
            return None
        lim = self._exchange.is_limit(symbol)
        if lim in ("up", "down"):
            return None

        kline = self._exchange.get_daily(symbol, 120)
        if kline is None or len(kline) < 25:
            return None

        close = kline["close"].astype(float)
        high = kline["high"].astype(float)
        low = kline["low"].astype(float)
        volume = kline["volume"].astype(float)

        ticker = self._exchange.get_ticker(symbol)
        amount = float(ticker.get("amount", 0) or 0)
        if amount < self._config.gate.min_daily_amount_cny:
            return None
        market_cap = float(ticker.get("market_cap", 0) or 0)
        if market_cap > 0 and market_cap < self._config.gate.min_market_cap_cny:
            return None

        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean()) if len(close) >= 60 else ma20
        last = float(close.iloc[-1])
        vol_avg5 = float(volume.tail(5).mean()) if len(volume) >= 5 else float(volume.iloc[-1])
        vol_ratio = float(volume.iloc[-1]) / vol_avg5 if vol_avg5 > 0 else 1.0
        chg5 = (last / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0.0

        factors = alpha_factors.compute_all(close, high, low, volume)
        alpha_sig = alpha_factors.aggregate_signal(factors)

        indicators = {
            "rsi_daily": _rsi(close),
            "above_ma20": 1.0 if last > ma20 else 0.0,
            "above_ma60": 1.0 if last > ma60 else 0.0,
            "vol_ratio": vol_ratio,
            "change_5d_pct": chg5,
        }
        score = score_buy(indicators, alpha_sig)
        name = str(ticker.get("name", symbol))

        return {
            "symbol": symbol,
            "name": name,
            "direction": "LONG",
            "score": score,
            "entry_price": last,
            "rsi_daily": indicators["rsi_daily"],
            "vol_ratio": vol_ratio,
            "change_5d_pct": chg5,
            "alpha_signal": alpha_sig,
            "board": infer_board(symbol),
            "amount": amount,
            "market_cap": market_cap,
        }
