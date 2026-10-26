from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit


class InstanceError(ValueError):
    pass


class ConfigurationConflict(InstanceError):
    pass


def _text(value: Any, field: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or len(value) > limit:
        raise InstanceError(f"Invalid {field}")
    value = value.strip()
    if not empty and not value:
        raise InstanceError(f"{field} cannot be empty")
    return value


def _url(value: Any) -> str:
    value = _text(value, "API address", 2048)
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise InstanceError("Invalid API address") from None
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise InstanceError("API address must be HTTP(S), without credentials, query or fragment")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise InstanceError("Invalid API address")
    if port is not None and not 1 <= port <= 65535:
        raise InstanceError("Invalid API port")
    return value.rstrip("/")
