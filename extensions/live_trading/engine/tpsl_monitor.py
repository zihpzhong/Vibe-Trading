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
from typing import Any, Callable, Optional

from .exchange import ExchangeBase
from .position_tracker import Position, PositionTracker
from extensions.live_trading.config import DCAConfig, DeRiskConfig

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
        de_risk_config: Optional[DeRiskConfig] = None,
        dca_config: Optional[DCAConfig] = None,
        dca_gate_engine: Optional[Any] = None,
        max_leverage: int = 5,
        position_size_pct: float = 0.05,
        status_log_interval: float = 60.0,
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
        self._dca_config = dca_config
        self._dca_gate_engine = dca_gate_engine
        self._max_leverage = max_leverage
        self._position_size_pct = position_size_pct
        self._status_poll_counter = 0
        self._status_poll_threshold = max(1, int(status_log_interval / poll_interval))
        # Track trailing stop levels per position (symbol → adjusted_stop_loss)
        self._trailing_stops: dict[str, float] = {}
        # Track peak prices for trailing stop calculation
        self._peak_prices: dict[str, float] = {}
        # De-risk config
        dr = de_risk_config or DeRiskConfig()
        self._de_risk_levels = [
            (dr.level1_loss_pct, dr.level1_sell_fraction),  # 0 → level 1
            (dr.level2_loss_pct, dr.level2_sell_fraction),  # 1 → level 2
            (dr.level3_loss_pct, dr.level3_sell_fraction),  # 2 → level 3
        ]
        self._doom_loss_pct = dr.doom_loss_pct

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

            self._update_trailing_stop(pos, current_price)            # 1. Trailing stop
            effective_sl = self._trailing_stops.get(pos.symbol, pos.stop_loss)

            if self._check_stop_loss(pos, current_price, effective_sl):  # 2. SL hard guard before DCA
                self._execute_sl(pos, current_price, effective_sl)
                continue

            self._check_dca(pos, current_price)                       # 3. DCA when losing

            if self._check_take_profit(pos, current_price):           # 4. TP
                self._execute_tp(pos, current_price)
            elif self._check_de_risk(pos, current_price):             # 5. De-risk partial exit
                pass

        # 定时持仓状态日志
        self._status_poll_counter += 1
        if self._status_poll_counter >= self._status_poll_threshold:
            self._status_poll_counter = 0
            self._log_position_status(prices)

        # 持久化 trailing stop 状态
        if hasattr(self._positions, "set_trailing_state"):
            try:
                self._positions.set_trailing_state(self._trailing_stops, self._peak_prices)
            except Exception as exc:
                logger.debug("Failed to persist trailing state: %s", exc)

    # ------------------------------------------------------------------
    # Periodic status logging
    # ------------------------------------------------------------------

    def _log_position_status(self, prices: dict[str, float]) -> None:
        """Log a concise status of all active positions to stdout.

        Called periodically (default every 60s) to provide real-time
        visibility into position PnL, SL distances, and trailing stop status.
        """
        active = self._positions.get_active_positions()
        if not active:
            return

        lines: list[str] = []
        total_floating = 0.0
        for pos in active:
            price = prices.get(pos.symbol, 0)
            if price <= 0:
                continue

            if pos.direction == "LONG":
                pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
            else:
                pnl_pct = (pos.entry_price - price) / pos.entry_price * 100
            pnl_usdt = pnl_pct / 100 * pos.entry_price * pos.quantity
            total_floating += pnl_usdt

            effective_sl = self._trailing_stops.get(pos.symbol, pos.stop_loss)
            if pos.direction == "LONG":
                sl_dist = (price - effective_sl) / effective_sl * 100
            else:
                sl_dist = (effective_sl - price) / effective_sl * 100
            trailing_tag = " T" if pos.symbol in self._trailing_stops else ""
            dca_tag = f" DCA{pos.dca_count}" if pos.dca_count > 0 else ""

            lines.append(
                f"  {pos.symbol:12s} {pos.direction:5s} "
                f"entry={pos.entry_price:.4f} cur={price:.4f} "
                f"PnL={pnl_pct:+.2f}% ({pnl_usdt:+.3f}USDT)"
                f" SL距={sl_dist:.1f}%{trailing_tag}{dca_tag}"
            )

        logger.info(
            "TPSL持仓状态 (%d active, 浮动合计: %+.3f USDT):\n%s",
            len(active), total_floating, "\n".join(lines),
        )

    # ------------------------------------------------------------------
    # Threshold checking
    # ------------------------------------------------------------------

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

        thresholds = [tp_pct]
        if pos.take_profit is not None:
            if pos.direction == "LONG":
                fixed_tp_pct = (pos.take_profit - pos.entry_price) / pos.entry_price
            else:
                fixed_tp_pct = (pos.entry_price - pos.take_profit) / pos.entry_price
            if fixed_tp_pct > 0:
                thresholds.append(fixed_tp_pct)
        tp_pct = min(thresholds)

        if pos.direction == "LONG":
            return price >= pos.entry_price * (1 + tp_pct)
        return price <= pos.entry_price * (1 - tp_pct)

    def _check_stop_loss(self, pos: Position, price: float, effective_sl: float) -> bool:
        """Check if SL level is breached."""
        if pos.direction == "LONG":
            return price <= effective_sl
        return price >= effective_sl

    # ------------------------------------------------------------------
    # DCA (阶梯加仓)
    # ------------------------------------------------------------------

    def _check_dca(self, pos: Position, current_price: float) -> bool:
        """Check and execute DCA if conditions are met.

        DCA trigger threshold references ``first_entry_cost`` (not average
        entry_price), consistent with de-risk, so triggers do not drift
        after previous DCA adds.

        Returns:
            True if a DCA order was executed, False otherwise.
        """
        cfg = self._dca_config
        if cfg is None or not cfg.enabled:
            return False
        if pos.dca_count >= cfg.max_dca_count:
            return False

        # 亏损计算（参照 first_entry_cost）
        if pos.direction == "LONG":
            loss_pct = (current_price - pos.first_entry_cost) / pos.first_entry_cost
        else:
            loss_pct = (pos.first_entry_cost - current_price) / pos.first_entry_cost
        if loss_pct >= 0:
            return False

        abs_loss_pct = abs(loss_pct) * 100
        if abs_loss_pct < cfg.trigger_loss_pct:
            return False

        # 账户级累计亏损检查
        if pos.direction == "LONG":
            total_unrealized = (current_price - pos.entry_price) * pos.quantity
        else:
            total_unrealized = (pos.entry_price - current_price) * pos.quantity
        loss_pct_of_account = abs(total_unrealized) / self._positions.account_balance
        if loss_pct_of_account > cfg.max_account_loss_pct / 100:
            logger.warning(
                "DCA SKIP %s: cumulative loss %.2f USDT (%.1f%% account) > %.0f%%",
                pos.symbol, total_unrealized, loss_pct_of_account * 100, cfg.max_account_loss_pct,
            )
            return False

        # 暴露率检查
        current_exposure = self._positions.get_exposure({pos.symbol: current_price}, include_unrealized=True)
        dca_mult = cfg.dca_multipliers[min(pos.dca_count, len(cfg.dca_multipliers) - 1)]
        max_exposure = self._positions.max_exposure_pct
        dca_lev = max(1, self._max_leverage // 2) if cfg.dca_leverage_halved else self._max_leverage
        dca_notional = (self._positions.account_balance * self._position_size_pct) * dca_mult * dca_lev
        post_dca_exposure = current_exposure + (dca_notional / self._positions.account_balance)
        if post_dca_exposure > max_exposure:
            logger.warning(
                "DCA SKIP %s: post-DCA exposure %.1f%% > %.0f%% cap",
                pos.symbol, post_dca_exposure * 100, max_exposure * 100,
            )
            return False

        # 单次不超账户 50%
        if dca_notional > self._positions.account_balance * 0.5:
            return False

        dca_qty = dca_notional / current_price
        if dca_qty <= 0:
            return False

        # Binance $20 最小名义价值
        if dca_notional < cfg.dca_min_notional_usdt:
            logger.warning(
                "DCA SKIP %s: notional $%.2f < $%.0f min",
                pos.symbol, dca_notional, cfg.dca_min_notional_usdt,
            )
            return False

        if self._dca_gate_engine is not None and not self._check_dca_gate(pos, dca_qty, current_price, dca_notional, dca_lev):
            return False

        self._execute_dca(pos, dca_qty, current_price)
        return True

    def _check_dca_gate(
        self,
        pos: Position,
        dca_qty: float,
        current_price: float,
        dca_notional: float,
        dca_lev: int,
    ) -> bool:
        """Re-run a simplified Execution Gate before adding to a losing position."""
        try:
            ticker = self._exchange.get_ticker(pos.symbol)
        except Exception as exc:
            logger.warning("DCA SKIP %s: ticker unavailable before DCA gate: %s", pos.symbol, exc)
            return False

        funding_rate: Optional[float]
        try:
            funding_rate = self._exchange.get_funding_rate(pos.symbol)
        except Exception as exc:
            funding_rate = None
            logger.warning("DCA SKIP %s: funding unavailable before DCA gate: %s", pos.symbol, exc)

        try:
            orderbook = self._exchange.get_orderbook(pos.symbol, 10)
        except Exception as exc:
            logger.warning("DCA SKIP %s: orderbook unavailable before DCA gate: %s", pos.symbol, exc)
            return False

        from extensions.live_trading.models import GateStatus, LiveSignal, SignalDirection

        direction = SignalDirection(pos.direction)
        targets = [pos.take_profit] if pos.take_profit else []
        signal = LiveSignal(
            symbol=pos.symbol,
            direction=direction,
            score=pos.entry_score,
            entry_price=current_price,
            stop_loss=pos.stop_loss,
            target_prices=targets,
        )
        result = self._dca_gate_engine.run_gate(
            signal,
            ticker=ticker,
            funding_rate=funding_rate,
            orderbook=orderbook,
            order_qty=dca_qty,
            account_balance=self._positions.account_balance,
            order_margin=dca_notional / max(dca_lev, 1),
        )
        if result.status != GateStatus.PASS:
            logger.warning("DCA SKIP %s: gate=%s %s", pos.symbol, result.status.value, result.summary)
            return False
        return True

    def _execute_dca(self, pos: Position, dca_qty: float, price: float) -> None:
        """Execute a DCA market order and update position."""
        dca_lev = max(1, self._max_leverage // 2) if self._dca_config.dca_leverage_halved else self._max_leverage
        logger.info(
            "DCA %s %s +%.4f @ %.4f (loss %.1f%%)",
            pos.symbol, pos.direction, dca_qty, price,
            (price - pos.first_entry_cost) / pos.first_entry_cost * 100,
        )
        try:
            if hasattr(self._exchange, "set_leverage"):
                self._exchange.set_leverage(pos.symbol, leverage=dca_lev)
            order = self._exchange.create_market_order(pos.symbol, pos.direction.lower(), dca_qty)
            logger.info("DCA order filled: %s", order)
            self._positions.adjust_position(pos.symbol, dca_qty, price)
        except Exception as exc:
            logger.warning("DCA execution failed for %s: %s", pos.symbol, exc)

    # ------------------------------------------------------------------
    # De-risk (NFI 风格分级减仓)
    # ------------------------------------------------------------------

    def _check_de_risk(self, pos: Position, current_price: float) -> bool:
        """Check and execute de-risk levels.

        De-risk thresholds are always referenced to ``first_entry_cost``
        (not average entry_price), making them invariant across DCA events.

        Returns:
            True if a de-risk partial exit or doom was actually executed.
            False if no action was taken (e.g. position in profit, or partial
            exit below Binance $20 minimum notional).
        """
        first_cost = pos.first_entry_cost
        if first_cost <= 0:
            return False

        # 计算亏损百分比（相对于首次入场成本）
        if pos.direction == "LONG":
            loss_pct = (current_price - first_cost) / first_cost * 100
        else:
            loss_pct = (first_cost - current_price) / first_cost * 100

        # 盈利时不触发 de-risk
        if loss_pct >= 0:
            return False

        abs_loss = abs(loss_pct)

        # 从当前 de_risk_level 开始检查后续级别（不重复触发已触发的级别）
        start_idx = max(0, pos.de_risk_level)  # de_risk_level: 0=none, 1=level1...
        if start_idx > 0 and start_idx <= len(self._de_risk_levels):
            start_idx = min(start_idx, len(self._de_risk_levels))

        for i in range(start_idx, len(self._de_risk_levels)):
            threshold_pct, sell_fraction = self._de_risk_levels[i]
            if abs_loss >= threshold_pct:
                level_number = i + 1
                sell_qty = pos.quantity * sell_fraction
                if self._execute_de_risk(pos, sell_qty, current_price, level_number):
                    return True
                # 跳过此级别（如 < $20 最小限额），继续检查下一级
                continue

        # Doom stop
        if abs_loss >= self._doom_loss_pct:
            logger.info(
                "DOOM triggered for %s: loss=%.1f%% >= %.1f%%, closing all",
                pos.symbol, abs_loss, self._doom_loss_pct,
            )
            side = "sell" if pos.direction == "LONG" else "buy"
            self._execute_order_with_retry(
                pos, side, pos.quantity, current_price,
                reason="DOOM", is_de_risk=True, de_risk_level=4,
            )
            return True

        return False

    def _execute_de_risk(self, pos: Position, sell_qty: float, current_price: float, level: int) -> bool:
        """Execute a de-risk partial sell order.

        Returns:
            True if the order was actually placed, False if skipped
            (e.g. below Binance $20 minimum notional).
        """
        if sell_qty <= 0:
            return False

        # Binance 最小名义价值检查
        notional = sell_qty * current_price
        if notional < 20:
            logger.warning(
                "DE-RISK level %d SKIP for %s: partial exit notional $%.2f < $20 min",
                level, pos.symbol, notional,
            )
            return False

        side = "sell" if pos.direction == "LONG" else "buy"
        self._execute_order_with_retry(
            pos, side, sell_qty, current_price,
            reason=f"DE_RISK_{level}", is_de_risk=True, de_risk_level=level,
        )
        return True

    def _execute_order_with_retry(
        self, pos: Position, side: str, qty: float, price: float,
        reason: str, is_de_risk: bool = False, de_risk_level: int = 0,
    ) -> None:
        """Execute a market order with 3x retry, then update tracker state.

        Args:
            pos: The position object.
            side: "buy" or "sell".
            qty: Quantity to trade.
            price: Current market price (for tracker update).
            reason: Close reason ("TP", "SL", "DOOM", "DE_RISK_1", etc.).
            is_de_risk: If True, call de_risk_partial_exit instead of close.
            de_risk_level: De-risk level (1-4), only used when is_de_risk=True.
        """
        logger.info(
            "%s for %s %s @ %.4f qty=%.4f",
            reason, pos.symbol, pos.direction, price, qty,
        )

        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                order = self._exchange.create_market_order(pos.symbol, side, qty, reduce_only=True)
                filled = order.get("filled", 0) or 0

                if is_de_risk and de_risk_level < 4:
                    # De-risk partial exit
                    if filled > 0:
                        self._positions.de_risk_partial_exit(
                            pos.symbol, de_risk_level, filled, price,
                        )
                    if filled < qty:
                        remaining = qty - filled
                        if remaining > 0:
                            min_qty = self._exchange.get_min_qty(pos.symbol)
                            if min_qty > 0 and remaining < min_qty:
                                self._positions.de_risk_partial_exit(
                                    pos.symbol, de_risk_level, remaining, price,
                                )
                else:
                    # Full close (TP, SL, DOOM, partial fill cleanup)
                    if filled < qty:
                        remaining = qty - filled
                        logger.warning(
                            "%s partial fill for %s: filled=%.4f of %.4f",
                            reason, pos.symbol, filled, qty,
                        )
                        self._positions.reduce_position(pos.symbol, filled or qty, price)
                        min_qty = self._exchange.get_min_qty(pos.symbol)
                        if min_qty > 0 and remaining < min_qty:
                            self._positions.close_position(pos.symbol, exit_price=price, reason=reason)
                    else:
                        self._positions.close_position(pos.symbol, exit_price=price, reason=reason)

                self._trailing_stops.pop(pos.symbol, None)
                self._peak_prices.pop(pos.symbol, None)

                if reason == "TP" and self._on_take_profit:
                    self._on_take_profit(pos)
                elif reason in ("SL", "DOOM") and self._on_stop_loss:
                    self._on_stop_loss(pos)
                return
            except Exception as exc:
                err_msg = str(exc)
                if "below minimum tradeable" in err_msg or "Quantity less than or equal to zero" in err_msg:
                    logger.warning(
                        "%s dust: %s qty=%.6f untradeable, closing in tracker",
                        reason, pos.symbol, qty,
                    )
                    if is_de_risk:
                        self._positions.de_risk_partial_exit(pos.symbol, de_risk_level, qty, price)
                    else:
                        self._positions.close_position(pos.symbol, exit_price=price, reason=reason)
                    return
                last_err = exc
                if attempt < MAX_RETRIES - 1:
                    delay = RETRY_BACKOFF[attempt]
                    logger.warning("%s retry %d/%d in %.1fs: %s", reason, attempt + 1, MAX_RETRIES, delay, exc)
                    time.sleep(delay)

        logger.error("%s failed after %d attempts for %s: %s", reason, MAX_RETRIES, pos.symbol, last_err)
        if self._on_error:
            assert last_err is not None
            self._on_error(last_err)

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
        logger.info(
            "SL triggered: %s %s @ %.4f (SL=%.4f)",
            pos.symbol, pos.direction, price, effective_sl,
        )

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
