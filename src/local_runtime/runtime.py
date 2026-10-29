"""Render an isolated server instance without inheriting the developer's environment."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import quote

from .instances import InstanceError, validate_settings
from .official_configuration import platforms,runtime_environment as official_environment,runtime_name
from .components import environment as component_environment
from src.sandbox_runtime.protocol import TOKEN_PATTERN


FEATURE_ENV = {
    "search": "AI_SEARCH_ENABLED", "media": "AI_MEDIA_ENABLED", "voice": "AI_VOICE_ENABLED",
    "video": "AI_VIDEO_DEEP_ENABLED", "browser": "AI_BROWSER_ENABLED", "sandbox": "AI_SANDBOX_ENABLED",
    "subagents": "AI_SUBAGENTS_ENABLED", "historian": "AI_HISTORIAN_ENABLED",
    "semantic": "AI_SEMANTIC_ENABLED", "proactive": "AI_PROACTIVE_ENABLED",
    "notifications": "AI_ALERT_NOTIFY_ENABLED", "cluster": "AI_CLUSTER_ENABLED",
}
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}$")


def runtime_environment(config: dict, private: dict, *, configured: bool = True, sandbox: dict | None = None,
                        sandbox_available: bool = False) -> dict[str, str]:
    settings = validate_settings(config["settings"])
    selected=platforms(settings.get('platforms'))
    if configured and selected["qq_official"] and config.get("official_recovery_required"):
        raise InstanceError("此官方备份缺少原始凭据与 pepper，保持停止；请恢复完整受保护备份")
    if configured and (not settings['default_model'] or selected['onebot'] and (not settings['bot_qq'] or not settings['admin_qq'])):
        raise InstanceError("Complete QQ, administrator and default model setup before starting")
    for key in (("database_password", "onebot_token", "onebot_secret") if selected["onebot"] else ("database_password",)):
        if not isinstance(private.get(key), str) or len(private[key]) < 32:
            raise InstanceError("Instance credentials are missing or invalid")
    result = {
        "ENVIRONMENT": "prod", "HOST": "0.0.0.0", "PORT": "8080", "DRIVER": "~fastapi",
        "AI_STATE_DIR": "/data/state", "AI_CACHE_DIR": "/data/cache", "AI_MEDIA_ROOT": "/data/state/media",
        "AI_POSTGRES_DSN": "postgresql://gaoji:" + quote(private["database_password"], safe="") + "@database:5432/gaoji?connect_timeout=5",
        "AI_POSTGRES_SCHEMA": "qq_bot", "AI_POSTGRES_POOL_MIN_SIZE": "1", "AI_POSTGRES_POOL_MAX_SIZE": "8",
        "AI_ADMIN_ENABLED": "true", "AI_ADMIN_PATH": "/bot-admin",
        "AI_ADMIN_SECRET_FILE": "/run/gaoji/admin.secret", "AI_ADMIN_ORIGIN": "",
        "AI_ADMIN_BOT_ID": settings["bot_qq"], "AI_ADMIN_USER_IDS": settings["admin_qq"],
        "AI_SANDBOX_ALLOWED_USERS": settings["admin_qq"], "AI_SANDBOX_ALLOW_ALL": "false",
        "SUPERUSERS": json.dumps([settings["admin_qq"]] if settings["admin_qq"] else []),
        "ONEBOT_ACCESS_TOKEN": private.get("onebot_token","") if selected["onebot"] else "", "ONEBOT_SECRET": private.get("onebot_secret","") if selected["onebot"] else "",
        "AI_SYSTEM_PROMPT": settings["system_prompt"], "AI_ENABLED_GROUPS": ",".join(settings["group_ids"]),
        "AI_DISABLED_GROUPS": "", "AI_GROUPS_REQUIRE_ALLOWLIST": str(not settings["allow_all_groups"]).lower(),
        "AI_MODEL_DEFAULT_PROFILE": settings["default_model"], "AI_LOCAL_MODEL_PROFILE": "",
        "AI_VISION_PROFILE": settings["vision_model"] or settings["default_model"],
        "AI_SIMPLE_CHAT_PROFILE": "", "AI_MODEL_FALLBACK_ENABLED": "true",
        "AI_RECALL_ROUTER_PROFILE": settings["default_model"],
        "AI_PROACTIVE_CLASSIFIER_PROFILE": settings["default_model"],
        "AI_HISTORIAN_PROFILE": settings["default_model"], "AI_DREAM_PROFILE": settings["default_model"],
        "AI_ARCHIVE_ENABLED": "false", "AI_MATRIX_ENABLED": "false", "AI_IMESSAGE_ENABLED": "false",
        "AI_CODESNAP_ENABLED": "false", "AI_SUBAGENT_ENTRY_ENABLED": str(settings["features"]["subagents"]).lower(),
        "AI_BROWSER_EXECUTABLE_PATH": "/usr/bin/chromium",
        "AI_MEDIA_WORKER_CONCURRENCY": "1", "AI_OBSERVABILITY_ENABLED": "true",
        "AI_APP_CAPACITY_FILE": "/run/gaoji-capacity/status.json", "AI_APP_INSTANCE_ID": config["id"],
    }
    result.update(official_environment(settings,private.get('official_reference'),configured=configured,revision=config['revision']))
    if not selected['onebot']:
        for name in ('AI_ADMIN_BOT_ID','AI_ADMIN_USER_IDS','AI_SANDBOX_ALLOWED_USERS','AI_ENABLED_GROUPS'):result[name]=''
        result['SUPERUSERS']='[]'
        if settings['components']['matrix']['enabled'] or settings['components']['imessage']['enabled']:
            raise InstanceError('OneBot bridges require OneBot to be enabled')
    profiles = {}
    for index, model in enumerate(settings["models"]):
        key = private.get("model_keys", {}).get(model["id"], "")
        if configured and model["api_key_required"] and not key:
            raise InstanceError(f"API key is missing for configured model {model['id']}")
        variable = f"GAOJI_MODEL_KEY_{index}"
        result[variable] = key
        profiles[model["id"]] = {field: model[field] for field in (
            "provider", "protocol", "base_url", "model", "api_key_required", "thinking", "reasoning_effort", "max_input_tokens", "max_output_tokens")}
        profiles[model["id"]].update({"api_key_env": variable, "fallback_profiles": model["fallbacks"],
            "capabilities": {"tools": model["tools"], "vision": model["vision"]}})
    result["AI_MODEL_PROFILES_JSON"] = json.dumps({"profiles": profiles, "default": settings["default_model"]}, ensure_ascii=False)
    result.update({environment: str(settings["features"][feature]).lower() for feature, environment in FEATURE_ENV.items()})
    if sandbox is not None:
        if (set(sandbox) != {"socket", "token"} or sandbox["socket"] != "/run/gaoji-sandbox/runner.sock"
                or not isinstance(sandbox["token"], str) or not TOKEN_PATTERN.fullmatch(sandbox["token"])):
            raise InstanceError("Invalid sandbox connection")
        result.update(AI_SANDBOX_RUNNER_SOCKET=sandbox["socket"], AI_SANDBOX_RUNNER_TOKEN=sandbox["token"],
            AI_SANDBOX_MAX_FILE_MB="20")
    result.update(component_environment(settings, private.get("component_keys", {}), configured=configured,
        sandbox_available=sandbox is not None or sandbox_available))
    if sandbox is None:
        result["AI_SANDBOX_ENABLED"] = "false"
    return result


def compose_document(config: dict, directory: Path, *, runtime_image: str, images: dict, bot_enabled: bool = False,
                     sandbox_enabled: bool = False, qq_enabled: bool = True) -> dict:
    identifier = config["id"]
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise InstanceError("Invalid instance ID")
    if not re.fullmatch(r"gaoji-app-runtime:[a-f0-9]{64}", runtime_image):
        raise InstanceError("Runtime image must use the verified bundle digest tag")
    if any(not _IMAGE.fullmatch(images.get(role, "")) for role in ("postgres", "napcat")):
        raise InstanceError("Runtime dependency images must be pinned by digest")
    ports = config["ports"]
    if any(type(ports.get(role)) is not int or not 1024 <= ports[role] <= 65535 for role in ("bot", "napcat")) or ports["bot"] == ports["napcat"]:
        raise InstanceError("Invalid service ports")
    root = str(directory.absolute())
    if any(ord(char) < 32 for char in root):
        raise InstanceError("Invalid data directory")
    def bind(source: str, target: str, *, readonly: bool = False) -> dict:
        return {"type": "bind", "source": (root + "/" + source).replace("$", "$$"), "target": target,
            "read_only": readonly, "bind": {"create_host_path": False}}
    logging = {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}}
    document = {"name": "gaoji-" + identifier, "services": {
        "database": {"image": images["postgres"], "restart": "unless-stopped", "logging": logging,
            "environment": {"POSTGRES_DB": "gaoji", "POSTGRES_USER": "gaoji", "POSTGRES_PASSWORD_FILE": "/run/secrets/postgres-password"},
            "volumes": [bind("database", "/var/lib/postgresql/data"), bind("config/postgres.password", "/run/secrets/postgres-password", readonly=True)],
            "healthcheck": {"test": ["CMD", "pg_isready", "-U", "gaoji", "-d", "gaoji"], "interval": "5s", "timeout": "3s", "retries": 20},
            "shm_size": "128m", "mem_limit": "1g"},
        "napcat": {"image": images["napcat"], **({} if qq_enabled else {"profiles": ["qq-handover"]}),
            "restart": "unless-stopped" if qq_enabled else "no", "logging": logging,
            "environment": {"TZ": "Asia/Shanghai"},
            "ports": [f"127.0.0.1:{ports['napcat']}:6099"], "mem_limit": "1g",
            "volumes": [bind("napcat/QQ", "/app/.config/QQ"), bind("napcat/config", "/app/napcat/config"), bind("files", "/data/outbox")]},
        "bot": {"image": runtime_image, "pull_policy": "never", **({} if bot_enabled else {"profiles": ["configured"]}), "restart": "unless-stopped", "logging": logging,
            "depends_on": {"database": {"condition": "service_healthy"}}, "init": True,
            "ports": [f"127.0.0.1:{ports['bot']}:8080"], "user": "10001:10001", "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"], "read_only": True, "mem_limit": "2g", "shm_size": "256m",
            "tmpfs": ["/tmp:rw,nosuid,nodev,size=512m,mode=1777"],
            "volumes": [bind("state", "/data/state"), bind("cache", "/data/cache"), bind("files", "/data/outbox"),
                bind("assets", "/opt/gaoji-models", readonly=True),
                bind("capacity", "/run/gaoji-capacity", readonly=True),
                *([bind("sandbox/socket", "/run/gaoji-sandbox", readonly=True)] if sandbox_enabled else []),
                bind("config/runtime.json", "/run/gaoji/runtime.json", readonly=True), bind("config/admin.secret", "/run/gaoji/admin.secret", readonly=True)]},
    }}
    selected=platforms(config.get('settings',{}).get('platforms'))
    if not selected['onebot']:document['services'].pop('napcat')
    reference=config.get('official_credential_ref')
    if selected['qq_official'] and reference:
        name=runtime_name(reference,config['revision'])
        document['services']['bot']['volumes'].append(bind('runtime-config/'+name,'/run/gaoji/'+name,readonly=True))
    return document
