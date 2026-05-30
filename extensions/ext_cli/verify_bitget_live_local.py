#!/usr/bin/env python3
"""Local verification: Bitget agent.json + mandate + live runner API.

Usage:
    PYTHONPATH=agent:extensions python3.12 extensions/ext_cli/verify_bitget_live_local.py
    PYTHONPATH=agent:extensions python3.12 extensions/ext_cli/verify_bitget_live_local.py --skip-server
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ROOT = _PROJECT_ROOT / "agent"
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_AGENT_ROOT))

API_PORT = int(os.environ.get("VIBE_TRADING_VERIFY_PORT", "8899"))
API_BASE = f"http://127.0.0.1:{API_PORT}"
BROKER = "bitget"


def _load_dotenv() -> None:
    env_path = _AGENT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except ImportError:
        pass


def _step(msg: str) -> None:
    print(f"\n==> {msg}")


def _ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def _fail(msg: str) -> int:
    print(f"  [FAIL] {msg}")
    return 1


def setup_agent_json(include_write: bool) -> int:
    from extensions.live.bitget_agent_setup import merge_bitget_into_agent_json, setup_status
    from extensions.live.bitget_bridge import patch_upstream

    patch_upstream()
    path = merge_bitget_into_agent_json(include_write_tools=include_write)
    _ok(f"agent.json updated: {path}")
    status = setup_status(path)
    print(f"  status: {json.dumps(status, ensure_ascii=False)}")
    if not status.get("credentials_in_env"):
        return _fail("BITGET_* not set in environment — load agent/.env first")
    return 0


def seed_mandate() -> int:
    from extensions.live.bitget_bridge import patch_upstream
    from extensions.live.bitget_mcp_seed import BITGET_BROKER_KEY
    from src.live.mandate.commit import commit_mandate
    from src.live.mandate.store import load_mandate
    from src.tools.propose_mandate_tool import ProposeMandateProfilesTool

    patch_upstream()
    ceilings = {
        "account_funding_usd": 500.0,
        "max_order_usd": 50.0,
        "max_total_exposure_usd": 200.0,
        "daily_trade_cap": 5,
        "leverage": 2,
        "instruments": ["crypto"],
        "asset_classes": ["crypto"],
        "universe": ["BTCUSDT", "ETHUSDT"],
    }
    raw = ProposeMandateProfilesTool().execute(
        broker=BITGET_BROKER_KEY,
        intent="local verify bitget live",
        ceilings=ceilings,
        session_id="verify_local",
    )
    proposal = json.loads(raw)
    if proposal.get("type") != "mandate.proposal":
        return _fail(f"propose failed: {proposal}")
    result = commit_mandate(
        proposal_id=proposal["proposal_id"],
        ordinal=2,
        adjustments=None,
        consent_ack=True,
        broker=BITGET_BROKER_KEY,
        account_ref="bitget_verify_local",
        session_id="verify_local",
    )
    mandate = load_mandate(BITGET_BROKER_KEY)
    if mandate is None:
        return _fail("mandate not on disk after commit")
    _ok(f"mandate committed: {result.get('mandate_id')}")
    return 0


def wait_health(timeout_s: float = 45.0) -> int:
    import httpx

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = httpx.get(f"{API_BASE}/health", timeout=3.0)
            if r.status_code == 200:
                _ok(f"API health @ {API_BASE}")
                return 0
        except Exception:
            pass
        time.sleep(0.5)
    return _fail(f"API not healthy at {API_BASE} within {timeout_s}s")


def api_live_flow() -> int:
    import httpx

    headers = {}
    api_key = os.environ.get("API_AUTH_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    r = httpx.get(f"{API_BASE}/live/status", params={"broker": BROKER}, headers=headers, timeout=30.0)
    if r.status_code != 200:
        return _fail(f"GET /live/status -> {r.status_code} {r.text[:300]}")
    body = r.json()
    _ok(f"live/status: brokers={len(body.get('brokers', []))}")
    for b in body.get("brokers", []):
        if b.get("auth", {}).get("broker") == BROKER:
            print(
                f"    oauth_token_present={b['auth'].get('oauth_token_present')} "
                f"is_live_broker={b['auth'].get('is_live_broker')} "
                f"mandate={'yes' if b.get('mandate') else 'no'}"
            )

    r2 = httpx.post(
        f"{API_BASE}/live/runner/start",
        json={"broker": BROKER, "foreground": False},
        headers=headers,
        timeout=60.0,
    )
    if r2.status_code == 200:
        _ok(f"runner/start: {r2.json()}")
    elif r2.status_code == 503:
        detail = r2.json().get("detail", r2.text)
        print(f"  [WARN] runner/start 503 (MCP/channel): {detail}")
        print("  This is expected if bitget MCP subprocess cannot connect yet.")
    else:
        return _fail(f"POST /live/runner/start -> {r2.status_code} {r2.text[:400]}")

    r3 = httpx.post(
        f"{API_BASE}/live/runner/stop",
        json={"broker": BROKER},
        headers=headers,
        timeout=30.0,
    )
    if r3.status_code == 200:
        _ok(f"runner/stop: {r3.json()}")
    else:
        print(f"  [WARN] runner/stop -> {r3.status_code}")

    return 0


def cli_live_start() -> int:
    env = os.environ.copy()
    env["VIBE_TRADING_API_URL"] = API_BASE
    env["PYTHONPATH"] = f"{_AGENT_ROOT}:{_PROJECT_ROOT}"
    proc = subprocess.run(
        [sys.executable, "-m", "cli", "live", "start", BROKER],
        cwd=str(_AGENT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        return _fail(f"cli live start exit {proc.returncode}")
    _ok("vibe-trading live start bitget (CLI relay)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-server", action="store_true", help="Assume serve already running")
    parser.add_argument("--no-cli", action="store_true", help="Skip CLI live start test")
    args = parser.parse_args()

    _load_dotenv()
    os.environ.setdefault("VIBE_TRADING_API_URL", API_BASE)

    rc = 0
    _step("Setup agent.json (bitget MCP)")
    rc = setup_agent_json(include_write=False) or rc

    _step("Seed test mandate (propose + commit)")
    rc = seed_mandate() or rc

    server_proc = None
    if not args.skip_server:
        _step(f"Start API server on :{API_PORT}")
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{_AGENT_ROOT}:{_PROJECT_ROOT}"
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "extensions.live.serve_with_bridge:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(API_PORT),
            ],
            cwd=str(_PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if wait_health() != 0:
            if server_proc.stdout:
                out = server_proc.stdout.read(4000) if hasattr(server_proc.stdout, "read") else ""
                print(out)
            server_proc.terminate()
            return 1

    _step("API live/status + runner start/stop")
    rc = api_live_flow() or rc

    if not args.no_cli and not args.skip_server:
        _step("CLI: live start bitget")
        rc = cli_live_start() or rc

    if server_proc is not None:
        _step("Stop API server")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        _ok("server stopped")

    _step("Summary")
    if rc == 0:
        print("  Local Bitget live verification completed.")
    else:
        print("  Completed with warnings/failures — see messages above.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
