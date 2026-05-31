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
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False

#: Live broker keys registered by this extension (stdio MCP, no OAuth).
_EXTRA_LIVE_KEYS: frozenset[str] = frozenset({"bitget"})

# Sizing snapshot cache — refreshed every N ticks to reduce MCP subprocess + LLM tokens.
# 缓存 balance/positions 快照，每第 N tick 刷新一次 / refresh every N runner ticks
_SIZING_CACHE: dict[str, Any] = {"tick_count": 0, "snapshot": None, "adapter": None}
_SIZING_REFRESH_INTERVAL = 5

# AGT Runner tick 间隔默认 60s；可用 LIVE_RUNNER_MARKET_WATCH_MS 覆盖 / override via env
_DEFAULT_RUNNER_MARKET_WATCH_MS = 60_000


def _runner_market_watch_ms() -> int:
    """Parse ``LIVE_RUNNER_MARKET_WATCH_MS`` (ms); invalid values fall back to 60s."""
    raw = os.environ.get("LIVE_RUNNER_MARKET_WATCH_MS", "").strip()
    if not raw:
        return _DEFAULT_RUNNER_MARKET_WATCH_MS
    try:
        ms = int(raw)
    except ValueError:
        logger.warning("Invalid LIVE_RUNNER_MARKET_WATCH_MS=%r — using 60s", raw)
        return _DEFAULT_RUNNER_MARKET_WATCH_MS
    if ms <= 0:
        logger.warning("LIVE_RUNNER_MARKET_WATCH_MS must be > 0 (got %s) — using 60s", ms)
        return _DEFAULT_RUNNER_MARKET_WATCH_MS
    return ms


def _ensure_runner_job_interval(broker: str, watch_ms: int) -> None:
    """Persist scheduler cadence so job store resume matches ``market_watch_ms``.

    上游 runner 重启时优先加载 jobs.json；若仍是旧的 interval:60000 会忽略 env。
    On restart, LiveRunner reloads persisted jobs before synthesizing from triggers.
    """
    import time

    from src.live.runtime.jobstore import JobStore
    from src.live.runtime.scheduler import Job

    store = JobStore()
    now_ms = int(time.time() * 1000)
    job = Job(
        id=f"{broker}-market-0",
        next_run_at=now_ms + min(watch_ms, 15_000),
        schedule=f"interval:{watch_ms}",
        payload={"broker": broker, "trigger": "market"},
    )
    try:
        store.save([job])
        logger.info(
            "bitget runner job store synced: %s interval=%dms",
            broker,
            watch_ms,
        )
    except Exception as exc:
        logger.warning("bitget runner job store sync failed: %s", exc)


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

    # 8. Patch runner prompt + order_guard with AGT sizing engine.
    _patch_runner_prompt()
    _patch_order_guard_sizing()

    # 10. AGT live-runner: tool allowlist + prompt constraints / 工具白名单
    _patch_agt_live_tool_filter()

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
        # Unwrap MCP envelope: extract structured_content or data.
        # The ``{"result": [...]}`` unwrap is already done at the source
        # in ``mcp._normalize_call_tool_result`` — no need to repeat here.
        if isinstance(raw, dict):
            return raw.get("structured_content") or raw.get("data") or raw
        return raw

    _og.LiveOrderGuardTool._read_first = _patched_read_first  # type: ignore[assignment]
    logger.info("bitget order_guard._read_first patched to unwrap MCP envelopes")


def _bitget_mcp_config(*, tool_timeout: int = 20) -> Any:
    """Build a standard Bitget MCP server config (shared by sizing + runner)."""
    from src.config.schema import MCPServerConfig

    return MCPServerConfig(
        type="stdio",
        command="python3",
        args=["extensions/live/bitget_mcp_server.py"],
        tool_timeout=tool_timeout,
    )


def _get_sizing_adapter() -> Any:
    """Get or create cached MCP adapter for sizing prompt refreshes."""
    from src.tools.mcp import MCPServerAdapter

    adapter = _SIZING_CACHE.get("adapter")
    if adapter is not None:
        return adapter
    adapter = MCPServerAdapter("bitget", _bitget_mcp_config())
    _SIZING_CACHE["adapter"] = adapter
    return adapter


def _invalidate_sizing_adapter() -> None:
    """Drop cached MCP adapter after transport errors."""
    _SIZING_CACHE["adapter"] = None


def _maybe_refresh_sizing() -> tuple[Any | None, Any | None]:
    """Return cached sizing snapshot; refresh from broker every 5 ticks.

    缓存 balance/positions 快照，每第 5 tick 从 broker 刷新一次。
    """
    tick = _SIZING_CACHE["tick_count"]
    snapshot = _SIZING_CACHE.get("snapshot")
    if tick % _SIZING_REFRESH_INTERVAL == 0 or snapshot is None:
        try:
            adapter = _get_sizing_adapter()
            balance = _unwrap(lambda: adapter.call_tool("get_account", {}))()
            positions = _unwrap(lambda: adapter.call_tool("get_positions", {}))()
            _SIZING_CACHE["snapshot"] = (balance, positions)
        except Exception as exc:
            logger.debug("sizing snapshot refresh failed: %s", exc)
            _invalidate_sizing_adapter()
            # 失败时保留旧快照 / keep stale snapshot on failure
    _SIZING_CACHE["tick_count"] = tick + 1
    cached = _SIZING_CACHE.get("snapshot")
    if cached is None:
        return None, None
    return cached


def _patch_runner_prompt() -> None:
    """Patch ``_pin_mandate_prompt`` with AGT sizing engine output each tick.

    Sizing plan cached & refreshed every ``_SIZING_REFRESH_INTERVAL`` ticks to
    reduce broker API calls and token consumption.

    每 tick 注入紧凑凯利仓位指引。sizing 数据缓存后每 5 tick 刷新一次。
    """
    from src.live.runtime import runner as _runner

    from extensions.live.agt_position_sizing import format_sizing_prompt_block, plan_from_account_snapshot

    _orig_prompt = _runner._pin_mandate_prompt

    def _patched_prompt(broker: str, mandate, now) -> str:
        base = _orig_prompt(broker, mandate, now)
        if broker.strip().lower() != "bitget":
            return base
        caps = mandate.hard_caps
        balance, positions = _maybe_refresh_sizing()
        plan = plan_from_account_snapshot(
            balance if isinstance(balance, dict) else None,
            positions if isinstance(positions, list) else None,
            mandate_max_order_notional=float(caps.max_order_notional_usd or 0),
            mandate_max_total_exposure=float(caps.max_total_exposure_usd or 0),
            mandate_account_funding=float(caps.account_funding_usd or 0),
        )
        from extensions.live.agt_runner_config import append_agt_live_prompt_constraints

        return append_agt_live_prompt_constraints(base + format_sizing_prompt_block(plan))

    _runner._pin_mandate_prompt = _patched_prompt  # type: ignore[assignment]
    logger.info("bitget runner prompt patched with AGT position sizing engine")


def _patch_agt_live_tool_filter() -> None:
    """Filter the agent tool registry for ``live-runner:*`` sessions.

    自主 tick 禁用 read_url / run_swarm 等研究工具，仅保留 Bitget MCP + live_trading。
    """
    import src.tools as _tools_mod

    if getattr(_tools_mod, "_agt_live_build_registry_patched", False):
        return

    from extensions.live.agt_runner_config import (
        filter_registry_for_agt_live,
        is_live_runner_session_title,
    )

    _orig_build = _tools_mod.build_registry

    def _build_registry_with_agt_filter(*args: Any, **kwargs: Any):  # noqa: ANN401
        registry = _orig_build(*args, **kwargs)
        session_id = kwargs.get("session_id")
        if not session_id:
            return registry
        try:
            title = _live_runner_session_title(session_id)
        except Exception:
            return registry
        if not is_live_runner_session_title(title):
            return registry
        return filter_registry_for_agt_live(registry)

    _tools_mod.build_registry = _build_registry_with_agt_filter  # type: ignore[assignment]
    _tools_mod._agt_live_build_registry_patched = True
    logger.info("agt live-runner build_registry tool filter installed")


def _live_runner_session_title(session_id: str) -> str | None:
    """Resolve session title for tool-filter gating."""
    if "api_server" in sys.modules:
        mod = sys.modules["api_server"]
        svc = mod._get_session_service()
        session = svc.store.get_session(session_id)
        return session.title if session else None
    return None


def _get_or_create_live_runner_session(svc: Any, broker: str) -> Any:  # noqa: ANN401
    """Reuse the newest ``live-runner:{broker}`` session instead of spawning another.

    复用已有 live-runner 会话，避免每次 start 产生新 session 堆积。
    """
    title = f"live-runner:{broker}"
    candidates = [s for s in svc.list_sessions(limit=200) if s.title == title]
    if candidates:
        session = max(candidates, key=lambda s: s.updated_at or s.created_at)
        logger.info(
            "reusing live-runner session %s (skipped %d duplicate titles)",
            session.session_id,
            len(candidates) - 1,
        )
        return session
    return svc.create_session(title=title)


def _patch_order_guard_sizing() -> None:
    """Reject buy orders below computed notional floor (margin-aware sizing)."""
    from src.live import order_guard as _og
    from src.live.enforcement import BREACH_KIND_QUANTITATIVE, _breach

    from extensions.live.agt_position_sizing import plan_from_account_snapshot, validate_entry_notional

    _orig_execute = _og.LiveOrderGuardTool.execute

    def _execute_with_sizing_inner(self, **kwargs: Any) -> str:
        """Inject sizing floor after mandate checks, before allow."""
        mandate = _og.load_mandate(self.broker)
        if mandate is None or mandate.schema_version != _og.MANDATE_SCHEMA_VERSION:
            return _orig_execute(self, **kwargs)

        if self._is_expired(mandate):
            return _orig_execute(self, **kwargs)
        if _og.halt_flag_set(self.broker):
            return _orig_execute(self, **kwargs)

        extractor = _og.get_extractor(self.broker)
        intent = extractor(self.remote_name, kwargs) if extractor is not None else None
        if intent is None:
            return _orig_execute(self, **kwargs)

        intent = self._normalize_intent_notional(intent)
        if intent is None:
            return _orig_execute(self, **kwargs)

        positions = self._read_first(_og._POSITIONS_TOOLS)
        balance = self._read_first(_og._BALANCE_TOOLS)
        daily_count = self._read_daily_count()

        breach = _og.check_mandate(
            mandate,
            intent,
            positions,
            balance,
            broker=self.broker,
            remote_tool=self.remote_name,
            daily_count=daily_count,
        )
        if breach is not None:
            if breach.kind in (_og.BREACH_KIND_UNIVERSE, _og.BREACH_KIND_INSTRUMENT):
                return self._deny_breach(breach, mandate=mandate, intent=intent, reauth=False)
            return self._deny_breach(breach, mandate=mandate, intent=intent, reauth=True)

        caps = mandate.hard_caps
        pos_rows = positions if isinstance(positions, list) else []
        bal_row = balance if isinstance(balance, dict) else None
        plan = plan_from_account_snapshot(
            bal_row,
            pos_rows,
            mandate_max_order_notional=float(caps.max_order_notional_usd or 0),
            mandate_max_total_exposure=float(caps.max_total_exposure_usd or 0),
            mandate_account_funding=float(caps.account_funding_usd or 0),
        )
        notional = float(intent.notional_usd or 0)
        reduce_only = bool(kwargs.get("reduce_only"))
        sizing_reason = validate_entry_notional(
            notional,
            plan,
            side=intent.side or "buy",
            is_reduce_only=reduce_only,
        )
        if sizing_reason:
            sizing_breach = _breach(
                broker=self.broker,
                remote_tool=self.remote_name,
                intent=intent,
                kind=BREACH_KIND_QUANTITATIVE,
                limit="agt_min_notional_usd",
                limit_value=plan.notional_min_usdt,
                attempted_value=notional,
                detail=sizing_reason,
            )
            return self._deny_breach(sizing_breach, mandate=mandate, intent=intent, reauth=False)

        return self._allow(mandate=mandate, intent=intent, kwargs=kwargs)

    _og.LiveOrderGuardTool.execute = _execute_with_sizing_inner  # type: ignore[assignment]
    logger.info("bitget order_guard patched with AGT min-notional sizing floor")


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

            adapter = MCPServerAdapter("bitget", _bitget_mcp_config(tool_timeout=30))

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
            session = _get_or_create_live_runner_session(svc, broker)
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

            watch_ms = _runner_market_watch_ms()
            _ensure_runner_job_interval(broker, watch_ms)
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
                market_watch_ms=watch_ms,
            )
            runner_holder["runner"] = runner
            logger.info(
                "bitget LiveRunner market_watch_ms=%d (%.1f min/tick)",
                watch_ms,
                watch_ms / 60_000,
            )
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

        # The ``{"result": [...]}`` unwrap is already done at the source
        # in ``mcp._normalize_call_tool_result``. structured_content is the
        # canonical payload field; data and text are fallbacks.
        sc = result.get("structured_content")
        if sc is not None:
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


