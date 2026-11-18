"""AI image generation via cliproxy (gemini-3.1-flash-image primary, gpt-image-2.5 fallback)."""
from __future__ import annotations

import base64
import inspect
import json
import os
from pathlib import PurePosixPath
import secrets
import time

import httpx

from ..adapters.qq_official.image_formats import detect_image
from ..application.owned_sandbox import OwnedSandboxExecutor, workspace_path
from ..tool_images import ToolImage
from .owned_sandbox import enabled as sandbox_enabled
from .progress import enabled as progress_enabled
from .registry import FunctionTool
from .state import structured_tool_source

GENERATE_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "调用 AI 出图模型（默认 gemini-3.1-flash-image，失败可能回退 gpt-image-2.5；审核或版权限制不回退）根据描述生成图片（梗图、头像、插画等），"
            "保存到本会话容器 /work 下并附给你预览。确认画面无误后调用 send_image 发到当前会话。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "详细的画面描述（风格、主体、构图、文字内容等）。",
                },
                "path": {
                    "type": "string",
                    "description": "可选输出路径（位于 /work 下，例如 /work/generated/meme.png）；不填则自动命名。",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
}


def _app_of(context):
    services = getattr(context, "services", None)
    return getattr(services, "context", services)


def _cliproxy_profile(context):
    app = _app_of(context)
    catalog = getattr(app, "model_catalog", None)
    if catalog is None:
        return None
    for profile in catalog.profiles:
        if profile.provider == "cliproxy" and profile.configured and profile.base_url and profile.api_key:
            return profile
    return None


def enabled(context) -> bool:
    return sandbox_enabled(context) and _cliproxy_profile(context) is not None


def _decode_data_url_or_b64(raw_url: str) -> bytes:
    if raw_url.startswith("data:"):
        _, _, payload = raw_url.partition(",")
        return base64.b64decode(payload)
    return base64.b64decode(raw_url)


# Only fixed vocabulary is returned: arbitrary provider text can contain the request or credentials.
_RESTRICTED = (
    "content_policy_violation", "content_filter", "content policy", "safety system",
    "safety filter", "safety policy", "moderation", "copyright", "prohibited_content",
    "image_safety", "image_recitation", "recitation", "内容审核", "内容安全", "版权",
)
_CODES = frozenset({
    "content_policy_violation", "content_filter", "moderation_blocked", "safety",
    "image_safety", "prohibited_content", "recitation", "image_recitation",
    "copyright_violation", "responsibleaipolicyviolation", "insufficient_quota",
    "quota_exceeded", "resource_exhausted", "rate_limit_exceeded", "invalid_request_error",
})
_EXCERPTS = _RESTRICTED + (
    "quota exceeded", "insufficient quota", "exceeded your current quota",
    "rate limit exceeded", "high demand", "额度不足", "配额不足",
)


class ImageProviderError(RuntimeError):
    def __init__(self, response: httpx.Response, route: str, *, finish_reason=None):
        try:
            body = response.json()
        except ValueError:
            body = None
        error = {"code": finish_reason} if finish_reason else body.get("error") if isinstance(body, dict) else None
        fields = error if isinstance(error, dict) else {"message": error}
        code = fields.get("code")
        if isinstance(code, str) and code.casefold() in _CODES:
            code = code.casefold()
        elif type(code) is not int or not 100 <= code <= 599:
            code = next((value.casefold() for value in (fields.get("type"), fields.get("status"))
                         if isinstance(value, str) and value.casefold() in _CODES), None)
        texts = [fields.get(key) for key in ("code", "type", "status", "message")]
        text = " ".join(value.casefold() for value in texts if isinstance(value, str))
        self.restricted = any(phrase in text for phrase in _RESTRICTED) or any(
            value in {"safety", "recitation", "responsibleaipolicyviolation"} for value in text.split()
        )
        message = fields.get("message")
        message = message.casefold() if isinstance(message, str) else ""
        excerpt = next((phrase for phrase in _EXCERPTS if phrase in message), None)
        details = [f"{route}_http_{response.status_code}"]
        if code is not None:
            details.append(f"code={code}")
        if excerpt:
            details.append(f"message={excerpt}")
        super().__init__(", ".join(details))


async def _generate_via_chat(client: httpx.AsyncClient, base_url: str, api_key: str, model: str, prompt: str):
    resp = await client.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}]},
        timeout=90.0,
    )
    if resp.status_code >= 400:
        raise ImageProviderError(resp, "chat_image")
    data = resp.json()
    choices = data.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    reason = choice.get('finish_reason')
    if isinstance(reason, str) and reason.casefold() in {'content_filter', 'safety', 'image_safety', 'recitation', 'image_recitation'}:
        raise ImageProviderError(resp, 'chat_image', finish_reason=reason)
    msg = choice.get("message")
    images = (msg.get("images") if isinstance(msg, dict) else None) or []
    if not images:
        raise RuntimeError("chat_image_empty")
    first = images[0]
    url = ""
    if isinstance(first, dict):
        img_obj = first.get("image_url")
        if isinstance(img_obj, dict):
            url = str(img_obj.get("url") or "")
        elif isinstance(first.get("b64_json"), str):
            url = first["b64_json"]
    if not url:
        raise RuntimeError("chat_image_missing_payload")
    raw = _decode_data_url_or_b64(url)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return raw, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


async def _generate_via_images_api(client: httpx.AsyncClient, base_url: str, api_key: str, model: str, prompt: str):
    resp = await client.post(
        f"{base_url.rstrip('/')}/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "prompt": prompt, "n": 1},
        timeout=90.0,
    )
    if resp.status_code >= 400:
        raise ImageProviderError(resp, "images_api")
    data = resp.json()
    items = data.get("data") or []
    b64 = items[0].get("b64_json") if items and isinstance(items[0], dict) else None
    if not isinstance(b64, str) or not b64:
        raise RuntimeError("images_api_empty")
    raw = base64.b64decode(b64)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return raw, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _ext_for_bytes(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8"):
        return ".jpg"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return ".gif"
    return ".png"


async def execute(turn, arguments):
    if not enabled(turn.context):
        return json.dumps({"ok": False, "code": "unavailable", "error": "当前环境未配置出图服务或工作区沙盒。"}, ensure_ascii=False)
    prompt = str((arguments or {}).get("prompt") or "").strip()
    if not prompt or len(prompt) > 4000:
        return json.dumps({"ok": False, "code": "invalid_prompt", "error": "请提供 1–4000 字的画面描述。"}, ensure_ascii=False)

    raw_path = str((arguments or {}).get("path") or "").strip()
    if raw_path:
        try:
            rel_path = workspace_path(raw_path)
        except Exception:
            return json.dumps({"ok": False, "code": "invalid_path", "error": "path 必须是 /work 下的合法相对路径。"}, ensure_ascii=False)
    else:
        rel_path = f"generated/img_{int(time.time())}_{secrets.token_hex(3)}.png"

    profile = _cliproxy_profile(turn.context)
    assert profile is not None
    primary_model = os.environ.get("AI_IMAGE_GEN_MODEL", "gemini-3.1-flash-image").strip() or "gemini-3.1-flash-image"
    fallback_model = os.environ.get("AI_IMAGE_GEN_FALLBACK_MODEL", "gpt-image-2.5").strip() or "gpt-image-2.5"

    used_model = primary_model
    in_tokens = out_tokens = 0
    raw_bytes: bytes | None = None
    last_err = ""

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        try:
            raw_bytes, in_tokens, out_tokens = await _generate_via_chat(
                client, profile.base_url, profile.api_key, primary_model, prompt
            )
        except Exception as err1:
            last_err = str(err1) if isinstance(err1, ImageProviderError) else "chat_image_error"
            if fallback_model and not (isinstance(err1, ImageProviderError) and err1.restricted):
                if progress_enabled(turn.context):
                    await turn.context.platform.progress.say("出图服务失败，我再尝试备用服务。")
                used_model = fallback_model
                try:
                    raw_bytes, in_tokens, out_tokens = await _generate_via_images_api(
                        client, profile.base_url, profile.api_key, fallback_model, prompt
                    )
                except Exception as err2:
                    detail = str(err2) if isinstance(err2, ImageProviderError) else "images_api_error"
                    last_err = f"{last_err}; fallback={detail}"

    if not raw_bytes:
        return json.dumps({"ok": False, "code": "generation_failed", "error": f"出图失败（{last_err or 'empty'}）。"}, ensure_ascii=False)

    ext = _ext_for_bytes(raw_bytes)
    p_obj = PurePosixPath(rel_path)
    if p_obj.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif"} or (
        ext == ".png" and p_obj.suffix.lower() != ".png"
    ) or (ext == ".jpg" and p_obj.suffix.lower() not in {".jpg", ".jpeg"}):
        rel_path = str(p_obj.with_suffix(ext))

    mime = detect_image(raw_bytes, PurePosixPath(rel_path).name)
    executor: OwnedSandboxExecutor = turn.context.agent_executor
    sid = await executor._ensure_workspace()
    write_fn = executor.sandbox_manager.write_file
    if "allow_large" in inspect.signature(write_fn).parameters:
        await executor._call(write_fn, executor.owner, sid, rel_path, raw_bytes, allow_large=True)
    else:
        await executor._call(write_fn, executor.owner, sid, rel_path, raw_bytes)

    app = _app_of(turn.context)
    source = structured_tool_source(turn.context)
    usage_store = getattr(app, "usage_store", None)
    db_call = getattr(app, "db_call", None)
    if usage_store is not None and source is not None and callable(db_call):
        await db_call(
            usage_store.record,
            scope_key=source.scope.key,
            source="image_gen",
            provider="cliproxy",
            model=used_model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
        )

    b64 = base64.b64encode(raw_bytes).decode("ascii")
    meta = json.dumps(
        {
            "ok": True,
            "path": f"/work/{rel_path}",
            "model": used_model,
            "mime": mime,
            "bytes": len(raw_bytes),
            "hint": "图片已保存到容器，确认无误后调用 send_image 发送到当前会话。",
        },
        ensure_ascii=False,
    )
    return ToolImage(meta, f"data:{mime};base64,{b64}")


HANDLERS = (FunctionTool(GENERATE_IMAGE_TOOL, enabled, execute, platform_neutral=True),)
