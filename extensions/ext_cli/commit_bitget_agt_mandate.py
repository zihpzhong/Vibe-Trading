#!/usr/bin/env python3
"""Commit Bitget AGT mandate with leverage-aware order sizing.

上游 ``propose_mandate_profiles`` 最激进档仅 30% 名义仓位，10X 下保证金过小。
本脚本按保证金目标（~12% funding）生成更大名义上限的 proposal 并 commit。

Usage:
    set -a && source agent/.env.agt && set +a
    PYTHONPATH=agent:. python3 extensions/ext_cli/commit_bitget_agt_mandate.py
    PYTHONPATH=agent:. python3 extensions/ext_cli/commit_bitget_agt_mandate.py --ordinal 3
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ROOT = _PROJECT_ROOT / "agent"
for path in (_PROJECT_ROOT, _AGENT_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_UNIVERSE = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "AVAXUSDT",
    "DOTUSDT",
    "SUIUSDT",
]


def _build_profiles(funding: float) -> list[dict[str, Any]]:
    """Build 3 profiles; max_order aligned with sizing engine ceiling (~90% funding)."""
    from extensions.live.agt_position_sizing import compute_entry_size, load_sizing_config

    cfg = load_sizing_config()
    plan = compute_entry_size(
        equity_usdt=funding,
        available_usdt=funding * 0.92,
        exposure_notional_usdt=0.0,
        open_positions=0,
        mandate_max_order_notional=round(funding * 0.95, 2),
        mandate_max_total_exposure=round(funding * 0.95, 2),
        mandate_account_funding=funding,
        config=cfg,
    )
    max_exposure = round(funding * 0.95, 2)
    max_order = round(funding * 0.90, 2)
    target = plan.notional_target_usdt
    half = round(target * 0.55, 2)
    quarter = round(target * 0.25, 2)
    return [
        {
            "ordinal": 1,
            "label": "稳健",
            "universe": _UNIVERSE,
            "max_order_usd": max(quarter, round(funding * 0.25, 2)),
            "max_total_exposure_usd": max_exposure,
            "daily_trade_cap": 3,
            "leverage": 8,
            "instruments": ["crypto"],
            "flatten_on_halt": False,
            "notes": f"~25% sizing target notional (~${quarter:.0f}).",
        },
        {
            "ordinal": 2,
            "label": "均衡",
            "universe": _UNIVERSE,
            "max_order_usd": max(half, round(funding * 0.55, 2)),
            "max_total_exposure_usd": max_exposure,
            "daily_trade_cap": 5,
            "leverage": 8,
            "instruments": ["crypto"],
            "flatten_on_halt": False,
            "notes": f"~55% sizing target notional (~${half:.0f}).",
        },
        {
            "ordinal": 3,
            "label": "激进",
            "universe": _UNIVERSE,
            "max_order_usd": max_order,
            "max_total_exposure_usd": max_exposure,
            "daily_trade_cap": 8,
            "leverage": 8,
            "instruments": ["crypto"],
            "flatten_on_halt": False,
            "notes": f"Full sizing target ~${target:.0f} notional at 8X.",
        },
    ]


def commit_leveraged_mandate(*, ordinal: int = 3, account_ref: str = "bitget_agt") -> dict[str, Any]:
    """Fetch live balance, propose leverage-aware profiles, commit selected ordinal."""
    from extensions.live.bitget_bridge import patch_upstream
    from extensions.live.bitget_mcp_seed import BITGET_BROKER_KEY
    from extensions.live.agt_position_sizing import estimate_kelly
    from extensions.trading.crypto.live._bitget_exchange import BitgetExchange
    from src.live.mandate.commit import commit_mandate, save_proposal
    from src.live.mandate.store import load_mandate

    patch_upstream()
    ex = BitgetExchange()
    funding = math.floor(float(ex.get_account_balance().get("USDT", 0) or 0) * 100) / 100
    if funding <= 0:
        raise RuntimeError("Bitget balance unavailable — check API keys / proxy")

    profiles = _build_profiles(funding)
    max_order_ceiling = round(funding * 0.90, 2)
    ceilings = {
        "account_funding_usd": funding,
        "max_order_usd": max_order_ceiling,
        "max_total_exposure_usd": round(funding * 0.95, 2),
        "daily_trade_cap": 8,
        "leverage": 8,
        "instruments": ["crypto"],
        "asset_classes": ["crypto"],
        "universe": _UNIVERSE,
    }

    proposal_id = f"mp_{uuid.uuid4().hex}"
    payload: dict[str, Any] = {
        "type": "mandate.proposal",
        "proposal_id": proposal_id,
        "session_id": "commit_bitget_agt_mandate",
        "intent_normalized": "Bitget AGT Kelly-fraction mandate",
        "account": {"broker": BITGET_BROKER_KEY, "type": "margin", "funded_by": "user"},
        "ceilings_ref": f"caps_{uuid.uuid4().hex}",
        "ceilings": ceilings,
        "profiles": profiles,
        "funding_note": "Funding from live Bitget sub-account balance.",
        "halt_note": "随时一句『停』= kill switch.",
    }
    save_proposal(payload)
    result = commit_mandate(
        proposal_id=proposal_id,
        ordinal=ordinal,
        adjustments=None,
        consent_ack=True,
        broker=BITGET_BROKER_KEY,
        account_ref=account_ref,
        session_id="commit_bitget_agt_mandate",
    )
    mandate = load_mandate(BITGET_BROKER_KEY)
    if mandate is None:
        raise RuntimeError("mandate missing after commit")

    caps = mandate.hard_caps
    kelly = estimate_kelly()
    return {
        "balance_usdt": funding,
        "mandate_id": result.get("mandate_id"),
        "ordinal": ordinal,
        "account_funding_usd": caps.account_funding_usd,
        "max_order_notional_usd": caps.max_order_notional_usd,
        "max_total_exposure_usd": caps.max_total_exposure_usd,
        "kelly_full": kelly.kelly_full,
        "kelly_scaled": kelly.kelly_scaled,
        "margin_pct": kelly.margin_pct,
        "margin_at_10x": round(caps.max_order_notional_usd / 10.0, 2),
        "margin_at_8x": round(caps.max_order_notional_usd / 8.0, 2),
        "profiles": profiles,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Commit Bitget AGT mandate with larger notional caps")
    parser.add_argument("--ordinal", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--account-ref", default="bitget_agt")
    args = parser.parse_args()

    info = commit_leveraged_mandate(ordinal=args.ordinal, account_ref=args.account_ref)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
