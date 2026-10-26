from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from ..model_catalog import SUPPORTED_REASONING_EFFORTS


def _model_overview(
    catalog: Any,
    gateway: Any = None,
) -> dict[str, object]:
    if catalog is None:
        return {"default": "", "profiles": []}
    health = (
        gateway.health_snapshot()
        if gateway is not None and hasattr(gateway, "health_snapshot")
        else {}
    )
    return {
        "default": str(catalog.default_name),
        "profiles": [
            {
                "name": profile.name,
                "provider": profile.provider,
                "protocol": profile.protocol,
                "model": profile.model,
                "configured": profile.configured,
                "reasoning_effort": profile.reasoning_effort or None,
                "supports_reasoning_effort": profile.provider
                in {"openai", "cliproxy"},
                "fallback_profiles": list(profile.fallback_profiles),
                "circuit_breaker_enabled": profile.circuit_breaker_enabled,
                "health": health.get(
                    profile.name,
                    {
                        "status": "unknown",
                        "consecutive_failures": 0,
                        "retry_after_seconds": 0,
                    },
                ),
                "capabilities": {
                    "tools": profile.capabilities.tools,
                    "forced_tool_choice": profile.capabilities.forced_tool_choice,
                    "streaming": profile.capabilities.streaming,
                    "json_mode": profile.capabilities.json_mode,
                    "vision": profile.capabilities.vision,
                },
            }
            for profile in catalog.profiles
        ],
    }


def _admin_model_profile(catalog: Any, requested: str | None) -> Any:
    normalized = str(requested or "").strip()
    if not normalized:
        return None
    if catalog is None:
        raise HTTPException(status_code=503, detail="模型目录不可用")
    try:
        profile = catalog.resolve(normalized)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="没有这个模型配置") from None
    if not profile.configured:
        raise HTTPException(status_code=409, detail="这个模型还没有配置可用的密钥")
    return profile


def _admin_reasoning_effort(requested: str | None) -> str | None:
    normalized = str(requested or "").strip().lower()
    if not normalized:
        return None
    if normalized not in SUPPORTED_REASONING_EFFORTS:
        raise HTTPException(status_code=422, detail="没有这个推理强度")
    return normalized


def _explicit_preference(preferences: Any, key: str) -> str | None:
    getter = getattr(preferences, "get_explicit", None)
    if callable(getter):
        return getter(key)
    return dict(preferences.items()).get(key)


def _preserve_admin_model_selections(
    catalog: Any,
    preferences: Any,
    settings: Any,
    group_id: int,
) -> None:
    """Keep the admin lane independent when the shared member model changes."""
    admin_user_ids = set(
        getattr(settings, "admin_user_ids", set()) or set()
    )
    if not admin_user_ids or catalog is None:
        return

    dynamic_default = (
        preferences.get_group_default(group_id)
        if hasattr(preferences, "get_group_default")
        else None
    )
    deployed_default = (
        (getattr(settings, "group_model_profiles", {}) or {}).get(group_id)
        if settings is not None
        else None
    )
    effective_default = catalog.resolve_preference(
        dynamic_default or deployed_default
    )
    stored_keys = {
        str(conversation_id)
        for conversation_id, _stored in preferences.items()
    }
    for user_id in admin_user_ids:
        conversation_id = f"group:{group_id}:user:{int(user_id)}"
        if conversation_id not in stored_keys:
            preferences.set(conversation_id, effective_default.name)


_GROUP_CONVERSATION_PATTERN = re.compile(r"^group:(\d+):user:(\d+)$")


_GROUP_SETTING_PATTERN = re.compile(
    r"^group:(\d+):(?:default|enabled|vision-auto-describe)$"
)


_GROUP_MEMBER_REASONING_PATTERN = re.compile(
    r"^group:(\d+):member-default$"
)


def _group_model_overview(
    catalog: Any,
    preferences: Any,
    reasoning_preferences: Any,
    settings: Any,
    message_ledger: Any,
    user_profiles: Any = None,
) -> dict[str, object]:
    if catalog is None:
        return {"default": {}, "items": [], "configured": False}

    default_profile = catalog.default
    group_ids: set[int] = set()
    if settings is not None:
        group_ids.update(getattr(settings, "enabled_groups", set()) or set())
        group_ids.update(getattr(settings, "disabled_groups", set()) or set())
        group_ids.update(
            (getattr(settings, "group_model_profiles", {}) or {}).keys()
        )

    if message_ledger is not None:
        try:
            for scope in message_ledger.list_scopes():
                if scope.kind != "group" or scope.platform != "onebot-v11":
                    continue
                try:
                    group_ids.add(int(scope.native_conversation_id))
                except (TypeError, ValueError):
                    continue
        except (OSError, RuntimeError, TypeError, ValueError):
            pass

    if user_profiles is not None:
        try:
            group_ids.update(user_profiles.group_ids())
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass

    overrides_by_group: dict[int, dict[int, dict[str, object]]] = {}
    preference_items = preferences.items() if preferences is not None else []
    for conversation_id, stored_preference in preference_items:
        match = _GROUP_CONVERSATION_PATTERN.fullmatch(str(conversation_id))
        if match is None:
            setting_match = _GROUP_SETTING_PATTERN.fullmatch(str(conversation_id))
            if setting_match is not None:
                group_ids.add(int(setting_match.group(1)))
            continue
        group_id = int(match.group(1))
        user_id = int(match.group(2))
        group_ids.add(group_id)
        resolved = catalog.resolve_preference(str(stored_preference))
        direct = catalog.try_resolve(str(stored_preference))
        recognized = direct is not None or any(
            profile.model == str(stored_preference)
            for profile in catalog.profiles
        )
        overrides_by_group.setdefault(group_id, {})[user_id] = {
            "user_id": user_id,
            "stored_preference": str(stored_preference),
            "profile": resolved.name,
            "provider": resolved.provider,
            "model": resolved.model,
            "recognized": recognized,
        }

    reasoning_by_group: dict[int, dict[int, str]] = {}
    member_effort_by_group: dict[int, str] = {}
    reasoning_items = (
        reasoning_preferences.items()
        if reasoning_preferences is not None
        else []
    )
    for conversation_id, stored_effort in reasoning_items:
        normalized_effort = str(stored_effort).strip().lower()
        if normalized_effort not in SUPPORTED_REASONING_EFFORTS:
            continue
        match = _GROUP_CONVERSATION_PATTERN.fullmatch(str(conversation_id))
        if match is not None:
            group_id = int(match.group(1))
            user_id = int(match.group(2))
            group_ids.add(group_id)
            reasoning_by_group.setdefault(group_id, {})[user_id] = (
                normalized_effort
            )
            continue
        default_match = _GROUP_MEMBER_REASONING_PATTERN.fullmatch(
            str(conversation_id)
        )
        if default_match is not None:
            group_id = int(default_match.group(1))
            group_ids.add(group_id)
            member_effort_by_group[group_id] = normalized_effort

    rows: list[dict[str, object]] = []
    admin_user_ids = set(
        getattr(settings, "admin_user_ids", set()) or set()
    )
    for group_id in sorted(group_ids):
        overrides = sorted(
            overrides_by_group.get(group_id, {}).values(),
            key=lambda item: int(item["user_id"]),
        )
        deployed_enabled = (
            bool(settings.is_group_enabled(group_id))
            if settings is not None
            else True
        )
        enabled_override = None
        if preferences is not None and hasattr(
            preferences,
            "get_group_enabled_override",
        ):
            enabled_override = preferences.get_group_enabled_override(group_id)
        enabled = (
            enabled_override
            if enabled_override is not None
            else deployed_enabled
        )
        vision_override = None
        if preferences is not None and hasattr(
            preferences,
            "get_group_vision_auto_describe_override",
        ):
            vision_override = preferences.get_group_vision_auto_describe_override(
                group_id
            )
        vision_deployed = bool(
            getattr(settings, "vision_auto_describe", False)
            if settings is not None
            else False
        )
        vision_auto_describe = (
            vision_override if vision_override is not None else vision_deployed
        )
        deployed_group_default = (
            (getattr(settings, "group_model_profiles", {}) or {}).get(group_id)
            if settings is not None
            else None
        )
        dynamic_group_default = None
        if preferences is not None and hasattr(preferences, "get_group_default"):
            dynamic_group_default = preferences.get_group_default(group_id)
        stored_group_default = dynamic_group_default or deployed_group_default
        group_default = catalog.resolve_preference(stored_group_default)
        member_reasoning_effort = member_effort_by_group.get(group_id)

        observed_members: list[dict[str, object]] = []
        if user_profiles is not None:
            try:
                observed_members = user_profiles.members(group_id)
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                observed_members = []
        members_by_id = {
            int(item["user_id"]): dict(item)
            for item in observed_members
            if int(item.get("user_id", 0)) > 0
        }
        for user_id in {
            *admin_user_ids,
            *overrides_by_group.get(group_id, {}),
            *reasoning_by_group.get(group_id, {}),
        }:
            members_by_id.setdefault(
                int(user_id),
                {
                    "user_id": int(user_id),
                    "nickname": "",
                    "card": "",
                    "display_name": f"QQ {int(user_id)}",
                    "last_seen": 0,
                },
            )

        classified: list[dict[str, object]] = []
        for user_id, member in members_by_id.items():
            explicit = overrides_by_group.get(group_id, {}).get(user_id)
            effective = (
                catalog.resolve_preference(str(explicit["stored_preference"]))
                if explicit is not None
                else group_default
            )
            supports_reasoning_effort = effective.provider in {
                "openai",
                "cliproxy",
            }
            explicit_effort = reasoning_by_group.get(group_id, {}).get(user_id)
            inherited_effort = (
                None
                if user_id in admin_user_ids
                else member_reasoning_effort
            )
            effective_effort = (
                explicit_effort
                or inherited_effort
                or effective.reasoning_effort
                or None
            )
            effort_source = (
                "unsupported"
                if not supports_reasoning_effort
                else "user"
                if explicit_effort is not None
                else "group"
                if inherited_effort is not None
                else "model"
                if effective.reasoning_effort
                else "server"
            )
            classified.append(
                {
                    **member,
                    "is_admin": user_id in admin_user_ids,
                    "explicit_profile": (
                        str(explicit["profile"]) if explicit is not None else None
                    ),
                    "effective_profile": effective.name,
                    "effective_provider": effective.provider,
                    "effective_model": effective.model,
                    "supports_reasoning_effort": supports_reasoning_effort,
                    "explicit_reasoning_effort": explicit_effort,
                    "effective_reasoning_effort": (
                        effective_effort if supports_reasoning_effort else None
                    ),
                    "reasoning_effort_source": effort_source,
                }
            )
        classified.sort(
            key=lambda item: (
                not bool(item["is_admin"]),
                -int(item.get("last_seen", 0)),
                int(item["user_id"]),
            )
        )
        rows.append(
            {
                "group_id": group_id,
                "enabled": enabled,
                "enabled_override": enabled_override,
                "enabled_source": (
                    "dashboard" if enabled_override is not None else "deployment"
                ),
                "vision_auto_describe": vision_auto_describe,
                "vision_auto_describe_override": vision_override,
                "vision_auto_describe_source": (
                    "dashboard" if vision_override is not None else "deployment"
                ),
                "default_profile": group_default.name,
                "default_provider": group_default.provider,
                "default_model": group_default.model,
                "group_override": stored_group_default is not None,
                "dynamic_group_profile": dynamic_group_default,
                "deployed_group_profile": deployed_group_default,
                "group_default_source": (
                    "dashboard"
                    if dynamic_group_default is not None
                    else "deployment"
                    if deployed_group_default is not None
                    else "global"
                ),
                "member_reasoning_effort": member_reasoning_effort,
                "overrides": overrides,
                "admins": [item for item in classified if item["is_admin"]],
                "members": [item for item in classified if not item["is_admin"]],
            }
        )

    return {
        "default": {
            "profile": default_profile.name,
            "provider": default_profile.provider,
            "model": default_profile.model,
        },
        "profiles": _model_overview(catalog)["profiles"],
        "items": rows,
        "configured": True,
    }
