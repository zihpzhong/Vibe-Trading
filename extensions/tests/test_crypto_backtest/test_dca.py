"""DCA path runs without error on synthetic backtest."""

from __future__ import annotations

import tempfile
from pathlib import Path

from extensions.live_trading.crypto_backtest.engine import CryptoLiveBacktestEngine, _NullSignalEngine
from extensions.run_crypto_backtest import _synthetic_ohlcv, filter_universe_by_coverage


def test_dca_enabled_backtest_completes():
    start, end = "2024-02-01", "2024-04-01"
    codes = ["BTCUSDT", "ETHUSDT"]
    data_map = {c: _synthetic_ohlcv(c, start, end, "1h", seed=99) for c in codes}
    data_map, _ = filter_universe_by_coverage(data_map, start, end, "1h", min_coverage=0.5)

    config = {
        "engine": "crypto_live",
        "initial_cash": 1000.0,
        "leverage": 3,
        "max_leverage": 3,
        "position_size_pct": 0.12,
        "pair_whitelist": codes,
        "enforce_whitelist": False,
        "scan_entry_threshold": 5,
        "min_notional_usdt": 5.0,
        "scan_every_n_bars": 12,
        "enable_dca": True,
        "dca": {"enabled": True, "trigger_loss_pct": 3.0, "dca_min_notional_usdt": 5.0},
        "enable_de_risk": False,
        "enable_trailing": False,
        "enable_stale": False,
        "execution_gate": {"min_liquidity_usdt": 1.0},
    }

    class _Loader:
        name = "test"

        def fetch(self, _c, _s, _e, **_kw):  # noqa: ANN001
            return data_map

    engine = CryptoLiveBacktestEngine(config)
    with tempfile.TemporaryDirectory() as tmp:
        metrics = engine.run_backtest(config, _Loader(), _NullSignalEngine(), Path(tmp))
    assert "error" not in metrics
