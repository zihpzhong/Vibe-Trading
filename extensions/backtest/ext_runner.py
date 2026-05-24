"""Extension-only backtest runner for ``astock`` / ``crypto_live`` engines.

Per docs/extension-guide.md: do not patch ``agent/backtest/runner.py``.
Use this entry from ``extensions/cli/run_*_backtest.py --runner`` instead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_VALID_ENGINES = frozenset({"astock", "crypto_live"})


def main(run_dir: Path) -> None:
    """Run extension backtest engines from a run directory (config.json + code/)."""
    agent_root = Path(__file__).resolve().parents[2] / "agent"
    project_root = agent_root.parent
    for p in (str(project_root), str(agent_root)):
        if p not in sys.path:
            sys.path.insert(0, p)

    from backtest.runner import (  # noqa: PLC0415 — agent import, read-only
        _detect_market,
        _detect_primary_source,
        _get_loader,
        _load_module_from_file,
        _normalize_codes,
    )
    from backtest.metrics import calc_bars_per_year

    run_dir = Path(run_dir)
    config_path = run_dir / "config.json"
    if not config_path.exists():
        print(json.dumps({"error": "config.json not found"}))
        sys.exit(1)

    config: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    engine_type = config.get("engine", "")
    if engine_type not in _VALID_ENGINES:
        print(
            json.dumps(
                {
                    "error": f"unsupported engine {engine_type!r}; "
                    f"use extensions/backtest/ext_runner.py with {_VALID_ENGINES}",
                }
            )
        )
        sys.exit(1)

    signal_path = run_dir / "code" / "signal_engine.py"
    if not signal_path.exists():
        print(json.dumps({"error": "code/signal_engine.py not found"}))
        sys.exit(1)

    signal_module = _load_module_from_file(signal_path, "signal_engine")
    engine_cls = getattr(signal_module, "SignalEngine", None)
    if engine_cls is None:
        print(json.dumps({"error": "SignalEngine class not found"}))
        sys.exit(1)

    source = config.get("source", "ccxt")
    codes = config.get("codes", [])
    interval = config.get("interval", "1D")
    codes = _normalize_codes(codes, source)
    config["codes"] = codes

    LoaderCls = _get_loader(source)
    loader = LoaderCls()
    data_map = loader.fetch(
        codes,
        config.get("start_date", ""),
        config.get("end_date", ""),
        fields=config.get("extra_fields") or None,
        interval=interval,
    )
    if not data_map:
        print(json.dumps({"error": "No data fetched"}))
        sys.exit(1)

    config["_run_card_effective_sources"] = [source]
    signal_engine = engine_cls()

    effective_source = _detect_primary_source(codes, source)
    market_types = {_detect_market(c) for c in codes}
    bars_per_year = None if len(market_types) > 1 else calc_bars_per_year(interval, effective_source)

    if engine_type == "astock":
        from extensions.trading.astock.backtest.engine import (
            AStockBacktestEngine,
            _NullSignalEngine,
        )

        signal_engine = _NullSignalEngine()
        market_engine = AStockBacktestEngine(config)
    else:
        from extensions.trading.crypto.backtest.engine import (
            CryptoLiveBacktestEngine,
            _NullSignalEngine,
        )

        signal_engine = _NullSignalEngine()
        market_engine = CryptoLiveBacktestEngine(config)

    market_engine.run_backtest(config, loader, signal_engine, run_dir, bars_per_year=bars_per_year)
