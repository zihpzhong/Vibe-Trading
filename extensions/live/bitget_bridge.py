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

import logging
import sys

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

    # 7. API / status surfaces (import api_server when already running as serve).
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
