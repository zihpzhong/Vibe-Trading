"""Extension bridge: auto-loads custom tools from extensions/tools/.

This file lives in src/tools/ so it's discovered by the existing
_discover_subclasses() auto-discovery mechanism. It dynamically imports
all modules under extensions/tools/, making any BaseTool subclass
defined there automatically registered in the ToolRegistry.

This is the ONLY file in the upstream source tree that belongs to
the custom branch. Its unique name (ext_bridge) makes upstream
collision extremely unlikely.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_EXT_TOOLS_DIR = Path(__file__).resolve().parents[3] / "extensions" / "tools"


def _load_ext_tools() -> None:
    """Import all modules in extensions/tools/ so their BaseTool subclasses
    are discovered by BaseTool.__subclasses__().
    """
    if not _EXT_TOOLS_DIR.exists():
        return

    for path in sorted(_EXT_TOOLS_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        module_name = f"extensions.tools.{path.stem}"
        if module_name in sys.modules:
            continue
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = mod
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                logger.debug("Loaded ext tool: %s", module_name)
        except Exception as exc:
            logger.warning("Failed to load ext tool %s: %s", path.name, exc)


_load_ext_tools()
