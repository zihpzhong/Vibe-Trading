"""Shared order-intent extraction helpers for live-broker extractors.

Reused by per-broker extractors (bitget, future brokers) to avoid
copy-pasting the same key-lookup and normalization logic from the
Robinhood extractor in upstream ``src.live.extractors``.
"""

from __future__ import annotations

#: Order-size keys for the notional path.
_NOTIONAL_KEYS = ("notional_usd", "notional", "dollar_amount", "amount")

#: Accepted side spellings → normalized ``"buy"`` / ``"sell"``.
_SIDE_ALIASES = {
    "buy": "buy",
    "b": "buy",
    "long": "buy",
    "sell": "sell",
    "s": "sell",
    "short": "sell",
}


def extract_symbol(kwargs: dict) -> str | None:
    """Return the normalized upper-case symbol, or ``None`` if absent."""
    for key in ("symbol", "ticker", "instrument"):
        value = kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def extract_side(kwargs: dict) -> str | None:
    """Return normalized ``"buy"`` / ``"sell"``, or ``None`` if ambiguous."""
    for key in ("side", "action", "direction"):
        value = kwargs.get(key)
        if isinstance(value, str):
            normalized = _SIDE_ALIASES.get(value.strip().lower())
            if normalized is not None:
                return normalized
    return None


def extract_size(kwargs: dict) -> tuple[float | None, float | None]:
    """Return ``(notional_usd, quantity)``, each parsed or ``None``."""
    notional = _first_positive_float(kwargs, _NOTIONAL_KEYS)
    quantity = _first_positive_float(kwargs, ("quantity", "qty", "units"))
    return notional, quantity


def _first_positive_float(kwargs: dict, keys: tuple[str, ...]) -> float | None:
    """Return the first present key's value as a positive float, else ``None``."""
    for key in keys:
        if key in kwargs:
            try:
                value = float(kwargs[key])
            except (TypeError, ValueError):
                return None
            if value != value or value <= 0:  # NaN or non-positive
                return None
            return value
    return None
