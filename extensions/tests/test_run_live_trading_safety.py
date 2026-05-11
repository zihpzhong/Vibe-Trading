"""Safety tests for the live trading entrypoint."""

from __future__ import annotations

import sys
from unittest.mock import patch

from extensions import run_live_trading


class TestRunLiveTradingArgs:
    def test_default_mode_is_dry_run(self) -> None:
        with patch.object(sys, "argv", ["run_live_trading.py"]):
            args = run_live_trading.parse_args()
        assert args.dry_run is True
        assert args.live is False

    def test_live_requires_explicit_confirm_phrase(self) -> None:
        with patch.object(sys, "argv", ["run_live_trading.py", "--live"]):
            args = run_live_trading.parse_args()
        assert run_live_trading.validate_live_mode(args) != ""

    def test_live_confirm_allows_trading(self) -> None:
        with patch.object(sys, "argv", ["run_live_trading.py", "--live", "--confirm-live", "I_UNDERSTAND"]):
            args = run_live_trading.parse_args()
        assert args.dry_run is False
        assert run_live_trading.validate_live_mode(args) == ""

    def test_minimum_notional_check_detects_default_small_account(self) -> None:
        notional = run_live_trading.estimate_max_order_notional(
            balance=50.0,
            position_size=0.05,
            max_leverage=5,
            max_position_pct=20.0,
        )
        assert notional == 12.5
        assert run_live_trading.validate_min_order_notional(notional, min_notional=20.0) != ""
