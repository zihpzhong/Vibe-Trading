"""Safety tests for the live trading entrypoint."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

from extensions.ext_cli import run_live_trading


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


class TestLoadRuntimeEnv:
    def test_env_local_does_not_override_injected_bitget_key(self, tmp_path, monkeypatch) -> None:
        main_key = "bg_main_account_xx"
        sub_key = "bg_sub_account_xx"
        ext_local = tmp_path / "env.local"
        ext_local.write_text(
            f"BITGET_API_KEY={sub_key}\nBITGET_SECRET=sub_secret\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("BITGET_API_KEY", main_key)
        monkeypatch.setenv("BITGET_SECRET", "main_secret")
        monkeypatch.setattr(run_live_trading, "_EXT_ENV_LOCAL", ext_local)
        monkeypatch.setattr(run_live_trading, "_AGENT_ENV", tmp_path / "missing.env")

        run_live_trading._load_runtime_env()

        assert os.environ["BITGET_API_KEY"] == main_key
        assert os.environ["BITGET_SECRET"] == "main_secret"

    def test_env_local_overrides_when_bitget_key_absent(self, tmp_path, monkeypatch) -> None:
        sub_key = "bg_sub_from_local"
        ext_local = tmp_path / "env.local"
        ext_local.write_text(f"BITGET_API_KEY={sub_key}\n", encoding="utf-8")
        monkeypatch.delenv("BITGET_API_KEY", raising=False)
        monkeypatch.setattr(run_live_trading, "_EXT_ENV_LOCAL", ext_local)
        monkeypatch.setattr(run_live_trading, "_AGENT_ENV", tmp_path / "missing.env")

        run_live_trading._load_runtime_env()

        assert os.environ.get("BITGET_API_KEY") == sub_key

    def test_skip_env_local_flag(self, tmp_path, monkeypatch) -> None:
        ext_local = tmp_path / "env.local"
        ext_local.write_text("BITGET_API_KEY=should_not_load\n", encoding="utf-8")
        monkeypatch.delenv("BITGET_API_KEY", raising=False)
        monkeypatch.setenv("VIBE_AUT_SKIP_ENV_LOCAL", "1")
        monkeypatch.setattr(run_live_trading, "_EXT_ENV_LOCAL", ext_local)
        monkeypatch.setattr(run_live_trading, "_AGENT_ENV", tmp_path / "missing.env")

        run_live_trading._load_runtime_env()

        assert "BITGET_API_KEY" not in os.environ
