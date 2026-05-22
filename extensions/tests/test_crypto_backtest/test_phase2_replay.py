"""Phase2 replay store — JSONL lookup gates entries."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from extensions.live_trading.crypto_backtest.phase2_replay import Phase2ReplayStore


def test_phase2_replay_pass_and_fail():
    ts = pd.Timestamp("2024-02-15 12:00:00")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "phase2.jsonl"
        rows = [
            {"ts": ts.isoformat(), "symbol": "BTCUSDT", "consensus": "PASS"},
            {"ts": ts.isoformat(), "symbol": "ETHUSDT", "consensus": "FAIL"},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        store = Phase2ReplayStore.load(path)
    assert store.allows_entry(ts, "BTCUSDT") is True
    assert store.allows_entry(ts, "ETHUSDT") is False
    assert store.verdict(ts, "SOLUSDT") == "NEUTRAL"
    assert store.allows_entry(ts, "SOLUSDT") is False
