"""Extension-only backtest helpers (no upstream agent/ edits per extension-guide)."""

from extensions.ext_backtest.ext_runner import main as run_ext_backtest

__all__ = ["run_ext_backtest"]
