"""Bitget order-intent extractor tests.

Tests import from ``extensions.live.bitget_extractor`` (not ``src.live.extractors``)
to avoid modifying upstream source files.
"""

from __future__ import annotations

from extensions.live.bitget_extractor import extract_order_intent
from src.live.mandate.model import InstrumentType


def test_extract_full_notional_order() -> None:
    """A place_order with symbol, side, notional_usd, and crypto type."""
    result = extract_order_intent("place_order", {
        "symbol": "BTCUSDT", "side": "buy",
        "notional_usd": 100.0, "instrument_type": "crypto",
    })
    assert result is not None
    assert result.symbol == "BTCUSDT"
    assert result.side == "buy"
    assert result.notional_usd == 100.0
    assert result.quantity is None
    assert result.instrument_type is InstrumentType.CRYPTO


def test_extract_quantity_order() -> None:
    """A place_order with quantity instead of notional."""
    result = extract_order_intent("place_order", {
        "symbol": "ETHUSDT", "side": "sell", "quantity": 0.5,
    })
    assert result is not None
    assert result.symbol == "ETHUSDT"
    assert result.side == "sell"
    assert result.quantity == 0.5
    assert result.notional_usd is None
    assert result.instrument_type is InstrumentType.CRYPTO  # default


def test_extract_notional_and_quantity() -> None:
    """Both notional and quantity present — both surfaced; gate reconciles."""
    result = extract_order_intent("place_order", {
        "symbol": "SOLUSDT", "side": "buy",
        "notional_usd": 200.0, "quantity": 1.5,
    })
    assert result is not None
    assert result.notional_usd == 200.0
    assert result.quantity == 1.5


def test_extract_defaults_to_crypto() -> None:
    """When instrument_type is omitted, defaults to CRYPTO."""
    result = extract_order_intent("place_order", {
        "symbol": "DOGEUSDT", "side": "buy", "notional_usd": 50.0,
    })
    assert result is not None
    assert result.instrument_type is InstrumentType.CRYPTO


def test_extract_side_aliases() -> None:
    """Various side spellings normalize correctly."""
    for raw, expected in [("b", "buy"), ("long", "buy"), ("s", "sell"), ("short", "sell")]:
        result = extract_order_intent("place_order", {
            "symbol": "BTCUSDT", "side": raw, "notional_usd": 100.0,
        })
        assert result is not None
        assert result.side == expected, f"side={raw!r} should map to {expected!r}"


def test_extract_symbol_upper_cases() -> None:
    """Symbol is normalized to upper-case."""
    result = extract_order_intent("place_order", {
        "symbol": "btcusdt", "side": "buy", "notional_usd": 100.0,
    })
    assert result is not None
    assert result.symbol == "BTCUSDT"


def test_extract_unknown_tool_returns_none() -> None:
    """A tool that is not place_order returns None."""
    result = extract_order_intent("cancel_order", {"order_id": "123", "symbol": "BTCUSDT"})
    assert result is None


def test_extract_missing_symbol_returns_none() -> None:
    """Missing symbol → DENY."""
    result = extract_order_intent("place_order", {
        "side": "buy", "notional_usd": 100.0,
    })
    assert result is None


def test_extract_missing_side_returns_none() -> None:
    """Missing side → DENY."""
    result = extract_order_intent("place_order", {
        "symbol": "BTCUSDT", "notional_usd": 100.0,
    })
    assert result is None


def test_extract_missing_size_returns_none() -> None:
    """Neither notional nor quantity → DENY."""
    result = extract_order_intent("place_order", {
        "symbol": "BTCUSDT", "side": "buy",
    })
    assert result is None


def test_extract_non_dict_kwargs_returns_none() -> None:
    """Non-dict kwargs → DENY."""
    result = extract_order_intent("place_order", "not_a_dict")
    assert result is None


def test_extract_zero_notional_returns_none() -> None:
    """Zero notional → DENY (non-positive)."""
    result = extract_order_intent("place_order", {
        "symbol": "BTCUSDT", "side": "buy", "notional_usd": 0,
    })
    assert result is None
