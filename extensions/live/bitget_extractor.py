"""Bitget order-intent extractor (SPEC.md Mandate Enforcement §4).

Bitget is a pure-crypto exchange, so the default instrument type is CRYPTO
when the caller omits ``instrument_type``. Shares extraction helpers with
other broker extractors via ``extensions.live._shared_extractor``.
"""

from __future__ import annotations

from extensions.live._shared_extractor import extract_size, extract_side, extract_symbol
from src.live.enforcement import OrderIntent
from src.live.mandate.model import InstrumentType

_ORDER_TOOLS = frozenset({"place_order"})

_INSTRUMENT_ALIASES = {
    "crypto": InstrumentType.CRYPTO,
    "cryptocurrency": InstrumentType.CRYPTO,
    "swap": InstrumentType.CRYPTO,
    "perpetual": InstrumentType.CRYPTO,
    "future": InstrumentType.CRYPTO,
    "futures": InstrumentType.CRYPTO,
}


def extract_order_intent(remote_name: str, kwargs: dict) -> OrderIntent | None:
    """Parse Bitget ``place_order`` kwargs into a normalized OrderIntent.

    Returns None (→ DENY) when the order cannot be unambiguously parsed.
    When ``instrument_type`` is omitted, defaults to CRYPTO.
    """
    if remote_name not in _ORDER_TOOLS or not isinstance(kwargs, dict):
        return None

    symbol = extract_symbol(kwargs)
    side = extract_side(kwargs)
    if symbol is None or side is None:
        return None

    instrument = _extract_instrument(kwargs) or InstrumentType.CRYPTO
    notional, quantity = extract_size(kwargs)
    if notional is None and quantity is None:
        return None

    return OrderIntent(
        symbol=symbol, side=side,
        notional_usd=notional, quantity=quantity,
        instrument_type=instrument,
    )


def _extract_instrument(kwargs: dict) -> InstrumentType | None:
    """Return the mapped InstrumentType, or ``None`` (→ caller defaults to CRYPTO).

    Unlike the Robinhood extractor, ``None`` is a valid return — the
    caller defaults to CRYPTO, so an absent ``instrument_type`` does not DENY.
    """
    for key in ("instrument_type", "asset_class", "type", "instrument_class"):
        value = kwargs.get(key)
        if isinstance(value, str):
            mapped = _INSTRUMENT_ALIASES.get(value.strip().lower())
            if mapped is not None:
                return mapped
    return None
