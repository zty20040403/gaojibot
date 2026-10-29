"""Pure card metadata parsing shared by input transports."""
import json
import re
from typing import Any

def extract_card_summary(data: dict[str, Any]) -> tuple[str, str]:
    encoded = data.get("data")
    if not isinstance(encoded, str):
        return "", ""
    try:
        payload = json.loads(encoded)
    except (TypeError, json.JSONDecodeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    title = _find_string(payload, ("title", "prompt", "desc"))
    url = _find_string(
        payload,
        (
            "qqdocurl",
            "qqDocUrl",
            "jumpUrl",
            "jump_url",
            "targetUrl",
            "target_url",
            "shareUrl",
            "share_url",
            "url",
        ),
    )
    return title, _normalize_card_url(url)


def _normalize_card_url(value: str) -> str:
    url = str(value).strip()
    if url.startswith("//"):
        return f"https:{url}"
    if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:[/:?#]|$)", url):
        return f"https://{url}"
    return url


def _find_string(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            candidate = _find_string(child, keys)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for child in value:
            candidate = _find_string(child, keys)
            if candidate:
                return candidate
    return ""
