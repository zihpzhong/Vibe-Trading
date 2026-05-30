"""Bitget classification map: test the map contents, not the classify_tool function.

The ``classify_tool`` function is already thoroughly tested upstream in
``agent/tests/test_classification.py``. Here we only test that the Bitget map
contains the expected entries — the map semantics are covered by upstream tests.
"""

from __future__ import annotations

from extensions.live.bitget_classification import BITGET_TOOL_CLASS
from src.live.classification import ToolClass, classify_tool


def test_bitget_curated_map_has_all_six_tools() -> None:
    """BITGET_TOOL_CLASS must contain exactly the expected 6 tools."""
    expected_reads = {"get_account", "get_positions", "get_quotes", "list_orders"}
    expected_writes = {"place_order", "cancel_order"}
    reads = {k for k, v in BITGET_TOOL_CLASS.items() if v is ToolClass.READ}
    writes = {k for k, v in BITGET_TOOL_CLASS.items() if v is ToolClass.WRITE}
    assert reads == expected_reads
    assert writes == expected_writes
    assert len(BITGET_TOOL_CLASS) == 6


def test_every_tool_classifies_correctly() -> None:
    """Every tool in the map, when passed to classify_tool with the map, returns its assigned class."""
    for tool_name, expected_class in BITGET_TOOL_CLASS.items():
        result = classify_tool(tool_name, None, BITGET_TOOL_CLASS)
        assert result is expected_class, f"{tool_name}: expected {expected_class}, got {result}"
