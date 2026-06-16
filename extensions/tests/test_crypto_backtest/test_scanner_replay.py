"""Scanner indicators/scores via CryptoBacktestExchange match direct path."""

from __future__ import annotations


from extensions.trading.crypto.backtest.exchange import CryptoBacktestExchange
from extensions.trading.crypto.live.market_scanner import MarketScanner
from extensions.ext_cli.run_crypto_backtest import _synthetic_ohlcv


def test_backtest_exchange_indicators_match_direct_compute():
    sym = "ETHUSDT"
    df = _synthetic_ohlcv(sym, "2024-01-01", "2024-03-01", "1h", seed=7)
    ts = df.index[120]

    ex = CryptoBacktestExchange({sym: df}, [sym])
    ex.set_current_bar(ts)
    ticker = ex.get_ticker(sym)
    k1h_ex = ex.get_kline(sym, "1h", 200)
    k15m_ex = ex.get_kline(sym, "15m", 20)
    ind_ex = MarketScanner.compute_indicators(ticker, k1h_ex, k15m_ex)

    hist = df.loc[df.index <= ts].tail(200)
    ind_manual = MarketScanner.compute_indicators(ticker, hist, hist.tail(20))

    for key in ("rsi_1h", "rsi_15m", "bb_pct", "vol_ratio", "price_in_8h_pct"):
        assert abs(ind_ex[key] - ind_manual[key]) < 1e-6, key

    assert MarketScanner.score_long(ind_ex) == MarketScanner.score_long(ind_manual)
    assert MarketScanner.score_short(ind_ex) == MarketScanner.score_short(ind_manual)


def test_scanner_whitelist_uses_exchange_bar_slice():
    sym = "BTCUSDT"
    df = _synthetic_ohlcv(sym, "2024-01-01", "2024-03-01", "1h", seed=11)
    ts = df.index[150]
    ex = CryptoBacktestExchange({sym: df}, [sym])
    ex.set_current_bar(ts)

    direct_long = MarketScanner.score_long(
        MarketScanner.compute_indicators(
            ex.get_ticker(sym),
            ex.get_kline(sym, "1h", 200),
            ex.get_kline(sym, "15m", 20),
        )
    )
    scan = MarketScanner(ex).scan(top_n=5, whitelist=[sym])
    scored = scan.rankings + scan.watchlist
    if scored:
        row = scored[0]
        assert row["symbol"] == sym
        if row["direction"] == "LONG":
            assert row["score"] == direct_long
        else:
            assert row["score"] == MarketScanner.score_short(
                MarketScanner.compute_indicators(
                    ex.get_ticker(sym),
                    ex.get_kline(sym, "1h", 200),
                    ex.get_kline(sym, "15m", 20),
                )
            )
