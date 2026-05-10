"""TP/SL Monitor — background daemon for take-profit and stop-loss execution.

Polls prices via exchange.get_tickers(), checks active positions against
TP/SL thresholds, and executes orders. Supports trailing stop and 3x retry
on SL execution failure.
"""

from __future__ import annotations

import logging
import time
from threading import Event, Thread
from typing import Callable, Optional

from .exchange import ExchangeBase
from .position_tracker import Position, PositionTracker

logger = logging.getLogger(__name__)

RETRY_BACKOFF = (1, 2, 4)
MAX_RETRIES = 3


class TPSLMonitor(Thread):
    """Background thread that monitors positions and executes TP/SL orders.

    Usage:
        monitor = TPSLMonitor(exchange, positions, poll_interval=5.0)
        monitor.start()
        # ... later ...
        monitor.stop()

    Config:
        trailing_activation_pct: Profit % at which trailing stop activates (default 3.0).
        trail_distance_pct: Distance to trail behind current price (default 1.5).
    """

    def __init__(
        self,
        exchange: ExchangeBase,
        positions: PositionTracker,
        poll_interval: float = 5.0,
        trailing_activation_pct: float = 3.0,
        trail_distance_pct: float = 1.5,
        on_take_profit: Optional[Callable[[Position], None]] = None,
        on_stop_loss: Optional[Callable[[Position], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        super().__init__(daemon=True, name="TPSLMonitor")
        self._exchange = exchange
        self._positions = positions
        self._poll_interval = poll_interval
        self._trailing_activation_pct = trailing_activation_pct
        self._trail_distance_pct = trail_distance_pct
        self._on_take_profit = on_take_profit
        self._on_stop_loss = on_stop_loss
        self._on_error = on_error
        self._stop_event = Event()
        # Track trailing stop levels per position (symbol → adjusted_stop_loss)
        self._trailing_stops: dict[str, float] = {}
        # Track peak prices for trailing stop calculation
        self._peak_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main monitoring loop."""
        logger.info("TPSL Monitor started (interval=%.1fs)", self._poll_interval)
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as exc:
                logger.error("TPSL Monitor poll error: %s", exc)
                if self._on_error:
                    self._on_error(exc)
            self._stop_event.wait(self._poll_interval)
        logger.info("TPSL Monitor stopped")

    def stop(self, timeout: float = 10.0) -> None:
        """Signal stop and wait for thread to finish."""
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=timeout)
            if self.is_alive():
                logger.warning("TPSL Monitor did not stop within %.1fs", timeout)

    # ------------------------------------------------------------------
    # Polling logic
    # ------------------------------------------------------------------

    def _poll(self) -> None:
        """Single poll cycle: fetch prices, check TP/SL, execute orders."""
        active = self._positions.get_active_positions()
        if not active:
            return

        # Batch fetch prices for all active symbols
        symbols = [p.symbol for p in active]
        try:
            tickers = self._exchange.get_tickers(symbols)
        except Exception as exc:
            logger.warning("get_tickers failed in TPSL Monitor: %s", exc)
            return

        prices: dict[str, float] = {}
        for t in tickers:
            prices[t["symbol"]] = float(t.get("last", 0))

        for pos in active:
            current_price = prices.get(pos.symbol, 0)
            if current_price <= 0:
                continue

            self._update_trailing_stop(pos, current_price)
            effective_sl = self._trailing_stops.get(pos.symbol, pos.stop_loss)

            if self._check_take_profit(pos, current_price):
                self._execute_tp(pos, current_price)
            elif self._check_stop_loss(pos, current_price, effective_sl):
                self._execute_sl(pos, current_price, effective_sl)

    # ------------------------------------------------------------------
    # Threshold checking
    # ------------------------------------------------------------------

    def _check_take_profit(self, pos: Position, price: float) -> bool:
        """Check if TP level is reached."""
        if pos.take_profit is None:
            return False
        if pos.direction == "LONG":
            return price >= pos.take_profit
        return price <= pos.take_profit

    def _check_stop_loss(self, pos: Position, price: float, effective_sl: float) -> bool:
        """Check if SL level is breached."""
        if pos.direction == "LONG":
            return price <= effective_sl
        return price >= effective_sl

    # ------------------------------------------------------------------
    # Trailing stop
    # ------------------------------------------------------------------

    def _update_trailing_stop(self, pos: Position, current_price: float) -> None:
        """Update trailing stop if activation threshold reached.

        Trailing stop activates after profit exceeds activation_pct.
        Once active, stop_loss trails behind the peak price by trail_distance_pct.
        Stop only moves in the favorable direction (never retreats).
        """
        if pos.direction == "LONG":
            pnl_pct = (current_price - pos.entry_price) / pos.entry_price * 100
            # Activate trailing stop when profit exceeds threshold
            if pnl_pct >= self._trailing_activation_pct:
                # Track peak price
                peak = self._peak_prices.get(pos.symbol, current_price)
                if current_price > peak:
                    peak = current_price
                    self._peak_prices[pos.symbol] = peak
                # Trailing stop = peak price - trail_distance%
                trail_sl = peak * (1 - self._trail_distance_pct / 100)
                current_sl = self._trailing_stops.get(pos.symbol, pos.stop_loss)
                # Only move up (never retreat)
                if trail_sl > current_sl:
                    self._trailing_stops[pos.symbol] = trail_sl
        else:  # SHORT
            pnl_pct = (pos.entry_price - current_price) / pos.entry_price * 100
            if pnl_pct >= self._trailing_activation_pct:
                peak = self._peak_prices.get(pos.symbol, current_price)
                if current_price < peak:
                    peak = current_price
                    self._peak_prices[pos.symbol] = peak
                trail_sl = peak * (1 + self._trail_distance_pct / 100)
                current_sl = self._trailing_stops.get(pos.symbol, pos.stop_loss)
                # Only move down (never retreat)
                if trail_sl < current_sl:
                    self._trailing_stops[pos.symbol] = trail_sl

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def _execute_tp(self, pos: Position, price: float) -> None:
        """Execute take-profit: market order to close position with retry."""
        side = "sell" if pos.direction == "LONG" else "buy"
        logger.info("TP triggered: %s %s @ %.4f (TP=%.4f)", pos.symbol, pos.direction, price, pos.take_profit)

        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                self._exchange.create_market_order(pos.symbol, side, pos.quantity)
                self._positions.close_position(pos.symbol)
                self._trailing_stops.pop(pos.symbol, None)
                self._peak_prices.pop(pos.symbol, None)
                if self._on_take_profit:
                    self._on_take_profit(pos)
                return
            except Exception as exc:
                last_err = exc
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BACKOFF[attempt]
                    logger.warning("TP retry %d/%d in %.1fs: %s", attempt + 1, MAX_RETRIES, delay, exc)
                    time.sleep(delay)

        logger.error("TP execution failed after %d attempts for %s: %s", MAX_RETRIES, pos.symbol, last_err)
        if self._on_error:
            assert last_err is not None
            self._on_error(last_err)

    def _execute_sl(self, pos: Position, price: float, effective_sl: float) -> None:
        """Execute stop-loss: STOP_MARKET order with 3x retry."""
        side = "sell" if pos.direction == "LONG" else "buy"
        detail = f"SL triggered: {pos.symbol} {pos.direction} @ {price:.4f} (SL={effective_sl:.4f})"
        logger.info(detail)

        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                self._exchange.create_stop_loss_order(pos.symbol, side, pos.quantity, effective_sl)
                self._positions.close_position(pos.symbol)
                self._trailing_stops.pop(pos.symbol, None)
                self._peak_prices.pop(pos.symbol, None)
                if self._on_stop_loss:
                    self._on_stop_loss(pos)
                return
            except Exception as exc:
                last_err = exc
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BACKOFF[attempt]
                    logger.warning("SL retry %d/%d in %.1fs: %s", attempt + 1, MAX_RETRIES, delay, exc)
                    time.sleep(delay)

        logger.error("SL execution failed after %d attempts for %s: %s", MAX_RETRIES, pos.symbol, last_err)
        if self._on_error:
            assert last_err is not None
            self._on_error(last_err)
