"""Desktop-owned component configuration; no arbitrary environment or host paths."""
from __future__ import annotations

from copy import deepcopy
import re

from .validation import InstanceError, _text, _url
from .video_assets import CONTAINER_MODEL_PATH
from . import matrix_config, imessage_config
from src.storage_admission import DEFAULT_POLICY, CapacityError, policy as capacity_policy


DEFAULTS = {
    "search": {"max_results": 5, "timeout_seconds": 10},
    "voice": {"name": "zh-CN-XiaoxiaoNeural", "rate": "+0%", "pitch": "+0Hz", "max_chars": 350},
    "embedding": {"base_url": "", "model": "bge-m3", "dimensions": 1024, "api_key_required": False},
    "cluster": {"base_url": "", "timeout_seconds": 12, "allowed_groups": [], "log_allowed_groups": []},
    "matrix": matrix_config.DEFAULTS,
    "imessage": imessage_config.DEFAULTS,
    "capacity": DEFAULT_POLICY,
    "historian": {"profile": ""},
    "alerts": {"base_url": "", "group_id": ""},
    "video": {"max_duration_minutes": 60, "max_download_mb": 512, "frame_count": 8, "threads": 2, "timeout_seconds": 1800},
}


class ComponentError(InstanceError):
    pass


def component_keys(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - {"embedding", "cluster", "matrix", "imessage"} or any(
        not isinstance(key, str) or len(key) > 8192 or any(char in key for char in "\0\r\n") for key in value.values()
    ):
        raise ComponentError("组件密钥格式不正确")
    result = {name: key.strip() for name, key in value.items()}
    token = result.get("cluster", "")
    if token and (not 32 <= len(token) <= 512 or any(not 33 <= ord(char) <= 126 for char in token)):
        raise ComponentError("集群 Token 必须是 32～512 位可见 ASCII 字符")
    if any(not 33 <= ord(char) <= 126 for char in result.get("matrix", "")):
        raise ComponentError("Matrix Access Token 需为可见 ASCII 字符")
    return result


def _integer(value, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ComponentError(f"{name}必须在 {minimum}～{maximum} 之间")


def validate_components(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ComponentError("不支持的组件配置")
    result = deepcopy(DEFAULTS)
    for name, options in value.items():
        if not isinstance(options, dict) or set(options) != set(DEFAULTS[name]):
            raise ComponentError("组件配置字段不完整或不支持")
        result[name] = deepcopy(options)
    _integer(result["search"]["max_results"], "搜索结果数", 1, 20)
    _integer(result["search"]["timeout_seconds"], "搜索超时秒数", 3, 60)
    voice = result["voice"]
    voice["name"] = _text(voice["name"], "voice name", 100)
    if not re.fullmatch(r"[a-zA-Z0-9-]+Neural", voice["name"]):
        raise ComponentError("请填写 Edge TTS 的 Neural 音色名称")
    for field, suffix in (("rate", "%"), ("pitch", "Hz")):
        value = voice[field]
        if not isinstance(value, str) or not re.fullmatch(r"[+-]\d{1,3}" + suffix, value) or not -100 <= int(value[:-len(suffix)]) <= 100:
            raise ComponentError("语速和音调需使用 -100 到 +100 的有符号数值")
    _integer(voice["max_chars"], "语音字数", 50, 1000)
    embedding = result["embedding"]
    embedding["base_url"] = _url(embedding["base_url"]) if embedding["base_url"] != "" else ""
    embedding["model"] = _text(embedding["model"], "embedding model", 200)
    _integer(embedding["dimensions"], "向量维数", 8, 8192)
    if embedding["dimensions"] != 1024:
        raise ComponentError("当前数据库使用 1024 维向量，请选择输出 1024 维的 Embedding 模型")
    if type(embedding["api_key_required"]) is not bool:
        raise ComponentError("Embedding 密钥要求需为开关")
    cluster = result["cluster"]
    cluster["base_url"] = _url(cluster["base_url"]) if cluster["base_url"] != "" else ""
    _integer(cluster["timeout_seconds"], "集群超时秒数", 1, 30)
    for field in ("allowed_groups", "log_allowed_groups"):
        groups = cluster[field]
        if not isinstance(groups, list) or len(groups) > 100 or any(
            not isinstance(group, str) or not re.fullmatch(r"[1-9][0-9]{4,14}", group) for group in groups
        ):
            raise ComponentError("集群群号须为数字列表，最多 100 个群")
        cluster[field] = list(dict.fromkeys(groups))
    if not set(cluster["log_allowed_groups"]) <= set(cluster["allowed_groups"]):
        raise ComponentError("能查看集群日志的群必须同时允许集群查询")
    result["matrix"] = matrix_config.validate_matrix(result["matrix"])
    result["imessage"] = imessage_config.validate_imessage(result["imessage"])
    try:
        result["capacity"] = capacity_policy(result["capacity"])
    except CapacityError as error:
        raise ComponentError(str(error)) from None
    result["historian"]["profile"] = _text(result["historian"]["profile"], "historian profile", 48, empty=True)
    alerts = result["alerts"]
    alerts["base_url"] = _url(alerts["base_url"]) if alerts["base_url"] != "" else ""
    alerts["group_id"] = _text(alerts["group_id"], "alert group", 15, empty=True)
    if alerts["group_id"] and not re.fullmatch(r"[1-9][0-9]{4,14}", alerts["group_id"]):
        raise ComponentError("告警群号格式不正确")
    video = result["video"]
    for name, label, minimum, maximum in (("max_duration_minutes", "视频时长分钟", 1, 60), ("max_download_mb", "视频下载 MiB", 10, 1024),
        ("frame_count", "视频关键帧数", 4, 12), ("threads", "转写线程数", 1, 8), ("timeout_seconds", "视频超时秒数", 60, 1800)):
        _integer(video[name], label, minimum, maximum)
    return result


def requirements(settings: dict, keys: dict, *, sandbox_available: bool = False) -> dict[str, str]:
    options = settings["components"]
    models = {model["id"]: model for model in settings["models"]}
    vision = models.get(settings["vision_model"] or settings["default_model"], {})
    missing = {"sandbox": "沙盒运行镜像尚未安装并验证"}
    if sandbox_available:
        missing.pop("sandbox")
    if not vision.get("vision"):
        missing["media"] = "先添加并选择支持识图的模型"
    if not settings["features"]["media"] or not vision.get("vision"):
        missing["video"] = "视频分析需要先开启识图与表情库，并选择支持识图的模型"
    embedding = options["embedding"]
    if not embedding["base_url"]:
        missing["semantic"] = "先填写 Embedding 服务地址"
    elif embedding["api_key_required"] and not keys.get("embedding"):
        missing["semantic"] = "先填写 Embedding 密钥"
    cluster = options["cluster"]
    if not cluster["base_url"]:
        missing["cluster"] = "先填写集群网关地址（Gaoji Cluster Control）"
    elif not keys.get("cluster"):
        missing["cluster"] = "先填写集群 Token"
    elif not settings["allow_all_groups"] and not set(cluster["allowed_groups"]) <= set(settings["group_ids"]):
        missing["cluster"] = "允许集群查询的群必须在已启用的群列表中"
    matrix_missing = matrix_config.missing_requirement(settings, keys.get("matrix", ""))
    if matrix_missing:
        missing["matrix"] = matrix_missing
    imessage_missing = imessage_config.missing_requirement(settings, keys.get("imessage", ""))
    if imessage_missing:
        missing["imessage"] = imessage_missing
    alerts = options["alerts"]
    if not alerts["base_url"] or not alerts["group_id"]:
        missing["notifications"] = "先填写 Alertmanager 地址与告警群号"
    elif not settings["allow_all_groups"] and alerts["group_id"] not in settings["group_ids"]:
        missing["notifications"] = "告警群必须在已启用的群列表中"
    return missing


def environment(settings: dict, keys: dict, *, configured: bool, sandbox_available: bool = False) -> dict[str, str]:
    if configured:
        missing = requirements(settings, keys, sandbox_available=sandbox_available)
        for name, enabled in settings["features"].items():
            if enabled and name in missing:
                raise ComponentError(missing[name])
        if settings["components"]["matrix"]["enabled"] and "matrix" in missing:
            raise ComponentError(missing["matrix"])
        if settings["components"]["imessage"]["enabled"] and "imessage" in missing:
            raise ComponentError(missing["imessage"])
    options = settings["components"]
    embedding, voice = options["embedding"], options["voice"]
    video = options["video"]
    cluster = options["cluster"]
    cluster_enabled = configured and settings["features"]["cluster"]
    return {
        **imessage_config.environment(settings, keys.get("imessage", ""), configured=configured,
            matrix_environment=matrix_config.environment(settings, keys.get("matrix", ""), configured=configured)),
        "AI_SEARCH_MAX_RESULTS": str(options["search"]["max_results"]),
        "AI_SEARCH_TIMEOUT_SECONDS": str(options["search"]["timeout_seconds"]),
        "AI_VOICE_PROVIDER": "edge", "AI_VOICE_NAME": voice["name"], "AI_VOICE_RATE": voice["rate"],
        "AI_VOICE_PITCH": voice["pitch"], "AI_VOICE_MAX_CHARS": str(voice["max_chars"]),
        "AI_EMBEDDING_BASE_URL": embedding["base_url"], "AI_EMBEDDING_MODEL": embedding["model"],
        "AI_EMBEDDING_DIMENSIONS": str(embedding["dimensions"]), "AI_EMBEDDING_API_KEY": keys.get("embedding", ""),
        "AI_CLUSTER_ENABLED": str(cluster_enabled).lower(),
        "AI_CLUSTER_CONTROL_URL": cluster["base_url"] if cluster_enabled else "",
        "AI_CLUSTER_CONTROL_TIMEOUT_SECONDS": str(cluster["timeout_seconds"]),
        "AI_FLEET_ALLOWED_GROUPS": ",".join(cluster["allowed_groups"]) if cluster_enabled else "",
        "AI_FLEET_LOG_ALLOWED_GROUPS": ",".join(cluster["log_allowed_groups"]) if cluster_enabled else "",
        # The container entrypoint consumes this value without exporting it to child processes.
        "AI_DESKTOP_CLUSTER_TOKEN": keys.get("cluster", "") if cluster_enabled else "",
        "AI_HISTORIAN_PROFILE": options["historian"]["profile"] or settings["default_model"],
        "AI_ALERTMANAGER_URL": options["alerts"]["base_url"], "AI_ALERT_NOTIFY_GROUP_ID": options["alerts"]["group_id"] or "0",
        "AI_VISION_AUTO_DESCRIBE": "false",
        "AI_VIDEO_WHISPER_MODEL_PATH": CONTAINER_MODEL_PATH,
        "AI_VIDEO_MAX_DURATION_MINUTES": str(video["max_duration_minutes"]), "AI_VIDEO_MAX_DOWNLOAD_MB": str(video["max_download_mb"]),
        "AI_VIDEO_FRAME_COUNT": str(video["frame_count"]), "AI_VIDEO_WHISPER_THREADS": str(video["threads"]),
        "AI_VIDEO_TIMEOUT_SECONDS": str(video["timeout_seconds"]),
    }
