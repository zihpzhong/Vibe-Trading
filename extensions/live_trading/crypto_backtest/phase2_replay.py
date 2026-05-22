"""Phase 2 verdict replay from recorded JSONL (no LLM in backtest)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from extensions.live_trading.crypto_backtest.exchange import normalize_symbol

logger = logging.getLogger(__name__)


def _ts_key(ts: pd.Timestamp) -> str:
    t = pd.Timestamp(ts)
    if t.tz is not None:
        t = t.tz_localize(None)
    return t.isoformat()


class Phase2ReplayStore:
    """Lookup consensus by (timestamp, symbol); missing → NEUTRAL."""

    def __init__(self) -> None:
        self._verdicts: dict[tuple[str, str], str] = {}

    @classmethod
    def load(cls, path: str | Path) -> Phase2ReplayStore:
        store = cls()
        p = Path(path)
        if not p.exists():
            logger.warning("Phase2 replay file not found: %s", p)
            return store
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = row.get("ts") or row.get("timestamp")
                sym = row.get("symbol", "")
                consensus = str(row.get("consensus", "NEUTRAL")).upper()
                if not ts_raw or not sym:
                    continue
                key_ts = _ts_key(pd.Timestamp(ts_raw))
                key_sym = normalize_symbol(sym)
                store._verdicts[(key_ts, key_sym)] = consensus
        logger.info("Phase2 replay loaded: %d records from %s", len(store._verdicts), p)
        return store

    def verdict(self, ts: pd.Timestamp, symbol: str) -> str:
        """Return PASS / NEUTRAL / FAIL; unknown timestamps → NEUTRAL."""
        key = (_ts_key(ts), normalize_symbol(symbol))
        return self._verdicts.get(key, "NEUTRAL")

    def allows_entry(self, ts: pd.Timestamp, symbol: str, *, fast_track_neutral: bool = False) -> bool:
        """Mirror live: only PASS proceeds; fast_track may pass ALL-NEUTRAL (optional)."""
        v = self.verdict(ts, symbol)
        if v == "PASS":
            return True
        if v == "FAIL":
            return False
        return bool(fast_track_neutral)
