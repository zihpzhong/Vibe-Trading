"""AGT live-runner 工具白名单与 tick 约束 / Tool allowlist and tick constraints for AGT.

Live-runner sessions (``live-runner:{broker}``) use a reduced tool surface so
autonomous ticks do not invoke ``read_url``, ``run_swarm``, or other research
tools that blow tick latency and third-party rate limits.

Prompt 优先级: AGT_LIVE_PROMPT 环境变量 > config.json:agt_live.prompt_template > 代码默认值
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.tools import ToolRegistry

logger = logging.getLogger(__name__)

LIVE_RUNNER_SESSION_PREFIX = "live-runner:"

#: Local tools allowed during AGT autonomous ticks / 自主 tick 允许的本地工具
AGT_LIVE_LOCAL_TOOLS: frozenset[str] = frozenset({
    "load_skill",
    "remember",
    "live_trading",
})

#: MCP tools with this prefix are kept (Bitget broker READ/WRITE gated elsewhere).
AGT_LIVE_MCP_PREFIX = "mcp_bitget_"

#: Explicitly blocked high-cost tools (logged if requested by preset).
AGT_LIVE_BLOCKED_TOOLS: frozenset[str] = frozenset({
    "read_url",
    "web_search",
    "run_swarm",
    "bash",
    "write_file",
    "edit_file",
    "backtest",
})

_DEFAULT_PROMPT_ADDENDUM = (
    "\n\n=== AGT LIVE TICK CONSTRAINTS (mandatory) ===\n"
    "- Market data: use ONLY Bitget MCP tools (mcp_bitget_get_account, "
    "mcp_bitget_get_positions, mcp_bitget_get_quotes, mcp_bitget_list_orders) "
    "or live_trading (get_positions, get_account, run_gate).\n"
    "- FORBIDDEN this tick: read_url, web_search, run_swarm, bash, backtest, "
    "and any OKX/Binance/CoinGecko REST URL fetches.\n"
    "- Do NOT load okx-market or other exchange REST skills for price data.\n"
    "- If mandate caps block new orders, HOLD quickly — do not spawn research "
    "sub-agents or bulk URL fetches.\n"
    "- Signal: When SIZING shows SIGNAL=GREEN, you SHOULD use get_quotes to "
    "check prices, run_gate to validate, and place an order if gate passes. "
    "Tgt=Flr is NOT a deadlock — orders at Tgt level pass the gate.\n"
)


def _config_json_path() -> Path:
    """Return path to extensions/config/config.json."""
    return Path(__file__).resolve().parent.parent / "config" / "config.json"


def _load_prompt_addendum_from_config() -> str:
    """从 config.json 的 agt_live.prompt_template 字段加载 prompt。

    Load prompt template from config.json ``agt_live.prompt_template``.
    Returns empty string if config is missing or field is empty.
    """
    path = _config_json_path()
    if not path.is_file():
        return ""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        template = (cfg or {}).get("agt_live", {}).get("prompt_template", "")
        if isinstance(template, str) and template.strip():
            return template.strip()
    except (OSError, ValueError) as exc:
        logger.warning("agt live-runner config.json read failed: %s", exc)
    return ""


def _load_prompt_addendum() -> str:
    """加载 prompt，优先级: 环境变量 > config.json > 代码默认。

    Load prompt addendum with precedence: env var > config.json > code default.
    """
    raw = os.environ.get("AGT_LIVE_PROMPT", "").strip()
    if raw:
        logger.info("agt live-runner prompt loaded from AGT_LIVE_PROMPT env var (%d chars)", len(raw))
        return raw
    from_cfg = _load_prompt_addendum_from_config()
    if from_cfg:
        logger.info("agt live-runner prompt loaded from config.json (%d chars)", len(from_cfg))
        return from_cfg
    logger.info("agt live-runner prompt using hardcoded default (%d chars)", len(_DEFAULT_PROMPT_ADDENDUM))
    return _DEFAULT_PROMPT_ADDENDUM


AGT_LIVE_PROMPT_ADDENDUM = _load_prompt_addendum()


def is_live_runner_session_title(title: str | None) -> bool:
    """Return True when a session title marks an AGT live-runner channel."""
    return bool(title and title.startswith(LIVE_RUNNER_SESSION_PREFIX))


def filter_registry_for_agt_live(full: ToolRegistry) -> ToolRegistry:
    """Return a registry containing only AGT live-runner allowed tools.

    仅保留 AGT 自主 tick 白名单内的工具。
    """
    from src.agent.tools import ToolRegistry

    filtered = ToolRegistry()
    kept: list[str] = []
    for name in full.tool_names:
        tool = full.get(name)
        if tool is None:
            continue
        if name in AGT_LIVE_LOCAL_TOOLS or name.startswith(AGT_LIVE_MCP_PREFIX):
            filtered.register(tool)
            kept.append(name)
        elif name in AGT_LIVE_BLOCKED_TOOLS:
            logger.debug("agt live-runner dropped blocked tool %r", name)
    logger.info(
        "agt live-runner tool filter: kept %d/%d tools (%s)",
        len(kept),
        len(full.tool_names),
        ", ".join(sorted(kept)[:12]) + ("…" if len(kept) > 12 else ""),
    )
    return filtered


def append_agt_live_prompt_constraints(prompt: str) -> str:
    """Append mandatory AGT live-tick tool constraints to a runner prompt."""
    if AGT_LIVE_PROMPT_ADDENDUM.strip() in prompt:
        return prompt
    return prompt + AGT_LIVE_PROMPT_ADDENDUM
