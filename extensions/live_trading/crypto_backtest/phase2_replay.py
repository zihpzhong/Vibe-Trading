"""Phase 2 verdict replay from recorded JSONL (no LLM in backtest)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from extensions.live_trading.crypto_backtest.exchange import normalize_symbol
from extensions.live_trading.engine.swarm_phase2 import swarm_to_phase2_consensus

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
        self._records: dict[tuple[str, str], dict[str, Any]] = {}

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
                if not ts_raw or not sym:
                    continue
                key_ts = _ts_key(pd.Timestamp(ts_raw))
                key_sym = normalize_symbol(sym)
                key = (key_ts, key_sym)
                store._records[key] = row
                consensus = str(row.get("consensus") or row.get("mono_consensus") or "NEUTRAL").upper()
                store._verdicts[key] = consensus
        logger.info("Phase2 replay loaded: %d records from %s", len(store._verdicts), p)
        return store

    def record(self, ts: pd.Timestamp, symbol: str) -> dict[str, Any]:
        """Return full JSONL row for a key, or empty dict."""
        key = (_ts_key(ts), normalize_symbol(symbol))
        return dict(self._records.get(key, {}))

    def verdict(self, ts: pd.Timestamp, symbol: str) -> str:
        """Return PASS / NEUTRAL / FAIL; unknown timestamps → NEUTRAL."""
        key = (_ts_key(ts), normalize_symbol(symbol))
        return self._verdicts.get(key, "NEUTRAL")

    def swarm_verdict(self, ts: pd.Timestamp, symbol: str) -> str:
        """Return raw Swarm consensus label; unknown → UNCLEAR."""
        row = self.record(ts, symbol)
        return str(row.get("swarm_consensus", "UNCLEAR")).upper()

    def dim_verdicts(self, ts: pd.Timestamp, symbol: str, *, source: str = "mono") -> dict[str, str]:
        """Return per-dim verdicts from mono or swarm fields."""
        row = self.record(ts, symbol)
        if source == "swarm":
            raw = row.get("swarm_dims") or row.get("dim_verdicts") or {}
            return {str(k): str(v).upper() for k, v in raw.items()}
        raw = row.get("mono_dims") or {}
        if not raw:
            dims = row.get("dimensions") or {}
            return {str(k): str(v.get("verdict", "?")).upper() for k, v in dims.items()}
        return {str(k): str(v).upper() for k, v in raw.items()}

    def allows_entry(self, ts: pd.Timestamp, symbol: str, *, fast_track_neutral: bool = False) -> bool:
        """Mirror live: only PASS proceeds; fast_track may pass ALL-NEUTRAL (optional)."""
        v = self.verdict(ts, symbol)
        if v == "PASS":
            return True
        if v == "FAIL":
            return False
        return bool(fast_track_neutral)

    def allows_entry_swarm(
        self,
        ts: pd.Timestamp,
        symbol: str,
        *,
        block_caution: bool = True,
    ) -> bool:
        """Gate entries using Swarm consensus labels."""
        swarm = self.swarm_verdict(ts, symbol)
        if swarm == "CONFIRMED":
            return True
        if swarm in ("DANGER",):
            return False
        if block_caution and swarm == "CAUTION":
            return False
        return False

    def allows_entry_swarm_as_phase2(self, ts: pd.Timestamp, symbol: str) -> bool:
        """Map Swarm consensus to Phase2 PASS/FAIL semantics for backtest.

        Matches live Phase 2 behavior: PASS → entry, FAIL → block,
        NEUTRAL/UNCLEAR → pass through (live system allows ALL-NEUTRAL).
        """
        mapped = swarm_to_phase2_consensus(self.swarm_verdict(ts, symbol))
        if mapped == "FAIL":
            return False
        return True
