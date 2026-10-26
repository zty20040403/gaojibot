from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import time
from typing import Any
import uuid

from .files import file_lock, read_json, write_json
from .validation import ConfigurationConflict, InstanceError, _text, _url
from .components import component_keys, validate_components


SCHEMA_VERSION = 1
INSTANCE_ID = re.compile(r"^[a-f0-9]{32}$")
PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
FEATURE_DEFAULTS = {
    "search": True, "media": False, "voice": False, "video": False,
    "browser": False, "sandbox": False, "subagents": True,
    "historian": False, "semantic": False, "proactive": False,
    "notifications": False, "cluster": False,
}
NEUTRAL_PROMPT = "你是高级，认真理解当前聊天内容，回答自然、准确。需要工具时实际调用，不能假装已经完成任务。"


def default_data_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise InstanceError("Windows user data directory is unavailable")
        return Path(base) / "Gaoji"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Gaoji"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "gaoji"


def _qq(value: Any, field: str, *, empty: bool = False) -> str:
    if value == "" and empty:
        return ""
    result = _text(value, field, 20)
    if not result.isascii() or not result.isdigit() or not 1000 <= int(result) < 2**63:
        raise InstanceError(f"Invalid {field}")
    return result


def _model(value: Any) -> dict:
    allowed = {"id", "label", "provider", "protocol", "base_url", "model", "api_key_required", "tools", "vision", "thinking", "reasoning_effort", "max_input_tokens", "max_output_tokens", "fallbacks"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise InstanceError("Unsupported model fields; provide API keys separately")
    result = deepcopy(value)
    identifier = result.get("id")
    if not isinstance(identifier, str) or not PROFILE_ID.fullmatch(identifier):
        raise InstanceError("Invalid model profile ID")
    result["label"] = _text(result.get("label", identifier), "model label", 100)
    provider = result.get("provider", "custom")
    if not isinstance(provider, str) or not PROFILE_ID.fullmatch(provider):
        raise InstanceError("Invalid model provider")
    result["provider"] = provider
    result["model"] = _text(result.get("model"), "model name", 200)
    result["base_url"] = _url(result.get("base_url"))
    result["protocol"] = result.get("protocol", "openai-chat")
    if result["protocol"] not in {"openai-chat", "anthropic-messages"}:
        raise InstanceError("Unsupported model protocol")
    for flag, default in (("api_key_required", True), ("tools", True), ("vision", False)):
        result[flag] = result.get(flag, default)
        if type(result[flag]) is not bool:
            raise InstanceError(f"{flag} must be a boolean")
    result["reasoning_effort"] = result.get("reasoning_effort", "")
    if result["reasoning_effort"] not in {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise InstanceError("Unsupported reasoning effort")
    result["thinking"] = result.get("thinking", "auto")
    if result["thinking"] not in {"auto", "enabled", "disabled"}:
        raise InstanceError("Unsupported thinking mode")
    for field in ("max_input_tokens", "max_output_tokens"):
        result[field] = result.get(field, 0)
        if type(result[field]) is not int or not 0 <= result[field] <= 10_000_000:
            raise InstanceError(f"Invalid {field}")
    fallbacks = result.get("fallbacks", [])
    if not isinstance(fallbacks, list) or not all(isinstance(item, str) for item in fallbacks):
        raise InstanceError("Invalid fallback models")
    result["fallbacks"] = list(dict.fromkeys(fallbacks))
    return result


def validate_settings(value: dict) -> dict:
    required = {"name", "bot_qq", "admin_qq", "group_ids", "allow_all_groups", "system_prompt", "models", "default_model", "vision_model", "features"}
    if not isinstance(value, dict) or set(value) - {"components"} != required:
        raise InstanceError("Invalid instance settings fields")
    result = deepcopy(value)
    result["name"] = _text(result["name"], "instance name", 80)
    result["bot_qq"] = _qq(result["bot_qq"], "bot QQ", empty=True)
    result["admin_qq"] = _qq(result["admin_qq"], "administrator QQ", empty=True)
    result["system_prompt"] = _text(result["system_prompt"], "system prompt", 16000)
    if type(result["allow_all_groups"]) is not bool:
        raise InstanceError("allow_all_groups must be a boolean")
    if not isinstance(result["group_ids"], list) or len(result["group_ids"]) > 1000:
        raise InstanceError("Invalid group selection")
    result["group_ids"] = sorted({_qq(group, "group QQ") for group in result["group_ids"]}, key=int)
    if not isinstance(result["models"], list) or len(result["models"]) > 100:
        raise InstanceError("Invalid model catalog")
    result["models"] = [_model(model) for model in result["models"]]
    ids = {model["id"] for model in result["models"]}
    if len(ids) != len(result["models"]):
        raise InstanceError("Model profile IDs must be unique")
    for field in ("default_model", "vision_model"):
        if not isinstance(result[field], str) or result[field] and result[field] not in ids:
            raise InstanceError(f"{field} must refer to a configured model")
    for model in result["models"]:
        if any(item not in ids or item == model["id"] for item in model["fallbacks"]):
            raise InstanceError("Fallback must refer to another configured model")
    graph = {model["id"]: model["fallbacks"] for model in result["models"]}
    visited: set[str] = set()
    visiting: set[str] = set()
    def visit(identifier: str) -> None:
        if identifier in visiting:
            raise InstanceError("Model fallback chain must not contain a cycle")
        if identifier in visited:
            return
        visiting.add(identifier)
        for child in graph[identifier]:
            visit(child)
        visiting.remove(identifier)
        visited.add(identifier)
    for identifier in graph:
        visit(identifier)
    if result["vision_model"] and not next(model["vision"] for model in result["models"] if model["id"] == result["vision_model"]):
        raise InstanceError("Vision model must have vision capability enabled")
    if not isinstance(result["features"], dict) or set(result["features"]) != set(FEATURE_DEFAULTS):
        raise InstanceError("Invalid feature selection")
    if any(type(flag) is not bool for flag in result["features"].values()):
        raise InstanceError("Feature settings must be booleans")
    result["components"] = validate_components(result.get("components", {}))
    if result["components"]["historian"]["profile"] not in ids | {""}:
        raise InstanceError("Historian model must refer to a configured profile")
    return result


class InstanceStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or default_data_root()).expanduser().absolute()
        self.instances_root = self.root / "instances"

    def directory(self, instance_id: str) -> Path:
        if not isinstance(instance_id, str) or not INSTANCE_ID.fullmatch(instance_id):
            raise InstanceError("Invalid instance ID")
        directory = self.instances_root / instance_id
        if self.instances_root.is_symlink() or directory.is_symlink():
            raise InstanceError("Instance directories must not be symbolic links")
        return directory

    def create(self, name: str = "高级") -> dict:
        name = _text(name, "instance name", 80)
        self.instances_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.instances_root.is_symlink():
            raise InstanceError("Instance directories must not be symbolic links")
        with file_lock(self.instances_root / ".create.lock"):
            instance_id = uuid.uuid4().hex
            directory = self.directory(instance_id)
            directory.mkdir(mode=0o700)
            for child in ("config", "state", "cache", "logs", "database", "napcat", "files", "backups"):
                (directory / child).mkdir(mode=0o700)
            (directory / "assets").mkdir(mode=0o755)
            (directory / "capacity").mkdir(mode=0o755)
            # Reserve distinct candidate ports under the instance creation lock.
            reserved = {port for item in self.list() for port in item["ports"].values()}
            ports = {}
            sockets = []
            try:
                for role in ("database", "bot", "napcat"):
                    for _ in range(100):
                        listener = socket.socket()
                        sockets.append(listener)
                        listener.bind(("127.0.0.1", 0))
                        port = listener.getsockname()[1]
                        if port in reserved:
                            listener.close()
                            continue
                        ports[role] = port
                        reserved.add(port)
                        break
                    else:
                        raise InstanceError("No available local port")
            finally:
                for listener in sockets:
                    listener.close()
            config = {"schema_version": SCHEMA_VERSION, "revision": 1, "id": instance_id,
                "created_at": int(time.time()), "ports": ports,
                "settings": {"name": name, "bot_qq": "", "admin_qq": "", "group_ids": [],
                    "allow_all_groups": False, "system_prompt": NEUTRAL_PROMPT,
                    "models": [], "default_model": "", "vision_model": "", "features": dict(FEATURE_DEFAULTS)}}
            write_json(directory / "config" / "secrets.json", {"onebot_token": secrets.token_urlsafe(32),
                "onebot_secret": secrets.token_urlsafe(32), "napcat_token": secrets.token_urlsafe(32),
                "database_password": secrets.token_urlsafe(32), "model_key_sets": {"1": {}}})
            write_json(directory / "instance.json", config)
            return config

    def _read(self, instance_id: str) -> dict:
        try:
            config = read_json(self.directory(instance_id) / "instance.json")
        except FileNotFoundError:
            raise InstanceError("Instance does not exist") from None
        if config.get("schema_version") != SCHEMA_VERSION or config.get("id") != instance_id:
            raise InstanceError("Unsupported or corrupted instance configuration")
        if type(config.get("revision")) is not int or config["revision"] < 1:
            raise InstanceError("Invalid instance revision")
        ports = config.get("ports")
        if not isinstance(ports, dict) or set(ports) != {"database", "bot", "napcat"} or len(set(ports.values())) != 3:
            raise InstanceError("Invalid instance ports")
        if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports.values()):
            raise InstanceError("Invalid instance port")
        config["settings"] = validate_settings(config.get("settings"))
        return config

    def read(self, instance_id: str) -> dict:
        directory = self.directory(instance_id)
        if not directory.is_dir():
            raise InstanceError("Instance does not exist")
        with file_lock(directory / ".config.lock"):
            return self._read(instance_id)

    def list(self) -> list[dict]:
        if not self.instances_root.is_dir():
            return []
        return [self.read(path.name) for path in sorted(self.instances_root.iterdir())
                if INSTANCE_ID.fullmatch(path.name) and (path / "instance.json").is_file()]

    def secrets(self, instance_id: str) -> dict:
        return read_json(self.directory(instance_id) / "config" / "secrets.json")

    def snapshot(self, instance_id: str) -> tuple[dict, dict]:
        directory = self.directory(instance_id)
        if not directory.is_dir():
            raise InstanceError("Instance does not exist")
        with file_lock(directory / ".config.lock"):
            config, private = self._read(instance_id), self.secrets(instance_id)
            private["model_keys"] = self._model_keys(config, private)
            private["component_keys"] = self._component_keys(config, private)
            return config, private

    @staticmethod
    def _model_keys(config: dict, private: dict) -> dict:
        value = private.get("model_key_sets", {}).get(str(config["revision"]))
        if not isinstance(value, dict):
            raise InstanceError("Credentials do not match the configuration revision")
        return dict(value)

    @staticmethod
    def _component_keys(config: dict, private: dict) -> dict:
        if "component_key_sets" not in private:
            return {}
        return component_keys(private["component_key_sets"].get(str(config["revision"])))

    def update(self, instance_id: str, settings: dict, *, expected_revision: int, model_keys: dict | None = None,
               service_keys: dict | None = None, model_key_sources: dict | None = None) -> dict:
        settings = validate_settings(settings)
        supplied = component_keys(service_keys if service_keys is not None else {})
        ids = {model["id"] for model in settings["models"]}
        sources = model_key_sources if model_key_sources is not None else {}
        if not isinstance(sources, dict) or set(sources) - ids or any(not isinstance(value, str) for value in sources.values()):
            raise InstanceError("Invalid model credential references")
        if model_keys is not None:
            if not isinstance(model_keys, dict) or set(model_keys) - ids:
                raise InstanceError("Keys must belong to configured models")
            for key in model_keys.values():
                if not isinstance(key, str) or "\0" in key or "\n" in key or "\r" in key or len(key) > 8192:
                    raise InstanceError("Invalid API key")
        if set(sources) & set(model_keys or {}):
            raise InstanceError("A model cannot supply both a key and a credential reference")
        directory = self.directory(instance_id)
        if not directory.is_dir():
            raise InstanceError("Instance does not exist")
        with file_lock(directory / ".config.lock"):
            config = self._read(instance_id)
            if type(expected_revision) is not int or config["revision"] != expected_revision:
                raise ConfigurationConflict("Configuration changed; reload before saving")
            private = self.secrets(instance_id)
            keys = self._model_keys(config, private)
            old_service_keys = self._component_keys(config, private)
            service_values = {**old_service_keys, **supplied}
            for name, label in (("embedding", "Embedding"), ("cluster", "集群"), ("matrix", "Matrix"), ("imessage", "iMessage")):
                fields = ("base_url", "user_id") if name == "matrix" else ("base_url", "bot_handle") if name == "imessage" else ("base_url",)
                if old_service_keys.get(name) and any(config["settings"]["components"][name][field] != settings["components"][name][field] for field in fields) and name not in supplied:
                    raise InstanceError(f"{label} 地址已更改，请重新填写密钥或明确清除旧密钥")
            old_models = {model["id"]: model for model in config["settings"]["models"]}
            copied_keys = {}
            new_models = {model["id"]: model for model in settings["models"]}
            for target, source in sources.items():
                original = old_models.get(source)
                if not original or not keys.get(source) or any(original[field] != new_models[target][field] for field in ("base_url", "protocol")):
                    raise InstanceError("复用密钥需要相同的 API 地址和接口类型，请重新填写密钥")
                copied_keys[target] = keys[source]
            for model in settings["models"]:
                old = old_models.get(model["id"])
                if old and keys.get(model["id"]) and any(old[field] != model[field] for field in ("base_url", "protocol")):
                    if model["id"] not in (model_keys or {}) and model["id"] not in copied_keys:
                        raise InstanceError("API 地址或接口类型已更改，请重新填写密钥，或明确清除旧密钥")
            if model_keys:
                keys.update({key: value.strip() for key, value in model_keys.items()})
            keys.update(copied_keys)
            # Publish secrets for the next revision first. A crash before the config
            # commit leaves the preceding config and its exact key set usable.
            private["model_key_sets"] = {str(config["revision"]): self._model_keys(config, private),
                str(config["revision"] + 1): {key: value for key, value in keys.items() if key in ids}}
            private["component_key_sets"] = {str(config["revision"]): old_service_keys,
                str(config["revision"] + 1): service_values}
            write_json(directory / "config" / "secrets.json", private)
            config["settings"] = settings
            config["revision"] += 1
            write_json(directory / "instance.json", config)
            return config

    def public_status(self, instance_id: str) -> dict:
        config, private = self.snapshot(instance_id)
        configured = {key for key, value in private["model_keys"].items() if value}
        for model in config["settings"]["models"]:
            model["has_key"] = model["id"] in configured
        config["data_directory"] = str(self.directory(instance_id))
        config["component_keys_present"] = [name for name, value in private["component_keys"].items() if value]
        return config
