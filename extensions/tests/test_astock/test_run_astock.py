"""Smoke test for A-share entry script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_run_astock_once_mock() -> None:
    root = Path(__file__).resolve().parents[3]
    script = root / "extensions" / "cli" / "run_astock_trading.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--mock", "--once", "--no-phase2"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
