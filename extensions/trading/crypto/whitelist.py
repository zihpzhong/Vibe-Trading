"""Top-50 cryptocurrency whitelist for live trading.

All symbols exclude stablecoins (USDT, USDC, USDe, DAI, FDUSD, etc.) and
liquid-staking derivatives (STETH, WBTC, WETH) since they have no independent
price discovery.

Usage:
    # From JSON config (recommended for portability)
    from extensions.trading.crypto.whitelist import load_whitelist
    wl = load_whitelist()
    scheduler.run_once(whitelist=wl.symbols)

    # Or inline from presets
    from extensions.trading.crypto.whitelist import TOP_50
    scheduler.run_once(whitelist=TOP_50)

    # With config
    from extensions.trading.crypto.config import LiveTradingConfig
    config = LiveTradingConfig(pair_whitelist=TOP_50)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Tier 1 — Blue-chip, deepest liquidity, top 10 by market cap
# ---------------------------------------------------------------------------
TIER_1: Final[list[str]] = [
    "BTC",   # Bitcoin
    "ETH",   # Ethereum
    "BNB",   # BNB
    "XRP",   # XRP
    "SOL",   # Solana
    "TRX",   # TRON
    "DOGE",  # Dogecoin
    "HYPE",  # Hyperliquid
    "ADA",   # Cardano
    "AVAX",  # Avalanche
]

# ---------------------------------------------------------------------------
# Tier 2 — Major L1/L2 platforms + high-volume blue chips, rank 11–25
# ---------------------------------------------------------------------------
TIER_2: Final[list[str]] = [
    "DOT",   # Polkadot
    "LINK",  # Chainlink
    "TON",   # Toncoin
    "LTC",   # Litecoin
    "BCH",   # Bitcoin Cash
    "NEAR",  # NEAR Protocol
    "UNI",   # Uniswap
    "POL",   # Polygon (ex-MATIC)
    "APT",   # Aptos
    "SUI",   # Sui
    "ATOM",  # Cosmos
    "INJ",   # Injective
    "FIL",   # Filecoin
]

# ---------------------------------------------------------------------------
# Tier 3 — Mid-cap with sufficient liquidity, rank 26–50
# ---------------------------------------------------------------------------
TIER_3: Final[list[str]] = [
    "AAVE",   # Aave
    "HBAR",   # Hedera
    "RENDER", # Render
    "KAS",    # Kaspa
    "STX",    # Stacks
    "ARB",    # Arbitrum
    "FET",    # Fetch.ai
    "VET",    # VeChain
    "RUNE",   # THORChain
    "ALGO",   # Algorand
    "IMX",    # Immutable
    "GRT",    # The Graph
    "PENDLE", # Pendle
    "TIA",    # Celestia
    "FLOW",   # Flow
    "CRV",    # Curve DAO
    "OP",     # Optimism
    "MINA",   # Mina
    "SEI",    # Sei
    "THETA",  # Theta Network
    "JUP",    # Jupiter
]

# ---------------------------------------------------------------------------
# Composite lists
# ---------------------------------------------------------------------------
TOP_50: Final[list[str]] = TIER_1 + TIER_2 + TIER_3

TOP_50_SYMBOLS: Final[list[str]] = [f"{c}USDT" for c in TOP_50]


# ---------------------------------------------------------------------------
# JSON config loading
# ---------------------------------------------------------------------------

@dataclass
class WhitelistProfile:
    """Loaded whitelist profile from a JSON config file."""

    name: str
    description: str
    version: str
    tiers: dict[str, list[dict]] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    symbols_usdt: list[str] = field(default_factory=list)
    coin_meta: dict[str, dict] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.symbols)


_JSON_PATH: Final[Path] = Path(__file__).with_name("whitelist.json")


@cache
def load_whitelist(path: str | Path | None = None) -> WhitelistProfile:
    """Load whitelist from a JSON config file.

    Args:
        path: Path to JSON file. Defaults to ``whitelist.json`` alongside this module.

    Returns:
        ``WhitelistProfile`` with parsed tiers, symbol list, and metadata.

    The result is cached so repeated calls hit memory (use ``load_whitelist.cache_clear()``
    to reload from disk).
    """
    src = Path(path) if path else _JSON_PATH
    raw = json.loads(src.read_text(encoding="utf-8"))

    all_coins: list[dict] = []
    coin_meta: dict[str, dict] = {}
    tiers: dict[str, list[dict]] = {}

    for tier_key, tier_data in raw["tiers"].items():
        coins = tier_data["coins"]
        tiers[tier_key] = coins
        all_coins.extend(coins)
        for c in coins:
            coin_meta[c["symbol"]] = c

    symbols = [c["symbol"] for c in all_coins]
    symbols_usdt = [f"{c['symbol']}USDT" for c in all_coins]

    return WhitelistProfile(
        name=raw["meta"]["name"],
        description=raw["meta"]["description"],
        version=raw["meta"]["version"],
        tiers=tiers,
        symbols=symbols,
        symbols_usdt=symbols_usdt,
        coin_meta=coin_meta,
    )


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def is_whitelisted(base_currency: str) -> bool:
    """Check if a base currency (e.g. ``\"SOL\"``) is in the top-50 whitelist."""
    return base_currency.upper().removesuffix("USDT") in TOP_50


def whitelisted_filter(tickers: list[dict]) -> list[dict]:
    """Filter a ticker list to only whitelisted pairs.

    Each ticker dict must have a ``\"symbol\"`` key like ``\"SOLUSDT\"``.
    """
    wl = set(TOP_50)
    return [t for t in tickers if t["symbol"].removesuffix("USDT") in wl]
