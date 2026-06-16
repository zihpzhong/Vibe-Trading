"""Curated read/write classification map for Bitget MCP Live Broker.

See ``src.live.robinhood_classification`` for the canonical pattern. This
file is structurally identical but namespaced under ``extensions.live`` to
avoid modifying upstream source files.

Map entries are authoritative and override Tier-1 ``annotations``. A tool
absent from this map and not annotated read-only is UNKNOWN → WRITE (fail-closed).
"""

from __future__ import annotations

# Import the enum from upstream — this is a read-only reference, not a
# modification of upstream source.
from src.live.classification import ToolClass

#: Frozen canonical Bitget read/write catalog.
BITGET_TOOL_CLASS: dict[str, ToolClass] = {
    # READ
    "get_account": ToolClass.READ,
    "get_positions": ToolClass.READ,
    "get_quotes": ToolClass.READ,
    "list_orders": ToolClass.READ,
    # WRITE
    "place_order": ToolClass.WRITE,
    "cancel_order": ToolClass.WRITE,
}
