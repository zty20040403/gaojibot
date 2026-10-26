"""Persist the QQ login handover separately from restored user data."""
from __future__ import annotations

from pathlib import Path
import re

from .files import read_json, write_json
from .setup_clients import SetupError


def read_handover(directory: Path) -> dict | None:
    path = directory / "config/qq-handover.json"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise SetupError("QQ 接管记录无效，未自动启动 QQ")
    try:
        value = read_json(path)
    except (OSError, ValueError):
        raise SetupError("QQ 接管记录无效，未自动启动 QQ") from None
    if (not isinstance(value, dict) or value.get("schema") != 1
            or value.get("state") not in {"pending", "approved"}
            or value.get("target_instance_id") != directory.name
            or any(not re.fullmatch(r"[a-f0-9]{32}", str(value.get(key, "")))
                   for key in ("source_instance_id", "operation_id"))):
        raise SetupError("QQ 接管记录无效，未自动启动 QQ")
    return value


def start_allowed(directory: Path) -> bool:
    value = read_handover(directory)
    return value is None or value["state"] == "approved"


def require_handover(directory: Path, source: str, operation: str) -> None:
    if source == directory.name or any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in (source, operation)):
        raise SetupError("无效的 QQ 迁移来源")
    write_json(directory / "config/qq-handover.json", {"schema": 1, "state": "pending",
        "source_instance_id": source, "target_instance_id": directory.name, "operation_id": operation})


def approve_handover(directory: Path) -> None:
    value = read_handover(directory)
    if value is None:
        raise SetupError("没有等待接管的 QQ，请刷新当前状态")
    write_json(directory / "config/qq-handover.json", {**value, "state": "approved"})


def clear_handover(directory: Path) -> None:
    path = directory / "config/qq-handover.json"
    if read_handover(directory) is not None:
        path.unlink()
        from .maintenance import sync_directory
        sync_directory(path.parent)
