"""A-share trading scheduler — session-aware scan pipeline."""

from __future__ import annotations

import logging
from datetime import datetime, time as dt_time
from typing import Optional
from zoneinfo import ZoneInfo

from extensions.live_trading.astock.conduction import check_market_conduction
from extensions.live_trading.astock.config import AStockTradingConfig
from extensions.live_trading.astock.exchange import AStockExchangeBase
from extensions.live_trading.astock.models import AStockPhase2Request, AStockScheduleReport, MarketConductionStatus
from extensions.live_trading.astock.position import AStockPositionTracker
from extensions.live_trading.astock.scanner import AStockScanner

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")

FAST_DIMS = ["技术面", "资金面", "风险", "板块", "形态"]
FULL_DIMS = FAST_DIMS + ["基本面", "政策面", "情绪", "筹码", "估值", "宏观", "机构", "事件"]


def trading_session(now: Optional[datetime] = None) -> str:
    """Return session label: premarket / morning / lunch / afternoon / tail / closed."""
    now = now or datetime.now(_CN_TZ)
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    if t < dt_time(9, 0):
        return "closed"
    if t < dt_time(9, 30):
        return "premarket"
    if t < dt_time(11, 30):
        return "morning"
    if t < dt_time(13, 0):
        return "lunch"
    if t < dt_time(14, 50):
        return "afternoon"
    if t < dt_time(15, 0):
        return "tail"
    return "closed"


class AStockScheduler:
    def __init__(
        self,
        exchange: AStockExchangeBase,
        positions: Optional[AStockPositionTracker] = None,
        config: Optional[AStockTradingConfig] = None,
        trading_enabled: bool = False,
    ) -> None:
        self._exchange = exchange
        self._positions = positions or AStockPositionTracker()
        self._config = config or AStockTradingConfig()
        self._scanner = AStockScanner(exchange, self._config)
        self._trading_enabled = trading_enabled

    def run_once(self, top_n: Optional[int] = None) -> AStockScheduleReport:
        top_n = top_n or self._config.scan_top_n
        session = trading_session()

        market_status = MarketConductionStatus.OK.value
        try:
            idx = self._exchange.get_market_index(self._config.market_index, 80)
            breadth = self._exchange.get_market_breadth()
            conduction = check_market_conduction(idx, breadth)
            market_status = conduction.value
        except Exception:
            logger.warning("market conduction failed", exc_info=True)

        if market_status == MarketConductionStatus.LOCK_ALL.value:
            return AStockScheduleReport(
                market_status=market_status,
                active_positions=self._positions.active_count,
                trading_enabled=self._trading_enabled,
                session=session,
            )

        universe = self._config.universe or getattr(self._exchange, "universe", [])
        scan = self._scanner.scan(top_n=top_n, universe=universe if universe else None)

        phase2: list[AStockPhase2Request] = []
        for r in scan.rankings:
            score = int(r["score"])
            if score < 5:
                continue
            tier = "fast_track" if score >= 7 else "enhanced"
            dims = FAST_DIMS if tier == "fast_track" else FULL_DIMS
            phase2.append(
                AStockPhase2Request(
                    symbol=r["symbol"],
                    name=r.get("name", r["symbol"]),
                    score=score,
                    tier=tier,
                    dims=dims,
                    entry_price=float(r.get("entry_price", 0)),
                )
            )

        return AStockScheduleReport(
            rankings=scan.rankings,
            phase2_requests=phase2,
            watchlist=scan.watchlist,
            market_status=market_status,
            active_positions=self._positions.active_count,
            trading_enabled=self._trading_enabled,
            scan_time_ms=scan.scan_time_ms,
            filtered_count=scan.filtered_count,
            session=session,
        )
