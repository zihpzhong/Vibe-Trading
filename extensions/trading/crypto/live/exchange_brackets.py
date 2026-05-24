"""开仓后在交易所挂原生 SL/TP bracket 单，以及撤单。
Place and cancel exchange-native SL/TP bracket orders after entry.

``LiveTradingConfig.use_exchange_bracket_orders`` 开启时使用。
Used by live trading when ``LiveTradingConfig.use_exchange_bracket_orders`` is enabled.
软件 TPSL 仍负责移动止损、DCA、de-risk 等；这些路径会先撤 exchange bracket 再市价平仓。
Software TPSL still handles trailing stop, DCA, de-risk, and dynamic TP — those paths
cancel exchange brackets before issuing market closes.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from extensions.trading.crypto.live.exchange import ExchangeBase
    from extensions.trading.crypto.live.position_tracker import Position

logger = logging.getLogger(__name__)

# 交易所仍有效的 Algo 单状态 / Algo order statuses considered active on exchange
_ACTIVE_ALGO_STATUSES = frozenset({"NEW", "TRIGGERED"})


def has_bracket_support(exchange: ExchangeBase) -> bool:
    """True when exchange can place stop-loss and take-profit conditional orders."""
    for method_name in ("create_stop_loss_order", "create_take_profit_order"):
        method = getattr(exchange, method_name, None)
        if method is None:
            logger.info(
                "Exchange %s missing %s — software TPSL fallback",
                type(exchange).__name__, method_name,
            )
            return False
        # Concrete methods on ExchangeBase raise NotImplementedError
        try:
            method("TEST", "sell", 0.001, 1.0)
        except NotImplementedError:
            logger.info(
                "Exchange %s %s raises NotImplementedError — software TPSL fallback",
                type(exchange).__name__, method_name,
            )
            return False
        except Exception:
            logger.warning(
                "Exchange %s %s probe failed (bracket support assumed):",
                type(exchange).__name__, method_name,
                exc_info=True,
            )
    logger.info(
        "Exchange %s supports bracket orders",
        type(exchange).__name__,
    )
    return True


def close_side(direction: str) -> str:
    """Binance side to close a LONG/SHORT position."""
    return "sell" if direction.upper() == "LONG" else "buy"


def is_algo_order_active(exchange: ExchangeBase, order_id: Optional[str]) -> bool:
    """Algo 条件单是否仍在交易所有效 / Whether an algo order is still active."""
    if not order_id:
        return False
    fetch = getattr(exchange, "fetch_algo_order", None)
    if not callable(fetch):
        return True  # 无法校验时保留原 ID / keep ID if exchange cannot verify
    try:
        raw = fetch(order_id)
        status = str(raw.get("status") or raw.get("algoStatus") or "").upper()
        return status in _ACTIVE_ALGO_STATUSES
    except Exception as exc:
        logger.warning("Algo order %s status check failed: %s — treat as inactive", order_id, exc)
        return False


def sanitize_bracket_order_ids(
    exchange: ExchangeBase,
    pos: Position,
) -> tuple[Optional[str], Optional[str]]:
    """校验并清理 DB 中已过期的 bracket order ID。
    Validate DB bracket IDs against exchange; drop stale/canceled algo orders.
    """
    sl_id = pos.sl_order_id if is_algo_order_active(exchange, pos.sl_order_id) else None
    tp_id = pos.tp_order_id if is_algo_order_active(exchange, pos.tp_order_id) else None
    if sl_id != pos.sl_order_id or tp_id != pos.tp_order_id:
        logger.warning(
            "Stale bracket IDs for %s: SL %s→%s TP %s→%s",
            pos.symbol, pos.sl_order_id or "-", sl_id or "-", pos.tp_order_id or "-", tp_id or "-",
        )
    return sl_id, tp_id


def place_bracket_orders(
    exchange: ExchangeBase,
    pos: Position,
) -> tuple[Optional[str], Optional[str]]:
    """Place STOP_MARKET + TAKE_PROFIT_MARKET (futures) for full position size.
    为全仓数量挂 STOP_MARKET + TAKE_PROFIT_MARKET（合约）。

    Returns:
        (sl_order_id, tp_order_id) 字符串；部分失败时可为 None。
        (sl_order_id, tp_order_id) as strings; either may be None on partial failure.
    """
    side = close_side(pos.direction)
    sl_id: Optional[str] = pos.sl_order_id
    tp_id: Optional[str] = pos.tp_order_id
    placed_sl_this_call: Optional[str] = None

    if not sl_id and pos.stop_loss and pos.stop_loss > 0:
        try:
            sl = exchange.create_stop_loss_order(
                pos.symbol, side, pos.quantity, pos.stop_loss,
            )
            sl_id = placed_sl_this_call = str(sl.get("order_id", "")) or None
            logger.info(
                "Exchange SL placed: %s %s qty=%.6f stop=%.4f order_id=%s",
                pos.symbol, pos.direction, pos.quantity, pos.stop_loss, sl_id,
            )
        except Exception as exc:
            logger.error("Exchange SL failed for %s: %s", pos.symbol, exc)

    if not tp_id and pos.take_profit and pos.take_profit > 0:
        try:
            tp = exchange.create_take_profit_order(
                pos.symbol, side, pos.quantity, pos.take_profit,
            )
            tp_id = str(tp.get("order_id", "")) or None
            logger.info(
                "Exchange TP placed: %s %s qty=%.6f tp=%.4f order_id=%s",
                pos.symbol, pos.direction, pos.quantity, pos.take_profit, tp_id,
            )
        except Exception as exc:
            logger.error("Exchange TP failed for %s: %s", pos.symbol, exc)
            # 仅回滚本次新挂的 SL，避免误撤已有有效单 / rollback SL placed in this call only
            if placed_sl_this_call:
                _safe_cancel(exchange, placed_sl_this_call, pos.symbol, label="SL")
                sl_id = None

    return sl_id, tp_id


def cancel_bracket_orders(
    exchange: ExchangeBase,
    pos: Position,
    *,
    sl: bool = True,
    tp: bool = True,
) -> None:
    """Cancel open exchange SL/TP orders for a position (best-effort)."""
    if not hasattr(exchange, "cancel_order"):
        return
    if sl and pos.sl_order_id:
        _safe_cancel(exchange, pos.sl_order_id, pos.symbol, label="SL")
    if tp and pos.tp_order_id:
        _safe_cancel(exchange, pos.tp_order_id, pos.symbol, label="TP")


def cancel_exchange_sl_order(exchange: ExchangeBase, pos: Position) -> None:
    """Cancel only the exchange stop-loss order (e.g. before trailing software SL)."""
    if pos.sl_order_id:
        _safe_cancel(exchange, pos.sl_order_id, pos.symbol, label="SL")


def _safe_cancel(
    exchange: ExchangeBase,
    order_id: str,
    symbol: str,
    *,
    label: str = "order",
) -> None:
    try:
        exchange.cancel_order(order_id, symbol)
        logger.info("Cancelled exchange %s order %s for %s", label, order_id, symbol)
    except Exception as exc:
        logger.debug("Cancel %s %s for %s: %s (may already filled)", label, order_id, symbol, exc)
