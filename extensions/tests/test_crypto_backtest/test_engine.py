"""CryptoLiveBacktestEngine — synthetic E2E."""

from __future__ import annotations


from extensions.trading.crypto.backtest.engine import CryptoLiveBacktestEngine, _NullSignalEngine
from extensions.ext_cli.run_crypto_backtest import _synthetic_ohlcv, filter_universe_by_coverage


def test_backtest_runs_on_synthetic_btc_eth():
    start, end = "2024-02-01", "2024-03-15"
    codes = ["BTCUSDT", "ETHUSDT"]
    data_map = {c: _synthetic_ohlcv(c, start, end, "1h", seed=1) for c in codes}
    data_map, _ = filter_universe_by_coverage(data_map, start, end, "1h", min_coverage=0.5)

    config = {
        "engine": "crypto_live",
        "initial_cash": 1000.0,
        "leverage": 3,
        "max_leverage": 3,
        "position_size_pct": 0.10,
        "pair_whitelist": codes,
        "enforce_whitelist": False,
        "scan_entry_threshold": 5,
        "min_notional_usdt": 5.0,
        "scan_every_n_bars": 24,
        "execution_gate": {"min_liquidity_usdt": 1.0},
    }

    class _Loader:
        name = "test"

        def fetch(self, _c, _s, _e, **_kw):  # noqa: ANN001
            return data_map

    import tempfile
    from pathlib import Path

    engine = CryptoLiveBacktestEngine(config)
    with tempfile.TemporaryDirectory() as tmp:
        metrics = engine.run_backtest(config, _Loader(), _NullSignalEngine(), Path(tmp))
    assert "error" not in metrics
    assert "total_return" in metrics
    assert metrics.get("final_value", 0) > 0
