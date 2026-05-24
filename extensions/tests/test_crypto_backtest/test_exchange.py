"""CryptoBacktestExchange — no lookahead."""

from __future__ import annotations

import pandas as pd

from extensions.trading.crypto.backtest.exchange import CryptoBacktestExchange, normalize_symbol


def _make_df(n: int = 100) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="1h")
    close = pd.Series(range(100, 100 + n), dtype=float, index=dates)
    return pd.DataFrame(
        {
            "open": close - 1,
            "high": close + 2,
            "low": close - 2,
            "close": close,
            "volume": 1e6,
        },
        index=dates,
    )


def test_normalize_symbol():
    assert normalize_symbol("btc") == "BTCUSDT"
    assert normalize_symbol("ETH/USDT") == "ETHUSDT"


def test_kline_no_lookahead():
    df = _make_df(50)
    ex = CryptoBacktestExchange({"BTCUSDT": df}, ["BTCUSDT"])
    mid = df.index[25]
    ex.set_current_bar(mid)
    k = ex.get_kline("BTCUSDT", "1h", 200)
    assert len(k) == 26
    assert k.index.max() <= mid
    assert float(k["close"].iloc[-1]) == 125.0


def test_future_bar_not_visible():
    df = _make_df(50)
    ex = CryptoBacktestExchange({"BTCUSDT": df}, ["BTCUSDT"])
    ex.set_current_bar(df.index[10])
    k = ex.get_kline("BTCUSDT", "1h", 200)
    assert df.index[40] not in k.index
