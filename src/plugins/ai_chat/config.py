from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from collections.abc import Mapping
from functools import partial

if TYPE_CHECKING:
    from .conversation_scope import ConversationScope


def _text_file(environ: Mapping[str, str], name: str) -> str:
    path = environ.get(name, "").strip()
    if not path:
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def _get_int(name: str, default: int, *, environ: Mapping[str, str] | None = None) -> int:
    value = (os.environ if environ is None else environ).get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_float(name: str, default: float, *, environ: Mapping[str, str] | None = None) -> float:
    value = (os.environ if environ is None else environ).get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_resource_limit(name: str, default: int, minimum: int, maximum: int, *, environ: Mapping[str, str] | None = None) -> int:
    raw = (os.environ if environ is None else environ).get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _get_bool(name: str, default: bool, *, environ: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if environ is None else environ).get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on", "enabled"}


def _get_group_ids(name: str, *, environ: Mapping[str, str] | None = None) -> set[int]:
    raw = (os.environ if environ is None else environ).get(name, "").strip()
    if not raw:
        return set()

    group_ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            group_ids.add(int(item))
        except ValueError:
            continue
    return group_ids


def _get_csv(name: str, *, environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in (os.environ if environ is None else environ).get(name, "").split(",")
        if item.strip()
    )


def _get_group_model_profiles(name: str, *, environ: Mapping[str, str] | None = None) -> dict[int, str]:
    raw = (os.environ if environ is None else environ).get(name, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must be a JSON object")

    profiles: dict[int, str] = {}
    for raw_group_id, raw_profile in payload.items():
        try:
            group_id = int(str(raw_group_id).strip())
        except ValueError as exc:
            raise ValueError(
                f"{name} contains an invalid QQ group id: {raw_group_id!r}"
            ) from exc
        profile = str(raw_profile).strip()
        if group_id <= 0 or not profile:
            raise ValueError(
                f"{name} requires positive group ids and non-empty profile names"
            )
        profiles[group_id] = profile
    return profiles


@dataclass(frozen=True)
class Settings:
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    deepseek_thinking: str
    model_default_profile: str
    model_simple_chat_profile: str
    local_model_profile: str
    local_model_probe_interval_seconds: int
    local_model_probe_timeout_seconds: int
    qwen_control_url: str
    qwen_control_token: str
    model_profiles_json: str
    model_fallback_enabled: bool
    model_circuit_failure_threshold: int
    model_circuit_cooldown_seconds: int
    model_circuit_long_cooldown_seconds: int
    group_model_profiles: dict[int, str]
    system_prompt: str
    style_examples: str
    ledger_enabled: bool
    turn_journal_enabled: bool
    turn_archive_ttl_days: int
    turn_archive_max_per_scope: int
    turn_archive_max_bytes: int
    turn_event_max_chars: int
    turn_expand_max_chars: int
    max_input_chars: int
    max_reply_chars: int
    stream_enabled: bool
    tool_max_rounds: int
    tool_simple_max_rounds: int
    tool_max_calls_per_round: int
    tool_max_total_calls: int
    tool_max_result_chars: int
    tool_max_context_chars: int
    search_enabled: bool
    ops_read_base_url: str
    ops_read_token_file: str
    search_auto_enabled: bool
    search_max_results: int
    search_timeout_seconds: int
    media_enabled: bool
    media_root: str
    vision_profile: str
    vision_auto_describe: bool
    vision_cache_seconds: int
    vision_cache_entries: int
    media_max_source_bytes: int
    media_max_vision_bytes: int
    media_prepare_threshold_bytes: int
    media_max_edge_pixels: int
    media_timeout_seconds: int
    media_max_attempts: int
    media_lease_seconds: int
    media_batch_size: int
    media_worker_concurrency: int
    video_whisper_model_path: str
    video_whisper_threads: int
    archive_enabled: bool
    archive_root: str
    archive_media_retention_days: int
    archive_delivery_retention_days: int
    archive_delivery_min_bytes: int
    archive_interval_seconds: int
    archive_batch_size: int
    outbox_enabled: bool
    outbox_lease_seconds: int
    outbox_max_attempts: int
    durable_jobs_enabled: bool
    durable_job_poll_seconds: float
    durable_job_lease_seconds: int
    durable_job_max_attempts: int
    durable_job_concurrency: int
    quota_enabled: bool
    quota_daily_calls: int
    quota_daily_input_tokens: int
    quota_daily_output_tokens: int
    postgres_schema: str
    postgres_pool_min_size: int
    postgres_pool_max_size: int
    postgres_pool_timeout_seconds: int
    postgres_health_check_interval_seconds: float
    postgres_node_names: tuple[str, ...]
    semantic_enabled: bool
    postgres_dsn: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    embedding_dimensions: int
    embedding_timeout_seconds: int
    dream_profile: str
    admin_enabled: bool
    admin_secret_file: str
    admin_origin: str
    admin_bot_id: str
    admin_path: str
    admin_user_ids: set[int]
    observability_enabled: bool
    metrics_path: str
    otel_service_name: str
    otel_exporter_otlp_endpoint: str
    sandbox_enabled: bool
    sandbox_backend: str
    sandbox_image: str
    sandbox_vm_image: str
    sandbox_vm_root: str
    sandbox_nix_cache_volume: str
    sandbox_allowed_users: set[int]
    sandbox_max_per_user: int
    sandbox_max_total: int
    sandbox_timeout_seconds: int
    sandbox_max_file_bytes: int
    enabled_groups: set[int]
    disabled_groups: set[int]
    groups_require_allowlist: bool = False
    sandbox_allow_all: bool = False
    sandbox_cpus: int = 2
    sandbox_pids_limit: int = 256
    sandbox_memory_mib: int = 8192
    sandbox_network: str = "isolated"
    search_provider: str = "scrape"
    search_api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        environment = dict(os.environ if environ is None else environ)
        integer = partial(_get_int, environ=environment)
        floating = partial(_get_float, environ=environment)
        resource_limit = partial(_get_resource_limit, environ=environment)
        boolean = partial(_get_bool, environ=environment)
        group_ids = partial(_get_group_ids, environ=environment)
        csv = partial(_get_csv, environ=environment)
        group_models = partial(_get_group_model_profiles, environ=environment)
        sandbox_max_file_mb = integer("AI_SANDBOX_MAX_FILE_MB", 20)
        intent_timeout = floating("AI_TOOL_INTENT_TIMEOUT_SECONDS", 3.0)
        intent_timeout = min(max(intent_timeout, 0.1), 10.0) if math.isfinite(intent_timeout) else 3.0
        search_provider = environment.get("AI_SEARCH_PROVIDER", "").strip().lower() or "scrape"
        if search_provider not in {"scrape", "tavily", "bocha"}:
            raise ValueError("AI_SEARCH_PROVIDER must be scrape, tavily or bocha")
        return cls(
            deepseek_api_key=environment.get("DEEPSEEK_API_KEY", "").strip(),
            deepseek_base_url=environment.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            deepseek_model=environment.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
            deepseek_thinking=environment.get("DEEPSEEK_THINKING", "disabled").strip().lower(),
            model_default_profile=(
                environment.get("AI_MODEL_DEFAULT_PROFILE", "deepseek").strip()
                or "deepseek"
            ),
            model_simple_chat_profile=environment.get(
                "AI_SIMPLE_CHAT_PROFILE",
                "",
            ).strip(),
            model_profiles_json=environment.get("AI_MODEL_PROFILES_JSON", "").strip(),
            local_model_profile=environment.get("AI_LOCAL_MODEL_PROFILE", "qwen-local").strip(),
            local_model_probe_interval_seconds=max(integer("AI_LOCAL_MODEL_PROBE_INTERVAL_SECONDS", 15), 1),
            local_model_probe_timeout_seconds=min(max(integer("AI_LOCAL_MODEL_PROBE_TIMEOUT_SECONDS", 2), 1), 10),
            qwen_control_url=environment.get("AI_QWEN_CONTROL_URL", "").strip(),
            qwen_control_token=environment.get("AI_QWEN_CONTROL_TOKEN", "").strip(),
            model_fallback_enabled=boolean("AI_MODEL_FALLBACK_ENABLED", True),
            model_circuit_failure_threshold=min(
                max(integer("AI_MODEL_CIRCUIT_FAILURE_THRESHOLD", 2), 1),
                10,
            ),
            model_circuit_cooldown_seconds=min(
                max(integer("AI_MODEL_CIRCUIT_COOLDOWN_SECONDS", 120), 10),
                3600,
            ),
            model_circuit_long_cooldown_seconds=min(
                max(
                    integer(
                        "AI_MODEL_CIRCUIT_LONG_COOLDOWN_SECONDS",
                        3600,
                    ),
                    60,
                ),
                86400,
            ),
            group_model_profiles=group_models(
                "AI_GROUP_MODEL_PROFILES_JSON"
            ),
            # Empty means the built-in persona in prompt.py. Files win over the
            # variable: a long persona and real chat lines stay off the public repo.
            system_prompt=_text_file(environment, "AI_SYSTEM_PROMPT_FILE")
            or environment.get("AI_SYSTEM_PROMPT", "").strip(),
            style_examples=_text_file(environment, "AI_STYLE_EXAMPLES_FILE"),
            ledger_enabled=boolean("AI_LEDGER_ENABLED", True),
            turn_journal_enabled=boolean("AI_TURN_JOURNAL_ENABLED", True),
            turn_archive_ttl_days=min(
                max(integer("AI_TURN_ARCHIVE_TTL_DAYS", 14), 0), 90
            ),
            turn_archive_max_per_scope=min(
                max(integer("AI_TURN_ARCHIVE_MAX_PER_SCOPE", 50), 0),
                500,
            ),
            turn_archive_max_bytes=min(
                max(integer("AI_TURN_ARCHIVE_MAX_KB", 512), 64),
                4096,
            )
            * 1024,
            turn_event_max_chars=min(
                max(integer("AI_TURN_EVENT_MAX_CHARS", 12000), 1000),
                100000,
            ),
            turn_expand_max_chars=min(
                max(integer("AI_TURN_EXPAND_MAX_CHARS", 10000), 1000),
                50000,
            ),
            max_input_chars=integer("AI_MAX_INPUT_CHARS", 1500),
            max_reply_chars=integer("AI_MAX_REPLY_CHARS", 3000),
            stream_enabled=boolean("AI_STREAM_ENABLED", True),
            tool_max_rounds=min(
                max(integer("AI_TOOL_MAX_ROUNDS", 30), 1), 100
            ),
            tool_simple_max_rounds=min(
                max(integer("AI_TOOL_SIMPLE_MAX_ROUNDS", 5), 1), 20
            ),
            tool_max_calls_per_round=min(
                max(integer("AI_TOOL_MAX_CALLS_PER_ROUND", 4), 1), 20
            ),
            tool_max_total_calls=min(
                max(integer("AI_TOOL_MAX_TOTAL_CALLS", 60), 1),
                200,
            ),
            tool_max_result_chars=min(
                max(integer("AI_TOOL_MAX_RESULT_CHARS", 12000), 1000),
                100000,
            ),
            tool_max_context_chars=min(
                max(integer("AI_TOOL_MAX_CONTEXT_CHARS", 60000), 5000),
                500000,
            ),
            search_enabled=boolean("AI_SEARCH_ENABLED", True),
            ops_read_base_url=environment.get('AI_OPS_READ_BASE_URL', '').strip(),
            ops_read_token_file=environment.get('AI_OPS_READ_TOKEN_FILE', '').strip(),
            search_auto_enabled=boolean("AI_SEARCH_AUTO_ENABLED", True),
            search_max_results=integer("AI_SEARCH_MAX_RESULTS", 5),
            search_timeout_seconds=integer("AI_SEARCH_TIMEOUT_SECONDS", 10),
            search_provider=search_provider,
            search_api_key=environment.get("AI_SEARCH_API_KEY", "").strip(),
            media_enabled=boolean("AI_MEDIA_ENABLED", False),
            media_root=environment.get("AI_MEDIA_ROOT", "").strip(),
            vision_profile=(
                environment.get("AI_VISION_PROFILE", "gpt-5.6-luna").strip()
                or "gpt-5.6-luna"
            ),
            vision_auto_describe=boolean(
                "AI_VISION_AUTO_DESCRIBE",
                False,
            ),
            vision_cache_seconds=max(
                integer("AI_VISION_CACHE_SECONDS", 600),
                0,
            ),
            vision_cache_entries=min(
                max(integer("AI_VISION_CACHE_ENTRIES", 256), 1),
                2048,
            ),
            media_max_source_bytes=max(
                integer("AI_MEDIA_MAX_SOURCE_MB", 100), 1
            )
            * 1024
            * 1024,
            media_max_vision_bytes=max(
                integer("AI_VISION_MAX_IMAGE_MB", 20), 1
            )
            * 1024
            * 1024,
            media_prepare_threshold_bytes=max(
                integer("AI_MEDIA_PREPARE_THRESHOLD_MB", 1), 0
            )
            * 1024
            * 1024,
            media_max_edge_pixels=min(
                max(integer("AI_MEDIA_MAX_EDGE_PX", 1568), 256), 4096
            ),
            media_timeout_seconds=min(
                max(integer("AI_VISION_TIMEOUT_SECONDS", 180), 5), 600
            ),
            media_max_attempts=min(
                max(integer("AI_MEDIA_MAX_ATTEMPTS", 5), 1), 20
            ),
            media_lease_seconds=min(
                max(integer("AI_MEDIA_LEASE_SECONDS", 240), 30), 1800
            ),
            media_batch_size=min(
                max(integer("AI_MEDIA_BATCH_SIZE", 4), 1), 20
            ),
            media_worker_concurrency=min(
                max(integer("AI_MEDIA_WORKER_CONCURRENCY", 2), 1), 8
            ),
            video_whisper_model_path=environment.get(
                "AI_VIDEO_WHISPER_MODEL_PATH", ""
            ).strip(),
            video_whisper_threads=min(
                max(integer("AI_VIDEO_WHISPER_THREADS", 8), 1), 32
            ),
            archive_enabled=boolean("AI_ARCHIVE_ENABLED", False),
            archive_root=environment.get("AI_ARCHIVE_ROOT", "").strip(),
            archive_media_retention_days=max(
                integer("AI_ARCHIVE_MEDIA_RETENTION_DAYS", 30), 1
            ),
            archive_delivery_retention_days=max(
                integer("AI_ARCHIVE_DELIVERY_RETENTION_DAYS", 7), 1
            ),
            archive_delivery_min_bytes=max(
                integer("AI_ARCHIVE_DELIVERY_MIN_MB", 1), 1
            )
            * 1024
            * 1024,
            archive_interval_seconds=max(
                integer("AI_ARCHIVE_INTERVAL_SECONDS", 60), 15
            ),
            archive_batch_size=min(
                max(integer("AI_ARCHIVE_BATCH_SIZE", 20), 1), 200
            ),
            outbox_enabled=boolean("AI_OUTBOX_ENABLED", True),
            outbox_lease_seconds=max(
                integer("AI_OUTBOX_LEASE_SECONDS", 90),
                10,
            ),
            outbox_max_attempts=min(
                max(integer("AI_OUTBOX_MAX_ATTEMPTS", 5), 1),
                50,
            ),
            durable_jobs_enabled=boolean("AI_DURABLE_JOBS_ENABLED", True),
            durable_job_poll_seconds=max(
                floating("AI_DURABLE_JOB_POLL_SECONDS", 2.0),
                0.1,
            ),
            durable_job_lease_seconds=min(
                max(integer("AI_DURABLE_JOB_LEASE_SECONDS", 300), 10),
                3600,
            ),
            durable_job_max_attempts=min(
                max(integer("AI_DURABLE_JOB_MAX_ATTEMPTS", 3), 1),
                50,
            ),
            durable_job_concurrency=min(
                max(integer("AI_DURABLE_JOB_CONCURRENCY", 2), 1),
                32,
            ),
            quota_enabled=boolean("AI_QUOTA_ENABLED", True),
            quota_daily_calls=max(
                integer("AI_QUOTA_DAILY_CALLS", 0),
                0,
            ),
            quota_daily_input_tokens=max(
                integer("AI_QUOTA_DAILY_INPUT_TOKENS", 0),
                0,
            ),
            quota_daily_output_tokens=max(
                integer("AI_QUOTA_DAILY_OUTPUT_TOKENS", 0),
                0,
            ),
            postgres_schema=(
                environment.get("AI_POSTGRES_SCHEMA", "qq_bot").strip()
                or "qq_bot"
            ),
            postgres_pool_min_size=min(
                max(integer("AI_POSTGRES_POOL_MIN_SIZE", 1), 1),
                20,
            ),
            postgres_pool_max_size=min(
                max(integer("AI_POSTGRES_POOL_MAX_SIZE", 24), 1),
                100,
            ),
            postgres_pool_timeout_seconds=max(
                integer("AI_POSTGRES_POOL_TIMEOUT_SECONDS", 10),
                1,
            ),
            postgres_health_check_interval_seconds=max(
                floating("AI_POSTGRES_HEALTH_CHECK_INTERVAL_SECONDS", 5.0),
                0.5,
            ),
            postgres_node_names=csv("AI_POSTGRES_NODE_NAMES"),
            semantic_enabled=boolean("AI_SEMANTIC_ENABLED", False),
            postgres_dsn=environment.get("AI_POSTGRES_DSN", "").strip(),
            embedding_base_url=environment.get(
                "AI_EMBEDDING_BASE_URL",
                "http://127.0.0.1:11434/v1",
            ).strip(),
            embedding_api_key=environment.get(
                "AI_EMBEDDING_API_KEY",
                "",
            ).strip(),
            embedding_model=environment.get(
                "AI_EMBEDDING_MODEL",
                "bge-m3",
            ).strip(),
            embedding_dimensions=min(
                max(integer("AI_EMBEDDING_DIMENSIONS", 1024), 8),
                8192,
            ),
            embedding_timeout_seconds=max(
                integer("AI_EMBEDDING_TIMEOUT_SECONDS", 30),
                5,
            ),
            dream_profile=environment.get("AI_DREAM_PROFILE", "").strip(),
            admin_enabled=boolean("AI_ADMIN_ENABLED", False),
            admin_secret_file=environment.get("AI_ADMIN_SECRET_FILE", "").strip(),
            admin_origin=environment.get("AI_ADMIN_ORIGIN", "").strip().rstrip("/"),
            admin_bot_id=environment.get("AI_ADMIN_BOT_ID", "").strip(),
            admin_path=(
                environment.get("AI_ADMIN_PATH", "/bot-admin").strip()
                or "/bot-admin"
            ),
            admin_user_ids=group_ids("AI_ADMIN_USER_IDS"),
            observability_enabled=boolean("AI_OBSERVABILITY_ENABLED", True),
            metrics_path=(
                environment.get("AI_METRICS_PATH", "/metrics").strip() or "/metrics"
            ),
            otel_service_name=(
                environment.get("OTEL_SERVICE_NAME", "gaoji").strip()
                or "gaoji"
            ),
            otel_exporter_otlp_endpoint=environment.get(
                "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                "",
            ).strip(),
            sandbox_enabled=boolean("AI_SANDBOX_ENABLED", False),
            sandbox_allow_all=boolean("AI_SANDBOX_ALLOW_ALL", False),
            sandbox_cpus=resource_limit("AI_SANDBOX_CPUS", 2, 1, 16),
            sandbox_pids_limit=resource_limit("AI_SANDBOX_PIDS_LIMIT", 256, 32, 4096),
            sandbox_memory_mib=resource_limit("AI_SANDBOX_MEMORY_MIB", 8192, 256, 8192),
            sandbox_network=environment.get("AI_SANDBOX_NETWORK", "isolated").strip().lower(),
            sandbox_backend=environment.get("AI_SANDBOX_BACKEND", "oci").strip().lower(),
            sandbox_image=environment.get("AI_SANDBOX_IMAGE", "").strip(),
            sandbox_vm_image=environment.get("AI_SANDBOX_VM_IMAGE", "").strip(),
            sandbox_vm_root=environment.get("AI_SANDBOX_VM_ROOT", "").strip(),
            sandbox_nix_cache_volume=(
                environment.get("AI_SANDBOX_NIX_CACHE_VOLUME", "gaoji-nix-v2").strip()
                or "gaoji-nix-v2"
            ),
            sandbox_allowed_users=group_ids(
                "AI_SANDBOX_ALLOWED_USERS"
            ),
            sandbox_max_per_user=min(
                max(integer("AI_SANDBOX_MAX_PER_USER", 2), 1), 5
            ),
            sandbox_max_total=min(
                max(integer("AI_SANDBOX_MAX_TOTAL", 8), 1), 20
            ),
            sandbox_timeout_seconds=min(
                max(integer("AI_SANDBOX_TIMEOUT_SECONDS", 120), 5), 300
            ),
            sandbox_max_file_bytes=(
                0
                if sandbox_max_file_mb <= 0
                else min(sandbox_max_file_mb, 100) * 1024 * 1024
            ),
            enabled_groups=group_ids("AI_ENABLED_GROUPS"),
            disabled_groups=group_ids("AI_DISABLED_GROUPS"),
            groups_require_allowlist=boolean("AI_GROUPS_REQUIRE_ALLOWLIST", False),
        )

    def is_group_enabled(self, group_id: int) -> bool:
        return group_id not in self.disabled_groups and (
            (not self.enabled_groups and not self.groups_require_allowlist)
            or group_id in self.enabled_groups
        )

    def is_scope_enabled(self, scope: ConversationScope) -> bool:
        """Deployment default only; official admission additionally requires binding."""
        if scope.kind != "group":
            raise ValueError("group settings require a group scope")
        if scope.platform != "onebot-v11":
            return False
        return self.is_group_enabled(int(scope.native_conversation_id))

    def model_profile_for_scope(self, scope: ConversationScope) -> str | None:
        if scope.kind != "group" or scope.platform != "onebot-v11":
            return None
        return self.group_model_profiles.get(int(scope.native_conversation_id))

    def is_sandbox_user_allowed(self, user_id: int) -> bool:
        return self.sandbox_enabled and (
            self.sandbox_allow_all
            or user_id in self.sandbox_allowed_users
        )


settings = Settings.from_env()
