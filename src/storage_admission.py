"""Read-only capacity admission for desktop-managed runtimes, not a hard quota."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import time


GIB = 1024 ** 3
MIB = 1024 ** 2
DEFAULT_POLICY = {"max_data_gib": 100, "reserve_free_gib": 2}
MAX_SAMPLE_AGE = 180


class CapacityError(ValueError):
    pass


def policy(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY):
        raise CapacityError("容量设置格式不正确")
    for key, minimum, maximum in (("max_data_gib", 5, 1048576), ("reserve_free_gib", 1, 1024)):
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise CapacityError("实例数据额度至少 5 GiB，磁盘预留至少 1 GiB")
    return dict(value)


def read_snapshot(filename: Path, instance_id: str, *, now: float | None = None) -> dict:
    try:
        if not isinstance(instance_id, str) or not re.fullmatch(r"[a-f0-9]{32}", instance_id):
            raise ValueError("instance")
        descriptor = os.open(filename, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 65536:
                raise ValueError("file")
            value = json.loads(stream.read(65537))
        if not isinstance(value, dict) or value.get("schema") != 1 or value.get("instance_id") != instance_id:
            raise ValueError("snapshot")
        moment = time.time() if now is None else now
        sampled = value.get("sampled_at")
        if type(sampled) is not int or not -5 <= moment - sampled <= MAX_SAMPLE_AGE:
            raise ValueError("stale")
        policy(value.get("policy"))
        if type(value.get("complete")) is not bool or not isinstance(value.get("disks"), list) or not value["disks"]:
            raise ValueError("measurement")
        for amount in [value.get("used_bytes"), *(item.get("free_bytes") for item in value["disks"] if isinstance(item, dict))]:
            if type(amount) is not int or amount < 0:
                raise ValueError("bytes")
        if any(not isinstance(item, dict) or type(item.get("free_bytes")) is not int for item in value["disks"]):
            raise ValueError("disk")
        return value
    except (OSError, ValueError, TypeError, OverflowError):
        raise CapacityError("容量状态尚未确认或已过期，暂停新的大文件操作；已有文件未删除，请检查 App 存储页") from None


def check_snapshot(value: dict, expected_bytes: int = 64 * MIB) -> None:
    if type(expected_bytes) is not int or expected_bytes < 0:
        raise CapacityError("预计新增文件大小无效")
    if not value["complete"]:
        raise CapacityError("容量统计尚未完整，暂停新的大文件操作；已有文件未删除")
    selected = policy(value["policy"])
    if value["used_bytes"] + expected_bytes > selected["max_data_gib"] * GIB:
        raise CapacityError("本实例的数据额度不足，暂停新的大文件操作；可在 App 调整额度或清理已确认的旧副本")
    if any(disk["free_bytes"] - expected_bytes < selected["reserve_free_gib"] * GIB for disk in value["disks"]):
        raise CapacityError("服务器磁盘已接近预留空间，暂停新的大文件操作；不会删除数据库或未交付文件")


def require_capacity(expected_bytes: int = 64 * MIB, *, filename: Path | None = None, instance_id: str | None = None) -> None:
    if filename is None:
        configured = os.environ.get("AI_APP_CAPACITY_FILE", "")
        if not configured:
            return
        filename = Path(configured)
        instance_id = os.environ.get("AI_APP_INSTANCE_ID", "")
    check_snapshot(read_snapshot(filename, instance_id or ""), expected_bytes)


def require_tool_capacity(name: str, effects: tuple[str, ...]) -> None:
    if name in {"sandbox_destroy", "browser_clear", "browser_close"}:
        return
    # Read-only agents and task delivery may continue; their individual download
    # and sandbox writes still pass the lower-level admission checks.
    if set(effects) & {"write:sandbox", "write:browser"}:
        require_capacity(256 * MIB)
