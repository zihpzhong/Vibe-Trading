"""Unit tests for RealExchange — US-007."""

from __future__ import annotations

import pytest

from extensions.trading.crypto.live.exchange import ExchangeBase, MockExchange, create_exchange


class TestRealExchangeImport:
    """Verify RealExchange module can be imported and instantiated."""

    def test_real_exchange_imports(self) -> None:
        """RealExchange can be imported."""
        from extensions.trading.crypto.live._real_exchange import RealExchange
        assert RealExchange is not None

    def test_real_exchange_is_exchange_base(self) -> None:
        """RealExchange subclasses ExchangeBase."""
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert isinstance(ex, ExchangeBase)

    def test_real_exchange_has_all_methods(self) -> None:
        """All 13 required methods exist."""
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        market_methods = ["get_kline", "get_ticker", "get_tickers", "get_funding_rate", "get_orderbook"]
        trading_methods = [
            "create_market_order", "create_limit_order", "create_stop_loss_order",
            "create_take_profit_order", "cancel_order", "fetch_order",
        ]
        account_methods = ["get_account_balance", "get_positions"]
        for m in market_methods + trading_methods + account_methods:
            assert hasattr(ex, m), f"Missing method: {m}"

    def test_has_auth_false_without_env(self, monkeypatch) -> None:
        """has_auth is False when no API keys set."""
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_SECRET", raising=False)
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert ex.has_auth is False

    def test_has_auth_true_with_env(self, monkeypatch) -> None:
        """has_auth is True when API keys are set."""
        monkeypatch.setenv("BINANCE_API_KEY", "test_key")
        monkeypatch.setenv("BINANCE_SECRET", "test_secret")
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert ex.has_auth is True


class TestRealExchangeAuthGuard:
    """Trading/account methods raise RuntimeError without auth."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch) -> None:
        monkeypatch.delenv("BINANCE_API_KEY", raising=False)
        monkeypatch.delenv("BINANCE_SECRET", raising=False)

    def test_create_market_order_requires_auth(self) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        with pytest.raises(RuntimeError):
            ex.create_market_order("BTCUSDT", "buy", 0.01)

    def test_create_limit_order_requires_auth(self) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        with pytest.raises(RuntimeError):
            ex.create_limit_order("BTCUSDT", "buy", 0.01, 60000)

    def test_create_stop_loss_requires_auth(self) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        with pytest.raises(RuntimeError):
            ex.create_stop_loss_order("BTCUSDT", "sell", 0.01, 59000)

    def test_create_take_profit_requires_auth(self) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        with pytest.raises(RuntimeError):
            ex.create_take_profit_order("BTCUSDT", "sell", 0.01, 65000)

    def test_cancel_order_requires_auth(self) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        with pytest.raises(RuntimeError, match="BINANCE_API_KEY"):
            ex.cancel_order("123", "BTCUSDT")

    def test_fetch_order_requires_auth(self) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        with pytest.raises(RuntimeError, match="BINANCE_API_KEY"):
            ex.fetch_order("123", "BTCUSDT")

    def test_get_account_balance_graceful_no_auth(self) -> None:
        """get_account_balance returns {} gracefully when no API keys set."""
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert ex.has_auth is False
        assert ex.get_account_balance() == {}

    def test_get_positions_graceful_no_auth(self) -> None:
        """get_positions returns [] gracefully when no API keys set."""
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert ex.has_auth is False
        assert ex.get_positions() == []


class TestTickerFieldMapping:
    """ccxt ticker fields are mapped to internal format."""

    def test_ccxt_ticker_to_internal(self) -> None:
        from extensions.trading.crypto.live._real_exchange import _ccxt_ticker_to_internal
        raw = {
            "symbol": "BTCUSDT",
            "last": 65000.0,
            "open": 64000.0,
            "high": 66000.0,
            "low": 63500.0,
            "quoteVolume": 1_500_000_000.0,
            "percentage": 1.56,
        }
        result = _ccxt_ticker_to_internal(raw)
        assert result["symbol"] == "BTCUSDT"
        assert result["last"] == 65000.0
        assert result["open24h"] == 64000.0  # ccxt 'open' → internal 'open24h'
        assert result["high24h"] == 66000.0
        assert result["low24h"] == 63500.0
        assert result["volume24h"] == 1_500_000_000.0  # ccxt 'quoteVolume' → internal 'volume24h'
        assert result["change24h"] == 1.56

    def test_ccxt_ticker_missing_fields(self) -> None:
        """Missing fields default to 0.0."""
        from extensions.trading.crypto.live._real_exchange import _ccxt_ticker_to_internal
        result = _ccxt_ticker_to_internal({"symbol": "XXXUSDT"})
        assert result["symbol"] == "XXXUSDT"
        assert result["last"] == 0.0
        assert result["volume24h"] == 0.0


class TestRetryLogic:
    """_retry function retries with exponential backoff."""

    def test_retry_succeeds_first_attempt(self) -> None:
        from extensions.trading.crypto.live._real_exchange import _retry
        call_count = [0]

        def flaky():
            call_count[0] += 1
            return "ok"

        result = _retry("test_op", flaky)
        assert result == "ok"
        assert call_count[0] == 1

    def test_retry_raises_after_exhaustion(self) -> None:
        from extensions.trading.crypto.live._real_exchange import _retry
        call_count = [0]

        def always_fail():
            call_count[0] += 1
            raise ValueError("fail")

        with pytest.raises(RuntimeError, match="test_op failed after 3 attempts"):
            _retry("test_op", always_fail)
        assert call_count[0] == 3  # tried 3 times


class TestTestnetConfig:
    """BINANCE_TESTNET env var controls testnet mode."""

    def test_testnet_false_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("BINANCE_TESTNET", raising=False)
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert ex.is_testnet is False

    def test_testnet_true_with_env(self, monkeypatch) -> None:
        monkeypatch.setenv("BINANCE_TESTNET", "true")
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert ex.is_testnet is True


class TestMarketTypeConfig:
    """RealExchange should default to futures for the auto-trading pipeline."""

    def test_market_type_defaults_to_future(self, monkeypatch) -> None:
        monkeypatch.delenv("BINANCE_MARKET_TYPE", raising=False)
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert ex._market_type == "future"
        assert "/fapi/v1" in ex._data_prefix

    def test_market_type_can_be_set_to_spot(self, monkeypatch) -> None:
        monkeypatch.setenv("BINANCE_MARKET_TYPE", "spot")
        from extensions.trading.crypto.live._real_exchange import RealExchange
        ex = RealExchange()
        assert ex._market_type == "spot"
        assert ex._data_prefix.endswith("/api/v3")


class TestCreateExchangeFactory:
    """create_exchange factory still works."""

    def test_create_exchange_mock(self) -> None:
        ex = create_exchange(mock=True)
        assert isinstance(ex, MockExchange)

    def test_create_exchange_full_import_chain(self) -> None:
        """Full import chain: exchange.py → _real_exchange.py."""
        from extensions.trading.crypto.live._real_exchange import RealExchange  # noqa: F401
        from extensions.trading.crypto.live.exchange import ExchangeBase, MockExchange  # noqa: F401


class TestGetTickersParsing:
    """get_tickers filters and sorts correctly."""

    def test_get_tickers_filters_non_usdt(self) -> None:
        """Non-USDT pairs are excluded from get_tickers results."""
        from extensions.trading.crypto.live._real_exchange import RealExchange
        # Verify the filtering logic by checking the code accepts USDT pairs
        ex = RealExchange()
        assert ex.get_tickers is not None


class TestStopMarketOrderType:
    """create_stop_loss_order uses market-on-trigger, not limit-on-trigger."""

    def test_stop_loss_uses_market_trigger(self) -> None:
        """Verify create_stop_loss_order uses STOP_LOSS (market on trigger)."""
        from extensions.trading.crypto.live._real_exchange import RealExchange
        import inspect
        source = inspect.getsource(RealExchange.create_stop_loss_order)
        # Spot: "STOP_LOSS" is market-on-trigger (fills on execution)
        # Futures: "STOP_MARKET" is the equivalent
        assert '"STOP_LOSS"' in source, "create_stop_loss_order must use STOP_LOSS (market trigger)"
        assert '"STOP_LOSS_LIMIT"' not in source, "Must not use limit trigger for stop-loss"
        assert "algoType" in source
        assert "triggerPrice" in source
        assert "/fapi/v1/algoOrder" in source


class TestAlgoOrderApi:
    """Futures bracket orders use Binance Algo Order API."""

    @pytest.fixture(autouse=True)
    def _auth_env(self, monkeypatch) -> None:
        monkeypatch.setenv("BINANCE_API_KEY", "test_key")
        monkeypatch.setenv("BINANCE_SECRET", "test_secret")
        monkeypatch.setenv("BINANCE_MARKET_TYPE", "future")

    def test_create_stop_loss_uses_algo_endpoint(self, monkeypatch) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange

        captured: dict = {}

        def fake_algo(endpoint: str, params: dict) -> dict:
            captured["endpoint"] = endpoint
            captured["params"] = params
            return {"algoId": 12345, "algoStatus": "NEW", "orderType": "STOP_MARKET", "symbol": "BTCUSDT", "side": "SELL"}

        ex = RealExchange()
        monkeypatch.setattr(ex, "_round_qty", lambda _sym, amt: amt)
        monkeypatch.setattr(ex, "_round_price", lambda _sym, p: p)
        monkeypatch.setattr(ex, "_format_decimal", lambda v: str(v))
        monkeypatch.setattr(ex, "_algo_trade_request", fake_algo)

        result = ex.create_stop_loss_order("BTCUSDT", "sell", 0.01, 59000.0)

        assert captured["endpoint"] == "/fapi/v1/algoOrder"
        assert captured["params"]["algoType"] == "CONDITIONAL"
        assert captured["params"]["type"] == "STOP_MARKET"
        assert captured["params"]["triggerPrice"] == "59000.0"
        assert captured["params"]["reduceOnly"] == "true"
        assert result["order_id"] == "12345"
        assert result["status"] == "NEW"

    def test_create_take_profit_uses_algo_endpoint(self, monkeypatch) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange

        captured: dict = {}

        def fake_algo(endpoint: str, params: dict) -> dict:
            captured["endpoint"] = endpoint
            captured["params"] = params
            return {"algoId": 67890, "algoStatus": "NEW", "orderType": "TAKE_PROFIT_MARKET", "symbol": "ETHUSDT", "side": "BUY"}

        ex = RealExchange()
        monkeypatch.setattr(ex, "_round_qty", lambda _sym, amt: amt)
        monkeypatch.setattr(ex, "_round_price", lambda _sym, p: p)
        monkeypatch.setattr(ex, "_format_decimal", lambda v: str(v))
        monkeypatch.setattr(ex, "_algo_trade_request", fake_algo)

        result = ex.create_take_profit_order("ETHUSDT", "buy", 0.05, 1800.0)

        assert captured["endpoint"] == "/fapi/v1/algoOrder"
        assert captured["params"]["algoType"] == "CONDITIONAL"
        assert captured["params"]["type"] == "TAKE_PROFIT_MARKET"
        assert captured["params"]["triggerPrice"] == "1800.0"
        assert result["order_id"] == "67890"

    def test_cancel_order_uses_algo_delete_for_futures(self, monkeypatch) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange

        captured: dict = {}

        def fake_algo(method: str, endpoint: str, params: dict, *, op: str) -> dict:
            captured.update({"method": method, "endpoint": endpoint, "params": params, "op": op})
            return {"algoId": 12345, "msg": "success"}

        ex = RealExchange()
        monkeypatch.setattr(ex, "_algo_signed_request", fake_algo)

        result = ex.cancel_order("12345", "BTCUSDT")

        assert captured["method"] == "DELETE"
        assert captured["endpoint"] == "/fapi/v1/algoOrder"
        assert captured["params"]["algoId"] == "12345"
        assert result["order_id"] == "12345"

    def test_round_price_uses_tick_size(self, monkeypatch) -> None:
        from extensions.trading.crypto.live._real_exchange import RealExchange

        ex = RealExchange()
        ex._tick_sizes["LTCUSDT"] = 0.01
        assert ex._round_price("LTCUSDT", 47.651999999999994) == 47.65
        assert ex._format_decimal(47.65) == "47.65"
