from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlparse

from .model_pricing import ModelPricing


SUPPORTED_MODEL_PROTOCOLS = frozenset(
    {
        "openai-chat",
        "anthropic-messages",
    }
)
SUPPORTED_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "none"}
)
_PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class ModelCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class ModelCapabilities:
    tools: bool = True
    forced_tool_choice: bool = True
    streaming: bool = True
    json_mode: bool = True
    model_listing: bool = True
    vision: bool = False
    video: bool = False

    @classmethod
    def defaults_for(cls, protocol: str) -> "ModelCapabilities":
        if protocol == "anthropic-messages":
            return cls(
                tools=True,
                streaming=False,
                json_mode=False,
                model_listing=False,
                vision=False,
            )
        return cls()

    @classmethod
    def from_mapping(
        cls,
        protocol: str,
        raw: object,
    ) -> "ModelCapabilities":
        defaults = cls.defaults_for(protocol)
        if raw is None:
            return defaults
        if not isinstance(raw, Mapping):
            raise ModelCatalogError("profile capabilities must be a JSON object")
        supported = {
            "tools",
            "forced_tool_choice",
            "streaming",
            "json_mode",
            "model_listing",
            "vision",
            "video",
        }
        unknown = sorted(str(key) for key in raw if str(key) not in supported)
        if unknown:
            raise ModelCatalogError(
                "unknown model capabilities: " + ", ".join(unknown)
            )
        return cls(
            tools=_mapping_bool(raw, "tools", defaults.tools),
            forced_tool_choice=_mapping_bool(
                raw,
                "forced_tool_choice",
                defaults.forced_tool_choice,
            ),
            streaming=_mapping_bool(raw, "streaming", defaults.streaming),
            json_mode=_mapping_bool(raw, "json_mode", defaults.json_mode),
            model_listing=_mapping_bool(
                raw,
                "model_listing",
                defaults.model_listing,
            ),
            vision=_mapping_bool(raw, "vision", defaults.vision),
            video=_mapping_bool(raw, "video", defaults.video),
        )


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    protocol: str
    model: str
    base_url: str
    api_key: str = field(default="", repr=False, compare=False)
    api_key_env: str = ""
    api_key_required: bool = True
    timeout_seconds: float = 60.0
    temperature: float | None = None
    thinking: str = "auto"
    reasoning_effort: str = ""
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    aliases: tuple[str, ...] = ()
    fallback_profiles: tuple[str, ...] = ()
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    circuit_breaker_enabled: bool = True
    pricing: ModelPricing | None = None

    @property
    def configured(self) -> bool:
        if not self.api_key_required:
            return True
        key = self.api_key.strip()
        return bool(key and not key.lower().startswith("replace-with"))

    @property
    def provider_identity(self) -> str:
        return f"{self.provider}:{self.protocol}"

    @property
    def client_cache_key(self) -> str:
        secret_fingerprint = hashlib.sha256(
            self.api_key.encode("utf-8")
        ).hexdigest()[:16]
        return "|".join(
            (
                self.protocol,
                self.base_url,
                str(self.timeout_seconds),
                secret_fingerprint,
            )
        )

    def with_model(self, model: str) -> "ModelProfile":
        selected = str(model).strip()
        if not selected or selected==self.model:
            return self
        # A caller-selected model must not inherit the configured model's rate.
        return replace(self, model=selected, pricing=None)

    def with_reasoning_effort(self, effort: str | None) -> "ModelProfile":
        selected = str(effort or "").strip().lower()
        if selected and selected not in SUPPORTED_REASONING_EFFORTS:
            raise ModelCatalogError(f"unsupported reasoning effort: {effort}")
        return replace(self, reasoning_effort=selected)


class ModelCatalog:
    def __init__(
        self,
        profiles: Mapping[str, ModelProfile],
        *,
        default_profile: str,
        legacy_fallback: bool = False,
    ) -> None:
        if not profiles:
            raise ModelCatalogError("at least one model profile is required")

        normalized: dict[str, ModelProfile] = {}
        aliases: dict[str, str] = {}
        for raw_name, profile in profiles.items():
            name = _normalize_profile_name(raw_name)
            if name in normalized:
                raise ModelCatalogError(f"duplicate model profile: {name}")
            if profile.name != name:
                profile = replace(profile, name=name)
            normalized[name] = profile

        for name, profile in normalized.items():
            for candidate in (name, *profile.aliases):
                alias = _normalize_profile_name(candidate)
                owner = aliases.get(alias)
                if owner is not None and owner != name:
                    raise ModelCatalogError(
                        f"model alias {alias!r} is used by both {owner!r} and {name!r}"
                    )
                aliases[alias] = name

        for name, profile in tuple(normalized.items()):
            fallbacks: list[str] = []
            for candidate in profile.fallback_profiles:
                alias = _normalize_profile_name(candidate)
                resolved = aliases.get(alias)
                if resolved is None:
                    raise ModelCatalogError(
                        f"model profile {name!r} references unknown fallback {alias!r}"
                    )
                if resolved != name and resolved not in fallbacks:
                    fallbacks.append(resolved)
            if profile.fallback_profiles != tuple(fallbacks):
                normalized[name] = replace(
                    profile,
                    fallback_profiles=tuple(fallbacks),
                )

        selected_default = _normalize_profile_name(default_profile)
        resolved_default = aliases.get(selected_default)
        if resolved_default is None:
            raise ModelCatalogError(
                f"default model profile {selected_default!r} does not exist"
            )

        self._profiles = MappingProxyType(normalized)
        self._aliases = MappingProxyType(aliases)
        self.default_name = resolved_default
        self.legacy_fallback = bool(legacy_fallback)

    @property
    def profiles(self) -> tuple[ModelProfile, ...]:
        return tuple(self._profiles.values())

    @property
    def default(self) -> ModelProfile:
        return self._profiles[self.default_name]

    def resolve(self, name_or_alias: str) -> ModelProfile:
        candidate = _normalize_profile_name(name_or_alias)
        name = self._aliases.get(candidate)
        if name is None:
            raise ModelCatalogError(f"unknown model profile: {name_or_alias}")
        return self._profiles[name]

    def try_resolve(self, name_or_alias: str | None) -> ModelProfile | None:
        if not name_or_alias or not str(name_or_alias).strip():
            return None
        try:
            return self.resolve(str(name_or_alias))
        except ModelCatalogError:
            return None

    def resolve_preference(self, preference: str | None) -> ModelProfile:
        if not preference or not str(preference).strip():
            return self.default
        selected = self.try_resolve(preference)
        if selected is not None:
            return selected

        model_matches = [
            profile
            for profile in self._profiles.values()
            if profile.model == str(preference).strip()
        ]
        if len(model_matches) == 1:
            return model_matches[0]
        if self.legacy_fallback and str(preference).strip().lower().startswith(
            "deepseek-"
        ):
            return self.default.with_model(str(preference))
        return self.default

    def resolve_runtime(
        self,
        *,
        profile: ModelProfile | str | None = None,
        model: str | None = None,
    ) -> ModelProfile:
        if isinstance(profile, ModelProfile):
            selected = profile
        elif profile:
            selected = self.resolve(profile)
        else:
            selected = self.default
        return selected.with_model(model or "")

    def find_runtime(
        self,
        *,
        profile: str = "",
        provider: str = "",
        model: str = "",
    ) -> ModelProfile | None:
        selected = self.try_resolve(profile)
        if selected is not None and (not model or selected.model == model):
            return selected
        matches = [
            item
            for item in self._profiles.values()
            if (not model or item.model == model)
            and (
                not provider
                or item.provider_identity == provider
                or item.provider == provider
            )
        ]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "ModelCatalog":
        environment = os.environ if environ is None else environ
        raw = str(getattr(settings, "model_profiles_json", "") or "").strip()
        default_name = str(
            getattr(settings, "model_default_profile", "deepseek") or "deepseek"
        ).strip()
        if raw:
            return cls.from_json(
                raw,
                default_profile=default_name,
                environ=environment,
            )

        legacy_model = str(getattr(settings, "deepseek_model", "deepseek-chat"))
        legacy_aliases = ["default", "ds"]
        if legacy_model == "deepseek-v4-flash":
            legacy_aliases.append("flash")
        if legacy_model == "deepseek-v4-pro":
            legacy_aliases.append("pro")
        legacy = ModelProfile(
            name=default_name,
            provider="deepseek",
            protocol="openai-chat",
            model=legacy_model,
            base_url=str(
                getattr(settings, "deepseek_base_url", "https://api.deepseek.com")
            ).rstrip("/"),
            api_key=str(getattr(settings, "deepseek_api_key", "")),
            api_key_env="DEEPSEEK_API_KEY",
            thinking=str(getattr(settings, "deepseek_thinking", "disabled")),
            aliases=tuple(legacy_aliases),
            capabilities=ModelCapabilities.defaults_for("openai-chat"),
        )
        profiles = {legacy.name: legacy}
        for short_name, preset_model in (
            ("flash", "deepseek-v4-flash"),
            ("pro", "deepseek-v4-pro"),
        ):
            preset_name = f"deepseek-{short_name}"
            if preset_model == legacy.model or preset_name in profiles:
                continue
            profiles[preset_name] = replace(
                legacy,
                name=preset_name,
                model=preset_model,
                aliases=(short_name,),
            )
        return cls(
            profiles,
            default_profile=legacy.name,
            legacy_fallback=True,
        )

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        default_profile: str,
        environ: Mapping[str, str] | None = None,
    ) -> "ModelCatalog":
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelCatalogError(
                f"AI_MODEL_PROFILES_JSON is invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise ModelCatalogError("AI_MODEL_PROFILES_JSON must be a JSON object")

        if "profiles" in payload:
            raw_profiles = payload["profiles"]
            embedded_default = payload.get("default") or payload.get(
                "default_profile"
            )
            reserved: set[str] = set()
        else:
            raw_profiles = payload
            embedded_default = payload.get("default_profile")
            reserved = {"default_profile"}
            if isinstance(payload.get("default"), str):
                embedded_default = payload["default"]
                reserved.add("default")
        if not isinstance(raw_profiles, Mapping):
            raise ModelCatalogError("model profiles must be a JSON object")

        profiles: dict[str, ModelProfile] = {}
        environment = os.environ if environ is None else environ
        for raw_name, raw_profile in raw_profiles.items():
            if str(raw_name) in reserved:
                continue
            name = _normalize_profile_name(raw_name)
            if not isinstance(raw_profile, Mapping):
                raise ModelCatalogError(f"model profile {name!r} must be an object")
            profiles[name] = _parse_profile(name, raw_profile, environment)

        return cls(
            profiles,
            default_profile=str(embedded_default or default_profile),
            legacy_fallback=False,
        )


def _parse_profile(
    name: str,
    raw: Mapping[str, object],
    environ: Mapping[str, str],
) -> ModelProfile:
    provider = str(raw.get("provider") or name).strip().lower()
    if not _PROFILE_NAME_PATTERN.fullmatch(provider):
        raise ModelCatalogError(
            f"model profile {name!r} has an invalid provider name"
        )
    protocol = str(
        raw.get("protocol")
        or ("anthropic-messages" if provider == "anthropic" else "openai-chat")
    ).strip().lower()
    if protocol not in SUPPORTED_MODEL_PROTOCOLS:
        raise ModelCatalogError(
            f"model profile {name!r} uses unsupported protocol {protocol!r}"
        )
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ModelCatalogError(f"model profile {name!r} is missing model")

    default_url = (
        "https://api.anthropic.com"
        if protocol == "anthropic-messages"
        else "https://api.openai.com/v1"
    )
    base_url = str(raw.get("base_url") or default_url).strip().rstrip("/")
    _validate_base_url(name, base_url)

    api_key_env = str(raw.get("api_key_env") or "").strip()
    direct_key = str(raw.get("api_key") or "").strip()
    if api_key_env and direct_key:
        raise ModelCatalogError(
            f"model profile {name!r} cannot set both api_key and api_key_env"
        )
    if api_key_env and not _ENV_NAME_PATTERN.fullmatch(api_key_env):
        raise ModelCatalogError(
            f"model profile {name!r} has an invalid api_key_env"
        )
    api_key = str(environ.get(api_key_env, "")).strip() if api_key_env else direct_key

    aliases_raw = raw.get("aliases", ())
    if isinstance(aliases_raw, str):
        aliases = (_normalize_profile_name(aliases_raw),)
    elif isinstance(aliases_raw, (list, tuple)):
        aliases = tuple(_normalize_profile_name(item) for item in aliases_raw)
    else:
        raise ModelCatalogError(f"model profile {name!r} aliases must be an array")

    fallback_raw = raw.get("fallback_profiles", ())
    if isinstance(fallback_raw, str):
        fallback_profiles = (_normalize_profile_name(fallback_raw),)
    elif isinstance(fallback_raw, (list, tuple)):
        fallback_profiles = tuple(
            _normalize_profile_name(item) for item in fallback_raw
        )
    else:
        raise ModelCatalogError(
            f"model profile {name!r} fallback_profiles must be an array"
        )

    timeout_seconds = _bounded_float(
        raw.get("timeout_seconds", 60.0),
        field_name=f"{name}.timeout_seconds",
        minimum=1.0,
        maximum=600.0,
    )
    temperature_raw = raw.get("temperature")
    temperature = (
        None
        if temperature_raw is None
        else _bounded_float(
            temperature_raw,
            field_name=f"{name}.temperature",
            minimum=0.0,
            maximum=1.0 if protocol == "anthropic-messages" else 2.0,
        )
    )
    thinking = str(raw.get("thinking") or "auto").strip().lower()
    if thinking not in {"auto", "enabled", "disabled"}:
        raise ModelCatalogError(
            f"model profile {name!r} thinking must be auto, enabled, or disabled"
        )
    reasoning_effort = str(raw.get("reasoning_effort") or "").strip().lower()
    if reasoning_effort and reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
        raise ModelCatalogError(
            f"model profile {name!r} reasoning_effort must be one of: "
            + ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
        )

    try:
        pricing = ModelPricing.from_mapping(raw.get('pricing'))
    except ValueError as error:
        raise ModelCatalogError(f'{name}.pricing: {error}') from None
    default_output_tokens = 4096 if protocol == "anthropic-messages" else 0
    return ModelProfile(
        name=name,
        provider=provider,
        protocol=protocol,
        model=model,
        base_url=base_url,
        api_key=api_key,
        api_key_env=api_key_env,
        api_key_required=_mapping_bool(raw, "api_key_required", True),
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        max_input_tokens=_nonnegative_int(
            raw.get("max_input_tokens", 0),
            f"{name}.max_input_tokens",
        ),
        max_output_tokens=_nonnegative_int(
            raw.get("max_output_tokens", default_output_tokens),
            f"{name}.max_output_tokens",
        ),
        aliases=aliases,
        fallback_profiles=fallback_profiles,
        circuit_breaker_enabled=_mapping_bool(raw, "circuit_breaker_enabled", True),
        pricing=pricing,
        capabilities=ModelCapabilities.from_mapping(
            protocol,
            raw.get("capabilities"),
        ),
    )


def _normalize_profile_name(value: object) -> str:
    name = str(value).strip().lower()
    if not _PROFILE_NAME_PATTERN.fullmatch(name):
        raise ModelCatalogError(
            f"invalid model profile name {value!r}; use lowercase letters, numbers, '.', '_', or '-'"
        )
    return name


def _validate_base_url(name: str, base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelCatalogError(
            f"model profile {name!r} has an invalid HTTP base_url"
        )


def _mapping_bool(
    raw: Mapping[str, object],
    key: str,
    default: bool,
) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool):
        return value
    raise ModelCatalogError(f"{key} must be true or false")


def _bounded_float(
    value: object,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelCatalogError(f"{field_name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ModelCatalogError(
            f"{field_name} must be between {minimum:g} and {maximum:g}"
        )
    return parsed


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise ModelCatalogError(f"{field_name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ModelCatalogError(f"{field_name} must be an integer") from exc
    if parsed < 0:
        raise ModelCatalogError(f"{field_name} must not be negative")
    return parsed
