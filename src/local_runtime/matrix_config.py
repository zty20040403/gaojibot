"""Desktop-owned Matrix mappings; no implicit cross-group forwarding."""
from __future__ import annotations

from copy import deepcopy
import json
import re

from .validation import InstanceError, _text, _url


DEFAULTS = {"enabled": False, "base_url": "", "user_id": "", "routes": []}


def validate_matrix(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise InstanceError("Matrix 配置字段不完整或不支持")
    result = deepcopy(value)
    if type(result["enabled"]) is not bool:
        raise InstanceError("Matrix 启用状态需为开关")
    result["base_url"] = _url(result["base_url"]) if result["base_url"] != "" else ""
    user = _text(result["user_id"], "Matrix user ID", 512, empty=True)
    if user and (not re.fullmatch(r"@[^\s:]+:[^\s]+", user) or any(ord(char) < 33 for char in user)):
        raise InstanceError("Matrix 用户 ID 应为 @用户:服务器")
    result["user_id"] = user
    if not isinstance(result["routes"], list) or len(result["routes"]) > 20:
        raise InstanceError("Matrix 最多配置 20 条会话映射")
    rooms, identifiers = set(), set()
    for route in result["routes"]:
        if not isinstance(route, dict) or set(route) != {"id", "qq_group", "room_id"}:
            raise InstanceError("Matrix 会话映射格式不正确")
        if not isinstance(route["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", route["id"]) or route["id"] in identifiers:
            raise InstanceError("Matrix 会话映射编号不正确或重复")
        identifiers.add(route["id"])
        if not isinstance(route["qq_group"], str) or not re.fullmatch(r"[1-9][0-9]{4,14}", route["qq_group"]):
            raise InstanceError("Matrix 对应的 QQ 群号须为数字")
        room = _text(route["room_id"], "Matrix room ID", 512)
        if not room.startswith("!") or len(room) < 2 or any(char.isspace() or ord(char) < 33 for char in room):
            raise InstanceError("Matrix 房间 ID 应以 ! 开头，不是房间名称或邀请链接")
        if room in rooms:
            raise InstanceError("同一个 Matrix 房间不能关联多个 QQ 群")
        rooms.add(room)
        route["room_id"] = room
    return result


def missing_requirement(settings: dict, key: str) -> str:
    options = settings["components"]["matrix"]
    if not options["base_url"] or not options["user_id"] or not key:
        return "先填写 Matrix 服务地址、用户 ID 和 Access Token"
    if not options["routes"]:
        return "先添加 Matrix 与 QQ 群的会话映射"
    if not settings["allow_all_groups"] and any(route["qq_group"] not in settings["group_ids"] for route in options["routes"]):
        return "Matrix 映射的 QQ 群必须在已启用的群列表中"
    return ""


def environment(settings: dict, key: str, *, configured: bool) -> dict[str, str]:
    options = settings["components"]["matrix"]
    enabled = configured and options["enabled"]
    bundles = {}
    if enabled:
        for route in options["routes"]:
            group = route["qq_group"]
            bundle = bundles.setdefault(group, {"name": "desktop-qq-" + group, "endpoints": [
                {"platform": "onebot-v11", "kind": "group", "id": group, "bot_user_id": settings["bot_qq"]},
            ]})
            bundle["endpoints"].append({"platform": "matrix", "kind": "group", "id": route["room_id"], "bot_user_id": options["user_id"]})
    return {"AI_MATRIX_ENABLED": str(enabled).lower(), "AI_MATRIX_HOMESERVER": options["base_url"] if enabled else "",
        "AI_MATRIX_USER_ID": options["user_id"] if enabled else "", "AI_MATRIX_ACCESS_TOKEN": key if enabled else "",
        "AI_MATRIX_APPSERVICE_TOKEN": "", "AI_MATRIX_SYNC_TIMEOUT_MS": "30000", "AI_MATRIX_SYNC_RETRY_SECONDS": "5",
        "AI_MIRROR_ROUTES_JSON": json.dumps(list(bundles.values()), ensure_ascii=False)}
