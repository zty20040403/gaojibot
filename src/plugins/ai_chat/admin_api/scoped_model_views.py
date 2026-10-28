"""Read-only scoped preferences; an override is never proof of membership."""

from __future__ import annotations

from typing import Any

from ..conversation_keys import ScopeSettingAddress, TurnAddress
from ..platform_types import EndpointKey


def scoped_model_rows(catalog: Any, preferences: Any, reasoning: Any, ledger: Any) -> list[dict[str, object]]:
    scopes = {}
    settings: dict[str, dict[str, str]] = {}
    efforts: dict[str, dict[str, str]] = {}
    overrides: dict[str, dict[str, dict[str, str]]] = {}
    for store, destination, value_name in (
        (preferences, settings, "profile"),
        (reasoning, efforts, "reasoning_effort"),
    ):
        for key, value in store.items() if store is not None else []:
            try:
                address = ScopeSettingAddress.parse(key)
            except ValueError:
                address = None
            if address is not None:
                if address.scope.account is not None and address.scope.platform != "onebot-v11":
                    scopes[address.scope.key] = address.scope
                    destination.setdefault(address.scope.key, {})[address.name] = value
                continue
            try:
                turn = TurnAddress.parse(key)
            except ValueError:
                continue
            actor = turn.actor
            if actor.account.platform == "onebot-v11" or actor.identity_kind != "group_member":
                continue
            # Per-actor preferences belong to the actual group, even if history
            # was explicitly bound to a different canonical conversation.
            scope = EndpointKey(actor.account, "group", actor.identity_namespace).scope
            scopes[scope.key] = scope
            stored = overrides.setdefault(scope.key, {}).setdefault(key, {
                "actor_key": actor.key, "history_scope_key": turn.scope.key,
            })
            stored[value_name] = value
    if ledger is not None:
        try:
            observed_scopes = list(ledger.list_scopes())
        except (OSError, RuntimeError, TypeError, ValueError):
            observed_scopes = []
        for scope in observed_scopes:
            if scope.kind == "group" and scope.platform != "onebot-v11" and getattr(scope, "account", None) is not None:
                scopes[scope.key] = scope
    rows = []
    for key, scope in sorted(scopes.items()):
        configured = settings.get(key, {})
        profile = catalog.resolve_preference(configured.get("default"))
        enabled = configured.get("enabled")
        rows.append({
            "scope_key": key,
            "platform": scope.platform,
            "account_key": scope.account.key,
            "native_conversation_id": scope.native_conversation_id,
            "enabled_override": True if enabled == "enabled" else False if enabled == "disabled" else None,
            "default_profile": profile.name,
            "default_provider": profile.provider,
            "default_model": profile.model,
            "member_reasoning_effort": efforts.get(key, {}).get("member-default"),
            "overrides": [
                {"turn_key": turn_key, **values}
                for turn_key, values in sorted(overrides.get(key, {}).items())
            ],
        })
    return rows
