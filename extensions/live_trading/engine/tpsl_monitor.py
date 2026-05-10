"""TP/SL Monitor — background daemon for take-profit and stop-loss execution.

Polls prices via exchange.get_tickers(), checks active positions against
TP/SL thresholds, and executes orders. Supports trailing stop and 3x retry
on SL execution failure.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
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

        # 从持久化状态恢复 trailing stop
        if hasattr(self._positions, "get_trailing_state"):
            try:
                state = self._positions.get_trailing_state()
                if state["trailing_stops"]:
                    self._trailing_stops = state["trailing_stops"]
                    self._peak_prices = state["peak_prices"]
                    logger.info("Restored trailing stops for %d symbols", len(self._trailing_stops))
            except Exception as exc:
                logger.warning("Failed to restore trailing state: %s", exc)

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


        # 持久化 trailing stop 状态
        if hasattr(self._positions, "set_trailing_state"):
            try:
                self._positions.set_trailing_state(self._trailing_stops, self._peak_prices)
            except Exception as exc:
                logger.debug("Failed to persist trailing state: %s", exc)
    def _check_take_profit(self, pos: Position, price: float) -> bool:
        """Check if TP level is reached — time-decaying minimal_roi style.

        The longer the position is held, the lower the TP threshold:
          < 30 min : +5%
          30-60 min : +3%
          60-120 min: +1%
          > 120 min : +0.1% (保本附近)

        If a fixed take_profit is set, uses the tighter of the two.
        """
        elapsed = time.time() - datetime.fromisoformat(pos.opened_at).timestamp()
        elapsed_min = elapsed / 60

        if elapsed_min < 30:
            tp_pct = 0.05
        elif elapsed_min < 60:
            tp_pct = 0.03
        elif elapsed_min < 120:
            tp_pct = 0.01
        else:
            tp_pct = 0.001

        # If fixed TP is set, use the tighter (lower) threshold
        if pos.take_profit is not None:
            if pos.direction == "LONG":
                fixed_pct = (pos.take_profit - pos.entry_price) / pos.entry_price
            else:
                fixed_pct = (pos.entry_price - pos.take_profit) / pos.entry_price
            tp_pct = min(tp_pct, fixed_pct)

        if pos.direction == "LONG":
            return price >= pos.entry_price * (1 + tp_pct)
        return price <= pos.entry_price * (1 - tp_pct)

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
                order = self._exchange.create_market_order(pos.symbol, side, pos.quantity, reduce_only=True)
                filled = order.get("filled", 0) or 0
                if filled < pos.quantity:
                    remaining = pos.quantity - filled
                    logger.warning(
                        "TP partial fill for %s: filled=%.4f of %.4f, remaining=%.4f",
                        pos.symbol, filled, pos.quantity, remaining,
                    )
                    self._positions.reduce_position(pos.symbol, filled or pos.quantity, price)
                    # If remaining is below exchange minQty, close in tracker (dust)
                    min_qty = self._exchange.get_min_qty(pos.symbol)
                    if min_qty > 0 and remaining < min_qty:
                        self._positions.close_position(pos.symbol, exit_price=price, reason="TP")
                        logger.info(
                            "TP dust cleanup: %s remaining=%.6f below minQty=%.6f, closed in tracker",
                            pos.symbol, remaining, min_qty,
                        )
                else:
                    self._positions.close_position(pos.symbol, exit_price=price, reason="TP")
                self._trailing_stops.pop(pos.symbol, None)
                self._peak_prices.pop(pos.symbol, None)
                if self._on_take_profit:
                    self._on_take_profit(pos)
                return
            except Exception as exc:
                err_msg = str(exc)
                # If quantity rounds to zero (below minimum tradeable), close as dust and give up
                if "below minimum tradeable" in err_msg or "Quantity less than or equal to zero" in err_msg:
                    logger.warning(
                        "TP dust: %s remaining qty=%.6f untradeable, closing in tracker",
                        pos.symbol, pos.quantity,
                    )
                    self._positions.close_position(pos.symbol, exit_price=price, reason="TP")
                    self._trailing_stops.pop(pos.symbol, None)
                    self._peak_prices.pop(pos.symbol, None)
                    return
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
        """Execute stop-loss: market order to close position with 3x retry.

        Uses market order (not STOP_LOSS) because the SL condition has already
        been triggered — we need immediate execution, not a conditional algo order.
        Binance spot API does not support STOP_LOSS on /api/v3/order.
        """
        side = "sell" if pos.direction == "LONG" else "buy"
        detail = f"SL triggered: {pos.symbol} {pos.direction} @ {price:.4f} (SL={effective_sl:.4f})"
        logger.info(detail)

        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                order = self._exchange.create_market_order(pos.symbol, side, pos.quantity, reduce_only=True)
                filled = order.get("filled", 0) or 0
                if filled < pos.quantity:
                    remaining = pos.quantity - filled
                    logger.warning(
                        "SL partial fill for %s: filled=%.4f of %.4f, remaining=%.4f",
                        pos.symbol, filled, pos.quantity, remaining,
                    )
                    self._positions.reduce_position(pos.symbol, filled or pos.quantity, price)
                    # If remaining is below exchange minQty, close in tracker (dust)
                    min_qty = self._exchange.get_min_qty(pos.symbol)
                    if min_qty > 0 and remaining < min_qty:
                        self._positions.close_position(pos.symbol, exit_price=price, reason="SL")
                        logger.info(
                            "SL dust cleanup: %s remaining=%.6f below minQty=%.6f, closed in tracker",
                            pos.symbol, remaining, min_qty,
                        )
                else:
                    self._positions.close_position(pos.symbol, exit_price=price, reason="SL")
                self._trailing_stops.pop(pos.symbol, None)
                self._peak_prices.pop(pos.symbol, None)
                if self._on_stop_loss:
                    self._on_stop_loss(pos)
                return
            except Exception as exc:
                err_msg = str(exc)
                if "below minimum tradeable" in err_msg or "Quantity less than or equal to zero" in err_msg:
                    logger.warning(
                        "SL dust: %s remaining qty=%.6f untradeable, closing in tracker",
                        pos.symbol, pos.quantity,
                    )
                    self._positions.close_position(pos.symbol, exit_price=price, reason="SL")
                    self._trailing_stops.pop(pos.symbol, None)
                    self._peak_prices.pop(pos.symbol, None)
                    return
                last_err = exc
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BACKOFF[attempt]
                    logger.warning("SL retry %d/%d in %.1fs: %s", attempt + 1, MAX_RETRIES, delay, exc)
                    time.sleep(delay)

        logger.error("SL execution failed after %d attempts for %s: %s", MAX_RETRIES, pos.symbol, last_err)
        if self._on_error:
            assert last_err is not None
            self._on_error(last_err)
