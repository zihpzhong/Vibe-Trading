"""Tests for A-share data layer — backends, symbol helpers, limit checks."""

from __future__ import annotations

from extensions.trading.astock.live.data import (
    DataChain,
    MockDataBackend,
    infer_board,
    limit_pct,
    normalize_symbol,
    symbol_to_ak_code,
)


def test_normalize_sh() -> None:
    assert normalize_symbol("600519") == "600519.SH"
    assert normalize_symbol("600519.SH") == "600519.SH"
    assert normalize_symbol("SH600519") == "600519.SH"


def test_normalize_sz() -> None:
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("300750") == "300750.SZ"


def test_symbol_to_ak_code() -> None:
    assert symbol_to_ak_code("600519.SH") == "600519"
    assert symbol_to_ak_code("000001.SZ") == "000001"


def test_infer_board() -> None:
    assert infer_board("600519.SH") == "主板"
    assert infer_board("688001.SH") == "科创板"
    assert infer_board("300750.SZ") == "创业板"
    assert infer_board("000001.SZ") == "主板"


def test_limit_pct() -> None:
    assert limit_pct("主板") == 10.0
    assert limit_pct("创业板") == 20.0
    assert limit_pct("科创板") == 20.0
    assert limit_pct("主板", is_st=True) == 5.0
    assert limit_pct("北交所") == 20.0


def test_mock_backend_get_daily() -> None:
    mock = MockDataBackend()
    df = mock.get_daily("600519.SH", "2024-01-01", "2024-01-31")
    assert not df.empty
    assert "close" in df.columns
    assert "open" in df.columns
    assert "high" in df.columns
    assert "low" in df.columns
    assert "volume" in df.columns
    assert "amount" in df.columns
    assert len(df) >= 15  # ~22 trading days in Jan


def test_mock_backend_get_realtime() -> None:
    mock = MockDataBackend()
    rows = mock.get_realtime(["600519.SH", "000001.SZ"])
    assert len(rows) == 2
    for r in rows:
        assert "symbol" in r
        assert "last" in r
        assert "prev_close" in r
        assert "market_cap" in r


def test_mock_backend_financials() -> None:
    mock = MockDataBackend()
    fin = mock.get_financials("600519.SH")
    assert "pe_ttm" in fin
    assert "pb" in fin
    assert "eps" in fin


def test_mock_backend_money_flow() -> None:
    mock = MockDataBackend()
    mf = mock.get_money_flow("600519.SH")
    assert "net_main_inflow" in mf
    assert "net_retail_inflow" in mf


def test_mock_backend_market_breadth() -> None:
    mock = MockDataBackend()
    bd = mock.get_market_breadth()
    assert "up" in bd
    assert "down" in bd
    assert "ratio" in bd


def test_is_limit() -> None:
    mock = MockDataBackend()
    assert mock.is_limit("600519.SH", 110.0, 100.0, "主板") == "up"
    assert mock.is_limit("600519.SH", 95.0, 100.0, "主板") == "none"
    assert mock.is_limit("600519.SH", 89.0, 100.0, "主板") == "down"


def test_data_chain_fallback_to_mock() -> None:
    chain = DataChain(["nonexistent"])
    df = chain.get_daily("600519.SH", "2024-01-01", "2024-01-31")
    assert not df.empty
    rows = chain.get_realtime(["600519.SH"])
    assert len(rows) >= 1


def test_data_chain_financials() -> None:
    chain = DataChain(["mock"])
    fin = chain.get_financials("600519.SH")
    assert isinstance(fin, dict)


def test_data_chain_money_flow() -> None:
    chain = DataChain(["mock"])
    mf = chain.get_money_flow("600519.SH")
    assert isinstance(mf, dict)
