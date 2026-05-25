"""Reconcile local PositionTracker state with exchange open positions."""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from .position_tracker import PositionTracker

logger = logging.getLogger(__name__)

# Emergency stop distance when adopting an orphan exchange position (%)
_ADOPT_STOP_PCT = 8.0


def reconcile_positions(
    tracker: PositionTracker,
    exchange_positions: list[dict[str, Any]],
    *,
    price_lookup: Optional[Callable[[str], float]] = None,
    exchange: Any = None,
) -> dict[str, Any]:
    """Align tracker with exchange positionRisk snapshot.

    - Local position missing on exchange → close locally (RECONCILE_GONE).
    - Exchange position missing locally → adopt into tracker (RECONCILE_ADOPT).

    Args:
        tracker: Position tracker instance.
        exchange_positions: Output of ``exchange.get_positions()``.
        price_lookup: Optional callable(symbol) -> mark price for ghost closes.

    Returns:
        Summary dict with keys ``removed``, ``adopted``, ``unchanged``.
    """
    exch_map: dict[str, dict[str, Any]] = {}
    for raw in exchange_positions:
        sym = str(raw.get("symbol", "")).strip()
        if not sym or float(raw.get("quantity", 0) or 0) <= 0:
            continue
        exch_map[sym] = raw

    removed: list[str] = []
    adopted: list[str] = []

    with tracker._lock:
        local_syms = set(tracker._positions.keys())
        exch_syms = set(exch_map.keys())

        for sym in sorted(local_syms - exch_syms):
            pos = tracker._positions.get(sym)
            if not pos:
                continue
            if exchange:
                try:
                    from .exchange_brackets import cancel_bracket_orders, has_bracket_support

                    if has_bracket_support(exchange):
                        cancel_bracket_orders(exchange, pos)
                except Exception:
                    logger.exception("Failed to cancel exchange brackets for %s on reconcile remove", sym)
            mark = price_lookup(sym) if price_lookup else None
            exit_price = mark if mark and mark > 0 else pos.entry_price
            tracker.close_position(sym, exit_price=exit_price, reason="RECONCILE_GONE")
            removed.append(sym)
            logger.warning(
                "Reconcile: removed ghost local position %s %s (not on exchange)",
                sym, pos.direction,
            )

        for sym in sorted(exch_syms - local_syms):
            ep = exch_map[sym]
            direction = str(ep.get("direction", "LONG")).upper()
            entry_price = float(ep.get("entry_price", 0) or 0)
            quantity = float(ep.get("quantity", 0) or 0)
            if entry_price <= 0 or quantity <= 0:
                logger.warning("Reconcile: skip adopt %s — invalid entry/qty", sym)
                continue
            if direction == "LONG":
                stop_loss = entry_price * (1 - _ADOPT_STOP_PCT / 100)
            else:
                stop_loss = entry_price * (1 + _ADOPT_STOP_PCT / 100)
            tracker.open_position(
                symbol=sym,
                direction=direction,
                entry_price=entry_price,
                quantity=quantity,
                stop_loss=stop_loss,
                take_profit=None,
                leverage=int(ep.get("leverage", 1) or 1),
                entry_score=-1,
            )
            adopted.append(sym)
            logger.warning(
                "Reconcile: adopted exchange position %s %s qty=%.6f @ %.4f",
                sym, direction, quantity, entry_price,
            )

    unchanged = len(exch_syms & local_syms)
    summary = {"removed": removed, "adopted": adopted, "unchanged": unchanged}

    # Place exchange bracket orders for newly adopted positions
    if exchange and adopted:
        _place_adopted_brackets(tracker, exchange, adopted)

    if removed or adopted:
        logger.info("Reconcile complete: %s", summary)
    return summary


def _place_adopted_brackets(
    tracker: PositionTracker,
    exchange: Any,
    adopted: list[str],
) -> None:
    """Place SL/TP bracket orders on exchange for positions adopted via reconcile."""
    try:
        from .exchange_brackets import has_bracket_support, place_bracket_orders

        if not has_bracket_support(exchange):
            logger.info("Exchange lacks bracket support — skip bracket placement for adopted positions")
            return
        for sym in adopted:
            pos = tracker.get_position(sym)
            if not pos or (pos.stop_loss is None or pos.stop_loss <= 0):
                continue
            from .exchange_brackets import cancel_symbol_bracket_algos

            cancel_symbol_bracket_algos(exchange, sym)
            sl_id, tp_id = place_bracket_orders(exchange, pos)
            tracker.set_bracket_order_ids(sym, sl_id, tp_id)
    except Exception:
        logger.exception("Failed to place bracket orders for adopted positions")
