#!/usr/bin/env python3
"""Wire Bitget into agent + mandate live trading (operator setup).

Writes ``mcp_servers.bitget`` into ``~/.vibe-trading/agent.json`` and prints
the remaining steps (mandate commit, enable WRITE tools, start runner).

Usage:
    python extensions/ext_cli/setup_bitget_live.py
    python extensions/ext_cli/setup_bitget_live.py --with-write-tools
    python extensions/ext_cli/setup_bitget_live.py --status-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_AGENT_ROOT = _PROJECT_ROOT / "agent"
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

from extensions.live.bitget_agent_setup import (  # noqa: E402
    merge_bitget_into_agent_json,
    setup_status,
)
from extensions.live.bitget_bridge import patch_upstream  # noqa: E402
from extensions.live.bitget_mcp_seed import BITGET_BROKER_KEY  # noqa: E402


def _print_next_steps() -> None:
    print(
      "\n下一步 / Next steps:\n"
      "  1. 确认 agent/.env 或环境中已设置 BITGET_API_KEY, BITGET_SECRET, BITGET_PASSPHRASE\n"
      "  2. 重启 vibe-trading serve / TUI（加载 MCP + bridge）\n"
      "  3. 在 chat 中请 agent 调用 propose_mandate_profiles(broker='bitget', ...)\n"
      "     或通过 API POST /mandate/commit 提交委托\n"
      "  4. 编辑 ~/.vibe-trading/agent.json ，在 bitget.enabled_tools 中加入:\n"
      "       place_order, cancel_order\n"
      "  5. 再次重启服务，然后:\n"
      f"       vibe-trading live status {BITGET_BROKER_KEY}\n"
      f"       vibe-trading live start {BITGET_BROKER_KEY}\n"
      "     （Bitget 无需 live authorize — 使用 API Key，非 OAuth）\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup Bitget MCP live broker for agent+mandate")
    parser.add_argument(
        "--with-write-tools",
        action="store_true",
        help="Include place_order/cancel_order in enabled_tools (only after mandate exists)",
    )
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Print readiness only; do not modify agent.json",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Override agent.json path",
    )
    args = parser.parse_args()

    patch_upstream()

    status = setup_status(args.config)
    print("Bitget live channel status:")
    for key, value in status.items():
        print(f"  {key}: {value}")

    if args.status_only:
        return 0

    path = merge_bitget_into_agent_json(
        args.config,
        include_write_tools=args.with_write_tools,
    )
    print(f"\nWrote MCP server block to: {path}")
    _print_next_steps()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
