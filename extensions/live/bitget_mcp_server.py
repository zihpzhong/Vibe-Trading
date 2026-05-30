"""Bitget MCP Live Broker server — stdio FastMCP server for Bitget exchange.

Wraps BitgetExchange (or MockExchange) methods as 6 MCP tools the upstream
live-trading agent calls through the mandate gate + order guard. Runs as a
stdio subprocess (no HTTP/OAuth) — API keys are read from env vars.

Usage:
    # List tools (mock mode, no API keys needed)
    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \\
      | python extensions/live/bitget_mcp_server.py --mock | head -5

    # Real mode (requires BITGET_API_KEY, BITGET_SECRET, BITGET_PASSPHRASE)
    python extensions/live/bitget_mcp_server.py

Tools:
    get_account      → exchange.get_account_balance()
    get_positions    → exchange.get_positions()
    get_quotes       → exchange.get_ticker(symbol)
    list_orders      → exchange.fetch_open_orders(symbol)
    place_order      → exchange.create_market_order() (notional→qty conversion)
    cancel_order     → exchange.cancel_order(order_id, symbol)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure the project root is on sys.path so ``extensions.trading.crypto.live``
# and ``extensions.backtest`` modules are importable when this script runs as a
# standalone stdio subprocess (the normal transport mode).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exchange factory (lazy import to keep server start fast)
# ---------------------------------------------------------------------------

def _create_exchange(mock: bool = False) -> Any:
    """Create a BitgetExchange or MockExchange instance.

    Args:
        mock: If True, return a MockExchange (no API keys needed).

    Returns:
        An ExchangeBase-compatible instance.
    """
    if mock:
        # Import from the crypto live module — handles both real and mock.
        from extensions.trading.crypto.live.exchange import MockExchange

        return MockExchange(seed_price=65_000.0)

    from extensions.trading.crypto.live._bitget_exchange import BitgetExchange

    return BitgetExchange()


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

def _create_server(mock: bool = False) -> Any:
    """Build and return the FastMCP server instance.

    Args:
        mock: If True, use MockExchange.

    Returns:
        A configured FastMCP server.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise ImportError(
            "The 'mcp' package is required. Install it with: pip install mcp"
        ) from None

    exchange = _create_exchange(mock)
    server = FastMCP("bitget-live-broker")

    # ------------------------------------------------------------------
    # READ tools
    # ------------------------------------------------------------------

    @server.tool()
    def get_account() -> str:
        """获取 Bitget 账户余额 / Get Bitget account balance.

        Returns:
            JSON string with equity and balance info.
        """
        try:
            snap = exchange.get_balance_snapshot()
            result = {
                "equity": snap["total"],
                "available": snap["free"],
            }
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @server.tool()
    def get_positions() -> str:
        """获取 Bitget 当前持仓 / Get Bitget open positions.

        Returns:
            JSON string with list of open positions.
        """
        try:
            positions = exchange.get_positions()
            return json.dumps(positions, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @server.tool()
    def get_quotes(symbol: str) -> str:
        """获取指定交易对的最新报价 / Get latest quote for a symbol.

        Args:
            symbol: Trading pair symbol, e.g. "BTCUSDT".

        Returns:
            JSON string with ticker data.
        """
        try:
            ticker = exchange.get_ticker(symbol)
            return json.dumps(ticker, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @server.tool()
    def list_orders(symbol: str = "") -> str:
        """获取当前未成交订单列表 / List open orders, optionally filtered by symbol.

        Args:
            symbol: Optional trading pair symbol, e.g. "BTCUSDT". Empty = all.

        Returns:
            JSON string with list of open orders.
        """
        try:
            if hasattr(exchange, "fetch_open_orders"):
                orders = exchange.fetch_open_orders(symbol if symbol else None)
            elif hasattr(exchange, "fetch_open_algo_orders"):
                orders = exchange.fetch_open_algo_orders(symbol if symbol else None)
            else:
                orders = []
            return json.dumps(orders, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # WRITE tools
    # ------------------------------------------------------------------

    @server.tool()
    def place_order(
        symbol: str,
        side: str,
        notional_usd: float | None = None,
        quantity: float | None = None,
        order_type: str = "market",
        price: float | None = None,
        reduce_only: bool = False,
    ) -> str:
        """开仓/平仓 / Place an order on Bitget.

        Accepts ``notional_usd`` or ``quantity`` (not both — when both are
        provided, quantity takes precedence). When only ``notional_usd`` is
        given, the server internally converts it to quantity using the latest
        ticker price.

        Args:
            symbol: Trading pair symbol, e.g. "BTCUSDT".
            side: Order side: "buy" or "sell".
            notional_usd: Optional order value in USD (alternative to quantity).
            quantity: Optional coin quantity (alternative to notional_usd).
            order_type: Order type: "market" or "limit" (default "market").
            price: Limit price (required for limit orders).
            reduce_only: If True, order only reduces position (default False).

        Returns:
            JSON string with order result.
        """
        try:
            # Resolve quantity from notional if needed.
            qty = quantity
            if qty is None and notional_usd is not None:
                ticker = exchange.get_ticker(symbol)
                last_price = float(ticker.get("last", 0) or 0)
                if last_price <= 0:
                    return json.dumps(
                        {"error": f"cannot resolve price for {symbol} — last price is {last_price}"},
                        ensure_ascii=False,
                    )
                qty = notional_usd / last_price

            if qty is None or qty <= 0:
                return json.dumps(
                    {"error": "either notional_usd or quantity is required with a positive value"},
                    ensure_ascii=False,
                )

            if order_type == "limit":
                if price is None or price <= 0:
                    return json.dumps(
                        {"error": "price is required for limit orders"},
                        ensure_ascii=False,
                    )
                result = exchange.create_limit_order(symbol, side, qty, price)
            else:
                result = exchange.create_market_order(symbol, side, qty, reduce_only)

            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @server.tool()
    def cancel_order(order_id: str, symbol: str) -> str:
        """取消订单 / Cancel an open order by ID.

        Args:
            order_id: The order ID to cancel.
            symbol: Trading pair symbol, e.g. "BTCUSDT".

        Returns:
            JSON string with cancellation result.
        """
        try:
            result = exchange.cancel_order(order_id, symbol)
            return json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    return server


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the Bitget MCP Live Broker server as a stdio subprocess."""
    mock = "--mock" in sys.argv[1:]
    server = _create_server(mock)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
