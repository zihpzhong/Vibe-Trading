"""Phase 1 Market Scanner — lightweight top-20 screening engine.

Translates SKILL.md Phase 1 scoring rules into pure Python:
7 indicators → LONG_Score (max 10) + SHORT_Score (max 10) → TOP 3 ranking.

All scoring functions are static methods for deterministic unit testing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .exchange import ExchangeBase

logger = logging.getLogger(__name__)

MIN_VOLUME_24H_USDT = 1_000_000  # 100 万 USDT
MIN_SCORE = 3

# Stablecoins that should never generate trading signals
_STABLECOINS: frozenset[str] = frozenset({
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP",
    "USD1", "USTC", "USDD", "FRAX", "LUSD", "PAXG",
})


@dataclass
class ScanResult:
    """Output of a single Phase 1 scan."""

    rankings: list[dict[str, Any]] = field(default_factory=list)
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    filtered_count: int = 0
    scan_time_ms: float = 0.0


def _rsi(close: pd.Series, period: int = 14) -> float:
    """Compute RSI using Wilder's smoothing (EMA of gains/losses)."""
    if len(close) < period + 1:
        return 50.0
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    last_avg_gain = avg_gain.iloc[-1]
    last_avg_loss = avg_loss.iloc[-1]
    if last_avg_loss == 0:
        return 100.0 if last_avg_gain > 0 else 50.0
    rs = last_avg_gain / last_avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _ema(close: pd.Series, period: int) -> float:
    """Exponential moving average."""
    if len(close) < period:
        return float(close.mean())
    return float(close.ewm(span=period, adjust=False).mean().iloc[-1])


def _bb_pct(close: pd.Series, period: int = 20, num_std: float = 2.0) -> float:
    """Bollinger Band %b: 0 = lower band, 1 = upper band."""
    if len(close) < period:
        return 0.5
    sma = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    last_upper = upper.iloc[-1]
    last_lower = lower.iloc[-1]
    last_close = close.iloc[-1]
    denom = last_upper - last_lower
    if denom == 0:
        return 0.5
    return float((last_close - last_lower) / denom)


class MarketScanner:
    """Phase 1 quick screen: Binance top 20 → 7 indicators → dual scoring → TOP 3.

    Usage:
        ex = MockExchange()
        scanner = MarketScanner(ex)
        result = scanner.scan()
        for r in result.rankings:
            print(r["symbol"], r["direction"], r["score"])
    """

    def __init__(self, exchange: ExchangeBase) -> None:
        self._exchange = exchange

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, top_n: int = 20) -> ScanResult:
        """Run full Phase 1 scan pipeline.

        Args:
            top_n: Number of top-volume symbols to screen.

        Returns:
            ScanResult with rankings (score ≥ MIN_SCORE) and watchlist (score 3-4).
        """
        t0 = time.perf_counter()
        tickers = self._exchange.get_tickers()
        if not tickers:
            elapsed = (time.perf_counter() - t0) * 1000
            return ScanResult(rankings=[], watchlist=[], filtered_count=0, scan_time_ms=elapsed)

        scored: list[dict[str, Any]] = []
        batch = tickers[:top_n]
        logger.info("Scanning %d symbols for Phase 1...", len(batch))
        for i, t in enumerate(batch):
            sym = t["symbol"]
            # Skip stablecoins — they should never generate trading signals
            base_currency = sym.removesuffix("USDT").removesuffix("USD")
            if base_currency in _STABLECOINS:
                logger.info("  Filter(稳定币): %s skipped", sym)
                continue
            if (i + 1) % 5 == 0 or i == 0:
                logger.info("  Progress: %d/%d — %s", i + 1, len(batch), sym)
            try:
                kline_1h = self._exchange.get_kline(sym, "1h", 200)
                kline_15m = self._exchange.get_kline(sym, "15m", 20)
            except Exception:
                logger.warning("get_kline failed for %s, skipping", sym)
                continue

            indicators = self.compute_indicators(t, kline_1h, kline_15m)
            long_score = self.score_long(indicators)
            short_score = self.score_short(indicators)
            direction = "LONG" if long_score >= short_score else "SHORT"
            score = max(long_score, short_score)
            rsi_extremity = abs(indicators["rsi_1h"] - 50.0)

            scored.append({
                "symbol": sym,
                "direction": direction,
                "score": score,
                "long_score": long_score,
                "short_score": short_score,
                "rsi_1h": indicators["rsi_1h"],
                "rsi_15m": indicators["rsi_15m"],
                "change_24h": indicators["change_24h"],
                "vol_ratio": indicators["vol_ratio"],
                "volume_24h": indicators["volume_24h"],
                "rsi_extremity": rsi_extremity,
            })

        rankings, watchlist, filtered = self.rank(scored)
        elapsed = (time.perf_counter() - t0) * 1000
        return ScanResult(
            rankings=rankings,
            watchlist=watchlist,
            filtered_count=filtered,
            scan_time_ms=elapsed,
        )

    # ------------------------------------------------------------------
    # Static indicator computation (pure functions for testability)
    # ------------------------------------------------------------------

    @staticmethod
    def compute_indicators(
        ticker: dict[str, Any],
        kline_1h: pd.DataFrame,
        kline_15m: pd.DataFrame,
    ) -> dict[str, Any]:
        """Compute 7 lightweight indicators from market data.

        Args:
            ticker: From get_ticker() — keys: symbol, last, open24h, volume24h, high24h, low24h.
            kline_1h: 1h OHLCV DataFrame (200 bars), columns: open, high, low, close, volume.
            kline_15m: 15m OHLCV DataFrame (20 bars).

        Returns:
            dict with: symbol, price, change_24h, rsi_1h, rsi_15m, ema200,
                       bb_pct, vol_ratio, price_in_8h_pct, volume_24h.
        """
        price = float(ticker.get("last", 0))
        change_24h = float(ticker.get("change24h", 0))

        close_1h = kline_1h["close"].astype(float)
        close_15m = kline_15m["close"].astype(float)
        volume_1h = kline_1h["volume"].astype(float)

        rsi_1h = _rsi(close_1h, 14)
        rsi_15m = _rsi(close_15m, 14)
        ema200 = _ema(close_1h, 200)
        bb_pct = _bb_pct(close_1h, 20, 2.0)

        # Volume ratio: latest bar volume / average of last 24 bars
        if len(volume_1h) >= 24:
            vol_ratio = float(volume_1h.iloc[-1] / volume_1h.tail(24).mean()) if volume_1h.tail(24).mean() > 0 else 1.0
        else:
            vol_ratio = 1.0

        # 8h range position: where price sits in last 8 1h bars
        if len(kline_1h) >= 8:
            high_8h = float(kline_1h["high"].tail(8).max())
            low_8h = float(kline_1h["low"].tail(8).min())
            denom = high_8h - low_8h
            price_in_8h_pct = float((price - low_8h) / denom) if denom > 0 else 0.5
        else:
            price_in_8h_pct = 0.5

        return {
            "symbol": ticker.get("symbol", ""),
            "price": price,
            "change_24h": change_24h,
            "rsi_1h": rsi_1h,
            "rsi_15m": rsi_15m,
            "ema200": ema200,
            "bb_pct": bb_pct,
            "vol_ratio": vol_ratio,
            "price_in_8h_pct": price_in_8h_pct,
            "volume_24h": float(ticker.get("volume24h", 0)),
        }

    # ------------------------------------------------------------------
    # Scoring — exact match with SKILL.md Step 2
    # ------------------------------------------------------------------

    @staticmethod
    def score_long(ind: dict[str, Any]) -> int:
        """Calculate LONG score (max 10).

        Rules from SKILL.md Step 2 — LONG_Score:
          Technical oversold: +2 RSI(1h)<30, +1 RSI(1h)30-40, +1 RSI(15m)<30, +1 RSI(15m)<RSI(1h)
          Price decline: +1 24h跌>5%, +2 24h跌>10%
          Position: +1 BB%<0.2, +1 price in lower 20% of 8h range
          Trend filter: +1 price>EMA200; 0 if price<EMA200 AND RSI>40
          Volume: +1 vol_ratio>1.5 AND 24h%<0
        """
        score = 0
        rsi_1h = ind["rsi_1h"]
        rsi_15m = ind["rsi_15m"]
        change_24h = ind["change_24h"]
        bb_pct = ind["bb_pct"]
        price = ind["price"]
        ema200 = ind["ema200"]
        vol_ratio = ind["vol_ratio"]
        price_in_8h_pct = ind["price_in_8h_pct"]

        # Technical oversold
        if rsi_1h < 30:
            score += 2
        elif 30 <= rsi_1h < 40:
            score += 1
        if rsi_15m < 30:
            score += 1
        if rsi_15m < rsi_1h:
            score += 1

        # Price decline
        if change_24h < -10:
            score += 2
        elif change_24h < -5:
            score += 1

        # Position confirmation
        if bb_pct < 0.2:
            score += 1
        if price_in_8h_pct < 0.2:
            score += 1

        # Trend filter
        if price > ema200:
            score += 1
        if price < ema200 and rsi_1h > 40:
            score = 0  # 下降趋势中非超卖 → 不做多

        # Volume confirmation
        if vol_ratio > 1.5 and change_24h < 0:
            score += 1

        return score

    @staticmethod
    def score_short(ind: dict[str, Any]) -> int:
        """Calculate SHORT score (max 10).

        Rules from SKILL.md Step 2 — SHORT_Score:
          Technical overbought: +2 RSI(1h)>70, +1 RSI(1h)60-70, +1 RSI(15m)>70, +1 RSI(15m)>RSI(1h)
          Price rise: +1 24h涨>5%, +2 24h涨>10%
          Position: +1 BB%>0.8, +1 price in upper 20% of 8h range
          Trend filter: +1 price<EMA200; 0 if price>EMA200 AND RSI<60
          Volume: +1 vol_ratio>1.5 AND 24h%>0
        """
        score = 0
        rsi_1h = ind["rsi_1h"]
        rsi_15m = ind["rsi_15m"]
        change_24h = ind["change_24h"]
        bb_pct = ind["bb_pct"]
        price = ind["price"]
        ema200 = ind["ema200"]
        vol_ratio = ind["vol_ratio"]
        price_in_8h_pct = ind["price_in_8h_pct"]

        # Technical overbought
        if rsi_1h > 70:
            score += 2
        elif 60 <= rsi_1h <= 70:
            score += 1
        if rsi_15m > 70:
            score += 1
        if rsi_15m > rsi_1h:
            score += 1

        # Price rise
        if change_24h > 10:
            score += 2
        elif change_24h > 5:
            score += 1

        # Position confirmation
        if bb_pct > 0.8:
            score += 1
        if price_in_8h_pct > 0.8:
            score += 1

        # Trend filter
        if price < ema200:
            score += 1
        if price > ema200 and rsi_1h < 60:
            score = 0  # 上升趋势中非超买 → 不做空

        # Volume confirmation
        if vol_ratio > 1.5 and change_24h > 0:
            score += 1

        return score

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    @staticmethod
    def rank(
        scored: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        """Rank scored symbols.

        1. Filter: volume_24h < 1M USDT → skip
        2. Filter: score < MIN_SCORE → skip
        3. Sort by score desc, then RSI extremity desc
        4. Separate into rankings (score ≥ 5) and watchlist (score 3-4)

        Returns:
            (rankings, watchlist, filtered_count)
        """
        filtered_count = 0

        # Filter by volume and score
        valid: list[dict[str, Any]] = []
        for s in scored:
            vol = s.get("volume_24h", 0) or 0
            if vol < MIN_VOLUME_24H_USDT:
                filtered_count += 1
                continue
            if s["score"] < MIN_SCORE:
                filtered_count += 1
                continue
            valid.append(s)

        # Sort: score desc, then RSI extremity desc
        valid.sort(key=lambda x: (x["score"], x["rsi_extremity"]), reverse=True)

        rankings: list[dict[str, Any]] = []
        watchlist: list[dict[str, Any]] = []
        for s in valid:
            if s["score"] >= 5:
                rankings.append(s)
            else:
                watchlist.append(s)

        return rankings, watchlist, filtered_count
