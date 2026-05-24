"""Runner dispatches engine=crypto_live end-to-end."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from extensions.cli.run_crypto_backtest import _synthetic_ohlcv, filter_universe_by_coverage


@pytest.mark.integration
def test_runner_crypto_live_engine():
    start, end = "2024-02-01", "2024-03-01"
    codes = ["BTCUSDT", "ETHUSDT"]
    data_map = {c: _synthetic_ohlcv(c, start, end, "1h", seed=3) for c in codes}
    data_map, _ = filter_universe_by_coverage(data_map, start, end, "1h", min_coverage=0.5)

    agent_root = Path(__file__).resolve().parents[3] / "agent"
    if str(agent_root) not in sys.path:
        sys.path.insert(0, str(agent_root))

    config = {
        "engine": "crypto_live",
        "codes": list(data_map.keys()),
        "start_date": start,
        "end_date": end,
        "source": "inline",
        "interval": "1h",
        "initial_cash": 1000.0,
        "leverage": 3,
        "max_leverage": 3,
        "enforce_whitelist": False,
        "scan_every_n_bars": 24,
        "min_notional_usdt": 5.0,
        "execution_gate": {"min_liquidity_usdt": 1.0},
    }

    class _Loader:
        name = "test"

        def fetch(self, _c, _s, _e, **_kw):  # noqa: ANN001
            return data_map

    from extensions.trading.crypto.backtest.engine import CryptoLiveBacktestEngine, _NullSignalEngine

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        (run_dir / "code").mkdir(exist_ok=True)
        (run_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
        engine = CryptoLiveBacktestEngine(config)
        metrics = engine.run_backtest(config, _Loader(), _NullSignalEngine(), run_dir)
    assert "error" not in metrics
    assert metrics.get("trade_count", 0) >= 0
    assert (run_dir / "artifacts").exists() or metrics.get("final_value")
