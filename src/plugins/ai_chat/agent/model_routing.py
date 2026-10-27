from __future__ import annotations

from functools import wraps
from typing import Mapping

from ..llm_gateway import LLMConfigError, completion_profile_scope
from ..model_catalog import ModelCatalog, ModelProfile
from .control import active_model_policy, active_task_id


ROLE_PREFERENCES = {
    "supervisor": ("gpt-5.6-terra", "qwen-local", "gpt-5.6-luna"),
    "coder": ("gpt-5.6-terra", "qwen-local", "gpt-5.6-luna"),
    "analyst": ("gpt-5.6-terra", "qwen-local", "gpt-5.6-luna"),
    "researcher": ("gpt-5.6-luna", "qwen-local", "gpt-5.6-terra"),
    "document": ("gpt-5.6-luna", "gpt-5.6-terra", "qwen-local"),
    "media": ("gpt-5.6-luna", "gpt-5.6-terra"),
    "operator": ("gpt-5.6-terra", "qwen-local", "gpt-5.6-luna"),
}


def agent_profile_names(catalog: ModelCatalog, overrides: Mapping[str, str]) -> frozenset[str]:
    explicit = {profile.name for name in overrides.values() if (profile := catalog.try_resolve(name)) is not None}
    return frozenset(
        profile.name for profile in catalog.profiles
        if profile.configured and profile.capabilities.tools
        and (("sol" not in profile.model.casefold() and "sol" not in profile.name.casefold()
              and profile.model != "codex-auto-review") or profile.name in explicit)
    )


def choose_agent_profile(
    role: str, default: ModelProfile, catalog: ModelCatalog, overrides: Mapping[str, str],
) -> ModelProfile:
    policy = active_model_policy.get()
    role_policy = policy.get("roles", {}).get(role, policy)
    mode = role_policy.get("mode", "auto")
    requested = role_policy.get("profile", "")
    effective_overrides = {**overrides, **({role: requested} if requested else {})}
    names = agent_profile_names(catalog, effective_overrides)
    if role == "media":
        names = frozenset(name for name in names if catalog.resolve(name).capabilities.vision)
    if mode in {"preferred", "locked"} and requested:
        profile = catalog.try_resolve(requested)
        if profile and profile.name in names:
            return profile
        if mode == "locked":
            raise LLMConfigError(f"Locked {role} model is unavailable or incompatible")
    override = overrides.get(role)
    if override:
        profile = catalog.try_resolve(override)
        if profile is None or profile.name not in names:
            raise LLMConfigError(f"invalid or unavailable explicit {role} model override")
        return profile
    for name in (*ROLE_PREFERENCES.get(role, ()), default.name, *sorted(names)):
        profile = catalog.try_resolve(name)
        if profile is not None and profile.name in names:
            return profile
    raise LLMConfigError("No configured tool-capable non-Sol model is available for subagents")


def scoped_agent_models(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        task_id = kwargs.get("task_id") or (args[0] if args and isinstance(args[0], int) else None)
        policy = self.store.execution_model_policy(task_id) if task_id else {}
        overrides = dict(self.profile_overrides)
        for role, value in policy.get("roles", {}).items():
            if value.get("profile"):
                overrides[role] = value["profile"]
        if policy.get("profile"):
            overrides["task"] = policy["profile"]
        names = agent_profile_names(self.model_catalog, overrides)
        policy_token = active_model_policy.set(policy)
        task_token = active_task_id.set(task_id)
        try:
            with completion_profile_scope(names):
                return await method(self, *args, **kwargs)
        finally:
            active_model_policy.reset(policy_token)
            active_task_id.reset(task_token)
    return wrapped


def model_scope_for_role(role: str, profile: ModelProfile, catalog: ModelCatalog, overrides: Mapping[str, str]):
    policy = active_model_policy.get()
    policy = policy.get("roles", {}).get(role, policy)
    names = agent_profile_names(catalog, {**overrides, **({role: policy["profile"]} if policy.get("profile") else {})})
    if role == "media":
        names = frozenset(name for name in names if catalog.resolve(name).capabilities.vision)
    return completion_profile_scope({profile.name} if policy.get("mode") == "locked" else names)


def validate_model_policy(policy: dict, catalog: ModelCatalog) -> dict:
    if set(policy) - {"mode", "profile", "roles"}:
        raise ValueError("Unknown model policy field")
    mode = policy.get("mode", "auto")
    if mode not in {"auto", "preferred", "locked"}:
        raise ValueError("mode must be auto, preferred or locked")
    name = str(policy.get("profile", ""))
    if mode == "auto":
        name = ""
    if mode != "auto" and not name:
        raise ValueError("preferred/locked requires a configured model profile")
    if name:
        profile = catalog.try_resolve(name)
        if profile is None or not profile.configured or not profile.capabilities.tools:
            raise ValueError("Model must be configured and support tools")
        name = profile.name
    roles = policy.get("roles", {})
    if not isinstance(roles, dict) or set(roles) - ROLE_PREFERENCES.keys():
        raise ValueError("Unknown specialist role")
    normalized = {"mode": mode, "profile": name, "roles": {}}
    for role, value in roles.items():
        if not isinstance(value, dict) or "roles" in value:
            raise ValueError("Invalid role model policy")
        clean = validate_model_policy(value, catalog)
        if role == "media" and clean["profile"] and not catalog.resolve(clean["profile"]).capabilities.vision:
            raise ValueError("Media specialist requires a vision-capable model")
        normalized["roles"][role] = clean
    return normalized
