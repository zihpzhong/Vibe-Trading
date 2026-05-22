"""AStockBacktestEngine — scanner-based A-share backtest.

Extends BaseEngine to reuse metrics, artifacts, validation, and run_card
infrastructure, but replaces the weight-based _execute_bars() with a
per-bar scanner evaluation loop that mirrors the A-share live trading pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from backtest.engines.base import BaseEngine
from backtest.models import EquitySnapshot, Position

from extensions.live_trading.astock.atr_stop import calculate_astock_stop
from extensions.live_trading.astock.backtest.config import AStockBacktestConfig
from extensions.live_trading.astock.backtest.exchange import BacktestExchange
from extensions.live_trading.astock.conduction import check_market_conduction
from extensions.live_trading.astock.gate import AStockGateEngine
from extensions.live_trading.astock.models import AStockSignal, GateStatus, MarketConductionStatus
from extensions.live_trading.astock.scanner import AStockScanner

logger = logging.getLogger(__name__)


# Null signal engine — A-share backtest generates signals inside _execute_bars
class _NullSignalEngine:
    """Returns neutral signals; all real logic is in AStockBacktestEngine._execute_bars()."""

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        return {code: pd.Series(0.0) for code in data_map}


class AStockBacktestEngine(BaseEngine):
    """A-share backtest engine using scanner-based daily evaluation.

    Config keys (in addition to BaseEngine config):
      - initial_cash: starting capital (default 1_000_000)
      - scan_top_n: symbols to evaluate per bar (default 20)
      - position_size_pct: capital fraction per position (default 0.12)
      - lot_size: minimum trade unit (default 100)
      - slippage: slippage rate (default 0.001)
      - commission_rate: default 0.00025
      - commission_min: default 5.0
      - stamp_tax: default 0.0005
      - transfer_fee: default 0.00001
      - market_index: index code for conduction (default "000300.SH")
    """

    def __init__(self, config: dict[str, Any]) -> None:
        config = {**config, "leverage": 1.0}
        super().__init__(config)
        bt_config = AStockBacktestConfig.from_dict(config)
        self._bt_config = bt_config
        self._astock_config = bt_config.to_trading_config()

        # Lazily created in _execute_bars
        self._bt_exchange: Optional[BacktestExchange] = None
        self._scanner: Optional[AStockScanner] = None
        self._gate: Optional[AStockGateEngine] = None

        # Per-position SL/TP (BaseEngine.Position has no such fields)
        self._sl_map: dict[str, float] = {}
        self._tp_map: dict[str, float] = {}

        # Trailing stop state
        self._trail_high: dict[str, float] = {}
        self._trail_active: dict[str, bool] = {}

        # Current bar conduction status (updated each bar)
        self._current_conduction: Optional[MarketConductionStatus] = None

        # Fee parameters
        self._commission_rate = bt_config.commission_rate
        self._commission_min = bt_config.commission_min
        self._stamp_tax = bt_config.stamp_tax
        self._transfer_fee = bt_config.transfer_fee
        self._slippage = bt_config.slippage

        # Override initial_capital from config after super().__init__ set it
        self.initial_capital = bt_config.initial_cash
        self.capital = bt_config.initial_cash

    # ── Abstract method implementations ──

    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        if direction == -1:
            return False  # no short
        if direction == 0:
            pos = self.positions.get(symbol)
            if pos is not None:
                bar_date = self._bar_date(bar)
                if bar_date is not None and pos.entry_time.date() == bar_date:
                    return False  # T+1
        return True

    def round_size(self, raw_size: float, _price: float) -> float:
        return max(int(raw_size / 100) * 100, 0)

    def calc_commission(self, size: float, price: float, _direction: int, is_open: bool) -> float:
        notional = size * price
        comm = max(notional * self._commission_rate, self._commission_min)
        comm += notional * self._transfer_fee
        if not is_open:
            comm += notional * self._stamp_tax
        return comm

    def apply_slippage(self, price: float, direction: int) -> float:
        return price * (1 + direction * self._slippage)

    # ── Override rebalance (no-op: we manage positions directly) ──

    def _rebalance(
        self,
        symbol: str,
        target_weight: float,
        df: Optional[pd.DataFrame],
        ts: pd.Timestamp,
        equity: float,
    ) -> None:
        pass  # managed inside _execute_bars

    # ── Core execution loop ──

    def _execute_bars(
        self,
        dates: pd.DatetimeIndex,
        data_map: dict[str, pd.DataFrame],
        close_df: pd.DataFrame,
        target_pos: pd.DataFrame,  # noqa: ARG002
        codes: list[str],
    ) -> None:
        # Lazy initialisation on first call
        if self._bt_exchange is None:
            self._bt_exchange = BacktestExchange(
                data_map,
                codes,
                lot_size=self._bt_config.lot_size,
                fees=self._bt_config.fees,
            )
            self._scanner = AStockScanner(self._bt_exchange, self._astock_config)
            self._gate = AStockGateEngine(self._astock_config)

        # Store data_map for _check_tpsl trailing stop high-price lookup
        self._bt_data_map = data_map

        for i, ts in enumerate(dates):
            self._bar_idx = i
            self._bt_exchange.set_current_date(ts)
            # Update conduction status for adaptive sizing
            self._current_conduction = self._get_conduction_status(ts)

            # Per-bar hook (open to subclasses)
            for c in codes:
                if ts in data_map.get(c, pd.DataFrame()).index:
                    self.on_bar(c, data_map[c].loc[ts], ts)

            # Conduction check
            if self._is_locked(ts):
                self._record_snapshot(close_df, ts)
                continue

            # Phase 1 scan
            scan = self._scanner.scan(top_n=self._bt_config.scan_top_n, universe=codes)

            # Process each high-score signal
            for r in scan.rankings:
                sym = r["symbol"]
                if sym not in data_map or sym in self.positions:
                    continue
                self._process_signal(sym, r, ts, data_map)

            # TPSL check for existing positions
            self._check_tpsl(close_df, ts)

            # Record equity snapshot
            self._record_snapshot(close_df, ts)

        # Force close remaining positions at end of backtest
        if len(dates) > 0:
            last_ts = dates[-1]
            for c in list(self.positions.keys()):
                price = self._safe_price(close_df, last_ts, c, self.positions[c].entry_price)
                self._active_symbol = c
                self._close_position(c, price, last_ts, "end_of_backtest")
                self._cleanup_position_state(c)

    # ── Internal helpers ──

    def _is_locked(self, ts: pd.Timestamp) -> bool:
        """Check market conduction — if LOCK_ALL, skip this bar entirely."""
        return self._get_conduction_status(ts) == MarketConductionStatus.LOCK_ALL

    def _get_conduction_status(self, ts: pd.Timestamp) -> MarketConductionStatus:
        """Return full market conduction status for adaptive sizing."""
        try:
            idx = self._bt_exchange.get_market_index(self._bt_config.market_index, 80)
            breadth = self._bt_exchange.get_market_breadth()
            return check_market_conduction(idx, breadth)
        except Exception:
            logger.debug("conduction check failed at %s", ts, exc_info=True)
            return MarketConductionStatus.OK

    def _process_signal(
        self,
        symbol: str,
        row: dict[str, Any],
        ts: pd.Timestamp,
        data_map: dict[str, pd.DataFrame],
    ) -> None:
        """Evaluate one scan result through stop → gate → place."""
        if len(self.positions) >= self._bt_config.max_positions:
            return
        entry_price = float(row.get("entry_price", 0))
        if entry_price <= 0:
            return

        # Calculate ATR stop
        kline = self._bt_exchange.get_daily(symbol, 120)
        sl, tp, _atr = calculate_astock_stop(kline, entry_price, self._bt_config.stop)

        signal = AStockSignal(
            symbol=symbol,
            name=str(row.get("name", symbol)),
            score=int(row.get("score", 0)),
            entry_price=entry_price,
            stop_loss=sl,
            take_profit=tp,
        )

        # Gate check
        balance = self.capital
        shares = self._calc_shares(balance, entry_price)
        result = self._gate.run_gate(signal, self._bt_exchange, balance, shares)
        if result.status != GateStatus.PASS:
            return

        # Place buy order
        self._place_buy(symbol, entry_price, shares, sl, tp, ts)

    def _calc_shares(self, balance: float, price: float) -> int:
        """Calculate lot-rounded share count from available capital.

        Applies market-adaptive sizing multiplier based on conduction status:
          STRONG → 1.0x, OK → 0.75x, CAUTION → 0.50x, LOCK_ALL → 0.0x
        """
        multiplier = {
            MarketConductionStatus.STRONG: 1.0,
            MarketConductionStatus.OK: 0.75,
            MarketConductionStatus.CAUTION: 0.50,
            MarketConductionStatus.LOCK_ALL: 0.0,
        }.get(self._current_conduction, 0.75)

        lot = self._bt_config.lot_size
        max_notional = balance * self._bt_config.position_size_pct * multiplier
        raw = int(max_notional / price) // lot * lot if price > 0 else lot
        return max(lot, raw)

    def _place_buy(
        self,
        symbol: str,
        price: float,
        shares: int,
        sl: float,
        tp: float,
        ts: pd.Timestamp,
    ) -> None:
        """Execute buy and record resulting position in BaseEngine's book."""
        result = self._bt_exchange.create_buy_order(symbol, price, shares)
        if result.get("status") != "filled":
            return

        filled = int(result.get("filled", 0))
        if filled <= 0:
            return

        commission = float(result.get("commission", 0))
        slipped = self.apply_slippage(price, 1)
        margin = slipped * filled

        if margin + commission > self.capital:
            return

        self.capital -= margin + commission
        self.positions[symbol] = Position(
            symbol=symbol,
            direction=1,
            entry_price=slipped,
            entry_time=ts,
            size=float(filled),
            leverage=1.0,
            entry_bar_idx=self._bar_idx,
            entry_commission=commission,
        )
        self._sl_map[symbol] = sl
        self._tp_map[symbol] = tp
        self._trail_high[symbol] = slipped  # track highest close since entry
        self._trail_active[symbol] = False

    def _check_tpsl(self, close_df: pd.DataFrame, ts: pd.Timestamp) -> None:
        """Check all open positions for stop-loss / take-profit / trailing / max-days."""
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]

            # T+1: same-day buy cannot sell
            if ts.date() == pos.entry_time.date():
                continue

            price = self._safe_price(close_df, ts, sym, pos.entry_price)
            sl = self._sl_map.get(sym)
            tp = self._tp_map.get(sym)

            # Get high price from data_map for trailing stop
            high = price
            dm = getattr(self, '_bt_data_map', None)
            if dm and sym in dm and ts in dm[sym].index:
                high = float(dm[sym].loc[ts, 'high'])

            # Update trailing stop high-water mark
            trail_high = self._trail_high.get(sym, price)
            if high > trail_high:
                self._trail_high[sym] = high
                trail_high = high

            trail_active = self._trail_active.get(sym, False)
            trail_act_pct = self._bt_config.trail_activation_pct
            trail_dist_pct = self._bt_config.trail_distance_pct

            # Activate trailing when price rises enough from entry
            if not trail_active and price >= pos.entry_price * (1 + trail_act_pct / 100):
                self._trail_active[sym] = True
                trail_active = True

            # Check exit conditions in priority order

            # 1. Max holding days
            holding_days = (ts.date() - pos.entry_time.date()).days
            if holding_days >= self._bt_config.max_holding_days:
                self._active_symbol = sym
                self._close_position(sym, price, ts, "max_days")
                self._cleanup_position_state(sym)
                continue

            # 2. Trailing stop (takes priority over fixed SL)
            if trail_active:
                trail_trigger = trail_high * (1 - trail_dist_pct / 100)
                if price <= trail_trigger:
                    self._active_symbol = sym
                    self._close_position(sym, price, ts, "trailing_stop")
                    self._cleanup_position_state(sym)
                    continue

            # 3. Fixed stop-loss
            if sl is not None and price <= sl:
                self._active_symbol = sym
                self._close_position(sym, price, ts, "stop_loss")
                self._cleanup_position_state(sym)
                continue

            # 4. Fixed take-profit
            if tp is not None and price >= tp:
                self._active_symbol = sym
                self._close_position(sym, price, ts, "take_profit")
                self._cleanup_position_state(sym)
                continue

    def _cleanup_position_state(self, sym: str) -> None:
        """Clean up per-position tracking state."""
        self._sl_map.pop(sym, None)
        self._tp_map.pop(sym, None)
        self._trail_high.pop(sym, None)
        self._trail_active.pop(sym, None)

    def _record_snapshot(self, close_df: pd.DataFrame, ts: pd.Timestamp) -> None:
        """Record an equity snapshot matching BaseEngine format."""
        equity = self._calc_equity(close_df, ts)
        total_unrealized = 0.0
        for p in self.positions.values():
            cp = self._safe_price(close_df, ts, p.symbol, p.entry_price)
            total_unrealized += self._calc_pnl(
                p.symbol, p.direction, p.size, p.entry_price, cp,
            )
        self.equity_snapshots.append(EquitySnapshot(
            timestamp=ts,
            capital=self.capital,
            unrealized=total_unrealized,
            equity=equity,
            positions=len(self.positions),
        ))

    @staticmethod
    def _bar_date(bar: pd.Series) -> Optional[Any]:
        """Extract date from bar for T+1 comparison."""
        for col in ("trade_date", "date"):
            if col in bar.index:
                val = bar[col]
                if hasattr(val, "date"):
                    return val.date()
                try:
                    return pd.Timestamp(val).date()
                except Exception:
                    pass
        if hasattr(bar, "name") and hasattr(bar.name, "date"):
            return bar.name.date()
        return None
