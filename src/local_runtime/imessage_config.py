"""Explicit BlueBubbles chat mappings owned by one desktop-managed instance."""
from __future__ import annotations

from copy import deepcopy
import json
import re

from .validation import InstanceError, _text, _url


DEFAULTS = {"enabled": False, "base_url": "", "bot_handle": "", "routes": []}


def validate_imessage(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise InstanceError("iMessage 配置字段不完整或不支持")
    result = deepcopy(value)
    if type(result["enabled"]) is not bool:
        raise InstanceError("iMessage 启用状态需为开关")
    result["base_url"] = _url(result["base_url"]) if result["base_url"] != "" else ""
    result["bot_handle"] = _text(result["bot_handle"], "iMessage account", 512, empty=True)
    if any(char.isspace() or ord(char) < 33 for char in result["bot_handle"]):
        raise InstanceError("iMessage 自身标识不能包含空白或控制字符")
    if not isinstance(result["routes"], list) or len(result["routes"]) > 20:
        raise InstanceError("iMessage 最多配置 20 条会话映射")
    chats, identifiers = set(), set()
    for route in result["routes"]:
        if not isinstance(route, dict) or set(route) != {"id", "qq_group", "chat_guid"}:
            raise InstanceError("iMessage 会话映射格式不正确")
        if not isinstance(route["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", route["id"]) or route["id"] in identifiers:
            raise InstanceError("iMessage 会话映射编号不正确或重复")
        identifiers.add(route["id"])
        if not isinstance(route["qq_group"], str) or not re.fullmatch(r"[1-9][0-9]{4,14}", route["qq_group"]):
            raise InstanceError("iMessage 对应的 QQ 群号须为数字")
        chat = _text(route["chat_guid"], "BlueBubbles chat GUID", 512)
        if not chat.startswith("iMessage;") or any(char.isspace() or ord(char) < 33 for char in chat):
            raise InstanceError("请填写以 iMessage; 开头的完整会话 GUID，不是联系人姓名")
        if chat in chats:
            raise InstanceError("同一个 iMessage 会话不能关联多个 QQ 群")
        chats.add(chat)
        route["chat_guid"] = chat
    return result


def missing_requirement(settings: dict, key: str) -> str:
    options = settings["components"]["imessage"]
    if not options["base_url"] or not options["bot_handle"] or not key:
        return "先填写自己 Mac 上的 BlueBubbles 地址、密码和 iMessage 自身标识"
    if not options["routes"]:
        return "先添加 iMessage 与 QQ 群的会话映射"
    if not settings["allow_all_groups"] and any(route["qq_group"] not in settings["group_ids"] for route in options["routes"]):
        return "iMessage 映射的 QQ 群必须在已启用的群列表中"
    return ""


def environment(settings: dict, key: str, *, configured: bool, matrix_environment: dict[str, str]) -> dict[str, str]:
    options = settings["components"]["imessage"]
    enabled = configured and options["enabled"]
    # Both transports share a canonical QQ conversation; merging must not emit
    # duplicate QQ endpoints or replace existing Matrix mappings.
    bundles = {bundle["endpoints"][0]["id"]: bundle for bundle in json.loads(matrix_environment["AI_MIRROR_ROUTES_JSON"])}
    if enabled:
        for route in options["routes"]:
            group = route["qq_group"]
            bundle = bundles.setdefault(group, {"name": "desktop-qq-" + group, "endpoints": [
                {"platform": "onebot-v11", "kind": "group", "id": group, "bot_user_id": settings["bot_qq"]},
            ]})
            bundle["endpoints"].append({"platform": "imessage", "kind": "group", "id": route["chat_guid"], "bot_user_id": options["bot_handle"]})
    return {**matrix_environment, "AI_IMESSAGE_ENABLED": str(enabled).lower(),
        "AI_IMESSAGE_BASE_URL": options["base_url"] if enabled else "",
        "AI_IMESSAGE_PASSWORD": key if enabled else "", "AI_IMESSAGE_BOT_HANDLE": options["bot_handle"] if enabled else "",
        "AI_IMESSAGE_WEBHOOK_TOKEN": "", "AI_IMESSAGE_CHAT_GUID": "", "AI_IMESSAGE_SEND_METHOD": "apple-script",
        "AI_IMESSAGE_POLL_ENABLED": str(enabled).lower(), "AI_IMESSAGE_POLL_SECONDS": "5",
        "AI_MIRROR_ROUTES_JSON": json.dumps(list(bundles.values()), ensure_ascii=False)}
