"""Tampering future bars must not change past ticker."""

from __future__ import annotations

import pandas as pd

from extensions.live_trading.crypto_backtest.exchange import CryptoBacktestExchange


def test_ticker_unchanged_when_future_mutated():
    dates = pd.date_range("2024-01-01", periods=30, freq="1h")
    close = pd.Series([100.0 + i for i in range(30)], index=dates)
    df = pd.DataFrame(
        {"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1e6},
        index=dates,
    )
    data = {"BTCUSDT": df.copy()}
    ex = CryptoBacktestExchange(data, ["BTCUSDT"])
    t0 = dates[15]
    ex.set_current_bar(t0)
    t_before = ex.get_ticker("BTCUSDT")["last"]

    data["BTCUSDT"].loc[dates[25], "close"] = 99999.0
    ex2 = CryptoBacktestExchange(data, ["BTCUSDT"])
    ex2.set_current_bar(t0)
    t_after = ex2.get_ticker("BTCUSDT")["last"]

    assert t_before == t_after == 115.0
