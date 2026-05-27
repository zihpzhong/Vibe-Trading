"""Unit tests for BitgetExchange — mocked ccxt calls."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from extensions.trading.crypto.live.exchange import ExchangeBase


class TestBitgetExchangeImport:
    """Verify BitgetExchange module can be imported."""

    def test_bitget_exchange_imports(self) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        assert BitgetExchange is not None

    @patch("ccxt.bitget")
    def test_bitget_is_exchange_base(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        assert isinstance(ex, ExchangeBase)


class TestBitgetExchangeNoAuth:
    """With empty env, has_auth is False and trading methods raise RuntimeError."""

    @patch("ccxt.bitget")
    def test_has_auth_false_without_env(self, mock_bitget: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BITGET_API_KEY", raising=False)
        monkeypatch.delenv("BITGET_SECRET", raising=False)
        monkeypatch.delenv("BITGET_PASSPHRASE", raising=False)
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        assert ex.has_auth is False

    @patch("ccxt.bitget")
    def test_has_auth_true_with_env(self, mock_bitget: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BITGET_API_KEY", "key")
        monkeypatch.setenv("BITGET_SECRET", "secret")
        monkeypatch.setenv("BITGET_PASSPHRASE", "pass")
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        assert ex.has_auth is True


class TestBitgetExchangeBracketSupport:
    """BitgetExchange does NOT support exchange-native bracket orders."""

    @patch("ccxt.bitget")
    def test_has_bracket_support_false(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live.exchange_brackets import has_bracket_support
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        assert has_bracket_support(ex) is False

    @patch("ccxt.bitget")
    def test_create_stop_loss_raises_not_implemented(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        with pytest.raises(NotImplementedError):
            ex.create_stop_loss_order("BTCUSDT", "sell", 0.01, 59000)

    @patch("ccxt.bitget")
    def test_create_take_profit_raises_not_implemented(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        with pytest.raises(NotImplementedError):
            ex.create_take_profit_order("BTCUSDT", "sell", 0.01, 65000)


class TestBitgetExchangeMarketData:
    """Market data methods work without auth."""

    @patch("ccxt.bitget")
    def test_get_ticker(self, mock_bitget: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.fetch_ticker.return_value = {
            "symbol": "BTC/USDT:USDT",
            "last": 65000.0,
            "open": 64000.0,
            "high": 66000.0,
            "low": 63500.0,
            "quoteVolume": 1_500_000_000.0,
            "percentage": 1.56,
        }
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        result = ex.get_ticker("BTCUSDT")

        assert result["symbol"] == "BTCUSDT"
        assert result["last"] == 65000.0
        assert result["volume24h"] == 1_500_000_000.0

    @patch("ccxt.bitget")
    def test_get_kline(self, mock_bitget: MagicMock) -> None:
        import pandas as pd
        mock_instance = MagicMock()
        mock_instance.fetch_ohlcv.return_value = [
            [1700000000000, 64000, 64500, 63900, 64200, 1000],
            [1700003600000, 64200, 64800, 64100, 64500, 1200],
        ]
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        df = ex.get_kline("BTCUSDT", "1h", 2)

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2
        assert "open" in df.columns
        assert "close" in df.columns

    @patch("ccxt.bitget")
    def test_get_funding_rate(self, mock_bitget: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.fetch_funding_rate.return_value = {"lastFundingRate": 0.0001}
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        result = ex.get_funding_rate("BTCUSDT")

        assert result == 0.0001

    @patch("ccxt.bitget")
    def test_get_orderbook(self, mock_bitget: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.fetch_order_book.return_value = {
            "bids": [[64000, 1.5], [63900, 2.0]],
            "asks": [[64100, 1.0], [64200, 1.8]],
        }
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        result = ex.get_orderbook("BTCUSDT", 2)

        assert len(result["bids"]) == 2
        assert len(result["asks"]) == 2
        assert result["bids"][0][0] == 64000

    @patch("ccxt.bitget")
    def test_get_tickers(self, mock_bitget: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.fetch_tickers.return_value = {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT",
                "last": 65000.0, "open": 64000.0, "high": 66000.0, "low": 63500.0,
                "quoteVolume": 1_500_000_000.0, "percentage": 1.56,
            },
            "ETH/USDT:USDT": {
                "symbol": "ETH/USDT:USDT",
                "last": 3200.0, "open": 3100.0, "high": 3300.0, "low": 3080.0,
                "quoteVolume": 800_000_000.0, "percentage": 3.2,
            },
        }
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        result = ex.get_tickers()

        assert len(result) == 2
        # Sorted by volume descending
        assert result[0]["symbol"] == "BTCUSDT"


class TestBitgetExchangeAuthGuards:
    """Trading methods require API keys."""

    @patch("ccxt.bitget")
    def test_create_market_order_requires_auth(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        with pytest.raises(RuntimeError, match="BITGET_API_KEY"):
            ex.create_market_order("BTCUSDT", "buy", 0.01)

    @patch("ccxt.bitget")
    def test_create_limit_order_requires_auth(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        with pytest.raises(RuntimeError, match="BITGET_API_KEY"):
            ex.create_limit_order("BTCUSDT", "buy", 0.01, 60000)

    @patch("ccxt.bitget")
    def test_cancel_order_requires_auth(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        with pytest.raises(RuntimeError, match="BITGET_API_KEY"):
            ex.cancel_order("123", "BTCUSDT")

    @patch("ccxt.bitget")
    def test_fetch_order_requires_auth(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        with pytest.raises(RuntimeError, match="BITGET_API_KEY"):
            ex.fetch_order("123", "BTCUSDT")


class TestBitgetExchangeBalance:
    """Balance methods return empty without auth."""

    @patch("ccxt.bitget")
    def test_get_account_balance_empty_no_auth(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        assert ex.get_account_balance() == {}

    @patch("ccxt.bitget")
    def test_get_available_balance_empty_no_auth(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        assert ex.get_available_balance() == {}

    @patch("ccxt.bitget")
    def test_get_positions_empty_no_auth(self, mock_bitget: MagicMock) -> None:
        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        assert ex.get_positions() == []


class TestBitgetExchangeSymbolConversion:
    """Symbol format conversion helpers."""

    def test_ccxt_symbol(self) -> None:
        from extensions.trading.crypto.live._bitget_exchange import _ccxt_symbol, _internal_symbol
        assert _ccxt_symbol("BTCUSDT") == "BTC/USDT:USDT"
        assert _ccxt_symbol("ETHUSDT") == "ETH/USDT:USDT"
        assert _internal_symbol("BTC/USDT:USDT") == "BTCUSDT"
        assert _internal_symbol("BTC/USDT") == "BTCUSDT"

    def test_roundtrip(self) -> None:
        from extensions.trading.crypto.live._bitget_exchange import _ccxt_symbol, _internal_symbol
        assert _internal_symbol(_ccxt_symbol("BTCUSDT")) == "BTCUSDT"
        assert _internal_symbol(_ccxt_symbol("SOLUSDT")) == "SOLUSDT"


class TestBitgetExchangeValidateSymbols:
    """validate_symbols filters unsupported pairs."""

    @patch("ccxt.bitget")
    def test_validate_symbols_filters_invalid(self, mock_bitget: MagicMock) -> None:
        mock_instance = MagicMock()
        mock_instance.markets = {}
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()

        # Override internal state after __init__ to bypass network dependency
        ex._ccxt.markets = {
            "BTC/USDT:USDT": {"id": "BTCUSDT"},
            "ETH/USDT:USDT": {"id": "ETHUSDT"},
        }
        ex._markets_loaded = True

        result = ex.validate_symbols(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

        assert result == ["BTCUSDT", "ETHUSDT"]
        assert "SOLUSDT" not in result


class TestBitgetExchangeTradingWithAuth:
    """Trading methods work with API keys set."""

    @patch("ccxt.bitget")
    def test_create_market_order(self, mock_bitget: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BITGET_API_KEY", "key")
        monkeypatch.setenv("BITGET_SECRET", "secret")
        monkeypatch.setenv("BITGET_PASSPHRASE", "pass")

        mock_instance = MagicMock()
        mock_instance.create_order.return_value = {
            "id": "12345",
            "symbol": "BTC/USDT:USDT",
            "side": "buy",
            "type": "market",
            "amount": 0.01,
            "filled": 0.01,
            "status": "closed",
            "average": 65000.0,
            "cost": 650.0,
        }
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        result = ex.create_market_order("BTCUSDT", "buy", 0.01)

        assert result["order_id"] == "12345"
        assert result["status"] == "closed"
        assert result["avg_price"] == 65000.0

    @patch("ccxt.bitget")
    def test_create_limit_order(self, mock_bitget: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BITGET_API_KEY", "key")
        monkeypatch.setenv("BITGET_SECRET", "secret")
        monkeypatch.setenv("BITGET_PASSPHRASE", "pass")

        mock_instance = MagicMock()
        mock_instance.create_order.return_value = {
            "id": "67890",
            "symbol": "ETH/USDT:USDT",
            "side": "sell",
            "type": "limit",
            "amount": 0.1,
            "price": 3200.0,
            "filled": 0.0,
            "status": "open",
        }
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        result = ex.create_limit_order("ETHUSDT", "sell", 0.1, 3200.0)

        assert result["order_id"] == "67890"
        assert result["status"] == "open"


class TestBitgetFilledExtraction:
    """Bitget create_order often omits filled; adapter must resolve it."""

    def test_extract_filled_from_info_fill_size(self) -> None:
        from extensions.trading.crypto.live._bitget_exchange import _extract_filled_qty

        raw = {"filled": 0, "status": "closed", "info": {"fillSize": "1177"}}
        assert _extract_filled_qty(raw, fallback_qty=1177.0) == 1177.0

    def test_extract_filled_closed_status_uses_amount(self) -> None:
        from extensions.trading.crypto.live._bitget_exchange import _extract_filled_qty

        raw = {"filled": 0, "status": "closed", "amount": 0.000421}
        assert _extract_filled_qty(raw) == 0.000421

    @patch("ccxt.bitget")
    def test_market_order_polls_fetch_order_when_filled_zero(
        self, mock_bitget: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BITGET_API_KEY", "key")
        monkeypatch.setenv("BITGET_SECRET", "secret")
        monkeypatch.setenv("BITGET_PASSPHRASE", "pass")

        mock_instance = MagicMock()
        mock_instance.create_order.return_value = {
            "id": "ord-1",
            "symbol": "NAORIS/USDT:USDT",
            "side": "sell",
            "type": "market",
            "filled": 0,
            "status": "open",
        }
        mock_instance.fetch_order.return_value = {
            "id": "ord-1",
            "symbol": "NAORIS/USDT:USDT",
            "side": "sell",
            "type": "market",
            "amount": 1177.0,
            "filled": 1177.0,
            "status": "closed",
            "average": 0.0345,
        }
        mock_bitget.return_value = mock_instance

        from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
        ex = BitgetExchange()
        result = ex.create_market_order("NAORISUSDT", "sell", 1177.0, reduce_only=True)

        assert result["filled"] == 1177.0
        mock_instance.fetch_order.assert_called()
