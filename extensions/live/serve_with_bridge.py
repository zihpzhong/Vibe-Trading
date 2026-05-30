"""Uvicorn entrypoint: load Bitget bridge before ``api_server.app``.

Use for local agent + mandate verification (stdio MCP has no separate OAuth)::

    cd Vibe-Trading
    set -a && source agent/.env && set +a
    PYTHONPATH=agent:. python3.12 -m uvicorn extensions.live.serve_with_bridge:app \\
        --host 127.0.0.1 --port 8899
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_AGENT = _ROOT / "agent"
_EXT_ENV_LOCAL = _ROOT / "extensions" / "config" / ".env.local"
for _path in (_AGENT, _ROOT):
    _s = str(_path)
    if _s not in sys.path:
        sys.path.insert(0, _s)

try:
    from dotenv import load_dotenv

    load_dotenv(_AGENT / ".env", override=False)
    if _EXT_ENV_LOCAL.is_file():
        # 子账号覆盖 agent/.env 中的 BITGET_*（仅本入口）/ Agent sub-account keys for serve only
        load_dotenv(_EXT_ENV_LOCAL, override=True)
except ImportError:
    pass

# 触发 ext_bridge → patch_upstream / Trigger bridge before api_server loads
import src.tools.ext_bridge  # noqa: F401, E402

from extensions.live.bitget_bridge import _patch_api_server_surfaces  # noqa: E402

import api_server  # noqa: E402

_patch_api_server_surfaces()

app = api_server.app
