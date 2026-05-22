"""Place and cancel exchange-native SL/TP bracket orders after entry.

Used by live trading when ``LiveTradingConfig.use_exchange_bracket_orders`` is enabled.
Software TPSL still handles trailing stop, DCA, de-risk, and dynamic TP — those paths
cancel exchange brackets before issuing market closes.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from extensions.live_trading.engine.exchange import ExchangeBase
    from extensions.live_trading.engine.position_tracker import Position

logger = logging.getLogger(__name__)


def has_bracket_support(exchange: ExchangeBase) -> bool:
    """True when exchange can place stop-loss and take-profit conditional orders."""
    return (
        hasattr(exchange, "create_stop_loss_order")
        and hasattr(exchange, "create_take_profit_order")
        and callable(getattr(exchange, "create_stop_loss_order"))
        and callable(getattr(exchange, "create_take_profit_order"))
    )


def close_side(direction: str) -> str:
    """Binance side to close a LONG/SHORT position."""
    return "sell" if direction.upper() == "LONG" else "buy"


def place_bracket_orders(
    exchange: ExchangeBase,
    pos: Position,
) -> tuple[Optional[str], Optional[str]]:
    """Place STOP_MARKET + TAKE_PROFIT_MARKET (futures) for full position size.

    Returns:
        (sl_order_id, tp_order_id) as strings; either may be None on partial failure.
    """
    side = close_side(pos.direction)
    sl_id: Optional[str] = None
    tp_id: Optional[str] = None

    if pos.stop_loss and pos.stop_loss > 0:
        try:
            sl = exchange.create_stop_loss_order(
                pos.symbol, side, pos.quantity, pos.stop_loss,
            )
            sl_id = str(sl.get("order_id", "")) or None
            logger.info(
                "Exchange SL placed: %s %s qty=%.6f stop=%.4f order_id=%s",
                pos.symbol, pos.direction, pos.quantity, pos.stop_loss, sl_id,
            )
        except Exception as exc:
            logger.error("Exchange SL failed for %s: %s", pos.symbol, exc)

    if pos.take_profit and pos.take_profit > 0:
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
            if sl_id:
                _safe_cancel(exchange, sl_id, pos.symbol)

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
