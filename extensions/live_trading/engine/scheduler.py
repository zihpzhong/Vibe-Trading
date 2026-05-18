"""Trading Scheduler — cron-like loop for auto-scanning and tiered decision.

Orchestrates: BTC conduction → Phase 1 scan → score-tiered Phase2Request generation.
Produces ScheduleReport consumed by Agent tools.
"""

from __future__ import annotations

import logging
from typing import Optional

from extensions.live_trading.models import Phase2Request, ScheduleReport

from .btc_conduction import check_btc_1h_trend, check_btc_conduction
from .exchange import ExchangeBase
from .market_scanner import MarketScanner
from .position_tracker import PositionTracker

logger = logging.getLogger(__name__)

# Phase 2 dimension sets per tier
FAST_TRACK_DIMS = ["dim1", "dim3", "dim5", "dim6", "dim7"]
"""High-score checks: technical, derivatives, volatility, microstructure, risk."""

ENHANCED_DIMS = ["dim1", "dim2", "dim3", "dim4", "dim5", "dim6", "dim7", "dim8"]
"""Medium-score checks: full skill coverage, including deterministic risk dimensions."""


class TradingScheduler:
    """Main scheduler that runs the scan→decide pipeline.

    Usage:
        scheduler = TradingScheduler(exchange, positions)
        report = scheduler.run_once()
        for req in report.phase2_requests:
            print(req.symbol, req.tier, req.dims)
    """

    def __init__(
        self,
        exchange: ExchangeBase,
        positions: Optional[PositionTracker] = None,
        trading_enabled: bool = False,
    ) -> None:
        self._exchange = exchange
        self._positions = positions or PositionTracker()
        self._trading_enabled = trading_enabled
        self._scanner = MarketScanner(exchange)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self, top_n: int = 20, whitelist: Optional[list[str]] = None) -> ScheduleReport:
        """Execute one full scan→decide cycle.

        1. BTC conduction check
        2. Phase 1 scan (MarketScanner)
        3. Tiered decision logic → Phase2Requests

        Returns:
            ScheduleReport with rankings, phase2_requests, watchlist, btc_status.
        """
        # Step 1: BTC conduction check — fetch 4h kline first
        try:
            btc_kline = self._exchange.get_kline("BTCUSDT", "4h", 60)
            btc_status = check_btc_conduction(btc_kline)
        except Exception:
            logger.warning("BTC conduction check failed, falling back to CONDUCTION_OK", exc_info=True)
            btc_status = "CONDUCTION_OK"

        # Step 1b: BTC 1h short-term trend check
        btc_1h_trend = "NEUTRAL"
        try:
            btc_1h_kline = self._exchange.get_kline("BTCUSDT", "1h", 50)
            btc_1h_trend = check_btc_1h_trend(btc_1h_kline)
        except Exception:
            pass

        if btc_status in ("LOCK_LONG", "LOCK_SHORT"):
            return ScheduleReport(
                rankings=[],
                phase2_requests=[],
                watchlist=[],
                btc_status=btc_status,
                btc_1h_trend=btc_1h_trend,
                active_positions=self._positions.active_count,
                trading_enabled=self._trading_enabled,
            )

        # Step 2: Phase 1 scan
        scan_result = self._scanner.scan(top_n=top_n, whitelist=whitelist)

        # Step 2b: Align scores with BTC 1h trend to prevent contradictory signals
        # WEAKNESS → cap LONG scores; STRENGTH → cap SHORT scores
        if btc_1h_trend in ("WEAKNESS", "STRENGTH"):
            capped = 0
            for r in scan_result.rankings:
                if btc_1h_trend == "WEAKNESS" and r["direction"] == "LONG" and r["score"] >= 6:
                    r["score"] = 5
                    capped += 1
                elif btc_1h_trend == "STRENGTH" and r["direction"] == "SHORT" and r["score"] >= 6:
                    r["score"] = 5
                    capped += 1
            if capped:
                logger.info("BTC %s → capped %d directional scores to 5", btc_1h_trend, capped)

        # Step 3: Tiered decisions
        phase2_requests: list[Phase2Request] = []
        if self._trading_enabled:
            # Rankings (score ≥ 5)
            for r in scan_result.rankings:
                req = self._score_to_request(r)
                if req:
                    phase2_requests.append(req)
            # Watchlist (score 3-4) — no Phase2Request
            # but included in report for Agent awareness

        return ScheduleReport(
            rankings=scan_result.rankings,
            phase2_requests=phase2_requests,
            watchlist=scan_result.watchlist,
            btc_status=btc_status,
            btc_1h_trend=btc_1h_trend,
            active_positions=self._positions.active_count,
            trading_enabled=self._trading_enabled,
            scan_time_ms=scan_result.scan_time_ms,
            filtered_count=scan_result.filtered_count,
        )

    # ------------------------------------------------------------------
    # Tiered decision logic
    # ------------------------------------------------------------------

    @staticmethod
    def _score_to_request(ranking: dict) -> Optional[Phase2Request]:
        """Convert a Phase 1 ranking entry into a Phase2Request based on score tier.

        Score ≥ 7 → fast_track (tech + derivatives + volatility + microstructure + risk)
        Score 5-6 → enhanced (all 8 dimensions)
        Score < 5  → None (watchlist only)
        """
        score = ranking.get("score", 0)
        if not isinstance(score, int) or score < 5:
            return None

        if score >= 7:
            tier = "fast_track"
            dims = list(FAST_TRACK_DIMS)
        else:
            tier = "enhanced"
            dims = list(ENHANCED_DIMS)

        return Phase2Request(
            symbol=str(ranking.get("symbol", "")),
            direction=str(ranking.get("direction", "LONG")),
            score=score,
            tier=tier,
            dims=dims,
            entry_price=float(ranking.get("entry_price", 0) or 0),
            change_24h=float(ranking.get("change_24h", 0)),
            rsi_1h=float(ranking.get("rsi_1h", 50)),
            price_tier=str(ranking.get("price_tier", "standard")),
        )

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @property
    def trading_enabled(self) -> bool:
        return self._trading_enabled

    @trading_enabled.setter
    def trading_enabled(self, value: bool) -> None:
        self._trading_enabled = value
