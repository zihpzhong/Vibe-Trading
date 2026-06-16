"""T+1 aware TP/SL monitor for A-shares."""

from __future__ import annotations

import logging
import time
from threading import Event, Thread
from typing import Callable, Optional

from extensions.trading.astock.config import AStockTradingConfig
from extensions.trading.astock.live.exchange import AStockExchangeBase
from extensions.trading.astock.live.position import AStockPositionTracker
from extensions.trading.astock.live.scheduler import trading_session

logger = logging.getLogger(__name__)


class AStockTPSLMonitor(Thread):
    def __init__(
        self,
        exchange: AStockExchangeBase,
        positions: AStockPositionTracker,
        config: Optional[AStockTradingConfig] = None,
        poll_interval: float = 30.0,
        on_close: Optional[Callable] = None,
    ) -> None:
        super().__init__(daemon=True, name="AStockTPSLMonitor")
        self._exchange = exchange
        self._positions = positions
        self._config = config or AStockTradingConfig()
        self._poll_interval = poll_interval
        self._on_close = on_close
        self._stop = Event()
        self._de_risk = self._config.de_risk

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            session = trading_session()
            if session in ("morning", "afternoon", "tail"):
                self._check_all(session)
            time.sleep(self._poll_interval)

    def _check_all(self, session: str) -> None:
        for pos in self._positions.get_active_positions():
            if pos.is_today_buy:
                continue
            try:
                ticker = self._exchange.get_ticker(pos.symbol)
            except Exception:
                continue
            last = float(ticker.get("last", 0))
            if last <= 0:
                continue
            pnl_pct = (last / pos.entry_price - 1) * 100

            if last <= pos.stop_loss:
                self._sell(pos.symbol, last, pos.shares, "stop_loss")
                continue
            if pos.take_profit and last >= pos.take_profit:
                self._sell(pos.symbol, last, pos.shares, "take_profit")
                continue

            if session == "tail":
                if pnl_pct <= -self._de_risk.tail_loss_full_pct:
                    self._sell(pos.symbol, last, pos.shares, "tail_full_exit")
                elif pnl_pct <= -self._de_risk.tail_loss_trim_pct:
                    trim = max(100, int(pos.shares * self._de_risk.tail_loss_trim_fraction) // 100 * 100)
                    if trim < pos.shares:
                        self._sell(pos.symbol, last, trim, "tail_trim")

    def _sell(self, symbol: str, price: float, shares: int, reason: str) -> None:
        result = self._exchange.create_sell_order(symbol, price, shares)
        if result.get("status") == "filled":
            self._positions.close_position(symbol, price, int(result.get("filled", shares)), reason, self._config.fees)
            if self._on_close:
                self._on_close(symbol, reason)
            logger.info("TPSL %s %s shares=%s", reason, symbol, shares)
        elif result.get("reason") == "t_plus_1":
            logger.debug("TPSL skip T+1 %s", symbol)
