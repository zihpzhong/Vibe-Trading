"""Bitget Live Broker — upstream data-structure bridge.

Monkey-patches the upstream's module-level constants so the existing live-broker
pipeline (``is_live_broker``, ``wrap_live_broker_tools``, ``get_extractor``)
works for ``"bitget"`` without modifying any ``agent/src/`` source files.

This module is imported by ``agent/src/tools/ext_bridge.py`` on every server
startup (that file is the one file the extension guide permits modifying in
``agent/src/``). An import of this module is idempotent — calling
:func:`patch_upstream` more than once is safe.

Extension guide principle
    "不修改上游文件（除 .gitignore 和 ext_bridge.py）"
    → The patching approach uses Python's dynamic nature to register bitget
      at runtime rather than editing upstream source.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False

#: Live broker keys registered by this extension (stdio MCP, no OAuth).
_EXTRA_LIVE_KEYS: frozenset[str] = frozenset({"bitget"})


def patch_upstream() -> None:
    """Register ``bitget`` in the upstream live-broker pipeline at runtime.

    Patches:

    1. ``registry.is_live_broker`` — ``"bitget"`` → live broker.
    2. ``registry._BROKER_CURATED_MAPS`` — classification map.
    3. ``registry._HOST_SUFFIX_TO_BROKER`` — host suffix (reserved).
    4. ``extractors.BROKER_EXTRACTORS`` — order intent extractor.
    5. ``schema.is_live_broker_entry`` — config-load wildcard rejection.
    6. ``api_server._known_live_brokers`` / ``_oauth_token_present`` — when loaded.
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    from src.live import registry as _registry
    from src.live.extractors import BROKER_EXTRACTORS as _extractors

    # 1. Patch is_live_broker (LIVE_BROKER_SERVER_KEYS is an immutable frozenset).
    _original_is_live = _registry.is_live_broker

    def _patched_is_live(server_name: str, url: str = "") -> bool:
        if server_name.strip().lower() in _EXTRA_LIVE_KEYS:
            return True
        return _original_is_live(server_name, url)

    _registry.is_live_broker = _patched_is_live

    # 2. Curated classification map.
    from extensions.live.bitget_classification import BITGET_TOOL_CLASS

    _registry._BROKER_CURATED_MAPS["bitget"] = BITGET_TOOL_CLASS

    # 3. URL host suffix (future HTTP transport).
    _registry._HOST_SUFFIX_TO_BROKER["bitget.com"] = "bitget"

    # 4. Order-intent extractor.
    from extensions.live.bitget_extractor import extract_order_intent

    _extractors["bitget"] = extract_order_intent

    # 5. Config validation: treat bitget like other live brokers.
    _patch_schema_is_live_broker_entry()

    # 6. Mandate propose/commit: propagate crypto asset_classes (ceilings → profile → universe).
    _patch_mandate_crypto_universe()

    # 7. Patch order_guard._read_first to unwrap MCP envelopes.
    _patch_order_guard_read_first()

    # 8. Patch runner prompt to add leverage guidance (3-8X self-assessment).
    _patch_runner_prompt()

    # 9. API / status surfaces (import api_server when already running as serve).
    _patch_api_server_surfaces()
    if "api_server" not in sys.modules:
        try:
            import api_server  # noqa: F401

            _patch_api_server_surfaces()
        except ImportError:
            pass

    logger.info("bitget live broker registered (upstream patched at runtime)")


def _patch_mandate_crypto_universe() -> None:
    """Ensure crypto ceilings yield ``universe.asset_classes`` containing ``crypto``.

    Upstream ``ProposeMandateProfilesTool`` omits ``asset_classes`` on profiles;
    ``commit._profile_to_universe`` then defaults to ``us_equity``, which denies
    all Bitget USDT-perp orders at enforcement time.
    """
    from src.live.mandate import commit as _commit
    from src.tools import propose_mandate_tool as _propose_mod

    _orig_universe = _commit._profile_to_universe

    def _profile_to_universe_crypto(profile):  # type: ignore[no-untyped-def]
        universe = _orig_universe(profile)
        explicit = profile.get("asset_classes")
        if explicit:
            universe["asset_classes"] = list(explicit)
            return universe
        instruments = profile.get("instruments") or profile.get("allowed_instruments") or []
        lowered = {str(item).lower() for item in instruments}
        if "crypto" in lowered:
            universe["asset_classes"] = ["crypto"]
        return universe

    _commit._profile_to_universe = _profile_to_universe_crypto  # type: ignore[assignment]

    _orig_synth = _propose_mod.ProposeMandateProfilesTool._synthesize_profiles

    def _synthesize_profiles_crypto(
        self,
        ceilings: dict,
        reauth_for: dict | None,
        flatten_on_halt: bool = False,
    ):  # type: ignore[no-untyped-def]
        profiles = _orig_synth(self, ceilings, reauth_for, flatten_on_halt)
        asset_classes = ceilings.get("asset_classes")
        if asset_classes:
            normalized = [str(value).lower() for value in asset_classes]
        elif "crypto" in {str(item).lower() for item in (ceilings.get("instruments") or [])}:
            normalized = ["crypto"]
        else:
            normalized = None
        if normalized:
            for profile in profiles:
                profile["asset_classes"] = normalized
        return profiles

    _propose_mod.ProposeMandateProfilesTool._synthesize_profiles = _synthesize_profiles_crypto  # type: ignore[method-assign]


def _patch_schema_is_live_broker_entry() -> None:
    """Wrap ``schema.is_live_broker_entry`` so ``bitget`` rejects wildcard allowlists."""
    from src.config import schema as _schema

    _original = _schema.is_live_broker_entry

    def _patched_entry(server_key: str, server: object) -> bool:
        if server_key.strip().lower() in _EXTRA_LIVE_KEYS:
            return True
        return _original(server_key, server)  # type: ignore[arg-type]

    _schema.is_live_broker_entry = _patched_entry  # type: ignore[assignment]


def _patch_order_guard_read_first() -> None:
    """Patch ``LiveOrderGuardTool._read_first`` to unwrap MCP envelopes for Bitget.

    ``_read_first`` returns the raw ``adapter.call_tool()`` result (a dict envelope),
    but the enforcement code expects the actual position/balance data inside it.
    Without unwrapping, ``_coerce_position_rows`` finds the ``"data"`` key containing
    a string like ``"get_positionsOutput(result=[])"`` instead of a list, returning
    ``None`` and causing a fail-closed breach.

    This patch extracts ``structured_content`` from the envelope before returning,
    matching what ``_unwrap`` does in the reconcile path.
    """
    from src.live import order_guard as _og

    _orig_read_first = _og.LiveOrderGuardTool._read_first

    def _patched_read_first(self, candidates: tuple[str, ...]) -> object:
        raw = _orig_read_first(self, candidates)
        # Unwrap MCP envelope: extract structured_content or data
        if isinstance(raw, dict):
            sc = raw.get("structured_content")
            if sc is not None:
                if isinstance(sc, dict) and list(sc) == ["result"]:
                    return sc["result"]
                return sc
            data = raw.get("data")
            if data is not None:
                return data
        return raw

    _og.LiveOrderGuardTool._read_first = _patched_read_first  # type: ignore[assignment]
    logger.info("bitget order_guard._read_first patched to unwrap MCP envelopes")


def _patch_runner_prompt() -> None:
    """Patch ``_pin_mandate_prompt`` to append AI self-assessed leverage guidance.

    The original prompt only states ``max_leverage`` as a ceiling; this patch
    adds an instruction telling the agent to assess market conditions and choose
    a leverage between 3X and 8X on its own judgment. The enforcement gate
    (``max_leverage=8.0`` in the mandate) remains the hard ceiling.
    """
    from src.live.runtime import runner as _runner

    _orig_prompt = _runner._pin_mandate_prompt

    def _patched_prompt(broker: str, mandate, now) -> str:
        base = _orig_prompt(broker, mandate, now)
        return base + (
            "\n\n=== LEVERAGE GUIDANCE ===\n"
            "Assess current market conditions (volatility, trend strength, risk) "
            "and self-select an appropriate leverage between 3X and 8X for each "
            "position. The mandate ceiling is 8X; aim for at least 3X unless "
            "conditions clearly warrant lower. This is an AI judgment call — "
            "scale leverage to conviction, not to the limit."
        )

    _runner._pin_mandate_prompt = _patched_prompt  # type: ignore[assignment]
    logger.info("bitget runner prompt patched with 3-8X leverage guidance")


def _patch_api_server_surfaces() -> None:
    """Patch live status helpers on ``api_server`` when that module is already loaded."""
    mod = sys.modules.get("api_server")
    if mod is None:
        return

    if hasattr(mod, "_known_live_brokers"):
        _original_known = mod._known_live_brokers

        def _patched_known_live_brokers() -> list[str]:
            return sorted(set(_original_known()) | _EXTRA_LIVE_KEYS)

        mod._known_live_brokers = _patched_known_live_brokers

    if hasattr(mod, "_oauth_token_present"):
        _original_oauth = mod._oauth_token_present

        def _patched_oauth_token_present(broker: str) -> bool:
            if broker.strip().lower() in _EXTRA_LIVE_KEYS:
                from extensions.live.bitget_agent_setup import bitget_credentials_configured

                return bitget_credentials_configured()
            return _original_oauth(broker)

        mod._oauth_token_present = _patched_oauth_token_present

    # Use _runner_factory (the upstream's official injection point) to wrap
    # Bitget read callables with _unwrap.  The factory calls the original
    # _build_live_runner then patches the read callables — no need to
    # replicate the upstream wiring logic.
    if hasattr(mod, "_runner_factory"):

        def _bitget_runner_factory(broker: str) -> Any:  # noqa: ANN401
            """Build a runner with the adapter constructed directly (bypass
            ``_live_broker_adapter`` which may load a stale/cached config)."""
            from src.live.audit import write_live_action
            from src.live.runtime.reconcile import reconcile
            from src.live.runtime.runner import LiveRunner
            from src.live.runtime.scheduler import Scheduler
            from src.live.runtime.triggers import Trigger
            from src.tools.mcp import MCPServerAdapter
            from src.config.schema import MCPServerConfig

            _cfg = MCPServerConfig(
                type="stdio",
                command="python3",
                args=["extensions/live/bitget_mcp_server.py"],
                tool_timeout=30,
            )
            adapter = MCPServerAdapter("bitget", _cfg)

            def _read(remote_tool: str):
                return _unwrap(lambda: adapter.call_tool(remote_tool, {}))

            def _submit(order: dict) -> dict:
                if order.get("action") == "cancel":
                    return adapter.call_tool("cancel_order", order)
                return adapter.call_tool("place_order", order)

            svc = getattr(
                __import__("api_server", fromlist=["_get_session_service"]),
                "_get_session_service",
            )()
            session = svc.create_session(title=f"live-runner:{broker}")
            session_id = session.session_id

            async def _agent_caller(sid: str, prompt: str) -> dict:
                return await svc.send_message(sid, prompt)

            def _audit_with_bus(event: Any) -> dict:
                return write_live_action(
                    event,
                    event_callback=lambda etype, record: svc.event_bus.emit(
                        session_id, etype, record
                    ),
                )

            runner_holder: dict = {}
            scheduler = Scheduler(
                lambda _job: runner_holder["runner"].run_once()
                if runner_holder.get("runner")
                else None
            )

            runner = LiveRunner(
                broker,
                agent_caller=_agent_caller,
                reconcile_fn=reconcile,
                read_positions=_read("get_positions"),
                read_balance=_read("get_account"),
                read_open_orders=_read("list_orders"),
                submit_fn=_submit,
                write_audit_fn=_audit_with_bus,
                scheduler=scheduler,
                triggers=[Trigger.market("crypto")],
                session_id=session_id,
            )
            runner_holder["runner"] = runner
            return runner

        mod._runner_factory = _bitget_runner_factory


def _unwrap(read_fn):  # type: ignore[no-untyped-def]
    """Wrap a read callable to unwrap MCP envelope dicts.

    ``adapter.call_tool`` returns a normalized envelope::

        {"status": "ok", "structured_content": ..., "data": ..., "text": ..., "server": ...}

    Reconcile expects raw data (list/dict), so we strip the envelope.

    FastMCP wraps returns inside ``{"result": <value>}`` when the value is a list
    (e.g. ``get_positions``, ``list_orders``). Dict returns such as ``get_account``
    appear as a flat dict with no ``"result"`` wrapper.  We handle both shapes.

    When ``status`` is ``"error"`` (MCP timeout / connection failure) we raise
    :class:`ValueError` so the runner's ``_run_reconcile`` try/except catches it
    and produces a clean ``TICK_RECONCILE_ERROR`` outcome — instead of leaking the
    raw envelope dict into ``list()`` / ``dict()`` calls that later crash with
    opaque ``ValueError`` or ``AttributeError``.
    """

    def _inner():
        result = read_fn()
        if not isinstance(result, dict):
            raise ValueError(f"bitget MCP call failed: unexpected type {type(result).__name__}")
        if result.get("status") != "ok":
            raise ValueError(
                f"bitget MCP call failed: {result.get('error', str(result))}"
            )

        sc = result.get("structured_content")
        if sc is not None:
            if isinstance(sc, dict) and list(sc) == ["result"]:
                # FastMCP wraps list returns in {"result": [...]}; unwrap it.
                return sc["result"]
            return sc
        data = result.get("data")
        if data is not None:
            return data
        text = result.get("text")
        if text is not None:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                pass
        raise ValueError(
            f"bitget MCP call failed: ok response has no extractable content: {result}"
        )

    return _inner


