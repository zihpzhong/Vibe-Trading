"""Run CryptoLiveBacktestEngine — scanner-based crypto backtest.

Usage:
    python extensions/run_crypto_backtest.py
    python extensions/run_crypto_backtest.py --fetch-only
    python extensions/run_crypto_backtest.py --synthetic --start 2024-01-01 --end 2024-06-01
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_AGENT_ROOT = _PROJECT_ROOT / "agent"
for _p in (_PROJECT_ROOT, _AGENT_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from extensions.live_trading.crypto_backtest.engine import CryptoLiveBacktestEngine, _NullSignalEngine
from extensions.live_trading.crypto_backtest.exchange import normalize_symbol

CACHE_DIR = Path(__file__).resolve().parent / ".cache_crypto"
DEFAULT_CODES = ["BTCUSDT", "ETHUSDT"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Crypto live-trading backtest (scanner pipeline)")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2024-06-01")
    p.add_argument("--interval", default="1h", choices=["1h", "4h", "1D"])
    p.add_argument("--codes", nargs="*", default=None, help="Symbols e.g. BTCUSDT ETHUSDT")
    p.add_argument("--initial-cash", type=float, default=50.0)
    p.add_argument("--fetch-only", action="store_true")
    p.add_argument("--synthetic", action="store_true", help="Use synthetic OHLCV (no network)")
    p.add_argument("--no-whitelist", action="store_true", help="Set enforce_whitelist=false in config")
    p.add_argument(
        "--runner",
        action="store_true",
        help="Run via extensions/ext_backtest/ext_runner.py + run_dir (per extension-guide)",
    )
    p.add_argument("--top50", action="store_true", help="Top50 whitelist (uses first N with --max-symbols)")
    p.add_argument(
        "--max-symbols",
        type=int,
        default=8,
        help="Cap universe when using --top50 (0 = all whitelist symbols)",
    )
    p.add_argument(
        "--proxy",
        default=None,
        help="HTTP proxy for CCXT (port e.g. 7897 or full URL http://127.0.0.1:7897)",
    )
    p.add_argument("--scan-every", type=int, default=12, help="Scan every N bars (default 12 for speed)")
    p.add_argument("--replay-phase2", action="store_true", help="Enable Phase 2 replay (verdict gating)")
    p.add_argument(
        "--replay-swarm",
        action="store_true",
        help="Enable Swarm Phase 2 replay (swarm consensus gating)",
    )
    return p.parse_args()


def _synthetic_ohlcv(
    symbol: str,
    start: str,
    end: str,
    interval: str = "1h",
    seed: int = 42,
) -> pd.DataFrame:
    """Deterministic trending OHLCV for tests and offline runs."""
    freq = {"1h": "1h", "4h": "4h", "1D": "1D"}.get(interval, "1h")
    dates = pd.date_range(start, end, freq=freq, inclusive="left")
    if len(dates) < 50:
        dates = pd.date_range(start, periods=200, freq=freq)
    rng = np.random.default_rng(seed + hash(symbol) % 10000)
    base = 65_000.0 if "BTC" in symbol else 3_200.0
    n = len(dates)
    rets = rng.normal(0.0002, 0.008, n)
    close = base * np.cumprod(1 + rets)
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    open_ = np.roll(close, 1)
    open_[0] = base
    vol = rng.uniform(100, 1000, n) * 1e6
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=dates,
    )


def expected_bars(start: str, end: str, interval: str) -> int:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    delta = end_ts - start_ts
    if interval == "1h":
        return max(1, int(delta.total_seconds() // 3600))
    if interval == "4h":
        return max(1, int(delta.total_seconds() // 14400))
    return max(1, int(delta.days))


def filter_universe_by_coverage(
    data_map: dict[str, pd.DataFrame],
    start: str,
    end: str,
    interval: str,
    min_coverage: float = 0.90,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Drop symbols with insufficient bars in [start, end]."""
    exp = expected_bars(start, end, interval)
    kept: dict[str, pd.DataFrame] = {}
    dropped: list[str] = []
    for sym, df in data_map.items():
        if df is None or df.empty:
            dropped.append(f"{sym}: empty")
            continue
        mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
        actual = int(mask.sum())
        cov = actual / exp if exp > 0 else 0.0
        if cov >= min_coverage:
            kept[sym] = df
        else:
            dropped.append(f"{sym}: coverage {cov:.1%} < {min_coverage:.0%}")
    return kept, dropped


def fetch_ccxt(
    codes: list[str],
    start: str,
    end: str,
    interval: str,
    cache_dir: Path,
) -> dict[str, pd.DataFrame]:
    """Fetch via extension CCXT helper (proxy/futures) with parquet cache."""
    from extensions.ext_backtest.ccxt_helpers import fetch_ohlcv_map

    cache_dir.mkdir(parents=True, exist_ok=True)
    data_map: dict[str, pd.DataFrame] = {}
    to_fetch: list[str] = []
    for code in codes:
        sym = normalize_symbol(code)
        cache_path = cache_dir / f"{sym}_{interval}.parquet"
        if cache_path.exists():
            data_map[sym] = pd.read_parquet(cache_path)
        else:
            to_fetch.append(sym)

    if to_fetch:
        ccxt_codes = [c.replace("USDT", "/USDT") for c in to_fetch]
        chunk = fetch_ohlcv_map(ccxt_codes, start, end, interval=interval)
        for sym in to_fetch:
            ccxt_code = sym.replace("USDT", "/USDT")
            df = chunk.get(ccxt_code) or chunk.get(sym)
            if df is None and chunk:
                continue
            if df is not None and not df.empty:
                data_map[sym] = df
                (cache_dir / f"{sym}_{interval}.parquet").parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(cache_dir / f"{sym}_{interval}.parquet")
    return data_map


def build_config(args: argparse.Namespace, codes: list[str]) -> dict[str, Any]:
    config: dict[str, Any] = {
        "codes": codes,
        "start_date": args.start,
        "end_date": args.end,
        "source": "ccxt",
        "engine": "crypto_live",
        "interval": args.interval,
        "initial_cash": args.initial_cash,
        "leverage": 5,
        "max_leverage": 5,
        "position_size_pct": 0.12,
        "reward_risk_ratio": 2.0,
        "pair_whitelist": codes,
        "enforce_whitelist": not args.no_whitelist,
        "scan_top_n": 0,
        "phase2_enabled": args.replay_phase2 or args.replay_swarm,
        "maker_rate": 0.0002,
        "taker_rate": 0.0005,
        "slippage": 0.0005,
        "funding_rate": 0.0001,
        "btc_symbol": "BTCUSDT",
        "phase2_replay_swarm": args.replay_swarm,
    }
    if args.replay_phase2 or args.replay_swarm:
        # Auto-discover latest replay JSONL
        replay_dir = Path.home() / ".vibe-trading" / "logs"
        jsonl_files = sorted(replay_dir.glob("phase2_replay_*.jsonl"))
        if jsonl_files:
            config["phase2_replay_path"] = str(jsonl_files[-1])
            print(f"  Phase 2 replay: {jsonl_files[-1].name}" +
                  (" (swarm)" if args.replay_swarm else ""))
    return config


def run_direct(config: dict[str, Any], data_map: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Run engine in-process (no subprocess)."""
    codes = list(data_map.keys())
    engine = CryptoLiveBacktestEngine(config)
    signal_engine = _NullSignalEngine()

    class _Loader:
        name = "inline"

        def fetch(self, _codes, _start, _end, **_kw):  # noqa: ANN001
            return data_map

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        return engine.run_backtest(config, _Loader(), signal_engine, run_dir, bars_per_year=None)


def run_via_runner(config: dict[str, Any], run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "code").mkdir(exist_ok=True)
    (run_dir / "code" / "signal_engine.py").write_text(
        "class SignalEngine:\n    def generate(self, data_map):\n        return {c: __import__('pandas').Series(0.0, index=d.index) for c, d in data_map.items()}\n",
        encoding="utf-8",
    )
    (run_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    from extensions.ext_backtest.ext_runner import main as ext_runner_main

    ext_runner_main(run_dir)


def _apply_proxy_env(proxy: str | None) -> None:
    from extensions.ext_backtest.ccxt_helpers import apply_proxy_env

    url = apply_proxy_env(proxy)
    if url:
        print(f"CCXT proxy: {url}")


def main() -> int:
    args = parse_args()
    _apply_proxy_env(args.proxy or os.environ.get("CCXT_PROXY") or os.environ.get("CCXT_PROXY_PORT"))
    if args.top50:
        from extensions.live_trading.crypto_backtest.config import CryptoBacktestConfig

        bt = CryptoBacktestConfig.with_top50()
        wl = bt.pair_whitelist
        if args.max_symbols <= 0:
            codes = list(wl)
            print(f"Top50 mode: {len(codes)} symbols (full whitelist)")
        else:
            codes = wl[: max(2, args.max_symbols)]
            print(f"Top50 mode: {len(codes)} symbols (cap={args.max_symbols})")
    else:
        codes = [normalize_symbol(c) for c in (args.codes or DEFAULT_CODES)]
    if normalize_symbol("BTCUSDT") not in codes:
        codes.insert(0, "BTCUSDT")

    if args.synthetic:
        data_map = {
            c: _synthetic_ohlcv(c, args.start, args.end, args.interval)
            for c in codes
        }
    else:
        data_map = fetch_ccxt(codes, args.start, args.end, args.interval, CACHE_DIR)
        if not data_map:
            print("No data fetched; falling back to synthetic OHLCV")
            data_map = {
                c: _synthetic_ohlcv(c, args.start, args.end, args.interval)
                for c in codes
            }

    data_map, dropped = filter_universe_by_coverage(
        data_map, args.start, args.end, args.interval, min_coverage=0.90,
    )
    for line in dropped:
        print(f"  [coverage] dropped {line}")
    if not data_map:
        print("No symbols passed coverage filter")
        return 1

    if args.fetch_only:
        for sym, df in data_map.items():
            print(f"  {sym}: {len(df)} bars")
        return 0

    config = build_config(args, list(data_map.keys()))
    config["scan_every_n_bars"] = args.scan_every

    if args.runner:
        out = _PROJECT_ROOT / "runs" / "crypto_backtest_latest"
        run_via_runner(config, out)
        print(f"Runner finished; artifacts in {out / 'artifacts'}")
        return 0

    metrics = run_direct(config, data_map)
    trade_list = metrics.pop("trades", [])
    print(json.dumps(metrics, indent=2, default=str))
    print(f"trades: {len(trade_list)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
