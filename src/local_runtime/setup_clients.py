"""Bounded setup probes. Provider and QQ credentials never enter diagnostics."""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import time
import threading
from urllib.parse import quote, urlencode, urlsplit

from .instances import InstanceError, _model
from .components import ComponentError, component_keys, validate_components
from src.llm_options import anthropic_messages_url, chat_options


class SetupError(ValueError):
    pass


def request_json(url: str, payload: dict | None, headers: dict | None = None, *, timeout: float = 20, missing_matrix_event: bool = False) -> dict | None:
    target = urlsplit(url)
    if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password:
        raise SetupError("接口地址无效")
    connection_type = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(target.hostname, target.port, timeout=timeout)
    try:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        path = (target.path or "/") + ("?" + target.query if target.query else "")
        connection.request("POST" if payload is not None else "GET", path, body=body,
                           headers={"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        if response.status == 404 and missing_matrix_event:
            content = response.read(8193)
            if len(content) <= 8192:
                value = json.loads(content)
                if isinstance(value, dict) and value.get("errcode") == "M_NOT_FOUND":
                    return None
            raise SetupError("Matrix 房间状态无法确认")
        if response.status != 200:
            descriptions = {401: "密钥不正确或已失效", 403: "此密钥没有访问权限", 404: "接口地址或模型名称不正确",
                            429: "服务限流或额度不足，请稍后再试"}
            raise SetupError(descriptions.get(response.status, f"接口暂时不可用（HTTP {response.status}）"))
        content = response.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise SetupError("接口返回的数据过大，已停止读取")
        value = json.loads(content)
        if not isinstance(value, dict):
            raise SetupError("接口没有返回正确的数据格式")
        return value
    except (OSError, http.client.HTTPException):
        raise SetupError("服务器连不上此接口，或等待超时；请检查地址和服务器网络") from None
    except (ValueError, UnicodeError) as error:
        if isinstance(error, SetupError):
            raise
        raise SetupError("接口返回的数据无法读取") from None
    finally:
        connection.close()


def probe_model(model: dict, key: str, *, listing: bool = False) -> dict:
    profile = _model(model)
    if not isinstance(key, str) or any(character in key for character in "\r\n\0") or len(key) > 8192:
        raise InstanceError("Invalid API key")
    if profile["api_key_required"] and not key:
        raise SetupError("请先填写 API Key")
    if listing and profile["protocol"] != "openai-chat":
        raise SetupError("此接口暂不支持自动列出模型，请填写服务商提供的模型名称")
    headers = {"Authorization": "Bearer " + key} if key else {}
    base = profile["base_url"]
    started = time.monotonic()
    if listing:
        result = request_json(base + "/models", None, headers)
        models = result.get("data", [])
        if not isinstance(models, list):
            raise SetupError("接口没有返回模型列表，请手动填写模型名称")
        return {"models": sorted({item["id"] for item in models if isinstance(item, dict)
                and isinstance(item.get("id"), str) and 0 < len(item["id"]) <= 200})[:200]}
    messages = [{"role": "user", "content": "Reply with OK."}]
    if profile["protocol"] == "anthropic-messages":
        payload = {"model": profile["model"], "max_tokens": profile["max_output_tokens"] or 4096, "messages": messages}
        if profile["thinking"] == "enabled":
            payload["thinking"] = {"type": "adaptive"}
        result = request_json(anthropic_messages_url(base), payload,
                              {"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=45)
        content = result.get("content", [])
        answered = any(isinstance(item, dict) and item.get("type") == "text" and item.get("text") for item in content) if isinstance(content, list) else False
    else:
        options = chat_options(profile["provider"], profile["protocol"], profile["thinking"], profile["reasoning_effort"])
        payload = {"model": profile["model"], "messages": messages, "stream": False, **options.pop("extra_body", {}), **options}
        if profile["max_output_tokens"]:
            payload["max_tokens"] = profile["max_output_tokens"]
        result = request_json(base + "/chat/completions", payload, headers, timeout=45)
        choices = result.get("choices", [])
        message = choices[0].get("message", {}) if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        answered = bool(message.get("content")) if isinstance(message, dict) else False
    if not answered:
        raise SetupError("接口连通了，但模型没有返回文字；请检查模型名称和推理设置")
    return {"responded": True, "latency_ms": round((time.monotonic() - started) * 1000)}


def probe_embedding(options: dict, key: str) -> dict:
    options = validate_components({"embedding": options})["embedding"]
    key = component_keys({"embedding": key})["embedding"]
    if not options["base_url"]:
        raise ComponentError("请先填写 Embedding 服务地址")
    if options["api_key_required"] and not key:
        raise ComponentError("请先填写 Embedding 密钥")
    started = time.monotonic()
    result = request_json(options["base_url"] + "/embeddings",
        {"model": options["model"], "input": ["connection check"], "dimensions": options["dimensions"]},
        {"Authorization": "Bearer " + key} if key else {}, timeout=30)
    data = result.get("data")
    vector = data[0].get("embedding") if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
    if not isinstance(vector, list) or not vector or any(type(number) not in {int, float} or not math.isfinite(number) for number in vector):
        raise SetupError("Embedding 接口没有返回有效的浮点向量")
    if len(vector) != options["dimensions"]:
        raise SetupError(f"Embedding 返回 {len(vector)} 维，与配置的 {options['dimensions']} 维不一致")
    return {"responded": True, "dimensions": len(vector), "latency_ms": round((time.monotonic() - started) * 1000)}


def probe_cluster(options: dict, key: str) -> dict:
    options = validate_components({"cluster": options})["cluster"]
    key = component_keys({"cluster": key})["cluster"]
    if not options["base_url"] or not key:
        raise ComponentError("请先填写集群接口地址和 Token")
    started = time.monotonic()
    result = request_json(options["base_url"] + "/v1/capabilities", None,
        {"Authorization": "Bearer " + key}, timeout=options["timeout_seconds"])
    capabilities = result.get("capabilities")
    if (result.get("backend") != "ops" or type(result.get("catalog_version")) is not int
        or result["catalog_version"] < 0 or type(result.get("checked_at")) is not int
        or not isinstance(capabilities, list) or len(capabilities) > 1000
        or "error" not in result or result["error"] is not None and not isinstance(result["error"], dict)):
        raise SetupError("此地址不是兼容的 Gaoji Cluster Control 接口；不能直接填写 MaxOps 地址")
    for item in capabilities:
        if (not isinstance(item, dict) or type(item.get("available")) is not bool or type(item.get("read_only")) is not bool
            or not isinstance(item.get("name"), str) or not item["name"]
            or not isinstance(item.get("operation"), str) or not item["operation"]):
            raise SetupError("集群接口的能力目录格式不兼容")
    available = sum(item["available"] for item in capabilities)
    # Gateway authentication and upstream readiness are different facts. Never
    # echo provider error text, operation names, hosts or credentials to the App.
    return {"responded": True, "available": available, "unavailable": len(capabilities) - available,
        "upstream_ready": result["error"] is None and available > 0,
        "catalog_version": result["catalog_version"], "latency_ms": round((time.monotonic() - started) * 1000)}


def probe_matrix(options: dict, key: str) -> dict:
    options = validate_components({"matrix": options})["matrix"]
    key = component_keys({"matrix": key})["matrix"]
    if not options["base_url"] or not options["user_id"] or not key or not options["routes"]:
        raise ComponentError("先填写 Matrix 服务地址、用户 ID、Token 和会话映射")
    started = time.monotonic()
    def get(path: str, *, missing_event: bool = False):
        remaining = 40 - (time.monotonic() - started)
        if remaining <= 0:
            raise SetupError("Matrix 检测超时，请稍后重试")
        return request_json(options["base_url"] + "/_matrix/client/v3" + path, None,
            {"Authorization": "Bearer " + key}, timeout=min(8, remaining), missing_matrix_event=missing_event)
    identity = get("/account/whoami")
    if identity.get("user_id") != options["user_id"] or identity.get("is_guest") is True:
        raise SetupError("Matrix Token 对应的账户与填写的用户 ID 不一致，或为访客账户")
    joined = get("/joined_rooms").get("joined_rooms")
    if not isinstance(joined, list) or any(not isinstance(room, str) for room in joined):
        raise SetupError("Matrix 没有返回有效的已加入房间列表")
    for index, route in enumerate(options["routes"], 1):
        if route["room_id"] not in joined:
            raise SetupError(f"Matrix 账户尚未加入第 {index} 条映射的房间")
        room = quote(route["room_id"], safe="")
        encryption = get(f"/rooms/{room}/state/m.room.encryption/", missing_event=True)
        if encryption is not None:
            raise SetupError(f"第 {index} 个 Matrix 房间已启用端到端加密，当前桥接尚不支持解密")
        power = get(f"/rooms/{room}/state/m.room.power_levels/", missing_event=True) or {}
        users, events = power.get("users", {}), power.get("events", {})
        if not isinstance(users, dict) or not isinstance(events, dict):
            raise SetupError("Matrix 房间权限格式不正确")
        level = users.get(options["user_id"], power.get("users_default", 0))
        needed = events.get("m.room.message", power.get("events_default", 0))
        if type(level) is not int or type(needed) is not int:
            raise SetupError("Matrix 房间权限格式不正确")
        if level < needed:
            raise SetupError(f"Matrix 账户没有第 {index} 个房间的发言权限")
    return {"responded": True, "rooms": len(options["routes"]), "latency_ms": round((time.monotonic() - started) * 1000)}


def probe_imessage(options: dict, key: str) -> dict:
    options = validate_components({"imessage": options})["imessage"]
    key = component_keys({"imessage": key})["imessage"]
    if not options["base_url"] or not options["bot_handle"] or not key or not options["routes"]:
        raise ComponentError("先填写 BlueBubbles 地址、密码、自身标识和会话映射")
    started = time.monotonic()
    def get(path, query=None):
        remaining = 40 - (time.monotonic() - started)
        if remaining <= 0:
            raise SetupError("iMessage 检测超时，请稍后重试")
        result = request_json(options["base_url"] + "/api/v1" + path + "?" + urlencode({**(query or {}), "password": key}),
            None, timeout=min(8, remaining))
        if result.get("status", 200) != 200 or result.get("error"):
            raise SetupError("BlueBubbles 没有确认请求成功")
        return result
    for index, route in enumerate(options["routes"], 1):
        path = "/chat/" + quote(route["chat_guid"], safe="")
        chat = get(path).get("data")
        if not isinstance(chat, dict) or chat.get("guid") != route["chat_guid"]:
            raise SetupError(f"BlueBubbles 未确认第 {index} 条会话映射")
        now = int(time.time() * 1000)
        result = get(path + "/message", {"after": now, "before": now, "limit": 1, "offset": 0, "sort": "ASC"})
        data, metadata = result.get("data"), result.get("metadata")
        if (not isinstance(data, list) or len(data) > 1 or not isinstance(metadata, dict)
                or type(metadata.get("total")) is not int or metadata["total"] < len(data)):
            raise SetupError("BlueBubbles 消息读取接口不兼容")
    return {"responded": True, "chats": len(options["routes"]), "send_verified": False,
        "latency_ms": round((time.monotonic() - started) * 1000)}


class NapCatSetup:
    def __init__(self, port: int, token: str):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.credential = ""
        self.expires = 0.0
        self.lock = threading.RLock()

    def call(self, endpoint: str, payload: dict | None = None):
        with self.lock:
            return self._call(endpoint, payload)

    def _call(self, endpoint: str, payload: dict | None = None):
        if self.expires <= time.monotonic():
            result = request_json(self.base + "/api/auth/login", {"hash": hashlib.sha256((self.token + ".napcat").encode()).hexdigest()})
            data = result.get("data")
            if result.get("code") != 0 or not isinstance(data, dict) or data.get("require2FA") or not data.get("Credential"):
                raise SetupError("QQ 登录组件需要重新验证，请检查其登录设置")
            self.credential = data["Credential"]
            self.expires = time.monotonic() + 3000
        result = request_json(self.base + "/api/" + endpoint, payload or {}, {"Authorization": "Bearer " + self.credential})
        if result.get("code") != 0:
            self.expires = 0
            raise SetupError("QQ 登录组件暂未就绪，请稍后刷新；不会清除已有登录信息")
        return result.get("data")

    def status(self) -> dict:
        result = self.call("QQLogin/CheckLoginStatus")
        if not isinstance(result, dict):
            raise SetupError("QQ 状态暂不可读")
        online = result.get("isLogin") is True
        identity = self.call("Debug/call", {"action": "get_login_info", "params": {}}) if online else {}
        if isinstance(identity, dict) and "retcode" in identity:
            identity = identity.get("data", {}) if identity.get("retcode") == 0 else {}
        identity = identity if isinstance(identity, dict) else {}
        qr = result.get("qrcodeurl", "")
        if not isinstance(qr, str) or len(qr) > 4096:
            qr = ""
        return {"online": online, "qq": str(identity.get("user_id", "")), "nickname": str(identity.get("nickname", ""))[:120],
                "phase": str(result.get("loginPhase", ""))[:80], "qrcode": "" if online else qr,
                "offline": result.get("isOffline") is True, "has_error": bool(result.get("loginError"))}

    def refresh(self) -> dict:
        self.call("QQLogin/RefreshQRcode")
        return self.status()

    def configure(self, qq: str, token: str) -> None:
        current = self.status()
        if not current["online"] or current["qq"] != qq:
            raise SetupError("登录的 QQ 与这次配置不一致，请重新确认")
        configuration = self.call("OB11Config/GetConfig")
        if not isinstance(configuration, dict):
            raise SetupError("QQ 消息连接配置暂不可读")
        # This instance is created exclusively for the desktop owner. Replace its
        # network adapters so no accidental listener or stale destination remains.
        configuration["network"] = {"httpServers": [], "httpClients": [], "httpSseServers": [], "websocketServers": [],
            "websocketClients": [{"name": "gaoji-desktop", "enable": True, "url": "ws://bot:8080/onebot/v11/ws",
                "messagePostFormat": "array", "reportSelfMessage": False, "reconnectInterval": 5000,
                "token": token, "debug": False, "heartInterval": 30000}]}
        self.call("OB11Config/SetConfig", {"config": json.dumps(configuration)})
        self.call("QQLogin/SetQuickLoginQQ", {"uin": qq})
