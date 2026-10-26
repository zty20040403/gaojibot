"""Provider wire options shared by chat requests and desktop connectivity probes."""
from __future__ import annotations


def thinking_body(provider: str, protocol: str, thinking: str) -> dict | None:
    if protocol != "openai-chat":
        return None
    if thinking in {"enabled", "on", "true", "1"}:
        enabled = True
    elif thinking in {"disabled", "off", "false", "0"}:
        enabled = False
    else:
        return None
    if provider == "qwen":
        return {"enable_thinking": enabled}
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}


def chat_options(provider: str, protocol: str, thinking: str, effort: str) -> dict:
    if protocol != "openai-chat":
        return {}
    body = thinking_body(provider, protocol, thinking)
    result = {"extra_body": body} if body is not None else {}
    if provider in {"openai", "cliproxy"} and effort:
        result["reasoning_effort"] = effort
    return result


def anthropic_messages_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized + "/messages"
    if normalized.endswith("/v1/messages"):
        return normalized
    return normalized + "/v1/messages"
