"""CryptoLiveBacktestEngine — scanner-based crypto backtest on historical bars."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Optional

import pandas as pd

from backtest.engines.crypto import CryptoEngine
from backtest.models import EquitySnapshot, Position, TradeRecord

from extensions.live_trading.crypto_backtest.config import CryptoBacktestConfig
from extensions.live_trading.crypto_backtest.exchange import CryptoBacktestExchange, normalize_symbol
from extensions.live_trading.crypto_backtest.phase2_replay import Phase2ReplayStore
from extensions.live_trading.engine.atr_stop import calculate_atr_stop
from extensions.live_trading.engine.btc_conduction import (
    BTCTrendHint,
    ConductionStatus,
    check_btc_1h_trend,
    check_btc_conduction,
)
from extensions.live_trading.engine.execution_gate import ExecGateEngine
from extensions.live_trading.engine.market_scanner import MarketScanner
from extensions.live_trading.models import GateStatus, LiveSignal, SignalDirection

logger = logging.getLogger(__name__)


class _NullSignalEngine:
    """Placeholder — signals are generated inside _execute_bars; target_pos unused."""

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        return {code: pd.Series(0.0, index=df.index) for code, df in data_map.items()}


def _apply_btc_1h_cap(rankings: list[dict[str, Any]], btc_1h_trend: str) -> None:
    """Mirror TradingScheduler BTC 1h score cap (in-place)."""
    if btc_1h_trend not in ("WEAKNESS", "STRENGTH"):
        return
    for r in rankings:
        rsi = r.get("rsi_1h", 50)
        vol = r.get("vol_ratio", 1.0)
        if btc_1h_trend == "WEAKNESS" and r["direction"] == "LONG" and rsi < 15 and vol >= 2.0:
            continue
        if btc_1h_trend == "STRENGTH" and r["direction"] == "SHORT" and rsi > 85 and vol >= 2.0:
            continue
        if btc_1h_trend == "WEAKNESS" and r["direction"] == "LONG" and r["score"] >= 6:
            r["score"] = 5
        elif btc_1h_trend == "STRENGTH" and r["direction"] == "SHORT" and r["score"] >= 6:
            r["score"] = 5


class CryptoLiveBacktestEngine(CryptoEngine):
    """Replay live scanner → gate → SL/TP on bar data; extends CryptoEngine for fees/funding."""

    def __init__(self, config: dict[str, Any]) -> None:
        lev = float(config.get("leverage", config.get("max_leverage", 5)))
        super().__init__(config)
        self._bt = CryptoBacktestConfig.from_dict(config)
        self.default_leverage = lev
        self.maker_rate = self._bt.maker_rate
        self.taker_rate = self._bt.taker_rate
        self.slippage_rate = self._bt.slippage
        self.funding_rate = self._bt.funding_rate

        self.initial_capital = self._bt.initial_cash
        self.capital = self._bt.initial_cash

        self._bt_exchange: Optional[CryptoBacktestExchange] = None
        self._scanner: Optional[MarketScanner] = None
        self._gate: Optional[ExecGateEngine] = None
        self._live_config = self._bt.to_live_config()

        self._sl_map: dict[str, float] = {}
        self._tp_map: dict[str, float] = {}
        self._cooldown_until: dict[str, int] = {}
        self._first_entry: dict[str, float] = {}
        self._de_risk_level: dict[str, int] = {}
        self._trail_peak: dict[str, float] = {}
        self._trail_active: dict[str, bool] = {}
        self._dca_count: dict[str, int] = {}
        self._bt_data_map: dict[str, pd.DataFrame] = {}
        self._phase2_replay: Optional[Phase2ReplayStore] = None
        if self._bt.phase2_replay_path:
            self._phase2_replay = Phase2ReplayStore.load(self._bt.phase2_replay_path)
        dr = self._bt.de_risk
        self._de_risk_levels = [
            (dr.level1_loss_pct, dr.level1_sell_fraction),
            (dr.level2_loss_pct, dr.level2_sell_fraction),
            (dr.level3_loss_pct, dr.level3_sell_fraction),
        ]
        self._doom_loss_pct = dr.doom_loss_pct
        self._btc_status: ConductionStatus = ConductionStatus.CONDUCTION_OK
        self._btc_1h: str = BTCTrendHint.NEUTRAL.value

    def _rebalance(
        self,
        symbol: str,
        target_weight: float,
        df: Optional[pd.DataFrame],
        ts: pd.Timestamp,
        equity: float,
    ) -> None:
        del symbol, target_weight, df, ts, equity

    def _execute_bars(
        self,
        dates: pd.DatetimeIndex,
        data_map: dict[str, pd.DataFrame],
        close_df: pd.DataFrame,
        target_pos: pd.DataFrame,  # noqa: ARG002
        codes: list[str],
    ) -> None:
        codes_norm = [normalize_symbol(c) for c in codes]

        if self._bt_exchange is None:
            self._bt_exchange = CryptoBacktestExchange(
                data_map,
                codes_norm,
                funding_rate=self._bt.funding_rate,
            )
            self._scanner = MarketScanner(self._bt_exchange)
            self._gate = ExecGateEngine(self._live_config)

        self._funding_applied.clear()
        self._funding_daily_done.clear()
        self._bt_data_map = data_map

        wl = self._bt.pair_whitelist if self._bt.pair_whitelist else None
        top_n = self._bt.scan_top_n if self._bt.scan_top_n > 0 else 20
        btc_sym = normalize_symbol(self._bt.btc_symbol)

        for i, ts in enumerate(dates):
            self._bar_idx = i
            self._bt_exchange.set_current_bar(ts)
            self._bt_exchange.set_account_cash(self.capital)

            for c in codes_norm:
                if c in data_map and ts in data_map[c].index:
                    self.on_bar(c, data_map[c].loc[ts], ts)

            self._check_tpsl(close_df, ts)

            if self._bt.scan_every_n_bars > 1 and i % self._bt.scan_every_n_bars != 0:
                self._record_snapshot(close_df, ts)
                continue

            self._btc_status, self._btc_1h = self._btc_regime()
            if self._btc_status in (ConductionStatus.LOCK_LONG, ConductionStatus.LOCK_SHORT):
                self._record_snapshot(close_df, ts)
                continue

            scan = self._scanner.scan(top_n=top_n, whitelist=wl)
            rankings = [r for r in scan.rankings if r.get("score", 0) >= self._bt.scan_entry_threshold]
            _apply_btc_1h_cap(rankings, self._btc_1h)

            for r in rankings:
                sym = normalize_symbol(r["symbol"])
                if sym in self.positions:
                    continue
                if len(self.positions) >= self._bt.max_positions:
                    break
                if self._in_cooldown(sym, r.get("direction", "LONG"), i):
                    continue
                if sym != btc_sym and self._btc_1h == BTCTrendHint.WEAKNESS.value and r["direction"] == "LONG":
                    continue
                if sym != btc_sym and self._btc_1h == BTCTrendHint.STRENGTH.value and r["direction"] == "SHORT":
                    continue
                self._process_ranking(sym, r, ts, close_df, i)

            self._record_snapshot(close_df, ts)

        if len(dates) > 0:
            last_ts = dates[-1]
            for c in list(self.positions.keys()):
                price = self._safe_price(close_df, last_ts, c, self.positions[c].entry_price)
                self._active_symbol = c
                self._close_position(c, price, last_ts, "end_of_backtest")
                self._cleanup_position_state(c)

    def _btc_regime(self) -> tuple[ConductionStatus, str]:
        ex = self._bt_exchange
        assert ex is not None
        btc = normalize_symbol(self._bt.btc_symbol)
        try:
            k4 = ex.get_kline(btc, "4h", 60)
            status = check_btc_conduction(k4, self._bt.btc_conduction)
        except Exception:
            logger.debug("BTC 4h conduction failed", exc_info=True)
            status = ConductionStatus.CONDUCTION_OK
        try:
            k1 = ex.get_kline(btc, "1h", 50)
            hint = check_btc_1h_trend(k1)
            trend = hint.value if hasattr(hint, "value") else str(hint)
        except Exception:
            trend = BTCTrendHint.NEUTRAL.value
        return status, trend

    def _in_cooldown(self, symbol: str, direction: str, bar_idx: int) -> bool:
        key = f"{symbol}:{direction}"
        until = self._cooldown_until.get(key, -1)
        return bar_idx < until

    def _set_cooldown(self, symbol: str, direction: str, bar_idx: int) -> None:
        key = f"{symbol}:{direction}"
        self._cooldown_until[key] = bar_idx + self._bt.signal_cooldown_bars

    def _active_rr(self, score: int) -> float:
        rr = self._bt.reward_risk_ratio
        if score >= 8:
            return max(rr, 4.0)
        if score >= 7:
            return max(rr, 3.0)
        if score >= 6:
            return max(rr, 2.5)
        return rr

    def _leverage_for_score(self, score: int) -> float:
        if score >= 7:
            return float(self._bt.max_leverage)
        if score >= 5:
            return float(max(1, self._bt.max_leverage // 2))
        return 1.0

    def _tier_factor(self, price_tier: str) -> float:
        return {
            "micro": 0.50,
            "low": 0.75,
            "standard": 1.0,
            "premium": 1.0,
        }.get(price_tier, 1.0)

    def _process_ranking(
        self,
        symbol: str,
        row: dict[str, Any],
        ts: pd.Timestamp,
        close_df: pd.DataFrame,
        bar_idx: int,
    ) -> None:
        ex = self._bt_exchange
        gate = self._gate
        assert ex is not None and gate is not None

        direction_str = str(row.get("direction", "LONG"))
        direction = SignalDirection.LONG if direction_str == "LONG" else SignalDirection.SHORT
        target_dir = 1 if direction == SignalDirection.LONG else -1
        score = int(row.get("score", 0))
        entry_price = float(row.get("entry_price", 0) or 0)
        if entry_price <= 0:
            ticker = ex.get_ticker(symbol)
            entry_price = float(ticker.get("last", 0))
        if entry_price <= 0:
            return

        if self._bt.phase2_enabled:
            if self._phase2_replay is None:
                return
            if self._bt.phase2_replay_swarm:
                if not self._phase2_replay.allows_entry_swarm_as_phase2(ts, symbol):
                    return
            elif not self._phase2_replay.allows_entry(
                ts,
                symbol,
                fast_track_neutral=self._bt.phase2_fast_track_neutral,
            ):
                return

        kline = ex.get_kline(symbol, "1h", 50)
        stop_price, _atr = calculate_atr_stop(
            kline,
            direction,
            entry_price,
            config=self._bt.atr_stop,
        )
        active_rr = self._active_rr(score)
        if direction == SignalDirection.LONG:
            tp_price = entry_price + (entry_price - stop_price) * active_rr
        else:
            tp_price = entry_price - (stop_price - entry_price) * active_rr

        leverage = self._leverage_for_score(score)
        tier = self._tier_factor(str(row.get("price_tier", "standard")))
        eff_size = min(
            self._bt.position_size_pct * tier,
            self._bt.execution_gate.max_position_pct / 100.0,
        )
        equity = self._calc_equity(close_df, ts)
        margin = equity * eff_size
        notional = margin * leverage
        if notional < self._bt.min_notional_usdt:
            return

        order_qty = notional / entry_price
        live_signal = LiveSignal(
            symbol=symbol,
            direction=direction,
            score=score,
            entry_price=entry_price,
            stop_loss=stop_price,
            target_prices=[tp_price],
            risk_reward_ratio=active_rr,
        )
        ticker = ex.get_ticker(symbol)
        funding = ex.get_funding_rate(symbol)
        orderbook = ex.get_orderbook(symbol, 10)
        gate_result = gate.run_gate(
            live_signal,
            ticker,
            funding,
            orderbook,
            order_qty=order_qty,
            account_balance=self.capital,
            order_margin=margin,
            whitelist=self._bt.gate_whitelist(),
        )
        if gate_result.status != GateStatus.PASS:
            return

        bar = pd.Series({"close": entry_price, "open": entry_price})
        if not self.can_execute(symbol, target_dir, bar):
            return

        self._open_from_signal(symbol, target_dir, entry_price, order_qty, leverage, stop_price, tp_price, ts, bar_idx)

    def _open_from_signal(
        self,
        symbol: str,
        target_dir: int,
        entry_price: float,
        raw_qty: float,
        leverage: float,
        sl: float,
        tp: float,
        ts: pd.Timestamp,
        bar_idx: int,
    ) -> None:
        slipped = self.apply_slippage(entry_price, target_dir)
        size = self.round_size(raw_qty, slipped)
        if size <= 0:
            return
        margin = self._calc_margin(symbol, size, slipped, leverage)
        comm = self.calc_commission(size, slipped, target_dir, is_open=True)
        if margin + comm > self.capital:
            return

        self.capital -= margin + comm
        self.positions[symbol] = Position(
            symbol=symbol,
            direction=target_dir,
            entry_price=slipped,
            entry_time=ts,
            size=size,
            leverage=leverage,
            entry_bar_idx=bar_idx,
            entry_commission=comm,
        )
        self._sl_map[symbol] = sl
        self._tp_map[symbol] = tp
        self._first_entry[symbol] = slipped
        self._de_risk_level[symbol] = 0
        self._trail_peak[symbol] = slipped
        self._trail_active[symbol] = False
        self._dca_count[symbol] = 0
        self._set_cooldown(symbol, "LONG" if target_dir == 1 else "SHORT", bar_idx)

    def _pnl_pct_vs_first(self, sym: str, price: float) -> float:
        pos = self.positions.get(sym)
        first = self._first_entry.get(sym, pos.entry_price if pos else 0)
        if not pos or first <= 0:
            return 0.0
        if pos.direction == 1:
            return (price - first) / first * 100
        return (first - price) / first * 100

    def _update_trailing(self, sym: str, price: float, high: float) -> float:
        """Return effective stop (trailing may tighten fixed SL)."""
        pos = self.positions[sym]
        base_sl = self._sl_map.get(sym, 0.0)
        if not self._bt.enable_trailing:
            return base_sl

        peak = self._trail_peak.get(sym, price)
        if pos.direction == 1:
            peak = max(peak, high, price)
            self._trail_peak[sym] = peak
            if not self._trail_active.get(sym) and price >= pos.entry_price * (1 + self._bt.trail_activation_pct / 100):
                self._trail_active[sym] = True
            if self._trail_active.get(sym):
                trail_sl = peak * (1 - self._bt.trail_distance_pct / 100)
                return max(base_sl, trail_sl) if base_sl > 0 else trail_sl
        else:
            trough = min(self._trail_peak.get(sym, price), price)
            self._trail_peak[sym] = trough
            if not self._trail_active.get(sym) and price <= pos.entry_price * (1 - self._bt.trail_activation_pct / 100):
                self._trail_active[sym] = True
            if self._trail_active.get(sym):
                trail_sl = trough * (1 + self._bt.trail_distance_pct / 100)
                return min(base_sl, trail_sl) if base_sl > 0 else trail_sl
        return base_sl

    def _partial_close(
        self,
        symbol: str,
        exit_price: float,
        ts: pd.Timestamp,
        qty: float,
        reason: str,
    ) -> None:
        pos = self.positions.get(symbol)
        if pos is None or qty <= 0:
            return
        close_qty = min(qty, pos.size)
        if close_qty <= 0:
            return
        slipped = self.apply_slippage(exit_price, -pos.direction)
        pnl = self._calc_pnl(symbol, pos.direction, close_qty, pos.entry_price, slipped)
        margin = self._calc_margin(symbol, close_qty, pos.entry_price, pos.leverage)
        exit_comm = self.calc_commission(close_qty, slipped, pos.direction, is_open=False)
        self.capital += margin + pnl - exit_comm
        remaining = pos.size - close_qty
        if remaining <= 1e-8:
            self._active_symbol = symbol
            self._close_position(symbol, slipped, ts, reason)
            self._cleanup_position_state(symbol)
            return
        self.positions[symbol] = replace(pos, size=remaining)
        self.trades.append(
            TradeRecord(
                symbol=symbol,
                direction=pos.direction,
                entry_price=pos.entry_price,
                exit_price=slipped,
                entry_time=pos.entry_time,
                exit_time=ts,
                size=close_qty,
                leverage=pos.leverage,
                pnl=pnl,
                pnl_pct=pnl / margin * 100 if margin > 1e-9 else 0.0,
                exit_reason=reason,
                holding_bars=max(self._bar_idx - pos.entry_bar_idx, 0),
                commission=exit_comm,
            )
        )

    def _check_tpsl(self, close_df: pd.DataFrame, ts: pd.Timestamp) -> None:
        for sym in list(self.positions.keys()):
            pos = self.positions[sym]
            price = self._safe_price(close_df, ts, sym, pos.entry_price)
            high = price
            if sym in self._bt_data_map and ts in self._bt_data_map[sym].index:
                high = float(self._bt_data_map[sym].loc[ts, "high"])

            effective_sl = self._update_trailing(sym, price, high)
            tp = self._tp_map.get(sym)

            if effective_sl > 0:
                if pos.direction == 1 and price <= effective_sl:
                    self._active_symbol = sym
                    self._close_position(sym, self.apply_slippage(price, -1), ts, "stop_loss")
                    self._cleanup_position_state(sym)
                    continue
                if pos.direction == -1 and price >= effective_sl:
                    self._active_symbol = sym
                    self._close_position(sym, self.apply_slippage(price, 1), ts, "stop_loss")
                    self._cleanup_position_state(sym)
                    continue

            if tp is not None:
                if pos.direction == 1 and price >= tp:
                    self._active_symbol = sym
                    self._close_position(sym, self.apply_slippage(price, -1), ts, "take_profit")
                    self._cleanup_position_state(sym)
                    continue
                if pos.direction == -1 and price <= tp:
                    self._active_symbol = sym
                    self._close_position(sym, self.apply_slippage(price, 1), ts, "take_profit")
                    self._cleanup_position_state(sym)
                    continue

            if self._check_dca(sym, price, ts, close_df):
                continue

            if self._bt.enable_de_risk and self._check_de_risk(sym, price, ts):
                continue

            if self._bt.enable_stale and self._check_stale(sym, price, ts):
                continue

    def _check_dca(self, sym: str, price: float, ts: pd.Timestamp, close_df: pd.DataFrame) -> bool:
        """Add to losing positions (mirrors TPSLMonitor DCA, bar-synchronous)."""
        cfg = self._bt.dca
        if not cfg.enabled or not self._bt.enable_dca:
            return False
        pos = self.positions.get(sym)
        if pos is None:
            return False
        count = self._dca_count.get(sym, 0)
        if count >= cfg.max_dca_count:
            return False
        if self._bar_idx - pos.entry_bar_idx < self._bt.entry_grace_bars:
            return False
        loss_pct = self._pnl_pct_vs_first(sym, price)
        if loss_pct >= 0 or abs(loss_pct) < cfg.trigger_loss_pct:
            return False

        equity = self._calc_equity(close_df, ts)
        if equity <= 0:
            return False
        unrealized = self._calc_pnl(sym, pos.direction, pos.size, pos.entry_price, price)
        if abs(unrealized) / equity > cfg.max_account_loss_pct / 100:
            return False

        dca_mult = cfg.dca_multipliers[min(count, len(cfg.dca_multipliers) - 1)]
        dca_lev = max(1.0, self._bt.max_leverage / 2) if cfg.dca_leverage_halved else float(self._bt.max_leverage)
        dca_notional = equity * self._bt.position_size_pct * dca_mult * dca_lev
        if dca_notional < cfg.dca_min_notional_usdt or dca_notional > equity * 0.5:
            return False

        dca_qty = dca_notional / price
        slipped = self.apply_slippage(price, pos.direction)
        margin = self._calc_margin(sym, dca_qty, slipped, dca_lev)
        comm = self.calc_commission(dca_qty, slipped, pos.direction, is_open=True)
        if margin + comm > self.capital:
            return False

        new_size = pos.size + dca_qty
        new_entry = (pos.entry_price * pos.size + slipped * dca_qty) / new_size
        self.capital -= margin + comm
        self.positions[sym] = replace(pos, size=new_size, entry_price=new_entry)
        self._dca_count[sym] = count + 1
        return False

    def _check_de_risk(self, sym: str, price: float, ts: pd.Timestamp) -> bool:
        pos = self.positions[sym]
        if self._bar_idx - pos.entry_bar_idx < self._bt.entry_grace_bars:
            return False
        loss_pct = self._pnl_pct_vs_first(sym, price)
        if loss_pct >= 0:
            return False
        abs_loss = abs(loss_pct)
        if abs_loss >= self._doom_loss_pct:
            self._active_symbol = sym
            self._close_position(sym, self.apply_slippage(price, -pos.direction), ts, "doom")
            self._cleanup_position_state(sym)
            return True

        level_done = self._de_risk_level.get(sym, 0)
        for i in range(level_done, len(self._de_risk_levels)):
            threshold, fraction = self._de_risk_levels[i]
            if abs_loss >= threshold:
                sell_qty = pos.size * fraction
                notional = sell_qty * price
                if notional < self._bt.min_notional_usdt:
                    self._active_symbol = sym
                    self._close_position(sym, self.apply_slippage(price, -pos.direction), ts, f"de_risk_{i + 1}")
                    self._cleanup_position_state(sym)
                    return True
                self._partial_close(sym, price, ts, sell_qty, f"de_risk_{i + 1}")
                self._de_risk_level[sym] = i + 1
                return sym not in self.positions or self.positions[sym].size <= 1e-8
        return False

    def _check_stale(self, sym: str, price: float, ts: pd.Timestamp) -> bool:
        pos = self.positions[sym]
        age_h = (ts - pos.entry_time).total_seconds() / 3600
        if age_h < self._bt.stale_hours:
            return False
        pnl_pct = abs(self._pnl_pct_vs_first(sym, price))
        if pnl_pct > self._bt.stale_pnl_pct:
            return False
        self._active_symbol = sym
        self._close_position(sym, self.apply_slippage(price, -pos.direction), ts, "stale")
        self._cleanup_position_state(sym)
        return True

    def _cleanup_position_state(self, sym: str) -> None:
        self._sl_map.pop(sym, None)
        self._tp_map.pop(sym, None)
        self._first_entry.pop(sym, None)
        self._de_risk_level.pop(sym, None)
        self._trail_peak.pop(sym, None)
        self._trail_active.pop(sym, None)
        self._dca_count.pop(sym, None)

    def _record_snapshot(self, close_df: pd.DataFrame, ts: pd.Timestamp) -> None:
        equity = self._calc_equity(close_df, ts)
        total_unrealized = 0.0
        for p in self.positions.values():
            cp = self._safe_price(close_df, ts, p.symbol, p.entry_price)
            total_unrealized += self._calc_pnl(p.symbol, p.direction, p.size, p.entry_price, cp)
        self.equity_snapshots.append(
            EquitySnapshot(
                timestamp=ts,
                capital=self.capital,
                unrealized=total_unrealized,
                equity=equity,
                positions=len(self.positions),
            )
        )
